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

# Глобальная переменная для управления торговлей (если False – сигналы игнорируются)
trading_enabled = False

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

# Глобальный словарь для хранения данных открытых позиций по символам.
# Для каждого символа сохраняются данные: entry_price, quantity, leverage, commission_entry, break_even_price
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
# Функция закрытия всех открытых позиций с использованием reduceOnly=True
def close_all_positions():
    logging.info("Начинается закрытие всех открытых позиций.")
    try:
        positions = binance_client.futures_position_information()
    except Exception as e:
        logging.error(f"❌ Ошибка получения позиций: {e}")
        return

    closed_symbols = []
    for pos in positions:
        amt = float(pos.get("positionAmt", 0))
        if abs(amt) > 0:
            symbol = pos.get("symbol")
            try:
                order = binance_client.futures_create_order(
                    symbol=symbol,
                    side="SELL" if amt > 0 else "BUY",
                    type="MARKET",
                    quantity=abs(amt),
                    reduceOnly=True
                )
                logging.info(f"✅ Позиция {symbol} закрыта: {order}")
                closed_symbols.append(symbol)
            except Exception as e:
                logging.error(f"❌ Ошибка закрытия позиции для {symbol}: {e}")
    if closed_symbols:
        send_telegram_message(f"🚫 Закрыты позиции по: {', '.join(closed_symbols)}")
    else:
        send_telegram_message("ℹ️ Нет открытых позиций для закрытия.")

# --------------------------
# Обработка закрытия позиции через Binance User Data Stream
def handle_user_data(msg):
    if msg.get('e') != 'ORDER_TRADE_UPDATE':
        return
    order = msg.get('o', {})
    logging.info("Order object: " + str(order))
    symbol = order.get('s', '')
    if symbol not in positions_entry_data:
        return
    if order.get('X') == 'FILLED' and order.get('ps', '') == 'BOTH':
        exit_price = float(order.get('avgPrice', order.get('ap', 0)))
        quantity = float(order.get('q', 0))
        pnl = float(order.get('rp', 0))
        commission_exit = float(order.get('commission', 0))
        entry_data = positions_entry_data.pop(symbol, {})
        entry_price = entry_data.get("entry_price", 0)
        leverage = entry_data.get("leverage", 1)
        commission_entry = entry_data.get("commission_entry", 0)
        break_even_price = entry_data.get("break_even_price", 0)
        total_commission = commission_entry + commission_exit
        net_pnl = pnl - total_commission
        net_break_even = break_even_price + total_commission
        direction = "LONG" if order.get('S', '') == "SELL" else "SHORT"
        # Определяем метод закрытия по типу ордера
        closing_method = "MANUAL"
        if order.get("type") == "TAKE_PROFIT_MARKET":
            closing_method = "TP"
        elif order.get("type") == "STOP_MARKET":
            closing_method = "SL"
        result_indicator = "🟩" if net_pnl > 0 else "🟥"
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
            f"Метод закрытия: {closing_method}"
        )
        send_telegram_message(message)
        logging.info("DEBUG: Telegram сообщение о закрытии отправлено:")
        logging.info(message)
        # Отменяем висячие ордера для данного символа, чтобы не оставались активными противоположные ордера
        try:
            binance_client.futures_cancel_all_open_orders(symbol=symbol)
            logging.info(f"🧹 Висячие ордера для {symbol} отменены.")
        except Exception as e:
            logging.error(f"❌ Ошибка отмены висячих ордеров для {symbol}: {e}")

# --------------------------
# Функция, которая раз в 30 секунд проверяет открытые позиции и отменяет ордера, если позиции отсутствуют
def auto_cancel_worker():
    while True:
        time.sleep(30)
        try:
            open_orders = binance_client.futures_get_open_orders()
            if open_orders:
                # Для каждого ордера проверяем, есть ли позиция по данному символу
                for order in open_orders:
                    symbol = order.get("symbol")
                    pos = get_position(symbol)
                    if pos is None or abs(float(pos.get("positionAmt", 0))) == 0:
                        binance_client.futures_cancel_all_open_orders(symbol=symbol)
                        logging.info(f"🧹 Автоочистка: Ордеры для {symbol} отменены, так как позиции нет.")
        except Exception as e:
            logging.error(f"❌ Ошибка автоочистки ордеров: {e}")

# --------------------------
# Запуск потока Binance User Data Stream
def start_userdata_stream():
    twm = ThreadedWebsocketManager(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET)
    twm.start()
    twm.start_futures_user_socket(callback=handle_user_data)
    logging.info("📡 Binance User Data Stream запущен для отслеживания закрытия позиций.")
    # Запускаем автоочистку ордеров
    threading.Thread(target=auto_cancel_worker, daemon=True).start()
    # Получаем listenKey вручнo после запуска WebSocket
    listen_key = binance_client.futures_stream_get_listen_key()
    def keep_alive():
        global listen_key
        while True:
            time.sleep(30 * 60)
            try:
                binance_client.futures_stream_keepalive(listenKey=listen_key)
                logging.info("✅ ListenKey keepalive выполнен успешно.")
            except Exception as e:
                logging.error(f"❌ Ошибка keepalive: {e}")
                try:
                    listen_key = binance_client.futures_stream_get_listen_key()
                    logging.info("✅ ListenKey обновлен.")
                except Exception as ex:
                    logging.error(f"❌ Ошибка обновления listenKey: {ex}")
    threading.Thread(target=keep_alive, daemon=True).start()

# --------------------------
# Функция опроса Telegram для управления ботом (команды /pause, /resume, /close_orders, /close_orders_pause_trading)
def poll_telegram_commands():
    global trading_enabled
    offset = None
    while True:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
        params = {}
        if offset:
            params["offset"] = offset
        try:
            response = requests.get(url, params=params, timeout=30)
            data = response.json()
            if data.get("ok") and data.get("result"):
                for update in data["result"]:
                    offset = update["update_id"] + 1
                    message = update.get("message")
                    if not message:
                        continue
                    text = message.get("text", "").strip().lower()
                    if text == "/pause":
                        trading_enabled = False
                        send_telegram_message("🚫 Бот приостановлен. Сигналы с TradingView игнорируются.")
                        logging.info("Получена команда /pause. Торговля отключена.")
                    elif text == "/resume":
                        trading_enabled = True
                        send_telegram_message("✅ Бот возобновил работу. Сигналы с TradingView принимаются.")
                        logging.info("Получена команда /resume. Торговля включена.")
                    elif text == "/close_orders":
                        close_all_positions()
                    elif text == "/close_orders_pause_trading":
                        close_all_positions()
                        trading_enabled = False
                        send_telegram_message("🚫 Все позиции закрыты и торговля приостановлена.")
                        logging.info("Получена команда /close_orders_pause_trading. Позиции закрыты, торговля отключена.")
        except Exception as e:
            logging.error(f"❌ Ошибка при опросе Telegram: {e}")
        time.sleep(2)

# --------------------------
# Вебхук для открытия позиции
@app.route("/webhook", methods=["POST"])
def webhook():
    global trading_enabled
    if not trading_enabled:
        logging.info("⚠️ Торговля отключена. Сигналы игнорируются.")
        return {"status": "skipped", "message": "Trading is disabled."}, 200

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

    pos = get_position(symbol_fixed)
    if pos and abs(float(pos.get("positionAmt", 0))) > 0:
        logging.info(f"⚠️ Позиция уже открыта по {symbol_fixed}. Сигнал {signal.upper()} игнорируется.")
        send_telegram_message(f"⚠️ Позиция уже открыта по {symbol_fixed}. Сигнал {signal.upper()} игнорируется.")
        return {"status": "skipped", "message": "Position already open."}

    try:
        leverage_resp = binance_client.futures_change_leverage(symbol=symbol_fixed, leverage=leverage)
        logging.info(f"✅ Установлено плечо {leverage} для {symbol_fixed}: {leverage_resp}")
    except Exception as e:
        logging.error(f"❌ Ошибка установки плеча для {symbol_fixed}: {e}")
        return {"status": "error", "message": f"Error setting leverage: {e}"}

    ticker = binance_client.futures_symbol_ticker(symbol=symbol_fixed)
    last_price = float(ticker["price"])
    min_notional = 20.0
    min_qty_required = min_notional / last_price
    if quantity < min_qty_required:
        logging.info(f"Количество {quantity} слишком мало, минимальное требуемое: {min_qty_required:.6f}. Автоматически устанавливаем минимальное количество.")
        quantity = min_qty_required

    side = "BUY" if signal == "long" else "SELL"

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

    commission_entry = 0.0
    try:
        trades = binance_client.futures_account_trades(symbol=symbol_fixed)
        for trade in reversed(trades):
            if trade.get("orderId") == order.get("orderId"):
                commission_entry = float(trade.get("commission", 0))
                break
    except Exception as e:
        logging.error(f"❌ Ошибка получения комиссии: {e}")

    # Получаем TP/SL процентные значения
    tp_perc = float(data.get("tp_perc", 0))
    sl_perc = float(data.get("sl_perc", 0))
    tp_sl_message = ""
    if tp_perc != 0 and sl_perc != 0:
        if signal == "long":
            tp_level = break_even_price * (1 + tp_perc/100)
            sl_level = break_even_price * (1 - sl_perc/100)
        else:
            tp_level = break_even_price * (1 - tp_perc/100)
            sl_level = break_even_price * (1 + sl_perc/100)
        try:
            tp_order = binance_client.futures_create_order(
                symbol=symbol_fixed,
                side="SELL" if signal=="long" else "BUY",
                type="TAKE_PROFIT_MARKET",
                stopPrice=round(tp_level, 2),
                closePosition=True,
                timeInForce="GTC"
            )
            logging.info(f"✅ TP ордер установлен для {symbol_fixed}: {tp_order}")
        except Exception as e:
            logging.error(f"❌ Ошибка установки TP ордера для {symbol_fixed}: {e}")
        try:
            sl_order = binance_client.futures_create_order(
                symbol=symbol_fixed,
                side="SELL" if signal=="long" else "BUY",
                type="STOP_MARKET",
                stopPrice=round(sl_level, 2),
                closePosition=True,
                timeInForce="GTC"
            )
            logging.info(f"✅ SL ордер установлен для {symbol_fixed}: {sl_order}")
        except Exception as e:
            logging.error(f"❌ Ошибка установки SL ордера для {symbol_fixed}: {e}")
        tp_sl_message = f"\nTP: {round(tp_level,2)} ({tp_perc}%)\nSL: {round(sl_level,2)} ({sl_perc}%)"

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
        f"{tp_sl_message}"
    )
    send_telegram_message(open_message)
    logging.info("DEBUG: Telegram сообщение об открытии отправлено:")
    logging.info(open_message)

    positions_entry_data[symbol_fixed] = {
        "entry_price": entry_price,
        "quantity": quantity,
        "leverage": leverage,
        "commission_entry": commission_entry,
        "break_even_price": break_even_price
    }

    return {"status": "ok", "signal": signal, "symbol": symbol_fixed}

if __name__ == "__main__":
    threading.Thread(target=poll_telegram_commands, daemon=True).start()
    threading.Thread(target=start_userdata_stream, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
