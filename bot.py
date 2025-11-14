import os
import pandas as pd
import numpy as np
import ccxt
import asyncio
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from ta.trend import SMAIndicator, EMAIndicator
from ta.momentum import RSIIndicator

TOKEN = os.getenv("TELEGRAM_TOKEN")
if TOKEN is None:
    print("Ошибка: TELEGRAM_TOKEN не найден!")
    exit(1)

TOP_SYMBOL_LIMIT = 50
TIMEFRAME = "1h"
PERIODS = 50
AUTO_INTERVAL = 3600  # 1 час
auto_enabled = False
auto_chat_ids = []

exchange = ccxt.bybit({'enableRateLimit': True})

# ==============================
# Получение символов и OHLCV
# ==============================
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

# ==============================
# Индикаторы и паттерны
# ==============================
def detect_candlestick(df):
    patterns = []
    open_p, close_p, high, low = df["open"].iloc[-1], df["close"].iloc[-1], df["high"].iloc[-1], df["low"].iloc[-1]
    if abs(open_p - close_p) / (high - low + 1e-9) < 0.1:
        patterns.append("Doji")
    if (high - max(open_p, close_p)) > 2*(max(open_p, close_p) - min(open_p, close_p)):
        patterns.append("Hammer")
    if len(df) > 1:
        prev_open, prev_close = df["open"].iloc[-2], df["close"].iloc[-2]
        if close_p > open_p and prev_close < prev_open and close_p > prev_open and open_p < prev_close:
            patterns.append("Bullish Engulfing")
    return patterns

def support_resistance(df):
    sup = df["low"].rolling(10, min_periods=1).min().iloc[-1]
    res = df["high"].rolling(10, min_periods=1).max().iloc[-1]
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
        risk = (price - support)/price*100
    elif side == "SHORT":
        risk = (resistance - price)/price*100
    else:
        risk = 0

    close_in = "2h" if side != "HOLD" else "-"
    reason_str = "; ".join(reason) if reason else "Нет явной причины"
    return f"{symbol_name} | Price: {price:.2f} | Close in: {close_in} | Reason: {reason_str} | Risk: {risk:.2f}%"

# ==============================
# Telegram команды
# ==============================
async def start(update, context):
    await update.message.reply_text(
        "Бот запущен!\n"
        "/auto – включить автосигналы\n"
        "/stopauto – отключить\n"
        "/nowsignal – сигнал прямо сейчас\n"
        "/debug – статус бота"
    )

async def nowsignal_cmd(update, context):
    await update.message.reply_text("Собираю сигналы...")
    symbols = get_top_symbols()
    if not symbols:
        await update.message.reply_text("Ошибка: не удалось получить список символов.")
        return
    signals = []
    for sym in symbols:
        df = await asyncio.to_thread(fetch_ohlcv, sym)
        if df is not None:
            signals.append(analyze_symbol(df, sym))
    if signals:
        await update.message.reply_text("\n".join(signals[:50]))
    else:
        await update.message.reply_text("Не удалось получить данные.")

async def auto_cmd(update, context):
    global auto_enabled
    auto_enabled = True
    chat_id = update.effective_chat.id
    if chat_id not in auto_chat_ids:
        auto_chat_ids.append(chat_id)
    await update.message.reply_text("Автосигналы включены!")

async def stop_auto_cmd(update, context):
    global auto_enabled
    auto_enabled = False
    await update.message.reply_text("Автосигналы отключены!")

async def debug_cmd(update, context):
    await update.message.reply_text(f"Бот работает. TOKEN найден: {'yes' if TOKEN else 'no'} | Автосигналы: {auto_enabled}")

# ==============================
# Автосигналы через asyncio.loop
# ==============================
async def auto_loop(app):
    global auto_enabled
    while True:
        if auto_enabled:
            symbols = get_top_symbols()
            messages = []
            for sym in symbols:
                df = await asyncio.to_thread(fetch_ohlcv, sym)
                if df is not None:
                    messages.append(analyze_symbol(df, sym))
            if messages:
                for chat_id in auto_chat_ids:
                    await app.bot.send_message(chat_id, "\n".join(messages[:50]))
        await asyncio.sleep(AUTO_INTERVAL)

# ==============================
# Запуск
# ==============================
if __name__ == "__main__":
    async def main():
        app = ApplicationBuilder().token(TOKEN).build()
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("nowsignal", nowsignal_cmd))
        app.add_handler(CommandHandler("auto", auto_cmd))
        app.add_handler(CommandHandler("stopauto", stop_auto_cmd))
        app.add_handler(CommandHandler("debug", debug_cmd))

        # Запускаем автосигналы после старта polling
        asyncio.create_task(auto_loop(app))

        await app.run_polling()

    asyncio.run(main())
