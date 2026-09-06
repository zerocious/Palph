# Бухучёт — теоретические вопросы

**Doc sync:** 2026-09-05.

Предмет **подключён**: `accounting` есть и в `SUBJECT_IDS` (`locale_bot.py`),
и в `SUBJECTS` (`bot.py`). Показывается в «📖 Подготовка» с режимом
🃏 Флэш-карты, потому что `flashcards.txt` непустой (обнаружение
data-driven, см. [../../docs/architecture.md](../../docs/architecture.md)).

## Папка `source/`

Сюда кладите **исходные файлы** с теоретическими вопросами (PDF, DOC/DOCX, TXT и т.п.). Бот их напрямую не читает — это черновое хранилище для ваших материалов.

## Готовый контент

| Файл | Описание |
|------|----------|
| `flashcards.txt` | 67 карточек: `[N] вопрос \|\| экзаменационный ответ \|\| topic` |
| `theory-with-hints.txt` | Те же пары + краткие подсказки (`## подсказка`, формат user_tasks) |

Источник вопросов: `source/Теоретические-вопросы-2-курс.txt` (+ PDF-дубликат).  
Перегенерация: `python scripts/generate_accounting_theory.py`.

Чего пока нет: `mcq.txt`, `tasks/`, `situational/`, `diagnostic/` — режимы
для них не появятся в меню, пока файлы пустые. Форматы — в
[общем README](../README.md) и
[../../docs/content-authoring.md](../../docs/content-authoring.md).
