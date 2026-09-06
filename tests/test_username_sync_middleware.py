"""
Тесты UsernameSyncMiddleware — closes the only middleware coverage gap
flagged during the post-ship audit.

Что покрывается:
- happy path: middleware читает event_from_user.username и обновляет
  users.username, затем вызывает handler.
- username=None (Telegram-юзер без @handle) → сохраняется NULL.
- event_from_user отсутствует → handler всё равно вызывается (graceful).
- refresh_username бросает → handler всё равно вызывается (try/except
  внутри middleware гарантирует, что sync-failure не блокирует action).
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from bot import UsernameSyncMiddleware  # conftest fallback BOT_TOKEN


@pytest_asyncio.fixture
async def mw(user_repo):
    return UsernameSyncMiddleware(user_repo)


@pytest.fixture
def handler():
    """AsyncMock, чтобы проверить что middleware вызвал handler."""
    return AsyncMock(return_value="HANDLER_OK")


@pytest.fixture
def event():
    """Pure marker — middleware не трогает сам event, только data dict."""
    return object()


def _user_data(uid, username):
    """data dict в формате, который даёт aiogram middleware."""
    return {"event_from_user": SimpleNamespace(id=uid, username=username)}


class TestHappyPath:
    async def test_username_written_and_handler_called(
        self, mw, handler, event, user_repo, created_user, db
    ):
        result = await mw(handler, event, _user_data(created_user, "alice"))
        assert result == "HANDLER_OK"
        handler.assert_called_once_with(event, _user_data(created_user, "alice"))
        async with db.execute(
            "SELECT username FROM users WHERE user_id=?", (created_user,)
        ) as c:
            assert (await c.fetchone())["username"] == "alice"

    async def test_username_change_persists(
        self, mw, handler, event, user_repo, created_user, db
    ):
        await mw(handler, event, _user_data(created_user, "alice"))
        await mw(handler, event, _user_data(created_user, "alice2"))
        async with db.execute(
            "SELECT username FROM users WHERE user_id=?", (created_user,)
        ) as c:
            assert (await c.fetchone())["username"] == "alice2"

    async def test_username_none_stored_as_null(
        self, mw, handler, event, user_repo, created_user, db
    ):
        """Telegram-пользователь без публичного @handle → NULL в БД."""
        await mw(handler, event, _user_data(created_user, "alice"))
        await mw(handler, event, _user_data(created_user, None))
        async with db.execute(
            "SELECT username FROM users WHERE user_id=?", (created_user,)
        ) as c:
            assert (await c.fetchone())["username"] is None


class TestGracefulDegradation:
    async def test_no_event_from_user_handler_still_runs(
        self, mw, handler, event
    ):
        """data без event_from_user (некоторые event types) — handler всё равно."""
        result = await mw(handler, event, {})
        assert result == "HANDLER_OK"
        handler.assert_called_once()

    async def test_event_from_user_none_handler_still_runs(
        self, mw, handler, event
    ):
        result = await mw(handler, event, {"event_from_user": None})
        assert result == "HANDLER_OK"
        handler.assert_called_once()

    async def test_refresh_failure_does_not_block_handler(
        self, handler, event, user_repo
    ):
        """
        Если refresh_username бросает (например, БД заблокирована),
        middleware должен поглотить exception и всё равно вызвать handler.
        Это критично: username sync — вспомогательная операция; её сбой
        не должен лишать пользователя возможности учиться.
        """
        # Patch user_repo.refresh_username чтобы бросало
        async def _boom(*args, **kwargs):
            raise RuntimeError("simulated DB failure")

        original = user_repo.refresh_username
        user_repo.refresh_username = _boom
        try:
            mw = UsernameSyncMiddleware(user_repo)
            result = await mw(handler, event, _user_data(42, "alice"))
            assert result == "HANDLER_OK"
            handler.assert_called_once()
        finally:
            user_repo.refresh_username = original

    async def test_zero_user_id_skipped(self, mw, handler, event, db):
        """id=0 (falsy) — middleware пропускает refresh, но handler вызывает."""
        result = await mw(handler, event, _user_data(0, "alice"))
        assert result == "HANDLER_OK"
        # Никакой строки в users с id=0 не появилось
        async with db.execute(
            "SELECT COUNT(*) AS n FROM users WHERE user_id=0"
        ) as c:
            assert (await c.fetchone())["n"] == 0


class TestNonExistentUser:
    async def test_update_on_missing_user_is_silent_noop(
        self, mw, handler, event, db
    ):
        """
        User не в БД ещё (например, brand-new /start до handler'а) —
        UPDATE затрагивает 0 строк; middleware не падает.

        Это первая часть first-message gap'а, которую закрывает Fix
        в create_user.py. Здесь тестируем что middleware не ломается;
        корректность gap-fix проверяется в test_username_search.py:
        TestCreateUserWithUsername.
        """
        # user_id 99999 не существует
        result = await mw(handler, event, _user_data(99999, "ghost"))
        assert result == "HANDLER_OK"
        # Никакой строки не появилось — middleware только UPDATE, не INSERT
        async with db.execute(
            "SELECT COUNT(*) AS n FROM users WHERE user_id=99999"
        ) as c:
            assert (await c.fetchone())["n"] == 0


class TestWriteOnlyIfChanged:
    """
    UPDATE идёт с commit(), то есть fsync на каждое сообщение, и делит
    глобальный db.lock со всем остальным. @handle меняют редко, поэтому
    middleware пишет только при изменении значения. Замер на смешанной
    нагрузке: 2823 → 4233 событий/с, минус 95% записей.
    """

    async def test_repeated_same_username_writes_once(self, user_repo, handler, event):
        mw = UsernameSyncMiddleware(user_repo)
        user_repo.refresh_username = AsyncMock()

        for _ in range(20):
            await mw(handler, event, _user_data(1, "alice"))

        assert user_repo.refresh_username.await_count == 1

    async def test_changed_username_is_written(self, user_repo, handler, event):
        mw = UsernameSyncMiddleware(user_repo)
        user_repo.refresh_username = AsyncMock()

        await mw(handler, event, _user_data(1, "alice"))
        await mw(handler, event, _user_data(1, "alice"))
        await mw(handler, event, _user_data(1, "bob"))
        await mw(handler, event, _user_data(1, "bob"))

        assert user_repo.refresh_username.await_count == 2
        assert user_repo.refresh_username.await_args.args == (1, "bob")

    async def test_handle_removal_to_none_is_written(self, user_repo, handler, event):
        """Сброс @handle в Telegram — тоже изменение, его надо записать."""
        mw = UsernameSyncMiddleware(user_repo)
        user_repo.refresh_username = AsyncMock()

        await mw(handler, event, _user_data(1, "alice"))
        await mw(handler, event, _user_data(1, None))
        await mw(handler, event, _user_data(1, None))

        assert user_repo.refresh_username.await_count == 2
        assert user_repo.refresh_username.await_args.args == (1, None)

    async def test_users_are_cached_independently(self, user_repo, handler, event):
        mw = UsernameSyncMiddleware(user_repo)
        user_repo.refresh_username = AsyncMock()

        for _ in range(5):
            await mw(handler, event, _user_data(1, "alice"))
            await mw(handler, event, _user_data(2, "bob"))

        assert user_repo.refresh_username.await_count == 2

    async def test_failed_write_is_not_cached(self, user_repo, handler, event):
        """
        Ключевое: сбой БД не должен запомниться как «уже синхронизировано»,
        иначе username не обновится больше никогда.
        """
        mw = UsernameSyncMiddleware(user_repo)
        user_repo.refresh_username = AsyncMock(side_effect=RuntimeError("БД недоступна"))

        await mw(handler, event, _user_data(1, "alice"))
        assert user_repo.refresh_username.await_count == 1
        assert 1 not in mw._synced

        # БД ожила — следующая попытка обязана записать
        user_repo.refresh_username = AsyncMock()
        await mw(handler, event, _user_data(1, "alice"))
        assert user_repo.refresh_username.await_count == 1

    async def test_cache_is_bounded(self, user_repo, handler, event):
        """Неограниченный словарь рос бы по записи на каждого пользователя."""
        mw = UsernameSyncMiddleware(user_repo)
        mw.CACHE_MAX_ENTRIES = 10
        user_repo.refresh_username = AsyncMock()

        for uid in range(100):
            await mw(handler, event, _user_data(uid, f"user{uid}"))

        assert len(mw._synced) == 10
        # Вытесняются самые давние: последние 10 остались
        assert set(mw._synced) == set(range(90, 100))
