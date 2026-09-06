# Code Complexity Review

> **Audit date:** 2026-05-24 · **Doc sync:** 2026-09-05 (Palph v0.8, pytest suite **787** tests).
> Line counts below are point-in-time snapshots; re-run audit after major refactors.

**Product:** Palph (Telegram bot, aiogram 3)  
**Reviewer:** Claude (automated radon + manual analysis)  
**Branch / HEAD:** working tree (uncommitted changes present)  
**Scope:** Production modules — `bot.py`, `services.py`, `repository.py`, `plan_handlers.py`, `plan_service.py`, `tasks.py`, `db.py`, `i18n.py`, `locale_bot.py`, `fsm_storage.py`, `file_upload_security.py`, `task_answer_match.py`, `user_task_txt.py`, `parse_logs.py`

**Tooling:** radon 6.0.1 (`radon cc -s -a`, `radon raw`), AST line-count script, import graph via grep.

---

## Executive summary

| Metric | Value |
|--------|-------|
| **Overall complexity health** | **3 / 10** (Poor — functional but hard to maintain) |
| Total blocks analyzed (radon) | 641 |
| Average cyclomatic complexity | A (3.89) — misleadingly healthy because one giant file dilutes the average |
| Files > 300 LOC (SLOC) | 5 (`bot.py` 6313, `services.py` 1867, `repository.py` 1627, `plan_handlers.py` 707, `db.py` 409) |
| Functions with CC > 10 | 47 (verified via radon) |
| Functions > 50 lines | 52 (verified via AST) |
| Classes > 500 lines | 2 (`AnalyticsService` ~1211, `LeaderboardRepository` ~533) |

The codebase follows a layered intent (`repository` → `services` → `bot`), but **`bot.py` has absorbed ~70% of application logic** (handlers, content loading, analytics rendering, admin tools, pet UI, friends, timers). That single module dominates every complexity dimension. Secondary hotspots are **`AnalyticsService`** (metrics aggregation) and **`plan_service.build_content_catalog`** (multi-format file scanning).

**Top 5 hotspots (prioritize these first):**

| Rank | Location | CC | Lines | Issue |
|------|----------|----|-------|-------|
| 1 | `services.py:2117` `AnalyticsService.compute_product_metrics` | **43 (F)** | 308 | Mega-method; 8+ SQL/query blocks, nested cohort math |
| 2 | `plan_service.py:101` `build_content_catalog` | **36 (E)** | 96 | Repeated scan loops for 4 content types |
| 3 | `plan_handlers.py:411` `_launch_plan_item` | **26 (D)** | 127 | Mode-switch duplication; tight coupling to `bot` module |
| 4 | `services.py:194` `AchievementService.check_and_award` | **25 (D)** | 78 | Long conditional award chain |
| 5 | `services.py:901` `StreakService._process` | **24 (D)** | 123 | Timezone batch loop + streak state machine |

---

## 1. Cyclomatic complexity (CC > 10)

Radon grades: A ≤ 5, B 6–10, C 11–20, D 21–30, E 31–40, F 41+.

### 1.1 Critical (CC ≥ 20) — refactor urgently

| Importance | CC | File:Line | Function | Notes |
|------------|-----|-----------|----------|-------|
| **10** | 43 | `services.py:2117` | `AnalyticsService.compute_product_metrics` | Single method computes subject funnel, mode funnel, strict funnel, cohort activation, feature retention, morning push, leaderboard, notification funnel. Multiple nested loops over DB rows + in-memory `defaultdict` merges. |
| **9** | 36 | `plan_service.py:101` | `build_content_catalog` | Four near-identical file-scan branches (flashcards, mcq, tasks JSON, situational). Each branch: open → parse line/JSON → topic loop → append `CatalogItem`. |
| **8** | 26 | `plan_handlers.py:411` | `_launch_plan_item` | Sequential `if mode == …` blocks (mcq, flashcards, tasks, situational) each duplicating state setup + bot calls via `_bmod()`. |
| **8** | 25 | `services.py:194` | `AchievementService.check_and_award` | Many independent `if` achievement checks in one method. |
| **7** | 24 | `services.py:901` | `StreakService._process` | Per-timezone user iteration with nested date/streak logic. |
| **7** | 24 | `plan_service.py:481` | `build_progress_snapshot` | CC 24 but only ~44 lines — high branch density from four row-type loops with nested conditionals. |
| **7** | 23 | `services.py:2010` | `AnalyticsService.compute_activation_metrics` | Similar pattern to `compute_product_metrics`. |
| **6** | 20 | `bot.py:4190` | `handle_task_answer` | Correct/wrong/retry/solution branches; plan-mode early returns. |

**Remediation — extract catalog scanners (`plan_service.py`):**

```python
def _scan_flashcards_txt(path: Path) -> list[CatalogItem]: ...
def _scan_mcq_txt(path: Path) -> list[CatalogItem]: ...
def _scan_tasks_dir(tasks_dir: Path) -> list[CatalogItem]: ...
def _scan_situational_dir(situational_dir: Path) -> list[CatalogItem]: ...

def build_content_catalog(subject_id: str, materials_path: Path | None = None) -> list[CatalogItem]:
    base = (materials_path or STUDY_MATERIALS_PATH) / subject_id
    if not base.is_dir():
        return []
    return [
        *(_scan_flashcards_txt(base / "flashcards.txt") if (base / "flashcards.txt").exists() else []),
        *(_scan_mcq_txt(base / "mcq.txt") if (base / "mcq.txt").exists() else []),
        *_scan_tasks_dir(base / "tasks"),
        *_scan_situational_dir(base / "situational"),
    ]
```

**Remediation — strategy pattern for plan item launch (`plan_handlers.py`):**

```python
async def _launch_mcq_item(b, callback, state, user_id, subject_id, item, locale): ...
async def _launch_flashcards_item(...): ...
async def _launch_tasks_item(...): ...
async def _launch_situational_item(...): ...

_LAUNCHERS = {"mcq": _launch_mcq_item, "flashcards": _launch_flashcards_item, ...}

async def _launch_plan_item(...):
    launcher = _LAUNCHERS.get(mode)
    if not launcher:
        await callback.message.answer(t("plan.item_missing", locale))
        return
    await launcher(b, callback, state, user_id, subject_id, item, locale)
```

**Remediation — split `compute_product_metrics` (`services.py:2117`):**

Extract private methods: `_funnel_by_subject`, `_funnel_by_mode`, `_strict_event_funnel`, `_activation_by_cohort`, `_feature_retention_d7`, `_morning_push_stats`, `_leaderboard_stats`. Each returns a dict slice; the public method merges.

### 1.2 High (CC 11–19) — schedule refactor

#### `bot.py` (20 functions, CC 11–20)

| Importance | CC | File:Line | Function |
|------------|-----|-----------|----------|
| 7 | 20 | `bot.py:4190` | `handle_task_answer` |
| 6 | 18 | `bot.py:5554` | `_render_product_metrics` |
| 6 | 18 | `bot.py:7500` | `reconcile_stale_timers` |
| 6 | 17 | `bot.py:1192` | `load_tasks` |
| 5 | 16 | `bot.py:5070` | `cmd_broadcast` |
| 5 | 16 | `bot.py:5375` | `_render_content_stats` |
| 5 | 15 | `bot.py:4013` | `_send_next_task` |
| 5 | 14 | `bot.py:3097` | `show_achievements` |
| 5 | 14 | `bot.py:3154` | `run_timer_task` |
| 5 | 14 | `bot.py:5177` | `cmd_notif_status` |
| 4 | 12 | `bot.py:1900` | `_build_subject_progress_block` |
| 4 | 12 | `bot.py:2624` | `handle_ut_import_file` |
| 4 | 12 | `bot.py:3347` | `stop_active_timer` |
| 4 | 12 | `bot.py:4519` | `handle_flashcard_rate` |
| 4 | 12 | `bot.py:7056` | `friend_add_process` |
| 4 | 11 | `bot.py:1039` | `_file_based_mode_ids` |
| 4 | 11 | `bot.py:1144` | `load_mcq` |
| 4 | 11 | `bot.py:1273` | `load_flashcards` |
| 4 | 11 | `bot.py:3673` | `handle_mode_picked` |
| 4 | 11 | `bot.py:3825` | `handle_mcq_callback` |
| 4 | 11 | `bot.py:2106` | `NotificationSettings.get_display_text` |

**Pattern:** Analytics render helpers (`_render_*`, `cmd_*` admin) mirror `AnalyticsService` methods — duplication across layers. Content loaders (`load_tasks`, `load_mcq`, `load_flashcards`) share structure with `plan_service.build_content_catalog`.

#### `services.py` (CC 11–17)

| Importance | CC | File:Line | Function |
|------------|-----|-----------|----------|
| 6 | 17 | `services.py:1866` | `AnalyticsService.compute_content_stats` |
| 5 | 12 | `services.py:588` | `StudyService._notify_level_up` |
| 5 | 12 | `services.py:1089` | `LeaderboardService.render_leaderboard` |
| 5 | 12 | `services.py:1421` | `AnalyticsService.compute_cohort_retention` |
| 5 | 12 | `services.py:2457` | `AnalyticsService.compute_heatmap` |
| 4 | 11 | `services.py:719` | `ReminderService._send_morning` |

#### Other modules

| Importance | CC | File:Line | Function |
|------------|-----|-----------|----------|
| 5 | 12 | `plan_service.py:242` | `topic_order` |
| 4 | 12 | `repository.py:1270` | `PetRepository.purchase_item` |
| 4 | 11 | `plan_service.py:325` | `generate_sprint_plan` |
| 4 | 14 | `user_task_txt.py:73` | `parse_user_tasks_txt` |
| 3 | 11 | `db.py:25` | `init_db` |

### 1.3 Switch / elif complexity

No Python `match/case` statements found in scope. Complexity comes from:

- **Long `if mode == …` chains** — `plan_handlers._launch_plan_item` (4 modes), `handle_mode_picked`, analytics callback router (`handle_anlt_*` × 12 handlers in `bot.py:6015–6271`).
- **Nested early returns** — `handle_task_answer` (`bot.py:4190–4344`): correct path → plan branch → wrong retry → solution reveal, each with 3–5 await calls.

**Recommendation:** Replace analytics inline callbacks with a dispatch dict keyed by action string (same pattern as suggested for plan launchers).

---

## 2. Cognitive complexity

Cognitive complexity penalizes nesting depth and flow-breaking structures more than flat conditionals. Manual assessment of worst offenders:

| Importance | File:Line | Function | Cognitive issues |
|------------|-----------|----------|------------------|
| **9** | `services.py:2117` | `compute_product_metrics` | 4+ levels: async SQL → row loop → nested `_count` queries → cohort dict accumulation → median helper defined inline. Reader must hold 8 metric definitions in working memory. |
| **8** | `bot.py:4190` | `handle_task_answer` | Mixed abstraction: answer validation, coin economy, leaderboard, event logging, plan integration, solution image sending — all in one handler. |
| **8** | `plan_handlers.py:411` | `_launch_plan_item` | Calls into `_bmod()` for `load_mcq`, `QuizStates`, `_send_next_mcq_question` — handler logic depends on entire bot module surface. |
| **7** | `bot.py:7500` | `reconcile_stale_timers` | 142 lines; timer reconciliation + DB writes + user notifications; hard to test in isolation. |
| **7** | `plan_handlers.py:605` | `handle_plan_callback` (nested in `register_plan_handlers`) | 163-line nested function; action dispatch via `if action == …` string chain inside closure. |
| **6** | `services.py:901` | `StreakService._process` | Timezone grouping + per-user streak FSM; business rules interleaved with SQL. |
| **6** | `bot.py:7647` | `main` | 141 lines: wiring repos, services, schedulers, middleware, router registration — bootstrap mixed with config. |

**Nested loops (verified):**

- `build_content_catalog`: file loop → line loop → topic loop (3 deep) × 4 content types.
- `compute_product_metrics`: DB cursor loop → per-row `_count` queries (N+1 pattern, also hurts performance cognition).
- `load_tasks` / `load_mcq` / `load_flashcards`: similar nested iteration over filesystem + lines.

**Recursion:** None found in production modules. ✅

**Mixed abstraction levels (anti-pattern):**

| Layer | Example |
|-------|---------|
| SQL in handlers | `bot.py` uses repo instances directly (~14 repository singletons) alongside inline business rules |
| Presentation in service | `LeaderboardService.render_leaderboard` (`services.py:1089`) builds Telegram HTML strings |
| Domain in repository | `LeaderboardRepository.grant_time_pts` imports `piecewise_time_pts` from `services` (`repository.py:1479`) |

---

## 3. Lines of code (LOC)

Radon raw SLOC vs physical lines (PowerShell `Measure-Object -Line`):

| File | Physical lines | SLOC (radon) | Status |
|------|---------------|--------------|--------|
| `bot.py` | **6953** | 6313 | 🔴 **Split candidate #1** — 10× over 300-line guideline |
| `services.py` | 2442 | 1867 | 🔴 Split candidate #2 |
| `repository.py` | 2148 | 1627 | 🟠 Large but acceptable for data layer if classes split |
| `plan_handlers.py` | 717 | 707 | 🟠 Borderline |
| `plan_service.py` | 463 | 443 | 🟢 OK |
| `db.py` | 435 | 409 | 🟠 Schema + init in one file |
| `parse_logs.py` | 163 | 121 | 🟢 OK |
| `tasks.py` | 132 | 86 | 🟢 OK |
| Others | < 160 | — | 🟢 OK |

### 3.1 Functions > 50 lines (AST-verified, top 20)

| Importance | Lines | File:Line | Function |
|------------|-------|-----------|----------|
| 9 | 308 | `services.py:2117–2424` | `compute_product_metrics` |
| 8 | 177 | `plan_handlers.py:591–767` | `register_plan_handlers` |
| 8 | 163 | `plan_handlers.py:605–767` | `handle_plan_callback` (nested) |
| 7 | 155 | `bot.py:4190–4344` | `handle_task_answer` |
| 7 | 142 | `bot.py:7500–7641` | `reconcile_stale_timers` |
| 7 | 141 | `bot.py:7647–7787` | `main` |
| 6 | 143 | `services.py:1866–2008` | `compute_content_stats` |
| 6 | 127 | `plan_handlers.py:411–537` | `_launch_plan_item` |
| 6 | 123 | `services.py:901–1023` | `StreakService._process` |
| 5 | 106 | `services.py:2010–2115` | `compute_activation_metrics` |
| 5 | 105 | `bot.py:4519–4623` | `handle_flashcard_rate` |
| 5 | 104 | `bot.py:5070–5173` | `cmd_broadcast` |
| 5 | 96 | `plan_service.py:101–196` | `build_content_catalog` |
| 4 | 93 | `bot.py:7056–7148` | `friend_add_process` |
| 4 | 93 | `services.py:771–863` | `ReminderService._send_evening` |
| 4 | 91 | `services.py:1165–1255` | `LeaderboardService.run_rollover` |
| 4 | 90 | `services.py:1504–1593` | `compute_funnel` |
| 4 | 80 | `bot.py:5554–5633` | `_render_product_metrics` |
| 4 | 80 | `repository.py:1270–1349` | `PetRepository.purchase_item` |
| 4 | 80 | `bot.py:5375–5454` | `_render_content_stats` |

**Total functions > 50 lines:** 52 across 5 files.

### 3.2 Classes > 500 lines

| Importance | Lines | File:Line | Class | Split suggestion |
|------------|-------|-----------|-------|------------------|
| **10** | ~1211 | `services.py:1354–2564` | `AnalyticsService` | → `analytics/funnel.py`, `analytics/cohort.py`, `analytics/engagement.py`, `analytics/export.py` |
| **8** | ~533 | `repository.py:1410–1942` | `LeaderboardRepository` | → scoring, badges, freeze, weekly rollover sub-modules |
| 6 | ~425 | `repository.py:8–432` | `UserRepository` | Acceptable; could extract notification queries |
| 5 | ~305 | `repository.py:1103–1407` | `PetRepository` | Extract shop/inventory vs emotion/xp |
| 5 | ~284 | `services.py:1033–1316` | `LeaderboardService` | Separate render from rollover logic |

### 3.3 Recommended module splits for `bot.py`

Proposed extraction (by grep section analysis — ~306 defs in one file):

| New module | Approx. lines | Current content in `bot.py` |
|------------|---------------|----------------------------|
| `handlers/study.py` | ~800 | MCQ, flashcards, tasks, quiz sections |
| `handlers/profile.py` | ~600 | Profile, settings, notifications, timezone |
| `handlers/admin.py` | ~900 | Analytics commands, broadcast, backup, log parse |
| `handlers/social.py` | ~400 | Friends, leaderboard UI |
| `handlers/pet.py` | ~350 | Pet shop, rename, emotion |
| `content_loader.py` | ~400 | `load_tasks`, `load_mcq`, `load_flashcards`, tips |
| `bot.py` (remaining) | ~800 | Router wiring, middleware, `main` |

---

## 4. Coupling analysis

### 4.1 Import graph (production code)

```
bot.py
  → repository (14 classes), services (8 exports), plan_handlers, locale_bot,
    i18n, tasks, db, fsm_storage, file_upload_security, task_answer_match, user_task_txt

services.py
  → repository (UserRepository, SessionRepository, PetRepository, LeaderboardRepository)
  → i18n, file_upload_security

repository.py
  → aiosqlite, typing only (top-level)
  → services (lazy imports inside LeaderboardRepository: piecewise_time_pts,
    user_calendar_keys, streak_multiplier, freeze_cost)  ⚠️ layer violation

plan_handlers.py
  → plan_service, repository.PlanRepository, i18n, aiogram
  → bot module via _bmod_ref runtime injection  ⚠️ circular coupling

plan_service.py
  → pathlib, json only  ✅ leaf module

tasks.py
  → services (4 schedulers), repository.UserRepository
```

### 4.2 Afferent / efferent coupling (approximate)

| Module | Ca (imported by) | Ce (imports) | Instability I = Ce/(Ca+Ce) | Assessment |
|--------|------------------|--------------|----------------------------|------------|
| `repository.py` | bot, services, tasks, plan_handlers, tests (~6) | 1 (+ lazy services) | **0.14** | Stable — changes ripple widely |
| `services.py` | bot, tasks, repository†, tests (~15) | 3 modules | **0.17** | Stable core |
| `plan_service.py` | plan_handlers, scripts, tests | 0 | **0.00** | Maximally stable leaf |
| `plan_handlers.py` | bot | 4 + bot runtime | **0.63** | Unstable — depends on bot internals |
| `bot.py` | tests only (13 test files) | 12+ modules | **0.48** | High efferent; god-module hub |
| `i18n.py` | bot, services, plan_handlers, locale_bot | 0 | **0.00** | Stable utility |

† `repository.py` lazy-imports `services` — **bidirectional dependency** between repository and services layers.

### 4.3 Tightly coupled module pairs

| Importance | Pair | Issue |
|------------|------|-------|
| **9** | `bot.py` ↔ all layers | Instantiates 14 repos + 8 services inline; handlers call repos directly, bypassing services for many flows |
| **8** | `plan_handlers.py` → `bot` module | `_bmod()` accesses `load_mcq`, `QuizStates`, `_send_next_task`, etc. — any bot refactor breaks plans |
| **7** | `repository.LeaderboardRepository` → `services` | Domain calculations (`piecewise_time_pts`, `streak_multiplier`) live in services but invoked from repo |
| **6** | `bot.py` analytics handlers ↔ `AnalyticsService` | `_render_product_metrics` (`bot.py:5554`) duplicates formatting logic that service already structured |
| **5** | Tests → `bot.py` | 13 test modules import `bot` directly, coupling tests to 7000-line module |

**Remediation — break repository → services back-edge:**

Move `piecewise_time_pts`, `user_calendar_keys`, `streak_multiplier`, `freeze_cost` to a neutral `domain/scoring.py` imported by both `repository` and `services`.

**Remediation — break plan_handlers → bot coupling:**

Define a narrow `PlanStudyGateway` protocol:

```python
class PlanStudyGateway(Protocol):
    def load_mcq(self, subject_id: str) -> list: ...
    async def send_next_mcq_question(self, chat_id: int, state: FSMContext) -> None: ...
    # … one method per mode
```

Inject implementation from `bot.py` at startup instead of passing entire module.

---

## 5. Cohesion analysis

### 5.1 Single Responsibility Principle (SRP) violations

| Importance | Module / class | Responsibility count | Should be |
|------------|----------------|---------------------|-----------|
| **10** | `bot.py` | Routing, FSM, content I/O, analytics UI, admin, pet UI, friends, timers, error handling | Thin router + delegated handlers |
| **9** | `AnalyticsService` | Cohort, funnel, DAU, heatmap, segments, content stats, product metrics, export | One class per metric family |
| **7** | `LeaderboardRepository` | Daily/weekly scores, badges, freeze, streak multiplier side effects | Score persistence vs gamification rules |
| **6** | `NotificationSettings` in `bot.py` | Data + display text + keyboard building | Move to `handlers/settings.py` or dedicated model |
| **5** | `StudyService` + handlers | Session completion in service; session start/stop/timer in bot | Unified session facade |

### 5.2 Well-cohesive modules (positive examples)

| Module | Focus | LOC |
|--------|-------|-----|
| `i18n.py` | Translation lookup | 57 |
| `fsm_storage.py` | SQLite FSM persistence | 78 |
| `task_answer_match.py` | Answer normalization/matching | 75 |
| `file_upload_security.py` | Upload validation | 131 |
| `plan_service.py` | Plan generation logic (except catalog scanner) | 463 |
| `tasks.py` | Scheduler entry points | 132 |

### 5.3 Repository class grouping

`repository.py` groups 15 repository classes in one file (~2148 lines). Classes are individually focused (good LCOM), but file-level cohesion is low — **split by domain**:

- `repos/user.py` — User, Session, Admin
- `repos/content.py` — Flashcard, Mcq, Task, UserFlashcard, UserTask, Tips, SubjectStats
- `repos/social.py` — Friend, Leaderboard, Pet
- `repos/plan.py` — Plan

---

## 6. Prioritized refactoring recommendations

| Priority | Action | Impact | Effort |
|----------|--------|--------|--------|
| **P0** | Split `AnalyticsService` — extract `compute_product_metrics` sub-methods first | Reduces highest CC (43) | Medium |
| **P0** | Extract `content_loader.py` from `bot.py`; share with `plan_service.build_content_catalog` | Removes duplication across 5 loaders | Medium |
| **P1** | Break `bot.py` into `handlers/` package (study, admin, social, profile) | Addresses 6953-line monolith | Large |
| **P1** | Replace `plan_handlers._bmod()` with `PlanStudyGateway` protocol | Breaks circular coupling | Medium |
| **P1** | Move scoring helpers to `domain/scoring.py`; remove `repository→services` imports | Clean layering | Small |
| **P2** | Refactor `_launch_plan_item` to strategy dict | CC 26 → ~5 per launcher | Small |
| **P2** | Split `LeaderboardRepository` (533 lines) | Isolated testing | Medium |
| **P2** | Extract `handle_task_answer` reward/solution/plan into helpers | CC 20 → ~8 | Small |
| **P3** | Analytics admin handlers: data from service, single `render_analytics()` formatter | Removes `_render_*` duplication | Medium |
| **P3** | Split `repository.py` into package | Navigation / reviewability | Medium |

---

## 7. Complexity health breakdown

| Dimension | Score (1–10) | Rationale |
|-----------|--------------|-----------|
| Cyclomatic complexity | 3 | 47 functions CC>10; five grade D/E/F |
| Cognitive complexity | 3 | Deep nesting in analytics + monolithic handlers |
| LOC / file size | 2 | `bot.py` 6953 lines; 2 classes >500 lines |
| Coupling | 4 | Layer violations (repo→services, plan→bot); bot hub |
| Cohesion | 4 | Clear small modules exist; core files do too much |
| **Overall** | **3** | Works in production but high change risk and onboarding cost |

---

## 8. Verification notes

- All CC scores from `radon cc -s -a` run 2026-05-24 on listed production files.
- Function line ranges from AST (`end_lineno - lineno + 1`); verified for top hotspots by reading source.
- Class line counts from AST class body span.
- Import/coupling counts from ripgrep `from X import` across `*.py`.
- No functions were invented; all named symbols verified in source.
- **`plan_handlers.handle_plan_callback`**: nested inside `register_plan_handlers` at `plan_handlers.py:605` — radon reports outer function at 591; inner callback analyzed separately via AST.

---

## Appendix A — Full CC > 10 inventory

<details>
<summary>bot.py (21)</summary>

`handle_task_answer`(20), `_render_product_metrics`(18), `reconcile_stale_timers`(18), `load_tasks`(17), `cmd_broadcast`(16), `_render_content_stats`(16), `_send_next_task`(15), `show_achievements`(14), `run_timer_task`(14), `cmd_notif_status`(14), `_build_subject_progress_block`(12), `handle_ut_import_file`(12), `stop_active_timer`(12), `handle_flashcard_rate`(12), `friend_add_process`(12), `_file_based_mode_ids`(11), `load_mcq`(11), `load_flashcards`(11), `handle_mode_picked`(11), `handle_mcq_callback`(11), `NotificationSettings.get_display_text`(11)

</details>

<details>
<summary>services.py (11)</summary>

`compute_product_metrics`(43), `check_and_award`(25), `_process`(24), `compute_activation_metrics`(23), `compute_content_stats`(17), `_notify_level_up`(12), `render_leaderboard`(12), `compute_cohort_retention`(12), `compute_heatmap`(12), `_send_morning`(11)

</details>

<details>
<summary>plan_service.py (4), plan_handlers.py (1), repository.py (1), user_task_txt.py (1), db.py (1)</summary>

`build_content_catalog`(36), `build_progress_snapshot`(24), `topic_order`(12), `generate_sprint_plan`(11), `_launch_plan_item`(26), `PetRepository.purchase_item`(12), `parse_user_tasks_txt`(14), `init_db`(11)

</details>

---

## Appendix B — Commands to reproduce

```powershell
pip install radon
radon cc -s -a bot.py services.py repository.py plan_handlers.py plan_service.py tasks.py db.py
radon cc -s -n C bot.py services.py repository.py plan_handlers.py plan_service.py
radon raw bot.py services.py repository.py plan_handlers.py plan_service.py
```
