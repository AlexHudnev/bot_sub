import os
import logging
from datetime import datetime, timedelta
import random
from typing import Optional
import asyncio
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, Router, types
from aiogram.types import (
    Message, InlineKeyboardButton, LabeledPrice, PreCheckoutQuery, SuccessfulPayment
)
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.enums import ParseMode
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext

import aiosqlite
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === Конфигурация ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "2"))
WELCOME_VIDEO_FILE_ID = os.getenv("WELCOME_VIDEO_FILE_ID")
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "").split(","))) if os.getenv("ADMIN_IDS") else []

PROVIDER_TOKEN_YOOKASSA = os.getenv("PROVIDER_TOKEN_YOOKASSA")
PROVIDER_TOKEN_STRIPE = os.getenv("PROVIDER_TOKEN_STRIPE")
ITEMS_PER_PAGE = 20

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()

# === Состояния ===
class PaymentState(StatesGroup):
    waiting_for_payment_method = State()

class AdminAction(StatesGroup):
    waiting_for_user_id = State()
    waiting_for_days = State()
    waiting_for_user_id_for_extend = State()

# === БД ===
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
        await db.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_payment_charge_id TEXT UNIQUE,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                months INTEGER NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
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

# === Управление доступом ===
async def get_invite_link() -> str:
    try:
        invite = await bot.create_chat_invite_link(
            chat_id=CHANNEL_ID,
            member_limit=1,
            expire_date=int((datetime.utcnow() + timedelta(hours=24)).timestamp())
        )
        return invite.invite_link
    except Exception as e:
        logger.error(f"Не удалось создать invite link: {e}")
        return "https://web.telegram.org/k/#5411737851"

async def send_invite_button(user_id: int, text: str = "✅ Доступ активирован!"):
    try:
        invite_link = await get_invite_link()
        kb = InlineKeyboardBuilder()
        kb.button(text="🔗 Войти в закрытый канал", url=invite_link)
        kb.adjust(1)
        await bot.send_message(user_id, text, reply_markup=kb.as_markup())
    except Exception as e:
        logger.error(f"Не удалось отправить invite кнопку {user_id}: {e}")

async def activate_subscription(telegram_id: int, days: int):
    user = await get_user_by_telegram(telegram_id)
    if user:
        await add_subscription(user["id"], days)

async def remove_from_channel(telegram_id: int):
    try:
        await bot.ban_chat_member(chat_id=CHANNEL_ID, user_id=telegram_id)
        await asyncio.sleep(1)
        await bot.unban_chat_member(chat_id=CHANNEL_ID, user_id=telegram_id)
    except Exception as e:
        logger.error(f"Не удалось удалить {telegram_id}: {e}")

# === Обработчики ===
@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
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
                await message.answer_video(video=video_id, caption="👋 Привет! Добро пожаловать в онлайн-салон!")
            except Exception as e:
                logger.error(f"Ошибка видео: {e}")

    welcome_text = (
        "🌟 Онлайн-салон \"Умный парикмахер\" 🌟\n\n"
        "Цена: от 299 RUB / 3 USD\n"
        "Пробный период: 2 дня бесплатно\n\n"
        "---\n\n"
        "Что ты получаешь:\n\n"
        "• Полный доступ к группе\n"
        "• Онлайн-консультации от стилистов\n"
        "• Пробный период — 2 дня бесплатно\n\n"
        "---\n\n"
        "После пробного периода — ты сам решаешь, продолжать ли оплату."
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Получить 2 дня бесплатно", callback_data="trial")
    kb.button(text="💰 Выбрать подписку", callback_data="select_duration")
    kb.adjust(1)
    await message.answer(welcome_text, reply_markup=kb.as_markup())

@router.callback_query(lambda c: c.data == "start")
async def back_to_start(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await cmd_start(callback.message, state)
    await callback.answer()

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
    await send_invite_button(callback.from_user.id, f"✅ Пробный период на {TRIAL_DAYS} дня(ей) активирован!")

@router.callback_query(lambda c: c.data == "select_duration")
async def select_duration(callback: types.CallbackQuery, state: FSMContext):
    kb = InlineKeyboardBuilder()
    for months in [1, 3]:
        kb.button(text=f"{months} мес", callback_data=f"duration_{months}")
    kb.button(text="⬅️ Назад", callback_data="start")
    kb.adjust(1)
    await callback.message.edit_text("На какой срок вам нужна подписка?", reply_markup=kb.as_markup())
    await state.set_state(PaymentState.waiting_for_payment_method)

@router.callback_query(lambda c: c.data == "subscribe_disabled")
async def subscribe_disabled(callback: types.CallbackQuery, state: FSMContext):
    kb = InlineKeyboardBuilder()
    for months in [1, 3]:
        kb.button(text=f"{months} мес", callback_data=f"duration_{months}")
    kb.button(text="⬅️ Назад", callback_data="start")
    kb.adjust(1)
    await callback.message.edit_text("На какой срок вам нужна подписка?", reply_markup=kb.as_markup())
    await state.set_state(PaymentState.waiting_for_payment_method)

@router.callback_query(lambda c: c.data.startswith("duration_"))
async def choose_payment_method(callback: types.CallbackQuery, state: FSMContext):
    try:
        months = int(callback.data.split("_", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Ошибка выбора срока.", show_alert=True)
        return

    await state.update_data(months=months)

    kb = InlineKeyboardBuilder()
    kb.button(text="🇷🇺 ЮKassa (RUB)", callback_data="pay_yookassa")
    # kb.button(text="🌍 Stripe (USD)", callback_data="pay_stripe")
    kb.button(text="🛠 Поддержка", url="https://web.telegram.org/k/#5411737851")
    kb.button(text="⬅️ Назад", callback_data="select_duration")
    kb.adjust(1)
    await callback.message.edit_text("Выберите способ оплаты:", reply_markup=kb.as_markup())


def pluralize_months(n: int) -> str:
    if n % 100 in (11, 12, 13, 14):
        return "месяцев"
    last_digit = n % 10
    if last_digit == 1:
        return "месяц"
    elif last_digit in (2, 3, 4):
        return "месяца"
    else:
        return "месяцев"


@router.callback_query(lambda c: c.data in ["pay_yookassa", "pay_stripe"])
async def send_invoice_by_method(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    months = data.get("months")
    if not months:
        await callback.answer("Сначала выберите срок подписки.", show_alert=True)
        return

    # Цены по сроку
    rub_prices = {1: 299, 3: 799, 6: 1499, 12: 2499}
    usd_prices = {1: 3, 3: 8, 6: 15, 12: 25}

    provider_key = callback.data
    if provider_key == "pay_yookassa":
        price = rub_prices.get(months, 299)
        currency = "RUB"
        provider_token = PROVIDER_TOKEN_YOOKASSA
    else:  # pay_stripe
        price = usd_prices.get(months, 3)
        currency = "USD"
        provider_token = PROVIDER_TOKEN_STRIPE

    if not provider_token:
        await callback.answer("Платежи через этот способ временно недоступны.", show_alert=True)
        return

    title = f"Подписка на {months} мес"
    description = f"Доступ к закрытому каналу на {months} {pluralize_months(months)}"
    payload = f"sub_{callback.from_user.id}_{months}_{provider_key}"
    prices = [LabeledPrice(label=title, amount=price * 100)]

    await bot.send_invoice(
        chat_id=callback.message.chat.id,
        title=title,
        description=description,
        payload=payload,
        provider_token=provider_token,
        currency=currency,
        prices=prices,
        start_parameter=f"sub_{months}_{provider_key}",
        need_name=False,
        need_phone_number=False,
        need_email=False,
        need_shipping_address=False,
        is_flexible=False
    )
    await callback.answer()

# === Обработка платежей ===
@router.pre_checkout_query()
async def pre_checkout(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@router.message(lambda m: m.content_type == "successful_payment")
async def successful_payment_handler(message: Message):
    payment = message.successful_payment
    payload = payment.invoice_payload  # "sub_12345_3_pay_yookassa"
    parts = payload.split("_")
    if len(parts) < 4:
        logger.error(f"Некорректный payload: {payload}")
        return

    try:
        user_id = int(parts[1])
        months = int(parts[2])
    except (ValueError, IndexError):
        logger.error(f"Ошибка разбора payload: {payload}")
        return

    amount = payment.total_amount / 100
    charge_id = payment.telegram_payment_charge_id

    async with aiosqlite.connect("/db/bot.db") as db:
        await db.execute(
            """INSERT INTO payments 
               (telegram_payment_charge_id, user_id, amount, months, status) 
               VALUES (?, ?, ?, ?, 'paid')""",
            (charge_id, user_id, amount, months)
        )
        await db.commit()

    await activate_subscription(user_id, months * 30)
    await send_invite_button(user_id, "✅ Оплата прошла успешно! Доступ активирован.")

# === Админка ===
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
    await callback.message.edit_text("Введите ID пользователя:")
    await state.set_state(AdminAction.waiting_for_user_id)

@router.callback_query(lambda c: c.data == "admin_extend")
async def admin_extend_start(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        return
    await callback.message.edit_text("Введите ID пользователя:")
    await state.set_state(AdminAction.waiting_for_user_id_for_extend)

def get_pagination_keyboard(current_page: int, total_pages: int):
    builder = InlineKeyboardBuilder()
    buttons = []

    if total_pages > 1:
        if current_page > 1:
            buttons.append(types.InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin_list_page:{current_page - 1}"))
        if current_page < total_pages:
            buttons.append(types.InlineKeyboardButton(text="Вперёд ➡️", callback_data=f"admin_list_page:{current_page + 1}"))

    if buttons:
        builder.row(*buttons)

    builder.row(types.InlineKeyboardButton(text="⬅️ В админ-меню", callback_data="admin_menu"))
    return builder.as_markup()


async def format_subscriptions_page(subs, page: int, total_pages: int) -> str:
    text = f"<b>Все подписки</b> (стр. {page}/{total_pages})\n\n"
    if not subs:
        return text + "Нет данных."

    for tg_id, first, last, username, expires_at, status in subs:
        # Имя
        name_parts = [part for part in [first, last] if part]
        display_name = " ".join(name_parts) if name_parts else "Без имени"
        if username:
            display_name += f" (@{username})"

        # Дата
        date_str = expires_at.split("T")[0] if "T" in expires_at else expires_at

        # Статус
        status_display = {
            "active": "✅ активна",
            "expired": "❌ истекла",
        }.get(status, f"ℹ️ {status}")

        text += (
            f"• {display_name} [<code>{tg_id}</code>]\n"
            f"  до {date_str} — {status_display}\n\n"
        )
    return text

async def fetch_all_subscriptions():
    """Получает все подписки с данными пользователей из вашей БД."""
    async with aiosqlite.connect("/db/bot.db") as db:
        cursor = await db.execute("""
            SELECT 
                u.telegram_id,
                u.first_name,
                u.last_name,
                u.username,
                s.expires_at,
                s.status
            FROM subscriptions s
            JOIN users u ON s.user_id = u.id
            ORDER BY s.expires_at DESC
        """)
        return await cursor.fetchall()

@router.callback_query(lambda c: c.data == "admin_list")
async def admin_list_subs(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return

    all_subs = await fetch_all_subscriptions()
    total = len(all_subs)
    total_pages = max(1, (total + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)

    if total == 0:
        await callback.message.edit_text("Нет подписок в базе.", reply_markup=get_admin_menu())
        await callback.answer()
        return

    # Первая страница
    page_subs = all_subs[:ITEMS_PER_PAGE]
    text = await format_subscriptions_page(page_subs, page=1, total_pages=total_pages)
    # Обход кэширования Telegram
    text += f"\u200B{random.randint(1, 999999)}"

    await callback.message.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=get_pagination_keyboard(1, total_pages)
    )
    await callback.answer()

@router.callback_query(lambda c: c.data.startswith("admin_list_page:"))
async def admin_list_page_handler(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return

    try:
        page = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.answer("Ошибка: неверный номер страницы.", show_alert=True)
        return

    all_subs = await fetch_all_subscriptions()
    total = len(all_subs)
    total_pages = max(1, (total + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)

    if page < 1 or page > total_pages:
        await callback.answer("Такой страницы не существует.", show_alert=True)
        return

    start = (page - 1) * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE
    page_subs = all_subs[start:end]

    text = await format_subscriptions_page(page_subs, page, total_pages)
    text += f"\u200B{random.randint(1, 999999)}"  # уникальный суффикс

    await callback.message.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=get_pagination_keyboard(page, total_pages)
    )
    await callback.answer()

@router.message(AdminAction.waiting_for_user_id)
async def admin_add_user_id(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    try:
        user_id = int(message.text.strip())
        await send_invite_button(user_id, "✅ Вам выдан доступ к каналу!")
        await message.answer("Пользователь добавлен.", reply_markup=get_admin_menu())
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
            await send_invite_button(user_id, f"✅ Подписка продлена на {days} дней.")
            user_info = await get_user_full_info(user_id)
            name = user_info["display_name"] if user_info else f"ID {user_id}"
            await message.answer(f"✅ Подписка для {name} продлена.", reply_markup=get_admin_menu())
    except ValueError:
        await message.answer("❌ Введите число дней:")
        return
    await state.clear()


async def has_active_subscription_by_telegram(telegram_id: int) -> bool:
    """Проверяет наличие активной (не просроченной) подписки по telegram_id."""
    now_iso = datetime.utcnow().isoformat()
    async with aiosqlite.connect("/db/bot.db") as db:
        cursor = await db.execute("""
            SELECT 1
            FROM subscriptions s
            JOIN users u ON s.user_id = u.id
            WHERE u.telegram_id = ? AND s.expires_at > ? AND s.status = 'active'
            LIMIT 1
        """, (telegram_id, now_iso))
        return await cursor.fetchone() is not None

# === Фоновые задачи ===
async def check_subscriptions():
    now = datetime.utcnow()
    tomorrow = now + timedelta(days=1)

    subscribers = await get_active_subscribers()
    for telegram_id, _, _, _, expires_at in subscribers:
        expires = datetime.fromisoformat(expires_at)
        if expires < now:
            async with aiosqlite.connect("/db/bot.db") as db:
                await db.execute("""
                    UPDATE subscriptions SET status = 'expired'
                    WHERE user_id = (SELECT id FROM users WHERE telegram_id = ?)
                """, (telegram_id,))
                await db.commit()
            
            has_new_sub = await has_active_subscription_by_telegram(telegram_id)
            if has_new_sub:
                logger.info(f"Пропускаем удаление {telegram_id}: обнаружена новая активная подписка.")
                continue
            await remove_from_channel(telegram_id)
            try:
                await bot.send_message(telegram_id, "❌ Ваша подписка истекла.")
            except:
                pass

    async with aiosqlite.connect("/db/bot.db") as db:
        # Находим пользователей, у которых:
        # - есть хотя бы одна expired подписка,
        # - и НЕТ ни одной активной (статус 'active' И expires_at >= now)
        cursor = await db.execute("""
            SELECT DISTINCT u.telegram_id
            FROM subscriptions s
            JOIN users u ON s.user_id = u.id
            WHERE s.status = 'expired'
              AND NOT EXISTS (
                  SELECT 1 FROM subscriptions s2
                  JOIN users u2 ON s2.user_id = u2.id
                  WHERE u2.telegram_id = u.telegram_id
                    AND s2.status = 'active'
                    AND datetime(s2.expires_at) >= datetime(?)
              )
        """, (now.isoformat(),))
        expired_without_active = await cursor.fetchall()

        for (telegram_id,) in expired_without_active:
            # Повторная проверка через функцию (опционально, для надёжности)
            if not await has_active_subscription_by_telegram(telegram_id):
                logger.info(f"Дополнительное удаление из канала: {telegram_id} (нет активной подписки)")
                await remove_from_channel(telegram_id)
                try:
                    await bot.send_message(telegram_id, "❌ Ваша подписка истекла. Вы удалены из канала.")
                except Exception as e:
                    logger.warning(f"Не удалось отправить повторное уведомление {telegram_id}: {e}")


    async with aiosqlite.connect("/db/bot.db") as db:
        cursor = await db.execute("""
            SELECT u.telegram_id, u.first_name, u.last_name
            FROM subscriptions s
            JOIN users u ON s.user_id = u.id
            WHERE s.status = 'active'
              AND date(s.expires_at) = date(?)
        """, (tomorrow.isoformat(),))
        rows = await cursor.fetchall()

        for telegram_id, first, last in rows:
            name = (first or "") + (" " + last if last else "")
            name = name.strip() or "Добрый человек"
            try:
                kb = InlineKeyboardBuilder()
                kb.button(text="💰 Продлить подписку", callback_data="select_duration")
                await bot.send_message(
                    telegram_id,
                    f"🔔 Привет, {name}!\n\n"
                    f"Ваша подписка заканчивается завтра.\n"
                    f"Хотите продлить?",
                    reply_markup=kb.as_markup()
                )
            except Exception as e:
                logger.error(f"Напоминание не отправлено {telegram_id}: {e}")


async def send_one_time_expired_notifications():
    """Один раз при запуске отправить уведомление тем, у кого подписка просрочена и нет новой."""
    now_iso = datetime.utcnow().isoformat()
    async with aiosqlite.connect("/db/bot.db") as db:
        # Находим всех пользователей с просроченными подписками (status = 'active', но expires_at < now)
        cursor = await db.execute("""
            SELECT DISTINCT u.telegram_id
            FROM subscriptions s
            JOIN users u ON s.user_id = u.id
            WHERE s.status = 'active' AND s.expires_at < ?
        """, (now_iso,))
        expired_users = await cursor.fetchall()

        for (telegram_id,) in expired_users:
            # Проверяем, есть ли у этого пользователя какая-нибудь другая активная (не просроченная) подписка
            has_active = await has_active_subscription_by_telegram(telegram_id)
            if has_active:
                continue  # Пропускаем — есть новая подписка

            try:
                kb = InlineKeyboardBuilder()
                kb.button(text="💰 Продлить подписку", callback_data="select_duration")
                await bot.send_message(
                    telegram_id,
                    "❌ Ваша подписка истекла. Хотите продлить доступ?",
                    reply_markup=kb.as_markup()
                )
                logger.info(f"Отправлено однократное уведомление о просрочке пользователю {telegram_id}")
            except Exception as e:
                logger.warning(f"Не удалось отправить уведомление {telegram_id}: {e}")

# === Запуск ===
async def main():
    await init_db()
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_subscriptions, CronTrigger(hour=9, minute=0))
    scheduler.add_job(check_subscriptions, IntervalTrigger(hours=6))
    scheduler.start()
    dp.include_router(router)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())