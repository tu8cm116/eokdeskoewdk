import os
import asyncio
import logging
from collections import deque
import asyncpg
from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor
from aiogram.types import ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton

# ---------- Логирование ----------
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
log = logging.getLogger(__name__)

# ---------- Конфигурация ----------
TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
MODERATOR_ID = int(os.getenv("MODERATOR_ID", "0"))  # ТВОЙ ID: 684261784

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

# ---------- БД (опционально) ----------
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
                    banned BOOLEAN DEFAULT FALSE
                );
                CREATE TABLE IF NOT EXISTS queue (user_id BIGINT PRIMARY KEY, joined_at TIMESTAMP DEFAULT NOW());
                CREATE TABLE IF NOT EXISTS pairs (user_id BIGINT PRIMARY KEY, partner_id BIGINT);
                CREATE INDEX IF NOT EXISTS idx_queue ON queue (joined_at);
            """)
        log.info("PostgreSQL подключён")
        return True
    except Exception as e:
        log.error(f"БД ошибка: {e}")
        return False

# ---------- Вспомогательные ----------
async def ensure_user(uid):
    if db_pool:
        async with db_pool.acquire() as conn:
            await conn.execute("INSERT INTO users(user_id) VALUES($1) ON CONFLICT DO NOTHING", uid)
    else:
        memory_status[uid] = memory_status.get(uid, 'idle')

async def add_to_queue(uid):
    if db_pool:
        async with db_pool.acquire() as conn:
            await conn.execute("INSERT INTO queue(user_id) VALUES($1) ON CONFLICT DO NOTHING", uid)
    else:
        memory_queue.append((uid, asyncio.get_event_loop().time()))
    log.info(f"Очередь: {[u[0] for u in memory_queue] if not db_pool else 'БД'}")

async def remove_from_queue(uid):
    if db_pool:
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM queue WHERE user_id = $1", uid)
    else:
        global memory_queue
        memory_queue = deque([x for x in memory_queue if x[0] != uid])

async def find_partner(exclude_id):
    if db_pool:
        async with db_pool.acquire() as conn:
            return await conn.fetchval("SELECT user_id FROM queue WHERE user_id != $1 ORDER BY joined_at LIMIT 1", exclude_id)
    else:
        for uid, _ in memory_queue:
            if uid != exclude_id:
                return uid
        return None

async def create_pair(a, b):
    if db_pool:
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM queue WHERE user_id IN ($1, $2)", a, b)
                await conn.execute("INSERT INTO pairs(user_id, partner_id) VALUES($1, $2), ($2, $1) ON CONFLICT DO NOTHING", a, b)
    else:
        memory_pairs[a] = b
        memory_pairs[b] = a
        memory_status[a] = memory_status[b] = 'chatting'
        await remove_from_queue(a)
        await remove_from_queue(b)
    log.info(f"Пара: {a} <-> {b}")

async def get_partner(uid):
    if db_pool:
        async with db_pool.acquire() as conn:
            return await conn.fetchval("SELECT partner_id FROM pairs WHERE user_id = $1", uid)
    else:
        return memory_pairs.get(uid)

async def break_pair(uid):
    partner = await get_partner(uid)
    if partner:
        if db_pool:
            async with db_pool.acquire() as conn:
                await conn.execute("DELETE FROM pairs WHERE user_id IN ($1, $2)", uid, partner)
        else:
            memory_pairs.pop(uid, None)
            memory_pairs.pop(partner, None)
            memory_status[uid] = memory_status[partner] = 'idle'
        log.info(f"Разрыв: {uid} <-> {partner}")
    return partner

async def is_banned(uid):
    if db_pool:
        async with db_pool.acquire() as conn:
            return await conn.fetchval("SELECT banned FROM users WHERE user_id = $1", uid) or False
    else:
        return uid in memory_banned

# ---------- ХЭНДЛЕРЫ ----------
waiting_tasks = {}
user_reporting = {}

# --- МОДЕРАТОРСКИЕ КОМАНДЫ (ПЕРВЫМИ!) ---
@dp.message_handler(commands=['mod'])
async def mod_entry(msg: types.Message):
    if msg.from_user.id != MODERATOR_ID:
        return await msg.answer("Доступ запрещён.")
    await msg.answer(
        "<b>Модераторская панель:</b>\n"
        "/complaints — жалобы\n"
        "/stats — статистика\n"
        "/bans — баны\n"
        "/exit — выход",
        parse_mode="HTML"
    )

@dp.message_handler(commands=['id'])
async def get_id(msg: types.Message):
    await msg.answer(f"Твой ID: <code>{msg.from_user.id}</code>", parse_mode="HTML")

# --- ОСНОВНЫЕ КОМАНДЫ ---
@dp.message_handler(commands=['start'])
async def start(msg: types.Message):
    uid = msg.from_user.id
    await ensure_user(uid)
    await break_pair(uid)
    await remove_from_queue(uid)
    await msg.answer(
        "<b>Анонимный чат</b>\n\n"
        "<b>Команды:</b>\n"
        "/search — найти собеседника\n"
        "/stop — выйти\n"
        "/next — следующий\n"
        "/report — пожаловаться\n"
        "/cancel — отмена поиска",
        parse_mode="HTML"
    )

@dp.message_handler(commands=['search'])
async def search(msg: types.Message):
    uid = msg.from_user.id
    if await is_banned(uid):
        return await msg.answer("Вы забанены.")
    if await get_partner(uid):
        return await msg.answer("Ты уже в чате. Используй /stop")

    await add_to_queue(uid)
    await msg.answer("Ищем собеседника... (до 30 сек)\n/cancel — отменить")

    task = asyncio.create_task(wait_for_partner(uid))
    waiting_tasks[uid] = task

async def wait_for_partner(uid):
    for _ in range(30):
        await asyncio.sleep(1)
        partner = await find_partner(uid)
        if partner:
            await create_pair(uid, partner)
            await bot.send_message(uid, "Собеседник найден! Пиши.", parse_mode="HTML")
            await bot.send_message(partner, "Собеседник найден! Пиши.", parse_mode="HTML")
            if uid in waiting_tasks:
                del waiting_tasks[uid]
            return
    await remove_from_queue(uid)
    await bot.send_message(uid, "Не нашли. Попробуй позже.")
    if uid in waiting_tasks:
        del waiting_tasks[uid]

@dp.message_handler(commands=['cancel'])
async def cancel(msg: types.Message):
    uid = msg.from_user.id
    if uid in waiting_tasks:
        waiting_tasks[uid].cancel()
        del waiting_tasks[uid]
    await remove_from_queue(uid)
    await msg.answer("Поиск отменён.")

@dp.message_handler(commands=['stop'])
async def stop_cmd(msg: types.Message):
    uid = msg.from_user.id
    if uid in waiting_tasks:
        waiting_tasks[uid].cancel()
        del waiting_tasks[uid]
    partner = await break_pair(uid)
    if partner:
        await bot.send_message(partner, "Собеседник вышел.")
    await msg.answer("Ты вышел из чата.")

@dp.message_handler(commands=['next'])
async def next_cmd(msg: types.Message):
    await stop_cmd(msg)
    await search(msg)

@dp.message_handler(commands=['report'])
async def report(msg: types.Message):
    uid = msg.from_user.id
    partner = await get_partner(uid)
    if partner:
        user_reporting[uid] = partner
        await msg.answer("Опиши проблему (текстом):", reply_markup=ReplyKeyboardRemove())
    else:
        await msg.answer("Ты не в чате.")

@dp.message_handler(lambda m: m.from_user.id in user_reporting)
async def report_reason(msg: types.Message):
    uid = msg.from_user.id
    partner = user_reporting.pop(uid)
    reason = msg.text or "Без причины"
    report_id = len(memory_reports) + 1
    memory_reports.append({"id": report_id, "from": uid, "to": partner, "reason": reason})
    await break_pair(uid)
    await msg.answer("Жалоба отправлена.")
    await bot.send_message(partner, "Чат завершён из-за жалобы.")
    if MODERATOR_ID:
        await bot.send_message(MODERATOR_ID, f"НОВАЯ ЖАЛОБА!\nОт: {uid}\nНа: {partner}\nПричина: {reason}\n/mod")

# --- МОДЕРАТОРСКИЕ ПАНЕЛИ ---
@dp.message_handler(commands=['complaints'])
async def show_reports(msg: types.Message):
    if msg.from_user.id != MODERATOR_ID: return
    if not memory_reports:
        return await msg.answer("Нет жалоб.")
    for r in memory_reports:
        kb = InlineKeyboardMarkup(row_width=1)
        # УДАЛЕНА КНОПКА "Удалить чат"
        kb.add(InlineKeyboardButton("Забанить", callback_data=f"ban_{r['to']}"))
        kb.add(InlineKeyboardButton("Игнор", callback_data=f"ign_{r['id']}"))
        await msg.answer(
            f"<b>Жалоба #{r['id']}</b>\n"
            f"От: <a href='tg://user?id={r['from']}'>{r['from']}</a>\n"
            f"На: <a href='tg://user?id={r['to']}'>{r['to']}</a>\n"
            f"Причина: {r['reason']}",
            reply_markup=kb,
            parse_mode="HTML"
        )

@dp.message_handler(commands=['stats'])
async def stats(msg: types.Message):
    if msg.from_user.id != MODERATOR_ID: return
    total = len(memory_status)
    chatting = sum(1 for s in memory_status.values() if s == 'chatting')
    queue = len(memory_queue)
    banned = len(memory_banned)
    await msg.answer(
        f"<b>Статистика</b>\n"
        f"Пользователей: {total}\n"
        f"В чате: {chatting}\n"
        f"В поиске: {queue}\n"
        f"Забанено: {banned}",
        parse_mode="HTML"
    )

@dp.message_handler(commands=['bans'])
async def show_bans(msg: types.Message):
    if msg.from_user.id != MODERATOR_ID: return
    if not memory_banned:
        return await msg.answer("Нет забаненных.")
    kb = InlineKeyboardMarkup(row_width=1)
    for uid in memory_banned:
        kb.add(InlineKeyboardButton(f"Разбанить {uid}", callback_data=f"unban_{uid}"))
    await msg.answer("Забаненные:", reply_markup=kb)

@dp.message_handler(commands=['exit'])
async def mod_exit(msg: types.Message):
    if msg.from_user.id != MODERATOR_ID: return
    await msg.answer("Выход из мод-панели.")

# --- CALLBACK ---
@dp.callback_query_handler(lambda c: c.data and c.data.startswith(("ban_", "ign_", "unban_")))
async def mod_cb(call: types.CallbackQuery):
    if call.from_user.id != MODERATOR_ID:
        return await call.answer("Нет доступа", show_alert=True)
    d = call.data
    try:
        if d.startswith("ban_"):
            uid = int(d.split("_")[1])
            memory_banned.add(uid)
            await break_pair(uid)
            await bot.send_message(uid, "Вы забанены.")
            await call.answer("Забанен")
        elif d.startswith("ign_"):
            rid = int(d.split("_")[1])
            global memory_reports
            memory_reports = [r for r in memory_reports if r['id'] != rid]
            await call.answer("Игнор")
        elif d.startswith("unban_"):
            uid = int(d.split("_")[1])
            memory_banned.discard(uid)
            await bot.send_message(uid, "Вы разбанены.")
            await call.answer("Разбанен")
    except Exception as e:
        log.error(f"Мод ошибка: {e}")
        await call.answer("Ошибка")

# --- ПЕРЕСЫЛКА ---
@dp.message_handler(content_types=types.ContentTypes.ANY)
async def relay(msg: types.Message):
    partner = await get_partner(msg.from_user.id)
    if not partner: return
    try:
        if msg.text: await bot.send_message(partner, msg.text)
        elif msg.photo: await bot.send_photo(partner, msg.photo[-1].file_id, caption=msg.caption)
        elif msg.sticker: await bot.send_sticker(partner, msg.sticker.file_id)
        elif msg.voice: await bot.send_voice(partner, msg.voice.file_id)
        elif msg.document: await bot.send_document(partner, msg.document.file_id)
        elif msg.video: await bot.send_video(partner, msg.video.file_id)
        else: await bot.send_message(partner, "Сообщение получено.")
    except Exception as e:
        log.error(f"Ошибка пересылки: {e}")
        await break_pair(msg.from_user.id)
        await msg.answer("Ошибка. Чат прерван.")

# ---------- Запуск ----------
async def on_startup(_):
    await init_db()
    log.info("Бот запущен")

if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup)
