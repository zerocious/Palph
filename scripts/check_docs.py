#!/usr/bin/env python3
"""
Проверка документации на соответствие коду.

Документация в этом проекте уже расходилась с кодом — счётчики тестов,
имена событий, списки таблиц и «известные проблемы» жили своей жизнью.
Скрипт закрывает эту дыру: всё, что можно сверить механически, сверяется.

Запуск:
    python scripts/check_docs.py           # все проверки
    python scripts/check_docs.py --quiet   # только провалы

Зависимости: стандартная библиотека. Проверка счётчиков тестов требует
pytest — без него она пропускается с пометкой SKIP, остальные работают.

Выход: 0 — всё сошлось, 1 — есть расхождения.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PASS, FAIL, SKIP = "ok", "FAIL", "skip"
results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, PASS if ok else FAIL, detail))


def skip(name: str, why: str) -> None:
    results.append((name, SKIP, why))


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def md_files() -> list[Path]:
    return [
        p for p in ROOT.rglob("*.md")
        if ".git" not in p.parts and "node_modules" not in p.parts
    ]


# ---------------------------------------------------------------- links
def check_links() -> None:
    broken: list[str] = []
    for md in md_files():
        for link in re.findall(r"\]\(([^)]+)\)", md.read_text(encoding="utf-8")):
            if link.startswith(("http", "#", "mailto:")):
                continue
            target = link.split("#")[0].strip()
            if not target:
                continue
            if not (md.parent / target).resolve().exists():
                broken.append(f"{md.relative_to(ROOT)} -> {link}")
    check("markdown-ссылки резолвятся", not broken, "; ".join(broken[:5]))


# ---------------------------------------------------------------- разметка
def check_markdown_hygiene() -> None:
    """
    Дефекты разметки, которые глазами пропускаются: незакрытый блок кода,
    код-спан, разорванный переносом строки, битый синтаксис ссылки,
    забытый TODO/FIXME в тексте.

    Намеренные два пробела в конце строки (жёсткий перенос Markdown) —
    не дефект и не проверяются.
    """
    bad: list[str] = []
    for md in md_files():
        text = md.read_text(encoding="utf-8")
        rel = md.relative_to(ROOT)
        if text.count("```") % 2:
            bad.append(f"{rel}: незакрытый блок ```")
        in_code = False
        for i, line in enumerate(text.split("\n"), 1):
            if line.strip().startswith("```"):
                in_code = not in_code
                continue
            if in_code:
                continue
            if line.count("`") % 2:
                bad.append(f"{rel}:{i}: непарный бэктик (код-спан разорван переносом?)")
            if re.search(r"\]\(\s*\)|\]\(\s+\S", line):
                bad.append(f"{rel}:{i}: битый синтаксис ссылки")
            outside_code = re.sub(r"`[^`]*`", "", line)
            # Только маркерный синтаксис («TODO:», «FIXME(...)»). Ссылки вида
            # «TODO #18» и «TODO.md» — обычная проза, их трогать нельзя.
            if re.search(r"\b(TODO|FIXME|XXX)\s*[:(]", outside_code):
                bad.append(f"{rel}:{i}: забытый маркер TODO/FIXME")
    check("разметка markdown без дефектов", not bad, "; ".join(bad[:5]))


# ---------------------------------------------------------------- schema
def check_schema() -> None:
    db = read("db.py")
    names = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", db))
    indexes = db.count("CREATE INDEX IF NOT EXISTS")
    dm = read("docs/data-model.md")

    check(
        f"docs/data-model: {len(names)} таблиц",
        f"**{len(names)} таблиц**" in dm or f"{len(names)} таблиц" in dm,
        f"в db.py {len(names)}",
    )
    # Одного количества мало: переименование таблицы его не меняет, а
    # документацию делает неверной. Поэтому сверяем имена поимённо.
    undocumented = sorted(t for t in names if f"`{t}`" not in dm)
    check("каждая таблица упомянута в docs/data-model", not undocumented,
          ", ".join(undocumented))
    # Обратная сторона: таблицу удалили или переименовали, а строка в обзоре
    # осталась. Разбираем именно нумерованную таблицу-обзор, а не все
    # бэктики подряд — иначе в «несуществующие таблицы» попадают колонки.
    overview = re.findall(r"^\| \d+ \| `(\w+)` \|", dm, re.M)
    check("обзор таблиц в docs/data-model разобрался",
          len(overview) == len(names), f"строк {len(overview)}, таблиц {len(names)}")
    ghosts = sorted(set(overview) - names)
    check("в обзоре нет несуществующих таблиц", not ghosts, ", ".join(ghosts))
    check(
        f"docs/data-model: {indexes} индексов",
        f"**{indexes} индексов**" in dm or f"{indexes} индексов" in dm,
        f"в db.py {indexes}",
    )
    repos = read("repository.py").count("\nclass ")
    arch = read("docs/architecture.md")
    check(f"docs/architecture: {repos} репозиториев", f"{repos} репозитори" in arch)


# ---------------------------------------------------------------- events
def check_events() -> None:
    src = "".join(read(f) for f in ("bot.py", "services.py", "plan_handlers.py"))
    # [a-z0-9_]+, а не [a-z_]+: имя с цифрой раньше просто выпадало из
    # выборки, и переименование события проверка не замечала.
    code = set(re.findall(r'event_repo\.log\(\s*[^,]+,\s*\n?\s*"([a-z0-9_]+)"', src))
    doc = read("docs/analytics.md")
    check("события вообще найдены в коде", len(code) >= 25, f"найдено {len(code)}")
    missing = sorted(e for e in code if f"`{e}`" not in doc)
    check("все события описаны в docs/analytics.md", not missing, ", ".join(missing))
    # Обратная сторона: событие удалили из кода, а из документа забыли.
    documented = set(re.findall(r"^\| `([a-z0-9_]+)` \|", doc, re.M))
    known_non_events = {"activity_events", "activity_progress"}
    ghosts = sorted(documented - code - known_non_events)
    check("в docs/analytics нет исчезнувших событий", not ghosts, ", ".join(ghosts))


# ---------------------------------------------------------------- export
def check_export_aliases() -> None:
    services = read("services.py")
    blk = services[services.index("EXPORTABLE_TABLES"):]
    blk = blk[blk.index("{"):blk.index("}") + 1]
    aliases = set(re.findall(r'"([a-z0-9_]+)":', blk))
    check("алиасы /export вообще найдены", len(aliases) >= 15, f"найдено {len(aliases)}")
    for doc in ("docs/analytics.md", "admin_commands.md"):
        text = read(doc)
        missing = sorted(a for a in aliases if f"`{a}`" not in text)
        check(f"{doc}: все {len(aliases)} алиасов /export", not missing, ", ".join(missing))
    # Список в docs/analytics перечислен явно — ловим и лишние имена.
    listed = re.search(r"`users`, `sessions`.*?`streak_freezes`", read("docs/analytics.md"), re.S)
    if listed:
        named = set(re.findall(r"`([a-z0-9_]+)`", listed.group(0)))
        check("в docs/analytics нет исчезнувших алиасов",
              not (named - aliases), ", ".join(sorted(named - aliases)))


# ---------------------------------------------------------------- locales
def check_locales() -> None:
    def leaves(d: dict) -> int:
        return sum(leaves(v) if isinstance(v, dict) else 1 for v in d.values())

    def paths(d: dict, prefix: str = "") -> set[str]:
        out: set[str] = set()
        for k, v in d.items():
            full = f"{prefix}{k}"
            out |= paths(v, f"{full}.") if isinstance(v, dict) else {full}
        return out

    ru = json.loads(read("locales/ru.json"))
    en = json.loads(read("locales/en.json"))
    check("паритет групп ru/en", ru.keys() == en.keys())
    # Сравнение полных путей, а не групп: переименование вложенного ключа
    # не меняло ни набор групп, ни их количество, и проходило незамеченным.
    only_ru = sorted(paths(ru) - paths(en))
    only_en = sorted(paths(en) - paths(ru))
    check("паритет полных путей ключей ru/en", not only_ru and not only_en,
          f"только в ru: {only_ru[:5]}; только в en: {only_en[:5]}")
    check("паритет числа ключей ru/en", leaves(ru) == leaves(en), f"{leaves(ru)} vs {leaves(en)}")
    i18n = read("docs/i18n.md")
    check(f"docs/i18n: {leaves(ru)} ключей", f"**{leaves(ru)}**" in i18n, f"реально {leaves(ru)}")
    check(f"docs/i18n: {len(ru)} групп", f"**{len(ru)}**" in i18n, f"реально {len(ru)}")


# ---------------------------------------------------------------- content
def check_content() -> None:
    tasks = sorted((ROOT / "study_materials/math/tasks").glob("*.json"))
    hints = [t for t in tasks if '"hint"' in t.read_text(encoding="utf-8")]
    for doc in ("study_materials/README.md", "study_materials/math/README.md",
                "docs/content-authoring.md"):
        text = read(doc)
        # \b, а не подстрока: «71» иначе находилось внутри «171» и т.п.
        check(f"{doc}: {len(tasks)} задач по математике",
              bool(re.search(rf"\b{len(tasks)}\b", text)), f"реально {len(tasks)}")
        check(f"{doc}: {len(hints)} подсказок",
              bool(re.search(rf"\b{len(hints)}\b", text)), f"реально {len(hints)}")


# ---------------------------------------------------------------- flags
def check_feature_flags() -> None:
    flags = {
        "PLAN_UI_ENABLED": read("plan_handlers.py"),
        "PET_CUSTOMIZATION_ENABLED": read("bot.py"),
        "PET_SINGLE_IMAGE_MODE": read("services.py"),
    }
    arch = read("docs/architecture.md")
    for name, src in flags.items():
        m = re.search(rf"^{name}\s*=\s*(True|False)", src, re.M)
        if not m:
            check(f"флаг {name} найден в коде", False)
            continue
        value = m.group(1)
        check(
            f"docs/architecture описывает {name} = {value}",
            f"{name} = {value}" in arch,
            "значение флага изменилось — обнови таблицу «Выключенные фичи»",
        )


# ---------------------------------------------------------------- константы
def check_balance_constants() -> None:
    """
    Числа баланса — то, что меняют при ребалансе и забывают в документации.

    ВСЕ значения читаются из кода. Захардкодить ожидаемое число прямо здесь
    нельзя: тогда проверка сравнивает документ с копией числа внутри самой
    проверки и переживает любой ребаланс — ровно та ошибка, из-за которой
    первая версия этой функции пропускала 7 мутаций из 14.
    """
    bot_py, services = read("bot.py"), read("services.py")
    repo, plan = read("repository.py"), read("plan_service.py")

    def const(src: str, name: str) -> str | None:
        m = re.search(rf"^\s*{name}\s*=\s*(.+?)\s*(?:#.*)?$", src, re.M)
        return m.group(1).strip().strip("'\"") if m else None

    def grab(src: str, pattern: str) -> str | None:
        m = re.search(pattern, src)
        return m.group(1) if m else None

    rewards = const(bot_py, "TASK_REWARDS_BY_ATTEMPT") or ""
    reward_first, _, reward_second = rewards.strip("[]").partition(",")

    cases: list[tuple[str, str | None, tuple[str, ...], str]] = [
        ("награда за задачу с 1-й попытки", reward_first.strip(),
         ("docs/features.md",), r"\*\*{v} 🪙\*\*"),
        ("награда со 2-й попытки", reward_second.strip(),
         ("docs/features.md",), r"\*\*{v} 🪙\*\*"),
        ("MAX_TASK_ATTEMPTS", const(bot_py, "MAX_TASK_ATTEMPTS"),
         ("docs/features.md",), r"MAX_TASK_ATTEMPTS = {v}\b"),
        ("QUIZ_INTERVALS", const(bot_py, "QUIZ_INTERVALS"),
         ("docs/features.md",), r"QUIZ_INTERVALS = {v}"),
        ("дневной кап задач", grab(repo, r"task_count < (\d+)"),
         ("docs/features.md",), r"\| {v} задач"),
        ("дневной кап квизов", grab(repo, r"quiz_count < (\d+)"),
         ("docs/features.md",), r"\| {v} верных"),
        ("дневной кап карточек", grab(repo, r"cards_count < (\d+)"),
         ("docs/features.md",), r"\| {v} успешных"),
        ("очки за задачу", grab(repo, r"task_pts = task_pts \+ (\d+)"),
         ("docs/features.md",), r"{v} pts за решённую"),
        ("очки за новую карточку", grab(repo, r"pts = (\d+) if is_new"),
         ("docs/features.md",), r"{v} pts за новую"),
        ("очки за повторную карточку", grab(repo, r"pts = \d+ if is_new else (\d+)"),
         ("docs/features.md",), r"{v} pts за повторную"),
        ("TOP_N_DISPLAY", const(services, "TOP_N_DISPLAY"),
         # «топ-{v}» без уточнения совпало бы с «топ-10 %» из блока наград.
         ("docs/features.md",), r"Отображается топ-{v} \(`TOP_N_DISPLAY`\)"),
        ("COIN_BONUS_TOP10_PCT", const(services, "COIN_BONUS_TOP10_PCT"),
         ("docs/features.md",), r"по {v} 🪙"),
        ("MIN_SEGMENT_FOR_TOP10_BONUS", const(services, "MIN_SEGMENT_FOR_TOP10_BONUS"),
         ("docs/features.md",), r"≥ {v} участник"),
        ("EF_FLOOR", const(services, "EF_FLOOR"),
         ("docs/features.md",), r"EF >= {v}"),
        ("TIPS_SEEN_COOLDOWN_DAYS", const(bot_py, "TIPS_SEEN_COOLDOWN_DAYS"),
         ("docs/features.md",), r"[Cc]ooldown {v} дн"),
        ("SPRINT_DAYS", const(plan, "SPRINT_DAYS"),
         ("docs/features.md",), r"{v}-дневный"),
        ("MIN_PLAN_ITEMS", const(plan, "MIN_PLAN_ITEMS"),
         ("docs/features.md",), r"минимум {v} элемент"),
        ("rate limit: действий", grab(bot_py, r"UserRateLimiter\(max_actions=(\d+), window_seconds=60, warn"),
         ("docs/security.md",), r"max_actions={v}"),
    ]

    for label, value, docs, pattern in cases:
        if not value:
            check(f"константа «{label}» прочитана из кода", False, "не удалось прочитать")
            continue
        pat = pattern.replace("{v}", re.escape(value))
        hit = any(re.search(pat, read(d)) for d in docs)
        check(f"{label} = {value} отражено в документации", hit,
              f"шаблон не найден в {', '.join(docs)}")


# ---------------------------------------------------------------- tests
def collect_test_counts() -> dict[str, int] | None:
    try:
        out = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q"],
            cwd=ROOT, capture_output=True, text=True, timeout=300,
            env={**os.environ, "BOT_TOKEN": os.environ.get("BOT_TOKEN", "docs-check")},
        ).stdout
    except Exception:
        return None
    counts: dict[str, int] = {}
    for line in out.splitlines():
        if "::" in line:
            f = os.path.basename(line.split("::")[0])
            counts[f] = counts.get(f, 0) + 1
    return counts or None


def check_test_counts() -> None:
    counts = collect_test_counts()
    if counts is None:
        skip("счётчики тестов", "pytest недоступен")
        return
    total = sum(counts.values())
    testing = read("docs/testing.md")

    rows = re.findall(
        r"^\| ([^|]+) \| ((?:`test_[^`]+\.py`(?:, )?)+) \| +(\d+) \|", testing, re.M
    )
    check("таблица областей в docs/testing.md непустая", bool(rows))
    bad = []
    claimed_sum = 0
    for area, files, claimed in rows:
        fs = re.findall(r"`(test_[^`]+\.py)`", files)
        actual = sum(counts.get(f, 0) for f in fs)
        claimed_sum += int(claimed)
        if actual != int(claimed):
            bad.append(f"{area.strip()}: в доке {claimed}, реально {actual}")
    check("строки таблицы областей сходятся", not bad, "; ".join(bad))
    check(
        f"сумма по областям = {total}",
        claimed_sum == total,
        f"в доке {claimed_sum}, реально {total}",
    )

    listed = set(re.findall(r"`(test_[^`]+\.py)`", testing))
    unlisted = sorted(set(counts) - listed)
    check("все тест-файлы перечислены", not unlisted, ", ".join(unlisted))
    ghosts = sorted(listed - set(counts))
    check("нет ссылок на удалённые тест-файлы", not ghosts, ", ".join(ghosts))

    files_claims = re.findall(r"в (\d+) файл", testing) + re.findall(r"в (\d+) файл", read("README.md"))
    wrong = [n for n in files_claims if int(n) != len(counts)]
    check(f"число тест-файлов = {len(counts)}", not wrong, f"в доке {set(wrong)}")

    # Ищем только те числа, которые ЗАЯВЛЯЮТ размер всего прогона. Частные
    # цифры («110 pytest в аналитических модулях») и явно исторические
    # («732 на момент закрытия фазы») трогать нельзя — они верны.
    total_claim_patterns = (
        r"pytest suite \*\*(\d+)\*\*",
        r"\*\*(\d+)\*\* (?:теста|тестов|tests)",
        r"(\d+) (?:теста|тестов) в suite",
        r"(\d+) (?:теста|тестов) в \d+ файл",
        r"(\d+) passed",
        r"\*\*Тесты:\*\* (\d+)",
        r"\*\*Тесты:\*\* \*\*(\d+)\*\*",
        r"(\d+) \(`pytest --collect-only",
        r"(\d+) total\*\* в suite",
    )
    # Строки, где число заведомо не про весь прогон: исторические сводки и
    # прогоны подмножества («47 passed on validation-related tests»).
    SCOPED = ("на момент", "было ", "(было", "targeted", "Ran:", "в аналитических модул")

    def is_scoped(line: str) -> bool:
        """True, если число на строке относится к подмножеству, а не ко всему прогону."""
        if any(marker in line for marker in SCOPED):
            return True
        # Строка перечисляет конкретные тест-файлы → это прогон подмножества.
        return bool(re.search(r"test_[a-z_]+\.py", line))

    stale = []
    for md in md_files():
        if md.name == "session_notes.md":
            continue  # журнал сессий: числа там намеренно зафиксированы прошлым
        for line in md.read_text(encoding="utf-8").splitlines():
            if is_scoped(line):
                continue
            for pat in total_claim_patterns:
                for num in re.findall(pat, line):
                    if int(num) != total:
                        stale.append(f"{md.relative_to(ROOT)}: {num} (ожидалось {total})")
    check(
        f"счётчик всего прогона везде равен {total}",
        not stale,
        "; ".join(sorted(set(stale))[:6]),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Сверка документации с кодом")
    ap.add_argument("--quiet", action="store_true", help="печатать только провалы")
    args = ap.parse_args()

    for fn in (
        check_links, check_markdown_hygiene, check_schema, check_events,
        check_export_aliases,
        check_locales, check_content, check_feature_flags,
        check_balance_constants, check_test_counts,
    ):
        try:
            fn()
        except Exception as e:  # проверка не должна падать молча
            check(f"{fn.__name__} выполнилась", False, f"{type(e).__name__}: {e}")

    failed = [r for r in results if r[1] == FAIL]
    for name, status, detail in results:
        if args.quiet and status != FAIL:
            continue
        mark = {PASS: "  ok  ", FAIL: " FAIL ", SKIP: " skip "}[status]
        print(f"[{mark}] {name}" + (f"  — {detail}" if detail and status != PASS else ""))

    print(f"\n{len(results) - len(failed)}/{len(results)} проверок пройдено")
    if failed:
        print("Документация разошлась с кодом. Поправь документ, а не проверку.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
