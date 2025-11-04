import os
import asyncpg
from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove

# ---------- Конфигурация ----------
TOKEN = os.getenv("BOT_TOKEN")
PGHOST = os.getenv("PGHOST")
PGUSER = os.getenv("PGUSER")
PGPASSWORD = os.getenv("PGPASSWORD")
PGDATABASE = os.getenv("PGDATABASE")
PGPORT = int(os.getenv("PGPORT", "5432"))

# ID модератора задаётся через переменные окружения
MODERATOR_ID = int(os.getenv("MODERATOR_ID", "0"))

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

bot = Bot(token=TOKEN)
dp = Dispatcher(bot)

db_pool = None

# ---------- Клавиатуры ----------
main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton("🔍 Найти собеседника")],
        [KeyboardButton("ℹ️ Помощь")]
    ],
    resize_keyboard=True
)

chat_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton("⏹ Стоп"), KeyboardButton("➡️ Следующий")],
        [KeyboardButton("⚠️ Пожаловаться")]
    ],
    resize_keyboard=True
)

waiting_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton("❌ Отмена")]
    ],
    resize_keyboard=True
)

mod_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton("📋 Жалобы"), KeyboardButton("📊 Статистика")]
    ],
    resize_keyboard=True
)

# ---------- Работа с БД ----------
async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(
        host=PGHOST, user=PGUSER, password=PGPASSWORD,
        database=PGDATABASE, port=PGPORT, min_size=1, max_size=5
    )
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                gender VARCHAR(10),
                age INT,
                interests TEXT,
                status VARCHAR(20) DEFAULT 'idle'
            );
            CREATE TABLE IF NOT EXISTS queue (
                user_id BIGINT PRIMARY KEY,
                joined_at TIMESTAMP DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS pairs (
                user_id BIGINT PRIMARY KEY,
                partner_id BIGINT NOT NULL,
                started_at TIMESTAMP DEFAULT NOW()
            );
        """)

async def ensure_user(user_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO users(user_id, status)
            VALUES($1, 'idle')
            ON CONFLICT (user_id) DO NOTHING
        """, user_id)

async def set_status(user_id: int, status: str):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET status=$2 WHERE user_id=$1", user_id, status)

async def add_to_queue(user_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO queue(user_id) VALUES($1) ON CONFLICT DO NOTHING", user_id)

async def remove_from_queue(user_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM queue WHERE user_id=$1", user_id)

async def find_partner(exclude_id: int):
    async with db_pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT user_id FROM queue WHERE user_id != $1 ORDER BY joined_at ASC LIMIT 1",
            exclude_id
        )

async def create_pair(a: int, b: int):
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM queue WHERE user_id IN ($1, $2)", a, b)
            await conn.execute(
                "INSERT INTO pairs(user_id, partner_id) VALUES($1, $2), ($2, $1) ON CONFLICT DO NOTHING",
                a, b
            )
            await conn.execute("UPDATE users SET status='chatting' WHERE user_id IN ($1, $2)", a, b)

async def get_partner(user_id: int):
    async with db_pool.acquire() as conn:
        return await conn.fetchval("SELECT partner_id FROM pairs WHERE user_id=$1", user_id)

async def break_pair(user_id: int):
    async with db_pool.acquire() as conn:
        partner = await conn.fetchval("SELECT partner_id FROM pairs WHERE user_id=$1", user_id)
        if partner:
            async with conn.transaction():
                await conn.execute("DELETE FROM pairs WHERE user_id IN ($1, $2)", user_id, partner)
                await conn.execute("UPDATE users SET status='idle' WHERE user_id IN ($1, $2)", user_id, partner)
        return partner

# ---------- Хэндлеры ----------
@dp.message_handler(commands=['start'])
async def start(msg: types.Message):
    await ensure_user(msg.from_user.id)
    await msg.answer("Привет! Я анонимный чат-бот.\nВыбери действие:", reply_markup=main_menu)

@dp.message_handler(lambda m: m.text in ["ℹ️ Помощь", "/help"])
async def help_cmd(msg: types.Message):
    await msg.answer("Напиши «🔍 Найти собеседника», чтобы начать поиск. Когда захочешь выйти — «⏹ Стоп».", reply_markup=main_menu)

@dp.message_handler(lambda m: m.text in ["🔍 Найти собеседника", "/search"])
async def search(msg: types.Message):
    uid = msg.from_user.id
    await ensure_user(uid)

    partner_now = await get_partner(uid)
    if partner_now:
        await msg.answer("Ты уже общаешься. Нажми «⏹ Стоп», чтобы завершить текущий чат.", reply_markup=chat_menu)
        return

    partner = await find_partner(uid)
    if partner:
        await create_pair(uid, partner)
        await bot.send_message(uid, "🔗 Собеседник найден! Можешь писать.", reply_markup=chat_menu)
        await bot.send_message(partner, "🔗 Собеседник найден! Можешь писать.", reply_markup=chat_menu)
    else:
        await add_to_queue(uid)
        await set_status(uid, 'waiting')
        await msg.answer("⏳ Ожидание собеседника...", reply_markup=waiting_menu)

@dp.message_handler(lambda m: m.text in ["❌ Отмена"])
async def cancel_search(msg: types.Message):
    uid = msg.from_user.id
    await remove_from_queue(uid)
    await set_status(uid, 'idle')
    await msg.answer("Поиск отменён.", reply_markup=main_menu)

@dp.message_handler(lambda m: m.text in ["⏹ Стоп", "/stop"])
async def stop(msg: types.Message):
    uid = msg.from_user.id
    await remove_from_queue(uid)
    partner = await break_pair(uid)
    if partner:
        await bot.send_message(partner, "❌ Собеседник покинул чат.", reply_markup=main_menu)
        await msg.answer("❌ Ты покинул чат.", reply_markup=main_menu)
    else:
        await msg.answer("Ты не в чате.", reply_markup=main_menu)

@dp.message_handler(lambda m: m.text in ["➡️ Следующий"])
async def next_chat(msg: types.Message):
    await stop(msg)
    await search(msg)

# ---------- Жалобы ----------
user_reporting = {}  # user_id -> partner_id

@dp.message_handler(lambda m: m.text in ["⚠️ Пожаловаться"])
async def report_start(msg: types.Message):
    uid = msg.from_user.id
    partner = await get_partner(uid)
    if partner:
        user_reporting[uid] = partner
        await msg.answer("Опиши причину жалобы текстом:", reply_markup=ReplyKeyboardRemove())
    else:
        await msg.answer("Ты не в чате.", reply_markup=main_menu)

@dp.message_handler(lambda m: m.from_user.id in user_reporting)
async def report_reason(msg: types.Message):
    uid = msg.from_user.id
    partner = user_reporting.pop(uid)
    reason = msg.text

    # Отправляем жалобу модератору
    if MODERATOR_ID:
        await bot.send_message(
            MODERATOR_ID,
            f"⚠️ Жалоба!\n"
            f"От: {uid}\n"
            f"На: {partner}\n"
            f"Причина: {reason}"
        )

    # Завершаем чат у обоих
    await break_pair(uid)
    await bot.send_message(uid, "Жалоба отправлена. Чат завершён.", reply_markup=main_menu)
    await bot.send_message(partner, "❌ Собеседник покинул чат.", reply_markup=main_menu)

# ---------- Мод-панель ----------
@dp.message_handler(commands=['mod'])
async def mod_panel(msg: types.Message):
    if msg.from_user.id != MODERATOR_ID:
        await msg.answer("⛔ У тебя нет доступа к мод‑пан
