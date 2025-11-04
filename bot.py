import os
import asyncpg
from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor

# Читаем переменные окружения (Railway задаёт их в Variables)
TOKEN = os.getenv("BOT_TOKEN")
PGHOST = os.getenv("PGHOST")
PGUSER = os.getenv("PGUSER")
PGPASSWORD = os.getenv("PGPASSWORD")
PGDATABASE = os.getenv("PGDATABASE")
PGPORT = int(os.getenv("PGPORT", "5432"))

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

bot = Bot(token=TOKEN)
dp = Dispatcher(bot)

db_pool = None

# Инициализация базы + автоматическое создание таблиц
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

# Утилиты работы с БД
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

# Хэндлеры команд
@dp.message_handler(commands=['start'])
async def start(msg: types.Message):
    await ensure_user(msg.from_user.id)
    await msg.answer(
        "Привет! Я анонимный чат-бот.\n"
        "Команды:\n"
        "/search — найти собеседника\n"
        "/stop — завершить чат\n"
        "/help — помощь"
    )

@dp.message_handler(commands=['help'])
async def help_cmd(msg: types.Message):
    await msg.answer("Напиши /search чтобы найти собеседника. Когда захочешь выйти — /stop.")

@dp.message_handler(commands=['search'])
async def search(msg: types.Message):
    uid = msg.from_user.id
    await ensure_user(uid)

    partner_now = await get_partner(uid)
    if partner_now:
        await msg.answer("Ты уже общаешься. Напиши /stop чтобы завершить текущий чат.")
        return

    partner = await find_partner(uid)
    if partner:
        await create_pair(uid, partner)
        await bot.send_message(uid, "🔗 Собеседник найден! Можешь писать.")
        await bot.send_message(partner, "🔗 Собеседник найден! Можешь писать.")
    else:
        await add_to_queue(uid)
        await set_status(uid, 'waiting')
        await msg.answer("⏳ Ожидание собеседника...")

@dp.message_handler(commands=['stop'])
async def stop(msg: types.Message):
    uid = msg.from_user.id
    await remove_from_queue(uid)
    partner = await break_pair(uid)
    if partner:
        await bot.send_message(partner, "❌ Собеседник покинул чат.")
        await msg.answer("❌ Ты покинул чат.")
    else:
        await msg.answer("Ты не в чате. Если хочешь найти собеседника, напиши /search.")

# Пересылка сообщений
@dp.message_handler(content_types=types.ContentTypes.ANY)
async def relay(msg: types.Message):
    uid = msg.from_user.id
    partner = await get_partner(uid)
    if not partner:
        await msg.answer("Ты не в чате. Напиши /search чтобы найти собеседника.")
        return

    if msg.text:
        await bot.send_message(partner, msg.text)
    elif msg.photo:
        await bot.send_photo(partner, msg.photo[-1].file_id, caption=msg.caption)
    elif msg.sticker:
        await bot.send_sticker(partner, msg.sticker.file_id)
    elif msg.voice:
        await bot.send_voice(partner, msg.voice.file_id, caption=msg.caption)
    elif msg.document:
        await bot.send_document(partner, msg.document.file_id, caption=msg.caption)
    elif msg.video:
        await bot.send_video(partner, msg.video.file_id, caption=msg.caption)
    else:
        await bot.send_message(partner, "Получено сообщение.")

# Запуск
async def on_startup(_):
    await init_db()
    print("Bot started and DB pool initialized")

async def on_shutdown(_):
    if db_pool:
        await db_pool.close()
    await bot.session.close()

if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup, on_shutdown=on_shutdown)
