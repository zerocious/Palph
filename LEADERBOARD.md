# StudyBuddy — Weekly Leaderboard System

Design spec for the weekly leaderboard. Source of truth for implementation
across multiple slices. Edit this file, not the formula in code, when
balancing changes.

Version: v1.1

## Formula

```
weekly_score = (time_pts + task_pts + quiz_pts + card_pts) × streak_multiplier
```

Weekly reset every Monday 00:00 in the user's local timezone.

## Components

### 1. Time (piecewise linear, diminishing returns)

Daily cap: 240 minutes (4 hours). Beyond that, 0 pts. Minimum 5 min/session
for credit.

| Minutes in day | Points per minute |
|---|---:|
| 0–60 | 1.00 |
| 61–120 | 0.75 |
| 121–180 | 0.50 |
| 181–240 | 0.25 |
| 241+ | 0 |

Daily totals summed across the 7 days of the week. Fractional points kept
internally; floored on display.

### 2. Math tasks (the mission lever — higher-math problems with verified numeric/letter answers)

- **40 pts** per correct task.
- **Daily cap: 5 tasks.** Above the cap, 0 pts.
- Wrong answers grant nothing and don't count toward the daily cap.
- Task bank refreshes daily; same problem reusable at 1–2 week interval.

### 3. Quizzes (term/concept questions, all subjects — includes MCQ)

- **5 pts** per correct answer.
- **Daily cap: 25 correct answers.** Above the cap, 0 pts.
- **Series bonus: +15 pts** for every 3 correct answers in a row (3, 6, 9, …).
  One wrong answer resets the running streak. Bonus applies only within the
  daily cap.
- **MCQ counts as quiz** for both pts and series tracking — one unified
  scoring surface, no separate balance to maintain.
- Series counter **resets at midnight in user's local TZ** (clean daily slate,
  matches the daily-cap pattern).

### 4. Flashcards (spaced repetition)

- **+3 pts** per **new** card correctly answered.
- **+5 pts** per **review** card correctly answered.
- **Daily cap: 8 correct reviews/day.** Above the cap, 0 pts.
- **Wrong answers (SM-2 quality < 3) grant nothing and don't count toward
  the daily cap.** Users who fail cards can keep going until they hit
  8 correct.
- **"New" definition:** the (user_id, card_hash) combo has no existing row
  in `flashcard_progress` — first encounter ever. Once the row exists
  (regardless of `repetitions` value, including 0 after a reset),
  subsequent correct answers are **reviews**.

### 5. Streak multiplier (rewards consistency, applied to weekly total)

| Streak length | Multiplier |
|---|---:|
| 0–2 days | ×1.00 |
| 3–6 days | ×1.05 |
| 7–13 days | ×1.10 |
| 14+ days | ×1.20 |

Streak counted by user's local timezone. Reset to 0 on missed day unless
frozen.

Multiplier applied **at read-time** using the user's current streak. For
weekly rollover snapshots (badge/reward awarding), uses the streak value at
the moment of rollover (Monday 00:00 user-local).

## Streak Freeze

Lets you skip one day without losing the streak. Coins-gated; activated in
profile; one freeze per 7 days max.

| Streak length when freezing | Cost |
|---|---:|
| ≤ 7 days | 500 coins |
| 8–20 days | 750 coins |
| 21+ days | 1000 coins |

Must be activated *before* the missed day rolls over locally. Two
consecutive missed days = unfreezable.

## Leaderboard Segments

System auto-routes the user — no extra buttons:

- **Newbie** — `registered < 7 days ago`. Sees only other newbies.
- **Main** — everyone else.

Separate **Friends** tab. Mutual-confirmation add via Telegram ID or
username. Friends list shows comparative weekly scores.

## Privacy Opt-out

Single toggle in the settings menu: **"👤 Скрыть из лидербордов"**.

- Backed by `users.hidden_from_leaderboards INTEGER NOT NULL DEFAULT 0`.
- All public leaderboard queries filter `WHERE hidden_from_leaderboards = 0`.
- Hidden users still see their own score and rank in their profile, marked
  "Вы скрыты" — so they know the toggle is working.
- **Score accumulation continues normally** — hidden ≠ paused.
- Hidden users **remain eligible** for badges and rollover coin bonuses
  (they earned them; only the public display is suppressed).
- Friends-tab visibility is unaffected — friends opt into each other
  explicitly via mutual confirmation, so the privacy boundary is different.

## Anti-Abuse Summary

- **Time:** 5-min minimum per session for credit; daily cap 240 min;
  diminishing returns.
- **Tasks:** daily cap 5 correct; daily bank refresh; 1–2 week reuse
  interval; wrong answers don't grant pts or burn cap.
- **Quizzes:** daily cap 25 correct; series bonus requires zero errors
  (no farming); wrong answers reset the running series.
- **Cards:** daily cap 8 correct reviews; only spaced-rep-approved cards
  count; wrong answers don't grant pts or burn cap.
- **Streak freeze:** coins-gated, scaled cost, max once per 7 days.

## Rewards

- **Top-3 main leaderboard:** unique 1-week badge in profile.
- **Top-1 newbie leaderboard:** "Прорыв недели" badge (1-week).
- **Top 10% of any leaderboard:** small coin bonus (cosmetic; doesn't feed
  back into the score).
- No minimum-score floor for v1 — even low-activity weeks can produce
  ranked top-3. Revisit if early-launch UX surfaces an issue.

## Transparency

Formula published in `/help` → FAQ section and in the news channel. Users
can plan a week against it.

---

## Implementation notes

These guide the build, not the design. Subject to revision as we ship.

### Phase 0 — Event property audit (required before Phase 1)

The backtest notebook needs to reconstruct historical scores from the
existing `events` table. Each scoring-relevant event must carry enough
properties to do that without joining mutable progress tables.

Audit task: grep every `event_repo.log()` call site in `bot.py` (~14
hooks) and confirm the following properties exist on the relevant events:

| Event | Required properties |
|---|---|
| `session.complete` | `duration` (minutes) |
| `quiz.answered` | `correct: bool` |
| `mcq.answered` | `correct: bool` |
| `task.answered` | `correct: bool` |
| `flashcard.reviewed` | `is_new: bool`, `correct: bool` (or `quality: int`) |

Missing properties get patched at the log site — ~1 line per hook, ~15
lines of edit total. Historical events before this patch are partially
reconstructable by joining `quiz_progress` / `task_progress` /
`flashcard_progress`, but new events from the patch date forward have
clean backtest semantics.

### Phasing

- **Phase 0** — Event property audit (~0.25 session)
- **Phase 1** — Score data layer (4 new tables, repository, scoring hooks,
  backtest notebook, tests) — ~1 session
- **Phase 2** — User-facing `/leaderboard` + Monday rollover scheduler +
  top-3 / breakthrough / top-10% reward distribution — ~1 session
- **Phase 3** — Streak freeze mechanic (UI + atomic coin deduction) —
  ~0.5 session
- **Phase 4** — Friends system (tables, FSM flow, friends-tab view) —
  ~1 session
- **Privacy opt-out toggle** ships with whichever phase first touches the
  settings menu (likely Phase 2).
