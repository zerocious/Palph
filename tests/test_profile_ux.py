"""Profile keyboard + conditional flashcard shortcuts after subject pick."""
import pytest

import bot as bot_module
from bot import (
    PROFILE_COMING_SOON_SUBJECTS,
    _build_profile_inline_keyboard,
    _build_subject_progress_block,
    _maybe_send_subject_fc_shortcuts,
    _maybe_send_flash_mode_fc_shortcuts,
)
from i18n import subject_label, t
from repository import UserFlashcardRepository


@pytest.mark.asyncio
async def test_profile_keyboard_includes_leaderboard_and_friends(db, user_repo, created_user):
    user_id = created_user
    markup = _build_profile_inline_keyboard(user_id, "ru")
    texts = [btn.text for row in markup.inline_keyboard for btn in row]
    assert "🏆 Рейтинг недели" in texts
    assert "👥 Друзья" in texts
    assert "🏆 Достижения" in texts


@pytest.mark.asyncio
async def test_subject_fc_shortcuts_only_when_own_cards(db, user_repo, created_user):
    from unittest.mock import AsyncMock, MagicMock

    user_id = created_user
    ufc = UserFlashcardRepository(db)
    bot_module.user_flashcard_repo = ufc
    message = MagicMock()
    message.answer = AsyncMock()

    await _maybe_send_subject_fc_shortcuts(message, user_id, "industrial-management", "ru")
    message.answer.assert_not_called()

    await ufc.create(user_id, "industrial-management", "term1", "def1")
    await _maybe_send_subject_fc_shortcuts(message, user_id, "industrial-management", "ru")
    assert message.answer.call_count == 1
    reply_markup = message.answer.call_args.kwargs.get("reply_markup") or message.answer.call_args[0][1]
    btn_data = [b.callback_data for row in reply_markup.inline_keyboard for b in row]
    assert any(d.startswith("fc_add:") for d in btn_data)
    assert any(d.startswith("fc_list:") for d in btn_data)


@pytest.mark.asyncio
async def test_flash_mode_shortcuts_only_without_own_cards(db, user_repo, created_user):
    from unittest.mock import AsyncMock, MagicMock

    user_id = created_user
    ufc = UserFlashcardRepository(db)
    bot_module.user_flashcard_repo = ufc
    message = MagicMock()
    message.answer = AsyncMock()

    await _maybe_send_flash_mode_fc_shortcuts(message, user_id, "industrial-management", "ru")
    assert message.answer.call_count == 1

    message.answer.reset_mock()
    await ufc.create(user_id, "industrial-management", "t2", "d2")
    await _maybe_send_flash_mode_fc_shortcuts(message, user_id, "industrial-management", "ru")
    message.answer.assert_not_called()


@pytest.mark.asyncio
async def test_profile_progress_shows_coming_soon_for_stub_subjects(created_user):
    user_id = created_user
    for subject_id in PROFILE_COMING_SOON_SUBJECTS:
        block = await _build_subject_progress_block(
            user_id, subject_id, subject_label(subject_id, "ru"), "ru",
        )
        assert t("progress.coming_soon", "ru") in block
        assert "📈" not in block
        assert "🕐" not in block
