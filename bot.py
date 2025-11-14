import os
import requests
import pandas as pd
import numpy as np
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from ta.trend import SMAIndicator, EMAIndicator
from ta.momentum import RSIIndicator, StochasticOscillator
from ta.volatility import BollingerBands, AverageTrueRange

TOKEN = os.getenv("TELEGRAM_TOKEN")
INTERVAL_SECONDS = 30 * 60  # каждые 30 минут

if TOKEN is None:
    print("Ошибка: TELEGRAM_TOKEN не найден!")
    exit(1)

# ===== Bybit API =====
def get_top_symbols(limit=250):
    url = "https://api.bybit.com/v2/public/tickers"
    resp = requests.get(url).json()
    symbols = [s["symbol"] for s in resp["result"] if s["quote_currency"] == "USDT"]
    return symbols[:limit]

def get_ohlcv(symbol, interval="1h", limit=100):
    url = f"https://api.bybit.com/v2/public/kline/list?symbol={symbol}&interval={interval}&limit={limit}"
    r = requests.get(url).json()
    data = r.get("result", [])
    if not data:
        return None
    df = pd.DataFrame(data)
    df["close"] = df["close"].astype(float)
    df["open"] = df["open"].astype(float)
    df["high"] = df["high"].astype(float)
    df["low"] = df["low"].astype(float)
    df["volume"] = df["volume"].astype(float)
    return df

# ===== Паттерны =====
def detect_candlestick(df):
    patterns = []
    o = df["open"].iloc[-1]
    c = df["close"].iloc[-1]
    h = df["high"].iloc[-1]
    l = df["low"].iloc[-1]

    # Doji
    if abs(o - c) / (h - l + 1e-9) < 0.1:
        patterns.append("Doji")

    # Hammer
    if (h - max(o, c)) > 2 * (max(o, c) - min(o, c)):
        patterns.append("Hammer")

    # Engulfing
    if len(df) > 1:
        po = df["open"].iloc[-2]
        pc = df["close"].iloc[-2]
        if (c > o and pc < po and c > po and o < pc):
            patterns.append("Engulfing")

    return patterns

def support_resistance(df):
    highs = df["high"]
    lows = df["low"]
    support = lows.rolling(window=10).min().iloc[-1]
    resistance = highs.rolling(window=10).max().iloc[-1]
    return support, resistance

# ===== Аналитика =====
def analyze_symbol(df, symbol_name):
    sma = SMAIndicator(df["close"], window=20).sma_indicator().iloc[-1]
    ema = EMAIndicator(df["close"], window=20).ema_indicator().iloc[-1]
    rsi = RSIIndicator(df["close"], window=14).rsi().iloc[-1]
    stoch = StochasticOscillator(df["high"], df["low"], df["close"], window=14).stoch().iloc[-1]
    atr = AverageTrueRange(df["high"], df["low"], df["close"], window=14).average_true_range().iloc[-1]
    bb_h = BollingerBands(df["close"], window=20).bollinger_hband().iloc[-1]
    bb_l = BollingerBands(df["close"], window=20).bollinger_lband().iloc[-1]

    vol = df["volume"].iloc[-1]
    patterns = detect_candlestick(df)
    support, resistance = support_resistance(df)

    price = df["close"].iloc[-1]

    side = "HOLD"
    reason = []

    if rsi < 30:
        side = "LONG"
        reason.append("RSI < 30 (перепроданность)")
    elif rsi > 70:
        side = "SHORT"
        reason.append("RSI > 70 (перекупленность)")

    if ema > sma:
        side = "LONG"
        reason.append("EMA > SMA (тренд вверх)")
    elif ema < sma:
        side = "SHORT"
        reason.append("EMA < SMA (тренд вниз)")

    if patterns:
        reason.append("Паттерн: " + ",".join(patterns))

    if side == "LONG":
        risk = (price - support) / price * 100
    elif side == "SHORT":
        risk = (resistance - price) / price * 100
    else:
        risk = 0

    reason_str = "; ".join(reason) if reason else "Нет причины"
    return f"{symbol_name} → {side} | Цена: {price:.4f} | Причина: {reason_str} | Риск: {risk:.2f}%"

# ===== Автосигналы =====
async def auto_signal(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.context["chat_id"]

    symbols = get_top_symbols()
    signals = []

    for sym in symbols:
        try:
            df = get_ohlcv(sym)
            if df is not None:
                signals.append(analyze_symbol(df, sym))
        except Exception:
            continue

    if signals:
        await context.bot.send_message(chat_id=chat_id, text="\n".join(signals))

# ===== Telegram команды =====
async def start(update, context):
    await update.message.reply_text(
        "Бот работает! Команды:\n"
        "/auto — включить авто-сигналы\n"
        "/stopauto — отключить\n"
        "/nowsignal — получить сигнал прямо сейчас"
    )

async def start_auto(update, context):
    chat_id = update.message.chat_id
    await update.message.reply_text("Автосигналы включены!")

    context.job_queue.run_repeating(
        auto_signal,
        interval=INTERVAL_SECONDS,
        first=3,
        context={"chat_id": chat_id},
        name=str(chat_id)
    )

async def stop_auto(update, context):
    chat_id = update.message.chat_id
    await update.message.reply_text("Автосигналы отключены!")

    jobs = context.job_queue.get_jobs_by_name(str(chat_id))
    for job in jobs:
        job.schedule_removal()

async def now_signal(update, context):
    chat_id = update.message.chat_id

    await update.message.reply_text("Делаю анализ...")

    symbols = get_top_symbols()
    signals = []

    for sym in symbols:
        try:
            df = get_ohlcv(sym)
            if df is not None:
                signals.append(analyze_symbol(df, sym))
        except Exception:
            continue

    if signals:
        await update.message.reply_text("\n".join(signals))
    else:
        await update.message.reply_text("Не удалось получить данные!")

# ===== Запуск =====
if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("auto", start_auto))
    app.add_handler(CommandHandler("stopauto", stop_auto))
    app.add_handler(CommandHandler("nowsignal", now_signal))

    app.run_polling()
