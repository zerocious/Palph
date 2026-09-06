Сырые идеи без оценки → [BACKLOG.md](BACKLOG.md). Сюда (TODO.md) попадают только размеченные задачи.

**Текущий фокус (2026-09-05):** v0.8 закрыт, пост-v0.8 работа отгружена
(см. секцию ниже). Следующее — контент ОПМ (#1), сбор 30+ дней `events`
для PA-ноутбуков, UX-сокращение навигации
(см. [user-flows.md](user-flows.md)), затем sprint-план к экзамену
([BACKLOG.md](BACKLOG.md)).

**Тесты:** 802 в suite, все зелёные. Требования к тестам —
[docs/testing.md](docs/testing.md). Технический справочник —
[docs/](docs/README.md).

---

## Пост-v0.8 (2026-05-26 … 2026-06-03) ✅ shipped

Работа, отгруженная после закрытия v0.8; в TODO раньше не фиксировалась.

22) [Контент] Математика — этюдный банк задач билета  
Готовность: `task-42`…`task-72` по шести группам билета (`generate_math_etalon_top3_tasks.py`), удалён дубль `task-08`, **36 задач** с полем `hint`; итого **71** text-only задача  
Приоритет: Must — **закрыто**

23) [UX] Подсказка вместо третьей попытки  
Готовность: `MAX_TASK_ATTEMPTS = 2`; после 1-й ошибки — `hint` (если есть), после 2-й — ответ/решение; награды 3 / 2 / 0 🪙  
Приоритет: Should — **закрыто**

24) [Контент] Бухучёт — теория с подсказками  
Готовность: `flashcards.txt` (67 карточек) + `theory-with-hints.txt`; предмет заведён в `SUBJECT_IDS`; фактические ошибки в ответах вычитаны  
Приоритет: Should — **закрыто**

25) [Питомец] Три эмоции + суточный арт  
Готовность: `neutral` / `joy` / `sad` вместо пяти (legacy-имена поддержаны фолбэком); `get_pet_time_period` → `assets/pet/<period>/`; пользовательский арт заменил плейсхолдеры  
Приоритет: Should — **закрыто**

26) [Безопасность] Хардening команд и callback-ввода  
Готовность: аллоулист `subject_id` во всех `fc_*` / `ut_*` / `taskgrp:` хендлерах, `safe_subject_dir` во всех загрузчиках, HTML-экранирование пользовательских строк, явные капы на `/broadcast` / `/reply` / поддержку / поиск друга; ОПМ скрыт из «Подготовки»; аудит [audits/command-input-security-audit.md](audits/command-input-security-audit.md)  
Приоритет: Must — **закрыто**

27) [Инфра] Persistent storage на bothost + i18n-регрессии  
Готовность: автоопределение `/app/data` при пустом `DB_PATH`, тест на сохранность БД при рестарте; восстановлены ключи локалей, потерянные при пересборке бандлов  
Приоритет: Must — **закрыто**

28) [UX] Таймер не сбрасывается при входе в «Подготовку»  
Готовность: `_detach_timer_for_study_flow`; таймер продолжает тикать, пока пользователь учится  
Приоритет: Should — **закрыто**

29) [Док] Технический справочник `docs/` + сверка всех md  
Готовность: 13 документов в [docs/](docs/README.md) (архитектура, модель данных, фичи, аналитика, конфигурация, эксплуатация, безопасность, i18n, контент, тесты, рекомендации, скрипты); все корневые md сверены с кодом; требование к тестам о времени зафиксировано  
Приоритет: Must — **закрыто**

---

Example:

[Тип] Краткий заголовок  
Ценность: зачем это пользователю / продукту  
Критерий готовности: как понять, что сделано  
Приоритет: Must / Should / Could / Won't (на сейчас)

---

## v0.8 — productivity tips (2026-05-22) ✅ shipped

18) [Фича] Советы по продуктивности — контент, UX, геймификация, интеграция  
Ценность: мотивация и onboarding внутри бота (тайм-менеджмент, память, «как пользоваться ботом»); +1🪙/день и ачивка за вовлечённость; совет дня в утреннем напоминании  
Готовность: `tips/*.json` + `tips/README.md`; кэш при старте; inline «Ещё совет» / «Все советы» / пагинация; контекстный pick + cooldown 7д (`user_tips_seen`); `user_tips_stats` + `TipsRepository`; событие `tip_viewed`; ачивка `10_tips_read`; FAQ обновлён; 26 тестов  
Приоритет: Must — **закрыто**

**Known issue — ✅ исправлено 2026-09-05:** пагинация «Все советы» (◀️/▶️) инкрементила `total_views`, что позволяло нафармить ачивку «Любознательный» перелистыванием одной категории. Теперь `handle_tips_list` вызывает рендер с `count_view=False`: листание пишет только `user_tips_seen` (cooldown), не увеличивает `total_views`, не даёт монету, не двигает ачивку и не логирует `tip_viewed`. «🔄 Ещё совет» считается просмотром как раньше. 5 регрессионных тестов в `tests/test_tips_gamification.py`.

---

## UX — сокращение кликов (2026-05-22)

21) [UX] Оптимизация стандартных user flows — **частично shipped 2026-05-25**  
Ценность: меньше кликов до учёбы и видимость рейтинга/друзей  
Готовность: ✅ «❓ Квизы» в главном меню (−1 клик); ✅ рейтинг в профиле; ✅ условные inline карточек. Остаётся: «▶️ Продолжить» (last subject+mode), MCQ returning ≤3 клика  
Приоритет: Could — остаток после контента #1

---

## v0.8 — user flashcards (2026-05-22) ✅ shipped

17) [Фича] Пользовательские флэш-карточки + перестроенный flow учёбы  
Ценность: студент добавляет свои термины по предмету и повторяет их с тем же SM-2, что и официальный контент; не нужно ждать наполнения `flashcards.txt`  
Готовность: таблица `user_flashcards`; CRUD через 📇 Мои карточки; `flashcard_source` (mix/official/own); ❓ Квизы → предмет → режим; прогресс/mastery учитывает свои карты; 16 тестов  
Приоритет: Must — **закрыто**

---

## v0.8 — PA analytics expansion (2026-05-22) ✅ shipped

19) [Аналитика] Сбор данных: events v0.8 + export + indexed columns  
Готовность: hook'и friend/pet/LB/settings/reminder; `events.subject_id/mode/tip_id`; export 20 таблиц (`friend_requests`, `weekly_badges`, `streak_freezes`); admin_commands  
Приоритет: Must — **закрыто**

20) [Аналитика] Продуктовые метрики в боте  
Готовность: `compute_product_metrics()` + `/product_metrics` + `anlt:product`; subject/mode breakdown; strict event funnel; activation по неделям; feature retention D7; morning push→session; leaderboard + notification funnel  
Приоритет: Must — **закрыто**

---

## v0.7 — спринт (2026-05-17)

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
handler). Главные PR-ы: #3 = `258fadc`, #5 = `55e70ec`. На feature-ветке
**732 теста** в suite на момент закрытия фазы (сейчас в suite 802).
Открытой leaderboard-работы нет.

16) [Фича] Полноценный цифровой питомец: 1 дизайн + эмоции + кастомизация + реальные картинки/GIF — **в PR #3 art track shipped 2026-05-19**:

> **Актуальное состояние (2026-09-05):** спека ниже — исторический текст
> задачи. С тех пор эмоций стало **три** (`neutral` / `joy` / `sad`),
> генератор ассетов делает **75 PNG + 3 GIF**, добавлены суточные варианты
> арта (`assets/pet/<period>/`), плейсхолдеры заменены пользовательским
> артом. Текущее поведение — [docs/features.md](docs/features.md) §5.

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

2) [Фича] Питомец грустит при пропуске сегодняшней сессии — **✅ закрыто** (v0.7/v0.8)  
Ценность: бриф «sad if no session today»  
Готовность: `derive_emotion` → `sad` при `has_studied_today=0`; вечернее напоминание с `sad.gif` (PR #5); тесты `test_derive_emotion`, reminder integration  
Приоритет: Should — **done**

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

### ✅ Ship'нуто (доступно через админ-команды)

**Две метрики активности** (см. `admin_commands.md` → «Метрики активности»):
- `activity_progress` — DAU/cohort/segments (progress tables)
- `activity_events` — heatmap/timeline (`events` table)

**Aggregate-метрики:**
- `/cohort_stats` — D1/D7/D30 retention по ISO-неделям регистрации
- `/funnel` — activation funnel + event funnel + step conversion (→%)
- `/activation` — time-to-value (медиана часов до первых событий, 24h/7d)
- `/product_metrics` — subject/mode breakdown, strict funnel, D7 feature retention, morning push, LB, notifications
- `/dau` — DAU/WAU/MAU + stickiness (оба источника активности)
- `/feature_usage` — 14 фич (учёба + v0.8: tips, own cards, pet, friends, LB, flash source)
- `/segments` — 5 сегментов вовлечённости + churned
- `/content_stats` — hardest terms, MCQ, EF, subject visits, official/user cards, top tips
- `/event_timeline [hours]` — лента событий (default 24h)
- `/heatmap [days]` — ASCII heatmap (default 30d)
- `/analytics` — dashboard с inline-меню

**Data export:**
- `/export <alias>` — CSV одной таблицы (**20 алиасов**)
- `/export all` — ZIP + `metadata.json` (`schema_version` v0.8)
- `/parse_logs` — ETL bot.log → CSV (historical backfill)

**Event-tracking:**
- `events` + колонки `subject_id`, `mode`, `tip_id`
- События: учёба, tips, flashcards, friend/pet/LB/settings/reminder

Реализация: `AnalyticsService` + `EventRepository` + `parse_logs.py`.
**110 pytest** в аналитических модулях (`test_analytics_service.py` 69,
`test_event_repository.py` 18, `test_log_parser.py` 20,
`test_pa_verify_export.py` 3) + **802 total** в suite.
Таксономия событий — [docs/analytics.md](docs/analytics.md).

### ✅ PA launch kit (2026-05-25)

- [analysis/README.md](analysis/README.md) — hub, workflow по 3 неделям
- [analysis/product_framework.md](analysis/product_framework.md) — цели, гипотезы, метрики, events
- [analysis/analytics_logbook.md](analysis/analytics_logbook.md) — дневник решений
- `scripts/pa_verify_export.py` — prelaunch verification + baseline
- `scripts/pa_weekly_snapshot.py` — weekly export + markdown summary
- Notebooks 01–04 + week1/week2/week3 templates + case study

### 🟡 Будущие расширения

- `visit_id` для path analysis в одном заходе
- `metrics_daily` pre-aggregate при росте базы

### 🎯 Главный портфолио-asset (внешняя аналитика)

После сбора 30+ дней реальных данных — заполнить case study и ноутбуки:

- `01_cohort_retention.ipynb` — retention heatmap
- `02_activation_funnel.ipynb` — funnel + time-to-value
- `03_feature_adoption.ipynb` — adoption + D7 correlation
- `04_session_patterns.ipynb` — heatmap + timing recommendations
- [analysis/week3/case_study_template.md](analysis/week3/case_study_template.md)

Сейчас в `analysis/`: launch kit + `leaderboard_backtest.ipynb`. User flows:
[user-flows.md](user-flows.md).

Это будет выглядеть в резюме как реальная PA-работа, не как «вот мой Telegram-бот».
