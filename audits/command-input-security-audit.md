# Command & User Input Security Audit

**Project:** Palph Telegram bot  
**Audit date:** 2026-06-01 · **Remediation:** 2026-06-01 (follow-up) · **Doc sync:** 2026-09-05  
**Scope:** Telegram commands, callback_data parsing, FSM text input, rate limiting, admin authorization  
**Prior work:** `audits/input-validation-audit.md`, `audits/file-upload-security-audit.md`, `file_upload_security.py`  
**Method:** Static review of `bot.py`, helpers, repositories; pytest on security-related tests

---

## Executive summary

The bot has **no SQL injection or OS command injection** in user-facing paths. Authorization on callback `user_id` fields is consistently enforced for profile, flashcards, tasks, settings, and friends.

**Fixes applied (2026-06-01):**

- Allowlist `subject_id` in all `fc_*` / `ut_*` / `taskgrp:` callback handlers and FSM flashcard create flow
- Defense-in-depth: `safe_subject_dir()` in all `load_*` filesystem helpers; quiz section keys allowlisted
- Telegram HTML escaping for user flashcard term/definition and pet name in HTML captions
- `safe_task_image_filename()` applied at task JSON load time
- **Follow-up:** explicit caps on `/broadcast`, `/reply`, support relay, friend-add FSM input; plain-text list previews sanitized; integration tests for callback subject rejection

**Risk score: 2 / 10** (down from 3/10 after initial pass)

---

## Findings by severity

### Critical — 0

No confirmed RCE, SQLi, or authentication bypass.

### High — 0 (1 remediated)

| ID | Issue | Status |
|----|-------|--------|
| H-1 | Path traversal via crafted `subject_id` in callbacks → `load_*` path joins | **Fixed** — `validate_subject_id()` + `safe_subject_dir()` |

### Medium — 0 open (4 remediated)

| ID | Issue | Status |
|----|-------|--------|
| M-1 | Telegram HTML injection via user flashcards | **Fixed** — `html_escape()` in save confirmation, `_send_flashcard`, `flash:show` |
| M-2 | User task `.txt` per-field length limits | **OK** — `user_task_txt.py` |
| M-3 | Task upload MIME/size/magic validation | **OK** — `file_upload_security.py` |
| M-4 | `subject_id` not validated on import callback start | **Fixed** |
| M-5 | Quiz section key not allowlisted | **Fixed** — `QUIZ_SECTION_KEYS` |

### Low — 0 open (4 remediated)

| ID | Issue | Status |
|----|-------|--------|
| L-1 | Pet name HTML in caption | **Fixed** — `html_escape(pet['name'])` |
| L-2 | Admin `/reply` / `/broadcast` unbounded text | **Fixed** — `truncate_text` / `truncate_for_telegram_message` (4096 cap) |
| L-3 | Support catch-all forwards full text | **Fixed** — `SUPPORT_MESSAGE_MAX_LEN` + prefix-aware truncate |
| L-4 | Friend-add FSM no explicit max input length | **Fixed** — `FRIEND_QUERY_MAX_LEN = 64`; username escaped in HTML reply |

### OK — positive controls

See prior audit; unchanged (parameterized SQL, admin gates, rate limits, IDOR checks, parsers).

---

## Tests

| File | Coverage |
|------|----------|
| `tests/test_command_input_security.py` | Loader sandbox, text limits, `_callback_allowlisted_subject` |
| `tests/test_file_upload_security.py` | Upload metadata, subject_id, field limits, path resolve |
| `tests/test_user_tasks.py` | Parser happy paths + field limits |
| `tests/test_admin_message_rate_limit.py` | Support spam limit |
| `tests/test_rate_limiter.py` | Middleware limiter |
| `tests/test_username_search.py` | Friend query parser |

Run security-related suite:

```bash
python -m pytest tests/test_command_input_security.py tests/test_file_upload_security.py tests/test_user_tasks.py tests/test_admin_message_rate_limit.py tests/test_rate_limiter.py tests/test_username_search.py -q
```

---

## Files changed

| File | Change |
|------|--------|
| `bot.py` | subject allowlist, HTML escape, admin/support/friend input caps, list preview sanitize |
| `file_upload_security.py` | `truncate_text`, `sanitize_plain_preview`, `truncate_for_telegram_message`, constants |
| `tests/test_command_input_security.py` | Loader + callback + limit tests |
| `audits/command-input-security-audit.md` | This document |

---

*End of command input security audit.*
