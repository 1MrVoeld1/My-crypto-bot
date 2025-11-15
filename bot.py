import os
import pandas as pd
import ccxt
import asyncio
import matplotlib.pyplot as plt
from io import BytesIO
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from ta.trend import SMAIndicator, EMAIndicator
from ta.momentum import RSIIndicator

TOKEN = os.getenv("TELEGRAM_TOKEN")
YOUR_CHAT_ID = 123456789  # ← замени на свой ID

TOP_SYMBOL_LIMIT = 20
TIMEFRAME = "1h"
PERIODS = 50
auto_tasks = {}

exchange = ccxt.bybit({'enableRateLimit': True})

# -------------------- ДАННЫЕ --------------------
def get_top_symbols(limit=TOP_SYMBOL_LIMIT):
    try:
        markets = exchange.load_markets()
        symbols = [s for s in markets if "/USDT" in s and markets[s]['type'] == 'future']
        return symbols[:limit]
    except Exception as e:
        print("Ошибка получения символов:", e)
        return []

def fetch_ohlcv(symbol):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=PERIODS)
        df = pd.DataFrame(ohlcv, columns=["timestamp","open","high","low","close","volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        return df
    except Exception as e:
        print(f"Ошибка OHLCV {symbol}: {e}")
        return None

# -------------------- АНАЛИЗ --------------------
def analyze_symbol(df, symbol_name):
    price = df["close"].iloc[-1]
    sma = SMAIndicator(df["close"],20).sma_indicator().iloc[-1]
    ema = EMAIndicator(df["close"],20).ema_indicator().iloc[-1]
    rsi = RSIIndicator(df["close"],14).rsi().iloc[-1]

    side = "HOLD"
    reason = []
    if rsi < 30:
        side="LONG"; reason.append("RSI<30")
    elif rsi>70:
        side="SHORT"; reason.append("RSI>70")
    if ema>sma:
        side="LONG"; reason.append("EMA>SMA")
    elif ema<sma:
        side="SHORT"; reason.append("EMA<SMA")
    return f"{symbol_name[:10]} | {price:.2f}$ | {side} | {'; '.join(reason)}"

# -------------------- ГРАФИК --------------------
def plot_candles(df, symbol):
    plt.figure(figsize=(6,3))
    plt.plot(df["close"], label="Close")
    plt.title(symbol)
    plt.tight_layout()
    buf = BytesIO()
    plt.savefig(buf, format='png')
    buf.seek(0)
    plt.close()
    return buf

# -------------------- TELEGRAM --------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text(
            "/nowsignal – сейчас\n"
            "/auto15 – каждые 15 мин\n"
            "/auto30 – каждые 30 мин\n"
            "/auto60 – каждый час\n"
            "/stopauto – остановить\n"
            "/debug – статус"
        )

async def nowsignal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    await update.message.reply_text("Собираю данные...")
    symbols = get_top_symbols()
    for sym in symbols[:10]:
        df = await asyncio.to_thread(fetch_ohlcv, sym)
        if df is not None:
            msg = analyze_symbol(df, sym)
            await update.message.reply_text(msg)
            buf = await asyncio.to_thread(plot_candles, df, sym)
            await update.message.reply_photo(buf)

async def debug_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    token_ok = TOKEN is not None
    try:
        exchange.load_markets()
        exchange_ok = True
    except:
        exchange_ok = False
    await update.message.reply_text(
        f"TOKEN: {token_ok}\n"
        f"Биржа: {exchange_ok}\n"
        f"Auto: {list(auto_tasks.keys()) or 'Off'}"
    )

# -------------------- ФОН АВТО --------------------
async def auto_loop(app, chat_id, interval):
    while True:
        symbols = get_top_symbols()
        for sym in symbols[:10]:
            df = await asyncio.to_thread(fetch_ohlcv, sym)
            if df is not None:
                msg = analyze_symbol(df, sym)
                await app.bot.send_message(chat_id, msg)
                buf = await asyncio.to_thread(plot_candles, df, sym)
                await app.bot.send_photo(chat_id, buf)
        await asyncio.sleep(interval*60)

async def start_auto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    if "30" not in auto_tasks:
        task = asyncio.create_task(auto_loop(context.application, update.message.chat_id, 30))
        auto_tasks["30"] = task
        await update.message.reply_text("Автосигналы 30 мин запущены!")

async def start_auto15(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    if "15" not in auto_tasks:
        task = asyncio.create_task(auto_loop(context.application, update.message.chat_id, 15))
        auto_tasks["15"] = task
        await update.message.reply_text("Автосигналы 15 мин запущены!")

async def start_auto60(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    if "60" not in auto_tasks:
        task = asyncio.create_task(auto_loop(context.application, update.message.chat_id, 60))
        auto_tasks["60"] = task
        await update.message.reply_text("Автосигналы 60 мин запущены!")

async def stop_auto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    for t in auto_tasks.values():
        t.cancel()
    auto_tasks.clear()
    await update.message.reply_text("Автосигналы остановлены!")

# -------------------- MAIN --------------------
def main():
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
