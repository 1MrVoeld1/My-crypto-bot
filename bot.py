import os
import pandas as pd
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from ta.trend import SMAIndicator, EMAIndicator
from ta.momentum import RSIIndicator
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

TOKEN = os.getenv("TELEGRAM_TOKEN")
INTERVAL_SECONDS = 30 * 60  # 30 минут
TOP_SYMBOL_LIMIT = 250

if TOKEN is None:
    print("Ошибка: TELEGRAM_TOKEN не найден!")
    exit(1)

# ==============================
# Playwright + Bybit
# ==============================
def get_bybit_prices():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto("https://www.bybit.com/derivatives/usdt")
        page.wait_for_timeout(5000)  # ждём подгрузку данных JS

        html = page.content()
        browser.close()

    soup = BeautifulSoup(html, "html.parser")
    data = []

    # Примерные селекторы, уточни в DevTools
    rows = soup.select("div.tableRow")
    for row in rows:
        try:
            symbol = row.select_one("div.symbol").text.strip()
            price = float(row.select_one("div.lastPrice").text.replace(",", ""))
            data.append({"symbol": symbol, "price": price})
        except:
            continue

    df = pd.DataFrame(data)
    return df

def get_top_symbols(limit=TOP_SYMBOL_LIMIT):
    df = get_bybit_prices()
    if df.empty:
        return []
    return df["symbol"].tolist()[:limit]

def get_ohlcv(symbol, interval="60", limit=200):
    df = get_bybit_prices()
    df_sym = df[df["symbol"] == symbol]
    if df_sym.empty:
        return None
    df_sym["open"] = df_sym["price"]
    df_sym["high"] = df_sym["price"]
    df_sym["low"] = df_sym["price"]
    df_sym["close"] = df_sym["price"]
    return df_sym

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
        if close_p > open_p and prev_close < prev_open and close_p > prev_open and open_p < prev_close:
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
        "/nowsignal – сигнал прямо сейчас\n"
        "/debug – статус бота"
    )

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
            except:
                continue

    if signals:
        await update.message.reply_text("\n".join(signals))
    else:
        await update.message.reply_text("Не удалось получить данные.")

async def debug(update, context):
    lines = []
    lines.append(f"TOKEN set: {'yes' if TOKEN else 'no'}")
    try:
        df = get_bybit_prices()
        lines.append(f"Bybit site ok, symbols found: {len(df)}")
    except Exception as e:
        lines.append(f"Bybit FAIL ({e})")
    await update.message.reply_text("\n".join(lines))

# ==============================
# Запуск
# ==============================
if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("nowsignal", now_signal))
    app.add_handler(CommandHandler("debug", debug))
    app.run_polling()
