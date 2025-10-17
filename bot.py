# bot.py
import os
import logging
from datetime import datetime, timedelta
from typing import Optional

from aiogram import Bot, Dispatcher, Router, types
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.enums import ParseMode

import aiosqlite
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
import asyncio
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "3"))
WELCOME_VIDEO_FILE_ID = os.getenv("WELCOME_VIDEO_FILE_ID")
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "").split(","))) if os.getenv("ADMIN_IDS") else []

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()

# ========================
# База данных
# ========================

async def init_db():
    async with aiosqlite.connect("bot.db") as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE,
                username TEXT,
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

async def create_or_get_user(telegram_id: int, username: str) -> bool:
    async with aiosqlite.connect("bot.db") as db:
        cursor = await db.execute(
            "INSERT OR IGNORE INTO users (telegram_id, username) VALUES (?, ?)",
            (telegram_id, username)
        )
        await db.commit()
        return cursor.rowcount > 0

async def set_trial_used(user_id: int):
    async with aiosqlite.connect("bot.db") as db:
        await db.execute("UPDATE users SET trial_used = 1 WHERE id = ?", (user_id,))
        await db.commit()

async def add_subscription(user_id: int, days: int):
    expires = datetime.utcnow() + timedelta(days=days)
    async with aiosqlite.connect("bot.db") as db:
        await db.execute(
            "INSERT INTO subscriptions (user_id, expires_at) VALUES (?, ?)",
            (user_id, expires.isoformat())
        )
        await db.commit()

async def get_user_by_telegram(telegram_id: int) -> Optional[dict]:
    async with aiosqlite.connect("bot.db") as db:
        cursor = await db.execute("SELECT id, trial_used FROM users WHERE telegram_id = ?", (telegram_id,))
        row = await cursor.fetchone()
        return {"id": row[0], "trial_used": bool(row[1])} if row else None

async def is_subscribed(telegram_id: int) -> bool:
    async with aiosqlite.connect("bot.db") as db:
        cursor = await db.execute("""
            SELECT s.expires_at FROM subscriptions s
            JOIN users u ON s.user_id = u.id
            WHERE u.telegram_id = ? AND s.status = 'active'
        """, (telegram_id,))
        row = await cursor.fetchone()
        if row:
            expires = datetime.fromisoformat(row[0])
            return expires > datetime.utcnow()
        return False

async def get_active_subscribers():
    async with aiosqlite.connect("bot.db") as db:
        cursor = await db.execute("""
            SELECT u.telegram_id, s.expires_at FROM subscriptions s
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
            await bot.send_message(telegram_id, f"Ваш доступ к каналу: {invite.invite_link}")
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
    is_new = await create_or_get_user(user.id, user.username or "")

    if is_new and WELCOME_VIDEO_FILE_ID:
        await message.answer_video(
            video=WELCOME_VIDEO_FILE_ID,
            caption="👋 Привет! Добро пожаловать в наше закрытое сообщество!"
        )

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Получить пробный период", callback_data="trial")
    kb.button(text="💰 Выбрать подписку", callback_data="subscribe_disabled")
    kb.adjust(1)

    await message.answer(
        "🔓 Доступ к эксклюзивному контенту\n\n"
        f"• Пробный период: {TRIAL_DAYS} дней бесплатно\n"
        "• Подписка: 1/3/6/12 месяцев (скоро)\n\n"
        "Нажмите кнопку ниже, чтобы начать:",
        reply_markup=kb.as_markup()
    )

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
    await add_to_channel(callback.from_user.id)

    await callback.message.edit_text(
        f"✅ Пробный период на {TRIAL_DAYS} дня(ей) активирован!\n"
        "Вы получили доступ к закрытому каналу."
    )

@router.callback_query(lambda c: c.data == "subscribe_disabled")
async def subscribe_disabled(callback: types.CallbackQuery):
    await callback.answer("Подписка скоро станет доступна!", show_alert=True)

# ========================
# Админка
# ========================

@router.message(Command("admin"))
async def admin_menu(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    await message.answer(
        "🛠 Админ-панель:\n"
        "/add_user <user_id> - добавить вручную\n"
        "/extend <user_id> <days> - продлить\n"
        "/list_subs - список активных"
    )

@router.message(Command("add_user"))
async def admin_add_user(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    try:
        _, user_id = message.text.split()
        user_id = int(user_id)
        await add_to_channel(user_id)
        await message.answer(f"✅ Пользователь {user_id} добавлен в канал.")
    except:
        await message.answer("Используйте: /add_user <user_id>")

@router.message(Command("extend"))
async def admin_extend(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    try:
        _, user_id, days = message.text.split()
        user_id = int(user_id)
        days = int(days)
        user = await get_user_by_telegram(user_id)
        if not user:
            await message.answer("Пользователь не найден.")
            return
        await add_subscription(user["id"], days)
        await add_to_channel(user_id)
        await message.answer(f"✅ Подписка продлена на {days} дней.")
    except:
        await message.answer("Используйте: /extend <user_id> <days>")

@router.message(Command("list_subs"))
async def admin_list(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    subs = await get_active_subscribers()
    if not subs:
        await message.answer("Нет активных подписок.")
        return
    text = "Активные подписки:\n"
    for tg_id, exp in subs:
        text += f"• {tg_id} до {exp.split('T')[0]}\n"
    await message.answer(text)

# ========================
# Фоновые задачи
# ========================

async def check_subscriptions():
    subscribers = await get_active_subscribers()
    now = datetime.utcnow()
    for telegram_id, expires_at in subscribers:
        expires = datetime.fromisoformat(expires_at)
        if expires < now:
            async with aiosqlite.connect("bot.db") as db:
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

# ========================
# Запуск
# ========================

async def main():
    await init_db()
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_subscriptions, IntervalTrigger(hours=6))
    scheduler.start()
    dp.include_router(router)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
