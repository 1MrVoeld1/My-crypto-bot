# bot.py — версия: scrape-symbols + ccxt OHLCV
import os
import re
import time
import asyncio
import requests
import traceback
import pandas as pd
import ccxt
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from ta.trend import SMAIndicator, EMAIndicator
from ta.momentum import RSIIndicator

# ============ Настройки ============
TOKEN = os.getenv("TELEGRAM_TOKEN")
YOUR_CHAT_ID = 7239571933  # замените на свой ID если нужно

TOP_SYMBOL_LIMIT = 50
TIMEFRAME = "1h"   # используем 1h
PERIODS = 50

auto_tasks = {}

# init ccxt exchange (используем для OHLCV)
exchange = ccxt.bybit({'enableRateLimit': True})

# Bybit markets page base
BYBIT_MARKETS_BASE = "https://www.bybit.com/en/markets/overview/?page=contract"

# ============ ВСПОМОГАТЕЛИ ДЛЯ СКРЕЙПИНГА ============
def _scrape_bybit_page(page_url):
    """Вернёт текст HTML страницы или None."""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; Bot/1.0)"
        }
        r = requests.get(page_url, headers=headers, timeout=12)
        r.raise_for_status()
        return r.text
    except Exception as e:
        # возврат None при ошибке
        return None

def _extract_symbols_from_html(html):
    """
    Ищет все вхождения вида ABCUSDT в HTML через regex.
    Возвращает уникальный список в порядке появления.
    """
    if not html:
        return []
    # Регулярка поймает строки типа BTCUSDT, AVAXUSDT и т.п.
    pattern = re.compile(r'\b[A-Z0-9]{2,12}USDT\b')
    found = pattern.findall(html.upper())
    # упорядочивание по первому появлению
    seen = {}
    for s in found:
        if s not in seen:
            seen[s] = True
    return list(seen.keys())

def get_top_symbols(limit=TOP_SYMBOL_LIMIT, max_pages=12):
    """
    Скрейпим Bybit overview (Derivatives) постранично.
    Возвращаем (symbols, code)
      code: 0 OK, 3 биржа/страницы недоступны
    """
    symbols = []
    try:
        for page in range(1, max_pages + 1):
            url = BYBIT_MARKETS_BASE + (f"&page={page}" if "page=" not in BYBIT_MARKETS_BASE else f"&p={page}")
            # Некоторые вариации URL — просто формируем безопасно:
            # если BYBIT_MARKETS_BASE уже содержит ?page=contract -> используем &page=n
            # safe approach: try both patterns:
            url_candidates = [
                f"https://www.bybit.com/en/markets/overview/?page=contract&page={page}",
                f"https://www.bybit.com/en/markets/overview/?page=contract&p={page}",
                f"https://www.bybit.com/en/markets/overview/?page=contract&tab=contract&page={page}",
            ]
            html = None
            for u in url_candidates:
                html = _scrape_bybit_page(u)
                if html:
                    break
            if not html:
                # попробуем основной URL with ?page=contract only for page==1
                if page == 1:
                    html = _scrape_bybit_page(BYBIT_MARKETS_BASE)
                if not html:
                    # если страница недоступна — продолжаем попытки кратко, но если несколько подряд fail — остановим
                    continue

            found = _extract_symbols_from_html(html)
            for s in found:
                if s not in symbols:
                    symbols.append(s)
                if len(symbols) >= limit:
                    break
            if len(symbols) >= limit:
                break
            # небольшой sleep, чтобы не спамить сайт
            time.sleep(0.6)
        if symbols:
            return symbols[:limit], 0
        else:
            return [], 3
    except Exception as e:
        return [], 3

# ============ OHLCV через ccxt ============
def fetch_ohlcv(symbol: str, timeframe=TIMEFRAME, limit=PERIODS):
    """
    Попытка получить OHLCV через ccxt.
    Возвращает (df, code) где code: 0 OK, 2 ошибка OHLCV
    """
    try:
        # ccxt expects symbol like "BTC/USDT" sometimes for spot; for bybit futures ccxt uses "BTC/USDT:USDT" or like "BTC/USDT:USDT"
        # Попробуем несколько форматов
        candidates = [symbol.replace("USDT", "/USDT"), symbol.replace("USDT", "/USDT:USDT"), symbol]
        last_exc = None
        for cand in candidates:
            try:
                ohlcv = exchange.fetch_ohlcv(cand, timeframe=timeframe, limit=limit)
                if ohlcv:
                    df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
                    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
                    return df, 0
            except Exception as e:
                last_exc = e
                continue
        # если не получилось ни с одним форматом — вернуть ошибку
        # логируем для отладки
        print(f"[fetch_ohlcv] failed for {symbol}, last exc: {repr(last_exc)}")
        return None, 2
    except Exception as e:
        print(f"[fetch_ohlcv] unexpected error for {symbol}: {e}")
        return None, 2

# ============ АНАЛИЗ ============
def detect_candlestick(df):
    patterns = []
    try:
        o, c, h, l = df["open"].iloc[-1], df["close"].iloc[-1], df["high"].iloc[-1], df["low"].iloc[-1]
        if abs(o - c) / (h - l + 1e-9) < 0.1:
            patterns.append("Doji")
        if (h - max(o, c)) > 2 * (max(o, c) - min(o, c)):
            patterns.append("Hammer")
        if len(df) > 1:
            po, pc = df["open"].iloc[-2], df["close"].iloc[-2]
            if c > o and pc < po and c > po and o < pc:
                patterns.append("Bullish Engulfing")
    except Exception:
        pass
    return patterns

def detect_double_top_bottom(df):
    if len(df) < 5:
        return None
    closes = df["close"].iloc[-5:].values
    try:
        if closes[0] < closes[1] > closes[2] < closes[3] < closes[4]:
            return "Double Top"
        if closes[0] > closes[1] < closes[2] > closes[3] > closes[4]:
            return "Double Bottom"
    except Exception:
        return None
    return None

def support_resistance(df):
    sup = df["low"].rolling(10, min_periods=1).min().iloc[-1]
    res = df["high"].rolling(10, min_periods=1).max().iloc[-1]
    return sup, res

def analyze_symbol(df, symbol_name):
    try:
        price = float(df["close"].iloc[-1])
    except Exception:
        price = 0.0
    patterns = detect_candlestick(df)
    figure = detect_double_top_bottom(df)
    support, resistance = support_resistance(df)
    try:
        sma = SMAIndicator(df["close"], 20).sma_indicator().iloc[-1]
    except Exception:
        sma = None
    try:
        ema = EMAIndicator(df["close"], 20).ema_indicator().iloc[-1]
    except Exception:
        ema = None
    try:
        rsi = RSIIndicator(df["close"], 14).rsi().iloc[-1]
    except Exception:
        rsi = None

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
    if figure:
        reason.append("Fig:" + figure)

    if side == "LONG":
        risk = (price - support) / price * 100 if price and support else 0
    elif side == "SHORT":
        risk = (resistance - price) / price * 100 if price and resistance else 0
    else:
        risk = 0

    close_in = "2h" if side != "HOLD" else "-"
    reason_str = "; ".join(reason) if reason else "No strong reason"
    return f"{symbol_name[:12]} | {price:.6f}$ | Close: {close_in} | {reason_str} | Risk: {risk:.2f}%"

# ============ TELEGRAM HELPERS ============
async def _safe_reply(update: Update, text: str):
    if update is None:
        return
    if getattr(update, "message", None):
        await update.message.reply_text(text)
    elif getattr(update, "callback_query", None):
        await update.callback_query.answer(text)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "/nowsignal – сейчас\n"
        "/auto15 – каждые 15 мин\n"
        "/auto30 – каждые 30 мин\n"
        "/auto60 – каждый час\n"
        "/stopauto – остановить\n"
        "/debug – статус"
    )
    await _safe_reply(update, txt)

async def nowsignal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _safe_reply(update, "Собираю список символов (scrape)...")
    symbols, code = get_top_symbols()
    if code != 0 or not symbols:
        await _safe_reply(update, f"Ошибка! Биржа/страницы недоступны, код: {code}")
        return

    await _safe_reply(update, f"Найдено символов: {len(symbols)}. Беру первые {min(len(symbols), 40)} для анализа...")
    msgs = []
    for sym in symbols[:TOP_SYMBOL_LIMIT]:
        df, df_code = await asyncio.to_thread(fetch_ohlcv, sym)
        if df is None:
            # присылаем короткий error line
            await _safe_reply(update, f"[{sym}] OHLCV FAIL, code={df_code}")
            continue
        try:
            msgs.append(analyze_symbol(df, sym))
        except Exception as e:
            await _safe_reply(update, f"[{sym}] ANALYSIS ERROR: {e}")
            continue

    if msgs:
        # отправляем чанками, 25 строк за раз
        chunk = 25
        for i in range(0, len(msgs), chunk):
            await _safe_reply(update, "\n".join(msgs[i:i+chunk]))
    else:
        await _safe_reply(update, "Ошибка: данные не получены (все символы вернули ошибку OHLCV).")

async def debug_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    token_ok = bool(TOKEN)
    # проверка доступа к сайту Bybit overview (scrape)
    test_html = _scrape_bybit_page(BYBIT_MARKETS_BASE)
    site_ok = True if test_html else False
    # проверка ccxt load_markets
    try:
        exchange.load_markets()
        exchange_ok = True
    except Exception:
        exchange_ok = False
    await _safe_reply(update, f"TOKEN: {token_ok}\nScrape Bybit page: {site_ok}\nccxt load_markets: {exchange_ok}\nAuto signals: {list(auto_tasks.keys()) or 'Off'}")

# ============ AUTOS ============
async def auto_loop(app, chat_id, interval):
    while True:
        symbols, code = get_top_symbols()
        if code != 0:
            try:
                await app.bot.send_message(chat_id, f"Ошибка! Биржа/страницы недоступны, код: {code}")
            except Exception:
                pass
            await asyncio.sleep(interval * 60)
            continue

        msgs = []
        for sym in symbols[:TOP_SYMBOL_LIMIT]:
            df, df_code = await asyncio.to_thread(fetch_ohlcv, sym)
            if df is None:
                try:
                    await app.bot.send_message(chat_id, f"[{sym}] OHLCV FAIL, code={df_code}")
                except Exception:
                    pass
                continue
            try:
                msgs.append(analyze_symbol(df, sym))
            except Exception:
                continue

        if msgs:
            # отправляем чанками
            chunk_size = 25
            for i in range(0, len(msgs), chunk_size):
                try:
                    await app.bot.send_message(chat_id, "\n".join(msgs[i:i+chunk_size]))
                except Exception:
                    pass
        await asyncio.sleep(interval * 60)

async def start_auto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "30" not in auto_tasks:
        task = asyncio.create_task(auto_loop(context.application, update.message.chat_id, 30))
        auto_tasks["30"] = task
        await _safe_reply(update, "Автосигналы каждые 30 минут запущены!")

async def start_auto15(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "15" not in auto_tasks:
        task = asyncio.create_task(auto_loop(context.application, update.message.chat_id, 15))
        auto_tasks["15"] = task
        await _safe_reply(update, "Автосигналы каждые 15 минут запущены!")

async def start_auto60(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "60" not in auto_tasks:
        task = asyncio.create_task(auto_loop(context.application, update.message.chat_id, 60))
        auto_tasks["60"] = task
        await _safe_reply(update, "Автосигналы каждый час запущены!")

async def stop_auto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for key, task in list(auto_tasks.items()):
        try:
            task.cancel()
        except Exception:
            pass
    auto_tasks.clear()
    await _safe_reply(update, "Автосигналы остановлены!")

# ============ MAIN ============
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
