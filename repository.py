# repository.py
import math

import aiosqlite
from typing import Optional, Dict, Any

class UserRepository:
    """
    Асинхронный репозиторий для работы с пользователями и их настройками.
    Не содержит бизнес-логики — только прямые операции с БД.
    """
    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    # ------------------------------------------------------------
    # 1. Создание пользователя
    # ------------------------------------------------------------
    async def create_user(
        self,
        user_id: int,
        timezone: str = "Europe/Moscow",
        username: str | None = None,
    ) -> None:
        """
        Создаёт запись в users и дефолтные настройки уведомлений.
        Если пользователь уже существует — ничего не делает.

        username — опциональный Telegram @handle. Передаётся caller'ом
        из message.from_user.username, чтобы first-message gap не
        наступал (см. UsernameSyncMiddleware: middleware UPDATE
        выполняется ДО создания строки, поэтому без явной передачи
        новый user получил бы NULL username до второй активности).
        """
        # INSERT OR IGNORE гарантирует идемпотентность
        await self.db.execute(
            "INSERT OR IGNORE INTO users (user_id, timezone, username) "
            "VALUES (?, ?, ?)",
            (user_id, timezone, username),
        )
        await self.db.execute(
            "INSERT OR IGNORE INTO notification_settings (user_id) VALUES (?)",
            (user_id,)
        )
        await self.db.commit()

    # ------------------------------------------------------------
    # 2. Проверка существования
    # ------------------------------------------------------------
    async def user_exists(self, user_id: int) -> bool:
        """
        Возвращает True, если пользователь с таким user_id уже есть в таблице users.
        """
        async with self.db.execute(
            "SELECT 1 FROM users WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row is not None

    # ------------------------------------------------------------
    # 3. Получение данных пользователя
    # ------------------------------------------------------------
    async def get_user(self, user_id: int) -> Optional[Dict[str, Any]]:
        """
        Возвращает полную информацию о пользователе (включая настройки)
        или None, если пользователь не найден.
        """
        async with self.db.execute(
            """
            SELECT u.*, 
                   ns.morning_enabled, ns.morning_time,
                   ns.evening_enabled, ns.evening_time,
                   ns.streak_enabled, ns.achievements_enabled
            FROM users u
            LEFT JOIN notification_settings ns ON u.user_id = ns.user_id
            WHERE u.user_id = ?
            """,
            (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            return dict(row)

    # ------------------------------------------------------------
    # 4. Начисление монет
    # ------------------------------------------------------------
    async def add_coins(self, user_id: int, amount: int) -> None:
        """
        Увеличивает total_coins пользователя на указанную сумму.
        Если пользователя нет — тихо игнорируется.
        """
        await self.db.execute(
            "UPDATE users SET total_coins = total_coins + ? WHERE user_id = ?",
            (amount, user_id)
        )
        await self.db.commit()

    # ------------------------------------------------------------
    # 5. Обновление стрика
    # ------------------------------------------------------------
    async def set_streak(self, user_id: int, streak: int) -> None:
        """
        Устанавливает current_streak в указанное значение.
        """
        await self.db.execute(
            "UPDATE users SET current_streak = ? WHERE user_id = ?",
            (streak, user_id)
        )
        await self.db.commit()

    # ------------------------------------------------------------
    # 6. Дополнительно: сброс флага "занимался сегодня"
    #    (используется в ежедневном обновлении стрика)
    # ------------------------------------------------------------
    async def set_has_studied_today(self, user_id: int, value: bool) -> None:
        await self.db.execute(
            "UPDATE users SET has_studied_today = ? WHERE user_id = ?",
            (1 if value else 0, user_id)
        )
        await self.db.commit()

    async def increment_sessions(self, user_id: int) -> None:
        """Увеличить total_sessions на 1 и обновить last_session."""
        await self.db.execute(
            "UPDATE users SET total_sessions = total_sessions + 1, "
            "last_session = datetime('now'), has_studied_today = 1 "
            "WHERE user_id = ?",
            (user_id,)
        )
        await self.db.commit()

    async def set_last_session(self, user_id: int, dt: str) -> None:
        await self.db.execute(
            "UPDATE users SET last_session = ? WHERE user_id = ?",
            (dt, user_id)
        )
        await self.db.commit()

    async def get_all_user_ids(self) -> list[int]:
        """Возвращает список всех зарегистрированных user_id (для рассылки и т.п.)."""
        async with self.db.execute("SELECT user_id FROM users") as cursor:
            rows = await cursor.fetchall()
            return [row["user_id"] for row in rows]

    async def set_timezone(self, user_id: int, tz: str) -> None:
        """Устанавливает часовой пояс пользователя (IANA-имя, например 'Europe/Moscow')."""
        await self.db.execute(
            "UPDATE users SET timezone = ? WHERE user_id = ?",
            (tz, user_id),
        )
        await self.db.commit()

    async def set_hidden_from_leaderboards(self, user_id: int, hidden: bool) -> None:
        """Скрывает/возвращает пользователя на публичные лидерборды
        (LEADERBOARD.md §Privacy). Не влияет на накопление очков и rewards."""
        await self.db.execute(
            "UPDATE users SET hidden_from_leaderboards = ? WHERE user_id = ?",
            (1 if hidden else 0, user_id),
        )
        await self.db.commit()

    async def is_hidden_from_leaderboards(self, user_id: int) -> bool:
        """Текущее состояние privacy-флага."""
        async with self.db.execute(
            "SELECT hidden_from_leaderboards FROM users WHERE user_id=?",
            (user_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return bool(row["hidden_from_leaderboards"]) if row else False

    # ------------------------------------------------------------
    # Username (Telegram @handle) — для friends-search
    # ------------------------------------------------------------
    async def refresh_username(self, user_id: int, username) -> None:
        """
        Обновляет users.username. Безусловный UPDATE — допускает и
        смену handle (str → str), и сброс в NULL (если пользователь
        удалил публичный handle на стороне Telegram).

        Принимает username как str или None. Вызывается из
        UsernameSyncMiddleware на каждый Message/CallbackQuery,
        чтобы кеш для friends-search не дрейфовал.
        """
        await self.db.execute(
            "UPDATE users SET username = ? WHERE user_id = ?",
            (username, user_id),
        )
        await self.db.commit()

    async def find_user_id_by_username(self, username: str):
        """
        Case-insensitive lookup по users.username. Caller отвечает за
        очистку входной строки от leading '@' и лишних пробелов.

        Возвращает user_id (int) или None если username не найден.
        Пустая строка → None defensively.
        """
        if not username:
            return None
        async with self.db.execute(
            "SELECT user_id FROM users WHERE username = ? COLLATE NOCASE",
            (username,),
        ) as cursor:
            row = await cursor.fetchone()
            return row["user_id"] if row else None

    async def get_distinct_timezones(self) -> list[str]:
        """Возвращает все часовые пояса, которые используются хотя бы одним пользователем."""
        async with self.db.execute(
            "SELECT DISTINCT timezone FROM users WHERE timezone IS NOT NULL"
        ) as cursor:
            rows = await cursor.fetchall()
            return [row["timezone"] for row in rows]

    async def get_users_for_streak_update_in_timezone(self, tz: str) -> list[dict]:
        """Пользователи указанного часового пояса для ежедневной обработки стрика."""
        async with self.db.execute(
            """
            SELECT u.user_id, u.has_studied_today, u.current_streak, u.total_coins,
                   u.last_streak_check_date,
                   COALESCE(ns.streak_enabled, 1) AS streak_enabled
            FROM users u
            LEFT JOIN notification_settings ns ON ns.user_id = u.user_id
            WHERE u.timezone = ?
            """,
            (tz,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_users_for_streak_update(self) -> list[dict]:
        """
        Возвращает всех пользователей с полями:
        user_id, has_studied_today, current_streak, total_coins
        """
        async with self.db.execute(
            "SELECT user_id, has_studied_today, current_streak, total_coins FROM users"
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def apply_streak_increment(self, user_id: int, new_streak: int, bonus: int) -> None:
        """
        Увеличивает стрик до new_streak, добавляет bonus монет,
        сбрасывает флаг has_studied_today.
        """
        await self.db.execute(
            "UPDATE users SET current_streak = ?, total_coins = total_coins + ?, has_studied_today = 0 WHERE user_id = ?",
            (new_streak, bonus, user_id)
        )
        await self.db.commit()

    async def apply_streak_reset(self, user_id: int) -> None:
        """
        Сбрасывает стрик в 0 и has_studied_today в 0.
        """
        await self.db.execute(
            "UPDATE users SET current_streak = 0, has_studied_today = 0 WHERE user_id = ?",
            (user_id,)
        )
        await self.db.commit()

    async def set_last_streak_check_date(self, user_id: int, local_date: str) -> None:
        """Отмечает, что nightly streak-check для user уже выполнен в local_date."""
        await self.db.execute(
            "UPDATE users SET last_streak_check_date = ? WHERE user_id = ?",
            (local_date, user_id),
        )
        await self.db.commit()

    async def get_notification_settings(self, user_id: int) -> dict | None:
        """
        Возвращает словарь с настройками уведомлений пользователя,
        или None, если пользователь не найден.
        """
        async with self.db.execute(
            "SELECT * FROM notification_settings WHERE user_id = ?",
            (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            return dict(row)

    async def update_notification_settings(self, user_id: int, settings: dict) -> None:
        """
        Создаёт или обновляет настройки уведомлений пользователя.
        UPSERT гарантирует сохранение даже если строка notification_settings
        отсутствует (legacy-пользователи без дефолтной записи).
        """
        values = (
            user_id,
            settings.get("morning_enabled", 1),
            settings.get("morning_time", "09:00"),
            settings.get("evening_enabled", 1),
            settings.get("evening_time", "21:00"),
            settings.get("streak_enabled", 1),
            settings.get("achievements_enabled", 1),
            settings.get("flashcard_source", "mix"),
        )
        await self.db.execute(
            """INSERT INTO notification_settings (
                user_id,
                morning_enabled, morning_time,
                evening_enabled, evening_time,
                streak_enabled, achievements_enabled,
                flashcard_source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                morning_enabled = excluded.morning_enabled,
                morning_time = excluded.morning_time,
                evening_enabled = excluded.evening_enabled,
                evening_time = excluded.evening_time,
                streak_enabled = excluded.streak_enabled,
                achievements_enabled = excluded.achievements_enabled,
                flashcard_source = excluded.flashcard_source""",
            values,
        )
        await self.db.commit()

    async def get_users_due_for_morning(self, tz: str, hhmm: str) -> list[dict]:
        """
        Пользователи указанного TZ, которым нужно отправить утреннее
        напоминание в указанную минуту (формат 'HH:MM' в локальном времени TZ).
        """
        async with self.db.execute(
            "SELECT u.user_id "
            "FROM users u "
            "JOIN notification_settings ns ON ns.user_id = u.user_id "
            "WHERE u.timezone = ? "
            "  AND ns.morning_enabled = 1 "
            "  AND ns.morning_time = ?",
            (tz, hhmm),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_users_due_for_evening(self, tz: str, hhmm: str) -> list[dict]:
        """
        Пользователи указанного TZ, которым нужно отправить вечернее напоминание
        в указанную минуту — но только если они ещё НЕ занимались сегодня.
        """
        async with self.db.execute(
            "SELECT u.user_id "
            "FROM users u "
            "JOIN notification_settings ns ON ns.user_id = u.user_id "
            "WHERE u.timezone = ? "
            "  AND ns.evening_enabled = 1 "
            "  AND ns.evening_time = ? "
            "  AND u.has_studied_today = 0",
            (tz, hhmm),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


class SessionRepository:
    """Работа с таблицей study_sessions (только сохранение факта сессии)."""
    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def add_session(self, user_id: int, duration: int, coins: int, bonus: int) -> int:
        """Сохраняет завершённую сессию и возвращает её id (для последующей оценки)."""
        cursor = await self.db.execute(
            "INSERT INTO study_sessions (user_id, duration_minutes, coins_earned, bonus_coins) "
            "VALUES (?, ?, ?, ?)",
            (user_id, duration, coins, bonus)
        )
        await self.db.commit()
        return cursor.lastrowid

    async def set_session_score(self, session_id: int, user_id: int, score: int) -> bool:
        """
        Проставляет оценку 1..4 для сессии. Возвращает True, если строка обновлена.
        Фильтр по user_id — защита от подделки чужого session_id в callback_data.
        """
        cursor = await self.db.execute(
            "UPDATE study_sessions SET score = ? WHERE id = ? AND user_id = ?",
            (score, session_id, user_id),
        )
        await self.db.commit()
        return cursor.rowcount > 0

    async def get_total_minutes(self, user_id: int) -> int:
        """
        Возвращает сумму минут всех завершённых сессий пользователя.
        """
        async with self.db.execute(
            "SELECT COALESCE(SUM(duration_minutes), 0) FROM study_sessions WHERE user_id = ?",
            (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0


class UserFlashcardRepository:
    """
    CRUD пользовательских флэш-карт (per user + subject).
    Hash namespace: префикс 'u' + 7 hex от id — не пересекается
    с официальными MD5(term)[:8].
    """

    MAX_PER_SUBJECT = 100

    @staticmethod
    def card_hash(card_id: int) -> str:
        return f"u{card_id:07x}"

    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def count_by_subject(self, user_id: int, subject_id: str) -> int:
        async with self.db.execute(
            "SELECT COUNT(*) FROM user_flashcards "
            "WHERE user_id = ? AND subject_id = ?",
            (user_id, subject_id),
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def create(
        self, user_id: int, subject_id: str, term: str, definition: str
    ) -> dict:
        """
        INSERT новой карточки. Raises ValueError('limit_exceeded') при лимите
        100 на (user_id, subject_id). IntegrityError при дубликате term.
        """
        term = term.strip()
        definition = definition.strip()
        count = await self.count_by_subject(user_id, subject_id)
        if count >= self.MAX_PER_SUBJECT:
            raise ValueError("limit_exceeded")
        cursor = await self.db.execute(
            "INSERT INTO user_flashcards (user_id, subject_id, term, definition) "
            "VALUES (?, ?, ?, ?)",
            (user_id, subject_id, term, definition),
        )
        await self.db.commit()
        card_id = cursor.lastrowid
        return {
            "id": card_id,
            "term": term,
            "definition": definition,
            "hash": self.card_hash(card_id),
        }

    async def list_by_subject(self, user_id: int, subject_id: str) -> list[dict]:
        async with self.db.execute(
            "SELECT id, term, definition FROM user_flashcards "
            "WHERE user_id = ? AND subject_id = ? "
            "ORDER BY created_at ASC, id ASC",
            (user_id, subject_id),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            {
                "id": row["id"],
                "term": row["term"],
                "definition": row["definition"],
                "hash": self.card_hash(row["id"]),
            }
            for row in rows
        ]

    async def delete(self, user_id: int, card_id: int) -> bool:
        """Удаляет карточку и связанный SM-2 прогресс по hash."""
        card_hash = self.card_hash(card_id)
        cursor = await self.db.execute(
            "DELETE FROM user_flashcards WHERE user_id = ? AND id = ?",
            (user_id, card_id),
        )
        if cursor.rowcount == 0:
            await self.db.commit()
            return False
        await self.db.execute(
            "DELETE FROM flashcard_progress WHERE user_id = ? AND card_hash = ?",
            (user_id, card_hash),
        )
        await self.db.commit()
        return True


class TipsRepository:
    """Просмотры советов по продуктивности: счётчик, cooldown, совет дня."""

    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def record_seen(self, user_id: int, tip_id: str) -> None:
        await self.db.execute(
            "INSERT INTO user_tips_seen (user_id, tip_id, seen_at) VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(user_id, tip_id) DO UPDATE SET seen_at = excluded.seen_at",
            (user_id, tip_id),
        )
        await self.db.commit()

    async def get_recently_seen_tip_ids(
        self, user_id: int, within_days: int = 7,
    ) -> set[str]:
        async with self.db.execute(
            "SELECT tip_id FROM user_tips_seen "
            "WHERE user_id = ? AND seen_at >= datetime('now', ?)",
            (user_id, f"-{within_days} days"),
        ) as cursor:
            rows = await cursor.fetchall()
            return {row["tip_id"] for row in rows}

    async def user_has_flashcards_due(self, user_id: int) -> bool:
        async with self.db.execute(
            "SELECT 1 FROM flashcard_progress "
            "WHERE user_id = ? AND next_review IS NOT NULL AND next_review <= datetime('now') "
            "LIMIT 1",
            (user_id,),
        ) as cursor:
            return await cursor.fetchone() is not None

    async def resolve_tip_of_day(
        self,
        user_id: int,
        local_date: str,
        all_tips: list[dict],
    ) -> dict | None:
        """Один и тот же совет на календарный день пользователя (стабильный tip_of_day_id)."""
        if not all_tips:
            return None
        tips_by_id = {t["id"]: t for t in all_tips if t.get("id")}

        async with self.db.lock:
            async with self.db.execute(
                "SELECT tip_of_day_id, tip_of_day_date FROM user_tips_stats WHERE user_id = ?",
                (user_id,),
            ) as cursor:
                row = await cursor.fetchone()
            if row and row["tip_of_day_date"] == local_date and row["tip_of_day_id"]:
                stored = tips_by_id.get(row["tip_of_day_id"])
                if stored:
                    return stored

            pick = all_tips[hash(f"{user_id}:{local_date}") % len(all_tips)]
            tip_id = pick["id"]
            if row:
                await self.db.execute(
                    "UPDATE user_tips_stats SET tip_of_day_id = ?, tip_of_day_date = ? "
                    "WHERE user_id = ?",
                    (tip_id, local_date, user_id),
                )
            else:
                await self.db.execute(
                    "INSERT INTO user_tips_stats "
                    "(user_id, total_views, tip_of_day_id, tip_of_day_date) VALUES (?, 0, ?, ?)",
                    (user_id, tip_id, local_date),
                )
            await self.db.commit()
            return pick

    async def record_view(self, user_id: int, local_date: str) -> tuple[int, bool]:
        """
        Увеличивает total_views на 1.
        Возвращает (новый total_views, coin_granted) — монета не чаще 1 раза в local_date.
        """
        async with self.db.lock:
            async with self.db.execute(
                "SELECT total_views, last_coin_date FROM user_tips_stats WHERE user_id = ?",
                (user_id,),
            ) as cursor:
                row = await cursor.fetchone()
            if row:
                total = row["total_views"] + 1
                coin_granted = row["last_coin_date"] != local_date
                if coin_granted:
                    await self.db.execute(
                        "UPDATE user_tips_stats SET total_views = ?, last_coin_date = ? "
                        "WHERE user_id = ?",
                        (total, local_date, user_id),
                    )
                else:
                    await self.db.execute(
                        "UPDATE user_tips_stats SET total_views = ? WHERE user_id = ?",
                        (total, user_id),
                    )
            else:
                total = 1
                coin_granted = True
                await self.db.execute(
                    "INSERT INTO user_tips_stats (user_id, total_views, last_coin_date) "
                    "VALUES (?, 1, ?)",
                    (user_id, local_date),
                )
            await self.db.commit()
            return total, coin_granted


class FlashcardRepository:
    """
    SM-2 прогресс по флэш-картам (v0.7 #15).
    Алгоритм-чистая функция живёт в services.sm2_update; репозиторий —
    только CRUD над таблицей flashcard_progress.
    """

    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def get_progress(self, user_id: int, card_hash: str) -> Optional[Dict[str, Any]]:
        """Возвращает текущее состояние карточки или None если карта новая."""
        async with self.db.execute(
            "SELECT ease_factor, interval_days, repetitions, last_review, next_review "
            "FROM flashcard_progress WHERE user_id = ? AND card_hash = ?",
            (user_id, card_hash),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def upsert_progress(
        self,
        user_id: int,
        card_hash: str,
        ease_factor: float,
        interval_days: int,
        repetitions: int,
        last_review: str,
        next_review: str,
    ) -> None:
        await self.db.execute(
            "INSERT INTO flashcard_progress "
            "(user_id, card_hash, ease_factor, interval_days, repetitions, last_review, next_review) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, card_hash) DO UPDATE SET "
            "ease_factor = excluded.ease_factor, "
            "interval_days = excluded.interval_days, "
            "repetitions = excluded.repetitions, "
            "last_review = excluded.last_review, "
            "next_review = excluded.next_review",
            (user_id, card_hash, ease_factor, interval_days, repetitions, last_review, next_review),
        )
        await self.db.commit()

    async def get_next_card_hash(self, user_id: int, candidate_hashes: list[str]) -> Optional[str]:
        """
        Возвращает hash следующей карточки для показа:
          1) overdue карточка с минимальным next_review;
          2) если overdue нет — первая карта из candidate_hashes,
             которой нет в flashcard_progress (новая);
          3) None если ни одной carte due нет.
        """
        if not candidate_hashes:
            return None
        placeholders = ",".join("?" * len(candidate_hashes))

        # 1) Overdue карты
        async with self.db.execute(
            f"SELECT card_hash FROM flashcard_progress "
            f"WHERE user_id = ? AND card_hash IN ({placeholders}) "
            f"AND next_review IS NOT NULL AND next_review <= datetime('now') "
            f"ORDER BY next_review ASC LIMIT 1",
            (user_id, *candidate_hashes),
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return row["card_hash"]

        # 2) Новые карты — те, которых нет в таблице
        async with self.db.execute(
            f"SELECT card_hash FROM flashcard_progress "
            f"WHERE user_id = ? AND card_hash IN ({placeholders})",
            (user_id, *candidate_hashes),
        ) as cursor:
            known = {row["card_hash"] for row in await cursor.fetchall()}
        for h in candidate_hashes:
            if h not in known:
                return h
        return None


class McqProgressRepository:
    """Per-question прогресс MCQ. Используется в экране прогресса."""

    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def record_attempt(self, user_id: int, question_hash: str, is_correct: bool) -> None:
        """Инкрементирует total_count; если ответ верный — ещё и correct_count."""
        from datetime import datetime
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        await self.db.execute(
            "INSERT INTO mcq_progress "
            "(user_id, question_hash, correct_count, total_count, last_attempt) "
            "VALUES (?, ?, ?, 1, ?) "
            "ON CONFLICT(user_id, question_hash) DO UPDATE SET "
            "correct_count = correct_count + excluded.correct_count, "
            "total_count = total_count + 1, "
            "last_attempt = excluded.last_attempt",
            (user_id, question_hash, 1 if is_correct else 0, now),
        )
        await self.db.commit()

    async def count_mastered(self, user_id: int, candidate_hashes: list[str]) -> int:
        """Сколько из переданных вопросов «выучены» (correct_count ≥ 1)."""
        if not candidate_hashes:
            return 0
        placeholders = ",".join("?" * len(candidate_hashes))
        async with self.db.execute(
            f"SELECT COUNT(*) FROM mcq_progress "
            f"WHERE user_id = ? AND correct_count >= 1 "
            f"AND question_hash IN ({placeholders})",
            (user_id, *candidate_hashes),
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0


class TaskProgressRepository:
    """Per-task прогресс. Используется в экране прогресса."""

    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def record_attempt(
        self, user_id: int, task_id: str, attempts_used: int, succeeded: bool
    ) -> None:
        """
        Сохраняет результат попытки. Идемпотентно: если задача уже решена
        (succeeded=1), повторный вызов с succeeded=1 ничего не портит.
        """
        from datetime import datetime
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        await self.db.execute(
            "INSERT INTO task_progress "
            "(user_id, task_id, attempts_used, succeeded, last_attempt) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, task_id) DO UPDATE SET "
            # Сохраняем минимум попыток (best result) и max(succeeded)
            "attempts_used = MIN(attempts_used, excluded.attempts_used), "
            "succeeded = MAX(succeeded, excluded.succeeded), "
            "last_attempt = excluded.last_attempt",
            (user_id, task_id, attempts_used, 1 if succeeded else 0, now),
        )
        await self.db.commit()

    async def count_mastered(self, user_id: int, candidate_ids: list[str]) -> int:
        """Сколько задач решены (succeeded=1)."""
        if not candidate_ids:
            return 0
        placeholders = ",".join("?" * len(candidate_ids))
        async with self.db.execute(
            f"SELECT COUNT(*) FROM task_progress "
            f"WHERE user_id = ? AND succeeded = 1 "
            f"AND task_id IN ({placeholders})",
            (user_id, *candidate_ids),
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0


class SubjectStatsRepository:
    """Aggregate-статистика per (user, subject): visits + last_activity."""

    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def bump_visit(self, user_id: int, subject_id: str) -> None:
        """+1 к visits + обновить last_activity до сейчас."""
        from datetime import datetime
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        await self.db.execute(
            "INSERT INTO user_subject_stats (user_id, subject_id, visits, last_activity) "
            "VALUES (?, ?, 1, ?) "
            "ON CONFLICT(user_id, subject_id) DO UPDATE SET "
            "visits = visits + 1, "
            "last_activity = excluded.last_activity",
            (user_id, subject_id, now),
        )
        await self.db.commit()

    async def get(self, user_id: int, subject_id: str) -> Optional[Dict[str, Any]]:
        async with self.db.execute(
            "SELECT visits, last_activity FROM user_subject_stats "
            "WHERE user_id = ? AND subject_id = ?",
            (user_id, subject_id),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


class EventRepository:
    """
    Append-only event log для PA-аналитики.

    Каждый significant user action → одна INSERT. Никаких UPDATE/DELETE —
    данные историчны. Properties — JSON-словарь произвольной формы для
    event-specific полей (не нужно schema migrations при добавлении новых
    типов событий).

    Все методы swallow exceptions с логом — event-logging не должен ломать
    основной flow бота. Худшее что может случиться — потерянная аналитика.
    """

    def __init__(self, db: aiosqlite.Connection):
        self.db = db
        # Import inside __init__ to avoid circular: services imports repository
        import logging
        self._logger = logging.getLogger("studybuddy_bot")

    @staticmethod
    def _resolve_event_dimensions(
        properties: dict | None,
        *,
        subject_id: str | None = None,
        mode: str | None = None,
        tip_id: str | None = None,
    ) -> tuple[str | None, str | None, str | None]:
        """Извлекает subject_id / mode / tip_id из аргументов или properties."""
        props = properties or {}
        subj = subject_id or props.get("subject_id") or props.get("subject")
        mod = mode or props.get("mode")
        tid = tip_id or props.get("tip_id")
        if subj is not None:
            subj = str(subj)[:128]
        if mod is not None:
            mod = str(mod)[:64]
        if tid is not None:
            tid = str(tid)[:64]
        return subj, mod, tid

    async def log(
        self,
        user_id: int | None,
        event_name: str,
        properties: dict | None = None,
        *,
        subject_id: str | None = None,
        mode: str | None = None,
        tip_id: str | None = None,
    ) -> None:
        """
        Регистрирует event. Не raises — failure тихо логируется в bot.log,
        чтобы analytics-issues не валили бизнес-логику бота.

        properties сериализуется в JSON. None → '{}' (пустой dict).
        subject_id / mode / tip_id — денормализованные колонки для SQL/pandas
        (дублируют частые ключи из properties).
        """
        import json
        try:
            props_json = json.dumps(properties or {}, ensure_ascii=False)
            subj, mod, tid = self._resolve_event_dimensions(
                properties,
                subject_id=subject_id,
                mode=mode,
                tip_id=tip_id,
            )
            await self.db.execute(
                "INSERT INTO events "
                "(user_id, event_name, properties, subject_id, mode, tip_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, event_name, props_json, subj, mod, tid),
            )
            await self.db.commit()
        except Exception as e:
            self._logger.warning(
                "events.log_failed event=%s user_id=%s reason=%s detail=%s",
                event_name, user_id, type(e).__name__, e,
            )


class AdminRepository:
    """
    Работа с таблицей admins. Источник истины — БД; `bot.py` держит
    in-memory кеш set[int] для is_admin() и обновляет его на add/remove.
    """

    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def get_all_ids(self) -> set[int]:
        """Возвращает все user_id админов из БД."""
        async with self.db.execute("SELECT user_id FROM admins") as cursor:
            rows = await cursor.fetchall()
            return {row["user_id"] for row in rows}

    async def add(self, user_id: int) -> bool:
        """
        Идемпотентное добавление. Возвращает True, если запись была создана
        (newly added), False — если уже существовала.
        """
        cursor = await self.db.execute(
            "INSERT OR IGNORE INTO admins (user_id) VALUES (?)",
            (user_id,),
        )
        await self.db.commit()
        return cursor.rowcount > 0

    async def remove(self, user_id: int) -> bool:
        """
        Возвращает True, если запись была удалена; False, если такого админа не было.
        """
        cursor = await self.db.execute(
            "DELETE FROM admins WHERE user_id = ?",
            (user_id,),
        )
        await self.db.commit()
        return cursor.rowcount > 0

    async def is_admin(self, user_id: int) -> bool:
        async with self.db.execute(
            "SELECT 1 FROM admins WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row is not None


class PetRepository:
    """
    Цифровой питомец (v0.7 TODO #16): единственный дизайн с derived-эмоциями
    (см. services.derive_emotion). Репозиторий хранит только данные —
    имя, цвет, аксессуар, уровень, xp, метку last_excited_at.

    Атомарность: большинство методов assume что caller держит self.db.lock
    (тот же паттерн, что UserRepository.add_coins). Исключение —
    purchase_item: спека требует transactional re-read баланса/уровня
    под локом, поэтому метод сам берёт self.db.lock.
    """

    # Каталог предметов: name → (unlock_level, price_coins).
    # Формулы из спеки: color = unlock_level × 20, accessory = unlock_level × 30
    # (с явными бесплатными дефолтами для orange/none).
    COLOR_CATALOG: Dict[str, tuple] = {
        "orange": (1, 0),    # ★ free default
        "grey":   (1, 20),
        "blue":   (2, 40),
        "green":  (2, 40),
        "pink":   (4, 80),
    }
    ACCESSORY_CATALOG: Dict[str, tuple] = {
        "none":    (1, 0),   # ★ free default
        "hat":     (1, 30),
        "glasses": (3, 90),
        "scarf":   (5, 150),
        "crown":   (8, 240),
    }

    @staticmethod
    def xp_to_level(xp: int) -> int:
        """Уровень питомца: floor(sqrt(xp / 10)) + 1, минимум 1."""
        if xp < 0:
            xp = 0
        return math.isqrt(xp // 10) + 1

    def __init__(self, db: aiosqlite.Connection):
        self.db = db
        import logging
        self._logger = logging.getLogger("studybuddy_bot")

    # ------------------------------------------------------------
    # 1. Создание питомца с дефолтами
    # ------------------------------------------------------------
    async def create_pet_with_defaults(
        self, user_id: int, name: str = "Питомец"
    ) -> bool:
        """
        Создаёт user_pet (если ещё нет) + сидит инвентарь двумя бесплатными
        дефолтами: (color, orange), (accessory, none). Идемпотентно.

        Возвращает True, если pet был создан этой операцией, False — уже был.
        """
        cursor = await self.db.execute(
            "INSERT OR IGNORE INTO user_pet (user_id, name) VALUES (?, ?)",
            (user_id, name),
        )
        created = cursor.rowcount > 0
        # Сидим инвентарь дефолтами (INSERT OR IGNORE — идемпотентно даже
        # если pet существовал, но инвентарь почему-то пустой).
        await self.db.execute(
            "INSERT OR IGNORE INTO user_pet_inventory "
            "(user_id, item_type, item_value) VALUES (?, 'color', 'orange')",
            (user_id,),
        )
        await self.db.execute(
            "INSERT OR IGNORE INTO user_pet_inventory "
            "(user_id, item_type, item_value) VALUES (?, 'accessory', 'none')",
            (user_id,),
        )
        await self.db.commit()
        return created

    # ------------------------------------------------------------
    # 2. Чтение
    # ------------------------------------------------------------
    async def get_pet(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Возвращает dict с pet-полями или None, если pet не создан."""
        async with self.db.execute(
            "SELECT * FROM user_pet WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_inventory(self, user_id: int) -> list:
        """
        Возвращает список купленных предметов:
        [{item_type, item_value, purchased_at}, ...]
        Пустой список, если пользователь без pet.
        """
        async with self.db.execute(
            "SELECT item_type, item_value, purchased_at FROM user_pet_inventory "
            "WHERE user_id = ? ORDER BY purchased_at ASC",
            (user_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------
    # 3. XP / level — auto-creates pet при первом обращении
    # ------------------------------------------------------------
    async def add_xp(self, user_id: int, minutes: int) -> tuple:
        """
        Начисляет `minutes` XP. Auto-создаёт pet с дефолтами, если его нет —
        так data layer self-contained: первая учебная сессия сама создаёт
        питомца, без явного шага в UI.

        Возвращает (old_level, new_level). Если new_level > old_level —
        last_excited_at обновляется тут же (level-up = excited).

        НЕ берёт self.db.lock — caller должен уже его держать (см.
        StudyService.complete_session, которая держит db.lock на всю
        composite-операцию).

        minutes <= 0 → no-op, возвращает (0, 0). Defensive.
        """
        if minutes <= 0:
            return (0, 0)

        # Auto-create user_pet + дефолтный инвентарь. INSERT OR IGNORE —
        # если pet уже есть, ничего не происходит.
        await self.db.execute(
            "INSERT OR IGNORE INTO user_pet (user_id) VALUES (?)",
            (user_id,),
        )
        await self.db.execute(
            "INSERT OR IGNORE INTO user_pet_inventory "
            "(user_id, item_type, item_value) VALUES (?, 'color', 'orange')",
            (user_id,),
        )
        await self.db.execute(
            "INSERT OR IGNORE INTO user_pet_inventory "
            "(user_id, item_type, item_value) VALUES (?, 'accessory', 'none')",
            (user_id,),
        )

        # Read-modify-write xp/level
        async with self.db.execute(
            "SELECT xp, level FROM user_pet WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
        old_level = row["level"]
        new_xp = row["xp"] + minutes
        new_level = self.xp_to_level(new_xp)

        if new_level > old_level:
            await self.db.execute(
                "UPDATE user_pet SET xp = ?, level = ?, "
                "last_excited_at = datetime('now') WHERE user_id = ?",
                (new_xp, new_level, user_id),
            )
            self._logger.info(
                "pet.levelup user_id=%s old_level=%s new_level=%s xp=%s",
                user_id, old_level, new_level, new_xp,
            )
        else:
            await self.db.execute(
                "UPDATE user_pet SET xp = ? WHERE user_id = ?",
                (new_xp, user_id),
            )
        await self.db.commit()
        return (old_level, new_level)

    # ------------------------------------------------------------
    # 4. Покупка предмета — атомарная под self.db.lock
    # ------------------------------------------------------------
    async def purchase_item(
        self, user_id: int, item_type: str, item_value: str
    ) -> str:
        """
        Атомарная покупка: re-read coins/level/ownership, deduct, INSERT
        inventory, auto-equip. Сам берёт self.db.lock (спека TODO #16).

        Возвращает один из статусов (для UI-feedback):
        - "purchased"          — успешная покупка + auto-equip
        - "already_owned"      — предмет уже в инвентаре (idempotent)
        - "unknown_item"       — item_type/item_value не в каталоге
        - "insufficient_level" — level пользователя ниже unlock_level
        - "insufficient_coins" — не хватает total_coins
        - "no_pet"             — у пользователя нет user_pet
                                 (defensive — add_xp создаёт pet
                                 при первой сессии)
        """
        catalog = (
            self.COLOR_CATALOG if item_type == "color"
            else self.ACCESSORY_CATALOG if item_type == "accessory"
            else None
        )
        if catalog is None or item_value not in catalog:
            return "unknown_item"
        unlock_level, price = catalog[item_value]

        async with self.db.lock:
            async with self.db.execute(
                "SELECT level FROM user_pet WHERE user_id = ?", (user_id,)
            ) as c:
                pet = await c.fetchone()
            if pet is None:
                return "no_pet"

            # Ownership check (idempotent return)
            async with self.db.execute(
                "SELECT 1 FROM user_pet_inventory "
                "WHERE user_id = ? AND item_type = ? AND item_value = ?",
                (user_id, item_type, item_value),
            ) as c:
                owned_row = await c.fetchone()
            if owned_row is not None:
                return "already_owned"

            if pet["level"] < unlock_level:
                return "insufficient_level"

            async with self.db.execute(
                "SELECT total_coins FROM users WHERE user_id = ?", (user_id,)
            ) as c:
                user_row = await c.fetchone()
            balance = user_row["total_coins"] if user_row else 0
            if balance < price:
                return "insufficient_coins"

            # Атомарно: deduct, insert inventory, auto-equip
            if price > 0:
                await self.db.execute(
                    "UPDATE users SET total_coins = total_coins - ? "
                    "WHERE user_id = ?",
                    (price, user_id),
                )
            await self.db.execute(
                "INSERT OR IGNORE INTO user_pet_inventory "
                "(user_id, item_type, item_value) VALUES (?, ?, ?)",
                (user_id, item_type, item_value),
            )
            col = "color" if item_type == "color" else "accessory"
            await self.db.execute(
                f"UPDATE user_pet SET {col} = ? WHERE user_id = ?",
                (item_value, user_id),
            )
            await self.db.commit()

            self._logger.info(
                "pet.purchase user_id=%s type=%s value=%s "
                "price=%s balance_after=%s",
                user_id, item_type, item_value, price, balance - price,
            )
            return "purchased"

    # ------------------------------------------------------------
    # 5. Equip уже купленного предмета (без покупки)
    # ------------------------------------------------------------
    async def equip(self, user_id: int, item_type: str, item_value: str) -> bool:
        """
        Надеть уже купленный предмет. Проверяет ownership в инвентаре.
        Возвращает True если equipped, False — если не в инвентаре или
        item_type не 'color'/'accessory'.
        """
        if item_type not in ("color", "accessory"):
            return False
        async with self.db.execute(
            "SELECT 1 FROM user_pet_inventory "
            "WHERE user_id = ? AND item_type = ? AND item_value = ?",
            (user_id, item_type, item_value),
        ) as c:
            row = await c.fetchone()
        if row is None:
            return False
        col = "color" if item_type == "color" else "accessory"
        await self.db.execute(
            f"UPDATE user_pet SET {col} = ? WHERE user_id = ?",
            (item_value, user_id),
        )
        await self.db.commit()
        return True

    # ------------------------------------------------------------
    # 6. Rename
    # ------------------------------------------------------------
    async def rename(self, user_id: int, new_name: str) -> bool:
        """
        Переименовать питомца. True если pet существует и имя обновлено,
        False — если pet ещё не создан.
        """
        cursor = await self.db.execute(
            "UPDATE user_pet SET name = ? WHERE user_id = ?",
            (new_name, user_id),
        )
        await self.db.commit()
        return cursor.rowcount > 0

    # ------------------------------------------------------------
    # 7. Mark excited (level-up / achievement)
    # ------------------------------------------------------------
    async def mark_excited(self, user_id: int) -> None:
        """
        Помечает last_excited_at = now. Используется derive_emotion для
        приоритета 'excited' в течение ~5 минут после ачивки или level-up.
        Тихий no-op, если pet ещё не создан.
        """
        await self.db.execute(
            "UPDATE user_pet SET last_excited_at = datetime('now') "
            "WHERE user_id = ?",
            (user_id,),
        )
        await self.db.commit()


class LeaderboardRepository:
    """
    Score-инкременты для weekly leaderboard (LEADERBOARD.md Phase 1).

    Все grant_-методы lock-free. Безопасность к гонкам обеспечивается
    атомарностью самих UPDATE-выражений ("UPDATE … WHERE quiz_count < 25"
    + проверка rowcount). Для grant_time_pts реальный read-modify-write
    остаётся, но практически невозможна одновременная отметка двух
    session_completed для одного user'а — таймер пользователя один.

    Helper'ы из services.py (piecewise_time_pts, user_calendar_keys)
    импортируются лениво внутри методов, чтобы не создавать circular
    dependency (services уже импортирует repository).
    """

    def __init__(self, db: aiosqlite.Connection):
        self.db = db
        import logging
        self._logger = logging.getLogger("studybuddy_bot")

    # ------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------
    async def _now_local_for_user(self, user_id: int):
        """
        datetime.now() в локальном TZ пользователя (из users.timezone).
        Fallback: Europe/Moscow при отсутствии user'а или неизвестном TZ.
        Используется grant_-методами, когда caller не передал now_local
        явно. Тесты обычно передают свой now_local для детерминизма.
        """
        import pytz
        from datetime import datetime
        async with self.db.execute(
            "SELECT timezone FROM users WHERE user_id=?", (user_id,)
        ) as c:
            row = await c.fetchone()
        tz_name = row["timezone"] if row else "Europe/Moscow"
        try:
            return datetime.now(pytz.timezone(tz_name))
        except pytz.UnknownTimeZoneError:
            return datetime.now(pytz.timezone("Europe/Moscow"))

    async def _ensure_rows(self, user_id: int, local_date: str, week_iso: str) -> None:
        """INSERT OR IGNORE для daily + weekly строк текущего дня/недели."""
        await self.db.execute(
            "INSERT OR IGNORE INTO daily_score_counters (user_id, local_date) "
            "VALUES (?, ?)",
            (user_id, local_date),
        )
        await self.db.execute(
            "INSERT OR IGNORE INTO weekly_scores (user_id, week_iso) "
            "VALUES (?, ?)",
            (user_id, week_iso),
        )

    # ------------------------------------------------------------
    # 1. Time pts — piecewise по дневным минутам (LEADERBOARD.md §1)
    # ------------------------------------------------------------
    async def grant_time_pts(
        self, user_id: int, duration_min: int, now_local=None
    ) -> float:
        """
        Начисляет time pts за сессию длиной duration_min, исходя из того,
        сколько минут уже было сегодня (для корректного piecewise).
        Хранимый time_minutes capped at 240; излишек не приносит pts.

        Возвращает фактически начисленные pts (REAL, неокруглённые).
        Вызов с duration_min <= 0 — no-op, возвращает 0.0.
        """
        from services import piecewise_time_pts, user_calendar_keys
        if duration_min <= 0:
            return 0.0
        if now_local is None:
            now_local = await self._now_local_for_user(user_id)
        local_date, week_iso = user_calendar_keys(now_local)
        await self._ensure_rows(user_id, local_date, week_iso)

        async with self.db.execute(
            "SELECT time_minutes FROM daily_score_counters "
            "WHERE user_id=? AND local_date=?",
            (user_id, local_date),
        ) as c:
            row = await c.fetchone()
        start = row["time_minutes"]
        end = start + duration_min
        pts = piecewise_time_pts(start, end)

        # Cap stored time_minutes at 240 — за пределом всё равно 0 pts,
        # хранить точное значение не нужно.
        new_time_minutes = min(end, 240)
        await self.db.execute(
            "UPDATE daily_score_counters "
            "SET time_minutes=?, time_pts = time_pts + ? "
            "WHERE user_id=? AND local_date=?",
            (new_time_minutes, pts, user_id, local_date),
        )
        await self.db.execute(
            "UPDATE weekly_scores SET time_pts = time_pts + ? "
            "WHERE user_id=? AND week_iso=?",
            (pts, user_id, week_iso),
        )
        await self.db.commit()
        return pts

    # ------------------------------------------------------------
    # 2. Math task pts — 40/correct, daily cap 5 (LEADERBOARD.md §2)
    # ------------------------------------------------------------
    async def grant_task_pts(self, user_id: int, now_local=None) -> bool:
        """
        Начисляет 40 pts за решённую math task, если daily cap (5) не превышен.
        Атомарная проверка cap через WHERE task_count < 5 + rowcount.
        Возвращает True если начислено, False если capped.
        """
        from services import user_calendar_keys
        if now_local is None:
            now_local = await self._now_local_for_user(user_id)
        local_date, week_iso = user_calendar_keys(now_local)
        await self._ensure_rows(user_id, local_date, week_iso)

        cursor = await self.db.execute(
            "UPDATE daily_score_counters SET task_count = task_count + 1 "
            "WHERE user_id=? AND local_date=? AND task_count < 5",
            (user_id, local_date),
        )
        if cursor.rowcount == 0:
            await self.db.commit()
            return False

        await self.db.execute(
            "UPDATE weekly_scores SET task_pts = task_pts + 40 "
            "WHERE user_id=? AND week_iso=?",
            (user_id, week_iso),
        )
        await self.db.commit()
        return True

    # ------------------------------------------------------------
    # 3. Quiz correct — 5 pts + series bonus +15/3 (LEADERBOARD.md §3)
    # ------------------------------------------------------------
    async def grant_quiz_pts_correct(
        self, user_id: int, now_local=None
    ) -> tuple:
        """
        Начисляет pts за правильный quiz/MCQ-ответ. MCQ считается quiz'ом
        для scoring (§3). Daily cap 25 correct. Series bonus +15 каждые
        3 правильных подряд (3, 6, 9, …).

        Возвращает (pts_awarded, series_bonus_fired). Если capped:
        (0, False).
        """
        from services import user_calendar_keys
        if now_local is None:
            now_local = await self._now_local_for_user(user_id)
        local_date, week_iso = user_calendar_keys(now_local)
        await self._ensure_rows(user_id, local_date, week_iso)

        cursor = await self.db.execute(
            "UPDATE daily_score_counters "
            "SET quiz_count = quiz_count + 1, "
            "    quiz_series_running = quiz_series_running + 1 "
            "WHERE user_id=? AND local_date=? AND quiz_count < 25",
            (user_id, local_date),
        )
        if cursor.rowcount == 0:
            await self.db.commit()
            return (0, False)

        async with self.db.execute(
            "SELECT quiz_series_running FROM daily_score_counters "
            "WHERE user_id=? AND local_date=?",
            (user_id, local_date),
        ) as c:
            new_series = (await c.fetchone())["quiz_series_running"]

        pts = 5
        series_bonus = (new_series % 3 == 0)
        if series_bonus:
            pts += 15

        await self.db.execute(
            "UPDATE weekly_scores SET quiz_pts = quiz_pts + ? "
            "WHERE user_id=? AND week_iso=?",
            (pts, user_id, week_iso),
        )
        await self.db.commit()
        return (pts, series_bonus)

    # ------------------------------------------------------------
    # 4. Quiz wrong — сброс серии (без pts)
    # ------------------------------------------------------------
    async def reset_quiz_series(self, user_id: int, now_local=None) -> None:
        """
        Сбрасывает quiz_series_running в 0. Вызывается при wrong answer
        в quiz/MCQ. Никаких pts не начисляет и не отнимает.
        """
        from services import user_calendar_keys
        if now_local is None:
            now_local = await self._now_local_for_user(user_id)
        local_date, _ = user_calendar_keys(now_local)
        # daily-row может ещё не существовать — создаём перед UPDATE,
        # чтобы reset «работал» даже как первое действие дня (хотя
        # серия и так 0 у новой строки).
        await self.db.execute(
            "INSERT OR IGNORE INTO daily_score_counters (user_id, local_date) "
            "VALUES (?, ?)",
            (user_id, local_date),
        )
        await self.db.execute(
            "UPDATE daily_score_counters SET quiz_series_running = 0 "
            "WHERE user_id=? AND local_date=?",
            (user_id, local_date),
        )
        await self.db.commit()

    # ------------------------------------------------------------
    # 5. Card pts — +3 new / +5 review, daily cap 8 (LEADERBOARD.md §4)
    # ------------------------------------------------------------
    async def grant_card_pts(
        self, user_id: int, is_new: bool, now_local=None
    ) -> int:
        """
        Начисляет pts за УСПЕШНО (quality ≥ 3) повторённую/изученную карту.
        Caller отвечает за фильтрацию по quality — repo не знает SM-2.
        +3 для new (нет строки в flashcard_progress), +5 для review.
        Daily cap 8 successful reviews.

        Возвращает pts (0, 3 или 5). 0 = capped.
        """
        from services import user_calendar_keys
        if now_local is None:
            now_local = await self._now_local_for_user(user_id)
        local_date, week_iso = user_calendar_keys(now_local)
        await self._ensure_rows(user_id, local_date, week_iso)

        cursor = await self.db.execute(
            "UPDATE daily_score_counters SET cards_count = cards_count + 1 "
            "WHERE user_id=? AND local_date=? AND cards_count < 8",
            (user_id, local_date),
        )
        if cursor.rowcount == 0:
            await self.db.commit()
            return 0

        pts = 3 if is_new else 5
        await self.db.execute(
            "UPDATE weekly_scores SET card_pts = card_pts + ? "
            "WHERE user_id=? AND week_iso=?",
            (pts, user_id, week_iso),
        )
        await self.db.commit()
        return pts

    # ------------------------------------------------------------
    # 6. Read helpers (для UI, тестов, backtest)
    # ------------------------------------------------------------
    async def get_daily_counters(
        self, user_id: int, local_date: str
    ) -> Optional[Dict[str, Any]]:
        """Возвращает dict с дневными счётчиками или None если строки нет."""
        async with self.db.execute(
            "SELECT * FROM daily_score_counters "
            "WHERE user_id=? AND local_date=?",
            (user_id, local_date),
        ) as c:
            row = await c.fetchone()
        return dict(row) if row else None

    async def get_weekly_score(
        self, user_id: int, week_iso: str
    ) -> Optional[Dict[str, Any]]:
        """Возвращает dict с weekly-компонентами или None если строки нет."""
        async with self.db.execute(
            "SELECT * FROM weekly_scores WHERE user_id=? AND week_iso=?",
            (user_id, week_iso),
        ) as c:
            row = await c.fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------
    # 7. Ranked-сегмент + rank lookup (для /leaderboard + rollover)
    # ------------------------------------------------------------
    async def get_ranked_segment(
        self,
        week_iso: str,
        segment: str,
        *,
        exclude_hidden: bool = True,
    ) -> list:
        """
        Возвращает список dict'ов всех пользователей в сегменте, отсортированный
        по total_final DESC. Каждая запись:
          user_id, time_pts, task_pts, quiz_pts, card_pts,
          current_streak, multiplier, total_base, total_final, hidden

        segment ∈ {'newbie', 'main'}.
        - newbie: julianday(now) - julianday(u.created_at) < 7
        - main:   julianday(now) - julianday(u.created_at) >= 7

        Сортировка — в Python, после применения streak_multiplier; SQL-side
        ORDER BY total_base некорректен из-за multiplier'а (1.20× для 14+
        дней может перевернуть top-3).

        exclude_hidden=False — для get_user_rank, где hidden юзер должен
        видеть свою позицию.
        """
        from services import streak_multiplier
        if segment == "newbie":
            seg_cond = "julianday('now') - julianday(u.created_at) < 7"
        elif segment == "main":
            seg_cond = "julianday('now') - julianday(u.created_at) >= 7"
        else:
            raise ValueError(f"Unknown segment: {segment!r}")
        hide_cond = "AND u.hidden_from_leaderboards = 0" if exclude_hidden else ""

        sql = (
            "SELECT ws.user_id, ws.time_pts, ws.task_pts, ws.quiz_pts, ws.card_pts, "
            "       u.current_streak, u.hidden_from_leaderboards "
            "FROM weekly_scores ws "
            "JOIN users u ON ws.user_id = u.user_id "
            f"WHERE ws.week_iso = ? AND {seg_cond} {hide_cond}"
        )
        async with self.db.execute(sql, (week_iso,)) as c:
            rows = await c.fetchall()

        result = []
        for r in rows:
            mult = streak_multiplier(r["current_streak"])
            base = (
                r["time_pts"] + r["task_pts"] + r["quiz_pts"] + r["card_pts"]
            )
            result.append({
                "user_id": r["user_id"],
                "time_pts": r["time_pts"],
                "task_pts": r["task_pts"],
                "quiz_pts": r["quiz_pts"],
                "card_pts": r["card_pts"],
                "current_streak": r["current_streak"],
                "multiplier": mult,
                "total_base": base,
                "total_final": base * mult,
                "hidden": bool(r["hidden_from_leaderboards"]),
            })
        result.sort(key=lambda x: x["total_final"], reverse=True)
        return result

    async def get_user_rank(
        self, user_id: int, week_iso: str, segment: str
    ) -> tuple:
        """
        Возвращает (rank, entry_dict) для user в указанном segment'е,
        или (None, None) если user не в сегменте или не имеет weekly-row.

        exclude_hidden=False внутри — чтобы hidden user мог увидеть
        свой rank (только себе). Для публичной leaderboard caller
        дополнительно фильтрует.
        """
        ranked = await self.get_ranked_segment(
            week_iso, segment, exclude_hidden=False
        )
        for idx, entry in enumerate(ranked):
            if entry["user_id"] == user_id:
                return (idx + 1, entry)
        return (None, None)

    # ------------------------------------------------------------
    # 8. Weekly badges — атомарное awarding + чтение активных
    # ------------------------------------------------------------
    async def award_badge(
        self,
        user_id: int,
        badge_id: str,
        awarded_for_week: str,
        *,
        duration_days: int = 7,
    ) -> bool:
        """
        Идемпотентное awarding: INSERT OR IGNORE на PK (user_id, badge_id,
        awarded_for_week). Возвращает True если бэдж впервые выдан этой
        операцией, False если уже был (например, при повторном rollover'е).

        Используется и для cosmetic badges (top_1/top_2/top_3/breakthrough),
        и для маркеров coin-бонусов (badge_id='top10_pct_bonus') — caller
        проверяет return и начисляет coins только если True.
        """
        from datetime import datetime, timedelta
        now = datetime.now()
        expires = now + timedelta(days=duration_days)
        cursor = await self.db.execute(
            "INSERT OR IGNORE INTO weekly_badges "
            "(user_id, badge_id, awarded_for_week, awarded_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                user_id, badge_id, awarded_for_week,
                now.strftime("%Y-%m-%d %H:%M:%S"),
                expires.strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        await self.db.commit()
        if cursor.rowcount > 0:
            self._logger.info(
                "leaderboard.badge_awarded user_id=%s badge=%s week=%s",
                user_id, badge_id, awarded_for_week,
            )
        return cursor.rowcount > 0

    async def get_active_badges(self, user_id: int) -> list:
        """
        Возвращает не-истёкшие бэджи пользователя, последние первыми.
        Используется для profile-отображения и /leaderboard'а.
        """
        async with self.db.execute(
            "SELECT badge_id, awarded_for_week, awarded_at, expires_at "
            "FROM weekly_badges "
            "WHERE user_id=? AND expires_at > datetime('now') "
            "ORDER BY awarded_at DESC",
            (user_id,),
        ) as c:
            rows = await c.fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------
    # 9. Streak freeze (LEADERBOARD.md §Streak Freeze)
    # ------------------------------------------------------------
    async def purchase_freeze(self, user_id: int, current_streak: int) -> str:
        """
        Атомарная покупка заморозки стрика под self.db.lock. Цена считается
        из `services.freeze_cost(current_streak)` (500 / 750 / 1000).

        Спека: «one freeze per 7 days max» — cooldown enforced через
        MAX(granted_at) > now - 7 days; даже если предыдущий freeze
        ещё не consumed, новый купить нельзя.

        Возвращает статус-строку для UI:
          - 'purchased'           — успешно
          - 'cooldown_active'     — freeze покупался в последние 7 дней
          - 'insufficient_coins'  — не хватает монет
        """
        from services import freeze_cost
        cost = freeze_cost(current_streak)

        async with self.db.lock:
            # Cooldown
            async with self.db.execute(
                "SELECT 1 FROM streak_freezes "
                "WHERE user_id=? AND granted_at > datetime('now', '-7 days') "
                "LIMIT 1",
                (user_id,),
            ) as c:
                if await c.fetchone():
                    return "cooldown_active"

            # Balance
            async with self.db.execute(
                "SELECT total_coins FROM users WHERE user_id=?", (user_id,),
            ) as c:
                row = await c.fetchone()
            balance = row["total_coins"] if row else 0
            if balance < cost:
                return "insufficient_coins"

            # Deduct + record
            await self.db.execute(
                "UPDATE users SET total_coins = total_coins - ? "
                "WHERE user_id=?",
                (cost, user_id),
            )
            await self.db.execute(
                "INSERT INTO streak_freezes "
                "(user_id, granted_at, streak_at_grant, cost_paid) "
                "VALUES (?, datetime('now'), ?, ?)",
                (user_id, current_streak, cost),
            )
            await self.db.commit()
            self._logger.info(
                "streak.freeze_purchased user_id=%s streak=%s cost=%s "
                "balance_after=%s",
                user_id, current_streak, cost, balance - cost,
            )
            return "purchased"

    async def has_active_freeze(self, user_id: int) -> bool:
        """True если у пользователя есть купленный, но ещё не использованный freeze."""
        async with self.db.execute(
            "SELECT 1 FROM streak_freezes "
            "WHERE user_id=? AND consumed_for_date IS NULL LIMIT 1",
            (user_id,),
        ) as c:
            return (await c.fetchone()) is not None

    async def consume_freeze_if_active(
        self, user_id: int, today_local: str
    ) -> bool:
        """
        Если у пользователя есть unused freeze — отмечает его как
        использованный (consumed_for_date=today_local). Используется в
        StreakService на missed-day path: вместо reset стрика, скармливаем
        ему накопленный freeze.

        Возвращает True если freeze был consumed (стрик сохраняется);
        False если активного freeze не было (caller сбрасывает стрик).
        """
        cursor = await self.db.execute(
            "UPDATE streak_freezes SET consumed_for_date=? "
            "WHERE user_id=? AND granted_at = ("
            "  SELECT granted_at FROM streak_freezes "
            "  WHERE user_id=? AND consumed_for_date IS NULL "
            "  ORDER BY granted_at ASC LIMIT 1"
            ")",
            (today_local, user_id, user_id),
        )
        await self.db.commit()
        if cursor.rowcount > 0:
            self._logger.info(
                "streak.freeze_consumed user_id=%s date=%s rows=%s",
                user_id, today_local, cursor.rowcount,
            )
        return cursor.rowcount > 0

    async def get_freeze_cooldown_remaining_days(self, user_id: int) -> int:
        """
        Возвращает сколько ПОЛНЫХ дней осталось до возможности купить
        следующий freeze. 0 = можно покупать прямо сейчас.
        """
        async with self.db.execute(
            "SELECT CAST(7 - (julianday('now') - julianday(MAX(granted_at))) "
            "       AS INTEGER) AS remaining "
            "FROM streak_freezes "
            "WHERE user_id=? AND granted_at > datetime('now', '-7 days')",
            (user_id,),
        ) as c:
            row = await c.fetchone()
        remaining = row["remaining"] if row else None
        return max(0, remaining) if remaining is not None else 0


class FriendRepository:
    """
    Friends system (Phase 4 / LEADERBOARD.md §Segments → Friends).

    Хранит pending requests + accepted friendships в нормализованной
    форме (user_a < user_b — одна строка на дружбу). Reverse-direction
    request на existing pending → auto-accept (cross-fires the friendship).
    """

    def __init__(self, db: aiosqlite.Connection):
        self.db = db
        import logging
        self._logger = logging.getLogger("studybuddy_bot")

    @staticmethod
    def _norm_pair(a: int, b: int) -> tuple:
        """Returns (smaller, larger). Используется для friendship-PK."""
        return (a, b) if a < b else (b, a)

    # ------------------------------------------------------------
    # Request lifecycle: send → accept / reject / cancel
    # ------------------------------------------------------------
    async def send_request(self, from_uid: int, to_uid: int) -> str:
        """
        Атомарно (под self.db.lock) отправляет friend-request. Возвращает
        статус-строку для UI:
          - 'self_target'     — попытка дружить с самим собой
          - 'user_not_found'  — target user'а нет в таблице users
          - 'already_friends' — дружба уже существует
          - 'already_pending' — same-direction request уже отправлен
          - 'auto_accepted'   — reverse-direction request от target существовал;
                                автоматически создаём friendship и удаляем
                                reverse-request (обоюдность достигнута)
          - 'sent'            — новый pending request создан
        """
        if from_uid == to_uid:
            return "self_target"

        async with self.db.lock:
            async with self.db.execute(
                "SELECT 1 FROM users WHERE user_id=?", (to_uid,),
            ) as c:
                if not await c.fetchone():
                    return "user_not_found"

            ua, ub = self._norm_pair(from_uid, to_uid)
            async with self.db.execute(
                "SELECT 1 FROM friendships WHERE user_a=? AND user_b=?",
                (ua, ub),
            ) as c:
                if await c.fetchone():
                    return "already_friends"

            # Reverse direction pending → auto-accept
            async with self.db.execute(
                "SELECT 1 FROM friend_requests "
                "WHERE from_user_id=? AND to_user_id=?",
                (to_uid, from_uid),
            ) as c:
                if await c.fetchone():
                    await self.db.execute(
                        "DELETE FROM friend_requests "
                        "WHERE from_user_id=? AND to_user_id=?",
                        (to_uid, from_uid),
                    )
                    await self.db.execute(
                        "INSERT INTO friendships (user_a, user_b) "
                        "VALUES (?, ?)",
                        (ua, ub),
                    )
                    await self.db.commit()
                    self._logger.info(
                        "friends.auto_accepted user_a=%s user_b=%s "
                        "via_from=%s",
                        ua, ub, from_uid,
                    )
                    return "auto_accepted"

            # New request (INSERT OR IGNORE — PK предотвращает дубль)
            cursor = await self.db.execute(
                "INSERT OR IGNORE INTO friend_requests "
                "(from_user_id, to_user_id) VALUES (?, ?)",
                (from_uid, to_uid),
            )
            await self.db.commit()
            if cursor.rowcount == 0:
                return "already_pending"
            self._logger.info(
                "friends.request_sent from=%s to=%s",
                from_uid, to_uid,
            )
            return "sent"

    async def accept_request(self, from_uid: int, to_uid: int) -> bool:
        """
        to_uid принимает request от from_uid. Транзакционно
        (под self.db.lock): DELETE pending + INSERT friendship.
        Возвращает True если accept'ed, False если pending request
        не существовал.
        """
        ua, ub = self._norm_pair(from_uid, to_uid)
        async with self.db.lock:
            cursor = await self.db.execute(
                "DELETE FROM friend_requests "
                "WHERE from_user_id=? AND to_user_id=?",
                (from_uid, to_uid),
            )
            if cursor.rowcount == 0:
                await self.db.commit()
                return False
            await self.db.execute(
                "INSERT OR IGNORE INTO friendships (user_a, user_b) "
                "VALUES (?, ?)",
                (ua, ub),
            )
            await self.db.commit()
            self._logger.info(
                "friends.accepted from=%s to=%s normalized=(%s,%s)",
                from_uid, to_uid, ua, ub,
            )
            return True

    async def reject_request(self, from_uid: int, to_uid: int) -> bool:
        """to_uid отклоняет request от from_uid. Returns True если был pending."""
        cursor = await self.db.execute(
            "DELETE FROM friend_requests "
            "WHERE from_user_id=? AND to_user_id=?",
            (from_uid, to_uid),
        )
        await self.db.commit()
        if cursor.rowcount > 0:
            self._logger.info(
                "friends.rejected from=%s to=%s", from_uid, to_uid,
            )
        return cursor.rowcount > 0

    async def cancel_request(self, from_uid: int, to_uid: int) -> bool:
        """from_uid отменяет отправленный им request. Same SQL as reject."""
        return await self.reject_request(from_uid, to_uid)

    # ------------------------------------------------------------
    # Read helpers
    # ------------------------------------------------------------
    async def get_pending_received(self, to_uid: int) -> list:
        """Список pending requests, полученных пользователем (отсорт. по дате)."""
        async with self.db.execute(
            "SELECT from_user_id, created_at FROM friend_requests "
            "WHERE to_user_id=? ORDER BY created_at ASC",
            (to_uid,),
        ) as c:
            return [dict(r) for r in await c.fetchall()]

    async def get_pending_sent(self, from_uid: int) -> list:
        """Список pending requests, отправленных пользователем."""
        async with self.db.execute(
            "SELECT to_user_id, created_at FROM friend_requests "
            "WHERE from_user_id=? ORDER BY created_at ASC",
            (from_uid,),
        ) as c:
            return [dict(r) for r in await c.fetchall()]

    async def get_friends(self, user_id: int) -> list:
        """Все user_id-друзей пользователя. UNION над обеими сторонами PK."""
        async with self.db.execute(
            "SELECT user_b AS friend_id FROM friendships WHERE user_a=? "
            "UNION "
            "SELECT user_a AS friend_id FROM friendships WHERE user_b=?",
            (user_id, user_id),
        ) as c:
            return [r["friend_id"] for r in await c.fetchall()]

    async def are_friends(self, a: int, b: int) -> bool:
        if a == b:
            return False
        ua, ub = self._norm_pair(a, b)
        async with self.db.execute(
            "SELECT 1 FROM friendships WHERE user_a=? AND user_b=?",
            (ua, ub),
        ) as c:
            return (await c.fetchone()) is not None

    async def remove_friend(self, a: int, b: int) -> bool:
        """Удалить дружбу bidirectional. Returns True если что-то удалено."""
        ua, ub = self._norm_pair(a, b)
        cursor = await self.db.execute(
            "DELETE FROM friendships WHERE user_a=? AND user_b=?",
            (ua, ub),
        )
        await self.db.commit()
        if cursor.rowcount > 0:
            self._logger.info(
                "friends.removed user_a=%s user_b=%s", ua, ub,
            )
        return cursor.rowcount > 0

    # ------------------------------------------------------------
    # Invite-links (BACKLOG → ship): t.me/Bot?start=friend_<token>
    # ------------------------------------------------------------
    async def create_invite_token(self, from_uid: int) -> str:
        """
        Создаёт новый invite-token для пользователя. TTL 30 дней.
        Multiuse: токен можно отдать многим людям, каждый клик → новая
        дружба. Возвращает сам токен (URL-safe строка ~16 символов).
        """
        import secrets
        token = secrets.token_urlsafe(12)
        await self.db.execute(
            "INSERT INTO friend_invite_tokens (token, from_user_id, expires_at) "
            "VALUES (?, ?, datetime('now', '+30 days'))",
            (token, from_uid),
        )
        await self.db.commit()
        self._logger.info(
            "friends.invite_token_created user_id=%s token=%s",
            from_uid, token,
        )
        return token

    async def find_invite_token(self, token: str):
        """
        Резолвит токен в from_user_id. Истёкшие токены трактуются как
        несуществующие — возвращает None.
        """
        if not token:
            return None
        async with self.db.execute(
            "SELECT from_user_id FROM friend_invite_tokens "
            "WHERE token=? AND expires_at > datetime('now')",
            (token,),
        ) as c:
            row = await c.fetchone()
        return row["from_user_id"] if row else None

    async def accept_invite(self, from_uid: int, invitee_uid: int) -> str:
        """
        Создаёт дружбу invitee + creator напрямую (skip pending state).
        Семантика: shared link = consent creator'а; click = consent
        invitee. Оба согласились → atomic INSERT в friendships.

        Идемпотентно: повторный вызов возвращает 'already_friends'.
        Self-invite (invitee == creator) запрещён.

        Также cleans up pending requests в обе стороны (если в это время
        был отправлен «обычный» request, deep-link его перекрывает).

        Returns статус: 'accepted' / 'already_friends' / 'self'.
        """
        if from_uid == invitee_uid:
            return "self"
        ua, ub = self._norm_pair(from_uid, invitee_uid)
        async with self.db.lock:
            async with self.db.execute(
                "SELECT 1 FROM friendships WHERE user_a=? AND user_b=?",
                (ua, ub),
            ) as c:
                if await c.fetchone():
                    return "already_friends"
            await self.db.execute(
                "INSERT INTO friendships (user_a, user_b) VALUES (?, ?)",
                (ua, ub),
            )
            # Если был pending request в любую сторону — удалим
            # (deep-link обходит pending state)
            await self.db.execute(
                "DELETE FROM friend_requests "
                "WHERE (from_user_id=? AND to_user_id=?) "
                "   OR (from_user_id=? AND to_user_id=?)",
                (from_uid, invitee_uid, invitee_uid, from_uid),
            )
            await self.db.commit()
            self._logger.info(
                "friends.invite_accepted creator=%s invitee=%s normalized=(%s,%s)",
                from_uid, invitee_uid, ua, ub,
            )
            return "accepted"
