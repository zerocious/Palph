"""Средний tier советов: контекст, cooldown, совет дня, категория bot."""
import pytest
import pytest_asyncio

import bot
from repository import TipsRepository


@pytest_asyncio.fixture
async def tips_repo(db):
    return TipsRepository(db)


class TestTipsSeenCooldown:
    async def test_recently_seen_within_7_days(self, tips_repo, created_user):
        await tips_repo.record_seen(created_user, "tm-01")
        seen = await tips_repo.get_recently_seen_tip_ids(created_user, 7)
        assert "tm-01" in seen

    async def test_pick_excludes_recently_seen(
        self, tips_repo, user_repo, created_user, monkeypatch,
    ):
        tips = bot.TIP_CATEGORIES["tm"]["tips"]
        if len(tips) < 2:
            pytest.skip("need at least 2 tm tips")
        await tips_repo.record_seen(created_user, tips[0]["id"])
        monkeypatch.setattr(bot, "tips_repo", tips_repo)
        monkeypatch.setattr(bot, "user_repo", user_repo)
        picked = await bot._pick_tip(created_user, "tm", "ru")
        assert picked["id"] != tips[0]["id"]


class TestContextualTags:
    async def test_timer_tag_when_active_timer(
        self, user_repo, created_user, monkeypatch,
    ):
        monkeypatch.setitem(bot.active_timers, created_user, object())
        monkeypatch.setattr(bot, "user_repo", user_repo)
        monkeypatch.setattr(bot, "tips_repo", TipsRepository(user_repo.db))
        tags = await bot._preferred_tip_tags(created_user)
        assert "timer" in tags

    async def test_flashcards_tag_when_due(
        self, tips_repo, user_repo, created_user, db, monkeypatch,
    ):
        await db.execute(
            "INSERT INTO flashcard_progress "
            "(user_id, card_hash, ease_factor, interval_days, repetitions, next_review) "
            "VALUES (?, 'abc12345', 2.5, 1, 1, datetime('now', '-1 hour'))",
            (created_user,),
        )
        await db.commit()
        monkeypatch.setattr(bot, "tips_repo", tips_repo)
        monkeypatch.setattr(bot, "user_repo", user_repo)
        tags = await bot._preferred_tip_tags(created_user)
        assert "flashcards" in tags


class TestTipOfDay:
    async def test_stable_per_calendar_day(self, tips_repo, created_user):
        tips = bot._all_tips_flat()
        d1 = await tips_repo.resolve_tip_of_day(created_user, "2026-05-22", tips)
        d2 = await tips_repo.resolve_tip_of_day(created_user, "2026-05-22", tips)
        d3 = await tips_repo.resolve_tip_of_day(created_user, "2026-05-23", tips)
        assert d1["id"] == d2["id"]
        assert d1["id"] != d3["id"] or len(tips) == 1


class TestBotGuideCategory:
    def test_bot_guide_loaded(self):
        assert len(bot.BOT_GUIDE_TIPS) == 2
        assert "bot" in bot.TIP_CATEGORIES

    def test_tips_keyboard_has_bot_button(self):
        from i18n import t
        texts = [btn.text for row in bot.get_tips_keyboard("ru").keyboard for btn in row]
        assert t("kb.tips_bot_guide", "ru") in texts
