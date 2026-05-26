# Week 1 — Activation Funnel

> Palph v0.8 · doc sync 2026-05-25 · [analysis/README.md](../README.md)

**Цель:** понять, доходят ли пользователи до первой ценности.

**Даты:** ___ → ___

## Ежедневный ритм

| Время | Действие | Команда / файл |
|-------|----------|----------------|
| Утро | Записать план дня | [analytics_logbook.md](../analytics_logbook.md) |
| День | Проверить воронку | `/funnel`, `/activation` |
| День | Engagement | `/dau` |
| Вечер | Записать наблюдения | analytics_logbook |
| Воскресенье | Weekly snapshot | `python scripts/pa_weekly_snapshot.py --week 1` |

## Чеклист

См. [checklist.md](checklist.md)

## Отчёт

Заполнить [report_template.md](report_template.md) в конце недели.

## Ноутбуки

- [02_activation_funnel.ipynb](../02_activation_funnel.ipynb) — графики воронки и time-to-value

## Ключевые вопросы недели

1. Сколько пользователей активируются в первые 24 часа?
2. Какой шаг воронки — главный drop-off?
3. Какой режим выбирают первым: таймер, карточки, квиз, советы?
4. Сколько кликов до первого полезного действия? (см. [user-flows.md](../../user-flows.md))
5. Какой канал привлечения даёт лучший activation?

## Быстрые UX-рекомендации (если видишь проблему)

| Наблюдение | Рекомендация | Приоритет |
|------------|--------------|-----------|
| Drop-off после /start | Упростить onboarding, добавить «Начать сразу» | P0 |
| Мало session_started | Продвинуть таймер в постах и FAQ | P1 |
| Drop-off subject→mode | Кнопка «Продолжить» last subject+mode | P1 |
| Мало tip_viewed | Совет дня в утреннем push работает? | P2 |
