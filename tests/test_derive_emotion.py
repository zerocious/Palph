"""
Тесты services.derive_emotion — pure function, выводит эмоцию питомца
из текущего состояния пользователя в момент рендера (см. v0.7 TODO #16).

Priority order (highest first):
    1. "joy"     — активный таймер или level-up/ачивка ≤ 5 минут назад
    2. "sad"     — пользователь сегодня ещё не учился
    3. "neutral" — дефолт (в т.ч. ночные часы)
"""
from datetime import datetime

import pytest

from services import derive_emotion


DAY = datetime(2026, 5, 18, 14, 0)
NIGHT = datetime(2026, 5, 18, 23, 0)


class TestPriorityOrder:
    def test_studying_maps_to_joy(self):
        """is_studying → joy, даже если всё остальное True."""
        emotion = derive_emotion(
            is_studying=True,
            recently_excited=True,
            has_studied_today=False,
            now_local=NIGHT,
        )
        assert emotion == "joy"

    def test_excited_maps_to_joy_over_sad(self):
        emotion = derive_emotion(
            is_studying=False,
            recently_excited=True,
            has_studied_today=False,
            now_local=NIGHT,
        )
        assert emotion == "joy"

    def test_sad_when_not_studied_today(self):
        emotion = derive_emotion(
            is_studying=False,
            recently_excited=False,
            has_studied_today=False,
            now_local=NIGHT,
        )
        assert emotion == "sad"

    def test_neutral_when_studied_today(self):
        emotion = derive_emotion(
            is_studying=False,
            recently_excited=False,
            has_studied_today=True,
            now_local=NIGHT,
        )
        assert emotion == "neutral"

    def test_neutral_default_daytime(self):
        emotion = derive_emotion(
            is_studying=False,
            recently_excited=False,
            has_studied_today=True,
            now_local=DAY,
        )
        assert emotion == "neutral"


class TestNightHoursStayNeutral:
    """
    Ночные часы больше не дают отдельную эмоцию — остаётся neutral,
    если пользователь сегодня учился.
    """
    BASE_KWARGS = dict(
        is_studying=False,
        recently_excited=False,
        has_studied_today=True,
    )

    @pytest.mark.parametrize("hour", [21, 22, 23, 0, 3, 5, 6, 7, 12, 18])
    def test_all_hours_neutral_when_studied(self, hour):
        now = datetime(2026, 5, 18, hour, 0)
        emotion = derive_emotion(now_local=now, **self.BASE_KWARGS)
        assert emotion == "neutral", f"hour={hour}: expected neutral, got {emotion}"
