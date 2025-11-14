# bot.py — улучшённая версия с /debug, устойчивыми запросами и безопасной обработкой
import os
import time
import requests
import pandas as pd
import numpy as np
import asyncio
import logging
from typing import List

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    JobQueue,
)

from ta.trend import SMAIndicator, EMAIndicator
from ta.momentum import RSIIndicator, StochasticOscillator
from ta.volatility import BollingerBands, AverageTrueRange

# ---------------- CONFIG ----------------
TOKEN = os.getenv("TELEGRAM_TOKEN")
INTERVAL_SECONDS = 30 * 60  # 30 минут
TOP_SYMBOL_LIMIT = 150      # можно снизить до 50/100, если нужно быстрее
BYBIT_TIMEOUT = 8           # таймаут для HTTP-запросов
BATCH_MESSAGE_SIZE = 10     # сколько строк сигналов отправлять в одном сообщении

if not TOKEN:
    print("Ошибка: TELEGRAM_TOKEN не найден!")
    raise SystemExit("TELEGRAM_TOKEN not set")

# Настройка логов — видно в Render logs
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bot")

# ---------------- Helpers (non-blocking wrappers) ----------------
async def safe_requests_get(url: str, params: dict | None = None, timeout: int = BYBIT_TIMEOUT):
    """Выполняет requests.get в пуле потоков, возвращает .json() или None"""
    def _req():
        try:
            r = requests.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.debug("HTTP error for %s - %s", url, e)
            return None
    return await asyncio.to_thread(_req)

async def safe_get_top_symbols(limit: int = TOP_SYMBOL_LIMIT) -> List[str]:
    url = "https://api.bybit.com/v2/public/tickers"
    data = await safe_requests_get(url)
    if not data or "result" not in data:
        logger.warning("Bybit /tickers returned no data")
        return []
    symbols = [s["symbol"] for s in data["result"] if s.get("quote_currency") == "USDT"]
    return symbols[:limit]

async def safe_get_ohlcv(symbol: str, interval: str = "1h", limit: int = 100):
    url = "https://api.bybit.com/v2/public/kline/list"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    data = await safe_requests_get(url, params=params)
    if not data or "result" not in data:
        return None
    arr = data.get("result", [])
    if not isinstance(arr, list) or len(arr) == 0:
        return None
    # преобразуем в DataFrame
    try:
        df = pd.DataFrame(arr)
        # ensure required columns exist
        for col in ("open", "high", "low", "close", "volume"):
            if col not in df.columns:
                logger.debug("Missing column %s in ohlcv for %s", col, symbol)
                return None
        df["close"] = df["close"].astype(float)
        df["open"] = df["open"].astype(float)
        df["high"] = df["high"].astype(float)
        df["low"] = df["low"].astype(float)
        df["volume"] = df["volume"].astype(float)
        return df
    except Exception as e:
        logger.exception("Failed to build DataFrame for %s: %s", symbol, e)
        return None

# ---------------- Indicators & Patterns ----------------
def detect_candlestick(df: pd.DataFrame) -> List[str]:
    patterns = []
    try:
        o = df["open"].iloc[-1]
        c = df["close"].iloc[-1]
        h = df["high"].iloc[-1]
        l = df["low"].iloc[-1]

        if abs(o - c) / (h - l + 1e-9) < 0.1:
            patterns.append("Doji")
        if (h - max(o, c)) > 2 * (max(o, c) - min(o, c)):
            patterns.append("Hammer")
        if len(df) > 1:
            po = df["open"].iloc[-2]
            pc = df["close"].iloc[-2]
            if (c > o and pc < po and c > po and o < pc):
                patterns.append("Engulfing")
    except Exception:
        logger.debug("detect_candlestick failed", exc_info=True)
    return patterns

def support_resistance(df: pd.DataFrame):
    highs = df["high"]
    lows = df["low"]
    support = lows.rolling(window=10, min_periods=1).min().iloc[-1]
    resistance = highs.rolling(window=10, min_periods=1).max().iloc[-1]
    return support, resistance

def analyze_symbol_sync(df: pd.DataFrame, symbol_name: str) -> str:
    """Синхронный анализ одной таблицы (вызывается внутри to_thread)"""
    try:
        sma = SMAIndicator(df["close"], window=20).sma_indicator().iloc[-1]
        ema = EMAIndicator(df["close"], window=20).ema_indicator().iloc[-1]
        rsi = RSIIndicator(df["close"], window=14).rsi().iloc[-1]
        # stoch можно не использовать в решении, сохраним для причины
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
            risk = (price - support) / price * 100 if price and support else 0
        elif side == "SHORT":
            risk = (resistance - price) / price * 100 if price and resistance else 0
        else:
            risk = 0

        reason_str = "; ".join(reason) if reason else "Нет явной причины"
        return f"{symbol_name} {side} {price:.6f} | {reason_str} | Risk:{risk:.2f}%"
    except Exception:
        logger.exception("analyze_symbol_sync failed for %s", symbol_name)
        return f"{symbol_name} ERROR"

# ---------------- Core async tasks ----------------
async def analyze_and_collect(symbols: List[str]) -> List[str]:
    results = []
    # параллелим в ограниченном пуле, чтобы не утонуть в запросах
    sem = asyncio.Semaphore(6)  # параллельно 6 запросов к Bybit
    async def worker(sym):
        async with sem:
            df = await safe_get_ohlcv(sym)
            if df is None:
                return None
            # heavy CPU ops (ta) — запускаем в threadpool
            return await asyncio.to_thread(analyze_symbol_sync, df, sym)

    async def safe_get_ohlcv(s):
        try:
            return await safe_get_ohlcv_inner(s)
        except Exception:
            return None

    # define inner to avoid name conflict
    async def safe_get_ohlcv_inner(s):
        return await safe_get_ohlcv_func(s)

    # actual wrapper using earlier defined function
    async def safe_get_ohlcv_func(s):
        return await safe_get_ohlcv(s)  # this calls the top-level safe_get_ohlcv

    # But simpler: call safe_get_ohlcv top-level directly
    tasks = [worker(sym) for sym in symbols]
    done = await asyncio.gather(*tasks, return_exceptions=True)
    for item in done:
        if isinstance(item, Exception):
            logger.debug("worker exception: %s", item)
            continue
        if item:
            results.append(item)
    return results

# Simpler alternative: call sequentially but in to_thread (safer)
async def analyze_and_collect_sequential(symbols: List[str]) -> List[str]:
    results = []
    for sym in symbols:
        df = await safe_get_ohlcv(sym)
        if df is None:
            continue
        res = await asyncio.to_thread(analyze_symbol_sync, df, sym)
        results.append(res)
    return results

# ---------------- Job — отправка сигналов ----------------
async def auto_signal_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.context.get("chat_id")
    if not chat_id:
        return
    symbols = await safe_get_top_symbols()
    if not symbols:
        await context.bot.send_message(chat_id=chat_id, text="Ошибка: не удалось получить список символов с Bybit.")
        return

    # для стабильности используем последовательный анализ (можно включить параллельный)
    signals = await analyze_and_collect_sequential(symbols)
    if not signals:
        await context.bot.send_message(chat_id=chat_id, text="Нет сигналов (или ошибка при сборе данных).")
        return

    # отправляем пачками, чтобы не превысить лимиты Telegram
    for i in range(0, len(signals), BATCH_MESSAGE_SIZE):
        chunk = signals[i:i + BATCH_MESSAGE_SIZE]
        try:
            await context.bot.send_message(chat_id=chat_id, text="\n".join(chunk))
            await asyncio.sleep(0.5)  # небольшой интервал между сообщениями
        except Exception as e:
            logger.exception("Failed to send chunk to %s: %s", chat_id, e)
            break

# ---------------- Command handlers ----------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Бот запущен ✅\n"
        "Команды:\n"
        "/auto — включить автосигналы (каждые 30 мин)\n"
        "/stopauto — отключить автосигналы\n"
        "/nowsignal — получить сигнал прямо сейчас\n"
        "/debug — проверить состояние бота"
    )

async def auto_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text("Автосигналы включены (первый будет в течение минуты).")
    # имя задачи — chat_id, это позволяет потом убрать только свои задания
    context.job_queue.run_repeating(auto_signal_job, interval=INTERVAL_SECONDS, first=3, context={"chat_id": chat_id}, name=str(chat_id))

async def stopauto_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    jobs = context.job_queue.get_jobs_by_name(str(chat_id))
    removed = 0
    for job in jobs:
        job.schedule_removal()
        removed += 1
    await update.message.reply_text(f"Автосигналы отключены. Удалено заданий: {removed}")

async def nowsignal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text("Собираю сигналы прямо сейчас...")
    symbols = await safe_get_top_symbols()
    if not symbols:
        await update.message.reply_text("Ошибка: не удалось получить список символов.")
        return
    signals = await analyze_and_collect_sequential(symbols[:TOP_SYMBOL_LIMIT])
    if not signals:
        await update.message.reply_text("Нет сигналов или ошибка при обработке.")
        return
    # отправляем первые N сигналов (ограничение размера сообщения)
    for i in range(0, len(signals), BATCH_MESSAGE_SIZE):
        chunk = signals[i:i + BATCH_MESSAGE_SIZE]
        await update.message.reply_text("\n".join(chunk))

async def debug_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    # проверим токен, job_queue, bybit
    info = []
    info.append(f"TOKEN set: {'yes' if TOKEN else 'no'}")
    # jobqueue
    try:
        jq = context.job_queue
        jobs = jq.jobs() if jq else []
        info.append(f"JobQueue jobs: {len(jobs)}")
    except Exception as e:
        info.append(f"JobQueue: error {e}")
    # bybit ping
    byb = await safe_requests_get("https://api.bybit.com/v2/public/tickers")
    info.append("Bybit API ok" if byb else "Bybit API FAIL")
    # send back
    await update.message.reply_text("\n".join(info))

# ---------------- Main ----------------
def main():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("auto", auto_cmd))
    app.add_handler(CommandHandler("stopauto", stopauto_cmd))
    app.add_handler(CommandHandler("nowsignal", nowsignal_cmd))
    app.add_handler(CommandHandler("debug", debug_cmd))

    logger.info("Starting bot")
    app.run_polling()

if __name__ == "__main__":
    main()
