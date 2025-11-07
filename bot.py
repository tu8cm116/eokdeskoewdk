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
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.contrib.fsm_storage.memory import MemoryStorage

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
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)
db_pool = None

# ---------- КАНАЛ (ПРИВАТНЫЙ) ----------
CHANNEL_INVITE_LINK = "https://t.me/+LLZuriSEQpk0ZDVk"
CHANNEL_ID = "LLZuriSEQpk0ZDVk"  # Извлечено из приватной ссылки (без https://t.me/+)

# Клавиатура подписки
subscribe_kb = InlineKeyboardMarkup()
subscribe_kb.add(InlineKeyboardButton("Подписаться на канал", url=CHANNEL_INVITE_LINK))
subscribe_kb.add(InlineKeyboardButton("Я подписался ✅", callback_data="check_sub"))

# ---------- Состояния ----------
class ReportState(StatesGroup):
    waiting_reason = State()

# ---------- Память ----------
memory_queue = deque()
memory_pairs = {}
memory_status = {}
memory_banned = set()
memory_reports = []
all_complaints = {}
user_codes = {}
user_reporting = {}
waiting_tasks = {}

# ---------- КЛАВИАТУРЫ ----------
main_menu = ReplyKeyboardMarkup(resize_keyboard=True)
main_menu.add(KeyboardButton("🔍 Найти собеседника"), KeyboardButton("ℹ️ Инфо"))
main_menu.add(KeyboardButton("🔑 Мой код"))

chat_menu = ReplyKeyboardMarkup(resize_keyboard=True)
chat_menu.add(KeyboardButton("⛔️ Стоп"), KeyboardButton("➡️ Следующий"))
chat_menu.add(KeyboardButton("🚩 Пожаловаться"))

waiting_menu = ReplyKeyboardMarkup(resize_keyboard=True)
waiting_menu.add(KeyboardButton("❌ Отмена"))

report_cancel_menu = ReplyKeyboardMarkup(resize_keyboard=True)
report_cancel_menu.add(KeyboardButton("❌ Отменить жалобу"))

mod_menu = ReplyKeyboardMarkup(resize_keyboard=True)
mod_menu.add(KeyboardButton("📋 Жалобы"), KeyboardButton("📊 Статистика"))
mod_menu.add(KeyboardButton("🔨 Баны"), KeyboardButton("🚪 Выйти"))

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

# ---------- ПОДПИСКА ----------
async def check_subscription(uid: int) -> bool:
    if uid == MODERATOR_ID:
        log.info(f"Модератор {uid} обходит проверку подписки")
        return True
    try:
        # Используем приватный chat_id
        member = await bot.get_chat_member(chat_id=f"-100{CHANNEL_ID}", user_id=uid)
        log.info(f"[ПОДПИСКА] {uid} → статус: {member.status}")
        return member.status in ["member", "administrator", "creator"]
    except Exception as e:
        log.error(f"[ПОДПИСКА] Ошибка для {uid}: {e} (тип: {type(e).__name__})")
        return False

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
                    status VARCHAR(20) DEFAULT 'idle',
                    banned BOOLEAN DEFAULT FALSE,
                    code TEXT
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
    return None

async def is_banned(uid):
    if db_pool:
        async with db_pool.acquire() as conn:
            return await conn.fetchval("SELECT banned FROM users WHERE user_id = $1", uid) or False
    else:
        return uid in memory_banned

# ---------- БЛОКИРОВКА ----------
async def ban_user_complete(uid):
    memory_banned.add(uid)
    if uid in waiting_tasks:
        waiting_tasks[uid].cancel()
        del waiting_tasks[uid]
    await remove_from_queue(uid)
    partner = await break_pair(uid)
    if partner:
        await bot.send_message(partner, "Собеседник завершил чат.", reply_markup=main_menu)
    await bot.send_message(uid, "🚫 Вы были заблокированы модерацией. Получите инструкции по обжалованию по кнопке «Инфо»", reply_markup=main_menu)
    if db_pool:
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE users SET banned = TRUE WHERE user_id = $1", uid)
    log.info(f"Пользователь {uid} забанен")

async def increment_complaints(uid):
    all_complaints[uid] = all_complaints.get(uid, 0) + 1
    count = all_complaints[uid]
    if count >= 5 and not await is_banned(uid):
        await ban_user_auto(uid)
    return count

async def ban_user_auto(uid):
    memory_banned.add(uid)
    if uid in waiting_tasks:
        waiting_tasks[uid].cancel()
        del waiting_tasks[uid]
    await remove_from_queue(uid)
    partner = await break_pair(uid)
    if partner:
        await bot.send_message(partner, "Собеседник завершил чат.", reply_markup=main_menu)
    await bot.send_message(uid, "😔 Вы забанены за многочисленные жалобы. Получите инструкции по обжалованию по кнопке «Инфо»", reply_markup=main_menu)
    if db_pool:
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE users SET banned = TRUE WHERE user_id = $1", uid)
    if MODERATOR_ID:
        code = await get_user_code(uid) or "—"
        await bot.send_message(MODERATOR_ID, f"🚫 АВТОБАН: <code>{uid}</code> (<code>{code}</code>) — 5+ жалоб")
    log.info(f"Автобан {uid}")

async def clear_complaints(uid):
    all_complaints.pop(uid, None)
    global memory_reports
    memory_reports = [r for r in memory_reports if r['to'] != uid]

# ---------- ПОИСК ----------
async def search_for_user(uid):
    if await is_banned(uid):
        await bot.send_message(uid, "🚫 Вы были заблокированы. Попробуйте снова после снятия блокировки.", reply_markup=main_menu)
        return
    if await get_partner(uid):
        await bot.send_message(uid, "Ты уже в чате.", reply_markup=chat_menu)
        return

    if not await check_subscription(uid):
        await bot.send_message(uid, "❌ Подпишись на канал, чтобы искать собеседника:", reply_markup=subscribe_kb)
        return

    await add_to_queue(uid)
    await bot.send_message(uid, "🔍 Ищем собеседника...", reply_markup=waiting_menu)
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
                await bot.send_message(uid, "✅ Собеседник найден! Соблюдайте правила.", reply_markup=chat_menu)
                await bot.send_message(partner, "✅ Собеседник найден! Соблюдайте правила.", reply_markup=chat_menu)
                if uid in waiting_tasks:
                    del waiting_tasks[uid]
                return
        await remove_from_queue(uid)
        if uid in waiting_tasks:
            del waiting_tasks[uid]
        await bot.send_message(uid, "К сожалению, нет свободных пользователей. Попробуй позже.", reply_markup=main_menu)
    except asyncio.CancelledError:
        pass

# ---------- ХЭНДЛЕРЫ ----------
@dp.message_handler(commands=['start'])
async def start(msg: types.Message):
    uid = msg.from_user.id
    await ensure_user(uid)
    await break_pair(uid)
    await remove_from_queue(uid)
    memory_status[uid] = 'idle'

    log.info(f"Пользователь {uid} запустил /start — проверка подписки...")

    if not await check_subscription(uid):
        await msg.answer(
            "❌ Для использования бота нужна подписка на официальный канал проекта.\n\n"
            "Подпишись и нажми кнопку ниже:",
            reply_markup=subscribe_kb
        )
        return

    await msg.answer(
        "🗡 Добро пожаловать в ARMOR.\n\n"
        "Анонимный чат для общения от проекта Racers. Прежде, чем приступать к общению ознакомьтесь с информацией, нажав на кнопку «Инфо».\n\n"
        "🎯 Выберите действие ниже:",
        reply_markup=main_menu
    )

@dp.callback_query_handler(lambda c: c.data == "check_sub")
async def check_sub_callback(call: types.CallbackQuery):
    uid = call.from_user.id
    log.info(f"Пользователь {uid} нажал 'Я подписался' — повторная проверка...")

    if await check_subscription(uid):
        await call.message.edit_text("✅ Подписка подтверждена! Теперь ты можешь пользоваться ботом.", reply_markup=None)
        await bot.send_message(
            uid,
            "🗡 Добро пожаловать в ARMOR.\n\n"
            "Анонимный чат для общения от проекта Racers. Прежде, чем приступать к общению ознакомьтесь с информацией, нажав на кнопку «Инфо».\n\n"
            "🎯 Выберите действие ниже:",
            reply_markup=main_menu
        )
    else:
        await call.answer("❌ Ты ещё не подписался! Подпишись по ссылке и попробуй снова.", show_alert=True)

@dp.message_handler(lambda m: m.text == "ℹ️ Инфо")
async def help_cmd(msg: types.Message):
    await msg.answer(
        "С правилами и инструкцией обжалования бана вы можете ознакомиться по данной ссылке:\n\n"
        "🔗 https://telegra.ph/ARMOR-11-05-11\n\n"
        "Проект закреплен за Racers",
        disable_web_page_preview=True,
        reply_markup=main_menu
    )

@dp.message_handler(lambda m: m.text == "🔑 Мой код")
async def my_code_button(msg: types.Message):
    uid = msg.from_user.id
    code = await get_or_create_code(uid)
    await msg.answer(f"🔑 Твой уникальный код: <code>{code}</code>", parse_mode="HTML", reply_markup=main_menu)

@dp.message_handler(lambda m: m.text == "🔍 Найти собеседника")
async def search_button(msg: types.Message):
    uid = msg.from_user.id
    if await get_partner(uid):
        return
    await search_for_user(uid)

@dp.message_handler(lambda m: m.text == "⛔️ Стоп")
async def stop_button(msg: types.Message):
    if not await get_partner(msg.from_user.id):
        return
    await stop_cmd(msg)

@dp.message_handler(lambda m: m.text == "➡️ Следующий")
async def next_button(msg: types.Message):
    if not await get_partner(msg.from_user.id):
        return
    await next_cmd(msg)

@dp.message_handler(lambda m: m.text == "🚩 Пожаловаться")
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
        await msg.answer("Опишите причину жалобы:", reply_markup=report_cancel_menu)
    else:
        await msg.answer("Ты не в чате.", reply_markup=main_menu)

@dp.message_handler(lambda m: m.text == "❌ Отменить жалобу")
async def cancel_report(msg: types.Message):
    uid = msg.from_user.id
    if uid not in user_reporting:
        return
    user_reporting.pop(uid, None)
    await msg.answer("Жалоба отменена. Продолжайте общение.", reply_markup=chat_menu)

@dp.message_handler(lambda m: m.from_user.id in user_reporting and m.text != "❌ Отменить жалобу")
async def report_reason(msg: types.Message):
    uid = msg.from_user.id
    partner = user_reporting.pop(uid, None)
    if not partner:
        return
    reason = msg.text or "Без причины"
    report_id = len(memory_reports) + 1
    from_code = await get_user_code(uid) or "—"
    to_code = await get_user_code(partner) or "—"
    memory_reports.append({"id": report_id, "from": uid, "to": partner, "reason": reason, "ignored": False})
    count = await increment_complaints(partner)
    await break_pair(uid)
    await msg.answer("Жалоба отправлена и будет рассмотрена модерацией.", reply_markup=main_menu)
    await bot.send_message(partner, "Диалог завершен из-за жалобы собеседника.", reply_markup=main_menu)
    if MODERATOR_ID:
        await bot.send_message(
            MODERATOR_ID,
            f"🚩 <b>ЖАЛОБА #{report_id}</b>\n"
            f"От: <code>{uid}</code> (<code>{from_code}</code>)\n"
            f"На: <code>{partner}</code> (<code>{to_code}</code>)\n"
            f"Причина: {reason}\n"
            f"Всего жалоб: {count}\n"
            f"/mod",
            parse_mode="HTML"
        )

@dp.message_handler(lambda m: m.text == "❌ Отмена", state=None)
async def cancel_search(msg: types.Message, state: FSMContext):
    uid = msg.from_user.id
    if uid in waiting_tasks:
        waiting_tasks[uid].cancel()
        del waiting_tasks[uid]
    await remove_from_queue(uid)
    await state.finish()
    await msg.answer("❌ Поиск отменён.", reply_markup=main_menu)

@dp.message_handler(commands=['search'])
async def search(msg: types.Message):
    await search_for_user(msg.from_user.id)

@dp.message_handler(commands=['cancel'])
async def cancel(msg: types.Message):
    uid = msg.from_user.id
    if uid in waiting_tasks:
        waiting_tasks[uid].cancel()
        del waiting_tasks[uid]
    await remove_from_queue(uid)
    await msg.answer("❌ Поиск отменён.", reply_markup=main_menu)

@dp.message_handler(commands=['stop'])
async def stop_cmd(msg: types.Message):
    uid = msg.from_user.id
    if uid in waiting_tasks:
        waiting_tasks[uid].cancel()
        del waiting_tasks[uid]
    partner = await break_pair(uid)
    if partner:
        await bot.send_message(partner, "Собеседник завершил чат.", reply_markup=main_menu)
    await msg.answer("Диалог завершен.", reply_markup=main_menu)

@dp.message_handler(commands=['next'])
async def next_cmd(msg: types.Message):
    uid = msg.from_user.id
    partner = await get_partner(uid)
    await stop_cmd(msg)
    if partner:
        await search_for_user(partner)
    await search_for_user(uid)

# --- МОДЕРАТОРСКИЕ ---
@dp.message_handler(commands=['mod'])
async def mod_entry(msg: types.Message):
    if msg.from_user.id != MODERATOR_ID:
        return await msg.answer("🚫 Доступ запрещён.")
    await msg.answer("🛠 Модераторская панель:", reply_markup=mod_menu)

@dp.message_handler(lambda m: m.text == "📋 Жалобы")
async def complaints_button(msg: types.Message):
    if msg.from_user.id != MODERATOR_ID:
        return
    await show_reports(msg)

@dp.message_handler(lambda m: m.text == "📊 Статистика")
async def stats_button(msg: types.Message):
    if msg.from_user.id != MODERATOR_ID:
        return
    await stats(msg)

@dp.message_handler(lambda m: m.text == "🔨 Баны")
async def bans_button(msg: types.Message):
    if msg.from_user.id != MODERATOR_ID:
        return
    await show_bans(msg)

@dp.message_handler(lambda m: m.text == "🚪 Выйти")
async def exit_button(msg: types.Message):
    if msg.from_user.id != MODERATOR_ID:
        return
    await msg.answer("✅ Выход в главное меню.", reply_markup=main_menu)

@dp.message_handler(commands=['complaints'])
async def show_reports(msg: types.Message):
    if msg.from_user.id != MODERATOR_ID:
        return
    active = [r for r in memory_reports if not r.get('ignored', False)]
    if not active:
        return await msg.answer("Нет жалоб.", reply_markup=mod_menu)
    for r in active:
        from_code = await get_user_code(r['from']) or "—"
        to_code = await get_user_code(r['to']) or "—"
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("🔨 Забанить", callback_data=f"ban_{r['to']}"))
        kb.add(InlineKeyboardButton("👁 Игнор", callback_data=f"ign_{r['id']}"))
        await msg.answer(
            f"🚩 <b>Жалоба #{r['id']}</b>\n"
            f"От: <code>{r['from']}</code> (<code>{from_code}</code>)\n"
            f"На: <code>{r['to']}</code> (<code>{to_code}</code>)\n"
            f"Причина: {r['reason']}",
            reply_markup=kb, parse_mode="HTML"
        )

@dp.message_handler(commands=['stats'])
async def stats(msg: types.Message):
    if msg.from_user.id != MODERATOR_ID:
        return
    total_users = len(memory_status)
    chatting = sum(1 for s in memory_status.values() if s == 'chatting')
    searching = sum(1 for s in memory_status.values() if s == 'searching')
    banned = len(memory_banned)
    total_complaints = sum(all_complaints.values())
    await msg.answer(
        f"📊 Статистика:\n"
        f"Пользователей: {total_users}\n"
        f"В чате: {chatting}\n"
        f"В поиске: {searching}\n"
        f"Забанено: {banned}\n"
        f"Жалоб всего: {total_complaints}",
        parse_mode="HTML", reply_markup=mod_menu
    )

@dp.message_handler(commands=['bans'])
async def show_bans(msg: types.Message):
    if msg.from_user.id != MODERATOR_ID:
        return
    if not memory_banned:
        return await msg.answer("Нет забаненных.", reply_markup=mod_menu)
    kb = InlineKeyboardMarkup()
    for uid in memory_banned:
        kb.add(InlineKeyboardButton(f"Разбанить {uid}", callback_data=f"unban_{uid}"))
    await msg.answer("🔨 Забаненные:", reply_markup=kb)

@dp.message_handler(commands=['user'])
async def user_info(msg: types.Message):
    if msg.from_user.id != MODERATOR_ID:
        return await msg.answer("🚫 Только для модератора.")
    text = msg.text.strip()
    if len(text.split()) < 2:
        return await msg.answer("ℹ️ Использование: /user <id или код>")
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
        return await msg.answer("❌ Пользователь не найден.")
    status = "в чате" if await get_partner(uid) else "не в чате"
    banned = "да" if await is_banned(uid) else "нет"
    code = await get_user_code(uid) or "Нет кода"
    total_complaints = all_complaints.get(uid, 0)
    user_reports = [r for r in memory_reports if r['to'] == uid]
    response = (
        f"👤 Пользователь\n"
        f"ID: <code>{uid}</code>\n"
        f"Код: <code>{code}</code>\n"
        f"Статус: {status}\n"
        f"Забанен: {banned}\n"
        f"Жалоб: {total_complaints}\n\n"
    )
    if user_reports:
        response += "<b>Жалобы:</b>\n"
        for r in user_reports:
            from_code = await get_user_code(r['from']) or "—"
            response += f"• От: <code>{r['from']}</code> (<code>{from_code}</code>)\n  Причина: {r['reason']}\n\n"
    else:
        response += "📭 Жалоб нет."
    await msg.answer(response, parse_mode="HTML")

@dp.message_handler(commands=['ban'])
async def ban_user(msg: types.Message):
    if msg.from_user.id != MODERATOR_ID:
        return
    text = msg.text.strip()
    if len(text.split()) < 2:
        return await msg.answer("ℹ️ Использование: /ban <id или код>")
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
        return await msg.answer("❌ Не найден.")
    await ban_user_complete(uid)
    await msg.answer("✅ Забанен.")

@dp.message_handler(commands=['unban'])
async def unban_user(msg: types.Message):
    if msg.from_user.id != MODERATOR_ID:
        return
    text = msg.text.strip()
    if len(text.split()) < 2:
        return await msg.answer("ℹ️ Использование: /unban <id или код>")
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
        return await msg.answer("❌ Не найден.")
    memory_banned.discard(uid)
    if db_pool:
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE users SET banned = FALSE WHERE user_id = $1", uid)
    await bot.send_message(uid, "🎉 Поздравляем, вы были разблокированы модерацией. Ваши жалобы обнулены. Впредь, соблюдайте правила. Приятного общения.", reply_markup=main_menu)
    await clear_complaints(uid)
    await msg.answer("✅ Разбанен. Жалобы обнулены.")

@dp.callback_query_handler(lambda c: c.data and c.data.startswith(("ban_", "ign_", "unban_")))
async def mod_cb(call: types.CallbackQuery):
    if call.from_user.id != MODERATOR_ID:
        return await call.answer("🚫 Нет доступа", show_alert=True)
    d = call.data
    try:
        if d.startswith("ban_"):
            uid = int(d.split("_")[1])
            await ban_user_complete(uid)
            await call.answer("🔨 Забанен")
        elif d.startswith("ign_"):
            rid = int(d.split("_")[1])
            for r in memory_reports:
                if r['id'] == rid:
                    r['ignored'] = True
                    break
            await call.answer("👁 Жалоба скрыта (осталась в статистике)")
        elif d.startswith("unban_"):
            uid = int(d.split("_")[1])
            memory_banned.discard(uid)
            if db_pool:
                async with db_pool.acquire() as conn:
                    await conn.execute("UPDATE users SET banned = FALSE WHERE user_id = $1", uid)
            await bot.send_message(uid, "🎉 Поздравляем, вы были разблокированы модерацией. Ваши жалобы обнулены. Впредь, соблюдайте правила. Приятного общения.", reply_markup=main_menu)
            await clear_complaints(uid)
            await call.answer("✅ Разбанен. Жалобы обнулены.")
    except Exception as e:
        log.error(f"Ошибка в мод-CB: {e}")
        await call.answer("❌ Ошибка")

@dp.message_handler(content_types=types.ContentTypes.ANY)
async def relay(msg: types.Message):
    if msg.from_user.id in user_reporting:
        return
    partner = await get_partner(msg.from_user.id)
    if not partner:
        return
    try:
        if msg.text:
            await bot.send_message(partner, msg.text)
        elif msg.photo:
            await bot.send_photo(partner, msg.photo[-1].file_id, caption=msg.caption)
        elif msg.sticker:
            await bot.send_sticker(partner, msg.sticker.file_id)
        elif msg.voice:
            await bot.send_voice(partner, msg.voice.file_id)
        elif msg.document:
            await bot.send_document(partner, msg.document.file_id)
        elif msg.video:
            await bot.send_video(partner, msg.video.file_id)
        else:
            await bot.send_message(partner, "Данный тип сообщения не поддерживается.")
    except Exception as e:
        log.error(f"Ошибка релея: {e}")
        await break_pair(msg.from_user.id)
        await msg.answer("❌ Ошибка. Чат прерван.", reply_markup=main_menu)

# ---------- ЗАПУСК ----------
async def on_startup(_):
    await init_db()
    await load_banned_users()
    await load_active_users()
    log.info("🚀 Бот запущен")

if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup)
