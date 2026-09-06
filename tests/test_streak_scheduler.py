"""
streak_scheduler — окно обработки стриков 23:58–23:59 в локальном времени зоны.

Регрессия: бэкап БД вызывался ВНУТРИ обхода часовых поясов. VACUUM INTO
копирует базу целиком и на большой БД идёт дольше двухминутного окна, а
now_local пересчитывается для каждой зоны заново. Зоны с одинаковым
смещением попадают в окно одновременно (только UTC+3 делят 25 зон), и те,
до кого очередь доходила после бэкапа, видели уже 00:0x и теряли сутки
обработки стриков.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta

import pytest
import pytz

os.environ.setdefault("BOT_TOKEN", "test-token-for-pytest-imports")

import tasks  # noqa: E402


class _Clock:
    """Управляемые часы: «долгие» операции двигают время вперёд."""

    def __init__(self, start_utc: datetime):
        self.now_utc = pytz.UTC.localize(start_utc)

    def advance(self, seconds: float) -> None:
        self.now_utc += timedelta(seconds=seconds)


class _FakeStreakService:
    def __init__(self, clock: _Clock, seconds_per_tz: float = 1.0):
        self.clock = clock
        self.seconds_per_tz = seconds_per_tz
        self.processed: list[str] = []

    async def process_users_in_timezone(self, tz_name: str) -> None:
        self.processed.append(tz_name)
        self.clock.advance(self.seconds_per_tz)


class _FakeBackupService:
    def __init__(self, clock: _Clock, seconds: float):
        self.clock = clock
        self.seconds = seconds
        self.calls = 0

    async def maybe_backup_for_today(self) -> None:
        self.calls += 1
        self.clock.advance(self.seconds)


class _FakeUserRepo:
    def __init__(self, tzs: list[str]):
        self._tzs = tzs

    async def get_distinct_timezones(self) -> list[str]:
        return self._tzs


async def _run_once(monkeypatch, clock, streak, repo, backup):
    """Крутит планировщик, пока он не отработает первую итерацию."""

    class _FakeDatetime:
        @staticmethod
        def now(tz=None):
            if tz is None:
                return clock.now_utc.replace(tzinfo=None)
            return clock.now_utc.astimezone(tz)

    monkeypatch.setattr(tasks, "datetime", _FakeDatetime)
    task = asyncio.create_task(tasks.streak_scheduler(streak, repo, backup))
    await asyncio.sleep(0.2)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# Все три зоны — UTC+3, значит в окно 23:58 они попадают одновременно.
SAME_OFFSET_TZS = ["Europe/Moscow", "Europe/Volgograd", "Africa/Nairobi"]
# 20:58 UTC == 23:58 в UTC+3
WINDOW_START_UTC = datetime(2026, 5, 18, 20, 58, 0)


class TestBackupDoesNotEatTheWindow:
    async def test_slow_backup_does_not_skip_other_timezones(self, monkeypatch):
        """Бэкап в 2.5 минуты не должен отнимать окно у остальных зон."""
        clock = _Clock(WINDOW_START_UTC)
        streak = _FakeStreakService(clock)
        backup = _FakeBackupService(clock, seconds=150)

        await _run_once(
            monkeypatch, clock, streak, _FakeUserRepo(SAME_OFFSET_TZS), backup
        )

        assert streak.processed == SAME_OFFSET_TZS
        assert backup.calls == 1  # один файл на глобальный день

    async def test_backup_skipped_when_no_timezone_hit_the_window(self, monkeypatch):
        """Вне окна бэкап не запускается — он привязан к обработке стриков."""
        clock = _Clock(datetime(2026, 5, 18, 12, 0, 0))  # 15:00 в UTC+3
        streak = _FakeStreakService(clock)
        backup = _FakeBackupService(clock, seconds=1)

        await _run_once(
            monkeypatch, clock, streak, _FakeUserRepo(SAME_OFFSET_TZS), backup
        )

        assert streak.processed == []
        assert backup.calls == 0

    async def test_works_without_backup_service(self, monkeypatch):
        """backup_service опционален — обработка стриков от него не зависит."""
        clock = _Clock(WINDOW_START_UTC)
        streak = _FakeStreakService(clock)

        await _run_once(
            monkeypatch, clock, streak, _FakeUserRepo(SAME_OFFSET_TZS), None
        )

        assert streak.processed == SAME_OFFSET_TZS

    async def test_unknown_timezone_does_not_block_the_rest(self, monkeypatch):
        clock = _Clock(WINDOW_START_UTC)
        streak = _FakeStreakService(clock)
        backup = _FakeBackupService(clock, seconds=1)
        tzs = ["Europe/Moscow", "Not/AZone", "Africa/Nairobi"]

        await _run_once(monkeypatch, clock, streak, _FakeUserRepo(tzs), backup)

        assert streak.processed == ["Europe/Moscow", "Africa/Nairobi"]
