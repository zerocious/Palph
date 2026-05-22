"""
Локализация UI бота: ru / en.
Строки в locales/<locale>.json, вложенные ключи через точку: t("start.welcome_new", "en").
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

LOCALES_DIR = Path(__file__).parent / "locales"
SUPPORTED_LOCALES = ("ru", "en")
DEFAULT_LOCALE = "ru"


@lru_cache(maxsize=8)
def _load_bundle(locale: str) -> dict:
    path = LOCALES_DIR / f"{locale}.json"
    if not path.is_file():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _resolve(data: dict, key: str) -> str | None:
    node = data
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    if isinstance(node, str):
        return node
    return None


def t(key: str, locale: str = DEFAULT_LOCALE, **kwargs) -> str:
    """Перевод с fallback ru → ключ."""
    loc = locale if locale in SUPPORTED_LOCALES else DEFAULT_LOCALE
    text = _resolve(_load_bundle(loc), key)
    if text is None and loc != DEFAULT_LOCALE:
        text = _resolve(_load_bundle(DEFAULT_LOCALE), key)
    if text is None:
        text = key
    if kwargs:
        try:
            return text.format(**kwargs)
        except KeyError:
            return text
    return text


def all_locale_texts(key: str) -> list[str]:
    """Все варианты текста кнопки для F.text.in_(...)."""
    return [t(key, loc) for loc in SUPPORTED_LOCALES]


def kb_in(*keys: str):
    """Фильтр aiogram для reply-кнопок по i18n-ключам."""
    from aiogram import F

    texts: list[str] = []
    for key in keys:
        texts.extend(all_locale_texts(key))
    return F.text.in_(texts)


def subject_label(subject_id: str, locale: str) -> str:
    return t(f"subjects.{subject_id}", locale)


def study_mode_label(mode_id: str, locale: str) -> str:
    return t(f"study_modes.{mode_id}", locale)


def quiz_section_label(section_key: str, locale: str) -> str:
    return t(f"quiz_sections.{section_key}", locale)
