"""
Листание «Все советы» не должно давать прогресс геймификации.

Стрелки ◀️/▶️ шли через тот же _on_tip_viewed, что и «Ещё совет», и каждый
тап увеличивал total_views — десять тапов выдавали ачивку 10_tips_read с
наградой. Баг был записан в TODO.md как «Known issue (defer)».
"""
import json
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

import bot
from repository import EventRepository, TipsRepository
from services import AchievementService


@pytest_asyncio.fixture
async def env(db, user_repo, created_user, achievements_catalog, monkeypatch):
    tips_repo = TipsRepository(db)
    events = EventRepository(db)
    ach = AchievementService(user_repo, achievements_catalog)
    notify = AsyncMock()
    monkeypatch.setattr(bot, "tips_repo", tips_repo)
    monkeypatch.setattr(bot, "user_repo", user_repo)
    monkeypatch.setattr(bot, "event_repo", events)
    monkeypatch.setattr(bot, "ach_service", ach)
    monkeypatch.setattr(bot, "send_achievement_notification", notify)
    return type("Env", (), dict(
        uid=created_user, db=db, tips=tips_repo, user_repo=user_repo, notify=notify,
    ))


async def _stats(env):
    async with env.db.execute(
        "SELECT total_views FROM user_tips_stats WHERE user_id=?", (env.uid,)
    ) as c:
        row = await c.fetchone()
    return row["total_views"] if row else 0


async def _achievement_done(env) -> bool:
    async with env.db.execute(
        "SELECT completed FROM user_achievements "
        "WHERE user_id=? AND achievement_id='10_tips_read'", (env.uid,)
    ) as c:
        row = await c.fetchone()
    return bool(row and row["completed"])


async def _events(env):
    async with env.db.execute(
        "SELECT properties FROM events WHERE user_id=? AND event_name='tip_viewed' "
        "ORDER BY id", (env.uid,)
    ) as c:
        return [json.loads(r["properties"]) for r in await c.fetchall()]


def _tip_ids(n):
    return [t["id"] for t in bot.TIP_CATEGORIES["tm"]["tips"][:n]]


class TestPagingDoesNotFarm:
    async def test_paging_does_not_award_achievement(self, env):
        for tip_id in _tip_ids(10) * 2:
            await bot._on_tip_viewed(env.uid, "tm", tip_id, count_progress=False)
        assert not await _achievement_done(env)
        env.notify.assert_not_called()

    async def test_paging_does_not_move_view_counter(self, env):
        for tip_id in _tip_ids(5):
            await bot._on_tip_viewed(env.uid, "tm", tip_id, count_progress=False)
        assert await _stats(env) == 0

    async def test_paging_grants_no_daily_coin(self, env):
        before = (await env.user_repo.get_user(env.uid))["total_coins"]
        await bot._on_tip_viewed(env.uid, "tm", _tip_ids(1)[0], count_progress=False)
        assert (await env.user_repo.get_user(env.uid))["total_coins"] == before

    async def test_paging_returns_no_gamification_suffix(self, env):
        suffix = await bot._on_tip_viewed(env.uid, "tm", _tip_ids(1)[0], count_progress=False)
        assert suffix == ""


class TestReadingStillCounts:
    async def test_reading_counts_and_rewards(self, env):
        """«Ещё совет» — по-прежнему настоящее чтение: монета + счётчик."""
        before = (await env.user_repo.get_user(env.uid))["total_coins"]
        await bot._on_tip_viewed(env.uid, "tm", _tip_ids(1)[0])
        assert await _stats(env) == 1
        assert (await env.user_repo.get_user(env.uid))["total_coins"] > before

    async def test_ten_reads_unlock_the_achievement(self, env):
        for tip_id in _tip_ids(10):
            await bot._on_tip_viewed(env.uid, "tm", tip_id)
        assert await _achievement_done(env)
        env.notify.assert_called_once()

    async def test_paging_between_reads_does_not_accelerate(self, env):
        """Девять чтений + сколько угодно листаний ≠ десятое чтение."""
        for tip_id in _tip_ids(9):
            await bot._on_tip_viewed(env.uid, "tm", tip_id)
        for tip_id in _tip_ids(10) * 3:
            await bot._on_tip_viewed(env.uid, "tm", tip_id, count_progress=False)
        assert not await _achievement_done(env)
        assert await _stats(env) == 9


class TestAnalyticsAndSeenUntouched:
    async def test_paging_still_emits_tip_viewed_event(self, env):
        """Аналитика читает tip_viewed в нескольких местах — листание не гасим."""
        tip_id = _tip_ids(1)[0]
        await bot._on_tip_viewed(env.uid, "tm", tip_id, count_progress=False)
        events = await _events(env)
        assert len(events) == 1
        assert events[0]["tip_id"] == tip_id and events[0]["category"] == "tm"
        assert events[0]["source"] == "list" and events[0]["counted"] is False

    async def test_paging_still_marks_tip_seen(self, env):
        tip_id = _tip_ids(1)[0]
        await bot._on_tip_viewed(env.uid, "tm", tip_id, count_progress=False)
        assert tip_id in await env.tips.get_recently_seen_tip_ids(env.uid, 7)

    async def test_unknown_category_is_ignored(self, env):
        assert await bot._on_tip_viewed(env.uid, "nope", "x", count_progress=False) == ""
        assert await _events(env) == []


def _callback(env, data):
    """Подставной CallbackQuery: ровно то, что трогают хендлеры советов."""
    from types import SimpleNamespace
    cb = SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=env.uid),
        message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
        answer=AsyncMock(),
    )
    return cb


class TestRealHandlers:
    """Через настоящие хендлеры, а не только через внутреннюю функцию."""

    async def test_arrow_taps_do_not_farm_the_achievement(self, env):
        for i in range(12):                       # больше десяти тапов по стрелке
            cb = _callback(env, f"tips:list:tm:{i % 5}")
            await bot.handle_tips_list(cb)
            cb.message.edit_text.assert_awaited()  # совет реально показан
        assert await _stats(env) == 0
        assert not await _achievement_done(env)

    async def test_more_button_still_counts(self, env):
        for _ in range(10):
            await bot.handle_tips_more(_callback(env, "tips:more:tm"))
        assert await _stats(env) == 10
        assert await _achievement_done(env)

    async def test_list_page_shows_no_progress_line(self, env):
        cb = _callback(env, "tips:list:tm:0")
        await bot.handle_tips_list(cb)
        body = cb.message.edit_text.await_args.args[0]
        from i18n import t
        assert t("tips.read_count", "ru", count=0).strip() not in body
