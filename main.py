from aiogram import Bot, Dispatcher, types
import asyncio
import os

# توکن ربات رو از متغیر محیطی می‌گیریم
TOKEN = os.getenv("BOT_TOKEN")

bot = Bot(token=TOKEN)
dp = Dispatcher()

@dp.message()
async def echo(message: types.Message):
    await message.answer("✅ Bot is on!")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
