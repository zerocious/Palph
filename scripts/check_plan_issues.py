#!/usr/bin/env python3
"""Run plan health checks (content gate + unit tests)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from plan_service import (  # noqa: E402
    build_content_catalog,
    catalog_has_minimum,
    load_diagnostic,
    MIN_PLAN_ITEMS,
)


def main() -> int:
    catalog = build_content_catalog("industrial-management")
    diag = load_diagnostic("industrial-management")
    print(
        f"catalog={len(catalog)} diagnostic={len(diag)} "
        f"min_ok={catalog_has_minimum(catalog, MIN_PLAN_ITEMS)}"
    )

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_plan_service.py", "tests/test_plan_repository.py", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
