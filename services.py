# services.py
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite
from aiogram.exceptions import TelegramForbiddenError

from repository import UserRepository, SessionRepository

logger = logging.getLogger("studybuddy_bot")


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

class StudyService:
    def __init__(self, user_repo: UserRepository, session_repo: SessionRepository, achievement_service: AchievementService):
        self.user_repo = user_repo
        self.session_repo = session_repo
        self.achievement_service = achievement_service

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
    def __init__(self, user_repo: UserRepository, bot=None):
        self.user_repo = user_repo
        self.bot = bot  # опционально, для отправки уведомлений

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
        incremented = 0
        reset = 0
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
                # Сброс стрика
                if current_streak > 0:
                    async with self.user_repo.db.lock:
                        await self.user_repo.apply_streak_reset(user_id)
                    reset += 1

        logger.info(
            "streak.batch tz=%s users=%s incremented=%s reset=%s bonuses=%s",
            tz, len(users), incremented, reset, bonuses_total,
        )


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