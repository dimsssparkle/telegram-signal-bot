from flask import Flask, request
import os
import requests
from binance.client import Client as BinanceClient

app = Flask(__name__)

# Получаем токен и chat_id для Telegram из переменных окружения
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    raise Exception("❌ TELEGRAM_TOKEN и TELEGRAM_CHAT_ID должны быть заданы в переменных окружения")

# Получаем ключи для Binance из переменных окружения
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")
if not BINANCE_API_KEY or not BINANCE_API_SECRET:
    raise Exception("❌ BINANCE_API_KEY и BINANCE_API_SECRET должны быть заданы в переменных окружения")

# Инициализируем Binance API-клиент
binance_client = BinanceClient(BINANCE_API_KEY, BINANCE_API_SECRET)
try:
    binance_client.ping()
    print("✅ Подключение к Binance успешно")
except Exception as e:
    print(f"❌ Ошибка подключения к Binance: {e}")

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

# Корневая страница для проверки работоспособности
@app.route("/")
def index():
    return "🚀 Бот работает!"

# Webhook для приёма сигналов от TradingView
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    if not data or "signal" not in data:
        return {"status": "error", "message": "No signal provided"}, 400

    signal = data["signal"]
    # Получаем символ из TradingView (например, "ETHUSDT.P")
    symbol_received = data.get("symbol", "N/A")
    # Преобразуем в формат Binance: оставляем часть до первой точки
    symbol_fixed = symbol_received.split('.')[0]

    print(f"📥 Получен сигнал: {signal}")
    print(f"📥 Получен символ: {symbol_received} -> Преобразованный: {symbol_fixed}")

    # Запрос баланса фьючерсного аккаунта Binance
    try:
        futures_balance = binance_client.futures_account_balance()
        usdt_balance = None
        for asset in futures_balance:
            if asset.get("asset") == "USDT":
                usdt_balance = asset.get("balance")
                break
        print(f"📊 Futures баланс: USDT {usdt_balance}")
    except Exception as e:
        print(f"❌ Ошибка получения баланса: {e}")
        usdt_balance = "не удалось получить баланс"

    # Здесь можно добавить логику открытия сделки на Binance по symbol_fixed и signal
    # Например: open_trade(symbol_fixed, signal)

    # Отправляем сообщение в Telegram с данными сигнала, отформатированным символом и балансом
    send_telegram_message(
        f"📡 Эй! Получен сигнал: *{signal.upper()}*\nСимвол: *{symbol_fixed}*\nFutures баланс: USDT {usdt_balance}"
    )

    return {"status": "ok", "signal": signal, "symbol": symbol_fixed}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
