# bot.py
import os
import logging
from datetime import datetime, timedelta
from typing import Optional

from aiogram import Bot, Dispatcher, Router, types
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command, CommandObject
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.enums import ParseMode
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext

import aiosqlite
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
import asyncio
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Конфигурация
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "7"))
WELCOME_VIDEO_FILE_ID = os.getenv("WELCOME_VIDEO_FILE_ID")
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "").split(","))) if os.getenv("ADMIN_IDS") else []

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()

# ========================
# Состояния для админки
# ========================
class AdminAction(StatesGroup):
    waiting_for_user_id = State()
    waiting_for_days = State()
    waiting_for_user_id_for_extend = State()

# ========================
# База данных
# ========================
async def init_db():
    async with aiosqlite.connect("/db/bot.db") as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                trial_used BOOLEAN DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                expires_at TEXT,
                status TEXT DEFAULT 'active',
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        """)
        await db.commit()

async def create_or_get_user(telegram_id: int, username: str, first_name: str, last_name: str) -> bool:
    async with aiosqlite.connect("/db/bot.db") as db:
        cursor = await db.execute(
            """INSERT OR IGNORE INTO users 
               (telegram_id, username, first_name, last_name) 
               VALUES (?, ?, ?, ?)""",
            (telegram_id, username, first_name, last_name)
        )
        await db.commit()
        return cursor.rowcount > 0

async def set_trial_used(user_id: int):
    async with aiosqlite.connect("/db/bot.db") as db:
        await db.execute("UPDATE users SET trial_used = 1 WHERE id = ?", (user_id,))
        await db.commit()

async def add_subscription(user_id: int, days: int):
    expires = datetime.utcnow() + timedelta(days=days)
    async with aiosqlite.connect("/db/bot.db") as db:
        await db.execute(
            "INSERT INTO subscriptions (user_id, expires_at) VALUES (?, ?)",
            (user_id, expires.isoformat())
        )
        await db.commit()

async def get_user_by_telegram(telegram_id: int) -> Optional[dict]:
    async with aiosqlite.connect("/db/bot.db") as db:
        cursor = await db.execute("SELECT id, trial_used FROM users WHERE telegram_id = ?", (telegram_id,))
        row = await cursor.fetchone()
        return {"id": row[0], "trial_used": bool(row[1])} if row else None

async def get_user_full_info(telegram_id: int):
    async with aiosqlite.connect("/db/bot.db") as db:
        cursor = await db.execute(
            "SELECT first_name, last_name, username FROM users WHERE telegram_id = ?",
            (telegram_id,)
        )
        row = await cursor.fetchone()
        if row:
            first, last, username = row
            name_parts = [n for n in [first, last] if n]
            display_name = " ".join(name_parts) or "Без имени"
            if username:
                display_name += f" (@{username})"
            return {"display_name": display_name}
        return None

async def get_active_subscribers():
    async with aiosqlite.connect("/db/bot.db") as db:
        cursor = await db.execute("""
            SELECT u.telegram_id, u.first_name, u.last_name, u.username, s.expires_at 
            FROM subscriptions s
            JOIN users u ON s.user_id = u.id
            WHERE s.status = 'active'
        """)
        return await cursor.fetchall()

# ========================
# Управление каналом
# ========================
async def add_to_channel(telegram_id: int):
    try:
        await bot.add_chat_member(chat_id=CHANNEL_ID, user_id=telegram_id)
        logger.info(f"Добавлен в канал: {telegram_id}")
    except Exception as e:
        logger.error(f"Не удалось добавить {telegram_id}: {e}")
        try:
            invite = await bot.create_chat_invite_link(
                chat_id=CHANNEL_ID,
                member_limit=1,
                expire_date=int((datetime.utcnow() + timedelta(hours=1)).timestamp())
            )
            await bot.send_message(telegram_id, f"✨ Ваш доступ к каналу:\n{invite.invite_link}")
        except Exception as ex:
            logger.error(f"Не удалось создать invite link: {ex}")

async def remove_from_channel(telegram_id: int):
    try:
        await bot.ban_chat_member(chat_id=CHANNEL_ID, user_id=telegram_id)
        await asyncio.sleep(1)
        await bot.unban_chat_member(chat_id=CHANNEL_ID, user_id=telegram_id)
        logger.info(f"Удалён из канала: {telegram_id}")
    except Exception as e:
        logger.error(f"Не удалось удалить {telegram_id}: {e}")

# ========================
# Обработчики
# ========================
@router.message(Command("start"))
async def cmd_start(message: Message):
    user = message.from_user
    is_new = await create_or_get_user(
        user.id,
        user.username or "",
        user.first_name or "",
        user.last_name or ""
    )

    if is_new and WELCOME_VIDEO_FILE_ID:
        video_id = WELCOME_VIDEO_FILE_ID.strip()
        if video_id.startswith("BAAC"):
            try:
                await message.answer_video(
                    video=video_id,
                    caption="👋 Привет! Добро пожаловать в онлайн-салон!"
                )
            except Exception as e:
                logger.error(f"Ошибка отправки видео: {e}")

    welcome_text = (
        "🌟 Онлайн-салон \"Умный парикмахер\" 🌟\n\n"
        "Цена: 299 RUB\n"
        "Пробный период: 7 дней бесплатно\n\n"
        "---\n\n"
        "Что ты получаешь:\n\n"
        "• Полный доступ к группе:\n"
        "  Рабочие лайфхаки, знания по домашним окрашиваниям, укладкам и уходу.\n\n"
        "• Онлайн-консультации:\n"
        "  Ведущие стилисты и колористы всегда готовы помочь.\n\n"
        "• Пробный период:\n"
        "  У тебя есть возможность попробовать 7 дней бесплатно.\n\n"
        "---\n\n"
        "После пробного периода:\n"
        "Ты сам решаешь, продолжать ли оплачивать доступ или нет."
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Получить 7 дней бесплатно", callback_data="trial")
    kb.button(text="💰 Выбрать подписку", callback_data="subscribe_disabled")
    kb.adjust(1)

    await message.answer(welcome_text, reply_markup=kb.as_markup())

@router.callback_query(lambda c: c.data == "trial")
async def trial_handler(callback: types.CallbackQuery):
    user = await get_user_by_telegram(callback.from_user.id)
    if not user:
        await callback.answer("Ошибка. Попробуйте /start", show_alert=True)
        return

    if user["trial_used"]:
        await callback.answer("Вы уже использовали пробный период.", show_alert=True)
        return

    await set_trial_used(user["id"])
    await add_subscription(user["id"], TRIAL_DAYS)
    
    # Создаём персональную ссылку
    invite_link = await get_invite_link()

    # Большую кнопку делаем через InlineKeyboard
    kb = InlineKeyboardBuilder()
    kb.button(text="🔗 Войти в закрытый канал", url=invite_link)
    kb.adjust(1)

    await callback.message.edit_text(
        f"✅ Пробный период на {TRIAL_DAYS} дня(ей) активирован!\n\n"
        f"Нажмите кнопку ниже, чтобы присоединиться:",
        reply_markup=kb.as_markup()
    )

async def get_invite_link() -> str:
    """Создаёт одноразовую invite-ссылку на канал"""
    try:
        invite = await bot.create_chat_invite_link(
            chat_id=CHANNEL_ID,
            member_limit=1,  # только 1 использование
            expire_date=int((datetime.utcnow() + timedelta(hours=24)).timestamp())
        )
        return invite.invite_link
    except Exception as e:
        logger.error(f"Не удалось создать invite link: {e}")
        return "https://t.me"

@router.callback_query(lambda c: c.data == "subscribe_disabled")
async def subscribe_disabled(callback: types.CallbackQuery):
    await callback.answer("Подписка скоро станет доступна!", show_alert=True)

# ========================
# Админка с кнопками
# ========================
def get_admin_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить пользователя", callback_data="admin_add")
    kb.button(text="⏳ Продлить подписку", callback_data="admin_extend")
    kb.button(text="📋 Список подписчиков", callback_data="admin_list")
    kb.adjust(1)
    return kb.as_markup()

@router.message(Command("admin"))
async def admin_menu(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    await message.answer("🛠 Выберите действие:", reply_markup=get_admin_menu())

@router.callback_query(lambda c: c.data == "admin_add")
async def admin_add_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        return
    await callback.message.edit_text("Введите ID пользователя (только цифры):")
    await state.set_state(AdminAction.waiting_for_user_id)

@router.callback_query(lambda c: c.data == "admin_extend")
async def admin_extend_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        return
    await callback.message.edit_text("Введите ID пользователя:")
    await state.set_state(AdminAction.waiting_for_user_id_for_extend)

@router.callback_query(lambda c: c.data == "admin_list")
async def admin_list_subs(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return
    subs = await get_active_subscribers()
    if not subs:
        text = "Нет активных подписок."
    else:
        text = "<b>Активные подписчики:</b>\n\n"
        for tg_id, first, last, username, exp in subs:
            name_parts = [part for part in [first, last] if part]
            display_name = " ".join(name_parts) if name_parts else "Без имени"
            if username:
                display_name += f" (@{username})"
            text += f"• {display_name} [<code>{tg_id}</code>] до {exp.split('T')[0]}\n"
    await callback.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=get_admin_menu())

@router.message(AdminAction.waiting_for_user_id)
async def admin_add_user_id(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    try:
        user_id = int(message.text.strip())
        await add_to_channel(user_id)
        await message.answer(f"✅ Пользователь добавлен в канал.", reply_markup=get_admin_menu())
    except ValueError:
        await message.answer("❌ Неверный ID. Введите только цифры:", reply_markup=get_admin_menu())
    await state.clear()

@router.message(AdminAction.waiting_for_user_id_for_extend)
async def admin_extend_user_id(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    try:
        user_id = int(message.text.strip())
        await state.update_data(target_user_id=user_id)
        await message.answer("На сколько дней продлить?")
        await state.set_state(AdminAction.waiting_for_days)
    except ValueError:
        await message.answer("❌ Неверный ID. Введите только цифры:")
        return

@router.message(AdminAction.waiting_for_days)
async def admin_extend_days(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    try:
        days = int(message.text.strip())
        data = await state.get_data()
        user_id = data["target_user_id"]

        user = await get_user_by_telegram(user_id)
        if not user:
            await message.answer("❌ Пользователь не найден.", reply_markup=get_admin_menu())
        else:
            await add_subscription(user["id"], days)
            await add_to_channel(user_id)
            user_info = await get_user_full_info(user_id)
            name = user_info["display_name"] if user_info else f"ID {user_id}"
            await message.answer(f"✅ Подписка для {name} продлена на {days} дней.", reply_markup=get_admin_menu())
    except ValueError:
        await message.answer("❌ Введите число дней:")
        return
    await state.clear()

# ========================
# Фоновые задачи
# ========================
async def check_subscriptions():
    subscribers = await get_active_subscribers()
    now = datetime.utcnow()
    tomorrow = now + timedelta(days=1)

    for telegram_id, _, _, _, expires_at in subscribers:
        expires = datetime.fromisoformat(expires_at)
        if expires < now:
            async with aiosqlite.connect("/db/bot.db") as db:
                await db.execute("""
                    UPDATE subscriptions SET status = 'expired'
                    WHERE user_id = (SELECT id FROM users WHERE telegram_id = ?)
                """, (telegram_id,))
                await db.commit()
            await remove_from_channel(telegram_id)
            try:
                await bot.send_message(telegram_id, "❌ Ваша подписка истекла.")
            except:
                pass

    async with aiosqlite.connect("/db/bot.db") as db:
        cursor = await db.execute("""
            SELECT u.telegram_id, u.first_name, u.last_name, u.username
            FROM subscriptions s
            JOIN users u ON s.user_id = u.id
            WHERE s.status = 'active'
              AND date(s.expires_at) = date(?)
        """, (tomorrow.isoformat(),))
        rows = await cursor.fetchall()

        for telegram_id, first, last, username in rows:
            name_parts = [n for n in [first, last] if n]
            name = " ".join(name_parts) or "Добрый человек"
            try:
                await bot.send_message(
                    telegram_id,
                    f"🔔 Привет, {name}!\n\n"
                    f"Ваша подписка на «Умный парикмахер» заканчивается завтра.\n\n"
                    f"Хотите продлить доступ? Напишите нам или нажмите «Выбрать подписку» в меню.",
                    reply_markup=InlineKeyboardBuilder()
                    .button(text="💰 Продлить подписку", callback_data="subscribe_disabled")
                    .as_markup()
                )
            except Exception as e:
                logger.error(f"Не удалось отправить напоминание {telegram_id}: {e}")

# ========================
# Запуск
# ========================

async def main():
    await init_db()
    scheduler = AsyncIOScheduler()
    # Проверяем каждый день в 09:00 UTC (можно изменить)
    scheduler.add_job(check_subscriptions, "cron", hour=9, minute=0)
    # И дополнительно каждые 6 часов для удаления (на случай сбоев)
    scheduler.add_job(check_subscriptions, IntervalTrigger(hours=6))
    scheduler.start()
    dp.include_router(router)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())