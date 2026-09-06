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


# ---------------------------------------------------------------- schema
def check_schema() -> None:
    db = read("db.py")
    tables = db.count("CREATE TABLE IF NOT EXISTS")
    indexes = db.count("CREATE INDEX IF NOT EXISTS")
    dm = read("docs/data-model.md")
    check(
        f"docs/data-model: {tables} таблиц",
        f"**{tables} таблиц**" in dm or f"{tables} таблиц" in dm,
        f"в db.py {tables}",
    )
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
    code = set(re.findall(r'event_repo\.log\(\s*[^,]+,\s*\n?\s*"([a-z_]+)"', src))
    doc = read("docs/analytics.md")
    missing = sorted(e for e in code if f"`{e}`" not in doc)
    check("все события описаны в docs/analytics.md", not missing, ", ".join(missing))


# ---------------------------------------------------------------- export
def check_export_aliases() -> None:
    services = read("services.py")
    blk = services[services.index("EXPORTABLE_TABLES"):]
    blk = blk[blk.index("{"):blk.index("}") + 1]
    aliases = set(re.findall(r'"([a-z_]+)":', blk))
    for doc in ("docs/analytics.md", "admin_commands.md"):
        text = read(doc)
        missing = sorted(a for a in aliases if f"`{a}`" not in text)
        check(f"{doc}: все {len(aliases)} алиасов /export", not missing, ", ".join(missing))


# ---------------------------------------------------------------- locales
def check_locales() -> None:
    def leaves(d: dict) -> int:
        return sum(leaves(v) if isinstance(v, dict) else 1 for v in d.values())

    ru = json.loads(read("locales/ru.json"))
    en = json.loads(read("locales/en.json"))
    check("паритет групп ru/en", ru.keys() == en.keys())
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
        check(f"{doc}: {len(tasks)} задач по математике", f"{len(tasks)}" in text)
        check(f"{doc}: {len(hints)} подсказок", f"{len(hints)}" in text)


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
        check_links, check_schema, check_events, check_export_aliases,
        check_locales, check_content, check_feature_flags, check_test_counts,
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
