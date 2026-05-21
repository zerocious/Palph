"""Геймификация советов: дневная монета и достижение 10_tips_read."""
import pytest
import pytest_asyncio

from repository import TipsRepository, UserRepository
from services import AchievementService


@pytest_asyncio.fixture
async def tips_repo(db):
    return TipsRepository(db)


@pytest_asyncio.fixture
async def ach_service(user_repo, achievements_catalog):
    return AchievementService(user_repo, achievements_catalog)


class TestTipsRepository:
    async def test_first_view_grants_coin(self, tips_repo, created_user):
        total, coin = await tips_repo.record_view(created_user, "2026-05-22")
        assert total == 1
        assert coin is True

    async def test_second_view_same_day_no_coin(self, tips_repo, created_user):
        await tips_repo.record_view(created_user, "2026-05-22")
        total, coin = await tips_repo.record_view(created_user, "2026-05-22")
        assert total == 2
        assert coin is False

    async def test_new_day_grants_coin_again(self, tips_repo, created_user):
        await tips_repo.record_view(created_user, "2026-05-22")
        total, coin = await tips_repo.record_view(created_user, "2026-05-23")
        assert total == 2
        assert coin is True


class TestTipsAchievement:
    async def test_10_tips_read_at_10_views(self, ach_service, created_user):
        new_ids, bonus = await ach_service.check_tips_award(created_user, 10)
        assert "10_tips_read" in new_ids
        assert bonus == 30

    async def test_progress_before_10(self, ach_service, created_user, user_repo, db):
        await ach_service.check_tips_award(created_user, 5)
        async with db.execute(
            "SELECT progress, target, completed FROM user_achievements "
            "WHERE user_id = ? AND achievement_id = '10_tips_read'",
            (created_user,),
        ) as cur:
            row = await cur.fetchone()
        assert row["progress"] == 5
        assert row["target"] == 10
        assert row["completed"] == 0

    async def test_not_re_awarded(self, ach_service, created_user):
        await ach_service.check_tips_award(created_user, 10)
        new_ids, bonus = await ach_service.check_tips_award(created_user, 15)
        assert "10_tips_read" not in new_ids
        assert bonus == 0
