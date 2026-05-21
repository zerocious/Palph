"""
Smoke test для analysis/queries/*.sql.

Цель: убедиться, что каждый reference-query параметрически валиден против
текущей схемы init_db. Не проверяем корректность результата — данных нет.
Catches schema drift (например, кто-то переименовал колонку) до того,
как кто-то открывает .sql файл и недоумевает почему он не запускается.

Поведение:
- Каждый .sql файл выполняется отдельно через `executescript` (поддерживает
  multi-statement queries).
- Файл, начинающийся с numeric prefix (NN_*.sql) считается живой query'ёй.
- README.md и любые dot-files игнорируются.
- Stub'ы (например 08_pre_exam_engagement.sql) тоже должны парситься —
  они опираются на текущую схему через SELECT-literal, реальный body
  закомментирован.
"""
import os
import re
from pathlib import Path

import pytest


_QUERIES_DIR = Path(__file__).parent.parent / "analysis" / "queries"
_FILE_RE = re.compile(r"^\d{2}_[a-z_]+\.sql$")


def _list_query_files() -> list[Path]:
    return sorted(p for p in _QUERIES_DIR.iterdir() if _FILE_RE.match(p.name))


def test_queries_dir_exists():
    assert _QUERIES_DIR.is_dir(), f"missing {_QUERIES_DIR}"


def test_at_least_eight_queries_present():
    files = _list_query_files()
    assert len(files) >= 8, f"expected ≥8 reference queries, got {len(files)}: {files}"


def test_readme_exists():
    assert (_QUERIES_DIR / "README.md").is_file()


@pytest.mark.parametrize("sql_path", _list_query_files(), ids=lambda p: p.name)
async def test_query_executes_against_schema(db, sql_path):
    """
    Each reference query must run against an empty но valid schema (init_db).
    Allows empty result sets — we test the query parses + binds to existing
    columns, не корректность данных.
    """
    sql = sql_path.read_text(encoding="utf-8")
    # executescript позволяет multi-statement в одной строке.
    await db.executescript(sql)


def test_no_mutating_keywords_in_queries():
    """
    Reference set должен быть read-only. Запрещаем DML/DDL keywords на
    верхнем уровне (вне коммент-блоков) — защита от случайной правки,
    которая мутировала бы прод-БД при запуске.
    """
    forbidden = ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "ALTER ", "TRUNCATE ")
    for sql_path in _list_query_files():
        # Удаляем '--' line comments; block-comments в SQLite не поддерживаются.
        cleaned = "\n".join(
            line for line in sql_path.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("--")
        ).upper()
        for kw in forbidden:
            assert kw not in cleaned, f"{sql_path.name} contains forbidden keyword: {kw.strip()}"
