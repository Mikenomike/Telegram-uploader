# main.py
import os
import asyncio
import hmac
import hashlib
import logging
import time
from typing import Optional, List, Dict, Any

import asyncpg
from fastapi import FastAPI, Request, Header
from starlette.responses import JSONResponse
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram import exceptions as tg_excs

# ----------------------------
# Logging
# ----------------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("uploader-bot")

# ----------------------------
# Config (ENV)
# ----------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS_ENV = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = set(int(x.strip()) for x in ADMIN_IDS_ENV.split(",") if x.strip())

STORAGE_CHANNEL_ID = int(os.getenv("STORAGE_CHANNEL_ID", "0"))  # -100...
DEFAULT_REQUIRED_CHANNEL_IDS = [int(x) for x in os.getenv("DEFAULT_REQUIRED_CHANNEL_IDS", "").split(",") if x.strip()]

DATABASE_URL = os.getenv("DATABASE_URL")
TOKEN_SECRET = os.getenv("TOKEN_SECRET", os.getenv("JWT_SECRET", "change_this_secret_please"))
WEBHOOK_BASE_URL = os.getenv("WEBHOOK_BASE_URL", os.getenv("RENDER_EXTERNAL_HOSTNAME", ""))  # e.g. https://your-app.onrender.com
KEEPALIVE_SECRET = os.getenv("KEEPALIVE_SECRET", os.getenv("KEEPALIVE", ""))

DEFAULT_DELETE_TIMEOUT = int(os.getenv("DELETE_TIMEOUT_DEFAULT", "20"))
RATE_LIMIT_COUNT = int(os.getenv("RATE_LIMIT_COUNT", "6"))
RATE_LIMIT_PERIOD = int(os.getenv("RATE_LIMIT_PERIOD", "60"))  # seconds

# validations
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN env required")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL env required")
if not WEBHOOK_BASE_URL:
    log.warning("WEBHOOK_BASE_URL not set; webhook may fail to configure correctly.")

WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = WEBHOOK_BASE_URL.rstrip("/") + WEBHOOK_PATH if WEBHOOK_BASE_URL else None

# ----------------------------
# DB pool (asyncpg)
# ----------------------------
_db_pool: Optional[asyncpg.pool.Pool] = None

async def get_db_pool():
    global _db_pool
    if _db_pool is None:
        log.info("Creating asyncpg pool...")
        _db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    return _db_pool

# DB helpers
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
        return await conn.fetchrow("SELECT * FROM files WHERE token=$1 AND active=true", token)

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
        row = await conn.fetchrow("INSERT INTO deliveries (file_id, user_id, sent_message_id) VALUES ($1,$2,$3) RETURNING id;", file_id, user_id, sent_message_id)
        return row['id']

async def mark_delivery_deleted(delivery_id:int):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE deliveries SET deleted_at = now() WHERE id=$1", delivery_id)

async def get_setting(key:str, default:Optional[str]=None):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM settings WHERE key=$1", key)
        return row['value'] if row else default

async def set_setting(key:str, value:str):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        await conn.execute("INSERT INTO settings(key,value) VALUES($1,$2) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", key, value)

# ----------------------------
# Bot & Dispatcher
# ----------------------------
bot = Bot(token=BOT_TOKEN)  # aiogram 3.6 style
dp = Dispatcher()

# ----------------------------
# Utilities: token, rate-limit, safe send
# ----------------------------
def make_token(file_unique_id: str) -> str:
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
        return await bot.send_message(user_id, text, parse_mode="HTML", **kwargs)
    except tg_excs.TelegramForbiddenError:
        log.warning("Bot forbidden for user %s", user_id)
        return None
    except Exception as e:
        log.exception("safe_send_message failed: %s", e)
        return None

# ----------------------------
# Channel_post processing (auto-link)
# ----------------------------
async def process_channel_post(update: dict):
    cp = update.get("channel_post")
    if not cp:
        return
    chat = cp.get("chat", {})
    chat_id = int(chat.get("id"))
    if STORAGE_CHANNEL_ID == 0 or chat_id != STORAGE_CHANNEL_ID:
        log.info("Ignoring channel_post (not storage channel)")
        return

    message_id = cp.get("message_id")
    # detect supported media and get file_unique_id and size
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
        log.info("channel_post: unsupported media type.")
        return

    file_unique_id = file_obj.get("file_unique_id")
    token = make_token(file_unique_id)

    try:
        fid = await insert_file_record(storage_chat_id=chat_id,
                                       storage_message_id=message_id,
                                       file_unique_id=file_unique_id,
                                       file_type=file_type,
                                       file_size=file_size or 0,
                                       token=token,
                                       required_channels=DEFAULT_REQUIRED_CHANNEL_IDS)
        log.info("Inserted file id=%s token=%s", fid, token)
    except Exception as e:
        log.exception("DB insert failed: %s", e)
        return

    # notify admins with deep link
    try:
        me = await bot.get_me()
        deep_link = f"t.me/{me.username}?start={token}"
        text = f"🎬 فایل جدید ثبت شد.\nToken: <code>{token}</code>\nDeep link: {deep_link}"
        for adm in ADMIN_IDS:
            await safe_send_message(adm, text)
    except Exception as e:
        log.warning("notify admins failed: %s", e)

# ----------------------------
# /start <token> handling: forward + warning + scheduled delete
# ----------------------------
async def handle_start_message(msg: types.Message, token: str):
    uid = msg.from_user.id

    if is_rate_limited(uid):
        await msg.answer("⛔ درخواست‌های شما زیاد شده؛ لطفا چند لحظه بعد تلاش کنید.")
        return

    # upsert user
    await upsert_user(uid, msg.from_user.username, msg.from_user.first_name, msg.from_user.last_name)

    row = await get_file_by_token(token)
    if not row:
        await msg.answer("❌ لینک نامعتبر یا منقضی شده.")
        return

    # membership checks
    req_channels = row.get("required_channels") or []
    for ch in req_channels:
        try:
            member = await bot.get_chat_member(int(ch), uid)
            if member.status in ("left", "kicked"):
                try:
                    ch_info = await bot.get_chat(int(ch))
                    join_url = f"https://t.me/{ch_info.username}" if getattr(ch_info, "username", None) else f"https://t.me/c/{str(ch)[4:]}"
                except Exception:
                    join_url = "https://t.me/"
                kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton("عضو شدن", url=join_url)]])
                await msg.answer("برای دسترسی باید عضو کانال مورد نظر شوی.", reply_markup=kb)
                return
        except Exception as e:
            log.warning("get_chat_member failed: %s", e)
            await msg.answer("خطا در بررسی عضویت — لطفا بعداً تلاش کنید.")
            return

    # forward message
    try:
        forwarded = await bot.forward_message(chat_id=uid, from_chat_id=row['storage_chat_id'], message_id=row['storage_message_id'])
    except tg_excs.TelegramForbiddenError:
        await msg.answer("⛔ من قادر به ارسال فایل نیستم (احتمالاً شما ربات را بلاک کرده‌اید).")
        return
    except Exception as e:
        log.exception("forward failed: %s", e)
        await msg.answer("خطا در ارسال فایل.")
        return

    # warning message
    timeout_setting = await get_setting('delete_timeout_seconds', str(DEFAULT_DELETE_TIMEOUT))
    try:
        timeout = int(timeout_setting)
    except Exception:
        timeout = DEFAULT_DELETE_TIMEOUT

    warning = await msg.answer(f"⚠️ این پیام و فایل پس از {timeout} ثانیه حذف خواهد شد. سریعاً ذخیره یا فوروارد کنید.")

    # record delivery + increment views
    delivery_id = await record_delivery(row['id'], uid, forwarded.message_id if forwarded else None)
    await increment_file_views(row['id'])

    # schedule delete
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
# Admin panel (simple inline)
# ----------------------------
async def send_admin_panel(chat_id:int):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("📋 لیست لینک‌ها", callback_data="admin:list_links")],
        [InlineKeyboardButton("⏱ تنظیم تایمر حذف", callback_data="admin:set_timer")],
        [InlineKeyboardButton("📣 ارسال همگانی", callback_data="admin:broadcast")],
        [InlineKeyboardButton("🔁 فعال/غیرفعال لینک", callback_data="admin:toggle_link")],
    ])
    await safe_send_message(chat_id, "📌 پنل مدیریت:", reply_markup=kb)

async def admin_list_links(chat_id:int):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, token, created_at, views, active FROM files ORDER BY created_at DESC LIMIT 200")
    if not rows:
        await safe_send_message(chat_id, "هیچ لینکی ثبت نشده.")
        return
    texts = []
    for r in rows:
        texts.append(f"ID: {r['id']}  token: <code>{r['token']}</code>\nviews: {r['views']} active: {r['active']}")
    # send chunked if too long
    chunk = "\n\n".join(texts)
    if len(chunk) > 3900:
        chunk = chunk[:3900] + "\n…"
    await safe_send_message(chat_id, chunk)

# broadcast simple state
_broadcast_waiting_for: Dict[int, bool] = {}

# ----------------------------
# Aiogram handlers (catch-all)
# ----------------------------
@dp.message()
async def all_messages(msg: types.Message):
    text = (msg.text or "").strip()
    uid = msg.from_user.id

    # /start token
    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        if len(parts) == 2:
            token = parts[1].strip()
            await handle_start_message(msg, token)
            return
        await msg.answer("سلام! برای دریافت فایل از لینک‌هایی که به شما داده شده استفاده کنید.")
        return

    # admin
    if text.startswith("/admin"):
        if uid not in ADMIN_IDS:
            await msg.answer("⛔ شما دسترسی ندارید")
            return
        await send_admin_panel(uid)
        return

    # admin setting number => set timer
    if uid in ADMIN_IDS and text.isdigit():
        await set_setting('delete_timeout_seconds', text)
        await msg.answer(f"✅ تایمر حذف روی {text} ثانیه تنظیم شد.")
        return

    # broadcast reply flow
    if _broadcast_waiting_for.get(uid) and uid in ADMIN_IDS:
        content = msg.text or ""
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT user_id FROM users WHERE blocked=false")
        count = 0
        for r in rows:
            try:
                await safe_send_message(r['user_id'], content)
                count += 1
                await asyncio.sleep(0.05)
            except Exception:
                continue
        _broadcast_waiting_for.pop(uid, None)
        await msg.answer(f"✅ پیام ارسال شد تقریبا به {count} کاربر.")
        return

    await msg.answer("✅ Bot is on!")

@dp.callback_query()
async def callback_handler(cq: types.CallbackQuery):
    uid = cq.from_user.id
    if uid not in ADMIN_IDS:
        await cq.answer("⛔ دسترسی ندارید", show_alert=True)
        return
    data = cq.data or ""
    if data == "admin:list_links":
        await admin_list_links(uid)
        await cq.answer()
        return
    if data == "admin:set_timer":
        await cq.answer("عدد ثانیه جدید را ارسال کنید (مثال: 20).", show_alert=True)
        return
    if data == "admin:broadcast":
        _broadcast_waiting_for[uid] = True
        await cq.answer("لطفا پیام را ارسال کنید تا برای همه ارسال شود.", show_alert=True)
        return
    if data == "admin:toggle_link":
        await cq.answer("این قابلیت در نسخه بعدی فعال خواهد شد.", show_alert=True)
        return
    await cq.answer()

# ----------------------------
# FastAPI app & webhook endpoint
# ----------------------------
app = FastAPI()

@app.post(WEBHOOK_PATH)
async def webhook(request: Request):
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)

    # fast path for channel_post: schedule processing in background and reply quickly
    if "channel_post" in data:
        asyncio.create_task(process_channel_post(data))
        # still feed update to dispatcher to keep internal handlers sane
        try:
            update = types.Update(**data)
            await dp.feed_update(bot, update)
        except Exception:
            pass
        return JSONResponse({"ok": True})

    # other updates: feed to aiogram dispatcher
    try:
        update = types.Update(**data)
        await dp.feed_update(bot, update)
    except Exception as e:
        log.exception("feed_update failed: %s", e)
    return JSONResponse({"ok": True})

@app.get("/health")
async def health(x_keepalive_secret: Optional[str] = Header(None)):
    # protect health with secret if set
    if KEEPALIVE_SECRET:
        if not x_keepalive_secret or x_keepalive_secret != KEEPALIVE_SECRET:
            return JSONResponse({"status":"forbidden"}, status_code=403)
    return JSONResponse({"status":"ok"})

# ----------------------------
# Startup / Shutdown
# ----------------------------
@app.on_event("startup")
async def on_startup():
    log.info("Startup: init DB pool")
    await get_db_pool()
    # set webhook
    if WEBHOOK_URL:
        try:
            await bot.delete_webhook(drop_pending_updates=True)
        except Exception:
            pass
        try:
            await bot.set_webhook(WEBHOOK_URL, allowed_updates=["message","channel_post","callback_query","chat_member"])
            log.info("Webhook set: %s", WEBHOOK_URL)
        except Exception as e:
            log.exception("Failed to set webhook: %s", e)
    else:
        log.warning("WEBHOOK_URL not configured; webhook not set")

@app.on_event("shutdown")
async def on_shutdown():
    log.info("Shutting down")
    try:
        await bot.delete_webhook()
    except Exception:
        pass
    await bot.session.close()
    global _db_pool
    if _db_pool:
        await _db_pool.close()

# ----------------------------
# Run (uvicorn via Render start command)
# ----------------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, log_level="info")
