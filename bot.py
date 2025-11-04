import os
import asyncio
import logging
import asyncpg
from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    InlineKeyboardMarkup, InlineKeyboardButton
)

# ---------- Логирование ----------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s'
)
log = logging.getLogger(__name__)

# ---------- Конфигурация ----------
TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")  # Railway даёт эту переменную
MODERATOR_ID = int(os.getenv("MODERATOR_ID", "0"))

if not TOKEN:
    raise RuntimeError("BOT_TOKEN не установлен! Добавь в Railway Variables.")

bot = Bot(token=TOKEN, parse_mode="HTML")
dp = Dispatcher(bot)
db_pool = None

# ---------- Клавиатуры ----------
main_menu = ReplyKeyboardMarkup(resize_keyboard=True)
main_menu.add(KeyboardButton("Найти собеседника"), KeyboardButton("Помощь"))

chat_menu = ReplyKeyboardMarkup(resize_keyboard=True)
chat_menu.add(KeyboardButton("Стоп"), KeyboardButton("Следующий"))
chat_menu.add(KeyboardButton("Пожаловаться"))

waiting_menu = ReplyKeyboardMarkup(resize_keyboard=True)
waiting_menu.add(KeyboardButton("Отмена"))

mod_menu = ReplyKeyboardMarkup(resize_keyboard=True)
mod_menu.add(KeyboardButton("Жалобы"), KeyboardButton("Статистика"))
mod_menu.add(KeyboardButton("Баны"), KeyboardButton("Выход"))

# ---------- Работа с БД (безопасная) ----------
async def init_db():
    global db_pool
    if not DATABASE_URL:
        log.warning("DATABASE_URL не найден — работаем без БД (режим тестирования)")
        return False

    try:
        # Railway использует SSL
        db_pool = await asyncpg.create_pool(
            dsn=DATABASE_URL,
            min_size=1,
            max_size=5,
            command_timeout=60
        )
        log.info("Подключено к PostgreSQL (Railway)")

        async with db_pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    status VARCHAR(20) DEFAULT 'idle',
                    banned BOOLEAN DEFAULT FALSE
                );
                CREATE TABLE IF NOT EXISTS queue (user_id BIGINT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS pairs (user_id BIGINT PRIMARY KEY, partner_id BIGINT);
                CREATE TABLE IF NOT EXISTS reports (
                    id SERIAL PRIMARY KEY,
                    from_user BIGINT, to_user BIGINT, reason TEXT,
                    reported_at TIMESTAMP DEFAULT NOW(), resolved BOOLEAN DEFAULT FALSE
                );
                CREATE TABLE IF NOT EXISTS bans (user_id BIGINT PRIMARY KEY, reason TEXT);
            """)
        log.info("Таблицы проверены/созданы")
        return True
    except Exception as e:
        log.error(f"Ошибка подключения к БД: {e}")
        db_pool = None
        return False

# ---------- Вспомогательные функции (работают с/без БД) ----------
async def ensure_user(user_id: int):
    if db_pool:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO users(user_id, status, banned) VALUES($1, 'idle', FALSE) ON CONFLICT DO NOTHING",
                user_id
            )

async def get_partner(user_id: int):
    if not db_pool: return None
    async with db_pool.acquire() as conn:
        return await conn.fetchval("SELECT partner_id FROM pairs WHERE user_id = $1", user_id)

async def break_pair(user_id: int):
    if not db_pool: return None
    async with db_pool.acquire() as conn:
        partner = await conn.fetchval("SELECT partner_id FROM pairs WHERE user_id = $1", user_id)
        if partner:
            await conn.execute("DELETE FROM pairs WHERE user_id IN ($1, $2)", user_id, partner)
            await conn.execute("UPDATE users SET status = 'idle' WHERE user_id IN ($1, $2)", user_id, partner)
        return partner

async def is_banned(user_id: int) -> bool:
    if not db_pool: return False
    async with db_pool.acquire() as conn:
        return await conn.fetchval("SELECT banned FROM users WHERE user_id = $1", user_id) or False

# ---------- Хэндлеры ----------
@dp.message_handler(commands=['start'])
async def start(msg: types.Message):
    uid = msg.from_user.id
    await ensure_user(uid)

    # Разрываем старый чат
    partner = await break_pair(uid)
    if partner and db_pool:
        await bot.send_message(partner, "Собеседник перезапустил чат.", reply_markup=main_menu)

    await msg.answer(
        "Привет! Я анонимный чат-бот.\n"
        "Нажми «Найти собеседника», чтобы начать общение.",
        reply_markup=main_menu
    )

@dp.message_handler(lambda m: m.text == "Помощь")
async def help_cmd(msg: types.Message):
    await msg.answer(
        "• Найти собеседника — начать чат\n"
        "• Стоп — выйти\n"
        "• Следующий — найти нового\n"
        "• Пожаловаться — если что-то не так",
        reply_markup=main_menu
    )

@dp.message_handler(lambda m: m.text == "Найти собеседника")
async def search(msg: types.Message):
    uid = msg.from_user.id
    if await is_banned(uid):
        return await msg.answer("Вы забанены.")

    if await get_partner(uid):
        return await msg.answer("Ты уже в чате. Нажми «Стоп».", reply_markup=chat_menu)

    await msg.answer("Ищем собеседника...", reply_markup=waiting_menu)
    # Простая логика: пока без очереди (для теста)
    await asyncio.sleep(2)
    await msg.answer("Собеседник не найден. Попробуй позже.", reply_markup=main_menu)

@dp.message_handler(lambda m: m.text == "Стоп")
async def stop(msg: types.Message):
    partner = await break_pair(msg.from_user.id)
    if partner:
        await bot.send_message(partner, "Собеседник вышел.", reply_markup=main_menu)
        await msg.answer("Ты вышел из чата.", reply_markup=main_menu)
    else:
        await msg.answer("Ты не в чате.", reply_markup=main_menu)

# ---------- Мод-панель ----------
@dp.message_handler(commands=['mod'])
async def mod_panel(msg: types.Message):
    if msg.from_user.id != MODERATOR_ID:
        return await msg.answer("Доступ запрещён.")
    await msg.answer("Модераторская панель:", reply_markup=mod_menu)

@dp.message_handler(lambda m: m.text == "Статистика")
async def stats(msg: types.Message):
    if msg.from_user.id != MODERATOR_ID:
        return
    if not db_pool:
        return await msg.answer("Статистика недоступна (нет БД).")
    async with db_pool.acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM users")
        chatting = await conn.fetchval("SELECT COUNT(*) FROM users WHERE status = 'chatting'")
        banned = await conn.fetchval("SELECT COUNT(*) FROM users WHERE banned = TRUE")
    await msg.answer(
        f"<b>Статистика</b>\n"
        f"Пользователей: {total}\n"
        f"В чате: {chatting}\n"
        f"Забанено: {banned}",
        reply_markup=mod_menu
    )

# ---------- Пересылка (простая) ----------
@dp.message_handler(content_types=types.ContentTypes.ANY)
async def relay(msg: types.Message):
    partner = await get_partner(msg.from_user.id)
    if not partner:
        return
    try:
        if msg.text:
            await bot.send_message(partner, msg.text)
        elif msg.photo:
            await bot.send_photo(partner, msg.photo[-1].file_id, caption=msg.caption)
        elif msg.sticker:
            await bot.send_send_sticker(partner, msg.sticker.file_id)
        else:
            await bot.send_message(partner, "Сообщение получено.")
    except:
        await break_pair(msg.from_user.id)
        await msg.answer("Чат прерван.", reply_markup=main_menu)

# ---------- Запуск ----------
async def on_startup(_):
    success = await init_db()
    if success:
        log.info("База данных готова")
    else:
        log.warning("Работаем без БД — только базовые функции")

async def on_shutdown(_):
    if db_pool:
        await db_pool.close()
    await bot.session.close()
    log.info("Бот остановлен")

if __name__ == "__main__":
    log.info("Запуск бота...")
    executor.start_polling(
        dp,
        skip_updates=True,
        on_startup=on_startup,
        on_shutdown=on_shutdown
    )
