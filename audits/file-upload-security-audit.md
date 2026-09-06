# File Upload Security Audit

**Project:** Palph Telegram bot (`tg bot 0.6 (settings not working)`)  
**Audit date:** 2026-05-22 · **Doc sync:** 2026-09-05 (product v0.8, 802 pytest)  
**Scope:** All user-facing and operator-facing file ingest, outbound file generation, and filesystem reads tied to uploaded or user-influenced identifiers  
**Method:** Static code review, ripgrep (`document`, `download`, `FSInputFile`, `ZipFile`, `PIL`, `open`, `write_bytes`), cross-reference with `audits/input-validation-audit.md`  
**Runtime model:** Long-polling aiogram 3 bot — **no HTTP upload endpoint**, no web server serving user files

---

## Executive summary

The codebase exposes **one inbound user upload path**: Telegram `document` import of custom study tasks (`.txt`) in `handle_ut_import_file`. That path uses **in-memory download** (no disk persistence), a **64 KiB size cap** (pre-download only), and **extension-based** type gating. It does **not** validate MIME types, magic bytes, or post-download size; there is **no antivirus** integration.

Outbound “file” flows (admin CSV/ZIP export, pet/task images, backups) are **server-generated or operator-curated** — not user uploads. Pillow is used **only at build time** (`scripts/build_pet_assets.py`), not when handling Telegram files.

Residual risk is concentrated in **weak content-type assurance** (rename-only `.txt`), **trust in Telegram `file_size`**, and **related filesystem reads** where `subject_id` or `solution_image` are not path-sandboxed (overlaps input-validation audit; included here because they affect file read safety).

**Risk score: 5 / 10** — no direct RCE or on-disk upload execution; gaps are spoofed types, resource abuse within SQLite/Telegram limits, and path composition on callback-driven reads.

---

## Upload surface inventory

| Flow | Direction | Handler / module | Persists to disk? |
|------|-----------|------------------|-------------------|
| User task `.txt` import | Inbound | `bot.py` `handle_ut_import_file` | No — `BytesIO` → SQLite |
| Admin `/export`, analytics ZIP | Outbound | `bot.py`, `services.py` `export_all_tables_zip` | In-memory only |
| Photo tasks / pet assets | Read-only | `bot.py` `load_tasks`, `services.render_pet` | Curated under `study_materials/`, `assets/pet/` |
| DB backup | Internal | `services.py` `BackupService` | `/app/data/backups` in Docker, `./backups` locally (server path, not user upload) |
| Support catch-all | Inbound text | `bot.py` `handle_any_message` | Append `messages.log` (text JSONL, not binary upload) |

**Grep confirmation:** Only `@router.message(..., F.document)` in `bot.py` (line 2485). No `F.photo` / `F.video` upload handlers.

---

## Checklist diff (requested “Check for” items)

| # | Control | Result | Notes |
|---|---------|--------|-------|
| 1 | File type validation (whitelist, not blacklist) | **Partial** | Extension suffix `.txt` only; no content whitelist |
| 2 | File size limits | **Partial** | `USER_TASK_FILE_MAX_BYTES` pre-download; no post-download `len()` check |
| 3 | Filename sanitization | **Partial** | `file_name` not written to disk; extension check only, not full name hardening |
| 4 | Anti-virus scanning integration | **Fail** | No ClamAV / cloud scan / quarantine |
| 5 | Storage location (outside webroot) | **Pass** | Upload never hits filesystem; no webroot in architecture |
| 6 | Direct execution prevention | **Pass** | Parsed as UTF-8 text → DB; no `exec`, shell, or script save |
| 7 | MIME type validation | **Fail** | `doc.mime_type` unused |
| 8 | Magic number verification | **Fail** | No content sniffing (e.g. NUL-heavy binary, ZIP header in `.txt`) |
| 9 | Image manipulation library vulnerabilities | **N/A (runtime)** | Pillow only in dev build script; runtime sends prebuilt PNG/GIF |
| 10 | ZIP bomb protection | **N/A (inbound)** | Users cannot upload ZIP; outbound ZIP is server-built CSVs |

---

## Findings

### F-1 — Extension-only type gate (bypassable metadata)

- **Severity:** Medium  
- **CWE:** [CWE-434](https://cwe.mitre.org/data/definitions/434.html) (Unrestricted Upload of File with Dangerous Type)

**Evidence:**

```2489:2492:bot.py
    doc = message.document
    if not doc.file_name or not doc.file_name.lower().endswith(".txt"):
        await message.answer(t("user_tasks.need_txt", locale))
        return
```

**Why it matters:** Attackers can send a binary or polyglot file renamed to `payload.txt`. The bot decodes as UTF-8 and stores strings in SQLite — not RCE, but can store unexpected content, break parsers, or waste DB/message budget. Telegram’s `mime_type` is not consulted.

**Exploitability:** Medium — send any `document` with `file_name="notes.txt"` and non-text bytes; if UTF-8 decodable, import proceeds.

**Reproduction:**

1. Open Settings → My tasks → Upload `.txt` for a subject.  
2. Send a Telegram document named `evil.txt` containing UTF-8–valid binary or huge single-line text.  
3. Observe import success if parse rules pass (no magic-byte rejection).

**Remediation:**

```python
ALLOWED_TASK_MIME = frozenset({"text/plain", "application/octet-stream"})  # TG often uses octet-stream

def _validate_task_upload(doc) -> str | None:
    """Return error i18n key or None if OK."""
    name = (doc.file_name or "").strip()
    if not name.lower().endswith(".txt"):
        return "user_tasks.need_txt"
    base = name.lower().removesuffix(".txt")
    if not base or "/" in name or "\\" in name or ".." in name:
        return "user_tasks.need_txt"
    if doc.mime_type and doc.mime_type not in ALLOWED_TASK_MIME:
        return "user_tasks.need_txt"
    return None
```

Add after download:

```python
raw_bytes = buffer.getvalue()
if len(raw_bytes) > USER_TASK_FILE_MAX_BYTES:
    await message.answer(t("user_tasks.file_too_big", locale, max_kb=USER_TASK_FILE_MAX_BYTES // 1024))
    return
# Reject obvious binary: high ratio of NUL or invalid UTF-8 already handled
if raw_bytes.startswith((b"\x50\x4b\x03\x04", b"\x7fELF", b"\x89PNG")):
    await message.answer(t("user_tasks.need_txt", locale))
    return
raw = raw_bytes.decode("utf-8")
```

Defense-in-depth: treat uploads as untrusted text; keep storage in DB only.

---

### F-2 — Size limit trusts Telegram `file_size` only (pre-download)

- **Severity:** Medium  
- **CWE:** [CWE-400](https://cwe.mitre.org/data/definitions/400.html) (Uncontrolled Resource Consumption)

**Evidence:**

```2493:2504:bot.py
    if doc.file_size and doc.file_size > USER_TASK_FILE_MAX_BYTES:
        await message.answer(
            t("user_tasks.file_too_big", locale, max_kb=USER_TASK_FILE_MAX_BYTES // 1024),
        )
        return
    ...
        await bot.download(doc, destination=buffer)
        raw = buffer.getvalue().decode("utf-8")
```

Constant: `USER_TASK_FILE_MAX_BYTES = 65536` at `bot.py:1676`.

**Why it matters:** If `file_size` is `None` or understated, the bot downloads the full object into memory before any length check — bounded by Telegram’s document limits (~20 MB) but above the advertised 64 KiB policy.

**Exploitability:** Low–Medium — upload when client/API omits or misreports size.

**Reproduction:**

1. Import flow as above with a document >64 KiB where `file_size` is missing (observe in Bot API raw object) or craft via API layer that understates size.  
2. Confirm download proceeds until decode; no `len(buffer.getvalue())` guard.

**Remediation:** Enforce **post-download** size (snippet in F-1). Optionally stream download with capped read:

```python
MAX = USER_TASK_FILE_MAX_BYTES
buf = bytearray()
async for chunk in bot.download(doc):  # if streaming API available
    buf.extend(chunk)
    if len(buf) > MAX:
        raise ValueError("too_large")
```

---

### F-3 — No MIME type or magic-number verification

- **Severity:** Medium  
- **CWE:** [CWE-434](https://cwe.mitre.org/data/definitions/434.html)

**Evidence:** `doc.mime_type` never referenced in `bot.py`. No `python-magic`, `filetype`, or byte-signature checks before `decode("utf-8")`.

**Why it matters:** Whitelist-by-extension without content verification fails OWASP file-upload guidance; polyglot files can confuse downstream tools if exports ever include user files.

**Exploitability:** Medium — combined with F-1.

**Remediation:** See F-1; for strict text-only policy, require decodable UTF-8 and reject files with >1% non-printable bytes (excluding `\n\r\t`).

---

### F-4 — No antivirus / malware scanning

- **Severity:** Low (for current architecture)  
- **CWE:** [CWE-1104](https://cwe.mitre.org/data/definitions/1104.html) (Use of Unmaintained Third Party Components) — operational gap rather than code defect

**Evidence:** No references to ClamAV, VirusTotal, or scan hooks in `bot.py`, `services.py`, `requirements.txt`, `Dockerfile`, or CI (`security.yml` runs `pip-audit` only).

**Why it matters:** Text-in-DB lowers malware risk vs. executable storage, but admins may open exported logs or forward imported content; defense-in-depth for future features (e.g. image upload) requires a hook now.

**Exploitability:** Low for current `.txt`-only path.

**Remediation (when uploads expand):**

```python
async def scan_bytes(data: bytes) -> bool:
    # Example: clamd INSTREAM or cloud API; fail closed for binaries
    ...
```

Run scan **after** size cap, **before** DB insert; quarantine path outside app dir if files are ever written.

---

### F-5 — Filename not sanitized (low impact: no disk write)

- **Severity:** Low  
- **CWE:** [CWE-22](https://cwe.mitre.org/data/definitions/22.html) (limited applicability — name not used in paths)

**Evidence:** `doc.file_name` used only for `.endswith(".txt")` (`bot.py:2490`). Download target is anonymous `BytesIO()`.

**Why it matters:** Safe today; future refactor might log or save under `file_name` and reintroduce traversal (`../../etc/passwd.txt`).

**Remediation:**

```python
import re
SAFE_NAME = re.compile(r"^[A-Za-z0-9._ -]{1,128}\.txt$", re.I)
if not SAFE_NAME.match(doc.file_name or ""):
    ...
```

Never persist user-supplied names; generate `import-{user_id}-{uuid}.txt` if disk storage is added.

---

### F-6 — Parsed task fields lack per-field length limits

- **Severity:** Medium  
- **CWE:** [CWE-400](https://cwe.mitre.org/data/definitions/400.html)

**Evidence:**

- `user_task_txt.py` `parse_user_tasks_txt` (lines 68–104): no max length on `problem`, `accepted`, `hint`.  
- `repository.py` `UserTaskRepository.bulk_create` (605–634): inserts parsed strings as-is.  
- File cap 64 KiB at `bot.py:1676` allows one enormous line.

**Why it matters:** DoS via fat DB rows and Telegram messages when listing/studying tasks; not a file-execution issue but part of upload handling chain.

**Exploitability:** Medium — one 64 KiB line in `tasks.txt`.

**Remediation:**

```python
MAX_PROBLEM_LEN = 2000
MAX_ANSWER_LEN = 500
MAX_HINT_LEN = 500
MAX_ANSWERS_PER_TASK = 20

# In parse_user_tasks_txt after building accepted list:
if len(problem) > MAX_PROBLEM_LEN:
    errors.append(f"Строка {line_no}: вопрос слишком длинный.")
    continue
accepted = accepted[:MAX_ANSWERS_PER_TASK]
if any(len(a) > MAX_ANSWER_LEN for a in accepted):
    errors.append(f"Строка {line_no}: ответ слишком длинный.")
    continue
```

---

### F-7 — `subject_id` from callback not allowlisted before import

- **Severity:** Medium (filesystem read in related flows)  
- **CWE:** [CWE-22](https://cwe.mitre.org/data/definitions/22.html)

**Evidence:**

```2450:2465:bot.py
@router.callback_query(F.data.startswith("ut_import:"))
async def handle_ut_import_start(callback: CallbackQuery, state: FSMContext):
    ...
        _, user_id_str, subject_id = callback.data.split(":", 2)
    ...
    await state.update_data(
        ut_subject_id=subject_id,
```

`SUBJECT_IDS` exists in `locale_bot.py:35` but is **not** validated here. Import itself only writes DB with that `subject_id`; study flows call `load_tasks(subject_id)` etc. with path joins (`bot.py:1085`, `1050`, `1028`).

**Why it matters:** Crafted `ut_import:{uid}:..` stores tasks under a bogus subject; `ut_study` / `fc_study` with `..` can read paths outside `study_materials/<canonical>/` (see input-validation audit §2.2).

**Exploitability:** Medium — custom callback_data (client allowing edit).

**Remediation:**

```python
from locale_bot import SUBJECT_IDS

if subject_id not in SUBJECT_IDS:
    await callback.answer("Unknown subject", show_alert=True)
    return
```

Apply in all `ut_*` / `fc_*` handlers before FSM update.

---

### F-8 — `solution_image` in task JSON can escape `tasks/` directory

- **Severity:** Medium (content-supply chain, not end-user upload)  
- **CWE:** [CWE-22](https://cwe.mitre.org/data/definitions/22.html)

**Evidence:**

```1105:1110:bot.py
        solution_filename = data.get("solution_image", f"{task_id}-solution.png")
        tasks.append({
            ...
            "solution_filename": str(solution_filename),
```

```3989:3990:bot.py
    tasks_dir = STUDY_MATERIALS_PATH / subject_id / "tasks"
    solution_path = tasks_dir / task.get("solution_filename", f"{task['id']}-solution.png")
```

Curated example uses safe name: `study_materials/industrial-management/tasks/task-01.json`.

**Why it matters:** Malicious or mistaken `solution_image: "../../../.env"` causes `solution_path.exists()` and `FSInputFile` to target files outside `tasks/` if present on disk.

**Exploitability:** Low for Telegram users — requires compromising `study_materials` content or repo; relevant to CI/content editors.

**Remediation:**

```python
def _safe_task_filename(name: str, task_id: str) -> str:
    base = Path(name).name  # strip directories
    if not re.fullmatch(r"[\w.-]+\.png", base):
        return f"{task_id}-solution.png"
    return base

solution_filename = _safe_task_filename(
    str(data.get("solution_image", f"{task_id}-solution.png")), task_id
)
# Before open:
resolved = (tasks_dir / solution_filename).resolve()
if not str(resolved).startswith(str(tasks_dir.resolve())):
    continue
```

---

### F-9 — Outbound ZIP export lacks extraction limits (not user upload)

- **Severity:** Low  
- **CWE:** [CWE-409](https://cwe.mitre.org/data/definitions/409.html) (Improper Handling of Highly Compressed Data) — **N/A for inbound**; noted for completeness

**Evidence:** `services.py` `export_all_tables_zip` (2521–2564) builds ZIP in memory from SQLite CSV exports; no user-supplied ZIP ingested. Tests use `zipfile.is_zipfile` (`tests/test_analytics_service.py`).

**Why it matters:** If the bot later **imports** ZIP backups or study packs, apply `MAX_EXTRACT_SIZE`, `MAX_FILES`, and compression ratio limits.

**Remediation (future inbound ZIP):**

```python
MAX_UNCOMPRESSED = 50 * 1024 * 1024
MAX_RATIO = 20
for info in zf.infolist():
    if info.file_size > MAX_UNCOMPRESSED:
        raise ValueError("zip bomb")
```

---

### F-10 — Pillow / image libraries at runtime

- **Severity:** N/A (informational)  
- **CWE:** —

**Evidence:**

- Runtime: `services.render_pet` reads static files only (`services.py:394–454`); `bot.send_photo(FSInputFile(...))` for curated paths.  
- Build-time: `scripts/build_pet_assets.py` imports `PIL` (line 35); **not** in `requirements.txt` production install.

**Why it matters:** CVEs in Pillow affect **asset build pipeline**, not Telegram upload handling. Pin Pillow in dev requirements and run `pip-audit` on `requirements-dev.txt` (already in CI).

**Remediation:** Keep user uploads off image decode paths; if photo upload is added, pin `Pillow>=10.x`, limit dimensions/pixels, disable exotic formats, use `Image.MAX_IMAGE_PIXELS`.

---

### F-11 — Positive controls (no issue)

| Control | Location |
|---------|----------|
| In-memory download (no upload directory) | `bot.py:2501–2504` `BytesIO` |
| No web server / webroot exposure | `Dockerfile` CMD `python bot.py`; no Flask/nginx |
| UTF-8 decode failure handled | `bot.py:2505–2507` `UnicodeDecodeError` |
| Per-subject task count cap | `UserTaskRepository.MAX_PER_SUBJECT = 50`, `bulk_create` |
| Global rate limit middleware | `RateLimitMiddleware` + `UserRateLimiter` (`bot.py:242–252`, `7267–7268`) |
| Export table names allowlisted | `AnalyticsService.EXPORTABLE_TABLES` (`services.py:1747+`) |
| Parameterized SQL for imported content | `repository.py:621–630` |

---

## Storage & execution summary

| Topic | Status |
|-------|--------|
| **Storage outside webroot** | Upload content → SQLite (`user_tasks`); no HTTP static mapping. Backups under `BACKUP_DIR` (`/app/data/backups` in compose). |
| **Direct execution prevention** | No `subprocess`, `eval`, or writing uploaded bytes to executable paths. Parser stores text fields only. |
| **Anti-virus** | Not implemented |

---

## Summary

### Risk score: **5 / 10**

Justification: Single upload path with reasonable size intent and no disk execution surface, offset by missing content validation, optional `file_size` bypass, and related path-trust issues on `subject_id` / JSON filenames. No inbound ZIP or user image upload reduces blast radius vs. typical web apps.

### Top prioritized fixes (fastest risk reduction)

1. **Post-download byte cap + reject binary magic** in `handle_ut_import_file` (F-1, F-2, F-3).  
2. **Allowlist `subject_id`** in `ut_import` / `ut_study` / `fc_study` and harden `load_*` path joins (F-7).  
3. **Per-field length limits** in `parse_user_tasks_txt` + optional DB column checks (F-6).  
4. **Sanitize `solution_image` / `solution_filename`** to basename under `tasks/` (F-8).  
5. **Document or add scan hook** before any future expansion to images/ZIP uploads (F-4, F-9).

### Tests gap

`tests/test_user_tasks.py` covers parser happy paths and repo limits — **not** extension bypass, post-download size, MIME, or path traversal. Suggested additions:

```python
def test_rejects_binary_magic_after_download():
    ...

@pytest.mark.asyncio
async def test_ut_import_rejects_invalid_subject_id():
    ...
```

---

## Files reviewed

| File | Role |
|------|------|
| `bot.py` | `handle_ut_import_file`, loaders, export send, photo send |
| `user_task_txt.py` | `.txt` parser |
| `repository.py` | `UserTaskRepository.bulk_create` |
| `services.py` | `export_all_tables_zip`, `render_pet`, `BackupService` |
| `plan_service.py` | Study materials scan (read-only) |
| `locale_bot.py` | `SUBJECT_IDS` |
| `scripts/build_pet_assets.py` | Pillow build-time assets |
| `requirements.txt`, `requirements-dev.txt`, `Dockerfile`, `docker-compose.yml` | Deps & deployment |
| `.github/workflows/security.yml` | Dependency CVE scan |
| `tests/test_user_tasks.py`, `tests/test_analytics_service.py` | Upload/export tests |

---

*End of file upload security audit.*
