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
        """Контракт: в пределах одного дня совет не меняется."""
        tips = bot._all_tips_flat()
        d1 = await tips_repo.resolve_tip_of_day(created_user, "2026-05-22", tips)
        d2 = await tips_repo.resolve_tip_of_day(created_user, "2026-05-22", tips)
        assert d1["id"] == d2["id"]

    async def test_tip_changes_across_days(self, tips_repo, created_user):
        """
        За неделю совет обязан смениться хотя бы раз. Раньше здесь
        сравнивались ровно две соседние даты — но «разные даты → разные
        советы» дизайном не гарантировано (это 1/N на пару), так что
        проверка была верна лишь по случайности выбранных дат.
        """
        tips = bot._all_tips_flat()
        picked = {
            (await tips_repo.resolve_tip_of_day(created_user, f"2026-05-{d}", tips))["id"]
            for d in range(22, 29)
        }
        assert len(picked) > 1 or len(tips) == 1

    def test_index_is_deterministic_in_process(self):
        n = len(bot._all_tips_flat())
        first = TipsRepository._tip_of_day_index(7, "2026-05-22", n)
        assert all(
            TipsRepository._tip_of_day_index(7, "2026-05-22", n) == first
            for _ in range(5)
        )

    def test_index_is_stable_across_processes(self):
        """
        Главная регрессия: выбор строился на встроенном hash(), а хеш
        строк в CPython рандомизирован per-process (PYTHONHASHSEED).
        Один и тот же (пользователь, дата) давал разный совет в разных
        запусках, и тест «разные даты → разные советы» падал случайно.
        """
        import os
        import subprocess
        import sys

        n = len(bot._all_tips_flat())
        expected = TipsRepository._tip_of_day_index(7, "2026-05-22", n)

        results = set()
        for seed in ("0", "1", "12345"):
            env = {**os.environ, "PYTHONHASHSEED": seed}
            out = subprocess.run(
                [
                    sys.executable, "-c",
                    "from repository import TipsRepository as T;"
                    f"print(T._tip_of_day_index(7, '2026-05-22', {n}))",
                ],
                capture_output=True, text=True, env=env, check=True,
            )
            results.add(int(out.stdout.strip()))

        assert results == {expected}, f"выбор зависит от процесса: {results}"

    def test_index_within_range(self):
        for n in (1, 2, 47, 1000):
            for uid in (0, 1, 999999):
                idx = TipsRepository._tip_of_day_index(uid, "2026-05-22", n)
                assert 0 <= idx < n


class TestBotGuideCategory:
    def test_bot_guide_loaded(self):
        assert len(bot.BOT_GUIDE_TIPS) == 7
        assert "bot" in bot.TIP_CATEGORIES

    def test_tips_keyboard_has_bot_button(self):
        from i18n import t
        texts = [btn.text for row in bot.get_tips_keyboard("ru").keyboard for btn in row]
        assert t("kb.tips_bot_guide", "ru") in texts
