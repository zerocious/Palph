"""
Тесты UserRepository.delete_user_completely.

Покрывает:
- Удаление основной строки users + CASCADE на FK-таблицы
- Ручное удаление таблиц БЕЗ FK (quiz_progress, flashcard_progress,
  mcq_progress, task_progress, user_subject_stats, events)
- Очистка fsm_storage по паттерну ":<uid>:<uid>:"
- Возвращаемый dict counts корректно отражает удалённые строки
- Не трогает данные ДРУГИХ пользователей
- Идемпотентно: повторный вызов на отсутствующем user'е возвращает нули
"""
import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def populated_user(user_repo, db) -> int:
    """
    Пользователь с записями в каждой из таблиц, чтобы тесты могли
    проверить, что все они вычищены при delete_user_completely.
    """
    uid = 1001
    await user_repo.create_user(uid)

    # FK-таблицы (CASCADE)
    await db.execute(
        "INSERT INTO study_sessions (user_id, duration_minutes, coins_earned) VALUES (?, ?, ?)",
        (uid, 25, 25),
    )
    await db.execute(
        "INSERT INTO user_achievements (user_id, achievement_id, progress, target, completed) "
        "VALUES (?, ?, ?, ?, ?)",
        (uid, "first_session", 1, 1, 1),
    )
    await db.execute(
        "INSERT INTO user_pet (user_id, name, color, accessory, level, xp) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (uid, "Pet", "orange", "none", 2, 50),
    )
    await db.execute(
        "INSERT INTO user_pet_inventory (user_id, item_type, item_value) VALUES (?, ?, ?)",
        (uid, "color", "orange"),
    )
    await db.execute(
        "INSERT INTO weekly_scores (user_id, week_iso, time_pts) VALUES (?, ?, ?)",
        (uid, "2026-W21", 30.0),
    )
    await db.execute(
        "INSERT INTO user_flashcards (user_id, subject_id, term, definition) "
        "VALUES (?, ?, ?, ?)",
        (uid, "math", "term", "def"),
    )
    await db.execute(
        "INSERT INTO user_tasks (user_id, subject_id, problem, accepted) "
        "VALUES (?, ?, ?, ?)",
        (uid, "math", "p", "a"),
    )
    await db.execute(
        "INSERT INTO user_tips_stats (user_id, total_views) VALUES (?, ?)",
        (uid, 5),
    )
    await db.execute(
        "INSERT INTO user_tips_seen (user_id, tip_id) VALUES (?, ?)",
        (uid, "tip-1"),
    )

    # No-FK таблицы (ручной DELETE)
    await db.execute(
        "INSERT INTO quiz_progress (user_id, term_hash, is_correct, streak) "
        "VALUES (?, ?, ?, ?)",
        (uid, "h1", 1, 1),
    )
    await db.execute(
        "INSERT INTO flashcard_progress (user_id, card_hash, ease_factor, repetitions) "
        "VALUES (?, ?, ?, ?)",
        (uid, "h2", 2.5, 1),
    )
    await db.execute(
        "INSERT INTO mcq_progress (user_id, question_hash, correct_count, total_count) "
        "VALUES (?, ?, ?, ?)",
        (uid, "h3", 1, 1),
    )
    await db.execute(
        "INSERT INTO task_progress (user_id, task_id, attempts_used, succeeded) "
        "VALUES (?, ?, ?, ?)",
        (uid, "t1", 1, 1),
    )
    await db.execute(
        "INSERT INTO user_subject_stats (user_id, subject_id, visits) VALUES (?, ?, ?)",
        (uid, "math", 3),
    )
    await db.execute(
        "INSERT INTO events (user_id, event_name, properties) VALUES (?, ?, ?)",
        (uid, "session_completed", "{}"),
    )

    # fsm_storage — приватный чат: key = bot_id:uid:uid:0
    await db.execute(
        "INSERT INTO fsm_storage (key, state, data) VALUES (?, ?, ?)",
        (f"8001:{uid}:{uid}:0", "SomeState", "{}"),
    )

    await db.commit()
    return uid


class TestDeletionWipesAllTables:
    async def test_users_row_deleted(self, user_repo, populated_user, db):
        await user_repo.delete_user_completely(populated_user)
        async with db.execute(
            "SELECT 1 FROM users WHERE user_id = ?", (populated_user,)
        ) as c:
            assert await c.fetchone() is None

    @pytest.mark.parametrize(
        "table",
        [
            "study_sessions",
            "user_achievements",
            "user_pet",
            "user_pet_inventory",
            "weekly_scores",
            "user_flashcards",
            "user_tasks",
            "user_tips_stats",
            "user_tips_seen",
            "notification_settings",
        ],
    )
    async def test_fk_cascade_tables_wiped(
        self, user_repo, populated_user, db, table
    ):
        await user_repo.delete_user_completely(populated_user)
        async with db.execute(
            f"SELECT COUNT(*) FROM {table} WHERE user_id = ?", (populated_user,)
        ) as c:
            row = await c.fetchone()
        assert row[0] == 0, f"FK cascade did not wipe {table}"

    @pytest.mark.parametrize(
        "table",
        [
            "quiz_progress",
            "flashcard_progress",
            "mcq_progress",
            "task_progress",
            "user_subject_stats",
            "events",
        ],
    )
    async def test_no_fk_tables_wiped(
        self, user_repo, populated_user, db, table
    ):
        await user_repo.delete_user_completely(populated_user)
        async with db.execute(
            f"SELECT COUNT(*) FROM {table} WHERE user_id = ?", (populated_user,)
        ) as c:
            row = await c.fetchone()
        assert row[0] == 0, f"manual DELETE did not wipe {table}"

    async def test_fsm_storage_wiped(self, user_repo, populated_user, db):
        await user_repo.delete_user_completely(populated_user)
        async with db.execute(
            "SELECT COUNT(*) FROM fsm_storage WHERE key LIKE ?",
            (f"%:{populated_user}:{populated_user}:%",),
        ) as c:
            row = await c.fetchone()
        assert row[0] == 0


class TestDeletionIsolation:
    async def test_other_users_data_untouched(self, user_repo, populated_user, db):
        """delete_user_completely(A) не должен затронуть данные B."""
        other = 9999
        await user_repo.create_user(other)
        await db.execute(
            "INSERT INTO study_sessions (user_id, duration_minutes, coins_earned) "
            "VALUES (?, ?, ?)",
            (other, 10, 10),
        )
        await db.execute(
            "INSERT INTO events (user_id, event_name, properties) VALUES (?, ?, ?)",
            (other, "x", "{}"),
        )
        await db.execute(
            "INSERT INTO fsm_storage (key, state, data) VALUES (?, ?, ?)",
            (f"8001:{other}:{other}:0", "S", "{}"),
        )
        await db.commit()

        await user_repo.delete_user_completely(populated_user)

        async with db.execute(
            "SELECT 1 FROM users WHERE user_id = ?", (other,)
        ) as c:
            assert await c.fetchone() is not None
        async with db.execute(
            "SELECT COUNT(*) FROM study_sessions WHERE user_id = ?", (other,)
        ) as c:
            row = await c.fetchone()
        assert row[0] == 1
        async with db.execute(
            "SELECT COUNT(*) FROM events WHERE user_id = ?", (other,)
        ) as c:
            row = await c.fetchone()
        assert row[0] == 1
        async with db.execute(
            "SELECT COUNT(*) FROM fsm_storage WHERE key LIKE ?",
            (f"%:{other}:{other}:%",),
        ) as c:
            row = await c.fetchone()
        assert row[0] == 1


class TestCountsDict:
    async def test_counts_includes_all_tables(self, user_repo, populated_user):
        counts = await user_repo.delete_user_completely(populated_user)
        # No-FK таблицы + fsm_storage + users — итого 8 ключей
        assert set(counts.keys()) == {
            "quiz_progress",
            "flashcard_progress",
            "mcq_progress",
            "task_progress",
            "user_subject_stats",
            "events",
            "fsm_storage",
            "users",
        }

    async def test_counts_reflect_actual_deletions(self, user_repo, populated_user):
        counts = await user_repo.delete_user_completely(populated_user)
        # Каждая no-FK таблица получила 1 INSERT в fixture
        assert counts["quiz_progress"] == 1
        assert counts["flashcard_progress"] == 1
        assert counts["mcq_progress"] == 1
        assert counts["task_progress"] == 1
        assert counts["user_subject_stats"] == 1
        assert counts["events"] == 1
        assert counts["fsm_storage"] == 1
        assert counts["users"] == 1


class TestIdempotency:
    async def test_deleting_unknown_user_returns_zeros(self, user_repo):
        counts = await user_repo.delete_user_completely(404404)
        assert counts["users"] == 0
        assert all(v == 0 for v in counts.values())

    async def test_double_delete_no_error(self, user_repo, populated_user):
        await user_repo.delete_user_completely(populated_user)
        counts = await user_repo.delete_user_completely(populated_user)
        assert counts["users"] == 0
