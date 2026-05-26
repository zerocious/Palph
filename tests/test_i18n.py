"""i18n smoke and regression tests."""
import ast
import importlib
import json
from pathlib import Path

from i18n import SUPPORTED_LOCALES, all_locale_texts, subject_label, t
from locale_bot import SUBJECT_IDS

ROOT = Path(__file__).resolve().parent.parent
LOCALES_DIR = ROOT / "locales"


def test_locale_json_valid():
    for loc in SUPPORTED_LOCALES:
        data = json.loads((LOCALES_DIR / f"{loc}.json").read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        assert "kb" in data and "start" in data


def test_t_returns_russian_by_default():
    assert "Доброе утро" in t("reminders.morning", "ru")


def test_t_english_locale():
    assert "good morning" in t("reminders.morning", "en").lower()


def test_t_fallback_to_ru_for_missing_en_key():
    assert t("nonexistent.key.xyz", "en") == "nonexistent.key.xyz"


def test_all_locale_texts_kb_keys():
    texts = all_locale_texts("kb.faq")
    assert len(texts) == len(SUPPORTED_LOCALES)
    assert "❓ FAQ" in texts


def test_language_picker_keys_exist():
    for loc in SUPPORTED_LOCALES:
        assert t("lang.picker_title", loc)
        assert t("lang.picker_title_bilingual", loc)
        assert t("lang.ru", loc)
        assert t("lang.en", loc)


def test_fc_keys_both_locales():
    for loc in SUPPORTED_LOCALES:
        assert t("fc.add_btn", loc)
        assert t("fc.list_btn", loc)
        assert t("fc.term_empty", loc)


def test_lang_saved_format_does_not_shadow_t_locale_param():
    """Regression: lang.saved must not pass locale= into t() format kwargs."""
    for loc in SUPPORTED_LOCALES:
        lang_name = t("lang.ru", loc) if loc == "ru" else t("lang.en", loc)
        msg = t("lang.saved", loc, lang_name=lang_name)
        assert msg != "lang.saved"
        assert lang_name in msg


def test_common_unexpected_error_translated():
    for loc in SUPPORTED_LOCALES:
        val = t("common.unexpected_error", loc)
        assert val != "common.unexpected_error"


def test_critical_keys_not_self_fallback():
    keys = [
        "user_tasks.instruction",
        "user_tasks.import_cancelled",
        "mcq.stopped",
        "task.done",
        "flash.stopped",
        "friends.invite_invalid",
        "timer.finished",
        "common.cancelled",
        "common.unexpected_error",
    ]
    for key in keys:
        for loc in SUPPORTED_LOCALES:
            val = t(key, loc)
            assert val != key, f"missing {key} for {loc}"


def test_bot_handlers_no_undefined_user_id_for_loc():
    """Regression: handlers must define user_id before await loc(user_id)."""
    src = (ROOT / "bot.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    issues = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        assigned = {a.arg for a in node.args.args}
        for n in ast.walk(node):
            if isinstance(n, ast.Assign):
                for target in n.targets:
                    if isinstance(target, ast.Name):
                        assigned.add(target.id)
            elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
                assigned.add(n.target.id)
            elif isinstance(n, (ast.For, ast.AsyncFor)) and isinstance(n.target, ast.Name):
                assigned.add(n.target.id)
        code = ast.get_source_segment(src, node) or ""
        if "await loc(user_id)" in code and "user_id" not in assigned:
            issues.append(node.name)
    assert issues == [], f"handlers with undefined user_id: {issues}"


def test_build_progress_view_no_loc_shadow():
    """build_progress_view must use user_loc, not shadow loc() helper."""
    src = (ROOT / "bot.py").read_text(encoding="utf-8")
    assert "user_loc = locale or await loc(user_id)" in src
    assert "return t(\"start.need_register\", loc)" not in src


def test_subject_button_labels_registered_for_all_locales():
    """Every localized subject label must map back to its subject_id."""
    bot = importlib.import_module("bot")
    texts = bot._all_subject_button_texts()
    assert len(texts) == len(SUBJECT_IDS) * len(SUPPORTED_LOCALES)
    for sid in SUBJECT_IDS:
        for loc in SUPPORTED_LOCALES:
            label = subject_label(sid, loc)
            assert label in texts
            assert bot.subject_id_from_button(label) == sid


def test_subject_picked_handler_not_gated_by_fsm_state():
    """Regression: subject clicks must not require QuizStates.choosing_subject."""
    src = (ROOT / "bot.py").read_text(encoding="utf-8")
    assert "async def handle_subject_picked" in src
    assert "QuizStates.choosing_subject, F.text.in_(_all_subject_button_texts())" not in src
    assert "F.text.in_(_all_subject_button_texts())" in src


def test_industrial_management_en_label():
    en = subject_label("industrial-management", "en")
    ru = subject_label("industrial-management", "ru")
    assert "Production management" in en
    assert "производственного" in ru
    assert en != ru
