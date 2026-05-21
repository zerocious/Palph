# Palph

Telegram-бот для формирования регулярных учебных привычек у студентов через
геймификацию (монеты, ачивки, стрики), Pomodoro-таймер, квизы и
цифрового питомца.

> **Имя проекта.** Бот переименован из **StudyBuddy** в **Palph**
> 2026-05-19. Все user-facing строки и документация обновлены. Для
> операционной стабильности (logs, backups, deployment) сохранены
> исторические внутренние имена: `studybuddy.db` (default DB-файл),
> `studybuddy_bot` (logger name), `studybuddy-{date}.db` (backup
> filename pattern), `studybuddy-bot` (docker-compose container_name).
> Переименование этих имён сломало бы existing production deployment'ы
> (миграция БД, monitoring tools индексирующие по logger name,
> docker-compose up при сохранённых volumes). Если нужен полный rename
> внутренних имён — это отдельная migration-задача с явным
> backup/restore шагом.

**Статус:** v0.8 shipped 2026-05-20. Спринт v0.7 закрыт на 5 из 6 пунктов
(4 учебных режима + SM-2 + админ-CRUD + резюм таймера) + **полностью
переработанный цифровой питомец** (data layer + art track + UI: profile
preview, customization picker с 4-state buttons, level-up notification
со списком разблокированных, FSM rename, sad-pet GIF в вечернем
напоминании). Weekly leaderboard — **все 4 фазы shipped**:
`/leaderboard` command, segment auto-routing (newbie/main), privacy
opt-out, UTC-anchored weekly rollover с top-3/breakthrough/top-10%
наградами, ❄️ заморозка стрика (profile-кнопка + cooldown + atomic
покупка), 👥 friends system (`/friends` команда + 👥 Друзья кнопка в
профиле + add по @username/Telegram ID + accept/reject/remove +
deep-link invite-links через `/share_friend` + friends-tab по weekly
score). Бот переименован с StudyBuddy в Palph (internals retained
for ops compat). Спек: [LEADERBOARD.md](LEADERBOARD.md). См.
[TODO.md](TODO.md) и [session_notes.md](session_notes.md).
Tests: 529 passing (+53 since v0.8). PR #6 shipped A/B framework +
reference SQL queries (+34); PR #7 shipped event-schema docs + system.deploy
markers (+19). PA-roadmap Tier 2 closed; next session — Tier 3.

---

## Быстрый старт

### 1. Требования

- Python 3.10+
- Аккаунт у [@BotFather](https://t.me/BotFather) и токен бота
- (опционально) свой Telegram user-id для роли главного админа

### 2. Установка

```bash
git clone <repo-url>
cd Palph
python -m venv venv
# Windows PowerShell:
.\venv\Scripts\Activate.ps1
# macOS / Linux:
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Конфиг

Скопируй шаблон и заполни:

```bash
cp .env.example .env
```

```env
BOT_TOKEN=<токен от @BotFather>
MAIN_ADMIN_ID=<свой Telegram user-id (число)>
SERVER_TIMEZONE=Europe/Moscow
DB_PATH=studybuddy.db
LOG_FILE=bot.log
BACKUP_DIR=backups
BACKUP_RETENTION_DAYS=30
LOG_LEVEL=INFO
```

### 4. Запуск

**Локально:**

```bash
python bot.py
```

**В Docker:**

```bash
docker compose up -d --build
docker compose logs -f bot
```

`docker-compose.yml` монтирует `./data/` на хосте в `/data` в контейнере —
SQLite-БД, логи и `admins.json.migrated` живут там, переживают rebuild.

При первом запуске:
- создаётся `studybuddy.db` (SQLite, WAL-режим)
- если рядом есть `admins.json` — он импортируется в БД и переименовывается
  в `admins.json.migrated` (повторно не запускается)
- `MAIN_ADMIN_ID` всегда сидится в таблицу `admins`

---

## Что умеет бот

- **Pomodoro-таймер** (стандартный 25 мин + кастомный 5–120 мин); живёт в
  фоне, переживает рестарт бота (FSM в SQLite + автовозобновление задач,
  пользователь видит сообщение «♻️ Таймер продолжается, осталось N мин»)
- **Монеты** за каждую минуту учёбы + бонусы за достижения и стрик
- **Стрики** — ежедневный шедулер в локальном TZ пользователя
- **9 достижений** (первые сессии, длинные стрики, суммарные минуты)
- **4 учебных режима** под несколько предметов; пустые контентом
  предметы/режимы автоматически скрываются (data-driven обнаружение):
  - 🎯 **Ситуационные квизы** — открытый ответ + keyword-grader +
    фиксированные интервалы `[1, 2, 4, 7]` дней
  - 🃏 **Флэш-карты с SM-2** — 3-кнопочный рейтинг ❌/😐/✅, per-card
    ease factor, EF floor 1.3; +1 🪙 за карточку любого рейтинга
  - ❓ **MCQ** — выбор из 4 вариантов с перетасовкой, +1 🪙 за правильный
  - 📷 **Задачи с картинкой** — `task-NN.png` + JSON с принимаемыми
    ответами, 3 попытки → solution image; награды +3 / +2 / +1 / 0 🪙
- **💾 Backup БД** — ежедневный snapshot после streak processing (23:59
  в первом TZ глобального дня). Atomic через SQLite `VACUUM INTO`,
  retention 30 дней (`BACKUP_RETENTION_DAYS` в env), папка
  `./backups/` (Docker: `/data/backups/`). Главный админ может
  принудительно snapshot через `/backup`. Для disaster-recovery —
  template script `scripts/backup_offsite.sh.example` (GPG-шифрование +
  rclone upload в S3/B2; ручная настройка на хосте).
- **🛡 Rate limiting** — sliding-window in-memory лимитер per user
  (30 actions / 60 sec, warn на 70%, hard block на 100%). Применяется
  как aiogram middleware ко всем Message + CallbackQuery. Админы
  exempt. Защита от спама/abuse'а — не DDoS (бот через polling, нет
  публичного endpoint'а). См. план cybersecurity в
  [.claude/plans/make-a-new-session-merry-castle.md](.claude/plans/).
- **📊 Экран прогресса** (в профиле кнопка `📊 Прогресс по предметам`):
  10-квадратный mastery-bar 🟩⬜ per subject, плюс actionable строки —
  «🔔 К повторению сегодня», «🕐 Активность», «📈 Заходов». Пустые
  предметы (math/english пока без контента) показываются с пометкой
  «🚧 Контент в разработке». Mastery считается из 4 режимов: ситуационные
  termы с `streak ≥ 3`, флэш-карты с `repetitions ≥ 3`, MCQ-вопросы
  отвеченные хотя бы раз верно, решённые задачи.
- **Цифровой питомец** — data-layer ([PR #2 merged](https://github.com/zerocious/Palph/pull/2))
  + art/UI track в PR #3: один дизайн, **5 derived эмоций**
  (`studying / excited / sad / sleepy / happy` — выводятся в момент
  рендера), 1 XP/мин, `level = floor(sqrt(xp/10)) + 1`,
  `user_pet_inventory` под кастомизацию, атомарная покупка под `db.lock`,
  **level-up notification** со списком новых разблокированных предметов,
  **pet detail screen** в профиле (фото-превью + name + level + xp),
  **4-state customization picker** (⭐ надето / ✓ куплено / 💰 N доступно /
  🔒 lv.N заблокировано) для 5 цветов и 5 аксессуаров с confirm-диалогом
  на покупку, **переименование** через FSM. Pillow build-script
  (`scripts/build_pet_assets.py`) генерирует 125 PNG + 5 GIF placeholder
  ассетов (programmer-art) — real artwork как отдельный track-замена
  файлов в `assets/pet/`. Sad-pet image в evening reminder — follow-up.
- **🏆 Weekly leaderboard** (PR #3) — еженедельная формула:
  `(time + math_task + quiz + card) × streak_multiplier`, daily caps,
  ISO-week rollover **UTC Tuesday 00:00**. Auto-routing **newbie / main**
  по `created_at < 7 days`. Команды: `/leaderboard` (свой сегмент +
  ранг), `/friends` (👥 ранжированный лист друзей + add/accept/reject
  по **@username или Telegram ID** + remove с confirm; username
  кешируется через middleware на каждый Message), `/share_friend` —
  **deep-link виральный invite** (`t.me/Bot?start=friend_<token>`,
  multiuse, 30-day TTL; клик = auto-friendship без pending state).
  👤 Privacy opt-out в настройках. ❄️ **Streak freeze** — coins-gated
  кнопка в профиле, тиры 500/750/1000, 7-day cooldown, потребляется
  в miss-day path вместо reset стрика. Top-3 / breakthrough / top-10%
  coin bonus раздаются автоматически на rollover, идемпотентно.
  Backtest notebook `analysis/leaderboard_backtest.ipynb` валидирует
  формулу на исторических `events` до боевого запуска.
- **Уведомления**: утро / вечер / стрик / ачивки; включается в ⚙️ Настройки;
  время и часовой пояс настраиваются per-user
- **❓ FAQ** — интерактивное меню с 9 вопросами + кнопка техподдержки.
  Каждый вопрос как отдельная inline-кнопка → message edit показывает
  ответ + `[◀️ К списку]`. Вопросы: миссия проекта, эффективность,
  питомец, монеты (earn/spend), SM-2, интервальное повторение, active
  recall, гарантия результатов.
- **🛠 Техподдержка**: пользователь пишет в чат → forward всем админам;
  ответ через `/reply <user_id> <текст>` (или через FAQ → «Связаться с
  техподдержкой»).
- **Админ-инструменты:** `/help`, `/broadcast`, `/notif_status`, `/addadmin`,
  `/rmadmin`, `/listadmins`, `/backup`. Подробно — в
  [admin_commands.md](admin_commands.md).
- **📊 PA-аналитика для портфолио** — отдельный dashboard `/analytics`
  с inline-меню по 5 разделам:
  - 🔁 **Cohort retention** (D1/D7/D30 по ISO-неделям регистрации)
  - 🎯 **Activation funnel** (6 шагов от registered до 7-day streak)
  - 👥 **Active users** (DAU / WAU / MAU + stickiness ratio)
  - 🎮 **Feature adoption** (% пользователей по каждой фиче / режиму)
  - 📦 **Export CSV →** (9 таблиц, каждая как Telegram-документ)

  Каждый раздел также доступен отдельной командой (`/cohort_stats`,
  `/funnel`, `/dau`, `/feature_usage`, `/export <alias>`).

---

## Архитектура

Слоистая: репозиторий → сервис → бот. Никаких прямых SQL-запросов в
хендлерах.

```
bot.py              # Хендлеры aiogram, FSM-состояния, клавиатуры, mode picker,
                    # 4 учебных режима, main()
db.py               # aiosqlite connection, init_db (схема + индексы + миграции)
repository.py       # UserRepository, SessionRepository, AdminRepository,
                    # FlashcardRepository, McqProgressRepository,
                    # TaskProgressRepository, SubjectStatsRepository
                    # (только CRUD, без бизнес-логики)
services.py         # AchievementService, StudyService, StreakService,
                    # ReminderService, BackupService, AnalyticsService,
                    # UserRateLimiter
                    # + чистая функция sm2_update() для SM-2
tasks.py            # Фоновые asyncio-шедулеры (стрики 23:59 локально,
                    # утро/вечер раз в минуту)
fsm_storage.py      # SQLite-бэкенд для aiogram FSM → состояние таймеров,
                    # квизов и мастеров переживает рестарт
achievements.json   # Каталог ачивок (id, иконка, описание, награда)
study_materials/    # Учебные материалы — data-driven дерево:
  industrial-management/
    situational/section-{i,ii,iii,iv}.txt   (термин ‖ опр ‖ ключи ‖ ситуация)
    flashcards.txt                          (термин ‖ определение)
    mcq.txt                          (вопрос ‖ верный ‖ w1 ‖ w2 ‖ w3)
    tasks/task-NN.{png,json,-solution.png}  (картинка-условие + JSON метаданных)
  math/, english/                           (то же без situational/, ждут контента)
```

**Таблицы в БД** (создаются в `db.init_db`):
- `users`, `notification_settings` — профиль и настройки уведомлений
- `study_sessions` — каждая завершённая сессия (длительность, монеты, рейтинг)
- `user_achievements` — прогресс по 9 достижениям
- `quiz_progress` — SRS для **ситуационных** квизов (fixed intervals)
- `flashcard_progress` — SM-2 состояние per (user, card): ease_factor,
  interval_days, repetitions, last_review, next_review
- `mcq_progress` — per-question статистика MCQ: correct_count, total_count
- `task_progress` — per-task: attempts_used, succeeded
- `user_subject_stats` — per-subject visits + last_activity (для экрана прогресса)
- `events` — append-only event log для PA-аналитики (одна строка на каждое
  значимое действие: registration / session_started/completed / mode_picked /
  subject_picked / quiz_answered / mcq_answered / task_attempted /
  flashcard_reviewed / achievement_unlocked). `properties` — JSON-словарь.
  Foundation для funnel/cohort/path-анализа.
- `admins` — список админов (источник истины; in-memory кеш для is_admin())
- `fsm_storage` — постоянное FSM хранилище для aiogram

---

## Документация по проекту

| Файл | Что внутри |
|------|-----------|
| [TODO.md](TODO.md) | Текущий спринт + размеченный бэклог (Must/Should/Could/Won't) |
| [BACKLOG.md](BACKLOG.md) | Сырые идеи без приоритета — отстойник перед TODO |
| [session_notes.md](session_notes.md) | История сессий разработки (по датам, что менялось и почему) |
| [admin_commands.md](admin_commands.md) | Справочник по админским командам |
| [LEADERBOARD.md](LEADERBOARD.md) | Спека weekly leaderboard (формула, segments, privacy, freeze, friends, rewards, phasing) — source-of-truth при ребалансе |

## Файлы инфраструктуры

| Файл | Назначение |
|------|------------|
| [Dockerfile](Dockerfile) | Python 3.12-slim образ; non-root user; `/data` для persistent state |
| [docker-compose.yml](docker-compose.yml) | Запуск с `./data/` volume на хосте; env_file: .env; log rotation |
| [.dockerignore](.dockerignore) | Исключает .env, БД, логи, тесты, docs из образа |
| [requirements.txt](requirements.txt) | Runtime: aiogram, aiosqlite, pytz, python-dotenv |
| [requirements-dev.txt](requirements-dev.txt) | Dev only: pytest + pytest-asyncio + pandas/matplotlib/jupyter для `analysis/*.ipynb` |
| [pytest.ini](pytest.ini) | asyncio_mode=auto; testpaths=tests |
| [tests/](tests/) | Юнит-тесты (**529 штук**: SM-2, services, progress repos, BackupService, AnalyticsService, RateLimiter, EventRepository, log parser, PetRepository + derive_emotion, leaderboard helpers + repository + service + run_rollover + streak freeze + friends + username-search + friend-invite-links + middleware + integration flows + render_pet + level-up notification + picker helpers + reminder service sad-pet animation + **ExperimentRepository (22) + reference SQL smoke (12)** — PR #6 + **system.deploy event (13) + events-doc drift (6)** — PR #7) |
| [docs/events_schema.md](docs/events_schema.md) | Source of truth для всех `event_repo.log(...)` событий: when_fires, required/optional properties, JSON examples. Drift-тестом enforce'ится синхронность с кодом (PR #7). |
| [analysis/](analysis/) | Jupyter notebooks для PA-валидации (`leaderboard_backtest.ipynb` — реплей events → реконструкция weekly scores) + [analysis/queries/](analysis/queries/) — 8 standalone reference SQL queries (cohort retention, activation funnel, RFM, feature adoption, session-length, math funnel, churn predictors, pre-exam stub) с README + schema-drift smoke test |
| [parse_logs.py](parse_logs.py) | ETL: bot.log → CSV (CLI + библиотека для `/parse_logs` command) |
| [.github/workflows/security.yml](.github/workflows/security.yml) | Weekly `pip-audit` через GitHub Actions — CVE-сканирование зависимостей |
| [scripts/backup_offsite.sh.example](scripts/backup_offsite.sh.example) | Template скрипт для GPG-шифрованных offsite backup'ов через rclone |

---

## Полезное при разработке

### Тесты

```bash
# Один раз — установить dev-зависимости (pytest + pytest-asyncio)
pip install -r requirements-dev.txt

# Запустить весь suite
pytest -v

# Только SM-2 или конкретный сервис
pytest tests/test_sm2.py
pytest tests/test_streak_service.py -v
```

Покрытие — **437 тестов** (~28 сек):

**Pre-leaderboard baseline (185):**

| Файл | Тестов | Что покрывает |
|------|--------|--------------|
| `test_sm2.py` | 24 | SM-2: стандартные переходы, fail-path, EF floor, parametrized properties |
| `test_achievement_service.py` | 14 | Все 9 ачивок, multi-award, идемпотентность |
| `test_streak_service.py` | 11 (→13 после freeze hook) | Инкремент, сброс, +15🪙 со 2-го дня, multi-user isolation; +freeze-integration (Phase 3) |
| `test_progress_repos.py` | 16 | MCQ counters, task best-attempts, subject visits |
| `test_backup_service.py` | 12 | Daily dedup, restart-survival, retention cleanup, manual snapshots, валидность SQLite-файла после VACUUM INTO |
| `test_analytics_service.py` | 58 | Cohort retention, funnel, DAU/MAU stickiness, feature adoption, segments, content stats, event timeline, heatmap, single-CSV + ZIP export-all |
| `test_rate_limiter.py` | 15 | Basic limiting, warn-zone + cooldown, user isolation, sliding window expiry, edge cases |
| `test_event_repository.py` | 15 | Append-only insert, JSON serialization, null user_id, multi-event ordering, user isolation, error swallowing |
| `test_log_parser.py` | 20 | parse_log_line для structured events / multi-word values / unstructured legacy / malformed; CSV roundtrip; CLI main() exit codes |

**Pet data layer (47, PR #2 merged):**

| Файл | Тестов | Что покрывает |
|------|--------|--------------|
| `test_pet_repository.py` | 32 | create/get/inventory + xp formula (parametrized) + atomic purchase under db.lock + insufficient coins/level/already-owned/unknown-item + equip ownership / rename / mark_excited |
| `test_derive_emotion.py` | 15 | 5 priority branches + 10 hour-boundary cases (sleepy `[22:00, 06:00)`) |

**Leaderboard + friends + invite-links (205, PR #3 in flight):**

| Файл | Тестов | Что покрывает |
|------|--------|--------------|
| `test_leaderboard_helpers.py` | 40 | piecewise_time_pts (12 tier-boundary cases), streak_multiplier (12 tiers), freeze_cost (9 tiers), user_calendar_keys (ISO week + year-boundary) |
| `test_leaderboard_repository.py` | 24 | grant_time/task/quiz/card + caps + series bonus (5/15) + reset_quiz_series + read helpers + default-TZ resolution |
| `test_leaderboard_service.py` | 49 | render_leaderboard segments + privacy + ranked-segment multiplier-vs-base flip + get_user_rank + award_badge idempotency + active_badges expiration + privacy flag + render_friends_tab + **run_rollover** (top-3 main / breakthrough newbie / top-10% bonus, threshold + idempotency + hidden eligibility) + _compute_ended_week_iso (year boundary) + **purchase_freeze + has/consume_freeze + cooldown** |
| `test_friend_repository.py` | 25 | send_request status paths incl. **auto_accepted-via-reverse-pending**, accept transactional, reject/cancel, get_friends UNION, symmetric are_friends/remove_friend, pending lists |
| `test_username_search.py` | 25 | parse_friend_query (11 input variants), refresh_username (set/change/clear-to-NULL/no-op), find_by_username (case-insensitive COLLATE NOCASE), **create_user(username=) closes first-message gap** (3) |
| `test_friend_invite_tokens.py` | 17 | create_invite_token (uniqueness + 30-day expiry), find (valid / unknown / empty / None / expired), accept_invite (happy / self / already-friends / normalized storage / pending cleanup), multi-use, end-to-end |
| `test_username_sync_middleware.py` | 8 | UsernameSyncMiddleware: happy path + change persists + NULL handling + graceful degradation (no event_from_user / refresh_failure / falsy user_id) + missing-user no-op |
| `test_integration_flows.py` | 9 | End-to-end: full leaderboard journey (study → score → rollover → badge), friends lifecycle (send→accept→render→remove), freeze cycle, privacy effect both POVs, username search e2e, multiplier reordering (A=1000@0-streak vs B=900@14-streak → B wins) |

Каждый тест получает свежую SQLite через `tempfile`-фикстуру —
параллелятся без collisions.

### Прочие проверки

```bash
# Синтаксис
python -c "import ast; ast.parse(open('bot.py', encoding='utf-8').read())"

# Импорт модуля без запуска main() — ловит name-errors в декораторах
python -c "import importlib.util; s=importlib.util.spec_from_file_location('t','bot.py'); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); print('OK')"
```

### Дебаг через SQLite напрямую

```bash
# Пользователи и прогресс
sqlite3 studybuddy.db "SELECT user_id, current_streak, total_coins FROM users"

# Админы
sqlite3 studybuddy.db "SELECT * FROM admins"

# SM-2 состояние карточек: какие на повторении, какие "разогнаны"
sqlite3 studybuddy.db "SELECT card_hash, ease_factor, interval_days, repetitions, next_review FROM flashcard_progress ORDER BY next_review"

# Что сейчас в активных FSM (таймеры, мастера, незавершённые сессии)
sqlite3 studybuddy.db "SELECT key, state FROM fsm_storage WHERE state IS NOT NULL"

# Топ-сессии по длительности
sqlite3 studybuddy.db "SELECT user_id, duration_minutes, coins_earned, score, created_at FROM study_sessions ORDER BY id DESC LIMIT 10"
```

### Логи

`bot.log` — ротация 5 МБ × 5 файлов (~25 МБ потолок). Уровень регулируется
через `LOG_LEVEL` в `.env`. Путь — через `LOG_FILE` (default `bot.log`;
в Docker → `/data/bot.log` на mounted volume). Шум сторонних библиотек
(aiogram, aiohttp, aiosqlite) глушится до WARNING.

Бизнес-события идут как структурированные строки `event.tag key=value`:
- `app.start`, `app.shutdown`
- `session.complete source=natural|stop|reconcile`, `session.rated`
- `reconcile.summary completed=X resumed=Y broken=Z`,
  `reconcile.resume user_id=X duration=Y remaining=Z`
- `mcq.session.complete user_id=X subject=Y correct=A total=B coins=C`
- `task.answered user_id=X task_id=Y attempts=N result=correct|wrong|show_solution`
- `flash.rated user_id=X hash=Y quality=Z reps=A->B ef=C->D interval=E->F next=...`
- `flash.session.complete user_id=X subject=Y reviewed=N coins=M`
- `admin.added`/`admin.removed user_id=X by=Y`
- `broadcast.start`/`broadcast.done delivered=X failed=Y`
- `backup.created path=X size=Y duration_ms=Z`, `backup.cleanup removed=N`
- `anlt.export.done alias=X rows=N size_kb=M`
- `streak.batch`, `reminder.morning.dispatched`

### PA-аналитика для портфолио

Бот спроектирован как **источник реальных данных для product-analyst анализа**:

1. **В чате** — `/analytics` показывает все ключевые метрики (cohort retention,
   funnel, DAU/WAU/MAU stickiness, feature adoption) одной командой.
2. **Снаружи (single table)** — `/export <alias>` шлёт CSV-файл любой
   таблицы. 11 алиасов: `users`, `sessions`, `achievements`, `quiz`,
   `flashcards`, `mcq`, `tasks`, `subject_stats`, `settings`, **`events`**,
   **`experiments`**.
3. **Снаружи (full dataset)** — `/export all` шлёт **ZIP всех 11 таблиц +
   `metadata.json`** одним сообщением. metadata содержит `exported_at`
   (UTC ISO-8601), `schema_version`, `row_counts` по каждой таблице.
   Reproducible export для Jupyter — открывай и сразу анализируй.
4. **Events table** — append-only лог каждого значимого действия с
   timestamp и JSON-properties. Foundation для funnel/cohort/path/
   retention анализа. Каждое действие пользователя → одна строка.
5. **`/parse_logs`** (или CLI `python parse_logs.py`) — ETL для исторических
   данных: парсит `bot.log` + ротированные `bot.log.1..5` в CSV. Спасает
   когда нужны события до того, как `events` table начала писаться (события
   уже есть в логе структурированно — нужен только parser). Колонки:
   `timestamp, level, event_name, user_id, properties (JSON), raw_text`.

После 30+ дней живых данных — заполняется папка `analysis/` Jupyter-ноутбуками
с key findings (см. план в [TODO.md](TODO.md) → секция PA-аналитика).
**Уже доступно прямо сейчас:** [analysis/queries/](analysis/queries/) — 8
standalone `.sql` файлов (cohort retention, activation funnel, RFM
segmentation, feature adoption, session-length distribution, math funnel,
churn predictors, pre-exam stub) + README с инструкциями запуска. Smoke-test
`tests/test_reference_queries.py` валидирует все против текущей схемы при
каждом pytest-прогоне.

**Roadmap расширений** (зафиксирован 2026-05-21 в TODO.md → `🟡 Будущие расширения`):
~~A/B-тест фреймворк~~ ✅ **PR #6**; ~~`analysis/queries/` с 8 reference
SQL-файлами~~ ✅ **PR #6**; ~~`docs/events_schema.md` + drift-test~~
✅ **PR #7**; ~~`system.deploy` event-маркеры~~ ✅ **PR #7**.
Остаются: `analysis/schema_v1.yaml`, Wilson CI на `/cohort_stats`,
survival analysis через `lifelines`, anonymization helper для публичных
notebooks, `/feedback` команда, in-bot NPS опрос. **Осталось 6 пунктов**
из изначальных 10.

---

## Лицензия

Личный проект. Все права защищены. Не для перераспределения.
