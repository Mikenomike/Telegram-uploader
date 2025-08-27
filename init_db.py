import asyncio
from db import get_pool, init_db

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
    expires_in INTEGER DEFAULT 20  -- ثانیه، پیش‌فرض ۲۰
);

CREATE TABLE IF NOT EXISTS settings (
    id SERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    value TEXT NOT NULL
);
"""

async def main():
    await init_db()
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(CREATE_TABLES)
        print("✅ Tables created successfully")

if __name__ == "__main__":
    asyncio.run(main())
