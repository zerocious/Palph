# Архитектура

> **Doc sync:** 2026-09-05 · код на коммите `0ac30af`.

Palph — однопроцессный Telegram-бот на **aiogram 3.x** + **aiosqlite**.
Никаких внешних сервисов: ни Redis, ни очередей, ни отдельной БД-инстанции.
Всё состояние — в одном SQLite-файле, всё расписание — в трёх asyncio-циклах
внутри процесса.

## Слои

```
        Telegram (long polling)
                 │
     ┌───────────▼─────────────┐
     │  bot.py  (handlers, UI) │  ~8100 строк: роутер, клавиатуры, FSM,
     │  plan_handlers.py       │  загрузчики контента, админ-команды
     └───────────┬─────────────┘
                 │  middlewares: UsernameSync → RateLimit
     ┌───────────▼─────────────┐
     │  services.py            │  бизнес-логика: сессии, стрики, напоминания,
     │  plan_service.py        │  лидерборд, аналитика, бэкапы, SM-2
     └───────────┬─────────────┘
     ┌───────────▼─────────────┐
     │  repository.py          │  15 репозиториев — единственное место с SQL
     └───────────┬─────────────┘
     ┌───────────▼─────────────┐
     │  db.py                  │  соединение, PRAGMA, схема, миграции
     │  fsm_storage.py         │  FSM aiogram поверх той же БД
     └───────────┬─────────────┘
              SQLite (WAL)
```

Правило слоёв: **новый SQL пишется в `repository.py`**. Штатные исключения —
`db.py` (схема и миграции), `fsm_storage.py` (своя таблица) и
`AnalyticsService` (агрегирующие запросы по природе аналитические).

Фактическое состояние честнее: в `bot.py` осталось **14** прямых
`db.execute(...)` — исторический долг, не образец для подражания:

| Место | Что делает | Почему так вышло |
|-------|-----------|------------------|
| `get_quiz_progress`, `update_quiz_progress`, `get_next_quiz_term` (≈1490–1550) | Чтение и запись `quiz_progress` | Для ситуационных квизов репозитория так и не завели — единственный режим учёбы без своего репозитория |
| `_count_situational_mastered`, `_count_flashcards_mastered`, `_count_situational_due`, `_count_flashcards_due` (≈1940–1995) | `COUNT(*)` с `IN (...)` по списку хешей для экрана прогресса | Запрос с динамическим числом плейсхолдеров писали прямо на месте |
| `show_achievements` (≈3352) | Чтение `user_achievements` | Обходит `AchievementService` |
| `reconcile_stale_timers` (≈7768–7860) | Чтение и чистка `fsm_storage` | Работа с FSM-таблицей на старте, до поднятия репозиториев |

Все они параметризованы (значения только через `?`), так что это вопрос
чистоты слоёв, а не безопасности. При следующем касании этих участков
логичный шаг — завести `QuizProgressRepository` и перенести туда первые три.

В остальном хендлеры в БД напрямую не ходят — только через репозитории и сервисы.

## Карта модулей

| Модуль | Строк | Ответственность |
|--------|------:|-----------------|
| `bot.py` | 8057 | Точка входа, роутер aiogram, все хендлеры, клавиатуры, FSM-состояния, загрузка учебного контента с диска, админ-команды, `main()` |
| `services.py` | 2858 | `StudyService`, `StreakService`, `ReminderService`, `LeaderboardService`, `AnalyticsService`, `BackupService`, `AchievementService`, `UserRateLimiter`, SM-2, формулы лидерборда, рендер питомца, resilience-обёртки Telegram |
| `repository.py` | 2373 | 15 репозиториев: `UserRepository`, `SessionRepository`, `UserFlashcardRepository`, `UserTaskRepository`, `TipsRepository`, `FlashcardRepository`, `McqProgressRepository`, `TaskProgressRepository`, `SubjectStatsRepository`, `EventRepository`, `AdminRepository`, `PetRepository`, `LeaderboardRepository`, `FriendRepository`, `PlanRepository` |
| `plan_handlers.py` | 791 | UI спринт-плана к экзамену. **Выключен флагом `PLAN_UI_ENABLED = False`** |
| `plan_service.py` | 545 | Чистые функции спринт-плана: каталог контента, диагностика, генерация 14-дневного плана |
| `db.py` | 542 | `get_db()`, `init_db()` (28 таблиц + 14 индексов), ALTER-миграции, разрешение путей `DB_PATH`/`LOG_FILE`/`BACKUP_DIR` |
| `file_upload_security.py` | 231 | Валидация загружаемых `.txt`, защита от path traversal, санитайз ключей ассетов питомца, лимиты длины |
| `parse_logs.py` | 196 | ETL `bot.log` → CSV событий (для `/parse_logs`) |
| `tasks.py` | 147 | Три фоновых планировщика: стрики+бэкап, напоминания, недельный rollover |
| `locale_bot.py` | 139 | Хелперы локализации: каталоги предметов/режимов/FAQ, команды бота, ярлыки |
| `fsm_storage.py` | 121 | `SQLiteStorage` — persistent FSM для aiogram |
| `user_task_txt.py` | 119 | Парсер пользовательских задач из `.txt` + промпт-инструкция |
| `task_answer_match.py` | 95 | Сверка числовых ответов (дроби, запятая/точка, нормализованный текст) |
| `i18n.py` | 76 | `t()`, `kb_in()`, загрузка бандлов `locales/*.json` |

Легаси: `archive/telegabot.py.legacy` — исходный монолит до рефакторинга,
в рантайме не участвует.

## Последовательность запуска (`bot.py:main`)

1. `get_db()` — соединение aiosqlite, `PRAGMA journal_mode=WAL`,
   `foreign_keys=ON`, `busy_timeout=5000`; к соединению прикрепляется
   `db.lock` (`asyncio.Lock`).
2. `init_db(db)` — `CREATE TABLE IF NOT EXISTS` × 28, индексы, затем
   идемпотентные `ALTER TABLE` миграции (см. [data-model.md](data-model.md)).
3. Конструируются 15 репозиториев, затем `Bot`, затем сервисы
   (порядок важен: `StudyService` получает `bot` для level-up-уведомлений).
4. `Dispatcher(storage=SQLiteStorage(db))` — FSM переживает рестарт.
5. Middlewares регистрируются **до** `include_router`:
   `UsernameSyncMiddleware` → `RateLimitMiddleware` (порядок важен: username
   обновляем даже когда rate-limit глушит событие).
6. `register_plan_handlers(...)` — только если `PLAN_UI_ENABLED`.
7. `_migrate_admins_json_to_db()` — одноразовый импорт `admins.json`,
   файл переименовывается в `admins.json.migrated`.
8. `MAIN_ADMIN_ID` всегда добавляется в `admins`; кеш `ADMINS` наполняется из БД.
9. `reconcile_stale_timers()` — разбор таймеров, оставшихся в FSM после
   прошлого запуска (см. ниже).
10. `set_my_commands` — дефолтный набор для всех + расширенный для каждого
    админа через `BotCommandScopeChat`.
11. Стартуют три фоновых задачи (`asyncio.create_task` + `add_done_callback`
    на логирование исключений).
12. `bot.get_me()` кеширует `@username` бота (нужен для deep-link инвайтов).
13. `dp.start_polling(bot)`; в `finally` — отмена фоновых задач и закрытие сессии.

## Фоновые циклы (`tasks.py`)

| Цикл | Период | Что делает |
|------|--------|------------|
| `streak_scheduler` | 60 с | Для каждого используемого TZ: если локально 23:58–23:59 и сегодня ещё не обрабатывали — прогоняет `StreakService.process_users_in_timezone(tz)`, затем `BackupService.maybe_backup_for_today()` (дедуп внутри сервиса) |
| `reminder_scheduler` | до начала следующей минуты + 0.5 с | Для каждого TZ считает локальное `HH:MM` и вызывает `ReminderService.tick(tz, hhmm)`; помнит последнюю минуту на TZ, чтобы не слать дважды. Heartbeat в лог раз в 10 итераций |
| `leaderboard_scheduler` | 60 с | Если сейчас **вторник 00:xx UTC** и сегодня ещё не запускались — `LeaderboardService.run_rollover(ended_week_iso)` |

Дедупликация везде двухуровневая: in-memory отметка «последняя дата/минута»
+ идемпотентность на уровне БД (`INSERT OR IGNORE`, `users.last_streak_check_date`).

**Почему rollover привязан к UTC-вторнику 00:00**, а не к локальной полуночи
пользователя: к этому моменту все TZ (от UTC+14 до UTC−12) уже пересекли
границу Sun→Mon локально, значит `weekly_scores` за прошедшую неделю
зафиксированы у всех — нет гонок с поздними записями. Пер-TZ rollover дал бы
фрагментированные мини-лидерборды.

## Восстановление таймеров после рестарта

`reconcile_stale_timers()` читает `fsm_storage` и для каждой записи в
состоянии `TimerStates.active`:

- `elapsed >= duration` → сессия закрывается: начисляются монеты и ачивки,
  логируется `session_completed` с `source=reconcile`, отправляется
  уведомление, FSM-строка удаляется. Запрос оценки сессии **не** шлётся —
  пользователь в этот момент не был активен.
- `elapsed < duration` → задача **возобновляется**: пересоздаётся
  `FSMContext` из той же storage, `start_timer()` поднимает asyncio-таск,
  который досыпает остаток от исходного `start_time`. Пользователю уходит
  «♻️ Таймер продолжается, осталось N мин».
- Битая запись (нет `start_time`, кривой `duration`, некорректный ключ) →
  удаляется с `WARNING` в лог.

Незавершённые мастера ввода кастомной длительности
(`TimerStates.waiting_for_duration`) чистятся безусловно.

## Конкурентность и целостность

- **Один `asyncio.Lock` на соединение** (`db.lock`). Берётся для
  read-modify-write: `complete_session`, `purchase_item`, переключение
  настроек, запись FSM. Вложенный захват запрещён — методы, вызываемые
  из-под лока (`PetRepository.add_xp`, `LeaderboardRepository.grant_time_pts`),
  сами лок не берут; это зафиксировано в их докстрингах.
- **Lock-free там, где хватает атомарности SQL.** Дневные капы лидерборда
  реализованы как `UPDATE … WHERE quiz_count < 25` + проверка `rowcount` —
  гонка невозможна без лока.
- `execute_with_db_retry()` в `db.py` — ретрай с экспоненциальной паузой на
  транзиентный `database is locked`.
- **WAL** позволяет читателям не блокировать писателя; `busy_timeout=5000`
  сглаживает короткие конфликты.
- Уведомления в Telegram отправляются **вне** `db.lock` — сетевой вызов не
  должен держать лок (см. `StudyService.complete_session`).

## Устойчивость к сбоям Telegram

В `services.py`:

- `_send_with_retry_after()` — до `MAX_SEND_ATTEMPTS = 3` попыток, уважает
  `retry_after` из `TelegramRetryAfter`, экспоненциальный backoff на
  транзиентных сетевых ошибках.
- `TelegramSendBreaker` — circuit breaker: 10 исчерпанных серий попыток
  (сетевые отказы; успешная отправка обнуляет счётчик) → пауза 60 с, чтобы
  не молотить в недоступный API. Пока брейкер открыт, отправки падают с
  `TelegramSendBreakerOpen` и тегом `telegram.breaker_skip`.
- `_TELEGRAM_SEND_SEM = asyncio.Semaphore(5)` — bulkhead: не больше 5
  одновременных исходящих отправок (актуально для рассылок и напоминаний).
- `send_with_telegram_bulkhead()` объединяет всё вышеперечисленное; им
  пользуются рассылки, напоминания, уведомления о стрике.

Классификация ошибок при отправке единообразна во всём коде:
`TelegramForbiddenError` (бот заблокирован) → `INFO` с `reason=blocked`;
`TelegramBadRequest` → `WARNING`; всё остальное → `WARNING`/`ERROR` с
`reason=<TypeName>`.

Глобальный `@router.errors()` (`global_error_handler`) ловит всё, что
пробилось сквозь хендлер: пишет `handler.unhandled` с трейсбеком и отвечает
пользователю нейтральным `common.unexpected_error`, чтобы апдейт не «завис»
молча. Исключение — `TelegramForbiddenError`: пользователю, который
заблокировал бота, отвечать бессмысленно, апдейт просто помечается
обработанным.

## Middlewares

| Middleware | Порядок | Что делает |
|------------|---------|------------|
| `UsernameSyncMiddleware` | 1-й | Обновляет `users.username` из `event_from_user.username` на каждом Message/CallbackQuery. Нужен для поиска друзей по `@handle` и подписей в лидерборде. Стоит первым, потому что rate-limit может «съесть» апдейт, а username хочется обновлять всегда |
| `RateLimitMiddleware` | 2-й | `UserRateLimiter`: скользящее окно 30 действий / 60 с. `warn` на 70 % (одно предупреждение не чаще раза в 30 с), `block` на 100 % — событие тихо отбрасывается. Админы освобождены |

## Загрузка учебного контента

Контент — файлы на диске, читаются заново на каждый вход в режим; кеша нет
ни у учебных материалов, ни у советов, поэтому правка файла видна без
рестарта бота:

- `study_materials/<subject>/flashcards.txt` — `термин || определение [|| темы]`
- `study_materials/<subject>/mcq.txt` — `вопрос || верный || неверный ×3 [|| темы]`
- `study_materials/<subject>/situational/section-*.txt` — ситуационные термины
- `study_materials/<subject>/tasks/task-NN.json` (+ опционально `task-NN.png`)
- `study_materials/<subject>/groups.json`, `topics.json`, `diagnostic/default.json`

**Обнаружение предметов и режимов — data-driven.** `available_modes()`
возвращает только те режимы, у которых реально есть непустой контент
(файловый или пользовательский), `available_subjects()` — только предметы
хотя бы с одним таким режимом. Поэтому пустой `english/flashcards.txt`
автоматически исчезает из меню, а появление пользовательских карточек
автоматически включает режим «флэш-карты» для этого предмета.

`PREP_HIDDEN_SUBJECT_IDS = {"industrial-management"}` — предмет исключён из
`available_subjects`, а `handle_subject_picked` отвечает на него заглушкой
«🚧 в разработке» (на случай устаревшей клавиатуры у пользователя). То есть
из UI он **недоступен полностью**, хотя контент на диске сохраняется и
подхватится сразу после удаления id из этого множества.

## Логирование

Логгер `studybuddy_bot` (историческое имя, см. README §«Имя проекта»),
`RotatingFileHandler` 5 МБ × 5 бэкапов + stdout.

Конвенция строки: **точечный тег + `key=value`**, без f-строк внутри
формата (`logger.info("session.complete user_id=%s", uid)`), без PII —
пользовательский текст в `bot.log` не пишется. Примеры тегов:
`app.start`, `session.complete`, `task.answered`, `reminder.evening.dispatched`,
`reconcile.resume`, `leaderboard.rollover.badge`, `events.log_failed`.
Полный список конвенций — в конце [../session_notes.md](../session_notes.md).

## Что намеренно не сделано

- **Нет webhook-режима** — только long polling. Публичного эндпоинта нет,
  что заодно снимает целый класс атак.
- **Нет отдельного слоя кеша** — SQLite локальный, запросы дешёвые.
  Кешируются только `ADMINS` (set в памяти), бандлы локалей
  (`i18n._load_bundle` под `lru_cache`) и `bot_username`. Правка
  `locales/*.json` требует рестарта, правка учебного контента и советов —
  нет.
- **Нет ORM** — сырой SQL в репозиториях, миграции через `ALTER TABLE`
  в `init_db`.

## Выключенные фичи (флаги)

Три готовых куска функциональности сейчас закрыты константами. Код, схема и
тесты живые — включение это одна строка, но пока их **нет в UI**, и
документация обязана это отражать.

| Флаг | Где | Что выключает |
|------|-----|---------------|
| `PLAN_UI_ENABLED = False` | `plan_handlers.py:41` | Спринт-план к экзамену: `register_plan_handlers` не вызывается из `main()`, ветки `if PLAN_UI_ENABLED` в семи местах `bot.py` не срабатывают |
| `PET_CUSTOMIZATION_ENABLED = False` | `bot.py:6702` | Покупку цветов и аксессуаров: кнопки не рисуются, строка «Цвет · Аксессуар» убрана из подписи, все `pet_*`-callback'и возвращают отказ первой строкой |
| `PET_SINGLE_IMAGE_MODE = True` | `services.py:478` | Разнообразие арта: `render_pet` возвращает `<period>/default.png` до того, как дойдёт до эмоции, цвета, аксессуара и GIF-ветки |
