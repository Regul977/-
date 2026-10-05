import asyncio
import json
import logging
import os
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

import holidays
from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    PollAnswer,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# ================== НАСТРОЙКИ ==================

BOT_TOKEN = os.getenv("BOT_TOKEN", "8958604127:AAFuukqFoynjU9naKiU7ipHsn2B71aaAn7o")

ADMIN_IDS = [5890881555, 1281286200]

TIMEZONE = "Europe/Moscow"

CONFIG_FILE = Path("config.json")
ACTIVE_POLL_FILE = Path("active_poll.json")

POLL_QUESTION = "Для тех кто опаздывает"
POLL_OPTIONS = [
    "Я приду к 1 паре",
    "Я опаздываю на 1 пару",
    "Я по заявлению",
]

# Расписание по умолчанию: пн–пт 07:30–09:30, сб–вс выключено
DEFAULT_SCHEDULE = {
    "mon": {"enabled": True,  "start": "07:30", "end": "09:30"},
    "tue": {"enabled": True,  "start": "07:30", "end": "09:30"},
    "wed": {"enabled": True,  "start": "07:30", "end": "09:30"},
    "thu": {"enabled": True,  "start": "07:30", "end": "09:30"},
    "fri": {"enabled": True,  "start": "07:30", "end": "09:30"},
    "sat": {"enabled": False, "start": "10:00", "end": "12:00"},
    "sun": {"enabled": False, "start": "10:00", "end": "12:00"},
}

DEFAULT_CONFIG = {
    "target_chat_id": None,
    "target_thread_id": None,
    "chat_title": None,
    "topic_name": None,
    "skip_holidays": True,
    "last_poll_date": None,
    "schedule": DEFAULT_SCHEDULE,
}

# Порядок дней недели и их названия
DAYS_ORDER = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAYS_RU = {
    "mon": "Понедельник", "tue": "Вторник", "wed": "Среда",
    "thu": "Четверг", "fri": "Пятница", "sat": "Суббота", "sun": "Воскресенье",
}
DAYS_RU_SHORT = {
    "mon": "Пн", "tue": "Вт", "wed": "Ср",
    "thu": "Чт", "fri": "Пт", "sat": "Сб", "sun": "Вс",
}

# Автоудаление
TEMP_MESSAGE_TTL = 10
USER_COMMAND_TTL = 3
SERVICE_MESSAGE_TTL = 10

# Страна для проверки праздников
HOLIDAY_COUNTRY = "RU"

# ================== ЛОГИРОВАНИЕ ==================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)
logging.getLogger("aiogram").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)

router = Router()

# ================== РАБОТА С JSON ==================

def ensure_json_file(path: Path, default: Any) -> None:
    if not path.exists():
        write_json(path, default)
        logger.info("Создан файл %s", path)


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(default, dict) and isinstance(data, dict):
            merged = default.copy()
            merged.update(data)
            return merged
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.error("Ошибка чтения %s: %s", path, e)
        return default


def write_json(path: Path, data: Any) -> None:
    try:
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.error("Ошибка записи %s: %s", path, e)


def read_config() -> Dict[str, Any]:
    """Читает config.json и гарантирует целостность структуры."""
    config = read_json(CONFIG_FILE, DEFAULT_CONFIG)

    # Верхний уровень
    for key, default_val in DEFAULT_CONFIG.items():
        if key not in config:
            config[key] = default_val

    # Вложенный schedule
    schedule = config.get("schedule") or {}
    for day, default_day in DEFAULT_SCHEDULE.items():
        if day not in schedule:
            schedule[day] = dict(default_day)
        else:
            for k, v in default_day.items():
                if k not in schedule[day]:
                    schedule[day][k] = v
    config["schedule"] = schedule

    return config


def save_config(data: Dict[str, Any]) -> None:
    write_json(CONFIG_FILE, data)


def read_active_poll() -> Dict[str, Any]:
    return read_json(ACTIVE_POLL_FILE, {})


def save_active_poll(data: Dict[str, Any]) -> None:
    write_json(ACTIVE_POLL_FILE, data)


# ================== РАБОТА СО ВРЕМЕНЕМ ==================

def parse_time(value: str) -> Optional[dtime]:
    """'07:30' → time(7, 30). None — если формат неверный."""
    if not value or not isinstance(value, str):
        return None
    try:
        parts = value.strip().split(":")
        if len(parts) != 2:
            return None
        h, m = int(parts[0]), int(parts[1])
        if not (0 <= h <= 23 and 0 <= m <= 59):
            return None
        return dtime(h, m)
    except Exception:
        return None


def today_key(dt: Optional[datetime] = None) -> str:
    if dt is None:
        dt = datetime.now(ZoneInfo(TIMEZONE))
    return DAYS_ORDER[dt.weekday()]


# ================== ГОЛОСА ==================

def add_vote_to_active_poll(user: dict, option_ids: list) -> None:
    active = read_active_poll()
    if not active:
        return
    votes = active.get("votes", {})
    user_id = str(user.get("id"))
    if not option_ids:
        votes.pop(user_id, None)
    else:
        votes[user_id] = {
            "username": user.get("username"),
            "first_name": user.get("first_name"),
            "last_name": user.get("last_name"),
            "option_ids": option_ids,
        }
    active["votes"] = votes
    save_active_poll(active)


def format_user_label(vote: dict) -> str:
    username = vote.get("username")
    first_name = (vote.get("first_name") or "").strip()
    last_name = (vote.get("last_name") or "").strip()
    if username:
        return f"@{username}"
    full = f"{first_name} {last_name}".strip()
    return full or "без имени"


# ================== ПРАВА И УВЕДОМЛЕНИЯ ==================

def is_admin(user_id: Optional[int]) -> bool:
    return user_id is not None and user_id in ADMIN_IDS


async def notify_admins(bot: Bot, text: str) -> None:
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text)
        except Exception as e:
            logger.warning("Не удалось отправить сообщение админу %s: %s", admin_id, e)


async def notify_admins_html(bot: Bot, text: str) -> None:
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML")
        except Exception as e:
            logger.warning("Не удалось отправить сообщение админу %s: %s", admin_id, e)


# ================== АВТОУДАЛЕНИЕ ==================

async def _delete_later(message: Message, delay: int) -> None:
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except Exception as e:
        logger.warning("Не удалось удалить сообщение %s: %s", message.message_id, e)


async def send_temp(message: Message, text: str, delete_after: int = TEMP_MESSAGE_TTL,
                    parse_mode: Optional[str] = None) -> None:
    kwargs = {}
    if parse_mode:
        kwargs["parse_mode"] = parse_mode
    sent = await message.answer(text, **kwargs)
    asyncio.create_task(_delete_later(sent, delete_after))


async def delete_user_command(message: Message, delay: int = USER_COMMAND_TTL) -> None:
    asyncio.create_task(_delete_later(message, delay))


async def _delete_service_message(bot: Bot, chat_id: int, message_id: int,
                                  delay: int = SERVICE_MESSAGE_TTL) -> None:
    await asyncio.sleep(delay)
    for attempt in (1, 2):
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
            logger.info("Сервисное сообщение %s удалено", message_id)
            return
        except Exception as e:
            if attempt == 1:
                await asyncio.sleep(0.5)
                continue
            logger.warning("Не удалось удалить сервисное сообщение %s: %s", message_id, e)


# ================== ПРАЗДНИКИ ==================

def is_holiday(check_date: Optional[datetime] = None) -> bool:
    if check_date is None:
        check_date = datetime.now(ZoneInfo(TIMEZONE))
    try:
        country_holidays = holidays.country_holidays(HOLIDAY_COUNTRY, years=check_date.year)
    except Exception as e:
        logger.warning("Не удалось загрузить праздники для %s: %s", HOLIDAY_COUNTRY, e)
        return False
    if check_date.date() in country_holidays:
        logger.info("Сегодня праздник: %s — %s",
                    check_date.date(), country_holidays.get(check_date.date()))
        return True
    return False


# ================== СЕРВИСНЫЕ СООБЩЕНИЯ ТЕМ ==================

@router.message(
    F.forum_topic_created
    | F.forum_topic_closed
    | F.forum_topic_reopened
    | F.forum_topic_edited
    | F.general_forum_topic_hidden
    | F.general_forum_topic_unhidden
)
async def delete_topic_service_message(message: Message, bot: Bot) -> None:
    asyncio.create_task(
        _delete_service_message(bot, message.chat.id, message.message_id, SERVICE_MESSAGE_TTL)
    )


# ================== ОТКРЫТИЕ / ЗАКРЫТИЕ ТЕМЫ ==================

async def open_topic(bot: Bot, chat_id: int, thread_id: int) -> bool:
    try:
        service = await bot.reopen_forum_topic(chat_id=chat_id, message_thread_id=thread_id)
        logger.info("Тема %s открыта", thread_id)
        if service is not None and getattr(service, "message_id", None):
            asyncio.create_task(
                _delete_service_message(bot, chat_id, service.message_id, SERVICE_MESSAGE_TTL)
            )
        return True
    except TelegramBadRequest as e:
        err = str(e).lower()
        if "topic_not_modified" in err or "already" in err:
            logger.info("Тема %s уже открыта", thread_id)
            return True
        logger.warning("Не удалось открыть тему %s: %s", thread_id, e)
        return False


async def close_topic(bot: Bot, chat_id: int, thread_id: int) -> None:
    try:
        service = await bot.close_forum_topic(chat_id=chat_id, message_thread_id=thread_id)
        logger.info("Тема %s закрыта обратно", thread_id)
        if service is not None and getattr(service, "message_id", None):
            asyncio.create_task(
                _delete_service_message(bot, chat_id, service.message_id, SERVICE_MESSAGE_TTL)
            )
    except TelegramBadRequest as e:
        logger.warning("Не удалось закрыть тему %s: %s", thread_id, e)


# ================== ТЕКСТЫ МЕНЮ ==================

def get_welcome_text() -> str:
    return (
        "👋 <b>Привет! Я бот для ежедневных опросов.</b>\n\n"
        "<b>Что я умею:</b>\n"
        "• Отправляю опрос в выбранные дни по расписанию\n"
        "• Автоматически закрываю его в указанное время и присылаю отчёт\n"
        "• В отчёте: количество голосов и <b>кто именно проголосовал</b>\n"
        "• Опрос: <i>«Для тех кто опаздывает»</i>\n"
        "• Варианты: <i>«Я приду к 1 паре», «Я опаздываю на 1 пару», «Я по заявлению»</i>\n"
        "• Голосование <b>не анонимное</b>\n"
        "• Можно пропускать праздники\n\n"
        "<b>Команды:</b>\n"
        "/start — это сообщение и меню\n"
        "/help — то же, что /start\n"
        "/schedule — расписание опросов\n"
        "/set_time &lt;день&gt; &lt;start&gt; &lt;end&gt; — задать время дня\n"
        "    пример: <code>/set_time mon 07:30 09:30</code>\n"
        "    <code>/set_time mon off</code> — выключить день\n"
        "    <code>/set_time mon on</code> — включить день\n"
        "/skip_holidays on|off — пропускать ли праздники\n"
        "/set_topic — выбрать тему для опросов\n"
        "/test_poll — создать опрос прямо сейчас\n"
        "/stop_poll — досрочно завершить опрос\n\n"
        "⚠️ Управление доступно только администраторам.\n\n"
        "Выберите действие кнопкой ниже 👇"
    )


def get_main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📋 Статус", callback_data="menu:status")],
            [InlineKeyboardButton(text="📅 Расписание", callback_data="menu:schedule")],
            [InlineKeyboardButton(text="🎯 Выбрать эту тему", callback_data="menu:set_topic")],
            [
                InlineKeyboardButton(text="🧪 Тестовый опрос", callback_data="menu:test_poll"),
                InlineKeyboardButton(text="⏹️ Завершить опрос", callback_data="menu:stop_poll"),
            ],
        ]
    )


def get_schedule_text(config: Dict[str, Any]) -> str:
    schedule = config.get("schedule", {})
    skip_h = config.get("skip_holidays", True)

    lines = ["📅 <b>Расписание опросов</b>\n"]
    for key in DAYS_ORDER:
        cfg = schedule.get(key, {})
        name = DAYS_RU[key]
        if cfg.get("enabled"):
            lines.append(f"✅ <b>{name}</b>: {cfg.get('start')} — {cfg.get('end')}")
        else:
            lines.append(f"❌ <b>{name}</b>: выключено")
    lines.append("")
    lines.append(f"Пропускать праздники: {'✅ да' if skip_h else '❌ нет'}")
    lines.append("")
    lines.append("Нажмите на день, чтобы включить/выключить.")
    lines.append("Чтобы изменить время — отправьте команду:")
    lines.append("<code>/set_time mon 07:30 09:30</code>")
    return "\n".join(lines)


def get_schedule_keyboard(config: Dict[str, Any]) -> InlineKeyboardMarkup:
    schedule = config.get("schedule", {})
    rows = []

    # Дни недели по 4 в ряд
    row = []
    for key in DAYS_ORDER:
        cfg = schedule.get(key, {})
        prefix = "✅" if cfg.get("enabled") else "❌"
        row.append(InlineKeyboardButton(
            text=f"{prefix} {DAYS_RU_SHORT[key]}",
            callback_data=f"sched:toggle:{key}",
        ))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    # Переключатель праздников
    skip_h = config.get("skip_holidays", True)
    rows.append([InlineKeyboardButton(
        text=f"🎉 Пропускать праздники: {'да' if skip_h else 'нет'}",
        callback_data="sched:toggle_holidays",
    )])

    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ================== КОМАНДЫ ==================

@router.message(Command("start"))
@router.message(Command("help"))
async def cmd_start(message: Message) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        await send_temp(message, "⛔ У вас нет доступа к этому боту.")
        await delete_user_command(message)
        return
    await message.answer(get_welcome_text(), parse_mode="HTML", reply_markup=get_main_menu())
    await delete_user_command(message)


@router.message(Command("schedule"))
async def cmd_schedule(message: Message) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        await send_temp(message, "⛔ У вас нет доступа к этому боту.")
        await delete_user_command(message)
        return
    config = read_config()
    await message.answer(
        get_schedule_text(config),
        parse_mode="HTML",
        reply_markup=get_schedule_keyboard(config),
    )
    await delete_user_command(message)


@router.message(Command("set_time"))
async def cmd_set_time(message: Message) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        await send_temp(message, "⛔ У вас нет доступа к этому боту.")
        await delete_user_command(message)
        return

    if not message.text:
        await send_temp(message, "Ошибка: пустое сообщение.")
        await delete_user_command(message)
        return

    parts = message.text.split()
    # /set_time <day> <start> <end>  ИЛИ  /set_time <day> on|off
    if len(parts) < 3:
        await send_temp(
            message,
            "Формат:\n"
            "/set_time &lt;день&gt; &lt;начало&gt; &lt;конец&gt;\n"
            "/set_time &lt;день&gt; on|off\n\n"
            "Дни: mon, tue, wed, thu, fri, sat, sun\n"
            "Пример: <code>/set_time mon 07:30 09:30</code>",
            parse_mode="HTML",
        )
        await delete_user_command(message)
        return

    day = parts[1].lower()
    if day not in DAYS_ORDER:
        await send_temp(message, f"Неизвестный день: {day}. Используйте: {', '.join(DAYS_ORDER)}")
        await delete_user_command(message)
        return

    config = read_config()
    schedule = config["schedule"]

    # /set_time mon on|off
    if len(parts) == 3 and parts[2].lower() in ("on", "off"):
        schedule[day]["enabled"] = parts[2].lower() == "on"
        config["schedule"] = schedule
        save_config(config)
        state = "включён" if schedule[day]["enabled"] else "выключен"
        await send_temp(message, f"✅ {DAYS_RU[day]} теперь {state}.")
        await delete_user_command(message)
        return

    # /set_time mon HH:MM HH:MM
    if len(parts) < 4:
        await send_temp(message, "Формат: <code>/set_time mon 07:30 09:30</code>", parse_mode="HTML")
        await delete_user_command(message)
        return

    start_str, end_str = parts[2], parts[3]
    start_t = parse_time(start_str)
    end_t = parse_time(end_str)
    if not start_t or not end_t:
        await send_temp(message, "Неверный формат времени. Пример: <code>07:30</code>", parse_mode="HTML")
        await delete_user_command(message)
        return

    if end_t <= start_t:
        await send_temp(message, "Время окончания должно быть позже начала.")
        await delete_user_command(message)
        return

    schedule[day]["start"] = f"{start_t.hour:02d}:{start_t.minute:02d}"
    schedule[day]["end"] = f"{end_t.hour:02d}:{end_t.minute:02d}"
    config["schedule"] = schedule
    save_config(config)

    await send_temp(
        message,
        f"✅ {DAYS_RU[day]}: {schedule[day]['start']} — {schedule[day]['end']}"
        f" ({'включено' if schedule[day]['enabled'] else 'день пока выключен'})",
    )
    await delete_user_command(message)
    logger.info("Обновлено расписание: %s → %s–%s",
                day, schedule[day]["start"], schedule[day]["end"])


@router.message(Command("skip_holidays"))
async def cmd_skip_holidays(message: Message) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        await send_temp(message, "⛔ У вас нет доступа к этому боту.")
        await delete_user_command(message)
        return

    parts = message.text.split() if message.text else []
    if len(parts) < 2 or parts[1].lower() not in ("on", "off"):
        await send_temp(message, "Формат: <code>/skip_holidays on|off</code>", parse_mode="HTML")
        await delete_user_command(message)
        return

    config = read_config()
    config["skip_holidays"] = parts[1].lower() == "on"
    save_config(config)

    state = "включён" if config["skip_holidays"] else "выключен"
    await send_temp(message, f"✅ Пропуск праздников теперь {state}.")
    await delete_user_command(message)


@router.message(Command("set_topic"))
async def cmd_set_topic(message: Message, bot: Bot) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        await send_temp(message, "⛔ У вас нет доступа к этому боту.")
        await delete_user_command(message)
        return
    if message.chat.type not in ("group", "supergroup"):
        await send_temp(message, "Эту команду нужно вызывать в супергруппе с темами.")
        await delete_user_command(message)
        return
    thread_id = message.message_thread_id
    if thread_id is None:
        await send_temp(message, "Не удалось определить тему. Отправьте в нужной теме.")
        await delete_user_command(message)
        return

    topic_name = "Неизвестно"
    if message.text:
        parts = message.text.split(maxsplit=1)
        if len(parts) > 1 and parts[1].strip():
            topic_name = parts[1].strip()
    if topic_name == "Неизвестно" and message.reply_to_message:
        created = getattr(message.reply_to_message, "forum_topic_created", None)
        if created:
            topic_name = created.name

    chat_title = message.chat.title or str(message.chat.id)

    config = read_config()
    config["target_chat_id"] = message.chat.id
    config["target_thread_id"] = thread_id
    config["chat_title"] = chat_title
    config["topic_name"] = topic_name
    save_config(config)

    await send_temp(
        message,
        "✅ Тема для ежедневных опросов сохранена:\n"
        f"Чат: {chat_title}\nТема: {topic_name}\nthread_id: {thread_id}"
    )
    await notify_admins(bot,
        f"Тема для опросов обновлена: {chat_title} / {topic_name} (thread_id={thread_id})")
    await delete_user_command(message)


@router.message(Command("test_poll"))
async def cmd_test_poll(message: Message, bot: Bot) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        await send_temp(message, "⛔ У вас нет доступа к этому боту.")
        await delete_user_command(message)
        return
    await send_temp(message, "Запускаю тестовый опрос...")
    await delete_user_command(message)
    await send_daily_poll(bot, manual=True)


@router.message(Command("stop_poll"))
async def cmd_stop_poll(message: Message, bot: Bot) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        await send_temp(message, "⛔ У вас нет доступа к этому боту.")
        await delete_user_command(message)
        return
    active = read_active_poll()
    if not active.get("chat_id") or not active.get("message_id"):
        await send_temp(message, "ℹ️ Активного опроса сейчас нет.")
        await delete_user_command(message)
        return
    await send_temp(message, "Завершаю опрос досрочно...")
    await delete_user_command(message)
    await stop_daily_poll(bot)


# ================== ГОЛОСОВАНИЕ ==================

@router.poll_answer()
async def on_poll_answer(poll_answer: PollAnswer) -> None:
    active = read_active_poll()
    if not active or active.get("poll_id") != poll_answer.poll_id:
        return
    user = poll_answer.user
    if user is None:
        return
    user_dict = {
        "id": user.id,
        "username": user.username,
        "first_name": user.first_name,
        "last_name": user.last_name,
    }
    add_vote_to_active_poll(user_dict, poll_answer.option_ids)
    logger.info("Голос: user_id=%s (%s), варианты=%s",
                user.id, user_dict.get("username") or user_dict.get("first_name"),
                poll_answer.option_ids)


# ================== ОБРАБОТКА КНОПОК ==================

@router.callback_query(F.data.startswith("menu:"))
async def on_menu_callback(callback: CallbackQuery, bot: Bot) -> None:
    user_id = callback.from_user.id if callback.from_user else None
    if not is_admin(user_id):
        await callback.answer("⛔ У вас нет доступа", show_alert=True)
        return

    action = callback.data.split(":", 1)[1] if callback.data else ""
    msg = callback.message

    if action == "status":
        config = read_config()
        active = read_active_poll()
        if config.get("target_chat_id") and config.get("target_thread_id") is not None:
            text = (
                f"📋 <b>Статус бота</b>\n\n"
                f"Чат: {config.get('chat_title')}\n"
                f"Тема: {config.get('topic_name')}\n"
                f"thread_id: {config.get('target_thread_id')}\n\n"
                f"Расписание — кнопка «📅 Расписание»."
            )
        else:
            text = ("📋 <b>Статус бота</b>\n\n"
                    "⚠️ Тема для опросов ещё не выбрана.\n"
                    "Перейдите в нужную тему и нажмите «🎯 Выбрать эту тему».")
        if active.get("message_id"):
            text += f"\n\n🟢 Активен опрос (message_id={active.get('message_id')})."
            text += f"\nГолосов получено: {len(active.get('votes', {}))}"
        await callback.answer(text, show_alert=True)

    elif action == "schedule":
        config = read_config()
        await callback.answer()
        await bot.send_message(
            chat_id=msg.chat.id,
            text=get_schedule_text(config),
            parse_mode="HTML",
            reply_markup=get_schedule_keyboard(config),
        )

    elif action == "back":
        await callback.answer()
        await bot.send_message(
            chat_id=msg.chat.id,
            text=get_welcome_text(),
            parse_mode="HTML",
            reply_markup=get_main_menu(),
        )

    elif action == "set_topic":
        thread_id = msg.message_thread_id if msg else None
        if thread_id is None:
            await callback.answer("Эту кнопку нужно нажимать внутри темы (Topics).", show_alert=True)
            return
        chat_id = msg.chat.id
        chat_title = msg.chat.title or str(chat_id)
        topic_name = f"topic #{thread_id}"
        config = read_config()
        config["target_chat_id"] = chat_id
        config["target_thread_id"] = thread_id
        config["chat_title"] = chat_title
        config["topic_name"] = topic_name
        save_config(config)
        await notify_admins(bot,
            f"Тема для опросов обновлена: {chat_title} / {topic_name} (thread_id={thread_id})")
        logger.info("Тема выбрана через кнопку: chat_id=%s, thread_id=%s", chat_id, thread_id)
        await callback.answer(f"✅ Тема сохранена:\n{chat_title} / {topic_name}", show_alert=True)

    elif action == "test_poll":
        config = read_config()
        if not config.get("target_chat_id") or config.get("target_thread_id") is None:
            await callback.answer("⚠️ Сначала выберите тему.", show_alert=True)
            return
        await callback.answer("Запускаю тестовый опрос...")
        await send_daily_poll(bot, manual=True)

    elif action == "stop_poll":
        active = read_active_poll()
        if not active.get("chat_id") or not active.get("message_id"):
            await callback.answer("ℹ️ Активного опроса сейчас нет.", show_alert=True)
            return
        await callback.answer("Завершаю опрос досрочно...")
        await stop_daily_poll(bot)

    else:
        await callback.answer("Неизвестное действие", show_alert=True)


@router.callback_query(F.data.startswith("sched:"))
async def on_schedule_callback(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id if callback.from_user else None
    if not is_admin(user_id):
        await callback.answer("⛔ У вас нет доступа", show_alert=True)
        return

    parts = callback.data.split(":", 2)
    action = parts[1] if len(parts) > 1 else ""

    config = read_config()

    if action == "toggle" and len(parts) > 2:
        day = parts[2]
        if day not in DAYS_ORDER:
            await callback.answer("Неизвестный день", show_alert=True)
            return
        schedule = config["schedule"]
        schedule[day]["enabled"] = not schedule[day].get("enabled", False)
        config["schedule"] = schedule
        save_config(config)
        state = "включён" if schedule[day]["enabled"] else "выключен"
        logger.info("День %s %s", day, state)
        await callback.answer(f"{DAYS_RU[day]}: {state}")

    elif action == "toggle_holidays":
        config["skip_holidays"] = not config.get("skip_holidays", True)
        save_config(config)
        state = "включён" if config["skip_holidays"] else "выключен"
        await callback.answer(f"Пропуск праздников: {state}")

    else:
        await callback.answer("Неизвестное действие", show_alert=True)
        return

    # Обновляем сообщение с расписанием
    try:
        await callback.message.edit_text(
            get_schedule_text(config),
            parse_mode="HTML",
            reply_markup=get_schedule_keyboard(config),
        )
    except Exception as e:
        logger.debug("Не удалось обновить сообщение расписания: %s", e)


# ================== ОПРОСЫ ==================

async def send_daily_poll(bot: Bot, manual: bool = False) -> None:
    """
    Создаёт опрос в выбранной теме.
    manual=True — вызвано из /test_poll (не меняет last_poll_date).
    manual=False — вызвано планировщиком (устанавливает last_poll_date).
    """
    config = read_config()
    chat_id = config.get("target_chat_id")
    thread_id = config.get("target_thread_id")

    if not chat_id or thread_id is None:
        logger.warning("Тема для опроса не выбрана")
        if not manual:
            await notify_admins(bot,
                "⚠️ Не выбрана тема для ежедневного опроса.\nОтправьте /set_topic.")
        return

    opened = await open_topic(bot, chat_id, thread_id)
    if not opened:
        await notify_admins(bot,
            "⚠️ Не удалось отправить опрос: не получилось открыть тему.\n"
            "Проверьте, что бот — админ и у него включено право «Управление темами».")
        return

    # Определяем время окончания для этого опроса
    now = datetime.now(ZoneInfo(TIMEZONE))
    day_cfg = config["schedule"].get(today_key(now), {})
    end_t = parse_time(day_cfg.get("end")) if day_cfg else None
    if end_t:
        end_at = now.replace(hour=end_t.hour, minute=end_t.minute, second=0, microsecond=0)
        if end_at <= now:
            # Если время окончания уже прошло — ставим +2 часа
            end_at = now.replace(microsecond=0) + asyncio.timedelta(hours=2) \
                if hasattr(asyncio, "timedelta") else now
    else:
        end_at = now

    try:
        message = await bot.send_poll(
            chat_id=chat_id,
            message_thread_id=thread_id,
            question=POLL_QUESTION,
            options=POLL_OPTIONS,
            is_anonymous=False,
            allows_multiple_answers=False,
            type="regular",
        )

        save_active_poll({
            "chat_id": chat_id,
            "message_id": message.message_id,
            "poll_id": message.poll.id,
            "thread_id": thread_id,
            "chat_title": config.get("chat_title"),
            "topic_name": config.get("topic_name"),
            "date": now.strftime("%Y-%m-%d"),
            "end_at": end_at.isoformat(),
            "votes": {},
        })

        logger.info("Опрос создан: chat_id=%s, message_id=%s (end_at=%s)",
                    chat_id, message.message_id, end_at.isoformat())

        if not manual:
            # Запоминаем, что сегодня опрос уже отправлен
            config["last_poll_date"] = now.strftime("%Y-%m-%d")
            save_config(config)

        await notify_admins(bot,
            f"Опрос создан в чате {config.get('chat_title')} "
            f"в теме {config.get('topic_name')}")

    except TelegramBadRequest as e:
        logger.exception("Ошибка Telegram при создании опроса")
        await notify_admins(bot, f"❌ Ошибка при создании опроса: {e}")
    except Exception as e:
        logger.exception("Неожиданная ошибка при создании опроса")
        await notify_admins(bot, f"❌ Ошибка при создании опроса: {e}")
    finally:
        await close_topic(bot, chat_id, thread_id)


async def stop_daily_poll(bot: Bot) -> None:
    active = read_active_poll()
    chat_id = active.get("chat_id")
    message_id = active.get("message_id")

    if not chat_id or not message_id:
        logger.info("Нет активного опроса для закрытия")
        return

    try:
        poll = await bot.stop_poll(chat_id=chat_id, message_id=message_id)

        votes = active.get("votes", {})
        voters_by_option: Dict[int, list] = {i: [] for i in range(len(poll.options))}
        for user_id, vote in votes.items():
            label = format_user_label(vote)
            for opt_idx in vote.get("option_ids", []):
                if opt_idx in voters_by_option:
                    voters_by_option[opt_idx].append(label)

        lines = []
        for idx, option in enumerate(poll.options):
            voters = voters_by_option.get(idx, [])
            if voters:
                lines.append(f"• <b>{option.text}</b> — {option.voter_count} чел.\n"
                             f"    {', '.join(voters)}")
            else:
                lines.append(f"• <b>{option.text}</b> — 0 чел.")

        results_text = "\n\n".join(lines)
        text = (
            f"📊 <b>Опрос в чате «{active.get('chat_title', chat_id)}» завершен.</b>\n"
            f"Тема: {active.get('topic_name', '—')}\n\n"
            f"Всего проголосовало: <b>{poll.total_voter_count}</b> человек.\n\n"
            f"{results_text}"
        )

        await notify_admins_html(bot, text)
        save_active_poll({})
        logger.info("Опрос закрыт: chat_id=%s, message_id=%s", chat_id, message_id)

    except TelegramBadRequest as e:
        logger.exception("Ошибка Telegram при закрытии опроса")
        await notify_admins(bot, f"❌ Ошибка при закрытии опроса: {e}")
    except Exception as e:
        logger.exception("Неожиданная ошибка при закрытии опроса")
        await notify_admins(bot, f"❌ Ошибка при закрытии опроса: {e}")


# ================== ПЛАНИРОВЩИК ==================

async def scheduler_tick(bot: Bot) -> None:
    """
    Запускается каждую минуту.
    - Если активный опрос и время окончания прошло — закрывает его.
    - Если сегодня день включён, текущее время в окне [start, end),
      опрос ещё не отправлялся сегодня — создаёт его.
    """
    now = datetime.now(ZoneInfo(TIMEZONE))
    config = read_config()

    # --- 1. Закрыть опрос, если пришло время ---
    active = read_active_poll()
    if active.get("message_id"):
        end_at_str = active.get("end_at")
        should_stop = False

        if end_at_str:
            try:
                end_at = datetime.fromisoformat(end_at_str)
                if end_at.tzinfo is None:
                    end_at = end_at.replace(tzinfo=ZoneInfo(TIMEZONE))
                if now >= end_at:
                    should_stop = True
                    logger.info("Достигнуто время окончания опроса (%s)", end_at.isoformat())
            except Exception as e:
                logger.warning("Не удалось разобрать end_at %s: %s", end_at_str, e)

        # Опрос остался с прошлого дня (например, бот был выключен)
        poll_date = active.get("date")
        today = now.strftime("%Y-%m-%d")
        if poll_date and poll_date != today:
            should_stop = True
            logger.info("Опрос с %s — закрываю как устаревший", poll_date)

        if should_stop:
            await stop_daily_poll(bot)
            return

    # --- 2. Проверить, не пора ли начать ---
    day_key = today_key(now)
    day_cfg = config["schedule"].get(day_key, {})

    if not day_cfg.get("enabled"):
        return

    if config.get("skip_holidays", True) and is_holiday(now):
        return

    start_t = parse_time(day_cfg.get("start"))
    end_t = parse_time(day_cfg.get("end"))
    if not start_t or not end_t:
        return

    current_t = now.time()
    if not (start_t <= current_t < end_t):
        return

    # Уже отправляли сегодня?
    today_str = now.strftime("%Y-%m-%d")
    if config.get("last_poll_date") == today_str:
        return

    logger.info("Время опроса по расписанию (%s %s–%s) — создаю",
                DAYS_RU_SHORT[day_key],
                day_cfg.get("start"), day_cfg.get("end"))
    await send_daily_poll(bot, manual=False)


# ================== ПРОВЕРКА ПРИ СТАРТЕ ==================

async def startup_check(bot: Bot) -> None:
    config = read_config()
    if not config.get("target_chat_id") or config.get("target_thread_id") is None:
        await notify_admins(bot,
            "⚠️ Бот запущен, но тема для опросов не выбрана.\n"
            "Отправьте /set_topic в нужной теме.")

    # Прогоняем тик один раз, чтобы подхватить «долгий» опрос
    try:
        await scheduler_tick(bot)
    except Exception as e:
        logger.exception("Ошибка при стартовом тике: %s", e)


# ================== ЗАПУСК ==================

async def main() -> None:
    ensure_json_file(CONFIG_FILE, DEFAULT_CONFIG)
    ensure_json_file(ACTIVE_POLL_FILE, {})

    if BOT_TOKEN == "ВСТАВЬТЕ_ТОКЕН_СЮДА":
        raise RuntimeError("Укажите BOT_TOKEN в переменной окружения BOT_TOKEN или в коде.")

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    scheduler = AsyncIOScheduler(timezone=ZoneInfo(TIMEZONE))

    # Каждый час в :00 секунд — тик планировщика
    scheduler.add_job(
        scheduler_tick,
        CronTrigger(second=0, timezone=ZoneInfo(TIMEZONE)),
        args=[bot],
        id="scheduler_tick",
        replace_existing=True,
        max_instances=1,
    )

    scheduler.start()
    logger.info("Планировщик запущен. Часовой пояс: %s. Тик — раз в минуту.", TIMEZONE)

    await startup_check(bot)

    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен вручную (Ctrl+C). Выход.")
