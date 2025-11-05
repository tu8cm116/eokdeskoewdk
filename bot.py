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
memory_status = {}      # uid -> 'idle' | 'searching' | 'chatting'
memory_banned = set()
memory_reports = []     # [{id, from, to, reason, ignored, timestamp}]
all_complaints = {}     # uid -> счётчик жалоб
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

# ---------- СИНХРОНИЗАЦИЯ ----------
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
    memory_status[uid] = 'idle'

async def add_to_queue(uid):
    if db_pool:
        async with db_pool.acquire() as conn:
            await conn.execute("INSERT INTO queue(user_id) VALUES($1) ON CONFLICT DO NOTHING", uid)
    else:
        memory_queue.append((uid, asyncio.get_event_loop().time()))
    memory_status[uid] = 'searching'

async def remove_from_queue(uid):
    if db_pool:
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM queue WHERE user_id = $1", uid)
    else:
        global memory_queue
        memory_queue = deque([x for x in memory_queue if x[0] != uid])
    memory_status[uid] = 'idle'

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
                await conn.execute("INSERT INTO pairs(user_id, partner_id) VALUES($1,$2),($2,$1) ON CONFLICT DO NOTHING", a, b)
    else:
        memory_pairs[a] = b
        memory_pairs[b] = a
        await remove_from_queue(a)
        await remove_from_queue(b)
    memory_status[a] = memory_status[b] = 'chatting'
    log.info(f"Пара: {a} <-> {b}")

async def get_partner(uid):
    if db_pool:
        async with db_pool.acquire() as conn:
            return await conn.fetchval("SELECT partner_id FROM pairs WHERE user_id = $1", uid)
    else:
        return memory_pairs.get(uid)

# РАЗРЫВ ПАРЫ + АВТООТМЕНА ЖАЛОБЫ
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

        # АВТООТМЕНА ЖАЛОБЫ
        user_reporting.pop(uid, None)
        user_reporting.pop(partner, None)

    return partner

async def is_banned(uid):
    if db_pool:
        async with db_pool.acquire() as conn:
            return await conn.fetchval("SELECT banned FROM users WHERE user_id = $1", uid) or False
    else:
        return uid in memory_banned

# ---------- АВТОБАН ----------
async def increment_complaints(uid):
    all_complaints[uid] = all_complaints.get(uid, 0) + 1
    count = all_complaints[uid]
    if count >= 5 and not await is_banned(uid):
        await ban_user_auto(uid)
    return count

async def ban_user_auto(uid):
    memory_banned.add(uid)
    if db_pool:
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE users SET banned = TRUE WHERE user_id = $1", uid)
    await break_pair(uid)
    await bot.send_message(uid, "Вы забанены автоматически (5+ жалоб).")
    if MODERATOR_ID:
        code = await get_user_code(uid) or "—"
        await bot.send_message(MODERATOR_ID, f"АВТОБАН: <code>{uid}</code> (<code>{code}</code>) — 5+ жалоб")

# ОБНУЛЕНИЕ ЖАЛОБ ПРИ РАЗБАНЕ
async def clear_complaints(uid):
    all_complaints.pop(uid, None)
    global memory_reports
    memory_reports = [r for r in memory_reports if r['to'] != uid]

# ---------- ХЭНДЛЕРЫ ----------
waiting_tasks = {}
user_reporting = {}

# --- ОСНОВНЫЕ ---
@dp.message_handler(commands=['start'])
async def start(msg: types.Message):
    uid = msg.from_user.id
    await ensure_user(uid)
    await break_pair(uid)
    await remove_from_queue(uid)
    memory_status[uid] = 'idle'
    await msg.answer("Привет! Анонимный чат.", reply_markup=main_menu)

@dp.message_handler(commands=['mod'])
async def mod_entry(msg: types.Message):
    if msg.from_user.id != MODERATOR_ID:
        return await msg.answer("Доступ запрещён.")
    await msg.answer("Модераторская панель:", reply_markup=mod_menu)

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

# --- КНОПКИ ---
@dp.message_handler(lambda m: m.text == "Мой код")
async def my_code_button(msg: types.Message):
    uid = msg.from_user.id
    code = await get_or_create_code(uid)
    await msg.answer(f"Твой код: <code>{code}</code>", parse_mode="HTML", reply_markup=main_menu)

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

# --- Пожаловаться ---
@dp.message_handler(lambda m: m.text == "Пожаловаться")
async def report_button(msg: types.Message):
    if not await get_partner(msg.from_user.id):
        return
    await report(msg)

@dp.message_handler(commands=['report'])
async def report(msg: types.Message):
    uid = msg.from_user.id
    partner = await get_partner(uid)
    if partner:
        user_reporting[uid] = partner
        cancel_kb = ReplyKeyboardMarkup(resize_keyboard=True)
        cancel_kb.add(KeyboardButton("Отмена"))
        await msg.answer("Опиши проблему:", reply_markup=cancel_kb)
    else:
        await msg.answer("Ты не в чате.", reply_markup=main_menu)

# ОТМЕНА ЖАЛОБЫ — КНОПКА РАБОТАЕТ!
@dp.message_handler(lambda m: m.text == "Отмена" and m.from_user.id in user_reporting)
async def cancel_report(msg: types.Message):
    uid = msg.from_user.id
    user_reporting.pop(uid, None)
    await msg.answer("Жалоба отменена. Продолжайте общение.", reply_markup=chat_menu)

# Отправка жалобы
@dp.message_handler(lambda m: m.from_user.id in user_reporting and m.text != "Отмена")
async def report_reason(msg: types.Message):
    uid = msg.from_user.id
    partner = user_reporting.pop(uid)
    reason = msg.text or "Без причины"
    report_id = len(memory_reports) + 1
    timestamp = msg.date.strftime("%d.%m %H:%M")

    from_code = await get_user_code(uid) or "—"
    to_code = await get_user_code(partner) or "—"

    memory_reports.append({
        "id": report_id,
        "from": uid,
        "to": partner,
        "reason": reason,
        "ignored": False,
        "timestamp": timestamp
    })
    all_complaints[partner] = all_complaints.get(partner, 0) + 1

    await break_pair(uid)
    await msg.answer("Жалоба отправлена.", reply_markup=main_menu)
    await bot.send_message(partner, "Чат завершён из-за жалобы.", reply_markup=main_menu)

    count = all_complaints[partner]

    if MODERATOR_ID:
        await bot.send_message(
            MODERATOR_ID,
            f"<b>ЖАЛОБА #{report_id}</b>\n"
            f"От: <code>{uid}</code> (<code>{from_code}</code>)\n"
            f"На: <code>{partner}</code> (<code>{to_code}</code>)\n"
            f"Причина: {reason}\n"
            f"Время: {timestamp}\n"
            f"Всего жалоб: {count}\n"
            f"/mod",
            parse_mode="HTML"
        )

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

async def wait_for_partner(uid):
    try:
        for _ in range(30):
            await asyncio.sleep(1)
            if await get_partner(uid):
                if uid in waiting_tasks:
                    del waiting_tasks[uid]
                return
            partner = await find_partner(uid)
            if partner:
                await create_pair(uid, partner)
                await bot.send_message(uid, "Собеседник найден! Пиши.", reply_markup=chat_menu)
                await bot.send_message(partner, "Собеседник найден! Пиши.", reply_markup=chat_menu)
                if uid in waiting_tasks:
                    del waiting_tasks[uid]
                return
        await remove_from_queue(uid)
        if uid in waiting_tasks:
            del waiting_tasks[uid]
        await bot.send_message(uid, "Не нашли. Попробуй позже.", reply_markup=main_menu)
    except asyncio.CancelledError:
        pass

@dp.message_handler(commands=['cancel'])
async def cancel(msg: types.Message):
    uid = msg.from_user.id
    if uid in waiting_tasks:
        waiting_tasks[uid].cancel()
        del waiting_tasks[uid]
    await remove_from_queue(uid)
    await msg.answer("Поиск отменён.", reply_markup=main_menu)

@dp.message_handler(commands=['stop'])
async def stop_cmd(msg: types.Message):
    uid = msg.from_user.id
    if uid in waiting_tasks:
        waiting_tasks[uid].cancel()
        del waiting_tasks[uid]
    partner = await break_pair(uid)
    if partner:
        await bot.send_message(partner, "Собеседник вышел.", reply_markup=main_menu)
    await msg.answer("Ты вышел.", reply_markup=main_menu)

@dp.message_handler(commands=['next'])
async def next_cmd(msg: types.Message):
    await stop_cmd(msg)
    await search(msg)

# --- МОДЕРАТОРСКИЕ КНОПКИ ---
@dp.message_handler(lambda m: m.text == "Жалобы")
async def complaints_button(msg: types.Message):
    if msg.from_user.id != MODERATOR_ID: return
    await show_reports(msg)

@dp.message_handler(lambda m: m.text == "Статистика")
async def stats_button(msg: types.Message):
    if msg.from_user.id != MODERATOR_ID: return
    await stats(msg)

@dp.message_handler(lambda m: m.text == "Баны")
async def bans_button(msg: types.Message):
    if msg.from_user.id != MODERATOR_ID: return
    await show_bans(msg)

@dp.message_handler(lambda m: m.text == "Выход")
async def exit_button(msg: types.Message):
    if msg.from_user.id != MODERATOR_ID: return
    await msg.answer("Выход в главное меню.", reply_markup=main_menu)

# --- МОДЕРАТОРСКИЕ КОМАНДЫ ---
@dp.message_handler(commands=['complaints'])
async def show_reports(msg: types.Message):
    if msg.from_user.id != MODERATOR_ID: return
    active = [r for r in memory_reports if not r.get('ignored', False)]
    if not active:
        return await msg.answer("Нет активных жалоб.", reply_markup=mod_menu)
    for r in active:
        from_code = await get_user_code(r['from']) or "—"
        to_code = await get_user_code(r['to']) or "—"
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("Забанить", callback_data=f"ban_{r['to']}"))
        kb.add(InlineKeyboardButton("Игнор", callback_data=f"ign_{r['id']}"))
        await msg.answer(
            f"<b>Жалоба #{r['id']}</b>\n"
            f"От: <code>{r['from']}</code> (<code>{from_code}</code>)\n"
            f"На: <code>{r['to']}</code> (<code>{to_code}</code>)\n"
            f"Причина: {r['reason']}\n"
            f"Время: {r.get('timestamp', '—')}",
            reply_markup=kb,
            parse_mode="HTML"
        )

@dp.message_handler(commands=['stats'])
async def stats(msg: types.Message):
    if msg.from_user.id != MODERATOR_ID: return
    total_users = len(memory_status)
    chatting = sum(1 for s in memory_status.values() if s == 'chatting')
    searching = sum(1 for s in memory_status.values() if s == 'searching')
    banned = len(memory_banned)
    total_complaints = sum(all_complaints.values())
    users_with_complaints = len(all_complaints)

    await msg.answer(
        f"<b>Статистика</b>\n"
        f"Пользователей: {total_users}\n"
        f"В чате: {chatting}\n"
        f"В поиске: {searching}\n"
        f"Забанено: {banned}\n"
        f"Всего жалоб: {total_complaints}\n"
        f"Пожаловались на: {users_with_complaints} чел.",
        parse_mode="HTML",
        reply_markup=mod_menu
    )

@dp.message_handler(commands=['bans'])
async def show_bans(msg: types.Message):
    if msg.from_user.id != MODERATOR_ID: return
    if not memory_banned:
        return await msg.answer("Нет забаненных.", reply_markup=mod_menu)
    kb = InlineKeyboardMarkup()
    for uid in memory_banned:
        kb.add(InlineKeyboardButton(f"Разбанить {uid}", callback_data=f"unban_{uid}"))
    await msg.answer("Забаненные:", reply_markup=kb)

# /user — ПОКАЗЫВАЕТ ВСЕ ЖАЛОБЫ С ПРИЧИНАМИ
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
    total_complaints = all_complaints.get(uid, 0)

    # Все жалобы на этого пользователя
    user_reports = [r for r in memory_reports if r['to'] == uid]

    response = (
        f"<b>Пользователь</b>\n"
        f"ID: <code>{uid}</code>\n"
        f"Код: <code>{code}</code>\n"
        f"Статус: {status}\n"
        f"Забанен: {banned}\n"
        f"Всего жалоб: {total_complaints}\n\n"
    )

    if user_reports:
        response += "<b>Жалобы:</b>\n"
        for r in user_reports:
            from_code = await get_user_code(r['from']) or "—"
            response += (
                f"• От: <code>{r['from']}</code> (<code>{from_code}</code>)\n"
                f"  Причина: {r['reason']}\n"
                f"  Время: {r.get('timestamp', '—')}\n\n"
            )
    else:
        response += "Жалоб нет."

    await msg.answer(response, parse_mode="HTML")

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
    if db_pool:
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE users SET banned = TRUE WHERE user_id = $1", uid)
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
    if db_pool:
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE users SET banned = FALSE WHERE user_id = $1", uid)
    await bot.send_message(uid, "Вы разбанены.")
    await clear_complaints(uid)
    await msg.answer("Разбанен. Жалобы обнулены.")

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
            if db_pool:
                async with db_pool.acquire() as conn:
                    await conn.execute("UPDATE users SET banned = TRUE WHERE user_id = $1", uid)
            await break_pair(uid)
            await bot.send_message(uid, "Вы забанены.")
            await call.answer("Забанен")
        elif d.startswith("ign_"):
            rid = int(d.split("_")[1])
            for r in memory_reports:
                if r['id'] == rid:
                    r['ignored'] = True
                    break
            await call.answer("Жалоба скрыта (осталась в статистике)")
        elif d.startswith("unban_"):
            uid = int(d.split("_")[1])
            memory_banned.discard(uid)
            if db_pool:
                async with db_pool.acquire() as conn:
                    await conn.execute("UPDATE users SET banned = FALSE WHERE user_id = $1", uid)
            await bot.send_message(uid, "Вы разбанены.")
            await clear_complaints(uid)
            await call.answer("Разбанен. Жалобы обнулены.")
    except Exception as e:
        log.error(f"Ошибка: {e}")
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
        log.error(f"Ошибка: {e}")
        await break_pair(msg.from_user.id)
        await msg.answer("Ошибка. Чат прерван.", reply_markup=main_menu)

# ---------- ЗАПУСК ----------
async def on_startup(_):
    await init_db()
    await load_banned_users()
    await load_active_users()
    log.info("Бот запущен")

if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup)
