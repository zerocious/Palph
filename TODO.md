Сырые идеи без оценки → [BACKLOG.md](BACKLOG.md). Сюда (TODO.md) попадают только размеченные задачи.

Example:

[Тип] Краткий заголовок  
Ценность: зачем это пользователю / продукту  
Критерий готовности: как понять, что сделано  
Приоритет: Must / Should / Could / Won't (на сейчас)

---

## v0.7 — текущий спринт (2026-05-17)

План: `C:\Users\User\.claude\plans\make-a-new-session-merry-castle.md`
Сессия: см. session_notes.md, запись от 2026-05-17

**Прогресс:** 5 из 6 пунктов закрыты + **пункт 16 полностью отгружен в `main`
к 2026-05-20**:
- **data layer** ✅ PR #2 = `9203aab` (schema + PetRepository +
  derive_emotion + XP-grant в complete_session + 47 тестов)
- **art track** ✅ PR #3 = `258fadc` (render_pet + 125 placeholder PNG +
  5 GIF, Pillow build-script, level-up notification, pet detail screen,
  4-state customization picker, FSM rename)
- **sad-pet reminder text** ✅ PR #4 = `fe69329` (интеграция
  derive_emotion в `ReminderService._send_evening` + 7 тестов)
- **sad-pet GIF attachment + 👥 Друзья в профиле** ✅ PR #5 = `55e70ec`
  (post-v0.8 follow-ups: bot.send_animation с sad.gif + caption +
  graceful FileNotFoundError fallback; кнопка `👥 Друзья` в профиле
  (3 keyboard sites), reusing existing friends_back handler)

Остаётся: **real artwork** (отдельный art-track — file replacement
в `assets/pet/`, без code changes); полностью закрывает TODO #16.

### ✅ Weekly Leaderboard — закрыт полностью

Спек и фазинг: [LEADERBOARD.md](LEADERBOARD.md). **Все 4 фазы shipped
2026-05-19** в PR #3 (Phase 0 audit, Phase 1 data layer, Phase 2a view +
privacy, Phase 2b rollover + rewards, Phase 3 freeze, Phase 4 friends) +
**username-search для /friends** (BACKLOG → ship) +
**deep-link invite-links** через `/share_friend` (BACKLOG → ship) +
**👥 Друзья кнопка в профиле** (post-v0.8 PR #5, reuses friends_back
handler). Главные PR-ы: #3 = `258fadc`, #5 = `55e70ec`. На текущем
`main` (`55e70ec`) **476 тестов** покрывают всю систему (включая
middleware, end-to-end integration flows, reminder service sad-pet
animation). Открытой leaderboard-работы нет.

16) [Фича] Полноценный цифровой питомец: 1 дизайн + эмоции + кастомизация + реальные картинки/GIF — **в PR #3 art track shipped 2026-05-19**:
- `render_pet` + 125 placeholder PNG + 5 GIF (Pillow build-script);
- level-up notification со списком разблокированных предметов;
- pet detail screen с image preview;
- 4-state customization picker (⭐/✓/💰/🔒) для цветов и аксессуаров;
- покупка через confirm dialog (атомарная под db.lock);
- equip уже купленного — instant;
- переименование через FSM.
Real artwork — отдельный art-track (placeholder PNG functional, но programmer-art ugly). Sad-pet image в reminder — отдельный follow-up после merge PR #4.  
Ценность: текущая «эмоция» привязана только к стрику и не персонализирована; полноценный питомец с реальными картинками = эмоциональная привязка → удержание; закрывает [TODO #2] в части «грустит, если сегодня не учился»  
Готовность:  
— Один дизайн питомца (не 5 видов). Таблица `user_pet(user_id, name, color, accessory, level, xp, created_at)` без `species` и без хранения эмоции. Поле `accessory NOT NULL` с sentinel-значением `"none"` (вместо nullable).  
— Вторая таблица `user_pet_inventory(user_id, item_type, item_value, purchased_at, PRIMARY KEY(user_id, item_type, item_value))` — инвентарь купленных предметов. На создании питомца сидится двумя бесплатными дефолтами: `(color, orange)` и `(accessory, none)`.  
— 5 эмоций *выводятся* из состояния пользователя в момент рендера (не хранятся): `studying` (активный таймер) > `excited` (level-up или ачивка за последние 5 мин) > `sad` (`has_studied_today=0`) > `sleepy` (22:00–06:00 локального времени) > `happy` (дефолт).  
— Гибридная кастомизация: имя; цвет (orange ★ free, grey ур.1 / 20🪙, blue/green ур.2 / 40🪙, pink ур.4 / 80🪙); аксессуар (none ★ free, hat ур.1 / 30🪙, glasses ур.3 / 90🪙, scarf ур.5 / 150🪙, crown ур.8 / 240🪙). **Уровень gates видимость**, **монеты gates покупку**. Формула цены: `unlock_level × 20` для цветов, `× 30` для аксессуаров. Всё купить = 690🪙 (~2–3 недели регулярной учёбы). Кнопка «Сменить вид» отсутствует (один дизайн).  
— Picker UX: 4 состояния — ⭐ (надето) / ✓ (куплено, не надето → надеть бесплатно) / 💰 N (доступно к покупке) / 🔒 ур.N · 💰 X (тизер залоченного, видна будущая цена). Покупка: tap 💰 N → preview-фото + confirm dialog «Купить за N 🪙?» → атомарная транзакция под `db.lock` (re-read баланса, deduct, INSERT OR IGNORE в инвентарь, auto-equip). Без рефандов в v1.  
— Уровень: 1 XP/мин учёбы, `level = floor(sqrt(xp/10)) + 1`. На level-up сообщение содержит список *разблокированных-но-непокупленных* предметов с их ценой («🎀 шарф открыт — купи за 150 🪙»), не выдаёт их бесплатно.  
— Ассеты: 5 поз питомца (grayscale, прозрачный фон) + 4 PNG-оверлея аксессуаров → build-script на Pillow генерирует 125 готовых PNG (`<emotion>_<color>_<accessory>.png`) и 5 универсальных GIF (`<emotion>.gif`, 2-кадровый луп) в `assets/pet/`. Pillow — dev-only зависимость, в рантайме бот шлёт уже готовые файлы через `FSInputFile`.  
— Telegram-доставка: `send_photo` + caption для рутинных поверхностей (профиль, превью кастомизации, активный таймер). `send_animation` (GIF) + последующее `send_photo`/`send_message` для двухтактных моментов: level-up (`excited.gif` + кастомный PNG), напоминание «грустный питомец» (`sad.gif` + текст).  
— Код: новый `PetRepository` (`create_pet_with_defaults`, `get_pet`, `get_inventory`, `purchase_item` — атомарная под `db.lock`, `equip` с проверкой ownership, `add_xp`, `rename`), чистая функция `derive_emotion(user, fsm_state, now_local) -> str`, `render_pet(user_pet, emotion, *, animated=False) -> FSInputFile`. UI кастомизации в профиле через инлайн-меню с FSM-state для переименования. Лог `pet.purchase user_id=X type=Y value=Z price=N balance_after=M`.  
Приоритет: Must (центральная фича сессии). Арт-трек отдельный от код-трека — код может быть запущен с placeholder-PNG, пока арт в работе.

---

## Бэклог (до v0.7)

1) [Контент] Заполнить Разделы II–IV для «Основы производственного менеджмента»  
Ценность: пользователь сейчас может пройти только Раздел I; брифу нужны все четыре, иначе квиз ощущается как заглушка  
Готовность: файлы `study_materials/industrial-management/situational/section-{ii,iii,iv}.txt` содержат ≥10 терминов каждый в формате «термин || определение || ключевые слова || ситуация»; кнопки разделов автоматически появятся (already data-driven)  
Приоритет: Should (до публичного запуска)

2) ✅ ~~[Фича] Питомец грустит при пропуске сегодняшней сессии~~ — **shipped**
в PR #2 (`9203aab`, pet data layer) через чистую функцию
`services.derive_emotion(user, fsm_state, now_local)` с sad-path по
`has_studied_today=0`, плюс PR #4 (`fe69329`) подключил её в
`ReminderService._send_evening`. Закрыто 2026-05-19.

5) [Алгоритм] SM-2 для **ситуационных** квизов (для флэш-карт ✅ сделано в v0.7 2026-05-17 — см. session_notes)  
Ценность: бриф упоминает SM-2; сейчас фиксированные интервалы [1,2,4,7] не адаптируются под сложность термина для конкретного пользователя  
Готовность: в `quiz_progress` добавляются `ease_factor`, `interval_days`, `repetitions`; переиспользуется чистая функция `services.sm2_update()` (уже написана); **блокер**: keyword-matching grader даёт почти бинарный сигнал (есть 2+ ключевых слов или нет), а SM-2 требует градиент 0–5. Сначала нужен лучший grader (semantic similarity, или промежуточный «частично знал»), иначе SM-2 деградирует до того же бинарного поведения  
Приоритет: Won't (на сейчас) — фиксированных интервалов хватает для ситуационного режима, пока не появится более качественный grader

6) [Фича] Daily tasks  
Ценность: дополнительный игровой механизм — задание дня с бонусом; в проектном контексте отмечено как «stub exists», но в коде следов нет  
Готовность: новая таблица daily_tasks, кнопка в главном меню, генерация задачи в начале дня, проверка выполнения, начисление бонуса  
Приоритет: Won't (на сейчас) — не входит в MVP-scope из брифа

---

## PA-аналитика (data collection для портфолио)

Контекст: проект используется как booster резюме для роли product analyst intern/junior. Цель — превратить бот в источник данных для realистичного PA-анализа в Jupyter.

### ✅ Ship'нуто (доступно прямо сейчас через админ-команды)

**Aggregate-метрики:**
- `/cohort_stats` — D1/D7/D30 retention по ISO-неделям регистрации
- `/funnel` — activation funnel (6 шагов: registered → started → 5+ sessions → 10+ sessions → 3-day streak → 7-day streak); % от total registered
- `/dau` — DAU / WAU / MAU + stickiness ratio (DAU/MAU) + новые пользователи сегодня
- `/feature_usage` — % adoption per feature: 4 учебных режима + Pomodoro + custom timezone + disabled notifications + custom reminder time
- `/segments` — user segmentation (5 сегментов: never_started / tried / active / power / churned), churned приоритетнее за счёт re-engagement actionable-сигнала
- `/content_stats` — hardest situational terms (low accuracy), most-attempted MCQ, progress coverage, flashcard EF distribution в 4 бакетах
- `/event_timeline [hours]` — лента последних N событий из events table (default 24h, clamp [1, 168])
- `/heatmap [days]` — ASCII-heatmap активности 7×8 (weekday × 3-hour buckets), default 30 дней, peak detection
- `/analytics` — единый dashboard с inline-меню по всем разделам (рекомендуется для удобства)

**Data export:**
- `/export <alias>` — CSV-дамп одной таблицы как Telegram-документ (11 алиасов: users, sessions, achievements, quiz, flashcards, mcq, tasks, subject_stats, settings, events, experiments)
- `/export all` — ZIP всех 11 таблиц + `metadata.json` (exported_at, schema_version, row_counts). **Killer feature** для Jupyter-анализа: одной командой получаешь reproducible dataset
- `/parse_logs` — ETL `bot.log + bot.log.1..N` → CSV (timestamp/level/event_name/user_id/properties JSON/raw_text). Покрывает historical backfill до того как events table начала писаться

**Event-tracking layer:**
- `events` table — append-only лог каждого значимого действия (14 hook'ов в bot.py); JSON-properties для event-specific полей. Foundation для funnel/cohort/path/time-to-action анализа в pandas.

Реализация: `services.AnalyticsService` + `repository.EventRepository` + `parse_logs.py`. **99 pytest-тестов** в `test_analytics_service.py` (58) + `test_event_repository.py` (15) + `test_log_parser.py` (20) + extras покрывают всю PA-инфраструктуру.

### 🟡 Будущие расширения

Сформированы 2026-05-21 как PA-портфолио roadmap. Tier'ы по signal-per-effort
(порядок воспроизводит исходную nightly-планёрку; пункт **#2 user properties via
/start onboarding** намеренно опущен по решению пользователя).

**Tier 1 — high signal для PA-портфолио**

1) ✅ [Аналитика] A/B-тест фреймворк — **framework shipped 2026-05-21**
(текущая сессия). Ship'нуто в этом PR:
— Таблица `experiments(user_id, experiment_name, variant, assigned_at)`
  + composite PK + `idx_experiments_name_variant`.
— `services.compute_variant` (pure SHA256 → variant) + `services.get_variant`
  (cache-aside через `ExperimentRepository`, optional `experiment.assigned`
  event-log).
— `EXPERIMENTS` registry в `services.py` (sentinel `_noop_v1` для smoke).
— `experiments` добавлен в `AnalyticsService.EXPORTABLE_TABLES` → доступен
  через `/export experiments` и `/export all`.
— 22 теста: deterministic, distribution 50/50 ± 5%, three-way split,
  idempotency, multi-experiment isolation, unknown-name KeyError, event
  logged once.

**Что осталось как follow-up (отдельный PR):** sprinkle `get_variant(...)`
на конкретной decision-точке (e.g. `pet_level_in_profile_v1`), записать
variant в `events.properties` для последующих событий пользователя, и
сделать notebook `experiments.ipynb` с retention-curves по variant +
significance test. Framework стоит, decision-сайтов пока нет.

3) ✅ [Аналитика] Reference SQL queries directory — **shipped 2026-05-21**
(текущая сессия). 8 standalone `.sql` файлов в `analysis/queries/` +
`README.md` + smoke-test `tests/test_reference_queries.py` (12 тестов:
существование, parametrized execute против init_db schema, защита от
DML/DDL keywords). #08 (`pre_exam_engagement`) — намеренный stub с
TODO-комментарием, ждёт PA-roadmap #2 (exam_date).

**Tier 2 — data hygiene, «mature data person»**

4) [Аналитика] Event schema documentation
Ценность: bot self-documenting для будущего аналитика (включая future you через
6 месяцев). Маркер «mature data person» в резюме.
Готовность: `docs/events_schema.md` — таблица: event_name | when_fires |
required_properties | optional_properties | example. Покрывает все 14 hook'ов из
`bot.py` + любые добавленные с тех пор. Автоматическая проверка drift через
test, который парсит `EventRepository.log_event(...)` вызовы и сверяет с .md
(опционально).
Приоритет: Should (низкий effort, высокий signal — ~30 минут).

5) [Аналитика] Stable CSV schema contract
Ценность: downstream notebooks знают, что ожидать от `/export all` across
версий. Предотвращает silent breakage notebooks при schema-changes.
Готовность: `analysis/schema_v1.yaml` с column types (`int`, `text`, `iso8601`,
`json`) и meaning для каждой из 10 экспортируемых таблиц. `metadata.json` из
`/export all` уже содержит `schema_version` — здесь её формализуем. При
несовместимых изменениях bump v1 → v2, старая версия yaml остаётся для legacy
notebooks.
Приоритет: Could.

6) [Аналитика] Deploy/version markers в events table
Ценность: correlate metric changes с релизами («D7 dropped 5pp after
2026-05-15 — what shipped?»). Anti-correlation = portfolio gold.
Готовность: на bot startup лог `system.deploy` event в events table с
properties `{version: "<short_hash>", started_at: "<iso>"}`. Hash берётся либо
из `git rev-parse --short HEAD` на build-time (через переменную окружения),
либо из `__version__` константы. Появляется в `/event_timeline` как маркер
«вот тут был деплой». Notebook `release_impact.ipynb` (опционально) — overlay
deploy markers на retention-кривую.
Приоритет: Should.

**Tier 3 — статистический / аналитический polish**

7) [Аналитика] Confidence intervals на /cohort_stats
Ценность: с <100 users point estimates lie. Wilson interval для binomial
retention. Показывает statistical literacy.
Готовность: `/cohort_stats` output расширен: D1/D7/D30 ± 95% CI (Wilson). ~1
час, `scipy.stats.binom.interval` или ручной Wilson. В тестах фиксируется
boundary case (n=0, n=1, p=0, p=1).
Приоритет: Could.

8) [Аналитика] Time-to-event метрики (survival analysis)
Ценность: beyond «did they retain D7» — survival analysis это правильный
фрейминг для understanding engagement timing.
Готовность:
— Метрики в `/dau` или новой команде `/timing`:
  · Time to second session (median + p25/p75)
  · Time from signup to first 25-min session (real engagement signal)
  · Median time-between-sessions per user
— Kaplan-Meier curve в notebook (`survival.ipynb`) с `lifelines` library —
  один-import.
— Тесты на корректность time-delta расчёта (включая timezone edge cases).
Приоритет: Could.

9) [Аналитика] Anonymization helper для shared notebooks
Ценность: можно публиковать notebooks в GitHub-портфолио без утечки real
Telegram user_id'ов (которые linkable к публичным профилям).
Готовность: `analysis/anonymize.py` — функция принимает CSV или DataFrame,
хэширует user_id'ы консистентно (HMAC-SHA256 с `secret_salt` из `.env`, не
коммитится). Notebook'и опционально включают `anonymize_df(df)` step перед
сохранением outputs. README.md в `analysis/` фиксирует правило: «никаких
raw user_id в коммите».
Приоритет: Must (как только хоть один notebook публикуется в GitHub) / Could
(пока notebooks только локально).

**Tier 4 — qualitative signal**

10) [Аналитика/Качественные данные] /feedback команда
Ценность: один Telegram-message → запись в БД. Раз в неделю grep по терминам
(«сложно», «не понимаю», «баг») + pair с analytics («users who say 'сложно'
churn at 2× rate» = real insight, отлично читается в резюме).
Готовность: команда `/feedback <text>` принимает свободный текст → запись в
`user_feedback(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES
users(user_id), text TEXT NOT NULL, sentiment TEXT, created_at TEXT NOT NULL
DEFAULT (datetime('now')))`. `sentiment` nullable (опционально enrich позже —
например, через rules-based scorer или LLM). Админ-команда `/feedback_dump`
для CSV-экспорта. Анти-абуз: rate-limit 1 message в час на user_id.
Приоритет: Could.

11) [Аналитика/Качественные данные] In-bot NPS survey
Ценность: NPS universally recognized метрика в резюме. Конкретное число для
slide deck'а.
Готовность: после 7 active days (`has_studied_today` true 7 раз, не подряд)
fire 1-shot prompt «Насколько вероятно, что ты порекомендуешь Palph другу?
(0–10)». Inline-keyboard 0..10. Запись в `user_nps(user_id INTEGER PRIMARY
KEY REFERENCES users(user_id), score INTEGER NOT NULL CHECK(score BETWEEN 0
AND 10), asked_at TEXT NOT NULL, answered_at TEXT NOT NULL DEFAULT
(datetime('now')))`. Score 9–10 = promoter, 7–8 = passive, 0–6 = detractor.
Команда `/nps` (admin) — текущий NPS + breakdown по сегментам. Один опрос на
пользователя в v1; повторный — отдельная задача.
Приоритет: Could.

### 🎯 Главный портфолио-asset (внешняя аналитика)

После сбора 30+ дней реальных данных:

- Папка `analysis/` в репозитории с Jupyter notebooks
- 4 notebook'а:
  - `01_cohort_retention.ipynb` — matplotlib heatmap retention table + key findings
  - `02_activation_funnel.ipynb` — waterfall chart + drop-off insights
  - `03_feature_adoption.ipynb` — adoption per mode + корреляция с retention
  - `04_session_patterns.ipynb` — heatmap часов × дней недели + рекомендации по timing
- `analysis/README.md` с executive summary + recommendations

Это будет выглядеть в резюме как реальная PA-работа, не как «вот мой Telegram-бот».
