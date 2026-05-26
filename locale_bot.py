"""
Хелперы локализации для bot.py и services.py.
"""
from __future__ import annotations

import json
from pathlib import Path

from aiogram.types import BotCommand

from i18n import (
    DEFAULT_LOCALE,
    SUPPORTED_LOCALES,
    quiz_section_label,
    study_mode_label,
    subject_label,
    t,
)

BOT_DIR = Path(__file__).parent
TIPS_DIR = BOT_DIR / "tips"

FAQ_IDS = (
    "mission",
    "efficiency",
    "pet",
    "spend_coins",
    "earn_coins",
    "sm2",
    "spaced_rep",
    "active_recall",
    "guarantee",
    "why_free",
)

SUBJECT_IDS = ("industrial-management", "math", "accounting", "english")
STUDY_MODE_IDS = ("situational", "flashcards", "mcq", "tasks")
QUIZ_SECTION_KEYS = ("i", "ii", "iii", "iv")


def normalize_locale(locale: str | None) -> str:
    if locale in SUPPORTED_LOCALES:
        return locale
    return DEFAULT_LOCALE


async def user_locale(user_repo, user_id: int) -> str:
    loc = await user_repo.get_locale(user_id)
    return normalize_locale(loc or None)


def faq_items(locale: str) -> list[dict[str, str]]:
    loc = normalize_locale(locale)
    items = []
    for item_id in FAQ_IDS:
        items.append({
            "id": item_id,
            "btn": t(f"faq.{item_id}.btn", loc),
            "title": t(f"faq.{item_id}.title", loc),
            "body": t(f"faq.{item_id}.body", loc),
        })
    return items


def load_achievements_catalog(locale: str) -> dict:
    loc = normalize_locale(locale)
    for filename in (f"achievements.{loc}.json", "achievements.json"):
        path = BOT_DIR / filename
        if path.is_file():
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    return {}


def _load_tips_json(filename: str, locale: str) -> list[dict]:
    for base in (TIPS_DIR / locale, TIPS_DIR):
        path = base / filename
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            return [x for x in data.get("tips", []) if x.get("title") and x.get("body")]
    return []


def tip_categories(locale: str) -> dict[str, dict]:
    loc = normalize_locale(locale)
    return {
        "tm": {
            "tips": _load_tips_json("time-management.json", loc),
            "emoji": "⏰",
            "title": t("tips.tm", loc),
        },
        "mem": {
            "tips": _load_tips_json("memory.json", loc),
            "emoji": "🧠",
            "title": t("tips.mem", loc),
        },
        "bot": {
            "tips": _load_tips_json("bot-guide.json", loc),
            "emoji": "🎯",
            "title": t("tips.bot", loc),
        },
    }


def commands_for_locale(locale: str) -> list[BotCommand]:
    loc = normalize_locale(locale)
    return [
        BotCommand(command="start", description=t("commands.start", loc)),
        BotCommand(command="stop", description=t("commands.stop", loc)),
        BotCommand(command="progress", description=t("commands.progress", loc)),
        BotCommand(command="pet", description=t("commands.pet", loc)),
        BotCommand(command="leaderboard", description=t("commands.leaderboard", loc)),
        BotCommand(command="friends", description=t("commands.friends", loc)),
        BotCommand(command="share_friend", description=t("commands.share_friend", loc)),
        BotCommand(command="delete_account", description=t("commands.delete_account", loc)),
    ]


def pet_emotion(streak: int, locale: str) -> str:
    loc = normalize_locale(locale)
    if streak == 0:
        return t("pet.emotion_0", loc)
    if streak < 3:
        return t("pet.emotion_low", loc)
    if streak < 7:
        return t("pet.emotion_mid", loc)
    return t("pet.emotion_high", loc)


def flash_source_labels(locale: str) -> dict[str, str]:
    loc = normalize_locale(locale)
    return {
        "mix": t("settings.flash_mix", loc),
        "official": t("settings.flash_official", loc),
        "own": t("settings.flash_own", loc),
    }


FLASH_SOURCE_CYCLE = ["mix", "official", "own"]
