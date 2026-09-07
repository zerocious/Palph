"""
Доставка обращений в поддержку.

Раньше отправка админам была обёрнута в `except Exception: pass`: сбой не
попадал даже в лог, а пользователю всё равно отвечали «✅ отправлено».
messages.log при этом не был запасным вариантом — ни одна команда бота
его не читает, и лежал он по относительному пути, то есть в Docker терялся
при каждом рестарте вместе со всеми обращениями.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

os.environ.setdefault("BOT_TOKEN", "test-token-for-pytest-imports")

import bot  # noqa: E402


class TestMessagesFileIsPersistent:
    def test_lives_next_to_the_log_file(self):
        """
        В Docker LOG_FILE указывает в персистентный /app/data. Архив
        обращений обязан лежать там же, а не в эфемерном слое контейнера.
        """
        from db import LOG_FILE

        assert Path(bot.MESSAGES_FILE).parent == Path(LOG_FILE).parent

    def test_path_is_not_bare_relative_when_log_is_absolute(self):
        """Регрессия: голая строка 'messages.log' игнорировала бы /app/data."""
        src = (Path(__file__).resolve().parent.parent / "bot.py").read_text(encoding="utf-8")
        assert 'MESSAGES_FILE = "messages.log"' not in src


class TestDeliveryFeedback:
    """Пользователю нельзя отвечать «отправлено», если не дошло никому."""

    @pytest.fixture(autouse=True)
    def _isolate(self, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, "MESSAGES_FILE", str(tmp_path / "messages.log"))
        monkeypatch.setattr(bot, "is_admin", lambda uid: False)
        monkeypatch.setattr(bot, "loc", AsyncMock(return_value="ru"))
        monkeypatch.setattr(bot, "get_main_keyboard", lambda locale: None)
        limiter = type("L", (), {"check": staticmethod(lambda user_id: "ok")})()
        monkeypatch.setattr(bot, "admin_message_limiter", limiter)

    def _message(self, text="нужна помощь"):
        from types import SimpleNamespace
        return SimpleNamespace(
            text=text, caption=None, content_type="text", message_id=1,
            from_user=SimpleNamespace(id=77, first_name="Иван", last_name="П"),
            answer=AsyncMock(),
        )

    async def test_success_tells_the_user_it_was_sent(self, monkeypatch):
        monkeypatch.setattr(bot, "ADMINS", [1, 2])
        monkeypatch.setattr(bot, "bot", type("B", (), {"send_message": AsyncMock()})())

        msg = self._message()
        await bot.handle_any_message(msg)

        said = msg.answer.await_args.args[0]
        assert "отправлено" in said

    async def test_total_failure_tells_the_user_it_did_not_go_through(self, monkeypatch):
        monkeypatch.setattr(bot, "ADMINS", [1, 2])
        failing = AsyncMock(side_effect=RuntimeError("Telegram недоступен"))
        monkeypatch.setattr(bot, "bot", type("B", (), {"send_message": failing})())

        msg = self._message()
        await bot.handle_any_message(msg)

        said = msg.answer.await_args.args[0]
        assert "Не получилось доставить" in said, (
            "при полном провале доставки пользователю нельзя отвечать «отправлено»"
        )

    async def test_partial_delivery_still_counts_as_sent(self, monkeypatch):
        """Один админ получил — обращение дошло, обманывать пользователя незачем."""
        monkeypatch.setattr(bot, "ADMINS", [1, 2])
        calls = {"n": 0}

        async def flaky(admin_id, text):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("этот админ заблокировал бота")

        # staticmethod: обычная функция в классе стала бы связанным методом,
        # и self занял бы место admin_id
        monkeypatch.setattr(bot, "bot", type("B", (), {"send_message": staticmethod(flaky)})())

        msg = self._message()
        await bot.handle_any_message(msg)

        assert "отправлено" in msg.answer.await_args.args[0]

    async def test_failure_is_logged(self, monkeypatch, caplog):
        monkeypatch.setattr(bot, "ADMINS", [1])
        monkeypatch.setattr(
            bot, "bot",
            type("B", (), {"send_message": AsyncMock(side_effect=RuntimeError("boom"))})(),
        )

        with caplog.at_level("WARNING"):
            await bot.handle_any_message(self._message())

        assert "support.delivery_failed" in caplog.text
