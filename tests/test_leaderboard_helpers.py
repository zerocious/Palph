"""
Тесты pure-функций scoring helpers'ов в services.py.

Эти 4 функции — единственное место, где зашита численная сторона
формулы лидерборда. Они должны быть железно надёжны: переломаются
helpers → переломаются все score-инкременты + backtest notebook.
"""
from datetime import datetime

import pytest

from services import (
    format_leaderboard_user_label,
    freeze_cost,
    piecewise_time_pts,
    streak_multiplier,
    user_calendar_keys,
)


# ============================================================
# format_leaderboard_user_label — @username или id= fallback
# ============================================================
class TestFormatLeaderboardUserLabel:
    def test_username(self):
        assert format_leaderboard_user_label("alice", 42) == "@alice"

    def test_no_username(self):
        assert format_leaderboard_user_label(None, 42) == "id=42"
        assert format_leaderboard_user_label("", 7) == "id=7"


# ============================================================
# piecewise_time_pts — рисует тарифную сетку:
#   0–60   мин  ×1.00
#   61–120 мин  ×0.75
#   121–180 мин ×0.50
#   181–240 мин ×0.25
#   241+   мин  ×0
# Хранится дневной max = 150 pts.
# ============================================================
class TestPiecewiseTimePts:
    def test_zero_minutes(self):
        assert piecewise_time_pts(0, 0) == 0.0

    def test_only_tier1(self):
        assert piecewise_time_pts(0, 60) == 60.0

    def test_through_tier2(self):
        # 60 × 1.0 + 60 × 0.75 = 105
        assert piecewise_time_pts(0, 120) == 105.0

    def test_through_tier3(self):
        # ... + 60 × 0.50 = 135
        assert piecewise_time_pts(0, 180) == 135.0

    def test_daily_max(self):
        # ... + 60 × 0.25 = 150 (daily cap)
        assert piecewise_time_pts(0, 240) == 150.0

    def test_overflow_no_extra_pts(self):
        # 241+ мин не приносят pts; cap 150.
        assert piecewise_time_pts(0, 300) == 150.0
        assert piecewise_time_pts(0, 1000) == 150.0

    def test_crossing_tier_boundary(self):
        # 30→90: 30 мин в tier1 (1.0) + 30 мин в tier2 (0.75) = 30 + 22.5 = 52.5
        assert piecewise_time_pts(30, 90) == 52.5

    def test_within_single_tier(self):
        # 10→30: 20 мин × 1.0 = 20
        assert piecewise_time_pts(10, 30) == 20.0
        # 100→120: 20 мин × 0.75 = 15
        assert piecewise_time_pts(100, 120) == 15.0

    def test_end_less_than_start(self):
        assert piecewise_time_pts(50, 30) == 0.0

    def test_end_equals_start(self):
        assert piecewise_time_pts(120, 120) == 0.0

    def test_partial_overflow(self):
        # 200→260: только 200..240 даёт pts = 40 × 0.25 = 10
        assert piecewise_time_pts(200, 260) == 10.0

    def test_entirely_above_cap(self):
        assert piecewise_time_pts(250, 300) == 0.0

    def test_negative_start_clamps_to_zero(self):
        # Defensive: -10→50 трактуется как 0→50
        assert piecewise_time_pts(-10, 50) == 50.0


# ============================================================
# streak_multiplier — множитель weekly_score по длине стрика.
# 0–2:   ×1.00
# 3–6:   ×1.05
# 7–13:  ×1.10
# 14+:   ×1.20
# ============================================================
class TestStreakMultiplier:
    @pytest.mark.parametrize("days,expected", [
        (0, 1.00),
        (1, 1.00),
        (2, 1.00),
        (3, 1.05),
        (4, 1.05),
        (6, 1.05),
        (7, 1.10),
        (8, 1.10),
        (13, 1.10),
        (14, 1.20),
        (30, 1.20),
        (365, 1.20),
    ])
    def test_tier(self, days, expected):
        assert streak_multiplier(days) == expected


# ============================================================
# freeze_cost — стоимость заморозки стрика монетами.
# ≤7:    500
# 8–20:  750
# 21+:   1000
# ============================================================
class TestFreezeCost:
    @pytest.mark.parametrize("days,expected", [
        (0, 500),
        (1, 500),
        (7, 500),
        (8, 750),
        (15, 750),
        (20, 750),
        (21, 1000),
        (30, 1000),
        (100, 1000),
    ])
    def test_tier(self, days, expected):
        assert freeze_cost(days) == expected


# ============================================================
# user_calendar_keys — local_date + ISO week из datetime.
# ============================================================
class TestUserCalendarKeys:
    def test_returns_tuple_of_strings(self):
        d, w = user_calendar_keys(datetime(2026, 5, 18, 14, 30))
        assert isinstance(d, str)
        assert isinstance(w, str)

    def test_local_date_format(self):
        d, _ = user_calendar_keys(datetime(2026, 5, 18, 14, 30))
        assert d == "2026-05-18"

    def test_iso_week_format(self):
        # 18 мая 2026 = понедельник, ISO неделя 21.
        _, w = user_calendar_keys(datetime(2026, 5, 18, 14, 30))
        assert w == "2026-W21"

    def test_iso_week_boundary_jan(self):
        # 1 января 2024 = понедельник, ISO год = 2024, неделя 1.
        _, w = user_calendar_keys(datetime(2024, 1, 1, 0, 0))
        assert w == "2024-W01"

    def test_iso_week_overlap_with_previous_year(self):
        # 31 декабря 2023 (воскресенье) попадает в ISO неделю 52 года 2023.
        # (ISO год отстаёт от Gregorian, когда конец года в начале недели.)
        _, w = user_calendar_keys(datetime(2023, 12, 31, 23, 59))
        assert w == "2023-W52"

    def test_time_ignored(self):
        """Часы/минуты не влияют на local_date или week_iso (только дата)."""
        d1, w1 = user_calendar_keys(datetime(2026, 5, 18, 0, 0))
        d2, w2 = user_calendar_keys(datetime(2026, 5, 18, 23, 59))
        assert d1 == d2
        assert w1 == w2
