# Session Notes

Running log of changes made per coding session. Newest entries at the top.

---

## Session — 2026-05-17

Goal: ship v0.7 — expand study (subjects + SM-2 flashcards + MCQ +
photo tasks), real digital pet (1 design + multi-emotion + customization),
main-menu 2×2 redesign, FAQ rewrite + tech-support absorbed, news channel
link, `/help` command.

Plan file: `C:\Users\User\.claude\plans\make-a-new-session-merry-castle.md`

### Planned scope

| # | Item | Status |
|---|------|--------|
| 7 | Main menu → 2×2 grid; remove tech-support button (absorbed into FAQ) | ✅ done |
| 3 | News button: real channel link via inline URL button | ✅ done |
| 5 | `/help` command + register in Telegram command picker (closes TODO #3) | ✅ done (admin-focused) |
| 4 | FAQ rewrite + absorb tech-support contact + cover new features | ✅ done |
| 1a | Study content restructure: `quizzes/` → `study_materials/`, add math + english subject folders | TODO |
| 1c | MCQ study mode (4-option inline keyboard) | TODO |
| 1d | Photo-task mode (image problem → 2 retries → solution image) | TODO |
| 1b | SM-2 flashcards (per-card ease factor + 3-button quality rating ❌/😐/✅) | TODO |
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

### Bugs caught

- _none yet — все правки этой партии прошли как статические edits; runtime-проверка через `python bot.py` ещё впереди_

### Files modified

- [bot.py](bot.py) — все 4 быстрые правки: импорты (`BotCommand`, scope-классы), `get_main_keyboard` (2×2), удаление `handle_support`, добавление `CHANNEL_URL` константы, переработка `handle_news` (inline-кнопка), переписанный `handle_faq` (7 Q&A + `@zerocious` в финале), новый `cmd_help` хендлер, `set_my_commands` логика в `main()`
- [bot.py](bot.py) — миграция админов: `ADMINS` теперь in-memory кеш над БД; `_migrate_admins_json_to_db()`; команды `/addadmin`/`/rmadmin`/`/listadmins`; `DEFAULT_COMMANDS`/`ADMIN_COMMANDS` на модульный уровень; `MAIN_ADMIN_ID` сидится в БД на старте
- [bot.py](bot.py) — резюм таймера: `run_timer_task` читает `start_time` из FSM data; `reconcile_stale_timers` ветка `resumed` пересоздаёт asyncio-задачу через `start_timer` и FSMContext; импорт `StorageKey`
- [repository.py](repository.py) — новый класс `AdminRepository` (get_all_ids / add / remove / is_admin)
- [admin_commands.md](admin_commands.md) — переписана секция «Как добавить/удалить админа»; добавлены подсекции для `/addadmin`, `/rmadmin`, `/listadmins`
- [.gitignore](.gitignore) — добавлены `admins.json.migrated*` и `messages.log` (раньше игнорировался только `messages.json`, которого больше нет)
- [TODO.md](TODO.md) — закрыты пп. 7 (admins.json → БД) и 8 (резюм таймера); оба сняты из бэклога

### Verification

- Статическая проверка: грепы подтверждают отсутствие лишних ссылок на `Техподдержка` и `handle_support`; `is_admin()` используется в `cmd_help` тем же паттерном, что в `/reply`/`/broadcast`/`/notif_status`. `python -c "import ast; ast.parse(...)"` подтверждает синтаксис `bot.py` и `repository.py`.
- **Live walkthrough ещё не запущен** — нужно `python bot.py` и в реальном Telegram:
  1. `/start` → проверить, что главное меню — это сетка 2×2 без кнопки техподдержки
  2. Нажать 📢 Новости → должна появиться inline-кнопка, открывающая `t.me/palph_study`
  3. Нажать ❓ FAQ → 7 вопросов, в конце «Остались вопросы? Напиши @zerocious»
  4. Отправить `/help` от админа → полный список; от не-админа → перенаправление на FAQ
  5. Открыть `/-`пикер в Telegram (нужно перезапустить клиент, чтобы подтянуть `set_my_commands`): админ видит `/help` + админские; обычный пользователь — только `/start` и `/stop`
  6. **Миграция админов**: при первом запуске после деплоя — в логе строка `admins.migration_done imported=3 archived=admins.json.migrated`; `ls` подтверждает что `admins.json` пропал, есть `admins.json.migrated`. На следующем рестарте — нет строки `migration_done` (повторно не запускается). `sqlite3 studybuddy.db "SELECT * FROM admins"` показывает 3 строки.
  7. **Управление админами**: от главного админа `/addadmin <test_id>` → ответ ✅ + в логе `admin.added`; от того же `test_id` `/help` теперь работает; `/listadmins` показывает 4 ID, главный с ★; `/rmadmin <test_id>` → ✅; `/listadmins` показывает 3 ID; `/rmadmin <MAIN_ADMIN_ID>` → ❌ «Нельзя удалить главного». От не-главного админа `/addadmin <id>` → ❌ «Только главный админ».
  8. **Резюм таймера**: запустить 5-минутный таймер, подождать 30 сек, Ctrl+C бота, подождать 10 сек, `python bot.py`. В логе: `reconcile.resume user_id=... duration=5 elapsed=0.5 remaining=5` и `reconcile.summary completed=0 resumed=1`. В чате — сообщение «♻️ Бот перезапустился — но твой таймер продолжается! Осталось: 5 мин». Через 4.5 минуты — таймер срабатывает естественно, монеты начисляются, прилетает запрос оценки.

### Deferred

- Real-time wall clock during timer (Item 6)
- Personal task manager (Item 6)
- Pet hunger/happiness/energy decay loop
- SM-2 for **situational quiz** (TODO #5 — needs a better grader first; SM-2 for flashcards ships in this session)
- Actual content for math/English (files created empty; user fills)
- Real channel URL + final FAQ wording (placeholders until user supplies)

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
