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
# Сохраняются: signal, entry_price, quantity, leverage, used_margin,
# commission_entry, break_even_price, liq_price, tp_perc, sl_perc
positions_entry_data = {}

# Глобальная переменная для listen_key
listen_key = None

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

# Функция для получения текущего Futures баланса (например, USDT)
def get_futures_balance():
    try:
        balances = binance_client.futures_account_balance()
        usdt_balance = next((item for item in balances if item["asset"] == "USDT"), None)
        if usdt_balance:
            return float(usdt_balance["balance"])
    except Exception as e:
        logging.error(f"❌ Ошибка получения баланса: {e}")
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
# Функция переключения позиции (switch_position)
def switch_position(new_signal, symbol, leverage, quantity):
    """
    Если открыта позиция с направлением, отличным от new_signal,
    закрываем текущую позицию (и связанные с ней ордера TP/SL), ждем обновления,
    и затем открываем новую позицию.
    Если позиция с тем же направлением уже открыта, сигнал игнорируется.
    """
    current_position = get_position(symbol)
    if current_position:
        current_amt = abs(float(current_position.get("positionAmt", 0)))
        if current_amt > 0:
            current_direction = "long" if float(current_position.get("positionAmt", 0)) > 0 else "short"
            if current_direction != new_signal:
                logging.info(f"Текущая позиция {current_direction.upper()} отличается от сигнала {new_signal.upper()}, переключаем позицию.")
                close_all_positions()  # Закрываем все позиции по данному символу
                time.sleep(2)  # Увеличена задержка для обновления данных после закрытия
            else:
                msg = f"⚠️ Позиция уже открыта с направлением {current_direction.upper()}, сигнал {new_signal.upper()} игнорируется."
                logging.info(msg)
                send_telegram_message(msg)
                return {"status": "skipped", "message": "Position already open."}
    try:
        leverage_resp = binance_client.futures_change_leverage(symbol=symbol, leverage=leverage)
        logging.info(f"✅ Установлено плечо {leverage} для {symbol}: {leverage_resp}")
    except Exception as e:
        err_msg = f"❌ Ошибка установки плеча для {symbol}: {e}"
        logging.error(err_msg)
        return {"status": "error", "message": err_msg}
    side = "BUY" if new_signal == "long" else "SELL"
    try:
        order = binance_client.futures_create_order(
            symbol=symbol,
            side=side,
            type="MARKET",
            quantity=quantity
        )
        logging.info(f"✅ Ордер создан: {order}")
    except Exception as e:
        err_msg = f"❌ Ошибка создания ордера для {symbol}: {e}"
        logging.error(err_msg)
        return {"status": "error", "message": err_msg}
    return {"status": "ok", "message": f"Opened {new_signal.upper()} position on {symbol}."}

# --------------------------
# Обработка закрытия позиции через Binance User Data Stream
def handle_user_data(msg):
    if msg.get('e') != 'ORDER_TRADE_UPDATE':
        return
    order = msg.get('o', {})
    symbol = order.get('s', '')
    if symbol not in positions_entry_data:
        return

    if order.get('X') == 'FILLED' and order.get('ps', '') == 'BOTH':
        time.sleep(2)  # время для обновления истории трейдов
        exit_price = float(order.get('avgPrice', order.get('ap', 0)))
        quantity = float(order.get('q', 0))
        pnl = float(order.get('rp', 0))
        
        commission_exit = 0.0
        try:
            trades = binance_client.futures_account_trades(symbol=symbol)
            if trades:
                last_trade = trades[-1]
                commission_exit = float(last_trade.get('commission', 0))
                logging.info("Последний трейд для закрытия: " + str(last_trade))
            else:
                logging.info("Трейдов для закрывающего ордера не найдено.")
        except Exception as e:
            logging.error(f"❌ Ошибка получения трейдов для закрывающей сделки: {e}")
        
        entry_data = positions_entry_data.pop(symbol, {})
        entry_price = entry_data.get("entry_price", 0)
        leverage = entry_data.get("leverage", 1)
        commission_entry = entry_data.get("commission_entry", 0)
        break_even_price = entry_data.get("break_even_price", 0)
        tp_perc = entry_data.get("tp_perc", 0)
        sl_perc = entry_data.get("sl_perc", 0)
        signal = entry_data.get("signal", "N/A")
        
        total_commission = commission_entry + commission_exit
        net_pnl = pnl - total_commission
        net_break_even = break_even_price + total_commission
        direction = "LONG" if order.get('S', '') == "SELL" else "SHORT"
        
        if tp_perc != 0 and sl_perc != 0:
            if direction == "LONG":
                tp_level = break_even_price * (1 + tp_perc/100)
                sl_level = break_even_price * (1 - sl_perc/100)
            else:
                tp_level = break_even_price * (1 - tp_perc/100)
                sl_level = break_even_price * (1 + sl_perc/100)
        else:
            tp_level = sl_level = None

        closing_method = "MANUAL"
        if order.get("ot") == "TAKE_PROFIT_MARKET":
            closing_method = "TP"
        elif order.get("ot") == "STOP_MARKET":
            closing_method = "SL"
        
        balance = get_futures_balance()
        balance_message = f"\nFutures баланс: USDT {balance}" if balance is not None else ""
        
        result_indicator = "🟩" if net_pnl > 0 else "🟥"
        message = (
            f"{result_indicator} Сделка закрыта!\n"
            f"Символ: {symbol}\n"
            f"Направление: {direction}\n"
            f"Количество: {quantity}\n"
            f"Цена входа: {entry_price}\n"
            f"Цена выхода: {exit_price}\n"
            f"Плечо: {leverage}\n"
            f"Комиссия входа: {commission_entry}\n"
            f"Комиссия выхода: {commission_exit}\n"
            f"Сумма комиссий: {total_commission}\n"
            f"PnL: {pnl}\n"
            f"Чистый PnL: {net_pnl}\n"
            f"Цена безубыточности: {break_even_price}\n"
            f"Чистая цена безубыточности: {net_break_even}\n"
            f"Метод закрытия: {closing_method}"
            f"{balance_message}"
        )
        send_telegram_message(message)
        logging.info("DEBUG: Telegram сообщение о закрытии отправлено:")
        logging.info(message)
        
        try:
            binance_client.futures_cancel_all_open_orders(symbol=symbol)
            logging.info(f"🧹 Висячие ордера для {symbol} отменены.")
        except Exception as e:
            logging.error(f"❌ Ошибка отмены висячих ордеров для {symbol}: {e}")

# --------------------------
# Функция автоочистки ордеров (раз в 30 сек)
def auto_cancel_worker():
    while True:
        time.sleep(30)
        try:
            open_orders = binance_client.futures_get_open_orders()
            if open_orders:
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
    global listen_key
    twm = ThreadedWebsocketManager(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET)
    twm.start()
    twm.start_futures_user_socket(callback=handle_user_data)
    logging.info("📡 Binance User Data Stream запущен для отслеживания закрытия позиций.")
    threading.Thread(target=auto_cancel_worker, daemon=True).start()
    
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
# Функция опроса Telegram для управления ботом (команды /pause, /resume, /close_orders, /close_orders_pause_trading, /balance, /active_trade)
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
                    elif text == "/balance":
                        balance = get_futures_balance()
                        if balance is not None:
                            send_telegram_message(f"💰 Текущий Futures баланс: USDT {balance}")
                        else:
                            send_telegram_message("❌ Не удалось получить баланс.")
                    elif text == "/active_trade":
                        if len(positions_entry_data) == 0:
                            send_telegram_message("ℹ️ Нет открытых позиций.")
                        else:
                            for sym, info in positions_entry_data.items():
                                active_signal = info.get("signal", "N/A")
                                if info.get("tp_perc", 0) != 0 and info.get("sl_perc", 0) != 0:
                                    if active_signal.lower() == "long":
                                        tp_level = info["break_even_price"] * (1 + info["tp_perc"]/100)
                                        sl_level = info["break_even_price"] * (1 - info["sl_perc"]/100)
                                    else:
                                        tp_level = info["break_even_price"] * (1 - info["tp_perc"]/100)
                                        sl_level = info["break_even_price"] * (1 + info["sl_perc"]/100)
                                    tp_sl_message = f"\nTP: {round(tp_level,2)} ({info['tp_perc']}%)\nSL: {round(sl_level,2)} ({info['sl_perc']}%)"
                                else:
                                    tp_sl_message = ""
                                active_message = (
                                    f"🚀 Активная сделка:\n"
                                    f"Символ: {sym}\n"
                                    f"Направление: {active_signal.upper()}\n"
                                    f"Количество: {info.get('quantity', 'N/A')}\n"
                                    f"Цена входа: {info.get('entry_price', 'N/A')}\n"
                                    f"Плечо: {info.get('leverage', 'N/A')}\n"
                                    f"Использованная маржа: {info.get('used_margin', 'N/A')}\n"
                                    f"Цена ликвидации: {info.get('liq_price', 'N/A')}\n"
                                    f"Комиссия входа: {info.get('commission_entry', 'N/A')}\n"
                                    f"Цена безубыточности: {info.get('break_even_price', 'N/A')}"
                                    f"{tp_sl_message}"
                                )
                                send_telegram_message(active_message)
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

    try:
        data = request.get_json(force=True)
    except Exception as e:
        logging.error(f"❌ Ошибка парсинга JSON: {e}")
        return {"status": "error", "message": "JSON parse error"}, 400

    logging.debug(f"DEBUG: Получен JSON: {data}")
    if not data or "signal" not in data:
        logging.error("❌ Нет поля 'signal' в полученных данных")
        return {"status": "error", "message": "No signal provided"}, 400

    signal = data["signal"].lower()
    symbol_received = data.get("symbol", "N/A")
    symbol_fixed = symbol_received.split('.')[0]

    # Динамические параметры: leverage и quantity
    leverage = int(data.get("leverage", 20))
    quantity = float(data.get("quantity", 0.02))

    logging.info(f"📥 Получен сигнал: {signal}")
    logging.info(f"📥 Символ: {symbol_received} -> {symbol_fixed}")
    logging.info(f"📥 Leverage: {leverage}, Quantity: {quantity}")

    current_pos = get_position(symbol_fixed)
    # Если позиция открыта, проверяем направление
    if current_pos and abs(float(current_pos.get("positionAmt", 0))) > 0:
        current_direction = "long" if float(current_pos.get("positionAmt", 0)) > 0 else "short"
        if current_direction != signal:
            # Если сигнал противоположный, переключаем позицию
            result = switch_position(signal, symbol_fixed, leverage, quantity)
            if result["status"] != "ok":
                return result
            # После переключения завершаем выполнение вебхука (новая позиция уже открыта)
            return result
        else:
            msg = f"⚠️ Позиция уже открыта с направлением {current_direction.upper()}. Сигнал {signal.upper()} игнорируется."
            logging.info(msg)
            send_telegram_message(msg)
            return {"status": "skipped", "message": "Position already open."}
    else:
        # Если позиции нет, открываем новую через switch_position
        result = switch_position(signal, symbol_fixed, leverage, quantity)
        if result["status"] != "ok":
            return result
        # После успешного открытия позиции продолжаем обработку для выставления TP/SL

    ticker = binance_client.futures_symbol_ticker(symbol=symbol_fixed)
    last_price = float(ticker["price"])

    exchange_info = binance_client.futures_exchange_info()
    symbol_info = next((s for s in exchange_info["symbols"] if s["symbol"] == symbol_fixed), None)
    if symbol_info:
        min_notional = None
        for f in symbol_info["filters"]:
            if f.get("filterType") == "MIN_NOTIONAL" and "minNotional" in f:
                min_notional = float(f["minNotional"])
                break
        if min_notional is None:
            min_notional = 20.0  # если фильтр не найден, используем 20 USDT по умолчанию
        quantity_precision = int(symbol_info.get("quantityPrecision", 3))
        min_qty_required = round(min_notional / last_price, quantity_precision)
        if quantity < min_qty_required:
            logging.info(f"Количество {quantity} слишком мало, минимальное требуемое: {min_qty_required:.6f}. Автоматически устанавливаем минимальное количество.")
            quantity = min_qty_required

    side = "BUY" if signal == "long" else "SELL"
    quantity = round(quantity, 3)

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
        "signal": signal,
        "entry_price": entry_price,
        "quantity": quantity,
        "leverage": leverage,
        "commission_entry": commission_entry,
        "break_even_price": break_even_price,
        "used_margin": used_margin,
        "liq_price": liq_price,
        "tp_perc": tp_perc,
        "sl_perc": sl_perc
    }

    return {"status": "ok", "signal": signal, "symbol": symbol_fixed}

if __name__ == "__main__":
    threading.Thread(target=poll_telegram_commands, daemon=True).start()
    threading.Thread(target=start_userdata_stream, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
