import os
import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
import asyncpg

logging.basicConfig(level=logging.INFO)

# --- Bot setup ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")
DATABASE_URL = os.getenv("DATABASE_URL")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- Database connection pool ---
async def get_pool():
    return await asyncpg.create_pool(DATABASE_URL)

# --- Commands ---
@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer("✅ Bot is on!")

@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if str(message.from_user.id) == ADMIN_ID:
        await message.answer("✅ You are admin!")
    else:
        await message.answer("⛔ شما دسترسی ندارید")

# --- Init DB command ---
@dp.message(Command("initdb"))
async def init_db_cmd(message: Message):
    if str(message.from_user.id) != ADMIN_ID:
        await message.answer("⛔ شما دسترسی ندارید")
        return

    CREATE_TABLES = """
    CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        telegram_id BIGINT UNIQUE NOT NULL,
        username TEXT,
        joined_at TIMESTAMP DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS links (
        id SERIAL PRIMARY KEY,
        url TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT NOW(),
        expires_in INTEGER DEFAULT 20
    );

    CREATE TABLE IF NOT EXISTS settings (
        id SERIAL PRIMARY KEY,
        name TEXT UNIQUE NOT NULL,
        value TEXT NOT NULL
    );
    """

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(CREATE_TABLES)

    await message.answer("✅ جداول با موفقیت ساخته شدند")

# --- Run bot ---
async def main():
    logging.info("🤖 Bot started")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
