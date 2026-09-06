# Конфигурация и деплой

> **Doc sync:** 2026-09-05.

## Переменные окружения

Читаются через `python-dotenv` из `.env` рядом с `bot.py` (шаблон —
`.env.example`). `.env` в `.gitignore`.

| Переменная | Обязательна | Дефолт | Назначение |
|------------|:-----------:|--------|------------|
| `BOT_TOKEN` | **да** | — | Токен от [@BotFather](https://t.me/BotFather). Без него процесс падает на старте с `RuntimeError` |
| `MAIN_ADMIN_ID` | практически да | `0` | Telegram id главного админа. Доливается в таблицу `admins` при каждом старте; только он может `/addadmin`, `/rmadmin`, `/listadmins`, `/backup` |
| `SERVER_TIMEZONE` | нет | `Europe/Moscow` | Fallback-TZ сервера; пользовательский TZ живёт в `users.timezone` |
| `DB_PATH` | нет | см. ниже | Путь к SQLite-файлу |
| `LOG_FILE` | нет | см. ниже | Путь к `bot.log` |
| `BACKUP_DIR` | нет | см. ниже | Каталог снапшотов БД |
| `BACKUP_RETENTION_DAYS` | нет | `30` | Сколько дней хранить бэкапы |
| `LOG_LEVEL` | нет | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

Захардкожено в коде (не через env): `TELEGRAM_TIMEOUT = 30` (таймаут
aiohttp-сессии) и `CHANNEL_URL = "https://t.me/palph_study"`.

## Разрешение путей

`db.resolve_env_path(name)` работает по трём уровням:

1. Явное значение переменной окружения — побеждает всегда.
2. Иначе, если похоже на контейнер (`/app/data` существует **или**
   существует `/app/bot.py`) — контейнерные дефолты:
   `/app/data/studybuddy.db`, `/app/data/bot.log`, `/app/data/backups`.
3. Иначе локальные дефолты рядом с кодом: `studybuddy.db`, `bot.log`, `backups`.

`ensure_persistent_dirs()` вызывается на импорте `bot.py` и создаёт
родительские каталоги для БД и лога плюс каталог бэкапов.

Именно поэтому на bothost.ru можно **не задавать** `DB_PATH` / `LOG_FILE` /
`BACKUP_DIR` — код сам подставит `/app/data/*`. Регрессия покрыта
`tests/test_db_paths.py`, включая проверку, что существующая БД переживает
рестарт.

## Локальный запуск

```bash
python -m venv venv
source venv/bin/activate          # Windows: .\venv\Scripts\Activate.ps1
pip install -r requirements.txt   # для тестов: requirements-dev.txt
cp .env.example .env              # заполнить BOT_TOKEN и MAIN_ADMIN_ID
python bot.py
```

Требуется **Python 3.10+** (код использует `X | Y` в аннотациях);
образ и CI собираются на **3.12**.

## Зависимости

Продакшен (`requirements.txt`) — намеренно четыре пакета:

```
aiogram>=3.26,<4      aiosqlite>=0.22,<1
pytz>=2024.1          python-dotenv>=1.0,<2
```

Разработка (`requirements-dev.txt`): `pytest>=9.0.3,<10` (нижняя граница —
фикс CVE-2025-71176), `pytest-asyncio>=1.0,<2`, `pandas`, `matplotlib`,
`jupyter` (для ноутбуков), `Pillow>=10.4,<12` (только генератор
плейсхолдер-ассетов питомца).

Скрипт `scripts/generate_math_etalon_top3_tasks.py` дополнительно требует
`numpy` и `scipy` — они не в requirements, потому что нужны один раз при
генерации контента, а не в рантайме.

## Docker

```bash
docker compose up -d --build
docker compose logs -f bot
```

- Образ: `python:3.12-slim`, отдельный слой для зависимостей, запуск от
  непривилегированного пользователя `app`.
- `docker-compose.yml` монтирует `./data` на хосте в `/app/data` в контейнере;
  туда идут БД, логи, бэкапы и `admins.json.migrated` — всё переживает rebuild.
- `container_name: studybuddy-bot` и `studybuddy.db` — исторические имена,
  сохранены ради совместимости с работающими деплоями (см.
  [../README.md](../README.md) §«Имя проекта»).
- Ротация docker-логов: 10 МБ × 3 файла; файловые логи всё равно пишутся
  в `/app/data/bot.log`.

## bothost.ru

Persistent storage у хостера — **`/app/data/`** (не `/data/`). Dockerfile
уже задаёт нужные `ENV`, а код умеет автоопределение, поэтому в панели
хостера достаточно `BOT_TOKEN` и `MAIN_ADMIN_ID`.

> На бесплатном тарифе данные могут сбрасываться при redeploy. Для
> сохранности между рестартами нужен платный тариф.

## CI

`.github/workflows/security.yml` — единственный workflow:
`pip-audit --strict` по `requirements.txt` и `requirements-dev.txt`.
Триггеры: еженедельно (пн 07:00 UTC), вручную (`workflow_dispatch`) и на
каждый PR, меняющий файлы зависимостей. Любая найденная уязвимость валит job.

**Тесты в CI не запускаются** — это осознанный пробел; локально
`python -m pytest -q` обязателен перед коммитом (см.
[testing.md](testing.md)).

## Что попадает в git, а что нет

`.gitignore` исключает: `.env*` (кроме `.env.example`), `*.db*`, `bot.log*`,
`admins.json*`, `messages.log`, `data/`, `backups/`,
`analysis/exports/**/*.zip` и `*.csv` (персональные данные), `.claude/`,
кеши Python и IDE.
