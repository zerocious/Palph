# services.py
import logging
import os
from collections import defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path
from time import monotonic

import aiosqlite
from aiogram.exceptions import TelegramForbiddenError

from repository import UserRepository, SessionRepository, PetRepository, LeaderboardRepository

logger = logging.getLogger("studybuddy_bot")


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

    def check(self, user_id: int) -> str:
        """Регистрирует event и возвращает ok/warn/block."""
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
# "5 эмоций выводятся из состояния пользователя в момент рендера
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
    Возвращает одну из 5 эмоций (priority по убыванию):
        1. "studying" — активный учебный таймер
        2. "excited"  — level-up или ачивка ≤ 5 минут назад
        3. "sad"      — пользователь сегодня ещё не учился
        4. "sleepy"   — локальное время в окне [22:00, 06:00)
                        (22:00 включительно, 06:00 исключительно)
        5. "happy"    — дефолт

    Все аргументы keyword-only: каждый caller-сайт обязан явно
    назвать что подаёт, чтобы не было перепутанных bool-аргументов.
    """
    if is_studying:
        return "studying"
    if recently_excited:
        return "excited"
    if not has_studied_today:
        return "sad"
    if now_local.hour >= 22 or now_local.hour < 6:
        return "sleepy"
    return "happy"


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
#   1. <emotion>_<color>_<accessory>.png — основной путь
#   2. <emotion>_orange_none.png — дефолтная комбинация
#   3. <emotion>_happy_none.png ИЛИ генерим первый существующий
#   4. raise FileNotFoundError — caller может graceful'но
#      деградировать до text-only message
# ------------------------------------------------------------
_ASSETS_PET_DIR = Path(__file__).resolve().parent / "assets" / "pet"


def render_pet(
    user_pet,            # dict с полями color, accessory ИЛИ None
    emotion: str,
    *,
    animated: bool = False,
) -> Path:
    """
    Returns Path к asset-файлу питомца. Pure: только path-resolution.

    `user_pet=None` трактуется как дефолт (orange + none) — для пользователей
    без созданного pet'a (добавляется auto в add_xp; до первой сессии row нет).

    `animated=True` → `<emotion>.gif` (один общий per emotion, без цвета/аксессуара,
    т.к. GIF используется в level-up/sad-reminder для драматического beat'а,
    конкретный цвет там не критичен).

    Возвращает Path. Существование файла проверяется внутри (fallback);
    raise FileNotFoundError если даже fallback'и отсутствуют (assets
    директория не была сгенерирована).
    """
    if animated:
        path = _ASSETS_PET_DIR / f"{emotion}.gif"
        if path.exists():
            return path
        # Animated fallback: happy.gif как универсальный нейтрал
        path = _ASSETS_PET_DIR / "happy.gif"
        if path.exists():
            return path
        raise FileNotFoundError(
            f"No animated assets for emotion={emotion!r}. "
            f"Run `python scripts/build_pet_assets.py` to generate them."
        )

    if user_pet is None:
        color = "orange"
        accessory = "none"
    else:
        color = user_pet.get("color", "orange")
        accessory = user_pet.get("accessory", "none")

    primary = _ASSETS_PET_DIR / f"{emotion}_{color}_{accessory}.png"
    if primary.exists():
        return primary

    # Fallback 1: дефолтная комбинация для этой эмоции
    fallback_default = _ASSETS_PET_DIR / f"{emotion}_orange_none.png"
    if fallback_default.exists():
        return fallback_default

    # Fallback 2: happy_orange_none — самая безопасная картинка
    fallback_happy = _ASSETS_PET_DIR / "happy_orange_none.png"
    if fallback_happy.exists():
        return fallback_happy

    raise FileNotFoundError(
        f"No pet assets found for emotion={emotion!r} color={color!r} "
        f"accessory={accessory!r}. Run `python scripts/build_pet_assets.py`."
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

            earned, bonus = await self.achievement_service.check_and_award(
                user_id, sessions, streak, total_minutes
            )

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
    def __init__(self, user_repo: UserRepository, bot):
        self.user_repo = user_repo
        self.bot = bot

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
                await self.bot.send_message(
                    chat_id=uid,
                    text=(
                        "🌅 Доброе утро!\n"
                        "Твой питомец ждёт первую сессию сегодня 🐾\n"
                        "Даже 5 минут — это уже победа."
                    ),
                )
            except TelegramForbiddenError:
                # Пользователь заблокировал бота — ожидаемо, INFO.
                logger.info(
                    "reminder.send_failed kind=morning uid=%s reason=blocked", uid
                )
            except Exception as e:
                logger.warning(
                    "reminder.send_failed kind=morning uid=%s reason=%s detail=%s",
                    uid, type(e).__name__, e,
                )

    async def _send_evening(self, tz: str, hhmm: str) -> None:
        users = await self.user_repo.get_users_due_for_evening(tz, hhmm)
        if users:
            logger.info(
                "reminder.evening.dispatched tz=%s hhmm=%s count=%s",
                tz, hhmm, len(users),
            )
        for u in users:
            uid = u["user_id"]
            try:
                await self.bot.send_message(
                    chat_id=uid,
                    text=(
                        "🌙 Вечер!\n"
                        "Сегодня ещё не было ни одной учебной сессии — "
                        "успей хотя бы 5 минут до полуночи, чтобы сохранить стрик 🔥"
                    ),
                )
            except TelegramForbiddenError:
                logger.info(
                    "reminder.send_failed kind=evening uid=%s reason=blocked", uid
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
        bonuses_total = 0
        for user in users:
            user_id = user["user_id"]
            has_studied = user["has_studied_today"]
            current_streak = user["current_streak"]

            if has_studied:
                new_streak = current_streak + 1
                # Бонус со второго дня стрика
                bonus = 15 if new_streak >= 2 else 0
                async with self.user_repo.db.lock:
                    await self.user_repo.apply_streak_increment(user_id, new_streak, bonus)
                incremented += 1
                bonuses_total += bonus

                # Уведомление (если передан bot)
                if self.bot and bonus > 0:
                    try:
                        await self.bot.send_message(
                            chat_id=user_id,
                            text=(
                                f"🌙 Добрый вечер! Твой стрик обновлён:\n"
                                f"🔥 {new_streak} дней подряд\n"
                                f"🪙 +{bonus} монет за упорство!"
                            )
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
                        # Notify пользователя что freeze отработал
                        if self.bot:
                            try:
                                await self.bot.send_message(
                                    chat_id=user_id,
                                    text=(
                                        f"❄️ Заморозка стрика сработала.\n"
                                        f"🔥 Стрик сохранён: {current_streak} дн."
                                    ),
                                )
                            except Exception as e:
                                logger.warning(
                                    "streak.freeze_notify_failed user_id=%s reason=%s",
                                    user_id, type(e).__name__,
                                )
                    else:
                        async with self.user_repo.db.lock:
                            await self.user_repo.apply_streak_reset(user_id)
                        reset += 1

        logger.info(
            "streak.batch tz=%s users=%s incremented=%s reset=%s frozen=%s bonuses=%s",
            tz, len(users), incremented, reset, frozen, bonuses_total,
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
        Имена пользователей не хранятся в БД, поэтому показываем
        user_id — UI слой может потом обогатить через bot.get_chat.
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
                lines.append(
                    f"{marker} {idx}. id={entry['user_id']}  "
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
                "Используй <b>/friends</b> и кнопку «➕ Добавить», "
                "чтобы пригласить кого-то по Telegram ID."
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
                "total_final": base * mult,
                "current_streak": streak_days,
            })
        rows.sort(key=lambda r: r["total_final"], reverse=True)

        rank_emojis = {1: "🥇", 2: "🥈", 3: "🥉"}
        lines = [f"<b>👥 Друзья · {week_iso}</b>", ""]
        for rank, row in enumerate(rows, start=1):
            emoji = rank_emojis.get(rank, "  ")
            suffix = " <i>(Вы)</i>" if row["user_id"] == user_id else ""
            lines.append(
                f"{emoji} id={row['user_id']}  "
                f"{row['total_final']:.0f} pts{suffix}"
            )
        return "\n".join(lines)


# ------------------------------------------------------------
# AnalyticsService — продуктовая аналитика для PA-портфолио.
# Чистые SQL-агрегаты над существующими таблицами; никаких новых
# таблиц не вводит. Возвращает structured dicts — UI/admin commands
# их рендерят как text/CSV.
# ------------------------------------------------------------
class AnalyticsService:
    """
    Cohort retention, funnel, активность пользователей. Все методы
    возвращают structured data — не строки. Это позволяет переиспользовать
    из разных команд (text-render, CSV-export, JSON-dump).

    Что считается «активностью» (для retention/DAU и т.п.):
      UNION всех timestamp-полей из:
        study_sessions.created_at        (Pomodoro-сессии)
        user_subject_stats.last_activity (визит в предмет)
        quiz_progress.last_attempt       (ответ на ситуац. квиз)
        flashcard_progress.last_review   (просмотр карточки)
        mcq_progress.last_attempt        (ответ на MCQ)
        task_progress.last_attempt       (попытка задачи)
    Все timestamp'ы — UTC (datetime('now') в SQLite), что упрощает
    cross-TZ retention за счёт некоторой неточности на границах суток.
    """

    def __init__(self, db):
        self.db = db

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

    async def compute_funnel(self) -> list[dict]:
        """
        Activation funnel: каждый шаг = % от total registered (не от предыдущего шага),
        чтобы метрики оставались сравнимыми и не нужно было заставлять шаги быть
        strict-subsets (в реале 3-day streak ≠ subset 10+ sessions).

        Returns: [{"name": str, "count": int, "pct": float | 0.0}, ...]
        """
        total = await self._count("SELECT COUNT(*) FROM users")
        if total == 0:
            return []
        steps_raw = [
            ("Registered", total),
            ("Started studying (≥1 session)", await self._count(
                "SELECT COUNT(*) FROM users WHERE total_sessions >= 1"
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
        return [
            {"name": name, "count": count, "pct": count / total}
            for name, count in steps_raw
        ]

    async def compute_engagement(self) -> dict:
        """
        DAU / WAU / MAU + новые пользователи сегодня + stickiness ratio.

        Стандартные метрики engagement:
          DAU = users с любой активностью сегодня
          WAU = последние 7 дней включая сегодня
          MAU = последние 30 дней включая сегодня
          stickiness = DAU / MAU (типичный «good» ~20%+ для consumer apps)

        Returns: {
            "today": "YYYY-MM-DD", "new_today": int, "dau": int, "wau": int,
            "mau": int, "stickiness": float | None, "total_users": int,
        }
        """
        activity = await self._all_activity_dates_per_user()
        today = datetime.now().date()
        w_cutoff = today - timedelta(days=6)
        m_cutoff = today - timedelta(days=29)

        dau = sum(1 for dates in activity.values() if today in dates)
        wau = sum(1 for dates in activity.values() if any(d >= w_cutoff for d in dates))
        mau = sum(1 for dates in activity.values() if any(d >= m_cutoff for d in dates))
        stickiness = (dau / mau) if mau > 0 else None

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
        "events": "events",  # append-only event log для PA-аналитики
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

        return {
            "hardest_situational": hardest,
            "most_attempted_mcq": popular_mcq,
            "progress_coverage": unused_counts,
            "flashcard_ef_distribution": ef_dist,
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

    async def export_all_tables_zip(self, schema_version: str = "v0.7") -> tuple[bytes, dict]:
        """
        Bundles all 10 exportable tables + metadata.json into a ZIP archive.

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