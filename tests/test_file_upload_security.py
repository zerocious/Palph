"""Tests for file_upload_security helpers and user task parser limits."""

import pytest
from pathlib import Path

from file_upload_security import (
    ZIP_MAX_UNCOMPRESSED_BYTES,
    decode_task_upload,
    resolve_path_under,
    safe_task_image_filename,
    sanitize_plain_preview,
    scan_upload_bytes,
    telegram_len,
    truncate_for_telegram_message,
    truncate_text,
    validate_subject_id,
    validate_task_document_metadata,
    validate_zip_member,
)
from user_task_txt import (
    MAX_ANSWER_LEN,
    MAX_PROBLEM_LEN,
    parse_user_tasks_txt,
)


class _FakeDoc:
    def __init__(self, file_name: str, mime_type: str | None = "text/plain", file_size: int | None = 10):
        self.file_name = file_name
        self.mime_type = mime_type
        self.file_size = file_size


class TestValidateSubjectId:
    def test_known(self):
        assert validate_subject_id("math") == "math"

    def test_unknown(self):
        assert validate_subject_id("..") is None
        assert validate_subject_id("evil") is None


class TestTaskDocumentMetadata:
    def test_valid_txt(self):
        assert validate_task_document_metadata(_FakeDoc("tasks.txt")) is None

    def test_rejects_path_in_name(self):
        assert validate_task_document_metadata(_FakeDoc("../x.txt")) == "user_tasks.need_txt"

    def test_rejects_bad_mime(self):
        assert validate_task_document_metadata(
            _FakeDoc("a.txt", mime_type="application/zip")
        ) == "user_tasks.need_txt"


class TestScanAndDecode:
    def test_rejects_zip_magic(self):
        assert scan_upload_bytes(b"\x50\x4b\x03\x04" + b"x" * 20) == "user_tasks.need_txt"

    def test_accepts_utf8_text(self):
        raw = "Q? || a\n".encode("utf-8")
        text, err = decode_task_upload(raw, 65536)
        assert err is None
        assert text == "Q? || a\n"

    def test_post_download_size(self):
        text, err = decode_task_upload(b"x" * 100, 50)
        assert text is None
        assert err == "user_tasks.file_too_big"


class TestSafeTaskImageFilename:
    def test_strips_directory(self):
        assert safe_task_image_filename("../../../etc/passwd.png", "task-01") == "passwd.png"

    def test_invalid_extension_fallback(self):
        assert safe_task_image_filename("evil.exe", "task-01") == "task-01-solution.png"


class TestResolvePathUnder:
    def test_blocks_traversal(self, tmp_path):
        base = tmp_path / "tasks"
        base.mkdir()
        assert resolve_path_under(base, "../outside.png") is None

    def test_allows_member(self, tmp_path):
        base = tmp_path / "tasks"
        base.mkdir()
        f = base / "task-01-solution.png"
        f.write_bytes(b"\x89PNG")
        resolved = resolve_path_under(base, "task-01-solution.png")
        assert resolved == f.resolve()


class TestParseUserTasksFieldLimits:
    def test_problem_too_long(self):
        line = "x" * (MAX_PROBLEM_LEN + 1) + " || a"
        tasks, errors = parse_user_tasks_txt(line)
        assert not tasks
        assert any("длинный" in e for e in errors)

    def test_answer_too_long(self):
        line = f"Q? || {'a' * (MAX_ANSWER_LEN + 1)}"
        tasks, errors = parse_user_tasks_txt(line)
        assert not tasks
        assert errors


class TestZipMemberValidation:
    def test_rejects_oversized(self):
        assert validate_zip_member(ZIP_MAX_UNCOMPRESSED_BYTES + 1, 1000) is False

    def test_rejects_high_ratio(self):
        assert validate_zip_member(1000, 1) is False

    def test_accepts_normal(self):
        assert validate_zip_member(1000, 500) is True


class TestUtf16LengthAccounting:
    """
    Telegram считает лимит сообщения в UTF-16 code units, а len() в Python —
    в кодовых точках. Любой не-BMP символ (эмодзи) весит 2 единицы вместо
    одной, и бот использует их повсеместно.

    Регрессия: пользователь слал 2048 эмодзи (ровно 4096 единиц — Telegram
    такое на вход пропускает), len() видел 2048 и обрезки не делал, к тексту
    добавлялся префикс «Новое сообщение от …», и итог уходил в 4147 единиц.
    Telegram отвечал 400, исключение проглатывалось `except Exception: pass`,
    админ не получал сообщение, а пользователю писали «отправлено».
    """

    def test_telegram_len_counts_utf16_units(self):
        assert telegram_len("abc") == 3
        assert telegram_len("🥇") == 2          # не-BMP → суррогатная пара
        assert telegram_len("é") == 1           # BMP → одна единица
        assert telegram_len("🥇" * 2048) == 4096

    def test_emoji_message_with_prefix_fits_telegram_limit(self):
        prefix = "📩 Новое сообщение от Иван Петров (ID: 123456789):\n"
        body = "🥇" * 2048  # максимум, который Telegram пропускает на вход
        out = truncate_for_telegram_message(prefix, body)
        assert telegram_len(prefix + out) <= 4096

    def test_surrogate_pairs_are_never_split(self):
        # Бюджет обрывается ровно посередине эмодзи — символ должен уйти целиком
        out = truncate_text("🥇" * 6, max_len=10)
        assert telegram_len(out) <= 10
        # Строка остаётся валидной: половинки суррогата не осталось
        assert out.encode("utf-16-le").decode("utf-16-le") == out

    def test_exact_limit_is_not_truncated(self):
        text = "🥇" * 5  # ровно 10 единиц
        assert truncate_text(text, max_len=10) == text

    def test_ascii_behaviour_unchanged(self):
        # Для ASCII единицы и кодовые точки совпадают — контракт прежний
        assert truncate_text("hello", max_len=10) == "hello"
        assert truncate_text("x" * 20, max_len=10) == "x" * 9 + "…"


class TestTextHelpers:
    def test_truncate_for_telegram_message(self):
        prefix = "header:\n"
        body = truncate_for_telegram_message(prefix, "x" * 5000)
        assert len(prefix + body) <= 4096

    def test_sanitize_plain_preview(self):
        assert "\n" not in sanitize_plain_preview("line1\nline2")
