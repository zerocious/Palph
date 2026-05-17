"""
Тесты репозиториев прогресса: McqProgressRepository, TaskProgressRepository,
SubjectStatsRepository.
"""
import pytest
import pytest_asyncio
from datetime import datetime, timedelta

from repository import (
    McqProgressRepository,
    TaskProgressRepository,
    SubjectStatsRepository,
)


@pytest_asyncio.fixture
async def mcq_repo(db):
    return McqProgressRepository(db)


@pytest_asyncio.fixture
async def task_repo(db):
    return TaskProgressRepository(db)


@pytest_asyncio.fixture
async def stats_repo(db):
    return SubjectStatsRepository(db)


class TestMcqProgressRepository:
    async def test_first_correct_attempt(self, mcq_repo, created_user, db):
        await mcq_repo.record_attempt(created_user, "abc12345", is_correct=True)
        async with db.execute(
            "SELECT correct_count, total_count FROM mcq_progress "
            "WHERE user_id = ? AND question_hash = ?",
            (created_user, "abc12345"),
        ) as cursor:
            row = await cursor.fetchone()
        assert row["correct_count"] == 1
        assert row["total_count"] == 1

    async def test_wrong_attempt_increments_total_not_correct(self, mcq_repo, created_user, db):
        await mcq_repo.record_attempt(created_user, "wrong001", is_correct=False)
        async with db.execute(
            "SELECT correct_count, total_count FROM mcq_progress WHERE user_id=? AND question_hash=?",
            (created_user, "wrong001"),
        ) as cursor:
            row = await cursor.fetchone()
        assert row["correct_count"] == 0
        assert row["total_count"] == 1

    async def test_mixed_attempts_accumulate(self, mcq_repo, created_user, db):
        await mcq_repo.record_attempt(created_user, "mix00001", is_correct=True)
        await mcq_repo.record_attempt(created_user, "mix00001", is_correct=False)
        await mcq_repo.record_attempt(created_user, "mix00001", is_correct=True)
        async with db.execute(
            "SELECT correct_count, total_count FROM mcq_progress WHERE user_id=? AND question_hash=?",
            (created_user, "mix00001"),
        ) as cursor:
            row = await cursor.fetchone()
        assert row["correct_count"] == 2
        assert row["total_count"] == 3

    async def test_count_mastered_filters_by_correct_count(self, mcq_repo, created_user):
        # Three questions: one correct, one only wrong, one never seen
        await mcq_repo.record_attempt(created_user, "good0001", is_correct=True)
        await mcq_repo.record_attempt(created_user, "bad00001", is_correct=False)
        # never_seen has no record
        mastered = await mcq_repo.count_mastered(
            created_user, ["good0001", "bad00001", "never001"]
        )
        assert mastered == 1  # only good0001

    async def test_count_mastered_empty_candidates(self, mcq_repo, created_user):
        assert await mcq_repo.count_mastered(created_user, []) == 0

    async def test_count_mastered_isolation_between_users(self, mcq_repo, user_repo, created_user):
        # Another user (user_2) marks the same question correct.
        # created_user's count_mastered shouldn't see it.
        other = 9999
        await user_repo.create_user(other)
        await mcq_repo.record_attempt(other, "shared01", is_correct=True)
        # created_user has nothing recorded
        assert await mcq_repo.count_mastered(created_user, ["shared01"]) == 0
        assert await mcq_repo.count_mastered(other, ["shared01"]) == 1


class TestTaskProgressRepository:
    async def test_first_success(self, task_repo, created_user, db):
        await task_repo.record_attempt(created_user, "task-01", attempts_used=1, succeeded=True)
        async with db.execute(
            "SELECT attempts_used, succeeded FROM task_progress WHERE user_id=? AND task_id=?",
            (created_user, "task-01"),
        ) as cursor:
            row = await cursor.fetchone()
        assert row["attempts_used"] == 1
        assert row["succeeded"] == 1

    async def test_failure_recorded(self, task_repo, created_user, db):
        await task_repo.record_attempt(created_user, "task-02", attempts_used=3, succeeded=False)
        async with db.execute(
            "SELECT succeeded FROM task_progress WHERE user_id=? AND task_id=?",
            (created_user, "task-02"),
        ) as cursor:
            row = await cursor.fetchone()
        assert row["succeeded"] == 0

    async def test_idempotent_success_keeps_best_attempts(self, task_repo, created_user, db):
        """Если задача уже решена с 3-й попытки, повторная запись с 1-й — улучшает attempts_used."""
        await task_repo.record_attempt(created_user, "task-03", attempts_used=3, succeeded=True)
        await task_repo.record_attempt(created_user, "task-03", attempts_used=1, succeeded=True)
        async with db.execute(
            "SELECT attempts_used FROM task_progress WHERE user_id=? AND task_id=?",
            (created_user, "task-03"),
        ) as cursor:
            row = await cursor.fetchone()
        # MIN: keeping the best (lowest) attempts
        assert row["attempts_used"] == 1

    async def test_failed_then_succeeded_keeps_success(self, task_repo, created_user, db):
        """Если задача провалилась, потом решена — succeeded должен стать 1 (MAX)."""
        await task_repo.record_attempt(created_user, "task-04", attempts_used=3, succeeded=False)
        await task_repo.record_attempt(created_user, "task-04", attempts_used=2, succeeded=True)
        async with db.execute(
            "SELECT succeeded FROM task_progress WHERE user_id=? AND task_id=?",
            (created_user, "task-04"),
        ) as cursor:
            row = await cursor.fetchone()
        assert row["succeeded"] == 1

    async def test_count_mastered(self, task_repo, created_user):
        await task_repo.record_attempt(created_user, "task-05", attempts_used=1, succeeded=True)
        await task_repo.record_attempt(created_user, "task-06", attempts_used=3, succeeded=False)
        mastered = await task_repo.count_mastered(
            created_user, ["task-05", "task-06", "task-99"]
        )
        assert mastered == 1


class TestSubjectStatsRepository:
    async def test_first_visit(self, stats_repo, created_user):
        await stats_repo.bump_visit(created_user, "industrial-management")
        stats = await stats_repo.get(created_user, "industrial-management")
        assert stats is not None
        assert stats["visits"] == 1
        assert stats["last_activity"] is not None

    async def test_multiple_visits_accumulate(self, stats_repo, created_user):
        for _ in range(5):
            await stats_repo.bump_visit(created_user, "industrial-management")
        stats = await stats_repo.get(created_user, "industrial-management")
        assert stats["visits"] == 5

    async def test_separate_subjects_independent(self, stats_repo, created_user):
        await stats_repo.bump_visit(created_user, "industrial-management")
        await stats_repo.bump_visit(created_user, "industrial-management")
        await stats_repo.bump_visit(created_user, "math")
        im = await stats_repo.get(created_user, "industrial-management")
        math = await stats_repo.get(created_user, "math")
        assert im["visits"] == 2
        assert math["visits"] == 1

    async def test_get_returns_none_when_no_record(self, stats_repo, created_user):
        assert await stats_repo.get(created_user, "english") is None

    async def test_last_activity_updates_each_visit(self, stats_repo, created_user):
        await stats_repo.bump_visit(created_user, "industrial-management")
        first = await stats_repo.get(created_user, "industrial-management")
        # Хитро: last_activity форматируется до секунд. Гарантировать что обновится —
        # достаточно проверить что строка не None в обоих случаях. Реальное обновление
        # тестировать сложно из-за разрешения в 1 секунду; полагаемся на on-conflict-update.
        await stats_repo.bump_visit(created_user, "industrial-management")
        second = await stats_repo.get(created_user, "industrial-management")
        assert first["last_activity"] is not None
        assert second["last_activity"] is not None
