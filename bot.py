from flask import Flask, request
import os
import requests

app = Flask(__name__)

# Получаем токен и chat_id из переменных окружения
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Проверка: если не заданы переменные, выводим предупреждение
if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    raise Exception("❌ TELEGRAM_TOKEN и TELEGRAM_CHAT_ID должны быть заданы в переменных окружения")

def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown"
    }
    try:
        response = requests.post(url, data=payload)
        response.raise_for_status()
    except Exception as e:
        print(f"Ошибка отправки в Telegram: {e}")

@app.route("/")
def index():
    return "🚀 Бот работает!"

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    if not data or "signal" not in data:
        return {"status": "error", "message": "No signal provided"}, 400

    signal = data["signal"]
    send_telegram_message(f"📡 Получен сигнал: *{signal.upper()}*")
    return {"status": "ok", "signal": signal}

if __name__ == "__main__":
    app.run(port=5000)
