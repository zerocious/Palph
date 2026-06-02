"""Tests for services.get_pet_time_period — local-hour buckets."""
from datetime import datetime

import pytest

from services import get_pet_time_period


class TestPetTimePeriodBuckets:
    @pytest.mark.parametrize("hour,expected", [
        (5, "night"),
        (6, "morning"),
        (11, "morning"),
        (12, "day"),
        (16, "day"),
        (17, "evening"),
        (21, "evening"),
        (22, "night"),
        (23, "night"),
        (0, "night"),
    ])
    def test_hour_boundaries(self, hour, expected):
        now = datetime(2026, 6, 2, hour, 30)
        assert get_pet_time_period(now) == expected
