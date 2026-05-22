"""
Парсинг пользовательских задач из .txt для импорта в бот.

Формат (UTF-8):
  вопрос || правильный ответ | другой вариант

Опционально подсказка после ответов:
  вопрос || ответ | вариант ## подсказка при ошибке

Строки с # в начале — комментарии. Пустые строки игнорируются.
"""

# Промпт для ChatGPT / Claude и т.п.: пользователь вставляет его вместе со своим текстом.
USER_TASK_REFORMAT_PROMPT = """\
Преобразуй учебные задачи ниже в формат для Telegram-бота.

Правила вывода (строго):
- Одна задача = одна строка текста
- Формат строки: вопрос || ответ1 | ответ2
- Между вопросом и ответами ровно два символа ||
- Несколько правильных ответов разделяй одним символом |
- Подсказка (если есть в исходнике): после ответов через ## текст подсказки
- Без нумерации, маркеров, заголовков и пустых строк
- Без пояснений — выведи только готовые строки задач

Пример строки:
2+2 сколько? || 4 | четыре

Исходный текст задач:
---
ВСТАВЬ_СЮДА_СВОЙ_ТЕКСТ
---"""

USER_TASK_TXT_INSTRUCTION = (
    """📋 <b>Как добавить свои задачи из .txt</b>

<b>Формат файла</b> (кодировка UTF-8, расширение .txt):

Каждая задача — <b>одна строка</b>:
<code>вопрос || ответ | другой ответ</code>

Разделитель <code>||</code> между условием и ответами.
Несколько правильных ответов — через <code>|</code>.

<b>Примеры:</b>
<code>2+2 сколько? || 4 | четыре</code>
<code>Метод фотографии рабочего времени? || фотография | фото</code>

<b>Подсказка</b> (необязательно), после ответов через <code>##</code>:
<code>Сколько планет? || 8 ## В Солнечной системе 8 планет</code>

<b>Комментарии</b> — строка начинается с <code>#</code>
Пустые строки пропускаются.

<b>Лимиты:</b> до 50 задач на предмет, файл до 64 КБ.

<b>Уже есть текст задач?</b> Скопируй промпт ниже в ChatGPT, Claude или другую нейросеть, замени <code>ВСТАВЬ_СЮДА_СВОЙ_ТЕКСТ</code> на свой материал, сохрани ответ в .txt (UTF-8) и отправь файл сюда.

<pre>"""
    + USER_TASK_REFORMAT_PROMPT.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    + """</pre>

Отправь готовый .txt файл в этот чат.
Для отмены — /cancel"""
)


def parse_user_tasks_txt(content: str) -> tuple[list[dict], list[str]]:
    """
    Парсит текст файла.
    Возвращает (tasks, errors).
    task: {problem, accepted: list[str], hint: str}
    """
    tasks: list[dict] = []
    errors: list[str] = []
    for line_no, raw in enumerate(content.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "||" not in line:
            errors.append(
                f"Строка {line_no}: нет «||» — нужен формат «вопрос || ответ»."
            )
            continue
        left, right = line.split("||", 1)
        problem = left.strip()
        if not problem:
            errors.append(f"Строка {line_no}: пустой вопрос.")
            continue
        hint = ""
        answers_part = right.strip()
        if "##" in answers_part:
            answers_part, _, hint_raw = answers_part.partition("##")
            hint = hint_raw.strip()
        accepted = [a.strip() for a in answers_part.split("|") if a.strip()]
        if not accepted:
            errors.append(f"Строка {line_no}: нет правильного ответа.")
            continue
        tasks.append({
            "problem": problem,
            "accepted": accepted,
            "hint": hint,
        })
    return tasks, errors
