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
    async def create_user(self, user_id: int, timezone: str = "Europe/Moscow") -> None:
        """
        Создаёт запись в users и дефолтные настройки уведомлений.
        Если пользователь уже существует — ничего не делает.
        """
        # INSERT OR IGNORE гарантирует идемпотентность
        await self.db.execute(
            "INSERT OR IGNORE INTO users (user_id, timezone) VALUES (?, ?)",
            (user_id, timezone)
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
            "SELECT user_id, has_studied_today, current_streak, total_coins "
            "FROM users WHERE timezone = ?",
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
        Обновляет настройки уведомлений пользователя.
        Ожидает словарь с ключами:
        morning_enabled, morning_time, evening_enabled, evening_time,
        streak_enabled, achievements_enabled.
        """
        await self.db.execute(
            """UPDATE notification_settings SET
            morning_enabled = ?,
            morning_time = ?,
            evening_enabled = ?,
            evening_time = ?,
            streak_enabled = ?,
            achievements_enabled = ?
            WHERE user_id = ?""",
            (
                settings.get("morning_enabled", 1),
                settings.get("morning_time", "09:00"),
                settings.get("evening_enabled", 1),
                settings.get("evening_time", "21:00"),
                settings.get("streak_enabled", 1),
                settings.get("achievements_enabled", 1),
                user_id
            )
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

    async def log(
        self,
        user_id: int | None,
        event_name: str,
        properties: dict | None = None,
    ) -> None:
        """
        Регистрирует event. Не raises — failure тихо логируется в bot.log,
        чтобы analytics-issues не валили бизнес-логику бота.

        properties сериализуется в JSON. None → '{}' (пустой dict).
        """
        import json
        try:
            props_json = json.dumps(properties or {}, ensure_ascii=False)
            await self.db.execute(
                "INSERT INTO events (user_id, event_name, properties) "
                "VALUES (?, ?, ?)",
                (user_id, event_name, props_json),
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
