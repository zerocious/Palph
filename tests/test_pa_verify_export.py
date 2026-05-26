"""
Tests for scripts/pa_verify_export.py — PA prelaunch verification.
"""
import io
import os
import tempfile
import zipfile
from datetime import datetime, timedelta

from db import get_db, init_db
from scripts.pa_verify_export import run_checks
from services import AnalyticsService


async def _seed_pa_data(db):
    """Minimal dataset for verify script checks."""
    now = datetime.now()
    ts = now.strftime("%Y-%m-%d %H:%M:%S")
    ts_old = (now - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")

    await db.execute(
        "INSERT INTO users (user_id, created_at, total_sessions) VALUES (?, ?, 2)",
        (101, ts_old),
    )
    await db.execute(
        "INSERT INTO users (user_id, created_at, total_sessions) VALUES (?, ?, 0)",
        (102, ts),
    )
    await db.execute(
        "INSERT INTO study_sessions (user_id, duration_minutes, coins_earned, created_at) "
        "VALUES (?, 25, 25, ?)",
        (101, ts_old),
    )
    for en in ("user_registered", "session_started", "subject_picked", "tip_viewed"):
        await db.execute(
            "INSERT INTO events (user_id, event_name, properties, created_at) "
            "VALUES (?, ?, '{}', ?)",
            (101, en, ts_old),
        )
    await db.commit()


async def _temp_db_with_seed():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = await get_db(path)
    await init_db(db)
    await _seed_pa_data(db)
    await db.close()
    return path


class TestPaVerifyExport:
    async def test_run_checks_passes_on_seeded_db(self):
        path = await _temp_db_with_seed()
        try:
            result, _analytics, zip_bytes, metadata = await run_checks(path)
            assert result.success
            assert zip_bytes is not None
            assert zipfile.is_zipfile(io.BytesIO(zip_bytes))
            assert metadata["schema_version"] == "v0.8"
            assert metadata["row_counts"]["users"] == 2
        finally:
            for ext in ("", "-wal", "-shm"):
                p = path + ext
                if os.path.exists(p):
                    os.remove(p)

    async def test_export_contains_all_tables(self, db):
        await _seed_pa_data(db)
        analytics = AnalyticsService(db)
        zip_bytes, metadata = await analytics.export_all_tables_zip()
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = set(zf.namelist())
        expected = {f"{t}.csv" for t in AnalyticsService.EXPORTABLE_TABLES.values()}
        assert expected.issubset(names)
        assert "metadata.json" in names

    async def test_critical_events_warning_when_missing(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            db = await get_db(path)
            await init_db(db)
            await db.execute("INSERT INTO users (user_id) VALUES (1)")
            await db.execute(
                "INSERT INTO events (user_id, event_name, properties) "
                "VALUES (1, 'user_registered', '{}')",
            )
            await db.commit()
            await db.close()

            result, _, _, _ = await run_checks(path)
            assert result.success
            assert any("Events not yet seen" in w for w in result.warnings)
        finally:
            for ext in ("", "-wal", "-shm"):
                p = path + ext
                if os.path.exists(p):
                    os.remove(p)
