"""
Тесты LeaderboardService + новых read-методов LeaderboardRepository,
а также privacy-флага в UserRepository.

Покрывает Phase 2a (render + privacy + ranked-segment + badges) и
Phase 2b (run_rollover + ended-week computation):
- render_leaderboard (формат текста, auto-routing сегмента, скрытые юзеры)
- get_ranked_segment (фильтр сегмента, multiplier, hidden)
- get_user_rank
- award_badge (идемпотентность)
- get_active_badges (expiration)
- is_hidden_from_leaderboards / set_hidden_from_leaderboards
- run_rollover (top-3 main + breakthrough newbie + top-10% bonus,
  идемпотентность повторного запуска, hidden остаётся eligible)
- _compute_ended_week_iso (UTC anchor)
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


# ============================================================
# run_rollover — Phase 2b
# ============================================================
async def _get_badge_count(db, user_id):
    """Сколько активных бэджей у юзера."""
    async with db.execute(
        "SELECT COUNT(*) AS n FROM weekly_badges WHERE user_id=?",
        (user_id,),
    ) as c:
        return (await c.fetchone())["n"]


async def _get_coins(db, user_id):
    async with db.execute(
        "SELECT total_coins FROM users WHERE user_id=?", (user_id,)
    ) as c:
        row = await c.fetchone()
    return row["total_coins"] if row else 0


class TestRunRollover:
    async def test_empty_world_no_op(self, lb_service):
        """Никаких юзеров — rollover не падает и считает нули."""
        stats = await lb_service.run_rollover(WEEK)
        assert stats == {
            "week": WEEK,
            "badges_awarded": 0,
            "coins_distributed": 0,
            "segments_processed": 0,
        }

    async def test_main_top3_awarded(self, lb_service, user_repo, lb_repo, db):
        # 5 main-юзеров с разными очками
        for uid, pts in [(1, 500), (2, 400), (3, 300), (4, 200), (5, 100)]:
            await _make_user(user_repo, db, uid, age_days=30)
            await _grant(lb_repo, uid, task=pts)

        stats = await lb_service.run_rollover(WEEK)
        # 3 топ-бэджа за main; newbie сегмент пустой
        assert stats["badges_awarded"] == 3
        assert stats["segments_processed"] == 1
        # top-10% не выдан (5 юзеров < MIN_SEGMENT_FOR_TOP10_BONUS=10)
        assert stats["coins_distributed"] == 0

        # User 1 = top_1, User 2 = top_2, User 3 = top_3
        async with db.execute(
            "SELECT user_id, badge_id FROM weekly_badges "
            "WHERE awarded_for_week=? ORDER BY user_id",
            (WEEK,),
        ) as c:
            rows = await c.fetchall()
        result = [(r["user_id"], r["badge_id"]) for r in rows]
        assert result == [(1, "top_1"), (2, "top_2"), (3, "top_3")]

    async def test_newbie_breakthrough_only(
        self, lb_service, user_repo, lb_repo, db
    ):
        # 3 новичка
        for uid, pts in [(1, 100), (2, 80), (3, 60)]:
            await _make_user(user_repo, db, uid, age_days=2)
            await _grant(lb_repo, uid, task=pts)

        stats = await lb_service.run_rollover(WEEK)
        # Только top-1 newbie получает breakthrough
        assert stats["badges_awarded"] == 1

        async with db.execute(
            "SELECT user_id, badge_id FROM weekly_badges WHERE awarded_for_week=?",
            (WEEK,),
        ) as c:
            rows = await c.fetchall()
        assert len(rows) == 1
        assert rows[0]["user_id"] == 1
        assert rows[0]["badge_id"] == "breakthrough"

    async def test_top10_pct_skipped_below_threshold(
        self, lb_service, user_repo, lb_repo, db
    ):
        """С 5 юзерами 10% = 0.5, скип — иначе bonus превращается в 'всем подряд'."""
        for uid in range(1, 6):
            await _make_user(user_repo, db, uid, age_days=30)
            await _grant(lb_repo, uid, task=(6 - uid) * 100)

        stats = await lb_service.run_rollover(WEEK)
        assert stats["coins_distributed"] == 0
        # Никаких top10_pct_bonus бэджей
        async with db.execute(
            "SELECT COUNT(*) AS n FROM weekly_badges "
            "WHERE badge_id='top10_pct_bonus'"
        ) as c:
            assert (await c.fetchone())["n"] == 0

    async def test_top10_pct_awarded_at_threshold(
        self, lb_service, user_repo, lb_repo, db
    ):
        """10 юзеров → 10% = 1 человек получает coin-бонус."""
        for uid in range(1, 11):
            await _make_user(user_repo, db, uid, age_days=30)
            await _grant(lb_repo, uid, task=(11 - uid) * 100)

        stats = await lb_service.run_rollover(WEEK)
        # 3 топ-бэджа + 1 top-10%
        assert stats["badges_awarded"] == 4
        assert stats["coins_distributed"] == LeaderboardService.COIN_BONUS_TOP10_PCT
        # User 1 — top_1 + top10_pct_bonus
        assert await _get_badge_count(db, 1) == 2
        assert await _get_coins(db, 1) == LeaderboardService.COIN_BONUS_TOP10_PCT

    async def test_top10_pct_floor_at_20_users(
        self, lb_service, user_repo, lb_repo, db
    ):
        """20 юзеров → 10% = 2 человека."""
        for uid in range(1, 21):
            await _make_user(user_repo, db, uid, age_days=30)
            await _grant(lb_repo, uid, task=(21 - uid) * 100)

        stats = await lb_service.run_rollover(WEEK)
        # 3 top + 2 top-10%
        assert stats["badges_awarded"] == 5
        assert (
            stats["coins_distributed"]
            == 2 * LeaderboardService.COIN_BONUS_TOP10_PCT
        )

    async def test_idempotent_no_duplicate_badges(
        self, lb_service, user_repo, lb_repo, db
    ):
        """Второй run_rollover для той же недели — no-op."""
        for uid in range(1, 11):
            await _make_user(user_repo, db, uid, age_days=30)
            await _grant(lb_repo, uid, task=(11 - uid) * 100)

        await lb_service.run_rollover(WEEK)
        # Снова, той же неделей
        stats_2 = await lb_service.run_rollover(WEEK)
        # Все badges INSERT OR IGNORE — повторно ничего не вставилось
        assert stats_2["badges_awarded"] == 0
        assert stats_2["coins_distributed"] == 0

    async def test_idempotent_no_double_coins(
        self, lb_service, user_repo, lb_repo, db
    ):
        """Повторный rollover не дублирует coin-бонус (rowcount-gated)."""
        for uid in range(1, 11):
            await _make_user(user_repo, db, uid, age_days=30)
            await _grant(lb_repo, uid, task=(11 - uid) * 100)

        await lb_service.run_rollover(WEEK)
        coins_after_first = await _get_coins(db, 1)
        await lb_service.run_rollover(WEEK)
        coins_after_second = await _get_coins(db, 1)
        assert coins_after_first == coins_after_second
        assert coins_after_first == LeaderboardService.COIN_BONUS_TOP10_PCT

    async def test_hidden_user_still_gets_rewards(
        self, lb_service, user_repo, lb_repo, db
    ):
        """Hidden user не отображается публично, но rewards получает —
        по спеке (LEADERBOARD.md §Privacy)."""
        # User 1 — hidden, top score; user 2 — visible, second
        await _make_user(user_repo, db, 1, age_days=30, hidden=True)
        await _make_user(user_repo, db, 2, age_days=30, hidden=False)
        await _grant(lb_repo, 1, task=500)
        await _grant(lb_repo, 2, task=300)

        await lb_service.run_rollover(WEEK)
        # User 1 (hidden) должен иметь top_1 badge
        async with db.execute(
            "SELECT badge_id FROM weekly_badges WHERE user_id=?",
            (1,),
        ) as c:
            badges = [r["badge_id"] for r in await c.fetchall()]
        assert "top_1" in badges

    async def test_different_weeks_independent(
        self, lb_service, user_repo, lb_repo, db
    ):
        """Rollover для разных недель — независимое awarding."""
        await _make_user(user_repo, db, 1, age_days=30)
        await _grant(lb_repo, 1, task=100)

        await lb_service.run_rollover(WEEK)
        # Для следующей недели — другой week_iso, но weekly_scores не имеет
        # данных. Должно быть no-op.
        stats = await lb_service.run_rollover("2026-W22")
        assert stats["badges_awarded"] == 0
        # Прошлая неделя по-прежнему имеет ровно 1 badge (top_1)
        async with db.execute(
            "SELECT COUNT(*) AS n FROM weekly_badges WHERE awarded_for_week=?",
            (WEEK,),
        ) as c:
            assert (await c.fetchone())["n"] == 1


# ============================================================
# tasks._compute_ended_week_iso — UTC Tuesday anchor
# ============================================================
class TestComputeEndedWeekIso:
    def test_tuesday_anchor_returns_previous_week(self):
        from tasks import _compute_ended_week_iso
        # Tue 2026-05-19 00:00 UTC → ended week was 2026-W20 (Mon 11 - Sun 17)
        now = datetime(2026, 5, 19, 0, 0)
        assert _compute_ended_week_iso(now) == "2026-W20"

    def test_january_year_boundary(self):
        from tasks import _compute_ended_week_iso
        # Tue 2026-01-06 00:00 UTC → ended week = Mon 2025-12-29 - Sun 2026-01-04
        # ISO year of that week = 2026, week 1.
        now = datetime(2026, 1, 6, 0, 0)
        assert _compute_ended_week_iso(now) == "2026-W01"

    def test_late_december_iso_week_52(self):
        from tasks import _compute_ended_week_iso
        # Tue 2024-12-31 00:00 UTC → ended week = Mon 2024-12-23 - Sun 2024-12-29
        # ISO week 52 of 2024.
        now = datetime(2024, 12, 31, 0, 0)
        assert _compute_ended_week_iso(now) == "2024-W52"
