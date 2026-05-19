"""
Тесты LeaderboardRepository — score-инкременты для weekly leaderboard.

Ключевые инварианты:
- Все grant_-методы атомарно enforce daily caps (через WHERE/rowcount)
- grant_time_pts использует piecewise; хранимый time_minutes capped at 240
- grant_quiz_pts_correct: 5 pts + 15 series bonus каждые 3 правильных подряд
- reset_quiz_series обнуляет ТОЛЬКО quiz_series_running, не quiz_count
- grant_card_pts: 3 (new) / 5 (review); caller отвечает за quality ≥ 3
- daily/weekly rows автокреативятся через _ensure_rows
"""
from datetime import datetime

import pytest
import pytest_asyncio

from repository import LeaderboardRepository


# Фиксированный «локальный сейчас» — детерминизм. Понедельник, 14:30 — точно
# дневной интервал (не sleepy, не на границе суток).
NOW = datetime(2026, 5, 18, 14, 30)
TODAY = "2026-05-18"
WEEK = "2026-W21"


@pytest_asyncio.fixture
async def lb_repo(db):
    return LeaderboardRepository(db)


# ============================================================
# grant_time_pts
# ============================================================
class TestGrantTimePts:
    async def test_first_session_pts(self, lb_repo, created_user):
        pts = await lb_repo.grant_time_pts(created_user, 60, now_local=NOW)
        assert pts == 60.0
        ws = await lb_repo.get_weekly_score(created_user, WEEK)
        assert ws["time_pts"] == 60.0
        dc = await lb_repo.get_daily_counters(created_user, TODAY)
        assert dc["time_minutes"] == 60

    async def test_accumulation_uses_previous_minutes(self, lb_repo, created_user):
        # Две сессии по 60 мин: первая в tier1 (60×1.0=60), вторая в tier2 (60×0.75=45)
        p1 = await lb_repo.grant_time_pts(created_user, 60, now_local=NOW)
        p2 = await lb_repo.grant_time_pts(created_user, 60, now_local=NOW)
        assert p1 == 60.0
        assert p2 == 45.0
        ws = await lb_repo.get_weekly_score(created_user, WEEK)
        assert ws["time_pts"] == 105.0

    async def test_daily_max(self, lb_repo, created_user):
        # 240 мин одним куском → 150 pts (daily max)
        pts = await lb_repo.grant_time_pts(created_user, 240, now_local=NOW)
        assert pts == 150.0

    async def test_over_cap_no_extra_pts(self, lb_repo, created_user):
        # Уже на cap → следующая сессия 30 мин = 0 pts.
        await lb_repo.grant_time_pts(created_user, 240, now_local=NOW)
        extra = await lb_repo.grant_time_pts(created_user, 30, now_local=NOW)
        assert extra == 0.0
        dc = await lb_repo.get_daily_counters(created_user, TODAY)
        # time_minutes capped at 240 (хранимое значение)
        assert dc["time_minutes"] == 240

    async def test_zero_or_negative_no_op(self, lb_repo, created_user):
        assert await lb_repo.grant_time_pts(created_user, 0, now_local=NOW) == 0.0
        assert await lb_repo.grant_time_pts(created_user, -5, now_local=NOW) == 0.0
        # Никаких строк не создалось
        assert await lb_repo.get_daily_counters(created_user, TODAY) is None
        assert await lb_repo.get_weekly_score(created_user, WEEK) is None

    async def test_now_local_default_uses_user_tz(self, lb_repo, created_user):
        # Без now_local — берёт users.timezone (Europe/Moscow по дефолту);
        # цель теста: вызов не падает и пишет в weekly_scores.
        pts = await lb_repo.grant_time_pts(created_user, 25)
        assert pts > 0
        # Хотя бы один weekly-row создан (любой week_iso)
        async with lb_repo.db.execute(
            "SELECT COUNT(*) AS n FROM weekly_scores WHERE user_id=?",
            (created_user,),
        ) as c:
            row = await c.fetchone()
        assert row["n"] >= 1


# ============================================================
# grant_task_pts
# ============================================================
class TestGrantTaskPts:
    async def test_first_call_grants(self, lb_repo, created_user):
        ok = await lb_repo.grant_task_pts(created_user, now_local=NOW)
        assert ok is True
        ws = await lb_repo.get_weekly_score(created_user, WEEK)
        assert ws["task_pts"] == 40

    async def test_daily_cap_at_5(self, lb_repo, created_user):
        # Первые 5 успешны
        for i in range(5):
            assert await lb_repo.grant_task_pts(created_user, now_local=NOW) is True
        # 6-я — capped
        assert await lb_repo.grant_task_pts(created_user, now_local=NOW) is False
        ws = await lb_repo.get_weekly_score(created_user, WEEK)
        assert ws["task_pts"] == 5 * 40

    async def test_independent_across_days(self, lb_repo, created_user):
        # Полный cap «вчера»
        yesterday = datetime(2026, 5, 17, 12, 0)
        for _ in range(5):
            await lb_repo.grant_task_pts(created_user, now_local=yesterday)
        assert await lb_repo.grant_task_pts(created_user, now_local=yesterday) is False
        # Сегодня (другая local_date) — снова можно
        assert await lb_repo.grant_task_pts(created_user, now_local=NOW) is True


# ============================================================
# grant_quiz_pts_correct + reset_quiz_series
# ============================================================
class TestGrantQuizPtsCorrect:
    async def test_first_correct(self, lb_repo, created_user):
        pts, bonus = await lb_repo.grant_quiz_pts_correct(created_user, now_local=NOW)
        assert pts == 5
        assert bonus is False

    async def test_third_correct_triggers_series_bonus(self, lb_repo, created_user):
        results = [
            await lb_repo.grant_quiz_pts_correct(created_user, now_local=NOW)
            for _ in range(3)
        ]
        assert results[0] == (5, False)
        assert results[1] == (5, False)
        assert results[2] == (20, True)   # 5 + 15 bonus
        ws = await lb_repo.get_weekly_score(created_user, WEEK)
        assert ws["quiz_pts"] == 5 + 5 + 20

    async def test_every_third_fires_bonus(self, lb_repo, created_user):
        bonuses = []
        for _ in range(9):
            pts, bonus = await lb_repo.grant_quiz_pts_correct(created_user, now_local=NOW)
            bonuses.append(bonus)
        # На 3-й, 6-й, 9-й — bonus
        assert bonuses == [False, False, True, False, False, True, False, False, True]
        ws = await lb_repo.get_weekly_score(created_user, WEEK)
        # 9 × 5 + 3 × 15 = 45 + 45 = 90
        assert ws["quiz_pts"] == 90

    async def test_daily_cap_25(self, lb_repo, created_user):
        # 25 правильных — все начисляются
        for _ in range(25):
            pts, _ = await lb_repo.grant_quiz_pts_correct(created_user, now_local=NOW)
            assert pts > 0
        # 26-й — capped
        pts, bonus = await lb_repo.grant_quiz_pts_correct(created_user, now_local=NOW)
        assert pts == 0
        assert bonus is False


class TestResetQuizSeries:
    async def test_reset_kills_pending_bonus(self, lb_repo, created_user):
        # 2 правильных
        await lb_repo.grant_quiz_pts_correct(created_user, now_local=NOW)
        await lb_repo.grant_quiz_pts_correct(created_user, now_local=NOW)
        # Wrong → reset
        await lb_repo.reset_quiz_series(created_user, now_local=NOW)
        # 3-й правильный после reset = НЕ bonus (серия с 1)
        pts, bonus = await lb_repo.grant_quiz_pts_correct(created_user, now_local=NOW)
        assert pts == 5
        assert bonus is False

    async def test_reset_preserves_quiz_count(self, lb_repo, created_user):
        for _ in range(10):
            await lb_repo.grant_quiz_pts_correct(created_user, now_local=NOW)
        await lb_repo.reset_quiz_series(created_user, now_local=NOW)
        dc = await lb_repo.get_daily_counters(created_user, TODAY)
        assert dc["quiz_count"] == 10
        assert dc["quiz_series_running"] == 0

    async def test_reset_on_empty_day_is_noop(self, lb_repo, created_user):
        # До reset нет ни одного правильного — reset не должен ломаться
        await lb_repo.reset_quiz_series(created_user, now_local=NOW)
        dc = await lb_repo.get_daily_counters(created_user, TODAY)
        assert dc["quiz_count"] == 0
        assert dc["quiz_series_running"] == 0


# ============================================================
# grant_card_pts
# ============================================================
class TestGrantCardPts:
    async def test_new_card_three_pts(self, lb_repo, created_user):
        pts = await lb_repo.grant_card_pts(created_user, is_new=True, now_local=NOW)
        assert pts == 3

    async def test_review_card_five_pts(self, lb_repo, created_user):
        pts = await lb_repo.grant_card_pts(created_user, is_new=False, now_local=NOW)
        assert pts == 5

    async def test_daily_cap_8(self, lb_repo, created_user):
        # 8 успешных reviews
        for i in range(8):
            pts = await lb_repo.grant_card_pts(
                created_user, is_new=(i == 0), now_local=NOW
            )
            assert pts > 0
        # 9-я — capped
        pts = await lb_repo.grant_card_pts(created_user, is_new=False, now_local=NOW)
        assert pts == 0

    async def test_weekly_accumulation(self, lb_repo, created_user):
        # 3 new + 2 review = 3×3 + 2×5 = 19
        for _ in range(3):
            await lb_repo.grant_card_pts(created_user, is_new=True, now_local=NOW)
        for _ in range(2):
            await lb_repo.grant_card_pts(created_user, is_new=False, now_local=NOW)
        ws = await lb_repo.get_weekly_score(created_user, WEEK)
        assert ws["card_pts"] == 19


# ============================================================
# Read helpers
# ============================================================
class TestReadHelpers:
    async def test_get_daily_counters_returns_none_if_unset(self, lb_repo, created_user):
        assert await lb_repo.get_daily_counters(created_user, TODAY) is None

    async def test_get_weekly_score_returns_none_if_unset(self, lb_repo, created_user):
        assert await lb_repo.get_weekly_score(created_user, WEEK) is None

    async def test_get_daily_counters_after_grant(self, lb_repo, created_user):
        await lb_repo.grant_task_pts(created_user, now_local=NOW)
        dc = await lb_repo.get_daily_counters(created_user, TODAY)
        assert dc is not None
        assert dc["task_count"] == 1

    async def test_get_weekly_score_after_grant(self, lb_repo, created_user):
        await lb_repo.grant_task_pts(created_user, now_local=NOW)
        ws = await lb_repo.get_weekly_score(created_user, WEEK)
        assert ws is not None
        assert ws["task_pts"] == 40
