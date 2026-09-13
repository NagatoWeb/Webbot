import os
import asyncio
import aiosqlite
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Form, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command

# ----------------- CONFIG & CREDENTIALS -----------------
DB_PATH = "bot_database.db"
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "supersecret123")  # Change this!

security = HTTPBasic()

def check_admin(credentials: HTTPBasicCredentials = Depends(security)):
    if credentials.username != ADMIN_USER or credentials.password != ADMIN_PASS:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

# ----------------- DATABASE HELPERS -----------------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        defaults = {
            "bot_token": os.getenv("BOT_TOKEN", ""),
            "welcome_text": "👋 Welcome to Our Bot!\n\nManage your services below.",
            "welcome_photo": "https://images.unsplash.com/photo-1618005182384-a83a8bd57fbe",
            "upi_id": "paytm.s21dj6b@pty"
        }
        for k, v in defaults.items():
            await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
        await db.commit()

async def get_setting(key: str) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else ""

async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        await db.commit()

# ----------------- BOT CONTROLLER -----------------
class BotManager:
    def __init__(self):
        self.bot: Bot | None = None
        self.dp: Dispatcher = Dispatcher()
        self.polling_task: asyncio.Task | None = None
        self.register_handlers()

    def register_handlers(self):
        @self.dp.message(Command("start"))
        async def handle_start(message: types.Message):
            welcome_text = await get_setting("welcome_text")
            photo_url = await get_setting("welcome_photo")
            upi_id = await get_setting("upi_id")

            kb = types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [types.InlineKeyboardButton(text=f"Pay via UPI ({upi_id})", callback_data="pay_upi")],
                    [types.InlineKeyboardButton(text="Support", url="https://t.me/telegram")]
                ]
            )
            if photo_url:
                try:
                    await message.answer_photo(photo=photo_url, caption=welcome_text, reply_markup=kb)
                    return
                except Exception:
                    pass
            await message.answer(welcome_text, reply_markup=kb)

    async def start(self, token: str):
        if not token:
            print("[BotManager] No token configured. Bot is idle.")
            return

        self.bot = Bot(token=token)
        print("[BotManager] Starting polling with active token...")
        
        async def runner():
            try:
                await self.dp.start_polling(self.bot)
            except asyncio.CancelledError:
                pass
            except Exception as e:
                print(f"[BotManager] Error in polling: {e}")

        self.polling_task = asyncio.create_task(runner())

    async def stop(self):
        if self.polling_task and not self.polling_task.done():
            print("[BotManager] Stopping polling task...")
            self.polling_task.cancel()
            try:
                await self.polling_task
            except asyncio.CancelledError:
                pass
        if self.bot:
            await self.bot.session.close()
            self.bot = None
        print("[BotManager] Bot stopped cleanly.")

    async def restart(self, new_token: str):
        await self.stop()
        await self.start(new_token)

manager = BotManager()

# ----------------- FASTAPI LIFECYCLE -----------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    token = await get_setting("bot_token")
    if token:
        await manager.start(token)
    yield
    await manager.stop()

app = FastAPI(lifespan=lifespan)

# ----------------- WEB ADMIN UI (DASHBOARD) -----------------
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Bot Admin Control Panel</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-slate-900 text-slate-100 min-h-screen flex items-center justify-center p-4">
    <div class="max-w-xl w-full bg-slate-800 rounded-2xl shadow-xl p-6 border border-slate-700">
        <h1 class="text-2xl font-bold mb-1 text-indigo-400">🤖 Bot Admin Dashboard</h1>
        <p class="text-sm text-slate-400 mb-6">Manage bot credentials, text, and media in real time.</p>

        {% if saved %}
        <div class="mb-4 p-3 rounded-lg bg-emerald-500/20 text-emerald-300 border border-emerald-500/40 text-sm">
            ✅ Settings updated successfully! Bot was reloaded.
        </div>
        {% endif %}

        <form method="POST" action="/admin/save" class="space-y-4">
            <div>
                <label class="block text-sm font-medium text-slate-300 mb-1">Telegram Bot Token</label>
                <input type="password" name="bot_token" value="{{ bot_token }}" required
                       class="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-indigo-500 font-mono">
                <span class="text-xs text-slate-400">Changing this will stop the old bot and start the new bot immediately.</span>
            </div>

            <div>
                <label class="block text-sm font-medium text-slate-300 mb-1">Welcome Text Message</label>
                <textarea name="welcome_text" rows="4" required
                          class="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-indigo-500">{{ welcome_text }}</textarea>
            </div>

            <div>
                <label class="block text-sm font-medium text-slate-300 mb-1">Welcome Banner Image URL</label>
                <input type="url" name="welcome_photo" value="{{ welcome_photo }}"
                       class="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-indigo-500">
            </div>

            <div>
                <label class="block text-sm font-medium text-slate-300 mb-1">UPI ID</label>
                <input type="text" name="upi_id" value="{{ upi_id }}" required
                       class="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-indigo-500">
            </div>

            <button type="submit" 
                    class="w-full bg-indigo-600 hover:bg-indigo-500 text-white font-semibold py-2.5 rounded-lg transition shadow-md">
                Save & Reload Bot
            </button>
        </form>
    </div>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
async def admin_page(request: Request, saved: bool = False, username: str = Depends(check_admin)):
    from jinja2 import Template
    tmpl = Template(HTML_TEMPLATE)
    html = tmpl.render(
        saved=saved,
        bot_token=await get_setting("bot_token"),
        welcome_text=await get_setting("welcome_text"),
        welcome_photo=await get_setting("welcome_photo"),
        upi_id=await get_setting("upi_id"),
    )
    return HTMLResponse(content=html)

@app.post("/admin/save")
async def save_settings(
    bot_token: str = Form(...),
    welcome_text: str = Form(...),
    welcome_photo: str = Form(...),
    upi_id: str = Form(...),
    username: str = Depends(check_admin)
):
    old_token = await get_setting("bot_token")
    
    await set_setting("bot_token", bot_token.strip())
    await set_setting("welcome_text", welcome_text.strip())
    await set_setting("welcome_photo", welcome_photo.strip())
    await set_setting("upi_id", upi_id.strip())

    # Hot reload bot with the new token if it changed
    if old_token.strip() != bot_token.strip() or not manager.bot:
        await manager.restart(bot_token.strip())

    return RedirectResponse(url="/?saved=true", status_code=status.HTTP_303_SEE_OTHER)
