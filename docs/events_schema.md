# Events table schema

The `events` table is an append-only log of every significant action in the
bot. It's the foundation for funnel, cohort, path, and time-to-action
analysis in pandas / Jupyter / SQL. **One row = one significant action.**
Never `UPDATE` or `DELETE`.

## Storage

```sql
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,                          -- nullable: system events
    event_name TEXT NOT NULL,
    properties TEXT NOT NULL DEFAULT '{}',    -- JSON-encoded dict
    created_at TEXT NOT NULL DEFAULT (datetime('now'))  -- UTC
);
CREATE INDEX idx_events_user_time ON events(user_id, created_at);
CREATE INDEX idx_events_name_time ON events(event_name, created_at);
```

- `properties` is a JSON dict; `EventRepository.log` uses
  `json.dumps(props, ensure_ascii=False)` so Cyrillic is stored readably.
- `created_at` is **UTC** (SQLite `datetime('now')`). Convert to user TZ
  at analysis time when needed.
- `EventRepository.log` swallows all errors with a `events.log_failed`
  warning in `bot.log` — analytics must never break the bot.

## Reading

| Surface | What it shows |
|---------|---------------|
| `/event_timeline [hours]` | Last N hours of events (default 24, clamped 1–168). Admin only. |
| `/export events` | CSV dump of the table as a Telegram document. |
| `/export all` | ZIP of all 11 tables incl. `events.csv` + `metadata.json`. |
| `parse_logs.py` | Historical backfill — reconstructs events from `bot.log` for periods before the `events` table existed. |
| `AnalyticsService` | Aggregates: `/cohort_stats`, `/funnel`, `/dau`, etc. |

## Naming convention

Two naming styles coexist intentionally:

- **`snake_case`** — user-action events (e.g., `session_completed`,
  `mcq_answered`). Verbs describe what the user did.
- **`namespace.verb`** — meta / system events (e.g., `system.deploy`,
  `experiment.assigned`). Dot-separated to mark them as "not directly
  user-initiated."

When adding a new event, follow whichever convention matches the kind.

## Event catalog

> Updated 2026-05-21. Drift between this table and the actual
> `event_repo.log(...)` call sites is caught by
> `tests/test_events_schema_doc.py` — any new `event_name` must appear
> here or be added to the whitelist with a TODO.

### User-action events (`snake_case`)

#### `user_registered`

**When fires:** First `/start` command from a new user — inside
`cmd_start`, immediately after `user_repo.create_user`.

**Properties**

| Key | Required | Type | Notes |
|-----|----------|------|-------|
| `language_code` | optional | string \| null | Telegram-provided ISO code (e.g., `"ru"`); may be `null` if user has none set. |

**Example**

```json
{"language_code": "ru"}
```

---

#### `session_started`

**When fires:** User launches a Pomodoro timer — either the default
25-min preset or a custom duration after `process_duration`.

**Properties**

| Key | Required | Type | Notes |
|-----|----------|------|-------|
| `duration` | required | int | Planned timer length in minutes (5–120). |
| `kind` | required | enum | `"standard"` (25-min preset) or `"custom"`. |

**Example**

```json
{"duration": 25, "kind": "standard"}
```

---

#### `session_completed`

**When fires:** Pomodoro session ends. Three sources, distinguished by
`source`:
- `"natural"` — timer counted down to zero on its own.
- `"stop"` — user pressed «⏹️ Остановить» or `/stop` before natural end.
- `"reconcile"` — bot was offline past the planned end-time; computed at
  startup in `reconcile_stale_timers`.

**Properties**

| Key | Required | Type | Notes |
|-----|----------|------|-------|
| `duration` | required | int | Actual studied minutes (≤ planned). |
| `coins` | required | int | Coins earned (= `duration` minus rounding). |
| `bonus_coins` | required | int | Achievement bonus, if any. |
| `session_id` | required | int | FK to `study_sessions.id`. |
| `achievements_earned` | required | int | Count of new achievements. |
| `source` | required | enum | `"natural"` \| `"stop"` \| `"reconcile"`. |

**Example**

```json
{
  "duration": 25, "coins": 25, "bonus_coins": 5,
  "session_id": 1042, "achievements_earned": 1, "source": "natural"
}
```

---

#### `achievement_unlocked`

**When fires:** Inside `complete_session`, once per newly-earned
achievement. Always follows a `session_completed` for the same `user_id`
within the same flow.

**Properties**

| Key | Required | Type | Notes |
|-----|----------|------|-------|
| `achievement_id` | required | string | Stable ID from `achievements.json`. |

**Example**

```json
{"achievement_id": "first_session"}
```

---

#### `mode_picked`

**When fires:** User taps one of the four study-mode buttons (situational,
flashcards, MCQ, tasks) inside the `❓ Квизы` submenu.

**Properties**

| Key | Required | Type | Notes |
|-----|----------|------|-------|
| `mode_id` | required | enum | `"situational"` \| `"flashcards"` \| `"mcq"` \| `"tasks"`. |

**Example**

```json
{"mode_id": "flashcards"}
```

---

#### `subject_picked`

**When fires:** After `mode_picked`, user taps a subject from the
mode-specific subject keyboard.

**Properties**

| Key | Required | Type | Notes |
|-----|----------|------|-------|
| `mode_id` | required | string | Same enum as `mode_picked`. |
| `subject_id` | required | string | E.g., `"Математика"`, `"industrial-management"`. |

**Example**

```json
{"mode_id": "mcq", "subject_id": "Математика"}
```

---

#### `mcq_answered`

**When fires:** User selects an answer button on an MCQ question.

**Properties**

| Key | Required | Type | Notes |
|-----|----------|------|-------|
| `subject_id` | required | string | Subject the question belongs to. |
| `question_hash` | required | string | SHA-based stable ID of the question text. |
| `is_correct` | required | bool | |
| `question_index` | required | int | 0-based position in the current session. |

**Example**

```json
{
  "subject_id": "Математика", "question_hash": "a1b2c3...",
  "is_correct": true, "question_index": 2
}
```

---

#### `task_attempted`

**When fires:** User submits an answer to a math task. May fire multiple
times per task (up to 3 attempts). The `succeeded=true` event is the
final attempt; subsequent attempts on the same task are blocked.

**Properties**

| Key | Required | Type | Notes |
|-----|----------|------|-------|
| `subject_id` | required | string | |
| `task_id` | required | string | Stable ID from `tasks/*.json`. |
| `attempts_used` | required | int | 1–3. |
| `succeeded` | required | bool | `true` = correct, `false` = exhausted attempts. |
| `coins` | required | int | Awarded coins (0 if `succeeded=false`). |

**Example**

```json
{
  "subject_id": "Математика", "task_id": "M-fraction-01",
  "attempts_used": 2, "succeeded": true, "coins": 8
}
```

---

#### `flashcard_reviewed`

**When fires:** User rates a flashcard via SM-2 quality button
(`❌ / 😐 / ✅`). Updates `flashcard_progress` and grants pts on success.

**Properties**

| Key | Required | Type | Notes |
|-----|----------|------|-------|
| `subject_id` | required | string | |
| `card_hash` | required | string | SHA-based stable ID. |
| `quality` | required | int | SM-2 quality 1, 3, or 5. |
| `is_new` | required | bool | `true` = first review. |
| `reps_before` | required | int | SM-2 repetitions before. |
| `reps_after` | required | int | SM-2 repetitions after. |
| `ef_before` | required | float | Ease-factor before (rounded to 3 places). |
| `ef_after` | required | float | Ease-factor after. |
| `interval_before` | required | int | Interval in days, before. |
| `interval_after` | required | int | Interval in days, after. |
| `next_review` | required | string | ISO date for next scheduled review. |

**Example**

```json
{
  "subject_id": "english", "card_hash": "d4e5f6...",
  "quality": 5, "is_new": false,
  "reps_before": 3, "reps_after": 4,
  "ef_before": 2.5, "ef_after": 2.6,
  "interval_before": 6, "interval_after": 15,
  "next_review": "2026-06-05"
}
```

---

#### `quiz_answered`

**When fires:** User submits a typed answer in a situational quiz.
Keyword-grader decides correctness.

**Properties**

| Key | Required | Type | Notes |
|-----|----------|------|-------|
| `subject_id` | required | string | Defaults to `"industrial-management"`. |
| `section` | required | string | Section ID (`"i"`, `"ii"`, …). |
| `term_hash` | required | string | SHA-based stable ID of the term. |
| `is_correct` | required | bool | |
| `streak_after` | required | int | Consecutive-correct count after this answer. |

**Example**

```json
{
  "subject_id": "industrial-management", "section": "i",
  "term_hash": "abc...", "is_correct": false, "streak_after": 0
}
```

### Meta / system events (`namespace.verb`)

#### `system.deploy`

**When fires:** Once per bot startup, right after admins are loaded and
the `app.start` log line. `user_id` is `NULL` (system-level event).

Used as a marker overlay on retention / engagement curves to attribute
metric changes to specific releases: «D7 dropped 5pp after 2026-05-15 —
what shipped?» (PA-roadmap #6).

**Properties**

| Key | Required | Type | Notes |
|-----|----------|------|-------|
| `version` | required | string | From `BOT_VERSION` env var, falling back to `git rev-parse --short HEAD`, falling back to `"unknown"`. See `services.resolve_deploy_version`. |
| `started_at_utc` | required | string | ISO-8601 UTC with `Z` suffix, e.g., `"2026-05-21T19:23:45Z"`. |
| `python_version` | required | string | `major.minor.patch`, e.g., `"3.14.4"`. |

**Example**

```json
{
  "version": "ba42532",
  "started_at_utc": "2026-05-21T19:23:45Z",
  "python_version": "3.14.4"
}
```

---

#### `experiment.assigned`

**When fires:** First time a given `(user_id, experiment_name)` is
resolved by `services.get_variant`. Subsequent calls hit the
`experiments` cache and **do not** fire again. (PA-roadmap #1, PR #6.)

**Properties**

| Key | Required | Type | Notes |
|-----|----------|------|-------|
| `experiment` | required | string | Key from `services.EXPERIMENTS` registry. |
| `variant` | required | string | One of the values in `EXPERIMENTS[experiment]`. |

**Example**

```json
{"experiment": "pet_level_in_profile_v1", "variant": "treatment"}
```

## Adding a new event

1. Pick the name following the convention (snake_case for user actions,
   namespace.verb for meta).
2. `await event_repo.log(user_id, "<name>", {…properties…})` at the
   firing point. `user_id=None` only for system-level events.
3. Add a new subsection to this file with the same structure as above.
4. Run `python -m pytest tests/test_events_schema_doc.py` — the drift
   test fails until your event is documented.
