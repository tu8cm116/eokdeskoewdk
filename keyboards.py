from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

# Главное меню
main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton("🔍 Найти собеседника")],
        [KeyboardButton("ℹ️ Помощь")]
    ],
    resize_keyboard=True
)

# Клавиатура во время чата
chat_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton("⏹ Стоп"), KeyboardButton("➡️ Следующий")],
        [KeyboardButton("⚠️ Пожаловаться")]
    ],
    resize_keyboard=True
)
