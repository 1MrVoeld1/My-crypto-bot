import os
import requests
import pandas as pd
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from ta.trend import SMAIndicator, EMAIndicator
from ta.momentum import RSIIndicator, StochasticOscillator
from ta.volatility import BollingerBands, AverageTrueRange

TOKEN = os.getenv("TELEGRAM_TOKEN")
INTERVAL_SECONDS = 30 * 60  # 30 минут

# <-- Настройка: сколько символов брать (изменено на 250) -->
TOP_SYMBOL_LIMIT = 250

if TOKEN is None:
    print("Ошибка: TELEGRAM_TOKEN не найден!")
    exit(1)


# ==============================
#  Актуальный Bybit API (v5)
# ==============================
def get_top_symbols(limit=TOP_SYMBOL_LIMIT):
    """
    Получить USDT деривативы через Bybit API v5.
    """
    url = "https://api.bybit.com/v5/market/instruments-info?category=linear"

    try:
        data = requests.get(url, timeout=10).json()
        rows = data.get("result", {}).get("list", [])
        # Фильтруем только USDT пары (деривативы)
        symbols = [x["symbol"] for x in rows if x.get("quoteCoin") == "USDT" or x.get("quoteCoin") == "USDT" ]
        return symbols[:limit]
    except Exception as e:
        print("Ошибка получения символов:", e)
        return []


def get_ohlcv(symbol, interval="60", limit=200):
    """
    Получить свечи: interval=60 → 1 час.
    """
    url = (
        "https://api.bybit.com/v5/market/kline"
        f"?category=linear&symbol={symbol}&interval={interval}&limit={limit}"
    )

    try:
        data = requests.get(url, timeout=10).json()
        klines = data.get("result", {}).get("list", [])
        if not klines:
            return None

        df = pd.DataFrame(
            klines,
            columns=["timestamp", "open", "high", "low", "close", "volume", "turnover"]
        )

        df["open"] = df["open"].astype(float)
        df["high"] = df["high"].astype(float)
        df["low"] = df["low"].astype(float)
        df["close"] = df["close"].astype(float)
        df["volume"] = df["volume"].astype(float)

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

    # Doji
    if abs(open_p - close_p) / (high - low + 1e-9) < 0.1:
        patterns.append("Doji")

    # Hammer (упрощённая проверка длинной тени)
    if (high - max(open_p, close_p)) > 2 * (max(open_p, close_p) - min(open_p, close_p)):
        patterns.append("Hammer")

    # Engulfing (бычье поглощение)
    if len(df) > 1:
        prev_open = df["open"].iloc[-2]
        prev_close = df["close"].iloc[-2]
        if (close_p > open_p and prev_close < prev_open and close_p > prev_open and open_p < prev_close):
            patterns.append("Bullish Engulfing")

    return patterns


def support_resistance(df):
    """
    Очень простой SR: 10 последних свечей.
    """
    sup = df["low"].rolling(10, min_periods=1).min().iloc[-1]
    res = df["high"].rolling(10, min_periods=1).max().iloc[-1]
    return sup, res


def analyze_symbol(df, symbol_name):
    # Индикаторы
    sma = SMAIndicator(df["close"], 20).sma_indicator().iloc[-1]
    ema = EMAIndicator(df["close"], 20).ema_indicator().iloc[-1]
    rsi = RSIIndicator(df["close"], 14).rsi().iloc[-1]
    # stoch = StochasticOscillator(df["high"], df["low"], df["close"]).stoch().iloc[-1]
    # atr = AverageTrueRange(df["high"], df["low"], df["close"]).average_true_range().iloc[-1]
    # bb_high = BollingerBands(df["close"]).bollinger_hband().iloc[-1]
    # bb_low = BollingerBands(df["close"]).bollinger_lband().iloc[-1]
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
# Автоматические сигналы
# ==============================
async def auto_signal(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.context["chat_id"]

    symbols = get_top_symbols()
    if not symbols:
        await context.bot.send_message(chat_id, "Ошибка: не удалось получить список символов.")
        return

    signals = []
    for sym in symbols:
        df = get_ohlcv(sym)
        if df is not None:
            try:
                s = analyze_symbol(df, sym)
                signals.append(s)
            except Exception:
                continue

    if signals:
        # Ограничим длинное сообщение: Telegram лимит на длину, лучше по факту отправлять в чанках,
        # но здесь простая отправка (можно расширить при необходимости).
        await context.bot.send_message(chat_id, "\n".join(signals))


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


async def start_auto(update, context):
    chat_id = update.message.chat_id
    await update.message.reply_text("Автосигналы включены! Интервал: 30 минут.")

    # Запускаем задачу с именем = chat_id (чтобы потом можно было удалить)
    context.job_queue.run_repeating(
        auto_signal,
        interval=INTERVAL_SECONDS,
        first=2,
        context={"chat_id": chat_id},
        name=str(chat_id),
    )


async def stop_auto(update, context):
    chat_id = update.message.chat_id
    await update.message.reply_text("Автосигналы отключены!")

    # Удаляем задания с именем chat_id
    jobs = context.job_queue.get_jobs_by_name(str(chat_id))
    for job in jobs:
        job.schedule_removal()


async def now_signal(update, context):
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


async def debug(update, context):
    # Выводим статус: токен, jobqueue count, доступность Bybit и сколько символов вернулось
    lines = []
    lines.append(f"TOKEN set: {'yes' if TOKEN else 'no'}")
    try:
        jq = context.job_queue
        jobs = jq.jobs() if jq else []
        lines.append(f"JobQueue jobs: {len(jobs)}")
    except Exception as e:
        lines.append(f"JobQueue: error {e}")

    # Проверим Bybit
    try:
        resp = requests.get("https://api.bybit.com/v5/market/instruments-info?category=linear", timeout=8).json()
        rows = resp.get("result", {}).get("list", [])
        lines.append(f"Bybit API ok, symbols found: {len(rows)}")
    except Exception as e:
        lines.append(f"Bybit API FAIL ({e})")

    await update.message.reply_text("\n".join(lines))


# ==============================
# Запуск
# ==============================
if __name_ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("auto", start_auto))
    app.add_handler(CommandHandler("stopauto", stop_auto))
    app.add_handler(CommandHandler("nowsignal", now_signal))
    app.add_handler(CommandHandler("debug", debug))

    app.run_polling()
