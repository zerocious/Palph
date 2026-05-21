"""Совет дня в утреннем напоминании."""
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from repository import TipsRepository
from services import ReminderService


@pytest_asyncio.fixture
async def tips_repo(db):
    return TipsRepository(db)


@pytest.mark.asyncio
async def test_morning_reminder_includes_tip_of_day(user_repo, tips_repo):
    import bot

    bot.user_repo = user_repo
    bot.tips_repo = tips_repo
    await user_repo.create_user(100)

    bot_sent: list[dict] = []

    async def capture_send(chat_id, text, parse_mode=None):
        bot_sent.append({"text": text, "parse_mode": parse_mode})

    mock_bot = AsyncMock()
    mock_bot.send_message = AsyncMock(side_effect=capture_send)

    async def _morning_users(tz, hhmm):
        return [{"user_id": 100}]

    svc = ReminderService(
        user_repo,
        mock_bot,
        morning_tip_builder=bot.build_morning_tip_block,
    )
    svc.user_repo.get_users_due_for_morning = _morning_users
    svc.user_repo.get_users_due_for_evening = AsyncMock(return_value=[])

    await svc.tick("Europe/Moscow", "09:00")

    assert mock_bot.send_message.call_count == 1
    msg = bot_sent[0]["text"]
    assert "Совет дня" in msg
    assert bot_sent[0]["parse_mode"] == "HTML"
