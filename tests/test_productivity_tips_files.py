"""Productivity tips files must exist next to bot.py (cwd-independent paths)."""
from pathlib import Path

import bot


def test_productivity_tips_files_exist():
    for path in (
        bot.TIME_MANAGEMENT_TIPS_FILE,
        bot.MEMORY_RETENTION_TIPS_FILE,
        bot.PRODUCTIVITY_LINKS_FILE,
    ):
        assert path.is_file(), f"missing: {path}"


def test_productivity_tips_files_are_non_empty():
    for path in (
        bot.TIME_MANAGEMENT_TIPS_FILE,
        bot.MEMORY_RETENTION_TIPS_FILE,
        bot.PRODUCTIVITY_LINKS_FILE,
    ):
        assert path.read_text(encoding="utf-8").strip(), f"empty: {path}"


def test_time_management_has_tip_lines():
    lines = [
        line.strip()
        for line in bot.TIME_MANAGEMENT_TIPS_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(lines) >= 1
