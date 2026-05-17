# StudyBuddy

Telegram-бот для формирования регулярных учебных привычек у студентов через
геймификацию (монеты, ачивки, стрики), Pomodoro-таймер, квизы и
цифрового питомца.

**Статус:** MVP в эксплуатации; идёт спринт v0.7 (расширение режимов учёбы +
полноценный питомец). См. [TODO.md](TODO.md) и [session_notes.md](session_notes.md).

---

## Быстрый старт

### 1. Требования

- Python 3.10+
- Аккаунт у [@BotFather](https://t.me/BotFather) и токен бота
- (опционально) свой Telegram user-id для роли главного админа

### 2. Установка

```bash
git clone <repo-url>
cd studybuddy
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
LOG_LEVEL=INFO
```

### 4. Запуск

```bash
python bot.py
```

При первом запуске:
- создаётся `studybuddy.db` (SQLite, WAL-режим)
- если рядом есть `admins.json` — он импортируется в БД и переименовывается
  в `admins.json.migrated` (повторно не запускается)
- `MAIN_ADMIN_ID` всегда сидится в таблицу `admins`

---

## Что умеет бот

- **Pomodoro-таймер** (стандартный 25 мин + кастомный 5–120 мин); живёт в
  фоне, переживает рестарт бота (FSM в SQLite + автовозобновление задач)
- **Монеты** за каждую минуту учёбы + бонусы за достижения и стрик
- **Стрики** — ежедневный шедулер в локальном TZ пользователя
- **9 достижений** (первые сессии, длинные стрики, суммарные минуты)
- **Квизы** с интервальным повторением по фиксированной сетке `[1, 2, 4, 7]`
- **Цифровой питомец** (пока что простой; в v0.7 — полная переработка)
- **Уведомления**: утро / вечер / стрик / ачивки; включается в ⚙️ Настройки;
  время и часовой пояс настраивается per-user
- **FAQ + техподдержка**: пользователь пишет в чат → forward всем админам;
  ответ через `/reply <user_id> <текст>`
- **Админ-инструменты**: `/help`, `/broadcast`, `/notif_status`, `/addadmin`,
  `/rmadmin`, `/listadmins`. Подробно — в [admin_commands.md](admin_commands.md).

---

## Архитектура

Слоистая: репозиторий → сервис → бот. Никаких прямых SQL-запросов в
хендлерах.

```
bot.py            # Хендлеры aiogram, FSM-состояния, клавиатуры, main()
db.py             # aiosqlite connection, init_db (схема + индексы + миграции)
repository.py     # UserRepository, SessionRepository, AdminRepository
                  # (только CRUD, без бизнес-логики)
services.py       # AchievementService, StudyService, StreakService,
                  # ReminderService (бизнес-логика — начисление монет,
                  # проверка ачивок, обработка стриков)
tasks.py          # Фоновые asyncio-шедулеры (стрики 23:59 локально,
                  # утро/вечер раз в минуту)
fsm_storage.py    # SQLite-бэкенд для aiogram FSM → состояние таймеров,
                  # квизов и мастеров переживает рестарт
achievements.json # Каталог ачивок (id, иконка, описание, награда)
quizzes/          # Учебные материалы (термины для интервального повторения)
```

Полная схема БД и список модулей —
[Archives/Startup/StudyBuddy – Контекст проекта.md](Archives/Startup/) (исторический документ;
описывает MVP-стадию).

---

## Документация по проекту

| Файл | Что внутри |
|------|-----------|
| [TODO.md](TODO.md) | Текущий спринт + размеченный бэклог (Must/Should/Could/Won't) |
| [BACKLOG.md](BACKLOG.md) | Сырые идеи без приоритета — отстойник перед TODO |
| [session_notes.md](session_notes.md) | История сессий разработки (по датам, что менялось и почему) |
| [admin_commands.md](admin_commands.md) | Справочник по админским командам |

---

## Полезное при разработке

### Проверки

```bash
# Синтаксис
python -c "import ast; ast.parse(open('bot.py', encoding='utf-8').read())"

# Импорт модуля без запуска main() — ловит name-errors в декораторах
python -c "import importlib.util; s=importlib.util.spec_from_file_location('t','bot.py'); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); print('OK')"
```

### Дебаг через SQLite напрямую

```bash
sqlite3 studybuddy.db "SELECT user_id, current_streak, total_coins FROM users"
sqlite3 studybuddy.db "SELECT * FROM admins"
sqlite3 studybuddy.db "SELECT key, state FROM fsm_storage WHERE state IS NOT NULL"
```

### Логи

`bot.log` — ротация 5 МБ × 5 файлов (~25 МБ потолок). Уровень регулируется
через `LOG_LEVEL` в `.env`. Шум сторонних библиотек (aiogram, aiohttp,
aiosqlite) глушится до WARNING.

Бизнес-события идут как структурированные строки `event.tag key=value`:
`app.start`, `session.complete source=natural|stop|reconcile`, `session.rated`,
`reconcile.summary completed=X resumed=Y broken=Z`, `admin.added`,
`broadcast.done delivered=X failed=Y`, `streak.batch`, и т. д.

---

## Лицензия

Личный проект. Все права защищены. Не для перераспределения.
