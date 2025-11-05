import os
import asyncio
import logging
import random
import string
from collections import deque
import asyncpg
from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    InlineKeyboardMarkup, InlineKeyboardButton
)

# ---------- Логирование ----------
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
log = logging.getLogger(__name__)

# ---------- Конфигурация ----------
TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
MODERATOR_ID = int(os.getenv("MODERATOR_ID", "0"))

if not TOKEN:
    raise RuntimeError("BOT_TOKEN не задан!")

bot = Bot(token=TOKEN, parse_mode="HTML")
dp = Dispatcher(bot)
db_pool = None

# ---------- Память ----------
memory_queue = deque()
memory_pairs = {}
memory_status = {}
memory_banned = set()
memory_reports = []
user_codes = {}  # uid -> code

# ---------- КРАСИВЫЕ КЛАВИАТУРЫ ----------
main_menu = ReplyKeyboardMarkup(resize_keyboard=True)
main_menu.add(KeyboardButton("Найти"), KeyboardButton("Помощь"))
main_menu.add(KeyboardButton("Мой ID"))  # НОВАЯ КНОПКА

chat_menu = ReplyKeyboardMarkup(resize_keyboard=True)
chat_menu.add(KeyboardButton("Стоп"), KeyboardButton("Следующий"))
chat_menu.add(KeyboardButton("Пожаловаться"))

waiting_menu = ReplyKeyboardMarkup(resize_keyboard=True)
waiting_menu.add(KeyboardButton("Отмена"))

mod_menu = ReplyKeyboardMarkup(resize_keyboard=True)
mod_menu.add(KeyboardButton("Жалобы"), KeyboardButton("Статистика"))
mod_menu.add(KeyboardButton("Баны"), KeyboardButton("Выход"))

# ---------- УНИКАЛЬНЫЙ КОД ----------
def generate_code():
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

async def get_or_create_code(uid):
    if uid in user_codes:
        return user_codes[uid]
    code = generate_code()
    while any(code == c for c in user_codes.values()):
        code = generate_code()
    user_codes[uid] = code
    return code

# ---------- БД ----------
async def init_db():
    global db_pool
    if not DATABASE_URL:
        log.warning("DATABASE_URL не найден — работаем в памяти")
        return False
    try:
        db_pool = await asyncpg.create_pool(dsn=DATABASE_URL, min_size=1, max_size=5)
        async with db_pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    status VARCHAR(20) DEFAULT 'idle',
                    code TEXT UNIQUE
                );
                CREATE TABLE IF NOT EXISTS queue (user_id BIGINT PRIMARY KEY, joined_at TIMESTAMP DEFAULT NOW());
                CREATE TABLE IF NOT EXISTS pairs (user_id BIGINT PRIMARY KEY, partner_id BIGINT);
                CREATE INDEX IF NOT EXISTS idx_queue ON queue (joined_at);
            """)
            try:
                await conn.execute("ALTER TABLE users ADD COLUMN banned BOOLEAN DEFAULT FALSE")
                log.info("Добавлен столбец 'banned'")
            except asyncpg.DuplicateColumnError:
                pass
        log.info("PostgreSQL подключён")
        return True
    except Exception as e:
        log.error(f"БД ошибка: {e}")
        return False

# ---------- Вспомогательные ----------
async def ensure_user(uid):
    if db_pool:
        async with db_pool.acquire() as conn:
            code = await get_or_create_code(uid)
            await conn.execute(
                "INSERT INTO users(user_id, code) VALUES($1, $2) ON CONFLICT(user_id) DO UPDATE SET code = $2",
                uid, code
            )
    else:
        memory_status[uid] = memory_status.get(uid, 'idle')
        await get_or_create_code(uid)

# ... (add_to_queue, remove_from_queue, find_partner, create_pair, get_partner, break_pair, is_banned — без изменений)

# ---------- ХЭНДЛЕРЫ ----------
waiting_tasks = {}
user_reporting = {}

@dp.message_handler(commands=['mod'])
async def mod_entry(msg: types.Message):
    if msg.from_user.id != MODERATOR_ID:
        return await msg.answer("Доступ запрещён.")
    await msg.answer("Модераторская панель:", reply_markup=mod_menu)

@dp.message_handler(commands=['start'])
async def start(msg: types.Message):
    uid = msg.from_user.id
    await ensure_user(uid)
    await break_pair(uid)
    await remove_from_queue(uid)
    await msg.answer("Привет! Анонимный чат.", reply_markup=main_menu)

@dp.message_handler(lambda m: m.text == "Помощь")
async def help_cmd(msg: types.Message):
    await msg.answer(
        "• Найти — начать поиск\n"
        "• Стоп — выйти\n"
        "• Следующий — найти нового\n"
        "• Пожаловаться — если плохо\n"
        "• Отмена — остановить поиск\n"
        "• Мой ID — твой код",
        reply_markup=main_menu
    )

# --- КНОПКИ ---
@dp.message_handler(lambda m: m.text == "Мой ID")
async def my_id_button(msg: types.Message):
    uid = msg.from_user.id
    code = await get_or_create_code(uid)
    await msg.answer(f"Твой ID: <code>{uid}</code>\nТвой код: <code>{code}</code>", parse_mode="HTML")

@dp.message_handler(lambda m: m.text == "Найти")
async def search_button(msg: types.Message):
    if await get_partner(msg.from_user.id):
        return
    await search(msg)

@dp.message_handler(lambda m: m.text == "Отмена")
async def cancel_button(msg: types.Message):
    if await get_partner(msg.from_user.id):
        return
    await cancel(msg)

@dp.message_handler(lambda m: m.text == "Стоп")
async def stop_button(msg: types.Message):
    if not await get_partner(msg.from_user.id):
        return  # Текст в чате — не команда
    await stop_cmd(msg)

@dp.message_handler(lambda m: m.text == "Следующий")
async def next_button(msg: types.Message):
    if not await get_partner(msg.from_user.id):
        return
    await next_cmd(msg)

@dp.message_handler(lambda m: m.text == "Пожаловаться")
async def report_button(msg: types.Message):
    if not await get_partner(msg.from_user.id):
        return
    await report(msg)

# --- КОМАНДЫ ---
@dp.message_handler(commands=['search'])
async def search(msg: types.Message):
    uid = msg.from_user.id
    if await is_banned(uid):
        return await msg.answer("Вы забанены.", reply_markup=main_menu)
    if await get_partner(uid):
        return await msg.answer("Ты уже в чате.", reply_markup=chat_menu)

    await add_to_queue(uid)
    await msg.answer("Ищем... (до 30 сек)", reply_markup=waiting_menu)

    task = asyncio.create_task(wait_for_partner(uid))
    waiting_tasks[uid] = task

# ... (wait_for_partner, cancel, stop_cmd, next_cmd, report, report_reason — без изменений)

# --- НОВЫЕ МОД-КОМАНДЫ ---
@dp.message_handler(commands=['user'])
async def user_info(msg: types.Message):
    if msg.from_user.id != MODERATOR_ID:
        return await msg.answer("Только для модератора.")
    text = msg.text.strip()
    if len(text.split()) < 2:
        return await msg.answer("Использование: /user <id или код>")
    query = text.split()[1]

    uid = None
    if query.isdigit():
        uid = int(query)
    else:
        # Поиск по коду
        for u, c in user_codes.items():
            if c == query.upper():
                uid = u
                break
        if not uid and db_pool:
            async with db_pool.acquire() as conn:
                uid = await conn.fetchval("SELECT user_id FROM users WHERE code = $1", query.upper())

    if not uid:
        return await msg.answer("Пользователь не найден.")

    status = "в чате" if await get_partner(uid) else "не в чате"
    banned = "да" if await is_banned(uid) else "нет"
    code = await get_or_create_code(uid)
    await msg.answer(
        f"<b>Пользователь</b>\n"
        f"ID: <code>{uid}</code>\n"
        f"Код: <code>{code}</code>\n"
        f"Статус: {status}\n"
        f"Забанен: {banned}",
        parse_mode="HTML"
    )

@dp.message_handler(commands=['ban'])
async def ban_user(msg: types.Message):
    if msg.from_user.id != MODERATOR_ID:
        return
    text = msg.text.strip()
    if len(text.split()) < 2:
        return await msg.answer("Использование: /ban <id или код>")
    query = text.split()[1]
    uid = None
    if query.isdigit():
        uid = int(query)
    else:
        for u, c in user_codes.items():
            if c == query.upper():
                uid = u
                break
        if not uid and db_pool:
            async with db_pool.acquire() as conn:
                uid = await conn.fetchval("SELECT user_id FROM users WHERE code = $1", query.upper())
    if not uid:
        return await msg.answer("Не найден.")
    memory_banned.add(uid)
    await break_pair(uid)
    await bot.send_message(uid, "Вы забанены.")
    await msg.answer("Забанен.")

@dp.message_handler(commands=['unban'])
async def unban_user(msg: types.Message):
    if msg.from_user.id != MODERATOR_ID:
        return
    text = msg.text.strip()
    if len(text.split()) < 2:
        return await msg.answer("Использование: /unban <id или код>")
    query = text.split()[1]
    uid = None
    if query.isdigit():
        uid = int(query)
    else:
        for u, c in user_codes.items():
            if c == query.upper():
                uid = u
                break
        if not uid and db_pool:
            async with db_pool.acquire() as conn:
                uid = await conn.fetchval("SELECT user_id FROM users WHERE code = $1", query.upper())
    if not uid:
        return await msg.answer("Не найден.")
    memory_banned.discard(uid)
    await bot.send_message(uid, "Вы разбанены.")
    await msg.answer("Разбанен.")

# ... (остальные хэндлеры без изменений)

# ---------- Запуск ----------
async def on_startup(_):
    await init_db()
    log.info("Бот запущен")

if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup)
