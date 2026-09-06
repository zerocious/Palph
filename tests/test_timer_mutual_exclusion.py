"""
Сторона бота во взаимном исключении таймеров.

Telegram-таймер и таймер desktop-приложения не должны идти одновременно:
иначе одно и то же время оплачивается дважды — монетами, XP питомца и
очками недельного лидерборда. Зеркальную (API) сторону покрывает
tests/test_api.py::TestTimerMutualExclusion.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

import bot  # conftest fallback BOT_TOKEN
from repository import DesktopTimerRepository


@pytest_asyncio.fixture
async def timer_repo(db, monkeypatch):
    """Подкладываем боту реальный репозиторий поверх тестовой БД."""
    repo = DesktopTimerRepository(db)
    monkeypatch.setattr(bot, "desktop_timer_repo", repo)
    return repo


@pytest.fixture
def message():
    """Минимальный Message: хендлеру нужны только from_user.id и answer()."""
    return SimpleNamespace(from_user=SimpleNamespace(id=42), answer=AsyncMock())


class TestDesktopTimerBlocksBot:
    async def test_no_desktop_timer_does_not_block(self, timer_repo, message):
        assert await bot._desktop_timer_blocks_start(message, "ru") is False
        message.answer.assert_not_called()

    async def test_running_desktop_timer_blocks_and_explains(
        self, timer_repo, message, created_user,
    ):
        await timer_repo.start(created_user, 25)

        assert await bot._desktop_timer_blocks_start(message, "ru") is True
        message.answer.assert_called_once()
        text = message.answer.call_args.args[0]
        assert "приложении" in text
        assert "25" in text  # остаток минут подставлен

    async def test_expired_desktop_timer_does_not_block(
        self, db, timer_repo, message, created_user,
    ):
        """
        Приложение закрыли, не завершив сессию: строка висит, время вышло.
        Блокировать Telegram-таймер из-за неё нельзя — человек остался бы
        без таймера до следующего запуска приложения.
        """
        await timer_repo.start(created_user, 25)
        await db.execute(
            "UPDATE desktop_timers SET started_at = datetime('now', '-30 minutes')"
        )
        await db.commit()

        assert await bot._desktop_timer_blocks_start(message, "ru") is False
        message.answer.assert_not_called()

    async def test_other_users_timer_does_not_block(self, timer_repo, message, db):
        from repository import UserRepository

        await UserRepository(db).create_user(999)
        await timer_repo.start(999, 25)

        assert await bot._desktop_timer_blocks_start(message, "ru") is False

    async def test_repository_failure_does_not_break_the_timer_button(
        self, timer_repo, message, monkeypatch,
    ):
        """
        Сбой чтения не должен ронять запуск таймера: без защиты человек
        в худшем случае получит двойное начисление, а с упавшей кнопкой —
        не сможет заниматься вообще.
        """
        async def boom(_user_id):
            raise RuntimeError("db is down")

        monkeypatch.setattr(bot.desktop_timer_repo, "get", boom)
        assert await bot._desktop_timer_blocks_start(message, "ru") is False

    async def test_missing_repo_is_tolerated(self, message, monkeypatch):
        """До завершения main() глобал ещё None — не падаем."""
        monkeypatch.setattr(bot, "desktop_timer_repo", None)
        assert await bot._desktop_timer_blocks_start(message, "ru") is False


class TestRemainingMinutesRounding:
    async def test_partial_minute_shows_at_least_one(
        self, db, timer_repo, message, created_user,
    ):
        """30 секунд остатка — «0 мин» выглядело бы как «уже можно»."""
        await timer_repo.start(created_user, 25)
        await db.execute(
            "UPDATE desktop_timers "
            "SET started_at = datetime('now', '-24 minutes', '-30 seconds')"
        )
        await db.commit()

        assert await bot._desktop_timer_blocks_start(message, "ru") is True
        assert "1" in message.answer.call_args.args[0]
