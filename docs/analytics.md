# Аналитика: события, метрики, экспорт

> **Doc sync:** 2026-09-05 · **30** имён событий · **20** экспортируемых таблиц.

Аналитический контур состоит из четырёх слоёв: `EventRepository` пишет
события → `AnalyticsService` считает метрики → админ-команды рендерят их в
Telegram → `/export` отдаёт CSV/ZIP для Jupyter (`analysis/`).

## Как логируются события

```python
await event_repo.log(user_id, "session_completed", {"duration": 25, ...})
```

- Таблица `events`: `user_id`, `event_name`, `properties` (JSON),
  `created_at` + денормализованные `subject_id` / `mode` / `tip_id`.
  Последние три извлекаются автоматически из `properties`, если не переданы
  явно (`_resolve_event_dimensions`), и обрезаются до 128/64/64 символов.
- **`log()` никогда не бросает исключение.** Любая ошибка гасится в
  `WARNING events.log_failed` — аналитика не имеет права уронить бизнес-логику.
- Append-only: никаких UPDATE/DELETE, кроме удаления аккаунта.

## Таксономия событий

### Жизненный цикл и учёба

| Событие | Ключевые properties |
|---------|---------------------|
| `user_registered` | `language_code` |
| `session_started` | `duration`, `kind` (`standard` / `custom`) |
| `session_completed` | `duration`, `coins`, `bonus_coins`, `session_id`, `achievements_earned`, `source` (`natural` / `stop` / `reconcile`) |
| `achievement_unlocked` | `achievement_id` |
| `subject_picked` | `subject_id` |
| `mode_picked` | `mode_id`, `subject_id` |

### Ответы на задания

| Событие | Ключевые properties |
|---------|---------------------|
| `quiz_answered` | `subject_id`, `section`, `term_hash`, `is_correct`, `streak_after` |
| `mcq_answered` | `subject_id`, `question_hash`, `is_correct`, `question_index` |
| `flashcard_reviewed` | `subject_id`, `card_hash`, `quality`, `is_new` |
| `task_attempted` | `subject_id`, `task_id`, `attempts_used`, `succeeded`, `coins` |

### Пользовательский контент

| Событие | Ключевые properties |
|---------|---------------------|
| `user_flashcard_created` | `subject_id`, `card_id` |
| `user_flashcard_deleted` | `subject_id`, `card_id` |
| `user_tasks_imported` | `subject_id`, `count` |
| `user_task_deleted` | `subject_id`, `task_db_id` |

### Социальное и соревновательное

| Событие | Ключевые properties |
|---------|---------------------|
| `friend_request_sent` | `target_user_id` (единственное событие, где поле называется так, а не `other_user_id`) |
| `friend_accepted` | `other_user_id`, `source` (в т.ч. `invite_link`) |
| `friend_removed` | `other_user_id` |
| `leaderboard_viewed` | `source` (`profile` / пусто = команда) |
| `leaderboard_privacy_toggled` | `hidden` |
| `freeze_purchased` | `cost`, `streak` |

### Питомец, советы, настройки, напоминания

| Событие | Ключевые properties |
|---------|---------------------|
| `pet_purchased` | `item_type`, `item_value` |
| `pet_equipped` | `item_type`, `item_value` |
| `pet_renamed` | `name` (обрезано до 20 символов — единственное событие с пользовательским текстом) |
| `tip_viewed` | `category`, `tip_id`, `total_views`, `coin_granted`. Пишется только на реальный просмотр (категория или «🔄 Ещё совет»); листание «📋 Все советы» события не создаёт |
| `settings_changed` | `setting`, `value` |
| `reminder_sent` | `kind` (`morning` / `evening`), `tz`, `hhmm` |

### Спринт-план (пишутся только при `PLAN_UI_ENABLED = True`)

`plan_started`, `plan_item_completed`, `plan_day_completed`, `plan_regenerated`.

## Две метрики активности — не путать

`AnalyticsService.ACTIVITY_METRIC_DEFINITIONS` намеренно разводит два
определения, потому что они дают разные числа и разные выводы:

| Метрика | Ключ | Источники | Что считает |
|---------|------|-----------|-------------|
| `activity_progress` | `dau` / `wau` / `mau` | `study_sessions.created_at`, `user_subject_stats.last_activity`, `quiz_progress.last_attempt`, `flashcard_progress.last_review`, `mcq_progress.last_attempt`, `task_progress.last_attempt` | Учебное действие, оставившее след в progress-таблицах. Заход в предмет без ответа тоже считается, поэтому обычно **выше** events |
| `activity_events` | `dau_events` / `wau_events` / `mau_events` | `events.created_at` (любое имя события, `user_id NOT NULL`) | Любое залогированное событие. Зависит от полноты хуков; до появления таблицы `events` — пусто, поэтому обычно **ниже** progress |

`activity_progress` используется в cohort retention, DAU/WAU/MAU и сегментах;
`activity_events` — в DAU/WAU/MAU, heatmap и timeline. `/dau` показывает обе.
Справочник отдаёт `AnalyticsService.get_activity_metric_definitions()`.
При обсуждении цифр всегда уточняйте, какая метрика имеется в виду.

## Что умеет `AnalyticsService`

| Метод | Команда | Что считает |
|-------|---------|-------------|
| `compute_cohort_retention()` | `/cohort_stats` | Retention D1/D7/D30 по недельным когортам |
| `compute_funnel()` | `/funnel` | Activation funnel: регистрация → первая сессия → ачивка → возврат |
| `compute_activation_metrics()` | `/activation` | Time-to-value: медиана и перцентили до первой сессии / первой фичи |
| `compute_product_metrics()` | `/product_metrics` | Разрезы subject/mode, feature retention D7, конверсия «утренний пуш → сессия», лидерборд |
| `compute_engagement()` | `/dau` | DAU / WAU / MAU + stickiness (обе метрики активности) |
| `compute_feature_usage()` | `/feature_usage` | % adoption по фичам |
| `compute_segments()` | `/segments` | 5 сегментов, first match wins: `never_started` (0 сессий) → `churned` (нет активности 14 дней) → `power` (≥ 10 сессий) → `active` (3–9) → `tried` (1–2) |
| `compute_content_stats()` | `/content_stats` | Самые сложные термины, популярные MCQ, распределение EF |
| `compute_event_timeline()` | `/event_timeline [hours]` | Лента последних событий |
| `compute_heatmap()` | `/heatmap [days]` | Активность по часам × дням недели |
| `export_table_csv()` | `/export <alias>` | CSV одной таблицы |
| `export_all_tables_zip()` | `/export all` | ZIP всех таблиц + `metadata.json` |

Все методы возвращают **структуры данных**, а не строки — рендер в текст
живёт отдельно (`_render_*` в `bot.py`), чтобы одни и те же цифры можно было
отдать и в Telegram, и в CSV, и в ноутбук.

`/analytics` — единый дашборд с inline-меню, из которого доступно всё
вышеперечисленное; отдельные команды остались для быстрых запросов.

## Экспорт

Имя таблицы нельзя параметризовать в SQL, поэтому `/export` работает по
**белому списку** `EXPORTABLE_TABLES` (алиас → реальная таблица), 20 записей:

`users`, `sessions`, `achievements`, `quiz`, `flashcards`, `mcq`, `tasks`,
`subject_stats`, `settings`, `events`, `user_flashcards`, `tips_stats`,
`tips_seen`, `pet`, `pet_inventory`, `friendships`, `friend_requests`,
`weekly_scores`, `weekly_badges`, `streak_freezes`.

`/export all` кладёт в ZIP все 20 CSV плюс `metadata.json` (версия схемы,
таймстемп, число строк по таблицам). Выгрузки складываются в
`analysis/exports/<week>/` и **не коммитятся** (`.gitignore` исключает
`*.zip` и `*.csv` в этой папке) — это персональные данные.

## ETL из логов

`/parse_logs` (и `python parse_logs.py`) разбирает `bot.log` по конвенции
`тег key=value` и выдаёт CSV событий. Нужен для восстановления истории до
того, как таблица `events` появилась, и для сверки «лог против БД».

## Ноутбуки

`analysis/*.ipynb` (01 — cohort retention, 02 — activation funnel,
03 — feature adoption, 04 — session patterns) читают выгрузки через
`analysis/lib/pa_loaders.py`, рисуют через `pa_charts.py`.
Подробности — [../analysis/README.md](../analysis/README.md).

Вспомогательные скрипты: `scripts/pa_verify_export.py` (проверка, что
аналитика вообще работает — прогонять до запуска) и
`scripts/pa_weekly_snapshot.py` (еженедельный снимок + markdown-сводка).

## Приватность

События содержат идентификаторы и метаданные действий. Пользовательского
текста в них нет — ни ответов на квизы, ни сообщений в поддержку — **за
единственным исключением**: `pet_renamed` сохраняет введённое имя питомца,
обрезанное до 20 символов. Это осознанно (имя питомца не относится к
чувствительным данным и нужно для анализа кастомизации), но при добавлении
новых событий правило остаётся прежним: пользовательский текст в
`properties` не кладём. То же для `bot.log`. См. [security.md](security.md)
и [../PRIVACY.ru.md](../PRIVACY.ru.md).
