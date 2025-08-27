# main.py
import os
import asyncio
import hmac
import hashlib
import logging
import time
from typing import Optional, List, Dict

import asyncpg
from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiogram.client.default import DefaultBotProperties

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
    except Exception:
        pass

STORAGE_CHANNEL_ID = int(os.getenv("STORAGE_CHANNEL_ID", "0"))
DEFAULT_REQUIRED_CHANNEL_IDS = [
    int(x) for x in os.getenv("DEFAULT_REQUIRED_CHANNEL_IDS", "").split(",") if x.strip()
]
DATABASE_URL = os.getenv("DATABASE_URL")
TOKEN_SECRET = os.getenv("TOKEN_SECRET", os.getenv("JWT_SECRET", "change_this_secret"))
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_BASE_URL", os.getenv("RENDER_EXTERNAL_HOSTNAME", ""))

DEFAULT_DELETE_TIMEOUT = int(os.getenv("DELETE_TIMEOUT_DEFAULT", os.getenv("DELETE_TIMEOUT", "20")))
RATE_LIMIT_COUNT = int(os.getenv("RATE_LIMIT_COUNT", "6"))
RATE_LIMIT_PERIOD = int(os.getenv("RATE_LIMIT_PERIOD", "60"))  # seconds

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required in env")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is required in env")
if not WEBHOOK_BASE_URL:
    log.warning("WEBHOOK_BASE_URL/RENDER_EXTERNAL_HOSTNAME not set — set_webhook may fail.")

# ----------------------------
# DB (asyncpg pool)
# ----------------------------
_db_pool: Optional[asyncpg.pool.Pool] = None

async def get_db_pool():
    global _db_pool
    if _db_pool is None:
        _db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    return _db_pool

async def insert_file_record(storage_chat_id:int, storage_message_id:int,
                             file_unique_id:str, file_type:str, file_size:int,
                             token:str, required_channels:List[int]=None):
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
        await conn.execute("""
            INSERT INTO settings(key,value) VALUES($1,$2)
            ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value
        """, key, value)

# ----------------------------
# Bot & Dispatcher & Webhook
# ----------------------------
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode="HTML")
)
dp = Dispatcher()

WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"https://{WEBHOOK_BASE_URL.strip('/')}{WEBHOOK_PATH}" if not WEBHOOK_BASE_URL.startswith("http") \
              else WEBHOOK_BASE_URL.rstrip("/") + WEBHOOK_PATH

# ----------------------------
# Utilities
# ----------------------------
def make_token(file_unique_id: str) -> str:
    # HMAC-SHA256(file_unique_id + nonce) -> hex[:36]
    nonce = os.urandom(12)
    digest = hmac.new(TOKEN_SECRET.encode(), file_unique_id.encode() + nonce, hashlib.sha256).hexdigest()
    return digest[:36]

_rate_map: Dict[int, List[float]] = {}

def is_rate_limited(user_id: int) -> bool:
    now_ts = time.time()
    lst = _rate_map.get(user_id, [])
    window_start = now_ts - RATE_LIMIT_PERIOD
    lst = [t for t in lst if t >= window_start]
    lst.append(now_ts)
    _rate_map[user_id] = lst
    return len(lst) > RATE_LIMIT_COUNT

async def safe_send_message(user_id:int, text:str, **kwargs):
    try:
        return await bot.send_message(user_id, text, **kwargs)
    except Exception as e:
        log.warning("safe_send_message failed: %s", e)
        return None

# ----------------------------
# Channel post → auto-link
# ----------------------------
async def process_channel_post(update: dict):
    cp = update.get("channel_post")
    if not cp:
        return
    chat = cp.get("chat", {})
    chat_id = int(chat.get("id"))
    if STORAGE_CHANNEL_ID == 0 or chat_id != STORAGE_CHANNEL_ID:
        return

    message_id = cp.get("message_id")
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
        file_db_id = await insert_file_record(
            storage_chat_id=chat_id,
            storage_message_id=message_id,
            file_unique_id=file_unique_id,
            file_type=file_type,
            file_size=file_size or 0,
            token=token,
            required_channels=DEFAULT_REQUIRED_CHANNEL_IDS
        )
        log.info("Inserted file id=%s token=%s", file_db_id, token)
    except Exception as e:
        log.exception("DB insert_file_record failed: %s", e)
        return

    me = await bot.get_me()
    deep_link = f"t.me/{me.username}?start={token}"
    text = (
        "🎬 ویدیوی جدید ثبت شد.\n"
        f"Token: <code>{token}</code>\nDeep link: {deep_link}\n"
        "برای اشتراک‌گذاری از این لینک استفاده کنید."
    )
    for admin in ADMIN_IDS:
        await safe_send_message(admin, text)

# ----------------------------
# Start handler (deep-link)
# ----------------------------
async def handle_start_with_token(msg: types.Message, token: str):
    uid = msg.from_user.id

    if is_rate_limited(uid):
        await msg.answer("⛔ درخواست‌های زیاد؛ لطفاً کمی بعد تلاش کنید.")
        return

    await upsert_user(
        uid,
        msg.from_user.username,
        msg.from_user.first_name,
        msg.from_user.last_name
    )

    row = await get_file_by_token(token)
    if not row:
        await msg.answer("❌ لینک نامعتبر یا غیرفعال است.")
        return

    # Membership checks
    req_channels = row.get("required_channels") or []
    for ch in req_channels:
        try:
            member = await bot.get_chat_member(int(ch), uid)
            if member.status in ("left", "kicked"):
                try:
                    ch_info = await bot.get_chat(int(ch))
                    if getattr(ch_info, "username", None):
                        join_url = f"https://t.me/{ch_info.username}"
                    else:
                        # private channel: t.me/c/<id-without -100>
                        join_url = f"https://t.me/c/{str(ch)[4:]}"
                except Exception:
                    join_url = "https://t.me/"
                kb = InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton("عضو شدن", url=join_url)]]
                )
                await msg.answer("برای دریافت فایل باید عضو کانال شوید.", reply_markup=kb)
                return
        except Exception as e:
            log.warning("get_chat_member failed: %s", e)
            await msg.answer("خطا در بررسی عضویت — بعداً تلاش کنید.")
            return

    # Forward
    try:
        forwarded = await bot.forward_message(
            chat_id=uid,
            from_chat_id=row['storage_chat_id'],
            message_id=row['storage_message_id']
        )
    except Exception as e:
        log.exception("forward failed: %s", e)
        await msg.answer("خطا در ارسال فایل.")
        return

    # Warning + schedule deletion
    timeout_setting = await get_setting('delete_timeout_seconds', str(DEFAULT_DELETE_TIMEOUT))
    try:
        timeout = int(timeout_setting)
    except Exception:
        timeout = DEFAULT_DELETE_TIMEOUT

    warning = await msg.answer(
        f"⚠️ این پیام و فایل پس از {timeout} ثانیه حذف می‌شود. سریعاً ذخیره یا فوروارد کنید."
    )

    delivery_id = await record_delivery(row['id'], uid, forwarded.message_id if forwarded else None)
    await increment_file_views(row['id'])

    async def do_delete():
        await asyncio.sleep(timeout)
        try:
            if forwarded:
                await bot.delete_message(uid, forwarded.message_id)
        except Exception as e:
            log.warning("delete forwarded failed: %s", e)
        try:
            await bot.delete_message(uid, warning.message_id)
        except Exception as e:
            log.warning("delete warning failed: %s", e)
        try:
            await mark_delivery_deleted(delivery_id)
        except Exception as e:
            log.warning("mark_delivery_deleted failed: %s", e)

    asyncio.create_task(do_delete())

# ----------------------------
# Admin panel (basic)
# ----------------------------
async def send_admin_panel(chat_id:int):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("لیست لینک‌ها", callback_data="admin:list_links")],
        [InlineKeyboardButton("تنظیم تایمر حذف", callback_data="admin:set_timer")],
        [InlineKeyboardButton("ارسال پیام همگانی", callback_data="admin:broadcast")],
        [InlineKeyboardButton("غیرفعال/فعال‌سازی لینک", callback_data="admin:toggle_link")],
    ])
    await safe_send_message(chat_id, "📌 پنل مدیریت:", reply_markup=kb)

async def admin_list_links(chat_id:int):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, token, created_at, views, active
            FROM files
            ORDER BY created_at DESC
            LIMIT 200
        """)
    if not rows:
        await safe_send_message(chat_id, "هیچ لینکی ثبت نشده.")
        return
    parts = []
    for r in rows:
        parts.append(
            f"ID: {r['id']}  token: <code>{r['token']}</code>\n"
            f"views: {r['views']}  active: {r['active']}\n—"
        )
    # ارسال در یک پیام (اگر خیلی طولانی شد تلگرام قطع می‌کند؛ برای شروع کافیه)
    txt = "\n".join(parts)
    if len(txt) > 3800:
        txt = txt[:3800] + "\n…"
    await safe_send_message(chat_id, txt)

# ----------------------------
# Aiogram handlers
# ----------------------------
@dp.message()
async def all_messages(msg: types.Message):
    text = (msg.text or "").strip()
    uid = msg.from_user.id

    # /start with token
    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        if len(parts) == 2:
            token = parts[1].strip()
            await handle_start_with_token(msg, token)
            return
        await msg.answer("سلام! برای دریافت فایل‌ها از لینک‌های اختصاصی استفاده کنید.")
        return

    if text.startswith("/admin"):
        if uid not in ADMIN_IDS:
            await msg.answer("⛔ شما دسترسی ندارید")
            return
        await send_admin_panel(uid)
        return

    if text.startswith("/help"):
        await msg.answer("دستورات: /start <token> ، /admin (فقط ادمین)")
        return

    await msg.answer("✅ Bot is on!")

@dp.callback_query()
async def admin_callbacks(cq: types.CallbackQuery):
    if cq.from_user.id not in ADMIN_IDS:
        await cq.answer("⛔ دسترسی ندارید", show_alert=True)
        return
    data = cq.data or ""
    if data == "admin:list_links":
        await admin_list_links(cq.from_user.id)
        await cq.answer()
        return
    if data == "admin:set_timer":
        await cq.answer("عدد ثانیه جدید را ارسال کنید (مثال: 20).", show_alert=True)
        return
    if data == "admin:broadcast":
        await cq.answer("پیام همگانی را به‌صورت Reply روی همین پیام بفرستید.", show_alert=True)
        return
    if data == "admin:toggle_link":
        await cq.answer("فعلاً در نسخه بعدی فعال می‌شود.", show_alert=True)
        return
    await cq.answer()

# ----------------------------
# Webhook server
# ----------------------------
app = web.Application()

# مسیر وبهوک اصلی برای Aiogram
SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
setup_application(app, dp, bot=bot)

# Health check
async def health(_):
    return web.json_response({"status": "ok"})
app.router.add_get("/health", health)

# پذیرش مستقیم JSON خام (برای تست دستی اگر نیاز شد)
async def raw_update(request):
    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400, text="invalid json")
    # اگر از این مسیر برای تست استفاده شد:
    if "channel_post" in data:
        asyncio.create_task(process_channel_post(data))
    return web.json_response({"ok": True})
app.router.add_post("/raw_update", raw_update)

# ----------------------------
# Startup / Shutdown
# ----------------------------
async def on_startup(_app):
    log.info("Starting up: init DB pool")
    await get_db_pool()
    # ست کردن وبهوک
    try:
        await bot.set_webhook(
            WEBHOOK_URL,
            allowed_updates=["message", "channel_post", "callback_query", "chat_member"]
        )
        log.info("Webhook set: %s", WEBHOOK_URL)
    except Exception as e:
        log.exception("Failed to set webhook: %s", e)

async def on_shutdown(_app):
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
# Run
# ----------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    web.run_app(app, host="0.0.0.0", port=port)
