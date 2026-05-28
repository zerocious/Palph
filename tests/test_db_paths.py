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

    assert base.is_dir()
    assert (base / "backups").is_dir()


@pytest.mark.asyncio
async def test_ensure_persistent_dirs_preserves_existing_db_on_restart(tmp_path, monkeypatch):
    """Simulate bot restart: dirs helper + get_db + init_db must not wipe data."""
    base = tmp_path / "app" / "data"
    db_path = base / "studybuddy.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("LOG_FILE", str(base / "bot.log"))
    monkeypatch.setenv("BACKUP_DIR", str(base / "backups"))

    ensure_persistent_dirs()
    db = await get_db(str(db_path))
    try:
        await init_db(db)
        await db.execute(
            "INSERT INTO users (user_id, total_coins) VALUES (?, ?)",
            (42, 999),
        )
        await db.commit()
    finally:
        await db.close()

    mtime_before = db_path.stat().st_mtime
    size_before = db_path.stat().st_size

    ensure_persistent_dirs()
    ensure_persistent_dirs()
    db = await get_db(str(db_path))
    try:
        await init_db(db)
        row = await db.execute_fetchall(
            "SELECT user_id, total_coins FROM users WHERE user_id = ?",
            (42,),
        )
    finally:
        await db.close()

    assert db_path.exists()
    assert db_path.stat().st_size >= size_before
    assert db_path.stat().st_mtime >= mtime_before
    assert len(row) == 1
    assert row[0]["user_id"] == 42
    assert row[0]["total_coins"] == 999


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
