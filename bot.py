import os
import pandas as pd
import numpy as np
import ccxt
import plotly.graph_objects as go
from ta.trend import SMAIndicator, EMAIndicator
from ta.momentum import RSIIndicator
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from datetime import datetime

TOKEN = os.getenv("TELEGRAM_TOKEN")
if TOKEN is None:
    print("Ошибка: TELEGRAM_TOKEN не найден!")
    exit(1)

TOP_SYMBOL_LIMIT = 250
PERIODS = 50  # количество свечей для анализа

# ==============================
# CCXT и Bybit
# ==============================
exchange = ccxt.bybit({
    'enableRateLimit': True,
})

def fetch_ohlcv(symbol: str, timeframe="1h", limit=PERIODS):
    """
    Получаем исторические свечи с Bybit через ccxt
    """
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        return df
    except Exception as e:
        print(f"Ошибка OHLCV для {symbol}: {e}")
        return None

def get_top_symbols(limit=TOP_SYMBOL_LIMIT):
    """
    Получаем USDT деривативы с Bybit через ccxt
    """
    try:
        markets = exchange.load_markets()
        symbols = [s for s in markets if "/USDT" in s and markets[s]['type'] == 'future']
        return symbols[:limit]
    except Exception as e:
        print("Ошибка получения символов:", e)
        return []

# ==============================
# Индикаторы и паттерны
# ==============================
def detect_candlestick(df):
    patterns = []
    open_p, close_p, high, low = df["open"].iloc[-1], df["close"].iloc[-1], df["high"].iloc[-1], df["low"].iloc[-1]

    # Doji
    if abs(open_p - close_p) / (high - low + 1e-9) < 0.1:
        patterns.append("Doji")
    # Hammer
    if (high - max(open_p, close_p)) > 2*(max(open_p, close_p) - min(open_p, close_p)):
        patterns.append("Hammer")
    # Bullish Engulfing
    if len(df) > 1:
        prev_open, prev_close = df["open"].iloc[-2], df["close"].iloc[-2]
        if (close_p > open_p and prev_close < prev_open and close_p > prev_open and open_p < prev_close):
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

    # Риск
    if side == "LONG":
        risk = (price - support)/price*100
    elif side == "SHORT":
        risk = (resistance - price)/price*100
    else:
        risk = 0

    reason_str = "; ".join(reason) if reason else "Нет явной причины"
    return f"{symbol_name} {side} {price:.6f} | {reason_str} | Risk:{risk:.2f}%"

def plot_candles(df, symbol_name):
    fig = go.Figure(data=[go.Candlestick(
        x=df["timestamp"],
        open=df["open"],
        high=df["high"],
        low=df["low"],
        close=df["close"],
        name=symbol_name
    )])
    fig.update_layout(title=symbol_name, xaxis_title="Time", yaxis_title="Price")
    filename = f"{symbol_name.replace('/','_')}.png"
    fig.write_image(filename)
    return filename

# ==============================
# Telegram команды
# ==============================
async def start(update, context):
    await update.message.reply_text(
        "Бот запущен!\n"
        "/nowsignal – сигнал прямо сейчас\n"
        "/debug – статус бота"
    )

async def nowsignal_cmd(update, context):
    await update.message.reply_text("Собираю сигналы с реальными данными...")
    symbols = get_top_symbols()
    if not symbols:
        await update.message.reply_text("Ошибка: не удалось получить список символов.")
        return

    signals = []
    for sym in symbols:
        df = fetch_ohlcv(sym)
        if df is not None:
            try:
                signal = analyze_symbol(df, sym)
                signals.append(signal)
                # график последней свечи
                img = plot_candles(df.tail(50), sym)
                await update.message.reply_photo(photo=open(img, "rb"))
            except Exception as e:
                print(f"Ошибка анализа {sym}: {e}")
                continue
    if signals:
        await update.message.reply_text("\n".join(signals))
    else:
        await update.message.reply_text("Не удалось получить данные.")

async def debug_cmd(update, context):
    await update.message.reply_text("Бот работает. ТОКЕН найден: yes")

# ==============================
# Запуск
# ==============================
if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("nowsignal", nowsignal_cmd))
    app.add_handler(CommandHandler("debug", debug_cmd))
    app.run_polling()
