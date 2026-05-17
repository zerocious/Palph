# bot.py
import asyncio
import json
import logging
import os
import re
import random
import hashlib
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytz
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, Router, F
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
    BotCommand, BotCommandScopeDefault, BotCommandScopeChat,
    FSInputFile,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from fsm_storage import SQLiteStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import StorageKey

from db import get_db, init_db
from repository import (
    UserRepository, SessionRepository, AdminRepository, FlashcardRepository,
    McqProgressRepository, TaskProgressRepository, SubjectStatsRepository,
)
from services import AchievementService, StudyService, StreakService, ReminderService, BackupService, sm2_update
from tasks import streak_scheduler, reminder_scheduler

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
mcq_repo: McqProgressRepository = None
task_repo: TaskProgressRepository = None
subject_stats_repo: SubjectStatsRepository = None
ach_service: AchievementService = None
study_service: StudyService = None
streak_service: StreakService = None
backup_service: BackupService = None
bot: Bot = None
dp: Dispatcher = None

# Активные таймеры: user_id -> asyncio.Task
# Держим строгие ссылки, чтобы задачи не были собраны GC,
# и чтобы их можно было отменить при остановке/перезапуске.
active_timers: dict[int, asyncio.Task] = {}

# ------------------------------------------------------------
# Состояния FSM
# ------------------------------------------------------------
class TimerStates(StatesGroup):
    waiting_for_duration = State()
    active = State()

class QuizStates(StatesGroup):
    # Новый mode-picker flow (введён в v0.7 #13):
    #   choosing_mode    — пользователь выбирает режим (situational/MCQ/...)
    #   choosing_subject — выбирает предмет (фильтр по subjects_with_mode)
    # Существующий situational flow:
    #   choosing_section — Раздел I/II/III/IV для ОПМ
    #   answering        — open-text ответ на ситуационный вопрос
    # MCQ flow (v0.7 #13):
    #   answering_mcq    — пользователь тапает inline-кнопки с вариантами
    choosing_mode = State()
    choosing_subject = State()
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


class SettingsStates(StatesGroup):
    # Универсальное состояние для ввода времени (утро/вечер).
    # Слот хранится в FSM data: {"slot": "morning" | "evening"}.
    waiting_for_time = State()


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
]
ADMIN_COMMANDS = DEFAULT_COMMANDS + [
    BotCommand(command="help", description="Справка по командам (для админов)"),
    BotCommand(command="reply", description="Ответ пользователю по ID"),
    BotCommand(command="broadcast", description="Рассылка всем"),
    BotCommand(command="notif_status", description="Диагностика уведомлений"),
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
    """Mode-picker: показывает только режимы с непустым контентом."""
    builder = ReplyKeyboardBuilder()
    for _, label in available_modes_global():
        builder.button(text=label)
    builder.button(text="⬅️ Назад к учебе")
    builder.adjust(1)
    return builder.as_markup(resize_keyboard=True)


def get_subject_keyboard_for_mode(mode_id: str) -> ReplyKeyboardMarkup:
    """Subject-picker для конкретного режима: только предметы с контентом."""
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


def available_modes(subject_id: str) -> list[tuple[str, str]]:
    """
    Возвращает режимы, у которых для данного предмета есть непустой контент.
    UI-слой строит меню режимов по этому списку — пустые режимы автоматически
    скрываются (как и пустые секции в available_quiz_sections).
    """
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


def available_subjects() -> list[tuple[str, str]]:
    """
    Возвращает только предметы, у которых есть хотя бы один непустой режим.
    Так пользователь не видит «mock»-кнопок предметов без контента.
    """
    return [(sid, label) for sid, label in SUBJECTS if available_modes(sid)]


def subjects_with_mode(mode_id: str) -> list[tuple[str, str]]:
    """Предметы, у которых есть контент для конкретного режима."""
    return [
        (sid, label)
        for sid, label in SUBJECTS
        if any(m[0] == mode_id for m in available_modes(sid))
    ]


def available_modes_global() -> list[tuple[str, str]]:
    """Режимы, у которых есть контент хотя бы для одного предмета."""
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
    if not await user_repo.user_exists(user_id):
        await user_repo.create_user(user_id)
        logger.info("user.registered user_id=%s", user_id)
        keyboard = ReplyKeyboardBuilder()
        keyboard.button(text="🔧 Настроить сейчас")
        keyboard.button(text="🚀 Начать сразу")
        keyboard.adjust(1)
        await message.answer(
            "🐾 Привет! Я — StudyBuddy, твой цифровой питомец для учёбы!\n\n"
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
        "id": "efficiency",
        "btn":   "1️⃣ Эффективность учёбы с ботом",
        "title": "1️⃣ Почему учиться с ботом эффективнее, чем самому?",
        "body": (
            "Бот объединяет несколько научно доказанных техник в один цикл: "
            "метод Помодоро (25-минутные сессии = меньше выгорания), "
            "мгновенная мотивация (монеты, достижения, эмоции питомца), "
            "регулярные напоминания и квизы с интервальным повторением. "
            "Ты получаешь структуру и обратную связь, которые в одиночку легко терять."
        ),
    },
    {
        "id": "pet",
        "btn":   "2️⃣ Зачем питомец",
        "title": "2️⃣ Зачем нужен питомец и как он помогает учиться?",
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
        "btn":   "3️⃣ На что тратить монеты",
        "title": "3️⃣ На что можно тратить монеты?",
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
        "btn":   "4️⃣ Как зарабатывать монеты",
        "title": "4️⃣ Как зарабатывать монеты?",
        "body": (
            "• +1 монета за каждую минуту учёбы через таймер\n"
            "• +15 бонусом, когда стрик достигает 2+ дней подряд\n"
            "• Бонусные монеты за получение достижений (список — в профиле)\n"
            "• +1 монета за каждый правильный MCQ-ответ\n"
            "• +1 монета за каждую просмотренную флэш-карту\n"
            "• До +3 монет за решение задачи с картинкой (зависит от попытки)"
        ),
    },
    {
        "id": "sm2",
        "btn":   "5️⃣ SM-2 для флэш-карт",
        "title": "5️⃣ Что такое SM-2 и почему это эффективно для флэш-карт?",
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
        "btn":   "6️⃣ Интервальное повторение",
        "title": "6️⃣ Что такое интервальное повторение?",
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
        "btn":   "7️⃣ Active recall в боте",
        "title": "7️⃣ Что такое active recall и какие методы есть в боте?",
        "body": (
            "Active recall — это извлечение информации из памяти «из головы», без "
            "подглядывания. Работает в 2–3 раза лучше, чем повторное чтение. В боте "
            "active recall встроен в каждый учебный режим:\n"
            "• Ситуационные квизы — вводишь определение по описанию ситуации\n"
            "• Флэш-карты — видишь термин, вспоминаешь, проверяешь себя\n"
            "• Тесты с выбором ответа — выбираешь правильный из 4 вариантов\n"
            "• Задачи с картинкой — решаешь и вводишь ответ\n"
            "Принцип: «если можешь объяснить — значит знаешь»."
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
    """Главное меню FAQ — 7 кнопок-вопросов + 1 кнопка техподдержки."""
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
    inline_kb.adjust(2, 1)
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
    mcq_qs = load_mcq(subject_id)
    tasks_list = load_tasks(subject_id)

    card_hashes = [c["hash"] for c in cards]
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
            "streak_enabled": 1, "achievements_enabled": 1
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
        lines.append(f"\n🌍 Часовой пояс: {tz_label(tz)}")
        return "\n".join(lines)

    async def get_keyboard(self) -> InlineKeyboardMarkup:
        settings = await self.load()
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
        kb.button(text="⬅️ Назад в профиль", callback_data=f"back_to_profile:{self.user_id}")
        kb.adjust(2, 2, 2, 1, 1)
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
        _, status_text = await ns.toggle(setting_type)
        await callback.message.edit_text(
            await ns.get_display_text(),
            reply_markup=await ns.get_keyboard()
        )
    except Exception as e:
        logger.error(f"Error toggling setting: {e}")
        await callback.answer("Ошибка переключения", show_alert=True)


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
    inline_kb.adjust(2, 1)
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
        await user_repo.create_user(user_id)
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
        await user_repo.create_user(user_id)
    await state.set_state(TimerStates.active)
    await state.update_data(duration=duration, start_time=datetime.now())
    await message.answer(
        f"⏱️ Таймер запущен на {duration} минут!\n"
        f"Ваш питомец ждёт вашего возвращения 🐾",
        reply_markup=get_timer_active_keyboard()
    )
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
# Квизы — общий flow: ❓ Квизы → mode picker → subject picker → режим
# ------------------------------------------------------------
@router.message(F.text == "❓ Квизы")
async def handle_quiz_menu(message: Message, state: FSMContext):
    modes = available_modes_global()
    if not modes:
        await message.answer(
            "🚧 Пока нет учебных материалов. Загляни позже!",
            reply_markup=get_study_keyboard(),
        )
        return
    await state.set_state(QuizStates.choosing_mode)
    await message.answer(
        "📖 Выбери режим учёбы:",
        reply_markup=get_mode_keyboard(),
    )


@router.message(QuizStates.choosing_mode, F.text == "⬅️ Назад к учебе")
async def handle_mode_back(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("📖 Раздел учёбы:", reply_markup=get_study_keyboard())


@router.message(QuizStates.choosing_mode, F.text.in_([label for _, label in STUDY_MODES]))
async def handle_mode_picked(message: Message, state: FSMContext):
    mode_id = next((mid for mid, label in STUDY_MODES if label == message.text), None)
    if not mode_id:
        return
    subjects = subjects_with_mode(mode_id)
    if not subjects:
        await message.answer(
            f"🚧 «{message.text}» — пока нет контента ни для одного предмета.",
            reply_markup=get_mode_keyboard(),
        )
        return
    await state.update_data(mode_id=mode_id, mode_label=message.text)
    await state.set_state(QuizStates.choosing_subject)
    await message.answer(
        f"{message.text}\nВыбери предмет:",
        reply_markup=get_subject_keyboard_for_mode(mode_id),
    )


@router.message(QuizStates.choosing_subject, F.text == "⬅️ Назад к режимам")
async def handle_subject_back(message: Message, state: FSMContext):
    await state.set_state(QuizStates.choosing_mode)
    await message.answer("📖 Выбери режим учёбы:", reply_markup=get_mode_keyboard())


@router.message(QuizStates.choosing_subject, F.text.in_([label for _, label in SUBJECTS]))
async def handle_subject_picked(message: Message, state: FSMContext):
    subject_id = next((sid for sid, label in SUBJECTS if label == message.text), None)
    if not subject_id:
        return
    data = await state.get_data()
    mode_id = data.get("mode_id")
    await state.update_data(subject_id=subject_id, subject_label=message.text)

    if mode_id == "situational":
        # Существующий flow: показать раздельный picker
        await state.set_state(QuizStates.choosing_section)
        await message.answer(
            f"📚 {message.text}\nВыбери раздел:",
            reply_markup=get_quiz_section_keyboard(),
        )
    elif mode_id == "mcq":
        await start_mcq_session(message, state, subject_id, subject_label=message.text)
    elif mode_id == "tasks":
        await start_task_session(message, state, subject_id, subject_label=message.text)
    elif mode_id == "flashcards":
        await start_flashcard_session(message, state, subject_id, subject_label=message.text)
    else:
        # На всякий случай — не должно сюда попадать
        await message.answer(
            "Что-то пошло не так. Возвращаемся к режимам.",
            reply_markup=get_mode_keyboard(),
        )
        await state.set_state(QuizStates.choosing_mode)


# ============================================================
# MCQ flow (Multiple Choice Quiz, #13)
# ============================================================
async def start_mcq_session(message: Message, state: FSMContext, subject_id: str, subject_label: str):
    questions = load_mcq(subject_id)
    if not questions:
        # subjects_with_mode уже фильтрует; это страховка на случай гонки с файлами
        await message.answer(
            "🚧 Для этого предмета пока нет MCQ-вопросов.",
            reply_markup=get_mode_keyboard(),
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
    if is_correct:
        await user_repo.add_coins(user_id, 1)
        feedback = "✅ Верно! +1 🪙"
        await state.update_data(mcq_correct_count=data.get("mcq_correct_count", 0) + 1)
    else:
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
    if not tasks:
        # subjects_with_mode уже фильтрует; страховка на случай гонки
        await message.answer(
            "🚧 Для этого предмета пока нет задач.",
            reply_markup=get_mode_keyboard(),
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
    cards = load_flashcards(subject_id)
    if not cards:
        await message.answer(
            "🚧 Для этого предмета пока нет флэш-карт.",
            reply_markup=get_mode_keyboard(),
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

    # Текущее состояние карты в БД (или дефолты для новой)
    progress = await flashcard_repo.get_progress(user_id, card_hash)
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
    else:
        streak = 0
    await update_quiz_progress(user_id, term["hash"], is_correct, streak)
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
@router.message(F.text == "🎓 Советы для продуктивности")
async def handle_tips_menu(message: Message):
    await message.answer("📚 Выберите категорию:", reply_markup=get_tips_keyboard())

@router.message(F.text == "⏰ Тайм-менеджмент")
async def handle_time_management(message: Message):
    try:
        with open("timemanagement.txt", "r", encoding="utf-8") as f:
            tips = [line.strip() for line in f if line.strip()]
        await message.answer(f"⏰ Совет:\n\n{random.choice(tips)}")
    except:
        await message.answer("Файл с советами не найден.")

@router.message(F.text == "🧠 Техники запоминания")
async def handle_memory_retention(message: Message):
    try:
        with open("memoryretention.txt", "r", encoding="utf-8") as f:
            tips = [line.strip() for line in f if line.strip()]
        await message.answer(f"🧠 Совет:\n\n{random.choice(tips)}")
    except:
        await message.answer("Файл с советами не найден.")

@router.message(F.text == "🔗 Ссылки на статьи и книги")
async def handle_links(message: Message):
    try:
        with open("links-to-productivity-material.txt", "r", encoding="utf-8") as f:
            content = f.read().strip()
        await message.answer(f"📚 Полезные материалы:\n\n{content}")
    except:
        await message.answer("Файл со ссылками не найден.")

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
        "🛠 Админские команды:\n"
        "/reply <user_id> <текст> — ответ пользователю по ID\n"
        "/broadcast <текст> — рассылка всем зарегистрированным\n"
        "/notif_status — диагностика уведомлений (TZ, расписание, попадаешь ли ты в текущую выборку)\n"
        "/help — эта справка\n"
    )
    if is_main:
        text += (
            "\n👑 Команды главного админа:\n"
            "/addadmin <user_id> — добавить нового админа\n"
            "/rmadmin <user_id> — удалить админа\n"
            "/listadmins — список всех админов\n"
            "/backup — принудительный snapshot БД (daily backup автоматически после стриков)\n"
        )
    text += (
        "\n📚 Общие команды (доступны всем):\n"
        "/start — регистрация / возврат в главное меню\n"
        "/stop — остановить активный таймер досрочно\n"
        "/cancel — отменить ввод (например, времени напоминания)\n"
        "/skip — пропустить шаг в мастере настройки уведомлений\n\n"
        "ℹ️ Кнопки главного меню: 📚 Учеба, ❓ FAQ, 📊 Мой профиль, 📢 Новости"
    )
    await message.answer(text)


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
    global db, user_repo, session_repo, admin_repo, flashcard_repo, mcq_repo, task_repo, subject_stats_repo, ach_service, study_service, streak_service, backup_service, bot, dp
    db = await get_db()
    await init_db(db)
    user_repo = UserRepository(db)
    session_repo = SessionRepository(db)
    admin_repo = AdminRepository(db)
    flashcard_repo = FlashcardRepository(db)
    mcq_repo = McqProgressRepository(db)
    task_repo = TaskProgressRepository(db)
    subject_stats_repo = SubjectStatsRepository(db)
    ach_service = AchievementService(user_repo, ACHIEVEMENTS)
    study_service = StudyService(user_repo, session_repo, ach_service)
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=SQLiteStorage(db))
    dp.include_router(router)
    streak_service = StreakService(user_repo, bot)
    reminder_service = ReminderService(user_repo, bot)
    # Backup сервис: snapshot БД раз в сутки после streak processing.
    # BACKUP_DIR/BACKUP_RETENTION_DAYS можно переопределить в .env;
    # в Docker — указываются в docker-compose чтобы лежали на mounted /data.
    backup_service = BackupService(
        db_path=os.getenv("DB_PATH", "studybuddy.db"),
        backup_dir=os.getenv("BACKUP_DIR", "backups"),
        retention_days=int(os.getenv("BACKUP_RETENTION_DAYS", "30")),
    )

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
    ]
    logger.info(
        "app.start admins=%s main_admin_id=%s server_tz=%s log_level=%s",
        len(ADMINS), MAIN_ADMIN_ID, SERVER_TIMEZONE,
        os.getenv("LOG_LEVEL", "INFO").upper(),
    )
    logger.info("✅ StudyBuddy запущен")
    try:
        await dp.start_polling(bot)
    finally:
        logger.info("app.shutdown")
        for t in background_tasks:
            t.cancel()

if __name__ == "__main__":
    asyncio.run(main())