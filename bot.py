from flask import Flask, request
import requests
import os

app = Flask(__name__)

# Заменишь этими переменными из окружения при деплое
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "your_telegram_token_here")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "your_chat_id_here")

@app.route("/signal", methods=["POST"])
def signal():
    data = request.get_json()
    message = data.get("message", "🚨 Сигнал получен!")

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message
    }

    try:
        r = requests.post(url, data=payload)
        if r.status_code == 200:
            return {"status": "ok"}
        else:
            return {"status": "error", "details": r.text}, 500
    except Exception as e:
        return {"status": "error", "details": str(e)}, 500

@app.route("/", methods=["GET"])
def home():
    return "🚀 Telegram Bot is running!"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
