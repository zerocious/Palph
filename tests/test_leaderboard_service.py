"""
Тесты LeaderboardService + новых read-методов LeaderboardRepository,
а также privacy-флага в UserRepository.

Scope соответствует тому, что shipped в Phase 2:
- render_leaderboard (формат текста, auto-routing сегмента, скрытые юзеры)
- get_ranked_segment (фильтр сегмента, multiplier, hidden)
- get_user_rank
- award_badge (идемпотентность)
- get_active_badges (expiration)
- is_hidden_from_leaderboards / set_hidden_from_leaderboards

Rollover/coin-bonus тесты не входят в этот файл — соответствующая логика
ещё не реализована (см. LEADERBOARD.md Phase 2 deferred).
"""
from datetime import datetime, timedelta

import pytest
import pytest_asyncio

from repository import LeaderboardRepository
from services import LeaderboardService


NOW = datetime(2026, 5, 18, 14, 30)   # Monday, mid-day
WEEK = "2026-W21"


@pytest_asyncio.fixture
async def lb_repo(db):
    return LeaderboardRepository(db)


@pytest_asyncio.fixture
async def lb_service(user_repo, lb_repo):
    return LeaderboardService(user_repo, lb_repo)


async def _make_user(user_repo, db, uid, age_days, *, streak=0, hidden=False):
    """Создаёт user и сразу 'старит' его на N дней через UPDATE created_at."""
    await user_repo.create_user(uid)
    created_at = (datetime.now() - timedelta(days=age_days)).strftime("%Y-%m-%d %H:%M:%S")
    await db.execute(
        "UPDATE users SET created_at=?, current_streak=?, hidden_from_leaderboards=? "
        "WHERE user_id=?",
        (created_at, streak, 1 if hidden else 0, uid),
    )
    await db.commit()


async def _grant(lb_repo, uid, *, time=0, task=0, quiz=0, card=0):
    """Прямой write в weekly_scores для теста; обходит cap-логику."""
    await lb_repo._ensure_rows(uid, "2026-05-18", WEEK)
    await lb_repo.db.execute(
        "UPDATE weekly_scores SET time_pts=?, task_pts=?, quiz_pts=?, card_pts=? "
        "WHERE user_id=? AND week_iso=?",
        (time, task, quiz, card, uid, WEEK),
    )
    await lb_repo.db.commit()


# ============================================================
# LeaderboardRepository — get_ranked_segment, get_user_rank
# ============================================================
class TestGetRankedSegment:
    async def test_empty_segment(self, lb_repo, user_repo, db):
        await _make_user(user_repo, db, 1, age_days=30)
        ranked = await lb_repo.get_ranked_segment(WEEK, "main")
        assert ranked == []

    async def test_segment_filter_separates_newbie_main(
        self, lb_repo, user_repo, db
    ):
        # Two users — one fresh (newbie), one old (main); both have scores.
        await _make_user(user_repo, db, 1, age_days=2)   # newbie
        await _make_user(user_repo, db, 2, age_days=30)  # main
        await _grant(lb_repo, 1, task=100)
        await _grant(lb_repo, 2, task=200)

        newbies = await lb_repo.get_ranked_segment(WEEK, "newbie")
        mains = await lb_repo.get_ranked_segment(WEEK, "main")
        assert [r["user_id"] for r in newbies] == [1]
        assert [r["user_id"] for r in mains] == [2]

    async def test_sorts_by_total_final_with_multiplier(
        self, lb_repo, user_repo, db
    ):
        # User A: base 1000, streak 0 → final 1000
        # User B: base 900, streak 14 → final 1080
        # B should rank above A even though base is lower.
        await _make_user(user_repo, db, 1, age_days=30, streak=0)
        await _make_user(user_repo, db, 2, age_days=30, streak=14)
        await _grant(lb_repo, 1, task=1000)
        await _grant(lb_repo, 2, task=900)

        ranked = await lb_repo.get_ranked_segment(WEEK, "main")
        assert [r["user_id"] for r in ranked] == [2, 1]
        assert ranked[0]["multiplier"] == 1.20
        assert ranked[0]["total_final"] == pytest.approx(1080.0)

    async def test_excludes_hidden_by_default(self, lb_repo, user_repo, db):
        await _make_user(user_repo, db, 1, age_days=30, hidden=False)
        await _make_user(user_repo, db, 2, age_days=30, hidden=True)
        await _grant(lb_repo, 1, task=100)
        await _grant(lb_repo, 2, task=200)  # top by score, но скрытый

        ranked = await lb_repo.get_ranked_segment(WEEK, "main")
        assert [r["user_id"] for r in ranked] == [1]

    async def test_includes_hidden_when_requested(self, lb_repo, user_repo, db):
        await _make_user(user_repo, db, 1, age_days=30, hidden=False)
        await _make_user(user_repo, db, 2, age_days=30, hidden=True)
        await _grant(lb_repo, 1, task=100)
        await _grant(lb_repo, 2, task=200)

        ranked = await lb_repo.get_ranked_segment(
            WEEK, "main", exclude_hidden=False
        )
        # Сортировка по total_final: 2 (200) > 1 (100)
        assert [r["user_id"] for r in ranked] == [2, 1]
        # Hidden-флаг сохраняется в record'е
        assert ranked[0]["hidden"] is True
        assert ranked[1]["hidden"] is False

    async def test_unknown_segment_raises(self, lb_repo):
        with pytest.raises(ValueError):
            await lb_repo.get_ranked_segment(WEEK, "premium")


class TestGetUserRank:
    async def test_returns_rank_and_entry(self, lb_repo, user_repo, db):
        await _make_user(user_repo, db, 1, age_days=30)
        await _make_user(user_repo, db, 2, age_days=30)
        await _make_user(user_repo, db, 3, age_days=30)
        await _grant(lb_repo, 1, task=300)
        await _grant(lb_repo, 2, task=200)
        await _grant(lb_repo, 3, task=100)

        rank, entry = await lb_repo.get_user_rank(2, WEEK, "main")
        assert rank == 2
        assert entry["user_id"] == 2

    async def test_returns_none_for_user_without_score(
        self, lb_repo, user_repo, db
    ):
        await _make_user(user_repo, db, 1, age_days=30)
        rank, entry = await lb_repo.get_user_rank(1, WEEK, "main")
        assert rank is None
        assert entry is None

    async def test_hidden_user_still_gets_rank(self, lb_repo, user_repo, db):
        """Hidden user видит свой rank — exclude_hidden=False внутри."""
        await _make_user(user_repo, db, 1, age_days=30, hidden=False)
        await _make_user(user_repo, db, 2, age_days=30, hidden=True)
        await _grant(lb_repo, 1, task=100)
        await _grant(lb_repo, 2, task=200)

        rank, entry = await lb_repo.get_user_rank(2, WEEK, "main")
        assert rank == 1   # hidden user реально первый по очкам
        assert entry["hidden"] is True


# ============================================================
# award_badge / get_active_badges
# ============================================================
class TestAwardBadge:
    async def test_first_award_returns_true(self, lb_repo, created_user):
        result = await lb_repo.award_badge(created_user, "top_1", WEEK)
        assert result is True

    async def test_duplicate_award_returns_false(self, lb_repo, created_user):
        first = await lb_repo.award_badge(created_user, "top_1", WEEK)
        second = await lb_repo.award_badge(created_user, "top_1", WEEK)
        assert first is True
        assert second is False

    async def test_different_weeks_independent(self, lb_repo, created_user):
        assert await lb_repo.award_badge(created_user, "top_1", "2026-W21")
        assert await lb_repo.award_badge(created_user, "top_1", "2026-W22")


class TestGetActiveBadges:
    async def test_returns_non_expired(self, lb_repo, created_user):
        await lb_repo.award_badge(created_user, "top_1", WEEK)
        badges = await lb_repo.get_active_badges(created_user)
        assert len(badges) == 1
        assert badges[0]["badge_id"] == "top_1"

    async def test_excludes_expired(self, lb_repo, created_user, db):
        # Награждаем + сразу руками двигаем expires_at в прошлое
        await lb_repo.award_badge(created_user, "top_1", WEEK)
        await db.execute(
            "UPDATE weekly_badges SET expires_at = datetime('now', '-1 day') "
            "WHERE user_id=? AND badge_id=?",
            (created_user, "top_1"),
        )
        await db.commit()
        badges = await lb_repo.get_active_badges(created_user)
        assert badges == []


# ============================================================
# UserRepository — privacy column
# ============================================================
class TestPrivacyFlag:
    async def test_default_visible(self, user_repo, created_user):
        assert await user_repo.is_hidden_from_leaderboards(created_user) is False

    async def test_toggle_hidden(self, user_repo, created_user):
        await user_repo.set_hidden_from_leaderboards(created_user, True)
        assert await user_repo.is_hidden_from_leaderboards(created_user) is True
        await user_repo.set_hidden_from_leaderboards(created_user, False)
        assert await user_repo.is_hidden_from_leaderboards(created_user) is False

    async def test_nonexistent_user_returns_false(self, user_repo):
        # Defensive: нет user'а = считаем как не-скрытый (рендерить нечего)
        assert await user_repo.is_hidden_from_leaderboards(99999) is False


# ============================================================
# LeaderboardService.render_leaderboard
# ============================================================
class TestRenderLeaderboard:
    async def test_newbie_segment_for_fresh_user(
        self, lb_service, user_repo, lb_repo, db
    ):
        await _make_user(user_repo, db, 1, age_days=2)
        await _grant(lb_repo, 1, task=80)
        text = await lb_service.render_leaderboard(1)
        assert "Новички" in text

    async def test_main_segment_for_old_user(
        self, lb_service, user_repo, lb_repo, db
    ):
        await _make_user(user_repo, db, 1, age_days=30)
        await _grant(lb_repo, 1, task=80)
        text = await lb_service.render_leaderboard(1)
        assert "Основной" in text

    async def test_empty_segment_message(self, lb_service, user_repo, db):
        await _make_user(user_repo, db, 1, age_days=30)
        text = await lb_service.render_leaderboard(1)
        assert "никто не набрал" in text.lower() or "пока без очков" in text.lower()

    async def test_user_marked_in_top(
        self, lb_service, user_repo, lb_repo, db
    ):
        await _make_user(user_repo, db, 1, age_days=30)
        await _grant(lb_repo, 1, task=200)
        text = await lb_service.render_leaderboard(1)
        # Маркер для собственной строки
        assert "👤" in text
        assert "id=1" in text

    async def test_hidden_user_sees_own_rank_marker(
        self, lb_service, user_repo, lb_repo, db
    ):
        await _make_user(user_repo, db, 1, age_days=30, hidden=True)
        await _grant(lb_repo, 1, task=100)
        text = await lb_service.render_leaderboard(1)
        # Hidden user НЕ в публичном топе, но видит свой ранг с пометкой
        assert "Вы скрыты" in text

    async def test_hidden_user_not_in_others_top(
        self, lb_service, user_repo, lb_repo, db
    ):
        await _make_user(user_repo, db, 1, age_days=30, hidden=False)
        await _make_user(user_repo, db, 2, age_days=30, hidden=True)
        await _grant(lb_repo, 1, task=50)
        await _grant(lb_repo, 2, task=500)  # Топ-1 по очкам, но скрытый
        # User 1 рендерит лидерборд — должен видеть себя первым, без user 2
        text = await lb_service.render_leaderboard(1)
        assert "id=1" in text
        assert "id=2" not in text
