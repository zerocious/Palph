# Session Notes

Running log of changes made per coding session. Newest entries at the top.

---

## Session — 2026-05-22 (documentation sync + user flows)

Goal: синхронизировать все `*.md` с v0.8; зафиксировать стандартные user flows
и идеи сокращения кликов.

**Итог:** [user-flows.md](user-flows.md) создан; README, TODO, BACKLOG,
admin_commands, LEADERBOARD, tips/README обновлены. **533 теста** (без изменений кода).

### Changes

| # | Area | Change |
|---|------|--------|
| 1 | Docs | Новый [user-flows.md](user-flows.md): mermaid-диаграммы, таблицы кликов, §14 оптимизации |
| 2 | README | PA `/analytics` (10 разделов), таблицы БД v0.8, sad-pet shipped, ссылка на user-flows |
| 3 | TODO | Фокус post-v0.8; #2 sad-pet → done; #21 UX flows (Could) |
| 4 | BACKLOG | Секция UX navigation; cross-links |
| 5 | admin_commands | Export 20 таблиц; меню analytics; `/product_metrics` |
| 6 | LEADERBOARD / tips | Related docs, post-ship notes |

### Verification

- `python -m pytest --collect-only -q` → **533 tests collected**

---

## Session — 2026-05-22 (productivity tips — full stack)

Goal: починить «файл не найден», довести советы до production-quality:
JSON-контент, inline UX, геймификация, контекст + совет дня в утреннем напоминании.

**Итог:** shipped; **518 тестов** зелёных (492 → +26).

### Changes

| # | Area | Change | Files |
|---|------|--------|-------|
| 1 | Paths | Загрузка советов через `Path(__file__).parent` (`BOT_DIR`), не cwd | [bot.py](bot.py) |
| 2 | Content | `tips/*.json` (time-management, memory, links, bot-guide); HTML-формат; `action` line | [tips/](tips/) |
| 3 | UX | Кэш при старте; inline «🔄 Ещё совет», «📋 Все советы», пагинация, URL-кнопки | [bot.py](bot.py) |
| 4 | Schema | `user_tips_stats`, `user_tips_seen`; `tip_of_day_id/date` | [db.py](db.py) |
| 5 | Repository | `TipsRepository`: record_view, record_seen, resolve_tip_of_day, flashcards_due | [repository.py](repository.py) |
| 6 | Gamification | +1🪙/день; ачивка `10_tips_read` («Любознательный»); event `tip_viewed` | [bot.py](bot.py), [services.py](services.py), [achievements.json](achievements.json) |
| 7 | Medium | Контекстные tags (timer / not studied / cards due); cooldown 7д; совет дня в 🌅 | [bot.py](bot.py), [services.py](services.py) |
| 8 | FAQ | earn_coins, mission, efficiency, active_recall — про советы и bot-guide | [bot.py](bot.py) |
| 9 | Tests | 26 новых: files, content, gamification, medium, morning reminder | [tests/test_productivity_tips_files.py](tests/test_productivity_tips_files.py), … |

### Productivity tips — поведение

- **Категории:** ⏰ тайм-менеджмент, 🧠 память, 🎯 как пользоваться ботом, 🔗 ссылки.
- **Контекст:** активный таймер → `timer`; не учился сегодня → `study`/`focus`/`bot`; есть due cards → `flashcards`.
- **Cooldown:** `user_tips_seen` — не повторять тот же `tip_id` 7 календарных дней (если пул пуст — показать снова).
- **Совет дня:** стабильный `tip_id` на день в TZ пользователя; в утреннем reminder через `ReminderService(morning_tip_builder=...)`.
- **Монета:** первая «полноценная» просмотр-сессия за день → +1🪙; совет дня в reminder **не** считает view.
- **Ачивка:** 10 просмотров (`total_views`) → `10_tips_read`, +30🪙.

### Known issue

Пагинация списка (`tips:list` ◀️/▶️) вызывает тот же hook, что random/more → можно нафармить счётчик ачивки быстрее 10 уникальных советов. Монета не эксплуатируется (1/день). Отложено в BACKLOG.

### Verification

- `python -m pytest tests/` — **518 passed** (~53s)

### Docs

- `*.md` синхронизированы на момент сессии (518 tests, tips architecture, events).
  Позже — полный doc-sync + [user-flows.md](user-flows.md) (533 tests).

---

## Session — 2026-05-22 (user flashcards feature)

Goal: пользовательские флэш-карточки по предметам, новый flow учёбы
«предмет → режим», настройка источника карточек (микс / официальные / свои).

**Итог:** feature shipped; **492 теста** зелёных на момент merge (476 baseline + 16 новых); после tips — **518**.

### Changes

| # | Area | Change | Files |
|---|------|--------|-------|
| 1 | Schema | Таблица `user_flashcards` (UNIQUE user+subject+term, CASCADE) + колонка `flashcard_source` в `notification_settings` (default `mix`, idempotent migration) | [db.py](db.py) |
| 2 | Repository | `UserFlashcardRepository`: create/list/delete, лимит 100/subject, hash `u{card_id:07x}`; при delete — cascade `flashcard_progress`. `UserRepository` сохраняет/читает `flashcard_source` | [repository.py](repository.py) |
| 3 | Study flow | `load_flashcards_for_study(user_id, subject, source)`; FSM перестроен: **❓ Квизы → предмет → режим** (было режим → предмет). Состояния: `choosing_subject` → `choosing_mode` | [bot.py](bot.py) |
| 4 | UI | FSM создания карточек (term ≤200, definition ≤1000); «📇 Мои карточки» в ⚙️ (список по предметам, add/delete); кнопка «🃏 Флэш-карты» циклит mix→official→own | [bot.py](bot.py) |
| 5 | Progress | Официальные + свои карты в mastery-bar и «🔔 К повторению» (`card_hashes` = official + user) | [bot.py](bot.py) |
| 6 | Tests | 11 + 5 тестов: repo CRUD, source mixing, hash isolation | [tests/test_user_flashcards.py](tests/test_user_flashcards.py), [tests/test_flashcard_source.py](tests/test_flashcard_source.py) |

### New study flow

```
❓ Квизы → выбор предмета → выбор режима → сессия
```

«⬅️ Назад к предметам» из mode-picker возвращает к subject-picker.

### User flashcards — поведение

- **Создание:** ⚙️ Настройки → 📇 Мои карточки → предмет → «➕ Добавить» → term → definition.
- **Удаление:** из списка карточек предмета; вместе с row удаляется SM-2 progress по `u{id:07x}`.
- **Повторение:** тот же SM-2 flow, что у официальных карт (`flashcard_progress`, +1🪙, leaderboard `flashcard_reviewed` с `is_new` / `quality ≥ 3`).
- **Источник пула:** ⚙️ → «🃏 Флэш-карты» — **Микс** (official+own) / **Официальные** / **Свои**. Default `mix`.
- **Hash:** официальные `md5(term)[:8]`; свои `u{card_id:07x}` — коллизий нет, один SM-2 namespace.

### Design notes

- Лимит 100/subject и UNIQUE(term) — защита от спама и дублей без модерации.
- `load_flashcards_for_study` — единая точка сборки пула; режим «Свои» при пустом списке показывает подсказку про 📇 Мои карточки.
- Leaderboard scoring **не менялся** — user cards проходят те же hooks (`grant_card_pts`, `is_new` до upsert). См. [LEADERBOARD.md](LEADERBOARD.md).

### Verification

- `python -m pytest tests/` — **492 passed** (476 → +16)

### Docs

- README, TODO, session_notes, admin_commands (content_stats), LEADERBOARD — синхронизированы в этой сессии.

---

## Session — 2026-05-20 (post-v0.8 follow-ups)

Goal: ship два deferred follow-up'а на main после merge PR #3 и PR #4:
sad-pet GIF в вечернем напоминании (вместо text-only) + кнопка
`👥 Друзья` в profile inline keyboard.

**Итог:** PR #5 = `55e70ec` merged в main; 476 тестов зелёных
(475 на предыдущем main + 1 новый тест на FileNotFoundError fallback).
Worktrees + branches очищены. **Бот feature-complete от original brief**
с code-side perspective — остаётся только реальное art-replacement в
`assets/pet/` и quiz content (sections II-IV) — content-authoring задачи.

### Changes

| # | Area | Change | Files |
|---|------|--------|-------|
| 1 | Service | `ReminderService._send_evening` для `emotion='sad'` теперь шлёт `bot.send_animation(assets/pet/sad.gif, caption=...)` через lazy-import `FSInputFile` + `render_pet(None, "sad", animated=True)`. Graceful `FileNotFoundError` fallback: если asset отсутствует — `bot.send_message` с тем же sad-pet текстом. Non-sad emotion path (defensive edge case) сохраняет `send_message` с generic fallback копией. | [services.py](services.py) |
| 2 | UI | Кнопка `👥 Друзья` в profile inline keyboard. Добавлена во ВСЕ ТРИ профильных keyboard-сайта: `cmd_profile`, `back_to_profile`, `pet_back_to_profile`. Callback reuses existing `friends_back:` handler (исторически "back to friends list" из inner screens, теперь doubles as "main friends-tab view" entry). Layout grid: adjust(2, 1, 1, 1, 1). | [bot.py](bot.py) |
| 3 | Tests | Реорганизация `tests/test_reminder_service.py` для нового send_animation path: `TestSadPetAnimation` (2) — animation called, send_message НЕ called; `TestAssetMissingFallback` (1 новый) — monkeypatch `services.render_pet` бросать FileNotFoundError, проверяем graceful text fallback; `TestFallbackCopy` (1) — defensive has_studied_today=1 ещё использует send_message; `TestEdgeCases` (3) — empty/blocked/unknown-TZ обновлены под send_animation. **Net +1 тест.** | [tests/test_reminder_service.py](tests/test_reminder_service.py) |

### Design notes

- **Lazy `FSInputFile` import** в `_send_evening` (внутри try/except) — `services.py` намеренно не импортирует `aiogram.types.FSInputFile` на module-level, чтобы сохранять loose coupling с UI-фреймворком. Caller-pattern.
- **Не переименовывали `friends_back` callback** — функционально работает, переименование задело бы много callback-сайтов в inner friends screens без functional gain. Comment в `bot.py` объясняет.
- **Пет image в /profile root НЕ добавлен** — для этого пришлось бы delete-and-resend всю message при back-navigation (text→photo transition). Защёлкнули в pet detail screen через `pet_menu` callback. Polish item.

### Verification

- `python -m py_compile bot.py services.py` — clean
- `python -m pytest tests/` — **476 passed** (475 → +1 новый)
- pip-audit — clean (без новых dependencies)

### Cleanup на merge

- Worktrees: `claude/leaderboard-system`, `claude/pet-sad-reminder`,
  `claude/post-v0.8-followups`, `claude/elastic-mirzakhani-0b7ce0`
  все удалены (последний — orphaned directory, файлы на диске, но
  git-tracking уже нет).
- Local + remote branches удалены для всех PR'ов 2/3/4/5.
- Local `main` синхронизирован с `origin/main` (`55e70ec`).

### Что ОСТАЛОСЬ для full feature-complete release

- **Real artwork** — file replacement в `assets/pet/` (drop replacement
  PNGs/GIFs over placeholder programmer-art). Без code changes.
- **TODO #1 content** — quiz content для sections II-IV (content
  authoring задача).
- **Manual QA** — ~30 минут live test против real Telegram с main
  admin аккаунтом перед announcement.

Code-side бот **готов к v0.8 announcement**.

---

## Session — 2026-05-19 (sad-pet reminder hook)

Goal: маленький tail-of-#16 — sad-pet интеграция в evening reminder.
Уже merged pet data layer даёт `derive_emotion` чистую функцию;
здесь подключаем её в `ReminderService._send_evening`, чтобы вечернее
напоминание показывало «🐾😢 Питомец загрустил» вместо generic
copy. Отдельный PR на ветке `claude/pet-sad-reminder` off main —
не пересекается с merged PR #3 leaderboard.

### Changes

| # | Area | Change | Files |
|---|------|--------|-------|
| 1 | Service | `ReminderService._send_evening` теперь вычисляет per-user `now_local` (через `pytz.timezone(tz)`) и вызывает `derive_emotion` для выбора копи. `is_studying`/`recently_excited` приходят False (нет FSM-доступа в scheduler); `has_studied_today` — из user dict. Sad-path → `_EVENING_SAD_PET_TEXT` («🐾😢 Питомец загрустил»). Fallback на старый текст — defensive, на случай если has_studied_today=1 слипнётся через SQL filter. Unknown TZ → naive datetime fallback. | [services.py](services.py) |
| 2 | Tests | `tests/test_reminder_service.py` (новый файл) — 6 тестов: sad-pet копи отправляется (default path), массовый send (3 user'а), fallback на не-sad emotion, empty users list → no-op, `TelegramForbiddenError` graceful handling, unknown TZ fallback. | [tests/test_reminder_service.py](tests/test_reminder_service.py) |

### Design decision

- **Не запрашиваем pet.last_excited_at** для recently_excited — это потребовало бы JOIN с user_pet и +1 SQL per user в reminder loop. `recently_excited=False` defensive подразумевает: вечером, после полной рабочей дня, любой level-up уже за пределами 5-минутного окна. Если станет проблемой — добавить отдельную колонку в `get_users_due_for_evening` query или JOIN.
- **Sad-pet копи стабильно одна и та же** для всех sad-юзеров. Persistent variation (e.g., рандомизация фраз) ускоряет engagement-decay; оставляем константную копи + emoji 🐾😢 как стабильный визуальный маркер.
- **Морning reminder не трогаем** — TODO #16 specifically requests sad-pet в reminder (evening). Утро остаётся generic с тем же tone что и было.

### Verification

- `python -m py_compile services.py` — clean
- `python -m pytest tests/` — **238 passed** (232 baseline + 6 new)
- Не запускали live integration (требует bot.send_message с реальным TG) — unit tests с AsyncMock покрывают.

### Follow-up after PR #4 merges

- **sad-pet image attachment** в `_send_evening`: после merge PR #3 в main всё `assets/pet/sad.gif` и `render_pet` доступны. ~10 LOC commit: вместо `bot.send_message(...)` использовать `bot.send_animation(FSInputFile(...sad.gif), caption=...)`. Отдельный коммит, чтобы PR #4 оставался минимальным.

---

## Session — 2026-05-19

Goal: design and ship the weekly leaderboard. Multi-phase build across one
long session: spec → Phase 0 audit → Phase 1 data layer → Phase 2a
user-facing view + privacy → Phase 2b rollover scheduler + rewards.
Pet PR (#2) merged early in the session to unblock leaderboard wiring.

**Итог:** Phase 2 целиком закрыт (a + b); **332 теста** зелёных
(185 baseline + 47 pet + 100 leaderboard); пет PR #2 squash-merged в
`main` как `9203aab`; PR #3 на `claude/leaderboard-system` содержит
весь leaderboard-стек. Phase 3 (streak freeze UI), Phase 4 (friends)
остаются deferred с прокладкой (схемы + repo-методы + design notes)
для следующих сессий.

Plan / spec файл: [LEADERBOARD.md](LEADERBOARD.md) (v1.1)

### Phases shipped

| Phase | Commit | What landed |
|-------|--------|-------------|
| Spec v1.1 | `3c46644` | LEADERBOARD.md — formula, segments, privacy, anti-abuse, rewards, phasing |
| Phase 0 — Event audit | `53e8c79` | Single patch: `flashcard_reviewed.is_new` added at the rate-handler call site, captured BEFORE `upsert_progress` writes the row. Other 4 scoring events already had what backtest needs. |
| Phase 1 — Data layer | `930a2e8` | 4 tables (`daily_score_counters`, `weekly_scores`, `streak_freezes`, `weekly_badges`) + `users.hidden_from_leaderboards`; 4 pure scoring helpers in services.py; `LeaderboardRepository` (7 methods) with lock-free atomic-UPDATE cap enforcement; wiring into 4 hooks in bot.py + `StudyService.complete_session`; 64 tests; `analysis/leaderboard_backtest.ipynb` |
| Phase 2a — Visibility | `9ab3ff1` | `LeaderboardRepository.get_ranked_segment` / `get_user_rank` / `award_badge` / `get_active_badges`; `UserRepository.set_hidden_from_leaderboards` / `is_hidden_from_leaderboards`; `LeaderboardService.render_leaderboard` (segment auto-routing, hidden-user marker); `/leaderboard` slash command; "Лидерборды: Виден/Скрыт" toggle in settings menu; 23 tests |
| Phasing doc | `a25655d` | LEADERBOARD.md phasing section updated to reflect shipped vs deferred |
| Docs refresh | `5626c6a` | README + TODO + session_notes synced after Phase 2a |
| Phase 2b — Rollover + rewards | `25c25b3` | `LeaderboardService.run_rollover` (top-3 main + breakthrough newbie + top-10% coin bonus, gated by `award_badge` INSERT OR IGNORE rowcount → idempotent on re-run); `leaderboard_scheduler` in `tasks.py` (UTC Tuesday 00:00 anchor — all global TZs have crossed local Mon by then); `_compute_ended_week_iso` pure helper; bot.py wires scheduler into `background_tasks`; 13 tests cover correctness, idempotency, top-10% threshold, hidden eligibility, ISO year boundaries |
| Phase 3 — Streak freeze | `2912da4` | `LeaderboardRepository.purchase_freeze` (atomic под `db.lock`, cooldown через `granted_at > now - 7 days`, returns `purchased`/`insufficient_coins`/`cooldown_active`); `has_active_freeze`, `consume_freeze_if_active(user_id, today_local)`, `get_freeze_cooldown_remaining_days`; `StreakService` принимает optional `leaderboard_repo` и на miss-day сначала consume freeze (если есть) перед reset; profile-кнопка «❄️ Заморозить стрик» + confirm-экран с cost/balance/availability; 17 тестов покрывают purchase atomicity, cooldown, consume, streak integration |
| Phase 4 — Friends system | `473006f` | 2 новые таблицы: `friend_requests` + нормализованные `friendships` (user_a<user_b, одна строка на дружбу). `FriendRepository` с полным lifecycle: `send_request` (включая auto-accept на reverse pending), accept (transactional DELETE+INSERT), reject/cancel, get_friends UNION, are_friends/remove_friend (symmetric). `LeaderboardService.render_friends_tab` (top-3 medals + (Вы) маркер). `/friends` команда + add-by-ID FSM + 📩 запросы с accept/reject + ➖ remove с confirm; cross-user notifications через bot.send_message с graceful exception handling. 29 тестов. |
| Docs sweep | `43b8901` | README features list + LEADERBOARD.md link added; BACKLOG entries для username-search + invite-links добавлены; admin_commands.md header note о user-facing командах. |
| Username-search для /friends | `25c5b27` + `6870bab` (fix) | BACKLOG-идея поднятая до ship в той же сессии. `users.username TEXT` migration + `UserRepository.refresh_username` / `find_user_id_by_username` (case-insensitive COLLATE NOCASE). `UsernameSyncMiddleware` обновляет username на каждом Message/CallbackQuery (graceful exception handling — sync-failure НЕ ломает handler). `services.parse_friend_query` — pure parser (`@alice`/`alice`/`12345`/`-12345`/empty). `friend_add_process` сначала пытается username path, fallback на numeric. 22 теста + fix-commit 3 теста (first-message gap). |
| New tests sweep (middleware + integration) | `49d392a` | Closed the "I claimed it works but never tested" middleware gap (8 tests) + 9 cross-component end-to-end integration flows (full leaderboard journey, friends lifecycle, freeze cycle, privacy effect, username search e2e, multiplier reordering). |
| Friend invite-links via deep links | `a2bf5bd` | Second BACKLOG-promotion ship. `friend_invite_tokens` table (token PK, 30-day TTL). `FriendRepository.create_invite_token` (secrets.token_urlsafe, ~16 char), `find_invite_token` (rejects expired), `accept_invite` (atomic, skip pending, clean up any reverse pending). `/share_friend` команда → формирует `t.me/<bot_username>?start=friend_<token>`. `/start friend_<token>` deep-link → `_process_friend_invite_link` резолвит токен, создаёт auto-friendship, уведомляет обе стороны. `bot_username` кеш через bot.get_me() на старте. 17 тестов: token uniqueness/expiry, find resolution, accept happy/self/already, multi-use (одна ссылка → N друзей), pending-request cleanup, end-to-end flow. |
| Rename: StudyBuddy → Palph | `a386ff8` | User-visible strings (welcome, FAQ, log message), doc titles (README, LEADERBOARD, admin_commands), export filename `palph-export-`, docker-compose comment. Kept legacy `studybuddy_bot` logger, `studybuddy.db` DB filename, `studybuddy-{date}.db` backup pattern, `container_name: studybuddy-bot` for ops-stability. New README "Имя проекта" callout документирует split. |
| Pet art track Phase A | `9421d0a` | Pillow placeholder generator (125 PNG + 5 GIF programmer-art), `services.render_pet` path-resolver с fallback chain, level-up notification в `StudyService.complete_session` со списком разблокированных предметов через `_notify_level_up`. 21 теста (render_pet path resolution + animated + fallbacks; level-up fires/skips/unlocks logic). |
| Pet art track Phase B | (pending commit) | Customization UI: pet detail screen (фото + name + level + xp), color/accessory pickers с 4-state buttons (⭐/✓/💰/🔒), confirm dialog для покупки, equip-from-owned instant, rename через FSM. Helper `_picker_button_label` инкапсулирует 4-state logic; pure function. 11 тестов покрывают каждое из 4 состояний + edge cases (no pet, precedence). |

### Key design calls captured

- **Tasks at 40 pts** (math problems = mission lever). Single-tier instead of original "3@50 + 5@10" — same daily cap of 200 pts, one rule to communicate.
- **Streak as weekly multiplier** (1.00 / 1.05 / 1.10 / 1.20 by tier) replacing useless flat `streak_days × 10`. Two users with same activity differ by streak-driven multiplier — meaningful ranking signal that rewards consistency without competing with the math-focus headline number.
- **Streak freeze cost tiered** (500 / 750 / 1000 by current streak length) — scales with what you have to lose.
- **All caps are daily** (LLM in original spec said "weekly" — corrected).
- **Cards: pts only on `quality >= 3`** (successful review). Wrong answers neither grant pts nor burn the daily-8 cap.
- **"New" card definition** = no existing row in `flashcard_progress` (NOT `repetitions == 0`, which is ambiguous after a reset). Captured at the call site before upsert.
- **MCQ counts as quiz** for scoring (single +5/correct surface, shared series counter).
- **Series counter resets at midnight local TZ** (clean daily slate, matches the daily-cap pattern).
- **Privacy opt-out** as single toggle in settings; hidden users still earn rewards, just don't appear in others' public top.
- **`now_local` optional** on all `grant_*` methods. Production callers pass `user_id` only (repo looks up TZ); tests pass explicit `now_local` for determinism.
- **No locks inside `grant_*`.** Atomic `UPDATE … WHERE counter < cap` enforces cap-safety. `grant_time_pts` is the one real read-modify-write but per-user race is impossible by app logic (one timer at a time).
- **Backtest notebook** validates the formula on existing `events` + `study_sessions` data before any user sees scores. Drives pandas/matplotlib/jupyter into requirements-dev.txt.

### Sequence + pet PR merge

Pet PR (TODO #16 data layer) was already a PR from the earlier session; merged via `gh pr merge --squash` (`9203aab`). Leaderboard branch rebased onto new main → `claude/elastic-mirzakhani-0b7ce0` (pet worktree) is now stale and can be deleted at convenience.

### Files modified this session (leaderboard branch only)

- **New:** `LEADERBOARD.md`, `analysis/leaderboard_backtest.ipynb`, `tests/test_leaderboard_helpers.py`, `tests/test_leaderboard_repository.py`, `tests/test_leaderboard_service.py`
- **Modified:** `db.py` (+108 lines, 4 tables + privacy column migration), `repository.py` (+440 lines, LeaderboardRepository + UserRepository.is_hidden* + set_hidden*), `services.py` (+215 lines, 4 helpers + LeaderboardService), `bot.py` (+73 lines, imports + globals + privacy toggle + /leaderboard command + 4 hook patches + `is_new_card` capture), `requirements-dev.txt` (+3 deps for notebook)

### Verification

- `python -m py_compile bot.py db.py repository.py services.py` — clean
- `python -m pytest tests/` — **319 passed** (185 → +47 pet → +64 Phase 1 → +23 Phase 2a)
- Schema smoke-test: `init_db` creates all 4 leaderboard tables + `hidden_from_leaderboards` column on `users`; idempotent on second run
- Notebook JSON validates with `json.load`

### Deferred to follow-up sessions

(Empty — LEADERBOARD.md spec полностью закрыт. Все 4 фазы shipped в PR #3.)

### Out of leaderboard scope but still open

- **Pet UI / art track** — TODO #16 в TODO.md. Data layer уже в `main` после
  PR #2. Остаётся UI кастомизации в профиле, Pillow build-script для 125
  PNG-ассетов, render_pet функция, level-up notification, sad-pet hook
  в ReminderService.

---

## Session — 2026-05-18

Goal: data layer slice of TODO #16 (полноценный питомец) — schema +
`PetRepository` + `derive_emotion` + XP-grant в `complete_session` + тесты.
UI / assets / `render_pet` / Pillow-build script — отдельный трек, в этой
сессии не трогаем.

### Changes

| # | Area | Change | Files |
|---|------|--------|-------|
| 1 | Schema | Две новые таблицы в `init_db` executescript (`IF NOT EXISTS`): `user_pet` (user_id PK, name, color, accessory, level, xp, last_excited_at, created_at) и `user_pet_inventory` (composite PK user_id+item_type+item_value, purchased_at). Обе с `ON DELETE CASCADE` от users. Эмоция не хранится — выводится в `derive_emotion`. | [db.py](db.py) |
| 2 | Repo | Новый `PetRepository` с каталогами `COLOR_CATALOG`/`ACCESSORY_CATALOG` (цены по формуле спеки) и методами `create_pet_with_defaults`, `get_pet`, `get_inventory`, `add_xp` (auto-creates pet + auto-marks excited при level-up), `purchase_item` (атомарная под `self.db.lock` — re-read coins/level/ownership/deduct/insert/auto-equip, возвращает status-строку), `equip` (с ownership check), `rename`, `mark_excited`. | [repository.py](repository.py) |
| 3 | Service | Module-level pure function `derive_emotion(*, is_studying, recently_excited, has_studied_today, now_local) -> str`. Priority: studying > excited > sad > sleepy > happy. Sleepy = `[22:00, 06:00)` — полуоткрытый интервал. Все аргументы keyword-only. | [services.py](services.py) |
| 4 | Service | `StudyService.__init__` принимает `pet_repo: PetRepository \| None = None`. В `complete_session` после `add_coins` (всё ещё под `db.lock`): `pet_repo.add_xp(user_id, duration)`; если `earned` non-empty — `mark_excited`. Level-up отмечает excited сам внутри `add_xp`. | [services.py](services.py) |
| 5 | Wiring | bot.py: добавлен `PetRepository` в imports, `pet_repo = PetRepository(db)` в `main()`, передан в `StudyService(...)`, добавлен в `global`-декларацию. | [bot.py](bot.py) |
| 6 | Tests | `tests/test_pet_repository.py` — 32 теста: create defaults + idempotency + custom name, get_pet/inventory None-paths, add_xp auto-create + xp-приращение + 8 параметризованных значений xp→level + level-up marks excited + no-op на 0/-N минутах, purchase happy path + insufficient coins/level + already_owned + unknown_item + no_pet, equip ownership/invalid type, rename existing/nonexistent, mark_excited. | [tests/test_pet_repository.py](tests/test_pet_repository.py) |
| 7 | Tests | `tests/test_derive_emotion.py` — 15 тестов: 5 priority-order сценариев + 10 параметризованных hour-boundary (21/22/23/0/3/5/6/7/12/18). | [tests/test_derive_emotion.py](tests/test_derive_emotion.py) |

### Design decisions

- **Pure function для эмоции** с примитивами в input'е (без db-доступа внутри): caller сам считает `recently_excited` из `pet.last_excited_at`. Так `derive_emotion` тривиально тестируется без БД, и сам цикл «прочитал → посчитал → render» остаётся прозрачным.
- **`last_excited_at` на user_pet, а не отдельный счётчик ачивок**: и level-up, и ачивка пишут в одно поле — derive_emotion смотрит одну колонку и одно дельта `< 5 min`. Если оба события за минуту — пишется второе, эффект тот же.
- **Auto-create pet в `add_xp`** (а не явный шаг при `/start`): data layer self-contained. Первая учебная сессия сама создаёт питомца с дефолтным именем «Питомец». UI-слой потом сможет дать кнопку «Переименовать», не заботясь о том, существует ли уже row.
- **Caller-holds-lock паттерн** для всех методов кроме `purchase_item`: по образцу `UserRepository.add_coins`. `complete_session` уже держит `db.lock` на всю композитную операцию, а `purchase_item` запускается standalone и спека явно требует transactional re-read под локом.
- **Sleepy boundary `[22:00, 06:00)`** — полуоткрытый интервал. 22:00 уже sleepy, 06:00 уже нет. Просто и тестируемо одним условием `hour >= 22 or hour < 6`.

### Verification

- `python -m py_compile db.py repository.py services.py bot.py` — все 4 syntax OK
- `python -m pytest tests/` — **232 passed in 16.19s** (185 предыдущих + 47 новых, ничего не сломалось)

### Deferred (для следующих сессий по #16)

- `render_pet(user_pet, emotion, *, animated=False) -> FSInputFile` — функция-сборщик путей к PNG/GIF
- Pillow build-script `scripts/build_pet_assets.py` — 5 поз × 5 цветов × 5 аксессуаров → 125 PNG + 5 GIF
- Placeholder-ассеты для code-track-разработки UI до того, как арт-трек закончит
- UI-flow кастомизации в `bot.py`: профиль с превью питомца, 4-state picker (⭐ / ✓ / 💰 / 🔒), confirm-dialog на покупку, FSM для rename
- Level-up notification — два сообщения (excited.gif + список разблокированных предметов с ценой)
- Sad-pet reminder hook — использовать `derive_emotion` в утреннем/вечернем напоминании ReminderService

---

## Session — 2026-05-17

Goal: ship v0.7 — expand study (subjects + SM-2 flashcards + MCQ +
photo tasks), real digital pet (1 design + multi-emotion + customization),
main-menu 2×2 redesign, FAQ rewrite + tech-support absorbed, news channel
link, `/help` command.

**Итог:** 5 из 6 пунктов плана закрыты + 12 бэклог-пунктов (admins→БД,
резюм таймера, **Dockerfile+pytest**, **система трекинга прогресса**,
**daily backup БД**, **PA-аналитика — основа** /cohort_stats /funnel /dau
/feature_usage /export /analytics dashboard, **FAQ v2** — миссия проекта +
гарантия эффективности, **cybersecurity baseline** — rate limiter +
pip-audit CI + offsite backup template, **append-only events table** —
foundation для funnel/cohort/path analysis, **`/parse_logs` ETL** —
bot.log → CSV для historical backfill, **`/export all` ZIP** — все 10
таблиц + metadata.json одним сообщением для Jupyter, **PA-расширения**:
/segments /content_stats /event_timeline /heatmap — закрыли секцию
Could из PA-roadmap'а целиком) + bonus-fix `stop_active_timer` + GitHub
push (`zerocious/Palph`). Pytest: **185 тестов** (~13 сек). Остался
только пункт #16 (полноценный питомец) — самая большая фича, разбита
на код-трек и арт-трек.

Plan file: `C:\Users\User\.claude\plans\make-a-new-session-merry-castle.md`

### Planned scope

| # | Item | Status |
|---|------|--------|
| 7 | Main menu → 2×2 grid; remove tech-support button (absorbed into FAQ) | ✅ done |
| 3 | News button: real channel link via inline URL button | ✅ done |
| 5 | `/help` command + register in Telegram command picker (closes TODO #3) | ✅ done (admin-focused) |
| 4 | FAQ rewrite + absorb tech-support contact + cover new features | ✅ done |
| 1a | Study content restructure: `quizzes/` → `study_materials/`, add math + english subject folders | ✅ done |
| 1c | MCQ study mode (4-option inline keyboard) | ✅ done |
| 1d | Photo-task mode (image problem → 2 retries → solution image) | ✅ done |
| 1b | SM-2 flashcards (per-card ease factor + 3-button quality rating ❌/😐/✅) | ✅ done |
| 2 | Digital pet: 1 design + 5 derived emotions + hybrid customization (level unlocks visibility, coins buy ownership) + `user_pet_inventory` table + 4-state picker + atomic purchase flow + real PNGs/GIFs | TODO |

### Decisions captured up front

- **Pet scope**: 1 pet design (not 5 species), name + color + accessory customization, derived multi-emotion (happy/sad/excited/sleepy/studying — *not* user-chosen), level + XP. No hunger/needs decay loop this session.
- **Pet customization economy** — hybrid model: **level gates visibility** (you can't see `crown` before lvl 8), **coins gate purchase** (once visible, you pay the price to own it). Pricing formula: `unlock_level × 20` for colors, `× 30` for accessories. Defaults (orange + none) are free at pet creation. New `user_pet_inventory` table tracks ownership. Picker shows 4 states (⭐ equipped / ✓ owned / 💰 N buy / 🔒 ур.N · 💰 X locked-with-teaser-price).
- **Pet rendering**: real PNG images via `send_photo` + caption for routine surfaces; 2-frame GIFs via `send_animation` reserved for level-up and sad-pet moments (two-message dramatic beat). Pillow used at *build time only* to bake 125 pre-rendered PNGs from 9 source assets.
- **Item 6** (real-time clock + personal task manager): **deferred** — value-vs-scope cost too low for now.
- **SM-2 replaces `[1,2,4,7]` intervals for flashcards only** (Leitner considered but rejected — SM-2's per-card ease factor wins for ~25 extra lines of code; aligns with original brief). Situational-text quiz (`quiz_progress` table) stays on fixed intervals because its keyword-grader doesn't give a quality gradient.
- **Content** (channel URL, final FAQ wording, math/English/IM terms) supplied by user after code lands — plan creates structure + empty files, data-driven discovery hides empty subjects/sections automatically.

### Changes

| # | Area | Change | Files |
|---|------|--------|-------|
| 7 | UX / main menu | Главное меню → сетка 2×2 (📚 Учеба, ❓ FAQ, 📊 Мой профиль, 📢 Новости). Кнопка «🛠 Техподдержка/Отзыв» удалена, `handle_support` вместе с ней; контакт `@zerocious` перенесён в FAQ. `get_main_keyboard` теперь `.adjust(2, 2)`. По запросу пользователя позиции FAQ и Учёба поменяны местами (Учёба слева как «основная» кнопка). | [bot.py](bot.py) |
| 3 | News | `handle_news` → инлайн-кнопка `url=CHANNEL_URL` с текстом «📢 Открыть канал»; интро-строка приглашает подписаться. Константа `CHANNEL_URL = "https://t.me/palph_study"` вынесена в конфиг-блок рядом с `SERVER_TIMEZONE`. | [bot.py](bot.py) |
| 5 | `/help` (admin-focused) | Новая `cmd_help`: админам — полный список (админские + общие команды + кнопки меню); не-админам — мягкое перенаправление на `❓ FAQ` и приглашение написать прямо в чат. `bot.set_my_commands` в `main()` использует два scope'а: `BotCommandScopeDefault` для всех (`/start`, `/stop`), `BotCommandScopeChat` per-admin для расширенного списка (+`/help`, `/reply`, `/broadcast`, `/notif_status`). Так обычные пользователи не видят админские команды в `/-`пикере. Try/except защищает от падения, если у админа ещё нет чата с ботом. Импорты: `BotCommand`, `BotCommandScopeDefault`, `BotCommandScopeChat`. | [bot.py](bot.py) |
| 4a | FAQ переписан | 7 новых вопросов в порядке от пользователя: (1) почему учиться с ботом эффективнее, (2) зачем питомец, (3) на что тратить монеты, (4) как зарабатывать монеты, (5) SM-2 для флэш-карт и его эффективность, (6) интервальное повторение (Эббингауз 1885), (7) active recall и его методы в боте. Финальная строка: «🛠 Остались вопросы? Напиши @zerocious». Полный размер ~3000 символов, в одно сообщение. Часть содержания — forward-looking (упоминает SM-2 / флэш-карты / MCQ / photo tasks, которые ещё не реализованы). | [bot.py](bot.py) |
| backlog #7 | admins.json → БД | Новый `AdminRepository` (CRUD + idempotent `add` через `INSERT OR IGNORE`). Глобал `ADMINS` теперь in-memory кеш, заполняется в `main()` из БД. `is_admin()` остаётся синхронным (горячий путь — catch-all handler на каждое сообщение). Миграция `_migrate_admins_json_to_db()` запускается раз: импортирует `admins.json` → таблицу `admins`, переименовывает файл в `admins.json.migrated` (повторные запуски — no-op; коллизия имени → суффикс timestamp'а). `MAIN_ADMIN_ID` всегда seedится в БД на старте — гарантия, что главный админ работает даже после `rm studybuddy.db`. Новые команды (только главный админ): `/addadmin <id>` (вставка в БД + кеш + расширение `/-пикера` через `BotCommandScopeChat`), `/rmadmin <id>` (с защитой от удаления главного), `/listadmins`. `DEFAULT_COMMANDS` / `ADMIN_COMMANDS` подняты на модульный уровень, чтобы /addadmin мог их применить новому админу динамически. Закрывает [TODO #7]. | [repository.py](repository.py), [bot.py](bot.py), [admin_commands.md](admin_commands.md), [.gitignore](.gitignore) |
| backlog #8 | Полное возобновление таймера | `reconcile_stale_timers` теперь различает 3 ветки: completed (elapsed ≥ duration → авто-завершение + уведомление), **resumed** (elapsed < duration → реконструкция `FSMContext` из `dp.storage`, пересоздание asyncio-таска через `start_timer`; FSM-запись НЕ удаляется), broken/malformed → чистим. `run_timer_task` рефакторен — теперь читает `start_time` из `state.data` и спит ровно до `start_time + duration` (раньше всегда спал `duration * 60`). Это унифицирует поведение для свежих и возобновлённых таймеров: для свежих `start_time` ≈ `datetime.now()`, поэтому результат тот же; для возобновлённых правильно учитывается прошедшее время. Пользователь после рестарта получает сообщение «♻️ Бот перезапустился — но твой таймер продолжается! Осталось: N мин» + клавиатура таймера. Лог `reconcile.resume user_id=X duration=Y elapsed=Z remaining=N`, summary-метрика `resumed` заменила `cleared`. Добавлен импорт `StorageKey` из `aiogram.fsm.storage.base`. Закрывает [TODO #8]. | [bot.py](bot.py) |
| 1a | Реструктура `study_materials/` | `quizzes/` снесена. Новая структура: `study_materials/{industrial-management,math,english}/`. Industrial-management сохраняет 4-секционный курс через под-папку `situational/section-{i,ii,iii,iv}.txt` (section-i.txt = 15 терминов перенесён, остальные пустые плейсхолдеры). Math и English без `situational/` — режим под учебники с теорией, для них он не нужен. Все 3 предмета получают плоский набор `flashcards.txt` + `mcq.txt` + `tasks/.gitkeep` как заглушки для будущих режимов (#13/#14/#15). Новый каталог `SUBJECTS: list[tuple[str, str]]` и `STUDY_MODES: list[tuple[str, str]]` определяют id ↔ label. Helpers `available_subjects()` (предметы с хотя бы одним непустым режимом) и `available_modes(subject_id)` (режимы с непустым контентом — по разному определяется для situational/flashcards/mcq/tasks). `available_quiz_sections(subject_id="industrial-management")` и `load_quiz_section(section, subject_id="industrial-management")` получили опциональный `subject_id` параметр — обратная совместимость 100%, существующий quiz-flow не сломан. Smoke-test: 8 assertions (структура папок, видимость только industrial-management, только section-i, 15 терминов в section-i, math/english пустые). Закрывает [TODO #12]. UI handlers пока **не** рефакторены — данные новые, поведение прежнее; UI-рефакт идёт в #13. | [bot.py](bot.py), `study_materials/` (создана), `quizzes/` (удалена) |
| extra | FAQ interactive | Старый монолитный FAQ (3000+ символов в одном сообщении) превращён в interactive-меню: 7 inline-кнопок-вопросов + 1 кнопка «🛠 Связаться с техподдержкой». Тап → message edits → показывает полный ответ + «◀️ К списку вопросов». Тап на back → возврат к меню (edit обратно). Один message-чан на весь FAQ-flow — не засоряем chat. Контент: `FAQ_ITEMS: list[dict]` с полями `id` / `btn` / `title` / `body` (порядок = порядок в меню); `FAQ_SUPPORT_ITEM` — отдельный dict для техподдержки; пользователь видит «Если у тебя есть вопрос... — напиши прямо здесь, перешлётся админам... Прямой контакт: @zerocious». Helpers: `_build_faq_menu_keyboard` / `_build_faq_answer_keyboard` / `_faq_lookup(id)`. Callbacks: `faq:show:{id}` → редактирует message в ответ; `faq:back` → возврат к меню. Все 8 button-лейблов ≤59 байт (Telegram лимит 64); все ответы ≤468 символов (лимит 4096). Бонус: в Q4 «Как зарабатывать монеты» добавлены упоминания новых источников из v0.7 (+1 за MCQ-правильный, +1 за флэш-карту, до +3 за task). Smoke-test: 7 ассертов (загрузка, btn-длины, лимиты callback_data, lookup, keyboards). | [bot.py](bot.py) |
| extra | Doc audit: admin_commands.md gap close (final session commit) | Найдено во время self-check'а в конце сессии: `admin_commands.md` отставал на 6 команд (`/segments`, `/content_stats`, `/event_timeline`, `/heatmap`, `/parse_logs`, `/export all`) от того что реально живёт в коде. Также было устаревшее «9 алиасов» в описании `/export` (теперь 10 с добавлением `events`). Доделал все 6 секций с примерами + объяснениями + обновил `/export` под двойную форму (`<alias>` vs `all`). Урок на будущее: каждый новый admin-handler требует одновременного апдейта в **5 местах**: ADMIN_COMMANDS picker, /help text, /analytics inline-menu (если visual), README features section, **admin_commands.md детальный reference**. Последний легко забыть, т.к. он самый длинный. Self-audit чеклист на будущее: grep `admin_commands.md` против `Command\("..."\)` в `bot.py` — что есть в коде, должно быть в docs. Финальный коммит сессии (`7374d12`). | [admin_commands.md](admin_commands.md) |
| extra | 4 PA-расширения: segments / content_stats / event_timeline / heatmap | **Закрыло секцию Could** в TODO.md PA-аналитики — все 4 идеи из изначального roadmap'а ship'нуты вместе. **`/segments`**: 5-уровневая user segmentation (never_started/tried/active/power/churned) с приоритезацией churned над active (churned = re-engagement actionable, важнее категории по числу сессий). Default churn threshold 14 дней — конфигурируется параметром. **`/content_stats`**: 4 sub-view'а — hardest situational terms (sort by AVG accuracy ASC, нужно ≥1 attempt), most-attempted MCQ (sort by total_count DESC), progress coverage (unique items touched per mode), flashcard EF distribution в 4 бакетах (<1.5 / 1.5-2.0 / 2.0-2.5 / ≥2.5). Hash→text mapping делает render-слой через `_build_hash_to_text_maps()` (загружает все sections/flashcards/mcq и строит dict[hash, text]) — service остаётся pure SQL. **`/event_timeline [hours]`**: SELECT из events с фильтром по created_at, clamp [1, 168], JSON.loads properties с fallback на `{}` для malformed. Render показывает HH:MM + user_id + event_name + top 2 properties, обрезка длинных values до 50 char. **`/heatmap [days]`**: 7-строчная × 8-колоночная сетка (weekday × 3-hour bucket), intensity-mapping через Unicode block chars `· ▁ ▃ ▅ ▆ ▇ █` нормализовано к peak'у. Peak detection для caption «Mon 12:00-14:59 (78 events)». Параметризовано days (clamp [1, 365]). **Архитектура**: 4 метода в AnalyticsService (~200 LoC), 4 render'а + 4 handler'а + 4 callback'а в bot.py (~280 LoC), регистрация в ADMIN_COMMANDS picker (теперь 21 команда) + `/help` + `/analytics` inline-menu (теперь 10 кнопок включая Close). **22 новых теста** (segments: priority order + churn override + never_started exemption + pct sums = 1.0; content_stats: sort order + EF bucket boundaries; timeline: filter window + limit + malformed JSON; heatmap: grid dims + bucket math + peak detection + window exclusion). Полный suite: **185 тестов** (~13 сек). | [services.py](services.py), [bot.py](bot.py), [tests/test_analytics_service.py](tests/test_analytics_service.py), [TODO.md](TODO.md), [README.md](README.md) |
| extra | `/export all` — ZIP всех таблиц + metadata.json | Один шаг от Telegram-чата до Jupyter-ready dataset. Новый метод `AnalyticsService.export_all_tables_zip(schema_version="v0.7") → (zip_bytes, metadata_dict)`. Использует `zipfile.ZipFile(mode="w", compression=ZIP_DEFLATED)` в `io.BytesIO()`: итерирует `EXPORTABLE_TABLES`, для каждой таблицы вызывает существующий `export_table_csv(alias)`, кладёт в архив как `{table_name}.csv`. **`metadata.json` schema**: `{exported_at: "ISO-8601 UTC", schema_version, row_counts: {table: int}, tables: [list]}` — последним записывается в ZIP чтобы row_counts были итоговые. Зачем metadata: воспроизводимость («этот анализ сделан на данных от 18 мая, schema v0.7, в users было N»). UI: `/export all` (один аргумент `all`); inline-menu в `/analytics → Export CSV →` имеет sticky-кнопку «📦📦 ALL tables (ZIP + metadata)» сверху отдельной строкой; tap → одно сообщение с ZIP-документом + caption (rows total, size KB, top-5 tables by row count). Хелпер `_send_all_tables_zip(reply_target)` shared между Message-handler'ом и Callback-handler'ом для DRY. Лог-событие `export.all_done tables=N rows=R size_kb=S`. **7 новых тестов** в `TestExportAllTablesZip`: zipfile.is_zipfile validation, все 10 expected CSVs + metadata.json в архиве, metadata schema (exported_at suffix Z, set(row_counts.keys())==set(tables)), row_counts соответствуют данным, ZIP'нутые CSV bytes-equal с individual `/export users`, metadata.json парсится после json.loads, empty-DB всё ещё валидный ZIP (header-only CSVs + counts=0). Полный suite: **163 теста** (~12 сек). | [services.py](services.py), [bot.py](bot.py) (_send_all_tables_zip helper + 2 hook'а), [tests/test_analytics_service.py](tests/test_analytics_service.py), [README.md](README.md), [TODO.md](TODO.md) |
| extra | `/parse_logs` — ETL bot.log → CSV | Дополняет events table: бот пишет structured events в `bot.log` (через `logger.info("event.tag user_id=X duration=Y ...")`) ещё до того как events table начала писаться (с предыдущего коммита). `parse_logs.py` парсит весь log + ротированные `bot.log.1..N` → CSV с теми же колонками что events table + extras (level, raw_text). **Regex дизайн**: `LOG_LINE_RE` ловит timestamp/logger/level/payload; `EVENT_HEAD_RE` определяет event_name (точечно-разделённый идентификатор в начале payload — `\w+\.\w+`); `KV_RE` использует **lazy quantifier + lookahead** `(\w+)=(.+?)(?=\s+\w+=|$)` для корректного разбора multi-word values (например `next=2026-06-03 14:35:01` где value содержит пробел). **Edge cases**: events без trailing key=values (типа `app.shutdown`) — `rest` optional через `(?:\s+(?P<rest>.*))?$`; user_id extracted top-level (как в events table); malformed lines → None и skip; non-UTF8 байты → `errors="replace"`. **Два режима использования**: (1) CLI — `python parse_logs.py [logs...] -o output.csv` с дефолтом `bot.log + bot.log.1..9`; (2) admin-команда `/parse_logs` импортирует `parse_log_file` + `to_csv_bytes`, шлёт CSV как Telegram document с caption (rows count + size + columns). Зачем именно так: для PA-резюме это **real data engineering** — log parsing + ETL → analytics-ready dataset. На своих 118 строк живого `bot.log` парсер дал 118 распарсенных событий за <100ms. **20 новых тестов** в `test_log_parser.py`: standard events / WARN-ERROR levels / multi-word values / unstructured (legacy) lines / malformed / user_id извлечение / partial line / trailing newline / multi-line file / CSV roundtrip / UTF-8 sanity (кириллица сохраняется) / column order stable / CLI main() exit codes (с/без файлов). Pure functions — без БД, без async, fast (~80ms). Полный suite: **156 тестов** (~12 сек). | [parse_logs.py](parse_logs.py) (новый), [bot.py](bot.py) (cmd_parse_logs + /-picker), [tests/test_log_parser.py](tests/test_log_parser.py) (новый), [README.md](README.md) |
| extra | Append-only events table | Новая таблица `events(id, user_id, event_name, properties, created_at)` — **foundation для PA-аналитики**. Одна строка на каждое значимое действие пользователя: `user_registered`, `session_started`/`session_completed` (с source=natural/stop/reconcile + achievements_earned), `mode_picked`, `subject_picked`, `quiz_answered` (subject/section/term_hash/is_correct/streak_after), `mcq_answered` (subject/question_hash/is_correct), `task_attempted` (subject/task_id/attempts/succeeded/coins), `flashcard_reviewed` (полный SM-2 trace: reps_before/after, ef_before/after, interval_before/after, next_review), `achievement_unlocked`. `properties` хранится как JSON (TEXT) — гибко расширяется без schema migrations. 2 индекса: `(user_id, created_at)` и `(event_name, created_at)`. **Новый `EventRepository.log(user_id, event_name, properties)`** — silent error-swallowing (event-logging НИКОГДА не должен ломать бизнес-flow бота; failure → warning в bot.log, INSERT не происходит). **14 hook'ов** в bot.py покрывают все mainstream user actions. `events` добавлен в `EXPORTABLE_TABLES` → `/export events` работает автоматически. Зачем именно это: с накопленным состоянием (mcq_progress.correct_count) можно сказать «сколько раз верно», но НЕ «через сколько минут после регистрации первый верный ответ». Events table даёт sequence — основу funnel/cohort/path/time-to-action анализа. Это industry-standard pattern (Mixpanel, Amplitude, Segment, Posthog все построены вокруг этой идеи). **15 новых тестов** в `test_event_repository.py`: basic INSERT, JSON serialization (dict/None/empty/unicode/nested), nullable user_id для system events, append-only semantics (не upsert), chronological ordering, multi-user isolation, error swallowing (бот не падает на bad properties). Полный pytest: **136 тестов** (~12 сек). Дальше: `/parse_logs` (ETL bot.log → events.csv для historical backfill) и `/export all` (ZIP всех таблиц + metadata.json). | [db.py](db.py), [repository.py](repository.py), [bot.py](bot.py) (14 hook'ов), [services.py](services.py), [tests/test_event_repository.py](tests/test_event_repository.py) |
| extra | Cybersecurity baseline + DDoS analysis | **Контекст:** пользователь спросил про DDoS-защиту и cybersecurity; приложил 2 reference-гайда (cybersecurity.txt + ddos guide.txt). Ключевой инсайт: гайды написаны под web-apps, но StudyBuddy — polling Telegram-бот без публичного HTTP-endpoint'а. Большая часть рекомендаций (Cloudflare/WAF/HTTPS/CSRF/login brute-force/origin IP) **не применима**. План написан в `.claude/plans/make-a-new-session-merry-castle.md` с shortlist того что РЕАЛЬНО актуально + что точно не нужно + bot-specific risks. **Реализовано из плана**: (1) **`UserRateLimiter`** в `services.py` — sliding-window per user_id (default 30 actions / 60s, warn-zone `[warn_at, max_actions)`, warn cooldown 30s, dict[int, deque] storage, не персистится — рестарт чистит). (2) **`RateLimitMiddleware`** в bot.py как aiogram BaseMiddleware — extract user_id из `data["event_from_user"]`, admins exempt, warn → polite message пользователю, block → silent drop. Регистрируется на `dp.message.middleware()` и `dp.callback_query.middleware()` ДО `include_router`. Лог-события `ratelimit.warned user_id=X` и `ratelimit.blocked user_id=X`. (3) **GitHub Action** `.github/workflows/security.yml` — `pip-audit --strict` на requirements.txt + requirements-dev.txt; trigger: weekly Mon 07:00 UTC + workflow_dispatch + PR с изменением requirements.txt. (4) **Template скрипт** `scripts/backup_offsite.sh.example` — GPG-encrypt → rclone copy в B2/S3 + cleanup по retention. С полным setup-инструкции (генерация GPG key, отправка private key OFF the VPS, cron). **Не делал (требует внешних действий пользователя)**: 2FA на BotFather/GitHub/email, VPS hardening (ufw/fail2ban/unattended-upgrades — будет когда деплой), реальная настройка rclone+B2. Тесты: 15 в `test_rate_limiter.py` (basic limiting, warn zone + cooldown, user isolation, sliding window expiry через `time.sleep(1.1)`, edge cases — zero threshold / high threshold / unknown user / reset). Полный pytest — **121 тест** (~14 сек). | [services.py](services.py), [bot.py](bot.py), [tests/test_rate_limiter.py](tests/test_rate_limiter.py), [.github/workflows/security.yml](.github/workflows/security.yml) (новый), [scripts/backup_offsite.sh.example](scripts/backup_offsite.sh.example) (новый), [README.md](README.md) |
| extra | PA-аналитика для портфолио | **Контекст:** проект используется как booster резюме для роли product analyst intern/junior. Цель — собирать реальные данные и иметь инструменты для PA-анализа. Новый класс `services.AnalyticsService` — pure SQL-агрегаты над существующими таблицами, никаких новых схем. 5 ship'нутых команд: **`/cohort_stats`** (D1/D7/D30 retention по ISO-неделям; strict definition «активен ровно в день signup+N»; UNION 6 источников активности — study_sessions, user_subject_stats, quiz_progress, flashcard_progress, mcq_progress, task_progress; eligibility filter — пользователи моложе N дней не входят в знаменатель D_N). **`/funnel`** — 6 шагов activation (registered → 1+ session → 5+ sessions → 10+ sessions → 3-day streak achievement → 7-day streak); % считается от total registered, не от prev step — шаги не обязательно strict-subsets (3-day streak ≠ subset 10+ sessions). ASCII bars `█░`. **`/dau`** — DAU / WAU / MAU + new users today + stickiness (DAU/MAU; ≥20% — типичный benchmark consumer apps). Использует `datetime.now().date()` вместо SQL `date('now')` для consistency с другими timestamp-операциями. **`/feature_usage`** — 8 фич: 4 учебных режима (situational/flashcards/MCQ/tasks; COUNT DISTINCT user_id из каждой progress-таблицы), Pomodoro (study_sessions), изменён ли часовой пояс, отключены ли какие-то уведомления, изменено ли время напоминаний. **`/export <alias>`** — CSV-дамп таблицы как Telegram document через `BufferedInputFile`. 9 алиасов whitelisted (users/sessions/achievements/quiz/flashcards/mcq/tasks/subject_stats/settings) — защита от SQL injection в table_name (SQLite не параметризует имена таблиц). Использует Python `csv.writer` в `io.StringIO` → UTF-8 bytes + row count. **`/analytics`** — единый dashboard с inline-меню: главный экран показывает компактную сводку (Today / Всего / DAU / Stickiness) + 6 кнопок (5 разделов + закрыть). Тап раздела → message edit к подробному view + `[◀️ К аналитике]`. Export submenu — 9 таблиц 2-в-ряд + back; тап на таблицу шлёт CSV отдельным сообщением (не edit'ом — чтобы можно было скачать несколько). Close → `message.delete()` с fallback на edit. Все callbacks admin-gated через `_anlt_check_admin`. **Pytest**: 29 тестов в `tests/test_analytics_service.py` — empty DB, single user too young, retention math, eligibility filter, funnel achievement counts, engagement edge cases (multi-user stickiness 1/3), feature_usage, export unknown alias raises, export CSV header/row count. **TODO.md**: добавлена top-level секция «PA-аналитика (data collection для портфолио)» с ship'нутыми командами + 5 backlog-идеями (segments, content_stats, event_timeline, parse_logs, heatmap; Could-приоритет) + план внешнего Jupyter-анализа после 30+ дней данных. Полный suite — **106 тестов** (~10 сек). | [services.py](services.py), [bot.py](bot.py), [tests/test_analytics_service.py](tests/test_analytics_service.py), [TODO.md](TODO.md), [admin_commands.md](admin_commands.md), [README.md](README.md) |
| extra | FAQ v2: миссия + гарантия | 2 новых вопроса добавлены в FAQ (всего теперь 9 + кнопка техподдержки = 10 inline-кнопок). **1️⃣ Миссия проекта** в самом начале — артикулирует «почему этот проект существует»: заменяем унылую зубрёжку геймификацией + научно-проверенными техниками памяти (spaced repetition / SM-2 / active recall / Pomodoro); 4 буллета value-props (маленькие победы / instant feedback против прокрастинации / привязка к питомцу / устойчивая привычка без силы воли); финальный hook «учишься эффективнее, и тебе это в кайф». **9️⃣ Гарантия эффективности** последним — честный ответ про «нет 100% гарантии и это нормально»: методики опираются на исследования (Эббингауз 1885 → современные мета-анализы), но реальный результат зависит от регулярности занятий, качества контента, обстоятельств (стресс/сон), стартовой точки. Все существующие вопросы переномерованы 2️⃣ через 8️⃣ — ids unchanged (efficiency/pet/spend_coins/earn_coins/sm2/spaced_rep/active_recall) для совместимости deep links если когда-то появятся. Все 9 кнопок ≤59 байт; самый длинный ответ — 832 символа (guarantee), под лимитом 4096. | [bot.py](bot.py) |
| extra | Daily backup БД | Новый `services.BackupService` делает snapshot БД через **SQLite `VACUUM INTO`** — атомарно, без WAL-мусора, в отдельном коннекте чтобы не вмешиваться в транзакции основного приложения. Имя daily-файла `studybuddy-YYYY-MM-DD.db`. Hook в `tasks.streak_scheduler`: после каждого `process_users_in_timezone` вызывается `maybe_backup_for_today()` — но фактический backup создаётся **только один раз за глобальный день** (dedup по server-local date через `_last_backup_date` + file-existence check на случай рестарта бота между TZ-tick'ами). Retention: 30 дней (env `BACKUP_RETENTION_DAYS`); cleanup удаляет `studybuddy-YYYY-MM-DD.db` старше retention'а **но не трогает** `studybuddy-manual-*.db` — manual snapshot'ы intentional, хранятся до явного удаления. Manual-режим: `force_backup()` создаёт файл с timestamp до секунд (`studybuddy-manual-YYYY-MM-DD-HHMMSS.db`), вызывается админ-командой `/backup` (только главный админ). Лог-события: `backup.created path=X size=N duration_ms=M`, `backup.cleanup removed=N retention_days=R`, `backup.failed reason=Y detail=Z`. Конфиг через env: `BACKUP_DIR` (default `backups`, в Docker `/data/backups` через compose), `BACKUP_RETENTION_DAYS` (default 30). `.env.example` заодно почищен от legacy Google Sheets credentials и переписан с описанием всех env-vars. Pytest: 12 новых тестов в `tests/test_backup_service.py` (создание файла, dedup в текущей сессии и после рестарта, force-backup не коллидирует на повторе, валидность backup как SQLite-файла, retention cleanup, manual-файлы выживают cleanup, malformed-имена не падают, dir создаётся при отсутствии, corrupt src БД ловится в try/except). Полный suite — **77 тестов** (~8 сек). | [services.py](services.py), [tasks.py](tasks.py), [bot.py](bot.py), [.env.example](.env.example), [.gitignore](.gitignore), [.dockerignore](.dockerignore), [docker-compose.yml](docker-compose.yml), [tests/test_backup_service.py](tests/test_backup_service.py), [admin_commands.md](admin_commands.md), [README.md](README.md) |
| extra | Система трекинга прогресса | Новый экран **📊 Прогресс по предметам** доступный из профиля (3-я inline-кнопка отдельной строкой; `back_to_profile` тоже обновлён). Экран: шапка с общими цифрами (монеты · мин учёбы · стрик), далее по каждому предмету — 10-квадратный mastery-bar 🟩⬜ (`PROGRESS_BAR_LENGTH=10`, helper `_render_bar(pct)`) + 3 actionable-строки: «🔔 К повторению сегодня» (overdue из quiz_progress + flashcard_progress, только `next_review ≤ now`), «🕐 Активность» (humanized: «сегодня в HH:MM» / «вчера» / «N дн. назад» через `_humanize_when`), «📈 Заходов». Пустые предметы (нет контента ни в одном из 4 режимов) — с пометкой «🚧 Контент в разработке». **Mastery-формула** агрегирует все 4 режима: situational `streak ≥ 3` (порог = достигнут 7-дневный интервал [1,2,4,7]), flashcards `repetitions ≥ 3` (3 успешных SM-2 ревью), MCQ `correct_count ≥ 1` (хоть раз верно), tasks `succeeded = 1` (любой попыткой). Bar % = total_mastered / total_items по всем режимам. **3 новых таблицы**: `mcq_progress(user_id, question_hash, correct_count, total_count, last_attempt)` — accumulates на каждом MCQ-ответе через UPSERT; `task_progress(user_id, task_id, attempts_used, succeeded, last_attempt)` — UPSERT с `MIN(attempts_used)` и `MAX(succeeded)` для идемпотентного «лучшего» результата; `user_subject_stats(user_id, subject_id, visits, last_activity)` — bumpается из 4 точек старта режимов. **3 новых репозитория**: `McqProgressRepository.record_attempt + count_mastered`, `TaskProgressRepository.record_attempt + count_mastered`, `SubjectStatsRepository.bump_visit + get`. **Hook-точки**: `start_mcq_session` / `start_flashcard_session` / `start_task_session` / `handle_quiz_section` → `bump_visit`; `handle_mcq_callback` → `mcq_repo.record_attempt`; `handle_task_answer` → `task_repo.record_attempt` (в обеих ветках: верный ответ и 3-я неверная попытка). UI: callback `show_progress:{user_id}` с анти-spoof проверкой (показывает только свой прогресс); `back_to_profile` обновлён чтобы включить новую кнопку — иначе из progress-экрана пользователь возвращался в старый профиль без неё. Smoke-test: 13 групп assertions (bar для 6 pct-значений, _mcq_hash детерминизм, _humanize_when для today/yesterday/weeks/None/invalid, async DB roundtrip с bump_visit + record_attempt accumulation + idempotency + isolation между пользователями + empty subject placeholder). Pytest: `tests/test_progress_repos.py` с 16 тестами (MCQ counter accumulation, task best-attempts при повторе, subject isolation). Полный pytest suite вырос с 49 до **65 тестов** (~5.7 сек). Pet emotion derive — пока не сделан; добавляем в #16. | [db.py](db.py), [repository.py](repository.py), [bot.py](bot.py), [tests/test_progress_repos.py](tests/test_progress_repos.py), [README.md](README.md) |
| backlog #4 | Dockerfile + pytest-тесты | **Docker**: Python 3.12-slim база, non-root user `app`, `/data` директория для persistent state. Cmd: `docker compose up -d --build`. Конфиг compose монтирует `./data:/data` на хосте, переопределяет `DB_PATH=/data/studybuddy.db` и `LOG_FILE=/data/bot.log` так что SQLite-БД + logs живут в volume и переживают rebuild. `.dockerignore` исключает .env, БД, логи, тесты, docs, archive/ из образа. Env_file: `.env` со стороны хоста; log-driver json-file с rotation 10MB×3. **Тесты**: pytest + pytest-asyncio в `requirements-dev.txt`; `pytest.ini` с `asyncio_mode=auto` и `testpaths=tests`. Файл `tests/conftest.py` даёт fixtures: `db` (свежая tempfile SQLite per test), `user_repo`, `session_repo`, `created_user`, `achievements_catalog` (загружает реальный achievements.json — гарантирует что тесты не дрейфуют от продакшна). **49 тестов** в трёх файлах: `test_sm2.py` (24: стандартные переходы q=5, fail-path q<3, EF floor поведение, parametrized property tests для каждого quality 0-5), `test_achievement_service.py` (14: каждая из 9 ачивок отдельно + не-выдача при недостатке + многократная выдача за один вызов + идемпотентность), `test_streak_service.py` (11: инкремент при has_studied=1, сброс при =0, флаг очищается, бонусные +15🪙 со 2-го дня но не с 1-го, multi-user isolation). Bot для StreakService мокируется через `unittest.mock.AsyncMock`. **Bonus в коде**: `bot.py` теперь читает `LOG_FILE` env var (default `"bot.log"`) — на хосте остаётся прежнее поведение, в Docker логи пишутся в `/data/bot.log`. Запуск: `pip install -r requirements-dev.txt && pytest -v` → **all 49 pass in 8.35s**. Закрывает [TODO #4]. | [Dockerfile](Dockerfile) (новый), [docker-compose.yml](docker-compose.yml) (новый), [.dockerignore](.dockerignore) (новый), [requirements-dev.txt](requirements-dev.txt) (новый), [pytest.ini](pytest.ini) (новый), `tests/` (4 файла), [bot.py](bot.py), [.gitignore](.gitignore), [README.md](README.md) |
| 1b | SM-2 flashcards | Чистая функция `sm2_update(quality, reps, ef, interval) → (new_interval, new_reps, new_ef)` в `services.py` — каноническая формула SuperMemo-2 (q<3 сбрасывает reps в 0 и interval=1; q≥3 — reps++, interval по ступеням 1/6/round(prev·EF)). EF корректируется по формуле `EF + 0.1 − (5−q) · (0.08 + (5−q) · 0.02)` с полом `EF_FLOOR=1.3`. Маппинг 3-кнопочного UI: «❌ Не знал»→q=1, «😐 Сложно»→q=3, «✅ Легко»→q=5 (константа `FLASH_QUALITY_BY_LABEL`). Новая таблица `flashcard_progress(user_id, card_hash, ease_factor REAL DEFAULT 2.5, interval_days INTEGER DEFAULT 0, repetitions INTEGER DEFAULT 0, last_review, next_review, PRIMARY KEY)` + индекс `(user_id, next_review)`. Новый `FlashcardRepository` (get_progress / upsert_progress / get_next_card_hash). `get_next_card_hash` приоритизирует overdue по `next_review ASC`, потом новые карты в порядке файла. UI flow: бот шлёт термин + inline «💡 Показать ответ»; тап → edit_text открывает определение и 3-кнопочный рейтинг; тап рейтинга → `sm2_update` → upsert + +1🪙 + следующая карта. **+1🪙 за карточку независимо от рейтинга** — поощряем честную самооценку, иначе пользователь спамил бы «✅ Легко» ради монет. `load_flashcards(subject_id)` парсит `flashcards.txt` (формат «термин ‖ определение»); `_flashcard_hash` — md5[:8] (тот же паттерн что у QuizTerm.hash). `quiz_progress` для ситуационных квизов не трогается — keyword-grader там бинарный, для SM-2 нужен градиент 0-5. 5 примеров карточек в `industrial-management/flashcards.txt`. Smoke-test: **7 SM-2 unit cases** (1-я/2-я/3-я успешная q=5, hard q=3, fail q=1, EF floor с q=1 на низком EF, q=4 не меняет EF) + bot wiring + load_flashcards + FlashcardRepository async round-trip с временной БД (overdue precedence, новая карта селекция, unknown user). Закрывает [TODO #15]; для ситуационных квизов SM-2 остаётся Won't (бэклог #5). | [services.py](services.py), [db.py](db.py), [repository.py](repository.py), [bot.py](bot.py), `study_materials/industrial-management/flashcards.txt` (5 карточек) |
| 1d | Photo-task режим | `load_tasks(subject_id)` сканирует `study_materials/<subject>/tasks/task-*.json` — для каждого требует наличия `task-NN.png` и непустого `accepted[]` (иначе warning + skip). JSON-поля: `problem` (текстовая подпись), `accepted` (список принимаемых вариантов), `solution_image` (имя файла, дефолт `<id>-solution.png`). `_normalize_task_answer(text)` — lowercase + strip-punct + collapse-whitespace; ответ матчится через set-inclusion в нормализованном пространстве. Flow: бот шлёт `task-NN.png` через `FSInputFile` + caption с problem + «✏️ Введи ответ:»; пользователь отвечает текстом. Награды: +3🪙 (1-я попытка) / +2🪙 (2-я) / +1🪙 (3-я) / 0🪙 (показ solution). Константы `TASK_REWARDS_BY_ATTEMPT = [3, 2, 1]` и `MAX_TASK_ATTEMPTS = 3` вынесены модульно. Счётчик попыток `task_attempts` в FSM data (без новой таблицы — как требовалось в брифе). После 3-й неверной — `bot.send_photo(solution_path)` + caption с правильным ответом; если solution-файла нет — текстовое сообщение с правильным ответом. Конец сессии: лог `task.session.complete user_id=X subject=Y correct=A total=B coins=C` + клавиатура study-menu. Stub-ветка для tasks в `handle_mode_picked` удалена (полный flow готов). Stub для flashcards остался (ждём #15). **Pre-existing fix**: `stop_active_timer` теперь проверяет `state.get_state() == TimerStates.active.state` перед `state.clear()` — до этого `/stop` во время MCQ/photo-task сессии чистил их FSM-state. Placeholder content: `industrial-management/tasks/task-01.{png,solution.png,json}` — 400×250 PNG через .NET System.Drawing (PowerShell, без Pillow) + JSON с реальным текстовым вопросом по терминам из section-i. Импорт `FSInputFile` из `aiogram.types`. Smoke-test: 10 групп assertions (helpers, FSM state, нормализация, load_tasks с 1 task, фильтр math/english пустых, mode discovery с 'tasks', subjects_with_mode, answer matching по нескольким вариантам, backwards compat 15+3). Закрывает [TODO #14]. | [bot.py](bot.py), `study_materials/industrial-management/tasks/` (3 файла) |
| 1c | MCQ-режим | Полный mode-picker flow: `❓ Квизы` → `get_mode_keyboard` (4 режима, фильтр по `available_modes_global`) → `get_subject_keyboard_for_mode(mode_id)` (фильтр по `subjects_with_mode(mode_id)`) → ветвление: situational сохраняет существующий section→answer flow без изменений (через дефолтный `subject_id="industrial-management"`); MCQ запускает новую сессию. Новые `QuizStates.choosing_mode`/`choosing_subject`/`answering_mcq` + удалён старый `handle_production_management`. MCQ-сессия: `load_mcq(subject_id)` парсит `mcq.txt` (формат `вопрос ‖ правильный ‖ w1 ‖ w2 ‖ w3`, # — комментарии, малформ-строки пропускаются); `random.shuffle` вопросов; 4 inline-кнопки с `callback_data="mcq:{idx}"` (idx — позиция в перетасованном списке), правильный idx хранится в FSM `mcq_current_correct_idx`. Callback handler редактирует сообщение (убирает inline-клавиатуру + дописывает ✅/❌ feedback), что блокирует повторный тап; правильный ответ → `user_repo.add_coins(+1)` + `mcq_correct_count++`; неверный → показ правильного. После всех вопросов — `_finish_mcq_session` с лог-строкой `mcq.session.complete user_id=X subject=Y correct=A total=B coins=A` + клавиатура study menu. Reply-клавиатура во время сессии — `get_mcq_active_keyboard()` (только «🛑 Завершить»). Stub-ветки для `flashcards`/`tasks` в `handle_mode_picked` — на случай если #15/#14 ещё не пришли, а контент уже добавлен (защитное программирование). 3 примера MCQ-вопросов добавлены в `industrial-management/mcq.txt` для немедленной проверки. Smoke-test: 10 групп assertions (helpers, удалённые артефакты, FSM-states, обнаружение контента, парсинг mcq.txt, обратная совместимость section-i 15 терминов). Закрывает [TODO #13]. | [bot.py](bot.py), `study_materials/industrial-management/mcq.txt` (создан) |

### Bugs caught

- **`stop_active_timer` чистил чужой FSM-state.** Pre-existing bug: при отсутствии `start_time` в FSM data функция всегда вызывала `state.clear()`, не проверяя в каком state сейчас пользователь. Это значило, что `/stop` во время MCQ или photo-task сессии стирал её прогресс. Поймано во время дизайна #14 (защитное мышление о command-flow); пофикшено там же двухстрочной проверкой `current_state == TimerStates.active.state` перед `state.clear()`. Затрагивало #13 (MCQ) тоже задним числом — теперь починено для всех режимов.
- _Других багов в этой сессии не было — все остальные правки прошли как чистые edits + smoke-тесты_

### Files modified

**Код:**
- [bot.py](bot.py) — крупный набор изменений (см. таблицу Changes выше): импорты, FSM-states, mode picker, 4 учебных режима, MCQ/photo-task/flashcards handlers, миграция админов, резюм таймера, фикс stop_active_timer
- [db.py](db.py) — таблица `flashcard_progress` + индекс `(user_id, next_review)`
- [repository.py](repository.py) — новые классы `AdminRepository`, `FlashcardRepository`
- [services.py](services.py) — чистая функция `sm2_update()` + константа `EF_FLOOR=1.3`

**Контент:**
- `study_materials/` (создана) ← `quizzes/` (удалена); структура `{industrial-management,math,english}/{situational,flashcards.txt,mcq.txt,tasks/}`
- `study_materials/industrial-management/mcq.txt` — 3 примера MCQ
- `study_materials/industrial-management/flashcards.txt` — 5 примеров карточек
- `study_materials/industrial-management/tasks/task-01.{png,-solution.png,json}` — placeholder для photo-task

**Инфраструктура / docs:**
- [.gitignore](.gitignore) — добавлены `admins.json.migrated*`, `messages.log`, `.claude/`, `.git_commit_msg*`, `_smoke_*.py`, `bot.log.*`
- [admin_commands.md](admin_commands.md) — обновлена секция про управление админами; добавлены `/addadmin`/`/rmadmin`/`/listadmins`
- [TODO.md](TODO.md) — закрыты пп. 7, 8, 12, 13, 14, 15; путь в п. 1 обновлён; п. 5 помечен что флэш-карты сделаны
- [README.md](README.md) (создан) — для GitHub-репозитория
- [BACKLOG.md](BACKLOG.md) (создан) — applied-math идея отложена в v0.8
- [requirements.txt](requirements.txt) (создан) — aiogram, aiosqlite, pytz, python-dotenv

**Git/GitHub:**
- 6 коммитов: initial → #12 → #13 → #14 (+ cleanup) → #15
- Запушено в `zerocious/Palph` (PRIVATE)

### Verification

**Автоматическая (4 smoke-теста + финальная сессионная проверка):**

| Скрипт | Кол-во групп assertions | Что покрывает |
|---|---|---|
| `_smoke_item12.py` | 8 | study_materials/ структура, helpers, фильтр math/english пустых, section-i не потерян |
| `_smoke_item13.py` | 10 | MCQ helpers/state/handlers, парсинг mcq.txt, mode picker visibility |
| `_smoke_item14.py` | 10 | load_tasks, нормализация ответа, +3/+2/+1/0 константа, backwards compat |
| `_smoke_item15.py` | 7 SM-2 unit + 6 module + 5 async DB | SM-2 алгоритм (стандартные переходы, EF floor, q=4 no-change), wiring, FlashcardRepository roundtrip |
| `_verify_session.py` | 9 групп (A–I) | Всё сразу: load, quick fixes, refactors, #12-15, full DB roundtrip — **прошёл 100%** |

Все smoke-скрипты удаляются после прохождения (через `.gitignore` паттерн `_smoke_*.py`). Финальная верификация подтвердила: bot.py загружается, все таблицы создаются (9 штук), helpers/states/handlers на месте, SM-2 алгоритм соответствует canonical SuperMemo-2 формуле.

**Статическая:** грепы подтверждают отсутствие лишних ссылок (`Техподдержка`/`handle_support`/`QUIZZES_PATH`/`get_quiz_keyboard`/`handle_production_management`); `python -c "import ast; ast.parse(...)"` подтверждает синтаксис всех модифицированных файлов.
**Live walkthrough — статус по пунктам:**

| Пункт | Live-подтверждено пользователем? |
|---|---|
| #9 menu 2×2 + #10 news link + #11 FAQ + #17 /help | ✅ да |
| #7 admins.json → БД + новые команды | ✅ да («все команды с администрированием работают») |
| #8 timer resume | ✅ да («продолжение тоже работает: ♻️ Осталось 4 мин») |
| #12 study_materials | косвенно (квиз ОПМ работает в новом расположении) |
| #13 MCQ / #14 photo tasks / #15 SM-2 flashcards | ⬜ ещё нужно |

**Полный план live-walkthrough** (✅ = подтверждено пользователем; ⬜ = осталось):

  1. ✅ `/start` → главное меню это сетка 2×2 без кнопки техподдержки
  2. ✅ 📢 Новости → inline-кнопка открывает `t.me/palph_study`
  3. ✅ ❓ FAQ → 7 вопросов, в конце «Остались вопросы? Напиши @zerocious»
  4. ✅ `/help` от админа → полный список; от не-админа → перенаправление на FAQ
  5. ✅ `/-`пикер в Telegram: админ видит `/help` + админские; обычный пользователь — только `/start`/`/stop`
  6. ✅ Миграция админов: `admins.migration_done imported=3 archived=admins.json.migrated` в логе; `admins.json` исчез, есть `admins.json.migrated`; БД содержит 3 строки в таблице `admins`
  7. ✅ Управление админами: `/addadmin`/`/rmadmin`/`/listadmins` все работают; защита от удаления главного админа; не-главный админ не может добавлять
  8. ✅ Резюм таймера: после Ctrl+C → рестарт → лог `reconcile.resume` + сообщение «♻️ Бот перезапустился, осталось N мин» → естественное завершение
  9. ⬜ **MCQ flow (#13):** `❓ Квизы` → 4 режима в picker → `❓ Тест с выбором` → 🏭 ОПМ → 3 вопроса с перетасовкой; ✅/❌ feedback в той же карточке (кнопки убираются); +1🪙 за правильный; финальный summary
  10. ⬜ **Photo task (#14):** 📷 Задачи → 🏭 ОПМ → placeholder картинка с текстом + «✏️ Введи ответ»; 1-я неверная → «осталось 2»; 2-я неверная → «осталось 1»; 3-я неверная → solution image + 0🪙; либо 1/2/3 верных → +3/+2/+1 🪙; нормализация (Фотография / ФОТОГРАФИЯ!!! / фотография рабочего времени — все принимаются)
  11. ⬜ **SM-2 flashcards (#15):** 🃏 Флэш-карты → 🏭 ОПМ → 5 карточек с показом термина → «💡 Показать ответ» → 3 кнопки рейтинга → feedback с интервалом; после сессии: `sqlite3 studybuddy.db "SELECT * FROM flashcard_progress"` показывает 5 строк с разными ease_factor / interval_days / next_review
  12. ⬜ **Bonus fix:** в активной MCQ/task/flash сессии отправить `/stop` → ответ «Сейчас нет активного таймера», но сессия НЕ должна сброситься (продолжаешь отвечать)

### Deferred

**В рамках v0.7 (осталось доделать):**
- 🐾 **Item #16 — полноценный питомец** (1 дизайн + 5 derived эмоций + hybrid customization + 2 таблицы + 4-state picker + build-script для 125 PNG + 5 GIF). Самая большая фича сессии; разбита на код-трек (~120 мин) и арт-трек (отдельно). Закрывает TODO #2 (sad pet on no session today) автоматически через `derive_emotion()`.

**За пределы v0.7:**
- Real-time wall clock during timer (item 6 из исходного плана) — deferred
- Personal task manager (item 6) — deferred
- Pet hunger/happiness/energy decay loop — не в этой версии
- SM-2 для **ситуационных** квизов (TODO #5) — Won't, нужен лучший grader сначала
- Контент для math/English (flashcards/mcq/tasks файлы пусты — пользователь заполняет когда готов)
- Финальная редакция FAQ wording — placeholder-текст из этой сессии; пользователь может переписать
- Applied-математика (контекстуализированные задачи) — defer до v0.8 после анализа engagement в #14, см. [BACKLOG.md](BACKLOG.md)

**Live walkthrough для #13/#14/#15** — пользователь подтвердил quick fixes + админ-команды + резюм таймера; для трёх новых учебных режимов live-проверка ещё впереди (см. Verification → план).

---

## Session — 2026-05-16

Goal: take the bot from "won't even start" to deploy-ready MVP. Triaged
critical issues (12 fixes), reconciled the product brief against the code
(audit), closed the four user-facing MVP gaps, then layered in five more
features the audit had flagged: `/broadcast`, timezone selection,
persistent FSM, emoji session rating, structured logging with rotation.
Cleaned up the legacy monolith (`telegabot.py` archived). Ended with a
live walkthrough that confirmed everything works in real Telegram.

### Part 4 — Late-session additions

| Area | Change | Files |
|------|--------|-------|
| Live-test bug | `"📚 Учеба"` button on the main keyboard had **no handler** — every press fell through to `handle_any_message` and was forwarded to admins as a support message. Added it to the existing `handle_back_to_study` filter via `F.text.in_(["📚 Учеба", "⬅️ Назад к учебе"])`. Verified all other main-menu / sub-menu buttons have matching handlers. | [bot.py](bot.py) |
| Feature | `/broadcast <текст>` admin command. Sends a message to every `user_id` in the `users` table, with per-user `try/except`, `0.05s` throttle, double-launch guard via boolean flag, and a final report (delivered / failed / first 10 failed IDs). New `UserRepository.get_all_user_ids()` to avoid loading unused columns. | [repository.py](repository.py), [bot.py](bot.py) |
| Feature (TODO #5) | **Timezone selection in settings.** 12 preset RU/CIS timezones (Kaliningrad → Kamchatka + Kyiv). New 🌍 button in ⚙️ Настройки opens an inline picker; on tap, saves `users.timezone` and returns to the settings view, which now displays the chosen TZ instead of a hardcoded "Москва (UTC+3)". Both schedulers refactored: each minute they iterate `get_distinct_timezones()`, compute local `HH:MM` per TZ, and call `tick(tz, hhmm)` / `process_users_in_timezone(tz)`. Reminder + streak queries gained a `timezone` filter so cross-TZ users don't bleed into each other's lists. Backward compat: `StreakService.process_all_users` preserved for ad-hoc full sweeps. Smoke-tested end-to-end (14 assertions across DB, repo, service layers). | [bot.py](bot.py), [services.py](services.py), [repository.py](repository.py), [tasks.py](tasks.py) |
| Refactor (TODO #8) | **Persistent FSM storage.** Replaced `MemoryStorage()` with a new `SQLiteStorage` (in `fsm_storage.py`) implementing aiogram's `BaseStorage`. Stores `(key, state, data)` rows in a new `fsm_storage` table; key is `"{bot_id}:{chat_id}:{user_id}:{thread_id}"`. JSON serialization uses a custom encoder/decoder so `datetime` values (timer `start_time`) survive a round-trip. `update_data` is atomic under the existing `db.lock`. **Bonus**: new `reconcile_stale_timers()` runs at startup — for any `TimerStates.active` row, auto-completes timers whose deadline passed during downtime (notifies user + awards coins) and clears the rest so users can't exploit the new persistence by waiting hours then pressing Stop. Smoke-tested (18 assertions: round-trip, datetime, update merge, key isolation, persistence across new storage instance, clear). | [fsm_storage.py](fsm_storage.py) (new), [db.py](db.py), [bot.py](bot.py) |
| Feature (TODO #1) | **Emoji rating after each session.** After timer completion (natural or `/stop`) the bot sends a follow-up message "Как прошла сессия?" with a 4-button inline keyboard (😞 😐 🙂 😍) plus "⏭ Пропустить". On tap, the score (1–4) is saved into a new `study_sessions.score` column. The `reconcile_stale_timers` path explicitly skips the prompt — user wasn't actively studying. Other changes: `SessionRepository.add_session` now returns `lastrowid`; new `set_session_score(session_id, user_id, score)` with a `user_id` filter to prevent spoofed `callback_data`; `StudyService.complete_session` returns `(earned, bonus, session_id)`. Schema migration is idempotent (`ALTER TABLE ADD COLUMN` wrapped in try/except). Smoke-tested (16 assertions: schema, idempotent re-init, lastrowid, anti-spoof, keyboard shape, 64-byte callback_data limit). | [bot.py](bot.py), [services.py](services.py), [repository.py](repository.py), [db.py](db.py) |
| Diagnostic | **`/notif_status` admin command** added to investigate a user-reported "reminders don't fire" bug. Shows user's `timezone`, current local time, current Moscow time, `morning_enabled` + `morning_time`, `evening_enabled` + `evening_time`, the list of timezones the scheduler is iterating, and — crucially — runs the actual `get_users_due_for_*` query and reports whether the user themselves would be in the result. Surfaces a clear warning when `has_studied_today=1` (which legitimately suppresses evening reminders). Also added a heartbeat line every 10 minutes in `reminder_scheduler` so a silent crash would be visible. | [bot.py](bot.py), [tasks.py](tasks.py) |
| Live walkthrough (TODO #1) | **MVP deploy-ready.** User drove the full bot end-to-end in real Telegram after all the session's fixes. All five scenarios pass: onboarding wizard with `/skip`, timer + `/stop` with rating prompt, quiz with right/wrong answers, time editing in ⚙️ Настройки, real reminder dispatch at configured time. The one "didn't fire" report turned out to be designed behavior — user had `has_studied_today=1` so the evening reminder was correctly suppressed (matches the brief's "evening reminder if no session yet" promise). The `/notif_status` diagnostic explicitly surfaces this case. No new bugs found in the walkthrough. | (no code changes) |
| Infra (TODO #5 partial) | **Structured logging + rotation.** `setup_logger()` now uses `RotatingFileHandler` (5MB × 5 backups = 25MB cap, replaces unbounded `FileHandler`). Log level read from `LOG_LEVEL` env (default INFO). Noisy third-party loggers — `aiogram`, `aiogram.event`, `aiohttp.access`, `aiosqlite` — silenced to WARNING so business events stay visible. Added consistent `event.tag key=value` business event lines: `app.start`, `app.shutdown`, `user.registered`, `session.complete` (sources: natural / stop / reconcile), `session.rated`, `broadcast.start`, `broadcast.done`, `reconcile.summary`, `streak.batch`, `reminder.morning.dispatched`, `reminder.evening.dispatched`. `TelegramForbiddenError` (user blocked the bot) now logged at INFO with `reason=blocked` — distinct from real send failures which go WARNING/ERROR with the exception class name. See **Logging convention** section below for the format. | [bot.py](bot.py), [services.py](services.py) |
| Cleanup | **Archived the legacy monolith.** `telegabot.py` (74690 bytes, the pre-refactor Google-Sheets-era version that still tried to `from sheets import …` — a module deleted long ago and uncallable in the current project) moved to `archive/telegabot.py.legacy`. Removed stale `__pycache__/sheets.cpython-314.pyc` for the same deleted module. Project root now only contains the active modular code (`bot.py`, `db.py`, `repository.py`, `services.py`, `tasks.py`, `fsm_storage.py`). | `telegabot.py` → `archive/`, `__pycache__/` |

### Part 1 — Critical fixes (correctness, stability, security)

| # | Area | Change | Files |
|---|------|--------|-------|
| 1 | Boot blocker | Rewrote `tasks.py` — file had no valid Python syntax (missing colons, quotes, docstring markers); bot crashed on import. | [tasks.py](tasks.py) |
| 2 | Boot blocker | Added missing `from dotenv import load_dotenv` import. | [db.py](db.py) |
| 3 | Boot blocker | Moved `from repository import …` to top of file; `UserRepository` was used as a type hint before its import. | [services.py](services.py) |
| 4 | Cleanup | Removed three duplicate method definitions in `UserRepository` (`get_users_for_streak_update`, `apply_streak_increment`, `apply_streak_reset`). | [repository.py](repository.py) |
| 5 | Concurrency | Attached an `asyncio.Lock` to the aiosqlite connection; wrapped the three read-modify-write hotspots (`complete_session`, streak increment/reset, settings toggle). Prevents lost updates when two coroutines act on the same user. | [db.py](db.py), [services.py](services.py), [bot.py](bot.py) |
| 6 | Memory / cancellation | Timer tasks were created with `asyncio.create_task(...)` and the reference discarded → potential GC + no way to cancel. Introduced `active_timers` registry + `start_timer()` / `stop_active_timer()` helpers. Stop button now properly cancels the background task. | [bot.py](bot.py) |
| 7 | Stability | Replaced `messages.json` (rewrite-on-every-message) with append-only `messages.log` (JSONL). No more read-modify-write races, no corruption on Ctrl-C, no O(n) growth cost per message. Removed `load_messages()` and the `messages_data` global. | [bot.py](bot.py) |
| 8 | Security | Created `.gitignore` (excludes `.env`, `credentials.json`, `*.db`, logs) + `.env.example` template. Confirmed no `.git` directory exists yet — secrets never committed. | `.gitignore`, `.env.example` |
| 9 | Quiz logic | **Two bugs.** (a) `get_next_quiz_term` fell back to `random.choice(all_terms)` after all terms were reviewed → "🎉 Все термины раздела повторены" message could never fire. (b) Feedback message said `2**min(streak,3)` days (= 2/4/8/8) but storage used `[1,2,4,7]` — mismatch from streak ≥ 3. Extracted `quiz_interval_days()` so feedback and storage share one source. | [bot.py](bot.py) |
| 10 | UX | Pressing "Назад в меню" during an active timer left the user without a Stop button. Added a `/stop` command that works from any state. Extracted stop-timer logic into `stop_active_timer()` helper. | [bot.py](bot.py) |
| 11 | Quiz path | `QUIZZES_PATH = Path("quizzes/...")` was relative to cwd. Switched to `Path(__file__).parent / ...` so it works under systemd/Docker. | [bot.py](bot.py) |
| 12 | Quiz matching | Substring check `kw.lower() in uw` matched `"ыр"` inside `"сырьё"`. Replaced with `_word_matches_keyword`: exact match for keywords ≤3 chars, prefix-of-stem (first 5 chars) for longer keywords. Handles Russian declensions (`"системы"` matches `"системами"`). | [bot.py](bot.py) |

### Part 2 — Doc audit

Compared `Archives/Startup/StudyBuddy — Product Brief.md` and
`Archives/Startup/StudyBuddy – Контекст проекта.md` against the actual
codebase. Saved as plan file at
`C:\Users\User\.claude\plans\glowing-toasting-flurry.md`.

Findings:
- **12 features fully implemented** — core Pomodoro / coins / streak / achievement / quiz / profile loop.
- **5 partial or different from docs** — pet mood (streak only, not "no session today"), onboarding wizard (entry only, no body), only quiz Section I has content, spaced repetition is fixed intervals (not SM-2), timezone stored but unset-able.
- **6 missing entirely** — edit notification times, morning reminders, evening reminders, admin `/broadcast`, daily-tasks stub, `/help`.
- **Per-MD list of dropped issues** (admins.json reload, bare except in tip handlers, 3-commit non-atomic complete_session, state.clear() rare skip, process_all_users serial) — explicitly **dropped** as acceptable for an MVP at <100 users.

### Part 3 — MVP gap fixes

Plan: four user-facing critical-path gaps identified in the audit.
Ordered so each builds on the previous.

| Step | Feature | What was added |
|------|---------|----------------|
| 1 | Edit notification times in settings | New `SettingsStates.waiting_for_time` FSM state + `TIME_RE` regex + `NotificationSettings.set_time()`. Two new buttons (🕘 Изменить next to Утро/Вечер) in the settings inline keyboard. New `request_time_change` callback + `process_time_input` handler. `/cancel` aborts cleanly. |
| 2 | Onboarding wizard body | Wired up handlers for the previously dead-end `SetupStates`: `🔧 Настроить сейчас` → ask morning → ask evening → save. `🚀 Начать сразу` skips wizard. `/skip` skips a slot (disables that reminder). Extracted `_parse_time_or_none()` so step 1 and 2 share validation. |
| 3 | Morning + evening reminder scheduler | New `ReminderService` (in `services.py`) + `reminder_scheduler` (in `tasks.py`). Wakes once a minute, calls `tick("HH:MM")` aligned to second 0 to avoid drift. Two new repo methods: `get_users_due_for_morning` / `get_users_due_for_evening`. Evening only fires if user hasn't studied today (matches brief). Background tasks now kept in `background_tasks` list and cancelled on shutdown. |
| 4 | Hide empty quiz section buttons | Made the section keyboard data-driven via `QUIZ_SECTIONS` constant + `available_quiz_sections()` helper. Sections with missing or empty content files are no longer shown. Drop content into the `.txt` later and the buttons re-appear with no code change. |

### Bugs caught during this session

By smoke tests:

1. **Step 2 (onboarding)** — initial regex `^([01]?\d|2[0-3]):([0-5]\d)$` rejected `9:5`. Loosened minutes group to `[0-5]?\d` so the normalization (`9:5` → `09:05`) actually works.
2. **Step 3 (reminders)** — test assertion incorrectly excluded a user from the evening list after I'd reset `has_studied_today=0` for everyone. Test fixed; behaviour was correct.
3. **#9 (quiz dates)** — my original audit claim that `next_review` and `last_attempt` formats mismatched was wrong: `last_attempt` is stored but never compared. Owned the mistake; real bugs (random fallback + interval display) fixed.
4. **Logging smoke (Windows quirk)** — first test attempt flooded stdout with 35k log lines from the console handler. Second attempt failed with `WinError 32` because a stale file handle from the first failed run held `bot.log` open and `RotatingFileHandler` couldn't rename. Replaced with a non-flooding test that verifies config (handler type, params, third-party silencing, tag round-trip) without forcing actual rotation. Real rotation is stdlib code on parameters we set correctly — works fine on the eventual Linux deploy.

By the user during live walkthrough:

5. **`📚 Учеба` button on main keyboard had no handler** — every press was being forwarded to admins as a "support message" because it fell through to `handle_any_message`. Fixed by extending `handle_back_to_study`'s filter to `F.text.in_(["📚 Учеба", "⬅️ Назад к учебе"])`.
6. **"Reminders don't fire"** — turned out to be designed behavior. The user's `has_studied_today=1` legitimately suppressed the evening reminder (matches the brief's "evening reminder if no session yet"). Discovered by building the `/notif_status` diagnostic. Not a code bug, but a confusing UX surface — `/notif_status` now explicitly shows a warning line when `has_studied_today=1`.

### Files modified this session

- [bot.py](bot.py) — extensive: ~30+ edits across handlers, FSM states, helpers, logger
- [services.py](services.py) — `complete_session` signature change, `ReminderService`, streak refactor, logging
- [repository.py](repository.py) — many new query methods (TZ-aware reminders, distinct TZs, set_timezone, set_session_score, get_all_user_ids)
- [db.py](db.py) — `db.lock` attached, new `fsm_storage` table, `score` column on `study_sessions`
- [tasks.py](tasks.py) — full rewrite + TZ-aware refactor of both schedulers
- [fsm_storage.py](fsm_storage.py) — new module: `SQLiteStorage` for persistent FSM
- `.gitignore` (new)
- `.env.example` (new)
- [TODO.md](TODO.md) — items closed and renumbered as they shipped
- `telegabot.py` → `archive/telegabot.py.legacy` (moved)
- `__pycache__/sheets.cpython-314.pyc` (removed; stale bytecode for deleted module)

### Verification

Every fix had a temporary `_smoke.py` written, run, and deleted after
passing. **~120+ assertions across ~10 smoke runs** in this session
(initial 3 fixes, then per-feature: edit notification times, onboarding
wizard, reminder scheduler, hide empty quiz sections, timezone, persistent
FSM, session rating, logging). All green at end. Capped off with a real
Telegram walkthrough by the user — see entry for "Live walkthrough" in
Part 4.

### Live walkthrough recipe (kept for re-runs after future changes)

```
python bot.py
# 1. /start from fresh account → "🔧 Настроить сейчас" → enter 09:00 → enter 21:00 → confirm
# 2. 📊 Мой профиль → ⚙️ Настройки → 🕘 Изменить next to Утро → enter 10:30 → confirm
# 3. 📚 Учеба → ⏱️ Стандартный таймер (25 мин) → /stop → confirm partial-coin award + rating prompt
# 4. 📚 Учеба → ❓ Квизы → 🏭 Основы… → keyboard shows only "Раздел I" + stop
# 5. edit morning_time to a minute in the near future, wait, confirm message arrives
# 6. /notif_status (admin) → confirm timezone, enabled flags, "включая тебя — ✅ да"
```

### Dropped / out-of-scope for this session

Still-open after Part 4 (numbers match current [TODO.md](TODO.md)):

- Quiz content for Sections II–IV (TODO #1 — Should, content task not code)
- Pet mood depending on "no session today" (TODO #2 — Should)
- `/help` command (TODO #3 — Could)
- Dockerfile + pytest suite (TODO #4 — Could; log rotation already shipped)
- SM-2 algorithm replacement for fixed `[1,2,4,7]` intervals (TODO #5 — Won't)
- Daily-tasks stub (TODO #6 — Won't)
- `admins.json` → DB (TODO #7 — Won't)
- Live timer **resume** after restart (TODO #8 — Could; follow-up to persistent FSM)

Originally deferred at audit time but **landed later in Part 4**:
~~Persistent FSM storage~~, ~~Admin /broadcast~~, ~~Timezone editing UI~~,
~~Session rating emoji~~, ~~Live walkthrough~~, ~~Log rotation~~ — done.

---

## Logging convention

Established this session — applies to all new `logger.*` calls. Keep the
existing outer format (`asctime - name - levelname - message`). Inside
the message, use a short dotted **event tag** followed by **key=value**
fields, in this order: who, what, how-much.

### Examples
```
2026-05-16 18:30:00 - studybuddy_bot - INFO - session.complete user_id=42 duration=25 coins=25 bonus=0 session_id=17 achievements=1 source=natural
2026-05-16 23:59:00 - studybuddy_bot - INFO - streak.batch tz=Europe/Moscow users=84 incremented=37 reset=42 bonuses=555
2026-05-16 09:00:00 - studybuddy_bot - INFO - reminder.morning.dispatched tz=Europe/Moscow hhmm=09:00 count=12
2026-05-16 21:00:00 - studybuddy_bot - INFO  - reminder.send_failed kind=evening uid=42 reason=blocked
2026-05-16 21:00:00 - studybuddy_bot - WARNING - reminder.send_failed kind=evening uid=99 reason=TelegramRetryAfter detail=Flood control...
```

### Tag taxonomy

| Tag | Level | When |
|-----|-------|------|
| `app.start` / `app.shutdown` | INFO | once per process lifecycle in `main()` |
| `scheduler.started` / `scheduler.heartbeat` | INFO | `reminder_scheduler` and `streak_scheduler` |
| `user.registered` | INFO | first `/start` for a new user |
| `session.complete` | INFO | timer ran out, was stopped, or was reconciled (`source=natural|stop|reconcile`) |
| `session.rated` | INFO | user tapped a rating emoji |
| `streak.batch` | INFO | per-TZ summary at 23:59 |
| `reminder.morning.dispatched` / `reminder.evening.dispatched` | INFO | scheduler tick matched ≥1 user in that TZ |
| `reminder.send_failed` | INFO (blocked) / WARNING (other) | per-user send failure inside a batch |
| `broadcast.start` / `broadcast.done` | INFO | admin `/broadcast` |
| `broadcast.send_failed` | INFO (blocked) / WARNING (other) | per-user failure inside the broadcast loop |
| `reconcile.summary` | INFO | after `reconcile_stale_timers` finishes at boot |
| `reconcile.notify_failed` | INFO (blocked) / ERROR (other) | couldn't tell the user their offline-timer was completed |
| `streak.notify_failed` | WARNING | couldn't deliver streak-bonus notification |
| `fsm.broken_state` | WARNING | reconciler found a row it couldn't parse |
| `tz.unknown` | WARNING | scheduler saw a TZ name `pytz` doesn't recognize |
| `migration.applied` | INFO | schema migration ran for real (not a no-op) — future use |

### Style rules

- One **dotted tag** followed by **key=value** pairs separated by single
  spaces. No JSON, no f-strings inside the format — use `logger.info("tag k=%s", value)`
  so logging's lazy interpolation works.
- **No PII**: never log user-typed text (quiz answers, support messages)
  in `bot.log`. Support messages already have their own `messages.log`
  JSONL audit channel.
- **Blocked-bot vs real errors**: catch `TelegramForbiddenError` first,
  log at INFO with `reason=blocked`. Catch broader `Exception` second,
  log at WARNING/ERROR with `reason=<TypeName>` and `detail=<exception>`.
- **Retrofit policy**: existing legacy `logger.error(f"...")` lines stay
  as-is. Apply the convention only to **new** logs going forward.

### Operational notes

- `LOG_LEVEL` env var controls the level (default `INFO`). Set
  `LOG_LEVEL=DEBUG` to enable verbose lines if/when you add any.
- Rotation cap is 25MB (5MB × 5 backups). Files appear as `bot.log`,
  `bot.log.1`, … `bot.log.5`.
- Quick grep recipes:
  - All activity for one user: `grep "user_id=42" bot.log*`
  - All sessions completed today: `grep "session.complete" bot.log* | grep "2026-05-16"`
  - All evening reminders ever fired: `grep "reminder.evening.dispatched" bot.log*`
  - All real (non-blocked) delivery failures: `grep "reason=" bot.log* | grep -v "reason=blocked"`

---

## Template for future sessions

```markdown
## Session — YYYY-MM-DD

Goal: <one sentence>

### Changes

| # | Area | Change | Files |
|---|------|--------|-------|
| 1 | ... | ... | ... |

### Bugs caught

- ...

### Files modified

- ...

### Verification

- ...

### Deferred

- ...
```
