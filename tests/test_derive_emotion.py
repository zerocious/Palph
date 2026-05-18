"""
Тесты services.derive_emotion — pure function, выводит эмоцию питомца
из текущего состояния пользователя в момент рендера (см. v0.7 TODO #16).

Priority order (highest first):
    1. "studying" — активный учебный таймер
    2. "excited"  — level-up или ачивка ≤ 5 минут назад
    3. "sad"      — пользователь сегодня ещё не учился
    4. "sleepy"   — локальное время в окне [22:00, 06:00)
    5. "happy"    — дефолт
"""
from datetime import datetime

import pytest

from services import derive_emotion


DAY = datetime(2026, 5, 18, 14, 0)   # 14:00 — точно не sleepy
NIGHT = datetime(2026, 5, 18, 23, 0)  # 23:00 — sleepy hours


class TestPriorityOrder:
    def test_studying_wins_over_everything(self):
        """is_studying — наивысший приоритет, даже если ВСЁ остальное True."""
        emotion = derive_emotion(
            is_studying=True,
            recently_excited=True,
            has_studied_today=False,
            now_local=NIGHT,
        )
        assert emotion == "studying"

    def test_excited_wins_over_sad_and_sleepy(self):
        emotion = derive_emotion(
            is_studying=False,
            recently_excited=True,
            has_studied_today=False,
            now_local=NIGHT,
        )
        assert emotion == "excited"

    def test_sad_wins_over_sleepy(self):
        """Если не учился сегодня — sad, даже ночью."""
        emotion = derive_emotion(
            is_studying=False,
            recently_excited=False,
            has_studied_today=False,
            now_local=NIGHT,
        )
        assert emotion == "sad"

    def test_sleepy_when_only_time_matches(self):
        emotion = derive_emotion(
            is_studying=False,
            recently_excited=False,
            has_studied_today=True,
            now_local=NIGHT,
        )
        assert emotion == "sleepy"

    def test_happy_default(self):
        emotion = derive_emotion(
            is_studying=False,
            recently_excited=False,
            has_studied_today=True,
            now_local=DAY,
        )
        assert emotion == "happy"


class TestSleepyBoundaries:
    """
    Окно sleepy = [22:00, 06:00) — 22:00 включительно, 06:00 исключительно.
    Учится сегодня, нет excited — изолируем тест от других веток.
    """
    BASE_KWARGS = dict(
        is_studying=False,
        recently_excited=False,
        has_studied_today=True,
    )

    @pytest.mark.parametrize("hour,expected", [
        (21, "happy"),    # 21:59 → не sleepy
        (22, "sleepy"),   # 22:00 → sleepy включается
        (23, "sleepy"),
        (0,  "sleepy"),   # полночь
        (3,  "sleepy"),
        (5,  "sleepy"),   # 05:59 → ещё sleepy
        (6,  "happy"),    # 06:00 → выходим из sleepy
        (7,  "happy"),
        (12, "happy"),
        (18, "happy"),
    ])
    def test_hour_boundary(self, hour, expected):
        now = datetime(2026, 5, 18, hour, 0)
        emotion = derive_emotion(now_local=now, **self.BASE_KWARGS)
        assert emotion == expected, f"hour={hour}: expected {expected}, got {emotion}"
