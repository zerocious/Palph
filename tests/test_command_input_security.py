"""Security tests for command/callback input validation and loader sandboxing."""

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
