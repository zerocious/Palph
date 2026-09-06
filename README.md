# Palph

Telegram-бот для формирования регулярных учебных привычек у студентов:
Pomodoro-таймер, четыре режима подготовки (ситуационные квизы, флэш-карты
с SM-2, MCQ, задачи), геймификация (монеты, ачивки, стрики, цифровой
питомец), недельный лидерборд с друзьями и продуктовая аналитика.

> **Статус:** v0.8, код на коммите `0ac30af` (2026-06-03).
> **Тесты:** 787, все зелёные (`python -m pytest -q`, ~24 с).
> **Документация:** [docs/](docs/README.md) — полный технический справочник.

> **Имя проекта.** Бот переименован из **StudyBuddy** в **Palph**
> 2026-05-19. Все строки интерфейса и документация обновлены. Ради
> операционной стабильности сохранены исторические внутренние имена:
> `studybuddy.db` (файл БД), `studybuddy_bot` (имя логгера),
> `studybuddy-{date}.db` (шаблон имени бэкапа), `studybuddy-bot`
> (`container_name` в docker-compose). Переименование сломало бы
> работающие деплои (миграция БД, мониторинг по имени логгера,
> `docker compose up` с сохранёнными volume). Полный rename внутренних
> имён — отдельная задача с явным шагом backup/restore.

---

## Быстрый старт

### Требования

- Python 3.10+ (образ и CI — 3.12)
- Токен бота от [@BotFather](https://t.me/BotFather)
- Свой Telegram user-id для роли главного админа

### Установка

```bash
git clone <repo-url>
cd Palph
python -m venv venv
source venv/bin/activate          # Windows: .\venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env               # заполнить BOT_TOKEN и MAIN_ADMIN_ID
python bot.py
```

Минимальный `.env`:

```env
BOT_TOKEN=<токен от @BotFather>
MAIN_ADMIN_ID=<свой Telegram user-id>
SERVER_TIMEZONE=Europe/Moscow
LOG_LEVEL=INFO
```

Пути `DB_PATH` / `LOG_FILE` / `BACKUP_DIR` задавать не обязательно — код
подставит локальные значения рядом с `bot.py`, а в контейнере
автоматически перейдёт на `/app/data/`. Полный список переменных —
[docs/configuration.md](docs/configuration.md).

### Docker

```bash
docker compose up -d --build
docker compose logs -f bot
```

`./data` на хосте монтируется в `/app/data` — БД, логи и бэкапы переживают
rebuild.

### bothost.ru

Persistent storage хостера — `/app/data/`. Dockerfile уже задаёт нужные
пути, код умеет автоопределение, поэтому в панели достаточно `BOT_TOKEN`
и `MAIN_ADMIN_ID`. На бесплатном тарифе данные могут сбрасываться при
redeploy.

### Что происходит при первом запуске

- создаётся `studybuddy.db` (SQLite, WAL), 28 таблиц и 14 индексов;
- если рядом лежит `admins.json` — импортируется в БД и переименовывается
  в `admins.json.migrated`;
- `MAIN_ADMIN_ID` доливается в таблицу `admins`;
- таймеры, оставшиеся в FSM от прошлого запуска, завершаются или
  возобновляются.

---

## Что умеет бот

### Учёба

- **Pomodoro-таймер** — стандартные 25 минут или свои 5–120. Живёт в фоне,
  переживает рестарт бота (FSM в SQLite + автовозобновление, пользователь
  получает «♻️ Таймер продолжается, осталось N мин»). Переход в
  «📖 Подготовка» таймер не останавливает.
- **Четыре режима подготовки** — вход: 📖 Подготовка → предмет → режим.
  Предметы и режимы без контента скрываются автоматически:
  - 🎯 **Ситуационные квизы** — открытый ответ, грейдер по ключевым
    словам, фиксированные интервалы `[1, 2, 4, 7]` дней;
  - 🃏 **Флэш-карты с SM-2** — рейтинг ❌/😐/✅, свой ease factor на
    карточку, пол EF 1.3, +1 🪙 за карточку при любом рейтинге. Источник
    настраивается: Микс / Официальные / Свои;
  - ❓ **MCQ** — 4 варианта с перетасовкой, +1 🪙 за верный;
  - 📷 **Задачи** — картинка или текст; две попытки: после первой ошибки
    подсказка (если есть), после второй — решение. Награда 3 / 2 / 0 🪙.
- **Свой контент** — до 100 флэш-карточек на предмет через 📇 Мои
  карточки и до 50 задач на предмет импортом `.txt` (файл до 64 КБ).

### Геймификация

- **Монеты** за каждую минуту учёбы, верные ответы, ачивки и стрик.
- **10 достижений** (сессии, стрики, минуты, советы) с наградами 25–125 🪙.
- **Стрики** — ночной прогон в 23:58–23:59 локального времени
  пользователя, +15 🪙 со второго дня.
- **Цифровой питомец** — 1 XP за минуту, `level = floor(sqrt(xp/10)) + 1`,
  три эмоции (`neutral` / `joy` / `sad`), выводимые в момент рендера, и
  четыре суточных варианта арта (утро / день / вечер / ночь). Уведомление о
  новом уровне со списком открывшегося, переименование. Тому, кто не
  учился, вечернее напоминание приходит с грустным питомцем.
  **Две фичи питомца сейчас выключены флагами:** `PET_SINGLE_IMAGE_MODE`
  (картинка зависит только от времени суток — не от эмоции и предметов) и
  `PET_CUSTOMIZATION_ENABLED` (покупка цветов и аксессуаров скрыта из UI;
  данные, каталоги и тесты на месте). Подробности —
  [docs/features.md](docs/features.md) §5.
- **🏆 Недельный лидерборд** —
  `(время + задачи + квизы + карточки) × множитель стрика`, дневные капы
  на каждый компонент, авто-сегменты «новички» / «основной», rollover во
  вторник 00:00 UTC с бэджами и монетным бонусом топ-10 %.
  ❄️ **Заморозка стрика** за 500 / 750 / 1000 🪙, не чаще раза в 7 дней.
  👤 Privacy opt-out: скрытый пользователь копит очки и получает награды,
  но не виден другим. Спека и баланс — [LEADERBOARD.md](LEADERBOARD.md).
- **👥 Друзья** — добавление по `@username` или Telegram ID, приём и
  отклонение заявок, удаление с подтверждением, вкладка друзей по
  недельному счёту и виральные deep-link приглашения `/share_friend`
  (multiuse, 3 дня).

### Вокруг учёбы

- **📊 Экран прогресса** — на каждый предмет mastery-bar 🟩⬜ из 10
  квадратов, «🔔 К повторению сегодня», «🕐 Активность», «📈 Заходов».
- **🎓 Советы по продуктивности** — тайм-менеджмент, память, «как
  пользоваться ботом», внешние ссылки. Контекстный подбор, cooldown 7
  дней, +1 🪙 за первый совет дня, совет дня в утреннем напоминании.
- **Уведомления** — утро, вечер, стрик, ачивки; время и часовой пояс
  настраиваются (50 пресетов TZ).
- **❓ FAQ** — 10 вопросов инлайн-меню + кнопка техподдержки.
  **🛠 Техподдержка** — сообщение пересылается админам, ответ через
  `/reply`. **📢 Новости** — кнопка на канал
  [`t.me/palph_study`](https://t.me/palph_study).
- **Локализация ru / en** — 443 ключа в каждом бандле, выбор языка при
  первом `/start` и в настройках.
- **`/delete_account`** — полное самостоятельное удаление данных
  (GDPR ст. 17 / 152-ФЗ).

### Инфраструктура

- **💾 Ежедневный бэкап БД** — атомарный `VACUUM INTO` после ночной
  обработки стриков, retention 30 дней, ручной снапшот через `/backup`.
- **🛡 Rate limiting** — 30 действий / 60 секунд на пользователя,
  предупреждение на 70 %, блок на 100 %; админы освобождены.
- **📊 PA-аналитика** — `/analytics` с инлайн-меню (когорты, воронка,
  time-to-value, продуктовые метрики, DAU/WAU/MAU, adoption, сегменты,
  контент, timeline, heatmap) и экспорт CSV / ZIP по 20 таблицам.

### Команды

**Пользовательские** (они же в `/`-пикере Telegram): `/start`, `/stop`,
`/progress`, `/pet`, `/leaderboard`, `/friends`, `/share_friend`,
`/delete_account`. Плюс `/cancel` внутри мастеров и `/skip` в онбординге —
в пикере их нет. `/help` — админская: обычному пользователю она отвечает
подсказкой открыть ❓ FAQ.

**Админские:** `/help`, `/reply`, `/broadcast`, `/notif_status`, `/analytics`,
`/cohort_stats`, `/funnel`, `/activation`, `/product_metrics`, `/dau`,
`/feature_usage`, `/segments`, `/content_stats`, `/event_timeline`,
`/heatmap`, `/export`, `/parse_logs`, плюс команды главного админа
`/addadmin`, `/rmadmin`, `/listadmins`, `/backup`.
Подробно — [admin_commands.md](admin_commands.md).

---

## Архитектура

Слоистая: `bot.py` (хендлеры) → `services.py` (логика) → `repository.py`
(SQL) → `db.py` (SQLite). Прямых SQL-запросов в хендлерах нет.

```
bot.py                   Хендлеры aiogram, FSM, клавиатуры, загрузка контента, main()
services.py              Study/Streak/Reminder/Leaderboard/Analytics/Backup/Achievement,
                         SM-2, формулы лидерборда, рендер питомца, resilience Telegram
repository.py            15 репозиториев — единственное место с SQL
db.py                    Соединение, PRAGMA, схема (28 таблиц), миграции, пути
tasks.py                 Три фоновых цикла: стрики+бэкап, напоминания, rollover
fsm_storage.py           FSM aiogram поверх SQLite — состояние переживает рестарт
i18n.py, locale_bot.py   Локализация ru/en
file_upload_security.py  Валидация загрузок, защита от path traversal
task_answer_match.py     Сверка числовых ответов
user_task_txt.py         Парсер своих задач из .txt
parse_logs.py            ETL bot.log → CSV
plan_service.py          Генератор спринт-плана (чистые функции)
plan_handlers.py         UI спринт-плана — выключен флагом PLAN_UI_ENABLED
```

Данные и контент:

```
achievements.json, achievements.en.json   Каталог ачивок
locales/{ru,en}.json                      Строки интерфейса (генерируются скриптом)
tips/*.json                               Советы по продуктивности
study_materials/<subject>/                Учебный контент, обнаружение data-driven
assets/pet/                               Арт питомца
analysis/                                 PA-аналитика: ноутбуки, шаблоны, выгрузки
audits/                                   Аудиты безопасности и устойчивости
scripts/                                  Инструменты и генераторы контента
tests/                                    787 тестов в 51 файле
```

Подробно — [docs/architecture.md](docs/architecture.md) и
[docs/data-model.md](docs/data-model.md).

---

## Документация

### Технический справочник — [docs/](docs/README.md)

| Документ | О чём |
|----------|-------|
| [docs/architecture.md](docs/architecture.md) | Слои, модули, запуск, фоновые циклы, конкурентность, логирование |
| [docs/data-model.md](docs/data-model.md) | 28 таблиц, индексы, миграции, удаление аккаунта |
| [docs/features.md](docs/features.md) | Точная механика каждой фичи: константы, формулы, состояния |
| [docs/analytics.md](docs/analytics.md) | 30 событий, метрики, экспорт |
| [docs/configuration.md](docs/configuration.md) | Переменные окружения, Docker, bothost, CI |
| [docs/operations.md](docs/operations.md) | Runbook: бэкапы, логи, диагностика инцидентов |
| [docs/security.md](docs/security.md) | Модель угроз и контроли |
| [docs/i18n.md](docs/i18n.md) | Локализация |
| [docs/content-authoring.md](docs/content-authoring.md) | Как добавлять контент |
| [docs/testing.md](docs/testing.md) | Требования к тестам |
| [docs/development-guide.md](docs/development-guide.md) | Рецепты и чеклисты для нового кода |
| [docs/scripts.md](docs/scripts.md) | Справочник по `scripts/` |

### Продуктовые и рабочие документы

| Файл | Что внутри |
|------|-----------|
| [LEADERBOARD.md](LEADERBOARD.md) | Спека лидерборда — источник истины при ребалансе |
| [user-flows.md](user-flows.md) | Пользовательские сценарии, подсчёт кликов, диаграммы |
| [admin_commands.md](admin_commands.md) | Справочник админских команд |
| [TODO.md](TODO.md) | Размеченные задачи (Must / Should / Could / Won't) |
| [BACKLOG.md](BACKLOG.md) | Сырые идеи до оценки |
| [session_notes.md](session_notes.md) | Журнал сессий разработки |
| [PRIVACY.md](PRIVACY.md) · [PRIVACY.ru.md](PRIVACY.ru.md) | Политика приватности (GDPR + 152-ФЗ) |
| [analysis/README.md](analysis/README.md) | PA-аналитика для портфолио |
| [study_materials/README.md](study_materials/README.md) | Форматы учебного контента |
| [tips/README.md](tips/README.md) | Формат советов |
| [audits/](audits/) | Аудиты: безопасность, ошибки, устойчивость, сложность |

---

## Разработка

### Тесты

```bash
pip install -r requirements-dev.txt
python -m pytest -q                      # 787 тестов, ~24 с
python -m pytest tests/test_sm2.py -v    # один файл
python -m pytest -k leaderboard -q       # по подстроке
```

**Ключевое требование:** в тестах не должно быть абсолютных календарных
дат как якоря «сейчас» — только относительные привязки; абсолютная дата
допустима лишь тогда, когда передаётся в код явным параметром. Полностью —
[docs/testing.md](docs/testing.md).

### Прочие проверки

```bash
python -m py_compile bot.py services.py repository.py db.py
python -c "import bot"                 # ловит name-errors в декораторах
python scripts/audit_i18n_keys.py      # ключи t() против locales/*.json
python scripts/pa_verify_export.py     # аналитика и экспорт живы
```

### Дебаг через SQLite

```bash
sqlite3 studybuddy.db "SELECT user_id, current_streak, total_coins FROM users"
sqlite3 studybuddy.db "SELECT * FROM admins"
sqlite3 studybuddy.db "SELECT card_hash, ease_factor, interval_days, repetitions, next_review \
                       FROM flashcard_progress ORDER BY next_review"
sqlite3 studybuddy.db "SELECT key, state FROM fsm_storage WHERE state IS NOT NULL"
sqlite3 studybuddy.db "SELECT created_at, event_name, user_id FROM events ORDER BY id DESC LIMIT 30"
```

### Логи

`bot.log`, ротация 5 МБ × 5 файлов, уровень через `LOG_LEVEL`. Формат —
`тег.через.точки key=value`; PII не пишется. Рецепты grep и разбор
инцидентов — [docs/operations.md](docs/operations.md).

### Перед коммитом

Прогнать тесты, обновить документацию, которую затрагивает изменение, и
`Doc sync` в её шапке, добавить запись в
[session_notes.md](session_notes.md). Полный чеклист —
[docs/development-guide.md](docs/development-guide.md).

---

## Файлы инфраструктуры

| Файл | Назначение |
|------|------------|
| [Dockerfile](Dockerfile) | `python:3.12-slim`, non-root, `/app/data` под состояние |
| [docker-compose.yml](docker-compose.yml) | Запуск с volume `./data`, `env_file: .env`, ротация логов |
| [.dockerignore](.dockerignore) | Исключает `.env`, БД, логи, тесты из образа |
| [requirements.txt](requirements.txt) | Рантайм: aiogram, aiosqlite, pytz, python-dotenv |
| [requirements-dev.txt](requirements-dev.txt) | pytest, pytest-asyncio, pandas, matplotlib, jupyter, Pillow |
| [pytest.ini](pytest.ini) | `asyncio_mode=auto`, `testpaths=tests` |
| [.github/workflows/security.yml](.github/workflows/security.yml) | Еженедельный `pip-audit --strict` по зависимостям |
| [scripts/backup_offsite.sh.example](scripts/backup_offsite.sh.example) | Шаблон offsite-бэкапа (GPG + rclone) |

---

## Лицензия

Личный проект. Все права защищены. Не для перераспределения.
