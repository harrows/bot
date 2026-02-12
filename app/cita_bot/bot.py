from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from .checker import check_once, CheckResult
from .config import load_settings
from .logging_setup import setup_logging
from .storage.db import Database

log = logging.getLogger("cita_bot")

JOB_NAME = "cita_monitor_job"


def is_admin(user_id: Optional[int], admins: list[int]) -> bool:
    return bool(user_id) and user_id in admins


HELP_TEXT = (
    "Команды:\n"
    "/start — приветствие\n"
    "/help — помощь\n"
    "/status — статус мониторинга\n"
    "/subscribe — подписать этот чат на уведомления\n"
    "/unsubscribe — отписать этот чат\n"
    "/start_monitor [сек] — включить мониторинг (например /start_monitor 180)\n"
    "/stop_monitor — выключить мониторинг\n"
    "/set_interval <сек> — изменить интервал (минимум 30 сек рекомендовано)\n"
    "\n"
    "Админ-команды:\n"
    "/list_subscribers — список подписчиков\n"
    "/test — тестовое уведомление\n"
)

# Ключи в settings-таблице
KEY_INTERVAL = "interval_seconds"
KEY_MONITOR_ENABLED = "monitor_enabled"
KEY_LAST_DIGEST = "last_digest"
KEY_LAST_HAS_SLOTS = "last_has_slots"


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Я мониторю слоты записи на citaconsular.es и уведомляю при появлении.\n\n"
        "1) В этом чате: /subscribe\n"
        "2) Включить мониторинг: /start_monitor 180\n"
        "Помощь: /help"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT)


async def cmd_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.bot_data["db"]
    chat_id = update.effective_chat.id
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    await db.aadd_subscriber(chat_id, created_at)
    await update.message.reply_text("✅ Чат подписан на уведомления.")


async def cmd_unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.bot_data["db"]
    chat_id = update.effective_chat.id
    await db.aremove_subscriber(chat_id)
    await update.message.reply_text("🟡 Чат отписан от уведомлений.")


async def cmd_list_subscribers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = context.application.bot_data["settings"]
    if not is_admin(update.effective_user.id if update.effective_user else None, settings.admins):
        await update.message.reply_text("⛔ Только для админов.")
        return
    db: Database = context.application.bot_data["db"]
    subs = await db.alist_subscribers()
    if not subs:
        await update.message.reply_text("Подписчиков нет.")
        return
    await update.message.reply_text("Подписчики (chat_id):\n" + "\n".join(str(x) for x in subs))


async def cmd_set_interval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.bot_data["db"]
    settings = context.application.bot_data["settings"]

    args = context.args
    if not args:
        cur = await db.aget_interval_seconds(settings.default_interval_seconds)
        await update.message.reply_text(f"Текущий интервал: {cur} сек. Используй: /set_interval <сек>")
        return

    try:
        sec = int(args[0])
        # важно: слишком частые проверки могут триггерить защиту
        sec = max(30, sec)
    except ValueError:
        await update.message.reply_text("Нужно число секунд. Пример: /set_interval 180")
        return

    await db.aset_setting(KEY_INTERVAL, str(sec))
    await update.message.reply_text(f"✅ Интервал установлен: {sec} сек.")

    # Если мониторинг уже работает — перезапустим job с новым интервалом
    if context.job_queue.get_jobs_by_name(JOB_NAME):
        await _restart_monitoring_job(context, sec)
        await update.message.reply_text("🔁 Мониторинг перезапущен с новым интервалом.")


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
        f"Флаг monitor_enabled в БД: {enabled or '0'}\n"
        f"Интервал: {interval} сек\n"
        f"Последняя проверка: {last_line}"
    )


async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = context.application.bot_data["settings"]
    if not is_admin(update.effective_user.id if update.effective_user else None, settings.admins):
        await update.message.reply_text("⛔ Только для админов.")
        return
    await _notify_all(context, "✅ Тестовое уведомление. Бот работает.")


async def cmd_start_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.bot_data["db"]
    settings = context.application.bot_data["settings"]

    interval = await db.aget_interval_seconds(settings.default_interval_seconds)

    if context.args:
        try:
            interval = max(30, int(context.args[0]))
            await db.aset_setting(KEY_INTERVAL, str(interval))
        except ValueError:
            await update.message.reply_text("Нужно число секунд. Пример: /start_monitor 180")
            return

    if context.job_queue.get_jobs_by_name(JOB_NAME):
        await update.message.reply_text("Мониторинг уже запущен. /status")
        return

    # ВАЖНО: сохраняем флаг, чтобы после твоих рестартов мониторинг сам восстановился
    await db.aset_setting(KEY_MONITOR_ENABLED, "1")

    await _start_monitoring_job(context, interval)
    await update.message.reply_text(f"🟢 Мониторинг запущен. Интервал: {interval} сек. /status")


async def cmd_stop_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.bot_data["db"]

    jobs = context.job_queue.get_jobs_by_name(JOB_NAME)
    for j in jobs:
        j.schedule_removal()

    # Сбрасываем флаг — после рестарта мониторинг не поднимется сам
    await db.aset_setting(KEY_MONITOR_ENABLED, "0")

    await update.message.reply_text("🔴 Мониторинг остановлен. /status")


async def _start_monitoring_job(context: ContextTypes.DEFAULT_TYPE, interval: int) -> None:
    # мониторинг общий (один job) на всех подписчиков
    context.job_queue.run_repeating(
        monitor_tick,
        interval=interval,
        first=1,
        name=JOB_NAME,
        data={"interval": interval},
    )


async def _restart_monitoring_job(context: ContextTypes.DEFAULT_TYPE, interval: int) -> None:
    jobs = context.job_queue.get_jobs_by_name(JOB_NAME)
    for j in jobs:
        j.schedule_removal()
    await asyncio.sleep(0.2)
    await _start_monitoring_job(context, interval)


async def _notify_all(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    db: Database = context.application.bot_data["db"]
    subs = await db.alist_subscribers()
    if not subs:
        log.info("No subscribers; skipping notify")
        return

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

        # антиспам: уведомляем только при изменении digest и при наличии слотов
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
    """
    Главная фишка: после любого твоего рестарта/деплоя мониторинг сам поднимется,
    если в БД стоит monitor_enabled=1 и есть подписчики.
    """
    settings = app.bot_data["settings"]
    db: Database = app.bot_data["db"]

    enabled = await db.aget_setting(KEY_MONITOR_ENABLED)
    if enabled != "1":
        log.info("Auto-restore: monitor_enabled != 1; skip")
        return

    subs = await db.alist_subscribers()
    if not subs:
        log.info("Auto-restore: no subscribers; skip")
        return

    interval = await db.aget_interval_seconds(settings.default_interval_seconds)

    if app.job_queue.get_jobs_by_name(JOB_NAME):
        log.info("Auto-restore: job already exists; skip")
        return

    app.job_queue.run_repeating(
        monitor_tick,
        interval=interval,
        first=1,
        name=JOB_NAME,
        data={"interval": interval},
    )
    log.info("Auto-restored monitoring: interval=%s sec, subscribers=%s", interval, len(subs))


def build_app() -> Application:
    settings = load_settings()
    setup_logging(Path(settings.log_dir))

    db = Database(Path(settings.db_path))
    db.init()

    async def _post_init(app: Application):
        # автоподнятие мониторинга после рестартов
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
    app.add_handler(CommandHandler("set_interval", cmd_set_interval))
    app.add_handler(CommandHandler("list_subscribers", cmd_list_subscribers))
    app.add_handler(CommandHandler("test", cmd_test))

    return app


def main() -> None:
    app = build_app()
    log.info("Starting bot polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)