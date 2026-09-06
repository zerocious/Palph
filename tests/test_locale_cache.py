"""
Кэш локали в bot.loc().

Локаль читается на каждый хендлер, а в некоторых — до пяти раз. Замер:
любой вызов к aiosqlite стоит ~0.17 мс round-trip'а к рабочему потоку
(тривиальный «SELECT 1» стоит столько же, сколько get_locale), так что
пять loc() — это ~0.85 мс на сообщение за значение, которое пользователь
меняет раз в жизни.

Кэш безопасен только пока инвалидация полная: users.locale пишется ровно
в одном месте (выбор языка) и исчезает при удалении аккаунта.
"""
from __future__ import annotations

import os
from unittest.mock import AsyncMock

import pytest

os.environ.setdefault("BOT_TOKEN", "test-token-for-pytest-imports")

import bot  # noqa: E402


@pytest.fixture(autouse=True)
def clean_cache():
    bot._locale_cache.clear()
    yield
    bot._locale_cache.clear()


class TestLocaleCache:
    async def test_repeated_reads_hit_the_database_once(self, monkeypatch):
        repo = type("R", (), {"get_locale": AsyncMock(return_value="en")})()
        monkeypatch.setattr(bot, "user_repo", repo)

        results = [await bot.loc(1) for _ in range(20)]

        assert results == ["en"] * 20
        assert repo.get_locale.await_count == 1

    async def test_users_cached_independently(self, monkeypatch):
        async def fake(uid):
            return "en" if uid == 1 else "ru"
        repo = type("R", (), {"get_locale": AsyncMock(side_effect=fake)})()
        monkeypatch.setattr(bot, "user_repo", repo)

        assert await bot.loc(1) == "en"
        assert await bot.loc(2) == "ru"
        assert await bot.loc(1) == "en"
        assert repo.get_locale.await_count == 2

    async def test_invalidation_returns_fresh_value(self, monkeypatch):
        """Смена языка обязана вступать в силу немедленно, а не по TTL."""
        repo = type("R", (), {"get_locale": AsyncMock(return_value="ru")})()
        monkeypatch.setattr(bot, "user_repo", repo)

        assert await bot.loc(7) == "ru"

        repo.get_locale = AsyncMock(return_value="en")   # пользователь сменил язык
        assert await bot.loc(7) == "ru", "без инвалидации отдаём старое — так и задумано"

        bot._invalidate_locale_cache(7)
        assert await bot.loc(7) == "en"

    async def test_invalidating_unknown_user_is_safe(self):
        bot._invalidate_locale_cache(999)  # не должно бросать

    async def test_cache_is_bounded(self, monkeypatch):
        repo = type("R", (), {"get_locale": AsyncMock(return_value="ru")})()
        monkeypatch.setattr(bot, "user_repo", repo)
        monkeypatch.setattr(bot, "_LOCALE_CACHE_MAX", 10)

        for uid in range(100):
            await bot.loc(uid)

        assert len(bot._locale_cache) == 10
        assert set(bot._locale_cache) == set(range(90, 100))


class TestInvalidationIsWiredUp:
    """
    Кэш живёт ровно столько, сколько полна инвалидация. Проверяем, что оба
    места, где users.locale перестаёт быть актуальным, её вызывают.
    """

    def test_language_change_and_deletion_invalidate(self):
        import ast
        from pathlib import Path

        src = (Path(__file__).resolve().parent.parent / "bot.py").read_text(encoding="utf-8")
        tree = ast.parse(src)

        def calls_in(fn_name_substr: str, callee: str) -> bool:
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.AsyncFunctionDef, ast.FunctionDef)):
                    continue
                names = {
                    n.func.attr for n in ast.walk(fn)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                }
                if fn_name_substr not in names:
                    continue
                plain = {
                    n.func.id for n in ast.walk(fn)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                }
                if callee not in plain:
                    return False
            return True

        assert calls_in("set_locale", "_invalidate_locale_cache"), (
            "обработчик смены языка не сбрасывает кэш локали — пользователь "
            "останется на старом языке до рестарта бота"
        )
        assert calls_in("delete_user_completely", "_invalidate_locale_cache"), (
            "удаление аккаунта не сбрасывает кэш локали"
        )
