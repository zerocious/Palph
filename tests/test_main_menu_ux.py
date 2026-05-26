"""Main menu layout: quizzes at top level, study submenu without quizzes."""
import importlib
from pathlib import Path

import pytest

from i18n import t

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def bot_mod():
    return importlib.import_module("bot")


def test_main_keyboard_has_prep_on_top_row(bot_mod):
    markup = bot_mod.get_main_keyboard("ru")
    rows = markup.keyboard
    assert len(rows) == 4
    assert len(rows[0]) == 1
    assert rows[0][0].text == t("kb.quizzes", "ru")
    assert "Подготовка" in rows[0][0].text
    assert len(rows[1]) == 1
    assert "Учебные инструменты" in rows[1][0].text


def test_main_keyboard_has_all_buttons(bot_mod):
    markup = bot_mod.get_main_keyboard("ru")
    texts = [btn.text for row in markup.keyboard for btn in row]
    assert t("kb.quizzes", "ru") in texts
    assert t("kb.study", "ru") in texts
    assert t("kb.news", "ru") in texts
    assert len(texts) == 5


def test_study_keyboard_has_no_quizzes(bot_mod):
    markup = bot_mod.get_study_keyboard("ru")
    texts = [btn.text for row in markup.keyboard for btn in row]
    assert t("kb.quizzes", "ru") not in texts
    assert t("kb.tips", "ru") in texts
    assert t("kb.standard_timer", "ru") in texts


def test_subject_keyboard_back_goes_to_main():
    text = (ROOT / "bot.py").read_text(encoding="utf-8")
    start = text.index("async def get_subject_keyboard")
    chunk = text[start : start + 400]
    assert "kb.back_main" in chunk
    assert "kb.back_study" not in chunk
