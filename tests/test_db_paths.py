import os
from pathlib import Path

import pytest

from db import ensure_persistent_dirs, get_db, init_db


@pytest.mark.asyncio
async def test_ensure_persistent_dirs_creates_nested_paths(tmp_path, monkeypatch):
    base = tmp_path / "app" / "data"
    monkeypatch.setenv("DB_PATH", str(base / "studybuddy.db"))
    monkeypatch.setenv("LOG_FILE", str(base / "bot.log"))
    monkeypatch.setenv("BACKUP_DIR", str(base / "backups"))

    ensure_persistent_dirs()

    assert (base / "backups").is_dir()


@pytest.mark.asyncio
async def test_get_db_creates_parent_and_inits(tmp_path):
    db_path = tmp_path / "nested" / "test.db"
    db = await get_db(str(db_path))
    try:
        await init_db(db)
    finally:
        await db.close()

    assert db_path.parent.is_dir()
    assert db_path.exists()
