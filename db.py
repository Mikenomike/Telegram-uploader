import asyncpg
import os

# گرفتن آدرس دیتابیس از Environment Variables
DATABASE_URL = os.getenv("DATABASE_URL")

# نگه داشتن pool اتصال‌ها
pool = None

async def init_db():
    global pool
    if pool is None:
        pool = await asyncpg.create_pool(DATABASE_URL)

async def get_pool():
    global pool
    if pool is None:
        await init_db()
    return pool
