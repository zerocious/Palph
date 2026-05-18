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

**Прогресс:** 5 из 6 пунктов закрыты (меню 2×2, новости, FAQ, /help, admins→БД,
резюм таймера, study_materials, MCQ, photo tasks, SM-2 флэш-карты). Остался п. 16.

16) [Фича] Полноценный цифровой питомец: 1 дизайн + эмоции + кастомизация + реальные картинки/GIF  
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

2) [Фича] Питомец грустит при пропуске сегодняшней сессии  
Ценность: бриф обещает «sad if no session today» — сейчас настроение зависит только от стрика, обещание не выполняется  
Готовность: get_pet_emotion учитывает has_studied_today (или дату last_session); если сегодня сессий не было — питомец грустный, даже при ненулевом стрике  
Приоритет: Should

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
- `/export <alias>` — CSV-дамп одной таблицы как Telegram-документ (10 алиасов: users, sessions, achievements, quiz, flashcards, mcq, tasks, subject_stats, settings, events)
- `/export all` — ZIP всех 10 таблиц + `metadata.json` (exported_at, schema_version, row_counts). **Killer feature** для Jupyter-анализа: одной командой получаешь reproducible dataset
- `/parse_logs` — ETL `bot.log + bot.log.1..N` → CSV (timestamp/level/event_name/user_id/properties JSON/raw_text). Покрывает historical backfill до того как events table начала писаться

**Event-tracking layer:**
- `events` table — append-only лог каждого значимого действия (14 hook'ов в bot.py); JSON-properties для event-specific полей. Foundation для funnel/cohort/path/time-to-action анализа в pandas.

Реализация: `services.AnalyticsService` + `repository.EventRepository` + `parse_logs.py`. **99 pytest-тестов** в `test_analytics_service.py` (58) + `test_event_repository.py` (15) + `test_log_parser.py` (20) + extras покрывают всю PA-инфраструктуру.

### 🟡 Будущие расширения

(Сейчас секция пуста — все идеи из изначального плана уже ship'нуты. Новые идеи накапливаются в [BACKLOG.md](BACKLOG.md), оттуда переезжают сюда при формализации.)

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
