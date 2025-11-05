import os
import asyncio
import logging
from collections import deque
import random
import string
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
user_codes = {}

# ---------- КЛАВИАТУРЫ ----------
main_menu = ReplyKeyboardMarkup(resize_keyboard=True)
main_menu.add(KeyboardButton("Найти"), KeyboardButton("Помощь"))
main_menu.add(KeyboardButton("Мой код"))  # ← Изменено с "Мой ID"

chat_menu = ReplyKeyboardMarkup(resize_keyboard=True)
chat_menu.add(KeyboardButton("Стоп"), KeyboardButton("Следующий"))
chat_menu.add(KeyboardButton("Пожаловаться"))

waiting_menu = ReplyKeyboardMarkup(resize_keyboard=True)
waiting_menu.add(KeyboardButton("Отмена"))

mod_menu = ReplyKeyboardMarkup(resize_keyboard=True)
mod_menu.add(KeyboardButton("Жалобы"), KeyboardButton("Статистика"))
mod_menu.add(KeyboardButton("Баны"), KeyboardButton("Выход"))

# ---------- КОДЫ ----------
def generate_code():
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

async def get_user_code(uid):
    if uid in user_codes:
        return user_codes[uid]
    if db_pool:
        async with db_pool.acquire() as conn:
            code = await conn.fetchval("SELECT code FROM users WHERE user_id = $1", uid)
            if code:
                user_codes[uid] = code
                return code
    return None

async def get_or_create_code(uid):
    code = await get_user_code(uid)
    if code:
        return code
    code = generate_code()
    while any(code == c for c in user_codes.values()):
        code = generate_code()
    user_codes[uid] = code
    if db_pool:
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE users SET code = $1 WHERE user_id = $2", code, uid)
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
                    status VARCHAR(20) DEFAULT 'idle'
                );
                CREATE TABLE IF NOT EXISTS queue (user_id BIGINT PRIMARY KEY, joined_at TIMESTAMP DEFAULT NOW());
                CREATE TABLE IF NOT EXISTS pairs (user_id BIGINT PRIMARY KEY, partner_id BIGINT);
                CREATE INDEX IF NOT EXISTS idx_queue ON queue (joined_at);
            """)
            try:
                await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS banned BOOLEAN DEFAULT FALSE")
            except:
                pass
            try:
                await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS code TEXT")
            except:
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
            exists = await conn.fetchval("SELECT 1 FROM users WHERE user_id = $1", uid)
            if not exists:
                code = await get_or_create_code(uid)
                await conn.execute("INSERT INTO users(user_id, code) VALUES($1, $2)", uid, code)
            else:
                code = await conn.fetchval("SELECT code FROM users WHERE user_id = $1", uid)
                if not code:
                    code = await get_or_create_code(uid)
                    await conn.execute("UPDATE users SET code = $1 WHERE user_id = $2", code, uid)
    else:
        memory_status[uid] = memory_status.get(uid, 'idle')
        await get_or_create_code(uid)

async def add_to_queue(uid): ...
async def remove_from_queue(uid): ...
async def find_partner(exclude_id): ...
async def create_pair(a, b): ...
async def get_partner(uid): ...
async def break_pair(uid): ...
async def is_banned(uid): ...

# (все вспомогательные функции — без изменений, как в предыдущей версии)

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
        "• Мой код — твой уникальный код",
        reply_markup=main_menu
    )

# --- КНОПКА "Мой код" (ТОЛЬКО КОД) ---
@dp.message_handler(lambda m: m.text == "Мой код")
async def my_code_button(msg: types.Message):
    uid = msg.from_user.id
    code = await get_or_create_code(uid)
    await msg.answer(f"Твой код: <code>{code}</code>", parse_mode="HTML", reply_markup=main_menu)

# --- Остальные кнопки ---
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
        return
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
async def search(msg: types.Message): ...

async def wait_for_partner(uid): ...

@dp.message_handler(commands=['cancel'])
async def cancel(msg: types.Message): ...

@dp.message_handler(commands=['stop'])
async def stop_cmd(msg: types.Message): ...

@dp.message_handler(commands=['next'])
async def next_cmd(msg: types.Message): ...

@dp.message_handler(commands=['report'])
async def report(msg: types.Message): ...

# --- ЖАЛОБА С КОДАМИ ---
@dp.message_handler(lambda m: m.from_user.id in user_reporting)
async def report_reason(msg: types.Message):
    uid = msg.from_user.id
    partner = user_reporting.pop(uid)
    reason = msg.text or "Без причины"
    report_id = len(memory_reports) + 1

    # Получаем коды
    from_code = await get_user_code(uid) or "—"
    to_code = await get_user_code(partner) or "—"

    memory_reports.append({"id": report_id, "from": uid, "to": partner, "reason": reason})
    await break_pair(uid)
    await msg.answer("Жалоба отправлена.", reply_markup=main_menu)
    await bot.send_message(partner, "Чат завершён.", reply_markup=main_menu)

    if MODERATOR_ID:
        await bot.send_message(
            MODERATOR_ID,
            f"<b>ЖАЛОБА #{report_id}</b>\n"
            f"От: <code>{uid}</code> (<code>{from_code}</code>)\n"
            f"На: <code>{partner}</code> (<code>{to_code}</code>)\n"
            f"Причина: {reason}\n"
            f"/mod",
            parse_mode="HTML"
        )

# --- МОДЕРАТОРСКИЕ КНОПКИ ---
@dp.message_handler(lambda m: m.text == "Жалобы")
async def complaints_button(msg: types.Message): ...

@dp.message_handler(lambda m: m.text == "Статистика")
async def stats_button(msg: types.Message): ...

@dp.message_handler(lambda m: m.text == "Баны")
async def bans_button(msg: types.Message): ...

@dp.message_handler(lambda m: m.text == "Выход")
async def exit_button(msg: types.Message):
    if msg.from_user.id != MODERATOR_ID: return
    await msg.answer("Выход в главное меню.", reply_markup=main_menu)

# --- МОДЕРАТОРСКИЕ КОМАНДЫ ---
@dp.message_handler(commands=['complaints'])
async def show_reports(msg: types.Message): ...

@dp.message_handler(commands=['stats'])
async def stats(msg: types.Message): ...

@dp.message_handler(commands=['bans'])
async def show_bans(msg: types.Message): ...

# --- /user (модератор видит ID + код) ---
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
    code = await get_user_code(uid) or "Нет кода"
    await msg.answer(
        f"<b>Пользователь</b>\n"
        f"ID: <code>{uid}</code>\n"
        f"Код: <code>{code}</code>\n"
        f"Статус: {status}\n"
        f"Забанен: {banned}",
        parse_mode="HTML"
    )

# --- /ban, /unban (с сохранением в БД) ---
@dp.message_handler(commands=['ban'])
async def ban_user(msg: types.Message): ...

@dp.message_handler(commands=['unban'])
async def unban_user(msg: types.Message): ...

# --- CALLBACK (с сохранением бана в БД) ---
@dp.callback_query_handler(lambda c: c.data and c.data.startswith(("ban_", "ign_", "unban_")))
async def mod_cb(call: types.CallbackQuery): ...

# --- ПЕРЕСЫЛКА ---
@dp.message_handler(content_types=types.ContentTypes.ANY)
async def relay(msg: types.Message): ...

# ---------- Запуск ----------
async def on_startup(_):
    await init_db()
    log.info("Бот запущен")

if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup)
