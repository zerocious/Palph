# Error-Handling Review — Palph (Telegram bot, aiogram 3)

**Date:** 2026-05-22
**Reviewer:** Claude (Opus 4.7)
**Branch / HEAD:** `main` @ `aa7071c`
**Scope:** `bot.py`, `services.py`, `repository.py`, `tasks.py`, `db.py`, `fsm_storage.py`, `i18n.py`, `locale_bot.py`

> This is a long-polling Telegram bot. The HTTP status-code categories in the audit
> brief (400 / 401 / 403 / 404 / 429 / 500) don't apply 1:1; they map to aiogram
> exception subclasses + business-layer errors. Mapping is called out per section.

---

## Executive summary

The codebase relies almost exclusively on a single error-handling primitive —
`try: … except Exception as e: logger.warning|error(…)` — repeated **~415 times
across the project (~339 in [bot.py](bot.py) alone)**. There is no central
aiogram error handler, no custom exception hierarchy, no retry/back-off layer,
and no separation between expected-and-handled vs. unexpected failure. The
schedulers and the per-user timer task are individually robust, but the
`asyncio.create_task(...)` invocations that launch them at
[bot.py:6989-6993](bot.py:6989) and [bot.py:2964](bot.py:2964) have no
`add_done_callback`, so a fatal exception silently kills the task with only
"Task exception was never retrieved" landing in stderr.

Three findings rated 8/10 or higher; eleven other actionable issues below.

---

## 1. ERROR HANDLING CONSISTENCY

### Finding 1.1 — No centralized aiogram error handler

**Severity: 9/10**

`dp.errors.register(...)` (aiogram 3 equivalent of an exception-handling
middleware / error-router) is **never wired up**. Verified by grepping for
`errors_router`, `dp.errors`, `@dp.error`, `errors.register`, `ErrorEvent`,
`router.errors`, `@router.error` — zero matches in `*.py`.

This means: any handler that raises an exception it didn't catch itself goes to
aiogram's default `loggers.event` traceback and the user sees **nothing** — no
"something went wrong, try again" message. With ~339 hand-rolled `try/except`
blocks in `bot.py`, the per-handler pattern compensates *most* of the time, but
gaps are unavoidable at this scale.

Concrete evidence of a gap: [bot.py:3036-3040](bot.py:3036) `handle_standard_timer`
calls `user_repo.user_exists` / `create_user` / `apply_user_bot_commands` with no
wrapping; a DB lock-timeout or a SQLite IO error here would propagate untouched.

**Remediation — minimal drop-in for [bot.py](bot.py) (place near other middleware
registration around [bot.py:6939](bot.py:6939)):**

```python
from aiogram.types import ErrorEvent

@dp.errors()
async def global_error_handler(event: ErrorEvent) -> bool:
    update = event.update
    exc = event.exception
    user_id = None
    chat_id = None
    if update.message:
        user_id = update.message.from_user.id if update.message.from_user else None
        chat_id = update.message.chat.id
    elif update.callback_query:
        user_id = update.callback_query.from_user.id
        chat_id = (
            update.callback_query.message.chat.id
            if update.callback_query.message else None
        )

    # logger.exception captures the traceback — bare logger.error(f"{e}") loses it
    logger.exception(
        "handler.unhandled user_id=%s chat_id=%s exc=%s",
        user_id, chat_id, type(exc).__name__,
    )

    # Friendly user-facing notice (no exception text leaked)
    if chat_id is not None:
        try:
            locale = await loc(user_id) if user_id else "ru"
            await bot.send_message(
                chat_id, t("common.unexpected_error", locale)
            )
        except Exception:
            pass
    return True  # mark as handled so aiogram doesn't re-log
```

Add `"common.unexpected_error"` to [locales/ru.json](locales/ru.json) and
[locales/en.json](locales/en.json) (e.g. "⚠️ Что-то пошло не так. Попробуй ещё
раз через минуту." / "⚠️ Something went wrong. Please try again in a minute.").

---

### Finding 1.2 — No custom exception hierarchy; semantics smuggled through `ValueError` strings

**Severity: 6/10**

There are no custom exception classes anywhere in the project (verified:
`class \w+(?:Error|Exception)\b` returns zero matches). Domain conditions are
encoded as:

- [repository.py:449](repository.py:449): `raise ValueError("limit_exceeded")`
  — sentinel string carries the semantic.
- [bot.py:2214-2221](bot.py:2214): caller does `except ValueError as e: if
  str(e) == "limit_exceeded": ...; raise` — string-equality on exception messages
  is brittle (any future `ValueError` from the same code path is masked as a
  generic error).
- [bot.py:73](bot.py:73): `raise RuntimeError("❌ BOT_TOKEN не установлен в .env!")`
  — generic, plus a UI-style emoji in an exception message that may end up in a
  systemd/journalctl log.
- [bot.py:1833,1864](bot.py:1833): `raise ValueError(f"Unknown setting type: ...")`
  / `f"Unknown slot: ..."` — these are programmer errors (should be `AssertionError`
  or `TypeError` at most) but are caught by the same `except Exception as e` blocks
  downstream as if they were user-input failures.
- [repository.py:1638](repository.py:1638): `raise ValueError(f"Unknown segment:
  {segment!r}")` — same issue.
- [services.py:1732](services.py:1732): `raise KeyError(f"Unknown table alias: ...")`
  — at least `KeyError` is more semantically right; but still no domain hierarchy.

**Remediation — add one small module, `errors.py`:**

```python
# errors.py
class PalphError(Exception):
    """Base class for all domain errors raised by Palph."""

class ConfigError(PalphError):
    """Missing or invalid environment configuration."""

class ValidationError(PalphError):
    """User-supplied input failed validation."""

class NotFoundError(PalphError):
    """Resource missing (user, card, task, table alias, ...)."""

class LimitExceededError(PalphError):
    """A per-user or per-resource cap has been hit."""

class PermissionDenied(PalphError):
    """Caller is not authorized for the requested action."""
```

Then convert the three smuggling sites:

```python
# bot.py:73
from errors import ConfigError
if not BOT_TOKEN:
    raise ConfigError("BOT_TOKEN is not set in .env")

# repository.py:449
from errors import LimitExceededError
if count >= self.MAX_PER_SUBJECT:
    raise LimitExceededError(self.MAX_PER_SUBJECT)

# bot.py:2214-2221 — caller becomes self-documenting
try:
    card = await user_flashcard_repo.create(user_id, subject_id, term, definition)
except LimitExceededError as e:
    await message.answer(t("fc.limit_reached", locale, max=e.args[0]))
    await state.clear()
    return
except sqlite3.IntegrityError:
    ...
```

---

### Finding 1.3 — Inconsistent error logging: `logger.error(f"...{e}")` vs `logger.exception` vs `logger.warning("... reason=%s", type(e).__name__)`

**Severity: 6/10**

Three styles coexist, none of them capture the traceback consistently:

1. **f-string concat (loses traceback):** 12 sites in `bot.py`, 5 in `tasks.py`:
   - [bot.py:408](bot.py:408): `logger.error(f"send_rating_prompt: не удалось отправить запрос оценки: {e}")`
   - [bot.py:2005](bot.py:2005): `logger.error(f"Error toggling setting: {e}")`
   - [bot.py:2948](bot.py:2948): `logger.error(f"Ошибка отправки завершения таймера {user_id}: {e}")`
   - [bot.py:6885](bot.py:6885): `logger.error(f"reconcile_stale_timers: запись {key!r} не обработана: {e}")`
   - [tasks.py:68, 109, 144](tasks.py:68): scheduler-loop catch-alls.
2. **Structured key=value (newer, better, but still no traceback):** e.g.
   [bot.py:5586](bot.py:5586), [services.py:602](services.py:602).
3. **Zero `logger.exception(...)` calls anywhere** (`logger\.exception` returns
   zero matches). That's the only style that captures `exc_info` for free.

This is the single biggest reason debugging production-only failures will be
painful: `bot.log` will show only the exception class name and message string,
never the line that raised.

**Remediation:** standardize on `logger.exception(...)` for the catch-all
`except Exception as e:` branches that represent *unexpected* failures, and
keep `logger.warning(... reason=%s, type(e).__name__)` for known/expected ones
(e.g. `TelegramForbiddenError`). Example patches:

```python
# bot.py:2005
- logger.error(f"Error toggling setting: {e}")
+ logger.exception("settings.toggle_failed user_id=%s setting=%s", user_id, setting_type)

# tasks.py:68
- logger.error(f"leaderboard_scheduler failed: {e}")
+ logger.exception("leaderboard_scheduler.tick_failed")

# tasks.py:109
- logger.error(f"streak_scheduler failed: {e}")
+ logger.exception("streak_scheduler.tick_failed")

# tasks.py:144
- logger.error(f"reminder_scheduler tick failed: {e}")
+ logger.exception("reminder_scheduler.tick_failed")
```

---

### Finding 1.4 — `except Exception: pass` swallows everything in 8+ places

**Severity: 5/10**

Sites: [bot.py:2698, 2709, 5627, 5925, 6179, 6756](bot.py:2698), plus three
`except Exception: pass` in [db.py:368, 381, 391, 401, 410, 419, 429, 441](db.py:368)
(migration ADD COLUMN idempotency).

The DB-migration ones (db.py) are intentionally swallowing "duplicate column"
errors — but they also swallow disk-full, read-only-DB, and schema-corruption
errors. The user-facing ones (e.g. bot.py:5925 `pet_menu` delete-message) are
mostly fine for ergonomic edge cases but lose forensic value.

**Remediation — narrow the catch in [db.py](db.py)** (the only place where
catch-everything is actually load-bearing):

```python
# db.py:365 — apply same pattern to all 8 migration blocks
try:
    await db.execute("ALTER TABLE study_sessions ADD COLUMN score INTEGER")
    await db.commit()
except aiosqlite.OperationalError as e:
    if "duplicate column" not in str(e).lower():
        raise
```

For the bot.py UI sites — at minimum add a `logger.debug(...)` so the swallow
is observable:

```python
# bot.py:2698-2699
try:
    await callback.message.edit_text(t("rating.thanks", await loc(user_id), emoji=emoji))
except Exception as e:
    logger.debug("rating.edit_failed user_id=%s reason=%s", user_id, type(e).__name__)
```

---

## 2. ERROR CATEGORIES

aiogram-bot mapping of the HTTP-style categories:

| HTTP analogue | This codebase                                                                 | Status |
| ------------- | ----------------------------------------------------------------------------- | ------ |
| 400 Validation| Inline `if not term: await message.answer(...)` — present, ad hoc             | OK     |
| 401 Auth      | n/a — Telegram authenticates users                                            | n/a    |
| 403 Forbidden | `is_admin()` guard + `callback.from_user.id != target_user_id` anti-spoof     | Partial|
| 404 Not found | `if not user: return` etc. — ad hoc                                           | Partial|
| 429 RateLimit | `UserRateLimiter` middleware (inbound). **No outbound `TelegramRetryAfter` handling.** | Gap    |
| 500 Server    | catch-all `except Exception`                                                  | Gap    |

### Finding 2.1 — No handling for `TelegramRetryAfter` (Telegram 429 flood control)

**Severity: 8/10**

Grep for `TelegramRetryAfter` / `TelegramBadRequest` / `TelegramServerError` /
`TelegramAPIError` / `TelegramNetworkError` returns **only `TelegramForbiddenError`**.
At [bot.py:4502](bot.py:4502) the broadcast uses a fixed `await asyncio.sleep(0.05)`
which on a large user base will *eventually* hit Telegram's per-bot rate limit
(~30 msgs/s, harder caps on broadcast). When that happens, aiogram raises
`TelegramRetryAfter(retry_after=N)` — and the current code lumps it into the
generic `except Exception as e:` branch at
[bot.py:4495-4501](bot.py:4495) and marks the recipient as "failed" forever
without retrying. A single 429 can permanently fail hundreds of valid recipients.

**Remediation — patch broadcast loop:**

```python
# bot.py:4487 — replace the existing per-uid loop body
from aiogram.exceptions import (
    TelegramForbiddenError, TelegramRetryAfter, TelegramBadRequest,
)
for uid in user_ids:
    try:
        await bot.send_message(uid, text)
        delivered += 1
    except TelegramForbiddenError:
        failed += 1
        failed_ids.append(uid)
        logger.info("broadcast.send_failed uid=%s reason=blocked", uid)
    except TelegramRetryAfter as e:
        # Telegram explicitly told us to wait — honour it, then retry this uid once
        logger.warning("broadcast.retry_after uid=%s seconds=%s", uid, e.retry_after)
        await asyncio.sleep(e.retry_after + 0.5)
        try:
            await bot.send_message(uid, text)
            delivered += 1
            continue
        except Exception as e2:
            failed += 1
            failed_ids.append(uid)
            logger.warning("broadcast.send_failed uid=%s reason=%s",
                           uid, type(e2).__name__)
    except TelegramBadRequest as e:
        # Permanent: chat not found / message too long etc. — don't retry
        failed += 1
        failed_ids.append(uid)
        logger.warning("broadcast.send_failed uid=%s reason=bad_request detail=%s", uid, e)
    except Exception as e:
        failed += 1
        failed_ids.append(uid)
        logger.warning("broadcast.send_failed uid=%s reason=%s",
                       uid, type(e).__name__)
    await asyncio.sleep(0.05)
```

Apply the same pattern in [services.py:715-735 (`_send_morning`)](services.py:715)
and [services.py:780-815 (`_send_evening`)](services.py:780): both currently
have only `TelegramForbiddenError` + catch-all.

---

### Finding 2.2 — `TelegramBadRequest` for "message is not modified" / "message to edit not found" caught as generic `Exception`

**Severity: 4/10**

There are ~30 sites that do `callback.message.edit_text(...)` wrapped in
`except Exception` — e.g.
[bot.py:1793](bot.py:1793), [bot.py:2105](bot.py:2105), [bot.py:2125](bot.py:2125),
[bot.py:2169](bot.py:2169), [bot.py:5451-5613](bot.py:5451) (the analytics views).
The fallback (re-`answer` the message) works, but the catch is too wide — it
also swallows network failures and serializer bugs.

**Remediation — narrow it:**

```python
# bot.py:1792-1796 (canonical version of the helper that ~10 sites already use)
from aiogram.exceptions import TelegramBadRequest
async def _edit_or_answer_settings(
    callback: CallbackQuery, text: str, reply_markup: InlineKeyboardMarkup
) -> None:
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        # "message is not modified" / "message to edit not found" — expected
        logger.debug("settings.edit_skipped user=%s reason=%s",
                     callback.from_user.id, e.message)
        await callback.message.answer(text, reply_markup=reply_markup)
```

---

### Finding 2.3 — Authorization checks scattered, no helper / decorator

**Severity: 4/10**

Two patterns recur:

- **Admin gate:** `if not is_admin(message.from_user.id): await message.answer("❌ Команда только для админов."); return` — copy-pasted at least at
  [bot.py:4456, 4525, 5237, 5640, 5662](bot.py:4456) (and more).
- **Owner gate (anti-spoof):** `if callback.from_user.id != target_user_id: await callback.answer(t("common.not_yours_...", ...), show_alert=True); return` — copy-pasted at least at
  [bot.py:1761, 2100, 2119, 2140, 2156, 2319, 6101, ...](bot.py:1761).

These aren't *bugs*, but they bypass the central error handler (Finding 1.1) and
fragment the "403"-equivalent category. Adding a small decorator gives a single
place to log/instrument auth failures:

```python
# bot.py — near is_admin()
def admin_only(handler):
    @functools.wraps(handler)
    async def wrapper(message_or_cb, *args, **kwargs):
        uid = message_or_cb.from_user.id
        if not is_admin(uid):
            logger.info("auth.denied admin handler=%s uid=%s", handler.__name__, uid)
            if isinstance(message_or_cb, CallbackQuery):
                await message_or_cb.answer("❌ Только для админов.", show_alert=True)
            else:
                await message_or_cb.answer("❌ Команда только для админов.")
            return
        return await handler(message_or_cb, *args, **kwargs)
    return wrapper
```

---

## 3. ASYNC ERROR HANDLING

### Finding 3.1 — Background tasks have no `add_done_callback` — silent death

**Severity: 9/10**

Four `asyncio.create_task(...)` sites; none of them attach an exception logger:

- [bot.py:2964](bot.py:2964): `active_timers[user_id] = task` — per-user pomodoro
- [bot.py:6989-6993](bot.py:6989): the three schedulers
  (`streak_scheduler`, `reminder_scheduler`, `leaderboard_scheduler`)

The schedulers each have an inner `while True: try: ... except Exception:
logger.error(...)` loop, so they survive per-tick failures. **But** any
exception raised *outside* that inner try (e.g. an exception during
`pytz.timezone(...)` setup before the loop, or a `CancelledError` re-raise on
shutdown bug) will kill the task, and the only signal will be the asyncio
warning "Task exception was never retrieved" which only fires when the task is
GC'd — typically minutes or never. The bot will keep polling messages but
streaks/reminders/rollover stop silently.

[bot.py:2964](bot.py:2964) `run_timer_task` is worse: it has
`except asyncio.CancelledError: pass` and `finally: active_timers.pop(...)`, but
**no generic `except Exception`** between [bot.py:2913](bot.py:2913) and
[bot.py:2956](bot.py:2956). A failed
`study_service.complete_session` or `event_repo.log` raises, the task dies,
the user's session is dropped, and the only trace is a stderr warning.

**Remediation — add a helper and use it everywhere:**

```python
# bot.py — near setup_logger()
def _log_task_exception(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.exception(
            "background_task.crashed name=%s",
            task.get_name(),
            exc_info=exc,
        )

# bot.py:2959 — start_timer
def start_timer(chat_id, state, user_id, duration):
    old = active_timers.get(user_id)
    if old and not old.done():
        old.cancel()
    task = asyncio.create_task(
        run_timer_task(chat_id, state, user_id, duration),
        name=f"timer-{user_id}",
    )
    task.add_done_callback(_log_task_exception)
    active_timers[user_id] = task

# bot.py:6989 — main()
background_tasks = []
for coro, name in (
    (streak_scheduler(streak_service, user_repo, backup_service), "streak"),
    (reminder_scheduler(reminder_service, user_repo), "reminder"),
    (leaderboard_scheduler(leaderboard_service), "leaderboard"),
):
    t_ = asyncio.create_task(coro, name=name)
    t_.add_done_callback(_log_task_exception)
    background_tasks.append(t_)
```

Additionally, harden [bot.py:2912-2956 `run_timer_task`](bot.py:2912):

```python
async def run_timer_task(chat_id, state, user_id, duration):
    try:
        # ... existing body unchanged ...
    except asyncio.CancelledError:
        raise  # let it propagate so the cancel completes; current `pass` is fine but explicit raise is more idiomatic
    except Exception:
        logger.exception(
            "timer.task_crashed user_id=%s duration=%s", user_id, duration,
        )
    finally:
        active_timers.pop(user_id, None)
```

---

### Finding 3.2 — `asyncio.run(main())` has no top-level exception handling

**Severity: 6/10**

At [bot.py:7021-7022](bot.py:7021):

```python
if __name__ == "__main__":
    asyncio.run(main())
```

If `main()` raises during startup (e.g. SQLite locked, network down, malformed
`achievements.json`), the process exits with a traceback to stderr but no log
entry in `bot.log` (because the file handler is attached to `studybuddy_bot`,
not the root logger, and bare `print`/uncaught traceback go to stderr). In a
Docker `restart=always` setup this becomes a crash-loop with no diagnostic
breadcrumb in the persistent log file.

**Remediation:**

```python
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("app.shutdown reason=keyboard_interrupt")
    except Exception:
        logger.exception("app.fatal_startup_error")
        raise
```

---

### Finding 3.3 — `dp.start_polling` is not wrapped; no graceful shutdown on polling crash

**Severity: 5/10**

At [bot.py:7014-7019](bot.py:7014):

```python
try:
    await dp.start_polling(bot)
finally:
    logger.info("app.shutdown")
    for t in background_tasks:
        t.cancel()
```

The `finally` cancels tasks but doesn't `await` their cancellation, so on a
real shutdown the schedulers' `asyncio.sleep(60)` calls may emit
"Task was destroyed but it is pending!" warnings, and any in-flight DB writes
inside `BackupService._vacuum_into` aren't awaited to completion.

**Remediation:**

```python
try:
    await dp.start_polling(bot)
finally:
    logger.info("app.shutdown")
    for t_ in background_tasks:
        t_.cancel()
    # await cancellation so loop drains cleanly
    await asyncio.gather(*background_tasks, return_exceptions=True)
    try:
        await bot.session.close()
    except Exception:
        logger.debug("bot.session.close_failed", exc_info=True)
    await db.close()
```

(`db.close()` is currently never called — would also fix a minor WAL-flush
issue on shutdown.)

---

### Finding 3.4 — Middleware swallows but does not surface `username_sync` failures

**Severity: 3/10**

[bot.py:182-197](bot.py:182) `UsernameSyncMiddleware`: on any DB error during
`refresh_username`, logs `warning` and continues. This is deliberate (commented
"Sync failure тихо логируется и НЕ должна прерывать handler"), and is the right
call. **No change needed**, listed here for completeness.

---

## 4. ERROR RECOVERY

### Finding 4.1 — No retry/back-off layer for Telegram API calls

**Severity: 7/10**

Telegram occasionally returns transient `TelegramServerError` (5xx) or
`TelegramNetworkError` (connection blip). The codebase has *zero* `retry`,
`backoff`, or `circuit_breaker` infrastructure — every `bot.send_message` /
`edit_text` / `send_animation` is one-shot. A 1-second network glitch during
the streak-bonus notification at
[services.py:907-921](services.py:907) costs the user that day's bonus message.

**Remediation — minimal retry helper, no dependency added:**

```python
# services.py — top of file
async def _telegram_call_with_retry(coro_factory, *, retries: int = 2):
    """
    Run `coro_factory()` with up to `retries` exponential-backoff retries on
    transient Telegram errors. coro_factory is a 0-arg callable returning the
    coroutine — needed because awaitables aren't reusable.
    """
    from aiogram.exceptions import (
        TelegramRetryAfter, TelegramServerError, TelegramNetworkError,
    )
    attempt = 0
    while True:
        try:
            return await coro_factory()
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 0.5)
        except (TelegramServerError, TelegramNetworkError) as e:
            if attempt >= retries:
                raise
            await asyncio.sleep(0.5 * (2 ** attempt))
            attempt += 1
```

Apply at the hot notification sites — for example at
[services.py:599](services.py:599):

```python
await _telegram_call_with_retry(
    lambda: self.bot.send_message(user_id, msg, parse_mode="HTML")
)
```

---

### Finding 4.2 — No circuit breaker

**Severity: 3/10 — Unable to verify need**

I don't see evidence this bot has scaled to a size where a circuit breaker
adds value (no caching of failed `chat_id`s, no batch sender). At ~533 tests
and presumably <1000 users (per the README description of "<100 пользователей"
heuristic in [bot.py:170](bot.py:170)), the simpler retry from 4.1 is enough.
Flagged only because the audit brief asked.

---

### Finding 4.3 — Existing fallback strategies are good

**Severity: N/A (positive)**

Several pieces of well-designed graceful degradation, called out so they
aren't accidentally regressed:

- [services.py:411-426 `render_pet`](services.py:411) — 3-tier fallback chain
  (specific combo → emotion default → universal happy → raise).
- [services.py:777-795 evening reminder sad-pet asset](services.py:777) —
  catches `FileNotFoundError` and falls back to text-only.
- [services.py:752-757 unknown TZ in `_send_evening`](services.py:752) —
  defensive `datetime.now()` fallback.
- [services.py:2568-2573 BackupService.maybe_backup_for_today](services.py:2568)
  — returns `None` on failure, doesn't crash the scheduler tick that called it.
- The `_edit_or_answer_settings` pattern ([bot.py:1788-1796](bot.py:1788)):
  edit-then-answer fallback for stale callback messages — solid.

---

## 5. ERROR INFORMATION

### Finding 5.1 — Exception class name + message leaked to admin UI

**Severity: 6/10**

Three sites leak raw `type(e).__name__: {e}` strings into Telegram messages:

- [bot.py:4442](bot.py:4442): `await message.answer(f"❌ Ошибка: {str(e)}")` —
  inside `/reply` admin command. Reaches whichever admin used the command.
- [bot.py:5206](bot.py:5206): `await reply_target.answer(f"❌ Export-all failed:
  {type(e).__name__}: {e}")`
- [bot.py:5268](bot.py:5268): `await message.answer(f"❌ Export failed:
  {type(e).__name__}: {e}")`
- [bot.py:5587](bot.py:5587): `await callback.answer(f"❌ {type(e).__name__}:
  {e}", show_alert=True)`

These are admin-only flows, so the *severity* is low (admins are trusted), but
the *pattern* is wrong for a few reasons: ① it bypasses i18n, ② if any of these
handlers is later opened to power-users it becomes a real leak, ③ Telegram has
a 200-char limit on `callback.answer(show_alert=True)` — a long SQL traceback
will be truncated and the actual diagnostic value lost.

**Remediation:** show a constant string to the user, log the detail:

```python
# bot.py:5263-5269 (representative; apply same pattern to 4442, 5206, 5587)
try:
    csv_bytes, row_count = await analytics_service.export_table_csv(arg)
except Exception:
    logger.exception("export.failed alias=%s", arg)
    await message.answer(
        "❌ Export failed. Подробности в bot.log "
        "(grep export.failed alias=<alias>)."
    )
    return
```

---

### Finding 5.2 — No dev/prod separation for error verbosity

**Severity: 4/10**

`LOG_LEVEL` exists ([bot.py:86](bot.py:86)) but there is no `DEBUG`/`ENV` flag
that gates user-visible error detail. In a dev environment you might want
`type(e).__name__` in the alert; in prod you don't. Currently the only knob is
the log level, which affects only what's written to `bot.log` — not what's
shown to the admin.

**Remediation — one env var, one helper:**

```python
# bot.py — near other env reads
DEBUG_MODE = os.getenv("PALPH_DEBUG", "").lower() in ("1", "true", "yes")

def safe_error_text(e: Exception, prefix: str = "❌ Ошибка") -> str:
    if DEBUG_MODE:
        return f"{prefix}: {type(e).__name__}: {e}"
    return f"{prefix}. Попробуй позже."
```

Then the leak sites become `await message.answer(safe_error_text(e, "❌ Export failed"))`.
Add `PALPH_DEBUG=` to [.env.example](.env.example).

---

### Finding 5.3 — `logger.error(f"...{e}")` loses the traceback (duplicate of 1.3, scored separately because this is the user-facing-debuggability angle)

**Severity: 7/10**

Already covered in Finding 1.3. The "Error information completeness" dimension
of the audit brief is most violated here: when a production user reports
"settings didn't save", the only line you'll find is `bot.log: 'Error toggling
setting: object of type X is not Y'` with no file/line. Half-an-hour repro
session vs. a five-second `grep`.

Same patch as 1.3. Combined with Finding 1.1 (central handler also using
`logger.exception`), this single change probably has the highest
debuggability ROI in this audit.

---

### Finding 5.4 — Bare logger handlers, root logger never configured

**Severity: 3/10**

[bot.py:78-116](bot.py:78) configures only the `studybuddy_bot` logger. The
root logger has no handler. Any third-party library that does
`logging.getLogger(__name__).error(...)` and isn't in the explicit silenced
list ([bot.py:113](bot.py:113)) will have its messages dropped silently. Low
priority because aiogram/aiohttp/aiosqlite are explicitly silenced and the
known-noisy ones are covered, but a future dependency addition will hit this.

**Remediation:** add a root-level NullHandler + propagate=True so unknown
loggers at least reach a sink:

```python
# bot.py:116 (end of setup_logger)
root = logging.getLogger()
if not root.handlers:
    root.addHandler(logging.NullHandler())
```

---

## Aggregate priority list

| # | Finding | Severity |
|---|---------|----------|
| 1.1 | No centralized aiogram error handler | **9/10** |
| 3.1 | Background tasks die silently (no `add_done_callback`) | **9/10** |
| 2.1 | No `TelegramRetryAfter` handling (broadcast + reminders) | **8/10** |
| 4.1 | No retry/back-off layer for Telegram API calls | **7/10** |
| 1.3 / 5.3 | Lossy logging — zero uses of `logger.exception` | **7/10 / 7/10** |
| 1.2 | No custom exception hierarchy; semantics via string equality | **6/10** |
| 3.2 | `asyncio.run(main())` has no top-level exception capture | **6/10** |
| 5.1 | Raw exception text leaked to admin UI in 4 sites | **6/10** |
| 1.4 | `except Exception: pass` swallows in DB migrations + UI | **5/10** |
| 3.3 | `dp.start_polling` shutdown doesn't await task cancellation | **5/10** |
| 5.2 | No dev/prod separation for error verbosity | **4/10** |
| 2.2 | `TelegramBadRequest` caught as generic `Exception` | **4/10** |
| 2.3 | Admin/owner gates copy-pasted instead of decorated | **4/10** |
| 5.4 | Root logger has no handler — third-party errors can vanish | **3/10** |
| 4.2 | Circuit breaker — **Unable to verify need** at current scale | **3/10** |
| 3.4 | `UsernameSyncMiddleware` swallows DB errors (intentional, no change) | **N/A** |
| 4.3 | Existing fallback chains (`render_pet`, evening reminder, backup) are good | **N/A (positive)** |

---

## Suggested implementation order

1. **Finding 1.3 / 5.3** (15 min, mechanical sed-like change) — gives you a
   traceback in `bot.log` immediately, makes everything else easier to debug.
2. **Finding 3.1** (30 min) — add `_log_task_exception` helper, wire into the
   4 `create_task` sites. Cheap, prevents silent scheduler death.
3. **Finding 1.1** (1 h) — global `@dp.errors()` handler. After this lands, you
   can start *removing* defensive `try/except` from individual handlers.
4. **Finding 2.1 + 4.1** (1 h) — Telegram-specific retry/back-off and 429
   handling. Required before scaling past current user base.
5. **Finding 1.2** (1 h) — introduce `errors.py`, migrate the 3 smuggling sites.
   Low-risk, pays compounding interest as the codebase grows.
6. The rest as time allows.
