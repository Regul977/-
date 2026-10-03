import asyncio
import json
import logging
import os
from datetime import datetime
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

BOT_TOKEN = os.getenv("BOT_TOKEN", "8963149421:AAHn1fHaHY-aRrvToq0lxdScWji4gX128_4")

# Список ID администраторов (узнать свой — @userinfobot)
ADMIN_IDS = [5890881555, 1281286200]

# Часовой пояс: Europe/Moscow, Europe/Kyiv, Asia/Almaty и т. д.
TIMEZONE = "Europe/Moscow"

# Файлы JSON
CONFIG_FILE = Path("config.json")
ACTIVE_POLL_FILE = Path("active_poll.json")

DEFAULT_CONFIG = {
    "target_chat_id": None,
    "target_thread_id": None,
    "chat_title": None,
    "topic_name": None,
}

# Текст опроса
POLL_QUESTION = "Для тех кто опаздывает"
POLL_OPTIONS = [
    "Я приду к 1 паре",
    "Я опаздываю на 1 пару",
    "Я по заявлению",
]

# Время жизни служебных сообщений бота (секунды)
TEMP_MESSAGE_TTL = 10

# Время жизни команды пользователя (секунды)
USER_COMMAND_TTL = 3

# Время жизни сервисных сообщений Telegram о темах (секунды)
SERVICE_MESSAGE_TTL = 10

# Страна для проверки праздников: RU, UA, KZ, BY, US, DE и т. д.
HOLIDAY_COUNTRY = "RU"

# Игнорировать проверку рабочего дня (True — удобно для отладки)
SKIP_WORKDAY_CHECK = False

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
    return read_json(CONFIG_FILE, DEFAULT_CONFIG)


def save_config(data: Dict[str, Any]) -> None:
    write_json(CONFIG_FILE, data)


def read_active_poll() -> Dict[str, Any]:
    return read_json(ACTIVE_POLL_FILE, {})


def save_active_poll(data: Dict[str, Any]) -> None:
    write_json(ACTIVE_POLL_FILE, data)


# ================== ГОЛОСА ==================

def add_vote_to_active_poll(user: dict, option_ids: list) -> None:
    """Сохраняет/обновляет голос пользователя в active_poll.json."""
    active = read_active_poll()
    if not active:
        return

    votes = active.get("votes", {})
    user_id = str(user.get("id"))

    if not option_ids:
        votes.pop(user_id, None)  # пользователь отозвал голос
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
    """@username, если есть, иначе «Имя Фамилия», иначе «без имени»."""
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


async def send_temp(message: Message, text: str, delete_after: int = TEMP_MESSAGE_TTL) -> None:
    sent = await message.answer(text)
    asyncio.create_task(_delete_later(sent, delete_after))


async def delete_user_command(message: Message, delay: int = USER_COMMAND_TTL) -> None:
    asyncio.create_task(_delete_later(message, delay))


async def _delete_service_message(
    bot: Bot,
    chat_id: int,
    message_id: int,
    delay: int = SERVICE_MESSAGE_TTL,
) -> None:
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


# ================== ПРОВЕРКА РАБОЧЕГО ДНЯ ==================

def is_workday(check_date: Optional[datetime] = None) -> bool:
    """Будний день и не праздник."""
    if check_date is None:
        check_date = datetime.now(ZoneInfo(TIMEZONE))

    if check_date.weekday() >= 5:
        logger.info("Сегодня выходной (%s) — опрос не отправляется", check_date.strftime("%A"))
        return False

    try:
        country_holidays = holidays.country_holidays(HOLIDAY_COUNTRY, years=check_date.year)
    except Exception as e:
        logger.warning("Не удалось загрузить праздники для %s: %s", HOLIDAY_COUNTRY, e)
        return True

    if check_date.date() in country_holidays:
        logger.info(
            "Сегодня праздник: %s — %s",
            check_date.date(),
            country_holidays.get(check_date.date()),
        )
        return False

    return True


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


# ================== ПРИВЕТСТВИЕ И МЕНЮ ==================

def get_welcome_text() -> str:
    return (
        "👋 <b>Привет! Я бот для ежедневных опросов.</b>\n\n"
        "<b>Что я умею:</b>\n"
        "• Каждый день <b>по будням</b> в <b>7:30</b> отправляю опрос в выбранную тему\n"
        "• В <b>9:30</b> автоматически закрываю его и присылаю отчёт админам\n"
        "• В отчёте: количество голосов и <b>кто именно проголосовал</b>\n"
        "• Опрос: <i>«Для тех кто опаздывает»</i>\n"
        "• Варианты: <i>«Я приду к 1 паре», «Я опаздываю на 1 пару», «Я по заявлению»</i>\n"
        "• Голосование <b>не анонимное</b> — видно, кто как ответил\n"
        "• Опрос <b>не отправляется</b> в выходные и праздники\n\n"
        "<b>Команды:</b>\n"
        "/start — показать это сообщение и меню\n"
        "/help — то же, что /start\n"
        "/set_topic — выбрать тему для опросов (вызывать внутри нужной темы)\n"
        "    можно с названием: <code>/set_topic 1 пара</code>\n"
        "/test_poll — создать опрос прямо сейчас (для теста)\n"
        "/stop_poll — досрочно завершить активный опрос\n\n"
        "⚠️ Управление доступно только администраторам.\n\n"
        "Выберите действие кнопкой ниже 👇"
    )


def get_main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📋 Статус", callback_data="menu:status")],
            [InlineKeyboardButton(text="🎯 Выбрать эту тему", callback_data="menu:set_topic")],
            [
                InlineKeyboardButton(text="🧪 Тестовый опрос", callback_data="menu:test_poll"),
                InlineKeyboardButton(text="⏹️ Завершить опрос", callback_data="menu:stop_poll"),
            ],
        ]
    )


# ================== КОМАНДЫ ==================

@router.message(Command("start"))
@router.message(Command("help"))
async def cmd_start(message: Message) -> None:
    """Приветствие + меню. Приветствие НЕ удаляется."""
    if not is_admin(message.from_user.id if message.from_user else None):
        await send_temp(message, "⛔ У вас нет доступа к этому боту.")
        await delete_user_command(message)
        return

    await message.answer(
        get_welcome_text(),
        parse_mode="HTML",
        reply_markup=get_main_menu(),
    )
    await delete_user_command(message)


@router.message(Command("set_topic"))
async def cmd_set_topic(message: Message, bot: Bot) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        await send_temp(message, "⛔ У вас нет доступа к этому боту.")
        await delete_user_command(message)
        return

    if message.chat.type not in ("group", "supergroup"):
        await send_temp(message, "Эту команду нужно вызывать в супергруппе с включёнными темами.")
        await delete_user_command(message)
        return

    thread_id = message.message_thread_id
    if thread_id is None:
        await send_temp(
            message,
            "Не удалось определить тему.\n"
            "Отправьте команду внутри нужной темы (Topics)."
        )
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

    save_config({
        "target_chat_id": message.chat.id,
        "target_thread_id": thread_id,
        "chat_title": chat_title,
        "topic_name": topic_name,
    })

    await send_temp(
        message,
        "✅ Тема для ежедневных опросов сохранена:\n"
        f"Чат: {chat_title}\n"
        f"Тема: {topic_name}\n"
        f"thread_id: {thread_id}"
    )

    await notify_admins(
        bot,
        f"Тема для опросов обновлена: {chat_title} / {topic_name} (thread_id={thread_id})"
    )

    logger.info(
        "Сохранена тема: chat_id=%s, thread_id=%s, topic=%s",
        message.chat.id, thread_id, topic_name,
    )
    await delete_user_command(message)


@router.message(Command("test_poll"))
async def cmd_test_poll(message: Message, bot: Bot) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        await send_temp(message, "⛔ У вас нет доступа к этому боту.")
        await delete_user_command(message)
        return

    await send_temp(message, "Запускаю тестовый опрос...")
    await delete_user_command(message)
    await send_daily_poll(bot, force=True)


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
    """Ловит голоса в нашем неанонимном опросе и сохраняет их в active_poll.json."""
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

    logger.info(
        "Голос: user_id=%s (%s), варианты=%s",
        user.id,
        user_dict.get("username") or user_dict.get("first_name"),
        poll_answer.option_ids,
    )


# ================== ОБРАБОТКА КНОПОК МЕНЮ ==================

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
                f"Опрос отправляется по будням в 7:30, закрывается в 9:30.\n"
                f"В выходные и праздники опрос не отправляется."
            )
        else:
            text = (
                "📋 <b>Статус бота</b>\n\n"
                "⚠️ Тема для опросов ещё не выбрана.\n"
                "Перейдите в нужную тему форума и нажмите "
                "«🎯 Выбрать эту тему» или отправьте /set_topic."
            )

        if active.get("message_id"):
            text += f"\n\n🟢 Сейчас активен опрос (message_id={active.get('message_id')})."
            text += f"\nГолосов получено: {len(active.get('votes', {}))}"

        await callback.answer(text, show_alert=True)

    elif action == "set_topic":
        thread_id = msg.message_thread_id if msg else None
        if thread_id is None:
            await callback.answer("Эту кнопку нужно нажимать внутри темы (Topics).", show_alert=True)
            return

        chat_id = msg.chat.id
        chat_title = msg.chat.title or str(chat_id)
        topic_name = f"topic #{thread_id}"

        save_config({
            "target_chat_id": chat_id,
            "target_thread_id": thread_id,
            "chat_title": chat_title,
            "topic_name": topic_name,
        })

        await notify_admins(
            bot,
            f"Тема для опросов обновлена: {chat_title} / {topic_name} (thread_id={thread_id})"
        )
        logger.info("Тема выбрана через кнопку: chat_id=%s, thread_id=%s", chat_id, thread_id)
        await callback.answer(f"✅ Тема сохранена:\n{chat_title} / {topic_name}", show_alert=True)

    elif action == "test_poll":
        config = read_config()
        if not config.get("target_chat_id") or config.get("target_thread_id") is None:
            await callback.answer("⚠️ Сначала выберите тему (кнопка «🎯 Выбрать эту тему»).", show_alert=True)
            return

        await callback.answer("Запускаю тестовый опрос...")
        await send_daily_poll(bot, force=True)

    elif action == "stop_poll":
        active = read_active_poll()
        if not active.get("chat_id") or not active.get("message_id"):
            await callback.answer("ℹ️ Активного опроса сейчас нет.", show_alert=True)
            return

        await callback.answer("Завершаю опрос досрочно...")
        await stop_daily_poll(bot)

    else:
        await callback.answer("Неизвестное действие", show_alert=True)


# ================== ОПРОСЫ ==================

async def send_daily_poll(bot: Bot, force: bool = False) -> None:
    """
    Создаёт ежедневный опрос в выбранной теме.
    Перед отправкой открывает тему, после — закрывает.
    force=True используется для /test_poll — игнорирует проверку рабочего дня.
    """
    if not force and not SKIP_WORKDAY_CHECK and not is_workday():
        logger.info("Сегодня выходной или праздник — опрос не отправляется")
        return

    config = read_config()

    chat_id = config.get("target_chat_id")
    thread_id = config.get("target_thread_id")

    if not chat_id or thread_id is None:
        logger.warning("Тема для опроса не выбрана")
        await notify_admins(
            bot,
            "⚠️ Не выбрана тема для ежедневного опроса.\n"
            "Отправьте /set_topic в нужной теме форума."
        )
        return

    opened = await open_topic(bot, chat_id, thread_id)
    if not opened:
        await notify_admins(
            bot,
            "⚠️ Не удалось отправить опрос: не получилось открыть тему.\n"
            "Проверьте, что бот — админ и у него включено право «Управление темами»."
        )
        return

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
            "votes": {},
        })

        logger.info("Опрос создан: chat_id=%s, message_id=%s", chat_id, message.message_id)

        await notify_admins(
            bot,
            f"Опрос создан в чате {config.get('chat_title')} "
            f"в теме {config.get('topic_name')}"
        )

    except TelegramBadRequest as e:
        logger.exception("Ошибка Telegram при создании опроса")
        await notify_admins(bot, f"❌ Ошибка при создании опроса: {e}")
    except Exception as e:
        logger.exception("Неожиданная ошибка при создании опроса")
        await notify_admins(bot, f"❌ Ошибка при создании опроса: {e}")
    finally:
        await close_topic(bot, chat_id, thread_id)


async def stop_daily_poll(bot: Bot) -> None:
    """Закрывает опрос и присылает подробный отчёт с юзернеймами."""
    active = read_active_poll()

    chat_id = active.get("chat_id")
    message_id = active.get("message_id")

    if not chat_id or not message_id:
        logger.info("Нет активного опроса для закрытия")
        await notify_admins(bot, "ℹ️ В 9:30 не было активного опроса для закрытия.")
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
                lines.append(
                    f"• <b>{option.text}</b> — {option.voter_count} чел.\n"
                    f"    {', '.join(voters)}"
                )
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


# ================== ПРОВЕРКА ПРИ СТАРТЕ ==================

async def startup_check(bot: Bot) -> None:
    config = read_config()

    if not config.get("target_chat_id") or config.get("target_thread_id") is None:
        await notify_admins(
            bot,
            "⚠️ Бот запущен, но тема для опросов не выбрана.\n"
            "Отправьте /set_topic в нужной теме."
        )

    active = read_active_poll()
    if active.get("chat_id") and active.get("message_id"):
        now = datetime.now(ZoneInfo(TIMEZONE))
        if now.hour > 9 or (now.hour == 9 and now.minute >= 30):
            logger.info("Найден активный опрос после 9:30. Закрываю...")
            await stop_daily_poll(bot)
        else:
            logger.info("Найден активный опрос. Он будет закрыт в 9:30.")


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

    # Пн–пт в 7:30 — создание опроса
    scheduler.add_job(
        send_daily_poll,
        CronTrigger(day_of_week="mon-fri", hour=7, minute=30, timezone=ZoneInfo(TIMEZONE)),
        args=[bot], id="send_daily_poll", replace_existing=True,
    )

    # Ежедневно в 9:30 — закрытие опроса и отчёт
    scheduler.add_job(
        stop_daily_poll,
        CronTrigger(hour=9, minute=30, timezone=ZoneInfo(TIMEZONE)),
        args=[bot], id="stop_daily_poll", replace_existing=True,
    )

    scheduler.start()
    logger.info(
        "Планировщик запущен. Часовой пояс: %s. Опросы: пн–пт в 7:30, кроме праздников (%s).",
        TIMEZONE, HOLIDAY_COUNTRY,
    )

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
