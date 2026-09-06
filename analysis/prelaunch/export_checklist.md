# Prelaunch Export Checklist

> Пройти **до публичного запуска**. **Doc sync:** 2026-09-05 (export **20** таблиц, schema v0.8 — сверено с `AnalyticsService.EXPORTABLE_TABLES`). Подтверждает, что PA-инфраструктура готова собирать данные. Таксономия событий — [../../docs/analytics.md](../../docs/analytics.md).

## Автоматическая проверка

```bash
python scripts/pa_verify_export.py
python scripts/pa_verify_export.py --save-baseline   # сохранить prelaunch snapshot
```

Ожидаемый результат: `ALL CHECKS PASSED` (или warnings только по пустой БД).

## Ручная проверка в боте (от имени админа)

### 1. Dashboard
- [ ] `/analytics` открывается, inline-меню работает
- [ ] Все 10 разделов отображаются без ошибок

### 2. Метрики
- [ ] `/dau` — DAU/WAU/MAU + stickiness
- [ ] `/funnel` — activation + event funnel
- [ ] `/activation` — time-to-value
- [ ] `/product_metrics` — subject/mode breakdown
- [ ] `/cohort_stats` — D1/D7/D30
- [ ] `/feature_usage` — 14 фич
- [ ] `/segments` — 5 сегментов + churned
- [ ] `/content_stats` — hardest terms, top tips
- [ ] `/heatmap` — ASCII heatmap
- [ ] `/event_timeline` — лента событий

### 3. Export
- [ ] `/export users` — CSV с header
- [ ] `/export events` — CSV с header
- [ ] `/export all` — ZIP с 20 CSV + metadata.json

### 4. metadata.json
- [ ] `exported_at` — ISO UTC
- [ ] `schema_version` — v0.8
- [ ] `row_counts` — все таблицы
- [ ] `tables` — список имён

## Тестовые пользователи (минимум 2)

Пройди полный flow от имени 2 тестовых аккаунтов и проверь events:

| Действие | Ожидаемый event_name |
|----------|---------------------|
| `/start` (новый) | `user_registered` |
| Запуск таймера 25 мин | `session_started` → `session_completed` |
| Предмет → режим → квиз | `subject_picked`, `mode_picked`, `quiz_answered` |
| Флэш-карта | `flashcard_reviewed` |
| MCQ | `mcq_answered` |
| Совет | `tip_viewed` |
| `/leaderboard` | `leaderboard_viewed` |

После теста:
```bash
python scripts/pa_verify_export.py --db studybuddy.db
```

## Таблицы в export (20 алиасов)

| Alias | Таблица | Для PA |
|-------|---------|--------|
| users | users | cohorts, segments |
| events | events | funnel, heatmap, timeline |
| sessions | study_sessions | engagement, timer |
| quiz | quiz_progress | content, mastery |
| flashcards | flashcard_progress | SM-2, retention |
| mcq | mcq_progress | content |
| tasks | task_progress | content |
| tips_stats | user_tips_stats | feature adoption |
| tips_seen | user_tips_seen | cooldown |
| pet | user_pet | gamification |
| friendships | friendships | social |
| weekly_scores | weekly_scores | leaderboard |

Полный список: `AnalyticsService.EXPORTABLE_TABLES` в [services.py](../../services.py).

## Baseline snapshot

После проверки сохрани prelaunch baseline:

```
analysis/exports/prelaunch/
  export_YYYY-MM-DD.zip
  verify_report.txt
  metadata.json
```

Команда:
```bash
python scripts/pa_verify_export.py --save-baseline
```

## Troubleshooting

| Проблема | Решение |
|----------|---------|
| events пустая | Пройди test flow; проверь event_repo.log в bot.py |
| `/export all` не приходит | Проверь размер ZIP; Telegram limit ~50MB |
| retention = 0% | Нормально для fresh DB; нужны users с age ≥8d |
| DAU events < DAU progress | Нормально; events зависит от hook completeness |

## Sign-off

| Проверка | Дата | Статус |
|----------|------|--------|
| pa_verify_export.py passed | | ⬜ |
| Admin commands OK | | ⬜ |
| Test users flow OK | | ⬜ |
| Baseline saved | | ⬜ |
| product_framework.md filled | | ⬜ |
| acquisition_tracker ready | | ⬜ |
