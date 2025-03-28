from flask import Flask, request
import os
import requests

app = Flask(__name__)

# Получаем токен и chat_id из переменных окружения
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Проверка: если не заданы переменные — останавливаем запуск
if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    raise Exception("❌ TELEGRAM_TOKEN и TELEGRAM_CHAT_ID должны быть заданы в переменных окружения")

# Функция отправки сообщения в Telegram
def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown"
    }
    try:
        response = requests.post(url, data=payload)
        print(f"📤 Отправка в Telegram: {response.status_code} - {response.text}")
        response.raise_for_status()
    except Exception as e:
        print(f"❌ Ошибка отправки в Telegram: {e}")

# Корневая страница (для проверки работоспособности)
@app.route("/")
def index():
    return "🚀 Бот работает!"

# Webhook — приём сигналов
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    if not data or "signal" not in data:
        return {"status": "error", "message": "No signal provided"}, 400

    signal = data["signal"]
    print(f"📥 Получен сигнал: {signal}")
    send_telegram_message(f"📡 Получен сигнал: *{signal.upper()}*")

    return {"status": "ok", "signal": signal}

# Запуск Flask-приложения
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
