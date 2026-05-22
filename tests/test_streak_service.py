"""
Тесты StreakService — обработка ежедневного апдейта стриков.

Бот моки́руется (notifications), потому что StreakService может опционально
шлёт сообщения. Логика — только над БД.
"""
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from repository import LeaderboardRepository
from services import StreakService


@pytest_asyncio.fixture
async def streak_service(user_repo):
    """StreakService с моковым ботом — не упадёт на send_message, но запоминает вызовы."""
    bot = AsyncMock()
    bot.send_message = AsyncMock()
    svc = StreakService(user_repo, bot=bot)
    svc._test_bot = bot  # для проверки вызовов в тестах
    return svc


@pytest_asyncio.fixture
async def streak_service_with_freeze(user_repo, db):
    """StreakService с leaderboard_repo, для тестов Phase 3 freeze-integration."""
    bot = AsyncMock()
    bot.send_message = AsyncMock()
    lb_repo = LeaderboardRepository(db)
    svc = StreakService(user_repo, bot=bot, leaderboard_repo=lb_repo)
    svc._test_bot = bot
    svc._test_lb_repo = lb_repo
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


# ============================================================
# Phase 3 — freeze integration в process_users_in_timezone
# (process_all_users / tz="*" не имеет today_local → freeze не consume'ится)
# ============================================================
class TestStreakFreezeIntegration:
    async def test_missed_day_with_freeze_preserves_streak(
        self, user_repo, streak_service_with_freeze, db
    ):
        """User с активной freeze + missed day → стрик сохраняется, freeze consumed."""
        uid = 500
        await user_repo.create_user(uid)
        await user_repo.set_streak(uid, 5)
        await user_repo.add_coins(uid, 1000)
        # Покупаем freeze
        result = await streak_service_with_freeze._test_lb_repo.purchase_freeze(uid, 5)
        assert result == "purchased"

        # has_studied_today=0 (дефолт) → missed day
        await streak_service_with_freeze.process_users_in_timezone("Europe/Moscow")

        # Стрик сохранён
        u = await user_repo.get_user(uid)
        assert u["current_streak"] == 5
        # Freeze consumed
        async with db.execute(
            "SELECT consumed_for_date FROM streak_freezes WHERE user_id=?",
            (uid,),
        ) as c:
            row = await c.fetchone()
        assert row["consumed_for_date"] is not None

    async def test_missed_day_without_freeze_resets(
        self, user_repo, streak_service_with_freeze
    ):
        """User БЕЗ freeze + missed day → стрик сбрасывается (стандартное поведение)."""
        uid = 501
        await user_repo.create_user(uid)
        await user_repo.set_streak(uid, 5)
        # has_studied_today=0; freeze не покупали

        await streak_service_with_freeze.process_users_in_timezone("Europe/Moscow")

        u = await user_repo.get_user(uid)
        assert u["current_streak"] == 0

    async def test_studied_day_with_freeze_keeps_freeze_unused(
        self, user_repo, streak_service_with_freeze, db
    ):
        """User с freeze, который ОТУЧИЛСЯ сегодня → freeze не consumed, остаётся в запасе."""
        uid = 502
        await user_repo.create_user(uid)
        await user_repo.set_streak(uid, 3)
        await user_repo.add_coins(uid, 1000)
        await streak_service_with_freeze._test_lb_repo.purchase_freeze(uid, 3)
        await user_repo.set_has_studied_today(uid, True)

        await streak_service_with_freeze.process_users_in_timezone("Europe/Moscow")

        # Стрик инкрементнулся
        u = await user_repo.get_user(uid)
        assert u["current_streak"] == 4
        # Freeze всё ещё активен
        async with db.execute(
            "SELECT consumed_for_date FROM streak_freezes WHERE user_id=?",
            (uid,),
        ) as c:
            row = await c.fetchone()
        assert row["consumed_for_date"] is None

    async def test_freeze_consumed_only_once_per_missed_day(
        self, user_repo, streak_service_with_freeze
    ):
        """После consume freeze user снова не учился → теперь стрик действительно сбрасывается."""
        uid = 503
        await user_repo.create_user(uid)
        await user_repo.set_streak(uid, 5)
        await user_repo.add_coins(uid, 1000)
        await streak_service_with_freeze._test_lb_repo.purchase_freeze(uid, 5)

        # День 1: missed → freeze срабатывает
        await streak_service_with_freeze.process_users_in_timezone("Europe/Moscow")
        u = await user_repo.get_user(uid)
        assert u["current_streak"] == 5

        # Симулируем следующий календарный день (idempotency marker).
        await user_repo.db.execute(
            "UPDATE users SET last_streak_check_date = '2000-01-01' WHERE user_id = ?",
            (uid,),
        )
        await user_repo.db.commit()

        # День 2: again missed → freeze уже потрачен, должен сбросить
        await streak_service_with_freeze.process_users_in_timezone("Europe/Moscow")
        u = await user_repo.get_user(uid)
        assert u["current_streak"] == 0
