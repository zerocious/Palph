# StudyBuddy

Telegram-бот для формирования регулярных учебных привычек у студентов через
геймификацию (монеты, ачивки, стрики), Pomodoro-таймер, квизы и
цифрового питомца.

**Статус:** MVP в эксплуатации; спринт v0.7 закрыт на 5 из 6 пунктов
(4 учебных режима + SM-2 + админ-CRUD + резюм таймера). Остался только
полноценный цифровой питомец. См. [TODO.md](TODO.md) и
[session_notes.md](session_notes.md).

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
- **Цифровой питомец** (пока простой; настроение по стрику; полный
  переработка с эмоциями/кастомизацией/уровнями — в v0.7 #16)
- **Уведомления**: утро / вечер / стрик / ачивки; включается в ⚙️ Настройки;
  время и часовой пояс настраиваются per-user
- **FAQ + техподдержка**: пользователь пишет в чат → forward всем админам;
  ответ через `/reply <user_id> <текст>`
- **Админ-инструменты**: `/help`, `/broadcast`, `/notif_status`, `/addadmin`,
  `/rmadmin`, `/listadmins`. Подробно — в [admin_commands.md](admin_commands.md).

---

## Архитектура

Слоистая: репозиторий → сервис → бот. Никаких прямых SQL-запросов в
хендлерах.

```
bot.py              # Хендлеры aiogram, FSM-состояния, клавиатуры, mode picker,
                    # 4 учебных режима, main()
db.py               # aiosqlite connection, init_db (схема + индексы + миграции)
repository.py       # UserRepository, SessionRepository, AdminRepository,
                    # FlashcardRepository (только CRUD, без бизнес-логики)
services.py         # AchievementService, StudyService, StreakService,
                    # ReminderService + чистая функция sm2_update() для SM-2
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
через `LOG_LEVEL` в `.env`. Шум сторонних библиотек (aiogram, aiohttp,
aiosqlite) глушится до WARNING.

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
- `streak.batch`, `reminder.morning.dispatched`

---

## Лицензия

Личный проект. Все права защищены. Не для перераспределения.
