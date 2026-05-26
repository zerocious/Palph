#!/usr/bin/env python3
"""
Verify Palph PA analytics infrastructure before/after launch.

Checks:
  - DB connectivity and table presence
  - AnalyticsService methods return valid data
  - Export ZIP is valid and contains all tables
  - Critical event names are documented

Usage:
  python scripts/pa_verify_export.py
  python scripts/pa_verify_export.py --db path/to/studybuddy.db
  python scripts/pa_verify_export.py --save-baseline
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from db import DB_PATH, get_db, init_db
from services import AnalyticsService

# Events expected after a full test-user flow (see export_checklist.md)
CRITICAL_EVENTS = frozenset({
    "user_registered",
    "session_started",
    "session_completed",
    "subject_picked",
    "mode_picked",
    "quiz_answered",
    "flashcard_reviewed",
    "mcq_answered",
    "task_attempted",
    "tip_viewed",
    "achievement_unlocked",
    "leaderboard_viewed",
    "reminder_sent",
})


class CheckResult:
    def __init__(self):
        self.passed: list[str] = []
        self.warnings: list[str] = []
        self.failed: list[str] = []

    def ok(self, msg: str) -> None:
        self.passed.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def fail(self, msg: str) -> None:
        self.failed.append(msg)

    @property
    def success(self) -> bool:
        return len(self.failed) == 0


async def _count(db, sql: str, params=()) -> int:
    async with db.execute(sql, params) as c:
        row = await c.fetchone()
        return row[0] if row else 0


async def run_checks(db_path: str) -> tuple[CheckResult, AnalyticsService, bytes | None, dict | None]:
    result = CheckResult()
    db = await get_db(db_path)
    await init_db(db)
    analytics = AnalyticsService(db)

    # --- DB basics ---
    user_count = await _count(db, "SELECT COUNT(*) FROM users")
    event_count = await _count(db, "SELECT COUNT(*) FROM events")
    result.ok(f"DB connected: {db_path}")
    result.ok(f"users={user_count}, events={event_count}")

    if user_count == 0:
        result.warn("No users yet — retention/funnel metrics will be empty (OK for prelaunch)")

    # --- Analytics methods ---
    methods = [
        ("compute_cohort_retention", analytics.compute_cohort_retention()),
        ("compute_funnel", analytics.compute_funnel()),
        ("compute_engagement", analytics.compute_engagement()),
        ("compute_feature_usage", analytics.compute_feature_usage()),
        ("compute_activation_metrics", analytics.compute_activation_metrics()),
        ("compute_product_metrics", analytics.compute_product_metrics()),
        ("compute_segments", analytics.compute_segments()),
        ("compute_content_stats", analytics.compute_content_stats()),
        ("compute_event_timeline", analytics.compute_event_timeline(hours=24)),
        ("compute_heatmap", analytics.compute_heatmap(days=7)),
    ]
    for name, coro in methods:
        try:
            data = await coro
            if name == "compute_event_timeline":
                if not isinstance(data, list):
                    result.fail(f"{name}: expected list, got {type(data)}")
                else:
                    result.ok(f"{name}() OK ({len(data)} events)")
            elif not isinstance(data, dict):
                result.fail(f"{name}: expected dict, got {type(data)}")
            else:
                result.ok(f"{name}() OK")
        except Exception as e:
            result.fail(f"{name}(): {type(e).__name__}: {e}")

    # --- Event coverage ---
    async with db.execute(
        "SELECT DISTINCT event_name FROM events ORDER BY event_name"
    ) as c:
        found = {row[0] for row in await c.fetchall()}

    if found:
        missing_critical = CRITICAL_EVENTS - found
        if missing_critical:
            result.warn(
                f"Events not yet seen (run test-user flow): {', '.join(sorted(missing_critical))}"
            )
        else:
            result.ok("All critical event types present in events table")
        result.ok(f"Distinct event_names: {len(found)}")
    else:
        result.warn("events table empty — run test-user flow before launch")

    # --- Export ZIP ---
    zip_bytes: bytes | None = None
    metadata: dict | None = None
    try:
        zip_bytes, metadata = await analytics.export_all_tables_zip()
        if not zipfile.is_zipfile(io.BytesIO(zip_bytes)):
            result.fail("export_all_tables_zip: invalid ZIP")
        else:
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                names = set(zf.namelist())
            expected = {f"{t}.csv" for t in AnalyticsService.EXPORTABLE_TABLES.values()}
            expected.add("metadata.json")
            missing = expected - names
            if missing:
                result.fail(f"ZIP missing files: {missing}")
            else:
                result.ok(f"Export ZIP valid: {len(names)} files, {len(zip_bytes)} bytes")
            if metadata and "schema_version" in metadata and "row_counts" in metadata:
                result.ok(f"metadata schema_version={metadata['schema_version']}")
            else:
                result.fail("metadata.json missing required fields")
    except Exception as e:
        result.fail(f"export_all_tables_zip(): {type(e).__name__}: {e}")

    await db.close()
    return result, analytics, zip_bytes, metadata


def format_report(result: CheckResult) -> str:
    lines = ["=" * 60, "Palph PA Export Verification Report", "=" * 60, ""]
    if result.passed:
        lines.append("PASSED:")
        for p in result.passed:
            lines.append(f"  [OK] {p}")
        lines.append("")
    if result.warnings:
        lines.append("WARNINGS:")
        for w in result.warnings:
            lines.append(f"  [WARN] {w}")
        lines.append("")
    if result.failed:
        lines.append("FAILED:")
        for f in result.failed:
            lines.append(f"  [FAIL] {f}")
        lines.append("")
    status = "ALL CHECKS PASSED" if result.success else "CHECKS FAILED"
    lines.append(status)
    lines.append(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    return "\n".join(lines)


def save_baseline(db_path: str, zip_bytes: bytes, metadata: dict, report: str) -> Path:
    out_dir = REPO_ROOT / "analysis" / "exports" / "prelaunch"
    out_dir.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    zip_path = out_dir / f"export_{date_str}.zip"
    zip_path.write_bytes(zip_bytes)
    (out_dir / "verify_report.txt").write_text(report, encoding="utf-8")
    (out_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return zip_path


async def main() -> int:
    parser = argparse.ArgumentParser(description="Verify Palph PA analytics export")
    parser.add_argument("--db", default=DB_PATH, help="Path to SQLite database")
    parser.add_argument("--save-baseline", action="store_true", help="Save prelaunch export snapshot")
    args = parser.parse_args()

    if not Path(args.db).exists():
        print(f"ERROR: Database not found: {args.db}", file=sys.stderr)
        return 1

    result, _analytics, zip_bytes, metadata = await run_checks(args.db)
    report = format_report(result)
    print(report)

    if args.save_baseline and zip_bytes and metadata and result.success:
        path = save_baseline(args.db, zip_bytes, metadata, report)
        print(f"\nBaseline saved: {path}")

    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
