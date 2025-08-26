import os
import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.utils.keyboard import ReplyKeyboardBuilder

# گرفتن توکن و لیست ادمین‌ها از متغیرهای محیطی
TOKEN = os.getenv("BOT_TOKEN")
ADMINS_ENV = os.getenv("ADMIN_IDS", "")  # مثال: "123456789,987654321"

# تبدیل رشته ادمین‌ها به مجموعه‌ای از اعداد
ADMIN_IDS = set()
for part in [p.strip() for p in ADMINS_ENV.split(",") if p.strip()]:
    try:
        ADMIN_IDS.add(int(part))
    except ValueError:
        pass

if not TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is not set")

bot = Bot(token=TOKEN)
dp = Dispatcher()

@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    # بررسی دسترسی
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ شما دسترسی ندارید")
        return

    kb = ReplyKeyboardBuilder()
    kb.button(text="تنظیم تایمر ⏱")
    kb.button(text="ارسال لینک 🎥")
    kb.adjust(2)

    await message.answer("📌 پنل مدیریت:", reply_markup=kb.as_markup(resize_keyboard=True))

@dp.message()
async def echo(message: types.Message):
    # پیام پیش‌فرض برای تست
    await message.answer("✅ Bot is on!")

async def main():
    try:
        await dp.start_polling(bot)
    finally:
        # بستن session بطور مرتب
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
