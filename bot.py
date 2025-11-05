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
memory_reports = []  # [{id, from, to, reason}]
user_complaints = {}  # uid -> количество жалоб НА него

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

# ---------- АВТОБАН И СЧЁТЧИК ЖАЛОБ ----------
async def increment_complaints(uid):
    """Увеличить счётчик жалоб и проверить автобан"""
    user_complaints[uid] = user_complaints.get(uid, 0) + 1
    count = user_complaints[uid]
    if count >= 5 and not await is_banned(uid):
        await ban_user_auto(uid)
    return count

async def ban_user_auto(uid):
    """Автобан"""
    memory_banned.add(uid)
    if db_pool:
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE users SET banned = TRUE WHERE user_id = $1", uid)
    await break_pair(uid)
    await bot.send_message(uid, "Вы забанены автоматически (5+ жалоб).")
    if MODERATOR_ID:
        code = await get_user_code(uid) or "—"
        await bot.send_message(MODERATOR_ID, f"АВТОБАН: <code>{uid}</code> (<code>{code}</code>) — 5+ жалоб")

async def clear_complaints(uid):
    """Обнулить жалобы при разбане"""
    user_complaints.pop(uid, None)
    global memory_reports
    memory_reports = [r for r in memory_reports if r['to'] != uid]

# ---------- ХЭНДЛЕРЫ ----------
waiting_tasks = {}
user_reporting = {}
user_codes = {}

# --- ЖАЛОБА (с автобаном) ---
@dp.message_handler(lambda m: m.from_user.id in user_reporting)
async def report_reason(msg: types.Message):
    uid = msg.from_user.id
    partner = user_reporting.pop(uid)
    reason = msg.text or "Без причины"
    report_id = len(memory_reports) + 1

    from_code = await get_user_code(uid) or "—"
    to_code = await get_user_code(partner) or "—"

    memory_reports.append({"id": report_id, "from": uid, "to": partner, "reason": reason})
    await break_pair(uid)
    await msg.answer("Жалоба отправлена.", reply_markup=main_menu)
    await bot.send_message(partner, "Чат завершён.", reply_markup=main_menu)

    # Увеличиваем счётчик и проверяем автобан
    count = await increment_complaints(partner)

    if MODERATOR_ID:
        await bot.send_message(
            MODERATOR_ID,
            f"<b>ЖАЛОБА #{report_id}</b>\n"
            f"От: <code>{uid}</code> (<code>{from_code}</code>)\n"
            f"На: <code>{partner}</code> (<code>{to_code}</code>)\n"
            f"Причина: {reason}\n"
            f"Жалоб на него: {count}\n"
            f"/mod",
            parse_mode="HTML"
        )

# --- /unban (с обнулением жалоб) ---
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
    if db_pool:
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE users SET banned = FALSE WHERE user_id = $1", uid)
    await bot.send_message(uid, "Вы разбанены.")
    await clear_complaints(uid)  # ← ОБНУЛЕНИЕ ЖАЛОБ
    await msg.answer("Разбанен. Жалобы обнулены.")

# --- CALLBACK (unban тоже обнуляет) ---
@dp.callback_query_handler(lambda c: c.data.startswith("unban_"))
async def unban_cb(call: types.CallbackQuery):
    if call.from_user.id != MODERATOR_ID:
        return await call.answer("Нет доступа", show_alert=True)
    uid = int(call.data.split("_")[1])
    memory_banned.discard(uid)
    if db_pool:
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE users SET banned = FALSE WHERE user_id = $1", uid)
    await bot.send_message(uid, "Вы разбанены.")
    await clear_complaints(uid)
    await call.answer("Разбанен. Жалобы обнулены.")

# --- Остальные хэндлеры (без изменений) ---
# ... (всё остальное — как в предыдущей версии)
