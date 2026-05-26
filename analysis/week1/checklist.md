# Week 1 Checklist — Activation

> Palph v0.8 · doc sync 2026-05-25

## День 0 (prelaunch)
- [ ] `python scripts/pa_verify_export.py --save-baseline`
- [ ] 2 test-user flow → events populated
- [ ] [export_checklist.md](../prelaunch/export_checklist.md) signed off
- [ ] [acquisition_tracker.md](../acquisition_tracker.md) — ≥3 поста готовы

## Ежедневно (Дни 1–7)
- [ ] `/dau` — записать DAU/WAU/new_today
- [ ] `/funnel` — записать top drop-off
- [ ] `/activation` — median hours + % 24h
- [ ] Запись в [analytics_logbook.md](../analytics_logbook.md)
- [ ] Собрать ≥1 фидбек (чат/комментарий/личное)

## День 3 (mid-week)
- [ ] Сравнить каналы в acquisition_tracker
- [ ] Проверить event_timeline — events логируются?
- [ ] Первые гипотезы: H1, H2 status update

## День 7 (end of week)
- [ ] `python scripts/pa_weekly_snapshot.py --week 1`
- [ ] Прогнать [02_activation_funnel.ipynb](../02_activation_funnel.ipynb)
- [ ] Заполнить [report_template.md](report_template.md)
- [ ] 3–5 наблюдений + 2–3 гипотезы на Week 2

## Метрики — зафиксировать

| Метрика | День 1 | День 3 | День 7 |
|---------|--------|--------|--------|
| Total users | | | |
| New today (avg) | | | |
| Activation 24h | | | |
| Median hours to session | | | |
| Top funnel drop-off | | | |
| First mode chosen | | | |

## Критерий успеха Week 1

- [ ] ≥15 регистраций
- [ ] Activation funnel задокументирован
- [ ] Главный drop-off идентифицирован
- [ ] Weekly report заполнен
