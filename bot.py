import os
import requests
import pandas as pd
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from ta.trend import SMAIndicator, EMAIndicator
from ta.momentum import RSIIndicator

TOKEN = os.getenv("TELEGRAM_TOKEN")
if TOKEN is None:
    print("Ошибка: TELEGRAM_TOKEN не найден!")
    exit(1)

TOP_SYMBOL_LIMIT = 250
auto_enabled = False

# ==============================
#  Актуальный Bybit API (v5)
# ==============================
def get_top_symbols(limit=TOP_SYMBOL_LIMIT):
    url = "https://api.bybit.com/v5/market/instruments-info?category=linear"
    try:
        data = requests.get(url, timeout=10).json()
        rows = data.get("result", {}).get("list", [])
        symbols = [x["symbol"] for x in rows if x.get("quoteCoin") == "USDT"]
        return symbols[:limit]
    except Exception as e:
        print("Ошибка получения символов:", e)
        return []

def get_ohlcv(symbol, interval="60", limit=200):
    url = f"https://api.bybit.com/v5/market/kline?category=linear&symbol={symbol}&interval={interval}&limit={limit}"
    try:
        data = requests.get(url, timeout=10).json()
        klines = data.get("result", {}).get("list", [])
        if not klines:
            return None
        df = pd.DataFrame(klines, columns=["timestamp", "open", "high", "low", "close", "volume", "turnover"])
        df[["open","high","low","close","volume"]] = df[["open","high","low","close","volume"]].astype(float)
        return df
    except Exception as e:
        print("Ошибка OHLCV:", e)
        return None

# ==============================
# Индикаторы и паттерны
# ==============================
def detect_candlestick(df):
    patterns = []
    open_p = df["open"].iloc[-1]
    close_p = df["close"].iloc[-1]
    high = df["high"].iloc[-1]
    low = df["low"].iloc[-1]

    if abs(open_p - close_p) / (high - low + 1e-9) < 0.1:
        patterns.append("Doji")
    if (high - max(open_p, close_p)) > 2 * (max(open_p, close_p) - min(open_p, close_p)):
        patterns.append("Hammer")
    if len(df) > 1:
        prev_open = df["open"].iloc[-2]
        prev_close = df["close"].iloc[-2]
        if (close_p > open_p and prev_close < prev_open and close_p > prev_open and open_p < prev_close):
            patterns.append("Bullish Engulfing")
    return patterns

def support_resistance(df):
    sup = df["low"].rolling(10, min_periods=1).min().iloc[-1]
    res = df["high"].rolling(10, min_periods=1).max().iloc[-1]
    return sup, res

def analyze_symbol(df, symbol_name):
    sma = SMAIndicator(df["close"], 20).sma_indicator().iloc[-1]
    ema = EMAIndicator(df["close"], 20).ema_indicator().iloc[-1]
    rsi = RSIIndicator(df["close"], 14).rsi().iloc[-1]
    price = df["close"].iloc[-1]
    patterns = detect_candlestick(df)
    support, resistance = support_resistance(df)

    side = "HOLD"
    reason = []

    if rsi is not None:
        if rsi < 30:
            side = "LONG"
            reason.append("RSI < 30")
        elif rsi > 70:
            side = "SHORT"
            reason.append("RSI > 70")

    if ema is not None and sma is not None:
        if ema > sma:
            side = "LONG"
            reason.append("EMA > SMA")
        elif ema < sma:
            side = "SHORT"
            reason.append("EMA < SMA")

    if patterns:
        reason.append("Pat:" + ",".join(patterns))

    if side == "LONG":
        risk = (price - support) / price * 100 if price and support else 0
    elif side == "SHORT":
        risk = (resistance - price) / price * 100 if price and resistance else 0
    else:
        risk = 0

    reason_str = "; ".join(reason) if reason else "Нет явной причины"
    return f"{symbol_name} {side} {price:.6f} | {reason_str} | Risk:{risk:.2f}%"

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

async def auto_cmd(update, context):
    global auto_enabled
    auto_enabled = True
    await update.message.reply_text("Автосигналы включены! Используй /nowsignal для ручного сбора сигналов.")

async def stop_auto_cmd(update, context):
    global auto_enabled
    auto_enabled = False
    await update.message.reply_text("Автосигналы отключены!")

async def nowsignal_cmd(update, context):
    await update.message.reply_text("Собираю сигналы прямо сейчас...")
    symbols = get_top_symbols()
    if not symbols:
        await update.message.reply_text("Ошибка: не удалось получить список символов.")
        return
    signals = []
    for sym in symbols:
        df = get_ohlcv(sym)
        if df is not None:
            try:
                signals.append(analyze_symbol(df, sym))
            except Exception:
                continue
    if signals:
        await update.message.reply_text("\n".join(signals))
    else:
        await update.message.reply_text("Не удалось получить данные.")

async def debug_cmd(update, context):
    global auto_enabled
    msg = f"TOKEN set: {'yes' if TOKEN else 'no'}\nАвтосигналы включены: {auto_enabled}\n"
    try:
        resp = requests.get("https://api.bybit.com/v5/market/instruments-info?category=linear", timeout=5).json()
        rows = resp.get("result", {}).get("list", [])
        msg += f"Bybit API OK, symbols found: {len(rows)}"
    except Exception as e:
        msg += f"Bybit API FAIL ({e})"
    await update.message.reply_text(msg)

# ==============================
# Запуск
# ==============================
if _name_ == "_main_":
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("auto", auto_cmd))
    app.add_handler(CommandHandler("stopauto", stop_auto_cmd))
    app.add_handler(CommandHandler("nowsignal", nowsignal_cmd))
    app.add_handler(CommandHandler("debug", debug_cmd))
    app.run_polling()
