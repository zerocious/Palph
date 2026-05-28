"""i18n smoke and regression tests."""
import ast
import importlib
import json
import re
from pathlib import Path

from i18n import SUPPORTED_LOCALES, all_locale_texts, subject_label, t
from locale_bot import SUBJECT_IDS

ROOT = Path(__file__).resolve().parent.parent
LOCALES_DIR = ROOT / "locales"

I18N_SOURCE_FILES = (
    ROOT / "bot.py",
    ROOT / "plan_handlers.py",
    ROOT / "locale_bot.py",
    ROOT / "services.py",
)
T_KEY_RE = re.compile(
    r"""t\(\s*[\"']([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]+)+)[\"']""",
    re.IGNORECASE,
)


def _flatten_locale_keys(data: dict, prefix: str = "") -> set[str]:
    out: set[str] = set()
    for key, value in data.items():
        full = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            out |= _flatten_locale_keys(value, full)
        else:
            out.add(full)
    return out


def _collect_t_keys_from_sources() -> set[str]:
    keys: set[str] = set()
    for path in I18N_SOURCE_FILES:
        if path.exists():
            keys |= set(T_KEY_RE.findall(path.read_text(encoding="utf-8")))
    return keys


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


def test_settings_language_btn_exists():
    for loc in SUPPORTED_LOCALES:
        label = t("settings.language_btn", loc, current=t("lang.ru", loc))
        assert label != "settings.language_btn", f"missing settings.language_btn for {loc}"
        assert "{current}" not in label


def test_fc_keys_both_locales():
    for loc in SUPPORTED_LOCALES:
        assert t("fc.add_btn", loc)
        assert t("fc.list_btn", loc)
        assert t("fc.term_empty", loc)


def test_lang_saved_format_uses_lang_name_kwarg():
    """Regression: lang.saved must use lang_name= (locale= shadows t()'s locale param)."""
    for loc in SUPPORTED_LOCALES:
        lang_name = t("lang.ru", loc) if loc == "ru" else t("lang.en", loc)
        msg = t("lang.saved", loc, lang_name=lang_name)
        assert msg != "lang.saved"
        assert lang_name in msg
        assert "{lang_name}" not in msg


def test_common_unexpected_error_translated():
    for loc in SUPPORTED_LOCALES:
        val = t("common.unexpected_error", loc)
        assert val != "common.unexpected_error"


def test_rating_thanks_translated_with_emoji():
    """Regression: rating.thanks must resolve after locale rebuild (not raw key)."""
    for loc in SUPPORTED_LOCALES:
        msg = t("rating.thanks", loc, emoji="🙂")
        assert msg != "rating.thanks"
        assert "🙂" in msg
        assert "{emoji}" not in msg


def test_rating_save_failed_translated():
    for loc in SUPPORTED_LOCALES:
        val = t("rating.save_failed", loc)
        assert val != "rating.save_failed"


def test_timer_rating_prompt_and_skip_translated():
    for loc in SUPPORTED_LOCALES:
        assert t("timer.rating_prompt", loc) != "timer.rating_prompt"
        assert t("timer.rating_skip", loc) != "timer.rating_skip"


def test_critical_keys_not_self_fallback():
    keys = [
        "user_tasks.instruction",
        "user_tasks.import_cancelled",
        "mcq.stopped",
        "task.done",
        "flash.stopped",
        "friends.invite_invalid",
        "timer.finished",
        "timer.rating_prompt",
        "timer.rating_skip",
        "timer.reconcile_resumed",
        "timer.reconcile_finished",
        "rating.thanks",
        "rating.save_failed",
        "common.cancelled",
        "common.unexpected_error",
        "settings.language_btn",
        "quiz.answer_prompt",
        "delete_account.done",
        "commands.delete_account",
    ]
    for key in keys:
        for loc in SUPPORTED_LOCALES:
            val = t(key, loc)
            assert val != key, f"missing {key} for {loc}"


def test_all_source_t_keys_exist_in_locales():
    """Regression: every t('dotted.key') in bot sources must resolve in ru/en JSON."""
    used = _collect_t_keys_from_sources()
    assert used, "expected at least one t() key in source files"
    locale_keys = {
        loc: _flatten_locale_keys(
            json.loads((LOCALES_DIR / f"{loc}.json").read_text(encoding="utf-8"))
        )
        for loc in SUPPORTED_LOCALES
    }
    missing = {
        loc: sorted(used - locale_keys[loc]) for loc in SUPPORTED_LOCALES
    }
    problems = {loc: keys for loc, keys in missing.items() if keys}
    assert not problems, f"missing locale keys: {problems}"


def test_timer_reconcile_resumed_translated():
    for loc in SUPPORTED_LOCALES:
        msg = t("timer.reconcile_resumed", loc, remaining=12)
        assert msg != "timer.reconcile_resumed"
        assert "12" in msg
        assert "{remaining}" not in msg


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
