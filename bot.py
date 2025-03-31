from flask import Flask, request
import os
import requests
import time
import threading
from binance.client import Client as BinanceClient
from binance import ThreadedWebsocketManager
import logging

app = Flask(__name__)

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

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
    logging.info("✅ Подключение к Binance успешно")
except Exception as e:
    logging.error(f"❌ Ошибка подключения к Binance: {e}")

# Глобальный словарь для хранения данных открытых позиций по символам
# Для каждого символа храним: entry_price, quantity, leverage, commission_entry, break_even_price
positions_entry_data = {}

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
        logging.info(f"📤 Отправка в Telegram: {response.status_code} - {response.text}")
        response.raise_for_status()
    except Exception as e:
        logging.error(f"❌ Ошибка отправки в Telegram: {e}")

# Функция для получения позиции по символу (на фьючерсном аккаунте)
def get_position(symbol):
    try:
        info = binance_client.futures_position_information(symbol=symbol)
        pos = next((p for p in info if p["symbol"] == symbol), None)
        logging.debug(f"DEBUG: get_position для {symbol}: {pos}")
        return pos
    except Exception as e:
        logging.error(f"❌ Ошибка получения позиции для {symbol}: {e}")
        return None

# --------------------------
# Обработка закрытия позиции через Binance User Data Stream
def handle_user_data(msg):
    # Фильтруем только события ORDER_TRADE_UPDATE
    if msg.get('e') != 'ORDER_TRADE_UPDATE':
        return
    order = msg.get('o', {})
    symbol = order.get('s', '')
    # Если для этого символа нет сохранённых данных об открытии, выходим
    if symbol not in positions_entry_data:
        return
    # Определяем, что это событие закрытия позиции:
    # Обычно для закрытия позиции статус "FILLED" и ps == "BOTH"
    if order.get('X') == 'FILLED' and order.get('ps', '') == 'BOTH':
        # Цена выхода: используем avgPrice или fallback ap
        exit_price = float(order.get('avgPrice', order.get('ap', 0)))
        # Количество закрытой позиции
        quantity = float(order.get('q', 0))
        # Realized PnL (если доступен)
        pnl = float(order.get('rp', 0))
        # Комиссия закрытия
        commission_exit = float(order.get('commission', 0))
        # Извлекаем данные об открытии
        entry_data = positions_entry_data.pop(symbol, {})
        entry_price = entry_data.get("entry_price", 0)
        leverage = entry_data.get("leverage", 1)
        commission_entry = entry_data.get("commission_entry", 0)
        break_even_price = entry_data.get("break_even_price", 0)
        total_commission = commission_entry + commission_exit
        net_pnl = pnl - total_commission
        # Рассчитываем чистую цену безубыточности (break-even + суммарные комиссии)
        net_break_even = break_even_price + total_commission
        # Определяем направление закрытия: если закрывающий ордер SELL, значит позиция LONG, иначе SHORT
        direction = "LONG" if order.get('S', '') == "SELL" else "SHORT"
        # Результативность сделки: зеленый, если чистый PnL положительный, иначе красный
        result_indicator = "🟩" if net_pnl > 0 else "🟥"
        # Формируем сообщение
        message = (
            f"{result_indicator} Сделка закрыта!\n"
            f"Символ: {symbol}\n"
            f"Направление: {direction}\n"
            f"Количество: {quantity}\n"
            f"Цена входа: {entry_price}\n"
            f"Цена выхода: {exit_price}\n"
            f"Плечо: {leverage}\n"
            f"Сумма комиссий: {total_commission}\n"
            f"PnL: {pnl}\n"
            f"Чистый PnL: {net_pnl}\n"
            f"Цена безубыточности: {break_even_price}\n"
            f"Чистая цена безубыточности: {net_break_even}\n"
            f"Метод закрытия: MANUAL"
        )
        send_telegram_message(message)
        logging.info("DEBUG: Telegram сообщение о закрытии отправлено:")
        logging.info(message)

# Запуск потока Binance User Data Stream
def start_userdata_stream():
    twm = ThreadedWebsocketManager(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET)
    twm.start()
    # Подписываемся на фьючерсный пользовательский поток
    twm.start_futures_user_socket(callback=handle_user_data)
    logging.info("📡 Binance User Data Stream запущен для отслеживания закрытия позиций.")

    # Поддержка listenKey (keep-alive) – в отдельном потоке
    def keep_alive():
        while True:
            time.sleep(30 * 60)
            try:
                binance_client.futures_stream_keepalive(listenKey=twm.listen_key)
            except Exception as e:
                logging.error(f"❌ Ошибка keepalive: {e}")
    threading.Thread(target=keep_alive, daemon=True).start()

# --------------------------
# Вебхук для открытия позиции
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    logging.debug(f"DEBUG: Получен JSON: {data}")
    if not data or "signal" not in data:
        logging.error("❌ Нет поля 'signal' в полученных данных")
        return {"status": "error", "message": "No signal provided"}, 400

    signal = data["signal"].lower()
    symbol_received = data.get("symbol", "N/A")
    symbol_fixed = symbol_received.split('.')[0]

    # Динамические параметры: leverage и quantity (если не переданы, используются значения по умолчанию)
    leverage = int(data.get("leverage", 20))
    quantity = float(data.get("quantity", 0.011))

    logging.info(f"📥 Получен сигнал: {signal}")
    logging.info(f"📥 Символ: {symbol_received} -> {symbol_fixed}")
    logging.info(f"📥 Leverage: {leverage}, Quantity: {quantity}")

    # Проверяем, есть ли уже открытая позиция по этому символу
    pos = get_position(symbol_fixed)
    if pos and abs(float(pos.get("positionAmt", 0))) > 0:
        logging.info(f"⚠️ Позиция уже открыта по {symbol_fixed}. Сигнал {signal.upper()} игнорируется.")
        send_telegram_message(f"⚠️ Позиция уже открыта по {symbol_fixed}. Сигнал {signal.upper()} игнорируется.")
        return {"status": "skipped", "message": "Position already open."}

    # Устанавливаем плечо для указанного символа
    try:
        leverage_resp = binance_client.futures_change_leverage(symbol=symbol_fixed, leverage=leverage)
        logging.info(f"✅ Установлено плечо {leverage} для {symbol_fixed}: {leverage_resp}")
    except Exception as e:
        logging.error(f"❌ Ошибка установки плеча для {symbol_fixed}: {e}")
        return {"status": "error", "message": f"Error setting leverage: {e}"}

    # Получаем текущую цену для расчёта notional
    ticker = binance_client.futures_symbol_ticker(symbol=symbol_fixed)
    last_price = float(ticker["price"])
    min_notional = 20.0  # Минимально допустимый notional (USDT)
    min_qty_required = min_notional / last_price
    if quantity < min_qty_required:
        logging.info(f"Количество {quantity} слишком мало, минимальное требуемое: {min_qty_required:.6f}. Автоматически устанавливаем минимальное количество.")
        quantity = min_qty_required

    # Определяем сторону сделки: если сигнал "long" – ордер BUY, иначе SELL
    side = "BUY" if signal == "long" else "SELL"

    # Открываем рыночный ордер
    try:
        order = binance_client.futures_create_order(
            symbol=symbol_fixed,
            side=side,
            type="MARKET",
            quantity=quantity
        )
        logging.info(f"✅ Ордер создан: {order}")
    except Exception as e:
        logging.error(f"❌ Ошибка создания ордера для {symbol_fixed}: {e}")
        return {"status": "error", "message": f"Error creating order: {e}"}

    # Ждем немного для обновления информации о позиции
    time.sleep(0.5)
    pos = get_position(symbol_fixed)
    if not pos:
        logging.error("❌ Не удалось получить информацию о позиции после ордера")
        return {"status": "error", "message": "Не удалось получить информацию о позиции"}

    try:
        entry_price = float(pos.get("entryPrice", 0))
        used_margin = float(pos.get("initialMargin", 0))
        liq_price = float(pos.get("liquidationPrice", 0))
        break_even_price = float(pos.get("breakEvenPrice", 0))
    except Exception as e:
        logging.error(f"❌ Ошибка извлечения данных позиции: {e}")
        entry_price, used_margin, liq_price, break_even_price = 0, 0, 0, 0

    # Получаем комиссию по ордеру входа (ищем последний трейд с совпадающим orderId)
    commission_entry = 0.0
    try:
        trades = binance_client.futures_account_trades(symbol=symbol_fixed)
        for trade in reversed(trades):
            if trade.get("orderId") == order.get("orderId"):
                commission_entry = float(trade.get("commission", 0))
                break
    except Exception as e:
        logging.error(f"❌ Ошибка получения комиссии: {e}")

    open_message = (
        f"🚀 Сделка открыта!\n"
        f"Символ: {symbol_fixed}\n"
        f"Направление: {signal.upper()}\n"
        f"Количество: {quantity}\n"
        f"Цена входа: {entry_price}\n"
        f"Плечо: {leverage}\n"
        f"Использованная маржа: {used_margin}\n"
        f"Цена ликвидации: {liq_price}\n"
        f"Комиссия входа: {commission_entry}\n"
        f"Цена безубыточности: {break_even_price}"
    )
    send_telegram_message(open_message)
    logging.info("DEBUG: Telegram сообщение об открытии отправлено:")
    logging.info(open_message)

    # Сохраняем данные об открытой позиции для последующего закрытия
    positions_entry_data[symbol_fixed] = {
        "entry_price": entry_price,
        "quantity": quantity,
        "leverage": leverage,
        "commission_entry": commission_entry,
        "break_even_price": break_even_price
    }

    return {"status": "ok", "signal": signal, "symbol": symbol_fixed}

if __name__ == "__main__":
    threading.Thread(target=start_userdata_stream, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
