# Exports Directory

**Doc sync:** 2026-09-05. Снимки в подпапках не редактировать вручную — только новые прогоны скриптов.

ZIP- и CSV-выгрузки из `/export all` и `scripts/pa_weekly_snapshot.py`.

## Структура

```
exports/
  prelaunch/          # baseline до релиза
    export_2026-05-25.zip
    verify_report.txt
  week-2026-W21/      # еженедельные снимки
    export_2026-05-25.zip
    weekly_summary.md
    metadata.json
```

## Как получить export

**Из бота (production):**
```
/export all
```
Telegram пришлёт ZIP-файл — сохрани сюда.

**Локально (dev):**
```bash
python scripts/pa_weekly_snapshot.py
python scripts/pa_verify_export.py --save-baseline
```

## Git

Файлы `.zip` и `.csv` **не коммитятся** (содержат user data).
Коммить только `metadata.json`, `verify_report.txt`, `weekly_summary.md` — если обезличены.
