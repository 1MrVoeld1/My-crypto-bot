import os
import requests
import pandas as pd
import numpy as np
from telegram.ext import Updater, CommandHandler
from telegram.ext import JobQueue
from ta.trend import SMAIndicator, EMAIndicator
from ta.momentum import RSIIndicator, StochasticOscillator
from ta.volatility import BollingerBands, AverageTrueRange

TOKEN = os.getenv("TELEGRAM_TOKEN")
INTERVAL_SECONDS = 30*60  # каждые 30 минут

# ===== Функции Bybit API =====
def get_top_symbols(limit=250):
    url = "https://api.bybit.com/v2/public/tickers"
    resp = requests.get(url).json()
    symbols = [s["symbol"] for s in resp["result"] if s["quote_currency"]=="USDT"]
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

# ===== Индикаторы и паттерны =====
def detect_candlestick(df):
    patterns = []
    open_p = df["open"].iloc[-1]
    close_p = df["close"].iloc[-1]
    high = df["high"].iloc[-1]
    low = df["low"].iloc[-1]
    
    # Doji
    if abs(open_p - close_p) / (high - low + 1e-9) < 0.1:
        patterns.append("Doji")
    # Hammer
    if (high - max(open_p, close_p)) > 2*(max(open_p, close_p) - min(open_p, close_p)):
        patterns.append("Hammer")
    # Engulfing
    if len(df) > 1:
        prev_open = df["open"].iloc[-2]
        prev_close = df["close"].iloc[-2]
        if (close_p > open_p and prev_close < prev_open and close_p > prev_open and open_p < prev_close):
            patterns.append("Engulfing")
    return patterns

def support_resistance(df):
    highs = df["high"]
    lows = df["low"]
    support = lows.rolling(window=10).min().iloc[-1]
    resistance = highs.rolling(window=10).max().iloc[-1]
    return support, resistance

def analyze_symbol(df):
    # Индикаторы тренда
    sma = SMAIndicator(df["close"], window=20).sma_indicator().iloc[-1]
    ema = EMAIndicator(df["close"], window=20).ema_indicator().iloc[-1]

    # Индикаторы перекупленности/перепроданности
    rsi = RSIIndicator(df["close"], window=14).rsi().iloc[-1]
    stoch = StochasticOscillator(df["high"], df["low"], df["close"], window=14).stoch().iloc[-1]

    # Волатильность
    atr = AverageTrueRange(df["high"], df["low"], df["close"], window=14).average_true_range().iloc[-1]
    bb_high = BollingerBands(df["close"], window=20).bollinger_hband().iloc[-1]
    bb_low = BollingerBands(df["close"], window=20).bollinger_lband().iloc[-1]

    # Объёмы
    vol = df["volume"].iloc[-1]

    # Свечные паттерны
    patterns = detect_candlestick(df)

    # Уровни поддержки/сопротивления
    support, resistance = support_resistance(df)

    # Логика сигнала
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
        reason.append("EMA > SMA (восходящий тренд)")
    elif ema < sma:
        side = "SHORT"
        reason.append("EMA < SMA (нисходящий тренд)")

    if patterns:
        reason.append("Паттерн: " + ",".join(patterns))
    
    price = df["close"].iloc[-1]

    # Простейший расчет риска: расстояние до поддержки/сопротивления в %
    if side == "LONG":
        risk = (price - support)/price*100
    elif side == "SHORT":
        risk = (resistance - price)/price*100
    else:
        risk = 0

    reason_str = "; ".join(reason) if reason else "Без явной причины"
    return f"{df['symbol'].iloc[-1]} {side} {price:.2f} 24ч | Причина: {reason_str} | Риск: {risk:.2f}%"

# ===== Автосигналы =====
def auto_signal(context):
    chat_id = context.job.context["chat_id"]
    symbols = get_top_symbols()
    signals = []
    for sym in symbols:
        try:
            df = get_ohlcv(sym)
            if df is not None:
                df["symbol"] = sym
                signal = analyze_symbol(df)
                signals.append(signal)
        except:
            continue
    if signals:
        context.bot.send_message(chat_id=chat_id, text="\n".join(signals))

# ===== Telegram команды =====
def start_auto(update, context):
    chat_id = update.message.chat_id
    update.message.reply_text("Автосигналы включены! Каждые 30 минут будут приходить сигналы.")
    context.job_queue.run_repeating(auto_signal, interval=INTERVAL_SECONDS, first=0, context={"chat_id": chat_id})

def start(update, context):
    update.message.reply_text("Бот запущен! Используй /auto для автосигналов.")

# ===== Запуск бота =====
from telegram.ext import ApplicationBuilder, CommandHandler

if __name__" == "_main_":
    app = ApplicationBuilder().token(TOKEN).build()
    
    # Регистрируем команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("auto", start_auto))
    
    # Запуск бота
    app.run_polling()
