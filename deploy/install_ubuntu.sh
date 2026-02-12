#!/usr/bin/env bash
set -euo pipefail

echo "[1/6] Installing system deps..."
sudo apt update
sudo apt install -y python3 python3-venv python3-pip

echo "[2/6] Creating venv..."
cd /opt/cita_bot/app
python3 -m venv .venv
source .venv/bin/activate

echo "[3/6] Installing python deps..."
pip install -r requirements.txt

echo "[4/6] Installing Playwright chromium..."
python -m playwright install chromium

echo "[5/6] Installing systemd unit..."
sudo cp /opt/cita_bot/deploy/systemd/cita-bot.service /etc/systemd/system/
sudo cp /opt/cita_bot/deploy/systemd/cita-bot.env /etc/default/cita-bot
sudo systemctl daemon-reload
sudo systemctl enable --now cita-bot

echo "[6/6] Done."
echo "Check status: sudo systemctl status cita-bot --no-pager"
echo "Follow logs:  journalctl -u cita-bot -f"
