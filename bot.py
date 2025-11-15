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

auto_tasks = {}  # словарь для задач авто-сигналов

# Инициализация биржи
exchange = ccxt.bybit({'enableRateLimit': True})

# -------------------- ДАННЫЕ --------------------
def get_top_symbols(limit=TOP_SYMBOL_LIMIT):
    try:
        markets = exchange.load_markets()
        symbols = [s for s in markets if "/USDT" in s and markets[s]['type'] == 'future']
        return symbols[:limit]
    except Exception as e:
        print("Ошибка получения символов:", e)
        return []

def fetch_ohlcv(symbol: str, timeframe=TIMEFRAME, limit=PERIODS):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        return df
    except Exception as e:
        return e  # возвращаем исключение, чтобы знать код ошибки

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

def support_resistance(df):
    sup = df["low"].rolling(10).min().iloc[-1]
    res = df["high"].rolling(10).max().iloc[-1]
    return sup, res

def analyze_symbol(df, symbol_name):
    price = df["close"].iloc[-1]
    patterns = detect_candlestick(df)
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
    symbols = get_top_symbols()
    msgs = []
    for sym in symbols:
        df = await asyncio.to_thread(fetch_ohlcv, sym)
        if isinstance(df, Exception):
            await update.message.reply_text(f"Ошибка! Данные с биржи не получены: {df}")
            return
        elif df is not None:
            msgs.append(analyze_symbol(df, sym))

    if msgs:
        await update.message.reply_text("\n".join(msgs[:40]))
    else:
        await update.message.reply_text("Ошибка: данные не получены")

async def debug_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    token_ok = bool(TOKEN)
    try:
        exchange.load_markets()
        exchange_ok = True
    except Exception as e:
        exchange_ok = False

    await update.message.reply_text(
        f"TOKEN: {token_ok}\n"
        f"Подключение к бирже: {exchange_ok}\n"
        f"Auto signals: {list(auto_tasks.keys()) or 'Off'}"
    )

# -------------------- ФОН АВТО --------------------
async def auto_loop(app, chat_id, interval):
    while True:
        symbols = get_top_symbols()
        msgs = []
        for sym in symbols:
            df = await asyncio.to_thread(fetch_ohlcv, sym)
            if isinstance(df, Exception):
                await app.bot.send_message(chat_id, f"Ошибка биржи: {df}")
                continue
            elif df is not None:
                msgs.append(analyze_symbol(df, sym))
        if msgs:
            await app.bot.send_message(chat_id, "\n".join(msgs[:40]))
        await asyncio.sleep(interval * 60)

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
