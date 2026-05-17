"""
pytest fixtures, общие для всех тестов.

Каждый тест получает свежую SQLite-БД (в tempfile), полностью изолированную.
Связь с aiosqlite требует event loop — pytest-asyncio в режиме `asyncio_mode = auto`
оборачивает все async-fixtures и async-тесты автоматически.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

import pytest
import pytest_asyncio

# Делаем корень проекта импортируемым (без устанавливания пакета)
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from db import get_db, init_db  # noqa: E402
from repository import UserRepository, SessionRepository  # noqa: E402


@pytest_asyncio.fixture
async def db():
    """Свежая SQLite-БД на каждый тест. Удаляется после."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        conn = await get_db(path)
        await init_db(conn)
        yield conn
        await conn.close()
    finally:
        for ext in ("", "-wal", "-shm"):
            p = path + ext
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass


@pytest_asyncio.fixture
async def user_repo(db):
    return UserRepository(db)


@pytest_asyncio.fixture
async def session_repo(db):
    return SessionRepository(db)


@pytest_asyncio.fixture
async def created_user(user_repo) -> int:
    """Готовый пользователь с дефолтными настройками. Возвращает user_id."""
    uid = 42
    await user_repo.create_user(uid)
    return uid


@pytest.fixture(scope="session")
def achievements_catalog() -> dict:
    """Реальный achievements.json из корня — чтобы тесты не дрейфовали от продакшна."""
    path = os.path.join(_ROOT, "achievements.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
