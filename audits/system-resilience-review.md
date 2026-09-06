# System Resilience Review — Palph (Telegram bot, aiogram 3)

> **Audit date:** 2026-05-24 · **Doc sync:** 2026-09-05 (pytest suite **793** tests).

**Date:** 2026-05-24  
**Scope:** Timeout handling, retry/backoff, circuit breaking, bulkhead/isolation, graceful degradation  
**Method:** Static code review — grep + read of `bot.py`, `services.py`, `db.py`, `repository.py`, `tasks.py`, `file_upload_security.py`, `fsm_storage.py`, `parse_logs.py`, `plan_handlers.py`, related tests  
**Runtime model:** Single-process long-polling bot, one shared `aiosqlite` connection, in-memory rate limiters

---

## Executive summary

The bot has **solid scheduler resilience** (per-loop try/except, idempotent streak/backup/rollover dedup), **targeted Telegram 429 retry** (`_send_with_retry_after`), **user-level rate limiting**, and **many graceful-degradation paths** (pet assets, reminders, i18n, feature flags). Background tasks and timer tasks register `_log_task_exception` done-callbacks; a centralized `@router.errors()` handler notifies users on unhandled handler failures.

Gaps are concentrated in **missing explicit timeouts** (HTTP/DB/long ops), **no circuit breaker**, **coarse single-connection SQLite** without `busy_timeout`, **inconsistent retry** (streak/broadcast/timer paths differ), and **unbounded in-memory admin exports** that can block the event loop under load.

**Overall resilience rating: 5 / 10**

| Area | Rating | Summary |
|------|--------|---------|
| Timeout handling | 3/10 | No configured HTTP/DB timeouts; no `asyncio.wait_for` on I/O |
| Retry logic | 6/10 | Good for `TelegramRetryAfter`; weak for transient network/DB errors |
| Circuit breaker | 1/10 | Not implemented |
| Bulkhead / isolation | 5/10 | Rate limits + locks; single DB conn; no send concurrency cap |
| Graceful degradation | 7/10 | Feature flags, asset/i18n/tip fallbacks, side-effect isolation |

---

## 1. TIMEOUT HANDLING

### R1.1 — Telegram / aiohttp uses default timeouts (unconfigured)

**Importance: 9/10**

**Locations:**
- `bot.py:7667` — `bot = Bot(token=BOT_TOKEN)` (no `session`, no `DefaultBotProperties`, no `ClientTimeout`)
- `bot.py:2637` — `await bot.download(doc, destination=buffer)` in `handle_ut_import_file`
- `bot.py:5108`, `bot.py:7783` — `bot.send_message`, `dp.start_polling(bot)`

**Finding:** All Telegram API calls inherit aiogram/aiohttp defaults. Slow or hung Telegram endpoints can block handler tasks indefinitely. No project-wide timeout policy.

**Remediation:**

```python
# bot.py — near imports
from aiohttp import ClientTimeout
from aiogram.client.session.aiohttp import AiohttpSession

TELEGRAM_TIMEOUT = ClientTimeout(total=30, connect=10, sock_read=20)

# bot.py:7667 — replace Bot(...) construction
session = AiohttpSession(timeout=TELEGRAM_TIMEOUT)
bot = Bot(token=BOT_TOKEN, session=session)
```

Optional per-call guard for downloads:

```python
# bot.py:2637 — handle_ut_import_file
await asyncio.wait_for(bot.download(doc, destination=buffer), timeout=60)
```

---

### R1.2 — SQLite has no `busy_timeout`; single connection can stall under contention

**Importance: 8/10**

**Locations:**
- `db.py:10-23` — `get_db()` sets WAL + foreign keys only
- `db.py:20` — `db.lock = asyncio.Lock()` (manual serialization, not SQLite busy handler)
- `fsm_storage.py:53-76` — `set_state` / `set_data` commit without always holding `db.lock`
- `repository.py:1109-1112` — docs: caller must hold lock for RMW; easy to miss

**Finding:** No `PRAGMA busy_timeout`. Concurrent FSM writes + repository RMW under `db.lock` can surface `database is locked` with no retry. Global lock serializes all critical paths — long `complete_session` or analytics queries block others.

**Remediation:**

```python
# db.py:21 — after WAL setup
await db.execute("PRAGMA busy_timeout=5000")  # ms; tune for prod
```

Wrap hot-path executes (optional):

```python
async def execute_with_db_retry(coro_factory, *, retries=3, base_delay=0.05):
    for attempt in range(retries):
        try:
            return await coro_factory()
        except aiosqlite.OperationalError as e:
            if "locked" not in str(e).lower() or attempt == retries - 1:
                raise
            await asyncio.sleep(base_delay * (2 ** attempt))
```

---

### R1.3 — Long-running admin analytics/export has no time budget

**Importance: 7/10**

**Locations:**
- `services.py:1772-1790` — `AnalyticsService.export_table_csv` — `SELECT *` loads full table into memory
- `services.py:2521-2564` — `export_all_tables_zip` — sequential full-table reads + ZIP in memory
- `services.py:2117+` — `compute_product_metrics` — multi-query aggregation
- `bot.py:5850-5879` — `_send_all_tables_zip` — no `wait_for`, blocks until complete
- `bot.py:5750+` — `/feature_usage`, `/product_metrics` admin handlers

**Finding:** Large datasets can hold the event loop for seconds/minutes, delaying reminders, timers, and user handlers on the same loop.

**Remediation:**

```python
# bot.py — _send_all_tables_zip
zip_bytes, metadata = await asyncio.wait_for(
    analytics_service.export_all_tables_zip(),
    timeout=120,
)
```

Longer-term: stream CSV rows; run heavy exports in `asyncio.to_thread()` or a dedicated worker.

---

### R1.4 — Schedulers and timer tasks: resilient sleep loops (positive)

**Importance: N/A (strength)**

**Locations:**
- `tasks.py:48-69` — `leaderboard_scheduler` — try/except per iteration, `asyncio.sleep(60)`
- `tasks.py:89-110` — `streak_scheduler` — same pattern
- `tasks.py:123-147` — `reminder_scheduler` — same + heartbeat log
- `bot.py:3154-3219` — `run_timer_task` — `asyncio.sleep(remaining_sec)`; broad except logs crash
- `bot.py:7754-7762` — background schedulers get `add_done_callback(_log_task_exception)`

**Finding:** Scheduler design tolerates per-tick failures without killing the loop. Timer task failures are logged and slot released in `finally`.

---

## 2. RETRY LOGIC

### R2.1 — `TelegramRetryAfter` handled with single retry + sleep (good, narrow scope)

**Importance: 7/10 (positive, incomplete)**

**Locations:**
- `services.py:25-43` — `_send_with_retry_after(send_callable, *, label, uid)` — **one** retry after `e.retry_after + 0.5`
- `services.py:742-747`, `814-837` — used by `ReminderService` morning/evening sends
- `bot.py:5114-5138` — `/broadcast` — inline same pattern

**Finding:** 429 flood-control is handled correctly. No retry on `ClientConnectorError`, `TimeoutError`, or 5xx-class failures. Second 429 on retry fails permanently.

**Remediation — extend helper:**

```python
# services.py — _send_with_retry_after
MAX_SEND_ATTEMPTS = 3
TRANSIENT = (asyncio.TimeoutError, ConnectionError, OSError)

for attempt in range(MAX_SEND_ATTEMPTS):
    try:
        return await send_callable()
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after + 0.5)
    except TRANSIENT:
        if attempt == MAX_SEND_ATTEMPTS - 1:
            raise
        await asyncio.sleep(0.5 * (2 ** attempt))
```

Apply to `StreakService._process` sends at `services.py:955-963`, `992-999` (currently raw `bot.send_message`).

---

### R2.2 — Idempotency markers on scheduled / financial paths (good)

**Importance: 8/10 (positive)**

**Locations:**
- `services.py:924-926` — `last_streak_check_date` skip in `StreakService._process`
- `services.py:2596-2610` — `BackupService.maybe_backup_for_today` — in-memory + file existence dedup
- `tasks.py:46-56` — `leaderboard_scheduler.last_run_date` dedup
- `tasks.py:87-103` — `streak_scheduler.last_check_date` per TZ
- `tasks.py:120-136` — `reminder_scheduler.last_tick` per TZ/minute
- `repository.py` — `award_badge` INSERT OR IGNORE (referenced in `tasks.py:40-42`)
- `bot.py:266-276` — `_claim_active_timer` — prevents double session completion

**Finding:** Nightly jobs and session completion have meaningful idempotency. Retries are mostly safe for streak check and backup.

---

### R2.3 — File upload download: fail-fast, no retry

**Importance: 5/10**

**Locations:**
- `bot.py:2636-2641` — `handle_ut_import_file` — single `bot.download`; generic except → user message

**Finding:** Transient Telegram download failures are not retried. Acceptable for UX simplicity; reduces resilience under flaky networks.

**Remediation (minimal):**

```python
for attempt in range(2):
    try:
        await asyncio.wait_for(bot.download(doc, destination=buffer), timeout=45)
        break
    except Exception as e:
        if attempt == 1:
            logger.error("ut.import_download_failed user=%s reason=%s", user_id, e)
            await message.answer(t("user_tasks.download_error", locale))
            return
        await asyncio.sleep(1)
```

---

### R2.4 — Tests cover rate limit / backup dedup / reminder fallbacks; not HTTP retry

**Importance: N/A (test gap)**

**Locations:**
- `tests/test_rate_limiter.py` — sliding window behavior
- `tests/test_backup_service.py` — daily dedup, VACUUM INTO
- `tests/test_reminder_service.py` — sad-pet fallback, forbidden user
- `tests/test_log_parser.py:72-78` — parses `TimeoutError` from log text (parser only, not runtime)

**Finding:** No tests assert Telegram transient retry or DB lock retry behavior.

---

## 3. CIRCUIT BREAKER

### R3.1 — No circuit breaker pattern anywhere

**Importance: 6/10**

**Verified:** Grep for `circuit`, `breaker`, `half.open` — **zero matches** in `*.py`.

**Impact areas without failure detection / open-state:**
- Telegram API during outage → every handler/send keeps attempting full latency
- Reminder tick with N users → N sequential failing sends per minute
- `/broadcast` → continues through full recipient list despite sustained failures

**Remediation — minimal in-process breaker for outbound Telegram:**

```python
# services.py (new)
class TelegramSendBreaker:
    def __init__(self, failure_threshold: int = 10, cooldown_seconds: float = 60):
        self._failures = 0
        self._open_until = 0.0
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds

    def allow(self) -> bool:
        return monotonic() >= self._open_until

    def record_success(self):
        self._failures = 0

    def record_failure(self):
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._open_until = monotonic() + self.cooldown_seconds
            logger.error("telegram.breaker_open cooldown=%s", self.cooldown_seconds)

# In _send_with_retry_after: if not breaker.allow(): raise or skip with metric
```

Fallback while open: queue reminder for next tick (already minute-granularity) or skip with structured log `telegram.breaker_skip`.

---

## 4. BULKHEAD (RESOURCE ISOLATION)

### R4.1 — User rate limiter + admin message limiter (good app-layer bulkhead)

**Importance: 7/10 (positive)**

**Locations:**
- `services.py:64-120` — `UserRateLimiter` — sliding window, `threading.Lock`
- `bot.py:299-304` — `rate_limiter` (30/60s) + `admin_message_limiter` (1/60s)
- `bot.py:344-391` — `RateLimitMiddleware` — admins exempt; `"block"` silently drops event
- `bot.py:7680-7690` — middleware registration order (username sync before rate limit)

**Finding:** Protects handlers from per-user abuse. State is in-memory (resets on restart — documented in `services.py:67-69`). Not a substitute for Telegram global limits.

---

### R4.2 — Global `db.lock` serializes all RMW; FSM writes partially unsynchronized

**Importance: 7/10**

**Locations:**
- `db.py:14-20` — one lock per connection
- `fsm_storage.py:91-96` — `update_data` uses lock; `set_state`/`set_data`/`get_*` do not
- `services.py:641` — `StudyService.complete_session` holds lock for full transaction
- `bot.py:206-214` — per-user `_timer_completion_lock` (good isolation for timer race)

**Finding:** Timer completion is well isolated per user. DB layer is a single bulkhead — analytics export + session complete + streak processing contend on one lock/connection.

**Remediation:** Hold `db.lock` in all FSM mutators, or use `update_data` exclusively:

```python
# fsm_storage.py:set_state
async with self.db.lock:
    await self.db.execute(...)
    await self.db.commit()
```

---

### R4.3 — No concurrency cap on outbound Telegram from schedulers

**Importance: 6/10**

**Locations:**
- `services.py:726-769` — `_send_morning` — sequential `for u in users`
- `services.py:793-863` — `_send_evening` — sequential
- `bot.py:5106-5156` — broadcast loop with `asyncio.sleep(0.05)` throttle only

**Finding:** Throttle prevents 429 but does not isolate scheduler send volume from interactive handler latency when lists grow. No `asyncio.Semaphore` for max in-flight Telegram calls.

**Remediation:**

```python
_TELEGRAM_SEND_SEM = asyncio.Semaphore(5)

async with _TELEGRAM_SEND_SEM:
    await _send_with_retry_after(...)
```

---

### R4.4 — Backup uses separate connection (good isolation)

**Importance: N/A (strength)

**Locations:**
- `services.py:2653-2654` — `_vacuum_into` opens dedicated `aiosqlite.connect(self.db_path)`

**Finding:** Backup snapshot does not share the main connection's transaction state.

---

## 5. GRACEFUL DEGRADATION

### R5.1 — Feature flags disable incomplete UI surfaces

**Importance: 8/10 (positive)**

**Locations:**
- `plan_handlers.py:41` — `PLAN_UI_ENABLED = False`
- `bot.py:6445` — `PET_CUSTOMIZATION_ENABLED = False`
- `bot.py:3653+`, `7691-7698` — plan handlers gated on flag
- `bot.py:6532+`, `6595+` — pet customization handlers gated

**Finding:** Backend plan/pet code can ship without exposing broken Telegram UI.

---

### R5.2 — Asset, content, and UI fallbacks (strong)

**Importance: 8/10 (positive)**

**Locations:**
- `services.py:388-449` — `render_pet` fallback chain → `FileNotFoundError`
- `services.py:807-837` — evening reminder: GIF → text on missing asset
- `services.py:731-740` — morning tip builder failure → send without tip block
- `i18n.py:36-43` — `t()` falls back ru → raw key
- `bot.py:918-921`, `944+` — tips JSON with legacy `.txt` fallback
- `bot.py:2024-2032` — `_edit_or_answer_settings` edit → answer fallback
- `bot.py:7770-7778` — `get_me()` failure → `bot_username = None`, invite links disabled
- `bot.py:307-340` — `UsernameSyncMiddleware` — sync failure logged, handler continues
- `file_upload_security.py:130-142` — `sanitize_pet_asset_keys` clamps invalid DB values

**Tests:** `tests/test_reminder_service.py`, `tests/test_render_pet.py`, `tests/test_i18n.py`, `tests/test_productivity_tips_files.py`

---

### R5.3 — Side effects fail soft; core data paths fail loud in logs

**Importance: 7/10 (positive)

**Locations:**
- `services.py:2616-2621` — backup errors → log + `None`, scheduler continues
- `services.py:754-769`, `850-863` — reminder send errors per-user, loop continues
- `services.py:964-969` — streak notify failure logged, DB already committed
- `bot.py:659-697` — `global_error_handler` — user sees `common.unexpected_error`
- `bot.py:542-552` — `send_rating_prompt` — failure logged, session already saved

---

### R5.4 — Rate-limit middleware drops events silently (degraded UX)

**Importance: 4/10**

**Locations:**
- `bot.py:368-371` — `status == "block"` → `return None` (no handler, no user feedback unless warn path fired earlier)

**Finding:** Hard block gives no message on the blocked action itself (warn may have fired earlier). By design for abuse; can look like "bot ignored me" under burst traffic.

**Remediation:** Optional single-line answer on block (i18n keys exist in locales: `errors.rate_limit_msg` / `rate_limit_cb` per `scripts/build_locales.py:374-375`).

---

### R5.5 — `parse_logs.py` resilient to malformed logs (operator tooling)

**Importance: 5/10 (positive, ancillary)

**Locations:**
- `parse_logs.py:52-105` — skips malformed lines; `errors="replace"` on read
- `parse_logs.py:97-105` — unstructured → `event_name="unstructured"`

**Finding:** Log ETL degrades gracefully; does not affect runtime bot resilience.

---

## 6. CROSS-CUTTING OBSERVATIONS

### Global error handler — present (improved since prior audit)

**Locations:** `bot.py:659-697` — `@router.errors()` → `global_error_handler`

Prior `audits/error-handling-review.md` noted this was missing; it is now wired on the root router before `dp.include_router(router)`.

### Background task crash visibility — present

**Locations:**
- `bot.py:147-168` — `_log_task_exception`
- `bot.py:3231` — timer tasks
- `bot.py:7760-7761` — scheduler tasks

### Shutdown — partial

**Locations:** `bot.py:7782-7792` — `finally: t.cancel()` on background tasks; no `await asyncio.gather(..., return_exceptions=True)` for clean drain; no explicit `bot.session.close()`.

---

## 7. IMPROVEMENT RECOMMENDATIONS (PRIORITIZED)

| Priority | Action | Effort | Impact |
|----------|--------|--------|--------|
| P0 | Configure `AiohttpSession` + `ClientTimeout`; wrap `bot.download` and export ZIP in `asyncio.wait_for` | Low | Prevents hung handlers |
| P0 | Add `PRAGMA busy_timeout` + optional locked-DB retry on hot paths | Low | Reduces SQLite stall cascades |
| P1 | Route all outbound Telegram sends through `_send_with_retry_after` (incl. streak, timer, broadcast) + transient retry | Medium | Consistent recovery from network blips |
| P1 | Add `asyncio.Semaphore` on scheduler/broadcast sends | Low | Isolates bulk sends from interactive traffic |
| P2 | Introduce lightweight `TelegramSendBreaker` for sustained API failures | Medium | Stops retry storms |
| P2 | Stream or off-thread large `/export all` | Medium | Protects event loop under growth |
| P3 | Sync all `SQLiteStorage` mutations under `db.lock` | Low | Closes FSM race window |
| P3 | User-visible message on rate-limit hard block | Low | Clearer degradation UX |

---

## 8. OVERALL RESILIENCE RATING

### **5 / 10**

**Rationale:** Production-minded patterns exist where the bot already hurt (429 retry, scheduler loops, backup dedup, pet/reminder fallbacks, feature flags). The system is **not fragile for a small user base**, but lacks **timeout boundaries**, **circuit breaking**, and **consistent retry/isolation** needed to survive Telegram outages, DB contention spikes, or dataset growth without visible stalls or silent drops.

**Target state (8/10):** Explicit I/O timeouts, unified send helper with transient retry, DB busy_timeout, send semaphore, breaker on outbound API, bounded admin exports — with 3–5 integration tests for lock retry and 429 double-hit.

---

## Appendix — Key symbols quick reference

| Symbol | File:Line | Role |
|--------|-----------|------|
| `_send_with_retry_after` | `services.py:25` | 429 retry helper |
| `UserRateLimiter` | `services.py:64` | Per-user sliding window |
| `RateLimitMiddleware` | `bot.py:344` | Handler gate |
| `global_error_handler` | `bot.py:659` | Unhandled exception UX |
| `_log_task_exception` | `bot.py:147` | Background task crash log |
| `get_db` | `db.py:10` | Single SQLite connection + lock |
| `BackupService` | `services.py:2573` | Daily VACUUM INTO snapshot |
| `*._scheduler` | `tasks.py:25,72,113` | Minute loops with try/except |
| `PLAN_UI_ENABLED` | `plan_handlers.py:41` | Plan UI feature flag |
| `PET_CUSTOMIZATION_ENABLED` | `bot.py:6445` | Pet shop UI feature flag |
