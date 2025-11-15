import os
import pandas as pd
import numpy as np
import ccxt
import asyncio
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from ta.trend import SMAIndicator, EMAIndicator
from ta.momentum import RSIIndicator
from datetime import datetime

TOKEN = os.getenv("TELEGRAM_TOKEN")
if TOKEN is None:
    print("Ошибка: TELEGRAM_TOKEN не найден!")
    exit(1)

YOUR_CHAT_ID = 7239571933  # ← ЗАМЕНИ НА СВОЙ ID

TOP_SYMBOL_LIMIT = 50
TIMEFRAME = "1h"
PERIODS = 50
auto_enabled = False

exchange = ccxt.bybit({'enableRateLimit': True})

# =========================================
# ФУНКЦИИ ДАННЫХ
# =========================================
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
        print(f"Ошибка OHLCV {symbol}: {e}")
        return None

# =========================================
# АНАЛИЗ
# =========================================
def detect_candlestick(df):
    patterns = []
    o, c, h, l = df["open"].iloc[-1], df["close"].iloc[-1], df["high"].iloc[-1], df["low"].iloc[-1]

    if abs(o - c) / (h - l + 1e-9) < 0.1:
        patterns.append("Doji")

    if (h - max(o, c)) > 2 * (max(o, c) - min(o, c)):
        patterns.append("Hammer")

    if len(df) > 1:
        p_o, p_c = df["open"].iloc[-2], df["close"].iloc[-2]
        if c > o and p_c < p_o and c > p_o and o < p_c:
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

    return f"{symbol_name} | Price: {price:.2f} | Close in: {close_in} | Reason: {'; '.join(reason)} | Risk: {risk:.2f}%"

# =========================================
# КОМАНДЫ TG
# =========================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Бот запущен!\n"
        "/nowsignal – получить анализ сейчас\n"
        "/auto – включить автосигналы\n"
        "/stopauto – отключить автосигналы\n"
        "/debug – статус"
    )

async def nowsignal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Собираю данные...")
    symbols = get_top_symbols()
    msgs = []

    for sym in symbols:
        df = await asyncio.to_thread(fetch_ohlcv, sym)
        if df is not None:
            msgs.append(analyze_symbol(df, sym))

    await update.message.reply_text("\n".join(msgs[:40]))

async def auto_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global auto_enabled
    auto_enabled = True
    await update.message.reply_text("Автосигналы ВКЛ.")

async def stopauto_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global auto_enabled
    auto_enabled = False
    await update.message.reply_text("Автосигналы ВЫКЛ.")

async def debug_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Бот работает. TOKEN: {'yes' if TOKEN else 'NO'} | Auto: {auto_enabled}"
    )

# =========================================
# ФОНОВЫЙ ЦИКЛ АВТО-СИГНАЛОВ
# =========================================
async def auto_loop(app):
    global auto_enabled
    while True:
        await asyncio.sleep(180)  # каждые 3 минуты
        if auto_enabled:
            symbols = get_top_symbols()
            msgs = []
            for sym in symbols:
                df = await asyncio.to_thread(fetch_ohlcv, sym)
                if df is not None:
                    msgs.append(analyze_symbol(df, sym))
            if msgs:
                await app.bot.send_message(YOUR_CHAT_ID, "\n".join(msgs[:40]))

# =========================================
# ЗАПУСК
# =========================================
def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("nowsignal", nowsignal_cmd))
    app.add_handler(CommandHandler("auto", auto_cmd))
    app.add_handler(CommandHandler("stopauto", stopauto_cmd))
    app.add_handler(CommandHandler("debug", debug_cmd))

    # запускаем авто-цикл
    asyncio.get_event_loop().create_task(auto_loop(app))

    # запускаем без asyncio.run() !!!
    app.run_polling()

if __name__ == "__main__":
    main()
