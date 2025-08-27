# main.py
import os
import asyncio
import hmac
import hashlib
import logging
import json
import time
from typing import Optional, List, Dict, Any
import asyncpg
from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

# ----------------------------
# Logging
# ----------------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("telegram-bot")

# ----------------------------
# Environment / Config
# ----------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS_ENV = os.getenv("ADMIN_IDS", "")  # comma separated
ADMIN_IDS = set()
for p in [x.strip() for x in ADMIN_IDS_ENV.split(",") if x.strip()]:
    try:
        ADMIN_IDS.add(int(p))
    except:
        pass

STORAGE_CHANNEL_ID = int(os.getenv("STORAGE_CHANNEL_ID", "0"))
DEFAULT_REQUIRED_CHANNEL_IDS = [int(x) for x in os.getenv("DEFAULT_REQUIRED_CHANNEL_IDS", "").split(",") if x.strip()]
DATABASE_URL = os.getenv("DATABASE_URL")
TOKEN_SECRET = os.getenv("TOKEN_SECRET", os.getenv("JWT_SECRET", "change_this_secret"))
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_BASE_URL", os.getenv("RENDER_EXTERNAL_URL", ""))  # try multiple fallbacks

DEFAULT_DELETE_TIMEOUT = int(os.getenv("DELETE_TIMEOUT_DEFAULT", os.getenv("DELETE_TIMEOUT", "20")))
RATE_LIMIT_COUNT = int(os.getenv("RATE_LIMIT_COUNT", "6"))
RATE_LIMIT_PERIOD = int(os.getenv("RATE_LIMIT_PERIOD", "60"))  # seconds

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required in env")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is required in env")

if not STORAGE_CHANNEL_ID:
    log.warning("STORAGE_CHANNEL_ID not set or zero — channel_post handling will be inactive until set.")

if not WEBHOOK_BASE_URL:
    log.warning("WEBHOOK_BASE_URL not set — webhook might not be configured correctly on startup.")


# ----------------------------
# DB (asyncpg pool)
# ----------------------------
_db_pool: Optional[asyncpg.pool.Pool] = None

async def get_db_pool():
    global _db_pool
    if _db_pool is None:
        _db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    return _db_pool

# helper DB functions (assume tables created via SQL)
async def insert_file_record(storage_chat_id:int, storage_message_id:int, file_unique_id:str, file_type:str, file_size:int, token:str, required_channels:List[int]=None):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO files (storage_chat_id, storage_message_id, file_unique_id, file_type, file_size, token, required_channels)
            VALUES ($1,$2,$3,$4,$5,$6,$7)
            RETURNING id;
        """, storage_chat_id, storage_message_id, file_unique_id, file_type, file_size, token, required_channels or [])
        return row['id']

async def get_file_by_token(token:str):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM files WHERE token=$1 AND active=true", token)
        return row

async def increment_file_views(file_id:int):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE files SET views = views + 1 WHERE id=$1", file_id)

async def upsert_user(user_id:int, username:Optional[str], first_name:Optional[str], last_name:Optional[str]):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO users (user_id, username, first_name, last_name, last_seen)
            VALUES ($1,$2,$3,$4, now())
            ON CONFLICT (user_id) DO UPDATE SET last_seen = now(), username = EXCLUDED.username;
        """, user_id, username, first_name, last_name)

async def record_delivery(file_id:int, user_id:int, sent_message_id:int):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO deliveries (file_id, user_id, sent_message_id) VALUES ($1,$2,$3) RETURNING id;
        """, file_id, user_id, sent_message_id)
        return row['id']

async def mark_delivery_deleted(delivery_id:int):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE deliveries SET deleted_at = now() WHERE id=$1", delivery_id)

async def get_setting(key:str, default:Optional[str]=None):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM settings WHERE key=$1", key)
        if row:
            return row['value']
        return default

async def set_setting(key:str, value:str):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO settings(key,value) VALUES($1,$2) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", key, value)


# ----------------------------
# Bot & Dispatcher & Webhook
# ----------------------------
bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher()

WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = WEBHOOK_BASE_URL.rstrip("/") + WEBHOOK_PATH if WEBHOOK_BASE_URL else None

# ----------------------------
# Utilities: secure token generation
# ----------------------------
def make_token(file_unique_id: str) -> str:
    # HMAC-SHA256 over file_unique_id + nonce, truncated
    nonce = os.urandom(12)
    hm = hmac.new(TOKEN_SECRET.encode(), file_unique_id.encode() + nonce, hashlib.sha256).hexdigest()
    # use base62-like by hex shortening (safe & URL-friendly)
    return hm[:36]  # 36 hex chars ~ 18 bytes

# ----------------------------
# Rate-limiter (in-memory, simple)
# ----------------------------
_rate_map: Dict[int, List[float]] = {}  # user_id -> timestamps

def is_rate_limited(user_id: int) -> bool:
    now_ts = time.time()
    lst = _rate_map.get(user_id, [])
    # remove old
    window_start = now_ts - RATE_LIMIT_PERIOD
    lst = [t for t in lst if t >= window_start]
    lst.append(now_ts)
    _rate_map[user_id] = lst
    return len(lst) > RATE_LIMIT_COUNT

# ----------------------------
# Helper: safe send with forbidden handling
# ----------------------------
async def safe_send_message(user_id:int, text:str, **kwargs):
    try:
        return await bot.send_message(user_id, text, **kwargs)
    except Exception as e:
        log.warning("safe_send_message failed: %s", e)
        return None

# ----------------------------
# Handlers: channel_post processing (create token automatically)
# ----------------------------
async def process_channel_post(update: dict):
    # Called when webhook receives channel_post update. Expects native update JSON.
    cp = update.get("channel_post")
    if not cp:
        return
    chat = cp.get("chat", {})
    chat_id = int(chat.get("id"))
    if STORAGE_CHANNEL_ID == 0:
        log.warning("STORAGE_CHANNEL_ID is not configured; ignoring channel_post.")
        return
    if chat_id != STORAGE_CHANNEL_ID:
        # ignore posts from other channels
        return

    message_id = cp.get("message_id")
    # find file object
    file_obj = None
    file_type = None
    file_size = None
    if "video" in cp:
        file_obj = cp["video"]; file_type = "video"; file_size = file_obj.get("file_size")
    elif "document" in cp:
        file_obj = cp["document"]; file_type = "document"; file_size = file_obj.get("file_size")
    elif "animation" in cp:
        file_obj = cp["animation"]; file_type = "animation"; file_size = file_obj.get("file_size")
    else:
        log.info("channel_post not media, ignoring")
        return

    file_unique_id = file_obj.get("file_unique_id")
    token = make_token(file_unique_id)
    try:
        file_db_id = await insert_file_record(storage_chat_id=chat_id, storage_message_id=message_id,
                                              file_unique_id=file_unique_id, file_type=file_type, file_size=file_size,
                                              token=token, required_channels=DEFAULT_REQUIRED_CHANNEL_IDS)
    except Exception as e:
        log.exception("DB insert_file_record failed: %s", e)
        return

    # Compose deep link for admin
    me = await bot.get_me()
    deep_link = f"t.me/{me.username}?start={token}"
    text = f"🎬 ویدیوی جدید در کانال ذخیره ثبت شد.\nToken: <code>{token}</code>\nDeep link: {deep_link}\n\nبرای ارسال به کاربران از طریق این لینک استفاده کنید."
    for admin in ADMIN_IDS:
        try:
            await safe_send_message(admin, text)
        except Exception as e:
            log.warning("notify admin failed %s: %s", admin, e)

# ----------------------------
# Handler: start deep link
# ----------------------------
class SimpleMsg:
    """Light wrapper to mimic parts of aiogram.types.Message for handle_start logic."""
    def __init__(self, user_dict, chat_dict, text, message_id):
        class U: pass
        self.from_user = U()
        self.from_user.id = user_dict.get("id")
        self.from_user.username = user_dict.get("username")
        self.from_user.first_name = user_dict.get("first_name")
        self.from_user.last_name = user_dict.get("last_name")
        self.chat = chat_dict
        self.text = text
        self.message_id = message_id

    async def answer(self, text, **kwargs):
        return await safe_send_message(self.from_user.id, text, **kwargs)

async def handle_start_msg_wrapper(user_dict, chat_dict, token):
    # create a SimpleMsg and call handle_start_message
    sm = SimpleMsg(user_dict, chat_dict, f"/start {token}", None)
    await handle_start_message(sm, token)

async def handle_start_message(message: SimpleMsg, token: str):
    user = message.from_user
    uid = int(user.id)
    # rate limit
    if is_rate_limited(uid):
        await message.answer("⛔ در حال حاضر درخواست‌های زیادی از شما ثبت شده — لطفا بعدا تلاش کنید.")
        return

    await upsert_user(uid, getattr(user, "username", None), getattr(user, "first_name", None), getattr(user, "last_name", None))

    row = await get_file_by_token(token)
    if not row:
        await message.answer("❌ لینک نامعتبر یا منقضی شده.")
        return

    # membership checks
    req_channels = row.get("required_channels") or []
    for ch in req_channels:
        try:
            member = await bot.get_chat_member(int(ch), uid)
            if member.status in ("left", "kicked"):
                # ask to join
                try:
                    ch_info = await bot.get_chat(int(ch))
                    join_url = f"https://t.me/{ch_info.username}" if getattr(ch_info, "username", None) else f"https://t.me/c/{str(ch)[4:]}"
                    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton("عضو شدن", url=join_url)]])
                    await message.answer("برای دیدن این ویدیو باید ابتدا عضو کانال شوید.", reply_markup=kb)
                except Exception:
                    await message.answer("برای دیدن این ویدیو باید ابتدا عضو کانال شوید.")
                return
        except Exception as e:
            log.warning("get_chat_member failed: %s", e)
            await message.answer("خطا در بررسی عضویت — لطفا بعدا تلاش کنید.")
            return

    # forward message
    storage_chat_id = row['storage_chat_id']
    storage_message_id = row['storage_message_id']
    try:
        forwarded = await bot.forward_message(chat_id=uid, from_chat_id=storage_chat_id, message_id=storage_message_id)
    except Exception as e:
        log.exception("forward failed: %s", e)
        await message.answer("خطا در ارسال فایل.")
        return

    # send warning
    timeout_setting = await get_setting('delete_timeout_seconds', str(DEFAULT_DELETE_TIMEOUT))
    try:
        timeout = int(timeout_setting)
    except:
        timeout = DEFAULT_DELETE_TIMEOUT

    warning = await bot.send_message(uid, f"⚠️ این پیام و فایل بعد از {timeout} ثانیه حذف خواهد شد. سریعاً سیو یا فوروارد کنید.")
    # record delivery and increment views
    delivery_id = await record_delivery(row['id'], uid, forwarded.message_id if forwarded else None)
    await increment_file_views(row['id'])

    # schedule delete async (non-blocking)
    async def do_delete(delivery_id_local, chat_id_local, forwarded_msg_id, warning_msg_id, delay):
        await asyncio.sleep(delay)
        try:
            if forwarded_msg_id:
                await bot.delete_message(chat_id_local, forwarded_msg_id)
        except Exception as e:
            log.warning("delete forwarded failed: %s", e)
        try:
            await bot.delete_message(chat_id_local, warning_msg_id)
        except Exception as e:
            log.warning("delete warning failed: %s", e)
        try:
            await mark_delivery_deleted(delivery_id_local)
        except Exception as e:
            log.warning("mark_delivery_deleted failed: %s", e)

    asyncio.create_task(do_delete(delivery_id, uid, forwarded.message_id if forwarded else None, warning.message_id, timeout))

# ----------------------------
# Admin panel (simple inline callbacks)
# ----------------------------
async def send_admin_panel(chat_id:int):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("لیست لینک‌ها", callback_data="admin:list_links")],
        [InlineKeyboardButton("تنظیم تایمر حذف", callback_data="admin:set_timer")],
        [InlineKeyboardButton("ارسال پیام همگانی", callback_data="admin:broadcast")],
        [InlineKeyboardButton("غیرفعال/فعال لینک", callback_data="admin:toggle_link")],
    ])
    await safe_send_message(chat_id, "📌 پنل مدیریت:", reply_markup=kb)

# helper: list links
async def admin_list_links(chat_id:int):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, token, created_at, views, active FROM files ORDER BY created_at DESC LIMIT 200")
    if not rows:
        await safe_send_message(chat_id, "هیچ لینکی ثبت نشده است.")
        return
    texts = []
    for r in rows:
        texts.append(f"ID: {r['id']}  token: <code>{r['token']}</code>\nviews: {r['views']} active: {r['active']}\n---")
    # send as chunks
    chunk = "\n\n".join(texts[:30])
    await safe_send_message(chat_id, chunk, parse_mode="HTML")

# ----------------------------
# aiogram message handlers (simple entrypoints for commands)
# ----------------------------
@dp.message()
async def generic_message_handler(msg: types.Message):
    text = (msg.text or "").strip()
    uid = msg.from_user.id
    # /start with token
    if text.startswith("/start"):
        parts = text.split()
        if len(parts) >= 2:
            token = parts[1].strip()
            await handle_start_message(msg, token)
            return
        else:
            await msg.answer("سلام! برای استفاده از لینک‌ها روی لینک‌ها کلیک کنید.")
            return

    if text.startswith("/admin"):
        if uid not in ADMIN_IDS:
            await msg.answer("⛔ شما دسترسی ندارید")
            return
        await send_admin_panel(uid)
        return

    # other simple commands
    if text.startswith("/help"):
        await msg.answer("دستورات: /start <token> و پنل ادمین /admin (برای ادمین‌ها)")
        return

    # fallback
    await msg.answer("✅ Bot is on!")

# ----------------------------
# Callback query handling for admin actions
# ----------------------------
@dp.callback_query()
async def callbacks_handler(cq: types.CallbackQuery):
    data = cq.data or ""
    uid = cq.from_user.id
    if uid not in ADMIN_IDS:
        await cq.answer("⛔ دسترسی ندارید", show_alert=True)
        return

    if data == "admin:list_links":
        await admin_list_links(uid)
        await cq.answer()
        return
    if data == "admin:set_timer":
        await cq.answer("ارسال کنید: عدد ثانیه جدید (مثال: 30)", show_alert=True)
        # next message from admin should be processed - a simple implementation below
        return
    if data == "admin:broadcast":
        await cq.answer("برای ارسال همگانی، پیام را همینجا بصورت reply به من بفرستید.", show_alert=True)
        return
    await cq.answer()

# ----------------------------
# Webhook / HTTP server entrypoints
# ----------------------------
app = web.Application()

# Register aiogram webhook handler
SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
setup_application(app, dp, bot=bot)

# Health route
async def health(request):
    return web.json_response({"status":"ok"})

app.router.add_get("/health", health)

# Manual endpoint to accept raw telegram update (safe fallback)
async def raw_update(request):
    try:
        data = await request.json()
    except:
        return web.Response(status=400, text="invalid json")
    # process channel_post quickly
    if "channel_post" in data:
        asyncio.create_task(process_channel_post(data))
    # messages: if /start <token> we can forward to handle_start_message wrapper
    if "message" in data:
        msg = data["message"]
        text = msg.get("text","")
        if text.startswith("/start"):
            parts = text.split()
            if len(parts) >= 2:
                token = parts[1]
                asyncio.create_task(handle_start_msg_wrapper(msg.get("from",{}), msg.get("chat",{}), token))
    return web.json_response({"ok": True})

app.router.add_post("/raw_update", raw_update)

# ----------------------------
# Startup / Shutdown
# ----------------------------
async def on_startup(app):
    log.info("Starting up: init DB pool")
    await get_db_pool()
    # set webhook
    if WEBHOOK_URL:
        try:
            await bot.set_webhook(WEBHOOK_URL, allowed_updates=["message","channel_post","callback_query","chat_member"])
            log.info("Webhook set: %s", WEBHOOK_URL)
        except Exception as e:
            log.exception("Failed to set webhook: %s", e)
    else:
        log.warning("WEBHOOK_URL not configured; skipping set_webhook")

async def on_shutdown(app):
    log.info("Shutting down")
    try:
        await bot.delete_webhook()
    except Exception:
        pass
    await bot.session.close()
    global _db_pool
    if _db_pool:
        await _db_pool.close()

app.on_startup.append(on_startup)
app.on_cleanup.append(on_shutdown)

# ----------------------------
# Run app
# ----------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    web.run_app(app, host="0.0.0.0", port=port)
