# YouTube -> Telegram Notification Bot

## 1. Telegram setup (2 min)
1. Telegram me @BotFather -> `/newbot` -> token mil jayega.
2. Apne bot ko ek message bhejo (jaise "hi").
3. Browser me kholo: `https://api.telegram.org/bot<TOKEN>/getUpdates`
   -> `"chat":{"id":123456789` wali number tumhara CHAT_ID hai.
   (Group me chahiye to bot ko group me add karke group me msg bhejo, id `-100...` jaisi hogi.)

## 2. VPS par install
```bash
sudo apt update && sudo apt install -y python3 python3-venv
mkdir -p ~/ytbot && cd ~/ytbot
# yahan bot.py, requirements.txt, channels.txt, .env.example copy karo
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .env.example .env
nano .env        # token aur chat id daalo
nano channels.txt  # channels daalo
```

Pehle ek baar chalake dekh lo:
```bash
./venv/bin/python bot.py
```
Pehli baar purane videos ka notification nahi aayega (spam se bachne ke liye). Naya video aate hi Telegram par aayega.

## 3. Hamesha chalane ke liye (systemd)
`sudo nano /etc/systemd/system/ytbot.service`:
```
[Unit]
Description=YouTube Telegram Bot
After=network-online.target

[Service]
WorkingDirectory=/home/USER/ytbot
ExecStart=/home/USER/ytbot/venv/bin/python bot.py
Restart=always
RestartSec=10
User=USER

[Install]
WantedBy=multi-user.target
```
(`USER` ko apne linux username se badlo.)

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ytbot
sudo journalctl -u ytbot -f    # logs dekhne ke liye
```

## Notes
- Naya channel add karna: `channels.txt` me line daalo, bas.
- Check interval `.env` me `CHECK_INTERVAL` (seconds) se badlo.
- Agar @handle se channel id na mile (YouTube consent page), to channel ka `UC...` id seedha daal do.
