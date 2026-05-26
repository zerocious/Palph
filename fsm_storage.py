"""SQLite-backed FSM storage for aiogram 3.x.

Replaces aiogram.fsm.storage.memory.MemoryStorage so that in-progress
wizards, quizzes, and timers survive bot restarts. Uses the existing
aiosqlite connection (table `fsm_storage` is created in db.init_db).

JSON serialization with a custom encoder/decoder preserves datetime
values (used for timer start_time).
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import aiosqlite
from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey


class _DateTimeEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, datetime):
            return {"__datetime__": obj.isoformat()}
        return super().default(obj)


def _datetime_hook(obj: dict) -> Any:
    if "__datetime__" in obj:
        return datetime.fromisoformat(obj["__datetime__"])
    return obj


def _dumps(data: dict) -> str:
    return json.dumps(data, cls=_DateTimeEncoder, ensure_ascii=False)


def _loads(s: str) -> dict:
    if not s:
        return {}
    return json.loads(s, object_hook=_datetime_hook)


def _key_str(key: StorageKey) -> str:
    """Сериализуем StorageKey в TEXT-ключ для SQLite."""
    return f"{key.bot_id}:{key.chat_id}:{key.user_id}:{key.thread_id or 0}"


class SQLiteStorage(BaseStorage):
    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def _fetch_state(self, key_str: str) -> str | None:
        async with self.db.execute(
            "SELECT state FROM fsm_storage WHERE key = ?",
            (key_str,),
        ) as cursor:
            row = await cursor.fetchone()
        return row["state"] if row else None

    async def _write_state(self, key_str: str, state_str: str | None) -> None:
        await self.db.execute(
            "INSERT INTO fsm_storage (key, state) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET state = excluded.state",
            (key_str, state_str),
        )
        await self.db.commit()

    async def _fetch_data(self, key_str: str) -> dict[str, Any]:
        async with self.db.execute(
            "SELECT data FROM fsm_storage WHERE key = ?",
            (key_str,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return {}
        return _loads(row["data"])

    async def _write_data(self, key_str: str, data: dict[str, Any]) -> None:
        await self.db.execute(
            "INSERT INTO fsm_storage (key, data) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET data = excluded.data",
            (key_str, _dumps(data or {})),
        )
        await self.db.commit()

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        state_str = state.state if isinstance(state, State) else state
        key_str = _key_str(key)
        async with self.db.lock:
            await self._write_state(key_str, state_str)

    async def get_state(self, key: StorageKey) -> str | None:
        key_str = _key_str(key)
        async with self.db.lock:
            return await self._fetch_state(key_str)

    async def set_data(self, key: StorageKey, data: dict[str, Any]) -> None:
        key_str = _key_str(key)
        async with self.db.lock:
            await self._write_data(key_str, data)

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        key_str = _key_str(key)
        async with self.db.lock:
            return await self._fetch_data(key_str)

    async def update_data(
        self, key: StorageKey, data: dict[str, Any]
    ) -> dict[str, Any]:
        """Атомарный read-modify-write под глобальным db.lock."""
        key_str = _key_str(key)
        async with self.db.lock:
            current = await self._fetch_data(key_str)
            current.update(data)
            await self._write_data(key_str, current)
        return current

    async def close(self) -> None:
        # Соединение управляется приложением, тут ничего не делаем.
        pass
