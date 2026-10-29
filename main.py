"""
Telegram-бот коуча Анны Малышевой.
На кнопку или вопрос отправляет текст ответа и файлы темы
(презентации, PDF, документы) из answers.json.
"""
import asyncio
import logging
import os
import socket

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from aiogram.utils.chat_action import ChatActionSender

from core import load_answers, find_topic, topic_files

logging.basicConfig(level=logging.INFO)

TG_MAX_FILE = 50 * 1024 * 1024  # лимит Telegram для отправки ботом

data = load_answers()
dp = Dispatcher()

# Кэш file_id: после первой загрузки файл повторно не отправляется на сервер
_file_ids: dict[str, str] = {}


def build_keyboard():
    titles = [t["title"] for t in data["topics"]]
    rows = [[KeyboardButton(text=t)] for t in titles]  # по одной кнопке в строке
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


def build_inline():
    """Кнопки прямо под приветствием."""
    rows = [[InlineKeyboardButton(text=t["title"], callback_data=f"topic:{i}")]
            for i, t in enumerate(data["topics"])]
    return InlineKeyboardMarkup(inline_keyboard=rows)


keyboard = build_keyboard()
inline_kb = build_inline()


async def send_topic_files(message: Message, topic: dict):
    files, missing = topic_files(topic)
    if not files and not missing:
        return
    async with ChatActionSender.upload_document(bot=message.bot, chat_id=message.chat.id):
        for path, name in files:
            key = str(path)
            if path.stat().st_size > TG_MAX_FILE:
                await message.answer(f"⚠️ Файл {name} больше 50 МБ — Telegram не даёт боту его отправить.")
                continue
            doc = _file_ids.get(key) or FSInputFile(path, filename=name)
            sent = await message.answer_document(doc)
            if sent.document:
                _file_ids[key] = sent.document.file_id
    if missing:
        names = ", ".join(p.name for p in missing)
        logging.warning("Не найдены файлы: %s", names)
        await message.answer("⚠️ Часть файлов временно недоступна, сообщите администратору.")


@dp.message(Command("reload"))
async def reload_answers(message: Message):
    global data, keyboard, inline_kb
    data = load_answers()
    keyboard = build_keyboard()
    inline_kb = build_inline()
    _file_ids.clear()
    await message.answer("Ответы и файлы обновлены ✅", reply_markup=keyboard)


@dp.message(F.text)
async def on_text(message: Message):
    reply, topic = find_topic(message.text, data)
    # Приветствие и «не понял» — с кнопкой прямо под сообщением
    markup = inline_kb if reply in (data["greeting"], data["fallback"]) else keyboard
    await message.answer(reply, reply_markup=markup, disable_web_page_preview=True)
    if topic:
        await send_topic_files(message, topic)


@dp.callback_query(F.data.startswith("topic:"))
async def on_button(callback: CallbackQuery):
    await callback.answer()
    try:
        topic = data["topics"][int(callback.data.split(":")[1])]
    except (ValueError, IndexError):
        await callback.message.answer(data["fallback"], reply_markup=inline_kb)
        return
    await callback.message.answer(topic["answer"], reply_markup=keyboard)
    await send_topic_files(callback.message, topic)


@dp.message()
async def on_other(message: Message):
    await message.answer(data["fallback"], reply_markup=inline_kb)


TG_HOST = "api.telegram.org"


async def _can_connect(family: int, timeout: float = 6.0) -> bool:
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(TG_HOST, 443, family=family), timeout
        )
        writer.close()
        return True
    except Exception:
        return False


async def build_session() -> AiohttpSession:
    """Настраивает подключение к Telegram.
    1) Если задан прокси (OUTBOUND_PROXY_URL / RELAXDEV_PROXY_URL / TG_PROXY) — через него.
    2) Иначе проверяет, доступен ли Telegram по IPv4; если нет, но доступен по IPv6
       (так на серверах RelaxDev) — работает только через IPv6."""
    proxy = (os.getenv("TG_PROXY") or os.getenv("OUTBOUND_PROXY_URL")
             or os.getenv("RELAXDEV_PROXY_URL"))
    if proxy:
        logging.info("Подключение к Telegram через прокси")
        return AiohttpSession(proxy=proxy)

    session = AiohttpSession()
    if await _can_connect(socket.AF_INET):
        logging.info("Подключение к Telegram по IPv4")
    elif await _can_connect(socket.AF_INET6):
        logging.info("IPv4 недоступен, подключение к Telegram по IPv6")
        session._connector_init["family"] = socket.AF_INET6
    else:
        logging.error("Telegram недоступен ни по IPv4, ни по IPv6 — проверьте сеть сервера")
    return session


async def main():
    token = os.getenv("BOT_TOKEN") or os.getenv("TG_TOKEN")
    if not token:
        raise SystemExit("Укажите переменную окружения BOT_TOKEN в панели RelaxDev")
    bot = Bot(token, session=await build_session())
    print("Telegram-бот запущен. Ctrl+C — остановить.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
