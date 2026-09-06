# Техническая документация Palph

Полный технический справочник по проекту. Каждый документ — самодостаточный
раздел; ссылки между ними и на корневые файлы репозитория.

> **Doc sync:** 2026-09-05 · код на коммите `0ac30af` (2026-06-03) ·
> pytest suite **803** теста, все зелёные.

## Карта документации

### Технический справочник (эта папка)

| Документ | О чём |
|----------|-------|
| [architecture.md](architecture.md) | Слои, карта модулей, запуск, фоновые циклы, конкурентность, обработка ошибок, логирование |
| [data-model.md](data-model.md) | Все 28 таблиц SQLite, колонки, индексы, миграции, инварианты |
| [features.md](features.md) | Пофичевая спецификация: онбординг, таймер, 4 режима учёбы, геймификация, питомец, лидерборд, друзья, советы |
| [analytics.md](analytics.md) | Таксономия событий (30 имён), админ-метрики, экспорт, ноутбуки |
| [configuration.md](configuration.md) | Переменные окружения, разрешение путей, Docker, bothost.ru, CI |
| [operations.md](operations.md) | Runbook: деплой, бэкап/восстановление, логи, диагностика инцидентов |
| [security.md](security.md) | Модель угроз и реализованные контроли |
| [i18n.md](i18n.md) | Система локализации ru/en, ключи, генерация бандлов |
| [content-authoring.md](content-authoring.md) | Как добавлять учебный контент, советы, арт питомца |
| [testing.md](testing.md) | **Требования к тестам** (включая правило о времени), фикстуры, как запускать |
| [development-guide.md](development-guide.md) | **Рекомендации при создании** нового кода: рецепты, чеклисты, инварианты |
| [scripts.md](scripts.md) | Справочник по `scripts/` |
| [glossary.md](glossary.md) | Термины проекта: mastery, rollover, тир, сегмент, фичефлаг, EF |

### Корневые документы репозитория

| Документ | О чём |
|----------|-------|
| [../README.md](../README.md) | Обзор продукта, быстрый старт, что умеет бот |
| [../LEADERBOARD.md](../LEADERBOARD.md) | Продуктовая спека weekly-лидерборда (источник истины по балансу) |
| [../user-flows.md](../user-flows.md) | Пользовательские сценарии и подсчёт кликов |
| [../admin_commands.md](../admin_commands.md) | Справочник админ-команд |
| [../TODO.md](../TODO.md) | Размеченные задачи и статус спринтов |
| [../BACKLOG.md](../BACKLOG.md) | Сырые идеи до оценки |
| [../PRIVACY.md](../PRIVACY.md) / [../PRIVACY.ru.md](../PRIVACY.ru.md) | Политика приватности (GDPR + 152-ФЗ) |
| [../session_notes.md](../session_notes.md) | Журнал изменений по сессиям разработки |
| [../audits/](../audits/) | Аудиты: безопасность, обработка ошибок, отказоустойчивость, сложность |
| [../analysis/README.md](../analysis/README.md) | PA-аналитика для портфолио |

## С чего начать

**Новому разработчику:** [architecture.md](architecture.md) →
[data-model.md](data-model.md) → [development-guide.md](development-guide.md) →
[testing.md](testing.md). Незнакомые термины — [glossary.md](glossary.md).

**Тому, кто наполняет контент:** [content-authoring.md](content-authoring.md) →
[../study_materials/README.md](../study_materials/README.md).

**Тому, кто эксплуатирует бота:** [configuration.md](configuration.md) →
[operations.md](operations.md).

**Продуктовому аналитику:** [analytics.md](analytics.md) →
[../analysis/README.md](../analysis/README.md) → [../admin_commands.md](../admin_commands.md).

## Хочу сделать X — куда смотреть

| Задача | Документ |
|--------|----------|
| Добавить хендлер, команду, таблицу, событие или строку интерфейса | [development-guide.md](development-guide.md) §Рецепты |
| Понять, почему нет кнопки / режима / предмета | [architecture.md](architecture.md) §Загрузка учебного контента, §Выключенные фичи |
| Разобраться, за что начисляются очки и монеты | [features.md](features.md) §4, §6 · [../LEADERBOARD.md](../LEADERBOARD.md) |
| Добавить задачи, карточки, MCQ или советы | [content-authoring.md](content-authoring.md) |
| Написать тест, не привязанный к календарю | [testing.md](testing.md) §1 |
| Развернуть бота или починить пути к БД | [configuration.md](configuration.md) |
| Восстановить БД из бэкапа, разобрать инцидент | [operations.md](operations.md) |
| Понять, какие данные о пользователе хранятся | [data-model.md](data-model.md) · [../PRIVACY.ru.md](../PRIVACY.ru.md) |
| Посчитать метрику или выгрузить данные | [analytics.md](analytics.md) · [../admin_commands.md](../admin_commands.md) |
| Перевести интерфейс или добавить язык | [i18n.md](i18n.md) |
| Проверить, что документация не разошлась с кодом | `python scripts/check_docs.py` |

## Правило актуальности

Документация — часть Definition of Done. Меняешь поведение — в том же
коммите правишь документ, который это поведение описывает, и обновляешь
`Doc sync` в его шапке. Детали — в [development-guide.md](development-guide.md)
§«Чеклист перед коммитом».

Часть этого правила проверяется автоматически:

```bash
python scripts/check_docs.py          # сверка с кодом (stdlib + pytest)
python scripts/check_docs.py --quiet  # печатать только провалы
```

Скрипт сверяет с кодом то, что расходилось на практике: имена и количество
таблиц и индексов, число репозиториев, таксономию событий (в обе стороны —
и незадокументированные, и исчезнувшие), алиасы `/export`, полные пути
ключей локалей, счётчики контента, значения фичефлагов, числа баланса
(награды, капы, очки, лимиты), счётчик тестов и таблицу по областям в
[testing.md](testing.md), все markdown-ссылки и гигиену разметки. Если проверка упала — **правится документ, а не
проверка**.

Те же проверки (кроме счётчиков тестов, которые поднимают pytest
подпроцессом) прогоняются внутри обычного `pytest -q` через
`tests/test_docs_consistency.py` — чтобы расхождение краснело само, даже
если про скрипт забыли.
