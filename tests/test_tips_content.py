"""Структурированный контент советов (tips/*.json)."""
import json
from pathlib import Path

import bot
from html import escape


TIPS_DIR = Path(__file__).resolve().parent.parent / "tips"


def test_tips_json_files_exist():
    for name in ("time-management.json", "memory.json", "links.json"):
        assert (TIPS_DIR / name).is_file()


def test_cached_tips_have_required_fields():
    for tips in (bot.TIME_MANAGEMENT_TIPS, bot.MEMORY_RETENTION_TIPS):
        assert len(tips) >= 1
        for tip in tips:
            assert tip.get("id")
            assert tip.get("title")
            assert tip.get("body")
            assert isinstance(tip.get("tags"), list)


def test_format_tip_message_uses_html_title():
    tip = bot.TIME_MANAGEMENT_TIPS[0]
    text = bot._format_tip_message("tm", tip, "ru")
    assert "<b>" in text
    assert escape(tip["title"]) in text
    assert "Попробуй сегодня" in text


def test_links_are_dicts_with_url():
    assert len(bot.PRODUCTIVITY_LINKS) >= 1
    for link in bot.PRODUCTIVITY_LINKS:
        assert link.get("url", "").startswith("http")


def test_json_schema_sample():
    data = json.loads((TIPS_DIR / "memory.json").read_text(encoding="utf-8"))
    assert data["category"] == "mem"
    assert len(data["tips"]) >= 10
