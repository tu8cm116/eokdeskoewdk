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
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
log = logging.getLogger(__name__)

# ---------- Конфигурация ----------
TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")  # Railway
MODERATOR_ID = int(os.getenv("MODERATOR_ID", "0"))

if not TOKEN:
    raise RuntimeError("BOT_TOKEN не задан!")

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

# ---------- БД ----------
async def init_db():
    global db_pool
    if not DATABASE_URL:
        log.warning("DATABASE_URL не найден — работаем без БД")
        return False

    try:
        db_pool = await asyncpg.create_pool(dsn=DATABASE_URL, min_size=1, max_size=5, command_timeout=60)
        log.info("Подключено к PostgreSQL")

        async with db_pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    status VARCHAR(20) DEFAULT 'idle',
                    banned BOOLEAN DEFAULT FALSE
                );
                CREATE TABLE IF NOT EXISTS queue (user_id BIGINT PRIMARY KEY, joined_at TIMESTAMP DEFAULT NOW());
                CREATE TABLE IF NOT EXISTS pairs (user_id BIGINT PRIMARY KEY, partner_id BIGINT);
                CREATE TABLE IF NOT EXISTS reports (
                    id SERIAL PRIMARY KEY,
                    from_user BIGINT, to_user BIGINT, reason TEXT,
                    reported_at TIMESTAMP DEFAULT NOW(), resolved BOOLEAN DEFAULT FALSE
                );
                CREATE TABLE IF NOT EXISTS bans (user_id BIGINT PRIMARY KEY, reason TEXT);
            """)
        log.info("Таблицы созданы")
        return True
    except Exception as e:
        log.error(f"БД ошибка: {e}")
        db_pool = None
        return False

# ---------- Вспомогательные ----------
async def ensure_user(uid):
    if db_pool:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO users(user_id, status, banned) VALUES($1, 'idle', FALSE) ON CONFLICT DO NOTHING",
                uid
            )

async def set_status(uid, status):
    if db_pool:
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE users SET status = $1 WHERE user_id = $2", status, uid)

async def add_to_queue(uid):
    if db_pool:
        async with db_pool.acquire() as conn:
            await conn.execute("INSERT INTO queue(user_id) VALUES($1) ON CONFLICT DO NOTHING", uid)

async def remove_from_queue(uid):
    if db_pool:
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM queue WHERE user_id = $1", uid)

async def find_partner(exclude_id):
    if not db_pool: return None
    async with db_pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT user_id FROM queue WHERE user_id != $1 ORDER BY joined_at ASC LIMIT 1",
            exclude_id
        )

async def create_pair(a, b):
    if not db_pool: return
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM queue WHERE user_id IN ($1, $2)", a, b)
            await conn.execute("INSERT INTO pairs(user_id, partner_id) VALUES($1, $2), ($2, $1)", a, b)
            await conn.execute("UPDATE users SET status = 'chatting' WHERE user_id IN ($1, $2)", a, b)

async def get_partner(uid):
    if not db_pool: return None
    async with db_pool.acquire() as conn:
        return await conn.fetchval("SELECT partner_id FROM pairs WHERE user_id = $1", uid)

async def break_pair(uid):
    if not db_pool: return None
    async with db_pool.acquire() as conn:
        partner = await conn.fetchval("SELECT partner_id FROM pairs WHERE user_id = $1", uid)
        if partner:
            async with conn.transaction():
                await conn.execute("DELETE FROM pairs WHERE user_id IN ($1, $2)", uid, partner)
                await conn.execute("UPDATE users SET status = 'idle' WHERE user_id IN ($1, $2)", uid, partner)
        return partner

async def is_banned(uid):
    if not db_pool: return False
    async with db_pool.acquire() as conn:
        return await conn.fetchval("SELECT banned FROM users WHERE user_id = $1", uid) or False

async def save_report(fr, to, reason):
    if db_pool:
        async with db_pool.acquire() as conn:
            await conn.execute("INSERT INTO reports(from_user, to_user, reason) VALUES($1, $2, $3)", fr, to, reason)

async def get_unresolved_reports():
    if not db_pool: return []
    async with db_pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM reports WHERE resolved = FALSE")

async def mark_report_resolved(rid):
    if db_pool:
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE reports SET resolved = TRUE WHERE id = $1", rid)

async def ban_user(uid, reason="Нарушение"):
    if db_pool:
        async with db_pool.acquire() as conn:
            await conn.execute("INSERT INTO bans(user_id, reason) VALUES($1, $2) ON CONFLICT DO UPDATE SET reason = $2", uid, reason)
            await conn.execute("UPDATE users SET banned = TRUE WHERE user_id = $1", uid)

async def unban_user(uid):
    if db_pool:
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM bans WHERE user_id = $1", uid)
            await conn.execute("UPDATE users SET banned = FALSE WHERE user_id = $1", uid)

async def get_banned_users():
    if not db_pool: return []
    async with db_pool.acquire() as conn:
        return await conn.fetch("SELECT user_id, reason FROM bans")

async def get_stats():
    if not db_pool: return 0, 0, 0, 0
    async with db_pool.acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM users")
        chatting = await conn.fetchval("SELECT COUNT(*) FROM users WHERE status = 'chatting'")
        queue = await conn.fetchval("SELECT COUNT(*) FROM queue")
        banned = await conn.fetchval("SELECT COUNT(*) FROM users WHERE banned = TRUE")
        return total, chatting, queue, banned

async def clear_active_chats():
    if db_pool:
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM pairs")
            await conn.execute("DELETE FROM queue")
            await conn.execute("UPDATE users SET status = 'idle'")

# ---------- Хэндлеры ----------
user_reporting = {}

@dp.message_handler(commands=['start'])
async def start(msg: types.Message):
    uid = msg.from_user.id
    await ensure_user(uid)
    await break_pair(uid)
    await remove_from_queue(uid)
    await set_status(uid, 'idle')
    await msg.answer("Привет! Анонимный чат.\nНажми «Найти собеседника».", reply_markup=main_menu)

@dp.message_handler(lambda m: m.text == "Помощь")
async def help_cmd(msg: types.Message):
    await msg.answer("• Найти — начать\n• Стоп — выйти\n• Пожаловаться — если плохо", reply_markup=main_menu)

@dp.message_handler(lambda m: m.text == "Найти собеседника")
async def search(msg: types.Message):
    uid = msg.from_user.id
    if await is_banned(uid):
        return await msg.answer("Вы забанены.", reply_markup=main_menu)

    if await get_partner(uid):
        return await msg.answer("Ты уже в чате.", reply_markup=chat_menu)

    await add_to_queue(uid)
    await set_status(uid, 'waiting')
    await msg.answer("Ищем собеседника... (до 30 сек)", reply_markup=waiting_menu)

    for _ in range(30):  # 30 секунд
        await asyncio.sleep(1)
        partner = await find_partner(uid)
        if partner:
            await create_pair(uid, partner)
            await bot.send_message(uid, "Собеседник найден! Пиши.", reply_markup=chat_menu)
            await bot.send_message(partner, "Собеседник найден! Пиши.", reply_markup=chat_menu)
            return
    await remove_from_queue(uid)
    await set_status(uid, 'idle')
    await msg.answer("Не нашли. Попробуй позже.", reply_markup=main_menu)

@dp.message_handler(lambda m: m.text == "Отмена")
async def cancel(msg: types.Message):
    await remove_from_queue(msg.from_user.id)
    await set_status(msg.from_user.id, 'idle')
    await msg.answer("Поиск отменён.", reply_markup=main_menu)

@dp.message_handler(lambda m: m.text in ["Стоп", "Следующий"])
async def stop(msg: types.Message):
    uid = msg.from_user.id
    partner = await break_pair(uid)
    if partner:
        await bot.send_message(partner, "Собеседник вышел.", reply_markup=main_menu)
        await msg.answer("Ты вышел.", reply_markup=main_menu)
    else:
        await msg.answer("Ты не в чате.", reply_markup=main_menu)
    if msg.text == "Следующий":
        await search(msg)

@dp.message_handler(lambda m: m.text == "Пожаловаться")
async def report_start(msg: types.Message):
    uid = msg.from_user.id
    partner = await get_partner(uid)
    if partner:
        user_reporting[uid] = partner
        await msg.answer("Опиши проблему:", reply_markup=ReplyKeyboardRemove())
    else:
        await msg.answer("Ты не в чате.", reply_markup=main_menu)

@dp.message_handler(lambda m: m.from_user.id in user_reporting)
async def report_reason(msg: types.Message):
    uid = msg.from_user.id
    partner = user_reporting.pop(uid)
    reason = msg.text or "Без причины"
    await save_report(uid, partner, reason)
    await break_pair(uid)
    await bot.send_message(uid, "Жалоба отправлена.", reply_markup=main_menu)
    await bot.send_message(partner, "Чат завершён.", reply_markup=main_menu)
    if MODERATOR_ID:
        await bot.send_message(MODERATOR_ID, f"НОВАЯ ЖАЛОБА!\nОт: {uid}\nНа: {partner}\nПричина: {reason}\n/mod")

# ---------- Пересылка ----------
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
    except:
        await break_pair(msg.from_user.id)
        await msg.answer("Чат прерван.", reply_markup=main_menu)

# ---------- Мод-панель ----------
@dp.message_handler(commands=['mod'])
async def mod_entry(msg: types.Message):
    if msg.from_user.id != MODERATOR_ID:
        return await msg.answer("Нет доступа.")
    await msg.answer("Мод-панель:", reply_markup=mod_menu)

@dp.message_handler(lambda m: m.text == "Жалобы")
async def show_reports(msg: types.Message):
    if msg.from_user.id != MODERATOR_ID: return
    reports = await get_unresolved_reports()
    if not reports:
        return await msg.answer("Нет жалоб.", reply_markup=mod_menu)
    for r in reports:
        kb = InlineKeyboardMarkup()
        kb.add(
            InlineKeyboardButton("Удалить чат", callback_data=f"del_{r['from_user']}_{r['to_user']}"),
            InlineKeyboardButton("Забанить", callback_data=f"ban_{r['to_user']}")
        )
        kb.add(InlineKeyboardButton("Игнор", callback_data=f"ign_{r['id']}"))
        await msg.answer(
            f"<b>Жалоба #{r['id']}</b>\n"
            f"От: <a href='tg://user?id={r['from_user']}'>{r['from_user']}</a>\n"
            f"На: <a href='tg://user?id={r['to_user']}'>{r['to_user']}</a>\n"
            f"Причина: {r['reason']}",
            reply_markup=kb
        )

@dp.message_handler(lambda m: m.text == "Статистика")
async def stats(msg: types.Message):
    if msg.from_user.id != MODERATOR_ID: return
    t, c, q, b = await get_stats()
    await msg.answer(f"<b>Статистика</b>\nПользователей: {t}\nВ чате: {c}\nВ поиске: {q}\nЗабанено: {b}", reply_markup=mod_menu)

@dp.message_handler(lambda m: m.text == "Баны")
async def bans(msg: types.Message):
    if msg.from_user.id != MODERATOR_ID: return
    users = await get_banned_users()
    if not users:
        return await msg.answer("Нет банов.", reply_markup=mod_menu)
    kb = InlineKeyboardMarkup()
    for u in users:
        kb.add(InlineKeyboardButton(f"Разбанить {u['user_id']}", callback_data=f"unban_{u['user_id']}"))
    await msg.answer("Забаненные:", reply_markup=kb)

@dp.message_handler(lambda m: m.text == "Выход")
async def mod_exit(msg: types.Message):
    if msg.from_user.id != MODERATOR_ID: return
    await msg.answer("Выход из мод-панели.", reply_markup=ReplyKeyboardRemove())

@dp.callback_query_handler(lambda c: c.data and c.data.startswith(("del_", "ban_", "ign_", "unban_")))
async def mod_cb(call: types.CallbackQuery):
    if call.from_user.id != MODERATOR_ID:
        return await call.answer("Нет доступа", show_alert=True)

    d = call.data
    try:
        if d.startswith("del_"):
            _, a, b = d.split("_")
            a, b = int(a), int(b)
            await break_pair(a); await break_pair(b)
            await bot.send_message(a, "Чат удалён модератором.")
            await bot.send_message(b, "Чат удалён модератором.")
            await call.answer("Чат удалён")
        elif d.startswith("ban_"):
            _, uid = d.split("_")
            await ban_user(int(uid))
            await break_pair(int(uid))
            await bot.send_message(int(uid), "Вы забанены.")
            await call.answer("Забанен")
        elif d.startswith("ign_"):
            _, rid = d.split("_")
            await mark_report_resolved(int(rid))
            await call.answer("Игнор")
        elif d.startswith("unban_"):
            _, uid = d.split("_")
            await unban_user(int(uid))
            await bot.send_message(int(uid), "Вы разбанены.")
            await call.answer("Разбанен")
    except Exception as e:
        await call.answer("Ошибка")

# ---------- Запуск ----------
async def on_startup(_):
    await init_db()
    await clear_active_chats()
    log.info("Бот запущен")

async def on_shutdown(_):
    if db_pool: await db_pool.close()
    await bot.session.close()

if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup, on_shutdown=on_shutdown)
