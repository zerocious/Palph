"""Security tests for command/callback input validation and loader sandboxing."""

import ast
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot import (
    _callback_allowlisted_subject,
    load_flashcards,
    load_mcq,
    load_quiz_section,
    load_task_groups,
    load_tasks,
)
from file_upload_security import (
    FRIEND_QUERY_MAX_LEN,
    TELEGRAM_MAX_MESSAGE_LEN,
    sanitize_plain_preview,
    truncate_for_telegram_message,
    truncate_text,
)


_BOT_PY = Path(__file__).resolve().parent.parent / "bot.py"


class TestOutputParseModeInvariant:
    """
    Экранирование вывода в этом боте держится на неявном условии: у Bot()
    НЕТ parse_mode по умолчанию (в aiogram 3 это None). Поэтому сообщения
    со свободным пользовательским текстом — условие своей задачи, превью
    списков своих задач и карточек — отправляются без parse_mode и не
    разбираются как HTML. Там, где HTML-разметка нужна, пользовательские
    строки уже проходят через html_escape (имя питомца, термин и
    определение карточки).

    Опасен именно рефакторинг «вынести повторяющийся parse_mode='HTML' из
    двух десятков вызовов в дефолт бота»: он выглядит безобидной уборкой,
    но разом делает HTML-разбираемыми ВСЕ пути со свободным текстом.
    Пользовательский '<' тогда либо протащит разметку, либо уронит
    отправку в 400.

    Тест не запрещает такой рефакторинг — он требует, чтобы его делали
    осознанно, обернув сырые пути в html_escape.
    """

    def test_aiogram_default_parse_mode_is_none(self):
        """Фиксируем поведение библиотеки, на которое опирается остальное."""
        from aiogram import Bot

        assert Bot(token="123:abc").default.parse_mode is None

    def test_bot_is_constructed_without_default_parse_mode(self):
        tree = ast.parse(_BOT_PY.read_text(encoding="utf-8"))
        constructions = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Bot"
        ]
        assert constructions, "не найден вызов Bot(...) в bot.py"

        for call in constructions:
            kwargs = {kw.arg for kw in call.keywords if kw.arg}
            assert "default" not in kwargs and "parse_mode" not in kwargs, (
                f"bot.py:{call.lineno}: у Bot() появился parse_mode по умолчанию. "
                "Перед этим нужно обернуть в html_escape пути, где "
                "пользовательский текст отправляется без parse_mode: условие "
                "своей задачи (_send_next_task) и превью в списках своих "
                "задач и карточек."
            )


class TestLoaderSubjectSandbox:
    def test_load_flashcards_rejects_traversal_subject(self):
        assert load_flashcards("..") == []
        assert load_flashcards("evil-subject") == []

    def test_load_mcq_rejects_traversal_subject(self):
        assert load_mcq("..") == []

    def test_load_tasks_rejects_traversal_subject(self):
        assert load_tasks("..") == []

    def test_load_task_groups_rejects_traversal_subject(self):
        assert load_task_groups("..") == {}

    def test_load_quiz_section_rejects_traversal_subject(self):
        assert load_quiz_section("i", "..") == []

    def test_load_quiz_section_rejects_invalid_section_key(self):
        assert load_quiz_section("../../../etc/passwd", "math") == []
        assert load_quiz_section("evil", "math") == []

    def test_known_subject_still_loads(self):
        cards = load_flashcards("math")
        assert isinstance(cards, list)


class TestTextLimits:
    def test_truncate_text_respects_max(self):
        assert truncate_text("hello", max_len=10) == "hello"
        assert truncate_text("x" * 20, max_len=10) == "x" * 9 + "…"

    def test_truncate_for_telegram_message_accounts_for_prefix(self):
        prefix = "P:\n"
        body = "a" * 5000
        out = truncate_for_telegram_message(prefix, body)
        assert len(prefix + out) <= TELEGRAM_MAX_MESSAGE_LEN

    def test_sanitize_plain_preview_collapses_newlines(self):
        assert sanitize_plain_preview("a\nb\nc", max_len=20) == "a b c"

    def test_friend_query_max_len_constant(self):
        assert FRIEND_QUERY_MAX_LEN == 64


@pytest.mark.asyncio
async def test_callback_allowlisted_subject_rejects_invalid(monkeypatch):
    callback = MagicMock()
    callback.from_user.id = 42
    callback.answer = AsyncMock()
    monkeypatch.setattr("bot.loc", AsyncMock(return_value="ru"))

    result = await _callback_allowlisted_subject(callback, "..")
    assert result is None
    callback.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_callback_allowlisted_subject_accepts_known(monkeypatch):
    callback = MagicMock()
    callback.from_user.id = 42
    callback.answer = AsyncMock()
    monkeypatch.setattr("bot.loc", AsyncMock(return_value="ru"))

    result = await _callback_allowlisted_subject(callback, "math")
    assert result == "math"
    callback.answer.assert_not_awaited()
