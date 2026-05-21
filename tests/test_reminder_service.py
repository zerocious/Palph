"""
Тесты ReminderService — sad-pet интеграция в evening reminder.

Покрывает:
- Sad-pet GIF + caption отправляется когда derive_emotion='sad'
  (default case: has_studied_today=0 — по filter'у это всегда так)
- FileNotFoundError из render_pet → graceful fallback на text-only
- Fallback copy через send_message используется defensively если
  has_studied_today=1 слипнулось через filter (теоретический edge case)
- Empty users list → ничего не шлём
- TelegramForbiddenError → graceful logging, не падает
- Unknown TZ → defensive fallback на naive datetime, всё ещё работает
"""
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiogram.exceptions import TelegramForbiddenError

import services as services_mod
from services import ReminderService


@pytest_asyncio.fixture
async def reminder_service(user_repo):
    bot = AsyncMock()
    bot.send_message = AsyncMock()
    bot.send_animation = AsyncMock()
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


class TestSadPetAnimation:
    async def test_sad_pet_gif_sent_when_has_studied_today_zero(
        self, reminder_service
    ):
        """Default путь: has_studied_today=0 → sad-pet animation."""
        await _mock_due_users(
            reminder_service,
            [{"user_id": 100, "has_studied_today": 0}],
        )
        await reminder_service.tick("Europe/Moscow", "21:00")

        reminder_service._test_bot.send_animation.assert_called_once()
        # send_message НЕ должен вызываться на sad path
        reminder_service._test_bot.send_message.assert_not_called()
        # Caption содержит sad-pet emoji
        call = reminder_service._test_bot.send_animation.call_args
        caption = call.kwargs.get("caption", "")
        assert "🐾😢" in caption

    async def test_multiple_users_all_get_sad_animation(
        self, reminder_service
    ):
        await _mock_due_users(
            reminder_service,
            [
                {"user_id": 1, "has_studied_today": 0},
                {"user_id": 2, "has_studied_today": 0},
                {"user_id": 3, "has_studied_today": 0},
            ],
        )
        await reminder_service.tick("Europe/Moscow", "21:00")
        assert reminder_service._test_bot.send_animation.call_count == 3
        for call in reminder_service._test_bot.send_animation.call_args_list:
            caption = call.kwargs.get("caption", "")
            assert "🐾😢" in caption


class TestReminderEvents:
    async def test_evening_logs_reminder_sent(self, user_repo, db):
        from repository import EventRepository

        bot = AsyncMock()
        bot.send_animation = AsyncMock()
        event_repo = EventRepository(db)
        svc = ReminderService(user_repo, bot=bot, event_repo=event_repo)

        async def _evening_stub(tz, hhmm):
            return [{"user_id": 42, "has_studied_today": 0}]

        async def _morning_stub(tz, hhmm):
            return []

        svc.user_repo.get_users_due_for_evening = _evening_stub
        svc.user_repo.get_users_due_for_morning = _morning_stub
        await user_repo.create_user(42)
        await svc.tick("Europe/Moscow", "21:00")

        async with db.execute(
            "SELECT event_name, properties FROM events WHERE user_id=42"
        ) as c:
            row = await c.fetchone()
        assert row["event_name"] == "reminder_sent"
        assert "evening" in row["properties"]


class TestAssetMissingFallback:
    async def test_filenotfound_falls_back_to_text(
        self, reminder_service, monkeypatch
    ):
        """
        Если assets/pet/sad.gif отсутствует (например, build-script не
        запускался) — render_pet бросает FileNotFoundError, и мы
        gracefully fallback'имся на bot.send_message с тем же sad-pet текстом.
        """
        def _missing(*args, **kwargs):
            raise FileNotFoundError("simulated missing asset")
        monkeypatch.setattr(services_mod, "render_pet", _missing)

        await _mock_due_users(
            reminder_service,
            [{"user_id": 1, "has_studied_today": 0}],
        )
        await reminder_service.tick("Europe/Moscow", "21:00")
        # Animation НЕ вызван (raise до него)
        reminder_service._test_bot.send_animation.assert_not_called()
        # Зато send_message — да, с sad-pet текстом
        reminder_service._test_bot.send_message.assert_called_once()
        text = reminder_service._test_bot.send_message.call_args.kwargs.get("text", "")
        assert "🐾😢" in text


class TestFallbackCopy:
    async def test_has_studied_today_true_uses_text_fallback(
        self, reminder_service
    ):
        """
        Defensive: если has_studied_today=1 слипнулось через filter
        (теоретически), мы НЕ говорим что пёс грустит — некорректно.
        Fallback копия через send_message (без animation).
        """
        await _mock_due_users(
            reminder_service,
            [{"user_id": 100, "has_studied_today": 1}],
        )
        await reminder_service.tick("Europe/Moscow", "21:00")
        # Sad-path animation НЕ вызван
        reminder_service._test_bot.send_animation.assert_not_called()
        # send_message вызван с generic копией
        reminder_service._test_bot.send_message.assert_called_once()
        sent_text = reminder_service._test_bot.send_message.call_args.kwargs.get(
            "text", ""
        )
        assert "🐾😢" not in sent_text


class TestEdgeCases:
    async def test_empty_users_no_send(self, reminder_service):
        await _mock_due_users(reminder_service, [])
        await reminder_service.tick("Europe/Moscow", "21:00")
        reminder_service._test_bot.send_animation.assert_not_called()
        reminder_service._test_bot.send_message.assert_not_called()

    async def test_blocked_user_handled_gracefully(self, reminder_service):
        """
        TelegramForbiddenError при отправке sad-animation — INFO log +
        продолжаем со следующим user'ом.
        """
        await _mock_due_users(
            reminder_service,
            [
                {"user_id": 1, "has_studied_today": 0},
                {"user_id": 2, "has_studied_today": 0},
            ],
        )
        async def _anim_side_effect(*args, **kwargs):
            uid = kwargs.get("chat_id") or args[0]
            if uid == 1:
                raise TelegramForbiddenError(
                    method=None, message="Forbidden: bot was blocked"
                )
        reminder_service._test_bot.send_animation.side_effect = _anim_side_effect
        # Не должно бросить наружу
        await reminder_service.tick("Europe/Moscow", "21:00")
        # User 2 всё равно получил попытку отправки
        assert reminder_service._test_bot.send_animation.call_count == 2

    async def test_unknown_timezone_defensive_fallback(self, reminder_service):
        """Несуществующий TZ → pytz бросит, но мы fall back на naive datetime."""
        await _mock_due_users(
            reminder_service,
            [{"user_id": 1, "has_studied_today": 0}],
        )
        # 'Not/A/Real/TZ' не валиден, но reminder не должен упасть
        await reminder_service.tick("Not/A/Real/TZ", "21:00")
        # Sad-path всё равно срабатывает (по умолчанию)
        reminder_service._test_bot.send_animation.assert_called_once()
