import os  # чтобы брать токен из переменных окружения
from telegram.ext import Updater, CommandHandler

# Получаем токен из переменной окружения
TOKEN = os.getenv("TELEGRAM_TOKEN")

# Функция, которая отвечает на команду /start
def start(update, context):
    update.message.reply_text("Бот запущен и работает!")

# Основная часть: создаём бота и запускаем его
if _name_ == "_main_":
    # Создаём updater для работы с ботом
    updater = Updater(TOKEN)
    
    # Привязываем команду /start к функции start
    updater.dispatcher.add_handler(CommandHandler("start", start))
    
    # Запускаем бота
    updater.start_polling()
    updater.idle()
