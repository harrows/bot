# cita_bot — Telegram бот для мониторинга слотов записи в консульство (citaconsular.es)

Бот **периодически проверяет** страницу виджета citaconsular.es и уведомляет подписанные чаты при появлении доступных слотов.

- Управление через команды Telegram: `/start_monitor`, `/stop_monitor`, `/status`, `/set_interval`, подписки.
- Хранение состояния и подписок в SQLite.
- Проверка реализована через Playwright (Chromium), чтобы корректно проходить всплывающее `alert()` и кнопку `Continue`.

> Важно: проект **не делает автоматическое бронирование**, только мониторинг + уведомления.

## Быстрый старт (локально)

1) Установи Python 3.10+ и пакеты системы (Ubuntu/Debian):
```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip
```

2) Перейди в каталог проекта:
```bash
cd /opt/cita_bot/app
```

3) Создай venv и поставь зависимости:
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

4) Сконфигурируй `.env`:
```bash
cp .env.example .env
nano .env
```

5) Запусти:
```bash
source .venv/bin/activate
python -m cita_bot
```

## Деплой на VPS (systemd)

Файлы unit лежат в `deploy/systemd/`.

1) Скопируй проект в `/opt/cita_bot` (именно так, как в архиве).
2) Настрой `/opt/cita_bot/app/.env`.
3) Установи зависимости и Chromium как в "Быстрый старт".
4) Поставь systemd unit:
```bash
sudo cp /opt/cita_bot/deploy/systemd/cita-bot.service /etc/systemd/system/
sudo cp /opt/cita_bot/deploy/systemd/cita-bot.env /etc/default/cita-bot
sudo systemctl daemon-reload
sudo systemctl enable --now cita-bot
sudo systemctl status cita-bot --no-pager
```

Логи:
- systemd: `journalctl -u cita-bot -f`
- файл: `/opt/cita_bot/logs/bot.log`

## Команды бота

- `/start` — приветствие и короткая справка
- `/help` — подробная справка
- `/status` — статус мониторинга, интервал, последние результаты
- `/start_monitor [сек]` — запустить мониторинг (например `/start_monitor 120`)
- `/stop_monitor` — остановить мониторинг
- `/set_interval <сек>` — изменить интервал
- `/subscribe` — подписать текущий чат на уведомления
- `/unsubscribe` — отписать текущий чат
- `/list_subscribers` — список подписчиков (только админы)
- `/test` — тестовое уведомление (только админы)

### Ограничения Telegram
Бот может писать человеку **только если человек хотя бы раз написал боту** (или добавил его в группу).

## Безопасность
- **Никогда не публикуй токен бота.**
- Если токен “утёк” — в @BotFather сделай `/revoke` и обнови `.env`.
