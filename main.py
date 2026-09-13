# -*- coding: utf-8 -*-
import asyncio
import io
import logging
import os
import urllib.parse
from collections import defaultdict
from contextlib import asynccontextmanager

import aiosqlite
import segno
from aiogram import BaseMiddleware, Bot, Dispatcher, F, types
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BufferedInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Template

# ==========================================
# CONFIG & AUTHENTICATION
# ==========================================
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "supersecret123")
AUTH_COOKIE_NAME = "session_token"
AUTH_SECRET = "admin_authenticated_session_key_99"
DB_NAME = "name_database.db"

INITIAL_BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ADMIN_IDS = [int(i.strip()) for i in os.getenv("ADMIN_IDS", "0").split(",") if i.strip().isdigit()]

logging.basicConfig(level=logging.INFO)


def require_admin(request: Request):
    token = request.cookies.get(AUTH_COOKIE_NAME)
    if token != AUTH_SECRET:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login"},
        )
    return True


# ==========================================
# DYNAMIC BOT CONTROLLER
# ==========================================
class BotManager:
    def __init__(self):
        self.bot: Bot | None = None
        self.dp: Dispatcher = Dispatcher(storage=MemoryStorage())
        self.polling_task: asyncio.Task | None = None
        self.session: AiohttpSession | None = None

    async def start(self, token: str):
        if not token or token == "YOUR_BOT_TOKEN_HERE":
            logging.warning("[BotManager] No valid token set. Bot is idle.")
            return

        self.session = AiohttpSession(timeout=60.0)
        self.bot = Bot(token=token, session=self.session)

        try:
            await self.bot.delete_webhook(drop_pending_updates=True)
        except Exception as e:
            logging.warning(f"[BotManager] Webhook notice: {e}")

        async def runner():
            try:
                logging.info("[BotManager] Starting polling...")
                await self.dp.start_polling(self.bot)
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logging.error(f"[BotManager] Polling runtime error: {e}")

        self.polling_task = asyncio.create_task(runner())

    async def stop(self):
        if self.polling_task and not self.polling_task.done():
            self.polling_task.cancel()
            try:
                await self.polling_task
            except asyncio.CancelledError:
                pass
            self.polling_task = None

        if self.dp:
            try:
                await self.dp.stop_polling()
            except Exception:
                pass

        if self.bot:
            try:
                if self.bot.session:
                    await self.bot.session.close()
            except Exception:
                pass
            self.bot = None

        logging.info("[BotManager] Bot stopped cleanly.")

    async def restart(self, new_token: str):
        await self.stop()
        await self.start(new_token)


manager = BotManager()

# ==========================================
# MESSAGE TRACKING SYSTEM
# ==========================================
user_messages = defaultdict(list)


def track(chat_id: int, message_id: int):
    if message_id not in user_messages[chat_id]:
        user_messages[chat_id].append(message_id)


async def delete_old_messages(chat_id: int, exclude_ids: list[int] | None = None):
    if not manager.bot:
        return
    exclude = set(exclude_ids or [])
    all_ids = [mid for mid in user_messages.get(chat_id, []) if mid not in exclude]
    user_messages[chat_id] = [mid for mid in user_messages.get(chat_id, []) if mid in exclude]

    if not all_ids:
        return

    for i in range(0, len(all_ids), 100):
        chunk = all_ids[i : i + 100]
        try:
            await manager.bot.delete_messages(chat_id=chat_id, message_ids=chunk)
        except TelegramBadRequest:
            for mid in chunk:
                try:
                    await manager.bot.delete_message(chat_id=chat_id, message_id=mid)
                except Exception:
                    pass
        except Exception as e:
            logging.debug(f"Failed to delete message chunk: {e}")


class MessageTrackerMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: types.TelegramObject, data: dict):
        if isinstance(event, types.Message):
            track(event.chat.id, event.message_id)
        return await handler(event, data)


# ==========================================
# DATABASE LAYER
# ==========================================
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                full_name TEXT,
                username TEXT,
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                premium_status TEXT DEFAULT 'Free',
                is_banned INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                plan_name TEXT,
                amount REAL,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS plans (
                plan_id TEXT PRIMARY KEY,
                name TEXT,
                amount REAL,
                validity TEXT
            )
        """)

        defaults = {
            "bot_token": INITIAL_BOT_TOKEN,
            "maintenance": "off",
            "upi_id": "paytm.s21dj6b@pty",
            "payee_name": "NAZIYA NASRIN",
            "welcome_photo": "https://images.unsplash.com/photo-1618005182384-a83a8bd57fbe?w=800",
            "welcome_text": (
                "👋 Welcome to Our Bot!\n\n"
                "✨ Explore features, view demos, check subscriptions, "
                "or manage your account using the buttons below."
            ),
            "plans_text": (
                "📦 Choose Your Membership Plan\n\n"
                "👉 Select any plan below to get an instant UPI QR payment card:"
            ),
            "demo_video": "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerBlazes.mp4",
        }
        for k, v in defaults.items():
            await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))

        default_plans = [
            ("plan_1", "INDIAN WEBSERIES", 99.0, "30 Days"),
            ("plan_2", "3 MONTHS SPECIAL", 249.0, "90 Days"),
            ("plan_3", "6 MONTHS VIP", 449.0, "180 Days"),
            ("plan_4", "1 YEAR ACCESS", 799.0, "365 Days"),
            ("plan_5", "LIFETIME PASS", 1299.0, "Lifetime"),
            ("plan_6", "4K ULTRA STREAM", 199.0, "30 Days"),
            ("plan_7", "PRO PASS", 349.0, "60 Days"),
            ("plan_8", "EXCLUSIVE HUB", 599.0, "90 Days"),
        ]
        for p in default_plans:
            await db.execute(
                "INSERT OR IGNORE INTO plans (plan_id, name, amount, validity) VALUES (?, ?, ?, ?)",
                p,
            )
        await db.commit()


async def get_setting(key: str) -> str:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else ""


async def update_setting(key: str, value: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        await db.commit()


async def get_all_plans():
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT plan_id, name, amount, validity FROM plans ORDER BY plan_id ASC") as cur:
            return await cur.fetchall()


async def get_plan(plan_id: str):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT plan_id, name, amount, validity FROM plans WHERE plan_id = ?", (plan_id,)) as cur:
            return await cur.fetchone()


async def update_plan(plan_id: str, name: str, amount: float, validity: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE plans SET name = ?, amount = ?, validity = ? WHERE plan_id = ?",
            (name, amount, validity, plan_id),
        )
        await db.commit()


async def add_or_update_user(user: types.User):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """
            INSERT INTO users (user_id, full_name, username)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET full_name = excluded.full_name, username = excluded.username
            """,
            (user.id, user.full_name, user.username or "N/A"),
        )
        await db.commit()


async def get_user(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT user_id, full_name, username, joined_at, premium_status, is_banned FROM users WHERE user_id = ?",
            (user_id,),
        ) as cur:
            return await cur.fetchone()


async def get_user_payment_stats(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            """
            SELECT 
                COUNT(CASE WHEN status = 'approved' THEN 1 END),
                COUNT(CASE WHEN status = 'pending' THEN 1 END),
                COUNT(*)
            FROM payments WHERE user_id = ?
            """,
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
            return {
                "approved": row[0] or 0,
                "pending": row[1] or 0,
                "total": row[2] or 0,
            }


async def set_user_ban_status(user_id: int, is_banned: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET is_banned = ? WHERE user_id = ?", (is_banned, user_id))
        await db.commit()


async def generate_upi_qr(plan_name: str, amount: float) -> io.BytesIO:
    upi_id = await get_setting("upi_id")
    payee_name = await get_setting("payee_name")
    upi_params = {
        "pa": upi_id,
        "pn": payee_name,
        "am": f"{amount:.2f}",
        "cu": "INR",
        "tn": f"Payment for {plan_name}",
    }
    upi_url = "upi://pay?" + urllib.parse.urlencode(upi_params)
    qr = segno.make(upi_url, error="m")
    buffer = io.BytesIO()
    qr.save(buffer, kind="png", scale=8, border=2)
    buffer.seek(0)
    return buffer


# ==========================================
# MIDDLEWARE & SECURITY
# ==========================================
class SecurityMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: types.TelegramObject, data: dict):
        user = data.get("event_from_user")
        if not user or user.id in ADMIN_IDS:
            return await handler(event, data)
        user_record = await get_user(user.id)
        if user_record and user_record[5] == 1:
            if isinstance(event, types.Message):
                await event.answer("You are banned from using this bot.")
            return
        if await get_setting("maintenance") == "on":
            if isinstance(event, types.Message):
                await event.answer("Bot is under maintenance. Please try again later.")
            return
        return await handler(event, data)


manager.dp.message.outer_middleware(MessageTrackerMiddleware())
manager.dp.message.outer_middleware(SecurityMiddleware())
manager.dp.callback_query.outer_middleware(SecurityMiddleware())


# ==========================================
# FSM STATES & KEYBOARDS
# ==========================================
class AdminStates(StatesGroup):
    waiting_for_broadcast = State()
    waiting_for_lookup = State()
    waiting_for_welcome_photo = State()
    waiting_for_welcome_text = State()
    waiting_for_plans_text = State()
    waiting_for_demo_video = State()
    waiting_for_upi_id = State()
    waiting_for_payee_name = State()
    waiting_for_plan_name = State()
    waiting_for_plan_price = State()
    waiting_for_plan_validity = State()


class PaymentStates(StatesGroup):
    waiting_for_screenshot = State()


def get_home_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="🎬 View Demo", callback_data="btn_view_demo")
    builder.button(text="⭐ My Premium", callback_data="btn_my_premium")
    builder.button(text="👤 My Profile", callback_data="btn_my_profile")
    builder.adjust(1)
    return builder.as_markup()


def get_demo_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="💎 Get Premium", callback_data="btn_get_premium")
    builder.button(text="🏠 Home", callback_data="btn_home")
    builder.adjust(2)
    return builder.as_markup()


async def get_8_plans_keyboard():
    builder = InlineKeyboardBuilder()
    plans = await get_all_plans()
    for pid, name, price, _ in plans:
        builder.button(text=f"🔥 {name} (Rs.{int(price)})", callback_data=f"buy_plan:{pid}")
    builder.button(text="🏠 Home", callback_data="btn_home")
    builder.adjust(*(1 for _ in range(len(plans) + 1)))
    return builder.as_markup()


def get_upi_card_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="📥 CHECK PAYMENT", callback_data="check_payment")
    builder.button(text="🔙 BACK TO PLANS", callback_data="btn_get_premium")
    builder.adjust(1)
    return builder.as_markup()


async def get_admin_menu():
    maint = await get_setting("maintenance")
    builder = InlineKeyboardBuilder()
    builder.button(text="Stats", callback_data="admin_stats")
    builder.button(text="User Lookup", callback_data="admin_lookup")
    builder.button(text="Broadcast", callback_data="admin_broadcast")
    builder.button(text=f"Maint: {maint.upper()}", callback_data="admin_toggle_maint")
    builder.button(text="Edit Photo", callback_data="adm_edit_photo")
    builder.button(text="Edit Welcome Msg", callback_data="adm_edit_wtext")
    builder.button(text="Edit Plans Msg", callback_data="adm_edit_ptext")
    builder.button(text="Edit Demo Video", callback_data="adm_edit_video")
    builder.button(text="Edit Payment UPI", callback_data="adm_edit_upi")
    builder.button(text="Edit Plan Buttons", callback_data="adm_edit_plans_list")
    builder.button(text="Close", callback_data="admin_close")
    builder.adjust(2, 2, 2, 2, 2, 1)
    return builder.as_markup()


def get_admin_user_card_keyboard(target_id: int, is_banned: int):
    builder = InlineKeyboardBuilder()
    builder.button(
        text="Unban User" if is_banned else "Ban User",
        callback_data=f"adm_ban:{target_id}:{0 if is_banned else 1}",
    )
    builder.button(text="Back", callback_data="admin_home")
    builder.adjust(1)
    return builder.as_markup()


# ==========================================
# TELEGRAM BOT FLOW HANDLERS
# ==========================================
async def send_welcome_flow(chat_id: int):
    if not manager.bot:
        return
    photo_url = await get_setting("welcome_photo")
    caption = await get_setting("welcome_text")
    try:
        m1 = await manager.bot.send_photo(
            chat_id=chat_id,
            photo=photo_url,
            caption=caption,
            reply_markup=get_home_keyboard(),
        )
    except Exception:
        m1 = await manager.bot.send_message(
            chat_id=chat_id, text=caption, reply_markup=get_home_keyboard()
        )
    track(chat_id, m1.message_id)

    plans_txt = await get_setting("plans_text")
    m2 = await manager.bot.send_message(
        chat_id=chat_id,
        text=plans_txt,
        reply_markup=await get_8_plans_keyboard(),
    )
    track(chat_id, m2.message_id)


@manager.dp.message(CommandStart())
async def handle_start(message: types.Message):
    await add_or_update_user(message.from_user)
    await send_welcome_flow(message.chat.id)


@manager.dp.callback_query(F.data == "btn_home")
async def nav_home(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    await delete_old_messages(callback.message.chat.id)
    try:
        await callback.message.delete()
    except Exception:
        pass
    await send_welcome_flow(callback.message.chat.id)


@manager.dp.callback_query(F.data == "btn_get_premium")
async def nav_plans(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    if not manager.bot:
        return
    text = await get_setting("plans_text")
    msg = await manager.bot.send_message(
        chat_id=callback.message.chat.id,
        text=text,
        reply_markup=await get_8_plans_keyboard(),
    )
    track(callback.message.chat.id, msg.message_id)


@manager.dp.callback_query(F.data == "btn_view_demo")
async def nav_demo(callback: types.CallbackQuery):
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    if not manager.bot:
        return
    video_url = await get_setting("demo_video")
    try:
        msg = await manager.bot.send_video(
            chat_id=callback.message.chat.id,
            video=video_url,
            caption="📺 Demo Video",
            reply_markup=get_demo_keyboard(),
        )
    except Exception:
        msg = await manager.bot.send_message(
            chat_id=callback.message.chat.id,
            text="📺 Demo video temporarily unavailable.",
            reply_markup=get_demo_keyboard(),
        )
    track(callback.message.chat.id, msg.message_id)


@manager.dp.callback_query(F.data == "btn_my_premium")
async def nav_my_premium(callback: types.CallbackQuery):
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    if not manager.bot:
        return
    user = await get_user(callback.from_user.id)
    plan = user[4] if user else "Free"
    text = f"⭐ My Premium Membership\n\nPlan: {plan}\nStatus: {'Active' if plan != 'Free' else 'Free Tier'}"
    msg = await manager.bot.send_message(
        chat_id=callback.message.chat.id,
        text=text,
        reply_markup=get_demo_keyboard(),
    )
    track(callback.message.chat.id, msg.message_id)


@manager.dp.callback_query(F.data == "btn_my_profile")
async def nav_my_profile(callback: types.CallbackQuery):
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    if not manager.bot:
        return
    user = await get_user(callback.from_user.id)
    if not user:
        return
    uid, full_name, username, joined_at, plan, _ = user
    payments = await get_user_payment_stats(uid)
    text = (
        f"👤 MY PROFILE\n"
        f"Name: {full_name}\n"
        f"Username: @{username}\n"
        f"ID: {uid}\n"
        f"Joined: {joined_at}\n"
        f"Plan: {plan}\n"
        f"Payments: Approved: {payments['approved']} | Pending: {payments['pending']}"
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="💎 Get Premium", callback_data="btn_get_premium")
    builder.button(text="🏠 Home", callback_data="btn_home")
    builder.adjust(2)
    msg = await manager.bot.send_message(
        chat_id=callback.message.chat.id,
        text=text,
        reply_markup=builder.as_markup(),
    )
    track(callback.message.chat.id, msg.message_id)


@manager.dp.callback_query(F.data.startswith("buy_plan:"))
async def process_plan_selection(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    if not manager.bot:
        return

    pid = callback.data.split(":")[1]
    plan = await get_plan(pid)
    if not plan:
        return
    _, plan_name, amount, validity = plan
    qr_buf = await generate_upi_qr(plan_name, amount)
    photo_file = BufferedInputFile(qr_buf.getvalue(), filename="qr.png")
    upi_id = await get_setting("upi_id")
    payee = await get_setting("payee_name")

    caption = (
        f"💳 UPI PAYMENT\n"
        f"Plan: {plan_name}\n"
        f"Amount: Rs.{amount:.2f}\n"
        f"Validity: {validity}\n\n"
        f"👤 Name: {payee}\n"
        f"🔑 UPI ID: {upi_id}\n\n"
        f"1️⃣ Scan QR and pay\n"
        f"2️⃣ Tap CHECK PAYMENT and upload screenshot"
    )
    await state.update_data(current_plan=plan_name, current_amount=amount)
    msg = await manager.bot.send_photo(
        chat_id=callback.message.chat.id,
        photo=photo_file,
        caption=caption,
        reply_markup=get_upi_card_keyboard(),
    )
    track(callback.message.chat.id, msg.message_id)


@manager.dp.callback_query(F.data == "check_payment")
async def handle_check_payment(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    if not manager.bot:
        return
    await state.set_state(PaymentStates.waiting_for_screenshot)
    msg = await manager.bot.send_message(
        chat_id=callback.message.chat.id,
        text="📸 Upload payment screenshot. /cancel to abort.",
    )
    track(callback.message.chat.id, msg.message_id)


@manager.dp.message(PaymentStates.waiting_for_screenshot, F.photo)
async def process_payment_proof(message: types.Message, state: FSMContext):
    if not manager.bot:
        return
    data = await state.get_data()
    plan_name = data.get("current_plan", "Unknown Plan")
    amount = data.get("current_amount", 0)

    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "INSERT INTO payments (user_id, plan_name, amount) VALUES (?, ?, ?)",
            (message.from_user.id, plan_name, amount),
        )
        pid = cur.lastrowid
        await db.commit()

    await state.clear()
    msg = await message.answer(
        "✅ Screenshot Received! Verification is in progress.",
        reply_markup=get_home_keyboard(),
    )
    track(message.chat.id, msg.message_id)

    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Approve", callback_data=f"adm_pay:{pid}:approved")
    builder.button(text="❌ Reject", callback_data=f"adm_pay:{pid}:rejected")
    builder.adjust(2)

    caption = (
        f"🔔 <b>New Payment Screenshot Received</b>\n\n"
        f"<b>Order ID:</b> #{pid}\n"
        f"<b>User ID:</b> <code>{message.from_user.id}</code>\n"
        f"<b>Username:</b> @{message.from_user.username or 'N/A'}\n"
        f"<b>Plan:</b> {plan_name}\n"
        f"<b>Amount:</b> Rs.{amount}"
    )

    photo_file_id = message.photo[-1].file_id

    for admin_id in ADMIN_IDS:
        try:
            await manager.bot.send_photo(
                chat_id=admin_id,
                photo=photo_file_id,
                caption=caption,
                parse_mode="HTML",
                reply_markup=builder.as_markup(),
            )
        except Exception as e:
            logging.error(f"Failed to send proof to admin {admin_id}: {e}")
            try:
                await manager.bot.send_message(
                    chat_id=admin_id,
                    text=f"{caption}\n\n⚠️ <i>(Screenshot could not be loaded directly)</i>",
                    parse_mode="HTML",
                    reply_markup=builder.as_markup(),
                )
            except Exception:
                pass


@manager.dp.message(PaymentStates.waiting_for_screenshot)
async def invalid_proof(message: types.Message, state: FSMContext):
    if message.text == "/cancel":
        await state.clear()
        await send_welcome_flow(message.chat.id)
        return
    msg = await message.answer("⚠️ Please send an image screenshot.")
    track(message.chat.id, message.message_id)


# ==========================================
# BOT ADMIN COMMANDS & HANDLERS
# ==========================================
@manager.dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if message.from_user.id in ADMIN_IDS:
        await message.answer("Admin Control Panel", reply_markup=await get_admin_menu())


@manager.dp.callback_query(F.data == "admin_home")
async def nav_admin_home(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    if callback.from_user.id in ADMIN_IDS:
        await callback.message.edit_text("Admin Control Panel", reply_markup=await get_admin_menu())
        await callback.answer()


@manager.dp.callback_query(F.data == "admin_close")
async def close_admin_panel(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.delete()
    await callback.answer()


@manager.dp.callback_query(F.data == "admin_toggle_maint")
async def handle_maintenance_toggle(callback: types.CallbackQuery):
    if callback.from_user.id in ADMIN_IDS:
        curr = await get_setting("maintenance")
        await update_setting("maintenance", "off" if curr == "on" else "on")
        await callback.message.edit_reply_markup(reply_markup=await get_admin_menu())
        await callback.answer("Maintenance toggled.")


@manager.dp.callback_query(F.data == "adm_edit_photo")
async def start_edit_welcome_photo(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id in ADMIN_IDS:
        await state.set_state(AdminStates.waiting_for_welcome_photo)
        await callback.message.edit_text("Send the new photo or URL:\n/cancel to abort.")
        await callback.answer()


@manager.dp.message(AdminStates.waiting_for_welcome_photo)
async def process_new_welcome_photo(message: types.Message, state: FSMContext):
    if message.text == "/cancel":
        await state.clear()
        await message.answer("Canceled.", reply_markup=await get_admin_menu())
        return
    target = message.photo[-1].file_id if message.photo else message.text
    await update_setting("welcome_photo", target)
    await state.clear()
    await message.answer("Welcome photo updated!", reply_markup=await get_admin_menu())


@manager.dp.callback_query(F.data == "adm_edit_wtext")
async def start_edit_welcome_text(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id in ADMIN_IDS:
        await state.set_state(AdminStates.waiting_for_welcome_text)
        await callback.message.edit_text("Send the new welcome text:\n/cancel to abort.")
        await callback.answer()


@manager.dp.message(AdminStates.waiting_for_welcome_text)
async def process_new_welcome_text(message: types.Message, state: FSMContext):
    if message.text == "/cancel":
        await state.clear()
        await message.answer("Canceled.", reply_markup=await get_admin_menu())
        return
    await update_setting("welcome_text", message.text)
    await state.clear()
    await message.answer("Welcome text updated!", reply_markup=await get_admin_menu())


@manager.dp.callback_query(F.data == "adm_edit_ptext")
async def start_edit_plans_text(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id in ADMIN_IDS:
        await state.set_state(AdminStates.waiting_for_plans_text)
        await callback.message.edit_text("Send the new plans text:\n/cancel to abort.")
        await callback.answer()


@manager.dp.message(AdminStates.waiting_for_plans_text)
async def process_new_plans_text(message: types.Message, state: FSMContext):
    if message.text == "/cancel":
        await state.clear()
        await message.answer("Canceled.", reply_markup=await get_admin_menu())
        return
    await update_setting("plans_text", message.text)
    await state.clear()
    await message.answer("Plans message text updated!", reply_markup=await get_admin_menu())


@manager.dp.callback_query(F.data == "adm_edit_video")
async def start_edit_demo_video(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id in ADMIN_IDS:
        await state.set_state(AdminStates.waiting_for_demo_video)
        await callback.message.edit_text("Send the new video or URL:\n/cancel to abort.")
        await callback.answer()


@manager.dp.message(AdminStates.waiting_for_demo_video)
async def process_new_demo_video(message: types.Message, state: FSMContext):
    if message.text == "/cancel":
        await state.clear()
        await message.answer("Canceled.", reply_markup=await get_admin_menu())
        return
    target = message.video.file_id if message.video else message.text
    await update_setting("demo_video", target)
    await state.clear()
    await message.answer("Demo video updated!", reply_markup=await get_admin_menu())


@manager.dp.callback_query(F.data == "adm_edit_upi")
async def start_edit_upi(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id in ADMIN_IDS:
        await state.set_state(AdminStates.waiting_for_upi_id)
        await callback.message.edit_text("Step 1: Enter new UPI ID:\n/cancel to abort.")
        await callback.answer()


@manager.dp.message(AdminStates.waiting_for_upi_id)
async def process_new_upi_id(message: types.Message, state: FSMContext):
    if message.text == "/cancel":
        await state.clear()
        await message.answer("Canceled.", reply_markup=await get_admin_menu())
        return
    await state.update_data(new_upi_id=message.text.strip())
    await state.set_state(AdminStates.waiting_for_payee_name)
    await message.answer("Step 2: Enter new Payee Name:")


@manager.dp.message(AdminStates.waiting_for_payee_name)
async def process_new_payee_name(message: types.Message, state: FSMContext):
    if message.text == "/cancel":
        await state.clear()
        await message.answer("Canceled.", reply_markup=await get_admin_menu())
        return
    d = await state.get_data()
    await update_setting("upi_id", d["new_upi_id"])
    await update_setting("payee_name", message.text.strip())
    await state.clear()
    await message.answer("UPI details updated!", reply_markup=await get_admin_menu())


@manager.dp.callback_query(F.data == "adm_edit_plans_list")
async def show_plans_for_editing(callback: types.CallbackQuery):
    if callback.from_user.id in ADMIN_IDS:
        plans = await get_all_plans()
        builder = InlineKeyboardBuilder()
        for pid, name, price, _ in plans:
            builder.button(text=f"{name} (Rs.{int(price)})", callback_data=f"adm_psel:{pid}")
        builder.button(text="Back", callback_data="admin_home")
        builder.adjust(1)
        await callback.message.edit_text("Select a button/plan to edit:", reply_markup=builder.as_markup())
        await callback.answer()


@manager.dp.callback_query(F.data.startswith("adm_psel:"))
async def select_plan_to_edit(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id in ADMIN_IDS:
        pid = callback.data.split(":")[1]
        await state.update_data(target_pid=pid)
        await state.set_state(AdminStates.waiting_for_plan_name)
        await callback.message.edit_text(f"Step 1: Send new Plan Name for `{pid}`:\n/cancel to abort.")
        await callback.answer()


@manager.dp.message(AdminStates.waiting_for_plan_name)
async def process_edit_plan_name(message: types.Message, state: FSMContext):
    if message.text == "/cancel":
        await state.clear()
        await message.answer("Canceled.", reply_markup=await get_admin_menu())
        return
    await state.update_data(new_pname=message.text.strip())
    await state.set_state(AdminStates.waiting_for_plan_price)
    await message.answer("Step 2: Enter new Price (e.g. 199):")


@manager.dp.message(AdminStates.waiting_for_plan_price)
async def process_edit_plan_price(message: types.Message, state: FSMContext):
    if message.text == "/cancel":
        await state.clear()
        await message.answer("Canceled.", reply_markup=await get_admin_menu())
        return
    try:
        price = float(message.text.strip())
        await state.update_data(new_pprice=price)
        await state.set_state(AdminStates.waiting_for_plan_validity)
        await message.answer("Step 3: Enter new Validity (e.g. 30 Days):")
    except ValueError:
        await message.answer("Please enter a numeric price.")


@manager.dp.message(AdminStates.waiting_for_plan_validity)
async def process_edit_plan_validity(message: types.Message, state: FSMContext):
    if message.text == "/cancel":
        await state.clear()
        await message.answer("Canceled.", reply_markup=await get_admin_menu())
        return
    d = await state.get_data()
    await update_plan(d["target_pid"], d["new_pname"], d["new_pprice"], message.text.strip())
    await state.clear()
    await message.answer("Plan updated!", reply_markup=await get_admin_menu())


@manager.dp.callback_query(F.data == "admin_stats")
async def handle_admin_stats(callback: types.CallbackQuery):
    if callback.from_user.id in ADMIN_IDS:
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT COUNT(*) FROM users") as cur:
                tot = (await cur.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM users WHERE is_banned=1") as cur:
                ban = (await cur.fetchone())[0]
            async with db.execute(
                "SELECT COUNT(*), SUM(amount) FROM payments WHERE status='approved'"
            ) as cur:
                row = await cur.fetchone()
                sales, rev = row[0] or 0, row[1] or 0.0
        builder = InlineKeyboardBuilder()
        builder.button(text="Back", callback_data="admin_home")
        await callback.message.edit_text(
            f"Analytics:\nTotal Users: {tot}\nActive: {tot - ban}\nBanned: {ban}\nApproved Orders: {sales}\nRevenue: Rs.{rev:.2f}",
            reply_markup=builder.as_markup(),
        )
        await callback.answer()


@manager.dp.callback_query(F.data == "admin_lookup")
async def start_user_lookup(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id in ADMIN_IDS:
        await state.set_state(AdminStates.waiting_for_lookup)
        await callback.message.edit_text("Send numeric Telegram ID:\n/cancel to abort.")
        await callback.answer()


@manager.dp.message(AdminStates.waiting_for_lookup)
async def process_user_lookup(message: types.Message, state: FSMContext):
    if message.text == "/cancel":
        await state.clear()
        await message.answer("Canceled.", reply_markup=await get_admin_menu())
        return
    if not message.text.isdigit():
        await message.answer("Send digits only.")
        return
    u = await get_user(int(message.text))
    await state.clear()
    if not u:
        await message.answer("User not found.", reply_markup=await get_admin_menu())
        return
    uid, name, uname, joined, plan, ban = u
    await message.answer(
        f"ID: {uid}\nName: {name}\nUsername: @{uname}\nPlan: {plan}\nStatus: {'Banned' if ban else 'Active'}",
        reply_markup=get_admin_user_card_keyboard(uid, ban),
    )


@manager.dp.callback_query(F.data.startswith("adm_ban:"))
async def handle_admin_ban(callback: types.CallbackQuery):
    if callback.from_user.id in ADMIN_IDS:
        _, uid, st = callback.data.split(":")
        await set_user_ban_status(int(uid), int(st))
        u = await get_user(int(uid))
        await callback.message.edit_text(
            f"User {uid} updated. Status: {'Banned' if u[5] else 'Active'}",
            reply_markup=get_admin_user_card_keyboard(int(uid), u[5]),
        )
        await callback.answer("Updated.")


@manager.dp.callback_query(F.data == "admin_broadcast")
async def start_admin_broadcast(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id in ADMIN_IDS:
        await state.set_state(AdminStates.waiting_for_broadcast)
        await callback.message.edit_text("Send message to broadcast:\n/cancel to abort.")
        await callback.answer()


@manager.dp.message(AdminStates.waiting_for_broadcast)
async def process_admin_broadcast(message: types.Message, state: FSMContext):
    if message.text == "/cancel":
        await state.clear()
        await message.answer("Canceled.", reply_markup=await get_admin_menu())
        return
    await state.clear()
    status_msg = await message.answer("Broadcasting...")
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id FROM users WHERE is_banned=0") as cur:
            users = await cur.fetchall()
    sent = 0
    for (uid,) in users:
        try:
            await message.copy_to(chat_id=uid)
            sent += 1
            await asyncio.sleep(0.05)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                await message.copy_to(chat_id=uid)
                sent += 1
            except Exception:
                pass
        except Exception:
            pass
    await status_msg.edit_text(
        f"Broadcast finished. Sent: {sent}", reply_markup=await get_admin_menu()
    )


@manager.dp.callback_query(F.data.startswith("adm_pay:"))
async def handle_admin_pay_approval(callback: types.CallbackQuery):
    if not manager.bot:
        return
    if callback.from_user.id in ADMIN_IDS:
        _, pid, act = callback.data.split(":")
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute(
                "SELECT user_id, plan_name FROM payments WHERE id=?", (int(pid),)
            ) as cur:
                p = await cur.fetchone()
            if not p:
                await callback.answer("Not found.")
                return
            t_uid, pl = p
            if act == "approved":
                await db.execute("UPDATE payments SET status='approved' WHERE id=?", (int(pid),))
                await db.execute("UPDATE users SET premium_status=? WHERE user_id=?", (pl, t_uid))
                await db.commit()
                try:
                    await manager.bot.send_message(
                        t_uid, f"Payment Approved! Your plan {pl} is active!"
                    )
                except Exception:
                    pass
                await callback.message.edit_caption(
                    caption=callback.message.caption + "\n\nSTATUS: APPROVED"
                )
            else:
                await db.execute("UPDATE payments SET status='rejected' WHERE id=?", (int(pid),))
                await db.commit()
                try:
                    await manager.bot.send_message(t_uid, "Payment verification failed.")
                except Exception:
                    pass
                await callback.message.edit_caption(
                    caption=callback.message.caption + "\n\nSTATUS: REJECTED"
                )
        await callback.answer("Status updated.")


# ==========================================
# FASTAPI LIFECYCLE & WEB DASHBOARD
# ==========================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    token = await get_setting("bot_token")
    if token and token != "YOUR_BOT_TOKEN_HERE":
        await manager.start(token)
    yield
    await manager.stop()


app = FastAPI(lifespan=lifespan)

# ==========================================
# MODERN TEMPLATES (LOGIN & DASHBOARD)
# ==========================================
LOGIN_PAGE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Admin Login - Bot Center</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        body { font-family: 'Plus Jakarta Sans', sans-serif; }
    </style>
</head>
<body class="bg-slate-950 text-slate-100 min-h-screen flex items-center justify-center p-4 selection:bg-indigo-500 selection:text-white">
    <div class="w-full max-w-md">
        <!-- Brand Logo/Header -->
        <div class="text-center mb-8">
            <div class="inline-flex items-center justify-center w-14 h-14 rounded-2xl bg-gradient-to-tr from-indigo-600 to-indigo-400 shadow-xl shadow-indigo-500/20 mb-3">
                <svg class="w-7 h-7 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"/>
                </svg>
            </div>
            <h1 class="text-2xl font-bold tracking-tight text-white">Bot Console</h1>
            <p class="text-sm text-slate-400 mt-1">Authenticate to manage telegram bot parameters</p>
        </div>

        <!-- Login Card -->
        <div class="bg-slate-900/90 backdrop-blur-xl border border-slate-800/80 rounded-2xl p-7 shadow-2xl shadow-black/50">
            {% if error %}
            <div class="mb-5 p-3.5 rounded-xl bg-rose-500/15 border border-rose-500/30 text-rose-400 text-xs font-medium flex items-center gap-2">
                <svg class="w-4 h-4 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"/></svg>
                <span>{{ error }}</span>
            </div>
            {% endif %}

            <form method="POST" action="/login" class="space-y-4">
                <div>
                    <label class="block text-xs font-semibold text-slate-300 uppercase tracking-wider mb-2">Username</label>
                    <input type="text" name="username" required autofocus placeholder="admin"
                           class="w-full bg-slate-950/80 border border-slate-700/70 rounded-xl px-4 py-2.5 text-sm text-white placeholder:text-slate-600 focus:outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 transition">
                </div>

                <div>
                    <label class="block text-xs font-semibold text-slate-300 uppercase tracking-wider mb-2">Password</label>
                    <input type="password" name="password" required placeholder="••••••••••••"
                           class="w-full bg-slate-950/80 border border-slate-700/70 rounded-xl px-4 py-2.5 text-sm text-white placeholder:text-slate-600 focus:outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 transition">
                </div>

                <button type="submit"
                        class="w-full mt-2 bg-indigo-600 hover:bg-indigo-500 active:scale-[0.99] text-white font-semibold py-2.5 rounded-xl shadow-lg shadow-indigo-600/25 transition">
                    Sign In
                </button>
            </form>
        </div>

        <p class="text-center text-xs text-slate-600 mt-6">&copy; Telegram Bot Engine. All rights reserved.</p>
    </div>
</body>
</html>
"""

DASHBOARD_PAGE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Bot Command Console</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        body { font-family: 'Plus Jakarta Sans', sans-serif; }
    </style>
</head>
<body class="bg-slate-950 text-slate-100 min-h-screen flex overflow-x-hidden">

    <!-- Mobile Backdrop -->
    <div id="sidebarBackdrop" onclick="toggleSidebar()" class="fixed inset-0 bg-black/60 z-30 backdrop-blur-sm hidden md:hidden"></div>

    <!-- 3-Line Sliding Sidebar -->
    <aside id="sidebar" class="fixed inset-y-0 left-0 z-40 w-64 bg-slate-900 border-r border-slate-800 p-5 flex flex-col justify-between -translate-x-full md:translate-x-0 transition-transform duration-200 ease-in-out md:static md:h-screen">
        <div class="space-y-6">
            <!-- Brand -->
            <div class="flex items-center justify-between">
                <div class="flex items-center gap-3">
                    <div class="w-9 h-9 rounded-xl bg-indigo-600 flex items-center justify-center font-bold text-white shadow-lg shadow-indigo-600/30">⚡</div>
                    <div>
                        <span class="font-bold text-base text-white">Bot Admin</span>
                        <p class="text-[11px] text-slate-400">Control & Settings</p>
                    </div>
                </div>
                <button onclick="toggleSidebar()" class="md:hidden text-slate-400 hover:text-white p-1">
                    <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/></svg>
                </button>
            </div>

            <!-- Navigation Sections -->
            <nav class="space-y-1.5 text-sm">
                <button onclick="switchTab('tab-bot')" id="nav-tab-bot" class="nav-btn w-full flex items-center gap-3 px-3.5 py-2.5 rounded-xl font-medium text-slate-300 hover:bg-slate-800 hover:text-white transition bg-slate-800 text-white">
                    <svg class="w-4 h-4 shrink-0 text-indigo-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 7a2 2 0 012 2m4 0a6 6 0 01-7.743 5.743L11 17H9v2H7v2H4a1 1 0 01-1-1v-2.586a1 1 0 01.293-.707l5.964-5.964A6 6 0 1121 9z"/></svg>
                    Bot Token & Polling
                </button>
                <button onclick="switchTab('tab-media')" id="nav-tab-media" class="nav-btn w-full flex items-center gap-3 px-3.5 py-2.5 rounded-xl font-medium text-slate-400 hover:bg-slate-800 hover:text-white transition">
                    <svg class="w-4 h-4 shrink-0 text-indigo-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z"/></svg>
                    Media & Welcome Texts
                </button>
                <button onclick="switchTab('tab-system')" id="nav-tab-system" class="nav-btn w-full flex items-center gap-3 px-3.5 py-2.5 rounded-xl font-medium text-slate-400 hover:bg-slate-800 hover:text-white transition">
                    <svg class="w-4 h-4 shrink-0 text-indigo-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 9V7a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2m2 4h10a2 2 0 002-2v-6a2 2 0 00-2-2H9a2 2 0 00-2 2v6a2 2 0 002 2zm7-5a2 2 0 11-4 0 2 2 0 014 0z"/></svg>
                    UPI & Maintenance
                </button>
                <button onclick="switchTab('tab-plans')" id="nav-tab-plans" class="nav-btn w-full flex items-center gap-3 px-3.5 py-2.5 rounded-xl font-medium text-slate-400 hover:bg-slate-800 hover:text-white transition">
                    <svg class="w-4 h-4 shrink-0 text-indigo-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8c-1.657 0-3 .895-3 2s1.343 2 3 2 3 .895 3 2-1.343 2-3 2m0-8c1.11 0 2.08.402 2.599 1M12 8V7m0 1v8m0 0v1m0-1c-1.11 0-2.08-.402-2.599-1M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
                    Subscription Plans
                </button>
            </nav>
        </div>

        <!-- Bot Status Indicator & Logout -->
        <div class="pt-4 border-t border-slate-800/80 space-y-3">
            <div class="p-3 bg-slate-950/70 border border-slate-800 rounded-xl flex items-center justify-between">
                <div>
                    <span class="text-[10px] uppercase font-bold text-slate-500 tracking-wider">Engine Status</span>
                    <p class="text-xs font-semibold text-slate-200">{{ 'Online (Polling)' if is_online else 'Standby / Idle' }}</p>
                </div>
                <span class="w-2.5 h-2.5 rounded-full {{ 'bg-emerald-500 shadow-emerald-500/50 shadow-md' if is_online else 'bg-amber-500' }} animate-pulse"></span>
            </div>

            <a href="/logout" class="w-full flex items-center justify-center gap-2 py-2 rounded-xl text-xs font-semibold text-rose-400 hover:bg-rose-500/10 transition">
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1"/></svg>
                Sign Out
            </a>
        </div>
    </aside>

    <!-- Main Workspace -->
    <main class="flex-1 flex flex-col min-w-0 h-screen overflow-y-auto">
        <!-- Top Sticky Bar -->
        <header class="sticky top-0 z-20 bg-slate-950/80 backdrop-blur-md border-b border-slate-800/80 px-4 md:px-8 py-3.5 flex items-center justify-between">
            <div class="flex items-center gap-3">
                <!-- 3-Line Hamburger Button -->
                <button onclick="toggleSidebar()" class="md:hidden p-2 rounded-xl bg-slate-900 border border-slate-800 text-slate-300 hover:text-white">
                    <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 12h16M4 18h16"/></svg>
                </button>
                <h2 id="sectionTitle" class="text-lg font-bold text-white">Bot Token & Polling</h2>
            </div>
            
            <div class="flex items-center gap-2">
                <span class="text-xs px-2.5 py-1 rounded-full bg-slate-900 border border-slate-800 text-slate-400 font-mono">v2.4</span>
            </div>
        </header>

        <div class="p-4 md:p-8 max-w-4xl w-full mx-auto space-y-6">
            {% if saved %}
            <div class="p-4 rounded-xl bg-emerald-500/10 border border-emerald-500/30 text-emerald-400 text-sm font-medium flex items-center gap-2">
                <svg class="w-5 h-5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/></svg>
                <span>Configuration synchronized! Dynamic worker updated automatically.</span>
            </div>
            {% endif %}

            <!-- Form Container -->
            <form method="POST" action="/admin/save">
                <!-- Tab 1: Bot Token & Polling -->
                <div id="tab-bot" class="tab-content space-y-5">
                    <div class="bg-slate-900/80 border border-slate-800 rounded-2xl p-6 space-y-4">
                        <div class="border-b border-slate-800 pb-3">
                            <h3 class="text-base font-bold text-white">Telegram Authentication</h3>
                            <p class="text-xs text-slate-400 mt-0.5">Define your Telegram bot token obtained from @BotFather.</p>
                        </div>
                        <div>
                            <label class="block text-xs font-semibold text-slate-300 uppercase tracking-wider mb-2">Bot API Token</label>
                            <input type="text" name="bot_token" value="{{ bot_token }}" required
                                   class="w-full bg-slate-950 border border-slate-700/80 rounded-xl px-4 py-3 text-sm text-white font-mono focus:outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 transition">
                            <p class="text-xs text-slate-500 mt-2">When this token is updated, the internal bot loop re-initializes on the fly.</p>
                        </div>
                    </div>
                </div>

                <!-- Tab 2: Media & Texts -->
                <div id="tab-media" class="tab-content space-y-5 hidden">
                    <div class="bg-slate-900/80 border border-slate-800 rounded-2xl p-6 space-y-5">
                        <div class="border-b border-slate-800 pb-3">
                            <h3 class="text-base font-bold text-white">Onboarding & Marketing Messages</h3>
                            <p class="text-xs text-slate-400 mt-0.5">Customize default text payloads sent to users during interactions.</p>
                        </div>
                        
                        <div>
                            <label class="block text-xs font-semibold text-slate-300 uppercase tracking-wider mb-2">Welcome Caption</label>
                            <textarea name="welcome_text" rows="4" required
                                      class="w-full bg-slate-950 border border-slate-700/80 rounded-xl px-4 py-3 text-sm text-white focus:outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 transition">{{ welcome_text }}</textarea>
                        </div>

                        <div>
                            <label class="block text-xs font-semibold text-slate-300 uppercase tracking-wider mb-2">Plans Header Text</label>
                            <textarea name="plans_text" rows="3" required
                                      class="w-full bg-slate-950 border border-slate-700/80 rounded-xl px-4 py-3 text-sm text-white focus:outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 transition">{{ plans_text }}</textarea>
                        </div>

                        <div class="grid grid-cols-1 md:grid-cols-2 gap-4 pt-2">
                            <div>
                                <label class="block text-xs font-semibold text-slate-300 uppercase tracking-wider mb-2">Welcome Banner URL</label>
                                <input type="url" name="welcome_photo" value="{{ welcome_photo }}" required
                                       class="w-full bg-slate-950 border border-slate-700/80 rounded-xl px-4 py-2.5 text-sm text-white focus:outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 transition">
                            </div>
                            <div>
                                <label class="block text-xs font-semibold text-slate-300 uppercase tracking-wider mb-2">Demo Video URL</label>
                                <input type="url" name="demo_video" value="{{ demo_video }}" required
                                       class="w-full bg-slate-950 border border-slate-700/80 rounded-xl px-4 py-2.5 text-sm text-white focus:outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 transition">
                            </div>
                        </div>
                    </div>
                </div>

                <!-- Tab 3: UPI & Maintenance -->
                <div id="tab-system" class="tab-content space-y-5 hidden">
                    <div class="bg-slate-900/80 border border-slate-800 rounded-2xl p-6 space-y-5">
                        <div class="border-b border-slate-800 pb-3">
                            <h3 class="text-base font-bold text-white">Payment Gateway & Safety</h3>
                            <p class="text-xs text-slate-400 mt-0.5">Parameters used to dynamically produce payment QR codes.</p>
                        </div>

                        <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                            <div>
                                <label class="block text-xs font-semibold text-slate-300 uppercase tracking-wider mb-2">Merchant UPI ID</label>
                                <input type="text" name="upi_id" value="{{ upi_id }}" required
                                       class="w-full bg-slate-950 border border-slate-700/80 rounded-xl px-4 py-2.5 text-sm text-white focus:outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 transition">
                            </div>
                            <div>
                                <label class="block text-xs font-semibold text-slate-300 uppercase tracking-wider mb-2">Payee Registered Name</label>
                                <input type="text" name="payee_name" value="{{ payee_name }}" required
                                       class="w-full bg-slate-950 border border-slate-700/80 rounded-xl px-4 py-2.5 text-sm text-white focus:outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 transition">
                            </div>
                        </div>

                        <div>
                            <label class="block text-xs font-semibold text-slate-300 uppercase tracking-wider mb-2">Maintenance Override</label>
                            <select name="maintenance" class="w-full bg-slate-950 border border-slate-700/80 rounded-xl px-4 py-2.5 text-sm text-white focus:outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 transition">
                                <option value="off" {{ 'selected' if maintenance == 'off' else '' }}>OFF (Normal Access)</option>
                                <option value="on" {{ 'selected' if maintenance == 'on' else '' }}>ON (Reject Non-Admin Requests)</option>
                            </select>
                        </div>
                    </div>
                </div>

                <!-- Global Floating Save Action for Form Tabs -->
                <div id="saveBar" class="pt-4">
                    <button type="submit" class="w-full bg-indigo-600 hover:bg-indigo-500 active:scale-[0.99] text-white font-bold py-3.5 rounded-xl shadow-xl shadow-indigo-600/20 transition flex items-center justify-center gap-2">
                        <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/></svg>
                        Apply & Save Modifications
                    </button>
                </div>
            </form>

            <!-- Tab 4: Subscription Plans -->
            <div id="tab-plans" class="tab-content space-y-5 hidden">
                <div class="bg-slate-900/80 border border-slate-800 rounded-2xl p-6 space-y-4">
                    <div class="border-b border-slate-800 pb-3">
                        <h3 class="text-base font-bold text-white">Membership Tier Configuration</h3>
                        <p class="text-xs text-slate-400 mt-0.5">Modify names, prices, and validity cycles for all 8 plans.</p>
                    </div>

                    <div class="overflow-x-auto">
                        <table class="w-full text-left text-sm text-slate-300">
                            <thead class="bg-slate-950 text-slate-400 uppercase text-[11px] tracking-wider">
                                <tr>
                                    <th class="p-3">Plan Key</th>
                                    <th class="p-3">Display Title</th>
                                    <th class="p-3">Price (₹)</th>
                                    <th class="p-3">Validity</th>
                                    <th class="p-3 text-right">Action</th>
                                </tr>
                            </thead>
                            <tbody class="divide-y divide-slate-800">
                                {% for pid, name, amount, validity in plans %}
                                <tr>
                                    <form method="POST" action="/admin/plans/update">
                                        <input type="hidden" name="plan_id" value="{{ pid }}">
                                        <td class="p-3 font-mono text-xs text-indigo-400">{{ pid }}</td>
                                        <td class="p-3"><input type="text" name="name" value="{{ name }}" class="bg-slate-950 border border-slate-700/80 rounded-lg px-2.5 py-1 text-sm text-white w-full max-w-[180px]"></td>
                                        <td class="p-3"><input type="number" step="any" name="amount" value="{{ amount }}" class="bg-slate-950 border border-slate-700/80 rounded-lg px-2.5 py-1 text-sm text-white w-20"></td>
                                        <td class="p-3"><input type="text" name="validity" value="{{ validity }}" class="bg-slate-950 border border-slate-700/80 rounded-lg px-2.5 py-1 text-sm text-white w-28"></td>
                                        <td class="p-3 text-right">
                                            <button type="submit" class="bg-slate-800 hover:bg-indigo-600 active:scale-95 text-white px-3 py-1.5 rounded-lg text-xs font-medium transition">Update</button>
                                        </td>
                                    </form>
                                </tr>
                                {% endfor %}
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>

        </div>
    </main>

    <!-- Tab & Navigation Logic -->
    <script>
        function toggleSidebar() {
            const sidebar = document.getElementById('sidebar');
            const backdrop = document.getElementById('sidebarBackdrop');
            sidebar.classList.toggle('-translate-x-full');
            backdrop.classList.toggle('hidden');
        }

        const titles = {
            'tab-bot': 'Bot Token & Polling',
            'tab-media': 'Media & Welcome Texts',
            'tab-system': 'UPI & Maintenance',
            'tab-plans': 'Subscription Plans'
        };

        function switchTab(tabId) {
            // Hide all tab panes
            document.querySelectorAll('.tab-content').forEach(el => el.classList.add('hidden'));
            
            // Show selected tab
            const target = document.getElementById(tabId);
            if (target) target.classList.remove('hidden');

            // Hide main form submit button on plans tab (plans use per-row update forms)
            const saveBar = document.getElementById('saveBar');
            if (tabId === 'tab-plans') {
                saveBar.classList.add('hidden');
            } else {
                saveBar.classList.remove('hidden');
            }

            // Update Header Title
            document.getElementById('sectionTitle').innerText = titles[tabId] || 'Admin Console';

            // Update Nav active styling
            document.querySelectorAll('.nav-btn').forEach(btn => {
                btn.classList.remove('bg-slate-800', 'text-white');
                btn.classList.add('text-slate-400');
            });
            const activeNav = document.getElementById('nav-' + tabId);
            if (activeNav) {
                activeNav.classList.add('bg-slate-800', 'text-white');
                activeNav.classList.remove('text-slate-400');
            }

            // Close mobile menu if open
            if (window.innerWidth < 768) {
                toggleSidebar();
            }
        }
    </script>
</body>
</html>
"""


# ==========================================
# FASTAPI ROUTES & AUTH WORKFLOW
# ==========================================
@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, error: str | None = None):
    # If already logged in, redirect directly to dashboard
    if request.cookies.get(AUTH_COOKIE_NAME) == AUTH_SECRET:
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    tmpl = Template(LOGIN_PAGE)
    return HTMLResponse(content=tmpl.render(error=error))


@app.post("/login")
async def process_login(username: str = Form(...), password: str = Form(...)):
    if username == ADMIN_USER and password == ADMIN_PASS:
        response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
        response.set_cookie(key=AUTH_COOKIE_NAME, value=AUTH_SECRET, httponly=True, max_age=86400 * 7)
        return response
    tmpl = Template(LOGIN_PAGE)
    return HTMLResponse(content=tmpl.render(error="Invalid administrator credentials."), status_code=status.HTTP_401_UNAUTHORIZED)


@app.get("/logout")
async def logout_admin():
    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(key=AUTH_COOKIE_NAME)
    return response


@app.get("/", response_class=HTMLResponse)
async def admin_dashboard(request: Request, saved: bool = False, is_auth: bool = Depends(require_admin)):
    tmpl = Template(DASHBOARD_PAGE)
    html = tmpl.render(
        saved=saved,
        is_online=manager.bot is not None,
        bot_token=await get_setting("bot_token"),
        welcome_text=await get_setting("welcome_text"),
        plans_text=await get_setting("plans_text"),
        welcome_photo=await get_setting("welcome_photo"),
        demo_video=await get_setting("demo_video"),
        upi_id=await get_setting("upi_id"),
        payee_name=await get_setting("payee_name"),
        maintenance=await get_setting("maintenance"),
        plans=await get_all_plans(),
    )
    return HTMLResponse(content=html)


@app.post("/admin/save")
async def save_general_settings(
    bot_token: str = Form(...),
    welcome_text: str = Form(...),
    plans_text: str = Form(...),
    welcome_photo: str = Form(...),
    demo_video: str = Form(...),
    upi_id: str = Form(...),
    payee_name: str = Form(...),
    maintenance: str = Form(...),
    is_auth: bool = Depends(require_admin),
):
    old_token = await get_setting("bot_token")
    cleaned_token = bot_token.strip()

    await update_setting("bot_token", cleaned_token)
    await update_setting("welcome_text", welcome_text.strip())
    await update_setting("plans_text", plans_text.strip())
    await update_setting("welcome_photo", welcome_photo.strip())
    await update_setting("demo_video", demo_video.strip())
    await update_setting("upi_id", upi_id.strip())
    await update_setting("payee_name", payee_name.strip())
    await update_setting("maintenance", maintenance.strip())

    if cleaned_token and (cleaned_token != old_token or not manager.bot):
        await manager.restart(cleaned_token)

    return RedirectResponse(url="/?saved=true", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/plans/update")
async def update_plan_details(
    plan_id: str = Form(...),
    name: str = Form(...),
    amount: float = Form(...),
    validity: str = Form(...),
    is_auth: bool = Depends(require_admin),
):
    await update_plan(plan_id, name.strip(), amount, validity.strip())
    return RedirectResponse(url="/?saved=true", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/health")
async def health_check():
    return {"status": "ok", "bot_online": manager.bot is not None}
