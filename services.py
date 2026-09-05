# services.py
import asyncio
import logging
import os
import threading
from collections import defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path
from time import monotonic

import aiosqlite

# Домен (services + repository) переиспользуется десктоп-клиентом, который
# ставится БЕЗ aiogram — тянуть в оффлайн-приложение aiogram+pydantic+
# magic-filter ради трёх классов исключений незачем. Когда aiogram есть
# (бот, тесты) — ловятся настоящие классы, поведение не меняется. Когда
# его нет, заглушки недостижимы: все три упоминаются только на путях
# отправки в Telegram, а они выполняются лишь при bot is not None.
# См. DESKTOP.md §2.1.
try:
    from aiogram.exceptions import (
        TelegramForbiddenError,
        TelegramRetryAfter,
        TelegramBadRequest,
    )
except ModuleNotFoundError:  # pragma: no cover - ветка десктоп-клиента
    class TelegramForbiddenError(Exception):
        """Заглушка: без aiogram отправки в Telegram не бывает."""

    class TelegramRetryAfter(Exception):
        """Заглушка: без aiogram отправки в Telegram не бывает."""

        retry_after = 1

    class TelegramBadRequest(Exception):
        """Заглушка: без aiogram отправки в Telegram не бывает."""

from repository import UserRepository, SessionRepository, PetRepository, LeaderboardRepository
from i18n import t, DEFAULT_LOCALE, SUPPORTED_LOCALES
from file_upload_security import (
    pet_emotion_file_stems,
    sanitize_pet_asset_keys,
    sanitize_pet_time_period,
)

logger = logging.getLogger("studybuddy_bot")

MAX_SEND_ATTEMPTS = 3
_TRANSIENT_SEND_ERRORS = (asyncio.TimeoutError, ConnectionError, OSError)
_TELEGRAM_SEND_SEM = asyncio.Semaphore(5)


class TelegramSendBreakerOpen(Exception):
    """Outbound Telegram sends paused after sustained failures."""


class TelegramSendBreaker:
    def __init__(self, failure_threshold: int = 10, cooldown_seconds: float = 60):
        self._failures = 0
        self._open_until = 0.0
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds

    def allow(self) -> bool:
        return monotonic() >= self._open_until

    def record_success(self):
        self._failures = 0

    def record_failure(self):
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._open_until = monotonic() + self.cooldown_seconds
            logger.error(
                "telegram.breaker_open cooldown=%s failures=%s",
                self.cooldown_seconds,
                self._failures,
            )


_telegram_send_breaker = TelegramSendBreaker()


async def _send_with_retry_after(send_callable, *, label: str, uid: int):
    """
    Запускает send_callable() (0-арг callable, возвращающий awaitable)
    с retry на TelegramRetryAfter и transient network errors.
    0-arg-callable shape нужен, потому что awaitables одноразовые.

    TelegramForbiddenError / TelegramBadRequest — пробрасываем сразу.
    """
    if not _telegram_send_breaker.allow():
        logger.warning("telegram.breaker_skip label=%s uid=%s", label, uid)
        raise TelegramSendBreakerOpen(f"Telegram send breaker open (label={label})")

    last_exc: Exception | None = None
    for attempt in range(MAX_SEND_ATTEMPTS):
        try:
            result = await send_callable()
            _telegram_send_breaker.record_success()
            return result
        except TelegramRetryAfter as e:
            last_exc = e
            logger.warning(
                "telegram.retry_after label=%s uid=%s seconds=%s attempt=%s",
                label, uid, e.retry_after, attempt + 1,
            )
            await asyncio.sleep(e.retry_after + 0.5)
        except _TRANSIENT_SEND_ERRORS as e:
            last_exc = e
            if attempt == MAX_SEND_ATTEMPTS - 1:
                _telegram_send_breaker.record_failure()
                raise
            logger.warning(
                "telegram.transient_error label=%s uid=%s reason=%s attempt=%s",
                label, uid, type(e).__name__, attempt + 1,
            )
            await asyncio.sleep(0.5 * (2 ** attempt))
        except Exception:
            raise

    _telegram_send_breaker.record_failure()
    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"telegram.send_exhausted label={label} uid={uid}")


async def send_with_telegram_bulkhead(send_callable, *, label: str, uid: int):
    """Bulk scheduler/broadcast sends: semaphore + unified retry helper."""
    async with _TELEGRAM_SEND_SEM:
        return await _send_with_retry_after(send_callable, label=label, uid=uid)


def _user_locale(locale: str | None) -> str:
    if locale in SUPPORTED_LOCALES:
        return locale
    return DEFAULT_LOCALE


# ------------------------------------------------------------
# UserRateLimiter — in-memory sliding-window лимитер per user_id.
# Защита от спама / abuse'а пользователями (которые проходят
# Telegram'овский global throttle, но всё ещё могут флудить
# бота сообщениями/тапами).
#
# Что НЕ покрывает:
#   - DDoS (для polling-бота нет публичного endpoint'а, attacker
#     не может туда добраться)
#   - Глобальный API-лимит Telegram'а (30 msg/s — мы туда не упрёмся
#     при разумных условиях)
# ------------------------------------------------------------
class UserRateLimiter:
    """
    Sliding-window rate-limit per user. Бакет = deque timestamps;
    при каждой проверке выкидываются протухшие. Не персистится —
    после рестарта бота все бакеты пусты, что для abuse-защиты
    приемлемо (атака начнётся заново и снова поймается).

    Trade-off threshold (default 30 actions / 60s):
      - Активный флэш-сеанс: ~1 тап / 5-10s = 6-12/мин ← OK
      - Спам / автоматика: 50+ actions/min ← блок

    Возвращает строковый статус:
      "ok"    — под threshold, action разрешён
      "warn"  — пользователь приближается к лимиту, отправить
                вежливое предупреждение (но action всё ещё разрешён)
      "block" — over hard limit, silently drop event
    """

    def __init__(
        self,
        max_actions: int = 30,
        window_seconds: int = 60,
        warn_threshold: float = 0.7,
        warn_cooldown_seconds: int = 30,
    ):
        self.max_actions = max_actions
        self.window_seconds = window_seconds
        self.warn_threshold = warn_threshold
        self.warn_cooldown_seconds = warn_cooldown_seconds
        # user_id → deque of monotonic timestamps
        self._buckets: dict[int, deque] = defaultdict(deque)
        # user_id → last warning sent (чтобы не спамить warnings)
        self._warned_at: dict[int, float] = {}
        self._lock = threading.Lock()

    def check(self, user_id: int) -> str:
        """Регистрирует event и возвращает ok/warn/block."""
        with self._lock:
            now = monotonic()
            cutoff = now - self.window_seconds
            bucket = self._buckets[user_id]
            # Чистим протухшие timestamps
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            current_count = len(bucket)

            if current_count >= self.max_actions:
                return "block"

            # Регистрируем новый event
            bucket.append(now)
            new_count = current_count + 1

            # Warn-зона: [warn_at, max_actions). Если warn_threshold=1.0, диапазон
            # пустой → warn'и выключены (для unit-тестов и тонкого тюнинга).
            warn_at = int(self.max_actions * self.warn_threshold)
            if warn_at <= new_count < self.max_actions:
                last_warn = self._warned_at.get(user_id, 0.0)
                if now - last_warn >= self.warn_cooldown_seconds:
                    self._warned_at[user_id] = now
                    return "warn"

            return "ok"

    def reset(self, user_id: int) -> None:
        """Сбросить state для пользователя (для тестов / админ-команд)."""
        with self._lock:
            self._buckets.pop(user_id, None)
            self._warned_at.pop(user_id, None)


# ------------------------------------------------------------
# SM-2 (SuperMemo-2) — чистая функция для алгоритма повторений.
# Применяется в режиме флэш-карт (v0.7 #15). Ситуационные квизы
# остаются на фиксированных интервалах [1,2,4,7] — их keyword-grader
# не даёт градиента качества для SM-2.
#
# Reference (не зависимость, переписано в Python):
#   thyagoluciano/sm2 на GitHub (Dart, GPL-3.0). Сам алгоритм
#   опубликован SuperMemo (Wozniak 1985), implementation-agnostic.
# ------------------------------------------------------------
EF_FLOOR = 1.3


def sm2_update(quality: int, repetitions: int, ease_factor: float, interval_days: int
               ) -> tuple[int, int, float]:
    """
    Стандартное обновление SM-2.

    Args:
        quality: 0–5. 0 = полный провал, 5 = идеальный ответ.
                 В нашем 3-кнопочном UI используется q=1/3/5
                 («❌ Не знал» / «😐 Сложно» / «✅ Легко»).
        repetitions: текущее число подряд успешных повторений.
        ease_factor: текущий EF (стартует с 2.5; пол — EF_FLOOR=1.3).
        interval_days: текущий интервал (дней до показа), 0 для новой карты.

    Returns:
        (new_interval_days, new_repetitions, new_ease_factor).

    Логика:
        quality < 3 → сбрасываем repetitions в 0, ставим interval=1 (завтра).
        quality >= 3 → repetitions += 1; interval по ступенькам:
                       1-я успешная — 1 день, 2-я — 6 дней, дальше — round(prev * EF).
        EF корректируется всегда по канонической формуле SM-2:
            EF += 0.1 − (5 − q) * (0.08 + (5 − q) * 0.02)
        EF не опускается ниже EF_FLOOR=1.3.
    """
    if quality < 3:
        new_reps = 0
        new_interval = 1
    else:
        new_reps = repetitions + 1
        if new_reps == 1:
            new_interval = 1
        elif new_reps == 2:
            new_interval = 6
        else:
            new_interval = round(interval_days * ease_factor)
    new_ef = ease_factor + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
    new_ef = max(EF_FLOOR, new_ef)
    return new_interval, new_reps, new_ef


class AchievementService:
    """Проверка и выдача достижений. Не зависит от полей target/progress в JSON."""
    def __init__(self, user_repo: UserRepository, definitions: dict):
        self.user_repo = user_repo
        self.definitions = definitions  # загружены из achievements.json

    async def check_and_award(self, user_id: int, sessions: int, streak: int, total_minutes: int) -> tuple[list, int]:
        """
        sessions – новое количество сессий (после завершения текущей)
        streak – текущий стрик до обновления (обновляется отдельно)
        total_minutes – общее количество минут (sessions * длительность одной сессии)
        Возвращает: (список id новых достижений, сумма бонусных монет)
        """
        user_achievements = await self._load_user_achievements(user_id)
        new_achievements = []
        bonus_coins = 0

        # 1. Первый шаг
        if sessions >= 1 and not user_achievements.get("first_session", {}).get("completed"):
            await self._complete_achievement(user_id, "first_session", target=1)
            new_achievements.append("first_session")
            bonus_coins += self._get_reward("first_session")

        # 2. Огненный (3 дня стрика)
        if streak >= 3 and not self._is_completed(user_achievements, "3_day_streak"):
            await self._complete_achievement(user_id, "3_day_streak", target=3)
            new_achievements.append("3_day_streak")
            bonus_coins += self._get_reward("3_day_streak")
        elif not self._is_completed(user_achievements, "3_day_streak"):
            await self._update_progress(user_id, "3_day_streak", progress=streak, target=3)

        # 3. Марафонец (100 минут)
        if total_minutes >= 100 and not self._is_completed(user_achievements, "100_minutes"):
            await self._complete_achievement(user_id, "100_minutes", target=100)
            new_achievements.append("100_minutes")
            bonus_coins += self._get_reward("100_minutes")
        elif not self._is_completed(user_achievements, "100_minutes"):
            await self._update_progress(user_id, "100_minutes", progress=total_minutes, target=100)

        # 4. Десятый шаг (10 сессий)
        if sessions >= 10 and not self._is_completed(user_achievements, "10_sessions"):
            await self._complete_achievement(user_id, "10_sessions", target=10)
            new_achievements.append("10_sessions")
            bonus_coins += self._get_reward("10_sessions")

        # 5. Неделя огня (7 дней)
        if streak >= 7 and not self._is_completed(user_achievements, "7_day_streak"):
            await self._complete_achievement(user_id, "7_day_streak", target=7)
            new_achievements.append("7_day_streak")
            bonus_coins += self._get_reward("7_day_streak")
        elif not self._is_completed(user_achievements, "7_day_streak"):
            await self._update_progress(user_id, "7_day_streak", progress=streak, target=7)

        # 6. Марафон (300 минут)
        if total_minutes >= 300 and not self._is_completed(user_achievements, "300_minutes"):
            await self._complete_achievement(user_id, "300_minutes", target=300)
            new_achievements.append("300_minutes")
            bonus_coins += self._get_reward("300_minutes")
        elif not self._is_completed(user_achievements, "300_minutes"):
            await self._update_progress(user_id, "300_minutes", progress=total_minutes, target=300)

        # 7. Тридцатый шаг (30 сессий)
        if sessions >= 30 and not self._is_completed(user_achievements, "30_sessions"):
            await self._complete_achievement(user_id, "30_sessions", target=30)
            new_achievements.append("30_sessions")
            bonus_coins += self._get_reward("30_sessions")

        # 8. Две недели огня (14 дней)
        if streak >= 14 and not self._is_completed(user_achievements, "14_day_streak"):
            await self._complete_achievement(user_id, "14_day_streak", target=14)
            new_achievements.append("14_day_streak")
            bonus_coins += self._get_reward("14_day_streak")
        elif not self._is_completed(user_achievements, "14_day_streak"):
            await self._update_progress(user_id, "14_day_streak", progress=streak, target=14)

        # 9. Ультрамарафон (750 минут)
        if total_minutes >= 750 and not self._is_completed(user_achievements, "750_minutes"):
            await self._complete_achievement(user_id, "750_minutes", target=750)
            new_achievements.append("750_minutes")
            bonus_coins += self._get_reward("750_minutes")
        elif not self._is_completed(user_achievements, "750_minutes"):
            await self._update_progress(user_id, "750_minutes", progress=total_minutes, target=750)

        return new_achievements, bonus_coins

    async def check_tips_award(self, user_id: int, tips_views: int) -> tuple[list, int]:
        """
        Достижение «10 советов по продуктивности» (10_tips_read).
        Вызывается после каждого просмотра совета; tips_views — накопительный счётчик.
        """
        ach_id = "10_tips_read"
        target = 10
        if ach_id not in self.definitions:
            return [], 0
        user_achievements = await self._load_user_achievements(user_id)
        if self._is_completed(user_achievements, ach_id):
            return [], 0
        if tips_views >= target:
            await self._complete_achievement(user_id, ach_id, target=target)
            return [ach_id], self._get_reward(ach_id)
        await self._update_progress(user_id, ach_id, progress=tips_views, target=target)
        return [], 0

    def _get_reward(self, ach_id: str) -> int:
        return self.definitions[ach_id]["reward"]

    def _is_completed(self, user_achievements: dict, ach_id: str) -> bool:
        return user_achievements.get(ach_id, {}).get("completed", False)

    async def _load_user_achievements(self, user_id: int) -> dict:
        async with self.user_repo.db.execute(
            "SELECT achievement_id, completed, progress, target FROM user_achievements WHERE user_id = ?",
            (user_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            return {
                row["achievement_id"]: {
                    "completed": bool(row["completed"]),
                    "progress": row["progress"],
                    "target": row["target"]
                }
                for row in rows
            }

    async def _complete_achievement(self, user_id: int, ach_id: str, target: int):
        await self.user_repo.db.execute(
            "INSERT INTO user_achievements (user_id, achievement_id, completed, progress, target) "
            "VALUES (?, ?, 1, ?, ?) ON CONFLICT(user_id, achievement_id) DO UPDATE SET "
            "completed = 1, progress = excluded.progress, target = excluded.target",
            (user_id, ach_id, target, target)   # progress = target, completed = 1
        )
        await self.user_repo.db.commit()

    async def _update_progress(self, user_id: int, ach_id: str, progress: int, target: int):
        await self.user_repo.db.execute(
            "INSERT INTO user_achievements (user_id, achievement_id, completed, progress, target) "
            "VALUES (?, ?, 0, ?, ?) ON CONFLICT(user_id, achievement_id) DO UPDATE SET "
            "progress = excluded.progress, target = excluded.target",
            (user_id, ach_id, progress, target)
        )
        await self.user_repo.db.commit()

# ------------------------------------------------------------
# derive_emotion — pure function для отображаемой эмоции питомца.
#
# Эмоция НЕ хранится в БД; каждый render-call перевычисляет её из
# текущего состояния пользователя. См. TODO #16:
# "3 эмоции выводятся из состояния пользователя в момент рендера
# (не хранятся)".
#
# Caller-обязательства:
#   - is_studying:        True, если у пользователя активный таймер
#                         (например, FSM state == TimerStates.running)
#   - recently_excited:   True, если pet.last_excited_at установлен и
#                         (now - last_excited_at) < timedelta(minutes=5)
#                         — caller сам читает таблицу и считает дельту
#   - has_studied_today:  булев users.has_studied_today (1/0 → True/False)
#   - now_local:          текущее время в локальном TZ пользователя
#                         (services уже умеет TZ-aware, см. ReminderService)
# ------------------------------------------------------------
def derive_emotion(
    *,
    is_studying: bool,
    recently_excited: bool,
    has_studied_today: bool,
    now_local: datetime,
) -> str:
    """
    Возвращает одну из 3 эмоций (priority по убыванию):
        1. "joy"     — активный таймер или level-up/ачивка ≤ 5 минут назад
        2. "sad"     — пользователь сегодня ещё не учился
        3. "neutral" — дефолт (в т.ч. ночные часы)

    Все аргументы keyword-only: каждый caller-сайт обязан явно
    назвать что подаёт, чтобы не было перепутанных bool-аргументов.
    """
    if is_studying or recently_excited:
        return "joy"
    if not has_studied_today:
        return "sad"
    return "neutral"


# ------------------------------------------------------------
# get_pet_time_period — сутки питомца (4 варианта арта).
# Использует локальный час пользователя (users.timezone / caller now_local).
# Границы под русский UX: утро 06–12, день 12–17, вечер 17–22, ночь 22–06.
# ------------------------------------------------------------
def get_pet_time_period(now_local: datetime) -> str:
    hour = now_local.hour
    if 6 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "day"
    if 17 <= hour < 22:
        return "evening"
    return "night"


# ------------------------------------------------------------
# render_pet — путь к asset-файлу питомца.
# Pure-функция, не делает I/O помимо `Path.exists()`. Caller
# заворачивает результат в `FSInputFile` или подобный wrapper
# UI-фреймворка (мы не импортируем FSInputFile, чтобы services.py
# не зависел от aiogram.types).
#
# Assets генерируются `scripts/build_pet_assets.py` (Pillow build-time);
# в production runtime — только чтение готовых PNG/GIF.
#
# Fallback chain (если запрошенной комбинации нет):
#   With time_period (subdir assets/pet/<period>/):
#   1. <period>/<emotion>_<color>_<accessory>.png
#   2. <period>/<emotion>_orange_none.png
#   3. <period>/default.png
#   Then legacy flat assets/pet/:
#   4. <emotion>_<color>_<accessory>.png
#   5. <emotion>_orange_none.png
#   6. neutral_orange_none.png (+ legacy happy_orange_none.png)
#   7. default.png
#   8. raise FileNotFoundError
# ------------------------------------------------------------
_ASSETS_PET_DIR = Path(__file__).resolve().parent / "assets" / "pet"
PET_SINGLE_IMAGE_MODE = True
_PET_DEFAULT_IMAGE = _ASSETS_PET_DIR / "default.png"


def _pet_period_dir(time_period: str) -> Path:
    return _ASSETS_PET_DIR / time_period


def _first_existing_path(candidates: list[Path]) -> Path | None:
    for path in candidates:
        if path.exists():
            return path
    return None


def _resolve_pet_time_period(
    *,
    now_local: datetime | None,
    time_period: str | None,
) -> str | None:
    if time_period is not None:
        return sanitize_pet_time_period(time_period)
    if now_local is not None:
        return get_pet_time_period(now_local)
    return None


def render_pet(
    user_pet,            # dict с полями color, accessory ИЛИ None
    emotion: str,
    *,
    animated: bool = False,
    now_local: datetime | None = None,
    time_period: str | None = None,
) -> Path:
    """
    Returns Path к asset-файлу питомца. Pure: только path-resolution.

    `user_pet=None` трактуется как дефолт (orange + none) — для пользователей
    без созданного pet'a (добавляется auto в add_xp; до первой сессии row нет).

    `now_local` / `time_period` — суточный вариант арта (morning/day/evening/night).
    Если передан только `now_local`, период вычисляется через `get_pet_time_period`.
    Отсутствующие period-файлы откатываются на legacy flat assets и default.png.

    `animated=True` → `<period>/<emotion>.gif` или `<emotion>.gif` (без цвета/аксессуара).
    При `PET_SINGLE_IMAGE_MODE=True` сначала ищется `<period>/default.png`, затем
    `assets/pet/default.png`.

    Возвращает Path. Существование файла проверяется внутри (fallback);
    raise FileNotFoundError если даже fallback'и отсутствуют (assets
    директория не была сгенерирована).
    """
    period = _resolve_pet_time_period(now_local=now_local, time_period=time_period)

    if PET_SINGLE_IMAGE_MODE:
        if period:
            period_default = _pet_period_dir(period) / "default.png"
            if period_default.exists():
                return period_default
        if _PET_DEFAULT_IMAGE.exists():
            return _PET_DEFAULT_IMAGE

    emotion, color, accessory = sanitize_pet_asset_keys(
        emotion,
        user_pet.get("color", "orange") if user_pet else "orange",
        user_pet.get("accessory", "none") if user_pet else "none",
    )

    if animated:
        gif_candidates: list[Path] = []
        for stem in pet_emotion_file_stems(emotion):
            if period:
                gif_candidates.append(_pet_period_dir(period) / f"{stem}.gif")
            gif_candidates.append(_ASSETS_PET_DIR / f"{stem}.gif")
        gif_candidates.extend([
            _ASSETS_PET_DIR / "neutral.gif",
            _ASSETS_PET_DIR / "happy.gif",
        ])
        found = _first_existing_path(gif_candidates)
        if found:
            return found
        raise FileNotFoundError(
            f"No animated assets for emotion={emotion!r} period={period!r}. "
            f"Run `python scripts/build_pet_assets.py` to generate them."
        )

    png_candidates: list[Path] = []
    if period:
        period_dir = _pet_period_dir(period)
        for stem in pet_emotion_file_stems(emotion):
            png_candidates.extend([
                period_dir / f"{stem}_{color}_{accessory}.png",
                period_dir / f"{stem}_orange_none.png",
            ])
        png_candidates.append(period_dir / "default.png")
    for stem in pet_emotion_file_stems(emotion):
        png_candidates.extend([
            _ASSETS_PET_DIR / f"{stem}_{color}_{accessory}.png",
            _ASSETS_PET_DIR / f"{stem}_orange_none.png",
        ])
    png_candidates.extend([
        _ASSETS_PET_DIR / "neutral_orange_none.png",
        _ASSETS_PET_DIR / "happy_orange_none.png",
        _PET_DEFAULT_IMAGE,
    ])
    found = _first_existing_path(png_candidates)
    if found:
        return found

    raise FileNotFoundError(
        f"No pet assets found for emotion={emotion!r} color={color!r} "
        f"accessory={accessory!r} period={period!r}. "
        f"Run `python scripts/build_pet_assets.py` or add assets under assets/pet/."
    )


# ------------------------------------------------------------
# Leaderboard scoring helpers — pure functions.
# Полный спек: LEADERBOARD.md. Эти функции — единственное место,
# где зашита численная сторона формулы; если нужно ребалансить —
# меняем здесь + соответствующую таблицу в LEADERBOARD.md.
# ------------------------------------------------------------
def piecewise_time_pts(start: int, end: int) -> float:
    """
    Pts за переход от `start` к `end` суммарных дневных минут учёбы.
    Tiers (LEADERBOARD.md §1):
      0–60 мин:    1.00 pts/мин
      61–120:      0.75
      121–180:     0.50
      181–240:     0.25
      241+:        0   (минуты выше 240 не приносят pts вообще)

    Семантика: caller передаёт «было N минут до сессии, стало M»,
    функция возвращает pts за конкретно эту дельту с учётом того,
    в каких tier'ах она лежит. Так корректно работает «склейка»
    нескольких сессий одного дня.
    """
    start = max(0, start)
    if end <= start:
        return 0.0
    tiers = [(60, 1.00), (120, 0.75), (180, 0.50), (240, 0.25)]
    pts, cursor = 0.0, start
    for tier_end, rate in tiers:
        if cursor >= tier_end:
            continue
        portion = min(end, tier_end) - cursor
        if portion > 0:
            pts += portion * rate
        cursor = min(end, tier_end)
        if cursor >= end:
            break
    return pts


def streak_multiplier(streak_days: int) -> float:
    """
    Weekly-score множитель (LEADERBOARD.md §5).
    Применяется на read-time к сумме компонентов недели.
    """
    if streak_days >= 14:
        return 1.20
    if streak_days >= 7:
        return 1.10
    if streak_days >= 3:
        return 1.05
    return 1.00


def freeze_cost(streak_days: int) -> int:
    """
    Coin-цена заморозки стрика в зависимости от длины (LEADERBOARD.md §Streak Freeze).
    Чем длиннее стрик — тем дороже сохранить.
    """
    if streak_days >= 21:
        return 1000
    if streak_days >= 8:
        return 750
    return 500


def user_calendar_keys(now_local: datetime) -> tuple:
    """
    Возвращает (local_date 'YYYY-MM-DD', week_iso 'YYYY-Www') из datetime
    в локальном TZ пользователя.

    week_iso использует ISO 8601 неделю (%G-W%V), так что неделя всегда
    начинается с понедельника — что точно соответствует rollover-семантике
    спеки. local_date — обычный Gregorian (%Y-%m-%d), используется для
    PK в daily_score_counters и для consumed_for_date в streak_freezes.
    """
    return now_local.strftime("%Y-%m-%d"), now_local.strftime("%G-W%V")


def format_leaderboard_user_label(username: str | None, user_id: int) -> str:
    """
    Публичная подпись пользователя в leaderboard/friends-tab.
    Username берётся из users.username (UsernameSyncMiddleware).
    """
    if username:
        return f"@{username}"
    return f"id={user_id}"


def parse_friend_query(text: str) -> tuple:
    """
    Парсит ввод friend-add FSM в (username, numeric_id). Один из них
    будет None — caller выбирает path по тому, что не-None. Если оба
    None — вход неразборчивый, caller просит повторить.

    Принимает:
      '@alice'  → ('alice', None)
      'alice'   → ('alice', None) — без @ трактуем как username
      '12345'   → (None, 12345)
      '-12345'  → (None, -12345)
      ''        → (None, None)
      'foo bar' → ('foo bar', None) — пробелы сохраняем; lookup всё
                  равно не найдёт (Telegram username их не разрешает)

    Эвристика "нет цифры → username" работает, потому что Telegram-handle
    всегда начинается с буквы (regex `[A-Za-z][A-Za-z0-9_]{4,31}`).
    Pure function; testable без bot.py.
    """
    text = (text or "").strip()
    if not text:
        return (None, None)
    if text.startswith("@"):
        username = text[1:].strip()
        return ((username if username else None), None)
    candidate = text[1:] if text.startswith("-") else text
    if candidate.isdigit():
        try:
            return (None, int(text))
        except ValueError:
            return (None, None)
    return (text, None)


class StudyService:
    def __init__(
        self,
        user_repo: UserRepository,
        session_repo: SessionRepository,
        achievement_service: AchievementService,
        pet_repo: PetRepository | None = None,
        leaderboard_repo: LeaderboardRepository | None = None,
        bot=None,
    ):
        self.user_repo = user_repo
        self.session_repo = session_repo
        self.achievement_service = achievement_service
        # pet_repo / leaderboard_repo / bot опциональны: тесты вне pet/leaderboard
        # флоу могут их не передавать — соответствующий grant / notification
        # просто skip'ается. В production bot.py передаёт все три.
        self.pet_repo = pet_repo
        self.leaderboard_repo = leaderboard_repo
        self.bot = bot  # для отправки level-up уведомлений

    async def _notify_level_up(
        self, user_id: int, old_level: int, new_level: int
    ) -> None:
        """
        Отправляет сообщение о level-up с перечислением только-что
        разблокированных предметов из COLOR_CATALOG / ACCESSORY_CATALOG.
        По спеке TODO #16: «сообщение содержит список разблокированных-
        но-непокупленных предметов с их ценой».

        Бесшумно поглощает Telegram exceptions (заблокирован, etc.) —
        notification является вспомогательным flow.
        """
        if self.bot is None or self.pet_repo is None:
            return

        unlocked = []
        for value, (lvl, price) in self.pet_repo.COLOR_CATALOG.items():
            if old_level < lvl <= new_level and price > 0:
                unlocked.append(f"🎨 цвет «{value}» — {price} 🪙")
        for value, (lvl, price) in self.pet_repo.ACCESSORY_CATALOG.items():
            if old_level < lvl <= new_level and price > 0:
                unlocked.append(f"🎁 «{value}» — {price} 🪙")

        msg = (
            f"🎉 <b>Уровень повышен!</b>\n"
            f"🐾 Питомец вырос: {old_level} → <b>{new_level}</b>"
        )
        if unlocked:
            msg += (
                "\n\n<b>Открылись новые предметы:</b>\n"
                + "\n".join(f"• {u}" for u in unlocked)
                + "\n\nКупить можно в профиле через customization picker."
            )
        else:
            msg += "\n\nНа этом уровне новых предметов не открылось — продолжай!"

        try:
            await self.bot.send_message(user_id, msg, parse_mode="HTML")
        except Exception as e:
            logger.warning(
                "pet.level_up_notify_failed user_id=%s new_level=%s reason=%s",
                user_id, new_level, type(e).__name__,
            )

    async def complete_session(self, user_id: int, duration: int) -> tuple[list, int, int]:
        """
        Возвращает (earned_achievements, bonus_coins, session_id).
        session_id нужен для последующей пользовательской оценки сессии.
        """
        if duration < 1:
            return [], 0, 0
        duration = min(duration, 120)

        async with self.user_repo.db.lock:
            user = await self.user_repo.get_user(user_id)
            if not user:
                await self.user_repo.create_user(user_id)
                user = await self.user_repo.get_user(user_id)

            previous_minutes = await self.session_repo.get_total_minutes(user_id)
            total_minutes = previous_minutes + duration

            sessions = user["total_sessions"] + 1
            streak = user["current_streak"]

            base_coins = duration

            settings = await self.user_repo.get_notification_settings(user_id)
            achievements_enabled = not settings or settings.get("achievements_enabled", 1)
            if achievements_enabled:
                earned, bonus = await self.achievement_service.check_and_award(
                    user_id, sessions, streak, total_minutes
                )
            else:
                earned, bonus = [], 0

            await self.user_repo.increment_sessions(user_id)
            await self.user_repo.add_coins(user_id, base_coins + bonus)
            session_id = await self.session_repo.add_session(user_id, duration, base_coins, bonus)

            # Pet XP-grant (v0.7 TODO #16). 1 XP / минута учёбы.
            # add_xp auto-создаёт pet при первой сессии и сам пометит
            # last_excited_at при level-up. Если ачивки были — тоже
            # пометим excited (вторая ветка приоритета derive_emotion).
            # Lock уже взят выше — pet_repo.add_xp его не берёт повторно.
            level_up_info = None
            if self.pet_repo is not None:
                old_level, new_level = await self.pet_repo.add_xp(user_id, duration)
                if earned:
                    await self.pet_repo.mark_excited(user_id)
                if new_level > old_level:
                    level_up_info = (old_level, new_level)

            # Leaderboard time pts (LEADERBOARD.md §1). Piecewise по дневным
            # минутам с учётом уже накопленных за сегодня. Repository сам
            # резолвит user TZ через users.timezone (now_local=None default).
            # Lock уже взят — grant_time_pts его не берёт повторно.
            if self.leaderboard_repo is not None:
                await self.leaderboard_repo.grant_time_pts(user_id, duration)

        # Level-up уведомление ВНЕ db.lock — не хотим держать лок ради
        # сетевого вызова к Telegram. На этой точке db-операции уже
        # commit'нуты, и небольшая задержка нотификации не критична.
        if level_up_info is not None:
            await self._notify_level_up(user_id, *level_up_info)

        return earned, bonus, session_id
    
class ReminderService:
    """
    Отправка утренних и вечерних напоминаний.
    Вызывается планировщиком раз в минуту для каждого TZ, с локальным hhmm этого TZ.
    """
    def __init__(
        self,
        user_repo: UserRepository,
        bot,
        morning_tip_builder=None,
        event_repo=None,
    ):
        self.user_repo = user_repo
        self.bot = bot
        # async (user_id, tz) -> str — HTML-блок «совет дня» (опционально).
        self.morning_tip_builder = morning_tip_builder
        self.event_repo = event_repo

    async def tick(self, tz: str, hhmm: str) -> None:
        """Отправляет утренние и вечерние напоминания для указанного TZ и hhmm."""
        await self._send_morning(tz, hhmm)
        await self._send_evening(tz, hhmm)

    async def _send_morning(self, tz: str, hhmm: str) -> None:
        users = await self.user_repo.get_users_due_for_morning(tz, hhmm)
        if users:
            logger.info(
                "reminder.morning.dispatched tz=%s hhmm=%s count=%s",
                tz, hhmm, len(users),
            )
        for u in users:
            uid = u["user_id"]
            try:
                locale = _user_locale(await self.user_repo.get_locale(uid))
                text = t("reminders.morning", locale)
                if self.morning_tip_builder:
                    try:
                        extra = await self.morning_tip_builder(uid, tz)
                        if extra:
                            text += extra
                    except Exception as e:
                        logger.warning(
                            "reminder.morning.tip_of_day_failed uid=%s reason=%s",
                            uid, e,
                        )
                parse_mode = "HTML" if self.morning_tip_builder else None
                await send_with_telegram_bulkhead(
                    lambda: self.bot.send_message(
                        chat_id=uid, text=text, parse_mode=parse_mode,
                    ),
                    label="morning", uid=uid,
                )
                if self.event_repo:
                    await self.event_repo.log(
                        uid,
                        "reminder_sent",
                        {"kind": "morning", "tz": tz, "hhmm": hhmm},
                    )
            except TelegramForbiddenError:
                # Пользователь заблокировал бота — ожидаемо, INFO.
                logger.info(
                    "reminder.send_failed kind=morning uid=%s reason=blocked", uid
                )
            except TelegramBadRequest as e:
                # Permanent: chat not found / message too long / parse error.
                logger.warning(
                    "reminder.send_failed kind=morning uid=%s reason=bad_request detail=%s",
                    uid, e,
                )
            except Exception as e:
                logger.warning(
                    "reminder.send_failed kind=morning uid=%s reason=%s detail=%s",
                    uid, type(e).__name__, e,
                )

    async def _send_evening(self, tz: str, hhmm: str) -> None:
        """
        Evening reminders с sad-pet интеграцией (TODO #16, последний остаток).
        Per-user now_local + derive_emotion → выбор копи. По спеке reminder
        фильтрует на has_studied_today=0, и derive_emotion вернёт 'sad'.
        """
        import pytz
        users = await self.user_repo.get_users_due_for_evening(tz, hhmm)
        if not users:
            return
        logger.info(
            "reminder.evening.dispatched tz=%s hhmm=%s count=%s",
            tz, hhmm, len(users),
        )
        try:
            now_local = datetime.now(pytz.timezone(tz))
        except Exception:
            # Defensive: unknown TZ → fallback на naive datetime,
            # эмоция всё равно вычислится корректно по полям user'а.
            now_local = datetime.now()

        for u in users:
            uid = u["user_id"]
            locale = _user_locale(await self.user_repo.get_locale(uid))
            evening_sad = t("reminders.evening_sad", locale)
            evening_fallback = t("reminders.evening_fallback", locale)
            # has_studied_today из SQL — int 0/1; конвертим в bool
            studied = bool(u.get("has_studied_today", 0))
            emotion = derive_emotion(
                is_studying=False,  # FSM-state не доступен из scheduler'а
                recently_excited=False,
                has_studied_today=studied,
                now_local=now_local,
            )
            try:
                if emotion == "sad":
                    # Sad-path: пробуем отправить sad.gif с caption. Если
                    # asset отсутствует (FileNotFoundError из render_pet) —
                    # graceful fallback на text-only sad-pet копи.
                    try:
                        from aiogram.types import FSInputFile
                        asset_path = render_pet(
                            None, "sad", animated=True, now_local=now_local,
                        )
                        media = FSInputFile(str(asset_path))
                        if asset_path.suffix.lower() == ".png":
                            await send_with_telegram_bulkhead(
                                lambda: self.bot.send_photo(
                                    chat_id=uid,
                                    photo=media,
                                    caption=evening_sad,
                                ),
                                label="evening_sad_photo", uid=uid,
                            )
                        else:
                            await send_with_telegram_bulkhead(
                                lambda: self.bot.send_animation(
                                    chat_id=uid,
                                    animation=media,
                                    caption=evening_sad,
                                ),
                                label="evening_sad_gif", uid=uid,
                            )
                    except FileNotFoundError:
                        await send_with_telegram_bulkhead(
                            lambda: self.bot.send_message(
                                chat_id=uid, text=evening_sad,
                            ),
                            label="evening_sad_text", uid=uid,
                        )
                else:
                    # Non-sad emotion path → text-only fallback копи.
                    # Defensive: SQL filter гарантирует has_studied_today=0,
                    # так что эта ветка достижима только при странных edge cases.
                    await send_with_telegram_bulkhead(
                        lambda: self.bot.send_message(
                            chat_id=uid, text=evening_fallback,
                        ),
                        label="evening_fallback", uid=uid,
                    )
                if self.event_repo:
                    await self.event_repo.log(
                        uid,
                        "reminder_sent",
                        {
                            "kind": "evening",
                            "tz": tz,
                            "hhmm": hhmm,
                            "emotion": emotion,
                        },
                    )
            except TelegramForbiddenError:
                logger.info(
                    "reminder.send_failed kind=evening uid=%s reason=blocked", uid
                )
            except TelegramBadRequest as e:
                logger.warning(
                    "reminder.send_failed kind=evening uid=%s reason=bad_request detail=%s",
                    uid, e,
                )
            except Exception as e:
                logger.warning(
                    "reminder.send_failed kind=evening uid=%s reason=%s detail=%s",
                    uid, type(e).__name__, e,
                )


class StreakService:
    """
    Обработка ежедневного обновления стриков.
    Вызывается ОДИН раз в сутки по расписанию.
    """
    def __init__(
        self,
        user_repo: UserRepository,
        bot=None,
        leaderboard_repo=None,  # type: LeaderboardRepository | None
    ):
        self.user_repo = user_repo
        self.bot = bot  # опционально, для отправки уведомлений
        # leaderboard_repo опционален: если передан, miss-day path сначала
        # пытается consume freeze (LEADERBOARD.md §Streak Freeze) и только
        # потом ресетит стрик. Без него — поведение как до Phase 3.
        self.leaderboard_repo = leaderboard_repo

    async def process_users_in_timezone(self, tz: str):
        """
        Обрабатывает стрики для пользователей указанного часового пояса.
        Вызывается планировщиком, когда в этом TZ наступает 23:59.
        """
        users = await self.user_repo.get_users_for_streak_update_in_timezone(tz)
        if not users:
            return
        await self._process(users, tz=tz)

    async def process_all_users(self):
        """Обратная совместимость: обработка всех пользователей независимо от TZ."""
        users = await self.user_repo.get_users_for_streak_update()
        if not users:
            return
        await self._process(users, tz="*")

    async def _process(self, users: list[dict], tz: str = "*"):
        # Считаем today_local один раз per-TZ для consume_freeze_if_active.
        # При tz == "*" (fallback path) freeze не consume'им — этот путь
        # не используется в production.
        today_local = None
        if tz != "*":
            try:
                import pytz
                today_local = datetime.now(pytz.timezone(tz)).strftime("%Y-%m-%d")
            except Exception:
                today_local = None

        incremented = 0
        reset = 0
        frozen = 0
        skipped = 0
        bonuses_total = 0
        for user in users:
            user_id = user["user_id"]
            has_studied = user["has_studied_today"]
            current_streak = user["current_streak"]
            streak_enabled = bool(user.get("streak_enabled", 1))

            if today_local and user.get("last_streak_check_date") == today_local:
                skipped += 1
                continue

            if not streak_enabled:
                async with self.user_repo.db.lock:
                    if has_studied:
                        await self.user_repo.set_has_studied_today(user_id, False)
                    if today_local:
                        await self.user_repo.set_last_streak_check_date(
                            user_id, today_local
                        )
                continue

            if has_studied:
                new_streak = current_streak + 1
                # Бонус со второго дня стрика
                bonus = 15 if new_streak >= 2 else 0
                async with self.user_repo.db.lock:
                    await self.user_repo.apply_streak_increment(user_id, new_streak, bonus)
                    if today_local:
                        await self.user_repo.set_last_streak_check_date(
                            user_id, today_local
                        )
                incremented += 1
                bonuses_total += bonus

                # Уведомление (если передан bot)
                if self.bot and bonus > 0:
                    try:
                        locale = _user_locale(await self.user_repo.get_locale(user_id))
                        await send_with_telegram_bulkhead(
                            lambda: self.bot.send_message(
                                chat_id=user_id,
                                text=t(
                                    "reminders.streak_bonus",
                                    locale,
                                    streak=new_streak,
                                    bonus=bonus,
                                ),
                            ),
                            label="streak_bonus", uid=user_id,
                        )
                    except Exception as e:
                        # Блокировка / прочие ошибки — пишем в лог, не падаем.
                        logger.warning(
                            "streak.notify_failed user_id=%s reason=%s",
                            user_id, type(e).__name__,
                        )
            else:
                if current_streak > 0:
                    # Сначала пробуем consume freeze; если активной заморозки
                    # нет — стрик сбрасывается как раньше.
                    consumed = False
                    if self.leaderboard_repo is not None and today_local:
                        consumed = await self.leaderboard_repo.consume_freeze_if_active(
                            user_id, today_local
                        )
                    if consumed:
                        frozen += 1
                        if today_local:
                            async with self.user_repo.db.lock:
                                await self.user_repo.set_last_streak_check_date(
                                    user_id, today_local
                                )
                        # Notify пользователя что freeze отработал
                        if self.bot:
                            try:
                                locale = _user_locale(
                                    await self.user_repo.get_locale(user_id)
                                )
                                await send_with_telegram_bulkhead(
                                    lambda: self.bot.send_message(
                                        chat_id=user_id,
                                        text=t(
                                            "reminders.freeze_used",
                                            locale,
                                            streak=current_streak,
                                        ),
                                    ),
                                    label="streak_freeze", uid=user_id,
                                )
                            except Exception as e:
                                logger.warning(
                                    "streak.freeze_notify_failed user_id=%s reason=%s",
                                    user_id, type(e).__name__,
                                )
                    else:
                        async with self.user_repo.db.lock:
                            await self.user_repo.apply_streak_reset(user_id)
                            if today_local:
                                await self.user_repo.set_last_streak_check_date(
                                    user_id, today_local
                                )
                        reset += 1
                elif today_local:
                    async with self.user_repo.db.lock:
                        await self.user_repo.set_last_streak_check_date(
                            user_id, today_local
                        )

        logger.info(
            "streak.batch tz=%s users=%s incremented=%s reset=%s frozen=%s "
            "skipped=%s bonuses=%s",
            tz, len(users), incremented, reset, frozen, skipped, bonuses_total,
        )


# ------------------------------------------------------------
# LeaderboardService — render + (future) rollover logic.
# Чистая обёртка над LeaderboardRepository: маршрутизация newbie vs main,
# форматирование Telegram-friendly текста, computation вспомогательных
# полей. Rollover/badges/coin-bonuses не реализованы в этом релизе —
# отдельный коммит.
# ------------------------------------------------------------
class LeaderboardService:
    """
    Главная UI-обёртка над LeaderboardRepository (LEADERBOARD.md §Segments).

    Auto-routing:
      - newbie: created_at < 7 days ago → видит топ только среди newbie
      - main:   created_at >= 7 days ago → видит main-сегмент

    Privacy:
      - hidden_from_leaderboards=1 → не показываемся другим, но видим
        свою позицию с маркером «Вы скрыты».
    """

    TOP_N_DISPLAY = 20

    # Top 10% coin bonus в конце недели — "small" по спеке (§Rewards).
    # 50 coins = ~1.25 math task. Видимый, но не настолько большой,
    # чтобы перевернуть экономию баланса монет.
    COIN_BONUS_TOP10_PCT = 50

    # Минимальный размер сегмента для имеющего смысл "top 10%". При
    # 3 пользователях "10%" = 0.3 → округляется в 0; при 9 — то же.
    # Без минимума мы бы выдавали бонус всем при маленьком сегменте,
    # что обесценивает rewards.
    MIN_SEGMENT_FOR_TOP10_BONUS = 10

    def __init__(
        self,
        user_repo: UserRepository,
        leaderboard_repo,  # type: LeaderboardRepository — late binding
        friend_repo=None,  # type: FriendRepository | None — Phase 4
    ):
        self.user_repo = user_repo
        self.leaderboard_repo = leaderboard_repo
        # friend_repo опционален: render_friends_tab требует его, остальной
        # leaderboard-стэк работает без. Тесты Phase 0-3 не передают.
        self.friend_repo = friend_repo

    async def _user_segment(self, user_id: int) -> str:
        """Возвращает 'newbie' или 'main' по created_at пользователя."""
        async with self.user_repo.db.execute(
            "SELECT (julianday('now') - julianday(created_at)) AS age_days "
            "FROM users WHERE user_id=?",
            (user_id,),
        ) as c:
            row = await c.fetchone()
        if row is None:
            return "newbie"  # defensive
        return "newbie" if row["age_days"] < 7 else "main"

    async def _current_week_iso(self, user_id: int) -> str:
        """Текущая ISO-неделя в TZ пользователя."""
        now_local = await self.leaderboard_repo._now_local_for_user(user_id)
        _, week_iso = user_calendar_keys(now_local)
        return week_iso

    async def render_leaderboard(self, user_id: int) -> str:
        """
        Текст leaderboard'а для отправки в Telegram (HTML parse_mode).
        Показывает:
          - сегмент (newbie/main), week_iso
          - TOP_N_DISPLAY топ-пользователей сегмента (исключая hidden)
          - собственный ранг пользователя ниже, если он за пределами топа
            или hidden (в этом случае с маркером «Вы скрыты»)
        Подписи пользователей — @username из users.username (синхронизируется
        UsernameSyncMiddleware), иначе fallback id=...
        """
        segment = await self._user_segment(user_id)
        week_iso = await self._current_week_iso(user_id)

        # Публичный топ (исключая hidden)
        public_top = await self.leaderboard_repo.get_ranked_segment(
            week_iso, segment, exclude_hidden=True
        )
        # Полный ranked (включая hidden) — для поиска позиции self
        full_ranked = await self.leaderboard_repo.get_ranked_segment(
            week_iso, segment, exclude_hidden=False
        )

        seg_label = "🆕 Новички" if segment == "newbie" else "🏆 Основной"
        lines = [
            f"<b>📊 Лидерборд недели · {week_iso}</b>",
            f"Сегмент: {seg_label}",
            "",
        ]

        if not public_top:
            lines.append("Пока никто не набрал очков в этой неделе.")
        else:
            lines.append("<b>Топ:</b>")
            for idx, entry in enumerate(public_top[: self.TOP_N_DISPLAY], start=1):
                marker = "👤" if entry["user_id"] == user_id else "  "
                label = format_leaderboard_user_label(
                    entry.get("username"), entry["user_id"]
                )
                lines.append(
                    f"{marker} {idx}. {label}  "
                    f"{entry['total_final']:.0f} pts  "
                    f"(×{entry['multiplier']:.2f})"
                )

        # Собственный ранг — если за пределами топа или hidden
        own_rank = None
        own_entry = None
        for idx, entry in enumerate(full_ranked, start=1):
            if entry["user_id"] == user_id:
                own_rank = idx
                own_entry = entry
                break

        if own_entry is None:
            lines.append("")
            lines.append("Вы пока без очков на этой неделе.")
        else:
            in_displayed_top = (
                not own_entry["hidden"]
                and any(
                    e["user_id"] == user_id
                    for e in public_top[: self.TOP_N_DISPLAY]
                )
            )
            if not in_displayed_top:
                lines.append("")
                hidden_note = " · Вы скрыты" if own_entry["hidden"] else ""
                lines.append(
                    f"Ваш ранг: <b>{own_rank}</b>  "
                    f"{own_entry['total_final']:.0f} pts"
                    f"{hidden_note}"
                )

        return "\n".join(lines)

    # ------------------------------------------------------------
    # Weekly rollover — выдача badges + top-10% coin bonus
    # ------------------------------------------------------------
    async def run_rollover(self, ended_week_iso: str) -> dict:
        """
        Выдаёт еженедельные награды за уже закончившуюся неделю.
        Спецификация: LEADERBOARD.md §Rewards.

        Для каждого сегмента (main, newbie):
          - top-3 main: бэджи `top_1`, `top_2`, `top_3`
          - top-1 newbie: бэдж `breakthrough`
          - top-10% (только если в сегменте ≥ MIN_SEGMENT_FOR_TOP10_BONUS
            пользователей): COIN_BONUS_TOP10_PCT монет каждому

        Идемпотентность: award_badge — INSERT OR IGNORE по PK
        (user_id, badge_id, awarded_for_week). Повторный run_rollover
        для той же week — no-op (нет дубль-бэджей, нет повторных
        начислений монет). Coin-бонус начисляется ТОЛЬКО когда
        award_badge('top10_pct_bonus', …) вернёт True.

        Hidden-пользователи **получают** rewards (заработали по спеке;
        scoring не зависит от privacy), просто не отображаются на
        публичных лидербордах. Поэтому здесь exclude_hidden=False.

        Возвращает stats dict: {
            'week': str, 'badges_awarded': int, 'coins_distributed': int,
            'segments_processed': int
        }
        """
        stats = {
            "week": ended_week_iso,
            "badges_awarded": 0,
            "coins_distributed": 0,
            "segments_processed": 0,
        }

        for segment in ("main", "newbie"):
            ranked = await self.leaderboard_repo.get_ranked_segment(
                ended_week_iso, segment, exclude_hidden=False
            )
            if not ranked:
                continue
            stats["segments_processed"] += 1

            if segment == "main":
                badge_by_rank = {1: "top_1", 2: "top_2", 3: "top_3"}
            else:  # newbie
                badge_by_rank = {1: "breakthrough"}

            for rank, entry in enumerate(ranked, start=1):
                badge_id = badge_by_rank.get(rank)
                if badge_id is None:
                    break
                newly = await self.leaderboard_repo.award_badge(
                    entry["user_id"], badge_id, ended_week_iso
                )
                if newly:
                    stats["badges_awarded"] += 1
                    logger.info(
                        "leaderboard.rollover.badge segment=%s rank=%s "
                        "badge=%s user_id=%s week=%s",
                        segment, rank, badge_id, entry["user_id"],
                        ended_week_iso,
                    )

            # Top-10% coin bonus — только когда сегмент достаточно велик.
            # Маленькие сегменты (<10 человек) пропускаем целиком, иначе
            # bonus превращается в "всем подряд" и обесценивается.
            if len(ranked) >= self.MIN_SEGMENT_FOR_TOP10_BONUS:
                top10_count = max(1, len(ranked) // 10)
                for entry in ranked[:top10_count]:
                    newly = await self.leaderboard_repo.award_badge(
                        entry["user_id"], "top10_pct_bonus", ended_week_iso
                    )
                    if newly:
                        stats["badges_awarded"] += 1
                        await self.user_repo.add_coins(
                            entry["user_id"], self.COIN_BONUS_TOP10_PCT
                        )
                        stats["coins_distributed"] += self.COIN_BONUS_TOP10_PCT
                        logger.info(
                            "leaderboard.rollover.coin_bonus segment=%s "
                            "user_id=%s coins=%s week=%s",
                            segment, entry["user_id"],
                            self.COIN_BONUS_TOP10_PCT, ended_week_iso,
                        )

        logger.info(
            "leaderboard.rollover.summary week=%s segments=%s "
            "badges_awarded=%s coins_distributed=%s",
            ended_week_iso, stats["segments_processed"],
            stats["badges_awarded"], stats["coins_distributed"],
        )
        return stats

    # ------------------------------------------------------------
    # Friends-tab (Phase 4 / LEADERBOARD.md §Segments → Friends)
    # ------------------------------------------------------------
    async def render_friends_tab(self, user_id: int) -> str:
        """
        Telegram-friendly текст friends-tab: viewer + его friends, отсортированы
        по total_final текущей недели (с применением streak_multiplier).

        Top 3 получают medal-emoji (🥇🥈🥉); собственная строка viewer'а
        размечена суффиксом «(Вы)». Пустой friends-list — подсказка
        добавить через /friends. Friends-tab показывает ВСЕХ друзей
        независимо от их hidden_from_leaderboards (privacy не применяется
        внутри взаимных дружб — добавление в друзья = opt-in видимости).
        """
        if self.friend_repo is None:
            return "Friends-функционал не настроен."

        week_iso = await self._current_week_iso(user_id)
        friend_ids = await self.friend_repo.get_friends(user_id)

        if not friend_ids:
            return (
                "<b>👥 Друзья</b>\n\n"
                "У тебя пока нет добавленных друзей.\n"
                "Используй <b>/friends</b>: «➕ Добавить» (ID или @username) "
                "или «🔗 Пригласить по ссылке» — отправь ссылку другу, "
                "он откроет бота и сразу станет твоим другом."
            )

        # Включаем себя в список для ранжирования
        all_ids = friend_ids + [user_id]
        rows = []
        for uid in all_ids:
            user = await self.user_repo.get_user(uid)
            ws = await self.leaderboard_repo.get_weekly_score(uid, week_iso)
            base = 0
            if ws is not None:
                base = (
                    ws["time_pts"] + ws["task_pts"]
                    + ws["quiz_pts"] + ws["card_pts"]
                )
            streak_days = user["current_streak"] if user else 0
            mult = streak_multiplier(streak_days)
            rows.append({
                "user_id": uid,
                "username": user.get("username") if user else None,
                "total_final": base * mult,
                "current_streak": streak_days,
            })
        rows.sort(key=lambda r: r["total_final"], reverse=True)

        rank_emojis = {1: "🥇", 2: "🥈", 3: "🥉"}
        lines = [f"<b>👥 Друзья · {week_iso}</b>", ""]
        for rank, row in enumerate(rows, start=1):
            emoji = rank_emojis.get(rank, "  ")
            suffix = " <i>(Вы)</i>" if row["user_id"] == user_id else ""
            label = format_leaderboard_user_label(
                row.get("username"), row["user_id"]
            )
            lines.append(
                f"{emoji} {label}  "
                f"{row['total_final']:.0f} pts{suffix}"
            )
        return "\n".join(lines)


# ------------------------------------------------------------
# AnalyticsService — продуктовая аналитика для PA-портфолио.
# Чистые SQL-агрегаты над существующими таблицами; никаких новых
# таблиц не вводит. Возвращает structured dicts — UI/admin commands
# их рендерят как text/CSV.
# ------------------------------------------------------------
# Две метрики «активности» — не смешивать в отчётах без подписи.
# См. compute_engagement() и admin_commands.md → «Метрики активности».
ACTIVITY_METRIC_DEFINITIONS = {
    "activity_progress": {
        "label": "activity_progress",
        "title": "Активность (progress tables)",
        "used_in": "cohort retention, DAU/WAU/MAU (dau/wau/mau), segments (churn)",
        "sources": [
            "study_sessions.created_at",
            "user_subject_stats.last_activity",
            "quiz_progress.last_attempt",
            "flashcard_progress.last_review",
            "mcq_progress.last_attempt",
            "task_progress.last_attempt",
        ],
        "meaning": "Пользователь сделал учебное действие с записью в progress/sessions.",
        "caveat": "Визит в предмет без квиза тоже считается (last_activity). Может быть выше, чем events.",
    },
    "activity_events": {
        "label": "activity_events",
        "title": "Активность (events table)",
        "used_in": "DAU/WAU/MAU (dau_events/wau_events/mau_events), heatmap, event timeline",
        "sources": ["events.created_at (любой event_name, user_id NOT NULL)"],
        "meaning": "Любое залогированное событие в append-only events.",
        "caveat": "Зависит от полноты hook'ов; до появления events — пусто. Может быть ниже progress.",
    },
}


class AnalyticsService:
    """
    Cohort retention, funnel, активность пользователей. Все методы
    возвращают structured data — не строки. Это позволяет переиспользовать
    из разных команд (text-render, CSV-export, JSON-dump).

    Две метрики активности — см. ACTIVITY_METRIC_DEFINITIONS и
    compute_engagement() (поля dau vs dau_events).
    """

    def __init__(self, db):
        self.db = db

    @staticmethod
    def get_activity_metric_definitions() -> dict:
        """Справочник двух метрик активности для админ-доков и /dau."""
        return ACTIVITY_METRIC_DEFINITIONS

    async def _all_activity_dates_per_user(self) -> dict[int, set]:
        """Возвращает {user_id: set([date, date, ...])} по всем источникам."""
        from collections import defaultdict
        activity = defaultdict(set)
        queries = [
            "SELECT user_id, created_at AS ts FROM study_sessions",
            "SELECT user_id, last_activity AS ts FROM user_subject_stats "
            "WHERE last_activity IS NOT NULL",
            "SELECT user_id, last_attempt AS ts FROM quiz_progress "
            "WHERE last_attempt IS NOT NULL",
            "SELECT user_id, last_review AS ts FROM flashcard_progress "
            "WHERE last_review IS NOT NULL",
            "SELECT user_id, last_attempt AS ts FROM mcq_progress "
            "WHERE last_attempt IS NOT NULL",
            "SELECT user_id, last_attempt AS ts FROM task_progress "
            "WHERE last_attempt IS NOT NULL",
        ]
        for sql in queries:
            async with self.db.execute(sql) as cursor:
                rows = await cursor.fetchall()
            for row in rows:
                ts_str = row["ts"]
                if not ts_str:
                    continue
                try:
                    ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                except (ValueError, TypeError):
                    continue
                activity[row["user_id"]].add(ts.date())
        return activity

    async def _all_activity_dates_per_user_events(self) -> dict[int, set]:
        """Активность по таблице events (метрика activity_events)."""
        activity: dict[int, set] = defaultdict(set)
        async with self.db.execute(
            "SELECT user_id, created_at FROM events WHERE user_id IS NOT NULL"
        ) as cursor:
            rows = await cursor.fetchall()
        for row in rows:
            ts_str = row["created_at"]
            if not ts_str:
                continue
            try:
                ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                continue
            activity[row["user_id"]].add(ts.date())
        return activity

    async def compute_cohort_retention(self) -> dict:
        """
        D1/D7/D30 retention по ISO-неделям регистрации.

        Definition (strict): D_N = % пользователей, активных на КАЛЕНДАРНУЮ
        дату (signup_date + N дней). Пользователи, чей signup_date был
        меньше N дней назад, не учитываются в знаменателе D_N (eligible=0).

        Returns: {
            "cohorts": [
                {"week": "2026-W20", "size": 12,
                 "d1": 0.67 | None, "d7": 0.25 | None, "d30": None},
                ...
            ],
            "total_users": 25,
            "today": "YYYY-MM-DD",
        }
        """
        from collections import defaultdict
        # Шаг 1: signup-даты
        signups: dict[int, "datetime.date"] = {}
        async with self.db.execute("SELECT user_id, created_at FROM users") as cursor:
            for row in await cursor.fetchall():
                try:
                    dt = datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S")
                    signups[row["user_id"]] = dt.date()
                except (ValueError, TypeError):
                    continue
        if not signups:
            return {"cohorts": [], "total_users": 0, "today": datetime.now().strftime("%Y-%m-%d")}

        # Шаг 2: активность
        activity = await self._all_activity_dates_per_user()

        # Шаг 3: bucket по ISO-неделям + считаем eligible/active per D_N
        today = datetime.now().date()
        cohort_data: dict[str, dict] = defaultdict(
            lambda: {
                "size": 0,
                "d1_eligible": 0, "d1_active": 0,
                "d7_eligible": 0, "d7_active": 0,
                "d30_eligible": 0, "d30_active": 0,
            }
        )
        for user_id, signup_date in signups.items():
            # Используем ISO-неделю в формате "YYYY-Www"
            iso_year, iso_week, _ = signup_date.isocalendar()
            week_key = f"{iso_year}-W{iso_week:02d}"
            bucket = cohort_data[week_key]
            bucket["size"] += 1
            user_age = (today - signup_date).days
            user_active_dates = activity.get(user_id, set())
            for day_n in (1, 7, 30):
                if user_age < day_n:
                    continue  # too young — не в знаменателе
                bucket[f"d{day_n}_eligible"] += 1
                target = signup_date + timedelta(days=day_n)
                if target in user_active_dates:
                    bucket[f"d{day_n}_active"] += 1

        # Шаг 4: % по каждой когорте
        cohorts = []
        for week in sorted(cohort_data.keys()):
            d = cohort_data[week]
            cohorts.append({
                "week": week,
                "size": d["size"],
                "d1":  d["d1_active"]  / d["d1_eligible"]  if d["d1_eligible"]  else None,
                "d7":  d["d7_active"]  / d["d7_eligible"]  if d["d7_eligible"]  else None,
                "d30": d["d30_active"] / d["d30_eligible"] if d["d30_eligible"] else None,
            })
        return {
            "cohorts": cohorts,
            "total_users": len(signups),
            "today": today.strftime("%Y-%m-%d"),
        }

    async def _count(self, sql: str, params: tuple = ()) -> int:
        """Хелпер: COUNT-запрос, возвращает int."""
        async with self.db.execute(sql, params) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def compute_funnel(self) -> dict:
        """
        Activation funnel (progress-based steps) + event-based steps + step conversion.

        Returns: {
            "total_registered": int,
            "steps": [{"name", "count", "pct", "conv_from_prev"}, ...],
            "event_steps": [{"name", "count", "pct", "conv_from_prev"}, ...],
        }
        pct — от total_registered; conv_from_prev — от предыдущего шага (None для первого).
        """
        total = await self._count("SELECT COUNT(*) FROM users")
        if total == 0:
            return {"total_registered": 0, "steps": [], "event_steps": []}

        steps_raw = [
            ("Registered", total),
            ("Started studying (≥1 session)", await self._count(
                "SELECT COUNT(*) FROM users WHERE total_sessions >= 1"
            )),
            ("Picked subject (events)", await self._count(
                "SELECT COUNT(DISTINCT user_id) FROM events "
                "WHERE event_name = 'subject_picked'"
            )),
            ("Picked mode (events)", await self._count(
                "SELECT COUNT(DISTINCT user_id) FROM events "
                "WHERE event_name = 'mode_picked'"
            )),
            ("≥1 quiz answer (events)", await self._count(
                "SELECT COUNT(DISTINCT user_id) FROM events "
                "WHERE event_name = 'quiz_answered'"
            )),
            ("≥1 flashcard review (events)", await self._count(
                "SELECT COUNT(DISTINCT user_id) FROM events "
                "WHERE event_name = 'flashcard_reviewed'"
            )),
            ("≥1 productivity tip (events)", await self._count(
                "SELECT COUNT(DISTINCT user_id) FROM events "
                "WHERE event_name = 'tip_viewed'"
            )),
            ("Reached 5+ sessions", await self._count(
                "SELECT COUNT(*) FROM users WHERE total_sessions >= 5"
            )),
            ("Reached 10+ sessions", await self._count(
                "SELECT COUNT(*) FROM users WHERE total_sessions >= 10"
            )),
            ("Earned 3-day streak achievement", await self._count(
                "SELECT COUNT(*) FROM user_achievements "
                "WHERE achievement_id = '3_day_streak' AND completed = 1"
            )),
            ("Earned 7-day streak achievement", await self._count(
                "SELECT COUNT(*) FROM user_achievements "
                "WHERE achievement_id = '7_day_streak' AND completed = 1"
            )),
        ]
        steps = self._funnel_steps_with_conversion(steps_raw, total)

        event_steps_raw = [
            ("Registered", total),
            ("session_started", await self._count(
                "SELECT COUNT(DISTINCT user_id) FROM events "
                "WHERE event_name = 'session_started'"
            )),
            ("subject_picked", await self._count(
                "SELECT COUNT(DISTINCT user_id) FROM events "
                "WHERE event_name = 'subject_picked'"
            )),
            ("mode_picked", await self._count(
                "SELECT COUNT(DISTINCT user_id) FROM events "
                "WHERE event_name = 'mode_picked'"
            )),
            ("quiz_answered", await self._count(
                "SELECT COUNT(DISTINCT user_id) FROM events "
                "WHERE event_name = 'quiz_answered'"
            )),
            ("flashcard_reviewed", await self._count(
                "SELECT COUNT(DISTINCT user_id) FROM events "
                "WHERE event_name = 'flashcard_reviewed'"
            )),
            ("tip_viewed", await self._count(
                "SELECT COUNT(DISTINCT user_id) FROM events "
                "WHERE event_name = 'tip_viewed'"
            )),
        ]
        event_steps = self._funnel_steps_with_conversion(event_steps_raw, total)
        return {
            "total_registered": total,
            "steps": steps,
            "event_steps": event_steps,
        }

    @staticmethod
    def _funnel_steps_with_conversion(
        steps_raw: list[tuple[str, int]], total: int
    ) -> list[dict]:
        steps = []
        prev_count = None
        for name, count in steps_raw:
            conv = (count / prev_count) if prev_count and prev_count > 0 else None
            steps.append({
                "name": name,
                "count": count,
                "pct": count / total if total else 0.0,
                "conv_from_prev": conv,
            })
            prev_count = count
        return steps

    async def compute_engagement(self) -> dict:
        """
        DAU / WAU / MAU + новые пользователи сегодня + stickiness ratio.

        Стандартные метрики engagement:
          DAU = users с любой активностью сегодня
          WAU = последние 7 дней включая сегодня
          MAU = последние 30 дней включая сегодня
          stickiness = DAU / MAU (типичный «good» ~20%+ для consumer apps)

        Returns: {
            "today": str, "new_today": int,
            "dau", "wau", "mau", "stickiness" — activity_progress,
            "dau_events", "wau_events", "mau_events", "stickiness_events",
            "activity_metric_definitions": dict,
            "total_users": int,
        }
        """
        activity = await self._all_activity_dates_per_user()
        activity_events = await self._all_activity_dates_per_user_events()
        today = datetime.now().date()
        w_cutoff = today - timedelta(days=6)
        m_cutoff = today - timedelta(days=29)

        def _engagement_counts(act: dict[int, set]) -> tuple[int, int, int]:
            d = sum(1 for dates in act.values() if today in dates)
            w = sum(1 for dates in act.values() if any(dd >= w_cutoff for dd in dates))
            m = sum(1 for dates in act.values() if any(dd >= m_cutoff for dd in dates))
            return d, w, m

        dau, wau, mau = _engagement_counts(activity)
        dau_ev, wau_ev, mau_ev = _engagement_counts(activity_events)
        stickiness = (dau / mau) if mau > 0 else None
        stickiness_events = (dau_ev / mau_ev) if mau_ev > 0 else None

        # Используем Python's today() вместо SQL date('now') — иначе SQLite
        # сравнит с UTC, а users.created_at может быть в other TZ context.
        # Для consistency со всей остальной engagement-логикой используем local time.
        today_str = today.strftime("%Y-%m-%d")
        new_today = await self._count(
            "SELECT COUNT(*) FROM users WHERE date(created_at) = ?",
            (today_str,),
        )
        total_users = await self._count("SELECT COUNT(*) FROM users")
        return {
            "today": today.strftime("%Y-%m-%d"),
            "new_today": new_today,
            "dau": dau,
            "wau": wau,
            "mau": mau,
            "stickiness": stickiness,
            "dau_events": dau_ev,
            "wau_events": wau_ev,
            "mau_events": mau_ev,
            "stickiness_events": stickiness_events,
            "activity_metric_definitions": ACTIVITY_METRIC_DEFINITIONS,
            "total_users": total_users,
        }

    async def compute_feature_usage(self) -> dict:
        """
        Feature adoption: % пользователей, использовавших каждую фичу.

        Считаем «использовал» как «есть запись в соответствующей таблице».
        Для timezone и notification settings — отклонение от дефолтов.

        Returns: {
            "total_users": int,
            "features": [{"name": str, "count": int, "pct": float}, ...]
        }
        """
        total = await self._count("SELECT COUNT(*) FROM users")
        if total == 0:
            return {"total_users": 0, "features": []}

        items = [
            ("🎯 Situational quizzes (≥1 ответ)", await self._count(
                "SELECT COUNT(DISTINCT user_id) FROM quiz_progress"
            )),
            ("🃏 Flashcards (≥1 ревью)", await self._count(
                "SELECT COUNT(DISTINCT user_id) FROM flashcard_progress"
            )),
            ("❓ MCQ (≥1 ответ)", await self._count(
                "SELECT COUNT(DISTINCT user_id) FROM mcq_progress"
            )),
            ("📷 Photo tasks (≥1 попытка)", await self._count(
                "SELECT COUNT(DISTINCT user_id) FROM task_progress"
            )),
            ("⏱️ Pomodoro (≥1 сессия)", await self._count(
                "SELECT COUNT(DISTINCT user_id) FROM study_sessions"
            )),
            ("🌍 Изменил часовой пояс", await self._count(
                "SELECT COUNT(*) FROM users WHERE timezone != 'Europe/Moscow'"
            )),
            ("🔕 Отключил хотя бы одно уведомление", await self._count(
                "SELECT COUNT(*) FROM notification_settings WHERE "
                "morning_enabled = 0 OR evening_enabled = 0 OR "
                "streak_enabled = 0 OR achievements_enabled = 0"
            )),
            ("⏰ Изменил время напоминаний", await self._count(
                "SELECT COUNT(*) FROM notification_settings WHERE "
                "morning_time != '09:00' OR evening_time != '21:00'"
            )),
            ("📇 Свои флэш-карточки (≥1)", await self._count(
                "SELECT COUNT(DISTINCT user_id) FROM user_flashcards"
            )),
            ("🎓 Советы (≥1 просмотр)", await self._count(
                "SELECT COUNT(*) FROM user_tips_stats WHERE total_views > 0"
            )),
            ("🐾 Питомец создан", await self._count(
                "SELECT COUNT(*) FROM user_pet"
            )),
            ("👥 ≥1 друг", await self._count(
                "SELECT COUNT(*) FROM ("
                "  SELECT user_a AS uid FROM friendships "
                "  UNION "
                "  SELECT user_b AS uid FROM friendships"
                ")"
            )),
            ("🏆 Weekly leaderboard (≥1 неделя)", await self._count(
                "SELECT COUNT(DISTINCT user_id) FROM weekly_scores"
            )),
            ("🃏 Источник карт ≠ mix", await self._count(
                "SELECT COUNT(*) FROM notification_settings "
                "WHERE flashcard_source IS NOT NULL AND flashcard_source != 'mix'"
            )),
        ]
        return {
            "total_users": total,
            "features": [
                {"name": name, "count": count, "pct": count / total}
                for name, count in items
            ],
        }

    # Whitelist таблиц для /export — защита от SQL-инъекций (имя таблицы
    # не параметризуется в SQLite). Ключи — короткие алиасы для UX.
    EXPORTABLE_TABLES: dict[str, str] = {
        "users": "users",
        "sessions": "study_sessions",
        "achievements": "user_achievements",
        "quiz": "quiz_progress",
        "flashcards": "flashcard_progress",
        "mcq": "mcq_progress",
        "tasks": "task_progress",
        "subject_stats": "user_subject_stats",
        "settings": "notification_settings",
        "events": "events",
        "user_flashcards": "user_flashcards",
        "tips_stats": "user_tips_stats",
        "tips_seen": "user_tips_seen",
        "pet": "user_pet",
        "pet_inventory": "user_pet_inventory",
        "friendships": "friendships",
        "friend_requests": "friend_requests",
        "weekly_scores": "weekly_scores",
        "weekly_badges": "weekly_badges",
        "streak_freezes": "streak_freezes",
    }

    async def export_table_csv(self, table_alias: str) -> tuple[bytes, int]:
        """
        Экспорт таблицы как CSV (UTF-8 bytes) + кол-во data-rows.
        Возвращает (csv_bytes, row_count). Если alias неизвестен — KeyError.
        """
        import csv
        import io
        if table_alias not in self.EXPORTABLE_TABLES:
            raise KeyError(f"Unknown table alias: {table_alias}")
        table = self.EXPORTABLE_TABLES[table_alias]
        async with self.db.execute(f"SELECT * FROM {table}") as cursor:
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(cols)
        for row in rows:
            writer.writerow([row[c] for c in cols])
        return buf.getvalue().encode("utf-8"), len(rows)

    async def compute_segments(self, churned_days: int = 14) -> dict:
        """
        User segmentation на 5 cohort'ов по уровню вовлечённости.

        Логика приоритезации (first match wins):
          - never_started: total_sessions == 0
          - churned: last_activity > churned_days назад (любая категория ниже,
            кроме never_started, может стать churned)
          - power: total_sessions ≥ 10
          - active: total_sessions 3-9
          - tried: total_sessions 1-2

        churned приоритетнее категории по sessions — это actionable
        retention-сигнал. Активный пользователь, не заходивший 2 недели,
        важнее чем «он active по числу сессий» для re-engagement.

        Returns: {
            "total_users": int,
            "segments": [
                {"name": str, "count": int, "pct": float},
                ...
            ],
            "churned_days_threshold": int,
        }
        """
        total = await self._count("SELECT COUNT(*) FROM users")
        if total == 0:
            return {"total_users": 0, "segments": [], "churned_days_threshold": churned_days}

        # last_activity per user из union всех progress-таблиц
        activity = await self._all_activity_dates_per_user()
        today = datetime.now().date()
        cutoff = today - timedelta(days=churned_days)

        # users + total_sessions
        async with self.db.execute("SELECT user_id, total_sessions FROM users") as cursor:
            user_rows = await cursor.fetchall()

        segments = {"never_started": 0, "tried": 0, "active": 0, "power": 0, "churned": 0}
        for row in user_rows:
            uid = row["user_id"]
            sessions = row["total_sessions"] or 0
            if sessions == 0:
                segments["never_started"] += 1
                continue
            # User is in tried/active/power based on sessions
            tentative = "power" if sessions >= 10 else "active" if sessions >= 3 else "tried"
            # Check churn (only matters if user had activity at all)
            user_dates = activity.get(uid, set())
            if user_dates:
                last_seen = max(user_dates)
                if last_seen < cutoff:
                    segments["churned"] += 1
                    continue
            # No activity recorded → still tentative (registered + did Pomodoro but no progress events)
            segments[tentative] += 1

        # Stable display order
        order = [
            ("never_started", "Never started (0 sessions)"),
            ("tried",         "Tried (1-2 sessions)"),
            ("active",        "Active (3-9 sessions)"),
            ("power",         "Power (≥10 sessions)"),
            ("churned",       f"Churned (>{churned_days}d inactive)"),
        ]
        return {
            "total_users": total,
            "segments": [
                {"name": label, "count": segments[key], "pct": segments[key] / total}
                for key, label in order
            ],
            "churned_days_threshold": churned_days,
        }

    async def compute_content_stats(self, top_n: int = 5) -> dict:
        """
        Content effectiveness statistics:
          - hardest_situational: terms с самым низким avg_accuracy (was_correct)
          - most_attempted_mcq: questions с максимальным total_count
          - unused_counts: сколько items не имеют записи в соответствующей progress-таблице
          - flashcard_ef_distribution: гистограмма ease_factor по бакетам

        Hash → text mapping ДЕЛАЕТ caller (bot.py знает про content files).
        Здесь только pure SQL.
        """
        # 1. Hardest situational: avg is_correct ascending. Нужны как minimum 2 попытки чтобы был сигнал.
        async with self.db.execute(
            "SELECT term_hash, "
            "       COUNT(*) AS attempts, "
            "       AVG(CAST(is_correct AS REAL)) AS accuracy "
            "FROM quiz_progress "
            "GROUP BY term_hash "
            "HAVING COUNT(*) >= 1 "
            "ORDER BY accuracy ASC, attempts DESC "
            "LIMIT ?",
            (top_n,),
        ) as cursor:
            hardest = [
                {"term_hash": r["term_hash"], "attempts": r["attempts"], "accuracy": r["accuracy"]}
                for r in await cursor.fetchall()
            ]

        # 2. Most-attempted MCQ
        async with self.db.execute(
            "SELECT question_hash, "
            "       SUM(total_count) AS attempts, "
            "       CAST(SUM(correct_count) AS REAL) / NULLIF(SUM(total_count), 0) AS accuracy "
            "FROM mcq_progress "
            "GROUP BY question_hash "
            "ORDER BY attempts DESC "
            "LIMIT ?",
            (top_n,),
        ) as cursor:
            popular_mcq = [
                {"question_hash": r["question_hash"], "attempts": r["attempts"], "accuracy": r["accuracy"]}
                for r in await cursor.fetchall()
            ]

        # 3. Counts of users-with-progress per mode (для оценки unused). Конкретные «unused items»
        # требуют знания контента — делается в render слое.
        unused_counts = {
            "situational_terms_attempted": await self._count(
                "SELECT COUNT(DISTINCT term_hash) FROM quiz_progress"
            ),
            "flashcards_reviewed": await self._count(
                "SELECT COUNT(DISTINCT card_hash) FROM flashcard_progress"
            ),
            "mcq_questions_seen": await self._count(
                "SELECT COUNT(DISTINCT question_hash) FROM mcq_progress"
            ),
            "tasks_attempted": await self._count(
                "SELECT COUNT(DISTINCT task_id) FROM task_progress"
            ),
        }

        # 4. EF distribution в flashcards: бакеты [1.3-1.5], [1.5-2.0], [2.0-2.5], [≥2.5]
        async with self.db.execute(
            "SELECT "
            "  SUM(CASE WHEN ease_factor < 1.5 THEN 1 ELSE 0 END) AS low, "
            "  SUM(CASE WHEN ease_factor >= 1.5 AND ease_factor < 2.0 THEN 1 ELSE 0 END) AS medlow, "
            "  SUM(CASE WHEN ease_factor >= 2.0 AND ease_factor < 2.5 THEN 1 ELSE 0 END) AS medhigh, "
            "  SUM(CASE WHEN ease_factor >= 2.5 THEN 1 ELSE 0 END) AS high, "
            "  COUNT(*) AS total "
            "FROM flashcard_progress"
        ) as cursor:
            row = await cursor.fetchone()
            ef_dist = {
                "lt_1_5":   row["low"] or 0,
                "1_5_to_2": row["medlow"] or 0,
                "2_to_2_5": row["medhigh"] or 0,
                "gte_2_5":  row["high"] or 0,
                "total":    row["total"] or 0,
            }

        # 5. Subject engagement (visits)
        async with self.db.execute(
            "SELECT subject_id, "
            "       COUNT(DISTINCT user_id) AS users, "
            "       SUM(visits) AS total_visits "
            "FROM user_subject_stats "
            "GROUP BY subject_id "
            "ORDER BY total_visits DESC "
            "LIMIT ?",
            (top_n,),
        ) as cursor:
            subject_engagement = [
                {
                    "subject_id": r["subject_id"],
                    "users": r["users"],
                    "total_visits": r["total_visits"],
                }
                for r in await cursor.fetchall()
            ]

        # 6. Official vs user flashcards (hash prefix u)
        async with self.db.execute(
            "SELECT "
            "  SUM(CASE WHEN card_hash LIKE 'u%' THEN 1 ELSE 0 END) AS user_cards, "
            "  SUM(CASE WHEN card_hash NOT LIKE 'u%' THEN 1 ELSE 0 END) AS official_cards, "
            "  COUNT(*) AS total "
            "FROM flashcard_progress"
        ) as cursor:
            fc_row = await cursor.fetchone()
            flashcard_hash_split = {
                "user_cards": fc_row["user_cards"] or 0,
                "official_cards": fc_row["official_cards"] or 0,
                "total": fc_row["total"] or 0,
            }

        # 7. Top tips (events)
        import json
        top_tips = []
        async with self.db.execute(
            "SELECT properties FROM events WHERE event_name = 'tip_viewed'"
        ) as cursor:
            tip_counts: dict[str, int] = defaultdict(int)
            for r in await cursor.fetchall():
                try:
                    props = json.loads(r["properties"]) if r["properties"] else {}
                except (json.JSONDecodeError, TypeError):
                    props = {}
                tip_id = props.get("tip_id") or "unknown"
                tip_counts[str(tip_id)] += 1
            top_tips = [
                {"tip_id": tid, "views": cnt}
                for tid, cnt in sorted(tip_counts.items(), key=lambda x: -x[1])[:top_n]
            ]

        return {
            "hardest_situational": hardest,
            "most_attempted_mcq": popular_mcq,
            "progress_coverage": unused_counts,
            "flashcard_ef_distribution": ef_dist,
            "subject_engagement": subject_engagement,
            "flashcard_hash_split": flashcard_hash_split,
            "top_tips": top_tips,
        }

    async def compute_activation_metrics(self) -> dict:
        """
        Time-to-value и доли быстрой активации (из events + users).

        Returns: {
            "users_with_signup": int,
            "time_to_hours": {event_name: {"median": float|None, "p75": float|None, "n": int}},
            "pct_first_session_within_24h": float | None,
            "pct_first_session_within_7d": float | None,
        }
        """
        async with self.db.execute(
            "SELECT user_id, created_at FROM users"
        ) as cursor:
            signup_rows = await cursor.fetchall()

        signups: dict[int, datetime] = {}
        for row in signup_rows:
            try:
                signups[row["user_id"]] = datetime.strptime(
                    row["created_at"], "%Y-%m-%d %H:%M:%S"
                )
            except (ValueError, TypeError):
                continue

        if not signups:
            return {
                "users_with_signup": 0,
                "time_to_hours": {},
                "pct_first_session_within_24h": None,
                "pct_first_session_within_7d": None,
            }

        event_names = (
            "session_started",
            "subject_picked",
            "mode_picked",
            "quiz_answered",
            "flashcard_reviewed",
            "tip_viewed",
        )
        first_event_at: dict[str, dict[int, datetime]] = {
            en: {} for en in event_names
        }
        async with self.db.execute(
            "SELECT user_id, event_name, created_at FROM events "
            "WHERE user_id IS NOT NULL"
        ) as cursor:
            event_rows = await cursor.fetchall()

        for row in event_rows:
            en = row["event_name"]
            if en not in first_event_at:
                continue
            uid = row["user_id"]
            try:
                ts = datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                continue
            if uid not in first_event_at[en] or ts < first_event_at[en][uid]:
                first_event_at[en][uid] = ts

        def _percentile_hours(values: list[float], p: float) -> float | None:
            if not values:
                return None
            values = sorted(values)
            idx = min(len(values) - 1, int(len(values) * p))
            return values[idx]

        time_to_hours = {}
        for en in event_names:
            deltas = []
            for uid, signup_ts in signups.items():
                first = first_event_at[en].get(uid)
                if not first or first < signup_ts:
                    continue
                deltas.append((first - signup_ts).total_seconds() / 3600.0)
            time_to_hours[en] = {
                "median": _percentile_hours(deltas, 0.5),
                "p75": _percentile_hours(deltas, 0.75),
                "n": len(deltas),
            }

        session_deltas = []
        for uid, signup_ts in signups.items():
            first = first_event_at["session_started"].get(uid)
            if not first or first < signup_ts:
                continue
            session_deltas.append((first - signup_ts).total_seconds() / 3600.0)

        n_signup = len(signups)
        within_24h = sum(1 for h in session_deltas if h <= 24)
        within_7d = sum(1 for h in session_deltas if h <= 24 * 7)
        n_session = len(session_deltas)

        return {
            "users_with_signup": n_signup,
            "time_to_hours": time_to_hours,
            "pct_first_session_within_24h": (
                within_24h / n_signup if n_signup else None
            ),
            "pct_first_session_within_7d": (
                within_7d / n_signup if n_signup else None
            ),
            "users_with_first_session": n_session,
        }

    async def compute_product_metrics(self, top_n: int = 8) -> dict:
        """
        Продуктовые метрики: breakdown по subject/mode, strict funnel,
        activation по когортам, feature retention D7, утренний push,
        leaderboard, notification funnel.
        """
        import json
        from collections import defaultdict

        total = await self._count("SELECT COUNT(*) FROM users")
        if total == 0:
            return {"total_registered": 0}

        # --- Funnel by subject (events.subject_id) ---
        by_subject = []
        async with self.db.execute(
            "SELECT subject_id, COUNT(DISTINCT user_id) AS users "
            "FROM events WHERE event_name = 'subject_picked' "
            "AND subject_id IS NOT NULL "
            "GROUP BY subject_id ORDER BY users DESC LIMIT ?",
            (top_n,),
        ) as cursor:
            for row in await cursor.fetchall():
                sid = row["subject_id"]
                mode_u = await self._count(
                    "SELECT COUNT(DISTINCT user_id) FROM events "
                    "WHERE event_name = 'mode_picked' AND subject_id = ?",
                    (sid,),
                )
                quiz_u = await self._count(
                    "SELECT COUNT(DISTINCT user_id) FROM events "
                    "WHERE event_name = 'quiz_answered' AND subject_id = ?",
                    (sid,),
                )
                by_subject.append({
                    "subject_id": sid,
                    "picked_subject": row["users"],
                    "picked_mode": mode_u,
                    "quiz_answered": quiz_u,
                    "pct_registered": row["users"] / total,
                })

        # --- Funnel by mode ---
        by_mode = []
        async with self.db.execute(
            "SELECT mode, COUNT(DISTINCT user_id) AS users "
            "FROM events WHERE event_name = 'mode_picked' "
            "AND mode IS NOT NULL "
            "GROUP BY mode ORDER BY users DESC"
        ) as cursor:
            for row in await cursor.fetchall():
                by_mode.append({
                    "mode": row["mode"],
                    "users": row["users"],
                    "pct_registered": row["users"] / total,
                })

        # --- Strict event funnel (ever did all steps 1..k) ---
        strict_order = [
            "session_started",
            "subject_picked",
            "mode_picked",
            "quiz_answered",
            "flashcard_reviewed",
        ]
        user_events: dict[int, set[str]] = defaultdict(set)
        async with self.db.execute(
            "SELECT user_id, event_name FROM events WHERE user_id IS NOT NULL"
        ) as cursor:
            for row in await cursor.fetchall():
                user_events[row["user_id"]].add(row["event_name"])

        strict_steps = [{"name": "Registered", "count": total, "pct_registered": 1.0}]
        prev = total
        cumulative_required = set()
        for en in strict_order:
            cumulative_required.add(en)
            count = sum(
                1 for evs in user_events.values()
                if cumulative_required.issubset(evs)
            )
            strict_steps.append({
                "name": en,
                "count": count,
                "pct_registered": count / total,
                "pct_of_prev": count / prev if prev else None,
            })
            prev = count

        # --- Activation by signup cohort (ISO week) ---
        signups: dict[int, tuple[datetime, str]] = {}
        async with self.db.execute("SELECT user_id, created_at FROM users") as cursor:
            for row in await cursor.fetchall():
                try:
                    dt = datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S")
                    iso_year, iso_week, _ = dt.date().isocalendar()
                    signups[row["user_id"]] = (
                        dt,
                        f"{iso_year}-W{iso_week:02d}",
                    )
                except (ValueError, TypeError):
                    continue

        first_session: dict[int, datetime] = {}
        async with self.db.execute(
            "SELECT user_id, MIN(created_at) AS ts FROM events "
            "WHERE event_name = 'session_started' GROUP BY user_id"
        ) as cursor:
            for row in await cursor.fetchall():
                try:
                    first_session[row["user_id"]] = datetime.strptime(
                        row["ts"], "%Y-%m-%d %H:%M:%S"
                    )
                except (ValueError, TypeError):
                    continue

        cohort_acc: dict[str, list[float]] = defaultdict(list)
        cohort_24h: dict[str, list[bool]] = defaultdict(list)
        for uid, (signup_dt, week) in signups.items():
            fs = first_session.get(uid)
            if not fs or fs < signup_dt:
                continue
            hours = (fs - signup_dt).total_seconds() / 3600.0
            cohort_acc[week].append(hours)
            cohort_24h[week].append(hours <= 24)

        def _median(vals: list[float]) -> float | None:
            if not vals:
                return None
            s = sorted(vals)
            return s[len(s) // 2]

        activation_by_cohort = []
        for week in sorted(cohort_acc.keys())[-top_n:]:
            hours_list = cohort_acc[week]
            flags = cohort_24h[week]
            activation_by_cohort.append({
                "week": week,
                "users_with_session": len(hours_list),
                "median_hours_to_session": _median(hours_list),
                "pct_session_within_24h": (
                    sum(flags) / len(flags) if flags else None
                ),
            })

        # --- Feature retention D7 (active on signup+7) ---
        activity = await self._all_activity_dates_per_user()
        today = datetime.now().date()

        async def _feature_user_sets() -> dict[str, set[int]]:
            sets: dict[str, set[int]] = {}
            async with self.db.execute(
                "SELECT DISTINCT user_id FROM events WHERE event_name = 'tip_viewed'"
            ) as c:
                sets["tips"] = {r["user_id"] for r in await c.fetchall()}
            async with self.db.execute(
                "SELECT DISTINCT user_id FROM user_flashcards"
            ) as c:
                sets["own_flashcards"] = {r["user_id"] for r in await c.fetchall()}
            async with self.db.execute("SELECT user_id FROM user_pet") as c:
                sets["pet"] = {r["user_id"] for r in await c.fetchall()}
            async with self.db.execute(
                "SELECT user_a AS uid FROM friendships "
                "UNION SELECT user_b AS uid FROM friendships"
            ) as c:
                sets["friends"] = {r["uid"] for r in await c.fetchall()}
            return sets

        feature_sets = await _feature_user_sets()
        feature_retention = []
        for label, with_set in feature_sets.items():
            eligible_with = eligible_without = retained_with = retained_without = 0
            for uid, signup_dt in ((u, s[0]) for u, s in signups.items()):
                if (today - signup_dt.date()).days < 8:
                    continue
                target_day = signup_dt.date() + timedelta(days=7)
                active_d7 = target_day in activity.get(uid, set())
                has_feat = uid in with_set
                if has_feat:
                    eligible_with += 1
                    if active_d7:
                        retained_with += 1
                else:
                    eligible_without += 1
                    if active_d7:
                        retained_without += 1
            feature_retention.append({
                "feature": label,
                "with_feature": {
                    "eligible": eligible_with,
                    "retained_d7": retained_with,
                    "rate": (
                        retained_with / eligible_with if eligible_with else None
                    ),
                },
                "without_feature": {
                    "eligible": eligible_without,
                    "retained_d7": retained_without,
                    "rate": (
                        retained_without / eligible_without
                        if eligible_without else None
                    ),
                },
            })

        # --- Morning reminder → session same calendar day ---
        morning_by_user_date: set[tuple[int, str]] = set()
        async with self.db.execute(
            "SELECT user_id, properties, date(created_at) AS d FROM events "
            "WHERE event_name = 'reminder_sent'"
        ) as cursor:
            for row in await cursor.fetchall():
                if not row["user_id"] or not row["d"]:
                    continue
                try:
                    props = json.loads(row["properties"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    props = {}
                if props.get("kind") == "morning":
                    morning_by_user_date.add((row["user_id"], row["d"]))

        session_by_user_date: set[tuple[int, str]] = set()
        async with self.db.execute(
            "SELECT user_id, date(created_at) AS d FROM events "
            "WHERE event_name = 'session_started'"
        ) as cursor:
            for row in await cursor.fetchall():
                if row["user_id"] and row["d"]:
                    session_by_user_date.add((row["user_id"], row["d"]))

        morning_pairs = len(morning_by_user_date)
        morning_then_session = len(
            morning_by_user_date & session_by_user_date
        )

        # --- Leaderboard ---
        lb_users_score = await self._count(
            "SELECT COUNT(DISTINCT user_id) FROM weekly_scores"
        )
        lb_hidden = await self._count(
            "SELECT COUNT(*) FROM users WHERE hidden_from_leaderboards = 1"
        )
        lb_freeze_rows = await self._count("SELECT COUNT(*) FROM streak_freezes")
        lb_freeze_users = await self._count(
            "SELECT COUNT(DISTINCT user_id) FROM streak_freezes"
        )
        lb_viewed = await self._count(
            "SELECT COUNT(DISTINCT user_id) FROM events "
            "WHERE event_name = 'leaderboard_viewed'"
        )

        # --- Notification funnel ---
        morning_enabled = await self._count(
            "SELECT COUNT(*) FROM notification_settings WHERE morning_enabled = 1"
        )
        evening_enabled = await self._count(
            "SELECT COUNT(*) FROM notification_settings WHERE evening_enabled = 1"
        )
        morning_sent_users = len({uid for uid, _ in morning_by_user_date})
        async with self.db.execute(
            "SELECT DISTINCT user_id, properties FROM events "
            "WHERE event_name = 'reminder_sent'"
        ) as cursor:
            evening_uids: set[int] = set()
            for row in await cursor.fetchall():
                try:
                    props = json.loads(row["properties"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    props = {}
                if props.get("kind") == "evening" and row["user_id"]:
                    evening_uids.add(row["user_id"])
            evening_sent_users = len(evening_uids)
        notif_funnel = [
            {"step": "Registered", "count": total},
            {"step": "Morning reminders ON", "count": morning_enabled},
            {"step": "Evening reminders ON", "count": evening_enabled},
            {"step": "Got morning push (events)", "count": morning_sent_users},
            {"step": "Got evening push (events)", "count": evening_sent_users},
            {
                "step": "Session same day as morning push",
                "count": morning_then_session,
            },
        ]

        return {
            "total_registered": total,
            "funnel_by_subject": by_subject,
            "funnel_by_mode": by_mode,
            "strict_event_funnel": strict_steps,
            "activation_by_cohort": activation_by_cohort,
            "feature_retention_d7": feature_retention,
            "morning_reminder_effect": {
                "morning_push_pairs": morning_pairs,
                "same_day_session": morning_then_session,
                "conversion_rate": (
                    morning_then_session / morning_pairs
                    if morning_pairs else None
                ),
            },
            "leaderboard": {
                "users_with_weekly_score": lb_users_score,
                "leaderboard_viewed_users": lb_viewed,
                "hidden_from_leaderboard": lb_hidden,
                "freeze_purchases": lb_freeze_rows,
                "users_bought_freeze": lb_freeze_users,
            },
            "notification_funnel": notif_funnel,
        }

    async def compute_event_timeline(self, hours: int = 24, limit: int = 50) -> list[dict]:
        """
        Последние N events из events table за последние `hours` часов.
        Returns list of dicts ordered by created_at DESC.
        """
        import json
        cutoff = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        async with self.db.execute(
            "SELECT id, user_id, event_name, properties, created_at "
            "FROM events "
            "WHERE created_at >= ? "
            "ORDER BY created_at DESC "
            "LIMIT ?",
            (cutoff, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        result = []
        for r in rows:
            try:
                props = json.loads(r["properties"]) if r["properties"] else {}
            except (json.JSONDecodeError, TypeError):
                props = {}
            result.append({
                "id": r["id"],
                "user_id": r["user_id"],
                "event_name": r["event_name"],
                "properties": props,
                "created_at": r["created_at"],
            })
        return result

    async def compute_heatmap(self, days: int = 30) -> dict:
        """
        Activity heatmap: events bucketed by (weekday, hour_bucket) за последние N дней.

        Использует 3-часовые бакеты: 8 колонок × 7 строк (weekdays). Подходит для
        Telegram <pre> отображения на мобиле без переноса строк.

        Returns: {
            "grid": [[int, int, ...], ...],     # 7 rows × 8 cols
            "weekday_labels": ["Mon", ..., "Sun"],
            "hour_labels": ["00", "03", ..., "21"],
            "total_events": int,
            "days": int,
            "peak": {"weekday": str, "hour_bucket": int, "count": int} | None,
        }
        """
        from collections import defaultdict
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        async with self.db.execute(
            "SELECT created_at FROM events WHERE created_at >= ?",
            (cutoff,),
        ) as cursor:
            rows = await cursor.fetchall()

        # 7 × 8 grid (weekday × 3-hour bucket)
        grid = [[0] * 8 for _ in range(7)]
        weekday_labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        hour_labels = [f"{h:02d}" for h in range(0, 24, 3)]

        for row in rows:
            try:
                ts = datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                continue
            wd = ts.weekday()           # 0=Mon ... 6=Sun
            hb = ts.hour // 3           # 0..7
            grid[wd][hb] += 1

        total = sum(sum(row) for row in grid)
        peak = None
        if total > 0:
            peak_count = max(max(row) for row in grid)
            for wd_idx, row in enumerate(grid):
                for hb, c in enumerate(row):
                    if c == peak_count:
                        peak = {
                            "weekday": weekday_labels[wd_idx],
                            "hour_bucket": hb,
                            "hour_range": f"{hour_labels[hb]}:00-{(hb*3 + 3) % 24:02d}:59",
                            "count": peak_count,
                        }
                        break
                if peak:
                    break

        return {
            "grid": grid,
            "weekday_labels": weekday_labels,
            "hour_labels": hour_labels,
            "total_events": total,
            "days": days,
            "peak": peak,
        }

    async def export_all_tables_zip(self, schema_version: str = "v0.8") -> tuple[bytes, dict]:
        """
        Bundles all exportable tables + metadata.json into a ZIP archive.

        Returns: (zip_bytes, metadata_dict).

        metadata.json schema:
            {
                "exported_at": "ISO-8601 UTC timestamp",
                "schema_version": "v0.7",
                "row_counts": {table_name: int, ...},
                "tables": [list of table_names in zip],
            }

        Used by /export all admin command — позволяет одной командой выгрузить
        весь analytics-dataset для внешнего Jupyter/pandas анализа.
        Воспроизводимость: metadata.json фиксирует timestamp и row-counts.
        """
        import io
        import json
        import zipfile
        from datetime import datetime, timezone

        metadata = {
            "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "schema_version": schema_version,
            "row_counts": {},
            "tables": [],
        }

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for alias, table_name in self.EXPORTABLE_TABLES.items():
                csv_bytes, row_count = await self.export_table_csv(alias)
                zf.writestr(f"{table_name}.csv", csv_bytes)
                metadata["row_counts"][table_name] = row_count
                metadata["tables"].append(table_name)
            # metadata.json последним — содержит итоговые row_counts
            zf.writestr(
                "metadata.json",
                json.dumps(metadata, indent=2, ensure_ascii=False),
            )

        return buf.getvalue(), metadata


# ------------------------------------------------------------
# Backup service — ежедневный snapshot БД после обработки стриков.
# Использует SQLite VACUUM INTO: атомарно, без WAL-мусора, минимум
# места занимает. Dedup по server-local date — один backup на день,
# даже если streak_scheduler кросает 23:59 в нескольких TZ.
# ------------------------------------------------------------
class BackupService:
    """
    Делает ежедневный snapshot БД в указанную папку. Хранит N дней,
    удаляет старше.

    Используется из streak_scheduler: после каждого process_users_in_timezone
    вызывается maybe_backup_for_today() — фактический backup создаётся
    только один раз в день (по server-local дате).

    Также может быть вызван вручную через /backup admin-команду —
    в этом случае используется force_backup() с отдельным timestamp'ом
    в имени файла, чтобы не пересекаться с daily snapshot.
    """

    def __init__(self, db_path: str, backup_dir: str = "backups", retention_days: int = 30):
        self.db_path = db_path
        self.backup_dir = Path(backup_dir)
        self.retention_days = retention_days
        # In-memory dedup: server-local дата последнего сделанного backup'а.
        # Файл-маркер тоже работает (если файл за сегодня уже есть — skip),
        # но in-memory быстрее и не делает stat на каждый tick scheduler'а.
        self._last_backup_date: str | None = None

    async def maybe_backup_for_today(self) -> Path | None:
        """
        Создаёт snapshot если за сегодня (server-local date) ещё не было.
        Возвращает Path к новому файлу или None если backup уже был.
        Любая ошибка → лог, возврат None (не падаем — это side-effect).
        """
        today = datetime.now().strftime("%Y-%m-%d")
        if self._last_backup_date == today:
            return None
        target = self.backup_dir / f"studybuddy-{today}.db"
        # File-existence check на случай рестарта бота — после рестарта
        # _last_backup_date снова None, но файл за сегодня уже может быть.
        if target.exists():
            self._last_backup_date = today
            return None
        try:
            await self._vacuum_into(target)
            self._last_backup_date = today
            await self._cleanup_old()
            return target
        except Exception as e:
            logger.error(
                "backup.failed reason=%s detail=%s",
                type(e).__name__, e,
            )
            return None

    async def force_backup(self) -> Path | None:
        """
        Принудительный backup (для admin-команды /backup или critical moments).
        Имя файла включает timestamp до секунд, чтобы не затереть daily.
        """
        now = datetime.now()
        suffix = now.strftime("%Y-%m-%d-%H%M%S")
        target = self.backup_dir / f"studybuddy-manual-{suffix}.db"
        try:
            await self._vacuum_into(target)
            await self._cleanup_old()
            return target
        except Exception as e:
            logger.error(
                "backup.force_failed reason=%s detail=%s",
                type(e).__name__, e,
            )
            return None

    async def _vacuum_into(self, target_path: Path) -> None:
        """
        SQLite VACUUM INTO — атомарный snapshot. Открывает свой коннект,
        чтобы не вмешиваться в транзакции основного приложения.
        """
        target_path.parent.mkdir(parents=True, exist_ok=True)
        # VACUUM INTO не поддерживает параметризацию пути — приходится
        # инлайнить. Эскейпим одинарные кавычки на всякий случай.
        # На Windows backslashes в путях работают для SQLite OK.
        safe_path = str(target_path).replace("'", "''")
        t0 = datetime.now()
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute(f"VACUUM INTO '{safe_path}'")
        duration_ms = int((datetime.now() - t0).total_seconds() * 1000)
        size = target_path.stat().st_size if target_path.exists() else 0
        logger.info(
            "backup.created path=%s size=%s duration_ms=%s",
            target_path.name, size, duration_ms,
        )

    async def _cleanup_old(self) -> None:
        """Удаляет daily-backup-файлы старше retention_days. Manual не трогаем."""
        if not self.backup_dir.exists():
            return
        cutoff = (datetime.now() - timedelta(days=self.retention_days)).date()
        removed = 0
        for f in self.backup_dir.glob("studybuddy-*.db"):
            # Trim daily backups ("studybuddy-YYYY-MM-DD.db") по дате из имени;
            # manual ("studybuddy-manual-...") сохраняем — это intentional snapshots.
            name = f.stem  # без .db
            if name.startswith("studybuddy-manual-"):
                continue
            date_str = name.replace("studybuddy-", "")
            try:
                file_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            except ValueError:
                continue
            if file_date < cutoff:
                try:
                    f.unlink()
                    removed += 1
                except OSError as e:
                    logger.warning("backup.cleanup_skip file=%s reason=%s", f.name, e)
        if removed > 0:
            logger.info("backup.cleanup removed=%s retention_days=%s", removed, self.retention_days)