"""
Тесты BackupService — ежедневный snapshot БД через SQLite VACUUM INTO.

Каждый тест получает свежую src-БД (через стандартную db fixture) +
изолированную backup-папку (через tmp_path).
"""
import asyncio
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest
import pytest_asyncio

from services import BackupService


@pytest_asyncio.fixture
async def backup_service(db, tmp_path) -> BackupService:
    """
    BackupService с реальной src-БД (из db fixture) и tmp_path как backup_dir.
    db.execute("PRAGMA database_list") даёт путь — берём оттуда.
    """
    async with db.execute("PRAGMA database_list") as cursor:
        row = await cursor.fetchone()
    db_path = row["file"]
    return BackupService(
        db_path=db_path,
        backup_dir=str(tmp_path / "backups"),
        retention_days=30,
    )


class TestMaybeBackupForToday:
    async def test_creates_file_on_first_call(self, backup_service):
        result = await backup_service.maybe_backup_for_today()
        assert result is not None
        assert result.exists()
        assert result.stat().st_size > 0
        today = datetime.now().strftime("%Y-%m-%d")
        assert today in result.name

    async def test_dedup_returns_none_second_call(self, backup_service):
        first = await backup_service.maybe_backup_for_today()
        assert first is not None
        second = await backup_service.maybe_backup_for_today()
        assert second is None, "second call same day should not create new file"

    async def test_dedup_via_file_existence_after_restart(self, backup_service):
        """После рестарта _last_backup_date снова None, но файл есть — skip."""
        first = await backup_service.maybe_backup_for_today()
        assert first is not None
        # Симулируем рестарт: новый сервис с тем же backup_dir
        new_service = BackupService(
            db_path=backup_service.db_path,
            backup_dir=str(backup_service.backup_dir),
            retention_days=backup_service.retention_days,
        )
        result = await new_service.maybe_backup_for_today()
        assert result is None, "after restart, file-existence check should dedup"


class TestForceBackup:
    async def test_creates_manual_file(self, backup_service):
        result = await backup_service.force_backup()
        assert result is not None
        assert result.exists()
        assert "manual" in result.name

    async def test_two_force_backups_dont_collide(self, backup_service):
        first = await backup_service.force_backup()
        # Wait at least 1 second so timestamp differs (suffix has %H%M%S)
        await asyncio.sleep(1.1)
        second = await backup_service.force_backup()
        assert first != second
        assert first.exists()
        assert second.exists()


class TestBackupFileIsValidSqlite:
    async def test_backup_can_be_opened_as_sqlite(self, backup_service, db, created_user):
        # Add some real data to src DB so backup has content
        await db.execute(
            "INSERT INTO study_sessions (user_id, duration_minutes, coins_earned) "
            "VALUES (?, 25, 25)",
            (created_user,),
        )
        await db.commit()

        backup_path = await backup_service.maybe_backup_for_today()
        assert backup_path is not None

        # Open backup as a fresh SQLite DB (synchronous sqlite3 for simplicity)
        conn = sqlite3.connect(backup_path)
        try:
            cur = conn.execute("SELECT COUNT(*) FROM users")
            assert cur.fetchone()[0] >= 1
            cur = conn.execute("SELECT user_id, duration_minutes FROM study_sessions")
            rows = cur.fetchall()
            assert len(rows) >= 1
            assert rows[0][0] == created_user
            assert rows[0][1] == 25
        finally:
            conn.close()


class TestCleanupOldBackups:
    async def test_removes_files_older_than_retention(self, backup_service):
        backup_dir = backup_service.backup_dir
        backup_dir.mkdir(parents=True, exist_ok=True)
        # Create a fake old backup (50 days ago)
        old_date = (datetime.now() - timedelta(days=50)).strftime("%Y-%m-%d")
        old_file = backup_dir / f"studybuddy-{old_date}.db"
        old_file.write_bytes(b"dummy sqlite header would be here")
        assert old_file.exists()
        # Run cleanup
        await backup_service._cleanup_old()
        assert not old_file.exists(), "50-day-old file should be removed"

    async def test_keeps_files_within_retention(self, backup_service):
        backup_dir = backup_service.backup_dir
        backup_dir.mkdir(parents=True, exist_ok=True)
        # 5 days old — well within 30-day retention
        recent_date = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")
        recent_file = backup_dir / f"studybuddy-{recent_date}.db"
        recent_file.write_bytes(b"dummy")
        await backup_service._cleanup_old()
        assert recent_file.exists(), "recent file must survive cleanup"

    async def test_manual_backups_never_removed(self, backup_service):
        """Manual snapshot'ы — intentional, не подпадают под daily retention."""
        backup_dir = backup_service.backup_dir
        backup_dir.mkdir(parents=True, exist_ok=True)
        # Create a "very old" manual backup
        old_manual = backup_dir / "studybuddy-manual-2020-01-01-120000.db"
        old_manual.write_bytes(b"dummy")
        await backup_service._cleanup_old()
        assert old_manual.exists(), "manual backups must survive cleanup"

    async def test_malformed_filenames_ignored(self, backup_service):
        """Файлы с неразбираемыми датами в имени не должны падать cleanup."""
        backup_dir = backup_service.backup_dir
        backup_dir.mkdir(parents=True, exist_ok=True)
        weird = backup_dir / "studybuddy-not-a-date.db"
        weird.write_bytes(b"x")
        # Should not raise
        await backup_service._cleanup_old()
        assert weird.exists()


class TestEdgeCases:
    async def test_backup_dir_created_if_missing(self, tmp_path, db):
        async with db.execute("PRAGMA database_list") as cursor:
            row = await cursor.fetchone()
        db_path = row["file"]
        # Subdir that doesn't exist yet
        missing_dir = tmp_path / "deep" / "nested" / "backups"
        assert not missing_dir.exists()
        svc = BackupService(db_path=db_path, backup_dir=str(missing_dir))
        result = await svc.maybe_backup_for_today()
        assert result is not None
        assert missing_dir.exists()

    async def test_corrupt_db_logs_but_doesnt_raise(self, tmp_path):
        """Если src БД не SQLite — backup ловит исключение, логирует, возвращает None."""
        bad_db = tmp_path / "garbage.db"
        bad_db.write_bytes(b"this is not a sqlite file " * 100)
        svc = BackupService(
            db_path=str(bad_db),
            backup_dir=str(tmp_path / "backups"),
        )
        # Не должно raise — внутри try/except, just logs
        result = await svc.maybe_backup_for_today()
        assert result is None
