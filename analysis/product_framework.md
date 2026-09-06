# Product Framework — Palph PA Launch

> Заполнить **до релиза**. Это «конституция» аналитики: что измеряем, зачем и какие гипотезы проверяем.
> **Doc sync:** 2026-09-05 · бот v0.8 · pytest **793** · [README.md](README.md) в этой папке.

## Продуктовая рамка

| Поле | Значение |
|------|----------|
| **Продукт** | Palph — Telegram-бот для регулярной учёбы студентов |
| **Аудитория** | Студенты перед зачётом/экзаменом (ОПМ, математика и др.) |
| **Главный вопрос** | Помогает ли Palph быстрее начать учиться и возвращаться к регулярным сессиям? |
| **Ценность** | Меньше хаоса в учёбе: фокус, повторение, геймификация, советы по продуктивности |
| **North Star (MVP)** | Пользователи, дошедшие до полезного учебного действия **и** вернувшиеся на D7 |

## Полезное действие (activation event)

Пользователь **активирован**, если в первые 24 часа после `/start` произошло хотя бы одно:

| Событие | event_name | Почему считаем |
|---------|------------|----------------|
| Запуск таймера | `session_started` | Pomodoro = фокус-сессия |
| Выбор предмета | `subject_picked` | Намерение учиться |
| Ответ на квиз | `quiz_answered` | Active recall |
| Ревью карточки | `flashcard_reviewed` | Интервальное повторение |
| Ответ MCQ | `mcq_answered` | Проверка знаний |
| Попытка задачи | `task_attempted` | Применение знаний |
| Просмотр совета | `tip_viewed` | Вовлечение в продуктивность |

**Primary activation metric:** `session_started` в первые 24h (медиана time-to-value из `/activation`).

## Метрики

### Acquisition
| Метрика | Формула / источник | Цель (3 нед.) |
|---------|-------------------|---------------|
| Регистрации | `COUNT(users)` | 30–50 |
| Новые сегодня | `/dau` → `new_today` | тренд ↑ |
| Источники | [acquisition_tracker.md](acquisition_tracker.md) | разметить ≥3 канала |

### Activation
| Метрика | Источник | Цель |
|---------|----------|------|
| % session в 24h | `/activation` | измерить baseline |
| Median hours to session | `/activation` | < 2h |
| Funnel conversion | `/funnel` | найти главный drop-off |
| Strict funnel | `/product_metrics` | step-by-step |

### Engagement
| Метрика | Источник | Цель |
|---------|----------|------|
| DAU / WAU / MAU | `/dau` | рост WAU |
| Stickiness (DAU/MAU) | `/dau` | ≥15% |
| Сессий на пользователя | `study_sessions` | ≥2 у activated |
| Минут таймера | `study_sessions.duration_minutes` | ≥25 у activated |

### Retention
| Метрика | Источник | Цель |
|---------|----------|------|
| D1 | `/cohort_stats` | ≥30% |
| D3 | `/cohort_stats` | ≥20% |
| D7 | `/cohort_stats` | ≥15% |
| Feature retention D7 | `/product_metrics` | tips/pet/timer vs baseline |

### Feature adoption
| Фича | Источник | Вопрос |
|------|----------|--------|
| Pomodoro | `/feature_usage` | Главный entry point? |
| Flashcards | `/feature_usage` | Связаны с retention? |
| MCQ / Quiz / Tasks | `/feature_usage` | Какой режим популярнее? |
| Tips | `/feature_usage` | Влияют на возврат? |
| Pet / Friends / LB | `/feature_usage` | Социальная механика работает? |

### Content fit
| Метрика | Источник | Вопрос |
|---------|----------|------|
| Subject breakdown | `/product_metrics` | Какой предмет выбирают? |
| Mode breakdown | `/product_metrics` | Какой режим первый? |
| Hardest terms | `/content_stats` | Где контент сложный? |
| Top tips | `/content_stats` | Какие советы читают? |

## Гипотезы (проверить за 3 недели)

| # | Гипотеза | Метрика | Критерий подтверждения |
|---|----------|---------|------------------------|
| H1 | Таймер — самый быстрый путь к activation | time-to-value по event | median(session_started) < median(quiz_answered) |
| H2 | Слишком много кликов до квиза снижает activation | funnel drop-off | conv(mode_picked → quiz_answered) < 50% |
| H3 | Советы повышают D7 retention | feature retention D7 | tips users D7 > non-tips D7 |
| H4 | Питомец/геймификация удерживает | feature retention D7 | pet users D7 > non-pet D7 |
| H5 | Студентам нужен план под экзамен | опрос + behavior | ≥40% «да» + низкий repeat subject pick |
| H6 | Утренние напоминания → сессия | morning push funnel | `/product_metrics` morning→session > baseline |

## Каталог событий (events table)

### Core learning
| event_name | Когда | Колонки |
|------------|-------|---------|
| `user_registered` | `/start` первый раз | properties |
| `session_started` | Запуск таймера | duration, kind |
| `session_completed` | Завершение таймера | duration, coins |
| `subject_picked` | Выбор предмета | subject_id |
| `mode_picked` | Выбор режима | subject_id, mode |
| `quiz_answered` | Ситуационный квиз | subject_id, mode |
| `flashcard_reviewed` | Оценка карточки | subject_id, mode |
| `mcq_answered` | MCQ ответ | subject_id, mode |
| `task_attempted` | Задача | subject_id, mode |
| `tip_viewed` | Просмотр совета | tip_id |

### Gamification & social
| event_name | Когда |
|------------|-------|
| `achievement_unlocked` | Получение ачивки |
| `leaderboard_viewed` | `/leaderboard` |
| `pet_purchased` / `pet_equipped` | Кастомизация питомца |
| `friend_added` / `friend_invite_sent` | Друзья |

### Notifications & settings
| event_name | Когда |
|------------|-------|
| `reminder_sent` | Отправка напоминания |
| `settings_changed` | Изменение настроек |

## Две метрики активности (не смешивать!)

| Метрика | Источник | Где используется |
|---------|----------|------------------|
| `activity_progress` | progress tables + study_sessions | cohort retention, segments |
| `activity_events` | events table | heatmap, timeline, DAU events |

Подробнее: [admin_commands.md](../admin_commands.md) → «Метрики активности».

## Сегменты пользователей

Из `/segments`:

| Сегмент | Определение | Действие |
|---------|-------------|----------|
| Never started | 0 sessions | Улучшить онбординг |
| Tried once | 1–2 sessions | Push к повтору |
| Active | 3+ sessions, active ≤7d | Удерживать |
| Power | 10+ sessions | Социальные фичи |
| Churned | Нет активности >14d | Win-back |

## Связь с roadmap

| Backlog-идея | Как валидировать |
|--------------|------------------|
| Учебный план на день | Опрос + % users без mode_picked repeat |
| Sprint 14 дней под экзамен | Опрос + retention перед экзаменом |
| UX «Продолжить» | Funnel drop-off subject→mode |

См. [BACKLOG.md](../BACKLOG.md).
