"""Productivity tips: JSON cache + legacy txt fallback paths."""
from pathlib import Path

import bot


def test_tips_dir_json_files_exist():
    tips_dir = bot.TIPS_DIR
    assert (tips_dir / "time-management.json").is_file()
    assert (tips_dir / "memory.json").is_file()
    assert (tips_dir / "links.json").is_file()


def test_legacy_txt_files_exist_for_fallback():
    for path in (
        bot.TIME_MANAGEMENT_TIPS_FILE,
        bot.MEMORY_RETENTION_TIPS_FILE,
        bot.PRODUCTIVITY_LINKS_FILE,
    ):
        assert path.is_file(), f"missing: {path}"


def test_productivity_tips_cache_non_empty():
    assert len(bot.TIME_MANAGEMENT_TIPS) >= 1
    assert len(bot.MEMORY_RETENTION_TIPS) >= 1
    assert len(bot.PRODUCTIVITY_LINKS) >= 1


def test_productivity_links_have_title_and_url():
    for link in bot.PRODUCTIVITY_LINKS:
        assert link["title"]
        assert link["url"].startswith("http")


def test_tip_categories_reference_cache():
    assert bot.TIP_CATEGORIES["tm"]["tips"] is bot.TIME_MANAGEMENT_TIPS
    assert bot.TIP_CATEGORIES["mem"]["tips"] is bot.MEMORY_RETENTION_TIPS


def test_tips_inline_keyboard_random_mode():
    markup = bot._tips_inline_keyboard("tm")
    texts = [b.text for row in markup.inline_keyboard for b in row]
    assert "🔄 Ещё совет" in texts
    assert "📋 Все советы" in texts
    assert "⬅️ К категориям" in texts


def test_tips_inline_keyboard_list_mode_has_pagination():
    markup = bot._tips_inline_keyboard("tm", list_page=1, list_total=5)
    callbacks = [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]
    assert "tips:list:tm:0" in callbacks
    assert "tips:list:tm:2" in callbacks
    assert "tips:more:tm" in callbacks
