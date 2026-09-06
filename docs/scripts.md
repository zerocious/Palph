# Справочник по `scripts/`

> **Doc sync:** 2026-09-05 · 14 python-скриптов + 1 shell-шаблон.

Ничего из этой папки не запускается в рантайме бота. Скрипты делятся на
три группы: **инструменты** (запускать регулярно), **генераторы контента**
(разовые прогоны при обновлении материалов) и **исторические патчи**
(оставлены для аудита, повторно запускать не нужно).

## Инструменты

| Скрипт | Запуск | Что делает |
|--------|--------|------------|
| `check_docs.py` | `python scripts/check_docs.py [--quiet]` | **Сверяет документацию с кодом**: количество таблиц и индексов, число репозиториев, таксономия событий, алиасы `/export`, ключи локалей, счётчики контента, значения фичефлагов, счётчик тестов и таблица по областям в `docs/testing.md`, все markdown-ссылки. Нужен только stdlib; проверка счётчиков тестов требует pytest и без него помечается `skip`. Выход `1` при расхождении — годится для CI |
| `audit_i18n_keys.py` | `python scripts/audit_i18n_keys.py` | Сверяет ключи из `t(...)` в `bot.py`, `plan_handlers.py`, `locale_bot.py`, `services.py` с `locales/*.json`. Печатает «Used keys / Missing in ru / Missing in en». **Обязателен после правки строк интерфейса** |
| `build_locales.py` | `python scripts/build_locales.py` | Генерирует `locales/ru.json` и `locales/en.json`. **Источник истины для строк — этот скрипт**, JSON — артефакт |
| `check_plan_issues.py` | `python scripts/check_plan_issues.py` | Контент-гейт спринт-плана: хватает ли материала на каталог + прогон юнит-тестов плана |
| `pa_verify_export.py` | `python scripts/pa_verify_export.py [--db path] [--save-baseline]` | Проверяет, что аналитика жива: доступность БД, наличие таблиц, валидность методов `AnalyticsService`, целостность ZIP-экспорта. Прогонять **до** публичного запуска и после изменений в аналитике |
| `pa_weekly_snapshot.py` | `python scripts/pa_weekly_snapshot.py [--week N] [--db path]` | Еженедельный снимок: экспорт всех таблиц + markdown-сводка в `analysis/exports/` |
| `build_pet_assets.py` | `python scripts/build_pet_assets.py [--with-periods]` | Pillow-генератор плейсхолдеров питомца: 75 PNG (3 эмоции × 5 цветов × 5 аксессуаров) + 3 GIF. Требует `Pillow` из `requirements-dev.txt` |
| `backup_offsite.sh.example` | скопировать и настроить | Шаблон offsite-бэкапа: GPG-шифрование снапшота + `rclone` в S3/B2. Ставится в cron на хосте после времени внутреннего бэкапа |

## Генераторы контента

Запускаются при обновлении учебных материалов; результат коммитится.

| Скрипт | Что генерирует |
|--------|----------------|
| `generate_math_bernoulli_tasks.py` | `study_materials/math/tasks/task-01…41.json` + диагностику |
| `generate_math_etalon_top3_tasks.py` | `task-42…72.json`, обновляет `groups.json` и `topics.json` (диагностику не перезаписывает). Требует `numpy` и `scipy` — их нет в requirements, ставятся вручную под разовый прогон |
| `apply_math_task_hints.py` | Проставляет `"hint"` в задачи из `source/top3_tasks_etalon_1.md`, конвертируя LaTeX в plain text |
| `generate_accounting_theory.py` | `study_materials/accounting/flashcards.txt` и `theory-with-hints.txt` из встроенного списка Q&A |

После любого прогона: пересчитать количества в
[../study_materials/README.md](../study_materials/README.md),
[../study_materials/math/README.md](../study_materials/math/README.md) и
[content-authoring.md](content-authoring.md), затем
`python -m pytest tests/test_math_bernoulli_tasks.py -q`.

## Исторические патчи

`i18n_patch_bot.py`, `i18n_fix_bot.py`, `i18n_wire_remaining.py` —
одноразовые скрипты, которыми `bot.py` переводили на `t()` при внедрении
локализации. Они выполняют текстовые замены по `bot.py` и **уже
применены**. Хранятся как документация того, что именно менялось;
повторный запуск в лучшем случае ничего не найдёт, в худшем — испортит
файл. Не запускать.
