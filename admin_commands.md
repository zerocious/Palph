# Admin Commands

Справочник по командам, которые работают только для администраторов
Palph-бота. Обычные пользователи получают `❌ Нет прав.` /
`❌ Команда только для админов.`

> **Doc sync:** 2026-09-05 · сверено с `bot.py` на коммите `0ac30af`:
> задокументированы все 20 админских команд, лишних нет.
> Технический контекст — [docs/analytics.md](docs/analytics.md)
> (что именно считает каждая метрика) и
> [docs/operations.md](docs/operations.md) (runbook).

---

> **Примечание про user-facing команды.** Этот файл документирует
> только админ-команды. Пользовательские слэш-команды
> (`/start`, `/help`, `/stop`, `/progress`, `/pet`, `/leaderboard`,
> `/friends`, `/share_friend`, `/delete_account`, `/cancel`) —
> в [README.md](README.md) и [docs/features.md](docs/features.md). Reply-кнопка **📢 Новости** —
> текст `nav.news_body` + inline «Перейти в канал» (`CHANNEL_URL`); см.
> [user-flows.md](user-flows.md) §12. Навигация по кнопкам и подсчёт
> кликов — в [user-flows.md](user-flows.md). Лидерборд-стек — в
> [LEADERBOARD.md](LEADERBOARD.md).

## Кто считается админом

Доступ определяется функцией `is_admin(user_id)` в [bot.py](bot.py):

```python
return user_id in ADMINS or user_id == MAIN_ADMIN_ID
```

- **`MAIN_ADMIN_ID`** — числовой Telegram ID из `.env`. Главный админ,
  всегда имеет доступ; единственный, кто может управлять списком
  (`/addadmin`, `/rmadmin`, `/listadmins`). Сидится в БД на каждом
  старте, удалить через `/rmadmin` нельзя.
- **`ADMINS`** — in-memory set, заполняется при старте из таблицы
  `admins` в БД. Команды `/addadmin` / `/rmadmin` обновляют и БД, и кеш
  атомарно — рестарт не требуется.

### Как добавить/удалить админа

Используй команды (от имени главного админа):

- `/addadmin <user_id>` — добавить
- `/rmadmin <user_id>` — удалить (главного админа удалить нельзя)
- `/listadmins` — посмотреть всех

Новый админ моментально получает расширенный набор в `/`-пикере Telegram
(через `BotCommandScopeChat`): пользовательские команды плюс `/help`,
`/reply`, `/broadcast`, `/notif_status`, весь PA-блок и команды главного
админа. При удалении пикер возвращается к дефолтному набору
(`commands_for_locale`: `/start`, `/stop`, `/progress`, `/pet`,
`/leaderboard`, `/friends`, `/share_friend`, `/delete_account`).

Команды главного админа (`/addadmin`, `/rmadmin`, `/listadmins`,
`/backup`) видны в пикере всем админам, но проверка внутри сверяет
`user_id` именно с `MAIN_ADMIN_ID` — обычный админ получит отказ.

> Файл `admins.json` (legacy) при первом старте импортируется в БД и
> переименовывается в `admins.json.migrated`. Повторные запуски — no-op.

---

## Команды

### `/reply <user_id> <текст>`

Отправляет указанному пользователю сообщение от имени бота.
Используется как ответ на обращение в техподдержку — пользователь видит
префикс `📨 Ответ от администратора:`.

**Пример:**
```
/reply 123456789 Привет! Перезапустил бота, попробуй ещё раз.
```

**Поведение:**
- ✅ → админу приходит `✅ Ответ отправлен пользователю 123456789`
- ❌ если `<user_id>` не число → `❌ Неверный ID.`
- ❌ если сообщение не доставлено (заблокирован бот, удалённый
  аккаунт) → `❌ Ошибка: <текст исключения>`

**Откуда брать `user_id`:** когда пользователь пишет в бот, его
сообщение пересылается всем админам в формате
`📩 Новое сообщение от <Имя> (ID: <user_id>): <текст>` — берёшь ID оттуда.

---

### `/broadcast <текст>`

Рассылает сообщение всем зарегистрированным пользователям бота.
Защита от двойного запуска: пока одна рассылка идёт, вторая не
стартует, админ получит `⚠️ Рассылка уже идёт. Дождись её завершения.`

**Пример:**
```
/broadcast 🛠 Уведомление: ночью бот будет на 5 минут недоступен из-за обновления. Стрики не пострадают.
```

**Поведение:**
- Стартовое сообщение админу: `📣 Начинаю рассылку для N пользователей…`
- Сообщения отправляются по одному с задержкой 0.05с (защита от
  лимита Telegram ~30 msg/s)
- Финальный отчёт админу:
  ```
  ✅ Рассылка завершена.
  📨 Доставлено: X
  ❌ Не доставлено: Y

  ID с ошибкой: 111, 222, 333… (первые 10)
  ```
- Без аргументов → подсказка по использованию
- Пустая база → `📭 В базе нет пользователей.`

**Что отправляется как plain text** — Markdown/HTML не парсится, поэтому
можно безопасно включать `*`, `_`, `<` без эскейпинга.

**Скорость:** ~5 секунд на 100 пользователей, ~50 секунд на 1000.

---

### `/notif_status`

Диагностическая команда. Показывает текущее состояние твоих собственных
настроек уведомлений и часового пояса, плюс симулирует, попадёшь ли ты в
выборку шедулера при срабатывании сохранённого `morning_time` /
`evening_time`.

**Пример вывода:**
```
📊 Диагностика уведомлений

👤 user_id: 123456789
🌍 timezone: Europe/Moscow
🕐 сейчас в твоём TZ: 18:27:43
🇷🇺 сейчас в Москве:    18:27:43

🌅 утро:  enabled=1, time=09:00
🌙 вечер: enabled=1, time=21:00

📡 шедулер обходит TZ: Europe/Moscow

🧪 при срабатывании 09:00 в твоём TZ: 1 пользователь(ей), включая тебя — ✅ да
🧪 при срабатывании 21:00 в твоём TZ: 1 пользователь(ей), включая тебя — ✅ да
```

**Когда использовать:**
- «Не пришло утреннее напоминание» → проверить `enabled` и `time`
- «Время в боте странное» → сверить `🕐 в твоём TZ` с реальным временем
- «А правда ли шедулер видит мой TZ?» → строка `📡 шедулер обходит TZ`

**Ключевые сигналы в выводе:**
| Сигнал | Значение |
|---|---|
| `timezone` ≠ ожидаемого | TZ был случайно изменён в ⚙️ Настройки → 🌍 |
| `enabled=0` | Слот выключен; ⚙️ Настройки → нажми переключатель |
| `включая тебя — ❌ НЕТ` | Запрос исключает тебя (обычно из-за `enabled=0`) |
| Предупреждение `has_studied_today=1` | Вечернее напоминание сегодня не придёт **по дизайну** |

---

### `/addadmin <user_id>` 👑 главный админ

Добавляет пользователя в админы. Запись идёт в таблицу `admins`, кеш
`ADMINS` обновляется тут же, новому админу немедленно расширяется
/-пикер Telegram.

**Пример:**
```
/addadmin 123456789
```

**Поведение:**
- ✅ → `Пользователь 123456789 добавлен в админы.`
- ℹ️ если уже админ → `Пользователь 123456789 уже админ.`
- ❌ если не главный админ → `Только главный админ может управлять списком админов.`

---

### `/rmadmin <user_id>` 👑 главный админ

Удаляет пользователя из админов. Команды в /-пикере возвращаются к
дефолтным.

**Поведение:**
- ✅ → `Пользователь 123456789 удалён из админов.`
- ℹ️ если не был админом → `Пользователь 123456789 и так не админ.`
- ❌ попытка удалить главного → `Нельзя удалить главного админа.`

---

### `/listadmins` 👑 главный админ

Список всех админов из БД. Главный отмечен `★ главный`.

---

### `/backup` 👑 главный админ

Принудительный snapshot БД через SQLite `VACUUM INTO` — атомарно,
без WAL-мусора. Имя файла: `studybuddy-manual-YYYY-MM-DD-HHMMSS.db`.

**Когда использовать:**
- Перед опасной миграцией / разворотом схемы
- Перед массовой админ-операцией (broadcast, очистка)
- Для скачивания снимка на локал (через scp с VPS / docker cp)

**Daily backup** работает автоматически — раз в день после streak processing
(один файл `studybuddy-YYYY-MM-DD.db` на глобальный день). Retention —
30 дней (`BACKUP_RETENTION_DAYS` env, можно увеличить). Manual snapshot'ы
**не подпадают** под daily-retention — хранятся до явного удаления.

**Где лежат:** `BACKUP_DIR` env (локально default `./backups/`; в Docker и на bothost — **`/app/data/backups/`**, задаётся в Dockerfile и docker-compose; путь `/data/backups` из ранних заметок устарел).

**Поведение:**
- ✅ → `Backup создан: studybuddy-manual-... ; Размер: N KB`
- ❌ → `Backup failed — посмотри в логи` (см. событие `backup.force_failed`)

---

## 📊 PA-аналитика

Все аналитические команды доступны любому админу (не только главному).

### `/analytics` — единый dashboard (рекомендуется)

Открывает interactive-меню с inline-кнопками. Удобнее, чем помнить
отдельные slash-команды.

**Главный экран (кнопки):**
- 🔁 Cohort retention → `/cohort_stats`
- 🎯 Activation funnel → `/funnel`
- ⏱️ Time-to-value → `/activation`
- 📈 Product metrics → `/product_metrics`
- 👥 Active users (DAU/WAU/MAU) → `/dau`
- 🎮 Feature adoption → `/feature_usage`
- 🧑‍🤝‍🧑 User segments → `/segments`
- 📚 Content stats → `/content_stats`
- 📜 Event timeline (24h) → `/event_timeline`
- 📅 Activity heatmap (30d) → `/heatmap`
- 📦 Export CSV →
- ✖️ Закрыть

Тап на раздел → message edits к подробному виду + `[◀️ К аналитике]`.
Закрыть → бот удаляет message (или редактирует в «закрыто», если delete
недоступен).

**📦 Export CSV →** подменю: **📦📦 ALL tables (ZIP)** + **20 алиасов**
по одной таблице. Тап шлёт CSV или ZIP отдельным документом.

### `/cohort_stats` — retention D1/D7/D30 по когортам

ASCII-таблица retention по ISO-неделям регистрации:

```
Cohort     | Size | D1     | D7     | D30
2026-W18   |    1 | 100.0% | 100.0% |   0.0%
2026-W19   |    4 |  75.0% |  50.0% |     —
2026-W20   |    8 |  87.5% |     — |     —
```

«—» = когорта моложе N дней (нет данных). D_N = strict: ровно в день
`signup + N`.

### Метрики активности (две, не смешивать)

| Метрика | Ключ в API | Источник | Где используется |
|---------|------------|----------|------------------|
| **activity_progress** | `dau`, `wau`, `mau`, `stickiness` | UNION timestamp из progress/sessions (см. `AnalyticsService._all_activity_dates_per_user`) | `/cohort_stats`, `/segments`, блок progress в `/dau` |
| **activity_events** | `dau_events`, `wau_events`, `mau_events`, `stickiness_events` | Любая строка в `events` с `user_id` | `/heatmap`, `/event_timeline`, блок events в `/dau` |

**Почему два числа:** progress фиксирует учебное действие с записью в БД; events — всё, что залогировал hook (может быть уже, а может ещё не быть для новой фичи). Progress часто **выше** (визит в предмет без event). Events **ниже**, если hook неполный, или **выше**, если много служебных событий на пользователя.

Справочник в коде: `services.ACTIVITY_METRIC_DEFINITIONS`, `AnalyticsService.get_activity_metric_definitions()`.

### `/funnel` — activation funnel

Два блока: **progress+events steps** и **event-only funnel**. % от total registered; колонка `→N%` — conversion от **предыдущего** шага. ASCII bar:

Шаги первого блока (`compute_funnel().steps`), в порядке вывода:

```
Registered                          ██████████ 100.0% (18)
Started studying (≥1 session)       ████████░░  77.8% (14)
Picked subject (events)             ███████░░░  66.7% (12)
Picked mode (events)                ██████░░░░  61.1% (11)
≥1 quiz answer (events)             █████░░░░░  50.0% (9)
≥1 flashcard review (events)        ████░░░░░░  38.9% (7)
≥1 productivity tip (events)        ███░░░░░░░  33.3% (6)
Reached 5+ sessions                 █████░░░░░  44.4% (8)
Reached 10+ sessions                ███░░░░░░░  16.7% (3)
Earned 3-day streak achievement     ████░░░░░░  27.8% (5)
Earned 7-day streak achievement     █░░░░░░░░░   5.6% (1)
```

Второй блок (`event_steps`) — чисто событийный, по именам событий:
`Registered`, `session_started`, `subject_picked`, `mode_picked`,
`quiz_answered`, `flashcard_reviewed`, `tip_viewed`.

Шаги — не strict-subsets (3-day streak ≠ subset 10+ sessions),
поэтому пропорции могут не убывать монотонно. Это intentionally honest.
Строгая (монотонная) воронка есть отдельно — в `/product_metrics`.

### `/dau` — DAU / WAU / MAU + stickiness

Два блока в одном отчёте — **activity_progress** и **activity_events** (см. таблицу выше). Пример:

```
activity_progress (progress tables):
  DAU: 3    WAU: 12    MAU: 18    Stickiness: 16.7%
activity_events (events table):
  DAU: 2    WAU: 10    MAU: 15    Stickiness: 13.3%
```

Stickiness ≥20% — типичный benchmark для consumer apps.

### `/activation` — time-to-value

Медиана и p75 **часов от signup** до первого события (`session_started`, `subject_picked`, `mode_picked`, `quiz_answered`, `flashcard_reviewed`, `tip_viewed`). Плюс доля registered с первой сессией в **24h** и **7d** (по `events`, не по `total_sessions`).

### `/product_metrics` — продуктовые метрики

Один отчёт, несколько блоков:

| Блок | Смысл |
|------|--------|
| **By subject** | `subject_picked` → `mode_picked` → `quiz_answered` per `subject_id` |
| **By mode** | Уникальные пользователи per `mode` |
| **Strict event funnel** | Пользователи, у которых *ever* были все шаги 1..k подряд по списку событий (монотонное падение count) |
| **Activation by week** | Медиана часов до первой сессии и % в 24h по ISO-неделе регистрации |
| **Feature retention D7** | Активность на `signup+7` (activity_progress): WITH vs WITHOUT tips / own cards / pet / friends |
| **Morning push** | `reminder_sent(morning)` и `session_started` в один календарный день |
| **Leaderboard** | weekly_scores, просмотры, hidden, покупки freeze |
| **Notification funnel** | registered → reminders ON → push sent (events) → session same day |

Также в `/analytics` → **📈 Product metrics**.

### `/feature_usage` — % adoption per feature

Учебные режимы, Pomodoro, настройки, **v0.8:** свои флэш-карточки, советы, питомец, друзья, weekly leaderboard, источник карт ≠ mix.

Всего **14** строк, в порядке вывода (`compute_feature_usage`):

```
🎯 Situational quizzes (≥1 ответ)    ████░░░░░░  44.4% (8)
🃏 Flashcards (≥1 ревью)             ███░░░░░░░  33.3% (6)
❓ MCQ (≥1 ответ)                    ██░░░░░░░░  22.2% (4)
📷 Photo tasks (≥1 попытка)          █░░░░░░░░░  11.1% (2)
⏱️ Pomodoro (≥1 сессия)              ████████░░  77.8% (14)
🌍 Изменил часовой пояс              ██░░░░░░░░  22.2% (4)
🔕 Отключил хотя бы одно уведомление █░░░░░░░░░  11.1% (2)
⏰ Изменил время напоминаний          █░░░░░░░░░  11.1% (2)
📇 Свои флэш-карточки (≥1)           ██░░░░░░░░  16.7% (3)
🎓 Советы (≥1 просмотр)              ████░░░░░░  38.9% (7)
🐾 Питомец создан                    ███████░░░  72.2% (13)
👥 ≥1 друг                           ██░░░░░░░░  16.7% (3)
🏆 Weekly leaderboard (≥1 неделя)    █████░░░░░  50.0% (9)
🃏 Источник карт ≠ mix               █░░░░░░░░░   5.6% (1)
```

### `/segments` — user segmentation

5-уровневая сегментация по вовлечённости:

```
Never started (0 sessions)    ██░░░░░░░░  22.2% (4)
Tried (1-2 sessions)           ███░░░░░░░  33.3% (6)
Active (3-9 sessions)          ███░░░░░░░  27.8% (5)
Power (≥10 sessions)           █░░░░░░░░░   5.6% (1)
Churned (>14d inactive)        █░░░░░░░░░  11.1% (2)
```

**Логика приоритезации**: `churned` приоритетнее категории по сессиям. Active user, не заходивший >14 дней — попадает в `churned` (re-engagement actionable важнее статичной метрики). `never_started` exempt — нечего re-engage без истории.

### `/content_stats` — что в контенте работает / не работает

Sub-view'ы:

- 🎯 **Hardest situational terms** — top-5 по низкой accuracy.
- ❓ **Most-attempted MCQ** — top-5 по объёму попыток.
- 📚 **Progress coverage** — unique items per mode.
- 🃏 **Flashcard EF distribution** — 4 бакета SM-2.
- 🃏 **Official vs user** — split по `card_hash LIKE 'u%'`.
- 📚 **Subject engagement** — visits по `user_subject_stats.subject_id`.
- 🎓 **Top tips** — агрегат `tip_viewed` из `events` по `tip_id`.

### `/event_timeline [hours]` — лента последних событий

Источник: `events` table. Использование:

```
/event_timeline           → последние 24 часа (default)
/event_timeline 48        → последние 48 часов
/event_timeline 168       → неделя (clamp max)
```

Limit: 50 событий. Формат строки: `HH:MM u=<user_id> event_name key1=v1 key2=v2`. Длинные values обрезаются.

**События v0.8+ (соц / питомец / настройки / push):**
- `friend_request_sent`, `friend_accepted`, `friend_removed`
- `leaderboard_viewed`, `freeze_purchased`, `leaderboard_privacy_toggled`
- `pet_purchased`, `pet_equipped`, `pet_renamed`
- `settings_changed` — `setting`, `value` (timezone, notifications, flashcard_source, …)
- `reminder_sent` — `kind` (morning/evening), `tz`, `hhmm`
- `tip_viewed`, `user_flashcard_created` / `user_flashcard_deleted`

**Колонки `events`:** `subject_id`, `mode`, `tip_id` — дублируют частые ключи из `properties` для SQL.

Совет дня в утреннем push **не** пишет `tip_viewed` (by design).

### `/heatmap [days]` — heatmap активности

ASCII-сетка 7×8 (weekday × 3-часовые бакеты), intensity через Unicode block characters:

```
      00 03 06 09 12 15 18 21
Mon:  ·  ·  ▁  ▃  ▅  █  ▅  ▂
Tue:  ·  ·  ▂  ▄  ▆  █  ▆  ▃
...
Total events: 1247 over 30 days
Peak: Mon 15:00-17:59 (78 events)
```

Default 30 дней, clamp [1, 365]. Server time. Использует events table → не сработает для исторических дней без events (нужен `/parse_logs` для backfill).

### `/export <alias>` — CSV-дамп одной таблицы

Отправляет CSV-файл как Telegram-документ. Использование:

```
/export users        → users-2026-05-18.csv
/export sessions     → study_sessions-2026-05-18.csv
/export flashcards   → flashcard_progress-2026-05-18.csv
/export events       → events-2026-05-18.csv
```

`/export` без аргумента → список доступных алиасов.

**Алиасы (v0.8, 20 таблиц):**  
`users`, `sessions`, `achievements`, `quiz`, `flashcards`, `mcq`, `tasks`,
`subject_stats`, `settings`, `events`, `user_flashcards`, `tips_stats`,
`tips_seen`, `pet`, `pet_inventory`, `friendships`, `friend_requests`,
`weekly_scores`, `weekly_badges`, `streak_freezes`.

### `/export all` — ZIP всего dataset'а + metadata.json

Bundle всех exportable таблиц + `metadata.json` (`schema_version`: **v0.8**):

```
palph-export-2026-05-18.zip
├── users.csv
├── study_sessions.csv
├── ... (все 20 таблиц, см. список алиасов выше)
├── events.csv
└── metadata.json   {exported_at, schema_version: "v0.8", row_counts, tables}
```

**Killer feature для PA-портфолио:** `unzip` + `pd.read_csv(...)` × N → анализируй любую корреляцию между таблицами в Jupyter одним loop'ом. `metadata.json` фиксирует timestamp и row-counts для воспроизводимости.

### `/parse_logs` — ETL bot.log → events CSV

Парсит `bot.log` + ротированные `bot.log.1..N` в CSV с колонками `timestamp, level, event_name, user_id, properties (JSON), raw_text`. Используется когда нужны события **до** того как `events` table начала писаться — данные уже есть в логах структурированно (`logger.info("session.complete user_id=X ...")`), нужен только парсер.

Также доступен как CLI: `python parse_logs.py [logs...] -o output.csv`.

---

## Что админ видит без команд

### Пересылка обращений в поддержку

Когда обычный пользователь пишет любое сообщение боту (вне FSM-флоу),
оно:
1. Записывается в `messages.log` (append-only JSONL) как аудит
2. Пересылается всем админам в формате
   `📩 Новое сообщение от <Имя> (ID: <user_id>): <текст>`
3. Пользователь получает подтверждение
   `✅ Твое сообщение отправлено! Администратор ответит в ближайшее время.`

Админ отвечает через `/reply <user_id> <текст>`.

> Сообщения **самого админа** в этот канал не попадают: для админа
> catch-all handler выводит подсказку «Используйте /reply для ответа
> пользователям» и не пересылает.

---

## Шорткаты для дебага через `sqlite3 studybuddy.db`

Если `/notif_status` не дал ответа — лезть в БД напрямую:

```sql
-- Все пользователи и их TZ
SELECT user_id, timezone, has_studied_today, current_streak, total_sessions
FROM users;

-- Настройки уведомлений одного пользователя
SELECT * FROM notification_settings WHERE user_id = 123456789;

-- Последние оценки сессий
SELECT id, user_id, duration_minutes, coins_earned, score, created_at
FROM study_sessions
ORDER BY id DESC LIMIT 20;

-- Текущее FSM-состояние пользователя
SELECT key, state, data FROM fsm_storage WHERE key LIKE '%:123456789:%';
```
