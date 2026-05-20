"""
Тесты ReminderService — sad-pet интеграция в evening reminder.

Покрывает:
- Sad-pet copy используется когда derive_emotion возвращает 'sad'
  (default case: has_studied_today=0 — по filter'у это всегда так)
- Fallback copy используется defensively если has_studied_today=1
  слипнулось через filter (теоретический edge case)
- Empty users list → нет send_message вызовов
- TelegramForbiddenError → graceful logging, не падает
- Unknown TZ → defensive fallback на naive datetime, всё ещё работает
"""
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiogram.exceptions import TelegramForbiddenError

from services import ReminderService


@pytest_asyncio.fixture
async def reminder_service(user_repo):
    bot = AsyncMock()
    bot.send_message = AsyncMock()
    svc = ReminderService(user_repo, bot=bot)
    svc._test_bot = bot
    return svc


async def _mock_due_users(reminder_service, users):
    """
    Patches user_repo.get_users_due_for_evening так, чтобы возвращался
    указанный список. Удобнее чем seedить реальные timezone+time условия.
    """
    async def _stub(tz, hhmm):
        return users
    reminder_service.user_repo.get_users_due_for_evening = _stub
    # morning не интересен в этих тестах — пустой stub
    async def _morning_stub(tz, hhmm):
        return []
    reminder_service.user_repo.get_users_due_for_morning = _morning_stub


class TestSadPetCopy:
    async def test_sad_pet_text_sent_when_has_studied_today_zero(
        self, reminder_service
    ):
        """Default путь: has_studied_today=0 → sad-pet копи."""
        await _mock_due_users(
            reminder_service,
            [{"user_id": 100, "has_studied_today": 0}],
        )
        await reminder_service.tick("Europe/Moscow", "21:00")

        reminder_service._test_bot.send_message.assert_called_once()
        sent_text = reminder_service._test_bot.send_message.call_args.kwargs.get(
            "text"
        ) or reminder_service._test_bot.send_message.call_args.args[1]
        assert "грустит" in sent_text or "🐾😢" in sent_text

    async def test_multiple_users_all_get_sad_pet(self, reminder_service):
        await _mock_due_users(
            reminder_service,
            [
                {"user_id": 1, "has_studied_today": 0},
                {"user_id": 2, "has_studied_today": 0},
                {"user_id": 3, "has_studied_today": 0},
            ],
        )
        await reminder_service.tick("Europe/Moscow", "21:00")
        assert reminder_service._test_bot.send_message.call_count == 3
        for call in reminder_service._test_bot.send_message.call_args_list:
            text = call.kwargs.get("text") or call.args[1]
            # Emoji marker — стабильнее чем конкретное слово
            assert "🐾😢" in text


class TestFallbackCopy:
    async def test_has_studied_today_true_uses_fallback(self, reminder_service):
        """
        Defensive: если has_studied_today=1 слипнулось через filter
        (теоретически), мы НЕ говорим что пёс грустит — некорректно.
        Fallback копия — generic.
        """
        await _mock_due_users(
            reminder_service,
            [{"user_id": 100, "has_studied_today": 1}],
        )
        await reminder_service.tick("Europe/Moscow", "21:00")
        sent_text = (
            reminder_service._test_bot.send_message.call_args.kwargs.get("text")
            or reminder_service._test_bot.send_message.call_args.args[1]
        )
        # Это путь не-sad — sad-pet копи не должна быть
        assert "🐾😢" not in sent_text
        # Но какой-то reminder всё равно отправляется
        assert reminder_service._test_bot.send_message.called


class TestEdgeCases:
    async def test_empty_users_no_send(self, reminder_service):
        await _mock_due_users(reminder_service, [])
        await reminder_service.tick("Europe/Moscow", "21:00")
        reminder_service._test_bot.send_message.assert_not_called()

    async def test_blocked_user_handled_gracefully(self, reminder_service):
        """TelegramForbiddenError при отправке — INFO log + продолжаем."""
        await _mock_due_users(
            reminder_service,
            [
                {"user_id": 1, "has_studied_today": 0},
                {"user_id": 2, "has_studied_today": 0},
            ],
        )
        # User 1 заблокировал бота
        async def _send_side_effect(*args, **kwargs):
            uid = kwargs.get("chat_id") or args[0]
            if uid == 1:
                raise TelegramForbiddenError(
                    method=None, message="Forbidden: bot was blocked"
                )

        reminder_service._test_bot.send_message.side_effect = _send_side_effect
        # Не должно бросить наружу
        await reminder_service.tick("Europe/Moscow", "21:00")
        # User 2 всё равно получил попытку отправки
        assert reminder_service._test_bot.send_message.call_count == 2

    async def test_unknown_timezone_defensive_fallback(self, reminder_service):
        """Несуществующий TZ → pytz бросит, но мы fall back на naive datetime."""
        await _mock_due_users(
            reminder_service,
            [{"user_id": 1, "has_studied_today": 0}],
        )
        # 'Not/A/Real/TZ' не валиден, но reminder не должен упасть
        await reminder_service.tick("Not/A/Real/TZ", "21:00")
        reminder_service._test_bot.send_message.assert_called_once()
