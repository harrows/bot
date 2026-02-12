from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from .checker import check_once, CheckResult
from .config import load_settings
from .logging_setup import setup_logging
from .storage.db import Database

log = logging.getLogger("cita_bot")

JOB_NAME = "cita_monitor_job"

KEY_INTERVAL = "interval_seconds"
KEY_MONITOR_ENABLED = "monitor_enabled"
KEY_LAST_DIGEST = "last_digest"
KEY_LAST_HAS_SLOTS = "last_has_slots"

BTN_STATUS = "📊 Статус"
BTN_SUBSCRIBE = "🔔 Подписаться"
BTN_UNSUBSCRIBE = "🔕 Отписаться"
BTN_START = "🟢 Старт мониторинга"
BTN_STOP = "🔴 Стоп мониторинга"
BTN_INTERVAL = "⏱ Интервал"
BTN_HELP = "ℹ️ Помощь"

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(BTN_STATUS), KeyboardButton(BTN_HELP)],
        [KeyboardButton(BTN_SUBSCRIBE), KeyboardButton(BTN_UNSUBSCRIBE)],
        [KeyboardButton(BTN_START), KeyboardButton(BTN_STOP)],
        [KeyboardButton(BTN_INTERVAL)],
    ],
    resize_keyboard=True,
)

HELP_TEXT = (
    "Я мониторю слоты записи на citaconsular.es и уведомляю подписанные чаты.\n\n"
    "Как пользоваться:\n"
    "1) Нажми «🔔 Подписаться» в нужном чате (личка или группа)\n"
    "2) Нажми «🟢 Старт мониторинга»\n"
    "3) При появлении слотов я пришлю уведомление.\n\n"
    "Интервал можно менять кнопкой «⏱ Интервал».\n"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Я готов.\n\n"
        "Нажми «🔔 Подписаться» → потом «🟢 Старт мониторинга».",
        reply_markup=MAIN_KB,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, reply_markup=MAIN_KB)


async def cmd_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.bot_data["db"]
    chat_id = update.effective_chat.id
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    await db.aadd_subscriber(chat_id, created_at)
    await update.message.reply_text("✅ Чат подписан на уведомления.", reply_markup=MAIN_KB)


async def cmd_unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.bot_data["db"]
    chat_id = update.effective_chat.id
    await db.aremove_subscriber(chat_id)
    await update.message.reply_text("🟡 Чат отписан от уведомлений.", reply_markup=MAIN_KB)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.bot_data["db"]
    settings = context.application.bot_data["settings"]

    interval = await db.aget_interval_seconds(settings.default_interval_seconds)
    running = bool(context.job_queue.get_jobs_by_name(JOB_NAME))
    enabled = await db.aget_setting(KEY_MONITOR_ENABLED)

    last = await db.aget_last_check()
    last_line = "нет данных"
    if last.checked_at:
        last_line = f"{last.checked_at} | has_slots={last.has_slots} | {last.summary}"

    await update.message.reply_text(
        f"Мониторинг: {'🟢 запущен' if running else '🔴 остановлен'}\n"
        f"Флаг monitor_enabled: {enabled or '0'}\n"
        f"Интервал: {interval} сек\n"
        f"Последняя проверка: {last_line}",
        reply_markup=MAIN_KB,
    )


async def _start_monitoring_job(context: ContextTypes.DEFAULT_TYPE, interval: int) -> None:
    context.job_queue.run_repeating(
        monitor_tick,
        interval=interval,
        first=1,
        name=JOB_NAME,
        data={"interval": interval},
    )


async def _restart_monitoring_job(context: ContextTypes.DEFAULT_TYPE, interval: int) -> None:
    for j in context.job_queue.get_jobs_by_name(JOB_NAME):
        j.schedule_removal()
    await asyncio.sleep(0.2)
    await _start_monitoring_job(context, interval)


async def cmd_start_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.bot_data["db"]
    settings = context.application.bot_data["settings"]
    interval = await db.aget_interval_seconds(settings.default_interval_seconds)

    if context.job_queue.get_jobs_by_name(JOB_NAME):
        await update.message.reply_text("Мониторинг уже запущен. Нажми «📊 Статус».", reply_markup=MAIN_KB)
        return

    await db.aset_setting(KEY_MONITOR_ENABLED, "1")
    await _start_monitoring_job(context, interval)
    await update.message.reply_text(f"🟢 Мониторинг запущен. Интервал: {interval} сек.", reply_markup=MAIN_KB)


async def cmd_stop_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.bot_data["db"]
    for j in context.job_queue.get_jobs_by_name(JOB_NAME):
        j.schedule_removal()
    await db.aset_setting(KEY_MONITOR_ENABLED, "0")
    await update.message.reply_text("🔴 Мониторинг остановлен.", reply_markup=MAIN_KB)


async def _notify_all(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    db: Database = context.application.bot_data["db"]
    subs = await db.alist_subscribers()
    for chat_id in subs:
        try:
            await context.bot.send_message(chat_id=chat_id, text=text, disable_web_page_preview=True)
        except Exception as e:
            log.warning("Failed to notify chat_id=%s: %s", chat_id, e)


async def monitor_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = context.application.bot_data["settings"]
    db: Database = context.application.bot_data["db"]

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

        changed = (prev_digest != res.digest)
        was_no_slots = (prev_has_slots in (None, "0"))

        if res.has_slots and (changed or was_no_slots):
            msg = (
                "🟢 Похоже, появились доступные слоты!\n\n"
                f"Время проверки: {res.checked_at}\n"
                f"URL: {settings.target_url}\n\n"
                f"Фрагмент страницы: {res.summary}"
            )
            await _notify_all(context, msg)

        log.info("Check done: has_slots=%s digest=%s summary=%s", res.has_slots, res.digest, res.summary[:120])

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
    if app.job_queue.get_jobs_by_name(JOB_NAME):
        return

    app.job_queue.run_repeating(
        monitor_tick,
        interval=interval,
        first=1,
        name=JOB_NAME,
        data={"interval": interval},
    )
    log.info("Auto-restored monitoring: interval=%s sec, subscribers=%s", interval, len(subs))


async def on_menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()

    if text == BTN_STATUS:
        await cmd_status(update, context)
        return
    if text == BTN_HELP:
        await cmd_help(update, context)
        return
    if text == BTN_SUBSCRIBE:
        await cmd_subscribe(update, context)
        return
    if text == BTN_UNSUBSCRIBE:
        await cmd_unsubscribe(update, context)
        return
    if text == BTN_START:
        await cmd_start_monitor(update, context)
        return
    if text == BTN_STOP:
        await cmd_stop_monitor(update, context)
        return
    if text == BTN_INTERVAL:
        await update.message.reply_text(
            "Введи интервал в секундах (минимум 30), например: 180\n"
            "Я восприму следующее сообщение как интервал.",
            reply_markup=MAIN_KB,
        )
        context.user_data["awaiting_interval"] = True
        return

    if context.user_data.get("awaiting_interval"):
        m = re.fullmatch(r"\s*(\d+)\s*", text)
        if not m:
            await update.message.reply_text("Нужно число. Например: 180", reply_markup=MAIN_KB)
            return
        sec = max(30, int(m.group(1)))
        db: Database = context.application.bot_data["db"]
        await db.aset_setting(KEY_INTERVAL, str(sec))
        context.user_data["awaiting_interval"] = False

        await update.message.reply_text(f"✅ Интервал установлен: {sec} сек.", reply_markup=MAIN_KB)

        if context.job_queue.get_jobs_by_name(JOB_NAME):
            await _restart_monitoring_job(context, sec)
            await update.message.reply_text("🔁 Мониторинг перезапущен с новым интервалом.", reply_markup=MAIN_KB)
        return


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

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("subscribe", cmd_subscribe))
    app.add_handler(CommandHandler("unsubscribe", cmd_unsubscribe))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("start_monitor", cmd_start_monitor))
    app.add_handler(CommandHandler("stop_monitor", cmd_stop_monitor))

    # кнопки меню
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_menu_text))

    return app


def main() -> None:
    app = build_app()
    log.info("Starting bot polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)
