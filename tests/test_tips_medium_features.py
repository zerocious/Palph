"""Средний tier советов: контекст, cooldown, совет дня, категория bot."""
import os
from pathlib import Path

import pytest
import pytest_asyncio

import bot
from repository import TipsRepository

ROOT = Path(__file__).resolve().parent.parent


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
    def test_pick_is_stable_across_processes(self):
        """
        Совет дня выбирался через встроенный hash() от строки, а он
        рандомизирован в каждом процессе (PYTHONHASHSEED). Один и тот же
        пользователь на ту же дату получал разный индекс после рестарта
        бота; стабильность держалась только на записи в user_tips_stats,
        а сам выбор был невоспроизводим по логам.

        Побочно это роняло test_stable_per_calendar_day: при 47 советах
        две даты совпадали примерно в 2% прогонов — тест падал раз в
        полсотни запусков.

        Проверяем свойство напрямую: два процесса с РАЗНЫМИ hash-seed'ами
        обязаны выбрать один и тот же совет.
        """
        import subprocess
        import sys

        script = (
            "import hashlib, sys;"
            "sys.path.insert(0, %r);"
            "d = hashlib.md5(b'7:2026-05-22').hexdigest();"
            "print(int(d, 16) %% 47)"
        ) % str(ROOT)

        picks = set()
        for seed in ("0", "1", "2"):
            env = dict(os.environ, PYTHONHASHSEED=seed)
            out = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True, text=True, env=env, cwd=str(ROOT),
            )
            assert out.returncode == 0, out.stderr
            picks.add(out.stdout.strip())

        assert len(picks) == 1, f"выбор разъехался между процессами: {picks}"

    def test_repository_does_not_use_randomized_hash(self):
        """
        Прямая защита от возврата к hash(): в resolve_tip_of_day его быть
        не должно — иначе выбор снова станет непредсказуемым между
        рестартами.
        """
        import ast

        src = (ROOT / "repository.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        target = next(
            fn for fn in ast.walk(tree)
            if isinstance(fn, ast.AsyncFunctionDef)
            and fn.name == "resolve_tip_of_day"
        )
        builtin_hash_calls = [
            n for n in ast.walk(target)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "hash"
        ]
        assert not builtin_hash_calls, (
            "resolve_tip_of_day снова использует встроенный hash() — он "
            "рандомизирован per-process, выбор совета дня перестанет быть "
            "воспроизводимым между рестартами бота"
        )

    async def test_stable_per_calendar_day(self, tips_repo, created_user):
        tips = bot._all_tips_flat()
        d1 = await tips_repo.resolve_tip_of_day(created_user, "2026-05-22", tips)
        d2 = await tips_repo.resolve_tip_of_day(created_user, "2026-05-22", tips)
        d3 = await tips_repo.resolve_tip_of_day(created_user, "2026-05-23", tips)
        assert d1["id"] == d2["id"]
        assert d1["id"] != d3["id"] or len(tips) == 1


class TestBotGuideCategory:
    def test_bot_guide_loaded(self):
        assert len(bot.BOT_GUIDE_TIPS) == 7
        assert "bot" in bot.TIP_CATEGORIES

    def test_tips_keyboard_has_bot_button(self):
        from i18n import t
        texts = [btn.text for row in bot.get_tips_keyboard("ru").keyboard for btn in row]
        assert t("kb.tips_bot_guide", "ru") in texts
