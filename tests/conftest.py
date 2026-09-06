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
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
import pytz

# Делаем корень проекта импортируемым (без устанавливания пакета)
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Test-only fallback: bot.py raises RuntimeError if BOT_TOKEN is unset
# at import time. Тесты, импортирующие из bot.py (например,
# UsernameSyncMiddleware), полагаются на этот fallback. Не переопределяет
# реальный BOT_TOKEN в env, если он уже установлен.
os.environ.setdefault("BOT_TOKEN", "test-token-for-pytest-imports")

from db import get_db, init_db  # noqa: E402
from repository import UserRepository, SessionRepository  # noqa: E402
from services import user_calendar_keys  # noqa: E402


# Дефолт users.timezone — в нём же LeaderboardService считает неделю.
USER_TZ = "Europe/Moscow"


def current_week_anchor() -> datetime:
    """
    Понедельник ТЕКУЩЕЙ недели, середина дня — точка отсчёта для тестов
    лидерборда.

    Зачем: сценарии начисляют очки на этот момент, а render читает неделю
    по реальным часам. Фиксированная дата в константе работала ровно до
    конца своей недели — четыре теста падали с конца мая именно поэтому.

    Часы берём в TZ пользователя, а не машины. Разница с UTC — три часа,
    и в воскресенье после 21:00 UTC начисление уходило бы в одну неделю,
    а рендер смотрел бы уже в следующую: тест падал бы раз в неделю на
    три часа.

    Возвращается наивный datetime — с такими работают репозитории.
    """
    now = datetime.now(pytz.timezone(USER_TZ))
    monday = (now - timedelta(days=now.weekday())).replace(
        hour=14, minute=30, second=0, microsecond=0
    )
    return monday.replace(tzinfo=None)


def current_week_keys() -> tuple[str, str]:
    """(local_date, week_iso) для current_week_anchor().

    Ключи выводим той же функцией, что и продакшн-код: изменение формата
    сломает тесты сразу, а не разойдётся с реальностью незаметно.
    """
    return user_calendar_keys(current_week_anchor())


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
