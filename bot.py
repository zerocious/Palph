# bot.py
import asyncio
import json
import logging
import os
import re
import random
import hashlib
import sqlite3
from html import escape as html_escape
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytz
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, Router, F, BaseMiddleware
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
    BotCommand, BotCommandScopeDefault, BotCommandScopeChat,
    FSInputFile, BufferedInputFile,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from fsm_storage import SQLiteStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import StorageKey

from db import get_db, init_db
from repository import (
    UserRepository, SessionRepository, AdminRepository, FlashcardRepository,
    UserFlashcardRepository, TipsRepository,
    McqProgressRepository, TaskProgressRepository, SubjectStatsRepository,
    EventRepository, PetRepository, LeaderboardRepository, FriendRepository,
)
from services import (
    AchievementService, StudyService, StreakService, ReminderService,
    BackupService, AnalyticsService, LeaderboardService, UserRateLimiter, sm2_update,
    freeze_cost, parse_friend_query, derive_emotion, render_pet,
)
from tasks import streak_scheduler, reminder_scheduler, leaderboard_scheduler

# ------------------------------------------------------------
# Настройки окружения
# ------------------------------------------------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
MAIN_ADMIN_ID = int(os.getenv("MAIN_ADMIN_ID", "0"))
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

    # LOG_FILE можно переопределить в env — в Docker мы пишем в `/data/bot.log`
    # (на mounted volume), локально по умолчанию остаётся `./bot.log`.
    log_file = os.getenv("LOG_FILE", "bot.log")
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

# ------------------------------------------------------------
# Загрузка достижений
# ------------------------------------------------------------
ACHIEVEMENTS_FILE = Path(__file__).parent / "achievements.json"
with open(ACHIEVEMENTS_FILE, "r", encoding="utf-8") as f:
    ACHIEVEMENTS = json.load(f)

# ------------------------------------------------------------
# Глобальные объекты (заполняются в main)
# ------------------------------------------------------------
db = None
user_repo: UserRepository = None
session_repo: SessionRepository = None
admin_repo: AdminRepository = None
flashcard_repo: FlashcardRepository = None
user_flashcard_repo: UserFlashcardRepository = None
mcq_repo: McqProgressRepository = None
task_repo: TaskProgressRepository = None
subject_stats_repo: SubjectStatsRepository = None
event_repo: EventRepository = None
tips_repo: TipsRepository = None
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

# Rate limiter — защита от спама/abuse'а на уровне приложения.
# Initialize in main(); attached to dispatcher как middleware.
rate_limiter: UserRateLimiter = None


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

    def __init__(self, limiter: UserRateLimiter):
        self.limiter = limiter
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
            return None  # silently drop, handler не вызывается

        if status == "warn":
            logger.info("ratelimit.warned user_id=%s", user.id)
            try:
                if isinstance(event, Message):
                    await event.answer(
                        "⏸ Слишком быстро! Подожди немного и продолжи."
                    )
                elif isinstance(event, CallbackQuery):
                    await event.answer(
                        "⏸ Слишком быстро — подожди немного.",
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


TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]?\d)$")


# Пресеты часовых поясов (IANA-имя, человекочитаемая метка).
# Покрывают Россию от Калининграда до Камчатки + ключевые столицы СНГ.
TZ_PRESETS: list[tuple[str, str]] = [
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


def build_rating_keyboard(session_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for score, emoji in RATING_EMOJIS:
        kb.button(text=emoji, callback_data=f"rate:{session_id}:{score}")
    kb.button(text="⏭ Пропустить", callback_data=f"rate_skip:{session_id}")
    kb.adjust(4, 1)
    return kb.as_markup()


async def send_rating_prompt(chat_id: int, session_id: int) -> None:
    """Отправляет пользователю запрос на оценку только что завершённой сессии."""
    try:
        await bot.send_message(
            chat_id,
            "Как прошла сессия?",
            reply_markup=build_rating_keyboard(session_id),
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
DEFAULT_COMMANDS = [
    BotCommand(command="start", description="Запуск бота / в главное меню"),
    BotCommand(command="stop", description="Остановить активный таймер"),
    BotCommand(command="progress", description="Прогресс по предметам"),
    BotCommand(command="pet", description="Питомец и кастомизация"),
    BotCommand(command="leaderboard", description="Недельный лидерборд"),
    BotCommand(command="friends", description="Друзья и рейтинг"),
]
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

def get_main_keyboard() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.button(text="📚 Учеба")
    builder.button(text="❓ FAQ")
    builder.button(text="📊 Мой профиль")
    builder.button(text="📢 Новости")
    builder.adjust(2, 2)
    return builder.as_markup(resize_keyboard=True)

def get_study_keyboard() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.button(text="⏱️ Стандартный таймер (25 мин)")
    builder.button(text="⏱️ Кастомный таймер")
    builder.button(text="❓ Квизы")
    builder.button(text="🎓 Советы для продуктивности")
    builder.button(text="🏠 Назад в меню")
    builder.adjust(3)
    return builder.as_markup(resize_keyboard=True)

def get_tips_keyboard() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.button(text="⏰ Тайм-менеджмент")
    builder.button(text="🧠 Техники запоминания")
    builder.button(text="🎯 Как пользоваться ботом")
    builder.button(text="🔗 Ссылки на статьи и книги")
    builder.button(text="⬅️ Назад к учебе")
    builder.adjust(2)
    return builder.as_markup(resize_keyboard=True)

def get_timer_active_keyboard() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.button(text="⏹️ Остановить")
    builder.button(text="⬅️ Назад в меню")
    builder.adjust(2)
    return builder.as_markup(resize_keyboard=True)

def get_mode_keyboard() -> ReplyKeyboardMarkup:
    """Legacy helper — предпочитайте get_mode_keyboard_for_subject()."""
    builder = ReplyKeyboardBuilder()
    for _, label in STUDY_MODES:
        builder.button(text=label)
    builder.button(text="⬅️ Назад к предметам")
    builder.adjust(1)
    return builder.as_markup(resize_keyboard=True)


async def get_subject_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    """Subject-picker: предметы с хотя бы одним доступным режимом."""
    builder = ReplyKeyboardBuilder()
    for _, label in await available_subjects(user_id):
        builder.button(text=label)
    builder.button(text="⬅️ Назад к учебе")
    builder.adjust(1)
    return builder.as_markup(resize_keyboard=True)


async def get_mode_keyboard_for_subject(subject_id: str, user_id: int) -> ReplyKeyboardMarkup:
    """Mode-picker для выбранного предмета."""
    builder = ReplyKeyboardBuilder()
    for _, label in await available_modes(subject_id, user_id):
        builder.button(text=label)
    builder.button(text="⬅️ Назад к предметам")
    builder.adjust(1)
    return builder.as_markup(resize_keyboard=True)


def get_subject_keyboard_for_mode(mode_id: str) -> ReplyKeyboardMarkup:
    """Legacy helper — предпочитайте get_subject_keyboard(user_id)."""
    builder = ReplyKeyboardBuilder()
    for _, label in subjects_with_mode(mode_id):
        builder.button(text=label)
    builder.button(text="⬅️ Назад к режимам")
    builder.adjust(1)
    return builder.as_markup(resize_keyboard=True)


def get_mcq_active_keyboard() -> ReplyKeyboardMarkup:
    """Активная MCQ-сессия: только кнопка выхода (выбор вариантов — inline)."""
    builder = ReplyKeyboardBuilder()
    builder.button(text="🛑 Завершить")
    builder.adjust(1)
    return builder.as_markup(resize_keyboard=True)


def get_task_active_keyboard() -> ReplyKeyboardMarkup:
    """Активная photo-task сессия: только кнопка выхода (ответ — текстом)."""
    builder = ReplyKeyboardBuilder()
    builder.button(text="🛑 Завершить")
    builder.adjust(1)
    return builder.as_markup(resize_keyboard=True)


def get_flash_active_keyboard() -> ReplyKeyboardMarkup:
    """Активная сессия флэш-карт: только выход (рейтинг — inline)."""
    builder = ReplyKeyboardBuilder()
    builder.button(text="🛑 Завершить")
    builder.adjust(1)
    return builder.as_markup(resize_keyboard=True)


QUIZ_SECTIONS = [
    ("Раздел I", "i"),
    ("Раздел II", "ii"),
    ("Раздел III", "iii"),
    ("Раздел IV", "iv"),
]


def available_quiz_sections(subject_id: str = "industrial-management") -> list[tuple[str, str]]:
    """
    Возвращает только те разделы ситуационных квизов, файлы которых
    существуют и не пусты. `subject_id` — id из SUBJECTS (имя папки).
    """
    section_dir = STUDY_MATERIALS_PATH / subject_id / "situational"
    available = []
    for label, key in QUIZ_SECTIONS:
        file_path = section_dir / f"section-{key}.txt"
        if file_path.exists() and file_path.stat().st_size > 0:
            available.append((label, key))
    return available


def get_quiz_section_keyboard() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    for label, _ in available_quiz_sections():
        builder.button(text=label)
    builder.button(text="🛑 Завершить квиз")
    builder.adjust(2)
    return builder.as_markup(resize_keyboard=True)

def get_quiz_answer_keyboard() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.button(text="🛑 Завершить квиз")
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

TIP_CATEGORIES: dict[str, dict] = {
    "tm": {
        "tips": TIME_MANAGEMENT_TIPS,
        "emoji": "⏰",
        "title": "Тайм-менеджмент",
    },
    "mem": {
        "tips": MEMORY_RETENTION_TIPS,
        "emoji": "🧠",
        "title": "Техники запоминания",
    },
    "bot": {
        "tips": BOT_GUIDE_TIPS,
        "emoji": "🎯",
        "title": "Как пользоваться ботом",
    },
}
TIP_COIN_PER_DAY = 1
TIPS_SEEN_COOLDOWN_DAYS = 7

# Каталог предметов: (id, label). id = имя папки в study_materials/.
SUBJECTS: list[tuple[str, str]] = [
    ("industrial-management", "🏭 Основы производственного менеджмента"),
    ("math",                  "🧮 Математика"),
    ("english",               "🇬🇧 Английский"),
]

# Каталог режимов учёбы. id определяет где лежит контент:
#   situational → subject/situational/section-*.txt (multi-section)
#   flashcards / mcq → subject/<mode>.txt (один файл)
#   tasks → subject/tasks/task-*.json + task-*.png
STUDY_MODES: list[tuple[str, str]] = [
    ("situational", "🎯 Ситуационные квизы"),
    ("flashcards",  "🃏 Флэш-карты"),
    ("mcq",         "❓ Тест с выбором ответа"),
    ("tasks",       "📷 Задачи с картинкой"),
]


def _file_based_modes(subject_id: str) -> list[tuple[str, str]]:
    """Режимы с непустым официальным контентом (файлы на диске)."""
    subject_path = STUDY_MATERIALS_PATH / subject_id
    if not subject_path.is_dir():
        return []
    result = []
    for mode_id, label in STUDY_MODES:
        if mode_id == "situational":
            section_dir = subject_path / "situational"
            if section_dir.is_dir() and any(
                p.stat().st_size > 0 for p in section_dir.glob("section-*.txt")
            ):
                result.append((mode_id, label))
        elif mode_id == "tasks":
            tasks_dir = subject_path / "tasks"
            if tasks_dir.is_dir() and any(tasks_dir.glob("task-*.json")):
                result.append((mode_id, label))
        else:
            file_path = subject_path / f"{mode_id}.txt"
            if file_path.exists() and file_path.stat().st_size > 0:
                result.append((mode_id, label))
    return result


async def available_modes(subject_id: str, user_id: int | None = None) -> list[tuple[str, str]]:
    """
    Режимы с контентом для предмета. Учитывает пользовательские флэш-карты:
    flashcards доступен, если есть flashcards.txt или свои карточки.
    """
    result = _file_based_modes(subject_id)
    if user_id is not None:
        user_count = await user_flashcard_repo.count_by_subject(user_id, subject_id)
        if user_count > 0 and not any(m[0] == "flashcards" for m in result):
            flash_label = next(label for mid, label in STUDY_MODES if mid == "flashcards")
            result.append(("flashcards", flash_label))
    return result


async def available_subjects(user_id: int) -> list[tuple[str, str]]:
    """Предметы, у которых есть хотя бы один доступный режим."""
    result = []
    for sid, label in SUBJECTS:
        if await available_modes(sid, user_id):
            result.append((sid, label))
    return result


def subjects_with_mode(mode_id: str) -> list[tuple[str, str]]:
    """Legacy sync helper — только официальный контент на диске."""
    return [
        (sid, label)
        for sid, label in SUBJECTS
        if any(m[0] == mode_id for m in _file_based_modes(sid))
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
    file_path = STUDY_MATERIALS_PATH / subject_id / "situational" / f"section-{section.lower()}.txt"
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
    file_path = STUDY_MATERIALS_PATH / subject_id / "mcq.txt"
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
                questions.append({
                    "question": parts[0],
                    "correct":  parts[1],
                    "wrongs":   parts[2:5],
                })
    return questions


def load_tasks(subject_id: str) -> list[dict]:
    """
    Читает study_materials/<subject>/tasks/task-*.json и возвращает список задач.
    Каждая задача — dict с полями:
      - 'id': str (из имени файла, напр. 'task-01')
      - 'problem': str (текстовая подпись к картинке, может быть пустой)
      - 'accepted': list[str] (принимаемые ответы — без нормализации,
        нормализация делается в _normalize_task_answer перед сравнением)
      - 'solution_filename': str (имя файла solution-картинки в той же папке;
        дефолт — '{id}-solution.png')
    Задачи без существующего task-NN.png или с пустым accepted — пропускаются
    с warning'ом в лог. Возвращает задачи отсортированные по id.
    """
    tasks_dir = STUDY_MATERIALS_PATH / subject_id / "tasks"
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
        image_path = tasks_dir / f"{task_id}.png"
        if not image_path.exists():
            logger.warning(f"task.missing_image task_id={task_id} expected={image_path.name}")
            continue
        accepted = data.get("accepted", [])
        if not isinstance(accepted, list) or not accepted:
            logger.warning(f"task.no_accepted task_id={task_id}")
            continue
        solution_filename = data.get("solution_image", f"{task_id}-solution.png")
        tasks.append({
            "id": task_id,
            "problem": str(data.get("problem", "")),
            "accepted": [str(a) for a in accepted],
            "solution_filename": str(solution_filename),
        })
    return tasks


def _normalize_task_answer(text: str) -> str:
    """Нормализует ответ для сравнения: lowercase, убрать пунктуацию, сжать пробелы."""
    no_punct = re.sub(r"[^\w\s]", " ", text.lower())
    return re.sub(r"\s+", " ", no_punct).strip()


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
    file_path = STUDY_MATERIALS_PATH / subject_id / "flashcards.txt"
    if not file_path.exists():
        return []
    cards = []
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split("||")]
            if len(parts) == 2:
                term, definition = parts
                cards.append({
                    "term": term,
                    "definition": definition,
                    "hash": _flashcard_hash(term),
                })
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
    return official + own


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

    if not await user_repo.user_exists(user_id):
        await user_repo.create_user(
            user_id, username=message.from_user.username
        )
        logger.info("user.registered user_id=%s", user_id)
        await event_repo.log(user_id, "user_registered", {
            "language_code": message.from_user.language_code,
        })
        keyboard = ReplyKeyboardBuilder()
        keyboard.button(text="🔧 Настроить сейчас")
        keyboard.button(text="🚀 Начать сразу")
        keyboard.adjust(1)
        await message.answer(
            "🐾 Привет! Я — Palph, твой цифровой питомец для учёбы!\n\n"
            "✨ Я помогу тебе учиться регулярно и без стресса. "
            "Даже 5 минут в день — это уже победа!\n\n"
            "Хочешь сначала настроить уведомления под себя или начать сразу?",
            reply_markup=keyboard.as_markup(resize_keyboard=True)
        )
        await state.set_state(SetupStates.choosing_path)
    else:
        user = await user_repo.get_user(user_id)
        await message.answer(
            f"😊 С возвращением! Твой питомец скучал!\n\n"
            f"📊 Твоя статистика:\n"
            f"• Всего сессий: {user['total_sessions']}\n"
            f"• Всего монет: {user['total_coins']} 🪙\n"
            f"• Твой стрик: {user['current_streak']} дней подряд 🔥\n\n"
            f"Чем займёмся сегодня?",
            reply_markup=get_main_keyboard()
        )

    # Обработка deep-link invite после стандартного welcome.
    # Существующий FSM-стейт onboarding'а не трогаем — invite — side effect.
    if deep_link_arg and deep_link_arg.startswith("friend_"):
        await _process_friend_invite_link(message, deep_link_arg)


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
        await message.answer(
            "⏳ Ссылка-приглашение недействительна или истекла."
        )
        return

    result = await friend_repo.accept_invite(creator_id, invitee_id)
    if result == "accepted":
        await event_repo.log(
            invitee_id,
            "friend_accepted",
            {"other_user_id": creator_id, "source": "invite_link"},
        )
        await message.answer(
            f"🎉 Ты добавлен в друзья к пользователю "
            f"<code>{creator_id}</code>!",
            parse_mode="HTML",
        )
        try:
            await bot.send_message(
                creator_id,
                f"🎉 Пользователь <code>{invitee_id}</code> присоединился "
                f"к тебе по ссылке-приглашению!",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.info(
                "friends.invite_notify_creator_failed creator=%s reason=%s",
                creator_id, type(e).__name__,
            )
    elif result == "already_friends":
        await message.answer("👥 Вы уже друзья.")
    elif result == "self":
        await message.answer(
            "🙂 Это твоя собственная ссылка — отправь её другим пользователям."
        )

@router.message(SetupStates.choosing_path, F.text == "🚀 Начать сразу")
async def setup_skip(message: Message, state: FSMContext):
    """Пропуск мастера настройки — оставляем дефолтные 09:00 / 21:00."""
    await state.clear()
    await message.answer(
        "👍 Готово! Можешь начинать учиться прямо сейчас.\n"
        "Время напоминаний можно поменять позже в «📊 Мой профиль» → «⚙️ Настройки».",
        reply_markup=get_main_keyboard(),
    )


@router.message(SetupStates.choosing_path, F.text == "🔧 Настроить сейчас")
async def setup_start(message: Message, state: FSMContext):
    """Начало мастера: спрашиваем утреннее время."""
    await state.set_state(SetupStates.setting_morning)
    await message.answer(
        "🌅 Во сколько отправлять утреннее напоминание?\n"
        "Введи время в формате ЧЧ:ММ (например, 09:00).\n"
        "Если не нужно — отправь /skip.",
    )


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
    normalized = _parse_time_or_none(message.text)
    if normalized is None:
        await message.answer(
            "❌ Неверный формат. Введи время как ЧЧ:ММ, например 09:00.\n"
            "Или /skip, если утреннее напоминание не нужно."
        )
        return
    await state.update_data(morning_time=normalized)
    await _ask_evening(message, state)


async def _ask_evening(message: Message, state: FSMContext):
    await state.set_state(SetupStates.setting_evening)
    await message.answer(
        "🌙 А во сколько вечернее напоминание?\n"
        "Введи время в формате ЧЧ:ММ (например, 21:00).\n"
        "Если не нужно — отправь /skip."
    )


@router.message(SetupStates.setting_evening, Command("skip"))
async def setup_skip_evening(message: Message, state: FSMContext):
    await state.update_data(evening_time=None)
    await _finish_setup(message, state)


@router.message(SetupStates.setting_evening)
async def setup_evening(message: Message, state: FSMContext):
    normalized = _parse_time_or_none(message.text)
    if normalized is None:
        await message.answer(
            "❌ Неверный формат. Введи время как ЧЧ:ММ, например 21:00.\n"
            "Или /skip, если вечернее напоминание не нужно."
        )
        return
    await state.update_data(evening_time=normalized)
    await _finish_setup(message, state)


async def _finish_setup(message: Message, state: FSMContext):
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
    summary_lines = ["✅ Настройки сохранены:"]
    summary_lines.append(
        f"🌅 Утро: {morning}" if morning else "🌅 Утро: отключено"
    )
    summary_lines.append(
        f"🌙 Вечер: {evening}" if evening else "🌙 Вечер: отключено"
    )
    summary_lines.append("\nПоменять можно в «📊 Мой профиль» → «⚙️ Настройки».")
    await message.answer("\n".join(summary_lines), reply_markup=get_main_keyboard())


FAQ_MENU_TEXT = "📖 Часто задаваемые вопросы\n\nВыбери вопрос:"

# Контент FAQ — массив словарей. Порядок = порядок кнопок в меню.
# btn: короткий лейбл для inline-кнопки (≤64 символа per Telegram API)
# title: полная формулировка вопроса (показывается в ответе)
# body: текст ответа
FAQ_ITEMS: list[dict[str, str]] = [
    {
        "id": "mission",
        "btn":   "1️⃣ Миссия проекта",
        "title": "1️⃣ Какая миссия у проекта?",
        "body": (
            "Palph создан, чтобы учёба перестала быть «надо» и стала «хочу».\n\n"
            "Мы соединяем геймификацию (монеты, стрики, питомец, ачивки, советы) с "
            "научно проверенными техниками запоминания (интервальное повторение, "
            "SM-2, active recall, Pomodoro). Получается система, которая:\n"
            "• заменяет унылую зубрёжку на серию маленьких побед,\n"
            "• даёт мгновенную обратную связь — главный антидот к прокрастинации,\n"
            "• поддерживает мотивацию через эмоциональную привязку к питомцу,\n"
            "• превращает учёбу в привычку, которая не требует силы воли каждый день.\n\n"
            "Главная цель: ты учишься эффективнее, и тебе это в кайф."
        ),
    },
    {
        "id": "efficiency",
        "btn":   "2️⃣ Эффективность учёбы с ботом",
        "title": "2️⃣ Почему учиться с ботом эффективнее, чем самому?",
        "body": (
            "Бот объединяет несколько научно доказанных техник в один цикл: "
            "метод Помодоро (25-минутные сессии = меньше выгорания), "
            "мгновенная мотивация (монеты, достижения, эмоции питомца), "
            "советы по продуктивности с небольшими наградами, "
            "регулярные напоминания и квизы с интервальным повторением. "
            "Ты получаешь структуру и обратную связь, которые в одиночку легко терять."
        ),
    },
    {
        "id": "pet",
        "btn":   "3️⃣ Зачем питомец",
        "title": "3️⃣ Зачем нужен питомец и как он помогает учиться?",
        "body": (
            "Питомец отражает твою активность: радуется, когда ты учишься, и "
            "грустит, если пропускаешь день. Это эмоциональный якорь — учиться "
            "не «ради дисциплины», а «чтобы твоему питомцу было хорошо». "
            "Связь с виртуальным персонажем доказанно повышает регулярность "
            "привычки."
        ),
    },
    {
        "id": "spend_coins",
        "btn":   "4️⃣ На что тратить монеты",
        "title": "4️⃣ На что можно тратить монеты?",
        "body": (
            "Сейчас монеты копятся как награда за учёбу и помогают получать "
            "достижения. В ближайших обновлениях за монеты можно будет покупать "
            "кастомизацию питомца: цвета (оранжевый, синий, зелёный…) и аксессуары "
            "(шляпа, очки, шарф, корона). В будущем добавим больше — следи за "
            "каналом 📢."
        ),
    },
    {
        "id": "earn_coins",
        "btn":   "5️⃣ Как зарабатывать монеты",
        "title": "5️⃣ Как зарабатывать монеты?",
        "body": (
            "• +1 монета за каждую минуту учёбы через таймер\n"
            "• +15 бонусом, когда стрик достигает 2+ дней подряд\n"
            "• Бонусные монеты за получение достижений (список — в профиле)\n"
            "• +1 монета за каждый правильный MCQ-ответ\n"
            "• +1 монета за каждую просмотренную флэш-карту\n"
            "• До +3 монет за решение задачи с картинкой (зависит от попытки)\n"
            "• +1 монета за первый совет дня в разделе «🎓 Советы для продуктивности» "
            "(тайм-менеджмент, запоминание или «как пользоваться ботом»)\n"
            "• Бонус за достижение «💡 Любознательный» — 10 просмотренных советов (+30 🪙)\n"
            "• В утреннем напоминании — «совет дня» (один на календарный день)"
        ),
    },
    {
        "id": "sm2",
        "btn":   "6️⃣ SM-2 для флэш-карт",
        "title": "6️⃣ Что такое SM-2 и почему это эффективно для флэш-карт?",
        "body": (
            "SM-2 (SuperMemo-2) — алгоритм, который «запоминает» сложность каждой "
            "карточки лично для тебя. С трудом вспомнил термин — карточка вернётся "
            "скоро; легко — через неделю или больше. Ты не тратишь время на то, "
            "что уже знаешь, и часто видишь именно то, что ускользает из памяти. "
            "Используется в Anki и опирается на исследования кривой забывания "
            "Эббингауза."
        ),
    },
    {
        "id": "spaced_rep",
        "btn":   "7️⃣ Интервальное повторение",
        "title": "7️⃣ Что такое интервальное повторение?",
        "body": (
            "Принцип: материал лучше запоминается, если повторять его с растущими "
            "промежутками — например, через 1, 3, 7, 16 дней. Каждое успешное "
            "повторение продлевает интервал; ошибка перезапускает счётчик. "
            "Эббингауз доказал это в 1885 году, и за 140 лет принцип подтверждён "
            "сотнями исследований. Запоминать через дни и недели в разы "
            "эффективнее, чем зубрить за вечер."
        ),
    },
    {
        "id": "active_recall",
        "btn":   "8️⃣ Active recall в боте",
        "title": "8️⃣ Что такое active recall и какие методы есть в боте?",
        "body": (
            "Active recall — это извлечение информации из памяти «из головы», без "
            "подглядывания. Работает в 2–3 раза лучше, чем повторное чтение. В боте "
            "active recall встроен в каждый учебный режим:\n"
            "• Ситуационные квизы — вводишь определение по описанию ситуации\n"
            "• Флэш-карты — видишь термин, вспоминаешь, проверяешь себя\n"
            "• Советы для продуктивности — короткие техники тайм-менеджмента и памяти\n"
            "• Тесты с выбором ответа — выбираешь правильный из 4 вариантов\n"
            "• Задачи с картинкой — решаешь и вводишь ответ\n"
            "Принцип: «если можешь объяснить — значит знаешь»."
        ),
    },
    {
        "id": "guarantee",
        "btn":   "9️⃣ Гарантия эффективности",
        "title": "9️⃣ Гарантируете ли вы результат?",
        "body": (
            "Эффективность очень вероятна, но 100% гарантии нет — и это нормально.\n\n"
            "Все методики бота (интервальное повторение, SM-2, active recall, "
            "Pomodoro, геймификация) опираются на десятилетия исследований: от "
            "Эббингауза (1885) до современных мета-анализов. У пользователей, "
            "которые регулярно учатся через бот, результаты заметно лучше, чем "
            "при пассивном перечитывании.\n\n"
            "Но реальный эффект зависит от:\n"
            "• тебя — регулярности занятий и честности самооценки в флэш-картах\n"
            "• контента — качества и структуры материала\n"
            "• обстоятельств — стресс, сон и самочувствие сильно влияют на память\n"
            "• стартовой точки — у всех разный уровень и темп\n\n"
            "Если коротко: при регулярных занятиях ты с очень высокой вероятностью "
            "улучшишь свои результаты. Но «гарантированно стать гением за месяц» — "
            "не обещаем, это было бы нечестно."
        ),
    },
    {
        "id": "commands",
        "btn":   "🔟 Быстрые команды",
        "title": "🔟 Какие команды есть для быстрой навигации?",
        "body": (
            "Можно писать команды в чат вместо кнопок — так быстрее попасть "
            "в нужный раздел:\n\n"
            "/start — главное меню и клавиатура\n"
            "/stop — остановить активный таймер Pomodoro\n"
            "/progress — прогресс по всем предметам\n"
            "/pet — экран питомца (уровень, XP, кастомизация)\n"
            "/leaderboard — недельный рейтинг (то же, что /leaderboards)\n"
            "/friends — друзья и недельный рейтинг среди них\n\n"
            "Квизы, FAQ, настройки и профиль — через кнопки главного меню "
            "или «📊 Мой профиль».\n\n"
            "Если бот ждёт ввод (переименование питомца, время напоминания) — "
            "отмена: /cancel."
        ),
    },
]

FAQ_SUPPORT_ITEM: dict[str, str] = {
    "id":    "support",
    "btn":   "🛠 Связаться с техподдержкой",
    "title": "🛠 Техподдержка",
    "body": (
        "Если у тебя есть вопрос, баг или предложение — просто напиши его прямо "
        "здесь, в этом чате. Сообщение перешлётся админам, тебе ответят как "
        "можно скорее.\n\n"
        "Прямой контакт админа: @zerocious"
    ),
}


def _build_faq_menu_keyboard() -> InlineKeyboardMarkup:
    """Главное меню FAQ — кнопки по FAQ_ITEMS + техподдержка."""
    kb = InlineKeyboardBuilder()
    for item in FAQ_ITEMS:
        kb.button(text=item["btn"], callback_data=f"faq:show:{item['id']}")
    kb.button(
        text=FAQ_SUPPORT_ITEM["btn"],
        callback_data=f"faq:show:{FAQ_SUPPORT_ITEM['id']}",
    )
    kb.adjust(1)  # 1 кнопка на строку — текст длинный, в 2 столбца не влезет
    return kb.as_markup()


def _build_faq_answer_keyboard() -> InlineKeyboardMarkup:
    """Кнопка возврата в меню FAQ."""
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ К списку вопросов", callback_data="faq:back")
    kb.adjust(1)
    return kb.as_markup()


def _faq_lookup(item_id: str) -> dict | None:
    """Найти FAQ-элемент по id (среди вопросов и техподдержки)."""
    if item_id == FAQ_SUPPORT_ITEM["id"]:
        return FAQ_SUPPORT_ITEM
    for item in FAQ_ITEMS:
        if item["id"] == item_id:
            return item
    return None


@router.message(F.text == "❓ FAQ")
async def handle_faq(message: Message):
    """Главное меню FAQ — кнопки на каждый вопрос + техподдержка."""
    await message.answer(
        FAQ_MENU_TEXT,
        reply_markup=_build_faq_menu_keyboard(),
    )


@router.callback_query(F.data.startswith("faq:show:"))
async def handle_faq_show(callback: CallbackQuery):
    """Показать ответ на конкретный вопрос FAQ — edit-in-place."""
    item_id = callback.data.split(":", 2)[2]
    item = _faq_lookup(item_id)
    if item is None:
        await callback.answer("Вопрос не найден", show_alert=True)
        return
    text = f"{item['title']}\n\n{item['body']}"
    try:
        await callback.message.edit_text(text, reply_markup=_build_faq_answer_keyboard())
    except Exception as e:
        # Если edit упал (например, сообщение старше 48ч) — шлём новое
        logger.warning("faq.edit_failed item=%s reason=%s", item_id, e)
        await callback.message.answer(text, reply_markup=_build_faq_answer_keyboard())
    await callback.answer()


@router.callback_query(F.data == "faq:back")
async def handle_faq_back(callback: CallbackQuery):
    """Вернуться к списку вопросов."""
    try:
        await callback.message.edit_text(FAQ_MENU_TEXT, reply_markup=_build_faq_menu_keyboard())
    except Exception as e:
        logger.warning("faq.back_edit_failed reason=%s", e)
        await callback.message.answer(FAQ_MENU_TEXT, reply_markup=_build_faq_menu_keyboard())
    await callback.answer()

@router.message(F.text == "📢 Новости")
async def handle_news(message: Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="📢 Открыть канал", url=CHANNEL_URL)
    await message.answer(
        "📢 Подпишись на наш канал — там анонсы, советы по учебе "
        "и обновления бота.",
        reply_markup=kb.as_markup(),
    )

@router.message(F.text == "📊 Мой профиль")
async def cmd_profile(message: Message):
    user_id = message.from_user.id
    user = await user_repo.get_user(user_id)
    if not user:
        await message.answer("Сначала напиши /start для регистрации")
        return
    inline_kb = InlineKeyboardBuilder()
    inline_kb.button(text="🏆 Достижения", callback_data=f"show_achievements:{user_id}:1")
    inline_kb.button(text="⚙️ Настройки", callback_data=f"settings_menu:{user_id}")
    inline_kb.button(text="📊 Прогресс по предметам", callback_data=f"show_progress:{user_id}")
    # Питомец: image preview + customization picker (TODO #16 Phase B).
    inline_kb.button(text="🐾 Питомец", callback_data=f"pet_menu:{user_id}")
    # Друзья: open friends-tab. Использует существующий friends_back-handler
    # (тот же, что callback'и accept/reject/remove). callback_data prefix
    # 'friends_back' семантически означает «main friends-tab view».
    inline_kb.button(text="👥 Друзья", callback_data=f"friends_back:{user_id}")
    # Заморозка стрика (LEADERBOARD.md §Streak Freeze). Кнопка всегда видна;
    # confirm-экран сам показывает доступность (cooldown / баланс / уже куплено).
    inline_kb.button(text="❄️ Заморозить стрик", callback_data=f"freeze_menu:{user_id}")
    inline_kb.adjust(2, 1, 1, 1, 1)
    await message.answer(
        f"📊 Твой профиль:\n"
        f"🆔 ID: {user_id}\n"
        f"📚 Всего сессий: {user['total_sessions']}\n"
        f"💰 Всего монет: {user['total_coins']} 🪙\n"
        f"🔥 Стрик: {user['current_streak']} дней подряд\n"
        f"⏱️ Последняя сессия: {user.get('last_session', 'никогда')}\n\n"
        f"🐾 Твой питомец: {get_pet_emotion(user['current_streak'])}",
        reply_markup=inline_kb.as_markup()
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
FLASHCARD_SOURCE_LABELS = {
    "mix": "Микс",
    "official": "Официальные",
    "own": "Свои",
}
FLASHCARD_SOURCE_CYCLE = ["mix", "official", "own"]
USER_FLASHCARD_TERM_MAX = 200
USER_FLASHCARD_DEFINITION_MAX = 1000


def _render_bar(pct: float) -> str:
    """Рендерит progress-bar из 10 квадратов. pct в [0..1]."""
    pct = max(0.0, min(1.0, pct))
    filled = round(pct * PROGRESS_BAR_LENGTH)
    return PROGRESS_BAR_FILLED * filled + PROGRESS_BAR_EMPTY * (PROGRESS_BAR_LENGTH - filled)


def _humanize_when(ts_str: str | None) -> str:
    """'сегодня в HH:MM' / 'вчера' / 'N дней назад' / 'давно'."""
    if not ts_str:
        return "—"
    try:
        ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return "—"
    now = datetime.now()
    delta_days = (now.date() - ts.date()).days
    if delta_days == 0:
        return f"сегодня в {ts.strftime('%H:%M')}"
    if delta_days == 1:
        return "вчера"
    if delta_days < 7:
        return f"{delta_days} дн. назад"
    return f"{delta_days} дн. назад"


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


async def _build_subject_progress_block(user_id: int, subject_id: str, subject_label: str) -> str:
    """
    Строит блок для одного предмета:
      <label>
      <bar> <pct>%
        🔔 К повторению ...
        🕐 Активность ...
        📈 Заходов ...
    Если контента нет — заглушка с «🚧 Скоро».
    """
    # Загружаем все items предмета. Используем существующие load_*-функции.
    section_terms: list[str] = []  # term_hash из всех непустых разделов situational
    for _label, key in available_quiz_sections(subject_id):
        for term in load_quiz_section(key, subject_id):
            section_terms.append(term.hash)
    cards = load_flashcards(subject_id)
    user_cards = await user_flashcard_repo.list_by_subject(user_id, subject_id)
    mcq_qs = load_mcq(subject_id)
    tasks_list = load_tasks(subject_id)

    card_hashes = [c["hash"] for c in cards] + [c["hash"] for c in user_cards]
    mcq_hashes = [_mcq_hash(q["question"]) for q in mcq_qs]
    task_ids = [t["id"] for t in tasks_list]

    total = len(section_terms) + len(card_hashes) + len(mcq_hashes) + len(task_ids)
    if total == 0:
        return (
            f"{subject_label}\n"
            f"{PROGRESS_BAR_EMPTY * PROGRESS_BAR_LENGTH}  0%\n"
            f"  🚧 Контент в разработке\n"
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
    last_activity = _humanize_when(stats["last_activity"] if stats else None)

    lines = [
        subject_label,
        f"{_render_bar(pct)} {pct_int}%",
    ]
    if total_due > 0:
        lines.append(f"  🔔 К повторению сегодня: {total_due}")
    else:
        lines.append(f"  🔔 К повторению сегодня: ничего")
    lines.append(f"  🕐 Активность: {last_activity}")
    lines.append(f"  📈 Заходов: {visits}")
    return "\n".join(lines) + "\n"


async def build_progress_view(user_id: int) -> str:
    """Полный текст экрана прогресса (Markdown/plain — без parse_mode)."""
    user = await user_repo.get_user(user_id)
    if not user:
        return "Сначала напиши /start для регистрации."
    # Общие монеты и стрик из users; общие минуты — из study_sessions
    total_minutes = await session_repo.get_total_minutes(user_id)
    header = (
        f"📊 Прогресс\n\n"
        f"Всего: 🪙 {user['total_coins']} монет · "
        f"⏱️ {total_minutes} мин учёбы · "
        f"🔥 стрик {user['current_streak']} дней\n"
    )
    blocks = []
    for subject_id, subject_label in SUBJECTS:
        blocks.append(await _build_subject_progress_block(user_id, subject_id, subject_label))
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
        await callback.answer("Это не твой прогресс", show_alert=True)
        return
    text = await build_progress_view(target_user_id)
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Профиль", callback_data=f"back_to_profile:{target_user_id}")
    kb.adjust(1)
    try:
        await callback.message.edit_text(text, reply_markup=kb.as_markup())
    except Exception:
        await callback.message.answer(text, reply_markup=kb.as_markup())
    await callback.answer()


# ------------------------------------------------------------


def get_pet_emotion(streak: int) -> str:
    if streak == 0:
        return "грустный 😢 (начни учиться сегодня!)"
    elif streak < 3:
        return "радостный 😊 (так держать!)"
    elif streak < 7:
        return "очень счастливый 🤗 (ты молодец!)"
    else:
        return "легендарный 🌟 (ты чемпион!)"

# ------------------------------------------------------------
# Настройки
# ------------------------------------------------------------
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

    async def toggle(self, setting_type: str) -> tuple[bool, str]:
        key_map = {
            "morning": "morning_enabled",
            "evening": "evening_enabled",
            "streak": "streak_enabled",
            "achievements": "achievements_enabled"
        }
        key = key_map.get(setting_type)
        if not key:
            raise ValueError(f"Unknown setting type: {setting_type}")
        # Lock защищает load-then-save: без него быстрые двойные клики
        # могут потерять одно из переключений.
        async with self.repo.db.lock:
            settings = await self.load()
            current = settings.get(key, 1)
            new_value = 0 if current else 1
            settings[key] = new_value
            await self.save(settings)
        status = "включены" if new_value else "отключены"
        label_map = {
            "morning": "🌅 Утро",
            "evening": "🌙 Вечер",
            "streak": "🔥 Стрик",
            "achievements": "🎉 Достижения"
        }
        return bool(new_value), f"{label_map[setting_type]}: {status}"

    async def cycle_flashcard_source(self) -> tuple[str, str]:
        """mix → official → own → mix. Возвращает (label, source_key)."""
        async with self.repo.db.lock:
            settings = await self.load()
            current = settings.get("flashcard_source", "mix")
            if current not in FLASHCARD_SOURCE_CYCLE:
                current = "mix"
            idx = FLASHCARD_SOURCE_CYCLE.index(current)
            new_source = FLASHCARD_SOURCE_CYCLE[(idx + 1) % len(FLASHCARD_SOURCE_CYCLE)]
            settings["flashcard_source"] = new_source
            await self.save(settings)
        return FLASHCARD_SOURCE_LABELS[new_source], new_source

    async def set_time(self, slot: str, time_str: str) -> None:
        """Сохраняет утреннее/вечернее время. slot ∈ {'morning','evening'}."""
        if slot not in ("morning", "evening"):
            raise ValueError(f"Unknown slot: {slot}")
        async with self.repo.db.lock:
            settings = await self.load()
            settings[f"{slot}_time"] = time_str
            await self.save(settings)

    async def get_display_text(self) -> str:
        settings = await self.load()
        user = await self.repo.get_user(self.user_id)
        tz = (user or {}).get("timezone") or "Europe/Moscow"
        hidden = await self.repo.is_hidden_from_leaderboards(self.user_id)
        lines = ["⚙️ Настройки уведомлений\n"]
        emoji_on = {"morning": "🌅", "evening": "🌙", "streak": "🔥", "achievements": "🎉"}
        emoji_off = {"morning": "🌚", "evening": "🌚", "streak": "❄️", "achievements": "🔕"}
        time_keys = {"morning": "morning_time", "evening": "evening_time"}
        labels = {"morning": "Утро", "evening": "Вечер", "streak": "Стрик", "achievements": "Достижения"}
        for key in ["morning", "evening", "streak", "achievements"]:
            enabled = settings.get(f"{key}_enabled", 1)
            emoji = emoji_on[key] if enabled else emoji_off[key]
            time_str = ""
            if key in time_keys:
                time_val = settings.get(time_keys[key], "")
                time_str = f" ({time_val})" if time_val else ""
            status = "✅ Включено" if enabled else "❌ Отключено"
            lines.append(f"{emoji} {labels[key]}{time_str}: {status}")
        source = settings.get("flashcard_source", "mix")
        source_label = FLASHCARD_SOURCE_LABELS.get(source, source)
        lines.append(f"\n🃏 Флэш-карты: {source_label}")
        lines.append(f"\n🌍 Часовой пояс: {tz_label(tz)}")
        lines.append(
            f"👤 Лидерборды: "
            f"{'❌ Скрыт (рейтинги не видны другим)' if hidden else '✅ Виден'}"
        )
        return "\n".join(lines)

    async def get_keyboard(self) -> InlineKeyboardMarkup:
        settings = await self.load()
        hidden = await self.repo.is_hidden_from_leaderboards(self.user_id)
        kb = InlineKeyboardBuilder()
        labels = {"morning": "Утро", "evening": "Вечер", "streak": "Стрик", "achievements": "Достижения"}
        # Утро: переключатель + кнопка изменения времени
        for key in ["morning", "evening"]:
            enabled = settings.get(f"{key}_enabled", 1)
            kb.button(
                text=f"{labels[key]}: {'Выкл' if enabled else 'Вкл'}",
                callback_data=f"settings_toggle:{key}:{self.user_id}",
            )
            kb.button(
                text=f"🕘 Изменить",
                callback_data=f"settings_time:{key}:{self.user_id}",
            )
        # Стрик / Достижения — только переключатели
        for key in ["streak", "achievements"]:
            enabled = settings.get(f"{key}_enabled", 1)
            kb.button(
                text=f"{labels[key]}: {'Выкл' if enabled else 'Вкл'}",
                callback_data=f"settings_toggle:{key}:{self.user_id}",
            )
        kb.button(text="🌍 Часовой пояс", callback_data=f"settings_tz_picker:{self.user_id}")
        # Privacy toggle: единственная кнопка для leaderboards (LEADERBOARD.md §Privacy).
        kb.button(
            text=f"👤 Лидерборды: {'Скрыт' if hidden else 'Виден'}",
            callback_data=f"settings_privacy:{self.user_id}",
        )
        source = settings.get("flashcard_source", "mix")
        source_label = FLASHCARD_SOURCE_LABELS.get(source, source)
        kb.button(
            text=f"🃏 Флэш-карты: {source_label}",
            callback_data=f"settings_flash_source:{self.user_id}",
        )
        kb.button(text="📇 Мои карточки", callback_data=f"fc_manage:{self.user_id}")
        kb.button(text="⬅️ Назад в профиль", callback_data=f"back_to_profile:{self.user_id}")
        kb.adjust(2, 2, 2, 1, 1, 1, 1)
        return kb.as_markup()

@router.callback_query(F.data.startswith("settings_menu:"))
async def show_settings_menu(callback: CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id
    ns = NotificationSettings(user_id, user_repo)
    await callback.message.edit_text(
        await ns.get_display_text(),
        reply_markup=await ns.get_keyboard()
    )

@router.callback_query(F.data.startswith("settings_toggle:"))
async def toggle_notification_setting(callback: CallbackQuery):
    await callback.answer()
    _, setting_type, _ = callback.data.split(":")
    ns = NotificationSettings(callback.from_user.id, user_repo)
    try:
        new_value, status_text = await ns.toggle(setting_type)
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
        await callback.message.edit_text(
            await ns.get_display_text(),
            reply_markup=await ns.get_keyboard()
        )
    except Exception as e:
        logger.error(f"Error toggling setting: {e}")
        await callback.answer("Ошибка переключения", show_alert=True)


@router.callback_query(F.data.startswith("settings_flash_source:"))
async def cycle_flashcard_source_setting(callback: CallbackQuery):
    try:
        target_user_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer()
        return
    if callback.from_user.id != target_user_id:
        await callback.answer("Это не твои настройки", show_alert=True)
        return
    ns = NotificationSettings(target_user_id, user_repo)
    async with db.lock:
        new_label, new_source = await ns.cycle_flashcard_source()
    await event_repo.log(
        target_user_id,
        "settings_changed",
        {"setting": "flashcard_source", "value": new_source},
    )
    await callback.message.edit_text(
        await ns.get_display_text(),
        reply_markup=await ns.get_keyboard(),
    )
    await callback.answer(f"🃏 Источник: {new_label}")


def _subject_label_by_id(subject_id: str) -> str:
    for sid, label in SUBJECTS:
        if sid == subject_id:
            return label
    return subject_id


def _build_fc_subject_picker_keyboard(user_id: int, prefix: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for sid, label in SUBJECTS:
        kb.button(text=label, callback_data=f"{prefix}:{user_id}:{sid}")
    kb.adjust(1)
    return kb.as_markup()


async def _build_fc_list_text(user_id: int, subject_id: str) -> str:
    cards = await user_flashcard_repo.list_by_subject(user_id, subject_id)
    subject_label = _subject_label_by_id(subject_id)
    if not cards:
        return (
            f"📇 Мои карточки — {subject_label}\n\n"
            f"Пока пусто. Нажми «➕ Добавить», чтобы создать первую."
        )
    lines = [f"📇 Мои карточки — {subject_label}", f"Всего: {len(cards)}", ""]
    for i, card in enumerate(cards, 1):
        lines.append(f"{i}. {card['term']}")
    return "\n".join(lines)


def _build_fc_list_keyboard(user_id: int, subject_id: str, cards: list[dict]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить", callback_data=f"fc_add:{user_id}:{subject_id}")
    for card in cards:
        term_preview = card["term"][:30] + ("…" if len(card["term"]) > 30 else "")
        kb.button(
            text=f"🗑 {term_preview}",
            callback_data=f"fc_del:{user_id}:{subject_id}:{card['id']}",
        )
    kb.button(text="⬅️ К предметам", callback_data=f"fc_manage:{user_id}")
    kb.adjust(1)
    return kb.as_markup()


async def _start_flashcard_create_wizard(
    message: Message,
    state: FSMContext,
    user_id: int,
    subject_id: str,
) -> None:
    subject_label = _subject_label_by_id(subject_id)
    await state.set_state(FlashcardCreateStates.waiting_for_term)
    await state.update_data(fc_subject_id=subject_id, fc_subject_label=subject_label)
    await message.answer(
        f"📇 Новая карточка — {subject_label}\n\n"
        f"Шаг 1/2: введи термин (вопрос), до {USER_FLASHCARD_TERM_MAX} символов.",
    )


@router.callback_query(F.data.startswith("fc_manage:"))
async def handle_fc_manage(callback: CallbackQuery):
    try:
        user_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer()
        return
    if callback.from_user.id != user_id:
        await callback.answer("Это не твои карточки", show_alert=True)
        return
    text = "📇 Мои карточки\n\nВыбери предмет:"
    kb = _build_fc_subject_picker_keyboard(user_id, "fc_list")
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
        await callback.answer("Это не твои карточки", show_alert=True)
        return
    cards = await user_flashcard_repo.list_by_subject(user_id, subject_id)
    text = await _build_fc_list_text(user_id, subject_id)
    kb = _build_fc_list_keyboard(user_id, subject_id, cards)
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
        await callback.answer("Это не твои карточки", show_alert=True)
        return
    await callback.answer()
    await _start_flashcard_create_wizard(callback.message, state, user_id, subject_id)


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
        await callback.answer("Это не твои карточки", show_alert=True)
        return
    deleted = await user_flashcard_repo.delete(user_id, card_id)
    if deleted:
        await event_repo.log(
            user_id,
            "user_flashcard_deleted",
            {"subject_id": subject_id, "card_id": card_id},
        )
    cards = await user_flashcard_repo.list_by_subject(user_id, subject_id)
    text = await _build_fc_list_text(user_id, subject_id)
    kb = _build_fc_list_keyboard(user_id, subject_id, cards)
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        await callback.message.answer(text, reply_markup=kb)
    await callback.answer("Удалено" if deleted else "Карточка не найдена")


@router.message(FlashcardCreateStates.waiting_for_term)
async def handle_fc_term(message: Message, state: FSMContext):
    term = (message.text or "").strip()
    if not term:
        await message.answer("Термин не может быть пустым. Попробуй ещё раз.")
        return
    if len(term) > USER_FLASHCARD_TERM_MAX:
        await message.answer(
            f"Слишком длинный термин (макс. {USER_FLASHCARD_TERM_MAX} символов)."
        )
        return
    await state.update_data(fc_term=term)
    await state.set_state(FlashcardCreateStates.waiting_for_definition)
    await message.answer(
        f"Шаг 2/2: введи определение (ответ), "
        f"до {USER_FLASHCARD_DEFINITION_MAX} символов."
    )


@router.message(FlashcardCreateStates.waiting_for_definition)
async def handle_fc_definition(message: Message, state: FSMContext):
    definition = (message.text or "").strip()
    if not definition:
        await message.answer("Определение не может быть пустым. Попробуй ещё раз.")
        return
    if len(definition) > USER_FLASHCARD_DEFINITION_MAX:
        await message.answer(
            f"Слишком длинное определение (макс. {USER_FLASHCARD_DEFINITION_MAX} символов)."
        )
        return

    data = await state.get_data()
    subject_id = data.get("fc_subject_id")
    subject_label = data.get("fc_subject_label", subject_id)
    term = data.get("fc_term", "")
    user_id = message.from_user.id

    try:
        card = await user_flashcard_repo.create(user_id, subject_id, term, definition)
    except ValueError as e:
        if str(e) == "limit_exceeded":
            await message.answer(
                f"Достигнут лимит {UserFlashcardRepository.MAX_PER_SUBJECT} карточек "
                f"на этот предмет. Удали старые, чтобы добавить новые."
            )
            await state.clear()
            return
        raise
    except sqlite3.IntegrityError:
        await message.answer(
            f"Карточка с термином «{term}» уже есть в этом предмете. "
            f"Введи другой термин:"
        )
        await state.set_state(FlashcardCreateStates.waiting_for_term)
        return
    except Exception as e:
        logger.error("fc.create_failed user_id=%s reason=%s", user_id, e)
        await message.answer("Не удалось сохранить карточку. Попробуй позже.")
        await state.clear()
        return

    await event_repo.log(
        user_id,
        "user_flashcard_created",
        {"subject_id": subject_id, "card_id": card["id"]},
    )
    await state.clear()

    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить ещё", callback_data=f"fc_add:{user_id}:{subject_id}")
    kb.button(text="📇 Мои карточки", callback_data=f"fc_list:{user_id}:{subject_id}")
    kb.button(text="🃏 Начать учёбу", callback_data=f"fc_study:{user_id}:{subject_id}")
    kb.adjust(1)
    await message.answer(
        f"✅ Карточка сохранена!\n\n"
        f"<b>{term}</b>\n<i>{definition}</i>",
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
        await callback.answer("Это не твоя сессия", show_alert=True)
        return
    subject_label = _subject_label_by_id(subject_id)
    await callback.answer()
    await state.set_state(QuizStates.choosing_mode)
    await state.update_data(subject_id=subject_id, subject_label=subject_label, mode_id="flashcards")
    await start_flashcard_session(
        callback.message, state, subject_id, subject_label=subject_label,
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
    await callback.answer()
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
        await callback.message.edit_text(
            await ns.get_display_text(),
            reply_markup=await ns.get_keyboard(),
        )
    except Exception as e:
        logger.warning("settings.privacy_toggle_render_failed user=%s err=%s", user_id, e)


@router.callback_query(F.data.startswith("settings_time:"))
async def request_time_change(callback: CallbackQuery, state: FSMContext):
    """Просит пользователя ввести новое время для утреннего/вечернего слота."""
    await callback.answer()
    _, slot, _ = callback.data.split(":")
    if slot not in ("morning", "evening"):
        await callback.answer("Неизвестный слот", show_alert=True)
        return
    label = "утреннего" if slot == "morning" else "вечернего"
    await state.set_state(SettingsStates.waiting_for_time)
    await state.update_data(slot=slot, return_to="settings")
    await callback.message.answer(
        f"🕘 Введи время {label} напоминания в формате ЧЧ:ММ (например, 09:30).\n"
        f"Для отмены отправь /cancel."
    )


@router.callback_query(F.data.startswith("rate:"))
async def handle_session_rating(callback: CallbackQuery):
    """Сохраняет эмодзи-оценку только что завершённой сессии."""
    await callback.answer()
    parts = callback.data.split(":")
    if len(parts) != 3:
        return
    try:
        session_id = int(parts[1])
        score = int(parts[2])
    except ValueError:
        return
    if score < 1 or score > len(RATING_EMOJIS):
        return
    user_id = callback.from_user.id
    updated = await session_repo.set_session_score(session_id, user_id, score)
    if not updated:
        # Сессия не принадлежит этому пользователю или не существует.
        await callback.answer("Не удалось сохранить оценку", show_alert=True)
        return
    logger.info("session.rated user_id=%s session_id=%s score=%s", user_id, session_id, score)
    emoji = next((e for s, e in RATING_EMOJIS if s == score), "")
    try:
        await callback.message.edit_text(f"✅ Спасибо за оценку! {emoji}")
    except Exception:
        pass


@router.callback_query(F.data.startswith("rate_skip:"))
async def handle_session_rating_skip(callback: CallbackQuery):
    """Пропуск оценки — просто убираем клавиатуру."""
    await callback.answer()
    try:
        await callback.message.edit_text("Без проблем 👌")
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
    kb.button(text="⬅️ Назад в настройки", callback_data=f"settings_menu:{user_id}")
    kb.adjust(1)
    await callback.message.edit_text(
        "🌍 Выбери свой часовой пояс — напоминания и сброс стрика будут привязаны к нему:",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data.startswith("settings_tz_set:"))
async def set_user_timezone(callback: CallbackQuery):
    """Сохраняет выбранный TZ и возвращается в меню настроек."""
    await callback.answer()
    tz_id = callback.data.split(":", 1)[1]
    if tz_id not in TZ_IDS:
        await callback.answer("Неизвестный часовой пояс", show_alert=True)
        return
    user_id = callback.from_user.id
    await user_repo.set_timezone(user_id, tz_id)
    await event_repo.log(
        user_id,
        "settings_changed",
        {"setting": "timezone", "value": tz_id},
    )
    ns = NotificationSettings(user_id, user_repo)
    await callback.message.edit_text(
        await ns.get_display_text(),
        reply_markup=await ns.get_keyboard(),
    )


@router.message(SettingsStates.waiting_for_time, Command("cancel"))
async def cancel_time_input(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=get_main_keyboard())


@router.message(SettingsStates.waiting_for_time)
async def process_time_input(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    match = TIME_RE.match(text)
    if not match:
        await message.answer(
            "❌ Неверный формат. Введи время как ЧЧ:ММ, например 09:30.\n"
            "Для отмены отправь /cancel."
        )
        return
    # Нормализуем "9:5" → "09:05"
    hours, minutes = match.group(1), match.group(2)
    normalized = f"{int(hours):02d}:{int(minutes):02d}"

    data = await state.get_data()
    slot = data.get("slot")
    if slot not in ("morning", "evening"):
        await state.clear()
        await message.answer("Ошибка состояния, попробуй ещё раз.", reply_markup=get_main_keyboard())
        return

    user_id = message.from_user.id
    ns = NotificationSettings(user_id, user_repo)
    await ns.set_time(slot, normalized)
    await event_repo.log(
        user_id,
        "settings_changed",
        {"setting": f"{slot}_time", "value": normalized},
    )
    await state.clear()

    label = "утреннее" if slot == "morning" else "вечернее"
    await message.answer(
        f"✅ Новое {label} время сохранено: {normalized}",
        reply_markup=get_main_keyboard(),
    )

@router.callback_query(F.data.startswith("back_to_profile:"))
async def back_to_profile(callback: CallbackQuery):
    await callback.answer()
    user_id = int(callback.data.split(":")[1])
    user = await user_repo.get_user(user_id)
    if not user:
        await callback.message.answer("Пользователь не найден")
        return
    inline_kb = InlineKeyboardBuilder()
    inline_kb.button(text="🏆 Достижения", callback_data=f"show_achievements:{user_id}:1")
    inline_kb.button(text="⚙️ Настройки", callback_data=f"settings_menu:{user_id}")
    inline_kb.button(text="📊 Прогресс по предметам", callback_data=f"show_progress:{user_id}")
    # Питомец: image preview + customization picker (TODO #16 Phase B).
    inline_kb.button(text="🐾 Питомец", callback_data=f"pet_menu:{user_id}")
    # Друзья: open friends-tab. Использует существующий friends_back-handler
    # (тот же, что callback'и accept/reject/remove). callback_data prefix
    # 'friends_back' семантически означает «main friends-tab view».
    inline_kb.button(text="👥 Друзья", callback_data=f"friends_back:{user_id}")
    # Заморозка стрика (LEADERBOARD.md §Streak Freeze). Кнопка всегда видна;
    # confirm-экран сам показывает доступность (cooldown / баланс / уже куплено).
    inline_kb.button(text="❄️ Заморозить стрик", callback_data=f"freeze_menu:{user_id}")
    inline_kb.adjust(2, 1, 1, 1, 1)
    await callback.message.edit_text(
        f"📊 Твой профиль:\n"
        f"🆔 ID: {user_id}\n"
        f"📚 Всего сессий: {user['total_sessions']}\n"
        f"💰 Всего монет: {user['total_coins']} 🪙\n"
        f"🔥 Стрик: {user['current_streak']} дней\n"
        f"⏱️ Последняя сессия: {user.get('last_session', 'никогда')}\n\n"
        f"🐾 Твой питомец: {get_pet_emotion(user['current_streak'])}",
        reply_markup=inline_kb.as_markup()
    )

# ------------------------------------------------------------
# Достижения
# ------------------------------------------------------------
@router.callback_query(F.data.startswith("show_achievements:"))
async def show_achievements(callback: CallbackQuery):
    await callback.answer()
    parts = callback.data.split(":")
    user_id = int(parts[1])
    page = int(parts[2]) if len(parts) > 2 else 1

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

# ------------------------------------------------------------
# Таймеры
# ------------------------------------------------------------
async def run_timer_task(chat_id: int, state: FSMContext, user_id: int, duration: int):
    try:
        # Спим до момента (start_time + duration). Для свежего таймера
        # start_time только что записан в state.data → remaining = duration*60.
        # Для таймера, восстановленного после рестарта в reconcile_stale_timers,
        # start_time остался от прошлого запуска → remaining = сколько осталось.
        data = await state.get_data()
        start_time = data.get("start_time")
        if not isinstance(start_time, datetime):
            start_time = datetime.now()
        deadline = start_time + timedelta(minutes=duration)
        remaining_sec = max(0, (deadline - datetime.now()).total_seconds())
        await asyncio.sleep(remaining_sec)
        current_state = await state.get_state()
        if current_state == TimerStates.active.state:
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
            response = (
                f"🎉 Таймер завершён!\n"
                f"⏱️ Сессия: {duration} минут\n"
                f"🪙 Получено: {duration} монет"
            )
            if bonus > 0:
                response += f"\n✨ Бонус за достижения: +{bonus} монет"
            response += f"\n📊 Всего монет: {user['total_coins']}"
            try:
                await bot.send_message(chat_id, response, reply_markup=get_main_keyboard())
            except Exception as e:
                logger.error(f"Ошибка отправки завершения таймера {user_id}: {e}")
            if earned:
                await send_achievement_notification(user_id, earned)
            await send_rating_prompt(chat_id, session_id)
            await state.clear()
    except asyncio.CancelledError:
        pass
    finally:
        active_timers.pop(user_id, None)


def start_timer(chat_id: int, state: FSMContext, user_id: int, duration: int) -> None:
    """Отменяет старый таймер пользователя (если есть) и запускает новый."""
    old = active_timers.get(user_id)
    if old and not old.done():
        old.cancel()
    task = asyncio.create_task(run_timer_task(chat_id, state, user_id, duration))
    active_timers[user_id] = task

async def send_achievement_notification(user_id: int, achievement_ids: list):
    settings = await user_repo.get_notification_settings(user_id)
    if settings and not settings.get("achievements_enabled", 1):
        return
    if len(achievement_ids) == 1:
        ach_id = achievement_ids[0]
        ach = ACHIEVEMENTS.get(ach_id, {})
        msg = (
            f"🎉 ПОЗДРАВЛЯЕМ! Ты получил(а) новое достижение:\n\n"
            f"{ach.get('icon', '🏆')} {ach.get('name', 'Достижение')}\n"
            f"{ach.get('description', '')}\n\n"
            f"🪙 Бонус: +{ach.get('reward', 0)} монет\n"
            f"🐾 Твой питомец гордится тобой!"
        )
    else:
        achievements_list = []
        total_reward = 0
        for ach_id in achievement_ids:
            ach = ACHIEVEMENTS.get(ach_id, {})
            achievements_list.append(f"{ach.get('icon', '🏆')} {ach.get('name', 'Достижение')} (+{ach.get('reward', 0)} монет)")
            total_reward += ach.get('reward', 0)
        msg = (
            f"🎊 ВАУ! Ты получил(а) несколько достижений за одну сессию:\n\n" +
            "\n".join(achievements_list) +
            f"\n\n🪙 Общий бонус: +{total_reward} монет\n"
            f"🔥 Ты настоящий чемпион учёбы!"
        )
    try:
        await bot.send_message(user_id, msg)
    except Exception as e:
        logger.error(f"Ошибка отправки уведомления о достижении {user_id}: {e}")

@router.message(F.text == "⏱️ Стандартный таймер (25 мин)")
async def handle_standard_timer(message: Message, state: FSMContext):
    user_id = message.from_user.id
    if not await user_repo.user_exists(user_id):
        await user_repo.create_user(
            user_id, username=message.from_user.username
        )
    current_state = await state.get_state()
    if current_state == TimerStates.active.state:
        data = await state.get_data()
        start_time = data.get("start_time")
        if not start_time:
            await message.answer("Таймер повреждён, начните заново.", reply_markup=get_study_keyboard())
            await state.clear()
            return
        elapsed = (datetime.now() - start_time).total_seconds() / 60
        remaining = max(0, data["duration"] - elapsed)
        await message.answer(
            f"⏱️ Таймер уже запущен!\n"
            f"Осталось: {remaining:.0f} минут\n",
            reply_markup=get_timer_active_keyboard()
        )
        return
    duration = 25
    await state.set_state(TimerStates.active)
    await state.update_data(duration=duration, start_time=datetime.now())
    await message.answer(
        f"⏱️ Таймер запущен на {duration} минут!\n"
        f"Ваш питомец ждёт вашего возвращения 🐾",
        reply_markup=get_timer_active_keyboard()
    )
    await event_repo.log(user_id, "session_started", {"duration": duration, "kind": "standard"})
    start_timer(message.chat.id, state, user_id, duration)

@router.message(F.text == "⏱️ Кастомный таймер")
async def handle_custom_timer_start(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state == TimerStates.active.state:
        data = await state.get_data()
        start_time = data.get("start_time")
        if not start_time:
            await message.answer("Таймер повреждён, начните заново.", reply_markup=get_study_keyboard())
            await state.clear()
            return
        elapsed = (datetime.now() - start_time).total_seconds() / 60
        remaining = max(0, data["duration"] - elapsed)
        await message.answer(
            f"⏱️ Таймер уже запущен!\n"
            f"Осталось: {remaining:.0f} минут\n",
            reply_markup=get_timer_active_keyboard()
        )
        return
    await message.answer("🔢 Сколько минут учиться? (5–120)")
    await state.set_state(TimerStates.waiting_for_duration)

@router.message(TimerStates.waiting_for_duration)
async def process_duration(message: Message, state: FSMContext):
    text = ''.join(filter(str.isdigit, message.text))
    if not text:
        await message.answer("❌ Введите число от 5 до 120:")
        return
    duration = int(text)
    if duration < 5 or duration > 120:
        await message.answer("⚠️ Минимум 5, максимум 120 минут. Попробуйте ещё раз:")
        return
    user_id = message.from_user.id
    if not await user_repo.user_exists(user_id):
        await user_repo.create_user(
            user_id, username=message.from_user.username
        )
    await state.set_state(TimerStates.active)
    await state.update_data(duration=duration, start_time=datetime.now())
    await message.answer(
        f"⏱️ Таймер запущен на {duration} минут!\n"
        f"Ваш питомец ждёт вашего возвращения 🐾",
        reply_markup=get_timer_active_keyboard()
    )
    await event_repo.log(user_id, "session_started", {"duration": duration, "kind": "custom"})
    start_timer(message.chat.id, state, user_id, duration)

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
    current_state = await state.get_state()
    if current_state != TimerStates.active.state:
        return False  # не таймерный flow — не трогаем чужие данные
    data = await state.get_data()
    start_time = data.get("start_time")
    if not start_time:
        await state.clear()
        return False
    duration = data["duration"]
    elapsed = (datetime.now() - start_time).total_seconds() / 60
    actual = min(int(elapsed), duration)
    if actual < 1:
        await message.answer("Слишком короткая сессия, монеты не начислены.", reply_markup=get_main_keyboard())
        await state.clear()
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
    response = f"⏹️ Таймер остановлен!\n⏱️ Фактическая сессия: {actual} мин\n🪙 Получено: {actual} монет"
    if bonus > 0:
        response += f"\n✨ Бонус за достижения: +{bonus} монет"
    response += f"\n📊 Всего монет: {user['total_coins']}"
    await message.answer(response, reply_markup=get_main_keyboard())
    if earned:
        await send_achievement_notification(user_id, earned)
    await send_rating_prompt(message.chat.id, session_id)
    await state.clear()
    return True


@router.message(TimerStates.active, F.text == "⏹️ Остановить")
async def handle_stop_timer(message: Message, state: FSMContext):
    if not await stop_active_timer(message, state):
        await message.answer("Таймер уже завершён.", reply_markup=get_study_keyboard())


@router.message(Command("stop"))
async def cmd_stop(message: Message, state: FSMContext):
    """Останавливает активный таймер из любого места (например, из главного меню)."""
    if not await stop_active_timer(message, state):
        await message.answer("Сейчас нет активного таймера.", reply_markup=get_main_keyboard())


@router.message(TimerStates.active, F.text == "⬅️ Назад в меню")
async def handle_back_to_menu_during_timer(message: Message, state: FSMContext):
    await message.answer(
        "🐾 Ты вернулся в главное меню.\n"
        "Таймер продолжает работать в фоне — сессия завершится автоматически.\n\n"
        "Чтобы остановить досрочно, отправь /stop.",
        reply_markup=get_main_keyboard()
    )

# ------------------------------------------------------------
# Квизы — общий flow: ❓ Квизы → subject picker → mode picker → режим
# ------------------------------------------------------------
@router.message(F.text == "❓ Квизы")
async def handle_quiz_menu(message: Message, state: FSMContext):
    user_id = message.from_user.id
    subjects = await available_subjects(user_id)
    if not subjects:
        await message.answer(
            "🚧 Пока нет учебных материалов. Загляни позже!",
            reply_markup=get_study_keyboard(),
        )
        return
    await state.set_state(QuizStates.choosing_subject)
    await message.answer(
        "📖 Выбери предмет:",
        reply_markup=await get_subject_keyboard(user_id),
    )


@router.message(QuizStates.choosing_subject, F.text == "⬅️ Назад к учебе")
async def handle_subject_back_to_study(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("📖 Раздел учёбы:", reply_markup=get_study_keyboard())


@router.message(QuizStates.choosing_subject, F.text.in_([label for _, label in SUBJECTS]))
async def handle_subject_picked(message: Message, state: FSMContext):
    subject_id = next((sid for sid, label in SUBJECTS if label == message.text), None)
    if not subject_id:
        return
    user_id = message.from_user.id
    modes = await available_modes(subject_id, user_id)
    if not modes:
        await message.answer(
            f"🚧 «{message.text}» — пока нет доступных режимов.",
            reply_markup=await get_subject_keyboard(user_id),
        )
        return
    await state.update_data(subject_id=subject_id, subject_label=message.text)
    await event_repo.log(user_id, "subject_picked", {"subject_id": subject_id})
    await state.set_state(QuizStates.choosing_mode)
    await message.answer(
        f"{message.text}\nВыбери режим учёбы:",
        reply_markup=await get_mode_keyboard_for_subject(subject_id, user_id),
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить карточку", callback_data=f"fc_add:{user_id}:{subject_id}")
    kb.button(text="📇 Мои карточки", callback_data=f"fc_list:{user_id}:{subject_id}")
    kb.adjust(2)
    await message.answer("Или управляй своими карточками:", reply_markup=kb.as_markup())


@router.message(QuizStates.choosing_mode, F.text == "⬅️ Назад к предметам")
async def handle_mode_back_to_subjects(message: Message, state: FSMContext):
    user_id = message.from_user.id
    await state.set_state(QuizStates.choosing_subject)
    await message.answer(
        "📖 Выбери предмет:",
        reply_markup=await get_subject_keyboard(user_id),
    )


@router.message(QuizStates.choosing_mode, F.text.in_([label for _, label in STUDY_MODES]))
async def handle_mode_picked(message: Message, state: FSMContext):
    mode_id = next((mid for mid, label in STUDY_MODES if label == message.text), None)
    if not mode_id:
        return
    data = await state.get_data()
    subject_id = data.get("subject_id")
    subject_label = data.get("subject_label", message.text)
    user_id = message.from_user.id
    if not subject_id:
        await state.set_state(QuizStates.choosing_subject)
        await message.answer(
            "Сначала выбери предмет:",
            reply_markup=await get_subject_keyboard(user_id),
        )
        return
    available = await available_modes(subject_id, user_id)
    if not any(m[0] == mode_id for m in available):
        await message.answer(
            f"🚧 «{message.text}» недоступен для этого предмета.",
            reply_markup=await get_mode_keyboard_for_subject(subject_id, user_id),
        )
        return
    await state.update_data(mode_id=mode_id, mode_label=message.text)
    await event_repo.log(user_id, "mode_picked", {
        "mode_id": mode_id, "subject_id": subject_id,
    })

    if mode_id == "situational":
        await state.set_state(QuizStates.choosing_section)
        await message.answer(
            f"📚 {subject_label}\nВыбери раздел:",
            reply_markup=get_quiz_section_keyboard(),
        )
    elif mode_id == "mcq":
        await start_mcq_session(message, state, subject_id, subject_label=subject_label)
    elif mode_id == "tasks":
        await start_task_session(message, state, subject_id, subject_label=subject_label)
    elif mode_id == "flashcards":
        await start_flashcard_session(message, state, subject_id, subject_label=subject_label)
    else:
        await message.answer(
            "Что-то пошло не так. Возвращаемся к режимам.",
            reply_markup=await get_mode_keyboard_for_subject(subject_id, user_id),
        )
        await state.set_state(QuizStates.choosing_mode)


# ============================================================
# MCQ flow (Multiple Choice Quiz, #13)
# ============================================================
async def start_mcq_session(message: Message, state: FSMContext, subject_id: str, subject_label: str):
    questions = load_mcq(subject_id)
    user_id = message.from_user.id
    if not questions:
        await message.answer(
            "🚧 Для этого предмета пока нет MCQ-вопросов.",
            reply_markup=await get_mode_keyboard_for_subject(subject_id, user_id),
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
        f"📝 MCQ — {subject_label}\n"
        f"Вопросов: {len(questions)}. За каждый правильный +1 🪙.",
        reply_markup=get_mcq_active_keyboard(),
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
    await bot.send_message(
        chat_id,
        f"❓ Вопрос {idx + 1}/{len(questions)}\n\n{q['question']}",
        reply_markup=kb.as_markup(),
    )


async def _finish_mcq_session(chat_id: int, state: FSMContext):
    data = await state.get_data()
    correct = data.get("mcq_correct_count", 0)
    total = len(data.get("mcq_questions", []))
    subject_label = data.get("subject_label", "")
    logger.info(
        "mcq.session.complete user_id=%s subject=%s correct=%s total=%s coins=%s",
        data.get("mcq_user_id"), data.get("subject_id"), correct, total, correct,
    )
    await bot.send_message(
        chat_id,
        f"🎉 Готово! {subject_label}\n"
        f"Правильных: {correct} из {total}\n"
        f"🪙 Заработано: {correct} монет",
        reply_markup=get_study_keyboard(),
    )
    await state.clear()


@router.message(QuizStates.answering_mcq, F.text == "🛑 Завершить")
async def handle_mcq_stop(message: Message, state: FSMContext):
    data = await state.get_data()
    correct = data.get("mcq_correct_count", 0)
    answered = data.get("mcq_index", 0)
    total = len(data.get("mcq_questions", []))
    logger.info(
        "mcq.session.stop user_id=%s subject=%s answered=%s/%s correct=%s",
        message.from_user.id, data.get("subject_id"), answered, total, correct,
    )
    await message.answer(
        f"⏹ MCQ остановлен.\n"
        f"Отвечено: {answered}/{total} (правильных: {correct})\n"
        f"🪙 Получено: {correct} монет",
        reply_markup=get_study_keyboard(),
    )
    await state.clear()


@router.callback_query(F.data.startswith("mcq:"))
async def handle_mcq_callback(callback: CallbackQuery, state: FSMContext):
    current_state = await state.get_state()
    if current_state != QuizStates.answering_mcq.state:
        await callback.answer("Сессия завершена", show_alert=False)
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
        await callback.answer("Состояние повреждено", show_alert=True)
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
        feedback = "✅ Верно! +1 🪙"
        await state.update_data(mcq_correct_count=data.get("mcq_correct_count", 0) + 1)
    else:
        # Wrong → сбрасываем series counter (LEADERBOARD.md §3).
        await leaderboard_repo.reset_quiz_series(user_id)
        feedback = f"❌ Неверно.\nПравильный ответ: {correct_text}"

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

    # Переходим к следующему вопросу
    await state.update_data(mcq_index=data.get("mcq_index", 0) + 1)
    await asyncio.sleep(1.0)  # короткая пауза, чтобы фидбек был заметен
    await _send_next_mcq_question(callback.message.chat.id, state)


# ============================================================
# Photo-task flow (#14)
# ============================================================
# Награды: +3 / +2 / +1 / 0 монет в зависимости от попытки (0 = открыли решение).
TASK_REWARDS_BY_ATTEMPT = [3, 2, 1]
MAX_TASK_ATTEMPTS = 3


async def start_task_session(message: Message, state: FSMContext, subject_id: str, subject_label: str):
    tasks = load_tasks(subject_id)
    user_id = message.from_user.id
    if not tasks:
        await message.answer(
            "🚧 Для этого предмета пока нет задач.",
            reply_markup=await get_mode_keyboard_for_subject(subject_id, user_id),
        )
        await state.set_state(QuizStates.choosing_mode)
        return
    await subject_stats_repo.bump_visit(message.from_user.id, subject_id)
    random.shuffle(tasks)
    # FSM data — только JSON-serializable: храним serializable view задачи,
    # пути к картинкам реконструируются по subject_id + task_id при отправке
    await state.update_data(
        task_questions=tasks,
        task_index=0,
        task_attempts=0,
        task_correct_count=0,
        task_coins_earned=0,
        task_user_id=message.from_user.id,
        task_subject_id=subject_id,
        task_subject_label=subject_label,
    )
    await state.set_state(QuizStates.answering_task)
    await message.answer(
        f"📷 Задачи — {subject_label}\n"
        f"Задач: {len(tasks)}. До 3 попыток на задачу. "
        f"Награды: +3 / +2 / +1 🪙; 0 🪙 если открыли решение.",
        reply_markup=get_task_active_keyboard(),
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
    tasks_dir = STUDY_MATERIALS_PATH / subject_id / "tasks"
    image_path = tasks_dir / f"{task['id']}.png"
    if not image_path.exists():
        # Контент изменился во время сессии — пропускаем
        logger.warning(
            "task.image_missing_at_send task_id=%s subject=%s expected=%s",
            task["id"], subject_id, image_path.name,
        )
        await state.update_data(task_index=idx + 1, task_attempts=0)
        await _send_next_task(chat_id, state)
        return
    # Сбрасываем счётчик попыток для новой задачи
    await state.update_data(task_attempts=0)

    caption_lines = [f"📷 Задача {idx + 1}/{len(tasks)}"]
    if task.get("problem"):
        caption_lines.append("")
        caption_lines.append(task["problem"])
    caption_lines.append("")
    caption_lines.append("✏️ Введи ответ:")
    caption = "\n".join(caption_lines)

    try:
        await bot.send_photo(chat_id, FSInputFile(image_path), caption=caption)
    except Exception as e:
        logger.error("task.send_photo_failed task_id=%s reason=%s", task["id"], e)
        # На случай если бот не смог отправить фото — переходим к следующей
        await state.update_data(task_index=idx + 1, task_attempts=0)
        await _send_next_task(chat_id, state)


async def _finish_task_session(chat_id: int, state: FSMContext):
    data = await state.get_data()
    correct = data.get("task_correct_count", 0)
    coins = data.get("task_coins_earned", 0)
    total = len(data.get("task_questions", []))
    subject_label = data.get("task_subject_label", "")
    logger.info(
        "task.session.complete user_id=%s subject=%s correct=%s total=%s coins=%s",
        data.get("task_user_id"), data.get("task_subject_id"),
        correct, total, coins,
    )
    await bot.send_message(
        chat_id,
        f"🎉 Готово! {subject_label}\n"
        f"Решено: {correct} из {total}\n"
        f"🪙 Заработано: {coins} монет",
        reply_markup=get_study_keyboard(),
    )
    await state.clear()


@router.message(QuizStates.answering_task, F.text == "🛑 Завершить")
async def handle_task_stop(message: Message, state: FSMContext):
    data = await state.get_data()
    correct = data.get("task_correct_count", 0)
    coins = data.get("task_coins_earned", 0)
    idx = data.get("task_index", 0)
    total = len(data.get("task_questions", []))
    logger.info(
        "task.session.stop user_id=%s subject=%s answered=%s/%s correct=%s coins=%s",
        message.from_user.id, data.get("task_subject_id"),
        idx, total, correct, coins,
    )
    await message.answer(
        f"⏹ Задачи остановлены.\n"
        f"Решено: {idx}/{total} (правильных: {correct})\n"
        f"🪙 Получено: {coins} монет",
        reply_markup=get_study_keyboard(),
    )
    await state.clear()


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
    user_norm = _normalize_task_answer(text)
    accepted_norm = {_normalize_task_answer(a) for a in task.get("accepted", [])}
    attempts = data.get("task_attempts", 0)
    user_id = message.from_user.id

    if user_norm in accepted_norm:
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
        await _send_next_task(message.chat.id, state)
        return

    new_attempts = attempts + 1
    if new_attempts < MAX_TASK_ATTEMPTS:
        remaining = MAX_TASK_ATTEMPTS - new_attempts
        await state.update_data(task_attempts=new_attempts)
        logger.info(
            "task.answered user_id=%s task_id=%s attempts=%s result=wrong remaining=%s",
            user_id, task["id"], new_attempts, remaining,
        )
        await message.answer(
            f"❌ Неверно. Попробуй ещё (осталось попыток: {remaining})."
        )
        return

    # 3-я неверная — открываем решение
    subject_id = data.get("task_subject_id", "")
    tasks_dir = STUDY_MATERIALS_PATH / subject_id / "tasks"
    solution_path = tasks_dir / task.get("solution_filename", f"{task['id']}-solution.png")
    correct_answer = task["accepted"][0] if task.get("accepted") else "(нет данных)"
    # Per-task tracking: задача НЕ решена (3 неверных, показали решение)
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
    if solution_path.exists():
        try:
            await bot.send_photo(
                message.chat.id,
                FSInputFile(solution_path),
                caption=(
                    f"💡 Решение:\n"
                    f"Правильный ответ: {correct_answer}\n"
                    f"Монеты за эту задачу: 0 🪙"
                ),
            )
        except Exception as e:
            logger.error("task.send_solution_failed task_id=%s reason=%s", task["id"], e)
            await message.answer(
                f"💡 Правильный ответ: {correct_answer}\nМонеты за эту задачу: 0 🪙"
            )
    else:
        await message.answer(
            f"💡 Правильный ответ: {correct_answer}\n"
            f"(Изображение решения не найдено)\n"
            f"Монеты за эту задачу: 0 🪙"
        )
    await state.update_data(task_index=idx + 1, task_attempts=0)
    await asyncio.sleep(1.0)
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
    user_id = message.from_user.id
    settings = await user_repo.get_notification_settings(user_id) or {}
    source = settings.get("flashcard_source", "mix")
    cards = await load_flashcards_for_study(user_id, subject_id, source)
    if not cards:
        source_label = FLASHCARD_SOURCE_LABELS.get(source, source)
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
            reply_markup=await get_mode_keyboard_for_subject(subject_id, user_id),
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
            reply_markup=get_study_keyboard(),
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
        reply_markup=get_flash_active_keyboard(),
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
        f"🃏 Карточка #{reviewed + 1}\n\n<b>{card['term']}</b>",
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
    if reviewed == 0:
        msg = "🎉 Все карточки уже проработаны. Возвращайся позже!"
    else:
        msg = (
            f"🎉 Сессия завершена!\n{subject_label}\n"
            f"Просмотрено карточек: {reviewed}\n"
            f"🪙 Заработано: {coins} монет"
        )
    await bot.send_message(chat_id, msg, reply_markup=get_study_keyboard())
    await state.clear()


@router.message(QuizStates.answering_flash, F.text == "🛑 Завершить")
async def handle_flashcard_stop(message: Message, state: FSMContext):
    data = await state.get_data()
    reviewed = data.get("flash_reviewed_count", 0)
    coins = data.get("flash_coins_earned", 0)
    logger.info(
        "flash.session.stop user_id=%s subject=%s reviewed=%s coins=%s",
        message.from_user.id, data.get("flash_subject_id"), reviewed, coins,
    )
    await message.answer(
        f"⏹ Сессия флэш-карт остановлена.\n"
        f"Просмотрено: {reviewed}\n"
        f"🪙 Получено: {coins} монет",
        reply_markup=get_study_keyboard(),
    )
    await state.clear()


@router.callback_query(F.data.startswith("flash:show:"))
async def handle_flashcard_show(callback: CallbackQuery, state: FSMContext):
    """Тап «💡 Показать ответ» — открывает определение + 3-кнопочный рейтинг."""
    current_state = await state.get_state()
    if current_state != QuizStates.answering_flash.state:
        await callback.answer("Сессия завершена", show_alert=False)
        return
    card_hash = callback.data.split(":", 2)[2]
    data = await state.get_data()
    card = data.get("flash_cards_by_hash", {}).get(card_hash)
    if not card:
        await callback.answer("Карточка не найдена", show_alert=True)
        return

    kb = InlineKeyboardBuilder()
    for label in ("❌ Не знал", "😐 Сложно", "✅ Легко"):
        kb.button(text=label, callback_data=f"flash:rate:{card_hash}:{FLASH_QUALITY_BY_LABEL[label]}")
    kb.adjust(3)

    reviewed = data.get("flash_reviewed_count", 0)
    new_text = (
        f"🃏 Карточка #{reviewed + 1}\n\n"
        f"<b>{card['term']}</b>\n\n"
        f"💡 <i>{card['definition']}</i>\n\n"
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
        await callback.answer("Сессия завершена", show_alert=False)
        return
    try:
        _, _, card_hash, quality_str = callback.data.split(":", 3)
        quality = int(quality_str)
    except (ValueError, IndexError):
        await callback.answer("Сломанный callback", show_alert=True)
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
@router.message(QuizStates.choosing_section, F.text.in_([label for label, _ in QUIZ_SECTIONS]))
async def handle_quiz_section(message: Message, state: FSMContext):
    section_map = {label: key for label, key in QUIZ_SECTIONS}
    section_key = section_map[message.text]
    data = await state.get_data()
    subject_id = data.get("subject_id", "industrial-management")
    terms = load_quiz_section(section_key, subject_id)
    if not terms:
        await message.answer("Раздел не найден или пуст.", reply_markup=get_quiz_section_keyboard())
        return
    user_id = message.from_user.id
    await subject_stats_repo.bump_visit(user_id, subject_id)
    next_term = await get_next_quiz_term(user_id, terms)
    if not next_term:
        await message.answer("🎉 Все термины раздела повторены! Отличная работа! 🏆", reply_markup=get_quiz_section_keyboard())
        return
    await state.update_data(
        current_term=next_term.to_dict(),
        section=section_key,
        section_name=message.text
    )
    await state.set_state(QuizStates.answering)
    await message.answer(
        f"✏️ Раздел: {message.text}\n\n"
        f"Напиши ДОСЛОВНОЕ определение термина:\n"
        f"«{next_term.term}»",
        reply_markup=get_quiz_answer_keyboard()
    )

@router.message(QuizStates.answering, F.text == "🛑 Завершить квиз")
async def handle_quiz_exit(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("✅ Квиз завершён. Прогресс сохранён.", reply_markup=get_study_keyboard())

@router.message(QuizStates.answering)
async def handle_quiz_answer(message: Message, state: FSMContext):
    data = await state.get_data()
    term = data.get("current_term")
    if not term:
        await message.answer("Ошибка, начните заново.", reply_markup=get_quiz_section_keyboard())
        await state.clear()
        return
    is_correct, feedback = check_text_answer(message.text, term["definition"], term["keywords"])
    user_id = message.from_user.id
    progress = await get_quiz_progress(user_id, term["hash"])
    streak = progress["streak"]
    if is_correct:
        streak += 1
        feedback += f"\n\n🔥 Термин будет повторён через {quiz_interval_days(streak)} дн."
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

    terms = load_quiz_section(data["section"])
    next_term = await get_next_quiz_term(user_id, terms)
    if next_term:
        await state.update_data(current_term=next_term.to_dict())
        await message.answer(
            f"✏️ Раздел: {data['section_name']}\n\n"
            f"Напиши ДОСЛОВНОЕ определение термина:\n"
            f"«{next_term.term}»",
            reply_markup=get_quiz_answer_keyboard()
        )
    else:
        await message.answer("🎉 Все термины раздела повторены! Ты молодец!", reply_markup=get_quiz_section_keyboard())
        await state.clear()

# ------------------------------------------------------------
# Советы
# ------------------------------------------------------------
def _format_tip_message(
    category: str,
    tip: dict,
    *,
    page: int | None = None,
    total: int | None = None,
) -> str:
    """HTML: жирный заголовок, тело, строка «Попробуй сегодня»."""
    meta = TIP_CATEGORIES[category]
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
        lines.extend(["", f"💡 <i>Попробуй сегодня:</i> {html_escape(action)}"])
    return "\n".join(lines)


def _tips_inline_keyboard(
    category: str,
    *,
    list_page: int | None = None,
    list_total: int | None = None,
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if list_page is None:
        kb.button(text="🔄 Ещё совет", callback_data=f"tips:more:{category}")
        kb.button(text="📋 Все советы", callback_data=f"tips:list:{category}:0")
        kb.button(text="⬅️ К категориям", callback_data="tips:menu")
        kb.adjust(2, 1)
    else:
        if list_page > 0:
            kb.button(text="◀️", callback_data=f"tips:list:{category}:{list_page - 1}")
        kb.button(text="🔄 Случайный", callback_data=f"tips:more:{category}")
        if list_total and list_page < list_total - 1:
            kb.button(text="▶️", callback_data=f"tips:list:{category}:{list_page + 1}")
        kb.button(text="⬅️ К категориям", callback_data="tips:menu")
        kb.adjust(3, 1)
    return kb.as_markup()


def _productivity_links_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for link in PRODUCTIVITY_LINKS:
        title = link["title"]
        label = title if len(title) <= 64 else f"{title[:61]}…"
        kb.button(text=label, url=link["url"])
    kb.button(text="⬅️ К категориям", callback_data="tips:menu")
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


def _all_tips_flat() -> list[dict]:
    out: list[dict] = []
    for meta in TIP_CATEGORIES.values():
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


async def _pick_tip(user_id: int, category: str) -> dict | None:
    """Совет с учётом cooldown (7 дн.) и контекста (таймер, стрик, карточки)."""
    tips = TIP_CATEGORIES.get(category, {}).get("tips", [])
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
    user = await user_repo.get_user(user_id)
    local_date = _user_local_date_str(user)
    tip = await tips_repo.resolve_tip_of_day(user_id, local_date, _all_tips_flat())
    if not tip:
        return ""
    cat_key = _category_key_from_tip_id(tip["id"])
    if cat_key not in TIP_CATEGORIES:
        cat_key = "tm"
    body = _format_tip_message(cat_key, tip)
    return f"\n\n———\n🌟 <b>Совет дня</b>\n\n{body}"


async def _on_tip_viewed(user_id: int, category: str, tip_id: str | None = None) -> str:
    """Монета за первый совет дня, ачивка за 10 советов, событие tip_viewed."""
    if category not in TIP_CATEGORIES:
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
        lines.append(f"\n\n+{TIP_COIN_PER_DAY} 🪙 за совет сегодня")
    if new_ach:
        tip_reward = ACHIEVEMENTS.get("10_tips_read", {}).get("reward", 30)
        lines.append(f"\n🏆 Достижение «Любознательный» — +{tip_reward} 🪙")
    elif total_views < 10:
        lines.append(f"\n\n📊 Прочитано советов: {total_views}/10")
    return "".join(lines)


async def _send_random_tip(message: Message, category: str) -> None:
    tip = await _pick_tip(message.from_user.id, category)
    if not tip:
        await message.answer("Советы пока не загружены.")
        return
    suffix = await _on_tip_viewed(message.from_user.id, category, tip.get("id"))
    await message.answer(
        _format_tip_message(category, tip) + suffix,
        reply_markup=_tips_inline_keyboard(category),
        parse_mode="HTML",
    )


async def _edit_or_send_tip(
    callback: CallbackQuery,
    category: str,
    tip: dict,
    *,
    page: int | None = None,
) -> None:
    tips = TIP_CATEGORIES[category]["tips"]
    total = len(tips)
    suffix = await _on_tip_viewed(callback.from_user.id, category, tip.get("id"))
    body = _format_tip_message(
        category, tip, page=page, total=total if page is not None else None,
    ) + suffix
    markup = _tips_inline_keyboard(
        category,
        list_page=page,
        list_total=total if page is not None else None,
    )
    try:
        await callback.message.edit_text(body, reply_markup=markup, parse_mode="HTML")
    except Exception:
        await callback.message.answer(body, reply_markup=markup, parse_mode="HTML")


@router.message(F.text == "🎓 Советы для продуктивности")
async def handle_tips_menu(message: Message):
    await message.answer("📚 Выберите категорию:", reply_markup=get_tips_keyboard())


@router.message(F.text == "⏰ Тайм-менеджмент")
async def handle_time_management(message: Message):
    await _send_random_tip(message, "tm")


@router.message(F.text == "🧠 Техники запоминания")
async def handle_memory_retention(message: Message):
    await _send_random_tip(message, "mem")


@router.message(F.text == "🎯 Как пользоваться ботом")
async def handle_bot_guide_tips(message: Message):
    await _send_random_tip(message, "bot")


@router.message(F.text == "🔗 Ссылки на статьи и книги")
async def handle_links(message: Message):
    if not PRODUCTIVITY_LINKS:
        await message.answer("Файл со ссылками пуст.")
        return
    await message.answer(
        "📚 Полезные материалы — нажми кнопку, чтобы открыть:",
        reply_markup=_productivity_links_keyboard(),
    )


@router.callback_query(F.data.startswith("tips:more:"))
async def handle_tips_more(callback: CallbackQuery):
    category = callback.data.split(":", 2)[2]
    if category not in TIP_CATEGORIES:
        await callback.answer("Неизвестная категория", show_alert=True)
        return
    tips = TIP_CATEGORIES[category]["tips"]
    if not tips:
        await callback.answer("Советы пусты", show_alert=True)
        return
    await callback.answer()
    tip = await _pick_tip(callback.from_user.id, category)
    if not tip:
        await callback.answer("Советы пусты", show_alert=True)
        return
    await _edit_or_send_tip(callback, category, tip)


@router.callback_query(F.data.startswith("tips:list:"))
async def handle_tips_list(callback: CallbackQuery):
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
    if category not in TIP_CATEGORIES:
        await callback.answer("Неизвестная категория", show_alert=True)
        return
    tips = TIP_CATEGORIES[category]["tips"]
    if not tips:
        await callback.answer("Советы пусты", show_alert=True)
        return
    page = max(0, min(page, len(tips) - 1))
    await callback.answer()
    await _edit_or_send_tip(callback, category, tips[page], page=page)


@router.callback_query(F.data == "tips:menu")
async def handle_tips_menu_callback(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer("📚 Выберите категорию:", reply_markup=get_tips_keyboard())

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
    try:
        user_id = int(user_id_str)
        await bot.send_message(user_id, f"📨 Ответ от администратора:\n\n{reply_text}")
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

    text = (command.args or "").strip()
    if not text:
        await message.answer(
            "❌ Использование: /broadcast <сообщение>\n"
            "Сообщение получат все зарегистрированные пользователи."
        )
        return

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
        delivered = 0
        failed = 0
        failed_ids: list[int] = []
        for uid in user_ids:
            try:
                await bot.send_message(uid, text)
                delivered += 1
            except TelegramForbiddenError:
                failed += 1
                failed_ids.append(uid)
                logger.info("broadcast.send_failed uid=%s reason=blocked", uid)
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
        zip_bytes, metadata = await analytics_service.export_all_tables_zip()
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
        await callback.answer("Только для админов", show_alert=True)
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
        await callback.answer("Неизвестная таблица", show_alert=True)
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

    log_file_path = Path(os.getenv("LOG_FILE", "bot.log"))
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
    try:
        text = await leaderboard_service.render_leaderboard(user_id)
        await message.answer(text, parse_mode="HTML")
        await event_repo.log(user_id, "leaderboard_viewed", {})
    except Exception as e:
        logger.warning(
            "leaderboard.render_failed user=%s err=%s detail=%s",
            user_id, type(e).__name__, e,
        )
        await message.answer("Не удалось загрузить лидерборд. Попробуй позже.")


# ============================================================
# Pet customization (TODO #16 Phase B): detail screen + picker UI
# ============================================================
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
        path = render_pet(pet, emotion)
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
        f"🐾 <b>{pet['name']}</b>\n\n"
        f"Уровень: <b>{pet['level']}</b>\n"
        f"XP: {pet['xp']}\n"
        f"Цвет: {pet['color']}  ·  Аксессуар: {pet['accessory']}\n"
        f"Эмоция сейчас: {emotion}\n\n"
        f"💰 Баланс: {user['total_coins']} 🪙"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="🎨 Цвета", callback_data=f"pet_colors:{user_id}")
    kb.button(text="🎁 Аксессуары", callback_data=f"pet_accessories:{user_id}")
    kb.button(text="✏️ Переименовать", callback_data=f"pet_rename:{user_id}")
    kb.button(text="◀️ Профиль", callback_data=f"pet_back_to_profile:{user_id}")
    kb.adjust(2, 1, 1)

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
    await callback.answer()
    await _render_picker(callback, "color")


@router.callback_query(F.data.startswith("pet_accessories:"))
async def pet_accessories(callback: CallbackQuery):
    await callback.answer()
    await _render_picker(callback, "accessory")


@router.callback_query(F.data.startswith("pet_locked:"))
async def pet_locked(callback: CallbackQuery):
    """Alert на нажатие locked / already-equipped кнопки."""
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
    inline_kb = InlineKeyboardBuilder()
    inline_kb.button(text="🏆 Достижения", callback_data=f"show_achievements:{user_id}:1")
    inline_kb.button(text="⚙️ Настройки", callback_data=f"settings_menu:{user_id}")
    inline_kb.button(text="📊 Прогресс по предметам", callback_data=f"show_progress:{user_id}")
    inline_kb.button(text="🐾 Питомец", callback_data=f"pet_menu:{user_id}")
    inline_kb.button(text="👥 Друзья", callback_data=f"friends_back:{user_id}")
    inline_kb.button(text="❄️ Заморозить стрик", callback_data=f"freeze_menu:{user_id}")
    inline_kb.adjust(2, 1, 1, 1, 1)
    await bot.send_message(
        callback.message.chat.id,
        f"📊 Твой профиль:\n"
        f"🆔 ID: {user_id}\n"
        f"📚 Всего сессий: {user['total_sessions']}\n"
        f"💰 Всего монет: {user['total_coins']} 🪙\n"
        f"🔥 Стрик: {user['current_streak']} дней подряд\n"
        f"⏱️ Последняя сессия: {user.get('last_session', 'никогда')}\n\n"
        f"🐾 Твой питомец: {get_pet_emotion(user['current_streak'])}",
        reply_markup=inline_kb.as_markup(),
    )


# ============================================================
# Friends system (Phase 4 / LEADERBOARD.md §Segments → Friends)
# ============================================================
def _friends_menu_keyboard(user_id: int, pending_count: int = 0) -> InlineKeyboardMarkup:
    """Inline-клавиатура для friends-tab: 3 действия + опциональный badge на pending."""
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить друга", callback_data=f"friend_add_start:{user_id}")
    pending_label = (
        f"📩 Запросы ({pending_count})" if pending_count > 0 else "📩 Запросы"
    )
    kb.button(text=pending_label, callback_data=f"friend_pending:{user_id}")
    kb.button(text="➖ Удалить друга", callback_data=f"friend_remove_list:{user_id}")
    kb.adjust(1)
    return kb.as_markup()


@router.message(Command("share_friend"))
async def cmd_share_friend(message: Message):
    """
    Создаёт invite-token и шлёт пользователю deep-link, которым тот
    может поделиться. Кто откроет ссылку — автоматически становится
    другом (skip pending). 30-day TTL, multiuse.
    """
    user_id = message.from_user.id
    if not await user_repo.user_exists(user_id):
        await message.answer("Сначала отправь /start.")
        return
    if not bot_username:
        await message.answer(
            "⚠️ Бот ещё не определил свой @username (не удалось получить "
            "его при старте). Попробуй позже."
        )
        return
    token = await friend_repo.create_invite_token(user_id)
    link = f"https://t.me/{bot_username}?start=friend_{token}"
    await message.answer(
        f"👥 <b>Поделись этой ссылкой с друзьями:</b>\n\n"
        f"<code>{link}</code>\n\n"
        f"Кто откроет ссылку — автоматически станет твоим другом 🎉\n"
        f"Срок действия: 30 дней.",
        parse_mode="HTML",
    )


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
    text_input = message.text or ""

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
                f"❌ Пользователь <code>@{username}</code> не найден.\n"
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
        await callback.answer("Запрос уже не активен.", show_alert=True)
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
        "ℹ️ Кнопки главного меню: 📚 Учеба, ❓ FAQ, 📊 Мой профиль, 📢 Новости"
    )
    await message.answer(text, parse_mode="HTML")


@router.message(F.text.in_(["📚 Учеба", "⬅️ Назад к учебе"]))
async def handle_back_to_study(message: Message):
    await message.answer("📖 Раздел учёбы:", reply_markup=get_study_keyboard())

@router.message(F.text == "🏠 Назад в меню")
async def handle_back_to_main(message: Message):
    await message.answer("🐾 Главное меню", reply_markup=get_main_keyboard())

@router.message()
async def handle_any_message(message: Message):
    user_id = message.from_user.id
    if is_admin(user_id):
        await message.answer("Используйте /reply для ответа пользователям.")
        return
    user_name = f"{message.from_user.first_name} {message.from_user.last_name or ''}".strip()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # Append-only JSONL: одна запись на строку.
    # Без read-modify-write — нет гонок, нет риска порчи при kill -9,
    # нет O(n) перезаписи на каждом сообщении.
    log_entry = {
        "timestamp": timestamp,
        "user_id": user_id,
        "user_name": user_name,
        "message_id": message.message_id,
        "text": message.text,
    }
    try:
        with open(MESSAGES_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.error(f"Не удалось записать сообщение пользователя в лог: {e}")
    for admin_id in ADMINS:
        try:
            await bot.send_message(admin_id, f"📩 Новое сообщение от {user_name} (ID: {user_id}):\n{message.text}")
        except Exception:
            pass
    await message.answer(
        "✅ Твое сообщение отправлено! Администратор ответит в ближайшее время.\n\n"
        "А пока можешь продолжить учиться — выбери Учеба в меню ниже 👇",
        reply_markup=get_main_keyboard()
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
    """
    from fsm_storage import _loads  # локальный импорт чтобы не загромождать вершину
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
            duration = data.get("duration", 25)
            if not isinstance(start_time, datetime):
                logger.warning("fsm.broken_state key=%s reason=no_start_time", key)
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

            elapsed = (datetime.now() - start_time).total_seconds() / 60
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
                    msg = (
                        f"🎉 Таймер на {duration} мин завершился, пока бот был офлайн.\n"
                        f"🪙 Получено: {duration} монет"
                    )
                    if bonus > 0:
                        msg += f"\n✨ Бонус за достижения: +{bonus} монет"
                    msg += f"\n📊 Всего монет: {user['total_coins']}"
                    await bot.send_message(chat_id, msg, reply_markup=get_main_keyboard())
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
                    await bot.send_message(
                        chat_id,
                        f"♻️ Бот перезапустился — но твой таймер продолжается!\n"
                        f"⏱️ Осталось: {remaining_min} мин",
                        reply_markup=get_timer_active_keyboard(),
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
    global db, user_repo, session_repo, admin_repo, flashcard_repo, user_flashcard_repo, mcq_repo, task_repo, subject_stats_repo, event_repo, tips_repo, pet_repo, leaderboard_repo, friend_repo, ach_service, study_service, streak_service, backup_service, analytics_service, leaderboard_service, rate_limiter, bot, dp, bot_username
    db = await get_db()
    await init_db(db)
    user_repo = UserRepository(db)
    session_repo = SessionRepository(db)
    admin_repo = AdminRepository(db)
    flashcard_repo = FlashcardRepository(db)
    user_flashcard_repo = UserFlashcardRepository(db)
    mcq_repo = McqProgressRepository(db)
    task_repo = TaskProgressRepository(db)
    subject_stats_repo = SubjectStatsRepository(db)
    event_repo = EventRepository(db)
    tips_repo = TipsRepository(db)
    pet_repo = PetRepository(db)
    leaderboard_repo = LeaderboardRepository(db)
    friend_repo = FriendRepository(db)
    ach_service = AchievementService(user_repo, ACHIEVEMENTS)
    bot = Bot(token=BOT_TOKEN)
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
    rl_middleware = RateLimitMiddleware(rate_limiter)
    # Username sync — обновляем users.username из event_from_user.username
    # перед всеми handler'ами. Регистрируем ДО rl_middleware, потому что
    # rate-limit может silently drop event (return None), а username хотим
    # обновить ВСЕГДА, пока юзер активен.
    username_sync = UsernameSyncMiddleware(user_repo)
    dp.message.middleware(username_sync)
    dp.callback_query.middleware(username_sync)
    dp.message.middleware(rl_middleware)
    dp.callback_query.middleware(rl_middleware)
    dp.include_router(router)
    streak_service = StreakService(user_repo, bot, leaderboard_repo=leaderboard_repo)
    reminder_service = ReminderService(
        user_repo, bot,
        morning_tip_builder=build_morning_tip_block,
        event_repo=event_repo,
    )
    # Backup сервис: snapshot БД раз в сутки после streak processing.
    # BACKUP_DIR/BACKUP_RETENTION_DAYS можно переопределить в .env;
    # в Docker — указываются в docker-compose чтобы лежали на mounted /data.
    backup_service = BackupService(
        db_path=os.getenv("DB_PATH", "studybuddy.db"),
        backup_dir=os.getenv("BACKUP_DIR", "backups"),
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
    background_tasks = [
        asyncio.create_task(streak_scheduler(streak_service, user_repo, backup_service)),
        asyncio.create_task(reminder_scheduler(reminder_service, user_repo)),
        # Weekly leaderboard rollover (UTC Tuesday 00:00 anchor). См. LEADERBOARD.md §Rewards.
        asyncio.create_task(leaderboard_scheduler(leaderboard_service)),
    ]
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

    logger.info("✅ Palph запущен")
    try:
        await dp.start_polling(bot)
    finally:
        logger.info("app.shutdown")
        for t in background_tasks:
            t.cancel()

if __name__ == "__main__":
    asyncio.run(main())