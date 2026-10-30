"""
Telegram-бот коуча Анны Малышевой.
- Приветствие с кнопками «Получить гайд» и «Записаться на сессию».
- Гайд: бот присылает PDF и уведомляет владельца.
- Запись: бот спрашивает имя, контакт и удобное время и присылает заявку владельцу.
- /stats — статистика для владельца, /myid — узнать свой Telegram ID.
"""
import asyncio
import html
import json
import logging
import os
import socket
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    User,
)
from aiogram.utils.chat_action import ChatActionSender

from core import load_answers, find_topic, normalize, topic_files

logging.basicConfig(level=logging.INFO)

TG_MAX_FILE = 50 * 1024 * 1024  # лимит Telegram для отправки ботом
TZ = ZoneInfo(os.getenv("TZ_NAME", "Asia/Yakutsk"))
DATA_DIR = Path(os.getenv("DATA_DIR", Path(__file__).resolve().parent / "data"))
STATS_FILE = DATA_DIR / "stats.json"
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_ID", "").replace(" ", "").split(",") if x.isdigit()}

data = load_answers()
dp = Dispatcher()
_file_ids: dict[str, str] = {}  # кэш file_id, чтобы не загружать файл каждый раз


def bk(key: str) -> str:
    """Тексты записи из answers.json (раздел booking)."""
    return data["booking"][key]


# ---------- клавиатуры ----------

CANCEL = "Отмена"
SKIP = "Пропустить"


def build_keyboard():
    rows = [[KeyboardButton(text=t["title"])] for t in data["topics"]]
    rows.append([KeyboardButton(text=bk("button"))])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


def build_inline():
    rows = [[InlineKeyboardButton(text=t["title"], callback_data=f"topic:{i}")]
            for i, t in enumerate(data["topics"])]
    rows.append([InlineKeyboardButton(text=bk("button"), callback_data="book")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb(*rows):
    return ReplyKeyboardMarkup(keyboard=[list(r) for r in rows], resize_keyboard=True)


keyboard = build_keyboard()
inline_kb = build_inline()


# ---------- статистика ----------

def load_stats() -> dict:
    try:
        return json.loads(STATS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def add_stat(kind: str, user: User):
    stats = load_stats()
    stats.setdefault(kind, []).append({"id": user.id, "t": datetime.now(TZ).isoformat()})
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        STATS_FILE.write_text(json.dumps(stats, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logging.warning("Не удалось сохранить статистику: %s", e)
    return stats


def count(items: list, since: datetime | None = None, unique=False) -> int:
    sel = [x for x in items if since is None or datetime.fromisoformat(x["t"]) >= since]
    return len({x["id"] for x in sel}) if unique else len(sel)


def who(user: User) -> str:
    name = html.escape(user.full_name or "Без имени")
    uname = f"@{user.username}" if user.username else "без username"
    return f'<a href="tg://user?id={user.id}">{name}</a> · {uname}'


async def notify_admins(bot: Bot, text: str):
    if not ADMIN_IDS:
        logging.warning("ADMIN_ID не задан — уведомление не отправлено: %s", text)
        return
    for admin in ADMIN_IDS:
        try:
            await bot.send_message(admin, text, parse_mode="HTML", disable_web_page_preview=True)
        except Exception as e:
            logging.warning("Не удалось отправить уведомление %s: %s", admin, e)


# ---------- гайд ----------

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
        logging.warning("Не найдены файлы: %s", ", ".join(p.name for p in missing))
        await message.answer("⚠️ Файл временно недоступен, попробуйте позже.")


async def give_topic(message: Message, user: User, topic: dict):
    await message.answer(topic["answer"], reply_markup=keyboard, disable_web_page_preview=True)
    await send_topic_files(message, topic)
    if topic.get("files"):
        kind = topic.get("id", "guide")
        stats = add_stat(kind, user)
        total = count(stats[kind], unique=True)
        now = datetime.now(TZ).strftime("%d.%m, %H:%M")
        label = html.escape(topic.get("stat_name", topic["title"]))
        await notify_admins(message.bot, f"🔔 Забрали: {label}\n{who(user)}\n{now} · всего людей: {total}")


# ---------- запись на сессию ----------

class Booking(StatesGroup):
    name = State()
    contact = State()
    time = State()


async def start_booking(message: Message, state: FSMContext, user: User):
    await state.clear()
    await state.set_state(Booking.name)
    default_name = user.first_name or ""
    rows = [[KeyboardButton(text=default_name)]] if default_name else []
    rows.append([KeyboardButton(text=CANCEL)])
    await message.answer(bk("ask_name"),
                         reply_markup=ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True))


@dp.message(StateFilter(Booking), F.text.casefold() == CANCEL.casefold())
async def booking_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(bk("cancelled"), reply_markup=keyboard)


@dp.message(Booking.name, F.text)
async def booking_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip()[:100])
    await state.set_state(Booking.contact)
    await message.answer(bk("ask_contact"), reply_markup=kb(
        [KeyboardButton(text="📱 Отправить мой номер", request_contact=True)],
        [KeyboardButton(text=CANCEL)],
    ))


@dp.message(Booking.contact, F.contact | F.text)
async def booking_contact(message: Message, state: FSMContext):
    contact = message.contact.phone_number if message.contact else message.text.strip()[:100]
    await state.update_data(contact=contact)
    await state.set_state(Booking.time)
    await message.answer(bk("ask_time"), reply_markup=kb(
        [KeyboardButton(text=SKIP)], [KeyboardButton(text=CANCEL)]))


@dp.message(Booking.time, F.text)
async def booking_time(message: Message, state: FSMContext):
    when = "" if message.text.strip().casefold() == SKIP.casefold() else message.text.strip()[:300]
    form = await state.get_data()
    await state.clear()
    user = message.from_user
    add_stat("booking", user)
    await message.answer(bk("done"), reply_markup=keyboard)
    e = html.escape
    await notify_admins(message.bot, (
        "📝 <b>Новая запись на сессию</b>\n"
        f"Имя: {e(form.get('name', ''))}\n"
        f"Контакт: {e(form.get('contact', ''))}\n"
        f"Удобное время / запрос: {e(when) or '—'}\n"
        f"Telegram: {who(user)}\n"
        f"{datetime.now(TZ).strftime('%d.%m.%Y, %H:%M')}"
    ))


@dp.message(StateFilter(Booking))
async def booking_wrong(message: Message):
    await message.answer("Пожалуйста, ответьте текстом или нажмите «Отмена».")


# ---------- команды ----------

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(data["greeting"], reply_markup=inline_kb)


@dp.message(Command("myid"))
async def cmd_myid(message: Message):
    await message.answer(f"Ваш Telegram ID: <code>{message.from_user.id}</code>", parse_mode="HTML")


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return await on_text(message, None)
    s = load_stats()
    now = datetime.now(TZ)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week = now - timedelta(days=7)
    lines = ["📊 Статистика\n"]
    for t in data["topics"]:
        items = s.get(t.get("id", "guide"), [])
        lines.append(f"{t.get('stat_name', t['title'])}: {count(items, unique=True)} чел.\n"
                     f"  сегодня: {count(items, today, True)} · за 7 дней: {count(items, week, True)}\n")
    b = s.get("booking", [])
    lines.append(f"Записей на сессию: {len(b)}\n"
                 f"  сегодня: {count(b, today)} · за 7 дней: {count(b, week)}")
    await message.answer("\n".join(lines))


@dp.message(Command("reload"))
async def cmd_reload(message: Message):
    global data, keyboard, inline_kb
    if ADMIN_IDS and message.from_user.id not in ADMIN_IDS:
        return
    data = load_answers()
    keyboard, inline_kb = build_keyboard(), build_inline()
    _file_ids.clear()
    await message.answer("Ответы и файлы обновлены ✅", reply_markup=keyboard)


# ---------- обычные сообщения и кнопки ----------

def wants_booking(text: str) -> bool:
    msg = normalize(text)
    if msg == normalize(bk("button")):
        return True
    return any(normalize(k) in msg for k in bk("keywords"))


@dp.message(F.text)
async def on_text(message: Message, state: FSMContext | None):
    if state is not None and wants_booking(message.text):
        return await start_booking(message, state, message.from_user)
    reply, topic = find_topic(message.text, data)
    if topic:
        return await give_topic(message, message.from_user, topic)
    markup = inline_kb if reply in (data["greeting"], data["fallback"]) else keyboard
    await message.answer(reply, reply_markup=markup, disable_web_page_preview=True)


@dp.callback_query(F.data == "book")
async def on_book(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await start_booking(callback.message, state, callback.from_user)


@dp.callback_query(F.data.startswith("topic:"))
async def on_button(callback: CallbackQuery):
    await callback.answer()
    try:
        topic = data["topics"][int(callback.data.split(":")[1])]
    except (ValueError, IndexError):
        return await callback.message.answer(data["fallback"], reply_markup=inline_kb)
    await give_topic(callback.message, callback.from_user, topic)


@dp.message()
async def on_other(message: Message):
    await message.answer(data["fallback"], reply_markup=inline_kb)


# ---------- подключение к Telegram ----------

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
    """1) Прокси, если задан (TG_PROXY / OUTBOUND_PROXY_URL / RELAXDEV_PROXY_URL).
    2) Иначе IPv4, а если он недоступен, но доступен IPv6 (как на RelaxDev), — IPv6."""
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
    if not ADMIN_IDS:
        logging.warning("ADMIN_ID не задан: уведомления о записях и гайде никому не придут. "
                        "Напишите боту /myid и добавьте этот номер в переменную ADMIN_ID.")
    bot = Bot(token, session=await build_session())
    print("Telegram-бот запущен. Ctrl+C — остановить.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
