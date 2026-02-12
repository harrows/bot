# CitaConsular Telegram Bot (server layout: /root/bot)

## Install
```bash
cd /root/bot/app
cp -n .env.example .env
nano .env
mkdir -p /root/bot/logs /root/bot/data /root/bot/data/screenshots

apt update
apt install -y python3 python3-venv python3-pip
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
