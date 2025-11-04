import os
import asyncpg
from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    InlineKeyboardMarkup, InlineKeyboardButton
)

# ---------- Конфигурация ----------
TOKEN = os.getenv("BOT_TOKEN")
PGHOST = os.getenv("PGHOST")
PGUSER = os.getenv("PGUSER")
PGPASSWORD = os.getenv("PGPASSWORD")
PGDATABASE = os.getenv("PGDATABASE")
PGPORT = int(os.getenv("PGPORT", "5432"))
MODERATOR_ID = int(os.getenv("MODERATOR_ID", "0"))

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

bot = Bot(token=TOKEN)
dp = Dispatcher(bot)
db_pool = None

# ---------- Клавиатуры ----------
main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton("Найти собеседника")],
        [KeyboardButton("Помощь")]
    ],
    resize_keyboard=True
)

chat_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton("Стоп"), KeyboardButton("Следующий")],
        [KeyboardButton("Пожаловаться")]
    ],
    resize_keyboard=True
)

waiting_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton("Отмена")]
    ],
    resize_keyboard=True
)

mod_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton("Жалобы"), KeyboardButton("Статистика")],
        [KeyboardButton("Баны"), KeyboardButton("Выход")]
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
                status VARCHAR(20) DEFAULT 'idle',
                banned BOOLEAN DEFAULT FALSE
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
            CREATE TABLE IF NOT EXISTS reports (
                id SERIAL PRIMARY KEY,
                from_user BIGINT,
                to_user BIGINT,
                reason TEXT,
                reported_at TIMESTAMP DEFAULT NOW(),
                resolved BOOLEAN DEFAULT FALSE
            );
            CREATE TABLE IF NOT EXISTS bans (
                user_id BIGINT PRIMARY KEY,
                banned_at TIMESTAMP DEFAULT NOW(),
                reason TEXT
            );
        """)

async def ensure_user(user_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO users(user_id, status, banned)
            VALUES($1, 'idle', FALSE)
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

async def clear_active_chats():
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM pairs")
        await conn.execute("DELETE FROM queue")
        await conn.execute("UPDATE users SET status='idle'")

# --- Жалобы ---
async def save_report(from_user: int, to_user: int, reason: str):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO reports(from_user, to_user, reason) VALUES($1, $2, $3)",
            from_user, to_user, reason
        )

async def get_unresolved_reports():
    async with db_pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM reports WHERE resolved = FALSE ORDER BY reported_at")

async def mark_report_resolved(report_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE reports SET resolved = TRUE WHERE id = $1", report_id)

# --- Баны ---
async def ban_user(user_id: int, reason: str = "Нарушение правил"):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO bans(user_id, reason) VALUES($1, $2) ON CONFLICT (user_id) DO UPDATE SET reason = $2",
            user_id, reason
        )
        await conn.execute("UPDATE users SET banned = TRUE WHERE user_id = $1", user_id)

async def unban_user(user_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM bans WHERE user_id = $1", user_id)
        await conn.execute("UPDATE users SET banned = FALSE WHERE user_id = $1", user_id)

async def is_banned(user_id: int) -> bool:
    async with db_pool.acquire() as conn:
        return await conn.fetchval("SELECT banned FROM users WHERE user_id = $1", user_id) or False

async def get_banned_users():
    async with db_pool.acquire() as conn:
        return await conn.fetch("SELECT user_id, reason FROM bans")

# --- Статистика ---
async def get_stats():
    async with db_pool.acquire() as conn:
        total_users = await conn.fetchval("SELECT COUNT(*) FROM users")
        in_chat = await conn.fetchval("SELECT COUNT(*) FROM users WHERE status = 'chatting'")
        in_queue = await conn.fetchval("SELECT COUNT(*) FROM queue")
        banned = await conn.fetchval("SELECT COUNT(*) FROM users WHERE banned = TRUE")
        return total_users, in_chat, in_queue, banned

# ---------- Хэндлеры ----------
@dp.message_handler(commands=['start'])
async def start(msg: types.Message):
    uid = msg.from_user.id
    await ensure_user(uid)

    # Завершаем старый чат
    partner = await break_pair(uid)
    if partner:
        await bot.send_message(partner, "Собеседник перезапустил чат.", reply_markup=main_menu)

    await remove_from_queue(uid)
    await set_status(uid, 'idle')

    await msg.answer(
        "Привет! Я анонимный чат-бот.\nВыбери действие:",
        reply_markup=main_menu
    )

@dp.message_handler(lambda m: m.text in ["Помощь", "/help"])
async def help_cmd(msg: types.Message):
    await msg.answer(
        "Напиши «Найти собеседника», чтобы начать поиск.\n"
        "Когда захочешь выйти — «Стоп».\n"
        "Можно отправлять текст, фото, стикеры, голосовые.",
        reply_markup=main_menu
    )

@dp.message_handler(lambda m: m.text in ["Найти собеседника", "/search"])
async def search(msg: types.Message):
    uid = msg.from_user.id
    await ensure_user(uid)

    if await is_banned(uid):
        return await msg.answer("Вы забанены и не можете использовать бота.", reply_markup=main_menu)

    partner_now = await get_partner(uid)
    if partner_now:
        await msg.answer("Ты уже общаешься. Нажми «Стоп», чтобы завершить.", reply_markup=chat_menu)
        return

    partner = await find_partner(uid)
    if partner:
        if await is_banned(partner):
            await remove_from_queue(partner)
            await break_pair(partner)
            await search(msg)
            return
        await create_pair(uid, partner)
        await bot.send_message(uid, "Собеседник найден! Можешь писать.", reply_markup=chat_menu)
        await bot.send_message(partner, "Собеседник найден! Можешь писать.", reply_markup=chat_menu)
    else:
        await add_to_queue(uid)
        await set_status(uid, 'waiting')
        await msg.answer("Ожидание собеседника...", reply_markup=waiting_menu)

@dp.message_handler(lambda m: m.text in ["Отмена"])
async def cancel_search(msg: types.Message):
    uid = msg.from_user.id
    await remove_from_queue(uid)
    await set_status(uid, 'idle')
    await msg.answer("Поиск отменён.", reply_markup=main_menu)

@dp.message_handler(lambda m: m.text in ["Стоп", "/stop"])
async def stop(msg: types.Message):
    uid = msg.from_user.id
    await remove_from_queue(uid)
    partner = await break_pair(uid)
    if partner:
        await bot.send_message(partner, "Собеседник покинул чат.", reply_markup=main_menu)
        await msg.answer("Ты покинул чат.", reply_markup=main_menu)
    else:
        await msg.answer("Ты не в чате.", reply_markup=main_menu)

@dp.message_handler(lambda m: m.text in ["Следующий"])
async def next_chat(msg: types.Message):
    await stop(msg)
    await search(msg)

# ---------- Жалобы ----------
user_reporting = {}

@dp.message_handler(lambda m: m.text in ["Пожаловаться"])
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
    reason = msg.text or "Причина не указана"

    await save_report(uid, partner, reason)

    if MODERATOR_ID:
        await bot.send_message(
            MODERATOR_ID,
            f"НОВАЯ ЖАЛОБА!\n"
            f"От: <a href='tg://user?id={uid}'>{uid}</a>\n"
            f"На: <a href='tg://user?id={partner}'>{partner}</a>\n"
            f"Причина: {reason}\n"
            f"/mod — открыть панель",
            parse_mode="HTML"
        )

    await break_pair(uid)
    await bot.send_message(uid, "Жалоба отправлена. Чат завершён.", reply_markup=main_menu)
    await bot.send_message(partner, "Собеседник завершил чат.", reply_markup=main_menu)

# ---------- Пересылка сообщений ----------
@dp.message_handler(content_types=types.ContentTypes.ANY)
async def relay(msg: types.Message):
    uid = msg.from_user.id
    partner = await get_partner(uid)
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
            await bot.send_voice(partner, msg.voice.file_id, caption=msg.caption)
        elif msg.audio:
            await bot.send_audio(partner, msg.audio.file_id, caption=msg.caption)
        elif msg.document:
            await bot.send_document(partner, msg.document.file_id, caption=msg.caption)
        elif msg.video:
            await bot.send_video(partner, msg.video.file_id, caption=msg.caption)
        elif msg.video_note:
            await bot.send_video_note(partner, msg.video_note.file_id)
        elif msg.animation:
            await bot.send_animation(partner, msg.animation.file_id, caption=msg.caption)
        else:
            await bot.send_message(partner, "Сообщение получено.")
    except Exception:
        await break_pair(uid)
        await bot.send_message(uid, "Ошибка отправки. Чат завершён.", reply_markup=main_menu)
        await bot.send_message(partner, "Собеседник отключился.", reply_markup=main_menu)

# ---------- Мод-панель ----------
@dp.message_handler(commands=['mod'])
async def mod_panel_entry(msg: types.Message):
    if msg.from_user.id != MODERATOR_ID:
        return await msg.answer("Доступ запрещён.")
    await msg.answer("Модераторская панель:", reply_markup=mod_menu)

@dp.message_handler(lambda m: m.text == "Жалобы")
async def show_reports(msg: types.Message):
    if msg.from_user.id != MODERATOR_ID:
        return
    reports = await get_unresolved_reports()
    if not reports:
        return await msg.answer("Нет активных жалоб.", reply_markup=mod_menu)

    for r in reports:
        kb = InlineKeyboardMarkup(row_width=2)
        kb.add(
            InlineKeyboardButton("Удалить чат", callback_data=f"mod_delchat_{r['from_user']}_{r['to_user']}"),
            InlineKeyboardButton("Забанить", callback_data=f"mod_ban_{r['to_user']}")
        )
        kb.add(InlineKeyboardButton("Игнорировать", callback_data=f"mod_ignore_{r['id']}"))

        await msg.answer(
            f"<b>Жалоба #{r['id']}</b>\n"
            f"От: <a href='tg://user?id={r['from_user']}'>{r['from_user']}</a>\n"
            f"На: <a href='tg://user?id={r['to_user']}'>{r['to_user']}</a>\n"
            f"Причина: {r['reason'] or '—'}",
            reply_markup=kb,
            parse_mode="HTML"
        )
    await msg.answer("Выберите действие.", reply_markup=mod_menu)

@dp.message_handler(lambda m: m.text == "Статистика")
async def show_stats(msg: types.Message):
    if msg.from_user.id != MODERATOR_ID:
        return
    total, in_chat, in_queue, banned = await get_stats()
    await msg.answer(
        f"<b>Статистика бота</b>\n\n"
        f"Всего пользователей: {total}\n"
        f"В чате: {in_chat}\n"
        f"В поиске: {in_queue}\n"
        f"Забанено: {banned}",
        parse_mode="HTML",
        reply_markup=mod_menu
    )

@dp.message_handler(lambda m: m.text == "Баны")
async def show_bans(msg: types.Message):
    if msg.from_user.id != MODERATOR_ID:
        return
    banned = await get_banned_users()
    if not banned:
        return await msg.answer("Нет забаненных пользователей.", reply_markup=mod_menu)

    text = "<b>Забаненные пользователи:</b>\n\n"
    kb = InlineKeyboardMarkup()
    for b in banned:
        text += f"• <a href='tg://user?id={b['user_id']}'>{b['user_id']}</a> — {b['reason']}\n"
        kb.add(InlineKeyboardButton(f"Разбанить {b['user_id']}", callback_data=f"mod_unban_{b['user_id']}"))
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")

@dp.message_handler(lambda m: m.text == "Выход")
async def mod_exit(msg: types.Message):
    if msg.from_user.id != MODERATOR_ID:
        return
    await msg.answer("Вы вышли из мод-панели.", reply_markup=ReplyKeyboardRemove())

# --- Inline-колбэки ---
@dp.callback_query_handler(lambda c: c.data and c.data.startswith("mod_"))
async def mod_callback(call: types.CallbackQuery):
    if call.from_user.id != MODERATOR_ID:
        return await call.answer("Нет доступа.", show_alert=True)

    data = call.data

    try:
        if data.startswith("mod_delchat_"):
            _, _, from_id, to_id = data.split("_")
            from_id, to_id = int(from_id), int(to_id)
            await break_pair(from_id)
            await break_pair(to_id)
            await bot.send_message(from_id, "Чат завершён модератором.")
            await bot.send_message(to_id, "Чат завершён модератором.")
            await call.answer("Чат удалён.")

        elif data.startswith("mod_ban_"):
            _, _, user_id = data.split("_")
            user_id = int(user_id)
            await ban_user(user_id, "Жалоба модератора")
            await break_pair(user_id)
            await bot.send_message(user_id, "Вы забанены за нарушение правил.")
            await call.answer("Пользователь забанен.")

        elif data.startswith("mod_ignore_"):
            _, _, report_id = data.split("_")
            await mark_report_resolved(int(report_id))
            await call.answer("Жалоба проигнорирована.")

        elif data.startswith("mod_unban_"):
            _, _, user_id = data.split("_")
            user_id = int(user_id)
            await unban_user(user_id)
            await bot.send_message(user_id, "Вы разбанены.")
            await call.answer("Пользователь разбанен.")
    except Exception as e:
        await call.answer("Ошибка обработки.", show_alert=True)

# ---------- Запуск ----------
async def on_startup(_):
    await init_db()
    await clear_active_chats()
    print("Бот запущен. Старые чаты очищены.")

async def on_shutdown(_):
    if db_pool:
        await db_pool.close()
    await bot.session.close()
    print("Бот остановлен.")

if __name__ == "__main__":
    executor.start_polling(
        dp,
        skip_updates=True,
        on_startup=on_startup,
        on_shutdown=on_shutdown
    )
