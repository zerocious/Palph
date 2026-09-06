# Эксплуатация (runbook)

> **Doc sync:** 2026-09-05.

## Что крутится в проде

Один процесс `python bot.py`: long polling + три фоновых asyncio-цикла
(стрики+бэкап, напоминания, недельный rollover). Состояние — один
SQLite-файл. Внешних зависимостей нет.

Признак здорового старта в логах:

```
app.start admins=N main_admin_id=... server_tz=... log_level=INFO
app.bot_username @...
reconcile.summary completed=.. resumed=.. broken=.. total=..
✅ Palph запущен
reminder_scheduler: started
leaderboard_scheduler: started
```

## Бэкапы

**Автоматически:** `BackupService.maybe_backup_for_today()` вызывается из
`streak_scheduler` после обработки стриков очередного TZ. Реальный снапшот
создаётся один раз за серверные сутки — первым TZ, который дошёл до 23:59.

- Механизм: `VACUUM INTO` на отдельном соединении — атомарно, не мешает
  транзакциям приложения.
- Имя: `backups/studybuddy-YYYY-MM-DD.db`.
- Дедуп двойной: in-memory `_last_backup_date` + проверка существования
  файла (чтобы рестарт бота не сделал второй снапшот за день).
- Retention: `BACKUP_RETENTION_DAYS` (по умолчанию 30). Чистятся **только**
  daily-файлы; ручные снапшоты сохраняются всегда.

**Вручную:** `/backup` (только главный админ) → `force_backup()` →
`backups/studybuddy-manual-YYYY-MM-DD-HHMMSS.db`.

**Ошибка бэкапа никогда не роняет бота** — только `ERROR backup.failed`
в лог. Поэтому раз в неделю стоит глазами проверять, что файлы появляются.

### Восстановление

```bash
docker compose down                       # или остановить процесс
cp data/backups/studybuddy-2026-06-01.db data/studybuddy.db
rm -f data/studybuddy.db-wal data/studybuddy.db-shm   # остатки WAL от старой БД
docker compose up -d
```

Проверить целостность до подмены:

```bash
sqlite3 data/backups/studybuddy-2026-06-01.db "PRAGMA integrity_check;"
sqlite3 data/backups/studybuddy-2026-06-01.db "SELECT COUNT(*) FROM users;"
```

### Offsite

`scripts/backup_offsite.sh.example` — шаблон: GPG-шифрование снапшота +
загрузка через `rclone` в S3/B2. Настраивается вручную на хосте и ставится
в cron **после** времени внутреннего бэкапа. Без этого все копии лежат на
той же машине, что и оригинал, — то есть это не disaster recovery.

## Логи

`bot.log` (путь — `LOG_FILE`), `RotatingFileHandler` 5 МБ × 5 бэкапов
(потолок ~25 МБ) + дублирование в stdout (`docker compose logs`).

Формат: `тег.через.точки key=value key=value`. Полезные рецепты:

```bash
grep "user_id=42" bot.log*                       # всё по одному пользователю
grep "session.complete" bot.log* | grep 2026-06-01
grep "reminder.evening.dispatched" bot.log*      # вечерние напоминания
grep "reason=" bot.log* | grep -v "reason=blocked"   # реальные отказы доставки
grep -E "backup.(created|failed)" bot.log*
grep "leaderboard.rollover" bot.log*
grep "telegram.breaker_open" bot.log*            # circuit breaker сработал
```

`LOG_LEVEL=DEBUG` включает подробный вывод. PII в логи не пишется —
пользовательский текст там искать бесполезно (и не должно появляться).

## Диагностика инцидентов

### Не приходят напоминания

1. `/notif_status` — показывает настройки и TZ **вызывающего админа** и
   симулирует, попадёт ли он в выборку шедулера на своё
   `morning_time` / `evening_time`. Чужого пользователя команда не
   диагностирует — для него смотрите `notification_settings` и
   `users.timezone` напрямую в БД (пример ниже).
2. `grep "reminder_scheduler heartbeat" bot.log*` — heartbeat раз в 10 минут
   подтверждает, что цикл жив.
3. Проверить `notification_settings` пользователя и его `users.timezone`.
4. `grep "reminder.send_failed" bot.log*` — `reason=blocked` значит, что
   пользователь заблокировал бота (это не наша поломка).

### Стрики не обновляются

- Обработка идёт в 23:58–23:59 **локального** TZ; если бот в это окно лежал,
  день пропущен — догоняющего прогона нет.
- `users.last_streak_check_date` показывает, за какой день уже отработали.
- `grep "streak_scheduler" bot.log*`.

### Награды за неделю не выданы

- Rollover — вторник 00:xx UTC. `grep "leaderboard.rollover" bot.log*`.
- Повторный запуск безопасен: `award_badge` идемпотентен по PK.
- Ручной прогон за конкретную неделю — только через python-консоль с тем же
  соединением; отдельной админ-команды нет.

### `database is locked`

- Единичные записи гасятся `busy_timeout=5000` и `execute_with_db_retry`.
- Систематические — искать долгую операцию под `db.lock` (см.
  [architecture.md](architecture.md) §«Конкурентность»): сетевой вызов
  внутри лока — это баг.

### Бот молчит / Telegram недоступен

- `telegram.breaker_open` — открылся circuit breaker, отправки на паузе 60 с.
- `telegram.retry_after` — Telegram троттлит, помогает уменьшить рассылки.
- `handler.unhandled` — необработанное исключение в хендлере, есть трейсбек.

## Ручной осмотр БД

```bash
sqlite3 data/studybuddy.db

.tables
SELECT COUNT(*) FROM users;
SELECT user_id, total_coins, current_streak, timezone, locale FROM users LIMIT 20;
SELECT * FROM admins;
-- что сегодня к повторению
SELECT user_id, COUNT(*) FROM flashcard_progress
 WHERE next_review <= date('now') GROUP BY user_id;
-- активные FSM (таймеры, мастера)
SELECT key, state FROM fsm_storage WHERE state IS NOT NULL;
-- последние события
SELECT created_at, event_name, user_id FROM events ORDER BY id DESC LIMIT 30;
```

## Регулярные проверки

| Периодичность | Что |
|---------------|-----|
| Ежедневно | Появился ли файл бэкапа за сегодня |
| Еженедельно | `grep -c "handler.unhandled" bot.log*`; результат pip-audit workflow |
| После деплоя | `reconcile.summary` в логе; `/notif_status`; один тестовый Pomodoro |
| Перед релизом | `python -m pytest -q` (792 теста), `python -m py_compile bot.py services.py repository.py` |

## Админ-доступ

Источник истины по админам — таблица `admins`; `MAIN_ADMIN_ID` доливается
на каждом старте. Права главного админа (`/addadmin`, `/rmadmin`,
`/listadmins`, `/backup`) проверяются сравнением с `MAIN_ADMIN_ID`, а не
по таблице, — обычный админ их не получит. Полный список команд —
[../admin_commands.md](../admin_commands.md).
