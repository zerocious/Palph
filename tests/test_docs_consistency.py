"""
Документация против кода — те же проверки, что в `scripts/check_docs.py`,
но внутри pytest, чтобы расхождение ловилось обычным прогоном, а не только
когда кто-то вспомнит про отдельный скрипт.

Проверка счётчиков тестов сюда намеренно не включена: она поднимает
`pytest --collect-only` подпроцессом, и внутри прогона это лишние секунды.
Её делает `python scripts/check_docs.py` (см. docs/testing.md).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_checker():
    spec = importlib.util.spec_from_file_location(
        "check_docs", ROOT / "scripts" / "check_docs.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_docs"] = module
    spec.loader.exec_module(module)
    return module


CHECKER = _load_checker()

# Всё, кроме check_test_counts (см. докстринг выше).
CHECKS = [
    CHECKER.check_links,
    CHECKER.check_markdown_hygiene,
    CHECKER.check_schema,
    CHECKER.check_events,
    CHECKER.check_export_aliases,
    CHECKER.check_locales,
    CHECKER.check_content,
    CHECKER.check_feature_flags,
    CHECKER.check_balance_constants,
]


@pytest.mark.parametrize("check_fn", CHECKS, ids=lambda f: f.__name__)
def test_documentation_matches_code(check_fn):
    CHECKER.results.clear()
    check_fn()
    failures = [
        f"{name}: {detail}" for name, status, detail in CHECKER.results
        if status == CHECKER.FAIL
    ]
    assert not failures, (
        "Документация разошлась с кодом — правь документ, а не проверку:\n  "
        + "\n  ".join(failures)
    )


def test_every_check_actually_asserts_something():
    """Проверка, которая ничего не записала в results, — пустышка."""
    for fn in CHECKS:
        CHECKER.results.clear()
        fn()
        assert CHECKER.results, f"{fn.__name__} не сделала ни одной проверки"
