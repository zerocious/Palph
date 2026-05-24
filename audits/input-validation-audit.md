# Input Validation Security Audit

**Project:** Telegram Study Buddy bot (`tg bot 0.6 (settings not working)`)  
**Date:** 2026-05-22  
**Scope:** Python sources — `bot.py`, `db.py`, `repository.py`, `services.py`, `i18n.py`, `locale_bot.py`, `user_task_txt.py`, `fsm_storage.py`, `tasks.py`, `parse_logs.py`, `scripts/*.py`, `tests/*.py`  
**Method:** Static review, pattern grep (`execute`, f-strings in SQL, `subprocess`, `open`, `Path`, `eval`, etc.), handler mapping, targeted pytest (`47 passed` on validation-related tests)

---

## Executive summary

The bot is a **Telegram-only** application (aiogram 3.x + SQLite). There is **no evidence of classic SQL injection or OS command injection** in production paths: queries use `?` placeholders, dynamic SQL fragments are either allowlisted (`EXPORTABLE_TABLES`, pet column names) or built from counted `?` placeholders for `IN (...)` clauses.

The main gaps are **Telegram HTML/markup injection** on user-authored flashcard content, **path traversal risk** when `subject_id` from callback data is passed into filesystem loaders without allowlisting, and **incomplete field-length validation** on imported user tasks. Authorization on callback `user_id` fields is generally solid for profile/settings/flashcards/tasks.

**Risk score: 4 / 10** — no confirmed RCE or SQLi; issues are mostly integrity, markup abuse, and constrained file read outside `study_materials/`.

---

## 1. Validation Matrix

Legend: **Pass** = adequate for threat model; **Partial** = some checks; **Fail** = missing or bypassable; **N/A** = not applicable.

| Endpoint / handler group | Inputs validated? | SQL | Command | Path | Type check | Status |
|--------------------------|-------------------|-----|---------|------|------------|--------|
| `/start`, deep link `friend_*` | Token lookup; self-invite blocked | N/A | N/A | N/A | int user ids | **Pass** |
| Setup (morning/evening time) | `TIME_RE`, skip | N/A | N/A | N/A | HH:MM | **Pass** |
| `lang:set:*` | `SUPPORTED_LOCALES` | N/A | N/A | N/A | locale enum | **Pass** |
| FAQ (`faq:show:*`) | `_faq_lookup` on static ids | N/A | N/A | N/A | catalog id | **Pass** |
| Profile / `show_progress:*` | `from_user.id == target` | Param | N/A | N/A | int | **Pass** |
| Settings toggles / time / TZ | toggle allowlist; `TIME_RE`; `TZ_IDS` | Param | N/A | N/A | slot/tz enum | **Pass** |
| `settings_flash_source:*` | ownership + cycle allowlist | Param | N/A | N/A | int user | **Pass** |
| User flashcards (`fc_*`) | term/def length; ownership | Param | N/A | **Partial** subject_id | int user | **Partial** |
| User tasks import (`ut_*`) | `.txt`, 64 KiB, parse rules | Param | N/A | **Partial** subject_id | file + count | **Partial** |
| Timer / `/stop` | duration 5–120; FSM guard | Param | N/A | N/A | int minutes | **Pass** |
| Quiz (subject/mode buttons) | `subject_id_from_button`, `mode_id_from_button` | Param | N/A | **Pass** (buttons) | whitelist labels | **Pass** |
| Quiz callbacks (`fc_study`, `ut_study`) | ownership | Param | N/A | **Fail** subject_id in callback | int user | **Partial** |
| MCQ / flash study | FSM state; flash quality 1/3/5 | Param | N/A | N/A | state + enum | **Partial** (HTML) |
| Session rating `rate:*` | score range; `set_session_score(user)` | Param | N/A | N/A | int | **Pass** |
| Tips | static JSON; `html_escape` on tip body | N/A | N/A | fixed paths | catalog | **Pass** |
| Pet shop / rename | catalog keys; name len ≤20 | Param col allowlist | N/A | asset render | catalog | **Partial** (HTML name) |
| Friends FSM / accept / reject | `parse_friend_query`; pending row DELETE | Param | N/A | N/A | int | **Pass** |
| Admin `/reply`, `/broadcast` | `is_admin`; int user_id | N/A | N/A | N/A | Partial length | **Partial** |
| Admin `/export`, `anlt:export:*` | alias ∈ `EXPORTABLE_TABLES` | Allowlist table name | N/A | N/A | alias enum | **Pass** |
| Admin analytics commands | `is_admin`; hours/days clamped | Param | N/A | fixed log path | int range | **Pass** |
| Catch-all support `handle_any_message` | 1 msg / 60s per user | N/A | N/A | fixed `messages.jsonl` | rate limit | **Partial** |
| Middleware `RateLimitMiddleware` | 30 actions / 60s (non-admin) | N/A | N/A | N/A | user id | **Pass** |
| `repository.py` / `services.py` (non-handler) | parameterized SQL | Param | N/A | backup path built server-side | typed | **Pass** |
| `i18n.py` / `locale_bot.py` | locale ∈ supported; fixed paths | N/A | N/A | no user path | locale | **Pass** |
| `user_task_txt.py` | format rules | N/A | N/A | N/A | **Partial** field lengths | **Partial** |
| `scripts/*.py` | dev tooling (not user-facing) | N/A | N/A | writes repo files | N/A | **N/A** |
| `tests/*.py` | test doubles | N/A | N/A | temp DB | N/A | **N/A** |

**Handler inventory:** 100 `@router.message` / `@router.callback_query` registrations in `bot.py` (lines 1209–6722 region). Matrix rows group them by feature; per-handler behavior follows the same pattern within each row.

---

## 2. Findings

### 2.1 Telegram HTML injection via user flashcards

- **Severity:** Medium  
- **CWE:** [CWE-79](https://cwe.mitre.org/data/definitions/79.html) (presentation layer — Telegram HTML parse mode)

**Evidence:**

- `bot.py` — `handle_fc_definition` sends saved term/definition with `parse_mode="HTML"` without escaping (lines 2244–2247).
- `bot.py` — `handle_flashcard_show` embeds `card['term']` and `card['definition']` in HTML (lines 3904–3911).
- `bot.py` — tips flow correctly uses `html_escape` (e.g. lines 4144–4145); flashcard paths do not.

**Why it matters:** Telegram interprets `<`, `>`, and entities in HTML mode. A user can break message layout, inject links, or phish other users who view the same chat history (less relevant in private bot chats, but affects admins viewing forwarded content and any shared screenshots).

**Exploitability:** High for self-content; send flashcard term `&lt;b&gt;click&lt;/b&gt;` or `<a href="https://evil.example">free coins</a>` during create/study.

**Minimal repro:**

1. Open “My flashcards”, add card with term `<b>URGENT</b>` and definition `<a href="https://example.com">link</a>`.
2. Start study session → bot renders attacker-controlled markup.

**Remediation:**

```python
from html import escape as html_escape

# In handle_fc_definition (confirmation message):
await message.answer(
    t("fc.saved", locale,
      term=html_escape(term),
      definition=html_escape(definition)),
    parse_mode="HTML",
    reply_markup=kb.as_markup(),
)

# In handle_flashcard_show:
f"<b>{html_escape(card['term'])}</b>\n\n"
f"💡 <i>{html_escape(card['definition'])}</i>\n\n"
```

Apply the same pattern anywhere user text is combined with `parse_mode="HTML"`.

---

### 2.2 Path traversal / arbitrary file read via `subject_id` in callbacks

- **Severity:** Medium  
- **CWE:** [CWE-22](https://cwe.mitre.org/data/definitions/22.html)

**Evidence:**

- Callback handlers take `subject_id` from `callback.data` without checking `SUBJECT_IDS`: e.g. `handle_fc_study` (2251–2268), `handle_ut_import_start` (2352–2368), `fc_add` / `fc_list` (2092–2147).
- Filesystem loaders join user-influenced `subject_id`:

```935:938:bot.py
def load_quiz_section(section: str, subject_id: str = "industrial-management") -> list[QuizTerm]:
    file_path = STUDY_MATERIALS_PATH / subject_id / "situational" / f"section-{section.lower()}.txt"
    if not file_path.exists():
```

Similar joins in `load_mcq` (958), `load_tasks` (990), `load_flashcards` (1051).

**Why it matters:** A crafted callback such as `fc_study:{uid}:..` resolves to `study_materials/../flashcards.txt`, potentially reading files outside the intended catalog (e.g. repo root, `.env` if present and named appropriately). Impact depends on host layout; still violates expected sandbox.

**Exploitability:** Medium — requires crafting `callback_data` (Telegram client or modified inline keyboard). Ownership check on `user_id` still applies; only affects attacker’s session.

**Minimal repro:**

1. While authenticated, trigger callback `fc_study:<your_id>:..` (via client that allows custom callback_data).
2. Observe `start_flashcard_session` calling `load_flashcards("..")` → path outside `study_materials/<canonical_subject>/`.

**Remediation:**

```python
from locale_bot import SUBJECT_IDS  # or shared constants module

def validate_subject_id(subject_id: str) -> str | None:
    return subject_id if subject_id in SUBJECT_IDS else None

# At start of each fc_*/ut_* handler after parsing:
subject_id = validate_subject_id(subject_id)
if subject_id is None:
    await callback.answer("Unknown subject", show_alert=True)
    return
```

Optional hardening in loaders:

```python
def _safe_subject_path(subject_id: str) -> Path | None:
    if subject_id not in SUBJECT_IDS:
        return None
    base = (STUDY_MATERIALS_PATH / subject_id).resolve()
    if not str(base).startswith(str(STUDY_MATERIALS_PATH.resolve())):
        return None
    return base
```

---

### 2.3 User task import — no per-field length limits

- **Severity:** Medium  
- **CWE:** [CWE-400](https://cwe.mitre.org/data/definitions/400.html)

**Evidence:**

- `user_task_txt.py` `parse_user_tasks_txt` (68–104): accepts arbitrary-length `problem`, `accepted[]`, `hint` per line (only file total capped at 64 KiB in `bot.py` 2395–2398).
- `repository.py` `UserTaskRepository.bulk_create` (544–573): inserts parsed strings without truncation.

**Why it matters:** A single line can consume most of the 64 KiB budget with huge `problem` text → large DB rows, fat Telegram messages when listing/studying tasks, and event JSON bloat.

**Exploitability:** Low–Medium — upload `.txt` with one very long line (within 64 KiB).

**Remediation:**

```python
MAX_PROBLEM_LEN = 2000
MAX_ANSWER_LEN = 500
MAX_HINT_LEN = 500

# In parse_user_tasks_txt after stripping:
if len(problem) > MAX_PROBLEM_LEN:
    errors.append(f"Строка {line_no}: вопрос слишком длинный.")
    continue
accepted = [a for a in accepted if a][:20]  # cap count
if any(len(a) > MAX_ANSWER_LEN for a in accepted):
    ...
```

---

### 2.4 Pet name reflected in HTML caption without escaping

- **Severity:** Low  
- **CWE:** CWE-79

**Evidence:** `_send_pet_menu` (`bot.py` 5871–5878) uses `f"🐾 <b>{pet['name']}</b>\n\n"` with `parse_mode="HTML"`. Rename allows 20 chars but not character class restriction (`pet_rename_process`, 6136–6159).

**Why it matters:** Same class of markup injection as flashcards, limited to pet name field.

**Remediation:** `html_escape(pet['name'])` in caption; optionally restrict name to alphanumeric + spaces.

---

### 2.5 Admin `/reply` and support relay — unbounded / unescaped text

- **Severity:** Low  
- **CWE:** CWE-20 / CWE-79

**Evidence:**

- `cmd_reply` (`bot.py` 4425–4442): forwards `reply_text` verbatim to users.
- `handle_any_message` (`bot.py` 6753–6755): forwards `message.text` to all admins.

**Why it matters:** Compromised admin account or social-engineering of admin could send HTML/markup to users if `parse_mode` were added later; today mostly plain text. Unbounded broadcast text can hit Telegram API limits (admin-only).

**Remediation:** Document max length (e.g. 4096); use plain text only; `html_escape` if switching to HTML.

---

### 2.6 Positive controls (no issue — documented for completeness)

| Control | Location |
|---------|----------|
| Parameterized SQLite | `repository.py` throughout (`?` bindings) |
| `IN (...)` clauses | Placeholder count only; values bound (`repository.py` 756–815, `bot.py` 1609–1658) |
| Export table names | `AnalyticsService.EXPORTABLE_TABLES` allowlist before `SELECT * FROM {table}` (`services.py` 1731–1734) |
| Pet UPDATE column | `item_type in ("color","accessory")` → fixed column names (`repository.py` 1254–1257, 1287–1290) |
| Callback IDOR checks | `from_user.id != user_id` on profile, FC, UT, achievements (`bot.py` multiple) |
| TZ allowlist | `TZ_IDS` (`bot.py` 2734–2735) |
| Friend accept | Deletes pending row `from_user_id=? AND to_user_id=?` (`repository.py` 1964–1971) |
| Rate limiting | `RateLimitMiddleware` + `admin_message_limiter` (`bot.py` 200–247, 6729–6734) |
| No `subprocess` / `eval` / `pickle` in app code | Grep across `*.py` (scripts are build-only) |

---

## 3. Summary

### Risk score: **4 / 10**

Justification: Strong SQL parameterization and admin/export allowlists reduce classic injection risk. Residual issues are **markup injection** (user content + HTML mode), **filesystem path composition** without `subject_id` validation on callback-driven flows, and **DoS-ish resource use** via large parsed tasks. No command execution or NoSQL layer was found.

### Top prioritized fixes

1. **Escape HTML** (or disable HTML mode) for all user-authored strings: flashcards, pet name, task previews if HTML is used.  
2. **Allowlist `subject_id`** in every `fc_*` / `ut_*` handler and in `load_*` helpers (defense in depth).  
3. **Add per-field max lengths** in `parse_user_tasks_txt` and optionally in `UserFlashcardRepository.create`.  
4. **Centralize `validate_subject_id()`** shared by callbacks and FSM state before import/study.  
5. **Audit remaining `parse_mode="HTML"`** usages with a grep for user/interpolated variables.

### Pytest note

Ran: `tests/test_i18n.py`, `test_user_tasks.py`, `test_friend_invite_tokens.py`, `test_settings_fixes.py`, `test_admin_message_rate_limit.py` — **47 passed**. Tests confirm notification upsert, rate limits, and task parsing happy paths; they do **not** cover HTML injection or path traversal (gaps for future security tests).

---

## 4. Checklist diff

| Category | Result | Notes |
|----------|--------|-------|
| SQL Injection | **Pass** | Parameterized queries; dynamic table/column names allowlisted |
| NoSQL Injection | **N/A** | SQLite only |
| Command Injection | **Pass** | No `subprocess`, `os.system`, `shell=True`, `eval`, `exec` in runtime code |
| XSS / markup | **Fail** | User flashcards (and pet name) in Telegram HTML mode without encoding |
| XXE | **N/A** | No XML parsing of user uploads |
| Path Traversal | **Fail** | `subject_id` from callbacks not restricted before `Path` joins |
| Request validation | **Partial** | Rate limits, file size/type, time/TZ enums; gaps on field lengths and callback `subject_id` |

---

## 5. Files reviewed

| File | Role |
|------|------|
| `bot.py` | All Telegram handlers, loaders, middleware |
| `db.py` | Schema / migrations |
| `repository.py` | Data access |
| `services.py` | Business logic, analytics export, backup |
| `i18n.py`, `locale_bot.py` | Locales (fixed paths) |
| `user_task_txt.py` | Task file parser |
| `fsm_storage.py` | FSM persistence (JSON, keyed by Telegram ids) |
| `tasks.py` | Schedulers (no user input) |
| `parse_logs.py` | Admin log ETL (fixed paths from env) |
| `scripts/*.py` | i18n build / patch tooling (not exposed to users) |
| `tests/*.py` | Regression coverage sampled via pytest |

---

*End of audit report.*
