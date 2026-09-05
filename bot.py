# bot.py
import asyncio
import json
import logging
import os
import re
import sys
import random
import hashlib
import sqlite3
import unicodedata
from html import escape as html_escape
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytz
from dotenv import load_dotenv

from task_answer_match import task_answer_matches

from aiogram import Bot, Dispatcher, Router, F, BaseMiddleware
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import (
    TelegramForbiddenError,
    TelegramBadRequest,
)
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
    BotCommand, BotCommandScopeDefault, BotCommandScopeChat,
    FSInputFile, BufferedInputFile,
    ErrorEvent,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from fsm_storage import SQLiteStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import StorageKey

from db import BACKUP_DIR, DB_PATH, LOG_FILE, ensure_persistent_dirs, get_db, init_db
from repository import (
    UserRepository, SessionRepository, AdminRepository, FlashcardRepository,
    UserFlashcardRepository, UserTaskRepository, TipsRepository,
    McqProgressRepository, TaskProgressRepository, SubjectStatsRepository,
    EventRepository, PetRepository, LeaderboardRepository, FriendRepository,
    PlanRepository, DeviceRepository, DesktopTimerRepository,
)
from services import (
    AchievementService, StudyService, StreakService, ReminderService,
    BackupService, AnalyticsService, LeaderboardService, UserRateLimiter, sm2_update,
    freeze_cost, parse_friend_query, derive_emotion, render_pet,
    _send_with_retry_after, send_with_telegram_bulkhead,
)
from tasks import streak_scheduler, reminder_scheduler, leaderboard_scheduler
from api import api_enabled, create_app as create_api_app, start_api_server
from user_task_txt import parse_user_tasks_txt
from file_upload_security import (
    decode_task_upload,
    FRIEND_QUERY_MAX_LEN,
    resolve_path_under,
    safe_subject_dir,
    safe_task_image_filename,
    sanitize_plain_preview,
    SUPPORT_MESSAGE_MAX_LEN,
    TELEGRAM_MAX_MESSAGE_LEN,
    truncate_for_telegram_message,
    truncate_text,
    validate_subject_id,
    validate_task_document_metadata,
)
from i18n import t, kb_in, all_locale_texts, subject_label, study_mode_label, quiz_section_label, SUPPORTED_LOCALES
from plan_handlers import (
    PLAN_UI_ENABLED,
    register_plan_handlers,
    maybe_offer_first_plan_prompt,
    build_plan_subject_keyboard,
    on_plan_activity_complete,
    plan_available,
    return_to_plan_without_complete,
)
from locale_bot import (
    user_locale,
    faq_items,
    load_achievements_catalog,
    tip_categories as tip_categories_for,
    commands_for_locale,
    pet_emotion,
    flash_source_labels,
    FLASH_SOURCE_CYCLE,
    SUBJECT_IDS,
    STUDY_MODE_IDS,
    QUIZ_SECTION_KEYS,
    FAQ_IDS,
)

# ------------------------------------------------------------
# Настройки окружения
# ------------------------------------------------------------
load_dotenv()
ensure_persistent_dirs()
BOT_TOKEN = os.getenv("BOT_TOKEN")
MAIN_ADMIN_ID = int(os.getenv("MAIN_ADMIN_ID", "0"))
# Numeric seconds — aiogram BaseSession.timeout and polling add polling_timeout to it.
TELEGRAM_TIMEOUT = 30
SERVER_TIMEZONE = os.getenv("SERVER_TIMEZONE", "Europe/Moscow")
CHANNEL_URL = "https://t.me/palph_study"

if not BOT_TOKEN:
    raise RuntimeError("❌ BOT_TOKEN не установлен в .env!")

# ------------------------------------------------------------
# Логгер
# ------------------------------------------------------------
def setup_logger():
    """
    Логгер уровня приложения. Уровень регулируется через env LOG_LEVEL
    (DEBUG/INFO/WARNING/ERROR), по умолчанию INFO. На диск пишется с
    ротацией 5 МБ × 5 файлов (≈25 МБ потолок). Шум сторонних библиотек
    (aiogram, aiohttp, aiosqlite) глушится до WARNING+, чтобы наши
    бизнес-события не тонули в HTTP-логах.
    """
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    logger = logging.getLogger("studybuddy_bot")
    logger.setLevel(level)
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # LOG_FILE можно переопределить в env — в Docker/bothost пишем в
    # `/app/data/bot.log` (persistent volume), локально — `./bot.log`.
    log_file = LOG_FILE
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=5 * 1024 * 1024,  # 5 МБ
        backupCount=5,             # bot.log + bot.log.1..5
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # Сторонние библиотеки глушим, чтобы наши INFO не тонули в HTTP-логах.
    for noisy in ("aiogram", "aiogram.event", "aiohttp.access", "aiosqlite"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return logger

logger = setup_logger()


def _log_task_exception(task: asyncio.Task) -> None:
    """
    done-callback для всех asyncio-задач, которые мы создаём вручную
    (schedulers, per-user timers). Без этого callback'а исключение,
    «улетевшее» из задачи, отобразится только как `Task exception was
    never retrieved` в stderr при GC задачи — то есть с задержкой и
    без traceback'а в bot.log. С callback'ом мы получаем
    `logger.exception` сразу как только задача упала.

    CancelledError — нормальный shutdown path; logger.exception для
    него не пишем.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is None:
        return
    logger.exception(
        "background_task.crashed name=%s exc=%s",
        task.get_name(), type(exc).__name__,
        exc_info=exc,
    )


# ------------------------------------------------------------
# Загрузка достижений
# ------------------------------------------------------------
ACHIEVEMENTS_FILE = Path(__file__).parent / "achievements.json"
with open(ACHIEVEMENTS_FILE, "r", encoding="utf-8") as f:
    ACHIEVEMENTS = json.load(f)  # ru fallback; UI uses load_achievements_catalog(locale)

# ------------------------------------------------------------
# Глобальные объекты (заполняются в main)
# ------------------------------------------------------------
db = None
user_repo: UserRepository = None
session_repo: SessionRepository = None
admin_repo: AdminRepository = None
flashcard_repo: FlashcardRepository = None
user_flashcard_repo: UserFlashcardRepository = None
user_task_repo: UserTaskRepository = None
mcq_repo: McqProgressRepository = None
task_repo: TaskProgressRepository = None
subject_stats_repo: SubjectStatsRepository = None
event_repo: EventRepository = None
plan_repo: PlanRepository = None
tips_repo: TipsRepository = None
device_repo: DeviceRepository = None
desktop_timer_repo: DesktopTimerRepository = None
ach_service: AchievementService = None
study_service: StudyService = None
streak_service: StreakService = None
backup_service: BackupService = None
analytics_service: AnalyticsService = None
bot: Bot = None
dp: Dispatcher = None

# Активные таймеры: user_id -> asyncio.Task
# Держим строгие ссылки, чтобы задачи не были собраны GC,
# и чтобы их можно было отменить при остановке/перезапуске.
active_timers: dict[int, asyncio.Task] = {}
# Pomodoro metadata when user studies (quiz flow) while timer asyncio task still runs.
pending_timer_sessions: dict[int, dict] = {}
_timer_completion_locks: dict[int, asyncio.Lock] = {}


def _timer_completion_lock(user_id: int) -> asyncio.Lock:
    lock = _timer_completion_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _timer_completion_locks[user_id] = lock
    return lock


def _release_active_timer_slot(user_id: int, task: asyncio.Task) -> None:
    """Снимает запись только если слот всё ещё принадлежит этой задаче."""
    if active_timers.get(user_id) is task:
        active_timers.pop(user_id, None)


CUSTOM_TIMER_MIN_MINUTES = 5
CUSTOM_TIMER_MAX_MINUTES = 120


def _normalize_timer_duration(raw) -> int | None:
    if isinstance(raw, bool):
        return None
    if not isinstance(raw, int):
        try:
            raw = int(raw)
        except (TypeError, ValueError):
            return None
    if raw < 1 or raw > 120:
        return None
    return raw


def _parse_custom_timer_duration(text: str | None) -> tuple[int | None, str | None]:
    """
    Parse user-entered custom timer duration (whole minutes only).
    Returns (minutes, error_key) where error_key is None on success,
    'invalid' for non-numeric/malformed input, or 'range' for out-of-bounds values.
    """
    if text is None:
        return None, "invalid"
    normalized = unicodedata.normalize("NFKC", text.strip())
    if not normalized or not normalized.isdecimal():
        return None, "invalid"
    try:
        duration = int(normalized)
    except ValueError:
        return None, "invalid"
    if duration < CUSTOM_TIMER_MIN_MINUTES or duration > CUSTOM_TIMER_MAX_MINUTES:
        return None, "range"
    return duration, None


async def _clear_custom_timer_duration_wait(state: FSMContext) -> None:
    """Drop stale custom-duration FSM if user navigates away."""
    if await state.get_state() == TimerStates.waiting_for_duration.state:
        await state.clear()


async def _claim_timer_session(state: FSMContext, user_id: int) -> dict | None:
    """
    Атомарно забирает данные таймера для завершения сессии: из
    pending_timer_sessions (фон во время «Подготовка») или из FSM active.
    Второй concurrent caller получит None.
    """
    async with _timer_completion_lock(user_id):
        pending = pending_timer_sessions.pop(user_id, None)
        if pending is not None:
            return pending
        if await state.get_state() != TimerStates.active.state:
            return None
        data = await state.get_data()
        await state.clear()
        return data


async def _claim_active_timer(state: FSMContext, user_id: int) -> dict | None:
    """Alias for timer completion paths."""
    return await _claim_timer_session(state, user_id)


async def _detach_timer_for_study_flow(
    state: FSMContext, user_id: int, chat_id: int,
) -> bool:
    """
    Сохраняет таймер в pending_timer_sessions, чтобы FSM можно было
    переключить на квизы без остановки asyncio-задачи.
    """
    async with _timer_completion_lock(user_id):
        if user_id in pending_timer_sessions:
            return True
        if await state.get_state() != TimerStates.active.state:
            return user_id in active_timers
        data = await state.get_data()
        start_time = data.get("start_time")
        if not isinstance(start_time, datetime):
            return False
        duration = _normalize_timer_duration(data.get("duration", 25)) or 25
        pending_timer_sessions[user_id] = {
            "duration": duration,
            "start_time": start_time,
            "chat_id": chat_id,
        }
        return True


def _timer_remaining_minutes(session: dict) -> float:
    duration = _normalize_timer_duration(session.get("duration", 25)) or 25
    start_time = session.get("start_time")
    if not isinstance(start_time, datetime):
        return 0.0
    elapsed = (datetime.now() - start_time).total_seconds() / 60
    return max(0.0, duration - elapsed)


async def _preserve_pending_timer_across_clear(user_id: int, state: FSMContext) -> None:
    """state.clear() не должен терять pending_timer_sessions."""
    pending = pending_timer_sessions.get(user_id)
    await state.clear()
    if pending is not None:
        pending_timer_sessions[user_id] = pending


def _ensure_timer_task_running(
    chat_id: int, state: FSMContext, user_id: int, duration: int,
) -> None:
    """Перезапускает asyncio-задачу, если FSM active, а task потерян/упал."""
    task = active_timers.get(user_id)
    if task is None or task.done():
        start_timer(chat_id, state, user_id, duration)


async def _cancel_timer_task(user_id: int) -> None:
    """Отменяет фоновую asyncio-задачу таймера без начисления монет."""
    task = active_timers.pop(user_id, None)
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# Rate limiter — защита от спама/abuse'а на уровне приложения.
# Initialize in main(); attached to dispatcher как middleware.
rate_limiter: UserRateLimiter = None

# Anti-spam: не больше 1 свободного сообщения админам в минуту (catch-all handler).
admin_message_limiter = UserRateLimiter(max_actions=1, window_seconds=60, warn_threshold=1.0)


class UsernameSyncMiddleware(BaseMiddleware):
    """
    Обновляет users.username из event_from_user.username на каждом
    Message/CallbackQuery. Telegram-юзер может менять @handle в любой
    момент, и friends-search должен находить актуальное значение.

    Безусловный UPDATE (1 SQL/event) — приемлемая цена для бота
    <100 пользователей. Если cost станет проблемой — можно перейти
    на in-memory cache + write-only-if-changed.

    Sync failure тихо логируется и НЕ должна прерывать handler.
    Username — вспомогательное поле, его недоступность не должна
    лишать пользователя возможности учиться.
    """

    def __init__(self, user_repo: UserRepository):
        self.user_repo = user_repo
        super().__init__()

    async def __call__(self, handler, event, data):
        try:
            user = data.get("event_from_user")
            if user is not None and user.id:
                # user.username — str или None; передаём как есть.
                # refresh_username безусловный UPDATE; если строки
                # пользователя нет (ещё не /start'нул), no-op
                # (rowcount=0, никаких ошибок).
                await self.user_repo.refresh_username(user.id, user.username)
        except Exception as e:
            logger.warning(
                "username.sync_failed user_id=%s reason=%s",
                getattr(getattr(event, "from_user", None), "id", None),
                type(e).__name__,
            )
        return await handler(event, data)


class RateLimitMiddleware(BaseMiddleware):
    """
    Sliding-window rate-limit на каждое Message/CallbackQuery.
    Админы exempt — у них доверенный доступ + им нужны broadcast/прочее.

    Telegram event extraction: aiogram middleware data dict содержит
    `event_from_user` (User объект) для любого type'а — Message,
    CallbackQuery, и т.д. Так что один middleware для обоих.
    """

    def __init__(self, limiter: UserRateLimiter, locale_fn=None):
        self.limiter = limiter
        self.locale_fn = locale_fn
        super().__init__()

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)
        # Админы не лимитятся — у них broadcast и прочее, false-positives
        # болезненны. Также главный админ может вручную нагрузить /backup
        # подряд для тестов.
        if is_admin(user.id):
            return await handler(event, data)

        status = self.limiter.check(user.id)
        if status == "block":
            logger.info("ratelimit.blocked user_id=%s", user.id)
            try:
                locale = await self.locale_fn(user.id) if self.locale_fn else "ru"
                if isinstance(event, Message):
                    await event.answer(t("errors.rate_limit_msg", locale))
                elif isinstance(event, CallbackQuery):
                    await event.answer(
                        t("errors.rate_limit_cb", locale),
                        show_alert=False,
                    )
            except Exception as e:
                logger.warning(
                    "ratelimit.block_send_failed user_id=%s reason=%s",
                    user.id, type(e).__name__,
                )
            return None  # silently drop, handler не вызывается

        if status == "warn":
            logger.info("ratelimit.warned user_id=%s", user.id)
            try:
                locale = await self.locale_fn(user.id) if self.locale_fn else "ru"
                if isinstance(event, Message):
                    await event.answer(t("errors.rate_limit_msg", locale))
                elif isinstance(event, CallbackQuery):
                    await event.answer(
                        t("errors.rate_limit_cb", locale),
                        show_alert=False,
                    )
            except Exception as e:
                logger.warning(
                    "ratelimit.warn_send_failed user_id=%s reason=%s",
                    user.id, type(e).__name__,
                )

        return await handler(event, data)

# ------------------------------------------------------------
# Состояния FSM
# ------------------------------------------------------------
class TimerStates(StatesGroup):
    waiting_for_duration = State()
    active = State()

class QuizStates(StatesGroup):
    # Flow учёбы (v0.9 user flashcards):
    #   choosing_subject — пользователь выбирает предмет
    #   choosing_mode    — выбирает режим (situational/MCQ/...) для предмета
    # Существующий situational flow:
    #   choosing_section — Раздел I/II/III/IV для ОПМ
    #   answering        — open-text ответ на ситуационный вопрос
    # MCQ flow (v0.7 #13):
    #   answering_mcq    — пользователь тапает inline-кнопки с вариантами
    choosing_subject = State()
    choosing_mode = State()
    choosing_section = State()
    answering = State()
    answering_mcq = State()
    answering_task = State()
    answering_flash = State()

class SetupStates(StatesGroup):
    choosing_language = State()
    choosing_path = State()
    setting_morning = State()
    setting_evening = State()
    confirming = State()


class FriendStates(StatesGroup):
    """FSM для добавления друга по Telegram ID (Phase 4)."""
    waiting_for_user_id = State()


class PetStates(StatesGroup):
    """FSM для переименования питомца (TODO #16 Phase B)."""
    waiting_for_name = State()


class SettingsStates(StatesGroup):
    # Универсальное состояние для ввода времени (утро/вечер).
    # Слот хранится в FSM data: {"slot": "morning" | "evening"}.
    waiting_for_time = State()


class FlashcardCreateStates(StatesGroup):
    waiting_for_term = State()
    waiting_for_definition = State()


class UserTaskImportStates(StatesGroup):
    waiting_for_file = State()


TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]?\d)$")


# Пресеты часовых поясов (IANA-имя, человекочитаемая метка).
# Россия/СНГ — русские подписи; остальной мир — EN (флаг + город + UTC).
TZ_PRESETS: list[tuple[str, str]] = [
    # — Россия и СНГ —
    ("Europe/Kaliningrad",  "🇷🇺 Калининград (UTC+2)"),
    ("Europe/Moscow",       "🇷🇺 Москва / 🇧🇾 Минск (UTC+3)"),
    ("Europe/Samara",       "🇷🇺 Самара (UTC+4)"),
    ("Asia/Yekaterinburg",  "🇷🇺 Екатеринбург / 🇺🇿 Ташкент (UTC+5)"),
    ("Asia/Omsk",           "🇷🇺 Омск / 🇰🇿 Алматы (UTC+6)"),
    ("Asia/Krasnoyarsk",    "🇷🇺 Красноярск (UTC+7)"),
    ("Asia/Irkutsk",        "🇷🇺 Иркутск (UTC+8)"),
    ("Asia/Yakutsk",        "🇷🇺 Якутск (UTC+9)"),
    ("Asia/Vladivostok",    "🇷🇺 Владивосток (UTC+10)"),
    ("Asia/Magadan",        "🇷🇺 Магадан (UTC+11)"),
    ("Asia/Kamchatka",      "🇷🇺 Камчатка (UTC+12)"),
    ("Europe/Kyiv",         "🇺🇦 Киев (UTC+2/+3)"),
    # — Западная и Центральная Европа —
    ("Europe/London",       "🇬🇧 London (UTC+0/+1)"),
    ("Europe/Lisbon",       "🇵🇹 Lisbon (UTC+0/+1)"),
    ("Europe/Paris",        "🇫🇷 Paris / 🇩🇪 Berlin (UTC+1/+2)"),
    ("Europe/Athens",       "🇬🇷 Athens / 🇷🇴 Bucharest (UTC+2/+3)"),
    ("Europe/Helsinki",     "🇫🇮 Helsinki / 🇸🇪 Stockholm (UTC+2/+3)"),
    ("Europe/Istanbul",     "🇹🇷 Istanbul (UTC+3)"),
    # — Америка —
    ("America/New_York",    "🇺🇸 New York / 🇨🇦 Toronto (UTC-5/-4)"),
    ("America/Chicago",     "🇺🇸 Chicago / 🇲🇽 Mexico City (UTC-6/-5)"),
    ("America/Denver",      "🇺🇸 Denver (UTC-7/-6)"),
    ("America/Phoenix",     "🇺🇸 Phoenix (UTC-7, no DST)"),
    ("America/Los_Angeles", "🇺🇸 Los Angeles / 🇨🇦 Vancouver (UTC-8/-7)"),
    ("America/Anchorage",   "🇺🇸 Anchorage (UTC-9/-8)"),
    ("Pacific/Honolulu",    "🇺🇸 Honolulu (UTC-10)"),
    ("America/Bogota",      "🇨🇴 Bogotá / 🇵🇪 Lima (UTC-5)"),
    ("America/Santiago",    "🇨🇱 Santiago (UTC-4/-3)"),
    ("America/Sao_Paulo",   "🇧🇷 São Paulo (UTC-3)"),
    ("America/Argentina/Buenos_Aires", "🇦🇷 Buenos Aires (UTC-3)"),
    # — Ближний Восток и Африка —
    ("Asia/Dubai",          "🇦🇪 Dubai (UTC+4)"),
    ("Asia/Riyadh",         "🇸🇦 Riyadh (UTC+3)"),
    ("Asia/Tehran",         "🇮🇷 Tehran (UTC+3:30/+4:30)"),
    ("Asia/Jerusalem",      "🇮🇱 Jerusalem (UTC+2/+3)"),
    ("Africa/Cairo",        "🇪🇬 Cairo (UTC+2)"),
    ("Africa/Johannesburg", "🇿🇦 Johannesburg (UTC+2)"),
    ("Africa/Lagos",        "🇳🇬 Lagos (UTC+1)"),
    ("Africa/Nairobi",      "🇰🇪 Nairobi (UTC+3)"),
    # — Южная и Юго-Восточная Азия —
    ("Asia/Kolkata",        "🇮🇳 Mumbai / Delhi (UTC+5:30)"),
    ("Asia/Karachi",        "🇵🇰 Karachi (UTC+5)"),
    ("Asia/Dhaka",          "🇧🇩 Dhaka (UTC+6)"),
    ("Asia/Bangkok",        "🇹🇭 Bangkok / 🇻🇳 Hanoi (UTC+7)"),
    ("Asia/Jakarta",        "🇮🇩 Jakarta (UTC+7)"),
    ("Asia/Singapore",      "🇸🇬 Singapore / 🇲🇾 KL (UTC+8)"),
    # — Восточная Азия и Океания —
    ("Asia/Shanghai",       "🇨🇳 Shanghai / Beijing (UTC+8)"),
    ("Asia/Hong_Kong",      "🇭🇰 Hong Kong (UTC+8)"),
    ("Asia/Tokyo",          "🇯🇵 Tokyo (UTC+9)"),
    ("Asia/Seoul",          "🇰🇷 Seoul (UTC+9)"),
    ("Australia/Perth",     "🇦🇺 Perth (UTC+8)"),
    ("Australia/Sydney",    "🇦🇺 Sydney / Melbourne (UTC+10/+11)"),
    ("Pacific/Auckland",    "🇳🇿 Auckland (UTC+12/+13)"),
]
TZ_IDS: set[str] = {tz for tz, _ in TZ_PRESETS}


def tz_label(tz_id: str) -> str:
    """Возвращает человекочитаемую метку часового пояса или сам TZ как fallback."""
    for tid, label in TZ_PRESETS:
        if tid == tz_id:
            return label
    return tz_id or "Europe/Moscow"


# 4-уровневая оценка сессии. Хранится как 1..4 в study_sessions.score.
RATING_EMOJIS: list[tuple[int, str]] = [
    (1, "😞"),
    (2, "😐"),
    (3, "🙂"),
    (4, "😍"),
]


def build_rating_keyboard(session_id: int, locale: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for score, emoji in RATING_EMOJIS:
        kb.button(text=emoji, callback_data=f"rate:{session_id}:{score}")
    kb.button(text=t("timer.rating_skip", locale), callback_data=f"rate_skip:{session_id}")
    kb.adjust(4, 1)
    return kb.as_markup()


async def send_rating_prompt(chat_id: int, session_id: int, user_id: int) -> None:
    """Отправляет пользователю запрос на оценку только что завершённой сессии."""
    locale = await loc(user_id)
    try:
        await bot.send_message(
            chat_id,
            t("timer.rating_prompt", locale),
            reply_markup=build_rating_keyboard(session_id, locale),
        )
    except Exception as e:
        logger.error(f"send_rating_prompt: не удалось отправить запрос оценки: {e}")

# ------------------------------------------------------------
# Администраторы и сообщения
# ------------------------------------------------------------
ADMINS_FILE = "admins.json"
MESSAGES_FILE = "messages.log"  # append-only JSONL: одна запись = одна строка JSON

# In-memory кеш админов. Источник истины — таблица `admins` в БД;
# кеш заполняется в main() из БД и обновляется командами /addadmin / /rmadmin.
# is_admin() работает синхронно по кешу — это горячий путь (catch-all handler
# вызывает её на каждое сообщение).
ADMINS: set[int] = set()
if MAIN_ADMIN_ID:
    # Главный админ всегда считается админом, даже если БД ещё не загружена.
    ADMINS.add(MAIN_ADMIN_ID)


def is_admin(user_id: int) -> bool:
    return user_id in ADMINS or user_id == MAIN_ADMIN_ID


# Команды для /-пикера Telegram. Объявлены модульно, чтобы /addadmin
# мог расширить пикер новому админу, а /rmadmin — вернуть к дефолтному.
DEFAULT_COMMANDS = commands_for_locale("ru")
ADMIN_COMMANDS = DEFAULT_COMMANDS + [
    BotCommand(command="help", description="Справка по командам (для админов)"),
    BotCommand(command="reply", description="Ответ пользователю по ID"),
    BotCommand(command="broadcast", description="Рассылка всем"),
    BotCommand(command="notif_status", description="Диагностика уведомлений"),
    BotCommand(command="analytics", description="📊 Dashboard PA-аналитики (всё в одном)"),
    BotCommand(command="cohort_stats", description="Retention D1/D7/D30 по когортам"),
    BotCommand(command="funnel", description="Activation funnel"),
    BotCommand(command="activation", description="Time-to-first-session & features"),
    BotCommand(command="product_metrics", description="Subject/mode, retention D7, push"),
    BotCommand(command="dau", description="DAU/WAU/MAU + stickiness"),
    BotCommand(command="feature_usage", description="% adoption per feature"),
    BotCommand(command="segments", description="User segmentation (power/active/...)"),
    BotCommand(command="content_stats", description="Hardest terms / popular MCQ / EF dist"),
    BotCommand(command="event_timeline", description="Last N events timeline"),
    BotCommand(command="heatmap", description="Activity heatmap (hours × weekdays)"),
    BotCommand(command="export", description="Export table as CSV"),
    BotCommand(command="parse_logs", description="bot.log → events CSV (ETL)"),
    BotCommand(command="backup", description="Snapshot БД (главный админ)"),
    BotCommand(command="addadmin", description="Добавить админа (главный админ)"),
    BotCommand(command="rmadmin", description="Удалить админа (главный админ)"),
    BotCommand(command="listadmins", description="Список админов (главный админ)"),
]


async def _migrate_admins_json_to_db() -> int:
    """
    Один раз импортирует admins.json в БД и переименовывает файл в
    admins.json.migrated, чтобы повторный запуск не пытался импортировать
    снова. Идемпотентно (INSERT OR IGNORE). Возвращает количество новых
    записей.
    """
    if not os.path.exists(ADMINS_FILE):
        return 0
    try:
        with open(ADMINS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        ids = data.get("admins", [])
    except Exception as e:
        logger.error(f"admins.migration_failed: не удалось прочитать {ADMINS_FILE}: {e}")
        return 0

    imported = 0
    for uid in ids:
        try:
            if await admin_repo.add(int(uid)):
                imported += 1
        except Exception as e:
            logger.warning(f"admins.migration: пропущен {uid!r}: {e}")

    migrated_path = ADMINS_FILE + ".migrated"
    try:
        # Не os.replace: если архив уже существует (повторный запуск с
        # восстановлённым admins.json) — оставим следы обоих файлов.
        if os.path.exists(migrated_path):
            migrated_path = f"{ADMINS_FILE}.migrated.{int(datetime.now().timestamp())}"
        os.rename(ADMINS_FILE, migrated_path)
        logger.info(f"admins.migration_done imported={imported} archived={migrated_path}")
    except Exception as e:
        logger.warning(f"admins.migration: rename failed: {e}")

    return imported

# ------------------------------------------------------------
# Роутер и клавиатуры
# ------------------------------------------------------------
router = Router()


async def loc(user_id: int) -> str:
    return await user_locale(user_repo, user_id)


# ------------------------------------------------------------
# Центральный обработчик неперехваченных исключений в handler'ах.
# Регистрируется на root router → ловит то, что не поймали локальные
# try/except в конкретных handler'ах. Цель: ① всегда писать traceback
# в bot.log через logger.exception, ② показывать пользователю
# дружелюбное локализованное сообщение вместо silent failure'а.
# Возврат True помечает событие как обработанное, чтобы aiogram не
# логировал его повторно своим дефолтным механизмом.
# ------------------------------------------------------------
@router.errors()
async def global_error_handler(event: ErrorEvent) -> bool:
    update = event.update
    exc = event.exception

    user_id: int | None = None
    chat_id: int | None = None
    if update.message is not None:
        if update.message.from_user is not None:
            user_id = update.message.from_user.id
        chat_id = update.message.chat.id
    elif update.callback_query is not None:
        user_id = update.callback_query.from_user.id
        if update.callback_query.message is not None:
            chat_id = update.callback_query.message.chat.id

    logger.exception(
        "handler.unhandled user_id=%s chat_id=%s exc=%s",
        user_id, chat_id, type(exc).__name__,
        exc_info=exc,
    )

    # TelegramForbiddenError = пользователь заблокировал бота. Слать
    # ему «что-то пошло не так» бессмысленно и вызовет ещё одну
    # ошибку — просто помечаем как обработанное.
    if isinstance(exc, TelegramForbiddenError):
        return True

    if chat_id is not None:
        try:
            locale = await loc(user_id) if user_id is not None else "ru"
            await bot.send_message(chat_id, t("common.unexpected_error", locale))
        except Exception as notify_exc:
            logger.warning(
                "handler.unhandled.notify_failed chat_id=%s reason=%s",
                chat_id, type(notify_exc).__name__,
            )

    return True


def _all_subject_button_texts() -> list[str]:
    return [subject_label(sid, l) for sid in SUBJECT_IDS for l in SUPPORTED_LOCALES]


def subject_id_from_button(text: str) -> str | None:
    for sid in SUBJECT_IDS:
        for l in SUPPORTED_LOCALES:
            if subject_label(sid, l) == text:
                return sid
    return None


def _all_mode_button_texts() -> list[str]:
    keys = list(STUDY_MODE_IDS) + ["tasks_own"]
    return [study_mode_label(mid, l) for mid in keys for l in SUPPORTED_LOCALES]


def mode_id_from_button(text: str) -> str | None:
    for mid in list(STUDY_MODE_IDS) + ["tasks_own"]:
        for l in SUPPORTED_LOCALES:
            if study_mode_label(mid, l) == text:
                return mid if mid != "tasks_own" else "tasks"
    return None


def language_picker_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=t("lang.ru", "ru"), callback_data="lang:set:ru")
    kb.button(text=t("lang.en", "en"), callback_data="lang:set:en")
    kb.adjust(2)
    return kb.as_markup()


async def apply_user_bot_commands(user_id: int) -> None:
    """Обновляет /-меню Telegram под язык пользователя."""
    try:
        cmds = commands_for_locale(await loc(user_id))
        await bot.set_my_commands(cmds, scope=BotCommandScopeChat(chat_id=user_id))
    except Exception as e:
        logger.warning("locale.set_commands_failed uid=%s reason=%s", user_id, type(e).__name__)


def get_main_keyboard(locale: str) -> ReplyKeyboardMarkup:
    """Главное меню: Подготовка (отдельная строка), учебные инструменты, профиль, FAQ, новости."""
    builder = ReplyKeyboardBuilder()
    builder.button(text=t("kb.quizzes", locale))
    builder.button(text=t("kb.study", locale))
    builder.button(text=t("kb.profile", locale))
    builder.button(text=t("kb.faq", locale))
    builder.button(text=t("kb.news", locale))
    builder.adjust(1, 1, 2, 1)
    return builder.as_markup(resize_keyboard=True)


def get_study_keyboard(locale: str) -> ReplyKeyboardMarkup:
    """Подменю «Учебные инструменты»: Pomodoro и советы (подготовка — отдельная кнопка в главном меню)."""
    builder = ReplyKeyboardBuilder()
    builder.button(text=t("kb.tips", locale))
    builder.button(text=t("kb.standard_timer", locale))
    builder.button(text=t("kb.custom_timer", locale))
    builder.button(text=t("kb.back_main", locale))
    builder.adjust(2, 1, 1)
    return builder.as_markup(resize_keyboard=True)

def get_tips_keyboard(locale: str) -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.button(text=t("kb.tips_time_mgmt", locale))
    builder.button(text=t("kb.tips_memory", locale))
    builder.button(text=t("kb.tips_bot_guide", locale))
    builder.button(text=t("kb.tips_links", locale))
    builder.button(text=t("kb.back_study", locale))
    builder.adjust(2)
    return builder.as_markup(resize_keyboard=True)

def get_timer_active_keyboard(locale: str) -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.button(text=t("kb.stop_timer", locale))
    builder.button(text=t("kb.back_main", locale))
    builder.adjust(2)
    return builder.as_markup(resize_keyboard=True)

def get_mode_keyboard(locale: str) -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    for mode_id in STUDY_MODE_IDS:
        builder.button(text=study_mode_label(mode_id, locale))
    builder.button(text=t("kb.back_subjects", locale))
    builder.adjust(1)
    return builder.as_markup(resize_keyboard=True)


async def get_subject_keyboard(user_id: int, locale: str) -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    for sid, _ in await available_subjects(user_id, locale):
        builder.button(text=subject_label(sid, locale))
    builder.button(text=t("kb.back_main", locale))
    builder.adjust(1)
    return builder.as_markup(resize_keyboard=True)


async def get_mode_keyboard_for_subject(
    subject_id: str, user_id: int, locale: str,
) -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    for mode_id, label in await available_modes(subject_id, user_id, locale):
        builder.button(text=label)
    builder.button(text=t("kb.back_subjects", locale))
    builder.adjust(1)
    return builder.as_markup(resize_keyboard=True)


def get_subject_keyboard_for_mode(mode_id: str, locale: str) -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    for sid, _ in subjects_with_mode(mode_id):
        builder.button(text=subject_label(sid, locale))
    builder.button(text=t("kb.back_modes", locale))
    builder.adjust(1)
    return builder.as_markup(resize_keyboard=True)


def get_mcq_active_keyboard(locale: str) -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.button(text=t("kb.finish_session", locale))
    builder.adjust(1)
    return builder.as_markup(resize_keyboard=True)


def get_task_active_keyboard(locale: str) -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.button(text=t("kb.finish_session", locale))
    builder.adjust(1)
    return builder.as_markup(resize_keyboard=True)


def get_flash_active_keyboard(locale: str) -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.button(text=t("kb.finish_session", locale))
    builder.adjust(1)
    return builder.as_markup(resize_keyboard=True)


def quiz_sections_for(locale: str) -> list[tuple[str, str]]:
    return [(quiz_section_label(k, locale), k) for k in QUIZ_SECTION_KEYS]


def _quiz_section_labels() -> list[str]:
    labels: list[str] = []
    for loc in SUPPORTED_LOCALES:
        labels.extend(l for l, _ in quiz_sections_for(loc))
    labels.extend(all_locale_texts("kb.finish_quiz"))
    return labels


def _quiz_section_map(locale: str) -> dict[str, str]:
    return {label: key for label, key in quiz_sections_for(locale)}


def _quiz_section_map_all() -> dict[str, str]:
    merged: dict[str, str] = {}
    for loc in SUPPORTED_LOCALES:
        merged.update(_quiz_section_map(loc))
    return merged


def _quiz_section_label_list() -> list[str]:
    return list(_quiz_section_map_all().keys())


def available_quiz_sections(
    subject_id: str = "industrial-management",
    locale: str = "ru",
) -> list[tuple[str, str]]:
    """Разделы ситуационных квизов с непустыми файлами (label, key)."""
    section_dir = STUDY_MATERIALS_PATH / subject_id / "situational"
    available = []
    for label, key in quiz_sections_for(locale):
        file_path = section_dir / f"section-{key}.txt"
        if file_path.exists() and file_path.stat().st_size > 0:
            available.append((label, key))
    return available


def get_quiz_section_keyboard(locale: str) -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    for label, _ in available_quiz_sections(locale=locale):
        builder.button(text=label)
    builder.button(text=t("kb.finish_quiz", locale))
    builder.adjust(2)
    return builder.as_markup(resize_keyboard=True)


def get_quiz_answer_keyboard(locale: str) -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.button(text=t("kb.finish_quiz", locale))
    builder.adjust(1)
    return builder.as_markup(resize_keyboard=True)

# ------------------------------------------------------------
# Каталог учебных материалов
# ------------------------------------------------------------
# Структура `study_materials/<subject>/<mode>` data-driven: пустые папки/
# файлы автоматически скрываются. Заполнение контента — в обязанности
# контентщика, код не требует доработки при добавлении файлов.
#
#   study_materials/
#   ├── industrial-management/         ← id предмета (имя папки)
#   │   ├── situational/section-i..iv.txt   (multi-section, открытый текст)
#   │   ├── flashcards.txt                  (term || definition)
#   │   ├── mcq.txt                         (вопрос || верный || w1 || w2 || w3)
#   │   └── tasks/task-NN.{json,png}        (картинка-условие + accepted[])
#   ├── math/                          ← без situational/ (не для учебника-теории)
#   │   ├── flashcards.txt
#   │   ├── mcq.txt
#   │   └── tasks/
#   └── english/
#       ├── flashcards.txt
#       ├── mcq.txt
#       └── tasks/
STUDY_MATERIALS_PATH = Path(__file__).parent / "study_materials"
BOT_DIR = Path(__file__).parent
TIPS_DIR = BOT_DIR / "tips"

# Legacy .txt — fallback, если JSON ещё не разложен (контентщик / старые деплои).
TIME_MANAGEMENT_TIPS_FILE = BOT_DIR / "timemanagement.txt"
MEMORY_RETENTION_TIPS_FILE = BOT_DIR / "memoryretention.txt"
PRODUCTIVITY_LINKS_FILE = BOT_DIR / "links-to-productivity-material.txt"


def _load_tips_json(filename: str) -> list[dict]:
    """
    Читает tips/<filename>. Ожидает {"tips": [{id, title, emoji, body, tags, action}, ...]}.
    """
    path = TIPS_DIR / filename
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    tips = data.get("tips", [])
    return [t for t in tips if t.get("title") and t.get("body")]


def _load_links_json() -> list[dict]:
    path = TIPS_DIR / "links.json"
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return [lnk for lnk in data.get("links", []) if lnk.get("title") and lnk.get("url")]


def _load_tips_legacy_txt(path: Path, category: str) -> list[dict]:
    """Конвертирует старый формат «эмодзи Заголовок: тело» в структуру JSON."""
    if not path.is_file():
        return []
    tips: list[dict] = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        idx = line.find(": ")
        if idx == -1:
            tips.append({
                "id": f"{category}-legacy-{i:02d}",
                "title": line[:60],
                "emoji": "",
                "body": line,
                "tags": ["study"],
                "action": "",
            })
            continue
        head, body = line[:idx].strip(), line[idx + 2 :].strip()
        m = re.match(r"^(\S+)\s+(.+)$", head)
        emoji, title = (m.group(1), m.group(2)) if m else ("", head)
        tips.append({
            "id": f"{category}-legacy-{i:02d}",
            "title": title,
            "emoji": emoji,
            "body": body,
            "tags": ["study"],
            "action": "",
        })
    return tips


def _load_category_tips(category: str, json_file: str, legacy_txt: Path) -> list[dict]:
    tips = _load_tips_json(json_file)
    if not tips:
        tips = _load_tips_legacy_txt(legacy_txt, category)
    return tips


# Кэш при импорте (как achievements.json).
TIME_MANAGEMENT_TIPS = _load_category_tips("tm", "time-management.json", TIME_MANAGEMENT_TIPS_FILE)
MEMORY_RETENTION_TIPS = _load_category_tips("mem", "memory.json", MEMORY_RETENTION_TIPS_FILE)
BOT_GUIDE_TIPS = _load_category_tips("bot", "bot-guide.json", BOT_DIR / "tips" / "_noop.txt")
PRODUCTIVITY_LINKS = _load_links_json()
if not PRODUCTIVITY_LINKS and PRODUCTIVITY_LINKS_FILE.is_file():
    _TIP_LINK_LINE_RE = re.compile(r"^(.+?):\s*(https?://\S+)\s*$")
    for i, line in enumerate(
        PRODUCTIVITY_LINKS_FILE.read_text(encoding="utf-8").splitlines(), 1
    ):
        line = line.strip()
        if not line:
            continue
        m = _TIP_LINK_LINE_RE.match(line)
        if m:
            PRODUCTIVITY_LINKS.append({
                "id": f"link-legacy-{i:02d}",
                "title": m.group(1).strip(),
                "url": m.group(2).strip(),
            })
        else:
            url_m = re.search(r"https?://\S+", line)
            if url_m:
                PRODUCTIVITY_LINKS.append({
                    "id": f"link-legacy-{i:02d}",
                    "title": line[: url_m.start()].strip(" :") or "Ссылка",
                    "url": url_m.group(0),
                })

def _tip_cats(locale: str) -> dict[str, dict]:
    return tip_categories_for(locale)


# RU catalog for tests and legacy references
TIP_CATEGORIES: dict[str, dict] = _tip_cats("ru")
TIP_COIN_PER_DAY = 1
TIPS_SEEN_COOLDOWN_DAYS = 7

# Каталог предметов: (id, label). id = имя папки в study_materials/.
SUBJECTS: list[tuple[str, str]] = [
    ("industrial-management", "🏭 Основы производственного менеджмента"),
    ("math",                  "🧮 Математика"),
    ("accounting",              "📊 Бухучёт"),
    ("english",               "🇬🇧 Английский"),
]

# Предметы без блока прогресса в профиле — только заглушка «в разработке».
PROFILE_COMING_SOON_SUBJECTS = ("english", "industrial-management")

# Скрыты в «📖 Подготовка» (предмет → режим), контент на диске сохраняется.
PREP_HIDDEN_SUBJECT_IDS = frozenset({"industrial-management"})

# Каталог режимов учёбы. id определяет где лежит контент:
#   situational → subject/situational/section-*.txt (multi-section)
#   flashcards / mcq → subject/<mode>.txt (один файл)
#   tasks → subject/tasks/task-*.json + task-*.png
STUDY_MODES: list[tuple[str, str]] = [
    (mid, study_mode_label(mid, "ru")) for mid in STUDY_MODE_IDS
]


def _file_based_mode_ids(subject_id: str) -> list[str]:
    """Режимы с непустым официальным контентом (файлы на диске)."""
    subject_path = STUDY_MATERIALS_PATH / subject_id
    if not subject_path.is_dir():
        return []
    result = []
    for mode_id in STUDY_MODE_IDS:
        if mode_id == "situational":
            section_dir = subject_path / "situational"
            if section_dir.is_dir() and any(
                p.stat().st_size > 0 for p in section_dir.glob("section-*.txt")
            ):
                result.append(mode_id)
        elif mode_id == "tasks":
            if load_tasks(subject_id):
                result.append(mode_id)
        else:
            file_path = subject_path / f"{mode_id}.txt"
            if file_path.exists() and file_path.stat().st_size > 0:
                result.append(mode_id)
    return result


async def available_modes(
    subject_id: str,
    user_id: int | None = None,
    locale: str = "ru",
) -> list[tuple[str, str]]:
    """
    Режимы с контентом для предмета. Учитывает пользовательские флэш-карты и задачи.
    """
    result = [
        (mode_id, study_mode_label(mode_id, locale))
        for mode_id in _file_based_mode_ids(subject_id)
    ]
    if user_id is not None:
        user_count = await user_flashcard_repo.count_by_subject(user_id, subject_id)
        if user_count > 0 and not any(m[0] == "flashcards" for m in result):
            result.append(("flashcards", study_mode_label("flashcards", locale)))
        user_task_count = await user_task_repo.count_by_subject(user_id, subject_id)
        if user_task_count > 0 and not any(m[0] == "tasks" for m in result):
            result.append(("tasks", study_mode_label("tasks_own", locale)))
    return result


async def available_subjects(user_id: int, locale: str = "ru") -> list[tuple[str, str]]:
    """Предметы, у которых есть хотя бы один доступный режим (меню «Подготовка»)."""
    result = []
    for sid in SUBJECT_IDS:
        if sid in PREP_HIDDEN_SUBJECT_IDS:
            continue
        if await available_modes(sid, user_id, locale):
            result.append((sid, subject_label(sid, locale)))
    return result


def subjects_with_mode(mode_id: str, locale: str = "ru") -> list[tuple[str, str]]:
    """Legacy sync helper — только официальный контент на диске."""
    return [
        (sid, subject_label(sid, locale))
        for sid in SUBJECT_IDS
        if sid not in PREP_HIDDEN_SUBJECT_IDS
        if mode_id in _file_based_mode_ids(sid)
    ]


def available_modes_global() -> list[tuple[str, str]]:
    """Legacy: режимы с официальным контентом хотя бы для одного предмета."""
    return [(mid, label) for mid, label in STUDY_MODES if subjects_with_mode(mid)]


# ------------------------------------------------------------
# Вспомогательные функции для квизов
# ------------------------------------------------------------

class QuizTerm:
    def __init__(self, term: str, definition: str, keywords: str, situation: str):
        self.term = term
        self.definition = definition
        self.keywords = [kw.strip() for kw in keywords.split(",")]
        self.situation = situation
        self.hash = hashlib.md5(term.encode()).hexdigest()[:8]

    def to_dict(self):
        return {
            "term": self.term,
            "definition": self.definition,
            "keywords": self.keywords,
            "situation": self.situation,
            "hash": self.hash
        }

def load_quiz_section(section: str, subject_id: str = "industrial-management") -> list[QuizTerm]:
    key = section.lower()
    if key not in QUIZ_SECTION_KEYS:
        return []
    base = safe_subject_dir(STUDY_MATERIALS_PATH, subject_id)
    if base is None:
        return []
    file_path = base / "situational" / f"section-{key}.txt"
    if not file_path.exists():
        return []
    terms = []
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split("||")]
            if len(parts) == 4:
                terms.append(QuizTerm(*parts))
    return terms


def load_mcq(subject_id: str) -> list[dict]:
    """
    Читает study_materials/<subject>/mcq.txt и возвращает список вопросов.
    Формат строки: 'вопрос || правильный || неправ1 || неправ2 || неправ3'
    Строки с # и пустые — игнорируются. Малформ-строки (< 5 частей) — пропускаются.
    Возвращает list of dicts: {"question", "correct", "wrongs": [w1, w2, w3]}.
    """
    base = safe_subject_dir(STUDY_MATERIALS_PATH, subject_id)
    if base is None:
        return []
    file_path = base / "mcq.txt"
    if not file_path.exists():
        return []
    questions = []
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split("||")]
            if len(parts) >= 5:
                entry = {
                    "question": parts[0],
                    "correct":  parts[1],
                    "wrongs":   parts[2:5],
                }
                if len(parts) >= 6 and parts[5].strip():
                    entry["topics"] = [t.strip() for t in parts[5].replace("|", ",").split(",") if t.strip()]
                questions.append(entry)
    return questions


def load_task_groups(subject_id: str) -> dict[str, dict]:
    """Читает study_materials/<subject>/groups.json — метаданные групп задач."""
    base = safe_subject_dir(STUDY_MATERIALS_PATH, subject_id)
    if base is None:
        return {}
    path = base / "groups.json"
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning("task.groups_load_failed subject=%s reason=%s", subject_id, e)
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(k): v for k, v in data.items()
        if isinstance(v, dict) and v.get("title")
    }


def load_tasks(subject_id: str, group_id: str | None = None) -> list[dict]:
    """
    Читает study_materials/<subject>/tasks/task-*.json и возвращает список задач.
    Каждая задача — dict с полями:
      - 'id': str (из имени файла, напр. 'task-01')
      - 'problem': str (текстовая подпись к картинке, может быть пустой)
      - 'accepted': list[str] (принимаемые ответы)
      - 'solution_filename': str (имя файла solution-картинки в той же папке;
        дефолт — '{id}-solution.png')
      - 'text_only': bool — PNG не обязателен
      - 'solution_text': str — текстовое решение
      - 'group': str — id группы (exam-task-1)
      - 'subtitle': str — подпись в UI (Пример 2, Вариант 1 · №1)
      - 'hint': str — педагогическая подсказка (показывается после 3-й ошибки)
    Задачи без PNG пропускаются, если не text_only и problem пустой.
    """
    base = safe_subject_dir(STUDY_MATERIALS_PATH, subject_id)
    if base is None:
        return []
    tasks_dir = base / "tasks"
    if not tasks_dir.is_dir():
        return []
    tasks = []
    for json_file in sorted(tasks_dir.glob("task-*.json")):
        try:
            with open(json_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.warning(f"task.load_failed file={json_file.name} reason={e}")
            continue
        task_id = json_file.stem  # 'task-01'
        task_group = str(data.get("group") or "")
        if group_id is not None and task_group != group_id:
            continue
        image_path = tasks_dir / f"{task_id}.png"
        text_only = bool(data.get("text_only"))
        problem = str(data.get("problem", ""))
        if not text_only and not image_path.exists():
            logger.warning(f"task.missing_image task_id={task_id} expected={image_path.name}")
            continue
        accepted = data.get("accepted", [])
        if not isinstance(accepted, list) or not accepted:
            logger.warning(f"task.no_accepted task_id={task_id}")
            continue
        solution_filename = safe_task_image_filename(
            str(data.get("solution_image", f"{task_id}-solution.png")),
            task_id,
        )
        raw_topics = data.get("topics") or []
        if isinstance(raw_topics, str):
            raw_topics = [raw_topics]
        tasks.append({
            "id": task_id,
            "problem": problem,
            "accepted": [str(a) for a in accepted],
            "solution_filename": str(solution_filename),
            "solution_text": str(data.get("solution_text") or ""),
            "text_only": text_only,
            "group": task_group,
            "subtitle": str(data.get("subtitle") or ""),
            "topics": [str(t) for t in raw_topics],
            "hint": str(data.get("hint") or ""),
            "kind": "official",
        })
    return tasks


async def load_tasks_for_study(
    user_id: int,
    subject_id: str,
    group_id: str | None = None,
) -> list[dict]:
    """Официальные задачи + пользовательские из БД (user tasks только без group_id)."""
    official = load_tasks(subject_id, group_id=group_id)
    if group_id is not None:
        return official
    return official + await user_task_repo.list_by_subject(user_id, subject_id)


def _flashcard_hash(term: str) -> str:
    """8-символьный hash термина (тот же паттерн, что у QuizTerm.hash)."""
    return hashlib.md5(term.encode("utf-8")).hexdigest()[:8]


def _mcq_hash(question: str) -> str:
    """8-символьный hash MCQ-вопроса для mcq_progress.question_hash."""
    return hashlib.md5(question.encode("utf-8")).hexdigest()[:8]


def load_flashcards(subject_id: str) -> list[dict]:
    """
    Читает study_materials/<subject>/flashcards.txt.
    Формат строки: 'термин || определение'.
    Возвращает list of dicts: {"term", "definition", "hash"}.
    Хэш — 8-символьный MD5 термина (PK части в flashcard_progress).
    """
    base = safe_subject_dir(STUDY_MATERIALS_PATH, subject_id)
    if base is None:
        return []
    file_path = base / "flashcards.txt"
    if not file_path.exists():
        return []
    cards = []
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split("||")]
            if len(parts) >= 2:
                term, definition = parts[0], parts[1]
                entry = {
                    "term": term,
                    "definition": definition,
                    "hash": _flashcard_hash(term),
                }
                if len(parts) >= 3 and parts[2].strip():
                    entry["topics"] = [t.strip() for t in parts[2].replace("|", ",").split(",") if t.strip()]
                cards.append(entry)
    return cards


async def load_flashcards_for_study(
    user_id: int,
    subject_id: str,
    source: str,
) -> list[dict]:
    """
    Загружает пул флэш-карт для сессии учёбы.
    source ∈ {'mix', 'official', 'own'}.
    """
    official = load_flashcards(subject_id)
    own = await user_flashcard_repo.list_by_subject(user_id, subject_id)
    if source == "official":
        return official
    if source == "own":
        return own
    # mix: own overrides official when the same term appears twice (different hashes).
    by_term: dict[str, dict] = {}
    for card in official:
        by_term[card["term"].lower().strip()] = card
    for card in own:
        by_term[card["term"].lower().strip()] = card
    return list(by_term.values())


def _word_matches_keyword(user_word: str, keyword: str) -> bool:
    """
    Проверяет, что user_word — форма того же слова, что и keyword.
    Для коротких ключей (≤3 символов) требуется точное совпадение,
    иначе подстроки вроде «ыр» сработали бы внутри «сырьё».
    Для длинных ключей пользовательское слово должно начинаться
    со стема (первые 5 символов) — это покрывает склонения вроде
    «системы» / «системами».
    """
    kw = keyword.lower()
    if len(kw) <= 3:
        return user_word == kw
    return user_word.startswith(kw[:5])


def check_text_answer(user_answer: str, correct_definition: str, keywords: list) -> tuple:
    user_norm = re.sub(r'[^\w\s]', ' ', user_answer.lower()).strip()
    correct_norm = re.sub(r'[^\w\s]', ' ', correct_definition.lower()).strip()
    user_words = user_norm.split()
    correct_words = correct_norm.split()

    def kw_present(kw: str) -> bool:
        return any(_word_matches_keyword(uw, kw) for uw in user_words)

    keyword_matches = sum(1 for kw in keywords if kw_present(kw))
    if keyword_matches < 2:
        missing = [kw for kw in keywords if not kw_present(kw)][:2]
        hint = f"❌ Не хватает ключевых слов: «{'», «'.join(missing)}»"
        return False, hint

    user_unique = set(user_words)
    correct_unique = set(correct_words)
    common = user_unique & correct_unique
    similarity = len(common) / max(len(correct_unique), 1)

    if similarity >= 0.80:
        if similarity >= 0.95:
            return True, "✅ Точно! Дословное определение усвоено."
        else:
            return True, f"✅ Верно! Точная формулировка:\n«{correct_definition}»"
    else:
        missing_words = correct_unique - user_unique
        if len(missing_words) <= 2:
            hint = f"💡 Почти! Добавь слова: «{'», «'.join(list(missing_words)[:2])}»"
        else:
            hint = "💡 Почти! Сравни со стандартной формулировкой:"
        return False, f"{hint}\n\n📖 Правильное определение:\n«{correct_definition}»"

async def get_quiz_progress(user_id: int, term_hash: str) -> dict:
    async with db.execute(
        "SELECT * FROM quiz_progress WHERE user_id = ? AND term_hash = ?",
        (user_id, term_hash)
    ) as cursor:
        row = await cursor.fetchone()
        if row:
            return {
                "last_attempt": row["last_attempt"],
                "is_correct": bool(row["is_correct"]),
                "streak": row["streak"],
                "next_review": row["next_review"]
            }
    return {"last_attempt": "", "is_correct": False, "streak": 0, "next_review": ""}

QUIZ_INTERVALS = [1, 2, 4, 7]


def quiz_interval_days(streak: int) -> int:
    """Возвращает количество дней до следующего повторения для текущего streak."""
    return QUIZ_INTERVALS[min(max(streak, 0), len(QUIZ_INTERVALS) - 1)]


async def update_quiz_progress(user_id: int, term_hash: str, is_correct: bool, streak: int):
    now = datetime.now()
    if is_correct:
        interval = quiz_interval_days(streak)
        next_review = (now + timedelta(days=interval)).strftime("%Y-%m-%d")
    else:
        next_review = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    await db.execute(
        "INSERT INTO quiz_progress (user_id, term_hash, last_attempt, is_correct, streak, next_review) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(user_id, term_hash) DO UPDATE SET "
        "last_attempt=excluded.last_attempt, is_correct=excluded.is_correct, "
        "streak=excluded.streak, next_review=excluded.next_review",
        (user_id, term_hash, now.strftime("%Y-%m-%d %H:%M"), int(is_correct), streak, next_review)
    )
    await db.commit()

async def get_next_quiz_term(user_id: int, all_terms: list[QuizTerm]) -> QuizTerm | None:
    now = datetime.now().strftime("%Y-%m-%d")
    async with db.execute(
        "SELECT term_hash, next_review FROM quiz_progress WHERE user_id = ?",
        (user_id,)
    ) as cursor:
        records = await cursor.fetchall()
    record_dict = {r["term_hash"]: r["next_review"] for r in records}
    overdue = []
    new_terms = []
    for term in all_terms:
        if term.hash in record_dict:
            if record_dict[term.hash] and record_dict[term.hash] <= now:
                overdue.append(term)
        else:
            new_terms.append(term)
    if overdue:
        return overdue[0]
    if new_terms:
        return new_terms[0]
    # Все термины раздела уже отвечены и ещё не подошёл срок повторения —
    # возвращаем None, чтобы хендлер показал «всё повторено».
    return None

# ------------------------------------------------------------
# Хендлеры команд и меню
# ------------------------------------------------------------
@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    user_id = message.from_user.id

    # Telegram передаёт deep-link arg как «/start <arg>». Парсим до создания
    # пользователя — нужен для invite-link flow.
    text = message.text or ""
    parts = text.split(maxsplit=1)
    deep_link_arg = parts[1].strip() if len(parts) > 1 else None

    is_new = not await user_repo.user_exists(user_id)
    if is_new:
        await user_repo.create_user(
            user_id, username=message.from_user.username
        )
        logger.info("user.registered user_id=%s", user_id)
        await event_repo.log(user_id, "user_registered", {
            "language_code": message.from_user.language_code,
        })

    saved_locale = await user_repo.get_locale(user_id)
    if is_new or not saved_locale:
        await state.set_state(SetupStates.choosing_language)
        await message.answer(
            t("lang.picker_title_bilingual", "ru"),
            reply_markup=language_picker_keyboard(),
        )
    else:
        await _clear_custom_timer_duration_wait(state)
        locale = await loc(user_id)
        user = await user_repo.get_user(user_id)
        await apply_user_bot_commands(user_id)
        await message.answer(
            t(
                "start.welcome_back",
                locale,
                total_sessions=user["total_sessions"],
                total_coins=user["total_coins"],
                current_streak=user["current_streak"],
            ),
            reply_markup=get_main_keyboard(locale),
        )

    # Обработка deep-link invite после стандартного welcome.
    # Существующий FSM-стейт onboarding'а не трогаем — invite — side effect.
    if deep_link_arg and deep_link_arg.startswith("friend_"):
        await _process_friend_invite_link(message, deep_link_arg)


@router.callback_query(F.data.startswith("lang:set:"))
async def handle_language_choice(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    locale = callback.data.split(":")[-1]
    if locale not in SUPPORTED_LOCALES:
        await callback.answer()
        return
    await user_repo.set_locale(user_id, locale)
    await apply_user_bot_commands(user_id)
    await callback.answer()

    current = await state.get_state()
    if current == SetupStates.choosing_language.state:
        keyboard = ReplyKeyboardBuilder()
        keyboard.button(text=t("kb.setup_now", locale))
        keyboard.button(text=t("kb.start_now", locale))
        keyboard.adjust(1)
        await callback.message.answer(
            t("start.welcome_new", locale),
            reply_markup=keyboard.as_markup(resize_keyboard=True),
        )
        await state.set_state(SetupStates.choosing_path)
    else:
        lang_name = t("lang.ru", locale) if locale == "ru" else t("lang.en", locale)
        await callback.message.answer(
            t("lang.saved", locale, lang_name=lang_name),
            reply_markup=get_main_keyboard(locale),
        )


async def _process_friend_invite_link(message: Message, deep_link_arg: str) -> None:
    """
    Обрабатывает /start friend_<token>: резолвит токен, создаёт дружбу
    invitee + creator (skip pending state), уведомляет обе стороны.
    Вызывается из cmd_start после стандартного welcome flow.
    """
    invitee_id = message.from_user.id
    token = deep_link_arg[len("friend_"):]
    creator_id = await friend_repo.find_invite_token(token)
    if creator_id is None:
        locale = await loc(invitee_id)
        await message.answer(t("friends.invite_invalid", locale))
        return

    result = await friend_repo.accept_invite(creator_id, invitee_id)
    if result == "accepted":
        await event_repo.log(
            invitee_id,
            "friend_accepted",
            {"other_user_id": creator_id, "source": "invite_link"},
        )
        locale = await loc(invitee_id)
        await message.answer(
            t("friends.invite_accepted", locale, creator_id=creator_id),
            parse_mode="HTML",
        )
        try:
            creator_locale = await loc(creator_id)
            await bot.send_message(
                creator_id,
                t("friends.invite_notify_creator", creator_locale, invitee_id=invitee_id),
                parse_mode="HTML",
            )
        except Exception as e:
            logger.info(
                "friends.invite_notify_creator_failed creator=%s reason=%s",
                creator_id, type(e).__name__,
            )
    elif result == "already_friends":
        await message.answer(t("friends.already_friends", await loc(invitee_id)))
    elif result == "self":
        await message.answer(t("friends.own_link", await loc(invitee_id)))

@router.message(SetupStates.choosing_path, kb_in("kb.start_now"))
async def setup_skip(message: Message, state: FSMContext):
    """Пропуск мастера настройки — оставляем дефолтные 09:00 / 21:00."""
    locale = await loc(message.from_user.id)
    await state.clear()
    await message.answer(
        t("setup.skip_done", locale),
        reply_markup=get_main_keyboard(locale),
    )


@router.message(SetupStates.choosing_path, kb_in("kb.setup_now"))
async def setup_start(message: Message, state: FSMContext):
    """Начало мастера: спрашиваем утреннее время."""
    locale = await loc(message.from_user.id)
    await state.set_state(SetupStates.setting_morning)
    await message.answer(t("setup.morning_prompt", locale))


def _parse_time_or_none(text: str) -> str | None:
    """Возвращает нормализованное 'HH:MM' или None, если ввод невалиден."""
    match = TIME_RE.match((text or "").strip())
    if not match:
        return None
    return f"{int(match.group(1)):02d}:{int(match.group(2)):02d}"


@router.message(SetupStates.setting_morning, Command("skip"))
async def setup_skip_morning(message: Message, state: FSMContext):
    await state.update_data(morning_time=None)
    await _ask_evening(message, state)


@router.message(SetupStates.setting_morning)
async def setup_morning(message: Message, state: FSMContext):
    locale = await loc(message.from_user.id)
    normalized = _parse_time_or_none(message.text)
    if normalized is None:
        await message.answer(
            t("setup.invalid_time", locale, example="09:00", slot=t("setup.slot_morning", locale))
        )
        return
    await state.update_data(morning_time=normalized)
    await _ask_evening(message, state)


async def _ask_evening(message: Message, state: FSMContext):
    locale = await loc(message.from_user.id)
    await state.set_state(SetupStates.setting_evening)
    await message.answer(t("setup.evening_prompt", locale))


@router.message(SetupStates.setting_evening, Command("skip"))
async def setup_skip_evening(message: Message, state: FSMContext):
    await state.update_data(evening_time=None)
    await _finish_setup(message, state)


@router.message(SetupStates.setting_evening)
async def setup_evening(message: Message, state: FSMContext):
    locale = await loc(message.from_user.id)
    normalized = _parse_time_or_none(message.text)
    if normalized is None:
        await message.answer(
            t("setup.invalid_time", locale, example="21:00", slot=t("setup.slot_evening", locale))
        )
        return
    await state.update_data(evening_time=normalized)
    await _finish_setup(message, state)


async def _finish_setup(message: Message, state: FSMContext):
    locale = await loc(message.from_user.id)
    data = await state.get_data()
    morning = data.get("morning_time")
    evening = data.get("evening_time")
    user_id = message.from_user.id

    ns = NotificationSettings(user_id, user_repo)
    async with user_repo.db.lock:
        settings = await ns.load()
        if morning is None:
            settings["morning_enabled"] = 0
        else:
            settings["morning_enabled"] = 1
            settings["morning_time"] = morning
        if evening is None:
            settings["evening_enabled"] = 0
        else:
            settings["evening_enabled"] = 1
            settings["evening_time"] = evening
        await ns.save(settings)

    await state.clear()
    summary_lines = [t("setup.saved_header", locale)]
    summary_lines.append(
        t("setup.morning_on", locale, time=morning) if morning else t("setup.morning_off", locale)
    )
    summary_lines.append(
        t("setup.evening_on", locale, time=evening) if evening else t("setup.evening_off", locale)
    )
    summary_lines.append("\n" + t("setup.change_later", locale))
    await message.answer("\n".join(summary_lines), reply_markup=get_main_keyboard(locale))


def _faq_support_item(locale: str) -> dict[str, str]:
    return {
        "id": "support",
        "btn": t("faq.support.btn", locale),
        "title": t("faq.support.title", locale),
        "body": t("faq.support.body", locale),
    }


def _build_faq_menu_keyboard(locale: str) -> InlineKeyboardMarkup:
    """Главное меню FAQ — кнопки по faq_items + техподдержка."""
    kb = InlineKeyboardBuilder()
    for item in faq_items(locale):
        kb.button(text=item["btn"], callback_data=f"faq:show:{item['id']}")
    support = _faq_support_item(locale)
    kb.button(text=support["btn"], callback_data=f"faq:show:{support['id']}")
    kb.adjust(1)
    return kb.as_markup()


def _build_faq_answer_keyboard(locale: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=t("faq.back_list", locale), callback_data="faq:back")
    kb.adjust(1)
    return kb.as_markup()


def _faq_lookup(item_id: str, locale: str) -> dict | None:
    if item_id == "support":
        return _faq_support_item(locale)
    for item in faq_items(locale):
        if item["id"] == item_id:
            return item
    return None


@router.message(kb_in("kb.faq"))
async def handle_faq(message: Message, state: FSMContext):
    await _clear_custom_timer_duration_wait(state)
    locale = await loc(message.from_user.id)
    await message.answer(
        t("faq.menu", locale),
        reply_markup=_build_faq_menu_keyboard(locale),
    )


@router.callback_query(F.data.startswith("faq:show:"))
async def handle_faq_show(callback: CallbackQuery):
    locale = await loc(callback.from_user.id)
    item_id = callback.data.split(":", 2)[2]
    item = _faq_lookup(item_id, locale)
    if item is None:
        await callback.answer(t("errors.faq_not_found", locale), show_alert=True)
        return
    text = f"{item['title']}\n\n{item['body']}"
    try:
        await callback.message.edit_text(
            text, reply_markup=_build_faq_answer_keyboard(locale),
        )
    except Exception as e:
        logger.warning("faq.edit_failed item=%s reason=%s", item_id, e)
        await callback.message.answer(
            text, reply_markup=_build_faq_answer_keyboard(locale),
        )
    await callback.answer()


@router.callback_query(F.data == "faq:back")
async def handle_faq_back(callback: CallbackQuery):
    locale = await loc(callback.from_user.id)
    menu = t("faq.menu", locale)
    try:
        await callback.message.edit_text(
            menu, reply_markup=_build_faq_menu_keyboard(locale),
        )
    except Exception as e:
        logger.warning("faq.back_edit_failed reason=%s", e)
        await callback.message.answer(
            menu, reply_markup=_build_faq_menu_keyboard(locale),
        )
    await callback.answer()


@router.callback_query(F.data == "lang:picker")
async def handle_lang_picker_from_settings(callback: CallbackQuery):
    locale = await loc(callback.from_user.id)
    await callback.message.answer(
        t("lang.picker_title", locale),
        reply_markup=language_picker_keyboard(),
    )
    await callback.answer()


@router.message(kb_in("kb.news"))
async def handle_news(message: Message, state: FSMContext):
    await _clear_custom_timer_duration_wait(state)
    locale = await loc(message.from_user.id)
    kb = InlineKeyboardBuilder()
    kb.button(text=t("nav.open_channel", locale), url=CHANNEL_URL)
    await message.answer(t("nav.news_body", locale), reply_markup=kb.as_markup())


@router.message(kb_in("kb.profile"))
async def cmd_profile(message: Message, state: FSMContext):
    await _clear_custom_timer_duration_wait(state)
    user_id = message.from_user.id
    locale = await loc(user_id)
    user = await user_repo.get_user(user_id)
    if not user:
        await message.answer(t("start.need_register", locale))
        return
    await message.answer(
        _profile_title_text(user, user_id, locale),
        reply_markup=_build_profile_inline_keyboard(user_id, locale),
    )

# ------------------------------------------------------------
# Экран прогресса по предметам
# ------------------------------------------------------------
PROGRESS_BAR_FILLED = "🟩"
PROGRESS_BAR_EMPTY = "⬜"
PROGRESS_BAR_LENGTH = 10

# Пороги «выучено». Можно менять централизованно.
SITUATIONAL_MASTERY_STREAK = 3   # streak в quiz_progress
FLASHCARD_MASTERY_REPS = 3       # repetitions в flashcard_progress
USER_FLASHCARD_TERM_MAX = 200
USER_FLASHCARD_DEFINITION_MAX = 1000
USER_TASK_FILE_MAX_BYTES = 65536


def _render_bar(pct: float) -> str:
    """Рендерит progress-bar из 10 квадратов. pct в [0..1]."""
    pct = max(0.0, min(1.0, pct))
    filled = round(pct * PROGRESS_BAR_LENGTH)
    return PROGRESS_BAR_FILLED * filled + PROGRESS_BAR_EMPTY * (PROGRESS_BAR_LENGTH - filled)


def _humanize_when(ts_str: str | None, locale: str = "ru") -> str:
    """Localized relative activity time."""
    if not ts_str:
        return "—"
    try:
        ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return "—"
    now = datetime.now()
    delta_days = (now.date() - ts.date()).days
    if delta_days == 0:
        return t("progress.today_at", locale, time=ts.strftime("%H:%M"))
    if delta_days == 1:
        return t("progress.yesterday", locale)
    return t("progress.days_ago", locale, days=delta_days)


async def _count_situational_mastered(user_id: int, term_hashes: list[str]) -> int:
    """Сколько ситуационных терминов с streak ≥ SITUATIONAL_MASTERY_STREAK."""
    if not term_hashes:
        return 0
    placeholders = ",".join("?" * len(term_hashes))
    async with db.execute(
        f"SELECT COUNT(*) FROM quiz_progress "
        f"WHERE user_id = ? AND streak >= ? AND term_hash IN ({placeholders})",
        (user_id, SITUATIONAL_MASTERY_STREAK, *term_hashes),
    ) as cursor:
        row = await cursor.fetchone()
        return row[0] if row else 0


async def _count_flashcards_mastered(user_id: int, card_hashes: list[str]) -> int:
    if not card_hashes:
        return 0
    placeholders = ",".join("?" * len(card_hashes))
    async with db.execute(
        f"SELECT COUNT(*) FROM flashcard_progress "
        f"WHERE user_id = ? AND repetitions >= ? AND card_hash IN ({placeholders})",
        (user_id, FLASHCARD_MASTERY_REPS, *card_hashes),
    ) as cursor:
        row = await cursor.fetchone()
        return row[0] if row else 0


async def _count_situational_due(user_id: int, term_hashes: list[str]) -> int:
    """Overdue ситуационных терминов (next_review ≤ сегодня)."""
    if not term_hashes:
        return 0
    today = datetime.now().strftime("%Y-%m-%d")
    placeholders = ",".join("?" * len(term_hashes))
    async with db.execute(
        f"SELECT COUNT(*) FROM quiz_progress "
        f"WHERE user_id = ? AND next_review IS NOT NULL AND next_review <= ? "
        f"AND term_hash IN ({placeholders})",
        (user_id, today, *term_hashes),
    ) as cursor:
        row = await cursor.fetchone()
        return row[0] if row else 0


async def _count_flashcards_due(user_id: int, card_hashes: list[str]) -> int:
    """Overdue флэш-карт (next_review ≤ сейчас)."""
    if not card_hashes:
        return 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    placeholders = ",".join("?" * len(card_hashes))
    async with db.execute(
        f"SELECT COUNT(*) FROM flashcard_progress "
        f"WHERE user_id = ? AND next_review IS NOT NULL AND next_review <= ? "
        f"AND card_hash IN ({placeholders})",
        (user_id, now, *card_hashes),
    ) as cursor:
        row = await cursor.fetchone()
        return row[0] if row else 0


async def _build_subject_progress_block(
    user_id: int, subject_id: str, subject_label: str, locale: str = "ru",
) -> str:
    """
    Строит блок для одного предмета:
      <label>
      <bar> <pct>%
        🔔 К повторению ...
        🕐 Активность ...
        📈 Заходов ...
    Если контента нет — заглушка с «🚧 Скоро».
    """
    if subject_id in PROFILE_COMING_SOON_SUBJECTS:
        return (
            f"{subject_label}\n"
            f"{PROGRESS_BAR_EMPTY * PROGRESS_BAR_LENGTH}  0%\n"
            f"{t('progress.coming_soon', locale)}"
        )

    # Загружаем все items предмета. Используем существующие load_*-функции.
    section_terms: list[str] = []  # term_hash из всех непустых разделов situational
    for _label, key in available_quiz_sections(subject_id, locale):
        for term in load_quiz_section(key, subject_id):
            section_terms.append(term.hash)
    cards = load_flashcards(subject_id)
    user_cards = await user_flashcard_repo.list_by_subject(user_id, subject_id)
    mcq_qs = load_mcq(subject_id)
    tasks_list = load_tasks(subject_id)
    user_tasks_list = await user_task_repo.list_by_subject(user_id, subject_id)

    card_hashes = [c["hash"] for c in cards] + [c["hash"] for c in user_cards]
    mcq_hashes = [_mcq_hash(q["question"]) for q in mcq_qs]
    task_ids = [t["id"] for t in tasks_list] + [t["id"] for t in user_tasks_list]

    total = len(section_terms) + len(card_hashes) + len(mcq_hashes) + len(task_ids)
    if total == 0:
        return (
            f"{subject_label}\n"
            f"{PROGRESS_BAR_EMPTY * PROGRESS_BAR_LENGTH}  0%\n"
            f"{t('progress.coming_soon', locale)}"
        )

    # Сколько items освоено в каждом режиме
    mastered_sit   = await _count_situational_mastered(user_id, section_terms)
    mastered_cards = await _count_flashcards_mastered(user_id, card_hashes)
    mastered_mcq   = await mcq_repo.count_mastered(user_id, mcq_hashes)
    mastered_tasks = await task_repo.count_mastered(user_id, task_ids)
    total_mastered = mastered_sit + mastered_cards + mastered_mcq + mastered_tasks
    pct = total_mastered / total
    pct_int = round(pct * 100)

    # Сколько items overdue (только SRS-режимы)
    due_sit   = await _count_situational_due(user_id, section_terms)
    due_cards = await _count_flashcards_due(user_id, card_hashes)
    total_due = due_sit + due_cards

    # Активность из user_subject_stats
    stats = await subject_stats_repo.get(user_id, subject_id)
    visits = stats["visits"] if stats else 0
    last_activity = _humanize_when(stats["last_activity"] if stats else None, locale)

    lines = [
        subject_label,
        f"{_render_bar(pct)} {pct_int}%",
    ]
    if total_due > 0:
        lines.append(t("progress.due_today", locale, count=total_due))
    else:
        lines.append(t("progress.due_none", locale))
    lines.append(t("progress.activity", locale, when=last_activity))
    lines.append(t("progress.visits", locale, count=visits))
    return "\n".join(lines) + "\n"


async def build_progress_view(user_id: int, locale: str | None = None) -> str:
    """Полный текст экрана прогресса (Markdown/plain — без parse_mode)."""
    user_loc = locale or await loc(user_id)
    user = await user_repo.get_user(user_id)
    if not user:
        return t("start.need_register", user_loc)
    total_minutes = await session_repo.get_total_minutes(user_id)
    header = t(
        "progress.title", user_loc,
        coins=user["total_coins"],
        minutes=total_minutes,
        streak=user["current_streak"],
    )
    blocks = []
    for subject_id in SUBJECT_IDS:
        label = subject_label(subject_id, user_loc)
        blocks.append(
            await _build_subject_progress_block(user_id, subject_id, label, user_loc)
        )
    return header + "\n" + "\n".join(blocks)


@router.callback_query(F.data.startswith("show_progress:"))
async def handle_show_progress(callback: CallbackQuery):
    try:
        target_user_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer()
        return
    # Анти-spoof: пользователь видит только свой прогресс
    if callback.from_user.id != target_user_id:
        await callback.answer(
            t("progress.not_yours", await loc(callback.from_user.id)),
            show_alert=True,
        )
        return
    locale = await loc(target_user_id)
    text = await build_progress_view(target_user_id, locale)
    kb = InlineKeyboardBuilder()
    kb.button(text=t("profile.back", locale), callback_data=f"back_to_profile:{target_user_id}")
    kb.adjust(1)
    try:
        await callback.message.edit_text(text, reply_markup=kb.as_markup())
    except Exception:
        await callback.message.answer(text, reply_markup=kb.as_markup())
    await callback.answer()


# ------------------------------------------------------------


def get_pet_emotion(streak: int, locale: str = "ru") -> str:
    return pet_emotion(streak, locale)

# ------------------------------------------------------------
# Настройки
# ------------------------------------------------------------
async def _edit_or_answer_settings(
    callback: CallbackQuery, text: str, reply_markup: InlineKeyboardMarkup
) -> None:
    """edit_text с fallback на answer — как в FAQ/progress handlers."""
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup)
    except Exception as e:
        logger.warning("settings.render_failed user=%s err=%s", callback.from_user.id, e)
        await callback.message.answer(text, reply_markup=reply_markup)


class NotificationSettings:
    def __init__(self, user_id: int, repo: UserRepository):
        self.user_id = user_id
        self.repo = repo

    async def load(self) -> dict:
        row = await self.repo.get_notification_settings(self.user_id)
        if row:
            return dict(row)
        return {
            "morning_enabled": 1, "morning_time": "09:00",
            "evening_enabled": 1, "evening_time": "21:00",
            "streak_enabled": 1, "achievements_enabled": 1,
            "flashcard_source": "mix",
        }

    async def save(self, settings: dict):
        await self.repo.update_notification_settings(self.user_id, settings)

    async def toggle(self, setting_type: str, locale: str = "ru") -> tuple[bool, str]:
        key_map = {
            "morning": "morning_enabled",
            "evening": "evening_enabled",
            "streak": "streak_enabled",
            "achievements": "achievements_enabled"
        }
        label_keys = {
            "morning": "settings.label_morning",
            "evening": "settings.label_evening",
            "streak": "settings.label_streak",
            "achievements": "settings.label_achievements",
        }
        key = key_map.get(setting_type)
        if not key:
            raise ValueError(f"Unknown setting type: {setting_type}")
        async with self.repo.db.lock:
            settings = await self.load()
            current = settings.get(key, 1)
            new_value = 0 if current else 1
            settings[key] = new_value
            await self.save(settings)
        status = (
            t("settings.enabled", locale) if new_value else t("settings.disabled", locale)
        )
        label = t(label_keys[setting_type], locale)
        return bool(new_value), t("settings.toggle_status", locale, label=label, status=status)

    async def cycle_flashcard_source(self) -> tuple[str, str]:
        """mix → official → own → mix. Возвращает (label, source_key)."""
        async with self.repo.db.lock:
            settings = await self.load()
            current = settings.get("flashcard_source", "mix")
            if current not in FLASH_SOURCE_CYCLE:
                current = "mix"
            idx = FLASH_SOURCE_CYCLE.index(current)
            new_source = FLASH_SOURCE_CYCLE[(idx + 1) % len(FLASH_SOURCE_CYCLE)]
            settings["flashcard_source"] = new_source
            await self.save(settings)
        locale = await user_locale(self.repo, self.user_id)
        labels = flash_source_labels(locale)
        return labels[new_source], new_source

    async def set_time(self, slot: str, time_str: str) -> None:
        """Сохраняет утреннее/вечернее время. slot ∈ {'morning','evening'}."""
        if slot not in ("morning", "evening"):
            raise ValueError(f"Unknown slot: {slot}")
        async with self.repo.db.lock:
            settings = await self.load()
            settings[f"{slot}_time"] = time_str
            await self.save(settings)

    async def get_display_text(self) -> str:
        locale = await user_locale(self.repo, self.user_id)
        settings = await self.load()
        user = await self.repo.get_user(self.user_id)
        tz = (user or {}).get("timezone") or "Europe/Moscow"
        hidden = await self.repo.is_hidden_from_leaderboards(self.user_id)
        lines = [t("settings.title", locale)]
        emoji_on = {"morning": "🌅", "evening": "🌙", "streak": "🔥", "achievements": "🎉"}
        emoji_off = {"morning": "🌚", "evening": "🌚", "streak": "❄️", "achievements": "🔕"}
        time_keys = {"morning": "morning_time", "evening": "evening_time"}
        label_keys = {
            "morning": "settings.label_morning",
            "evening": "settings.label_evening",
            "streak": "settings.label_streak",
            "achievements": "settings.label_achievements",
        }
        for key in ["morning", "evening", "streak", "achievements"]:
            enabled = settings.get(f"{key}_enabled", 1)
            emoji = emoji_on[key] if enabled else emoji_off[key]
            time_str = ""
            if key in time_keys:
                time_val = settings.get(time_keys[key], "")
                time_str = f" ({time_val})" if time_val else ""
            status = t("settings.enabled", locale) if enabled else t("settings.disabled", locale)
            lines.append(f"{emoji} {t(label_keys[key], locale)}{time_str}: {status}")
        source = settings.get("flashcard_source", "mix")
        source_labels = flash_source_labels(locale)
        lines.append(t("settings.flashcards", locale, source=source_labels.get(source, source)))
        lines.append(t("settings.timezone", locale, tz=tz_label(tz)))
        lines.append(
            t("settings.leaderboard_hidden", locale)
            if hidden
            else t("settings.leaderboard_visible", locale)
        )
        saved = await self.repo.get_locale(self.user_id) or "ru"
        lang_label = t("lang.ru", locale) if saved == "ru" else t("lang.en", locale)
        lines.append(t("settings.language", locale) + f": {lang_label}")
        return "\n".join(lines)

    async def get_keyboard(self) -> InlineKeyboardMarkup:
        locale = await user_locale(self.repo, self.user_id)
        settings = await self.load()
        hidden = await self.repo.is_hidden_from_leaderboards(self.user_id)
        kb = InlineKeyboardBuilder()
        label_keys = {
            "morning": "settings.label_morning",
            "evening": "settings.label_evening",
            "streak": "settings.label_streak",
            "achievements": "settings.label_achievements",
        }
        toggle_off = t("settings.toggle_off", locale)
        toggle_on = t("settings.toggle_on", locale)
        for key in ["morning", "evening"]:
            enabled = settings.get(f"{key}_enabled", 1)
            kb.button(
                text=f"{t(label_keys[key], locale)}: {toggle_off if enabled else toggle_on}",
                callback_data=f"settings_toggle:{key}:{self.user_id}",
            )
            kb.button(
                text=t("settings.change_time", locale),
                callback_data=f"settings_time:{key}:{self.user_id}",
            )
        for key in ["streak", "achievements"]:
            enabled = settings.get(f"{key}_enabled", 1)
            kb.button(
                text=f"{t(label_keys[key], locale)}: {toggle_off if enabled else toggle_on}",
                callback_data=f"settings_toggle:{key}:{self.user_id}",
            )
        kb.button(text=t("settings.timezone_btn", locale), callback_data=f"settings_tz_picker:{self.user_id}")
        kb.button(
            text=(
                t("settings.leaderboard_btn_hidden", locale)
                if hidden
                else t("settings.leaderboard_btn_visible", locale)
            ),
            callback_data=f"settings_privacy:{self.user_id}",
        )
        source = settings.get("flashcard_source", "mix")
        source_labels = flash_source_labels(locale)
        kb.button(
            text=t("settings.flashcards", locale, source=source_labels.get(source, source)),
            callback_data=f"settings_flash_source:{self.user_id}",
        )
        saved = await self.repo.get_locale(self.user_id) or "ru"
        lang_label = t("lang.ru", locale) if saved == "ru" else t("lang.en", locale)
        kb.button(
            text=t("settings.language_btn", locale, current=lang_label),
            callback_data="lang:picker",
        )
        kb.button(text=t("settings.my_cards", locale), callback_data=f"fc_manage:{self.user_id}")
        kb.button(text=t("settings.my_tasks", locale), callback_data=f"ut_manage:{self.user_id}")
        kb.button(text=t("settings.back_profile", locale), callback_data=f"back_to_profile:{self.user_id}")
        kb.adjust(2, 2, 2, 1, 1, 1, 1, 1, 1)
        return kb.as_markup()

@router.callback_query(F.data.startswith("settings_menu:"))
async def show_settings_menu(callback: CallbackQuery):
    user_id = callback.from_user.id
    ns = NotificationSettings(user_id, user_repo)
    await _edit_or_answer_settings(
        callback,
        await ns.get_display_text(),
        await ns.get_keyboard(),
    )
    await callback.answer()

@router.callback_query(F.data.startswith("settings_toggle:"))
async def toggle_notification_setting(callback: CallbackQuery):
    _, setting_type, _ = callback.data.split(":")
    user_id = callback.from_user.id
    locale = await loc(user_id)
    ns = NotificationSettings(user_id, user_repo)
    try:
        new_value, status_text = await ns.toggle(setting_type, locale)
        key_map = {
            "morning": "morning_enabled",
            "evening": "evening_enabled",
            "streak": "streak_enabled",
            "achievements": "achievements_enabled",
        }
        await event_repo.log(
            callback.from_user.id,
            "settings_changed",
            {
                "setting": key_map.get(setting_type, setting_type),
                "value": int(new_value),
            },
        )
        await _edit_or_answer_settings(
            callback,
            await ns.get_display_text(),
            await ns.get_keyboard(),
        )
        await callback.answer()
    except Exception as e:
        logger.error(f"Error toggling setting: {e}")
        await callback.answer(t("settings.toggle_error", locale), show_alert=True)


@router.callback_query(F.data.startswith("settings_flash_source:"))
async def cycle_flashcard_source_setting(callback: CallbackQuery):
    try:
        target_user_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer()
        return
    if callback.from_user.id != target_user_id:
        await callback.answer(t("common.not_yours_settings", await loc(callback.from_user.id)), show_alert=True)
        return
    ns = NotificationSettings(target_user_id, user_repo)
    new_label, new_source = await ns.cycle_flashcard_source()
    await event_repo.log(
        target_user_id,
        "settings_changed",
        {"setting": "flashcard_source", "value": new_source},
    )
    await _edit_or_answer_settings(
        callback,
        await ns.get_display_text(),
        await ns.get_keyboard(),
    )
    locale = await loc(target_user_id)
    await callback.answer(t("settings.source_changed", locale, label=new_label))


def _subject_label_by_id(subject_id: str, locale: str = "ru") -> str:
    return subject_label(subject_id, locale)


async def _callback_allowlisted_subject(
    callback: CallbackQuery, subject_id: str,
) -> str | None:
    """Return allowlisted subject_id or alert and None."""
    validated = validate_subject_id(subject_id)
    if validated is None:
        await callback.answer(
            t("errors.state_error", await loc(callback.from_user.id)),
            show_alert=True,
        )
        return None
    return validated


def _build_fc_subject_picker_keyboard(user_id: int, prefix: str, locale: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for sid in SUBJECT_IDS:
        kb.button(text=subject_label(sid, locale), callback_data=f"{prefix}:{user_id}:{sid}")
    kb.adjust(1)
    return kb.as_markup()


async def _build_fc_list_text(user_id: int, subject_id: str, locale: str) -> str:
    cards = await user_flashcard_repo.list_by_subject(user_id, subject_id)
    subject_label = _subject_label_by_id(subject_id, locale)
    if not cards:
        return (
            f"{t('fc.list_title', locale, subject=subject_label)}\n\n"
            f"{t('fc.list_empty', locale)}"
        )
    lines = [
        t("fc.list_title", locale, subject=subject_label),
        t("fc.list_total", locale, count=len(cards)),
        "",
    ]
    for i, card in enumerate(cards, 1):
        lines.append(f"{i}. {sanitize_plain_preview(card['term'])}")
    return "\n".join(lines)


def _build_subject_fc_shortcuts_keyboard(
    user_id: int, subject_id: str, locale: str,
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=t("fc.add_btn", locale), callback_data=f"fc_add:{user_id}:{subject_id}")
    kb.button(text=t("fc.list_btn", locale), callback_data=f"fc_list:{user_id}:{subject_id}")
    kb.adjust(2)
    return kb.as_markup()


async def _maybe_send_subject_fc_shortcuts(
    message: Message, user_id: int, subject_id: str, locale: str,
) -> None:
    """Короткие inline для своих карточек — только если они уже есть по предмету."""
    if await user_flashcard_repo.count_by_subject(user_id, subject_id) <= 0:
        return
    await message.answer(
        t("nav.manage_cards", locale),
        reply_markup=_build_subject_fc_shortcuts_keyboard(user_id, subject_id, locale),
    )


async def _maybe_send_flash_mode_fc_shortcuts(
    message: Message, user_id: int, subject_id: str, locale: str,
) -> None:
    """При входе в флэш-карты без своих карточек — один раз предложить добавить."""
    if await user_flashcard_repo.count_by_subject(user_id, subject_id) > 0:
        return
    await message.answer(
        t("nav.manage_cards", locale),
        reply_markup=_build_subject_fc_shortcuts_keyboard(user_id, subject_id, locale),
    )


def _profile_title_text(user: dict, user_id: int, locale: str) -> str:
    last_session = user.get("last_session") or t("profile.never", locale)
    return t(
        "profile.title",
        locale,
        user_id=user_id,
        total_sessions=user["total_sessions"],
        total_coins=user["total_coins"],
        current_streak=user["current_streak"],
        last_session=last_session,
        pet_emotion=get_pet_emotion(user["current_streak"], locale),
    )


def _build_profile_inline_keyboard(user_id: int, locale: str) -> InlineKeyboardMarkup:
    inline_kb = InlineKeyboardBuilder()
    inline_kb.button(
        text=t("profile.achievements", locale),
        callback_data=f"show_achievements:{user_id}:1",
    )
    inline_kb.button(text=t("profile.settings", locale), callback_data=f"settings_menu:{user_id}")
    inline_kb.button(text=t("profile.progress", locale), callback_data=f"show_progress:{user_id}")
    inline_kb.button(
        text=t("profile.leaderboard", locale),
        callback_data=f"leaderboard_show:{user_id}",
    )
    inline_kb.button(text=t("profile.pet", locale), callback_data=f"pet_menu:{user_id}")
    inline_kb.button(text=t("profile.friends", locale), callback_data=f"friends_back:{user_id}")
    inline_kb.button(
        text=t("profile.freeze_streak", locale),
        callback_data=f"freeze_menu:{user_id}",
    )
    inline_kb.adjust(2, 2, 1, 2)
    return inline_kb.as_markup()


def _build_fc_list_keyboard(
    user_id: int, subject_id: str, cards: list[dict], locale: str,
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=t("fc.manage_add_btn", locale), callback_data=f"fc_add:{user_id}:{subject_id}")
    for card in cards:
        term_preview = card["term"][:30] + ("…" if len(card["term"]) > 30 else "")
        kb.button(
            text=f"🗑 {term_preview}",
            callback_data=f"fc_del:{user_id}:{subject_id}:{card['id']}",
        )
    kb.button(text=t("fc.back_subjects", locale), callback_data=f"fc_manage:{user_id}")
    kb.adjust(1)
    return kb.as_markup()


async def _start_flashcard_create_wizard(
    message: Message,
    state: FSMContext,
    user_id: int,
    subject_id: str,
    locale: str,
) -> None:
    subject_id = validate_subject_id(subject_id)
    if subject_id is None:
        await message.answer(t("errors.state_error", locale))
        return
    subject_label = _subject_label_by_id(subject_id, locale)
    await state.set_state(FlashcardCreateStates.waiting_for_term)
    await state.update_data(fc_subject_id=subject_id, fc_subject_label=subject_label)
    await message.answer(
        f"{t('fc.wizard_title', locale, subject=subject_label)}\n\n"
        f"{t('fc.term_prompt', locale, max=USER_FLASHCARD_TERM_MAX)}",
    )


@router.callback_query(F.data.startswith("fc_manage:"))
async def handle_fc_manage(callback: CallbackQuery):
    try:
        user_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer()
        return
    if callback.from_user.id != user_id:
        await callback.answer(t("common.not_yours_cards", await loc(callback.from_user.id)), show_alert=True)
        return
    locale = await loc(user_id)
    text = t("fc.manage_title", locale)
    kb = _build_fc_subject_picker_keyboard(user_id, "fc_list", locale)
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        await callback.message.answer(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("fc_list:"))
async def handle_fc_list(callback: CallbackQuery):
    try:
        _, user_id_str, subject_id = callback.data.split(":", 2)
        user_id = int(user_id_str)
    except (ValueError, IndexError):
        await callback.answer()
        return
    if callback.from_user.id != user_id:
        await callback.answer(t("common.not_yours_cards", await loc(callback.from_user.id)), show_alert=True)
        return
    subject_id = await _callback_allowlisted_subject(callback, subject_id)
    if subject_id is None:
        return
    locale = await loc(user_id)
    cards = await user_flashcard_repo.list_by_subject(user_id, subject_id)
    text = await _build_fc_list_text(user_id, subject_id, locale)
    kb = _build_fc_list_keyboard(user_id, subject_id, cards, locale)
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        await callback.message.answer(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("fc_add:"))
async def handle_fc_add(callback: CallbackQuery, state: FSMContext):
    try:
        _, user_id_str, subject_id = callback.data.split(":", 2)
        user_id = int(user_id_str)
    except (ValueError, IndexError):
        await callback.answer()
        return
    if callback.from_user.id != user_id:
        await callback.answer(t("common.not_yours_cards", await loc(callback.from_user.id)), show_alert=True)
        return
    subject_id = await _callback_allowlisted_subject(callback, subject_id)
    if subject_id is None:
        return
    locale = await loc(user_id)
    await callback.answer()
    await _start_flashcard_create_wizard(callback.message, state, user_id, subject_id, locale)


@router.callback_query(F.data.startswith("fc_del:"))
async def handle_fc_delete(callback: CallbackQuery):
    try:
        _, user_id_str, subject_id, card_id_str = callback.data.split(":", 3)
        user_id = int(user_id_str)
        card_id = int(card_id_str)
    except (ValueError, IndexError):
        await callback.answer()
        return
    if callback.from_user.id != user_id:
        await callback.answer(t("common.not_yours_cards", await loc(callback.from_user.id)), show_alert=True)
        return
    subject_id = await _callback_allowlisted_subject(callback, subject_id)
    if subject_id is None:
        return
    deleted = await user_flashcard_repo.delete(user_id, card_id)
    if deleted:
        await event_repo.log(
            user_id,
            "user_flashcard_deleted",
            {"subject_id": subject_id, "card_id": card_id},
        )
    locale = await loc(user_id)
    cards = await user_flashcard_repo.list_by_subject(user_id, subject_id)
    text = await _build_fc_list_text(user_id, subject_id, locale)
    kb = _build_fc_list_keyboard(user_id, subject_id, cards, locale)
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        await callback.message.answer(text, reply_markup=kb)
    locale = await loc(callback.from_user.id)
    await callback.answer(t("fc.deleted_ok", locale) if deleted else t("fc.deleted_fail", locale))


@router.message(FlashcardCreateStates.waiting_for_term)
async def handle_fc_term(message: Message, state: FSMContext):
    user_id = message.from_user.id
    locale = await loc(user_id)
    term = (message.text or "").strip()
    if not term:
        await message.answer(t("fc.term_empty", locale))
        return
    if len(term) > USER_FLASHCARD_TERM_MAX:
        await message.answer(t("fc.term_too_long", locale, max=USER_FLASHCARD_TERM_MAX))
        return
    await state.update_data(fc_term=term)
    await state.set_state(FlashcardCreateStates.waiting_for_definition)
    await message.answer(
        t("fc.definition_prompt", locale, max=USER_FLASHCARD_DEFINITION_MAX),
    )


@router.message(FlashcardCreateStates.waiting_for_definition)
async def handle_fc_definition(message: Message, state: FSMContext):
    user_id = message.from_user.id
    locale = await loc(user_id)
    definition = (message.text or "").strip()
    if not definition:
        await message.answer(t("fc.definition_empty", locale))
        return
    if len(definition) > USER_FLASHCARD_DEFINITION_MAX:
        await message.answer(
            t("fc.definition_too_long", locale, max=USER_FLASHCARD_DEFINITION_MAX),
        )
        return

    data = await state.get_data()
    subject_id = validate_subject_id(data.get("fc_subject_id") or "")
    if subject_id is None:
        await message.answer(t("errors.state_error", locale))
        await state.clear()
        return
    term = data.get("fc_term", "")

    try:
        card = await user_flashcard_repo.create(user_id, subject_id, term, definition)
    except ValueError as e:
        if str(e) == "limit_exceeded":
            await message.answer(
                t("fc.limit_reached", locale, max=UserFlashcardRepository.MAX_PER_SUBJECT),
            )
            await state.clear()
            return
        raise
    except sqlite3.IntegrityError:
        await message.answer(t("fc.duplicate_term", locale, term=term))
        await state.set_state(FlashcardCreateStates.waiting_for_term)
        return
    except Exception as e:
        logger.error("fc.create_failed user_id=%s reason=%s", user_id, e)
        await message.answer(t("fc.save_failed", locale))
        await state.clear()
        return

    await event_repo.log(
        user_id,
        "user_flashcard_created",
        {"subject_id": subject_id, "card_id": card["id"]},
    )
    await state.clear()

    kb = InlineKeyboardBuilder()
    kb.button(text=t("fc.add_more", locale), callback_data=f"fc_add:{user_id}:{subject_id}")
    kb.button(text=t("fc.list_btn", locale), callback_data=f"fc_list:{user_id}:{subject_id}")
    kb.button(text=t("fc.start_study", locale), callback_data=f"fc_study:{user_id}:{subject_id}")
    kb.adjust(1)
    await message.answer(
        t("fc.saved", locale, term=html_escape(term), definition=html_escape(definition)),
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data.startswith("fc_study:"))
async def handle_fc_study(callback: CallbackQuery, state: FSMContext):
    try:
        _, user_id_str, subject_id = callback.data.split(":", 2)
        user_id = int(user_id_str)
    except (ValueError, IndexError):
        await callback.answer()
        return
    if callback.from_user.id != user_id:
        await callback.answer(t("common.not_yours_session", await loc(callback.from_user.id)), show_alert=True)
        return
    subject_id = await _callback_allowlisted_subject(callback, subject_id)
    if subject_id is None:
        return
    locale = await loc(user_id)
    subj_lbl = _subject_label_by_id(subject_id, locale)
    await callback.answer()
    await state.set_state(QuizStates.choosing_mode)
    await state.update_data(subject_id=subject_id, subject_label=subj_lbl, mode_id="flashcards")
    await start_flashcard_session(
        callback.message, state, subject_id, subject_label=subj_lbl,
    )


def _build_ut_subject_picker_keyboard(user_id: int, prefix: str, locale: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for sid in SUBJECT_IDS:
        kb.button(text=subject_label(sid, locale), callback_data=f"{prefix}:{user_id}:{sid}")
    kb.adjust(1)
    return kb.as_markup()


async def _build_ut_list_text(user_id: int, subject_id: str, locale: str) -> str:
    tasks = await user_task_repo.list_by_subject(user_id, subject_id)
    subject_label = _subject_label_by_id(subject_id, locale)
    if not tasks:
        return (
            f"{t('user_tasks.list_title', locale, subject=subject_label)}\n\n"
            f"{t('user_tasks.list_empty', locale)}"
        )
    lines = [
        t("user_tasks.list_title", locale, subject=subject_label),
        t("user_tasks.list_total", locale, count=len(tasks)),
        "",
    ]
    for i, task in enumerate(tasks, 1):
        preview = sanitize_plain_preview(task["problem"], max_len=50)
        lines.append(f"{i}. {preview}")
    return "\n".join(lines)


def _build_ut_list_keyboard(
    user_id: int, subject_id: str, tasks: list[dict], locale: str,
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=t("user_tasks.import_btn", locale), callback_data=f"ut_import:{user_id}:{subject_id}")
    for task in tasks:
        db_id = int(task["id"][1:], 16)
        preview = sanitize_plain_preview(task["problem"], max_len=28)
        kb.button(
            text=f"🗑 {preview}",
            callback_data=f"ut_del:{user_id}:{subject_id}:{db_id}",
        )
    kb.button(text=t("user_tasks.start_solving_btn", locale), callback_data=f"ut_study:{user_id}:{subject_id}")
    kb.button(text=t("user_tasks.back_subjects", locale), callback_data=f"ut_manage:{user_id}")
    kb.adjust(1)
    return kb.as_markup()


@router.callback_query(F.data.startswith("ut_manage:"))
async def handle_ut_manage(callback: CallbackQuery):
    try:
        user_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer()
        return
    if callback.from_user.id != user_id:
        await callback.answer(t("common.not_yours_tasks", await loc(callback.from_user.id)), show_alert=True)
        return
    locale = await loc(user_id)
    text = t("user_tasks.manage_title", locale)
    kb = _build_ut_subject_picker_keyboard(user_id, "ut_list", locale)
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        await callback.message.answer(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("ut_list:"))
async def handle_ut_list(callback: CallbackQuery):
    try:
        _, user_id_str, subject_id = callback.data.split(":", 2)
        user_id = int(user_id_str)
    except (ValueError, IndexError):
        await callback.answer()
        return
    if callback.from_user.id != user_id:
        await callback.answer(t("common.not_yours_tasks", await loc(callback.from_user.id)), show_alert=True)
        return
    subject_id = await _callback_allowlisted_subject(callback, subject_id)
    if subject_id is None:
        return
    locale = await loc(user_id)
    tasks = await user_task_repo.list_by_subject(user_id, subject_id)
    text = await _build_ut_list_text(user_id, subject_id, locale)
    kb = _build_ut_list_keyboard(user_id, subject_id, tasks, locale)
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        await callback.message.answer(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("ut_import:"))
async def handle_ut_import_start(callback: CallbackQuery, state: FSMContext):
    try:
        _, user_id_str, subject_id = callback.data.split(":", 2)
        user_id = int(user_id_str)
    except (ValueError, IndexError):
        await callback.answer()
        return
    if callback.from_user.id != user_id:
        await callback.answer(t("common.not_yours_tasks", await loc(callback.from_user.id)), show_alert=True)
        return
    subject_id = await _callback_allowlisted_subject(callback, subject_id)
    if subject_id is None:
        return
    await callback.answer()
    locale = await loc(user_id)
    await state.set_state(UserTaskImportStates.waiting_for_file)
    await state.update_data(
        ut_subject_id=subject_id,
        ut_subject_label=_subject_label_by_id(subject_id, locale),
    )
    await callback.message.answer(
        t("user_tasks.instruction", locale),
        parse_mode="HTML",
    )


@router.message(UserTaskImportStates.waiting_for_file, Command("cancel"))
async def handle_ut_import_cancel(message: Message, state: FSMContext):
    user_id = message.from_user.id
    locale = await loc(user_id)
    await state.clear()
    await message.answer(
        t("user_tasks.import_cancelled", locale),
        reply_markup=get_main_keyboard(locale),
    )


@router.message(UserTaskImportStates.waiting_for_file, F.document)
async def handle_ut_import_file(message: Message, state: FSMContext):
    user_id = message.from_user.id
    locale = await loc(user_id)
    doc = message.document
    meta_err = validate_task_document_metadata(doc)
    if meta_err:
        await message.answer(t(meta_err, locale))
        return

    from io import BytesIO

    buffer = BytesIO()
    try:
        await asyncio.wait_for(bot.download(doc, destination=buffer), timeout=60)
    except Exception as e:
        logger.error("ut.import_download_failed user=%s reason=%s", user_id, e)
        await message.answer(t("user_tasks.download_error", locale))
        return

    text_content, decode_err = decode_task_upload(buffer.getvalue(), USER_TASK_FILE_MAX_BYTES)
    if decode_err:
        if decode_err == "user_tasks.file_too_big":
            await message.answer(
                t(decode_err, locale, max_kb=USER_TASK_FILE_MAX_BYTES // 1024),
            )
        else:
            await message.answer(t(decode_err, locale))
        return

    parsed, errors = parse_user_tasks_txt(text_content)
    if not parsed and errors:
        await message.answer(
            t("user_tasks.parse_no_valid", locale, errors="\n".join(errors[:10])),
        )
        return
    if not parsed:
        await message.answer(t("user_tasks.empty_file", locale))
        return

    data = await state.get_data()
    subject_id = validate_subject_id(data.get("ut_subject_id") or "")
    if subject_id is None:
        await state.clear()
        await message.answer(t("errors.state_error", locale))
        return
    added, err = await user_task_repo.bulk_create(user_id, subject_id, parsed)
    if err == "limit_exceeded":
        await message.answer(
            t("user_tasks.limit_exceeded", locale, max=UserTaskRepository.MAX_PER_SUBJECT),
        )
        await state.clear()
        return

    await state.clear()
    await event_repo.log(
        user_id,
        "user_tasks_imported",
        {"subject_id": subject_id, "count": added},
    )
    lines = [t("user_tasks.import_added", locale, count=added)]
    if errors:
        lines.append(t("user_tasks.import_skipped", locale, count=len(errors)))
        lines.append("\n".join(errors[:5]))
    kb = InlineKeyboardBuilder()
    kb.button(text=t("user_tasks.list_btn", locale), callback_data=f"ut_list:{user_id}:{subject_id}")
    kb.button(text=t("user_tasks.start_solving_btn", locale), callback_data=f"ut_study:{user_id}:{subject_id}")
    kb.adjust(1)
    await message.answer("\n\n".join(lines), reply_markup=kb.as_markup())


@router.message(UserTaskImportStates.waiting_for_file)
async def handle_ut_import_wrong(message: Message):
    locale = await loc(message.from_user.id)
    await message.answer(t("user_tasks.send_file", locale))


@router.callback_query(F.data.startswith("ut_del:"))
async def handle_ut_delete(callback: CallbackQuery):
    try:
        _, user_id_str, subject_id, task_db_id_str = callback.data.split(":", 3)
        user_id = int(user_id_str)
        task_db_id = int(task_db_id_str)
    except (ValueError, IndexError):
        await callback.answer()
        return
    if callback.from_user.id != user_id:
        await callback.answer(t("common.not_yours_tasks", await loc(callback.from_user.id)), show_alert=True)
        return
    subject_id = await _callback_allowlisted_subject(callback, subject_id)
    if subject_id is None:
        return
    deleted = await user_task_repo.delete(user_id, task_db_id)
    if deleted:
        await event_repo.log(
            user_id,
            "user_task_deleted",
            {"subject_id": subject_id, "task_db_id": task_db_id},
        )
    locale = await loc(user_id)
    tasks = await user_task_repo.list_by_subject(user_id, subject_id)
    text = await _build_ut_list_text(user_id, subject_id, locale)
    kb = _build_ut_list_keyboard(user_id, subject_id, tasks, locale)
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        await callback.message.answer(text, reply_markup=kb)
    locale = await loc(callback.from_user.id)
    await callback.answer(t("common.deleted", locale) if deleted else t("common.task_not_found", locale))


@router.callback_query(F.data.startswith("ut_study:"))
async def handle_ut_study(callback: CallbackQuery, state: FSMContext):
    try:
        _, user_id_str, subject_id = callback.data.split(":", 2)
        user_id = int(user_id_str)
    except (ValueError, IndexError):
        await callback.answer()
        return
    if callback.from_user.id != user_id:
        await callback.answer(t("common.not_yours_session", await loc(callback.from_user.id)), show_alert=True)
        return
    subject_id = await _callback_allowlisted_subject(callback, subject_id)
    if subject_id is None:
        return
    locale = await loc(user_id)
    subj_lbl = _subject_label_by_id(subject_id, locale)
    await callback.answer()
    await state.set_state(QuizStates.choosing_mode)
    await state.update_data(
        subject_id=subject_id,
        subject_label=subj_lbl,
        mode_id="tasks",
    )
    await start_task_session(
        callback.message, state, subject_id, subject_label=subj_lbl,
    )


@router.callback_query(F.data.startswith("freeze_menu:"))
async def freeze_menu(callback: CallbackQuery):
    """
    Экран details + confirm для покупки заморозки стрика
    (LEADERBOARD.md §Streak Freeze). Показывает текущий стрик, цену
    и баланс. Кнопка «Купить» появляется только когда покупка
    действительно возможна; иначе экран объясняет почему нет.
    """
    await callback.answer()
    user_id = callback.from_user.id
    user = await user_repo.get_user(user_id)
    if not user:
        return
    current_streak = user["current_streak"]
    balance = user["total_coins"]
    cost = freeze_cost(current_streak)

    has_active = await leaderboard_repo.has_active_freeze(user_id)
    cooldown_days = await leaderboard_repo.get_freeze_cooldown_remaining_days(user_id)

    lines = [
        "❄️ <b>Заморозка стрика</b>",
        "",
        f"🔥 Текущий стрик: <b>{current_streak}</b> дн.",
        f"💰 Баланс: <b>{balance}</b> 🪙",
        f"💸 Цена: <b>{cost}</b> 🪙",
        "",
    ]

    kb = InlineKeyboardBuilder()
    can_purchase = (
        not has_active and cooldown_days == 0 and balance >= cost
    )

    if has_active:
        lines.append(
            "✅ У тебя уже есть активная заморозка — "
            "сработает при следующем пропущенном дне."
        )
    elif cooldown_days > 0:
        lines.append(
            f"⏳ Кулдаун: следующая заморозка через "
            f"<b>{cooldown_days}</b> дн."
        )
    elif balance < cost:
        lines.append(f"❌ Не хватает <b>{cost - balance}</b> 🪙.")
    else:
        lines.append(
            "Заморозка сохранит стрик при ОДНОМ пропущенном дне. "
            "Покупка действует до использования."
        )

    if can_purchase:
        kb.button(
            text=f"✅ Купить за {cost} 🪙",
            callback_data=f"freeze_confirm:{user_id}",
        )
    kb.button(text="◀️ Профиль", callback_data=f"back_to_profile:{user_id}")
    kb.adjust(1)

    try:
        await callback.message.edit_text(
            "\n".join(lines),
            reply_markup=kb.as_markup(),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning(
            "freeze.menu_render_failed user=%s err=%s", user_id, e,
        )


@router.callback_query(F.data.startswith("freeze_confirm:"))
async def freeze_confirm(callback: CallbackQuery):
    """
    Атомарная покупка заморозки. Двойной тап безопасен:
    LeaderboardRepository.purchase_freeze под self.db.lock проверяет
    cooldown заново и вернёт 'cooldown_active' при повторе.
    """
    await callback.answer()
    user_id = callback.from_user.id
    user = await user_repo.get_user(user_id)
    if not user:
        return
    current_streak = user["current_streak"]
    result = await leaderboard_repo.purchase_freeze(user_id, current_streak)

    if result == "purchased":
        await event_repo.log(
            user_id,
            "freeze_purchased",
            {
                "cost": freeze_cost(current_streak),
                "streak": current_streak,
            },
        )
        text = (
            f"❄️ Заморозка куплена за <b>{freeze_cost(current_streak)}</b> 🪙.\n\n"
            f"🔥 Стрик: <b>{current_streak}</b> дн. — сохранится при "
            f"следующем пропущенном дне."
        )
    elif result == "insufficient_coins":
        text = "❌ Не хватает монет."
    elif result == "cooldown_active":
        text = "⏳ Заморозка уже покупалась в последние 7 дней."
    else:
        text = "Что-то пошло не так. Попробуй позже."

    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Профиль", callback_data=f"back_to_profile:{user_id}")
    try:
        await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    except Exception as e:
        logger.warning("freeze.confirm_render_failed user=%s err=%s", user_id, e)


@router.callback_query(F.data.startswith("settings_privacy:"))
async def toggle_privacy(callback: CallbackQuery):
    """Переключает users.hidden_from_leaderboards (LEADERBOARD.md §Privacy)."""
    user_id = callback.from_user.id
    current = await user_repo.is_hidden_from_leaderboards(user_id)
    new_hidden = not current
    await user_repo.set_hidden_from_leaderboards(user_id, new_hidden)
    await event_repo.log(
        user_id,
        "leaderboard_privacy_toggled",
        {"hidden": new_hidden},
    )
    ns = NotificationSettings(user_id, user_repo)
    try:
        await _edit_or_answer_settings(
            callback,
            await ns.get_display_text(),
            await ns.get_keyboard(),
        )
    except Exception as e:
        logger.warning("settings.privacy_toggle_render_failed user=%s err=%s", user_id, e)
    await callback.answer()


@router.callback_query(F.data.startswith("settings_time:"))
async def request_time_change(callback: CallbackQuery, state: FSMContext):
    """Просит пользователя ввести новое время для утреннего/вечернего слота."""
    _, slot, _ = callback.data.split(":")
    locale = await loc(callback.from_user.id)
    if slot not in ("morning", "evening"):
        await callback.answer(t("settings.unknown_slot", locale), show_alert=True)
        return
    slot_label = t(
        "settings.slot_morning" if slot == "morning" else "settings.slot_evening",
        locale,
    )
    await state.set_state(SettingsStates.waiting_for_time)
    await state.update_data(slot=slot, return_to="settings")
    await callback.message.answer(
        t("settings.time_change_prompt", locale, slot=slot_label),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("rate:"))
async def handle_session_rating(callback: CallbackQuery):
    """Сохраняет эмодзи-оценку только что завершённой сессии."""
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer()
        return
    try:
        session_id = int(parts[1])
        score = int(parts[2])
    except ValueError:
        await callback.answer()
        return
    if score < 1 or score > len(RATING_EMOJIS):
        await callback.answer()
        return
    user_id = callback.from_user.id
    updated = await session_repo.set_session_score(session_id, user_id, score)
    if not updated:
        # Сессия не принадлежит этому пользователю или не существует.
        await callback.answer(t("rating.save_failed", await loc(user_id)), show_alert=True)
        return
    logger.info("session.rated user_id=%s session_id=%s score=%s", user_id, session_id, score)
    emoji = next((e for s, e in RATING_EMOJIS if s == score), "")
    try:
        await callback.message.edit_text(t("rating.thanks", await loc(user_id), emoji=emoji))
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("rate_skip:"))
async def handle_session_rating_skip(callback: CallbackQuery):
    """Пропуск оценки — просто убираем клавиатуру."""
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@router.callback_query(F.data.startswith("settings_tz_picker:"))
async def show_tz_picker(callback: CallbackQuery):
    """Показывает inline-клавиатуру со списком часовых поясов."""
    await callback.answer()
    user_id = callback.from_user.id
    kb = InlineKeyboardBuilder()
    for tz_id, label in TZ_PRESETS:
        kb.button(text=label, callback_data=f"settings_tz_set:{tz_id}")
    locale = await loc(user_id)
    kb.button(text=t("settings.tz_back", locale), callback_data=f"settings_menu:{user_id}")
    kb.adjust(1)
    await callback.message.edit_text(
        t("settings.tz_picker_title", locale),
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data.startswith("settings_tz_set:"))
async def set_user_timezone(callback: CallbackQuery):
    """Сохраняет выбранный TZ и возвращается в меню настроек."""
    tz_id = callback.data.split(":", 1)[1]
    if tz_id not in TZ_IDS:
        await callback.answer(t("common.unknown_tz", await loc(callback.from_user.id)), show_alert=True)
        return
    user_id = callback.from_user.id
    await user_repo.set_timezone(user_id, tz_id)
    await event_repo.log(
        user_id,
        "settings_changed",
        {"setting": "timezone", "value": tz_id},
    )
    ns = NotificationSettings(user_id, user_repo)
    await _edit_or_answer_settings(
        callback,
        await ns.get_display_text(),
        await ns.get_keyboard(),
    )
    await callback.answer()


@router.message(SettingsStates.waiting_for_time, Command("cancel"))
async def cancel_time_input(message: Message, state: FSMContext):
    user_id = message.from_user.id
    locale = await loc(user_id)
    await state.clear()
    await message.answer(
        t("common.cancelled", locale),
        reply_markup=get_main_keyboard(locale),
    )


@router.message(SettingsStates.waiting_for_time)
async def process_time_input(message: Message, state: FSMContext):
    user_id = message.from_user.id
    locale = await loc(user_id)
    text = (message.text or "").strip()
    match = TIME_RE.match(text)
    if not match:
        await message.answer(t("settings.time_invalid", locale))
        return
    # Нормализуем "9:5" → "09:05"
    hours, minutes = match.group(1), match.group(2)
    normalized = f"{int(hours):02d}:{int(minutes):02d}"

    data = await state.get_data()
    slot = data.get("slot")
    if slot not in ("morning", "evening"):
        await state.clear()
        await message.answer(
            t("errors.state_error", locale),
            reply_markup=get_main_keyboard(locale),
        )
        return
    ns = NotificationSettings(user_id, user_repo)
    await ns.set_time(slot, normalized)
    await event_repo.log(
        user_id,
        "settings_changed",
        {"setting": f"{slot}_time", "value": normalized},
    )
    await state.clear()

    slot_label = t(
        "settings.slot_morning" if slot == "morning" else "settings.slot_evening",
        locale,
    )
    await message.answer(
        t("settings.time_saved", locale, slot=slot_label, time=normalized),
        reply_markup=get_main_keyboard(locale),
    )

@router.callback_query(F.data.startswith("back_to_profile:"))
async def back_to_profile(callback: CallbackQuery):
    try:
        user_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer()
        return
    if callback.from_user.id != user_id:
        await callback.answer(t("common.not_yours_profile", await loc(callback.from_user.id)), show_alert=True)
        return
    user = await user_repo.get_user(user_id)
    if not user:
        await callback.message.answer("Пользователь не найден")
        return
    locale = await loc(user_id)
    text = _profile_title_text(user, user_id, locale)
    markup = _build_profile_inline_keyboard(user_id, locale)
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except Exception as e:
        logger.warning("profile.back_render_failed user=%s err=%s", user_id, e)
        await callback.message.answer(text, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data.startswith("leaderboard_show:"))
async def leaderboard_show_from_profile(callback: CallbackQuery):
    try:
        user_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer()
        return
    if callback.from_user.id != user_id:
        await callback.answer(
            t("common.not_yours_profile", await loc(callback.from_user.id)),
            show_alert=True,
        )
        return
    locale = await loc(user_id)
    try:
        text = await leaderboard_service.render_leaderboard(user_id)
        await callback.message.answer(text, parse_mode="HTML")
        await event_repo.log(user_id, "leaderboard_viewed", {"source": "profile"})
    except Exception as e:
        logger.warning(
            "leaderboard.render_failed user=%s source=profile err=%s",
            user_id, e,
        )
        await callback.message.answer(t("leaderboard.load_failed", locale))
    await callback.answer()


# ------------------------------------------------------------
# Достижения
# ------------------------------------------------------------
@router.callback_query(F.data.startswith("show_achievements:"))
async def show_achievements(callback: CallbackQuery):
    parts = callback.data.split(":")
    try:
        user_id = int(parts[1])
        page = int(parts[2]) if len(parts) > 2 else 1
    except (ValueError, IndexError):
        await callback.answer()
        return
    if callback.from_user.id != user_id:
        await callback.answer(t("common.not_yours_achievements", await loc(callback.from_user.id)), show_alert=True)
        return

    async with db.execute(
        "SELECT achievement_id, completed, progress, target FROM user_achievements WHERE user_id = ?",
        (user_id,)
    ) as cursor:
        rows = await cursor.fetchall()
    user_achievements = {}
    for row in rows:
        user_achievements[row["achievement_id"]] = {
            "completed": bool(row["completed"]),
            "progress": row["progress"],
            "target": row["target"]
        }

    page_achievements = {k: v for k, v in ACHIEVEMENTS.items() if v.get("page", 1) == page}
    text = f"🏆 Достижения (страница {page}/3)\n\n"
    for ach_id, ach_data in page_achievements.items():
        ach_name = ach_data["name"]
        ach_desc = ach_data["description"]
        ach_reward = ach_data["reward"]
        ach_icon = ach_data["icon"]
        status = user_achievements.get(ach_id)
        if status and status["completed"]:
            text += f"✅ {ach_icon} {ach_name}\n   {ach_desc}\n   🪙 +{ach_reward} — ПОЛУЧЕНО!\n\n"
        elif status:
            progress = status["progress"]
            target = status["target"]
            text += f"⏳ {ach_icon} {ach_name}\n   {ach_desc}\n   🪙 +{ach_reward} — {progress}/{target}\n\n"
        else:
            text += f"🔒 {ach_icon} {ach_name}\n   {ach_desc}\n   🪙 +{ach_reward} — ЗАБЛОКИРОВАНО\n\n"

    keyboard = InlineKeyboardBuilder()
    for p in range(1, 4):
        if p != page:
            keyboard.button(text=str(p), callback_data=f"show_achievements:{user_id}:{p}")
    keyboard.button(text="◀️ Профиль", callback_data=f"back_to_profile:{user_id}")
    keyboard.adjust(3, 1)
    try:
        await callback.message.edit_text(text, reply_markup=keyboard.as_markup())
    except Exception:
        await callback.message.answer(text, reply_markup=keyboard.as_markup())
    await callback.answer()

# ------------------------------------------------------------
# Таймеры
# ------------------------------------------------------------
async def run_timer_task(chat_id: int, state: FSMContext, user_id: int, duration: int):
    this_task = asyncio.current_task()
    try:
        # Спим до момента (start_time + duration). Для свежего таймера
        # start_time только что записан в state.data → remaining = duration*60.
        # Для таймера, восстановленного после рестарта в reconcile_stale_timers,
        # start_time остался от прошлого запуска → remaining = сколько осталось.
        data = await state.get_data()
        start_time = data.get("start_time")
        stored_duration = _normalize_timer_duration(data.get("duration", duration))
        if stored_duration is not None:
            duration = stored_duration
        if not isinstance(start_time, datetime):
            pending = pending_timer_sessions.get(user_id)
            if pending and isinstance(pending.get("start_time"), datetime):
                start_time = pending["start_time"]
                duration = (
                    _normalize_timer_duration(pending.get("duration", duration))
                    or duration
                )
            else:
                logger.warning("timer.invalid_start_time user_id=%s", user_id)
                await _claim_timer_session(state, user_id)
                return
        deadline = start_time + timedelta(minutes=duration)
        remaining_sec = max(0, (deadline - datetime.now()).total_seconds())
        await asyncio.sleep(remaining_sec)
        claimed = await _claim_active_timer(state, user_id)
        if claimed is None:
            return
        duration = _normalize_timer_duration(claimed.get("duration", duration)) or duration
        earned, bonus, session_id = await study_service.complete_session(user_id, duration)
        logger.info(
            "session.complete user_id=%s duration=%s coins=%s bonus=%s session_id=%s achievements=%s source=natural",
            user_id, duration, duration, bonus, session_id, len(earned),
        )
        await event_repo.log(user_id, "session_completed", {
            "duration": duration, "coins": duration, "bonus_coins": bonus,
            "session_id": session_id, "achievements_earned": len(earned),
            "source": "natural",
        })
        for ach_id in earned:
            await event_repo.log(user_id, "achievement_unlocked", {"achievement_id": ach_id})
        user = await user_repo.get_user(user_id)
        locale = await loc(user_id)
        response = t("timer.finished", locale, duration=duration)
        if bonus > 0:
            response += t("timer.bonus", locale, bonus=bonus)
        response += t("timer.total_coins", locale, total_coins=user["total_coins"])
        try:
            await _send_with_retry_after(
                lambda: bot.send_message(
                    chat_id, response, reply_markup=get_main_keyboard(locale),
                ),
                label="timer_finished", uid=user_id,
            )
        except TelegramForbiddenError:
            logger.info("timer.notify_failed user_id=%s reason=blocked", user_id)
        except Exception as e:
            logger.error(f"Ошибка отправки завершения таймера {user_id}: {e}")
        if earned:
            await send_achievement_notification(user_id, earned)
        if session_id:
            await send_rating_prompt(chat_id, session_id, user_id)
    except asyncio.CancelledError:
        # Нормальный shutdown / переход в новый таймер — наружу не пробрасываем,
        # _log_task_exception тоже игнорирует cancelled().
        pass
    except Exception:
        # Без этой ветки исключение в complete_session / send_message / event_repo
        # тихо убило бы задачу, и пользователь потерял бы сессию без следов.
        # add_done_callback тоже сработает, но дублируем здесь, чтобы тайминг
        # был ясен из bot.log (видно, на каком шаге упало).
        logger.exception(
            "timer.task_crashed user_id=%s duration=%s", user_id, duration,
        )
    finally:
        if this_task is not None:
            _release_active_timer_slot(user_id, this_task)


def start_timer(chat_id: int, state: FSMContext, user_id: int, duration: int) -> None:
    """Отменяет старый таймер пользователя (если есть) и запускает новый."""
    old = active_timers.get(user_id)
    if old and not old.done():
        old.cancel()
    task = asyncio.create_task(
        run_timer_task(chat_id, state, user_id, duration),
        name=f"timer-{user_id}",
    )
    task.add_done_callback(_log_task_exception)
    active_timers[user_id] = task

async def send_achievement_notification(user_id: int, achievement_ids: list):
    settings = await user_repo.get_notification_settings(user_id)
    if settings and not settings.get("achievements_enabled", 1):
        return
    locale = await loc(user_id)
    catalog = load_achievements_catalog(locale)
    default_name = t("achievements_notify.default_name", locale)
    if len(achievement_ids) == 1:
        ach_id = achievement_ids[0]
        ach = catalog.get(ach_id, {})
        msg = t(
            "achievements_notify.single",
            locale,
            icon=ach.get("icon", "🏆"),
            name=ach.get("name", default_name),
            description=ach.get("description", ""),
            reward=ach.get("reward", 0),
        )
    else:
        achievements_list = []
        total_reward = 0
        for ach_id in achievement_ids:
            ach = catalog.get(ach_id, {})
            achievements_list.append(
                t(
                    "achievements_notify.multiple_item",
                    locale,
                    icon=ach.get("icon", "🏆"),
                    name=ach.get("name", default_name),
                    reward=ach.get("reward", 0),
                )
            )
            total_reward += ach.get("reward", 0)
        msg = (
            t("achievements_notify.multiple_header", locale)
            + "\n".join(achievements_list)
            + t("achievements_notify.multiple_footer", locale, total_reward=total_reward)
        )
    try:
        await bot.send_message(user_id, msg)
    except Exception as e:
        logger.error(f"Ошибка отправки уведомления о достижении {user_id}: {e}")

@router.message(kb_in("kb.standard_timer"))
async def handle_standard_timer(message: Message, state: FSMContext):
    user_id = message.from_user.id
    if not await user_repo.user_exists(user_id):
        await user_repo.create_user(
            user_id, username=message.from_user.username
        )
        await apply_user_bot_commands(user_id)
    locale = await loc(user_id)
    current_state = await state.get_state()
    if current_state == TimerStates.waiting_for_duration.state:
        await state.clear()
    pending = pending_timer_sessions.get(user_id)
    if pending is not None:
        await message.answer(
            t("timer.already_running", locale, remaining=_timer_remaining_minutes(pending)),
            reply_markup=get_timer_active_keyboard(locale),
        )
        return
    if current_state == TimerStates.active.state:
        data = await state.get_data()
        start_time = data.get("start_time")
        if not start_time:
            await message.answer(
                t("timer.corrupted", locale),
                reply_markup=get_study_keyboard(locale),
            )
            await state.clear()
            return
        duration_running = _normalize_timer_duration(data.get("duration", 25)) or 25
        _ensure_timer_task_running(message.chat.id, state, user_id, duration_running)
        await message.answer(
            t("timer.already_running", locale, remaining=_timer_remaining_minutes(data)),
            reply_markup=get_timer_active_keyboard(locale),
        )
        return
    if await _desktop_timer_blocks_start(message, locale):
        return
    duration = 25
    await state.set_state(TimerStates.active)
    await state.update_data(duration=duration, start_time=datetime.now())
    await message.answer(
        t("timer.started", locale, duration=duration),
        reply_markup=get_timer_active_keyboard(locale),
    )
    await event_repo.log(user_id, "session_started", {"duration": duration, "kind": "standard"})
    start_timer(message.chat.id, state, user_id, duration)

@router.message(kb_in("kb.custom_timer"))
async def handle_custom_timer_start(message: Message, state: FSMContext):
    user_id = message.from_user.id
    locale = await loc(user_id)
    current_state = await state.get_state()
    if current_state == TimerStates.waiting_for_duration.state:
        await state.clear()
    pending = pending_timer_sessions.get(user_id)
    if pending is not None:
        await message.answer(
            t("timer.already_running", locale, remaining=_timer_remaining_minutes(pending)),
            reply_markup=get_timer_active_keyboard(locale),
        )
        return
    if current_state == TimerStates.active.state:
        data = await state.get_data()
        start_time = data.get("start_time")
        if not start_time:
            await message.answer(
                t("timer.corrupted", locale),
                reply_markup=get_study_keyboard(locale),
            )
            await state.clear()
            return
        duration_running = _normalize_timer_duration(data.get("duration", 25)) or 25
        _ensure_timer_task_running(message.chat.id, state, user_id, duration_running)
        await message.answer(
            t("timer.already_running", locale, remaining=_timer_remaining_minutes(data)),
            reply_markup=get_timer_active_keyboard(locale),
        )
        return
    if await _desktop_timer_blocks_start(message, locale):
        return
    await message.answer(t("timer.custom_ask", locale))
    await state.set_state(TimerStates.waiting_for_duration)

async def _desktop_timer_blocks_start(message: Message, locale: str) -> bool:
    """
    True, если таймер уже идёт в desktop-приложении — тогда не запускаем
    второй в Telegram и объясняем почему.

    Зеркало проверки в api.handle_pomodoro_start: два параллельных таймера
    начисляли бы монеты, XP и очки лидерборда за одно и то же время дважды.
    Ошибка чтения не должна ронять запуск таймера — в худшем случае просто
    не сработает защита, это лучше, чем неработающая кнопка.
    """
    if desktop_timer_repo is None:
        return False  # main() ещё не отработал (в тестах импорта)
    try:
        state = await desktop_timer_repo.get(message.from_user.id)
    except Exception as e:
        logger.warning(
            "timer.desktop_check_failed user=%s reason=%s",
            message.from_user.id, type(e).__name__,
        )
        return False
    if not state or state["remaining_seconds"] <= 0:
        return False
    remaining_min = max(1, -(-state["remaining_seconds"] // 60))  # ceil
    await message.answer(
        t("timer.desktop_running", locale, remaining=remaining_min),
        reply_markup=get_study_keyboard(locale),
    )
    return True


async def stop_active_timer(message: Message, state: FSMContext) -> bool:
    """
    Останавливает активный таймер пользователя, начисляет монеты и достижения.
    Возвращает True, если таймер был остановлен; False, если активного таймера не было.

    ВАЖНО: не трогаем FSM-state, если пользователь сейчас в другом flow
    (MCQ / photo-task / квиз). До v0.7 этот хелпер делал state.clear() безусловно,
    что ломало MCQ/task-сессии при вызове /stop.
    """
    user_id = message.from_user.id
    task = active_timers.pop(user_id, None)
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    claimed = await _claim_timer_session(state, user_id)
    if claimed is None:
        return False
    start_time = claimed.get("start_time")
    if not isinstance(start_time, datetime):
        await state.clear()
        return False
    duration = _normalize_timer_duration(claimed.get("duration", 25)) or 25
    elapsed = max(0, (datetime.now() - start_time).total_seconds() / 60)
    actual = min(int(elapsed), duration)
    locale = await loc(user_id)
    if actual < 1:
        await message.answer(
            t("timer.too_short", locale),
            reply_markup=get_main_keyboard(locale),
        )
        return True
    earned, bonus, session_id = await study_service.complete_session(user_id, actual)
    logger.info(
        "session.complete user_id=%s duration=%s coins=%s bonus=%s session_id=%s achievements=%s source=stop",
        user_id, actual, actual, bonus, session_id, len(earned),
    )
    await event_repo.log(user_id, "session_completed", {
        "duration": actual, "coins": actual, "bonus_coins": bonus,
        "session_id": session_id, "achievements_earned": len(earned),
        "source": "stop",
    })
    for ach_id in earned:
        await event_repo.log(user_id, "achievement_unlocked", {"achievement_id": ach_id})
    user = await user_repo.get_user(user_id)
    response = t("timer.stopped", locale, actual=actual)
    if bonus > 0:
        response += t("timer.bonus", locale, bonus=bonus)
    response += t("timer.total_coins", locale, total_coins=user["total_coins"])
    await message.answer(response, reply_markup=get_main_keyboard(locale))
    if earned:
        await send_achievement_notification(user_id, earned)
    if session_id:
        await send_rating_prompt(message.chat.id, session_id, message.from_user.id)
    return True


@router.message(TimerStates.active, kb_in("kb.stop_timer"))
async def handle_stop_timer(message: Message, state: FSMContext):
    user_id = message.from_user.id
    locale = await loc(user_id)
    if not await stop_active_timer(message, state):
        await message.answer(
            t("timer.already_done", locale),
            reply_markup=get_study_keyboard(locale),
        )


@router.message(Command("stop"))
async def cmd_stop(message: Message, state: FSMContext):
    """Останавливает активный таймер из любого места (например, из главного меню)."""
    user_id = message.from_user.id
    locale = await loc(user_id)
    if await state.get_state() == TimerStates.waiting_for_duration.state:
        await state.clear()
        await message.answer(
            t("timer.no_active", locale),
            reply_markup=get_study_keyboard(locale),
        )
        return
    if not await stop_active_timer(message, state):
        await message.answer(
            t("timer.no_active", locale),
            reply_markup=get_main_keyboard(locale),
        )


# ------------------------------------------------------------
# /delete_account — самостоятельная реализация GDPR Art. 17 / 152-ФЗ ст. 14
# (право на стирание). Two-step confirm чтобы исключить случайное
# удаление от мисс-тапа в /-пикере. Главный админ заблокирован: он
# должен сначала сменить MAIN_ADMIN_ID в .env, иначе бот станет
# неуправляемым после удаления.
# ------------------------------------------------------------
@router.message(Command("delete_account"))
async def cmd_delete_account(message: Message):
    user_id = message.from_user.id
    locale = await loc(user_id)

    if user_id == MAIN_ADMIN_ID:
        await message.answer(
            t("delete_account.main_admin_blocked", locale),
            parse_mode="HTML",
        )
        return

    if not await user_repo.user_exists(user_id):
        await message.answer(t("delete_account.no_data", locale))
        return

    kb = InlineKeyboardBuilder()
    kb.button(
        text=t("delete_account.confirm_btn", locale),
        callback_data=f"delete_account_confirm:{user_id}",
    )
    kb.button(
        text=t("delete_account.cancel_btn", locale),
        callback_data=f"delete_account_cancel:{user_id}",
    )
    kb.adjust(1)
    await message.answer(
        t("delete_account.confirm_prompt", locale),
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data.startswith("delete_account_cancel:"))
async def handle_delete_account_cancel(callback: CallbackQuery):
    try:
        target_uid = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer()
        return
    # Anti-spoof: чужой callback просто молча игнорим (никакого alert'а
    # — мы не хотим, чтобы посторонний знал, что у user'а есть pending
    # delete confirm).
    if target_uid != callback.from_user.id:
        await callback.answer()
        return
    locale = await loc(callback.from_user.id)
    try:
        await callback.message.edit_text(
            t("delete_account.cancelled", locale), reply_markup=None,
        )
    except TelegramBadRequest:
        await callback.message.answer(t("delete_account.cancelled", locale))
    await callback.answer()


@router.callback_query(F.data.startswith("delete_account_confirm:"))
async def handle_delete_account_confirm(callback: CallbackQuery, state: FSMContext):
    try:
        target_uid = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer()
        return
    if target_uid != callback.from_user.id:
        await callback.answer()
        return

    user_id = callback.from_user.id

    # Capture locale BEFORE delete — после стирания get_locale вернёт ''
    # и UI вынужденно упадёт на ru fallback.
    locale = await loc(user_id)

    # Двойная защита: если main-admin как-то всё-таки добрался до
    # confirm-кнопки (нажал из старого сообщения после смены
    # MAIN_ADMIN_ID — маловероятно, но), всё равно блокируем.
    if user_id == MAIN_ADMIN_ID:
        try:
            await callback.message.edit_text(
                t("delete_account.main_admin_blocked", locale),
                parse_mode="HTML",
                reply_markup=None,
            )
        except TelegramBadRequest:
            await callback.message.answer(
                t("delete_account.main_admin_blocked", locale),
                parse_mode="HTML",
            )
        await callback.answer()
        return

    # Снимаем активный pomodoro-таймер: задача держит ссылку на FSM
    # и на user_id и после удаления продолжила бы пытаться писать
    # в стертую БД-строку.
    timer_task = active_timers.pop(user_id, None)
    pending_timer_sessions.pop(user_id, None)
    if timer_task is not None and not timer_task.done():
        timer_task.cancel()

    # FSM-state в памяти — clear; persistent fsm_storage будет
    # вычищена в delete_user_completely.
    try:
        await state.clear()
    except Exception:
        logger.debug("delete_account.state_clear_failed user_id=%s", user_id)

    # admins-таблица не имеет FK на users → удалить руками. И в
    # in-memory кеш ADMINS тоже.
    ADMINS.discard(user_id)
    try:
        await admin_repo.remove(user_id)
    except Exception as e:
        logger.warning(
            "delete_account.admin_remove_failed user_id=%s reason=%s",
            user_id, type(e).__name__,
        )

    counts = await user_repo.delete_user_completely(user_id)
    logger.info("account.deleted user_id=%s counts=%s", user_id, counts)

    try:
        await callback.message.edit_text(
            t("delete_account.done", locale), reply_markup=None,
        )
    except TelegramBadRequest:
        await callback.message.answer(t("delete_account.done", locale))
    await callback.answer()


@router.message(TimerStates.active, kb_in("kb.back_main"))
async def handle_back_to_menu_during_timer(message: Message, state: FSMContext):
    locale = await loc(message.from_user.id)
    await message.answer(
        t("timer.back_menu", locale),
        reply_markup=get_main_keyboard(locale),
    )

# ------------------------------------------------------------
# Подготовка — из главного меню: предмет → режим → сессия
# ------------------------------------------------------------
@router.message(kb_in("kb.quizzes"))
async def handle_quiz_menu(message: Message, state: FSMContext):
    user_id = message.from_user.id
    locale = await loc(user_id)
    await _clear_custom_timer_duration_wait(state)
    subjects = await available_subjects(user_id, locale)
    if not subjects:
        await message.answer(
            t("nav.no_materials", locale),
            reply_markup=get_main_keyboard(locale),
        )
        return
    timer_hint = ""
    if user_id in active_timers or await state.get_state() == TimerStates.active.state:
        if await _detach_timer_for_study_flow(state, user_id, message.chat.id):
            timer_hint = f"\n\n{t('timer.still_running_hint', locale)}"
    await state.update_data(subject_id=None, subject_label=None, mode_id=None, mode_label=None)
    await state.set_state(QuizStates.choosing_subject)
    await message.answer(
        t("nav.pick_subject", locale) + timer_hint,
        reply_markup=await get_subject_keyboard(user_id, locale),
    )


@router.message(QuizStates.choosing_subject, kb_in("kb.back_main"))
async def handle_subject_back_to_main(message: Message, state: FSMContext):
    user_id = message.from_user.id
    locale = await loc(user_id)
    await _preserve_pending_timer_across_clear(user_id, state)
    await message.answer(t("nav.main_menu", locale), reply_markup=get_main_keyboard(locale))


@router.message(F.text.in_(_all_subject_button_texts()))
async def handle_subject_picked(message: Message, state: FSMContext):
    # Subject reply buttons must work even when FSM state was cleared (bot restart,
    # navigation without state sync) while Telegram still shows the subject keyboard.
    subject_id = subject_id_from_button(message.text)
    if not subject_id:
        return
    user_id = message.from_user.id
    locale = await loc(user_id)
    subject_lbl = subject_label(subject_id, locale)
    if subject_id in PREP_HIDDEN_SUBJECT_IDS:
        await state.set_state(QuizStates.choosing_subject)
        await message.answer(
            f"{subject_lbl}\n{t('progress.coming_soon', locale).strip()}",
            reply_markup=await get_subject_keyboard(user_id, locale),
        )
        return
    modes = await available_modes(subject_id, user_id, locale)
    if not modes:
        await message.answer(
            t("nav.no_modes", locale, subject=subject_lbl),
            reply_markup=await get_subject_keyboard(user_id, locale),
        )
        return
    await state.update_data(subject_id=subject_id, subject_label=subject_lbl)
    await event_repo.log(user_id, "subject_picked", {"subject_id": subject_id})

    if subject_id == "math":
        mode_id = "tasks"
        mode_lbl = study_mode_label(mode_id, locale)
        await state.update_data(mode_id=mode_id, mode_label=mode_lbl)
        await event_repo.log(user_id, "mode_picked", {
            "mode_id": mode_id, "subject_id": subject_id,
        })
        groups = load_task_groups(subject_id)
        if groups:
            await _show_task_group_picker(message, state, subject_id, subject_lbl, groups, locale)
        else:
            await start_task_session(message, state, subject_id, subject_label=subject_lbl)
    else:
        await state.set_state(QuizStates.choosing_mode)
        await message.answer(
            t("nav.pick_mode", locale, subject_label=subject_lbl),
            reply_markup=await get_mode_keyboard_for_subject(subject_id, user_id, locale),
        )
        await _maybe_send_subject_fc_shortcuts(message, user_id, subject_id, locale)

    if PLAN_UI_ENABLED:
        ok, _ = await plan_available(subject_id)
        if ok:
            plan_kb = await build_plan_subject_keyboard(user_id, subject_id, locale)
            await message.answer(t("plan.subject_menu", locale), reply_markup=plan_kb)
            await maybe_offer_first_plan_prompt(message, state, user_id, subject_id, locale)


@router.message(QuizStates.choosing_mode, kb_in("kb.back_subjects"))
async def handle_mode_back_to_subjects(message: Message, state: FSMContext):
    user_id = message.from_user.id
    locale = await loc(user_id)
    await state.set_state(QuizStates.choosing_subject)
    await message.answer(
        t("nav.pick_subject", locale),
        reply_markup=await get_subject_keyboard(user_id, locale),
    )


@router.message(QuizStates.choosing_mode, F.text.in_(_all_mode_button_texts()))
async def handle_mode_picked(message: Message, state: FSMContext):
    mode_id = mode_id_from_button(message.text)
    if not mode_id:
        return
    data = await state.get_data()
    subject_id = data.get("subject_id")
    user_id = message.from_user.id
    locale = await loc(user_id)
    subject_lbl = data.get("subject_label", subject_label(subject_id or "", locale))
    if not subject_id:
        await state.set_state(QuizStates.choosing_subject)
        await message.answer(
            t("nav.pick_subject", locale),
            reply_markup=await get_subject_keyboard(user_id, locale),
        )
        return
    available = await available_modes(subject_id, user_id, locale)
    if not any(m[0] == mode_id for m in available):
        await message.answer(
            t("nav.no_modes", locale, subject=study_mode_label(mode_id, locale)),
            reply_markup=await get_mode_keyboard_for_subject(subject_id, user_id, locale),
        )
        return
    mode_lbl = study_mode_label(mode_id, locale)
    await state.update_data(mode_id=mode_id, mode_label=mode_lbl)
    await event_repo.log(user_id, "mode_picked", {
        "mode_id": mode_id, "subject_id": subject_id,
    })

    if mode_id == "situational":
        await state.set_state(QuizStates.choosing_section)
        await message.answer(
            t("quiz.pick_section_menu", locale, subject=subject_lbl),
            reply_markup=get_quiz_section_keyboard(locale),
        )
    elif mode_id == "mcq":
        await start_mcq_session(message, state, subject_id, subject_label=subject_lbl)
    elif mode_id == "tasks":
        groups = load_task_groups(subject_id)
        if groups:
            await _show_task_group_picker(message, state, subject_id, subject_lbl, groups, locale)
        else:
            await start_task_session(message, state, subject_id, subject_label=subject_lbl)
    elif mode_id == "flashcards":
        await _maybe_send_flash_mode_fc_shortcuts(message, user_id, subject_id, locale)
        await start_flashcard_session(message, state, subject_id, subject_label=subject_lbl)
    else:
        await message.answer(
            t("errors.generic", locale),
            reply_markup=await get_mode_keyboard_for_subject(subject_id, user_id, locale),
        )
        await state.set_state(QuizStates.choosing_mode)


# ============================================================
# MCQ flow (Multiple Choice Quiz, #13)
# ============================================================
async def start_mcq_session(message: Message, state: FSMContext, subject_id: str, subject_label: str):
    questions = load_mcq(subject_id)
    user_id = message.from_user.id
    locale = await loc(user_id)
    if not questions:
        await message.answer(
            t("mcq.no_questions", locale),
            reply_markup=await get_mode_keyboard_for_subject(subject_id, user_id, locale),
        )
        await state.set_state(QuizStates.choosing_mode)
        return
    await subject_stats_repo.bump_visit(message.from_user.id, subject_id)
    random.shuffle(questions)
    await state.update_data(
        mcq_questions=questions,
        mcq_index=0,
        mcq_correct_count=0,
        mcq_user_id=message.from_user.id,
    )
    await state.set_state(QuizStates.answering_mcq)
    await message.answer(
        t("mcq.session_intro", locale, subject_label=subject_label, count=len(questions)),
        reply_markup=get_mcq_active_keyboard(locale),
    )
    await _send_next_mcq_question(message.chat.id, state)


async def _send_next_mcq_question(chat_id: int, state: FSMContext):
    data = await state.get_data()
    questions = data.get("mcq_questions", [])
    idx = data.get("mcq_index", 0)
    if idx >= len(questions):
        await _finish_mcq_session(chat_id, state)
        return
    q = questions[idx]
    options = [q["correct"], *q["wrongs"]]
    random.shuffle(options)
    correct_idx = options.index(q["correct"])

    kb = InlineKeyboardBuilder()
    for i, opt in enumerate(options):
        kb.button(text=opt, callback_data=f"mcq:{i}")
    kb.adjust(1)

    await state.update_data(
        mcq_current_correct_idx=correct_idx,
        mcq_current_correct_text=q["correct"],
    )
    uid = data.get("mcq_user_id", chat_id)
    locale = await loc(uid)
    await bot.send_message(
        chat_id,
        t("mcq.question", locale, idx=idx + 1, total=len(questions), question=q["question"]),
        reply_markup=kb.as_markup(),
    )


async def _finish_mcq_session(chat_id: int, state: FSMContext):
    data = await state.get_data()
    correct = data.get("mcq_correct_count", 0)
    total = len(data.get("mcq_questions", []))
    subj_lbl = data.get("subject_label", "")
    uid = data.get("mcq_user_id", chat_id)
    locale = await loc(uid)
    logger.info(
        "mcq.session.complete user_id=%s subject=%s correct=%s total=%s coins=%s",
        uid, data.get("subject_id"), correct, total, correct,
    )
    await bot.send_message(
        chat_id,
        t("mcq.done", locale, subject_label=subj_lbl, correct=correct, total=total),
        reply_markup=get_study_keyboard(locale),
    )
    await state.clear()


@router.message(QuizStates.answering_mcq, kb_in("kb.finish_session"))
async def handle_mcq_stop(message: Message, state: FSMContext):
    user_id = message.from_user.id
    locale = await loc(user_id)
    data = await state.get_data()
    correct = data.get("mcq_correct_count", 0)
    answered = data.get("mcq_index", 0)
    total = len(data.get("mcq_questions", []))
    logger.info(
        "mcq.session.stop user_id=%s subject=%s answered=%s/%s correct=%s",
        user_id, data.get("subject_id"), answered, total, correct,
    )
    await message.answer(
        t("mcq.stopped", locale, answered=answered, total=total, correct=correct),
        reply_markup=get_study_keyboard(locale),
    )
    await state.clear()


@router.callback_query(F.data.startswith("mcq:"))
async def handle_mcq_callback(callback: CallbackQuery, state: FSMContext):
    current_state = await state.get_state()
    if current_state != QuizStates.answering_mcq.state:
        await callback.answer(t("mcq.session_ended", await loc(callback.from_user.id)), show_alert=False)
        return
    try:
        user_idx = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer()
        return

    data = await state.get_data()
    correct_idx = data.get("mcq_current_correct_idx")
    correct_text = data.get("mcq_current_correct_text", "")
    if correct_idx is None:
        # Не должно случиться: state без current_correct_idx
        await callback.answer(t("mcq.state_broken", await loc(callback.from_user.id)), show_alert=True)
        return

    user_id = callback.from_user.id
    is_correct = (user_idx == correct_idx)
    # Per-question tracking (для экрана прогресса)
    questions = data.get("mcq_questions", [])
    cur_idx = data.get("mcq_index", 0)
    if 0 <= cur_idx < len(questions):
        q_hash = _mcq_hash(questions[cur_idx]["question"])
        await mcq_repo.record_attempt(user_id, q_hash, is_correct)
        await event_repo.log(user_id, "mcq_answered", {
            "subject_id": data.get("subject_id"),
            "question_hash": q_hash,
            "is_correct": is_correct,
            "question_index": cur_idx,
        })
    if is_correct:
        # Leaderboard: MCQ считается quiz'ом (LEADERBOARD.md §3).
        await leaderboard_repo.grant_quiz_pts_correct(user_id)
        await user_repo.add_coins(user_id, 1)
        feedback = t("mcq.correct", await loc(user_id))
        await state.update_data(mcq_correct_count=data.get("mcq_correct_count", 0) + 1)
    else:
        # Wrong → сбрасываем series counter (LEADERBOARD.md §3).
        await leaderboard_repo.reset_quiz_series(user_id)
        feedback = t("mcq.wrong", await loc(user_id), answer=correct_text)

    # Убираем inline-кнопки + дописываем feedback, чтобы повторный тап не сработал
    try:
        original = callback.message.text or ""
        await callback.message.edit_text(
            f"{original}\n\n{feedback}",
            reply_markup=None,
        )
    except Exception as e:
        logger.warning("mcq.edit_failed user_id=%s reason=%s", user_id, e)

    await callback.answer()

    data = await state.get_data()
    if PLAN_UI_ENABLED and data.get("plan_single"):
        locale = await loc(user_id)
        if is_correct:
            await on_plan_activity_complete(user_id, state, success=True)
        else:
            await return_to_plan_without_complete(
                callback.message.chat.id,
                user_id,
                state,
                locale,
                message=t("plan.item_wrong", locale),
            )
        return

    # Переходим к следующему вопросу
    await state.update_data(mcq_index=data.get("mcq_index", 0) + 1)
    await asyncio.sleep(1.0)  # короткая пауза, чтобы фидбек был заметен
    await _send_next_mcq_question(callback.message.chat.id, state)


# ============================================================
# Photo-task flow (#14)
# ============================================================
# Награды: +3 / +2 / 0 монет (0 = открыли ответ после 2-й ошибки).
TASK_REWARDS_BY_ATTEMPT = [3, 2]
MAX_TASK_ATTEMPTS = 2


async def _show_task_group_picker(
    message: Message,
    state: FSMContext,
    subject_id: str,
    subject_label: str,
    groups: dict[str, dict],
    locale: str,
) -> None:
    """Inline-меню групп задач перед стартом сессии."""
    kb = InlineKeyboardBuilder()
    for group_id, meta in groups.items():
        title = meta.get("title") or group_id
        count = len(load_tasks(subject_id, group_id=group_id))
        if count == 0:
            continue
        kb.button(
            text=f"{title} ({count})",
            callback_data=f"taskgrp:{message.from_user.id}:{subject_id}:{group_id}",
        )
    if not kb.export():
        await start_task_session(message, state, subject_id, subject_label=subject_label)
        return
    kb.adjust(1)
    await state.set_state(QuizStates.choosing_mode)
    await message.answer(
        t("task.pick_group", locale, subject=subject_label),
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data.startswith("taskgrp:"))
async def handle_task_group_picked(callback: CallbackQuery, state: FSMContext):
    try:
        _, user_id_str, subject_id, group_id = callback.data.split(":", 3)
        user_id = int(user_id_str)
    except (ValueError, IndexError):
        await callback.answer()
        return
    if callback.from_user.id != user_id:
        await callback.answer(t("common.not_yours_session", await loc(callback.from_user.id)), show_alert=True)
        return
    subject_id = await _callback_allowlisted_subject(callback, subject_id)
    if subject_id is None:
        return
    groups = load_task_groups(subject_id)
    group_meta = groups.get(group_id) or {}
    locale = await loc(user_id)
    subject_lbl = subject_label(subject_id, locale)
    await callback.answer()
    await state.update_data(
        subject_id=subject_id,
        subject_label=subject_lbl,
        mode_id="tasks",
        task_group_id=group_id,
        task_group_title=group_meta.get("title") or group_id,
    )
    await start_task_session(
        callback.message,
        state,
        subject_id,
        subject_label=subject_lbl,
        group_id=group_id,
        group_title=group_meta.get("title") or group_id,
    )


async def start_task_session(
    message: Message,
    state: FSMContext,
    subject_id: str,
    subject_label: str,
    group_id: str | None = None,
    group_title: str | None = None,
):
    subject_id = validate_subject_id(subject_id)
    if subject_id is None:
        locale = await loc(message.from_user.id)
        await message.answer(t("errors.state_error", locale))
        await state.set_state(QuizStates.choosing_mode)
        return
    user_id = message.from_user.id
    locale = await loc(user_id)
    tasks = await load_tasks_for_study(user_id, subject_id, group_id=group_id)
    if not tasks:
        await message.answer(
            t("task.no_tasks", locale),
            reply_markup=await get_mode_keyboard_for_subject(subject_id, user_id, locale),
        )
        await state.set_state(QuizStates.choosing_mode)
        return
    await subject_stats_repo.bump_visit(message.from_user.id, subject_id)
    random.shuffle(tasks)
    await state.update_data(
        task_questions=tasks,
        task_index=0,
        task_attempts=0,
        task_correct_count=0,
        task_coins_earned=0,
        task_user_id=message.from_user.id,
        task_subject_id=subject_id,
        task_subject_label=subject_label,
        task_group_id=group_id,
        task_group_title=group_title or "",
    )
    await state.set_state(QuizStates.answering_task)
    await message.answer(
        t("task.session_start", locale, subject_label=subject_label, count=len(tasks)),
        reply_markup=get_task_active_keyboard(locale),
    )
    await _send_next_task(message.chat.id, state)


async def _send_next_task(chat_id: int, state: FSMContext):
    data = await state.get_data()
    tasks = data.get("task_questions", [])
    idx = data.get("task_index", 0)
    if idx >= len(tasks):
        await _finish_task_session(chat_id, state)
        return
    subject_id = data.get("task_subject_id", "")
    task = tasks[idx]
    await state.update_data(task_attempts=0)

    group_title = data.get("task_group_title") or ""
    subtitle = task.get("subtitle") or ""
    header_parts = []
    if group_title:
        header_parts.append(group_title)
    if subtitle:
        header_parts.append(subtitle)
    header_parts.append(f"{idx + 1}/{len(tasks)}")
    header = " · ".join(header_parts)
    lines = [t("task.item_header", await loc(data.get("task_user_id", chat_id)), header=header)]
    if task.get("problem"):
        lines.append("")
        lines.append(task["problem"])
    lines.append("")
    lines.append(t("task.enter_answer", await loc(data.get("task_user_id", chat_id))))
    text = "\n".join(lines)

    if task.get("kind") == "user":
        try:
            await bot.send_message(chat_id, text)
        except Exception as e:
            logger.error("task.send_text_failed task_id=%s reason=%s", task["id"], e)
            await state.update_data(task_index=idx + 1, task_attempts=0)
            await _send_next_task(chat_id, state)
        return

    if task.get("text_only") or (
        task.get("kind") == "official"
        and not (STUDY_MATERIALS_PATH / subject_id / "tasks" / f"{task['id']}.png").exists()
    ):
        try:
            await bot.send_message(chat_id, text)
        except Exception as e:
            logger.error("task.send_text_failed task_id=%s reason=%s", task["id"], e)
            await state.update_data(task_index=idx + 1, task_attempts=0)
            await _send_next_task(chat_id, state)
        return

    tasks_dir = STUDY_MATERIALS_PATH / subject_id / "tasks"
    image_path = tasks_dir / f"{task['id']}.png"
    if not image_path.exists():
        logger.warning(
            "task.image_missing_at_send task_id=%s subject=%s expected=%s",
            task["id"], subject_id, image_path.name,
        )
        await state.update_data(task_index=idx + 1, task_attempts=0)
        await _send_next_task(chat_id, state)
        return

    try:
        await bot.send_photo(chat_id, FSInputFile(image_path), caption=text)
    except Exception as e:
        logger.error("task.send_photo_failed task_id=%s reason=%s", task["id"], e)
        await state.update_data(task_index=idx + 1, task_attempts=0)
        await _send_next_task(chat_id, state)


async def _finish_task_session(chat_id: int, state: FSMContext):
    data = await state.get_data()
    if PLAN_UI_ENABLED and data.get("plan_single"):
        user_id = data.get("task_user_id", chat_id)
        locale = await loc(user_id)
        await return_to_plan_without_complete(chat_id, user_id, state, locale)
        return
    correct = data.get("task_correct_count", 0)
    coins = data.get("task_coins_earned", 0)
    total = len(data.get("task_questions", []))
    subject_label = data.get("task_subject_label", "")
    logger.info(
        "task.session.complete user_id=%s subject=%s correct=%s total=%s coins=%s",
        data.get("task_user_id"), data.get("task_subject_id"),
        correct, total, coins,
    )
    uid = data.get("task_user_id", chat_id)
    locale = await loc(uid)
    await bot.send_message(
        chat_id,
        t("task.done", locale, subject_label=subject_label, correct=correct, total=total, coins=coins),
        reply_markup=get_study_keyboard(locale),
    )
    await state.clear()


@router.message(QuizStates.answering_task, kb_in("kb.finish_session"))
async def handle_task_stop(message: Message, state: FSMContext):
    user_id = message.from_user.id
    locale = await loc(user_id)
    data = await state.get_data()
    correct = data.get("task_correct_count", 0)
    coins = data.get("task_coins_earned", 0)
    idx = data.get("task_index", 0)
    total = len(data.get("task_questions", []))
    logger.info(
        "task.session.stop user_id=%s subject=%s answered=%s/%s correct=%s coins=%s",
        user_id, data.get("task_subject_id"),
        idx, total, correct, coins,
    )
    await message.answer(
        t("task.stopped", locale, idx=idx, total=total, correct=correct, coins=coins),
        reply_markup=get_study_keyboard(locale),
    )
    await state.clear()


def _official_task_solution_path(subject_id: str, task: dict) -> Path | None:
    base = safe_subject_dir(STUDY_MATERIALS_PATH, subject_id)
    if base is None:
        return None
    tasks_dir = base / "tasks"
    if not tasks_dir.is_dir():
        return None
    filename = safe_task_image_filename(
        task.get("solution_filename", f"{task['id']}-solution.png"),
        task["id"],
    )
    return resolve_path_under(tasks_dir, filename)


def _official_task_has_solution(subject_id: str, task: dict) -> bool:
    """True if an official task has a solution PNG and/or solution_text to show."""
    if (task.get("solution_text") or "").strip():
        return True
    path = _official_task_solution_path(subject_id, task)
    return path is not None and path.exists()


async def _send_official_task_solution(
    chat_id: int,
    *,
    task: dict,
    subject_id: str,
    locale: str,
    correct_answer: str,
    after_failure: bool = False,
) -> None:
    """Send solution PNG when present; otherwise fall back to solution_text."""
    solution_path = _official_task_solution_path(subject_id, task)
    solution_body = (task.get("solution_text") or "").strip()

    if solution_path is not None and solution_path.exists():
        caption = (
            t("task.solution_image", locale, answer=correct_answer)
            if after_failure
            else "💡 Решение:"
        )
        try:
            await bot.send_photo(chat_id, FSInputFile(solution_path), caption=caption)
            return
        except Exception as e:
            logger.error("task.send_solution_failed task_id=%s reason=%s", task["id"], e)

    if solution_body:
        if after_failure:
            await bot.send_message(
                chat_id,
                t("task.solution_text_block", locale, solution=solution_body, answer=correct_answer),
            )
        else:
            await bot.send_message(chat_id, f"💡 Решение:\n{solution_body}")
        return

    if after_failure:
        await bot.send_message(
            chat_id,
            t("task.solution_missing_image", locale, answer=correct_answer),
        )


@router.message(QuizStates.answering_task)
async def handle_task_answer(message: Message, state: FSMContext):
    # Команды и кнопки уже разобраны выше; здесь ответ — обычный текст
    text = message.text or ""
    if not text or text.startswith("/"):
        return
    data = await state.get_data()
    tasks = data.get("task_questions", [])
    idx = data.get("task_index", 0)
    if idx >= len(tasks):
        await _finish_task_session(message.chat.id, state)
        return
    task = tasks[idx]
    user_id = message.from_user.id
    locale = await loc(user_id)
    attempts = data.get("task_attempts", 0)

    if task_answer_matches(text, task.get("accepted", [])):
        coins_for_this = TASK_REWARDS_BY_ATTEMPT[min(attempts, MAX_TASK_ATTEMPTS - 1)]
        await user_repo.add_coins(user_id, coins_for_this)
        # Per-task tracking: задача решена с (attempts+1)-й попытки
        await task_repo.record_attempt(
            user_id, task["id"], attempts_used=attempts + 1, succeeded=True
        )
        # Leaderboard: 40 pts за math task (mission lever, LEADERBOARD.md §2).
        # Daily cap 5 — grant_task_pts вернёт False сверх лимита, тихо.
        await leaderboard_repo.grant_task_pts(user_id)
        await event_repo.log(user_id, "task_attempted", {
            "subject_id": data.get("task_subject_id"),
            "task_id": task["id"],
            "attempts_used": attempts + 1,
            "succeeded": True,
            "coins": coins_for_this,
        })
        await state.update_data(
            task_correct_count=data.get("task_correct_count", 0) + 1,
            task_coins_earned=data.get("task_coins_earned", 0) + coins_for_this,
            task_index=idx + 1,
            task_attempts=0,
        )
        logger.info(
            "task.answered user_id=%s task_id=%s attempts=%s result=correct coins=%s",
            user_id, task["id"], attempts + 1, coins_for_this,
        )
        await message.answer(f"✅ Верно! +{coins_for_this} 🪙")
        await asyncio.sleep(1.0)
        if task.get("kind") != "user":
            subject_id = data.get("task_subject_id", "")
            correct_answer = task["accepted"][0] if task.get("accepted") else "(нет данных)"
            if _official_task_has_solution(subject_id, task):
                await _send_official_task_solution(
                    message.chat.id,
                    task=task,
                    subject_id=subject_id,
                    locale=locale,
                    correct_answer=correct_answer,
                )
                await asyncio.sleep(1.0)
        if PLAN_UI_ENABLED and data.get("plan_single"):
            await on_plan_activity_complete(user_id, state, success=True)
            return
        await _send_next_task(message.chat.id, state)
        return

    new_attempts = attempts + 1
    subject_id = data.get("task_subject_id", "")
    correct_answer = task["accepted"][0] if task.get("accepted") else "(нет данных)"
    hint = (task.get("hint") or "").strip()

    if new_attempts < MAX_TASK_ATTEMPTS:
        remaining = MAX_TASK_ATTEMPTS - new_attempts
        await state.update_data(task_attempts=new_attempts)
        logger.info(
            "task.answered user_id=%s task_id=%s attempts=%s result=wrong remaining=%s",
            user_id, task["id"], new_attempts, remaining,
        )
        if new_attempts == 1 and hint:
            await message.answer(t("task.hint_only", locale, hint=hint))
        else:
            await message.answer(
                t("task.wrong_retry", locale, remaining=remaining),
            )
        return

    # 2-я неверная — открываем ответ / решение
    await task_repo.record_attempt(
        user_id, task["id"], attempts_used=new_attempts, succeeded=False
    )
    await event_repo.log(user_id, "task_attempted", {
        "subject_id": subject_id,
        "task_id": task["id"],
        "attempts_used": new_attempts,
        "succeeded": False,
        "coins": 0,
    })
    logger.info(
        "task.answered user_id=%s task_id=%s attempts=%s result=show_solution coins=0",
        user_id, task["id"], new_attempts,
    )
    if task.get("kind") == "user" or not _official_task_has_solution(subject_id, task):
        await message.answer(t("task.solution", locale, answer=correct_answer))
    else:
        await _send_official_task_solution(
            message.chat.id,
            task=task,
            subject_id=subject_id,
            locale=locale,
            correct_answer=correct_answer,
            after_failure=True,
        )
    await state.update_data(task_index=idx + 1, task_attempts=0)
    await asyncio.sleep(1.0)
    if PLAN_UI_ENABLED and data.get("plan_single"):
        await return_to_plan_without_complete(
            message.chat.id,
            user_id,
            state,
            locale,
            message=t("plan.item_wrong", locale),
        )
        return
    await _send_next_task(message.chat.id, state)


# ============================================================
# Flashcards flow with SM-2 (#15)
# ============================================================
# UI: 3-кнопочный inline-рейтинг после показа ответа.
# Маппинг кнопка → quality для sm2_update:
FLASH_QUALITY_BY_LABEL = {
    "❌ Не знал":  1,
    "😐 Сложно":   3,
    "✅ Легко":    5,
}
FLASH_COINS_PER_CARD = 1  # +1🪙 за просмотр независимо от рейтинга
                         # (честная самооценка > монетомаксимизация)


async def start_flashcard_session(message: Message, state: FSMContext, subject_id: str, subject_label: str):
    subject_id = validate_subject_id(subject_id)
    if subject_id is None:
        locale = await loc(message.from_user.id)
        await message.answer(t("errors.state_error", locale))
        await state.set_state(QuizStates.choosing_mode)
        return
    user_id = message.from_user.id
    settings = await user_repo.get_notification_settings(user_id) or {}
    source = settings.get("flashcard_source", "mix")
    cards = await load_flashcards_for_study(user_id, subject_id, source)
    if not cards:
        locale = await loc(user_id)
        source_label = flash_source_labels(locale).get(source, source)
        hints = {
            "own": (
                "В настройках выбран источник «Свои», но своих карточек пока нет.\n"
                "Добавь карточки через «📇 Мои карточки» или смени источник в ⚙️ Настройки."
            ),
            "official": (
                "В настройках выбран источник «Официальные», но официальных карточек "
                "для этого предмета пока нет.\n"
                "Смени источник на «Микс» или «Свои» в ⚙️ Настройки."
            ),
            "mix": (
                "Для этого предмета пока нет флэш-карт.\n"
                "Добавь свои через «📇 Мои карточки» или дождись официального контента."
            ),
        }
        await message.answer(
            f"🚧 Нет карточек для учёбы (источник: {source_label}).\n\n"
            f"{hints.get(source, hints['mix'])}",
            reply_markup=await get_mode_keyboard_for_subject(subject_id, user_id, await loc(user_id)),
        )
        await state.set_state(QuizStates.choosing_mode)
        return

    cards_by_hash = {c["hash"]: c for c in cards}
    candidate_hashes = list(cards_by_hash.keys())
    next_hash = await flashcard_repo.get_next_card_hash(message.from_user.id, candidate_hashes)
    if next_hash is None:
        await message.answer(
            "🎉 Все карточки этого предмета уже проработаны на сегодня!\n"
            "Возвращайся позже — SM-2 покажет их, когда придёт срок повторения.",
            reply_markup=get_study_keyboard(await loc(user_id)),
        )
        return

    await subject_stats_repo.bump_visit(message.from_user.id, subject_id)
    await state.update_data(
        flash_cards_by_hash=cards_by_hash,
        flash_candidate_hashes=candidate_hashes,
        flash_reviewed_count=0,
        flash_coins_earned=0,
        flash_user_id=message.from_user.id,
        flash_subject_id=subject_id,
        flash_subject_label=subject_label,
        flash_current_hash=None,  # будет проставлено в _send_flashcard
    )
    await state.set_state(QuizStates.answering_flash)
    await message.answer(
        f"🃏 Флэш-карты — {subject_label}\n"
        f"Алгоритм SM-2 подбирает интервалы автоматически. Будь честен с собой при оценке.",
        reply_markup=get_flash_active_keyboard(await loc(user_id)),
    )
    await _send_flashcard(message.chat.id, state, next_hash)


async def _send_flashcard(chat_id: int, state: FSMContext, card_hash: str):
    data = await state.get_data()
    card = data.get("flash_cards_by_hash", {}).get(card_hash)
    if not card:
        logger.warning("flash.card_missing_in_session hash=%s", card_hash)
        await _finish_flashcard_session(chat_id, state)
        return
    await state.update_data(flash_current_hash=card_hash)
    reviewed = data.get("flash_reviewed_count", 0)

    kb = InlineKeyboardBuilder()
    kb.button(text="💡 Показать ответ", callback_data=f"flash:show:{card_hash}")
    kb.adjust(1)
    await bot.send_message(
        chat_id,
        f"🃏 Карточка #{reviewed + 1}\n\n<b>{html_escape(card['term'])}</b>",
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
    )


async def _finish_flashcard_session(chat_id: int, state: FSMContext):
    data = await state.get_data()
    reviewed = data.get("flash_reviewed_count", 0)
    coins = data.get("flash_coins_earned", 0)
    subject_label = data.get("flash_subject_label", "")
    logger.info(
        "flash.session.complete user_id=%s subject=%s reviewed=%s coins=%s",
        data.get("flash_user_id"), data.get("flash_subject_id"),
        reviewed, coins,
    )
    uid = data.get("flash_user_id", chat_id)
    locale = await loc(uid)
    if reviewed == 0:
        msg = t("flash.all_reviewed", locale)
    else:
        msg = t(
            "flash.session_done", locale,
            subject_label=subject_label, reviewed=reviewed, coins=coins,
        )
    await bot.send_message(chat_id, msg, reply_markup=get_study_keyboard(locale))
    await state.clear()


@router.message(QuizStates.answering_flash, kb_in("kb.finish_session"))
async def handle_flashcard_stop(message: Message, state: FSMContext):
    user_id = message.from_user.id
    locale = await loc(user_id)
    data = await state.get_data()
    reviewed = data.get("flash_reviewed_count", 0)
    coins = data.get("flash_coins_earned", 0)
    logger.info(
        "flash.session.stop user_id=%s subject=%s reviewed=%s coins=%s",
        user_id, data.get("flash_subject_id"), reviewed, coins,
    )
    await message.answer(
        t("flash.stopped", locale, reviewed=reviewed, coins=coins),
        reply_markup=get_study_keyboard(locale),
    )
    await state.clear()


@router.callback_query(F.data.startswith("flash:show:"))
async def handle_flashcard_show(callback: CallbackQuery, state: FSMContext):
    """Тап «💡 Показать ответ» — открывает определение + 3-кнопочный рейтинг."""
    current_state = await state.get_state()
    if current_state != QuizStates.answering_flash.state:
        await callback.answer(t("mcq.session_ended", await loc(callback.from_user.id)), show_alert=False)
        return
    card_hash = callback.data.split(":", 2)[2]
    data = await state.get_data()
    card = data.get("flash_cards_by_hash", {}).get(card_hash)
    if not card:
        await callback.answer(t("common.card_not_found", await loc(callback.from_user.id)), show_alert=True)
        return

    kb = InlineKeyboardBuilder()
    for label in ("❌ Не знал", "😐 Сложно", "✅ Легко"):
        kb.button(text=label, callback_data=f"flash:rate:{card_hash}:{FLASH_QUALITY_BY_LABEL[label]}")
    kb.adjust(3)

    reviewed = data.get("flash_reviewed_count", 0)
    new_text = (
        f"🃏 Карточка #{reviewed + 1}\n\n"
        f"<b>{html_escape(card['term'])}</b>\n\n"
        f"💡 <i>{html_escape(card['definition'])}</i>\n\n"
        f"Как тебе далось? Оцени честно — алгоритм подберёт интервал:"
    )
    try:
        await callback.message.edit_text(new_text, reply_markup=kb.as_markup(), parse_mode="HTML")
    except Exception as e:
        logger.warning("flash.show_edit_failed hash=%s reason=%s", card_hash, e)
    await callback.answer()


@router.callback_query(F.data.startswith("flash:rate:"))
async def handle_flashcard_rate(callback: CallbackQuery, state: FSMContext):
    """Тап рейтинга — применяем SM-2, начисляем монету, шлём следующую."""
    current_state = await state.get_state()
    if current_state != QuizStates.answering_flash.state:
        await callback.answer(t("mcq.session_ended", await loc(callback.from_user.id)), show_alert=False)
        return
    try:
        _, _, card_hash, quality_str = callback.data.split(":", 3)
        quality = int(quality_str)
    except (ValueError, IndexError):
        await callback.answer(t("common.broken_callback", await loc(callback.from_user.id)), show_alert=True)
        return
    if quality not in (1, 3, 5):
        await callback.answer()
        return

    data = await state.get_data()
    user_id = callback.from_user.id

    # Текущее состояние карты в БД (или дефолты для новой).
    # is_new_card фиксируется ЗДЕСЬ (до upsert_progress), потому что
    # `reps_before == 0` в логе события неоднозначен: 0 может означать
    # либо «карту впервые видим», либо «строка есть, но сбросилась
    # после неверного ответа». Для leaderboard-scoring (LEADERBOARD.md §4:
    # +3 pts за новую, +5 за review) нужна точная семантика «нет строки
    # на момент ответа = new».
    progress = await flashcard_repo.get_progress(user_id, card_hash)
    is_new_card = progress is None
    if progress:
        ef = float(progress["ease_factor"])
        reps = int(progress["repetitions"])
        interval = int(progress["interval_days"])
    else:
        ef, reps, interval = 2.5, 0, 0

    new_interval, new_reps, new_ef = sm2_update(quality, reps, ef, interval)
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    next_review = (now + timedelta(days=new_interval)).strftime("%Y-%m-%d %H:%M:%S")
    await flashcard_repo.upsert_progress(
        user_id=user_id,
        card_hash=card_hash,
        ease_factor=new_ef,
        interval_days=new_interval,
        repetitions=new_reps,
        last_review=now_str,
        next_review=next_review,
    )
    await user_repo.add_coins(user_id, FLASH_COINS_PER_CARD)
    # Leaderboard: pts только за УСПЕШНЫЙ review (LEADERBOARD.md §4).
    # quality 1 (❌ Не знал) → no pts; quality 3 (😐) / 5 (✅) → grant.
    # +3 для new, +5 для review. Daily cap 8 successful (внутри repo).
    if quality >= 3:
        await leaderboard_repo.grant_card_pts(user_id, is_new=is_new_card)
    await event_repo.log(user_id, "flashcard_reviewed", {
        "subject_id": data.get("flash_subject_id"),
        "card_hash": card_hash,
        "quality": quality,
        "is_new": is_new_card,
        "reps_before": reps, "reps_after": new_reps,
        "ef_before": round(ef, 3), "ef_after": round(new_ef, 3),
        "interval_before": interval, "interval_after": new_interval,
        "next_review": next_review,
    })

    logger.info(
        "flash.rated user_id=%s hash=%s quality=%s reps=%s->%s ef=%.2f->%.2f interval=%s->%s next=%s",
        user_id, card_hash, quality, reps, new_reps, ef, new_ef, interval, new_interval, next_review,
    )

    # Feedback на той же карте (убираем кнопки, дописываем строку)
    quality_labels = {1: "❌ Не знал", 3: "😐 Сложно", 5: "✅ Легко"}
    feedback = (
        f"\n\n{quality_labels.get(quality, '')} → следующее повторение через "
        f"<b>{new_interval}</b> дн. (+{FLASH_COINS_PER_CARD} 🪙)"
    )
    try:
        original = callback.message.html_text or callback.message.text or ""
        await callback.message.edit_text(
            f"{original}{feedback}",
            reply_markup=None,
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning("flash.rate_edit_failed hash=%s reason=%s", card_hash, e)
    await callback.answer()

    # Обновляем сессионные счётчики
    await state.update_data(
        flash_reviewed_count=data.get("flash_reviewed_count", 0) + 1,
        flash_coins_earned=data.get("flash_coins_earned", 0) + FLASH_COINS_PER_CARD,
    )

    if PLAN_UI_ENABLED and data.get("plan_single"):
        await on_plan_activity_complete(user_id, state, success=True)
        return

    # Следующая карта
    candidate_hashes = data.get("flash_candidate_hashes", [])
    await asyncio.sleep(0.8)
    next_hash = await flashcard_repo.get_next_card_hash(user_id, candidate_hashes)
    if next_hash is None:
        await _finish_flashcard_session(callback.message.chat.id, state)
        return
    await _send_flashcard(callback.message.chat.id, state, next_hash)


# ============================================================
# Situational quiz flow (existing)
# ============================================================
@router.message(QuizStates.choosing_section, kb_in("kb.finish_quiz"))
async def handle_quiz_exit_from_section(message: Message, state: FSMContext):
    user_id = message.from_user.id
    locale = await loc(user_id)
    await state.clear()
    await message.answer(
        t("quiz.finished_saved", locale),
        reply_markup=get_study_keyboard(locale),
    )


@router.message(QuizStates.choosing_section, F.text.in_(_quiz_section_label_list()))
async def handle_quiz_section(message: Message, state: FSMContext):
    user_id = message.from_user.id
    locale = await loc(user_id)
    section_key = _quiz_section_map_all().get(message.text)
    if not section_key:
        return
    data = await state.get_data()
    subject_id = data.get("subject_id", "industrial-management")
    terms = load_quiz_section(section_key, subject_id)
    if not terms:
        await message.answer(
            t("quiz.section_empty", locale),
            reply_markup=get_quiz_section_keyboard(locale),
        )
        return
    await subject_stats_repo.bump_visit(user_id, subject_id)
    next_term = await get_next_quiz_term(user_id, terms)
    if not next_term:
        await message.answer(
            t("quiz.section_done_great", locale),
            reply_markup=get_quiz_section_keyboard(locale),
        )
        return
    await state.update_data(
        current_term=next_term.to_dict(),
        section=section_key,
        section_name=message.text
    )
    await state.set_state(QuizStates.answering)
    await message.answer(
        t("quiz.answer_prompt", locale, section=message.text, term=next_term.term),
        reply_markup=get_quiz_answer_keyboard(locale),
    )

@router.message(QuizStates.answering, kb_in("kb.finish_quiz"))
async def handle_quiz_exit(message: Message, state: FSMContext):
    user_id = message.from_user.id
    locale = await loc(user_id)
    await state.clear()
    await message.answer(
        t("quiz.finished_saved", locale),
        reply_markup=get_study_keyboard(locale),
    )

@router.message(QuizStates.answering)
async def handle_quiz_answer(message: Message, state: FSMContext):
    data = await state.get_data()
    term = data.get("current_term")
    user_id = message.from_user.id
    locale = await loc(user_id)
    if not term:
        await message.answer(
            t("quiz.restart", locale),
            reply_markup=get_quiz_section_keyboard(locale),
        )
        await state.clear()
        return
    is_correct, feedback = check_text_answer(message.text, term["definition"], term["keywords"])
    progress = await get_quiz_progress(user_id, term["hash"])
    streak = progress["streak"]
    if is_correct:
        streak += 1
        feedback += t("quiz.repeat_in_days", locale, days=quiz_interval_days(streak))
        # Leaderboard: 5 pts + возможный series-bonus (LEADERBOARD.md §3).
        await leaderboard_repo.grant_quiz_pts_correct(user_id)
    else:
        streak = 0
        await leaderboard_repo.reset_quiz_series(user_id)
    await update_quiz_progress(user_id, term["hash"], is_correct, streak)
    await event_repo.log(user_id, "quiz_answered", {
        "subject_id": data.get("subject_id", "industrial-management"),
        "section": data.get("section"),
        "term_hash": term["hash"],
        "is_correct": is_correct,
        "streak_after": streak,
    })
    await message.answer(feedback)
    await asyncio.sleep(1.5)

    if PLAN_UI_ENABLED and data.get("plan_single"):
        if is_correct:
            await on_plan_activity_complete(user_id, state, success=True)
        else:
            await return_to_plan_without_complete(
                message.chat.id,
                user_id,
                state,
                locale,
                message=t("plan.item_wrong", locale),
            )
        return

    terms = load_quiz_section(data["section"])
    next_term = await get_next_quiz_term(user_id, terms)
    if next_term:
        await state.update_data(current_term=next_term.to_dict())
        await message.answer(
            t("quiz.answer_prompt", locale, section=data["section_name"], term=next_term.term),
            reply_markup=get_quiz_answer_keyboard(locale),
        )
    else:
        await message.answer(
            t("quiz.section_done", locale),
            reply_markup=get_quiz_section_keyboard(locale),
        )
        await state.clear()

# ------------------------------------------------------------
# Советы
# ------------------------------------------------------------
def _format_tip_message(
    category: str,
    tip: dict,
    locale: str,
    *,
    page: int | None = None,
    total: int | None = None,
) -> str:
    """HTML: жирный заголовок, тело, строка «Попробуй сегодня»."""
    meta = _tip_cats(locale)[category]
    emoji = tip.get("emoji") or meta["emoji"]
    title = html_escape(tip["title"])
    body = html_escape(tip["body"])
    if body and body[0].islower():
        body = body[0].upper() + body[1:]
    header = f"{emoji} <b>{title}</b>"
    if page is not None and total is not None:
        lines = [f"{meta['emoji']} {meta['title']} — {page + 1}/{total}", "", header, "", body]
    else:
        lines = [header, "", body]
    action = (tip.get("action") or "").strip()
    if action:
        lines.extend(["", t("tips.try_today", locale, action=html_escape(action))])
    return "\n".join(lines)


def _tips_inline_keyboard(
    category: str,
    locale: str,
    *,
    list_page: int | None = None,
    list_total: int | None = None,
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if list_page is None:
        kb.button(text=t("tips.more", locale), callback_data=f"tips:more:{category}")
        kb.button(text=t("tips.all", locale), callback_data=f"tips:list:{category}:0")
        kb.button(text=t("tips.back_categories", locale), callback_data="tips:menu")
        kb.adjust(2, 1)
    else:
        if list_page > 0:
            kb.button(text="◀️", callback_data=f"tips:list:{category}:{list_page - 1}")
        kb.button(text=t("tips.random", locale), callback_data=f"tips:more:{category}")
        if list_total and list_page < list_total - 1:
            kb.button(text="▶️", callback_data=f"tips:list:{category}:{list_page + 1}")
        kb.button(text=t("tips.back_categories", locale), callback_data="tips:menu")
        kb.adjust(3, 1)
    return kb.as_markup()


def _productivity_links_keyboard(locale: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for link in PRODUCTIVITY_LINKS:
        title = link["title"]
        label = title if len(title) <= 64 else f"{title[:61]}…"
        kb.button(text=label, url=link["url"])
    kb.button(text=t("tips.back_categories", locale), callback_data="tips:menu")
    kb.adjust(1)
    return kb.as_markup()


def _user_local_date_str(user: dict | None) -> str:
    tz_name = (user or {}).get("timezone") or "Europe/Moscow"
    try:
        return datetime.now(pytz.timezone(tz_name)).date().isoformat()
    except Exception:
        return datetime.now(pytz.timezone("Europe/Moscow")).date().isoformat()


def _category_key_from_tip_id(tip_id: str) -> str:
    """tm-01 → tm, bot-03 → bot."""
    return tip_id.split("-", 1)[0] if tip_id and "-" in tip_id else "tm"


def _all_tips_flat(locale: str = "ru") -> list[dict]:
    out: list[dict] = []
    for meta in _tip_cats(locale).values():
        out.extend(meta["tips"])
    return out


async def _preferred_tip_tags(user_id: int) -> set[str]:
    """Контекст пользователя → приоритетные tags для подбора совета."""
    tags: set[str] = set()
    if user_id in active_timers:
        tags.add("timer")
    user = await user_repo.get_user(user_id)
    if user and not user.get("has_studied_today"):
        tags.update({"study", "focus", "timer", "bot"})
    if await tips_repo.user_has_flashcards_due(user_id):
        tags.add("flashcards")
    return tags


async def _pick_tip(user_id: int, category: str, locale: str) -> dict | None:
    """Совет с учётом cooldown (7 дн.) и контекста (таймер, стрик, карточки)."""
    tips = _tip_cats(locale).get(category, {}).get("tips", [])
    if not tips:
        return None
    seen = await tips_repo.get_recently_seen_tip_ids(user_id, TIPS_SEEN_COOLDOWN_DAYS)
    pool = [t for t in tips if t["id"] not in seen] or list(tips)
    preferred = await _preferred_tip_tags(user_id)
    if preferred:
        tagged = [t for t in pool if preferred & set(t.get("tags", []))]
        if tagged:
            pool = tagged
    return random.choice(pool)


async def build_morning_tip_block(user_id: int, tz: str) -> str:
    """HTML-блок «совет дня» для утреннего напоминания."""
    locale = await loc(user_id)
    user = await user_repo.get_user(user_id)
    local_date = _user_local_date_str(user)
    tip = await tips_repo.resolve_tip_of_day(user_id, local_date, _all_tips_flat(locale))
    if not tip:
        return ""
    cat_key = _category_key_from_tip_id(tip["id"])
    if cat_key not in _tip_cats(locale):
        cat_key = "tm"
    body = _format_tip_message(cat_key, tip, locale)
    return t("reminders.tip_of_day_header", locale) + body


async def _on_tip_viewed(user_id: int, category: str, tip_id: str | None = None) -> str:
    """Монета за первый совет дня, ачивка за 10 советов, событие tip_viewed."""
    locale = await loc(user_id)
    if category not in _tip_cats(locale):
        return ""
    if tip_id:
        await tips_repo.record_seen(user_id, tip_id)
    user = await user_repo.get_user(user_id)
    local_date = _user_local_date_str(user)
    total_views, coin_granted = await tips_repo.record_view(user_id, local_date)

    if coin_granted:
        await user_repo.add_coins(user_id, TIP_COIN_PER_DAY)

    new_ach, ach_bonus = await ach_service.check_tips_award(user_id, total_views)
    if ach_bonus:
        await user_repo.add_coins(user_id, ach_bonus)

    await event_repo.log(user_id, "tip_viewed", {
        "category": category,
        "tip_id": tip_id,
        "total_views": total_views,
        "coin_granted": coin_granted,
    })
    for ach_id in new_ach:
        await event_repo.log(user_id, "achievement_unlocked", {"achievement_id": ach_id})
    if new_ach:
        await send_achievement_notification(user_id, new_ach)

    lines: list[str] = []
    if coin_granted:
        lines.append(t("tips.coin_today", locale, coins=TIP_COIN_PER_DAY))
    if new_ach:
        catalog = load_achievements_catalog(locale)
        tip_reward = catalog.get("10_tips_read", {}).get("reward", 30)
        lines.append(t("tips.achievement_curiosity", locale, reward=tip_reward))
    elif total_views < 10:
        lines.append(t("tips.read_count", locale, count=total_views))
    return "".join(lines)


async def _send_random_tip(message: Message, category: str) -> None:
    user_id = message.from_user.id
    locale = await loc(user_id)
    tip = await _pick_tip(user_id, category, locale)
    if not tip:
        await message.answer(t("tips.not_loaded", locale))
        return
    suffix = await _on_tip_viewed(user_id, category, tip.get("id"))
    await message.answer(
        _format_tip_message(category, tip, locale) + suffix,
        reply_markup=_tips_inline_keyboard(category, locale),
        parse_mode="HTML",
    )


async def _edit_or_send_tip(
    callback: CallbackQuery,
    category: str,
    tip: dict,
    *,
    page: int | None = None,
) -> None:
    locale = await loc(callback.from_user.id)
    tips = _tip_cats(locale)[category]["tips"]
    total = len(tips)
    suffix = await _on_tip_viewed(callback.from_user.id, category, tip.get("id"))
    body = _format_tip_message(
        category, tip, locale, page=page, total=total if page is not None else None,
    ) + suffix
    markup = _tips_inline_keyboard(
        category,
        locale,
        list_page=page,
        list_total=total if page is not None else None,
    )
    try:
        await callback.message.edit_text(body, reply_markup=markup, parse_mode="HTML")
    except Exception:
        await callback.message.answer(body, reply_markup=markup, parse_mode="HTML")


@router.message(kb_in("kb.tips"))
async def handle_tips_menu(message: Message, state: FSMContext):
    await _clear_custom_timer_duration_wait(state)
    locale = await loc(message.from_user.id)
    await message.answer(t("tips.menu", locale), reply_markup=get_tips_keyboard(locale))


@router.message(kb_in("kb.tips_time_mgmt"))
async def handle_time_management(message: Message):
    await _send_random_tip(message, "tm")


@router.message(kb_in("kb.tips_memory"))
async def handle_memory_retention(message: Message):
    await _send_random_tip(message, "mem")


@router.message(kb_in("kb.tips_bot_guide"))
async def handle_bot_guide_tips(message: Message):
    await _send_random_tip(message, "bot")


@router.message(kb_in("kb.tips_links"))
async def handle_links(message: Message):
    locale = await loc(message.from_user.id)
    if not PRODUCTIVITY_LINKS:
        await message.answer(t("tips.links_empty", locale))
        return
    await message.answer(
        t("tips.links_title", locale),
        reply_markup=_productivity_links_keyboard(locale),
    )


@router.callback_query(F.data.startswith("tips:more:"))
async def handle_tips_more(callback: CallbackQuery):
    locale = await loc(callback.from_user.id)
    category = callback.data.split(":", 2)[2]
    if category not in _tip_cats(locale):
        await callback.answer(t("tips.unknown_category", locale), show_alert=True)
        return
    tips = _tip_cats(locale)[category]["tips"]
    if not tips:
        await callback.answer(t("tips.empty_category", locale), show_alert=True)
        return
    await callback.answer()
    tip = await _pick_tip(callback.from_user.id, category, locale)
    if not tip:
        await callback.answer(t("tips.empty_category", locale), show_alert=True)
        return
    await _edit_or_send_tip(callback, category, tip)


@router.callback_query(F.data.startswith("tips:list:"))
async def handle_tips_list(callback: CallbackQuery):
    locale = await loc(callback.from_user.id)
    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer()
        return
    category = parts[2]
    try:
        page = int(parts[3])
    except ValueError:
        await callback.answer()
        return
    if category not in _tip_cats(locale):
        await callback.answer(t("tips.unknown_category", locale), show_alert=True)
        return
    tips = _tip_cats(locale)[category]["tips"]
    if not tips:
        await callback.answer(t("tips.empty_category", locale), show_alert=True)
        return
    page = max(0, min(page, len(tips) - 1))
    await callback.answer()
    await _edit_or_send_tip(callback, category, tips[page], page=page)


@router.callback_query(F.data == "tips:menu")
async def handle_tips_menu_callback(callback: CallbackQuery):
    locale = await loc(callback.from_user.id)
    await callback.answer()
    await callback.message.answer(t("tips.menu", locale), reply_markup=get_tips_keyboard(locale))

# ------------------------------------------------------------
# Админка и обратная связь
# ------------------------------------------------------------
@router.message(Command("reply"))
async def cmd_reply(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Нет прав.")
        return
    args = command.args
    if not args or len(args.split(maxsplit=1)) < 2:
        await message.answer("❌ Использование: /reply <user_id> <сообщение>")
        return
    user_id_str, reply_text = args.split(maxsplit=1)
    reply_text = truncate_text(reply_text.strip())
    if not reply_text:
        await message.answer("❌ Сообщение не может быть пустым.")
        return
    try:
        user_id = int(user_id_str)
        prefix = "📨 Ответ от администратора:\n\n"
        body = truncate_for_telegram_message(prefix, reply_text)
        await bot.send_message(user_id, f"{prefix}{body}")
        await message.answer(f"✅ Ответ отправлен пользователю {user_id}")
    except ValueError:
        await message.answer("❌ Неверный ID.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)}")

# Простая защита от двойного запуска /broadcast.
# В одном event loop достаточно булева флага: проверка и установка
# идут синхронно, без await-точки между ними.
_broadcast_in_progress = False


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, command: CommandObject):
    """Рассылка сообщения всем зарегистрированным пользователям."""
    global _broadcast_in_progress

    if not is_admin(message.from_user.id):
        await message.answer("❌ Нет прав.")
        return

    text = truncate_text((command.args or "").strip())
    if not text:
        await message.answer(
            "❌ Использование: /broadcast <сообщение>\n"
            "Сообщение получат все зарегистрированные пользователи."
        )
        return
    truncated = len((command.args or "").strip()) > TELEGRAM_MAX_MESSAGE_LEN

    if _broadcast_in_progress:
        await message.answer("⚠️ Рассылка уже идёт. Дождись её завершения.")
        return

    user_ids = await user_repo.get_all_user_ids()
    if not user_ids:
        await message.answer("📭 В базе нет пользователей.")
        return

    _broadcast_in_progress = True
    try:
        admin_id = message.from_user.id
        logger.info(
            "broadcast.start admin_id=%s recipients=%s text_len=%s",
            admin_id, len(user_ids), len(text),
        )
        await message.answer(f"📣 Начинаю рассылку для {len(user_ids)} пользователей…")
        if truncated:
            await message.answer(
                f"⚠️ Текст обрезан до {TELEGRAM_MAX_MESSAGE_LEN} символов (лимит Telegram)."
            )
        delivered = 0
        failed = 0
        failed_ids: list[int] = []
        for uid in user_ids:
            try:
                await send_with_telegram_bulkhead(
                    lambda uid=uid: bot.send_message(uid, text),
                    label="broadcast", uid=uid,
                )
                delivered += 1
            except TelegramForbiddenError:
                failed += 1
                failed_ids.append(uid)
                logger.info("broadcast.send_failed uid=%s reason=blocked", uid)
            except TelegramBadRequest as e:
                # Permanent: chat not found / message too long / parse error —
                # retry не поможет, помечаем как failed без back-off'а.
                failed += 1
                failed_ids.append(uid)
                logger.warning(
                    "broadcast.send_failed uid=%s reason=bad_request detail=%s",
                    uid, e,
                )
            except Exception as e:
                failed += 1
                failed_ids.append(uid)
                logger.warning(
                    "broadcast.send_failed uid=%s reason=%s detail=%s",
                    uid, type(e).__name__, e,
                )
            # Лёгкий троттлинг, чтобы не упереться в лимит Telegram (~30 msg/s)
            await asyncio.sleep(0.05)

        logger.info(
            "broadcast.done admin_id=%s delivered=%s failed=%s",
            admin_id, delivered, failed,
        )
        report = (
            f"✅ Рассылка завершена.\n"
            f"📨 Доставлено: {delivered}\n"
            f"❌ Не доставлено: {failed}"
        )
        if failed_ids:
            preview = ", ".join(str(i) for i in failed_ids[:10])
            extra = "" if len(failed_ids) <= 10 else f" и ещё {len(failed_ids) - 10}"
            report += f"\n\nID с ошибкой: {preview}{extra}"
        await message.answer(report)
    finally:
        _broadcast_in_progress = False


@router.message(Command("notif_status"))
async def cmd_notif_status(message: Message):
    """Диагностика подписок и напоминаний — показывает текущее состояние пользователя."""
    if not is_admin(message.from_user.id):
        await message.answer("❌ Команда только для админов.")
        return

    user_id = message.from_user.id
    user = await user_repo.get_user(user_id)
    settings = await user_repo.get_notification_settings(user_id)

    if not user:
        await message.answer("Сначала /start.")
        return
    if not settings:
        await message.answer("Нет строки в notification_settings. Это бага — сообщи разработчику.")
        return

    tz_name = user.get("timezone") or "Europe/Moscow"
    try:
        user_tz = pytz.timezone(tz_name)
        user_now = datetime.now(user_tz).strftime("%H:%M:%S")
    except Exception as e:
        user_now = f"ошибка: {e}"
    moscow_now = datetime.now(pytz.timezone("Europe/Moscow")).strftime("%H:%M:%S")

    distinct_tzs = await user_repo.get_distinct_timezones()

    # Симулируем срабатывание шедулера на сохранённое morning_time/evening_time.
    m_time = settings.get("morning_time") or ""
    e_time = settings.get("evening_time") or ""
    morning_hit = await user_repo.get_users_due_for_morning(tz_name, m_time)
    evening_hit = await user_repo.get_users_due_for_evening(tz_name, e_time)
    morning_includes_me = user_id in {u["user_id"] for u in morning_hit}
    evening_includes_me = user_id in {u["user_id"] for u in evening_hit}

    text = (
        f"📊 Диагностика уведомлений\n\n"
        f"👤 user_id: <code>{user_id}</code>\n"
        f"🌍 timezone: <code>{tz_name}</code>\n"
        f"🕐 сейчас в твоём TZ: <code>{user_now}</code>\n"
        f"🇷🇺 сейчас в Москве:    <code>{moscow_now}</code>\n\n"
        f"🌅 утро:  enabled=<code>{settings.get('morning_enabled')}</code>, "
        f"time=<code>{m_time}</code>\n"
        f"🌙 вечер: enabled=<code>{settings.get('evening_enabled')}</code>, "
        f"time=<code>{e_time}</code>\n\n"
        f"📡 шедулер обходит TZ: <code>{', '.join(distinct_tzs) or '—'}</code>\n\n"
        f"🧪 при срабатывании <code>{m_time}</code> в твоём TZ: "
        f"{len(morning_hit)} пользователь(ей), включая тебя — "
        f"{'✅ да' if morning_includes_me else '❌ НЕТ'}\n"
        f"🧪 при срабатывании <code>{e_time}</code> в твоём TZ: "
        f"{len(evening_hit)} пользователь(ей), включая тебя — "
        f"{'✅ да' if evening_includes_me else '❌ НЕТ'}"
    )
    if user.get("has_studied_today"):
        text += "\n\n⚠️ <i>has_studied_today=1, поэтому вечернее напоминание сегодня НЕ придёт по дизайну.</i>"

    await message.answer(text, parse_mode="HTML")


@router.message(Command("addadmin"))
async def cmd_addadmin(message: Message, command: CommandObject):
    """Добавляет пользователя в админы. Только главный админ (MAIN_ADMIN_ID)."""
    if message.from_user.id != MAIN_ADMIN_ID:
        await message.answer("❌ Только главный админ может управлять списком админов.")
        return
    if not command.args:
        await message.answer("❌ Использование: /addadmin <user_id>")
        return
    try:
        new_id = int(command.args.strip())
    except ValueError:
        await message.answer("❌ Неверный ID — нужно число.")
        return

    added = await admin_repo.add(new_id)
    if not added:
        await message.answer(f"ℹ️ Пользователь <code>{new_id}</code> уже админ.", parse_mode="HTML")
        return

    ADMINS.add(new_id)
    # Расширяем /-пикер новому админу. Если у Telegram нет чата с ним —
    # вызов упадёт, но это не критично: команды появятся, когда он откроет бот.
    try:
        await bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=new_id))
    except Exception as e:
        logger.warning(
            "set_my_commands.failed admin_id=%s reason=%s detail=%s",
            new_id, type(e).__name__, e,
        )

    logger.info("admin.added user_id=%s by=%s", new_id, message.from_user.id)
    await message.answer(f"✅ Пользователь <code>{new_id}</code> добавлен в админы.", parse_mode="HTML")


@router.message(Command("rmadmin"))
async def cmd_rmadmin(message: Message, command: CommandObject):
    """Удаляет пользователя из админов. Только главный админ."""
    if message.from_user.id != MAIN_ADMIN_ID:
        await message.answer("❌ Только главный админ может управлять списком админов.")
        return
    if not command.args:
        await message.answer("❌ Использование: /rmadmin <user_id>")
        return
    try:
        rm_id = int(command.args.strip())
    except ValueError:
        await message.answer("❌ Неверный ID — нужно число.")
        return

    if rm_id == MAIN_ADMIN_ID:
        await message.answer("❌ Нельзя удалить главного админа (MAIN_ADMIN_ID из .env).")
        return

    removed = await admin_repo.remove(rm_id)
    if not removed:
        await message.answer(f"ℹ️ Пользователь <code>{rm_id}</code> и так не админ.", parse_mode="HTML")
        return

    ADMINS.discard(rm_id)
    # Возвращаем дефолтные команды в /-пикер.
    try:
        await bot.set_my_commands(DEFAULT_COMMANDS, scope=BotCommandScopeChat(chat_id=rm_id))
    except Exception as e:
        logger.warning(
            "set_my_commands.failed admin_id=%s reason=%s detail=%s",
            rm_id, type(e).__name__, e,
        )

    logger.info("admin.removed user_id=%s by=%s", rm_id, message.from_user.id)
    await message.answer(f"✅ Пользователь <code>{rm_id}</code> удалён из админов.", parse_mode="HTML")


def _format_pct(value: float | None) -> str:
    """0.667 → '66.7%'; None → '—' (когда метрика недоступна)."""
    if value is None:
        return "—"
    return f"{value * 100:.1f}%"


def _render_cohort_table(data: dict) -> str:
    """Plain-text ASCII-таблица retention'а. Заворачивается в <pre> для HTML."""
    cohorts = data["cohorts"]
    if not cohorts:
        return "Пока нет данных — нет ни одного пользователя."
    # Ширины колонок: статичные, чтобы выровнять
    lines = []
    lines.append(f"{'Cohort':<10} | {'Size':>4} | {'D1':>6} | {'D7':>6} | {'D30':>6}")
    lines.append("-" * 46)
    for c in cohorts:
        lines.append(
            f"{c['week']:<10} | "
            f"{c['size']:>4} | "
            f"{_format_pct(c['d1']):>6} | "
            f"{_format_pct(c['d7']):>6} | "
            f"{_format_pct(c['d30']):>6}"
        )
    lines.append("-" * 46)
    lines.append(f"Total users: {data['total_users']}; today: {data['today']}")
    return "\n".join(lines)


def _render_segments(data: dict) -> str:
    total = data["total_users"]
    if total == 0:
        return "Пока нет пользователей."
    max_name = max(len(s["name"]) for s in data["segments"])
    lines = []
    for s in data["segments"]:
        bar_count = int(s["pct"] * 10)
        bar = "█" * bar_count + "░" * (10 - bar_count)
        lines.append(
            f"{s['name']:<{max_name}} {bar} {s['pct']*100:5.1f}% ({s['count']})"
        )
    lines.append("─" * (max_name + 25))
    lines.append(f"{'Total':<{max_name}} {'─' * 10}        ({total})")
    return "\n".join(lines)


def _build_hash_to_text_maps() -> tuple[dict[str, str], dict[str, str]]:
    """
    Строит мапы для обогащения analytics-результатов текстом:
      - situational term_hash → term_text (все секции, все subjects)
      - mcq question_hash → question_text
    Лимит на длину текста: 60 символов с обрезкой.
    """
    term_map: dict[str, str] = {}
    mcq_map: dict[str, str] = {}
    for sid, _label in SUBJECTS:
        # Situational terms из всех секций
        for _section_label, section_key in available_quiz_sections(sid):
            for term in load_quiz_section(section_key, sid):
                term_map[term.hash] = term.term
        # MCQ
        for q in load_mcq(sid):
            mcq_map[_mcq_hash(q["question"])] = q["question"]
    return term_map, mcq_map


def _render_content_stats(data: dict) -> str:
    """Рендерит content stats; enriches хеши именами через _build_hash_to_text_maps."""
    term_map, mcq_map = _build_hash_to_text_maps()
    lines = []

    # Hardest situational
    lines.append("🎯 <b>Hardest situational terms</b> (low accuracy first):")
    if not data["hardest_situational"]:
        lines.append("  <i>нет данных — никто ещё не отвечал</i>")
    else:
        for item in data["hardest_situational"]:
            h = item["term_hash"]
            text = term_map.get(h, f"<unknown:{h}>")[:50]
            acc = (item["accuracy"] or 0.0) * 100
            lines.append(f"  • {text} — {acc:.0f}% accuracy ({item['attempts']} attempts)")
    lines.append("")

    # Most-attempted MCQ
    lines.append("❓ <b>Most-attempted MCQ</b> (high volume first):")
    if not data["most_attempted_mcq"]:
        lines.append("  <i>нет данных — никто ещё не отвечал</i>")
    else:
        for item in data["most_attempted_mcq"]:
            h = item["question_hash"]
            text = mcq_map.get(h, f"<unknown:{h}>")[:50]
            attempts = item["attempts"] or 0
            acc = (item["accuracy"] or 0.0) * 100
            lines.append(f"  • {text}... — {attempts} attempts, {acc:.0f}% accuracy")
    lines.append("")

    # Progress coverage
    pc = data["progress_coverage"]
    lines.append("📚 <b>Progress coverage</b> (unique items touched):")
    lines.append(f"  • Situational terms attempted:    {pc['situational_terms_attempted']}")
    lines.append(f"  • Flashcards reviewed:            {pc['flashcards_reviewed']}")
    lines.append(f"  • MCQ questions seen:             {pc['mcq_questions_seen']}")
    lines.append(f"  • Tasks attempted:                {pc['tasks_attempted']}")
    lines.append("")

    # EF distribution
    ef = data["flashcard_ef_distribution"]
    total = ef["total"]
    lines.append("🃏 <b>Flashcard EF distribution</b>:")
    if total == 0:
        lines.append("  <i>нет данных — никто не оценивал карточки</i>")
    else:
        def pct(n): return f"{n/total*100:.1f}%" if total else "—"
        lines.append(f"  • EF < 1.5  (трудные):  {ef['lt_1_5']:>3} ({pct(ef['lt_1_5'])})")
        lines.append(f"  • 1.5–2.0:              {ef['1_5_to_2']:>3} ({pct(ef['1_5_to_2'])})")
        lines.append(f"  • 2.0–2.5:              {ef['2_to_2_5']:>3} ({pct(ef['2_to_2_5'])})")
        lines.append(f"  • EF ≥ 2.5 (лёгкие):    {ef['gte_2_5']:>3} ({pct(ef['gte_2_5'])})")
        lines.append(f"  <i>Чем ниже EF — тем сложнее карта пользователю по SM-2.</i>")
    lines.append("")

    split = data.get("flashcard_hash_split") or {}
    lines.append("🃏 <b>Flashcards: official vs user</b> (SM-2 rows):")
    if not split.get("total"):
        lines.append("  <i>нет данных</i>")
    else:
        lines.append(f"  • Official: {split.get('official_cards', 0)}")
        lines.append(f"  • User (u* hash): {split.get('user_cards', 0)}")
    lines.append("")

    lines.append("📚 <b>Subject engagement</b> (visits):")
    for item in data.get("subject_engagement") or []:
        lines.append(
            f"  • {item['subject_id']}: {item['total_visits']} visits, "
            f"{item['users']} users"
        )
    if not data.get("subject_engagement"):
        lines.append("  <i>нет данных</i>")
    lines.append("")

    lines.append("🎓 <b>Top tips</b> (tip_viewed events):")
    if not data.get("top_tips"):
        lines.append("  <i>нет данных</i>")
    else:
        for item in data["top_tips"]:
            lines.append(f"  • {item['tip_id']}: {item['views']} views")
    return "\n".join(lines)


def _render_event_timeline(events: list[dict], hours: int) -> str:
    if not events:
        return f"<i>Нет событий за последние {hours} часов.</i>"
    # Формат: HH:MM user=ID event_name key1=v1 key2=v2 (max 2 keys для компактности)
    lines = []
    for e in events:
        # HH:MM из created_at
        try:
            hhmm = e["created_at"][11:16]
        except (TypeError, IndexError):
            hhmm = "??:??"
        uid_part = f"u={e['user_id']}" if e["user_id"] else "u=—"
        # Top 2 properties для компактности
        props = e["properties"]
        prop_str = ""
        if isinstance(props, dict) and props:
            top_props = list(props.items())[:2]
            prop_str = " " + " ".join(f"{k}={v}" for k, v in top_props)
            if len(prop_str) > 50:
                prop_str = prop_str[:47] + "..."
        lines.append(f"{hhmm} {uid_part:<14} {e['event_name']}{prop_str}")
    return "\n".join(lines)


def _render_heatmap(data: dict) -> str:
    """
    7×8 grid с intensity blocks (· ▁ ▃ ▅ ▇ █). Lookup по % от peak.
    """
    grid = data["grid"]
    total = data["total_events"]
    if total == 0:
        return "<i>Нет событий за период.</i>"
    peak = max(max(row) for row in grid) or 1
    chars = " ·▁▃▅▆▇█"  # 8 уровней intensity (включая 0 = пробел/пусто)
    lines = []
    # Header: hour labels
    header = "      " + " ".join(f"{h}" for h in data["hour_labels"])
    lines.append(header)
    for wd_idx, row in enumerate(grid):
        label = data["weekday_labels"][wd_idx]
        cells = []
        for count in row:
            if count == 0:
                cells.append(" ·")
            else:
                # Map 1..peak to 1..7 (indices into chars)
                level = min(7, max(1, int(count / peak * 7 + 0.5)))
                cells.append(f" {chars[level]}")
        lines.append(f"{label}: {''.join(cells)}")
    return "\n".join(lines)


def _render_funnel(steps: list[dict], *, show_conv: bool = True) -> str:
    if not steps:
        return "Пока нет пользователей."
    max_name = max(len(s["name"]) for s in steps)
    lines = []
    for s in steps:
        bar_count = int(s["pct"] * 10)
        bar = "█" * bar_count + "░" * (10 - bar_count)
        conv = s.get("conv_from_prev")
        conv_str = f"  →{conv*100:4.0f}%" if show_conv and conv is not None else ""
        lines.append(
            f"{s['name']:<{max_name}} {bar} {s['pct']*100:5.1f}% ({s['count']}){conv_str}"
        )
    return "\n".join(lines)


def _render_activation_metrics(data: dict) -> str:
    if data.get("users_with_signup", 0) == 0:
        return "Пока нет пользователей."
    lines = [
        f"Users with signup: {data['users_with_signup']}",
        f"Users with 1st session (events): {data.get('users_with_first_session', 0)}",
    ]
    p24 = data.get("pct_first_session_within_24h")
    p7 = data.get("pct_first_session_within_7d")
    if p24 is not None:
        lines.append(f"1st session within 24h: {p24*100:.1f}% of registered")
    if p7 is not None:
        lines.append(f"1st session within 7d:  {p7*100:.1f}% of registered")
    lines.append("")
    lines.append("Time to first event (hours, median / p75, n):")
    for en, stats in (data.get("time_to_hours") or {}).items():
        med = stats.get("median")
        p75 = stats.get("p75")
        n = stats.get("n", 0)
        med_s = f"{med:.1f}" if med is not None else "—"
        p75_s = f"{p75:.1f}" if p75 is not None else "—"
        lines.append(f"  {en:<22} {med_s:>6} / {p75_s:>6}  (n={n})")
    return "\n".join(lines)


def _pct_str(rate: float | None) -> str:
    return f"{rate * 100:.1f}%" if rate is not None else "—"


def _render_product_metrics(data: dict) -> str:
    if data.get("total_registered", 0) == 0:
        return "Пока нет пользователей."
    total = data["total_registered"]
    lines = [f"Registered: {total}", ""]

    lines.append("📚 By subject (subject_picked → mode → quiz):")
    if not data.get("funnel_by_subject"):
        lines.append("  (нет данных)")
    else:
        for row in data["funnel_by_subject"]:
            lines.append(
                f"  {row['subject_id']}: subj {row['picked_subject']} "
                f"({row['pct_registered']*100:.0f}%) · mode {row['picked_mode']} "
                f"· quiz {row['quiz_answered']}"
            )
    lines.append("")

    lines.append("🎯 By mode (mode_picked):")
    for row in data.get("funnel_by_mode") or []:
        lines.append(
            f"  {row['mode']}: {row['users']} ({row['pct_registered']*100:.0f}%)"
        )
    lines.append("")

    lines.append("🔒 Strict event funnel (ever did steps 1..k):")
    for s in data.get("strict_event_funnel") or []:
        prev = s.get("pct_of_prev")
        prev_s = f" →{_pct_str(prev)}" if prev is not None else ""
        lines.append(
            f"  {s['name']}: {s['count']} ({s['pct_registered']*100:.0f}%){prev_s}"
        )
    lines.append("")

    lines.append("📅 Activation by signup week:")
    for c in data.get("activation_by_cohort") or []:
        med = c.get("median_hours_to_session")
        med_s = f"{med:.0f}h" if med is not None else "—"
        lines.append(
            f"  {c['week']}: n={c['users_with_session']} "
            f"med→session {med_s} · 24h {_pct_str(c.get('pct_session_within_24h'))}"
        )
    lines.append("")

    lines.append("📈 Feature retention D7 (active on signup+7):")
    for fr in data.get("feature_retention_d7") or []:
        w = fr["with_feature"]
        wo = fr["without_feature"]
        lines.append(
            f"  {fr['feature']}: WITH {_pct_str(w['rate'])} "
            f"({w['retained_d7']}/{w['eligible']}) · "
            f"WITHOUT {_pct_str(wo['rate'])} "
            f"({wo['retained_d7']}/{wo['eligible']})"
        )
    lines.append("")

    mre = data.get("morning_reminder_effect") or {}
    lines.append("🌅 Morning push → session same day:")
    lines.append(
        f"  pushes: {mre.get('morning_push_pairs', 0)} · "
        f"converted: {mre.get('same_day_session', 0)} · "
        f"rate: {_pct_str(mre.get('conversion_rate'))}"
    )
    lines.append("")

    lb = data.get("leaderboard") or {}
    lines.append("🏆 Leaderboard:")
    lines.append(
        f"  weekly_score rows: {lb.get('users_with_weekly_score', 0)} · "
        f"viewed: {lb.get('leaderboard_viewed_users', 0)} · "
        f"hidden: {lb.get('hidden_from_leaderboard', 0)} · "
        f"freezes: {lb.get('freeze_purchases', 0)} "
        f"({lb.get('users_bought_freeze', 0)} users)"
    )
    lines.append("")

    lines.append("🔔 Notification funnel:")
    for step in data.get("notification_funnel") or []:
        lines.append(f"  {step['step']}: {step['count']}")
    return "\n".join(lines)


def _render_engagement(data: dict) -> str:
    stickiness = data["stickiness"]
    stick_str = f"{stickiness * 100:.1f}%" if stickiness is not None else "—"
    stick_ev = data.get("stickiness_events")
    stick_ev_str = f"{stick_ev * 100:.1f}%" if stick_ev is not None else "—"
    lines = [
        f"Today:                  {data['today']}",
        f"New users today:        {data['new_today']}",
        "",
        "activity_progress (progress tables):",
        f"  DAU:                  {data['dau']}",
        f"  WAU (7d):             {data['wau']}",
        f"  MAU (30d):            {data['mau']}",
        f"  Stickiness:           {stick_str}",
        "",
        "activity_events (events table):",
        f"  DAU:                  {data.get('dau_events', 0)}",
        f"  WAU (7d):             {data.get('wau_events', 0)}",
        f"  MAU (30d):            {data.get('mau_events', 0)}",
        f"  Stickiness:           {stick_ev_str}",
        "",
        f"Total registered:       {data['total_users']}",
    ]
    return "\n".join(lines)


def _render_feature_usage(data: dict) -> str:
    if data["total_users"] == 0:
        return "Пока нет пользователей."
    lines = [f"% of {data['total_users']} registered users:\n"]
    max_name = max(len(f["name"]) for f in data["features"])
    for f in data["features"]:
        bar_count = int(f["pct"] * 10)
        bar = "█" * bar_count + "░" * (10 - bar_count)
        lines.append(
            f"{f['name']:<{max_name}} {bar} {f['pct']*100:5.1f}% ({f['count']})"
        )
    return "\n".join(lines)


@router.message(Command("funnel"))
async def cmd_funnel(message: Message):
    """Activation funnel — шаги от регистрации до 7-дневного стрика. % от total registered."""
    if not is_admin(message.from_user.id):
        await message.answer("❌ Команда только для админов.")
        return
    funnel = await analytics_service.compute_funnel()
    body = _render_funnel(funnel.get("steps") or [])
    ev_body = _render_funnel(funnel.get("event_steps") or [])
    note = (
        "\n\n<i>% от registered; →% = conversion от предыдущего шага. "
        "Верхний блок — progress + events mix; нижний — только events.</i>"
    )
    await message.answer(
        f"📊 <b>Activation funnel</b>\n\n<pre>{body}</pre>\n\n"
        f"<b>Event funnel</b>\n<pre>{ev_body}</pre>{note}",
        parse_mode="HTML",
    )


@router.message(Command("dau"))
async def cmd_dau(message: Message):
    """DAU / WAU / MAU + stickiness ratio."""
    if not is_admin(message.from_user.id):
        await message.answer("❌ Команда только для админов.")
        return
    data = await analytics_service.compute_engagement()
    body = _render_engagement(data)
    note = (
        "\n\n<i><b>Две метрики:</b> activity_progress (progress tables) — cohort/segments; "
        "activity_events (events table) — heatmap/timeline. Могут расходиться. "
        "Stickiness ≥20% — benchmark.</i>"
    )
    await message.answer(
        f"👥 <b>Active users</b>\n\n<pre>{body}</pre>{note}",
        parse_mode="HTML",
    )


@router.message(Command("activation"))
async def cmd_activation(message: Message):
    """Time-to-first-session и time-to-first-feature (из events)."""
    if not is_admin(message.from_user.id):
        await message.answer("❌ Команда только для админов.")
        return
    data = await analytics_service.compute_activation_metrics()
    body = _render_activation_metrics(data)
    await message.answer(
        f"⏱️ <b>Activation & time-to-value</b>\n\n<pre>{body}</pre>\n\n"
        f"<i>Медиана часов от signup до первого event_name. "
        f"24h/7d — доля всех registered с session_started в окне.</i>",
        parse_mode="HTML",
    )


@router.message(Command("product_metrics"))
async def cmd_product_metrics(message: Message):
    """Продуктовые метрики: subject/mode, strict funnel, D7 retention, push, LB."""
    if not is_admin(message.from_user.id):
        await message.answer("❌ Команда только для админов.")
        return
    data = await analytics_service.compute_product_metrics()
    body = _render_product_metrics(data)
    note = (
        "\n\n<i>Strict funnel = пользователи, у которых ever были ВСЕ шаги 1..k. "
        "D7 retention = активность ровно на signup+7 (activity_progress). "
        "Утро: reminder_sent(morning) и session_started в один календарный день.</i>"
    )
    await message.answer(
        f"📈 <b>Product metrics</b>\n\n<pre>{body}</pre>{note}",
        parse_mode="HTML",
    )


@router.message(Command("feature_usage"))
async def cmd_feature_usage(message: Message):
    """% пользователей, использовавших каждую фичу."""
    if not is_admin(message.from_user.id):
        await message.answer("❌ Команда только для админов.")
        return
    data = await analytics_service.compute_feature_usage()
    body = _render_feature_usage(data)
    await message.answer(
        f"🎮 <b>Feature adoption</b>\n\n<pre>{body}</pre>",
        parse_mode="HTML",
    )


@router.message(Command("segments"))
async def cmd_segments(message: Message):
    """User segmentation: never_started / tried / active / power / churned."""
    if not is_admin(message.from_user.id):
        await message.answer("❌ Команда только для админов.")
        return
    data = await analytics_service.compute_segments()
    body = _render_segments(data)
    note = (
        f"\n\n<i>Churned = последняя активность > {data['churned_days_threshold']} дн. назад. "
        f"Активность ≠ только Pomodoro: учитывается любое событие в progress-таблицах.</i>"
    )
    await message.answer(
        f"👥 <b>User segmentation</b>\n\n<pre>{body}</pre>{note}",
        parse_mode="HTML",
    )


@router.message(Command("content_stats"))
async def cmd_content_stats(message: Message):
    """Content effectiveness: hardest terms, popular MCQ, coverage, EF distribution."""
    if not is_admin(message.from_user.id):
        await message.answer("❌ Команда только для админов.")
        return
    data = await analytics_service.compute_content_stats()
    body = _render_content_stats(data)
    await message.answer(
        f"📚 <b>Content stats</b>\n\n{body}",
        parse_mode="HTML",
    )


@router.message(Command("event_timeline"))
async def cmd_event_timeline(message: Message, command: CommandObject):
    """
    Лента последних событий из events table.
    Использование: /event_timeline [hours]   # default 24
    """
    if not is_admin(message.from_user.id):
        await message.answer("❌ Команда только для админов.")
        return
    hours = 24
    if command.args:
        try:
            hours = int(command.args.strip())
            hours = max(1, min(168, hours))  # clamp [1, 168=7 days]
        except ValueError:
            pass
    events = await analytics_service.compute_event_timeline(hours=hours, limit=50)
    body = _render_event_timeline(events, hours)
    await message.answer(
        f"📜 <b>Event timeline</b> (last {hours}h, top {len(events)})\n\n<pre>{body}</pre>",
        parse_mode="HTML",
    )


@router.message(Command("heatmap"))
async def cmd_heatmap(message: Message, command: CommandObject):
    """
    Activity heatmap — события по часам × дням недели.
    Использование: /heatmap [days]   # default 30
    """
    if not is_admin(message.from_user.id):
        await message.answer("❌ Команда только для админов.")
        return
    days = 30
    if command.args:
        try:
            days = int(command.args.strip())
            days = max(1, min(365, days))
        except ValueError:
            pass
    data = await analytics_service.compute_heatmap(days=days)
    body = _render_heatmap(data)
    note_parts = [f"Total events: {data['total_events']} over {days} days"]
    if data["peak"]:
        p = data["peak"]
        note_parts.append(f"Peak: {p['weekday']} {p['hour_range']} ({p['count']} events)")
    note = "\n".join(note_parts)
    await message.answer(
        f"📅 <b>Activity heatmap</b>\n\n<pre>{body}\n\n{note}</pre>\n\n"
        f"<i>3-часовые бакеты × 7 дней недели. Server time.</i>",
        parse_mode="HTML",
    )


async def _send_all_tables_zip(reply_target) -> None:
    """
    Хелпер: собирает ZIP всех таблиц + metadata.json и шлёт пользователю.
    reply_target должен иметь .answer + .answer_document (Message или callback.message).
    """
    try:
        zip_bytes, metadata = await asyncio.wait_for(
            analytics_service.export_all_tables_zip(),
            timeout=120,
        )
    except asyncio.TimeoutError:
        logger.error("export.all_failed reason=timeout")
        await reply_target.answer("❌ Export-all timed out after 120s.")
        return
    except Exception as e:
        logger.error("export.all_failed reason=%s detail=%s", type(e).__name__, e)
        await reply_target.answer(f"❌ Export-all failed: {type(e).__name__}: {e}")
        return
    filename = f"palph-export-{datetime.now().strftime('%Y-%m-%d')}.zip"
    size_kb = len(zip_bytes) / 1024
    total_rows = sum(metadata["row_counts"].values())
    logger.info(
        "export.all_done tables=%s rows=%s size_kb=%.1f",
        len(metadata["tables"]), total_rows, size_kb,
    )
    top_5 = sorted(metadata["row_counts"].items(), key=lambda kv: -kv[1])[:5]
    breakdown = "\n".join(f"  • {t}: {n}" for t, n in top_5)
    await reply_target.answer_document(
        BufferedInputFile(zip_bytes, filename),
        caption=(
            f"📦 <b>Full dataset export</b>\n"
            f"Tables: {len(metadata['tables'])} · Rows: {total_rows} · {size_kb:.1f} KB\n"
            f"+ metadata.json со schema_version и timestamp\n\n"
            f"<b>Top tables by row count:</b>\n{breakdown}"
        ),
        parse_mode="HTML",
    )


@router.message(Command("export"))
async def cmd_export(message: Message, command: CommandObject):
    """
    Экспорт таблицы как CSV-файл (Telegram document).
    Использование:
      /export <table_alias>  — одну таблицу как CSV
      /export all            — все таблицы + metadata.json как ZIP
    """
    if not is_admin(message.from_user.id):
        await message.answer("❌ Команда только для админов.")
        return
    arg = (command.args or "").strip().lower()
    aliases = sorted(AnalyticsService.EXPORTABLE_TABLES.keys())
    if not arg:
        aliases_str = "\n".join(f"  • <code>{a}</code>" for a in aliases)
        await message.answer(
            "📦 Экспорт таблиц для анализа в Jupyter / pandas.\n\n"
            "Использование:\n"
            "  <code>/export &lt;alias&gt;</code> — одна таблица как CSV\n"
            "  <code>/export all</code> — все таблицы + metadata.json как ZIP\n\n"
            f"Доступные алиасы:\n{aliases_str}",
            parse_mode="HTML",
        )
        return
    # Special: /export all → ZIP всех таблиц
    if arg == "all":
        await _send_all_tables_zip(message)
        return
    if arg not in AnalyticsService.EXPORTABLE_TABLES:
        await message.answer(
            f"❌ Неизвестная таблица: <code>{arg}</code>\n"
            f"Доступно: {', '.join(aliases)} (или <code>all</code> для ZIP всех)",
            parse_mode="HTML",
        )
        return
    try:
        csv_bytes, row_count = await analytics_service.export_table_csv(arg)
    except Exception as e:
        logger.error("export.failed alias=%s reason=%s detail=%s", arg, type(e).__name__, e)
        await message.answer(f"❌ Export failed: {type(e).__name__}: {e}")
        return
    table_name = AnalyticsService.EXPORTABLE_TABLES[arg]
    filename = f"{table_name}-{datetime.now().strftime('%Y-%m-%d')}.csv"
    size_kb = len(csv_bytes) / 1024
    logger.info("export.done alias=%s table=%s rows=%s size_kb=%.1f", arg, table_name, row_count, size_kb)
    await message.answer_document(
        BufferedInputFile(csv_bytes, filename),
        caption=f"📦 <b>{table_name}</b>\n{row_count} rows · {size_kb:.1f} KB",
        parse_mode="HTML",
    )


# ============================================================
# /analytics — единый dashboard с inline-меню по разделам
# ============================================================
async def _build_analytics_home_text() -> str:
    """Главный экран /analytics: краткая сводка + приглашение."""
    data = await analytics_service.compute_engagement()
    stick = data["stickiness"]
    stick_str = f"{stick * 100:.1f}%" if stick is not None else "—"
    return (
        f"📊 <b>PA-аналитика</b>\n\n"
        f"<i>Today: {data['today']}</i>\n"
        f"👥 Всего: <b>{data['total_users']}</b> · "
        f"DAU: <b>{data['dau']}</b> · "
        f"Stickiness: <b>{stick_str}</b>\n\n"
        f"Выбери раздел:"
    )


def _build_analytics_menu_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔁 Cohort retention",                callback_data="anlt:cohort")
    kb.button(text="🎯 Activation funnel",               callback_data="anlt:funnel")
    kb.button(text="⏱️ Time-to-value",                   callback_data="anlt:activation")
    kb.button(text="📈 Product metrics",                 callback_data="anlt:product")
    kb.button(text="👥 Active users (DAU/WAU/MAU)",      callback_data="anlt:dau")
    kb.button(text="🎮 Feature adoption",                callback_data="anlt:features")
    kb.button(text="🧑‍🤝‍🧑 User segments",                callback_data="anlt:segments")
    kb.button(text="📚 Content stats",                   callback_data="anlt:content")
    kb.button(text="📜 Event timeline (24h)",            callback_data="anlt:timeline")
    kb.button(text="📅 Activity heatmap (30d)",          callback_data="anlt:heatmap")
    kb.button(text="📦 Export CSV →",                    callback_data="anlt:export_menu")
    kb.button(text="✖️ Закрыть",                          callback_data="anlt:close")
    kb.adjust(1)
    return kb.as_markup()


def _build_analytics_back_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ К аналитике", callback_data="anlt:back")
    kb.adjust(1)
    return kb.as_markup()


def _build_analytics_export_menu_keyboard() -> InlineKeyboardMarkup:
    """Подменю экспорта: «all» сверху отдельной строкой, потом таблицы по 2 в ряд + back."""
    kb = InlineKeyboardBuilder()
    # «All» одной кнопкой сверху — это «main path» для Jupyter-анализа.
    kb.button(text="📦📦 ALL tables (ZIP + metadata)", callback_data="anlt:export:all")
    # AnalyticsService.EXPORTABLE_TABLES — class attribute; не зависит от того,
    # инициализирован ли global analytics_service (важно для импорта в тестах).
    for alias in sorted(AnalyticsService.EXPORTABLE_TABLES.keys()):
        kb.button(text=f"📦 {alias}", callback_data=f"anlt:export:{alias}")
    kb.button(text="◀️ К аналитике", callback_data="anlt:back")
    # 1-кнопочная строка для «all», потом по 2 на ряд, потом 1 для back
    kb.adjust(1, 2, 2, 2, 2, 2, 1)
    return kb.as_markup()


async def _anlt_check_admin(callback: CallbackQuery) -> bool:
    """Анти-spoof: callbacks из /analytics доступны только админам."""
    if not is_admin(callback.from_user.id):
        await callback.answer(t("common.admin_only", await loc(callback.from_user.id)), show_alert=True)
        return False
    return True


@router.message(Command("analytics"))
async def cmd_analytics(message: Message):
    """Единая dashboard-команда: все PA-метрики через inline-меню."""
    if not is_admin(message.from_user.id):
        await message.answer("❌ Команда только для админов.")
        return
    text = await _build_analytics_home_text()
    await message.answer(
        text,
        reply_markup=_build_analytics_menu_keyboard(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "anlt:cohort")
async def handle_anlt_cohort(callback: CallbackQuery):
    if not await _anlt_check_admin(callback):
        return
    data = await analytics_service.compute_cohort_retention()
    table = _render_cohort_table(data)
    text = (
        f"🔁 <b>Cohort retention</b>\n\n<pre>{table}</pre>\n\n"
        f"<i>D_N = % активных ровно в день (signup + N). «—» = когорта моложе N дней.</i>"
    )
    try:
        await callback.message.edit_text(
            text, reply_markup=_build_analytics_back_keyboard(), parse_mode="HTML",
        )
    except Exception as e:
        logger.warning("anlt.edit_failed view=cohort reason=%s", e)
    await callback.answer()


@router.callback_query(F.data == "anlt:activation")
async def handle_anlt_activation(callback: CallbackQuery):
    if not await _anlt_check_admin(callback):
        return
    data = await analytics_service.compute_activation_metrics()
    body = _render_activation_metrics(data)
    text = (
        f"⏱️ <b>Activation & time-to-value</b>\n\n<pre>{body}</pre>\n\n"
        f"<i>Часы от signup до первого события (events table).</i>"
    )
    try:
        await callback.message.edit_text(
            text, reply_markup=_build_analytics_back_keyboard(), parse_mode="HTML",
        )
    except Exception as e:
        logger.warning("anlt.edit_failed view=activation reason=%s", e)
    await callback.answer()


@router.callback_query(F.data == "anlt:product")
async def handle_anlt_product(callback: CallbackQuery):
    if not await _anlt_check_admin(callback):
        return
    data = await analytics_service.compute_product_metrics()
    body = _render_product_metrics(data)
    text = (
        f"📈 <b>Product metrics</b>\n\n<pre>{body}</pre>\n\n"
        f"<i>Subject/mode из events; strict funnel; D7 retention; push/LB.</i>"
    )
    try:
        await callback.message.edit_text(
            text, reply_markup=_build_analytics_back_keyboard(), parse_mode="HTML",
        )
    except Exception as e:
        logger.warning("anlt.edit_failed view=product reason=%s", e)
    await callback.answer()


@router.callback_query(F.data == "anlt:funnel")
async def handle_anlt_funnel(callback: CallbackQuery):
    if not await _anlt_check_admin(callback):
        return
    funnel = await analytics_service.compute_funnel()
    body = _render_funnel(funnel.get("steps") or [])
    ev_body = _render_funnel(funnel.get("event_steps") or [])
    text = (
        f"🎯 <b>Activation funnel</b>\n\n<pre>{body}</pre>\n\n"
        f"<b>Event funnel</b>\n<pre>{ev_body}</pre>\n\n"
        f"<i>% от registered; →% = conv от prev step.</i>"
    )
    try:
        await callback.message.edit_text(
            text, reply_markup=_build_analytics_back_keyboard(), parse_mode="HTML",
        )
    except Exception as e:
        logger.warning("anlt.edit_failed view=funnel reason=%s", e)
    await callback.answer()


@router.callback_query(F.data == "anlt:dau")
async def handle_anlt_dau(callback: CallbackQuery):
    if not await _anlt_check_admin(callback):
        return
    data = await analytics_service.compute_engagement()
    body = _render_engagement(data)
    text = (
        f"👥 <b>Active users</b>\n\n<pre>{body}</pre>\n\n"
        f"<i>activity_progress vs activity_events — см. admin_commands.md. "
        f"Stickiness ≥20% — benchmark.</i>"
    )
    try:
        await callback.message.edit_text(
            text, reply_markup=_build_analytics_back_keyboard(), parse_mode="HTML",
        )
    except Exception as e:
        logger.warning("anlt.edit_failed view=dau reason=%s", e)
    await callback.answer()


@router.callback_query(F.data == "anlt:features")
async def handle_anlt_features(callback: CallbackQuery):
    if not await _anlt_check_admin(callback):
        return
    data = await analytics_service.compute_feature_usage()
    body = _render_feature_usage(data)
    text = f"🎮 <b>Feature adoption</b>\n\n<pre>{body}</pre>"
    try:
        await callback.message.edit_text(
            text, reply_markup=_build_analytics_back_keyboard(), parse_mode="HTML",
        )
    except Exception as e:
        logger.warning("anlt.edit_failed view=features reason=%s", e)
    await callback.answer()


@router.callback_query(F.data == "anlt:segments")
async def handle_anlt_segments(callback: CallbackQuery):
    if not await _anlt_check_admin(callback):
        return
    data = await analytics_service.compute_segments()
    body = _render_segments(data)
    text = (
        f"👥 <b>User segments</b>\n\n<pre>{body}</pre>\n\n"
        f"<i>Churned = последняя активность > {data['churned_days_threshold']} дн. назад.</i>"
    )
    try:
        await callback.message.edit_text(
            text, reply_markup=_build_analytics_back_keyboard(), parse_mode="HTML",
        )
    except Exception as e:
        logger.warning("anlt.edit_failed view=segments reason=%s", e)
    await callback.answer()


@router.callback_query(F.data == "anlt:content")
async def handle_anlt_content(callback: CallbackQuery):
    if not await _anlt_check_admin(callback):
        return
    data = await analytics_service.compute_content_stats()
    body = _render_content_stats(data)
    text = f"📚 <b>Content stats</b>\n\n{body}"
    try:
        await callback.message.edit_text(
            text, reply_markup=_build_analytics_back_keyboard(), parse_mode="HTML",
        )
    except Exception as e:
        logger.warning("anlt.edit_failed view=content reason=%s", e)
    await callback.answer()


@router.callback_query(F.data == "anlt:timeline")
async def handle_anlt_timeline(callback: CallbackQuery):
    if not await _anlt_check_admin(callback):
        return
    events = await analytics_service.compute_event_timeline(hours=24, limit=50)
    body = _render_event_timeline(events, 24)
    text = (
        f"📜 <b>Event timeline</b> (last 24h, top {len(events)})\n\n<pre>{body}</pre>\n\n"
        f"<i>Для другого окна: <code>/event_timeline 48</code> (часы 1..168).</i>"
    )
    try:
        await callback.message.edit_text(
            text, reply_markup=_build_analytics_back_keyboard(), parse_mode="HTML",
        )
    except Exception as e:
        logger.warning("anlt.edit_failed view=timeline reason=%s", e)
    await callback.answer()


@router.callback_query(F.data == "anlt:heatmap")
async def handle_anlt_heatmap(callback: CallbackQuery):
    if not await _anlt_check_admin(callback):
        return
    data = await analytics_service.compute_heatmap(days=30)
    body = _render_heatmap(data)
    note_parts = [f"Total events: {data['total_events']} over {data['days']} days"]
    if data["peak"]:
        p = data["peak"]
        note_parts.append(f"Peak: {p['weekday']} {p['hour_range']} ({p['count']} events)")
    text = (
        f"📅 <b>Activity heatmap</b>\n\n<pre>{body}\n\n{chr(10).join(note_parts)}</pre>\n\n"
        f"<i>3-часовые бакеты × 7 дней. Server time. "
        f"Для другого окна: <code>/heatmap 7</code> (дни 1..365).</i>"
    )
    try:
        await callback.message.edit_text(
            text, reply_markup=_build_analytics_back_keyboard(), parse_mode="HTML",
        )
    except Exception as e:
        logger.warning("anlt.edit_failed view=heatmap reason=%s", e)
    await callback.answer()


@router.callback_query(F.data == "anlt:export_menu")
async def handle_anlt_export_menu(callback: CallbackQuery):
    if not await _anlt_check_admin(callback):
        return
    try:
        await callback.message.edit_text(
            "📦 <b>Export CSV</b>\n\n"
            "Выбери таблицу — CSV-файл придёт отдельным сообщением.\n"
            "После загрузки можно выбрать ещё или вернуться к аналитике.",
            reply_markup=_build_analytics_export_menu_keyboard(),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning("anlt.edit_failed view=export_menu reason=%s", e)
    await callback.answer()


@router.callback_query(F.data.startswith("anlt:export:"))
async def handle_anlt_export_table(callback: CallbackQuery):
    if not await _anlt_check_admin(callback):
        return
    alias = callback.data.split(":", 2)[2]
    # Special: "all" → ZIP всех таблиц + metadata.json
    if alias == "all":
        await _send_all_tables_zip(callback.message)
        await callback.answer("📦 ZIP sent")
        return
    if alias not in AnalyticsService.EXPORTABLE_TABLES:
        await callback.answer(t("common.unknown_table", await loc(callback.from_user.id)), show_alert=True)
        return
    try:
        csv_bytes, row_count = await analytics_service.export_table_csv(alias)
    except Exception as e:
        logger.error("anlt.export_failed alias=%s reason=%s", alias, e)
        await callback.answer(f"❌ {type(e).__name__}: {e}", show_alert=True)
        return
    table_name = AnalyticsService.EXPORTABLE_TABLES[alias]
    filename = f"{table_name}-{datetime.now().strftime('%Y-%m-%d')}.csv"
    size_kb = len(csv_bytes) / 1024
    logger.info("anlt.export.done alias=%s rows=%s size_kb=%.1f", alias, row_count, size_kb)
    # Отправляем как отдельное сообщение — не editting'ом, чтобы пользователь
    # мог выбрать ещё одну таблицу без возврата.
    await callback.message.answer_document(
        BufferedInputFile(csv_bytes, filename),
        caption=f"📦 <b>{table_name}</b>\n{row_count} rows · {size_kb:.1f} KB",
        parse_mode="HTML",
    )
    await callback.answer(f"📦 {table_name} sent")


@router.callback_query(F.data == "anlt:back")
async def handle_anlt_back(callback: CallbackQuery):
    if not await _anlt_check_admin(callback):
        return
    text = await _build_analytics_home_text()
    try:
        await callback.message.edit_text(
            text, reply_markup=_build_analytics_menu_keyboard(), parse_mode="HTML",
        )
    except Exception as e:
        logger.warning("anlt.edit_failed view=back reason=%s", e)
    await callback.answer()


@router.callback_query(F.data == "anlt:close")
async def handle_anlt_close(callback: CallbackQuery):
    if not await _anlt_check_admin(callback):
        return
    try:
        await callback.message.delete()
    except Exception:
        # Если delete недоступен (>48ч или нет прав) — редактируем в текст-заглушку
        try:
            await callback.message.edit_text("✅ Аналитика закрыта.", reply_markup=None)
        except Exception:
            pass
    await callback.answer()


@router.message(Command("cohort_stats"))
async def cmd_cohort_stats(message: Message):
    """
    D1/D7/D30 retention по ISO-неделям регистрации.
    Strict-definition: активен ровно в день signup+N.
    Активность = любое событие в study_sessions/quiz_progress/flashcard_progress/
    mcq_progress/task_progress/user_subject_stats.
    """
    if not is_admin(message.from_user.id):
        await message.answer("❌ Команда только для админов.")
        return
    data = await analytics_service.compute_cohort_retention()
    table = _render_cohort_table(data)
    note = (
        "\n\n<i>D_N = % активных ровно в день (signup + N). "
        "«—» = когорта моложе N дней, данных пока нет. "
        "Активность = любое действие (Pomodoro / квиз / флэш / MCQ / задача).</i>"
    )
    await message.answer(
        f"📊 <b>Retention по когортам</b>\n\n<pre>{table}</pre>{note}",
        parse_mode="HTML",
    )


@router.message(Command("parse_logs"))
async def cmd_parse_logs(message: Message):
    """
    Парсит bot.log + ротированные bot.log.* в CSV, шлёт как Telegram-документ.
    Поднимает «прошлые» события (до того, как мы начали писать в events table).
    """
    if not is_admin(message.from_user.id):
        await message.answer("❌ Только для админов.")
        return
    from parse_logs import parse_log_file, to_csv_bytes

    log_file_path = Path(LOG_FILE)
    log_paths: list[Path] = []
    if log_file_path.exists():
        log_paths.append(log_file_path)
    # Rotated copies: bot.log.1, .2, ..., до 9 (RotatingFileHandler даёт max 5,
    # но проверяем больше для запаса).
    for i in range(1, 10):
        p = log_file_path.with_suffix(log_file_path.suffix + f".{i}")
        if p.exists():
            log_paths.append(p)

    if not log_paths:
        await message.answer(
            f"❌ Не найдено лог-файлов рядом с <code>{log_file_path}</code>.",
            parse_mode="HTML",
        )
        return

    all_rows: list[dict] = []
    for path in log_paths:
        try:
            rows = parse_log_file(path)
            all_rows.extend(rows)
        except Exception as e:
            logger.warning("parse_logs.file_failed file=%s reason=%s", path.name, e)
    all_rows.sort(key=lambda r: r["timestamp"])

    if not all_rows:
        await message.answer("📜 Файлы найдены, но парсинг дал 0 строк.")
        return

    csv_bytes = to_csv_bytes(all_rows)
    filename = f"events_from_logs-{datetime.now().strftime('%Y-%m-%d')}.csv"
    size_kb = len(csv_bytes) / 1024
    logger.info(
        "parse_logs.done files=%s rows=%s size_kb=%.1f",
        len(log_paths), len(all_rows), size_kb,
    )
    await message.answer_document(
        BufferedInputFile(csv_bytes, filename),
        caption=(
            f"📜 <b>Events from logs</b>\n"
            f"Files: {len(log_paths)} · Rows: {len(all_rows)} · {size_kb:.1f} KB\n\n"
            f"Колонки: timestamp, level, event_name, user_id, properties (JSON), raw_text"
        ),
        parse_mode="HTML",
    )


@router.message(Command("backup"))
async def cmd_backup(message: Message):
    """Принудительный snapshot БД. Только главный админ.
    Имя файла включает timestamp до секунд — не пересекается с daily-snapshot."""
    if message.from_user.id != MAIN_ADMIN_ID:
        await message.answer("❌ Только главный админ.")
        return
    await message.answer("💾 Делаю backup...")
    path = await backup_service.force_backup()
    if path is None:
        await message.answer("❌ Backup failed — посмотри в логи.")
        return
    size_kb = path.stat().st_size / 1024
    await message.answer(
        f"✅ Backup создан: <code>{path.name}</code>\n"
        f"Размер: {size_kb:.1f} KB\n"
        f"Папка: <code>{path.parent}</code>",
        parse_mode="HTML",
    )


@router.message(Command("listadmins"))
async def cmd_listadmins(message: Message):
    """Список админов. Только главный админ."""
    if message.from_user.id != MAIN_ADMIN_ID:
        await message.answer("❌ Только главный админ может смотреть список.")
        return
    ids = sorted(ADMINS)
    if not ids:
        await message.answer("📭 Список админов пуст.")
        return
    lines = [
        f"• <code>{uid}</code>{' ★ главный' if uid == MAIN_ADMIN_ID else ''}"
        for uid in ids
    ]
    await message.answer(
        f"👑 Админов: <b>{len(ids)}</b>\n\n" + "\n".join(lines),
        parse_mode="HTML",
    )


@router.message(Command("progress"))
async def cmd_progress(message: Message):
    """Прогресс по предметам — то же, что кнопка в профиле."""
    user_id = message.from_user.id
    user = await user_repo.get_user(user_id)
    if not user:
        await message.answer("Сначала напиши /start для регистрации")
        return
    text = await build_progress_view(user_id)
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Профиль", callback_data=f"back_to_profile:{user_id}")
    kb.adjust(1)
    await message.answer(text, reply_markup=kb.as_markup())


@router.message(Command("leaderboard", "leaderboards"))
async def cmd_leaderboard(message: Message):
    """Показывает недельный лидерборд: auto-routing newbie vs main + собственный ранг.
    См. LEADERBOARD.md."""
    user_id = message.from_user.id
    locale = await loc(user_id)
    try:
        text = await leaderboard_service.render_leaderboard(user_id)
        await message.answer(text, parse_mode="HTML")
        await event_repo.log(user_id, "leaderboard_viewed", {})
    except Exception as e:
        logger.warning(
            "leaderboard.render_failed user=%s err=%s detail=%s",
            user_id, type(e).__name__, e,
        )
        await message.answer(t("leaderboard.load_failed", locale))


# ============================================================
# Pet customization (TODO #16 Phase B): detail screen + picker UI
# ============================================================
PET_CUSTOMIZATION_ENABLED = False


async def _compute_pet_emotion_for_user(user_id: int) -> tuple:
    """
    Возвращает (emotion_str, FSInputFile | None). image=None если
    asset не найден — caller graceful'но fallback'нет на text-only.

    FSM-state не доступна тут (профиль обычно открывается вне таймера),
    поэтому is_studying=False. recently_excited вычисляется из
    pet.last_excited_at (в окне 5 минут).
    """
    from datetime import datetime, timedelta
    import pytz
    user = await user_repo.get_user(user_id)
    pet = await pet_repo.get_pet(user_id)

    recently_excited = False
    if pet and pet.get("last_excited_at"):
        try:
            last = datetime.strptime(
                pet["last_excited_at"], "%Y-%m-%d %H:%M:%S"
            )
            recently_excited = (datetime.now() - last) < timedelta(minutes=5)
        except (ValueError, TypeError):
            pass

    has_studied_today = bool(user["has_studied_today"]) if user else False
    tz_name = (user or {}).get("timezone") or "Europe/Moscow"
    try:
        now_local = datetime.now(pytz.timezone(tz_name))
    except Exception:
        now_local = datetime.now()

    emotion = derive_emotion(
        is_studying=False,
        recently_excited=recently_excited,
        has_studied_today=has_studied_today,
        now_local=now_local,
    )
    try:
        path = render_pet(pet, emotion, now_local=now_local)
        return emotion, FSInputFile(str(path))
    except FileNotFoundError:
        return emotion, None


def _picker_button_label(value: str, catalog: dict, user_pet, owned: set,
                          item_type: str) -> tuple:
    """
    Возвращает (button_text, callback_data) для одной кнопки picker'а.
    4 состояния по спеке: ⭐ equipped / ✓ owned / 💰 buyable / 🔒 locked.

    item_type ∈ {'color', 'accessory'} — для построения callback_data.
    """
    unlock_level, price = catalog[value]
    user_level = user_pet["level"] if user_pet else 1
    is_equipped = user_pet and user_pet.get(item_type) == value
    is_owned = value in owned

    if is_equipped:
        return (f"⭐ {value}", "pet_locked:equipped")
    if is_owned:
        return (f"✓ {value}", f"pet_equip:{item_type}:{value}")
    if user_level < unlock_level:
        return (
            f"🔒 ур.{unlock_level} · 💰{price} {value}",
            f"pet_locked:level:{unlock_level}",
        )
    return (f"💰{price} {value}", f"pet_buy:{item_type}:{value}")


async def _send_pet_menu(chat_id: int, user_id: int) -> None:
    """Pet detail screen: фото + caption + customization kb."""
    pet = await pet_repo.get_pet(user_id)
    if pet is None:
        await pet_repo.create_pet_with_defaults(user_id)
        pet = await pet_repo.get_pet(user_id)

    user = await user_repo.get_user(user_id)
    emotion, image = await _compute_pet_emotion_for_user(user_id)

    caption = (
        f"🐾 <b>{html_escape(pet['name'])}</b>\n\n"
        f"Уровень: <b>{pet['level']}</b>\n"
        f"XP: {pet['xp']}\n"
    )
    if PET_CUSTOMIZATION_ENABLED:
        caption += f"Цвет: {pet['color']}  ·  Аксессуар: {pet['accessory']}\n"
    caption += (
        f"Эмоция сейчас: {emotion}\n\n"
        f"💰 Баланс: {user['total_coins']} 🪙"
    )
    kb = InlineKeyboardBuilder()
    if PET_CUSTOMIZATION_ENABLED:
        kb.button(text="🎨 Цвета", callback_data=f"pet_colors:{user_id}")
        kb.button(text="🎁 Аксессуары", callback_data=f"pet_accessories:{user_id}")
    kb.button(text="✏️ Переименовать", callback_data=f"pet_rename:{user_id}")
    kb.button(text="◀️ Профиль", callback_data=f"pet_back_to_profile:{user_id}")
    if PET_CUSTOMIZATION_ENABLED:
        kb.adjust(2, 1, 1)
    else:
        kb.adjust(1, 1)

    if image is not None:
        try:
            await bot.send_photo(
                chat_id=chat_id,
                photo=image,
                caption=caption,
                parse_mode="HTML",
                reply_markup=kb.as_markup(),
            )
            return
        except Exception as e:
            logger.warning("pet.menu_send_photo_failed user=%s err=%s", user_id, e)

    await bot.send_message(
        chat_id=chat_id,
        text=caption + "\n\n<i>(изображение питомца недоступно)</i>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(Command("pet"))
async def cmd_pet(message: Message):
    """Питомец — то же, что кнопка в профиле."""
    user_id = message.from_user.id
    user = await user_repo.get_user(user_id)
    if not user:
        await message.answer("Сначала напиши /start для регистрации")
        return
    await _send_pet_menu(message.chat.id, user_id)


@router.callback_query(F.data.startswith("pet_menu:"))
async def pet_menu(callback: CallbackQuery):
    """Pet detail из профиля: удаляем текст профиля, отправляем фото питомца."""
    await callback.answer()
    user_id = callback.from_user.id
    try:
        await callback.message.delete()
    except Exception:
        pass
    await _send_pet_menu(callback.message.chat.id, user_id)


async def _render_picker(callback: CallbackQuery, item_type: str) -> None:
    """Generic picker renderer для colors/accessories."""
    if not PET_CUSTOMIZATION_ENABLED:
        await callback.answer()
        user_id = callback.from_user.id
        try:
            await callback.message.delete()
        except Exception:
            pass
        await _send_pet_menu(callback.message.chat.id, user_id)
        return

    user_id = callback.from_user.id
    pet = await pet_repo.get_pet(user_id)
    if pet is None:
        await pet_repo.create_pet_with_defaults(user_id)
        pet = await pet_repo.get_pet(user_id)

    if item_type == "color":
        catalog = PetRepository.COLOR_CATALOG
        title = "🎨 Цвета"
    else:
        catalog = PetRepository.ACCESSORY_CATALOG
        title = "🎁 Аксессуары"

    inventory = await pet_repo.get_inventory(user_id)
    owned = {i["item_value"] for i in inventory if i["item_type"] == item_type}

    kb = InlineKeyboardBuilder()
    for value in catalog.keys():
        text, cb_data = _picker_button_label(value, catalog, pet, owned, item_type)
        kb.button(text=text, callback_data=cb_data)
    kb.button(text="◀️ Назад к питомцу", callback_data=f"pet_menu:{user_id}")
    kb.adjust(1)

    msg = (
        f"<b>{title}</b>\n\n"
        f"⭐ — надето\n"
        f"✓ — куплено (нажми чтобы надеть)\n"
        f"💰 — доступно к покупке\n"
        f"🔒 — заблокировано до указанного уровня\n\n"
        f"Уровень: <b>{pet['level']}</b>"
    )
    try:
        if callback.message.photo:
            await callback.message.edit_caption(
                caption=msg, parse_mode="HTML", reply_markup=kb.as_markup(),
            )
        else:
            await callback.message.edit_text(
                msg, parse_mode="HTML", reply_markup=kb.as_markup(),
            )
    except Exception as e:
        logger.warning("pet.picker_render_failed user=%s err=%s", user_id, e)


@router.callback_query(F.data.startswith("pet_colors:"))
async def pet_colors(callback: CallbackQuery):
    if not PET_CUSTOMIZATION_ENABLED:
        await callback.answer()
        return
    await callback.answer()
    await _render_picker(callback, "color")


@router.callback_query(F.data.startswith("pet_accessories:"))
async def pet_accessories(callback: CallbackQuery):
    if not PET_CUSTOMIZATION_ENABLED:
        await callback.answer()
        return
    await callback.answer()
    await _render_picker(callback, "accessory")


@router.callback_query(F.data.startswith("pet_locked:"))
async def pet_locked(callback: CallbackQuery):
    """Alert на нажатие locked / already-equipped кнопки."""
    if not PET_CUSTOMIZATION_ENABLED:
        await callback.answer()
        return
    data = callback.data
    if data.startswith("pet_locked:level:"):
        try:
            lvl = int(data.split(":", 2)[2])
            await callback.answer(
                f"🔒 Открывается на уровне {lvl}. Учись больше!",
                show_alert=True,
            )
        except (ValueError, IndexError):
            await callback.answer()
    elif data == "pet_locked:equipped":
        await callback.answer("⭐ Уже надето!")
    else:
        await callback.answer()


@router.callback_query(F.data.startswith("pet_equip:"))
async def pet_equip(callback: CallbackQuery):
    """Надеть уже купленный предмет (color/accessory)."""
    if not PET_CUSTOMIZATION_ENABLED:
        await callback.answer()
        return
    user_id = callback.from_user.id
    try:
        _, item_type, item_value = callback.data.split(":", 2)
    except ValueError:
        await callback.answer()
        return
    success = await pet_repo.equip(user_id, item_type, item_value)
    await callback.answer(
        f"⭐ Надето: {item_value}" if success else "❌ Не удалось надеть",
        show_alert=not success,
    )
    if success:
        await event_repo.log(
            user_id,
            "pet_equipped",
            {"item_type": item_type, "item_value": item_value},
        )
        await _render_picker(callback, item_type)


@router.callback_query(F.data.startswith("pet_buy:"))
async def pet_buy_confirm_dialog(callback: CallbackQuery):
    """Confirm dialog перед покупкой."""
    if not PET_CUSTOMIZATION_ENABLED:
        await callback.answer()
        return
    await callback.answer()
    user_id = callback.from_user.id
    try:
        _, item_type, item_value = callback.data.split(":", 2)
    except ValueError:
        return
    catalog = (
        PetRepository.COLOR_CATALOG if item_type == "color"
        else PetRepository.ACCESSORY_CATALOG if item_type == "accessory"
        else None
    )
    if catalog is None or item_value not in catalog:
        return
    unlock_level, price = catalog[item_value]
    user = await user_repo.get_user(user_id)

    msg = (
        f"💰 <b>Купить {item_type} «{item_value}»?</b>\n\n"
        f"Цена: <b>{price}</b> 🪙\n"
        f"Твой баланс: {user['total_coins']} 🪙\n"
        f"После покупки: <b>{user['total_coins'] - price}</b> 🪙\n\n"
        f"После покупки предмет автоматически надевается."
    )
    kb = InlineKeyboardBuilder()
    kb.button(
        text=f"✅ Купить за {price} 🪙",
        callback_data=f"pet_buy_do:{item_type}:{item_value}",
    )
    back_cb = (
        f"pet_colors:{user_id}" if item_type == "color"
        else f"pet_accessories:{user_id}"
    )
    kb.button(text="◀️ Отмена", callback_data=back_cb)
    kb.adjust(1)
    try:
        if callback.message.photo:
            await callback.message.edit_caption(
                caption=msg, parse_mode="HTML", reply_markup=kb.as_markup(),
            )
        else:
            await callback.message.edit_text(
                msg, parse_mode="HTML", reply_markup=kb.as_markup(),
            )
    except Exception as e:
        logger.warning("pet.buy_confirm_failed user=%s err=%s", user_id, e)


@router.callback_query(F.data.startswith("pet_buy_do:"))
async def pet_buy_do(callback: CallbackQuery):
    """Атомарная покупка через PetRepository.purchase_item."""
    if not PET_CUSTOMIZATION_ENABLED:
        await callback.answer()
        return
    user_id = callback.from_user.id
    try:
        _, item_type, item_value = callback.data.split(":", 2)
    except ValueError:
        await callback.answer()
        return
    result = await pet_repo.purchase_item(user_id, item_type, item_value)
    feedback_map = {
        "purchased": f"✅ Куплено и надето: {item_value}!",
        "already_owned": "👌 У тебя уже есть этот предмет.",
        "insufficient_coins": "❌ Не хватает монет.",
        "insufficient_level": "🔒 Уровень слишком низкий.",
        "unknown_item": "❌ Такого предмета не существует.",
        "no_pet": "❌ Питомец ещё не создан — сделай первую сессию.",
    }
    await callback.answer(
        feedback_map.get(result, "Что-то пошло не так."),
        show_alert=(result != "purchased"),
    )
    if result == "purchased":
        await event_repo.log(
            user_id,
            "pet_purchased",
            {"item_type": item_type, "item_value": item_value},
        )
    await _render_picker(callback, item_type)


@router.callback_query(F.data.startswith("pet_rename:"))
async def pet_rename_start(callback: CallbackQuery, state: FSMContext):
    """Войти в FSM ожидания нового имени."""
    await callback.answer()
    await state.set_state(PetStates.waiting_for_name)
    prompt = (
        "✏️ <b>Переименовать питомца</b>\n\n"
        "Введи новое имя (до 20 символов).\n"
        "Для отмены отправь /cancel."
    )
    try:
        if callback.message.photo:
            await callback.message.edit_caption(caption=prompt, parse_mode="HTML")
        else:
            await callback.message.edit_text(prompt, parse_mode="HTML")
    except Exception:
        await bot.send_message(callback.message.chat.id, prompt, parse_mode="HTML")


@router.message(PetStates.waiting_for_name, Command("cancel"))
async def pet_rename_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Переименование отменено.")


@router.message(PetStates.waiting_for_name)
async def pet_rename_process(message: Message, state: FSMContext):
    """Принимает новое имя и переименовывает питомца."""
    user_id = message.from_user.id
    new_name = (message.text or "").strip()
    if not new_name:
        await message.answer(
            "Имя не может быть пустым. Попробуй ещё раз или /cancel."
        )
        return
    if len(new_name) > 20:
        await message.answer(
            f"Слишком длинное ({len(new_name)} симв., максимум 20). Попробуй короче или /cancel."
        )
        return
    ok = await pet_repo.rename(user_id, new_name)
    await state.clear()
    if ok:
        await event_repo.log(
            user_id,
            "pet_renamed",
            {"name": new_name[:20]},
        )
        await message.answer(f"✅ Питомец теперь называется «{new_name}».")
    else:
        await message.answer(
            "Не удалось переименовать — возможно, питомец ещё не создан. "
            "Сделай первую сессию через /start."
        )


@router.callback_query(F.data.startswith("pet_back_to_profile:"))
async def pet_back_to_profile(callback: CallbackQuery):
    """
    Возврат из photo-based pet detail в text-based профиль.
    Удаляем фото-сообщение и шлём свежее текстовое сообщение профиля.
    Отдельный handler (не общий back_to_profile), потому что
    callback.message здесь photo — edit_text не работает.
    """
    await callback.answer()
    user_id = callback.from_user.id
    try:
        await callback.message.delete()
    except Exception:
        pass
    user = await user_repo.get_user(user_id)
    if not user:
        return
    locale = await loc(user_id)
    await bot.send_message(
        callback.message.chat.id,
        _profile_title_text(user, user_id, locale),
        reply_markup=_build_profile_inline_keyboard(user_id, locale),
    )


# ============================================================
# Friends system (Phase 4 / LEADERBOARD.md §Segments → Friends)
# ============================================================
def _friends_menu_keyboard(user_id: int, pending_count: int = 0) -> InlineKeyboardMarkup:
    """Inline-клавиатура для friends-tab: add / invite-link / pending / remove."""
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить друга", callback_data=f"friend_add_start:{user_id}")
    kb.button(text="🔗 Пригласить по ссылке", callback_data=f"friend_share_link:{user_id}")
    pending_label = (
        f"📩 Запросы ({pending_count})" if pending_count > 0 else "📩 Запросы"
    )
    kb.button(text=pending_label, callback_data=f"friend_pending:{user_id}")
    kb.button(text="➖ Удалить друга", callback_data=f"friend_remove_list:{user_id}")
    kb.adjust(1)
    return kb.as_markup()


async def _build_friend_invite_message(user_id: int) -> str | None:
    """
    Создаёт invite-token и возвращает HTML-текст с deep-link
    (t.me/Bot?start=friend_<token>). None — если @username бота недоступен.
    """
    if not bot_username:
        return None
    token = await friend_repo.create_invite_token(user_id)
    link = f"https://t.me/{bot_username}?start=friend_{token}"
    return (
        f"👥 <b>Поделись этой ссылкой с друзьями:</b>\n\n"
        f"<code>{link}</code>\n\n"
        f"Кто откроет ссылку — автоматически станет твоим другом 🎉\n"
        f"Срок действия: 3 дня."
    )


_BOT_USERNAME_UNAVAILABLE = (
    "⚠️ Бот ещё не определил свой @username (не удалось получить "
    "его при старте). Попробуй позже."
)


@router.message(Command("share_friend"))
async def cmd_share_friend(message: Message):
    """
    Создаёт invite-token и шлёт пользователю deep-link, которым тот
    может поделиться. Кто откроет ссылку — автоматически становится
    другом (skip pending). 3-day TTL, multiuse.
    """
    user_id = message.from_user.id
    if not await user_repo.user_exists(user_id):
        await message.answer("Сначала отправь /start.")
        return
    invite_text = await _build_friend_invite_message(user_id)
    if invite_text is None:
        await message.answer(_BOT_USERNAME_UNAVAILABLE)
        return
    await message.answer(invite_text, parse_mode="HTML")


@router.callback_query(F.data.startswith("friend_share_link:"))
async def friend_share_link(callback: CallbackQuery):
    """Кнопка «Пригласить по ссылке» в friends-tab."""
    await callback.answer()
    user_id = callback.from_user.id
    invite_text = await _build_friend_invite_message(user_id)
    if invite_text is None:
        await callback.message.answer(_BOT_USERNAME_UNAVAILABLE)
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ К друзьям", callback_data=f"friends_back:{user_id}")
    try:
        await callback.message.edit_text(
            invite_text,
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
    except Exception as e:
        logger.warning("friends.share_link_render_failed user=%s err=%s", user_id, e)
        await callback.message.answer(invite_text, parse_mode="HTML")


# ------------------------------------------------------------
# Desktop-приложение: привязка устройства (api.py)
# ------------------------------------------------------------
@router.message(Command("link_app"))
async def cmd_link_app(message: Message):
    """
    Выдаёт одноразовый код для входа в desktop-приложение. Код меняется
    на долгоживущий токен через POST /auth/link (см. api.py), поэтому
    паролей у пользователя нет — аккаунт остаётся телеграмным.
    """
    user_id = message.from_user.id
    locale = await loc(user_id)
    if not await user_repo.user_exists(user_id):
        await message.answer(t("devices.needs_start", locale))
        return
    try:
        code = await device_repo.create_link_code(user_id)
        devices = await device_repo.list_devices(user_id)
    except Exception as e:
        logger.warning(
            "devices.link_code_failed user=%s reason=%s detail=%s",
            user_id, type(e).__name__, e,
        )
        await message.answer(t("devices.link_failed", locale))
        return

    text = t(
        "devices.link_intro", locale,
        code=DeviceRepository.format_code(code),
        minutes=DeviceRepository.CODE_TTL_MINUTES,
    )
    if devices:
        text += t("devices.linked_count", locale, count=len(devices))
    await message.answer(text, parse_mode="HTML")


@router.message(Command("unlink_app"))
async def cmd_unlink_app(message: Message):
    """Отзывает все токены устройств пользователя (и висящий код привязки)."""
    user_id = message.from_user.id
    locale = await loc(user_id)
    count = await device_repo.revoke_all(user_id)
    if count:
        await message.answer(t("devices.unlink_done", locale, count=count))
    else:
        await message.answer(t("devices.unlink_none", locale))


@router.message(Command("friends"))
async def cmd_friends(message: Message):
    """Friends-tab: weekly-ранжированный список друзей + меню действий."""
    user_id = message.from_user.id
    try:
        text = await leaderboard_service.render_friends_tab(user_id)
        pending = await friend_repo.get_pending_received(user_id)
        await message.answer(
            text,
            parse_mode="HTML",
            reply_markup=_friends_menu_keyboard(user_id, len(pending)),
        )
    except Exception as e:
        logger.warning(
            "friends.render_failed user=%s err=%s detail=%s",
            user_id, type(e).__name__, e,
        )
        await message.answer("Не удалось загрузить друзей. Попробуй позже.")


@router.callback_query(F.data.startswith("friends_back:"))
async def friends_back(callback: CallbackQuery):
    """Возврат к friends-tab из любого вложенного экрана."""
    await callback.answer()
    user_id = callback.from_user.id
    text = await leaderboard_service.render_friends_tab(user_id)
    pending = await friend_repo.get_pending_received(user_id)
    try:
        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=_friends_menu_keyboard(user_id, len(pending)),
        )
    except Exception as e:
        logger.warning("friends.back_render_failed user=%s err=%s", user_id, e)


# ------------------------------------------------------------
# Add friend — FSM (waiting for user_id)
# ------------------------------------------------------------
@router.callback_query(F.data.startswith("friend_add_start:"))
async def friend_add_start(callback: CallbackQuery, state: FSMContext):
    """Просит ввести @username ИЛИ Telegram ID."""
    await callback.answer()
    user_id = callback.from_user.id
    await state.set_state(FriendStates.waiting_for_user_id)
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Отмена", callback_data=f"friends_back:{user_id}")
    try:
        await callback.message.edit_text(
            "🆔 Введи <b>@username</b> или <b>Telegram ID</b> пользователя.\n\n"
            "Примеры:\n"
            "  • <code>@alice</code>\n"
            "  • <code>alice</code> (без @)\n"
            "  • <code>123456789</code>\n\n"
            "💡 Username сработает, только если пользователь хотя бы раз "
            "взаимодействовал с ботом — на /start мы запоминаем его @handle. "
            "Если поиск не нашёл — попроси прислать тебе свой Telegram ID "
            "через @userinfobot.\n\n"
            "Для отмены отправь /cancel.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
    except Exception as e:
        logger.warning("friends.add_start_render_failed user=%s err=%s", user_id, e)


@router.message(FriendStates.waiting_for_user_id, Command("cancel"))
async def friend_add_cancel(message: Message, state: FSMContext):
    """Отмена add-friend FSM (через /cancel)."""
    await state.clear()
    await message.answer("Добавление отменено.")


@router.message(FriendStates.waiting_for_user_id)
async def friend_add_process(message: Message, state: FSMContext):
    """
    Парсит введённый @username ИЛИ Telegram ID, резолвит к user_id и
    отправляет request. Если успешно — шлёт notification target'у
    (с inline-кнопками Accept/Reject).
    """
    user_id = message.from_user.id
    text_input = (message.text or "").strip()
    if len(text_input) > FRIEND_QUERY_MAX_LEN:
        await message.answer(
            f"❌ Слишком длинный ввод (максимум {FRIEND_QUERY_MAX_LEN} символов). "
            "Введи @username или числовой Telegram ID (или /cancel)."
        )
        return

    username, target_id = parse_friend_query(text_input)
    if username is None and target_id is None:
        await message.answer(
            "❌ Не понял ввод. Введи @username или числовой Telegram ID "
            "(или /cancel)."
        )
        return

    # Username path — резолвим к user_id через кеш users.username
    if username is not None:
        target_id = await user_repo.find_user_id_by_username(username)
        if target_id is None:
            await message.answer(
                f"❌ Пользователь <code>@{html_escape(username)}</code> не найден.\n"
                f"Возможно, он ещё не открывал бота, скрыл @handle или "
                f"имя написано с опечаткой. Попроси его прислать тебе "
                f"свой числовой Telegram ID и попробуй снова через /friends.",
                parse_mode="HTML",
            )
            await state.clear()
            return

    await state.clear()
    result = await friend_repo.send_request(user_id, target_id)
    if result == "sent":
        await event_repo.log(
            user_id,
            "friend_request_sent",
            {"target_user_id": target_id},
        )
    elif result == "auto_accepted":
        await event_repo.log(
            user_id,
            "friend_accepted",
            {"other_user_id": target_id, "source": "auto_accept"},
        )

    feedback_map = {
        "self_target": "🙂 Нельзя добавить самого себя.",
        "user_not_found": "❌ Пользователь с таким ID не зарегистрирован в боте.",
        "already_friends": "👥 Вы уже друзья.",
        "already_pending": "📩 Запрос уже отправлен; ждём ответа.",
        "auto_accepted": (
            "🎉 У этого пользователя уже был запрос к тебе — "
            "вы автоматически стали друзьями!"
        ),
        "sent": "✅ Запрос отправлен. Жди подтверждения.",
    }
    feedback = feedback_map.get(result, "Что-то пошло не так. Попробуй позже.")

    # Notify target если был отправлен новый request или произошёл auto-accept.
    if result == "sent":
        kb = InlineKeyboardBuilder()
        kb.button(text="✅ Принять", callback_data=f"friend_accept:{user_id}")
        kb.button(text="❌ Отклонить", callback_data=f"friend_reject:{user_id}")
        kb.adjust(2)
        try:
            await bot.send_message(
                target_id,
                f"👥 Пользователь <code>{user_id}</code> хочет добавить тебя в друзья.",
                parse_mode="HTML",
                reply_markup=kb.as_markup(),
            )
        except Exception as e:
            logger.info(
                "friends.notify_failed_target user=%s reason=%s",
                target_id, type(e).__name__,
            )
            feedback += "\n\n⚠️ Не удалось доставить уведомление — возможно, пользователь заблокировал бота."
    elif result == "auto_accepted":
        try:
            await bot.send_message(
                target_id,
                f"🎉 Пользователь <code>{user_id}</code> отправил тебе запрос — "
                f"вы автоматически стали друзьями (у тебя был встречный запрос).",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.info(
                "friends.notify_failed_auto_accept user=%s reason=%s",
                target_id, type(e).__name__,
            )

    await message.answer(feedback)


# ------------------------------------------------------------
# Pending received requests — Accept / Reject
# ------------------------------------------------------------
@router.callback_query(F.data.startswith("friend_pending:"))
async def friend_pending_list(callback: CallbackQuery):
    """Список входящих запросов; для каждого — Accept/Reject inline."""
    await callback.answer()
    user_id = callback.from_user.id
    pending = await friend_repo.get_pending_received(user_id)
    if not pending:
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data=f"friends_back:{user_id}")
        try:
            await callback.message.edit_text(
                "📩 Входящих запросов нет.",
                reply_markup=kb.as_markup(),
            )
        except Exception as e:
            logger.warning("friends.pending_empty_render user=%s err=%s", user_id, e)
        return

    lines = ["📩 <b>Входящие запросы:</b>", ""]
    kb = InlineKeyboardBuilder()
    for req in pending:
        fid = req["from_user_id"]
        lines.append(f"• id=<code>{fid}</code>")
        kb.button(text=f"✅ Принять id={fid}", callback_data=f"friend_accept:{fid}")
        kb.button(text=f"❌ Отклонить id={fid}", callback_data=f"friend_reject:{fid}")
    kb.button(text="◀️ Назад", callback_data=f"friends_back:{user_id}")
    kb.adjust(2)  # 2 кнопки на ряд (accept+reject пары)
    try:
        await callback.message.edit_text(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
    except Exception as e:
        logger.warning("friends.pending_list_render user=%s err=%s", user_id, e)


@router.callback_query(F.data.startswith("friend_accept:"))
async def friend_accept(callback: CallbackQuery):
    """Принять входящий request. callback_data = friend_accept:<from_user_id>"""
    await callback.answer()
    me = callback.from_user.id
    try:
        from_uid = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        return
    accepted = await friend_repo.accept_request(from_uid, me)
    if not accepted:
        await callback.answer(t("common.request_inactive", await loc(callback.from_user.id)), show_alert=True)
    else:
        await event_repo.log(
            me,
            "friend_accepted",
            {"other_user_id": from_uid, "source": "request_accept"},
        )
        # Notify requester
        try:
            await bot.send_message(
                from_uid,
                f"🎉 Пользователь <code>{me}</code> принял твой запрос — "
                f"теперь вы друзья!",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.info(
                "friends.notify_failed_accept user=%s reason=%s",
                from_uid, type(e).__name__,
            )
    # Возврат к friends-tab
    text = await leaderboard_service.render_friends_tab(me)
    pending = await friend_repo.get_pending_received(me)
    try:
        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=_friends_menu_keyboard(me, len(pending)),
        )
    except Exception as e:
        logger.warning("friends.accept_render user=%s err=%s", me, e)


@router.callback_query(F.data.startswith("friend_reject:"))
async def friend_reject(callback: CallbackQuery):
    """Отклонить входящий request. callback_data = friend_reject:<from_user_id>"""
    await callback.answer()
    me = callback.from_user.id
    try:
        from_uid = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        return
    await friend_repo.reject_request(from_uid, me)
    # Молча возвращаемся в friends-tab (без уведомления отправителю — спека)
    text = await leaderboard_service.render_friends_tab(me)
    pending = await friend_repo.get_pending_received(me)
    try:
        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=_friends_menu_keyboard(me, len(pending)),
        )
    except Exception as e:
        logger.warning("friends.reject_render user=%s err=%s", me, e)


# ------------------------------------------------------------
# Remove friend (with confirm)
# ------------------------------------------------------------
@router.callback_query(F.data.startswith("friend_remove_list:"))
async def friend_remove_list(callback: CallbackQuery):
    """Список текущих друзей с inline-кнопками для удаления."""
    await callback.answer()
    user_id = callback.from_user.id
    friends = await friend_repo.get_friends(user_id)
    if not friends:
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data=f"friends_back:{user_id}")
        try:
            await callback.message.edit_text(
                "У тебя пока нет друзей, которых можно удалить.",
                reply_markup=kb.as_markup(),
            )
        except Exception as e:
            logger.warning("friends.remove_empty_render user=%s err=%s", user_id, e)
        return

    kb = InlineKeyboardBuilder()
    for fid in friends:
        kb.button(text=f"➖ id={fid}", callback_data=f"friend_remove_confirm:{fid}")
    kb.button(text="◀️ Назад", callback_data=f"friends_back:{user_id}")
    kb.adjust(1)
    try:
        await callback.message.edit_text(
            "Выбери друга для удаления:",
            reply_markup=kb.as_markup(),
        )
    except Exception as e:
        logger.warning("friends.remove_list_render user=%s err=%s", user_id, e)


@router.callback_query(F.data.startswith("friend_remove_confirm:"))
async def friend_remove_confirm(callback: CallbackQuery):
    """Confirm-диалог перед удалением."""
    await callback.answer()
    me = callback.from_user.id
    try:
        target = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да, удалить", callback_data=f"friend_remove_do:{target}")
    kb.button(text="◀️ Отмена", callback_data=f"friends_back:{me}")
    kb.adjust(1)
    try:
        await callback.message.edit_text(
            f"Удалить пользователя <code>{target}</code> из друзей?",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
    except Exception as e:
        logger.warning("friends.remove_confirm_render user=%s err=%s", me, e)


@router.callback_query(F.data.startswith("friend_remove_do:"))
async def friend_remove_do(callback: CallbackQuery):
    """Реально удаляет дружбу + возврат в friends-tab."""
    await callback.answer()
    me = callback.from_user.id
    try:
        target = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        return
    await friend_repo.remove_friend(me, target)
    await event_repo.log(
        me,
        "friend_removed",
        {"other_user_id": target},
    )
    text = await leaderboard_service.render_friends_tab(me)
    pending = await friend_repo.get_pending_received(me)
    try:
        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=_friends_menu_keyboard(me, len(pending)),
        )
    except Exception as e:
        logger.warning("friends.remove_do_render user=%s err=%s", me, e)


@router.message(Command("help"))
async def cmd_help(message: Message):
    """Админская справка по командам. Обычным пользователям советует открыть FAQ."""
    if not is_admin(message.from_user.id):
        await message.answer(
            "ℹ️ Эта команда — для админов.\n"
            "Если у тебя вопрос по боту, открой кнопку «❓ FAQ» или напиши его прямо здесь — мы прочитаем."
        )
        return
    is_main = message.from_user.id == MAIN_ADMIN_ID
    text = (
        "🛠 <b>Админские команды</b>\n"
        "\n"
        "<b>Общение с пользователями:</b>\n"
        "/reply &lt;user_id&gt; &lt;текст&gt; — ответ пользователю по ID\n"
        "/broadcast &lt;текст&gt; — рассылка всем зарегистрированным\n"
        "\n"
        "<b>Диагностика:</b>\n"
        "/notif_status — TZ, расписание, попадаешь ли ты в текущую выборку\n"
        "/help — эта справка\n"
        "\n"
        "<b>📊 PA-аналитика</b> (data collection для портфолио):\n"
        "/analytics — 🎯 единый dashboard со всеми разделами (рекомендую)\n"
        "/cohort_stats — D1/D7/D30 retention по неделям регистрации\n"
        "/funnel — activation funnel (% от регистраций)\n"
        "/activation — time-to-value (медиана часов до первых событий)\n"
        "/product_metrics — subject/mode, strict funnel, D7 retention, push, LB\n"
        "/dau — DAU / WAU / MAU (activity_progress + activity_events)\n"
        "/feature_usage — % adoption per feature\n"
        "/segments — user segmentation (power / active / tried / churned / never_started)\n"
        "/content_stats — hardest terms, popular MCQ, EF distribution\n"
        "/event_timeline [hours] — лента событий за последние N часов (default 24)\n"
        "/heatmap [days] — heatmap активности (часы × дни недели, default 30 дней)\n"
        "/export &lt;alias&gt; — CSV-дамп одной таблицы\n"
        "/export all — ZIP всех таблиц + metadata.json (для Jupyter)\n"
        "/parse_logs — bot.log + rotated → events CSV (historical backfill)\n"
    )
    if is_main:
        text += (
            "\n"
            "👑 <b>Только главный админ:</b>\n"
            "/addadmin &lt;user_id&gt; — добавить нового админа\n"
            "/rmadmin &lt;user_id&gt; — удалить админа\n"
            "/listadmins — список всех админов\n"
            "/backup — принудительный snapshot БД (daily backup автоматически после стриков)\n"
        )
    text += (
        "\n"
        "📚 <b>Общие команды</b> (доступны всем):\n"
        "/start — регистрация / возврат в главное меню\n"
        "/stop — остановить активный таймер досрочно\n"
        "/cancel — отменить ввод (например, времени напоминания)\n"
        "/skip — пропустить шаг в мастере настройки уведомлений\n\n"
        "ℹ️ Кнопки главного меню: 📖 Подготовка (отдельная строка), 📚 Учебные инструменты, "
        "📊 Мой профиль, ❓ FAQ, 📢 Новости"
    )
    await message.answer(text, parse_mode="HTML")


@router.message(kb_in("kb.study", "kb.back_study"))
async def handle_back_to_study(message: Message, state: FSMContext):
    await _clear_custom_timer_duration_wait(state)
    user_id = message.from_user.id
    locale = await loc(user_id)
    await message.answer(t("nav.study_section", locale), reply_markup=get_study_keyboard(locale))

@router.message(kb_in("kb.back_main"))
async def handle_back_to_main(message: Message, state: FSMContext):
    await _clear_custom_timer_duration_wait(state)
    # Не срабатывает во время TimerStates.active — отдельный handler выше.
    user_id = message.from_user.id
    await _preserve_pending_timer_across_clear(user_id, state)
    locale = await loc(user_id)
    await message.answer(t("nav.main_menu", locale), reply_markup=get_main_keyboard(locale))


@router.message(TimerStates.waiting_for_duration, Command("cancel"))
async def cancel_custom_timer_duration(message: Message, state: FSMContext):
    user_id = message.from_user.id
    locale = await loc(user_id)
    await state.clear()
    await message.answer(
        t("common.cancelled", locale),
        reply_markup=get_study_keyboard(locale),
    )


@router.message(TimerStates.waiting_for_duration)
async def process_duration(message: Message, state: FSMContext):
    user_id = message.from_user.id
    locale = await loc(user_id)
    duration, error_key = _parse_custom_timer_duration(message.text)
    if error_key == "invalid":
        await message.answer(t("timer.custom_invalid", locale))
        return
    if error_key == "range":
        await message.answer(t("timer.custom_range", locale))
        return
    if not await user_repo.user_exists(user_id):
        await user_repo.create_user(
            user_id, username=message.from_user.username
        )
        await apply_user_bot_commands(user_id)
    if await _desktop_timer_blocks_start(message, locale):
        await state.clear()
        return
    await state.set_state(TimerStates.active)
    await state.update_data(duration=duration, start_time=datetime.now())
    await message.answer(
        t("timer.started", locale, duration=duration),
        reply_markup=get_timer_active_keyboard(locale),
    )
    await event_repo.log(user_id, "session_started", {"duration": duration, "kind": "custom"})
    start_timer(message.chat.id, state, user_id, duration)


@router.message()
async def handle_any_message(message: Message):
    user_id = message.from_user.id
    if is_admin(user_id):
        await message.answer("Используйте /reply для ответа пользователям.")
        return
    locale = await loc(user_id)
    if admin_message_limiter.check(user_id) == "block":
        logger.info("admin_message.ratelimited user_id=%s", user_id)
        await message.answer(
            t("support.rate_limited", locale),
            reply_markup=get_main_keyboard(locale),
        )
        return
    user_name = f"{message.from_user.first_name} {message.from_user.last_name or ''}".strip()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # Append-only JSONL: одна запись на строку.
    # Без read-modify-write — нет гонок, нет риска порчи при kill -9,
    # нет O(n) перезаписи на каждом сообщении.
    text = message.text or message.caption
    if not text:
        text = f"[{message.content_type}]"
    else:
        text = truncate_text(text, max_len=SUPPORT_MESSAGE_MAX_LEN)
    log_entry = {
        "timestamp": timestamp,
        "user_id": user_id,
        "user_name": user_name,
        "message_id": message.message_id,
        "text": text,
    }
    try:
        with open(MESSAGES_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.error(f"Не удалось записать сообщение пользователя в лог: {e}")
    for admin_id in ADMINS:
        try:
            admin_prefix = f"📩 Новое сообщение от {user_name} (ID: {user_id}):\n"
            admin_body = truncate_for_telegram_message(admin_prefix, text)
            await bot.send_message(admin_id, f"{admin_prefix}{admin_body}")
        except Exception:
            pass
    await message.answer(
        t("support.message_sent", locale),
        reply_markup=get_main_keyboard(locale),
    )

# ------------------------------------------------------------
# Восстановление после перезапуска
# ------------------------------------------------------------
async def reconcile_stale_timers():
    """
    После рестарта бота asyncio-задачи таймеров потеряны, но FSM-состояние
    TimerStates.active осталось в БД (благодаря persistent SQLiteStorage).
    Проходим по всем активным записям и решаем по elapsed/duration:
      • elapsed >= duration → авто-завершение: начисляем монеты, уведомляем,
        очищаем FSM-запись.
      • elapsed <  duration → ВОЗОБНОВЛЯЕМ задачу: реконструируем FSMContext
        из той же storage, пересоздаём asyncio-таск через start_timer().
        run_timer_task сам прочитает start_time и поспит ровно столько,
        сколько осталось до deadline. FSM-запись НЕ удаляется.
      • broken/malformed → чистим.
    Также сбрасываем незавершённый ввод custom duration (waiting_for_duration).
    """
    from fsm_storage import _loads  # локальный импорт чтобы не загромождать вершину

    async with db.execute(
        "SELECT key FROM fsm_storage WHERE state = ?",
        (TimerStates.waiting_for_duration.state,),
    ) as cursor:
        waiting_rows = await cursor.fetchall()
    for row in waiting_rows:
        await db.execute("DELETE FROM fsm_storage WHERE key = ?", (row["key"],))
    if waiting_rows:
        await db.commit()
        logger.info("reconcile.cleared_waiting duration_wizards=%s", len(waiting_rows))

    async with db.execute(
        "SELECT key, data FROM fsm_storage WHERE state = ?",
        (TimerStates.active.state,),
    ) as cursor:
        rows = await cursor.fetchall()

    completed = 0
    resumed = 0
    broken = 0

    for row in rows:
        key = row["key"]
        try:
            data = _loads(row["data"])
            start_time = data.get("start_time")
            duration = _normalize_timer_duration(data.get("duration", 25))
            if not isinstance(start_time, datetime) or duration is None:
                logger.warning(
                    "fsm.broken_state key=%s reason=%s",
                    key,
                    "no_start_time" if not isinstance(start_time, datetime) else "bad_duration",
                )
                broken += 1
                await db.execute("DELETE FROM fsm_storage WHERE key = ?", (key,))
                await db.commit()
                continue

            parts = key.split(":")
            if len(parts) < 3:
                logger.warning("fsm.broken_state key=%s reason=malformed_key", key)
                broken += 1
                await db.execute("DELETE FROM fsm_storage WHERE key = ?", (key,))
                await db.commit()
                continue
            bot_id = int(parts[0])
            chat_id = int(parts[1])
            user_id = int(parts[2])
            thread_id = int(parts[3]) if len(parts) > 3 else 0

            elapsed = max(0, (datetime.now() - start_time).total_seconds() / 60)
            if elapsed >= duration:
                # Таймер должен был завершиться, пока бот был офлайн — начисляем.
                # Запрос на оценку здесь НЕ отправляем: пользователь не был активен.
                earned, bonus, _session_id = await study_service.complete_session(user_id, duration)
                completed += 1
                logger.info(
                    "session.complete user_id=%s duration=%s coins=%s bonus=%s session_id=%s achievements=%s source=reconcile",
                    user_id, duration, duration, bonus, _session_id, len(earned),
                )
                await event_repo.log(user_id, "session_completed", {
                    "duration": duration, "coins": duration, "bonus_coins": bonus,
                    "session_id": _session_id, "achievements_earned": len(earned),
                    "source": "reconcile",
                })
                for ach_id in earned:
                    await event_repo.log(user_id, "achievement_unlocked", {"achievement_id": ach_id})
                try:
                    user = await user_repo.get_user(user_id)
                    locale = await loc(user_id)
                    msg = t("timer.reconcile_finished", locale, duration=duration)
                    if bonus > 0:
                        msg += t("timer.bonus", locale, bonus=bonus)
                    msg += t("timer.total_coins", locale, total_coins=user["total_coins"])
                    await _send_with_retry_after(
                        lambda chat_id=chat_id, msg=msg: bot.send_message(
                            chat_id, msg, reply_markup=get_main_keyboard(locale),
                        ),
                        label="reconcile_finished", uid=user_id,
                    )
                    if earned:
                        await send_achievement_notification(user_id, earned)
                except TelegramForbiddenError:
                    logger.info("reconcile.notify_failed user_id=%s reason=blocked", user_id)
                except Exception as e:
                    logger.error(
                        "reconcile.notify_failed user_id=%s reason=%s detail=%s",
                        user_id, type(e).__name__, e,
                    )
                # Завершённый таймер — чистим FSM-запись.
                await db.execute("DELETE FROM fsm_storage WHERE key = ?", (key,))
                await db.commit()
            else:
                # Возобновляем задачу. FSM-state и data остаются нетронутыми —
                # run_timer_task прочитает оригинальный start_time из state.data
                # и поспит ровно (duration - elapsed) минут.
                fsm_key = StorageKey(
                    bot_id=bot_id,
                    chat_id=chat_id,
                    user_id=user_id,
                    thread_id=thread_id,
                )
                state = FSMContext(storage=dp.storage, key=fsm_key)
                start_timer(chat_id, state, user_id, duration)
                resumed += 1
                remaining_min = max(0, round(duration - elapsed))
                logger.info(
                    "reconcile.resume user_id=%s duration=%s elapsed=%.2f remaining=%s",
                    user_id, duration, elapsed, remaining_min,
                )
                try:
                    resume_locale = await loc(user_id)
                    await _send_with_retry_after(
                        lambda chat_id=chat_id, resume_locale=resume_locale, remaining_min=remaining_min: bot.send_message(
                            chat_id,
                            t("timer.reconcile_resumed", resume_locale, remaining=remaining_min),
                            reply_markup=get_timer_active_keyboard(resume_locale),
                        ),
                        label="reconcile_resumed", uid=user_id,
                    )
                except TelegramForbiddenError:
                    logger.info("reconcile.notify_failed user_id=%s reason=blocked", user_id)
                except Exception as e:
                    logger.warning(
                        "reconcile.notify_failed user_id=%s reason=%s detail=%s",
                        user_id, type(e).__name__, e,
                    )
        except Exception as e:
            logger.error(f"reconcile_stale_timers: запись {key!r} не обработана: {e}")

    logger.info(
        "reconcile.summary completed=%s resumed=%s broken=%s total=%s",
        completed, resumed, broken, len(rows),
    )


# ------------------------------------------------------------
# Запуск приложения
# ------------------------------------------------------------
async def main():
    global db, user_repo, session_repo, admin_repo, flashcard_repo, user_flashcard_repo, user_task_repo, mcq_repo, task_repo, subject_stats_repo, event_repo, plan_repo, tips_repo, pet_repo, leaderboard_repo, friend_repo, device_repo, desktop_timer_repo, ach_service, study_service, streak_service, backup_service, analytics_service, leaderboard_service, rate_limiter, bot, dp, bot_username
    db = await get_db()
    await init_db(db)
    user_repo = UserRepository(db)
    session_repo = SessionRepository(db)
    admin_repo = AdminRepository(db)
    flashcard_repo = FlashcardRepository(db)
    user_flashcard_repo = UserFlashcardRepository(db)
    user_task_repo = UserTaskRepository(db)
    mcq_repo = McqProgressRepository(db)
    task_repo = TaskProgressRepository(db)
    subject_stats_repo = SubjectStatsRepository(db)
    event_repo = EventRepository(db)
    plan_repo = PlanRepository(db)
    tips_repo = TipsRepository(db)
    pet_repo = PetRepository(db)
    leaderboard_repo = LeaderboardRepository(db)
    friend_repo = FriendRepository(db)
    device_repo = DeviceRepository(db)
    desktop_timer_repo = DesktopTimerRepository(db)
    ach_service = AchievementService(user_repo, ACHIEVEMENTS)
    session = AiohttpSession(timeout=TELEGRAM_TIMEOUT)
    bot = Bot(token=BOT_TOKEN, session=session)
    study_service = StudyService(
        user_repo, session_repo, ach_service,
        pet_repo, leaderboard_repo, bot=bot,
    )
    leaderboard_service = LeaderboardService(user_repo, leaderboard_repo, friend_repo=friend_repo)
    # bot создан выше для передачи в StudyService (level-up notifications).
    # streak_service ниже также получит leaderboard_repo для consume_freeze_if_active.
    dp = Dispatcher(storage=SQLiteStorage(db))
    # Rate-limit middleware: тротлим не-админских пользователей
    # ≥ 30 actions / 60 секунд (warn на 70%, hard block на 100%).
    # Регистрируем ДО include_router, чтобы middleware применялся
    # ко всем хендлерам в роутере.
    rate_limiter = UserRateLimiter(max_actions=30, window_seconds=60)
    rl_middleware = RateLimitMiddleware(rate_limiter, locale_fn=loc)
    # Username sync — обновляем users.username из event_from_user.username
    # перед всеми handler'ами. Регистрируем ДО rl_middleware, потому что
    # rate-limit может silently drop event (return None), а username хотим
    # обновить ВСЕГДА, пока юзер активен.
    username_sync = UsernameSyncMiddleware(user_repo)
    dp.message.middleware(username_sync)
    dp.callback_query.middleware(username_sync)
    dp.message.middleware(rl_middleware)
    dp.callback_query.middleware(rl_middleware)
    if PLAN_UI_ENABLED:
        register_plan_handlers(
            router,
            plan_repo=plan_repo,
            loc_fn=loc,
            bot_instance=bot,
            bot_module=sys.modules[__name__],
        )
    dp.include_router(router)
    streak_service = StreakService(user_repo, bot, leaderboard_repo=leaderboard_repo)
    reminder_service = ReminderService(
        user_repo, bot,
        morning_tip_builder=build_morning_tip_block,
        event_repo=event_repo,
    )
    # Backup сервис: snapshot БД раз в сутки после streak processing.
    # BACKUP_DIR/BACKUP_RETENTION_DAYS можно переопределить в .env;
    # в Docker/bothost — на mounted /app/data (см. docker-compose / Dockerfile).
    backup_service = BackupService(
        db_path=DB_PATH,
        backup_dir=BACKUP_DIR,
        retention_days=int(os.getenv("BACKUP_RETENTION_DAYS", "30")),
    )
    analytics_service = AnalyticsService(db)

    # Один раз: миграция admins.json → таблица admins. Файл переименовывается
    # в admins.json.migrated. Повторные запуски — no-op.
    await _migrate_admins_json_to_db()

    # Гарантируем что главный админ всегда в БД (даже если admins.json не было
    # или удалена строка). MAIN_ADMIN_ID из .env — единственный источник истины
    # для роли «главного».
    if MAIN_ADMIN_ID:
        await admin_repo.add(MAIN_ADMIN_ID)

    # Загружаем in-memory кеш из БД. is_admin() работает по нему синхронно.
    ADMINS.clear()
    ADMINS.update(await admin_repo.get_all_ids())

    # Обработка таймеров, оставшихся в FSM после прошлого запуска:
    # • если истекли — авто-завершение
    # • если в процессе — ВОЗОБНОВЛЕНИЕ задачи с правильным остатком
    await reconcile_stale_timers()

    # Регистрация команд в Telegram /-пикере. Дефолтный scope — для всех,
    # расширенный — отдельно для каждого админа через BotCommandScopeChat,
    # чтобы обычные пользователи не видели админские команды в подсказках.
    await bot.set_my_commands(DEFAULT_COMMANDS, scope=BotCommandScopeDefault())
    for admin_id in ADMINS:
        try:
            await bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception as e:
            logger.warning(
                "set_my_commands.failed admin_id=%s reason=%s detail=%s",
                admin_id, type(e).__name__, e,
            )

    # Держим строгие ссылки на задачи, иначе их может собрать GC.
    # name + add_done_callback нужны, чтобы исключение, пробившее внутренний
    # while-True try/except в schedulers'е (или поднявшееся ДО входа в цикл),
    # сразу засветилось в bot.log через _log_task_exception, а не молча
    # повисло до GC задачи.
    background_tasks = []
    for coro, task_name in (
        (streak_scheduler(streak_service, user_repo, backup_service), "streak_scheduler"),
        (reminder_scheduler(reminder_service, user_repo), "reminder_scheduler"),
        # Weekly leaderboard rollover (UTC Tuesday 00:00 anchor). См. LEADERBOARD.md §Rewards.
        (leaderboard_scheduler(leaderboard_service), "leaderboard_scheduler"),
    ):
        bg = asyncio.create_task(coro, name=task_name)
        bg.add_done_callback(_log_task_exception)
        background_tasks.append(bg)
    logger.info(
        "app.start admins=%s main_admin_id=%s server_tz=%s log_level=%s",
        len(ADMINS), MAIN_ADMIN_ID, SERVER_TIMEZONE,
        os.getenv("LOG_LEVEL", "INFO").upper(),
    )
    # Кеш @username бота для построения t.me/<name>?start=friend_<token>
    # deep-link'ов. get_me() — один HTTP round-trip за весь lifetime бота.
    try:
        me = await bot.get_me()
        bot_username = me.username
        logger.info("app.bot_username @%s", bot_username)
    except Exception as e:
        bot_username = None
        logger.warning(
            "app.bot_username_fetch_failed reason=%s — invite-links disabled",
            type(e).__name__,
        )

    # HTTP API для desktop-клиента (см. api.py). Поднимается в этом же
    # event loop, чтобы делить с ботом одно соединение с SQLite; выключен,
    # пока в окружении нет API_ENABLED=1.
    api_runner = None
    if api_enabled():
        try:
            api_runner = await start_api_server(create_api_app(
                user_repo=user_repo,
                session_repo=session_repo,
                pet_repo=pet_repo,
                device_repo=device_repo,
                timer_repo=desktop_timer_repo,
                ach_service=ach_service,
                study_service=study_service,
                achievements=ACHIEVEMENTS,
                event_repo=event_repo,
            ))
        except Exception as e:
            # Занятый порт не должен ронять бота: Telegram-часть важнее,
            # приложение переживёт отсутствие API до следующего рестарта.
            logger.error(
                "api.start_failed reason=%s detail=%s — бот работает без API",
                type(e).__name__, e,
            )

    logger.info("✅ Palph запущен")
    try:
        await dp.start_polling(bot)
    finally:
        logger.info("app.shutdown")
        if api_runner is not None:
            try:
                await api_runner.cleanup()
            except Exception as e:
                logger.warning(
                    "api.shutdown_failed reason=%s detail=%s",
                    type(e).__name__, e,
                )
        for t in background_tasks:
            t.cancel()
        try:
            await bot.session.close()
        except Exception as e:
            logger.warning(
                "app.session_close_failed reason=%s detail=%s",
                type(e).__name__, e,
            )

if __name__ == "__main__":
    asyncio.run(main())