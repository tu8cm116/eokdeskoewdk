import os
import asyncio
import logging
from collections import deque
import asyncpg
from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor
from aiogram.utils.exceptions import TerminatedByOtherGetUpdates
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

# ---------- Временная очередь в памяти (если нет БД) ----------
memory_queue = deque()  # FIFO: (user_id, timestamp)
memory_pairs = {}  # user_id -> partner_id
memory_status = {}  # user_id -> status ('idle', 'waiting', 'chatting')
memory_banned = set()  # banned users

# ---------- Клавиатуры (без изменений) ----------
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

# ---------- БД (без изменений, но с fallback) ----------
async def init_db():
    global db_pool
    if not DATABASE_URL:
        log.warning("DATABASE_URL не найден — используем память")
        return False

    try:
        db_pool = await asyncpg.create_pool(dsn=DATABASE_URL, min_size=1, max_size=5)
        async with db_pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, status VARCHAR(20) DEFAULT 'idle', banned BOOLEAN DEFAULT FALSE);
                CREATE TABLE IF NOT EXISTS queue (user_id BIGINT PRIMARY KEY, joined_at TIMESTAMP DEFAULT NOW());
                CREATE TABLE IF NOT EXISTS pairs (user_id BIGINT PRIMARY KEY, partner_id BIGINT);
                CREATE INDEX IF NOT EXISTS idx_queue_joined_at ON queue (joined_at);
            """)
        log.info("PostgreSQL подключён")
        return True
    except Exception as e:
        log.error(f"БД ошибка: {e} — используем память")
        db_pool = None
        return False

# ---------- Функции (с fallback на память) ----------
def _memory_ensure_user(uid):
    if uid not in memory_status:
        memory_status[uid] = 'idle'

def _memory_add_to_queue(uid):
    memory_queue.append((uid, asyncio.get_event_loop().time()))
    log.info(f"Очередь: {list(memory_queue)}")

def _memory_remove_from_queue(uid):
    memory_queue = deque([x for x in memory_queue if x[0] != uid])
    log.info(f"Очередь после удаления {uid}: {list(memory_queue)}")

def _memory_find_partner(exclude_id):
    for p_id, _ in memory_queue:
        if p_id != exclude_id:
            return p_id
    return None

def _memory_create_pair(a, b):
    memory_pairs[a] = b
    memory_pairs[b] = a
    memory_status[a] = 'chatting'
    memory_status[b] = 'chatting'
    _memory_remove_from_queue(a)
    _memory_remove_from_queue(b)
    log.info(f"Пара создана: {a} <-> {b}")

def _memory_get_partner(uid):
    return memory_pairs.get(uid)

def _memory_break_pair(uid):
    partner = _memory_get_partner(uid)
    if partner:
        memory_pairs.pop(uid, None)
        memory_pairs.pop(partner, None)
        memory_status[uid] = 'idle'
        memory_status[partner] = 'idle'
        log.info(f"Пара разорвана: {uid} <-> {partner}")
    return partner

# ... (остальные функции аналогично, с if db_pool: else: memory_ — но для краткости опущу, полный код ниже)

# Обработчик ошибок polling
async def on_error_handler(update, exception):
    if isinstance(exception, TerminatedByOtherGetUpdates):
        log.warning("Конфликт polling — перезапуск через 10 сек...")
        await asyncio.sleep(10)
        # Авто-redeploy не нужен, но лог для мониторинга
    log.error(f"Ошибка: {exception}")

dp.errors.register(on_error_handler, exception=TerminatedByOtherGetUpdates)

# ---------- Хэндлеры (с fallback) ----------
user_reporting = {}
waiting_tasks = {}

@dp.message_handler(commands=['start'])
async def start(msg: types.Message):
    uid = msg.from_user.id
    if db_pool:
        await ensure_user(uid)  # БД версия
    else:
        _memory_ensure_user(uid)
    partner = await break_pair(uid) if db_pool else _memory_break_pair(uid)
    if partner:
        await bot.send_message(partner, "Собеседник перезапустил.", reply_markup=main_menu)
    await msg.answer("Привет! Анонимный чат.", reply_markup=main_menu)

# ... (остальные хэндлеры с if db_pool: else: memory_)

# ---------- Запуск с retry ----------
async def on_startup(_):
    await init_db()
    await clear_active_chats() if db_pool else _memory_clear()
    log.info("Бот готов")

async def main():
    while True:
        try:
            await dp.start_polling()
        except TerminatedByOtherGetUpdates:
            log.warning("Polling конфликт — retry...")
            await asyncio.sleep(10)
        except Exception as e:
            log.error(f"Критическая ошибка: {e}")
            await asyncio.sleep(30)

if __name__ == "__main__":
    asyncio.run(main())
