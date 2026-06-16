import os
import re
import logging
import asyncio
from datetime import date
from io import BytesIO

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message

from google import genai
from google.genai import types as genai_types

# ── Логирование ──────────────────────────────────────────────────────────────
os.makedirs("/app/data", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("/app/data/bot.log", encoding="utf-8"),
    ]
)
logger = logging.getLogger(__name__)

# ── Переменные окружения ──────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not TELEGRAM_TOKEN or not GEMINI_API_KEY:
    raise RuntimeError("Не заданы переменные окружения TELEGRAM_TOKEN и/или GEMINI_API_KEY")

# ── Gemini клиент ─────────────────────────────────────────────────────────────
gemini_client = genai.Client(
    api_key=GEMINI_API_KEY,
    http_options={"headers": {"x-goog-user-project": ""}},
)
GEMINI_MODEL = "gemini-2.5-flash"

# ── База данных в памяти ──────────────────────────────────────────────────────
user_db: dict[int, dict] = {}

# ── FSM состояния ─────────────────────────────────────────────────────────────
class Survey(StatesGroup):
    weight = State()
    height = State()
    age = State()
    goal_weight = State()

# ── Формула Миффлина-Сан Жеора ────────────────────────────────────────────────
def calc_daily_norm(weight: float, height: float, age: int) -> int:
    bmr = 10 * weight + 6.25 * height - 5 * age
    return round(bmr * 1.4)

# ── Сброс остатка при новом дне ───────────────────────────────────────────────
def refresh_daily_remaining(user_id: int) -> None:
    data = user_db[user_id]
    today = date.today()
    if data.get("last_date") != today:
        data["remaining"] = data["daily_norm"]
        data["last_date"] = today

# ── Aiogram ──────────────────────────────────────────────────────────────────
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "👋 Привет! Я помогу считать калории по фото еды.\n\n"
        "Сначала заполним небольшую анкету.\n\n"
        "⚖️ Введи свой текущий вес (кг), например: <b>75</b>",
        parse_mode="HTML",
    )
    await state.set_state(Survey.weight)

@dp.message(Survey.weight)
async def survey_weight(message: Message, state: FSMContext) -> None:
    try:
        weight = float(message.text.replace(",", "."))
        if not (20 <= weight <= 300):
            raise ValueError
    except (ValueError, AttributeError):
        await message.answer("⚠️ Введи корректный вес (от 20 до 300 кг):")
        return
    await state.update_data(weight=weight)
    await message.answer("📏 Введи свой рост (см), например: <b>175</b>", parse_mode="HTML")
    await state.set_state(Survey.height)

@dp.message(Survey.height)
async def survey_height(message: Message, state: FSMContext) -> None:
    try:
        height = float(message.text.replace(",", "."))
        if not (100 <= height <= 250):
            raise ValueError
    except (ValueError, AttributeError):
        await message.answer("⚠️ Введи корректный рост (от 100 до 250 см):")
        return
    await state.update_data(height=height)
    await message.answer("🎂 Введи свой возраст (лет), например: <b>30</b>", parse_mode="HTML")
    await state.set_state(Survey.age)

@dp.message(Survey.age)
async def survey_age(message: Message, state: FSMContext) -> None:
    try:
        age = int(message.text.strip())
        if not (10 <= age <= 120):
            raise ValueError
    except (ValueError, AttributeError):
        await message.answer("⚠️ Введи корректный возраст (от 10 до 120 лет):")
        return
    await state.update_data(age=age)
    await message.answer("🎯 Введи целевой вес (кг), например: <b>68</b>", parse_mode="HTML")
    await state.set_state(Survey.goal_weight)

@dp.message(Survey.goal_weight)
async def survey_goal_weight(message: Message, state: FSMContext) -> None:
    try:
        goal_weight = float(message.text.replace(",", "."))
        if not (20 <= goal_weight <= 300):
            raise ValueError
    except (ValueError, AttributeError):
        await message.answer("⚠️ Введи корректный целевой вес (от 20 до 300 кг):")
        return

    data = await state.get_data()
    weight: float = data["weight"]
    height: float = data["height"]
    age: int = data["age"]

    daily_norm = calc_daily_norm(weight, height, age)

    user_db[message.from_user.id] = {
        "weight": weight,
        "height": height,
        "age": age,
        "goal_weight": goal_weight,
        "daily_norm": daily_norm,
        "remaining": daily_norm,
        "last_date": date.today(),
    }

    await state.clear()
    await message.answer(
        f"✅ <b>Профиль сохранён!</b>\n\n"
        f"⚖️ Текущий вес: {weight} кг\n"
        f"📏 Рост: {height} см\n"
        f"🎂 Возраст: {age} лет\n"
        f"🎯 Цель: {goal_weight} кг\n\n"
        f"🔥 Твоя суточная норма: <b>{daily_norm} ккал</b>\n\n"
        f"Теперь отправляй фото еды — я посчитаю калории! 📸",
        parse_mode="HTML",
    )

@dp.message(F.photo)
async def handle_photo(message: Message) -> None:
    user_id = message.from_user.id

    if user_id not in user_db:
        await message.answer("👋 Сначала пройди анкету — напиши /start")
        return

    refresh_daily_remaining(user_id)
    await message.answer("🔍 Анализирую фото, подожди секунду...")

    try:
        # Скачиваем фото
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        logger.info("Скачиваю файл: %s", file.file_path)

        file_bytes_io = BytesIO()
        await bot.download_file(file.file_path, destination=file_bytes_io)
        image_bytes = file_bytes_io.getvalue()
        logger.info("Фото скачано, размер: %d байт", len(image_bytes))

        # Отправляем в Gemini
        prompt = (
            "Ты профессиональный диетолог. Оцени блюдо на фото. "
            "Напиши ТОЛЬКО краткое название еды и примерные калории в формате: "
            "Название - ХХХ ккал. Будь максимально лаконичен, без лишнего текста."
        )

        logger.info("Отправляю запрос в Gemini...")
        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[
                genai_types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                genai_types.Part.from_text(text=prompt),
            ],
        )

        gemini_text = response.text.strip()
        logger.info("Gemini ответ: %s", gemini_text)

        # Парсим калории
        calories_eaten = None
        food_name = gemini_text

        # Попытка 1: формат "Название - ХХХ ккал"
        if " - " in gemini_text:
            try:
                parts = gemini_text.split(" - ", 1)
                food_name = parts[0].strip()
                numbers = re.findall(r'\d+', parts[1])
                if numbers:
                    calories_eaten = int(numbers[0])
            except Exception:
                pass

        # Попытка 2: найти число рядом со словом ккал
        if calories_eaten is None:
            numbers = re.findall(r'(\d+)\s*(?:ккал|kal|cal|калор)', gemini_text, re.IGNORECASE)
            if numbers:
                calories_eaten = int(numbers[0])
                food_name = gemini_text.split('\n')[0].strip()

        # Попытка 3: первое число >= 50
        if calories_eaten is None:
            all_numbers = [int(n) for n in re.findall(r'\d+', gemini_text) if int(n) >= 50]
            if all_numbers:
                calories_eaten = all_numbers[0]
                food_name = gemini_text.split('\n')[0].strip()

        logger.info("Распознано: %s = %s ккал", food_name, calories_eaten)

        if calories_eaten is None:
            await message.answer(
                f"🍽 Gemini ответил:\n<i>{gemini_text}</i>\n\n"
                "⚠️ Не удалось распознать калории. Попробуй другое фото.",
                parse_mode="HTML",
            )
            return

        data = user_db[user_id]
        data["remaining"] -= calories_eaten
        remaining = data["remaining"]
        daily_norm = data["daily_norm"]

        if remaining >= 0:
            status_line = f"✅ Осталось на сегодня: <b>{remaining} ккал</b>"
        else:
            over = abs(remaining)
            status_line = f"⚠️ Норма превышена на <b>{over} ккал</b>!"

        await message.answer(
            f"🍽 <b>{food_name}</b>\n"
            f"🔥 Калорий: <b>{calories_eaten} ккал</b>\n\n"
            f"📊 Суточная норма: {daily_norm} ккал\n"
            f"{status_line}",
            parse_mode="HTML",
        )

    except Exception as e:
        logger.exception("Ошибка при обработке фото для user %s: %s", user_id, str(e))
        await message.answer(
            f"❌ Ошибка: <code>{str(e)[:200]}</code>\n\nПопробуй ещё раз.",
            parse_mode="HTML",
        )

@dp.message(F.text)
async def handle_text(message: Message, state: FSMContext) -> None:
    current_state = await state.get_state()
    if current_state is not None:
        return
    await message.answer(
        "📸 Отправь фото еды, чтобы посчитать калории.\n"
        "Или /start, чтобы заново заполнить анкету."
    )

async def main() -> None:
    logger.info("Бот запущен")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
