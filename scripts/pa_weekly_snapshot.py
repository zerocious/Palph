#!/usr/bin/env python3
"""
Weekly PA snapshot: export all tables + generate markdown summary.

Usage:
  python scripts/pa_weekly_snapshot.py
  python scripts/pa_weekly_snapshot.py --week 1
  python scripts/pa_weekly_snapshot.py --db studybuddy.db
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from db import DB_PATH, get_db, init_db
from services import AnalyticsService


def _pct(v: float | None) -> str:
    return f"{v * 100:.1f}%" if v is not None else "n/a"


def _hours(v: float | None) -> str:
    return f"{v:.1f}h" if v is not None else "n/a"


async def build_summary(analytics: AnalyticsService) -> str:
    cohort = await analytics.compute_cohort_retention()
    funnel = await analytics.compute_funnel()
    engagement = await analytics.compute_engagement()
    activation = await analytics.compute_activation_metrics()
    features = await analytics.compute_feature_usage()
    segments = await analytics.compute_segments()
    product = await analytics.compute_product_metrics()

    lines = [
        f"# Weekly PA Summary",
        f"",
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        f"",
        f"## Engagement",
        f"- Total users: {engagement.get('total_users', 0)}",
        f"- New today: {engagement.get('new_today', 0)}",
        f"- DAU: {engagement.get('dau', 0)} | WAU: {engagement.get('wau', 0)} | MAU: {engagement.get('mau', 0)}",
        f"- Stickiness (DAU/MAU): {_pct(engagement.get('stickiness'))}",
        f"",
        f"## Activation",
        f"- First session within 24h: {_pct(activation.get('pct_first_session_within_24h'))}",
        f"- First session within 7d: {_pct(activation.get('pct_first_session_within_7d'))}",
        f"- Users with first session: {activation.get('users_with_first_session', 0)}",
        f"",
    ]

    tth = activation.get("time_to_hours", {})
    if tth:
        lines.append("### Time-to-value (median hours)")
        for en, stats in tth.items():
            lines.append(f"- {en}: {_hours(stats.get('median'))} (n={stats.get('n', 0)})")
        lines.append("")

    lines.append("## Funnel (top steps)")
    for step in funnel.get("event_steps", [])[:6]:
        lines.append(f"- {step['name']}: {step['count']} ({_pct(step['pct'])})")
    lines.append("")

    lines.append("## Retention (latest cohorts)")
    for c in cohort.get("cohorts", [])[-3:]:
        lines.append(
            f"- {c['week']} (n={c['size']}): D1={_pct(c['d1'])}, D7={_pct(c['d7'])}, D30={_pct(c['d30'])}"
        )
    lines.append("")

    lines.append("## Feature adoption (top 5)")
    for f in features.get("features", [])[:5]:
        lines.append(f"- {f['name']}: {f['count']} ({_pct(f['pct'])})")
    lines.append("")

    lines.append("## Segments")
    for s in segments.get("segments", []):
        lines.append(f"- {s['name']}: {s['count']} ({_pct(s['pct'])})")
    lines.append("")

    by_mode = product.get("by_mode", [])
    if by_mode:
        lines.append("## Mode breakdown")
        for m in by_mode:
            lines.append(f"- {m['mode']}: {m['users']} users ({_pct(m['pct_registered'])})")
        lines.append("")

    lines.extend([
        "## Action items",
        "- [ ] Update analytics_logbook.md",
        "- [ ] Review funnel drop-offs",
        "- [ ] Check acquisition_tracker.md",
        "",
    ])
    return "\n".join(lines)


async def main() -> int:
    parser = argparse.ArgumentParser(description="Weekly PA snapshot")
    parser.add_argument("--db", default=DB_PATH, help="Path to SQLite database")
    parser.add_argument("--week", type=int, help="Week number label (1, 2, 3)")
    args = parser.parse_args()

    if not Path(args.db).exists():
        print(f"ERROR: Database not found: {args.db}", file=sys.stderr)
        return 1

    db = await get_db(args.db)
    await init_db(db)
    analytics = AnalyticsService(db)

    iso = datetime.now().isocalendar()
    week_label = f"week-{iso.year}-W{iso.week:02d}"
    if args.week:
        week_label = f"week-{args.week}-{iso.year}-W{iso.week:02d}"

    out_dir = REPO_ROOT / "analysis" / "exports" / week_label
    out_dir.mkdir(parents=True, exist_ok=True)

    zip_bytes, metadata = await analytics.export_all_tables_zip()
    date_str = datetime.now().strftime("%Y-%m-%d")
    zip_path = out_dir / f"export_{date_str}.zip"
    zip_path.write_bytes(zip_bytes)

    (out_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    summary = await build_summary(analytics)
    summary_path = out_dir / "weekly_summary.md"
    summary_path.write_text(summary, encoding="utf-8")

    await db.close()

    print(f"Snapshot saved: {out_dir}")
    print(f"  - {zip_path.name}")
    print(f"  - weekly_summary.md")
    print(f"  - metadata.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
