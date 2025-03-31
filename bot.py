from flask import Flask, request
import os
import requests
import time
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

# Инициализируем Binance API-клиента
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

# Функция для получения позиции по символу (на фьючерсном аккаунте)
def get_position(symbol):
    try:
        info = binance_client.futures_position_information(symbol=symbol)
        pos = next((p for p in info if p["symbol"] == symbol), None)
        print(f"DEBUG: get_position для {symbol}: {pos}")
        return pos
    except Exception as e:
        print(f"❌ Ошибка получения позиции для {symbol}: {e}")
        return None

# Корневая страница для проверки работоспособности
@app.route("/")
def index():
    return "🚀 Бот работает!"

# Webhook для приёма сигналов от TradingView и открытия позиции на Binance
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    print("DEBUG: Получен JSON:", data)
    if not data or "signal" not in data:
        print("❌ Нет поля 'signal' в полученных данных")
        return {"status": "error", "message": "No signal provided"}, 400

    # Извлекаем сигнал и символ из запроса
    signal = data["signal"].lower()
    symbol_received = data.get("symbol", "N/A")
    symbol_fixed = symbol_received.split('.')[0]

    # Извлекаем динамические параметры: leverage и quantity
    leverage = int(data.get("leverage", 10))
    quantity = float(data.get("quantity", 0.011))

    print(f"📥 Получен сигнал: {signal}")
    print(f"📥 Получен символ: {symbol_received} -> {symbol_fixed}")
    print(f"📥 Leverage: {leverage}, Quantity: {quantity}")

    # Проверяем, есть ли уже открытая позиция по этому символу
    pos = get_position(symbol_fixed)
    if pos and abs(float(pos.get("positionAmt", 0))) > 0:
        print(f"⚠️ Позиция уже открыта по {symbol_fixed}. Сигнал {signal} игнорируется.")
        send_telegram_message(f"⚠️ Позиция уже открыта по {symbol_fixed}. Сигнал {signal.upper()} игнорируется.")
        return {"status": "skipped", "message": "Position already open."}

    # Устанавливаем плечо для указанного символа
    try:
        leverage_resp = binance_client.futures_change_leverage(symbol=symbol_fixed, leverage=leverage)
        print(f"✅ Установлено плечо {leverage} для {symbol_fixed}: {leverage_resp}")
    except Exception as e:
        print(f"❌ Ошибка установки плеча для {symbol_fixed}: {e}")
        return {"status": "error", "message": f"Error setting leverage: {e}"}

    # Получаем текущую цену для расчёта notional
    ticker = binance_client.futures_symbol_ticker(symbol=symbol_fixed)
    last_price = float(ticker["price"])
    min_notional = 20.0  # Минимально допустимый notional (в USDT)
    min_qty_required = min_notional / last_price
    if quantity < min_qty_required:
        print(f"Количество {quantity} слишком мало, минимальное требуемое: {min_qty_required:.6f}. Автоматически устанавливаем минимальное количество.")
        quantity = min_qty_required

    # Определяем сторону сделки: если сигнал "long" – ордер BUY, иначе SELL
    side = "BUY" if signal == "long" else "SELL"

    # Открываем рыночный ордер с рассчитанным количеством
    try:
        order = binance_client.futures_create_order(
            symbol=symbol_fixed,
            side=side,
            type="MARKET",
            quantity=quantity
        )
        print(f"✅ Ордер создан: {order}")
    except Exception as e:
        print(f"❌ Ошибка создания ордера для {symbol_fixed}: {e}")
        return {"status": "error", "message": f"Error creating order: {e}"}

    # Ждем немного, чтобы данные о позиции обновились
    time.sleep(0.5)
    pos = get_position(symbol_fixed)
    if not pos:
        print("❌ Не удалось получить информацию о позиции после ордера")
        return {"status": "error", "message": "Не удалось получить информацию о позиции"}

    try:
        entry_price = float(pos.get("entryPrice", 0))
        used_margin = float(pos.get("initialMargin", 0))
        liq_price = float(pos.get("liquidationPrice", 0))
    except Exception as e:
        print(f"❌ Ошибка извлечения данных позиции: {e}")
        entry_price, used_margin, liq_price = 0, 0, 0

    # Получаем комиссию по ордеру входа
    commission = 0.0
    try:
        trades = binance_client.futures_account_trades(symbol=symbol_fixed)
        for trade in reversed(trades):
            if trade.get("orderId") == order.get("orderId"):
                commission = float(trade.get("commission", 0))
                break
    except Exception as e:
        print(f"❌ Ошибка получения комиссии: {e}")

    # Формируем сообщение для Telegram с фактическими данными
    message = (
        f"🚀 Сделка открыта!\n"
        f"Символ: {symbol_fixed}\n"
        f"Направление: {signal.upper()}\n"
        f"Количество: {quantity}\n"
        f"Цена входа: {entry_price}\n"
        f"Плечо: {leverage}\n"
        f"Использованная маржа: {used_margin}\n"
        f"Цена ликвидации: {liq_price}\n"
        f"Комиссия входа: {commission}"
    )
    send_telegram_message(message)
    print("DEBUG: Telegram сообщение отправлено:")
    print(message)

    return {"status": "ok", "signal": signal, "symbol": symbol_fixed}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
