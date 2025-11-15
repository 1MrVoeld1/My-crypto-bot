import os
import pandas as pd
import ccxt
import asyncio
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from ta.trend import SMAIndicator, EMAIndicator
from ta.momentum import RSIIndicator

TOKEN = os.getenv("TELEGRAM_TOKEN")
YOUR_CHAT_ID = 7239571933  # замените на свой ID

TOP_SYMBOL_LIMIT = 50
TIMEFRAME = "1h"
PERIODS = 50
auto_tasks = {}

exchange = ccxt.bybit({'enableRateLimit': True})

# -------------------- ДАННЫЕ --------------------
def get_top_symbols(limit=TOP_SYMBOL_LIMIT):
    try:
        markets = exchange.load_markets()
        symbols = [s for s in markets if "/USDT" in s and markets[s]['type'] == 'future']
        return symbols[:limit], 0
    except Exception as e:
        print("Ошибка получения символов:", e)
        return [], 3  # код 3 = биржа недоступна

def fetch_ohlcv(symbol: str, timeframe=TIMEFRAME, limit=PERIODS):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        return df, 0
    except Exception as e:
        print(f"Ошибка OHLCV {symbol}: {e}")
        return None, 2  # код 2 = не удалось получить данные

# -------------------- АНАЛИЗ --------------------
def detect_candlestick(df):
    patterns = []
    o, c, h, l = df["open"].iloc[-1], df["close"].iloc[-1], df["high"].iloc[-1], df["low"].iloc[-1]
    if abs(o - c) / (h - l + 1e-9) < 0.1:
        patterns.append("Doji")
    if (h - max(o, c)) > 2 * (max(o, c) - min(o, c)):
        patterns.append("Hammer")
    if len(df) > 1:
        po, pc = df["open"].iloc[-2], df["close"].iloc[-2]
        if c > o and pc < po and c > po and o < pc:
            patterns.append("Bullish Engulfing")
    return patterns

def detect_double_top_bottom(df):
    """
    Простейшая проверка: двойная вершина / двойное дно
    """
    if len(df) < 5:
        return None
    closes = df["close"].iloc[-5:]
    # Двойная вершина
    if closes[0] < closes[1] > closes[2] < closes[3] < closes[4]:
        return "Double Top"
    # Двойное дно
    if closes[0] > closes[1] < closes[2] > closes[3] > closes[4]:
        return "Double Bottom"
    return None

def support_resistance(df):
    sup = df["low"].rolling(10).min().iloc[-1]
    res = df["high"].rolling(10).max().iloc[-1]
    return sup, res

def analyze_symbol(df, symbol_name):
    price = df["close"].iloc[-1]
    patterns = detect_candlestick(df)
    figure = detect_double_top_bottom(df)
    support, resistance = support_resistance(df)
    sma = SMAIndicator(df["close"], 20).sma_indicator().iloc[-1]
    ema = EMAIndicator(df["close"], 20).ema_indicator().iloc[-1]
    rsi = RSIIndicator(df["close"], 14).rsi().iloc[-1]

    side = "HOLD"
    reason = []

    if rsi < 30:
        side = "LONG"
        reason.append("RSI < 30")
    elif rsi > 70:
        side = "SHORT"
        reason.append("RSI > 70")

    if ema > sma:
        side = "LONG"
        reason.append("EMA > SMA")
    elif ema < sma:
        side = "SHORT"
        reason.append("EMA < SMA")

    if patterns:
        reason.append("Pat:" + ",".join(patterns))
    if figure:
        reason.append("Fig:" + figure)

    if side == "LONG":
        risk = (price - support) / price * 100
    elif side == "SHORT":
        risk = (resistance - price) / price * 100
    else:
        risk = 0

    close_in = "2h" if side != "HOLD" else "-"
    return f"{symbol_name[:10]} | {price:.2f}$ | Close: {close_in} | {'; '.join(reason)} | Risk: {risk:.2f}%"

# -------------------- TELEGRAM --------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/nowsignal – сейчас\n"
        "/auto15 – каждые 15 мин\n"
        "/auto30 – каждые 30 мин\n"
        "/auto60 – каждый час\n"
        "/stopauto – остановить\n"
        "/debug – статус"
    )

async def nowsignal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Собираю данные...")
    symbols, code = get_top_symbols()
    if code != 0 or not symbols:
        await update.message.reply_text(f"Ошибка! Биржа недоступна, код ошибки: {code}")
        return

    msgs = []
    for sym in symbols:
        df, df_code = await asyncio.to_thread(fetch_ohlcv, sym)
        if df is not None:
            msgs.append(analyze_symbol(df, sym))
        else:
            await update.message.reply_text(f"Ошибка! Данные с {sym} не получены, код ошибки: {df_code}")
    if msgs:
        await update.message.reply_text("\n".join(msgs[:40]))

async def debug_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    token_ok = True if TOKEN else False
    try:
        exchange.load_markets()
        exchange_ok = True
    except:
        exchange_ok = False
    await update.message.reply_text(
        f"TOKEN: {token_ok}\n"
        f"Подключение к бирже: {exchange_ok}\n"
        f"Auto signals: {list(auto_tasks.keys()) or 'Off'}"
    )

# -------------------- ФОН АВТО --------------------
async def auto_loop(app, chat_id, interval):
    while True:
        symbols, code = get_top_symbols()
        if code != 0:
            await app.bot.send_message(chat_id, f"Ошибка! Биржа недоступна, код: {code}")
            await asyncio.sleep(interval*60)
            continue

        msgs = []
        for sym in symbols:
            df, df_code = await asyncio.to_thread(fetch_ohlcv, sym)
            if df is not None:
                msgs.append(analyze_symbol(df, sym))
            else:
                await app.bot.send_message(chat_id, f"Ошибка! Данные с {sym} не получены, код: {df_code}")
        if msgs:
            await app.bot.send_message(chat_id, "\n".join(msgs[:40]))
        await asyncio.sleep(interval*60)

# -------------------- КОМАНДЫ АВТО --------------------
async def start_auto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "30" not in auto_tasks:
        task = asyncio.create_task(auto_loop(context.application, update.message.chat_id, 30))
        auto_tasks["30"] = task
        await update.message.reply_text("Автосигналы каждые 30 минут запущены!")

async def start_auto15(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "15" not in auto_tasks:
        task = asyncio.create_task(auto_loop(context.application, update.message.chat_id, 15))
        auto_tasks["15"] = task
        await update.message.reply_text("Автосигналы каждые 15 минут запущены!")

async def start_auto60(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "60" not in auto_tasks:
        task = asyncio.create_task(auto_loop(context.application, update.message.chat_id, 60))
        auto_tasks["60"] = task
        await update.message.reply_text("Автосигналы каждый час запущены!")

async def stop_auto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for key, task in auto_tasks.items():
        task.cancel()
    auto_tasks.clear()
    await update.message.reply_text("Автосигналы остановлены!")

# -------------------- MAIN --------------------
def main():
    if not TOKEN:
        print("Ошибка: TELEGRAM_TOKEN не найден! Код ошибки 1")
        return

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("nowsignal", nowsignal_cmd))
    app.add_handler(CommandHandler("debug", debug_cmd))
    app.add_handler(CommandHandler("auto15", start_auto15))
    app.add_handler(CommandHandler("auto30", start_auto))
    app.add_handler(CommandHandler("auto60", start_auto60))
    app.add_handler(CommandHandler("stopauto", stop_auto))

    app.run_polling()

if __name__ == "__main__":
    main()
