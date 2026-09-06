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


class TestPaginationDoesNotFarmAchievement:
    """
    Регрессия TODO #18: листание «📋 Все советы» (◀️/▶️) раньше вызывало
    полный gamification-hook, поэтому ачивку «Любознательный» можно было
    получить перелистыванием одной категории, а total_views переставал
    отражать число прочитанных советов.
    """

    @pytest_asyncio.fixture
    async def wired_bot(self, tips_repo, user_repo, ach_service, created_user, monkeypatch):
        import bot
        from repository import EventRepository
        monkeypatch.setattr(bot, "tips_repo", tips_repo)
        monkeypatch.setattr(bot, "user_repo", user_repo)
        monkeypatch.setattr(bot, "ach_service", ach_service)
        monkeypatch.setattr(bot, "event_repo", EventRepository(user_repo.db))
        return bot

    async def _tip_events(self, db, uid) -> int:
        async with db.execute(
            "SELECT COUNT(*) AS n FROM events "
            "WHERE user_id = ? AND event_name = 'tip_viewed'",
            (uid,),
        ) as cur:
            row = await cur.fetchone()
        return row["n"]

    async def _total_views(self, db, uid) -> int:
        async with db.execute(
            "SELECT total_views FROM user_tips_stats WHERE user_id = ?", (uid,)
        ) as cur:
            row = await cur.fetchone()
        return row["total_views"] if row else 0

    async def test_paging_does_not_increment_total_views(
        self, wired_bot, tips_repo, created_user, db,
    ):
        for _ in range(15):
            suffix = await wired_bot._on_tip_viewed(
                created_user, "tm", "tm-01", count_view=False,
            )
            assert suffix == ""
        assert await self._total_views(db, created_user) == 0
        assert await self._tip_events(db, created_user) == 0

    async def test_paging_still_records_seen_for_cooldown(
        self, wired_bot, tips_repo, created_user,
    ):
        await wired_bot._on_tip_viewed(created_user, "tm", "tm-01", count_view=False)
        seen = await tips_repo.get_recently_seen_tip_ids(created_user, 7)
        assert "tm-01" in seen

    async def test_paging_does_not_award_achievement(
        self, wired_bot, created_user, db,
    ):
        for _ in range(12):
            await wired_bot._on_tip_viewed(
                created_user, "tm", "tm-01", count_view=False,
            )
        async with db.execute(
            "SELECT completed FROM user_achievements "
            "WHERE user_id = ? AND achievement_id = '10_tips_read'",
            (created_user,),
        ) as cur:
            row = await cur.fetchone()
        assert row is None, "ачивка не должна двигаться от листания"

    async def test_real_view_still_counts(self, wired_bot, created_user, db):
        suffix = await wired_bot._on_tip_viewed(created_user, "tm", "tm-01")
        assert await self._total_views(db, created_user) == 1
        assert await self._tip_events(db, created_user) == 1
        assert suffix != ""

    @staticmethod
    def _fake_callback(uid: int, data: str):
        """Минимальный CallbackQuery: хендлеру нужны data, from_user и message."""
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        message = SimpleNamespace(
            edit_text=AsyncMock(), answer=AsyncMock(), chat=SimpleNamespace(id=uid),
        )
        return SimpleNamespace(
            data=data,
            from_user=SimpleNamespace(id=uid),
            message=message,
            answer=AsyncMock(),
        )

    async def test_list_handler_does_not_count_views(
        self, wired_bot, created_user, db,
    ):
        """Прогон настоящего `tips:list` через хендлер: счётчик не двигается."""
        for page in range(5):
            cb = self._fake_callback(created_user, f"tips:list:tm:{page}")
            await wired_bot.handle_tips_list(cb)
            assert cb.message.edit_text.await_count == 1, "страница должна отрисоваться"
        assert await self._total_views(db, created_user) == 0
        assert await self._tip_events(db, created_user) == 0

    async def test_more_handler_counts_view(self, wired_bot, created_user, db):
        """«🔄 Ещё совет» через тот же рендер — просмотр засчитывается."""
        cb = self._fake_callback(created_user, "tips:more:tm")
        await wired_bot.handle_tips_more(cb)
        assert cb.message.edit_text.await_count == 1
        assert await self._total_views(db, created_user) == 1
        assert await self._tip_events(db, created_user) == 1
