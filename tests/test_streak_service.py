"""
Тесты StreakService — обработка ежедневного апдейта стриков.

Бот моки́руется (notifications), потому что StreakService может опционально
шлёт сообщения. Логика — только над БД.
"""
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from services import StreakService


@pytest_asyncio.fixture
async def streak_service(user_repo):
    """StreakService с моковым ботом — не упадёт на send_message, но запоминает вызовы."""
    bot = AsyncMock()
    bot.send_message = AsyncMock()
    svc = StreakService(user_repo, bot=bot)
    svc._test_bot = bot  # для проверки вызовов в тестах
    return svc


class TestStreakIncrement:
    async def test_streak_bumps_when_studied(self, user_repo, streak_service):
        uid = 100
        await user_repo.create_user(uid)
        await user_repo.set_has_studied_today(uid, True)
        await streak_service.process_all_users()
        user = await user_repo.get_user(uid)
        assert user["current_streak"] == 1

    async def test_has_studied_flag_cleared_after_processing(self, user_repo, streak_service):
        """Флаг has_studied_today сбрасывается в 0 — иначе на следующий день будет ложный bump."""
        uid = 101
        await user_repo.create_user(uid)
        await user_repo.set_has_studied_today(uid, True)
        await streak_service.process_all_users()
        user = await user_repo.get_user(uid)
        assert user["has_studied_today"] == 0

    async def test_streak_continues_across_days(self, user_repo, streak_service):
        """Дважды process_all_users с has_studied=True между ними → стрик растёт до 2."""
        uid = 102
        await user_repo.create_user(uid)
        # День 1
        await user_repo.set_has_studied_today(uid, True)
        await streak_service.process_all_users()
        # День 2
        await user_repo.set_has_studied_today(uid, True)
        await streak_service.process_all_users()
        user = await user_repo.get_user(uid)
        assert user["current_streak"] == 2


class TestStreakReset:
    async def test_streak_resets_when_not_studied(self, user_repo, streak_service):
        uid = 200
        await user_repo.create_user(uid)
        await user_repo.set_streak(uid, 5)
        # has_studied_today=0 по дефолту
        await streak_service.process_all_users()
        user = await user_repo.get_user(uid)
        assert user["current_streak"] == 0

    async def test_reset_zero_stays_zero(self, user_repo, streak_service):
        uid = 201
        await user_repo.create_user(uid)
        # streak=0, has_studied=0 → ничего не должно сломаться
        await streak_service.process_all_users()
        user = await user_repo.get_user(uid)
        assert user["current_streak"] == 0
        assert user["has_studied_today"] == 0


class TestStreakBonusCoins:
    async def test_no_bonus_on_day_1(self, user_repo, streak_service):
        """С 1-го дня стрика бонус НЕ начисляется (бонус начинается со 2-го)."""
        uid = 300
        await user_repo.create_user(uid)
        await user_repo.set_has_studied_today(uid, True)
        coins_before = (await user_repo.get_user(uid))["total_coins"]
        await streak_service.process_all_users()
        coins_after = (await user_repo.get_user(uid))["total_coins"]
        assert coins_after == coins_before, "1-й день стрика — без бонуса"

    async def test_bonus_15_coins_from_day_2(self, user_repo, streak_service):
        uid = 301
        await user_repo.create_user(uid)
        await user_repo.set_streak(uid, 1)
        await user_repo.set_has_studied_today(uid, True)
        coins_before = (await user_repo.get_user(uid))["total_coins"]
        await streak_service.process_all_users()
        coins_after = (await user_repo.get_user(uid))["total_coins"]
        assert coins_after - coins_before == 15
        # И стрик стал 2
        assert (await user_repo.get_user(uid))["current_streak"] == 2

    async def test_bonus_continues_on_higher_streaks(self, user_repo, streak_service):
        """Бонус +15 за каждый день стрика начиная со 2-го (не только на 2-м)."""
        uid = 302
        await user_repo.create_user(uid)
        await user_repo.set_streak(uid, 10)
        await user_repo.set_has_studied_today(uid, True)
        coins_before = (await user_repo.get_user(uid))["total_coins"]
        await streak_service.process_all_users()
        coins_after = (await user_repo.get_user(uid))["total_coins"]
        assert coins_after - coins_before == 15


class TestMultipleUsers:
    async def test_independent_users(self, user_repo, streak_service):
        """Каждый пользователь обрабатывается изолированно — один сбросился, второй растёт."""
        await user_repo.create_user(401)
        await user_repo.set_streak(401, 5)
        # has_studied_today=0 → должен сбросить

        await user_repo.create_user(402)
        await user_repo.set_streak(402, 3)
        await user_repo.set_has_studied_today(402, True)
        # has_studied_today=1 → стрик растёт

        await streak_service.process_all_users()

        u1 = await user_repo.get_user(401)
        u2 = await user_repo.get_user(402)
        assert u1["current_streak"] == 0
        assert u2["current_streak"] == 4
