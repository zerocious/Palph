"""TZ_PRESETS must use valid, unique IANA names (pytz)."""
import pytz
import pytest

from bot import TZ_IDS, TZ_PRESETS


def test_tz_presets_ids_are_unique():
    ids = [tz for tz, _ in TZ_PRESETS]
    assert len(ids) == len(set(ids)), f"duplicate TZ ids: {ids}"


def test_tz_presets_ids_are_valid_iana():
    for tz_id, _ in TZ_PRESETS:
        assert tz_id in pytz.all_timezones_set, f"invalid IANA timezone: {tz_id}"


def test_tz_ids_matches_presets():
    assert TZ_IDS == {tz for tz, _ in TZ_PRESETS}
