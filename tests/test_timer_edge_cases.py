"""Timer edge-case regression tests (standard + custom flows)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

import bot
from fsm_storage import SQLiteStorage, _dumps
from repository import SessionRepository, UserRepository
from services import AchievementService, StudyService


@pytest.fixture(autouse=True)
def _reset_active_timers():
    bot.active_timers.clear()
    bot.pending_timer_sessions.clear()
    bot._timer_completion_locks.clear()
    yield
    bot.active_timers.clear()
    bot.pending_timer_sessions.clear()
    bot._timer_completion_locks.clear()


@pytest_asyncio.fixture
async def study_stack(db, achievements_catalog):
    user_repo = UserRepository(db)
    session_repo = SessionRepository(db)
    ach_service = AchievementService(user_repo, achievements_catalog)
    study_service = StudyService(user_repo, session_repo, ach_service)
    return user_repo, session_repo, study_service


@pytest_asyncio.fixture
async def fsm_state(db):
    storage = SQLiteStorage(db)
    key = StorageKey(bot_id=1, chat_id=100, user_id=42, thread_id=0)
    return FSMContext(storage=storage, key=key)


class TestNormalizeTimerDuration:
    def test_valid_range(self):
        assert bot._normalize_timer_duration(25) == 25
        assert bot._normalize_timer_duration("60") == 60

    def test_rejects_invalid(self):
        assert bot._normalize_timer_duration(0) is None
        assert bot._normalize_timer_duration(-5) is None
        assert bot._normalize_timer_duration(121) is None
        assert bot._normalize_timer_duration(True) is None
        assert bot._normalize_timer_duration("abc") is None


class TestNormalizeTimerDuration:
    def test_valid_range(self):
        assert bot._normalize_timer_duration(25) == 25
        assert bot._normalize_timer_duration("60") == 60

    def test_rejects_invalid(self):
        assert bot._normalize_timer_duration(0) is None
        assert bot._normalize_timer_duration(-5) is None
        assert bot._normalize_timer_duration(121) is None
        assert bot._normalize_timer_duration(True) is None
        assert bot._normalize_timer_duration("abc") is None


class TestParseCustomTimerDuration:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("25", 25),
            ("5", 5),
            ("120", 120),
            ("05", 5),
            ("  30  ", 30),
            ("２５", 25),  # fullwidth digits (NFKC)
        ],
    )
    def test_accepts_valid_input(self, text, expected):
        duration, error = bot._parse_custom_timer_duration(text)
        assert duration == expected
        assert error is None

    @pytest.mark.parametrize(
        "text",
        [
            None,
            "",
            "   ",
            "abc",
            "25🔥",
            "10 minutes",
            "10 20",
            "10abc20",
            "25.5",
            "25,5",
            "-5",
            "+25",
            "4",
            "121",
            "999999",
            "'; DROP TABLE users;--",
            "true",
        ],
    )
    def test_rejects_invalid_or_out_of_range(self, text):
        duration, error = bot._parse_custom_timer_duration(text)
        assert duration is None
        assert error in ("invalid", "range")

    def test_invalid_vs_range_messages(self):
        assert bot._parse_custom_timer_duration("abc") == (None, "invalid")
        assert bot._parse_custom_timer_duration("4") == (None, "range")
        assert bot._parse_custom_timer_duration("999") == (None, "range")
        assert bot._parse_custom_timer_duration(None) == (None, "invalid")
        assert bot._parse_custom_timer_duration("007") == (7, None)


def _duration_message(text: str | None, user_id: int = 42, chat_id: int = 100) -> MagicMock:
    message = MagicMock()
    message.text = text
    message.from_user.id = user_id
    message.from_user.username = "tester"
    message.chat.id = chat_id
    message.answer = AsyncMock()
    return message


@pytest.mark.asyncio
async def test_process_duration_valid_starts_timer(fsm_state, monkeypatch):
    uid = 42
    start_timer = MagicMock()
    monkeypatch.setattr(bot, "loc", AsyncMock(return_value="ru"))
    monkeypatch.setattr(bot, "user_repo", AsyncMock(user_exists=AsyncMock(return_value=True)))
    monkeypatch.setattr(bot, "event_repo", AsyncMock(log=AsyncMock()))
    monkeypatch.setattr(bot, "start_timer", start_timer)
    monkeypatch.setattr(bot, "t", lambda key, locale, **kw: key)

    await fsm_state.set_state(bot.TimerStates.waiting_for_duration)
    message = _duration_message("25")

    await bot.process_duration(message, fsm_state)

    assert await fsm_state.get_state() == bot.TimerStates.active.state
    data = await fsm_state.get_data()
    assert data["duration"] == 25
    message.answer.assert_awaited_once()
    start_timer.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text,expected_key",
    [
        ("abc", "timer.custom_invalid"),
        ("4", "timer.custom_range"),
        ("200", "timer.custom_range"),
        (None, "timer.custom_invalid"),
    ],
)
async def test_process_duration_invalid_stays_waiting(fsm_state, monkeypatch, text, expected_key):
    monkeypatch.setattr(bot, "loc", AsyncMock(return_value="en"))
    monkeypatch.setattr(bot, "t", lambda key, locale, **kw: key)
    monkeypatch.setattr(bot, "start_timer", MagicMock())

    await fsm_state.set_state(bot.TimerStates.waiting_for_duration)
    message = _duration_message(text)

    await bot.process_duration(message, fsm_state)

    assert await fsm_state.get_state() == bot.TimerStates.waiting_for_duration.state
    message.answer.assert_awaited_once_with(expected_key)


@pytest.mark.asyncio
async def test_process_duration_retry_after_invalid(fsm_state, monkeypatch):
    monkeypatch.setattr(bot, "loc", AsyncMock(return_value="ru"))
    monkeypatch.setattr(bot, "user_repo", AsyncMock(user_exists=AsyncMock(return_value=True)))
    monkeypatch.setattr(bot, "event_repo", AsyncMock(log=AsyncMock()))
    monkeypatch.setattr(bot, "start_timer", MagicMock())
    monkeypatch.setattr(bot, "t", lambda key, locale, **kw: key)

    await fsm_state.set_state(bot.TimerStates.waiting_for_duration)

    bad = _duration_message("nope")
    await bot.process_duration(bad, fsm_state)
    assert await fsm_state.get_state() == bot.TimerStates.waiting_for_duration.state

    good = _duration_message("25")
    await bot.process_duration(good, fsm_state)
    assert await fsm_state.get_state() == bot.TimerStates.active.state


@pytest.mark.asyncio
async def test_cancel_custom_timer_duration_clears_state(fsm_state, monkeypatch):
    monkeypatch.setattr(bot, "loc", AsyncMock(return_value="ru"))
    monkeypatch.setattr(bot, "t", lambda key, locale, **kw: key)

    await fsm_state.set_state(bot.TimerStates.waiting_for_duration)
    message = _duration_message("/cancel")

    await bot.cancel_custom_timer_duration(message, fsm_state)

    assert await fsm_state.get_state() is None
    message.answer.assert_awaited_once()
    assert message.answer.await_args.args[0] == "common.cancelled"


@pytest.mark.asyncio
async def test_cmd_stop_clears_waiting_for_duration(fsm_state, monkeypatch):
    monkeypatch.setattr(bot, "loc", AsyncMock(return_value="ru"))
    monkeypatch.setattr(bot, "t", lambda key, locale, **kw: key)
    monkeypatch.setattr(bot, "stop_active_timer", AsyncMock(return_value=False))

    await fsm_state.set_state(bot.TimerStates.waiting_for_duration)
    message = _duration_message("/stop")

    await bot.cmd_stop(message, fsm_state)

    assert await fsm_state.get_state() is None
    message.answer.assert_awaited_once()
    assert message.answer.await_args.args[0] == "timer.no_active"


@pytest.mark.asyncio
async def test_back_main_clears_waiting_for_duration(fsm_state, monkeypatch):
    monkeypatch.setattr(bot, "loc", AsyncMock(return_value="ru"))
    monkeypatch.setattr(bot, "t", lambda key, locale, **kw: key)

    await fsm_state.set_state(bot.TimerStates.waiting_for_duration)
    message = _duration_message("back")

    await bot.handle_back_to_main(message, fsm_state)

    assert await fsm_state.get_state() is None


class TestActiveTimerSlot:
    def test_release_only_own_task(self):
        async def _runner():
            old = asyncio.create_task(asyncio.sleep(60), name="old")
            new = asyncio.create_task(asyncio.sleep(60), name="new")
            bot.active_timers[7] = new
            bot._release_active_timer_slot(7, old)
            assert bot.active_timers[7] is new
            bot._release_active_timer_slot(7, new)
            assert 7 not in bot.active_timers
            old.cancel()
            new.cancel()

        asyncio.run(_runner())


@pytest.mark.asyncio
async def test_claim_active_timer_is_exclusive(fsm_state):
    await fsm_state.set_state(bot.TimerStates.active)
    await fsm_state.update_data(duration=25, start_time=datetime.now())

    first = await bot._claim_active_timer(fsm_state, 42)
    second = await bot._claim_active_timer(fsm_state, 42)

    assert first is not None
    assert first["duration"] == 25
    assert second is None
    assert await fsm_state.get_state() is None


@pytest.mark.asyncio
async def test_run_timer_invalid_start_time_skips_completion(study_stack, fsm_state, monkeypatch):
    user_repo, session_repo, study_service = study_stack
    uid = 42
    await user_repo.create_user(uid)

    monkeypatch.setattr(bot, "user_repo", user_repo)
    monkeypatch.setattr(bot, "session_repo", session_repo)
    monkeypatch.setattr(bot, "study_service", study_service)
    monkeypatch.setattr(bot, "event_repo", AsyncMock(log=AsyncMock()))
    monkeypatch.setattr(bot, "bot", AsyncMock())

    await fsm_state.set_state(bot.TimerStates.active)
    await fsm_state.update_data(duration=25, start_time="not-a-datetime")

    await bot.run_timer_task(100, fsm_state, uid, 25)

    user = await user_repo.get_user(uid)
    assert user["total_sessions"] == 0
    assert await fsm_state.get_state() is None


@pytest.mark.asyncio
async def test_run_timer_natural_completion(study_stack, fsm_state, monkeypatch):
    user_repo, session_repo, study_service = study_stack
    uid = 42
    await user_repo.create_user(uid)

    send_message = AsyncMock()
    monkeypatch.setattr(bot, "user_repo", user_repo)
    monkeypatch.setattr(bot, "session_repo", session_repo)
    monkeypatch.setattr(bot, "study_service", study_service)
    monkeypatch.setattr(bot, "event_repo", AsyncMock(log=AsyncMock()))
    monkeypatch.setattr(bot, "bot", AsyncMock(send_message=send_message))
    monkeypatch.setattr(bot, "loc", AsyncMock(return_value="ru"))
    monkeypatch.setattr(bot, "send_achievement_notification", AsyncMock())
    monkeypatch.setattr(bot, "send_rating_prompt", AsyncMock())

    await fsm_state.set_state(bot.TimerStates.active)
    await fsm_state.update_data(
        duration=25,
        start_time=datetime.now() - timedelta(minutes=25, seconds=1),
    )

    await bot.run_timer_task(100, fsm_state, uid, 25)

    user = await user_repo.get_user(uid)
    assert user["total_sessions"] == 1
    assert user["total_coins"] >= 25
    send_message.assert_awaited()
    assert await fsm_state.get_state() is None


@pytest.mark.asyncio
async def test_stop_race_with_natural_finish(study_stack, fsm_state, monkeypatch):
    """Stop and natural finish concurrently must not double-award coins."""
    user_repo, session_repo, study_service = study_stack
    uid = 42
    await user_repo.create_user(uid)

    monkeypatch.setattr(bot, "user_repo", user_repo)
    monkeypatch.setattr(bot, "session_repo", session_repo)
    monkeypatch.setattr(bot, "study_service", study_service)
    monkeypatch.setattr(bot, "event_repo", AsyncMock(log=AsyncMock()))
    monkeypatch.setattr(bot, "bot", AsyncMock(send_message=AsyncMock()))
    monkeypatch.setattr(bot, "loc", AsyncMock(return_value="ru"))
    monkeypatch.setattr(bot, "send_achievement_notification", AsyncMock())
    monkeypatch.setattr(bot, "send_rating_prompt", AsyncMock())

    await fsm_state.set_state(bot.TimerStates.active)
    await fsm_state.update_data(
        duration=25,
        start_time=datetime.now() - timedelta(minutes=25, seconds=1),
    )

    message = MagicMock()
    message.from_user.id = uid
    message.chat.id = 100
    message.answer = AsyncMock()

    natural = asyncio.create_task(bot.run_timer_task(100, fsm_state, uid, 25))
    stop = asyncio.create_task(bot.stop_active_timer(message, fsm_state))
    await asyncio.gather(natural, stop)

    user = await user_repo.get_user(uid)
    assert user["total_sessions"] == 1
    assert user["total_coins"] >= 25


@pytest.mark.asyncio
async def test_ensure_timer_task_running_restarts_orphan(study_stack, fsm_state, monkeypatch):
    user_repo, _, study_service = study_stack
    uid = 42
    await user_repo.create_user(uid)

    monkeypatch.setattr(bot, "user_repo", user_repo)
    monkeypatch.setattr(bot, "study_service", study_service)
    monkeypatch.setattr(bot, "event_repo", AsyncMock(log=AsyncMock()))
    monkeypatch.setattr(bot, "bot", AsyncMock())
    monkeypatch.setattr(bot, "loc", AsyncMock(return_value="ru"))
    monkeypatch.setattr(bot, "send_achievement_notification", AsyncMock())
    monkeypatch.setattr(bot, "send_rating_prompt", AsyncMock())

    await fsm_state.set_state(bot.TimerStates.active)
    await fsm_state.update_data(
        duration=25,
        start_time=datetime.now() - timedelta(minutes=24, seconds=50),
    )

    bot._ensure_timer_task_running(100, fsm_state, uid, 25)
    assert uid in bot.active_timers
    task = bot.active_timers[uid]
    assert not task.done()

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_reconcile_clears_waiting_for_duration(db, monkeypatch):
    user_repo = UserRepository(db)
    await user_repo.create_user(42)
    key = "1:100:42:0"
    await db.execute(
        "INSERT INTO fsm_storage (key, state, data) VALUES (?, ?, ?)",
        (key, bot.TimerStates.waiting_for_duration.state, "{}"),
    )
    await db.commit()

    monkeypatch.setattr(bot, "db", db)
    monkeypatch.setattr(bot, "user_repo", user_repo)
    monkeypatch.setattr(bot, "study_service", AsyncMock())
    monkeypatch.setattr(bot, "event_repo", AsyncMock(log=AsyncMock()))
    monkeypatch.setattr(bot, "bot", AsyncMock())
    monkeypatch.setattr(bot, "loc", AsyncMock(return_value="ru"))
    monkeypatch.setattr(bot, "dp", MagicMock(storage=SQLiteStorage(db)))

    await bot.reconcile_stale_timers()

    async with db.execute("SELECT COUNT(*) AS n FROM fsm_storage WHERE key = ?", (key,)) as c:
        row = await c.fetchone()
    assert row["n"] == 0


@pytest.mark.asyncio
async def test_reconcile_expired_timer_completes(study_stack, db, monkeypatch):
    user_repo, _, study_service = study_stack
    uid = 42
    await user_repo.create_user(uid)

    key = "1:100:42:0"
    data = _dumps({
        "duration": 25,
        "start_time": datetime.now() - timedelta(minutes=30),
    })
    await db.execute(
        "INSERT INTO fsm_storage (key, state, data) VALUES (?, ?, ?)",
        (key, bot.TimerStates.active.state, data),
    )
    await db.commit()

    send_message = AsyncMock()
    monkeypatch.setattr(bot, "db", db)
    monkeypatch.setattr(bot, "user_repo", user_repo)
    monkeypatch.setattr(bot, "study_service", study_service)
    monkeypatch.setattr(bot, "event_repo", AsyncMock(log=AsyncMock()))
    monkeypatch.setattr(bot, "bot", AsyncMock(send_message=send_message))
    monkeypatch.setattr(bot, "loc", AsyncMock(return_value="ru"))
    monkeypatch.setattr(bot, "send_achievement_notification", AsyncMock())
    monkeypatch.setattr(bot, "dp", MagicMock(storage=SQLiteStorage(db)))

    await bot.reconcile_stale_timers()

    user = await user_repo.get_user(uid)
    assert user["total_sessions"] == 1
    send_message.assert_awaited()
    async with db.execute("SELECT COUNT(*) AS n FROM fsm_storage WHERE key = ?", (key,)) as c:
        row = await c.fetchone()
    assert row["n"] == 0


@pytest.mark.asyncio
async def test_reconcile_invalid_duration_is_broken(db, monkeypatch):
    key = "1:100:42:0"
    data = _dumps({"duration": 0, "start_time": datetime.now()})
    await db.execute(
        "INSERT INTO fsm_storage (key, state, data) VALUES (?, ?, ?)",
        (key, bot.TimerStates.active.state, data),
    )
    await db.commit()

    monkeypatch.setattr(bot, "db", db)
    monkeypatch.setattr(bot, "study_service", AsyncMock())
    monkeypatch.setattr(bot, "event_repo", AsyncMock())
    monkeypatch.setattr(bot, "bot", AsyncMock())
    monkeypatch.setattr(bot, "dp", MagicMock())

    await bot.reconcile_stale_timers()

    async with db.execute("SELECT COUNT(*) AS n FROM fsm_storage WHERE key = ?", (key,)) as c:
        row = await c.fetchone()
    assert row["n"] == 0


@pytest.mark.asyncio
async def test_quiz_menu_preserves_active_timer(study_stack, fsm_state, monkeypatch):
    """Opening Preparation while timer runs should detach to background, not stop session."""
    user_repo, _, _ = study_stack
    uid = 42
    await user_repo.create_user(uid)

    start_time = datetime.now() - timedelta(minutes=10)
    await fsm_state.set_state(bot.TimerStates.active)
    await fsm_state.update_data(duration=25, start_time=start_time)
    bot.active_timers[uid] = MagicMock(done=MagicMock(return_value=False))

    stop_timer = AsyncMock(return_value=True)
    monkeypatch.setattr(bot, "loc", AsyncMock(return_value="ru"))
    monkeypatch.setattr(bot, "available_subjects", AsyncMock(return_value=[("math", "Math")]))
    monkeypatch.setattr(bot, "get_subject_keyboard", AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr(bot, "stop_active_timer", stop_timer)
    monkeypatch.setattr(bot, "_cancel_timer_task", AsyncMock())
    monkeypatch.setattr(bot, "t", lambda key, locale, **kw: key)

    message = MagicMock()
    message.from_user.id = uid
    message.chat.id = uid
    message.answer = AsyncMock()

    await bot.handle_quiz_menu(message, fsm_state)

    stop_timer.assert_not_awaited()
    assert uid in bot.pending_timer_sessions
    assert await fsm_state.get_state() == bot.QuizStates.choosing_subject.state


@pytest.mark.asyncio
async def test_claim_timer_session_from_pending(study_stack, fsm_state):
    """Natural completion after prep detach uses pending_timer_sessions."""
    uid = 99
    start_time = datetime.now() - timedelta(minutes=5)
    bot.pending_timer_sessions[uid] = {
        "duration": 25,
        "start_time": start_time,
        "chat_id": 100,
    }
    await fsm_state.set_state(bot.QuizStates.choosing_subject)

    claimed = await bot._claim_timer_session(fsm_state, uid)
    assert claimed is not None
    assert claimed["start_time"] == start_time
    assert uid not in bot.pending_timer_sessions

    second = await bot._claim_timer_session(fsm_state, uid)
    assert second is None
