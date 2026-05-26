"""
Load exported Palph datasets (ZIP from /export all or individual CSVs).

Usage in notebooks:
    from analysis.lib.pa_loaders import load_export_zip, find_latest_export

    users, events, meta = load_export_zip("analysis/exports/week-2026-W21/export.zip")
"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
EXPORTS_DIR = REPO_ROOT / "analysis" / "exports"


def find_latest_export(subdir: str | None = None) -> Path | None:
    """Return newest .zip under analysis/exports/ (optionally in subdir)."""
    base = EXPORTS_DIR / subdir if subdir else EXPORTS_DIR
    if not base.exists():
        return None
    zips = sorted(base.rglob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    return zips[0] if zips else None


def load_export_zip(
    zip_path: str | Path,
) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """
    Load users.csv, events.csv, metadata.json from export ZIP.

    Returns: (users_df, events_df, metadata_dict)
    """
    zip_path = Path(zip_path)
    with zipfile.ZipFile(zip_path) as zf:
        meta = json.loads(zf.read("metadata.json").decode("utf-8"))
        users = pd.read_csv(io.BytesIO(zf.read("users.csv")))
        events = pd.read_csv(io.BytesIO(zf.read("events.csv")))
    return users, events, meta


def load_table_from_zip(zip_path: str | Path, table_name: str) -> pd.DataFrame:
    """Load any table CSV from export ZIP by table name (e.g. 'study_sessions')."""
    csv_name = f"{table_name}.csv"
    with zipfile.ZipFile(zip_path) as zf:
        return pd.read_csv(io.BytesIO(zf.read(csv_name)))


def load_sqlite(db_path: str | Path | None = None) -> dict[str, pd.DataFrame]:
    """
    Load core tables directly from SQLite (fallback when no export yet).

    Requires pandas + sqlite3.
    """
    import sqlite3

    db_path = Path(db_path or REPO_ROOT / "studybuddy.db")
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")

    conn = sqlite3.connect(db_path)
    tables = ["users", "events", "study_sessions", "quiz_progress", "flashcard_progress"]
    result = {}
    for t in tables:
        try:
            result[t] = pd.read_sql_query(f"SELECT * FROM {t}", conn)
        except Exception:
            result[t] = pd.DataFrame()
    conn.close()
    return result
