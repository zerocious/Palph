# repository.py
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

