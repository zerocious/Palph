"""
UserSerializationMiddleware — сериализация апдейтов одного пользователя.

aiogram запускает polling с handle_as_tasks=True, поэтому два подряд
отправленных сообщения одного человека обрабатываются параллельно. Хендлеры
делают read-modify-write через FSM, и без сериализации одно из изменений
теряется: воспроизводилось как потеря task_index и двойное начисление монет
за один верный ответ.
"""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("BOT_TOKEN", "test-token-for-pytest-imports")

from bot import UserSerializationMiddleware  # noqa: E402


def _event_for(user_id: int | None):
    """Минимальный data-dict, как его собирает aiogram."""
    user = SimpleNamespace(id=user_id) if user_id is not None else None
    return object(), {"event_from_user": user}


class TestSerialization:
    async def test_same_user_updates_do_not_lose_writes(self):
        """Ядро фикса: read-modify-write больше не теряет изменения."""
        mw = UserSerializationMiddleware()
        shared = {"counter": 0}

        async def handler(event, data):
            current = shared["counter"]     # чтение
            await asyncio.sleep(0)          # точка переключения
            shared["counter"] = current + 1  # запись

        event, data = _event_for(42)
        await asyncio.gather(*[mw(handler, event, data) for _ in range(50)])
        assert shared["counter"] == 50

    async def test_different_users_are_not_serialized(self):
        """
        Замок берётся на user_id: разные пользователи обязаны идти
        параллельно, иначе один медленный хендлер тормозит весь бот.
        """
        mw = UserSerializationMiddleware()
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow(event, data):
            started.set()
            await release.wait()

        async def quick(event, data):
            return "готово"

        ev_slow, data_slow = _event_for(1)
        ev_quick, data_quick = _event_for(2)

        slow_task = asyncio.create_task(mw(slow, ev_slow, data_slow))
        await started.wait()
        # Второй пользователь проходит, пока первый держит свой замок
        result = await asyncio.wait_for(mw(quick, ev_quick, data_quick), timeout=1)
        assert result == "готово"

        release.set()
        await slow_task

    async def test_handler_result_is_returned(self):
        mw = UserSerializationMiddleware()

        async def handler(event, data):
            return "значение"

        event, data = _event_for(7)
        assert await mw(handler, event, data) == "значение"

    async def test_event_without_user_passes_through(self):
        mw = UserSerializationMiddleware()

        async def handler(event, data):
            return "прошло"

        event, data = _event_for(None)
        assert await mw(handler, event, data) == "прошло"
        assert mw._locks == {}


class TestLockRegistryCleanup:
    """
    Словарь замков не должен расти по записи на каждого пользователя за всё
    время работы процесса — ровно та утечка, что была в UserRateLimiter.
    """

    async def test_locks_released_after_completion(self):
        mw = UserSerializationMiddleware()

        async def handler(event, data):
            return None

        for uid in range(200):
            event, data = _event_for(uid)
            await mw(handler, event, data)

        assert mw._locks == {}
        assert mw._pending == {}

    async def test_lock_survives_while_others_wait(self):
        """Замок нельзя удалять, пока за ним кто-то стоит в очереди."""
        mw = UserSerializationMiddleware()
        first_inside = asyncio.Event()
        release = asyncio.Event()
        seen: list[int] = []

        async def blocking(event, data):
            first_inside.set()
            await release.wait()
            seen.append(1)

        async def waiting(event, data):
            seen.append(2)

        event, data = _event_for(99)
        t1 = asyncio.create_task(mw(blocking, event, data))
        await first_inside.wait()
        t2 = asyncio.create_task(mw(waiting, event, data))
        await asyncio.sleep(0)  # даём второй задаче встать в очередь

        assert 99 in mw._locks, "замок удалён, пока за ним ждут"
        assert mw._pending[99] == 2

        release.set()
        await asyncio.gather(t1, t2)
        assert seen == [1, 2]      # порядок сохранён
        assert mw._locks == {}     # и убрано после завершения

    async def test_exception_releases_the_lock(self):
        """Упавший хендлер не должен оставлять пользователя заблокированным."""
        mw = UserSerializationMiddleware()

        async def boom(event, data):
            raise RuntimeError("сбой в хендлере")

        async def ok(event, data):
            return "жив"

        event, data = _event_for(5)
        with pytest.raises(RuntimeError):
            await mw(boom, event, data)

        assert mw._locks == {}
        assert await asyncio.wait_for(mw(ok, event, data), timeout=1) == "жив"
