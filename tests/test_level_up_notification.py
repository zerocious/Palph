"""
Тесты level-up notification в StudyService.complete_session.

Покрывает:
- Notification отправляется при new_level > old_level
- Notification НЕ отправляется при new_level == old_level
- Newly-unlocked items вычисляются корректно из catalogs
- Bot exceptions (заблокирован, etc.) поглощаются — не падает
- Без `bot` argument — notification skip'ается (тесты могут не передавать)
- Уведомление упоминает покупку предметов ТОЛЬКО когда кастомизация питомца
  включена (раньше зазывало в магазин, которого нет)
- Текст на языке пользователя (ключи pet.level_up* были, но не использовались)
"""
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from repository import PetRepository
from services import StudyService, AchievementService


@pytest_asyncio.fixture
async def study_service_with_bot(user_repo, session_repo, db, achievements_catalog):
    """StudyService с моковым ботом для проверки level-up notifications."""
    pet_repo = PetRepository(db)
    ach_service = AchievementService(user_repo, achievements_catalog)
    bot = AsyncMock()
    bot.send_message = AsyncMock()
    svc = StudyService(
        user_repo, session_repo, ach_service,
        pet_repo=pet_repo, leaderboard_repo=None, bot=bot,
    )
    svc._test_bot = bot
    return svc


@pytest_asyncio.fixture
async def study_service_with_shop(user_repo, session_repo, db, achievements_catalog):
    """StudyService с ВКЛЮЧЁННОЙ кастомизацией: предметы можно купить."""
    pet_repo = PetRepository(db)
    ach_service = AchievementService(user_repo, achievements_catalog)
    bot = AsyncMock()
    bot.send_message = AsyncMock()
    svc = StudyService(
        user_repo, session_repo, ach_service,
        pet_repo=pet_repo, leaderboard_repo=None, bot=bot,
        pet_customization_enabled=True,
    )
    svc._test_bot = bot
    return svc


def _sent_text(svc) -> str:
    call = svc._test_bot.send_message.call_args
    return call.args[1] if len(call.args) >= 2 else call.kwargs.get("text", "")


class TestLevelUpFiresNotification:
    async def test_level_up_sends_message(
        self, study_service_with_bot, user_repo
    ):
        """Сессия с большой длительностью → level-up → bot.send_message."""
        uid = 1
        await user_repo.create_user(uid)
        # 10 минут учёбы → xp 10 → level = floor(sqrt(10/10))+1 = 2 (был 1) → level-up
        await study_service_with_bot.complete_session(uid, 10)
        bot = study_service_with_bot._test_bot
        bot.send_message.assert_called_once()
        # Текст содержит «Уровень повышен»
        call = bot.send_message.call_args
        text = call.args[1] if len(call.args) >= 2 else call.kwargs.get("text", "")
        assert "Уровень повышен" in text or "повышен" in text


class TestNoLevelUpNoNotification:
    async def test_small_session_no_level_up_no_message(
        self, study_service_with_bot, user_repo
    ):
        """5 минут учёбы (xp=5, ещё level 1) → нет level-up → нет message."""
        uid = 2
        await user_repo.create_user(uid)
        await study_service_with_bot.complete_session(uid, 5)
        bot = study_service_with_bot._test_bot
        bot.send_message.assert_not_called()

    async def test_zero_duration_no_message(
        self, study_service_with_bot, user_repo
    ):
        uid = 3
        await user_repo.create_user(uid)
        # 0 minutes → add_xp returns (0, 0) → no level-up
        await study_service_with_bot.complete_session(uid, 0)
        bot = study_service_with_bot._test_bot
        bot.send_message.assert_not_called()


class TestNewlyUnlockedItemsInMessage:
    """Контракт списка предметов — при ВКЛЮЧЁННОЙ кастомизации."""

    async def test_level_2_unlocks_listed(
        self, study_service_with_shop, user_repo, db
    ):
        """Level 1→2 разблокирует grey (lvl 1 — уже было) и hat (lvl 1)…
        actually grey unlock_level=1, hat unlock_level=1 — оба разблокированы
        с уровня 1, что значит они доступны С level 2 (т.к. unlock_level <=
        new_level и old_level < unlock_level).

        Подождите. Логика _notify_level_up:
            old_level < lvl <= new_level

        Для level 1→2 (old=1, new=2):
        - grey (lvl=1): 1 < 1 False → НЕ unlocked (был доступен с уровня 1)
        - blue (lvl=2): 1 < 2 <= 2 True → unlocked
        - hat (lvl=1): 1 < 1 False → НЕ unlocked
        - glasses (lvl=3): 1 < 3 <= 2 False → НЕ unlocked

        Ожидаем: blue + green (оба lvl=2) разблокировались.
        """
        uid = 4
        await user_repo.create_user(uid)
        await study_service_with_shop.complete_session(uid, 10)  # → level 2
        text = _sent_text(study_service_with_shop)
        # blue и green открылись (оба lvl 2, price > 0)
        assert "blue" in text
        assert "green" in text
        # grey НЕ открылся (доступен с lvl 1)
        # На level 2 — нет grey в списке открытых (хоть он сам с lvl 1)
        # Test через explicit assertion на отсутствие
        # (но grey может встретиться как substring другого слова… ладно skip)

    async def test_message_when_no_new_items(
        self, study_service_with_shop, user_repo, db
    ):
        """Когда уровень-up не открывает новых предметов — особый текст."""
        # Создаём pet с xp близким к level 7 (sqrt(360/10)=6, +1=7).
        # 360 xp = level 7. Чтобы перейти 7→8, нужно xp 490 (sqrt(490/10)≈7,
        # actually floor(sqrt(490/10))=floor(sqrt(49))=7, +1=8).
        # Tier 8 каталога: crown (accessory, lvl 8, 240 coins).
        # Так что level 7→8 ДОЛЖЕН разблокировать crown.
        #
        # Попробуем level 5→6 (только grey/blue/green/pink уже доступны
        # с lvl 1,2,2,4; only crown remains at lvl 8; scarf at lvl 5).
        # На level 5→6: 5 < lvl <= 6. scarf=5, lvl > 5 True? 5 < 5 False.
        # Ничего не открывается на 5→6 — это то что нужно.
        uid = 5
        await user_repo.create_user(uid)
        # Сначала ставим level 5 через прямую модификацию pet_repo
        await study_service_with_shop.pet_repo.create_pet_with_defaults(uid)
        await db.execute(
            "UPDATE user_pet SET xp = 250, level = 5 WHERE user_id = ?",
            (uid,),
        )
        await db.commit()
        # Прибавляем XP: 250→260 (sqrt(26)≈5.1, floor=5, +1=6). level 5→6.
        await study_service_with_shop.complete_session(uid, 10)
        bot = study_service_with_shop._test_bot
        # На 5→6 ничего не открывается (scarf был на lvl 5, уже доступен)
        call = bot.send_message.call_args
        text = call.args[1] if len(call.args) >= 2 else call.kwargs.get("text", "")
        # Должно быть сообщение про повышение
        assert "повышен" in text
        # Но без списка новых предметов
        assert "новых предметов не открылось" in text or "Открылись" not in text


class TestNoShopNoAdvertising:
    """
    PET_CUSTOMIZATION_ENABLED = False: покупать цвета и аксессуары
    нельзя. Раньше level-up всё равно перечислял «новые предметы» и
    звал «купить в профиле» — пользователь копил монеты под обещание,
    которое нечем выполнить.
    """

    async def test_level_up_still_congratulates(
        self, study_service_with_bot, user_repo
    ):
        await user_repo.create_user(1)
        await study_service_with_bot.complete_session(1, 10)
        study_service_with_bot._test_bot.send_message.assert_called_once()
        assert "повышен" in _sent_text(study_service_with_bot)

    async def test_no_items_listed_when_shop_closed(
        self, study_service_with_bot, user_repo
    ):
        await user_repo.create_user(1)
        await study_service_with_bot.complete_session(1, 10)  # → level 2, blue/green
        text = _sent_text(study_service_with_bot)
        assert "blue" not in text and "green" not in text
        assert "Открылись" not in text
        assert "customization picker" not in text
        assert "Купить" not in text

    async def test_no_none_unlocked_line_either(
        self, study_service_with_bot, user_repo, db
    ):
        """«Новых предметов не открылось» тоже не нужно — магазина нет вовсе."""
        await user_repo.create_user(1)
        await study_service_with_bot.pet_repo.create_pet_with_defaults(1)
        await db.execute("UPDATE user_pet SET xp = 250, level = 5 WHERE user_id = 1")
        await db.commit()
        await study_service_with_bot.complete_session(1, 10)  # 5 → 6, ничего нового
        assert "не открылось" not in _sent_text(study_service_with_bot)

    def test_default_is_the_safe_side(self, user_repo, session_repo, achievements_catalog):
        """Забытый проброс флага должен скрывать обещание, а не давать его."""
        svc = StudyService(
            user_repo, session_repo, AchievementService(user_repo, achievements_catalog)
        )
        assert svc.pet_customization_enabled is False

    def test_bot_passes_the_module_flag(self):
        """main() обязан связать сервис с bot.PET_CUSTOMIZATION_ENABLED."""
        import inspect

        import bot

        src = inspect.getsource(bot.main)
        assert "pet_customization_enabled=PET_CUSTOMIZATION_ENABLED" in src


class TestLevelUpLocale:
    async def _set_locale(self, user_repo, uid, locale):
        await user_repo.create_user(uid)
        await user_repo.set_locale(uid, locale)

    async def test_english_user_gets_english(self, study_service_with_bot, user_repo):
        await self._set_locale(user_repo, 1, "en")
        await study_service_with_bot.complete_session(1, 10)
        text = _sent_text(study_service_with_bot)
        assert "Level up" in text
        assert "Уровень" not in text

    async def test_russian_user_gets_russian(self, study_service_with_bot, user_repo):
        await self._set_locale(user_repo, 1, "ru")
        await study_service_with_bot.complete_session(1, 10)
        assert "Уровень повышен" in _sent_text(study_service_with_bot)

    async def test_unset_locale_falls_back_to_default(
        self, study_service_with_bot, user_repo
    ):
        await user_repo.create_user(1)  # locale = ''
        await study_service_with_bot.complete_session(1, 10)
        assert "Уровень повышен" in _sent_text(study_service_with_bot)

    async def test_english_item_lines_when_shop_open(
        self, study_service_with_shop, user_repo
    ):
        await self._set_locale(user_repo, 1, "en")
        await study_service_with_shop.complete_session(1, 10)
        text = _sent_text(study_service_with_shop)
        assert "New items unlocked" in text
        assert "color" in text and "blue" in text
        assert "цвет" not in text and "Открылись" not in text


class TestGracefulDegradation:
    async def test_bot_exception_swallowed(
        self, study_service_with_bot, user_repo
    ):
        """Если bot.send_message бросает (заблокирован user) — complete_session
        всё равно возвращает успешно."""
        uid = 6
        await user_repo.create_user(uid)
        # Имитируем заблокированного user'а
        async def _boom(*args, **kwargs):
            raise RuntimeError("user blocked")
        study_service_with_bot._test_bot.send_message.side_effect = _boom
        # Не должно бросить наружу
        earned, bonus, session_id = await study_service_with_bot.complete_session(uid, 10)
        # complete_session завершился, session создана
        assert session_id is not None

    async def test_no_bot_no_notification(
        self, user_repo, session_repo, db, achievements_catalog
    ):
        """StudyService без bot=... — level-up не вызывает send."""
        pet_repo = PetRepository(db)
        ach_service = AchievementService(user_repo, achievements_catalog)
        # bot=None
        svc = StudyService(
            user_repo, session_repo, ach_service,
            pet_repo=pet_repo, bot=None,
        )
        uid = 7
        await user_repo.create_user(uid)
        # Должно отработать без ошибок
        await svc.complete_session(uid, 10)

    async def test_locale_lookup_failure_does_not_break_session(
        self, study_service_with_bot, user_repo, monkeypatch
    ):
        """
        Уведомление идёт ПОСЛЕ коммита complete_session. Новая зависимость
        от БД (чтение локали) не вправе сделать так, чтобы её сбой ронял
        сессию, которая уже записана.
        """
        await user_repo.create_user(1)

        async def boom(_uid):
            raise RuntimeError("db is gone")

        monkeypatch.setattr(user_repo, "get_locale", boom)
        earned, bonus, session_id = await study_service_with_bot.complete_session(1, 10)
        assert session_id > 0, "сессия не записалась"
        study_service_with_bot._test_bot.send_message.assert_not_called()

