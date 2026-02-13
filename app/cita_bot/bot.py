from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone
from pathlib import Path
from time import time as now_time

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from .checker import check_once, CheckResult, EmptyPageError, ContinueNotFoundError
from .config import load_settings
from .logging_setup import setup_logging
from .storage.db import Database

log = logging.getLogger("cita_bot")

JOB_NAME = "cita_monitor_job"

KEY_INTERVAL = "interval_seconds"
KEY_MONITOR_ENABLED = "monitor_enabled"
KEY_LAST_DIGEST = "last_digest"
KEY_LAST_HAS_SLOTS = "last_has_slots"
KEY_EMPTY_STREAK = "empty_streak"
KEY_COOLDOWN_UNTIL = "cooldown_until_epoch"


# --- UI labels (ReplyKeyboard sends plain text) ---
BTN_STATUS = "📊 Статус"
BTN_HELP = "ℹ️ Помощь"
BTN_SUB = "🔔 Подписаться"
BTN_UNSUB = "🔕 Отписаться"
BTN_START = "🟢 Старт мониторинга"
BTN_STOP = "🔴 Стоп мониторинга"
BTN_INTERVAL = "🕒 Интервал"


def main_keyboard() -> ReplyKeyboardMarkup:
    kb = [
        [KeyboardButton(BTN_STATUS), KeyboardButton(BTN_HELP)],
        [KeyboardButton(BTN_SUB), KeyboardButton(BTN_UNSUB)],
        [KeyboardButton(BTN_START), KeyboardButton(BTN_STOP)],
        [KeyboardButton(BTN_INTERVAL)],
    ]
    return ReplyKeyboardMarkup(kb, resize_keyboard=True, is_persistent=True)


def _fmt_dt(epoch: int) -> str:
    if epoch <= 0:
        return "—"
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


async def _notify_all(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    db: Database = context.application.bot_data["db"]
    subs = await db.alist_subscribers()
    for chat_id in subs:
        try:
            await context.bot.send_message(chat_id=chat_id, text=text, disable_web_page_preview=True)
        except Exception as e:
            log.warning("Failed to notify chat_id=%s: %s", chat_id, e)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled error while processing update=%r", update, exc_info=context.error)


# ---------------- commands ----------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Бот запущен.\n"
        "Команды:\n"
        "/subscribe — подписаться\n"
        "/unsubscribe — отписаться\n"
        "/start_monitor — включить мониторинг\n"
        "/stop_monitor — выключить\n"
        "/status — статус\n"
        "/set_interval <сек> — интервал\n\n"
        "Можно пользоваться кнопками ниже 👇",
        reply_markup=main_keyboard(),
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Кнопки:\n"
        f"- {BTN_STATUS}\n"
        f"- {BTN_SUB} / {BTN_UNSUB}\n"
        f"- {BTN_START} / {BTN_STOP}\n"
        f"- {BTN_INTERVAL}\n\n"
        "Тест: отправь `ping` — отвечу `pong`.",
        reply_markup=main_keyboard(),
    )


async def cmd_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.bot_data["db"]
    chat_id = update.effective_chat.id
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    await db.aadd_subscriber(chat_id, created_at)
    await update.message.reply_text("✅ Подписка включена.", reply_markup=main_keyboard())


async def cmd_unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.bot_data["db"]
    chat_id = update.effective_chat.id
    await db.aremove_subscriber(chat_id)
    await update.message.reply_text("🔕 Подписка отключена.", reply_markup=main_keyboard())


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.bot_data["db"]
    settings = context.application.bot_data["settings"]

    interval = await db.aget_interval_seconds(settings.default_interval_seconds)
    running = bool(context.job_queue.get_jobs_by_name(JOB_NAME))
    enabled = await db.aget_setting(KEY_MONITOR_ENABLED)
    empty_streak = await db.aget_int(KEY_EMPTY_STREAK, 0)
    cooldown_until = await db.aget_int(KEY_COOLDOWN_UNTIL, 0)
    cooldown_active = cooldown_until > int(now_time())

    last = await db.aget_last_check()
    last_line = "нет данных"
    if last.checked_at:
        last_line = f"{last.checked_at} | has_slots={last.has_slots} | {last.summary}"

    await update.message.reply_text(
        f"Мониторинг: {'🟢 запущен' if running else '🔴 остановлен'}\n"
        f"monitor_enabled: {enabled or '0'}\n"
        f"Интервал: {interval} сек\n"
        f"Empty-streak: {empty_streak}\n"
        f"Cooldown until: {_fmt_dt(cooldown_until)} ({'активен' if cooldown_active else 'не активен'})\n"
        f"Последняя проверка: {last_line}",
        reply_markup=main_keyboard(),
    )


async def _start_job(context: ContextTypes.DEFAULT_TYPE, interval: int) -> None:
    context.job_queue.run_repeating(
        monitor_tick,
        interval=interval,
        first=1,
        name=JOB_NAME,
        data={"interval": interval},
        job_kwargs={"max_instances": 1, "coalesce": True, "misfire_grace_time": 60},
    )


async def _restart_job(context: ContextTypes.DEFAULT_TYPE, interval: int) -> None:
    for j in context.job_queue.get_jobs_by_name(JOB_NAME):
        j.schedule_removal()
    await asyncio.sleep(0.2)
    await _start_job(context, interval)


async def cmd_set_interval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.bot_data["db"]
    settings = context.application.bot_data["settings"]

    if not context.args:
        await update.message.reply_text("Использование: /set_interval 300", reply_markup=main_keyboard())
        return

    sec = int(context.args[0])
    sec = max(90, sec)
    await db.aset_setting(KEY_INTERVAL, str(sec))
    await update.message.reply_text(f"✅ Интервал: {sec} сек.", reply_markup=main_keyboard())

    if context.job_queue.get_jobs_by_name(JOB_NAME):
        await _restart_job(context, sec)
        await update.message.reply_text("🔁 Мониторинг перезапущен.", reply_markup=main_keyboard())


async def cmd_start_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.bot_data["db"]
    settings = context.application.bot_data["settings"]

    if context.job_queue.get_jobs_by_name(JOB_NAME):
        await update.message.reply_text("Мониторинг уже запущен.", reply_markup=main_keyboard())
        return

    interval = await db.aget_interval_seconds(settings.default_interval_seconds)
    interval = max(90, interval)
    await db.aset_setting(KEY_MONITOR_ENABLED, "1")
    await _start_job(context, interval)
    await update.message.reply_text(f"🟢 Мониторинг запущен ({interval} сек).", reply_markup=main_keyboard())


async def cmd_stop_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.bot_data["db"]
    for j in context.job_queue.get_jobs_by_name(JOB_NAME):
        j.schedule_removal()
    await db.aset_setting(KEY_MONITOR_ENABLED, "0")
    await update.message.reply_text("🔴 Мониторинг остановлен.", reply_markup=main_keyboard())


# ---------------- reply-keyboard router ----------------

async def on_menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    t = update.message.text.strip()
    chat_id = update.effective_chat.id if update.effective_chat else None
    log.info("INCOMING message chat_id=%s text=%r", chat_id, t)

    # железный тест
    if t.lower() == "ping":
        await update.message.reply_text("pong", reply_markup=main_keyboard())
        return

    if t == BTN_STATUS:
        await cmd_status(update, context); return
    if t == BTN_HELP:
        await cmd_help(update, context); return
    if t == BTN_SUB:
        await cmd_subscribe(update, context); return
    if t == BTN_UNSUB:
        await cmd_unsubscribe(update, context); return
    if t == BTN_START:
        await cmd_start_monitor(update, context); return
    if t == BTN_STOP:
        await cmd_stop_monitor(update, context); return
    if t == BTN_INTERVAL:
        await update.message.reply_text("Сменить интервал: /set_interval 300 (мин. 90)", reply_markup=main_keyboard())
        return

    await update.message.reply_text("Не понял. Нажми кнопку или /help.", reply_markup=main_keyboard())


# ---------------- monitor job ----------------

async def monitor_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = context.application.bot_data["settings"]
    db: Database = context.application.bot_data["db"]

    now_epoch = int(now_time())
    cooldown_until = await db.aget_int(KEY_COOLDOWN_UNTIL, 0)
    if cooldown_until > now_epoch:
        log.info("Cooldown active until %s, skipping tick.", _fmt_dt(cooldown_until))
        return

    try:
        res: CheckResult = await check_once(
            target_url=str(settings.target_url),
            data_dir=Path(settings.data_dir),
            screenshot_on_slots=settings.screenshot_on_slots,
            headless=True,
        )
        await db.aupdate_last_check(res.checked_at, res.has_slots, res.summary)

        prev_digest = await db.aget_setting(KEY_LAST_DIGEST)
        prev_has_slots = await db.aget_setting(KEY_LAST_HAS_SLOTS)

        await db.aset_setting(KEY_LAST_DIGEST, res.digest)
        await db.aset_setting(KEY_LAST_HAS_SLOTS, "1" if res.has_slots else "0")

        await db.aset_int(KEY_EMPTY_STREAK, 0)
        await db.aset_int(KEY_COOLDOWN_UNTIL, 0)

        changed = (prev_digest != res.digest)
        was_no_slots = (prev_has_slots in (None, "0"))

        if res.has_slots and (changed or was_no_slots):
            msg = (
                "✅ Похоже, появились слоты!\n"
                f"Время: {res.checked_at}\n"
                f"URL: {settings.target_url}\n\n"
                f"{res.summary}"
            )
            await _notify_all(context, msg)

        log.info("Check done: has_slots=%s digest=%s", res.has_slots, res.digest)

    except EmptyPageError as e:
        streak = await db.aget_int(KEY_EMPTY_STREAK, 0) + 1
        await db.aset_int(KEY_EMPTY_STREAK, streak)

        if streak >= 3:
            minutes = random.randint(25, 45)
        elif streak >= 2:
            minutes = random.randint(15, 25)
        else:
            minutes = random.randint(5, 10)

        until = int(now_time()) + minutes * 60
        await db.aset_int(KEY_COOLDOWN_UNTIL, until)

        log.warning("EmptyPageError streak=%s cooldown=%smin until=%s. %s", streak, minutes, _fmt_dt(until), e)

    except ContinueNotFoundError as e:
        log.warning("ContinueNotFoundError: %s", e)

    except Exception as e:
        log.exception("Monitor tick failed: %s", e)


async def auto_restore_monitoring(app: Application) -> None:
    settings = app.bot_data["settings"]
    db: Database = app.bot_data["db"]

    enabled = await db.aget_setting(KEY_MONITOR_ENABLED)
    if enabled != "1":
        return

    subs = await db.alist_subscribers()
    if not subs:
        return

    interval = await db.aget_interval_seconds(settings.default_interval_seconds)
    interval = max(90, interval)

    if app.job_queue.get_jobs_by_name(JOB_NAME):
        return

    app.job_queue.run_repeating(
        monitor_tick,
        interval=interval,
        first=1,
        name=JOB_NAME,
        data={"interval": interval},
        job_kwargs={"max_instances": 1, "coalesce": True, "misfire_grace_time": 60},
    )
    log.info("Auto-restored monitoring: interval=%s sec, subscribers=%s", interval, len(subs))


def build_app() -> Application:
    settings = load_settings()
    setup_logging(Path(settings.log_dir))

    db = Database(Path(settings.db_path))
    db.init()

    async def _post_init(app: Application):
        await auto_restore_monitoring(app)

    app = Application.builder().token(settings.tg_bot_token).post_init(_post_init).build()
    app.bot_data["settings"] = settings
    app.bot_data["db"] = db

    app.add_error_handler(on_error)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("subscribe", cmd_subscribe))
    app.add_handler(CommandHandler("unsubscribe", cmd_unsubscribe))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("set_interval", cmd_set_interval))
    app.add_handler(CommandHandler("start_monitor", cmd_start_monitor))
    app.add_handler(CommandHandler("stop_monitor", cmd_stop_monitor))

    # reply-keyboard buttons (plain text)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_menu_text))

    return app


def main() -> None:
    app = build_app()
    log.info("Starting bot polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)
