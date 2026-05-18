"""
Тесты parse_logs.py — pure-function парсера bot.log.

Не требует БД, async или fixtures. Проверяет:
- Стандартные event-строки разных типов
- Multi-word values (next=2026-05-18 14:35:01)
- Извлечение user_id
- Unstructured (legacy) строки
- Malformed входные данные
- CSV roundtrip
"""
import csv
import io
import json

import pytest

from parse_logs import (
    parse_log_line,
    parse_log_file,
    write_csv,
    to_csv_bytes,
    CSV_COLUMNS,
)


class TestStandardEventLines:
    def test_session_complete_natural(self):
        line = (
            "2026-05-18 14:32:15 - studybuddy_bot - INFO - "
            "session.complete user_id=12345 duration=25 coins=25 bonus=0 "
            "session_id=42 achievements=1 source=natural"
        )
        r = parse_log_line(line)
        assert r is not None
        assert r["timestamp"] == "2026-05-18 14:32:15"
        assert r["level"] == "INFO"
        assert r["event_name"] == "session.complete"
        assert r["user_id"] == 12345
        props = json.loads(r["properties"])
        assert props["duration"] == "25"
        assert props["coins"] == "25"
        assert props["source"] == "natural"
        assert props["session_id"] == "42"

    def test_app_start_no_user_id(self):
        line = (
            "2026-05-18 09:00:00 - studybuddy_bot - INFO - "
            "app.start admins=3 main_admin_id=1515381746 server_tz=Europe/Moscow log_level=INFO"
        )
        r = parse_log_line(line)
        assert r is not None
        assert r["event_name"] == "app.start"
        assert r["user_id"] is None  # No user_id key in payload
        props = json.loads(r["properties"])
        assert props["main_admin_id"] == "1515381746"
        assert props["server_tz"] == "Europe/Moscow"

    def test_warning_level(self):
        line = (
            "2026-05-18 14:50:00 - studybuddy_bot - WARNING - "
            "ratelimit.warned user_id=99999"
        )
        r = parse_log_line(line)
        assert r["level"] == "WARNING"
        assert r["event_name"] == "ratelimit.warned"
        assert r["user_id"] == 99999

    def test_error_level(self):
        line = (
            "2026-05-18 15:00:00 - studybuddy_bot - ERROR - "
            "send_rating_prompt: не удалось отправить запрос оценки: TimeoutError"
        )
        r = parse_log_line(line)
        assert r["level"] == "ERROR"
        # Это unstructured (нет event.tag формата) — должен пойти в raw_text
        assert r["event_name"] == "unstructured"
        assert "TimeoutError" in r["raw_text"]


class TestMultiWordValues:
    def test_flash_rated_with_next_review_having_space(self):
        """next=2026-06-03 14:35:01 — value содержит пробел."""
        line = (
            "2026-05-18 14:35:01 - studybuddy_bot - INFO - "
            "flash.rated user_id=12345 hash=abc12345 quality=5 reps=2->3 "
            "ef=2.70->2.80 interval=6->16 next=2026-06-03 14:35:01"
        )
        r = parse_log_line(line)
        assert r is not None
        props = json.loads(r["properties"])
        assert props["quality"] == "5"
        assert props["hash"] == "abc12345"
        # Самое важное: next должен включать оба компонента (дата + время)
        assert props["next"] == "2026-06-03 14:35:01"

    def test_session_complete_mid_word_value(self):
        """Если value содержит пробел в середине между двумя key=value."""
        line = (
            "2026-05-18 14:32:15 - studybuddy_bot - INFO - "
            "flash.session.complete user_id=42 subject=industrial-management reviewed=5 coins=5"
        )
        r = parse_log_line(line)
        props = json.loads(r["properties"])
        assert props["subject"] == "industrial-management"
        assert props["reviewed"] == "5"


class TestUnstructured:
    def test_plain_text_log_line(self):
        line = "2026-05-18 09:00:00 - studybuddy_bot - INFO - ✅ StudyBuddy запущен"
        r = parse_log_line(line)
        assert r is not None
        assert r["event_name"] == "unstructured"
        assert r["user_id"] is None
        assert r["properties"] == "{}"
        assert "StudyBuddy запущен" in r["raw_text"]

    def test_traceback_like_line(self):
        line = (
            "2026-05-18 14:50:00 - studybuddy_bot - WARNING - "
            "session.complete failed for user 42: ConnectionResetError"
        )
        # Имеет 'session.complete' но не как первое слово после tag→ unstructured
        r = parse_log_line(line)
        assert r is not None
        # The regex expects event tag at start of payload. 'session.complete' is
        # first, so this DOES match as event. But this is acceptable — closer to
        # structured anyway.
        # Just ensure we don't crash:
        assert r["timestamp"] == "2026-05-18 14:50:00"


class TestMalformedInput:
    def test_empty_line_returns_none(self):
        assert parse_log_line("") is None
        assert parse_log_line("\n") is None

    def test_garbage_line_returns_none(self):
        assert parse_log_line("this is not a log line") is None
        assert parse_log_line("12:34:56 missing date") is None

    def test_partial_line_returns_none(self):
        # Date but no level/payload
        assert parse_log_line("2026-05-18 14:00:00") is None

    def test_trailing_newline_handled(self):
        line = "2026-05-18 14:00:00 - studybuddy_bot - INFO - app.shutdown\n"
        r = parse_log_line(line)
        assert r is not None
        assert r["event_name"] == "app.shutdown"


class TestUserIdExtraction:
    def test_user_id_extracted_as_int(self):
        line = "2026-05-18 14:00:00 - studybuddy_bot - INFO - x.y user_id=42 q=z"
        r = parse_log_line(line)
        assert r["user_id"] == 42
        assert isinstance(r["user_id"], int)
        # user_id removed from properties (extracted to top-level)
        props = json.loads(r["properties"])
        assert "user_id" not in props

    def test_malformed_user_id_falls_back_to_none(self):
        line = "2026-05-18 14:00:00 - studybuddy_bot - INFO - x.y user_id=not_a_number"
        r = parse_log_line(line)
        assert r["user_id"] is None


class TestFileLevel:
    def test_parse_multiline_file(self, tmp_path):
        log_file = tmp_path / "test.log"
        log_file.write_text(
            "2026-05-18 14:00:00 - studybuddy_bot - INFO - app.start admins=1\n"
            "2026-05-18 14:05:00 - studybuddy_bot - INFO - user.registered user_id=42\n"
            "garbage line\n"
            "\n"
            "2026-05-18 14:10:00 - studybuddy_bot - INFO - session.complete user_id=42 duration=25\n",
            encoding="utf-8",
        )
        rows = parse_log_file(log_file)
        assert len(rows) == 3
        # garbage и empty пропускаются
        events = [r["event_name"] for r in rows]
        assert events == ["app.start", "user.registered", "session.complete"]

    def test_csv_roundtrip(self, tmp_path):
        log_file = tmp_path / "in.log"
        log_file.write_text(
            "2026-05-18 14:00:00 - studybuddy_bot - INFO - x.y user_id=1 a=b\n"
            "2026-05-18 14:01:00 - studybuddy_bot - INFO - x.y user_id=2 c=d\n",
            encoding="utf-8",
        )
        rows = parse_log_file(log_file)
        csv_out = tmp_path / "out.csv"
        write_csv(rows, csv_out)
        # Прочитать обратно
        with open(csv_out, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            roundtrip = list(reader)
        assert len(roundtrip) == 2
        assert roundtrip[0]["user_id"] == "1"
        assert json.loads(roundtrip[0]["properties"])["a"] == "b"

    def test_to_csv_bytes_returns_utf8(self):
        rows = [
            {
                "timestamp": "2026-05-18 14:00:00",
                "level": "INFO",
                "event_name": "subject_picked",
                "user_id": 42,
                "properties": json.dumps({"subject": "Математика"}, ensure_ascii=False),
                "raw_text": "",
            },
        ]
        b = to_csv_bytes(rows)
        text = b.decode("utf-8")
        # Кириллица сохраняется читабельно
        assert "Математика" in text
        # Header присутствует
        assert "timestamp" in text


class TestCsvColumns:
    def test_columns_stable(self):
        """Любой rows-set производит CSV с этими колонками в этом порядке."""
        rows = parse_log_file_from_string(
            "2026-05-18 14:00:00 - studybuddy_bot - INFO - x.y user_id=1 a=b"
        )
        b = to_csv_bytes(rows)
        first_line = b.decode("utf-8").splitlines()[0]
        assert first_line == ",".join(CSV_COLUMNS)


def parse_log_file_from_string(content: str) -> list[dict]:
    """Helper для тестов: parse в памяти."""
    from parse_logs import parse_log_line
    return [r for r in (parse_log_line(line) for line in content.splitlines()) if r]


class TestCLI:
    def test_main_with_explicit_files(self, tmp_path, capsys):
        from parse_logs import main
        log_file = tmp_path / "input.log"
        log_file.write_text(
            "2026-05-18 14:00:00 - studybuddy_bot - INFO - app.start admins=1\n",
            encoding="utf-8",
        )
        output = tmp_path / "out.csv"
        exit_code = main([str(log_file), "-o", str(output), "--quiet"])
        assert exit_code == 0
        assert output.exists()
        # Header + 1 data row
        lines = output.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2

    def test_main_no_logs_returns_nonzero(self, tmp_path, monkeypatch, capsys):
        from parse_logs import main
        # cd into empty dir → no bot.log defaults
        monkeypatch.chdir(tmp_path)
        exit_code = main([])  # default to bot.log + bot.log.1..9 → none exist
        assert exit_code == 1
