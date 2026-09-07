"""
Тесты лога свободных обращений в поддержку (messages.log).

Держат две регрессии:

1. Путь. Константа была захардкожена относительной ("messages.log"), а
   рабочая директория контейнера — /app, тогда как persistent volume
   смонтирован в /app/data. Обращения пользователей писались в
   эфемерный слой образа и пропадали при каждой пересборке.

2. Удаление. delete_user_completely чистит только таблицы, а этот файл
   хранит user_id, имя и текст сообщений — то есть переживал бы
   /delete_account (GDPR Art. 17 / 152-ФЗ ст. 14).
"""
import json
import os

import pytest

import bot
import db as db_module
from bot import _append_support_message, purge_support_messages


def _entry(user_id: int, text: str) -> dict:
    return {
        "timestamp": "2026-09-07 12:00:00",
        "user_id": user_id,
        "user_name": f"User {user_id}",
        "message_id": user_id * 10,
        "text": text,
    }


def _read(path) -> list[dict]:
    """Разобранные JSON-строки лога. Битые строки пропускаем: тест на их
    сохранность специально кладёт в файл не-JSON и проверяет его сырым
    чтением."""
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


@pytest.fixture
def messages_file(tmp_path, monkeypatch):
    """Изолированный messages.log на каждый тест."""
    target = tmp_path / "messages.log"
    monkeypatch.setattr(bot, "MESSAGES_FILE", str(target))
    return target


class TestPathResolution:
    def test_container_default_lands_in_data_volume(self):
        """
        В контейнере файл обязан жить в /app/data — том, который
        переживает пересборку. Именно этого не было.
        """
        assert db_module._container_defaults()["MESSAGES_FILE"] == "/app/data/messages.log"

    def test_local_default_unchanged(self):
        """Локальный запуск не должен поменять поведение."""
        assert db_module._LOCAL_DEFAULTS["MESSAGES_FILE"] == "messages.log"

    def test_env_override_wins(self, tmp_path, monkeypatch):
        target = tmp_path / "custom-messages.log"
        monkeypatch.setenv("MESSAGES_FILE", str(target))
        assert db_module.resolve_env_path("MESSAGES_FILE") == str(target)

    def test_ensure_persistent_dirs_creates_parent(self, tmp_path, monkeypatch):
        base = tmp_path / "app" / "data"
        for name, fname in (
            ("DB_PATH", "studybuddy.db"),
            ("LOG_FILE", "bot.log"),
            ("MESSAGES_FILE", "messages.log"),
        ):
            monkeypatch.setenv(name, str(base / fname))
        monkeypatch.setenv("BACKUP_DIR", str(base / "backups"))

        db_module.ensure_persistent_dirs()
        assert base.is_dir()


class TestAppend:
    async def test_appends_one_json_line_per_entry(self, messages_file):
        await _append_support_message(_entry(1, "первое"))
        await _append_support_message(_entry(2, "второе"))

        rows = _read(messages_file)
        assert [r["user_id"] for r in rows] == [1, 2]
        assert [r["text"] for r in rows] == ["первое", "второе"]

    async def test_survives_non_ascii(self, messages_file):
        await _append_support_message(_entry(1, "привет 🐾"))
        assert _read(messages_file)[0]["text"] == "привет 🐾"


class TestPurge:
    async def test_removes_only_target_user(self, messages_file):
        for uid, text in ((1, "a"), (2, "b"), (1, "c"), (3, "d")):
            await _append_support_message(_entry(uid, text))

        removed = await purge_support_messages(1)

        assert removed == 2
        rows = _read(messages_file)
        assert [r["user_id"] for r in rows] == [2, 3]
        assert all(r["user_id"] != 1 for r in rows)

    async def test_no_matching_lines_leaves_file_untouched(self, messages_file):
        await _append_support_message(_entry(2, "b"))
        before = messages_file.read_bytes()

        assert await purge_support_messages(999) == 0
        assert messages_file.read_bytes() == before

    async def test_missing_file_is_not_an_error(self, messages_file):
        assert not messages_file.exists()
        assert await purge_support_messages(1) == 0

    async def test_malformed_lines_are_preserved(self, messages_file):
        """Битую строку не наша задача терять — она может быть чужой записью."""
        await _append_support_message(_entry(1, "a"))
        with open(messages_file, "a", encoding="utf-8") as f:
            f.write("это не json\n")
        await _append_support_message(_entry(2, "b"))

        assert await purge_support_messages(1) == 1

        raw = messages_file.read_text(encoding="utf-8").splitlines()
        assert "это не json" in raw
        assert [r["user_id"] for r in _read(messages_file)] == [2]

    async def test_rewrite_leaves_no_temp_file(self, messages_file):
        for uid in (1, 2):
            await _append_support_message(_entry(uid, "x"))
        await purge_support_messages(1)

        leftovers = [p.name for p in messages_file.parent.iterdir() if p.name.endswith(".tmp")]
        assert leftovers == []

    async def test_purge_then_append_keeps_both(self, messages_file):
        """После перезаписи файл остаётся пригодным для дозаписи."""
        await _append_support_message(_entry(1, "a"))
        await _append_support_message(_entry(2, "b"))
        await purge_support_messages(1)
        await _append_support_message(_entry(3, "c"))

        assert [r["user_id"] for r in _read(messages_file)] == [2, 3]


class TestConcurrency:
    async def test_parallel_appends_lose_nothing(self, messages_file):
        import asyncio

        await asyncio.gather(
            *(_append_support_message(_entry(i, f"msg{i}")) for i in range(25))
        )
        rows = _read(messages_file)
        assert len(rows) == 25
        assert sorted(r["user_id"] for r in rows) == list(range(25))

    async def test_append_during_purge_is_not_lost(self, messages_file):
        """
        Гонка, ради которой стоит лок: purge читает файл целиком и
        подменяет его. Без сериализации параллельная дозапись попала бы
        в уже прочитанную копию и исчезла при подмене.
        """
        import asyncio

        for uid in (1, 1, 2):
            await _append_support_message(_entry(uid, "old"))

        await asyncio.gather(
            purge_support_messages(1),
            _append_support_message(_entry(3, "new")),
        )

        user_ids = sorted(r["user_id"] for r in _read(messages_file))
        assert 3 in user_ids, "дозапись во время purge потерялась"
        assert 1 not in user_ids
        assert user_ids == [2, 3]
