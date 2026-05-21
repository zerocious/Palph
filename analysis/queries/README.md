# Reference SQL queries

Standalone executable queries against the Palph SQLite database. Drop them
into DB Browser for SQLite, DataGrip, or `sqlite3` CLI and they should run
unchanged on:

- A live `studybuddy.db` (production / development).
- A fresh schema-only DB produced by `db.init_db()` (used in the smoke test
  in `tests/test_reference_queries.py`).

Empty result sets are valid — most queries are meaningful only against
real data accumulated over weeks. The smoke test only checks that the SQL
**parses** and **executes** against the current schema.

## Running

```bash
# Against the live DB (path from .env):
sqlite3 studybuddy.db < analysis/queries/01_cohort_retention.sql

# Against /export all bundle (after unzipping the CSVs):
# Re-create a SQLite DB from the CSVs first — see analysis/README.md
# for the import script (planned: PA-roadmap #9 helper).

# All queries in one shot, separated by '---':
for f in analysis/queries/*.sql; do
  echo "=== $f ===";
  sqlite3 studybuddy.db < "$f";
done
```

## Files

| # | File | Asks |
|---|------|------|
| 01 | `01_cohort_retention.sql` | What share of users from each signup ISO week are still active D1/D7/D30 later? |
| 02 | `02_activation_funnel.sql` | How many users pass each step from registration → first session → 5+ sessions → 3-day streak → 7-day streak? |
| 03 | `03_rfm_segmentation.sql` | Recency × Frequency × Monetary (coins) — classic five-bucket segmentation over the coin economy. |
| 04 | `04_feature_adoption_by_cohort.sql` | What's the per-feature adoption rate inside each weekly cohort? Reveals if newer cohorts onboard differently. |
| 05 | `05_session_length_distribution.sql` | Pomodoro `duration_minutes` distribution — counts per bucket + p25/p50/p75. |
| 06 | `06_math_specific_funnel.sql` | Math (`Математика`) subject funnel: visited → answered an MCQ → answered correctly → completed a task. Mission-critical metric. |
| 07 | `07_churn_predictors.sql` | Per-user signal table: first-week activity vs whether the user is still active by D30. Feeds into a churn-correlation notebook. |
| 08 | `08_pre_exam_engagement.sql` | **Stub.** Blocked on PA-roadmap #2 (`users.exam_date`); has a TODO header explaining the gap. |

## Conventions

- **Comments at the top** of each file: purpose, parameters (with defaults),
  output shape. Keep these in sync if the query is edited.
- **SQLite-only syntax**: `strftime('%Y-W%W', ...)`, `julianday(...)`,
  `json_extract(properties, '$.key')`. Don't reach for window functions
  beyond `ROW_NUMBER() / RANK() / PERCENT_RANK()` — older SQLite builds
  miss them.
- **Date arithmetic**: `events.created_at` is UTC (SQLite `datetime('now')`).
  Cross-timezone retention is approximate at day boundaries — acceptable
  for cohort-level metrics.
- **No mutating statements.** Reference set is read-only.

## Adding a query

1. Pick a question worth answering with one file.
2. Write the SQL with a header comment block (`-- Purpose: …`).
3. Add a row to the table above.
4. Run the smoke test: `python -m pytest tests/test_reference_queries.py`.
