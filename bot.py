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
memory_status = {}      # uid -> 'idle' | 'chatting'
memory_banned = set()
memory_reports = []
user_complaints = {}
user_codes = {}

# ---------- КЛАВИАТУРЫ ----------
main_menu = ReplyKeyboardMarkup(resize_keyboard=True)
main_menu.add(KeyboardButton("Найти"), KeyboardButton("Помощь"))
main_menu.add(KeyboardButton("Мой код"))

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

# ---------- СИНХРОНИЗАЦИЯ ПРИ ЗАПУСКЕ ----------
async def load_banned_users():
    if db_pool:
        async with db_pool.acquire() as conn:
            banned = await conn.fetch("SELECT user_id FROM users WHERE banned = TRUE")
            for row in banned:
                memory_banned.add(row['user_id'])
    log.info(f"Загружено {len(memory_banned)} забаненных")

async def load_active_users():
    if db_pool:
        async with db_pool.acquire() as conn:
            # Загружаем всех, кто в очереди или в чате
            in_queue = await conn.fetch("SELECT user_id FROM queue")
            in_chat = await conn.fetch("SELECT user_id FROM pairs")
            for row in in_queue:
                uid = row['user_id']
                memory_status[uid] = 'searching'
            for row in in_chat:
                uid = row['user_id']
                memory_status[uid] = 'chatting'
    log.info(f"Загружено {len(memory_status)} активных пользователей")

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
    memory_status[uid] = memory_status.get(uid, 'idle')  # ← ФИКС: обновляем статус

async def add_to_queue(uid):
    if db_pool:
        async with db_pool.acquire() as conn:
            await conn.execute("INSERT INTO queue(user_id) VALUES($1) ON CONFLICT DO NOTHING", uid)
    else:
        memory_queue.append((uid, asyncio.get_event_loop().time()))
    memory_status[uid] = 'searching'  # ← ФИКС

async def remove_from_queue(uid):
    if db_pool:
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM queue WHERE user_id = $1", uid)
    else:
        global memory_queue
        memory_queue = deque([x for x in memory_queue if x[0] != uid])
    memory_status[uid] = 'idle'  # ← ФИКС

async def create_pair(a, b):
    if db_pool:
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM queue WHERE user_id IN ($1, $2)", a, b)
                await conn.execute("INSERT INTO pairs(user_id, partner_id) VALUES($1, $2), ($2, $1) ON CONFLICT DO NOTHING", a, b)
    else:
        memory_pairs[a] = b
        memory_pairs[b] = a
        await remove_from_queue(a)
        await remove_from_queue(b)
    memory_status[a] = memory_status[b] = 'chatting'  # ← ФИКС
    log.info(f"Пара: {a} <-> {b}")

async def break_pair(uid):
    partner = await get_partner(uid)
    if partner:
        if db_pool:
            async with db_pool.acquire() as conn:
                await conn.execute("DELETE FROM pairs WHERE user_id IN ($1, $2)", uid, partner)
        else:
            memory_pairs.pop(uid, None)
            memory_pairs.pop(partner, None)
        memory_status[uid] = memory_status[partner] = 'idle'  # ← ФИКС
        log.info(f"Разрыв: {uid} <-> {partner}")
    return partner

# ... (остальные вспомогательные без изменений)

# ---------- ХЭНДЛЕРЫ ----------
@dp.message_handler(commands=['start'])
async def start(msg: types.Message):
    uid = msg.from_user.id
    await ensure_user(uid)
    await break_pair(uid)
    await remove_from_queue(uid)
    memory_status[uid] = 'idle'  # ← ФИКС
    await msg.answer("Привет! Анонимный чат.", reply_markup=main_menu)

# ... (остальные хэндлеры — как в предыдущей версии)

# --- СТАТИСТИКА (ФИКС) ---
@dp.message_handler(commands=['stats'])
async def stats(msg: types.Message):
    if msg.from_user.id != MODERATOR_ID: return

    # Подсчёт из памяти
    total_users = len(memory_status)
    chatting = sum(1 for s in memory_status.values() if s == 'chatting')
    searching = sum(1 for s in memory_status.values() if s == 'searching')
    banned = len(memory_banned)

    await msg.answer(
        f"<b>Статистика</b>\n"
        f"Всего пользователей: {total_users}\n"
        f"В чате: {chatting}\n"
        f"В поиске: {searching}\n"
        f"Забанено: {banned}",
        parse_mode="HTML",
        reply_markup=mod_menu
    )

# ---------- ЗАПУСК ----------
async def on_startup(_):
    await init_db()
    await load_banned_users()     # ← ФИКС
    await load_active_users()     # ← ФИКС
    log.info("Бот запущен")

if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup)
