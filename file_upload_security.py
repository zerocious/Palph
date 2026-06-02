"""
File upload and filesystem-read hardening for Telegram document imports
and study-materials path resolution.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from locale_bot import SUBJECT_IDS

if TYPE_CHECKING:
    from aiogram.types import Document

# --- User task .txt upload ---
ALLOWED_TASK_MIME = frozenset({"text/plain", "application/octet-stream"})
TASK_FILENAME_RE = re.compile(r"^[A-Za-z0-9._ -]{1,128}\.txt$", re.IGNORECASE)

BINARY_MAGIC_PREFIXES = (
    b"\x50\x4b\x03\x04",  # ZIP
    b"\x7fELF",
    b"\x89PNG\r\n\x1a\n",
    b"\x89PNG",
    b"GIF87a",
    b"GIF89a",
    b"%PDF-",
    b"\x4d\x5a",
)

MAX_NUL_RATIO = 0.05

# --- Task JSON image names (content-supply chain) ---
TASK_IMAGE_RE = re.compile(r"^[\w.-]+\.png$", re.IGNORECASE)

# --- Pet asset keys (DB → filesystem) ---
PET_EMOTIONS = frozenset({"neutral", "joy", "sad"})
# Legacy emotion names → new asset stems (backward compat for old PNG/GIF files)
PET_EMOTION_LEGACY_FILES: dict[str, tuple[str, ...]] = {
    "joy": ("happy", "excited"),
    "neutral": ("sleepy", "studying"),
    "sad": (),
}
# Legacy input names → normalized emotion (derive_emotion / caller strings)
PET_EMOTION_LEGACY_INPUT: dict[str, str] = {
    "happy": "joy",
    "excited": "joy",
    "sleepy": "neutral",
    "studying": "neutral",
}
PET_COLORS = frozenset({"orange", "grey", "blue", "green", "pink"})
PET_ACCESSORIES = frozenset({"none", "hat", "glasses", "scarf", "crown"})
PET_TIME_PERIODS = frozenset({"morning", "day", "evening", "night"})

# --- ZIP (future inbound archives) ---
ZIP_MAX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
ZIP_MAX_COMPRESSION_RATIO = 20
ZIP_MAX_MEMBER_COUNT = 500

# Telegram Bot API message text limit (UTF-16 code units; safe cap for plain text).
TELEGRAM_MAX_MESSAGE_LEN = 4096
FRIEND_QUERY_MAX_LEN = 64
SUPPORT_MESSAGE_MAX_LEN = TELEGRAM_MAX_MESSAGE_LEN
LIST_PREVIEW_MAX_LEN = 80


def truncate_text(
    text: str,
    max_len: int = TELEGRAM_MAX_MESSAGE_LEN,
    suffix: str = "…",
) -> str:
    """Trim text to max_len, appending suffix when truncated."""
    if max_len <= 0:
        return ""
    if len(text) <= max_len:
        return text
    if len(suffix) >= max_len:
        return text[:max_len]
    return text[: max_len - len(suffix)] + suffix


def sanitize_plain_preview(text: str, max_len: int = LIST_PREVIEW_MAX_LEN) -> str:
    """Single-line preview for plain-text lists (no HTML parse mode)."""
    collapsed = " ".join((text or "").split())
    return truncate_text(collapsed, max_len)


def truncate_for_telegram_message(
    prefix: str,
    body: str,
    max_len: int = TELEGRAM_MAX_MESSAGE_LEN,
) -> str:
    """Truncate body so prefix + body fits Telegram message limit."""
    max_body = max(0, max_len - len(prefix))
    return truncate_text(body, max_len=max_body)


def validate_subject_id(subject_id: str) -> str | None:
    """Return subject_id if allowlisted, else None."""
    return subject_id if subject_id in SUBJECT_IDS else None


def validate_task_document_metadata(doc: Document) -> str | None:
    """
    Pre-download metadata checks.
    Returns i18n key (e.g. 'user_tasks.need_txt') or None if OK.
    """
    name = (doc.file_name or "").strip()
    if not TASK_FILENAME_RE.match(name):
        return "user_tasks.need_txt"
    if doc.mime_type and doc.mime_type not in ALLOWED_TASK_MIME:
        return "user_tasks.need_txt"
    return None


def scan_upload_bytes(raw: bytes) -> str | None:
    """
    Lightweight content scan (no external AV).
    Returns i18n key or None if OK.
    """
    if not raw:
        return "user_tasks.empty_file"
    for magic in BINARY_MAGIC_PREFIXES:
        if raw.startswith(magic):
            return "user_tasks.need_txt"
    nul_count = raw.count(b"\x00")
    if nul_count / len(raw) > MAX_NUL_RATIO:
        return "user_tasks.need_txt"
    return None


def decode_task_upload(raw: bytes, max_bytes: int) -> tuple[str | None, str | None]:
    """
    Post-download size cap, scan, UTF-8 decode.
    Returns (text, error_i18n_key).
    """
    if len(raw) > max_bytes:
        return None, "user_tasks.file_too_big"
    scan_err = scan_upload_bytes(raw)
    if scan_err:
        return None, scan_err
    try:
        return raw.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, "user_tasks.read_error"


def safe_subject_dir(materials_root: Path, subject_id: str) -> Path | None:
    """Resolved study_materials/<subject_id> or None if invalid / escapes root."""
    if validate_subject_id(subject_id) is None:
        return None
    root = materials_root.resolve()
    base = (root / subject_id).resolve()
    try:
        base.relative_to(root)
    except ValueError:
        return None
    return base if base.is_dir() else base  # may not exist yet for user-only subjects


def safe_task_image_filename(name: str, task_id: str) -> str:
    """Basename-only PNG under tasks/ directory."""
    base = Path(str(name)).name
    if not TASK_IMAGE_RE.fullmatch(base):
        return f"{task_id}-solution.png"
    return base


def resolve_path_under(directory: Path, filename: str) -> Path | None:
    """Resolve filename inside directory; None if traversal escapes."""
    directory = directory.resolve()
    candidate = (directory / filename).resolve()
    try:
        candidate.relative_to(directory)
    except ValueError:
        return None
    return candidate


def normalize_pet_emotion(emotion: str) -> str:
    """Map legacy emotion names to the current 3-emotion set."""
    if emotion in PET_EMOTIONS:
        return emotion
    return PET_EMOTION_LEGACY_INPUT.get(emotion, "neutral")


def pet_emotion_file_stems(emotion: str) -> tuple[str, ...]:
    """Asset filename stems: new name first, then legacy fallbacks."""
    normalized = normalize_pet_emotion(emotion)
    stems = [normalized]
    stems.extend(PET_EMOTION_LEGACY_FILES.get(normalized, ()))
    return tuple(dict.fromkeys(stems))


def sanitize_pet_asset_keys(
    emotion: str,
    color: str,
    accessory: str,
) -> tuple[str, str, str]:
    """Clamp pet render keys to known catalog values."""
    emotion = normalize_pet_emotion(emotion)
    if color not in PET_COLORS:
        color = "orange"
    if accessory not in PET_ACCESSORIES:
        accessory = "none"
    return emotion, color, accessory


def sanitize_pet_time_period(period: str | None) -> str | None:
    """Return period if allowlisted, else None (skip period-specific lookup)."""
    if period in PET_TIME_PERIODS:
        return period
    return None


def validate_zip_member(
    uncompressed_size: int,
    compress_size: int,
    *,
    max_uncompressed: int = ZIP_MAX_UNCOMPRESSED_BYTES,
    max_ratio: int = ZIP_MAX_COMPRESSION_RATIO,
) -> bool:
    """Return False if member looks like a zip bomb (for future inbound ZIP)."""
    if uncompressed_size < 0 or compress_size < 0:
        return False
    if uncompressed_size > max_uncompressed:
        return False
    if compress_size > 0 and uncompressed_size / compress_size > max_ratio:
        return False
    return True
