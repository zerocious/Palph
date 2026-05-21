# Palph — Weekly Leaderboard System

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
- **User-created flashcards** (2026-05-22): same scoring rules and daily cap.
  Stored in `user_flashcards`; `card_hash` = `u{card_id:07x}` (distinct from
  official `md5(term)[:8]`). SM-2 state lives in the same `flashcard_progress`
  table; `flashcard_reviewed` events use the same `is_new` + `quality` semantics.
  Source filter (`mix` / `official` / `own`) affects which cards appear in
  study sessions only — not how points are counted once reviewed.

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

### Phase 0 — Event property audit (DONE 2026-05-18)

Audited all `event_repo.log()` call sites in `bot.py`. Result: only
`flashcard_reviewed` required a patch; everything else already carries
the properties the backtest needs.

| Event | Properties on log | Needed for backtest | Status |
|---|---|---|:---:|
| `session_completed` | `duration`, `coins`, `bonus_coins`, `source`, ... | `duration` for time pts | ✅ |
| `quiz_answered` | `is_correct`, `subject_id`, `term_hash`, `streak_after` | `is_correct` | ✅ |
| `mcq_answered` | `is_correct`, `subject_id`, `question_hash`, ... | `is_correct` (MCQ = quiz) | ✅ |
| `task_attempted` | `succeeded`, `task_id`, `attempts_used`, `coins` | `succeeded` (≡ correct for tasks) | ✅ |
| `flashcard_reviewed` | `quality`, `reps_*`, `ef_*`, `interval_*`, **`is_new`** (added) | `is_new` + `quality ≥ 3` for correct | ⚠️→✅ |

The `flashcard_reviewed` gap: at the rate-handler call site, the existing
`reps_before` was ambiguous (`0` could mean "first encounter" OR "reset
after wrong answer"). Patched to capture `is_new = (progress is None)`
*before* `upsert_progress` writes the row, and added `is_new` to the
event properties.

Historical events before this patch are partially reconstructable for
flashcards by joining `flashcard_progress` and inferring "new = first
ever `flashcard_reviewed` event for this `(user_id, card_hash)`."
Events from this patch forward have clean backtest semantics directly.

### Phasing

- **Phase 0** — Event property audit ✅ done (commit `53e8c79`)
- **Phase 1** — Score data layer: 4 tables + helpers + `LeaderboardRepository`
  + scoring hooks (`complete_session`, quiz, MCQ, task, flashcard) + 64 tests
  + backtest notebook ✅ done (commit `930a2e8`)
- **Phase 2a** — `/leaderboard` view + privacy toggle ✅ done (commit `9ab3ff1`)
  - `LeaderboardService.render_leaderboard` with segment auto-routing
  - `LeaderboardRepository.get_ranked_segment` / `get_user_rank` /
    `award_badge` / `get_active_badges`
  - `/leaderboard` slash command in bot.py
  - Privacy toggle button in settings menu + `users.hidden_from_leaderboards`
    accessors on `UserRepository`
  - 23 tests covering ranked-segment correctness, multiplier-vs-base ordering,
    hidden filter, user-rank, badge idempotency, expiration, render output
- **Phase 2b** ✅ done — Monday rollover scheduler + reward distribution
  - `LeaderboardService.run_rollover(week_iso)` — awards top-3 main /
    breakthrough newbie / top-10% coin bonus (50 coins per slot,
    constant `COIN_BONUS_TOP10_PCT`); idempotent via `award_badge`
    INSERT OR IGNORE; coin payout gated by rowcount so re-runs don't
    double-credit.
  - `leaderboard_scheduler` in `tasks.py` — wakes every 60s, fires when
    UTC Tuesday 00:00 hits; computes `ended_week_iso` via
    `_compute_ended_week_iso(now_utc - 2 days)`. Date-based dedup
    (`last_run_date`) avoids re-firing within the 60-second window.
  - Anchor: UTC Tuesday 00:00. All global TZs have crossed their
    local Monday boundary by this point, so `weekly_scores` for the
    just-ended week is fully locked — no race with late-TZ writes.
    13 tests cover rollover correctness across segments, top-10%
    threshold (`< 10` users → skip), idempotency on re-fire, hidden
    users still eligible, ISO week edge cases at year boundary.
- **Phase 3** ✅ done — Streak freeze mechanic
  - `LeaderboardRepository.purchase_freeze(user_id, current_streak)`:
    atomic under `db.lock`. Cooldown check (`granted_at > now - 7 days`),
    balance check via `users.total_coins`, deduct + insert row.
    Returns status string `purchased` / `insufficient_coins` /
    `cooldown_active`.
  - `has_active_freeze`, `consume_freeze_if_active(user_id, today_local)`,
    `get_freeze_cooldown_remaining_days` helpers.
  - `StreakService` accepts optional `leaderboard_repo`. On miss-day
    path: try `consume_freeze_if_active` first; if it returns True,
    skip streak reset and send a notification ("❄️ Заморозка
    сработала, стрик сохранён"). Otherwise reset as before.
  - Profile inline keyboard gets "❄️ Заморозить стрик" button → details
    screen showing current streak / cost / balance / availability →
    confirm button only when purchasable. Double-tap-safe via repo-level
    atomic cooldown check.
  - 17 tests cover: purchase happy path / insufficient coins / exact
    balance / cooldown / cost-tier; has_active before/after purchase
    and consume; consume idempotency; cooldown days helper; streak
    integration (preserved with freeze, reset without, untouched on
    studied day, consumed only once per missed day).
- **Phase 4** ✅ done — Friends system
  - 2 new tables: `friend_requests` (PK from+to, CHECK from!=to) +
    `friendships` normalized one-row-per-pair (PK user_a+user_b,
    CHECK user_a<user_b).
  - `FriendRepository` with full lifecycle: `send_request` (status
    incl. self_target / user_not_found / already_friends /
    already_pending / **auto_accepted** — reverse-direction pending
    cross-fires the friendship), `accept_request` (transactional
    DELETE + INSERT), `reject_request`, `cancel_request`,
    `get_pending_received`, `get_pending_sent`, `get_friends`
    (UNION over both PK sides), `are_friends`, `remove_friend` —
    last two normalize internally so order-of-args doesn't matter.
  - `LeaderboardService` gains optional `friend_repo` and
    `render_friends_tab(user_id)` — list user + friends sorted by
    current week's `total_final` (multiplier applied),
    🥇🥈🥉 medals for top-3, `(Вы)` marker for self row.
  - `/friends` slash command with inline keyboard: ➕ Add (FSM enters
    Telegram ID, sends request, notifies target with accept/reject
    buttons), 📩 Pending (incoming requests with per-row
    accept/reject), ➖ Remove (lists friends, confirm dialog).
    Cross-user notifications via `bot.send_message` (graceful on
    blocked-bot exception).
  - 29 tests: `test_friend_repository.py` covers all status paths,
    auto-accept, normalization, UNION read, symmetric ops; new
    `TestRenderFriendsTab` covers empty hint, multi-friend sorting
    by total_final, multiplier ordering, no-friend-repo graceful
    fallback.
- **Privacy opt-out toggle** ✅ shipped with Phase 2a
