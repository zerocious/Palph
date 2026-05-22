"""Regression tests for settings persistence, streak toggles, and related fixes."""
import asyncio
import os

import pytest
import pytest_asyncio

from repository import LeaderboardRepository, UserRepository
from services import AchievementService, StreakService, StudyService

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


@pytest_asyncio.fixture
async def study_service(user_repo, session_repo, achievements_catalog):
    ach_service = AchievementService(user_repo, achievements_catalog)
    return StudyService(user_repo, session_repo, ach_service)


@pytest_asyncio.fixture
async def lb_repo(db):
    return LeaderboardRepository(db)


@pytest.mark.asyncio
async def test_notification_settings_upsert_without_row(db):
    repo = UserRepository(db)
    await db.execute("INSERT INTO users (user_id) VALUES (777)")
    await db.commit()

    assert await repo.get_notification_settings(777) is None

    await repo.update_notification_settings(777, {"morning_enabled": 0})
    row = await repo.get_notification_settings(777)
    assert row is not None
    assert row["morning_enabled"] == 0


@pytest.mark.asyncio
async def test_streak_disabled_skips_increment(user_repo):
    bot = None
    svc = StreakService(user_repo, bot=bot)
    uid = 880
    await user_repo.create_user(uid)
    await user_repo.set_streak(uid, 3)
    await user_repo.set_has_studied_today(uid, True)
    await user_repo.update_notification_settings(uid, {"streak_enabled": 0})

    await svc.process_users_in_timezone("Europe/Moscow")

    user = await user_repo.get_user(uid)
    assert user["current_streak"] == 3
    assert user["has_studied_today"] == 0


@pytest.mark.asyncio
async def test_streak_idempotency_same_day(user_repo):
    svc = StreakService(user_repo, bot=None)
    uid = 881
    await user_repo.create_user(uid)
    await user_repo.set_streak(uid, 2)
    await user_repo.set_has_studied_today(uid, True)

    await svc.process_users_in_timezone("Europe/Moscow")
    await svc.process_users_in_timezone("Europe/Moscow")

    user = await user_repo.get_user(uid)
    assert user["current_streak"] == 3


@pytest.mark.asyncio
async def test_complete_session_rejects_invalid_duration(study_service, user_repo):
    uid = 882
    await user_repo.create_user(uid)
    earned, bonus, session_id = await study_service.complete_session(uid, 0)
    assert earned == []
    assert bonus == 0
    assert session_id == 0
    user = await user_repo.get_user(uid)
    assert user["total_sessions"] == 0


@pytest.mark.asyncio
async def test_achievements_disabled_skips_award(study_service, user_repo):
    uid = 883
    await user_repo.create_user(uid)
    await user_repo.update_notification_settings(uid, {"achievements_enabled": 0})
    earned, bonus, _ = await study_service.complete_session(uid, 30)
    assert earned == []
    assert bonus == 0


@pytest.mark.asyncio
async def test_consume_freeze_only_one_row(lb_repo, user_repo, created_user, db):
    await db.execute(
        "INSERT INTO streak_freezes "
        "(user_id, granted_at, streak_at_grant, cost_paid) "
        "VALUES (?, '2026-01-01 10:00:00', 5, 500)",
        (created_user,),
    )
    await db.execute(
        "INSERT INTO streak_freezes "
        "(user_id, granted_at, streak_at_grant, cost_paid) "
        "VALUES (?, '2026-01-02 10:00:00', 5, 500)",
        (created_user,),
    )
    await db.commit()
    consumed = await lb_repo.consume_freeze_if_active(created_user, "2026-05-19")
    assert consumed is True
    async with db.execute(
        "SELECT COUNT(*) AS n FROM streak_freezes "
        "WHERE user_id=? AND consumed_for_date IS NULL",
        (created_user,),
    ) as c:
        row = await c.fetchone()
    assert row["n"] == 1


@pytest.mark.asyncio
async def test_flashcard_source_cycle_no_deadlock(user_repo):
    """cycle_flashcard_source uses db.lock once — must not hang."""
    from bot import NotificationSettings

    uid = 884
    await user_repo.create_user(uid)
    ns = NotificationSettings(uid, user_repo)

    async def run():
        return await ns.cycle_flashcard_source()

    label, source = await asyncio.wait_for(run(), timeout=2.0)
    assert source in ("official", "own", "mix")
    assert label
