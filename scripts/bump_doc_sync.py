#!/usr/bin/env python3
"""
Проставляет сегодняшнюю дату в `Doc sync` каждого правленого документа.

Правило проекта: документ, который меняли, обязан обновить `Doc sync` в
шапке — `scripts/check_docs.py` это требование проверяет и краснеет ещё до
коммита. Руками это скучная работа на десяток файлов, поэтому скрипт.

    python scripts/bump_doc_sync.py            # изменённые (git status)
    python scripts/bump_doc_sync.py --dry-run  # показать, ничего не менять
    python scripts/bump_doc_sync.py docs/a.md  # только указанные файлы

Трогается **только шапка** (первые 12 строк) и только та дата, которая уже
там стоит: скрипт не добавляет `Doc sync` документам, у которых его нет —
это осознанное решение автора документа, а не забывчивость.
"""
from __future__ import annotations

import argparse
import datetime
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HEADER_LINES = 12
DOC_SYNC_RE = re.compile(r"(Doc sync[:*]*\s*\**\s*)(\d{4}-\d{2}-\d{2})")


def modified_markdown() -> list[str]:
    """Изменённые и новые .md по данным git."""
    proc = subprocess.run(
        ("git", "status", "--porcelain", "--", "*.md"),
        cwd=ROOT, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print("git status не отработал — укажите файлы аргументами.", file=sys.stderr)
        return []
    out = []
    for line in proc.stdout.splitlines():
        rel = line[3:].strip()
        if " -> " in rel:  # переименование
            rel = rel.split(" -> ", 1)[1]
        out.append(rel.strip('"'))
    return out


def bump(rel: str, today: str, dry_run: bool) -> str | None:
    """Возвращает описание изменения или None, если менять нечего."""
    path = ROOT / rel
    if not path.exists():
        return None
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    header = "".join(lines[:HEADER_LINES])
    m = DOC_SYNC_RE.search(header)
    if m is None or m.group(2) == today:
        return None
    new_header = header[:m.start(2)] + today + header[m.end(2):]
    if not dry_run:
        path.write_text(new_header + "".join(lines[HEADER_LINES:]), encoding="utf-8")
    return f"{rel}: {m.group(2)} → {today}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Обновить Doc sync в правленых документах")
    ap.add_argument("files", nargs="*", help="конкретные файлы (по умолчанию — git status)")
    ap.add_argument("--dry-run", action="store_true", help="показать, ничего не менять")
    args = ap.parse_args()

    today = datetime.date.today().isoformat()
    targets = args.files or modified_markdown()
    changed = [d for d in (bump(rel, today, args.dry_run) for rel in targets) if d]
    for line in changed:
        print(("[dry-run] " if args.dry_run else "") + line)
    print(f"{len(changed)} документ(ов) из {len(targets)} изменённых")
    return 0


if __name__ == "__main__":
    sys.exit(main())
