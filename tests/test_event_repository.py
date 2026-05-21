"""
Тесты EventRepository — append-only event log для PA-аналитики.

Ключевые инварианты:
- log() никогда не raises (silent error → log в bot.log)
- properties сериализуется как JSON; None → "{}"
- user_id может быть None (system-level event)
- Каждый log() = одна новая строка в events (никаких UPDATE/DELETE)
"""
import json

import pytest
import pytest_asyncio

from repository import EventRepository


@pytest_asyncio.fixture
async def event_repo(db):
    return EventRepository(db)


class TestBasicInsert:
    async def test_log_inserts_one_row(self, event_repo, created_user, db):
        await event_repo.log(created_user, "user_registered", {"language_code": "ru"})
        async with db.execute("SELECT COUNT(*) FROM events") as c:
            row = await c.fetchone()
        assert row[0] == 1

    async def test_log_stores_event_name(self, event_repo, created_user, db):
        await event_repo.log(created_user, "session_started", {"duration": 25})
        async with db.execute("SELECT event_name FROM events WHERE user_id=?", (created_user,)) as c:
            row = await c.fetchone()
        assert row["event_name"] == "session_started"

    async def test_log_stores_user_id(self, event_repo, created_user, db):
        await event_repo.log(created_user, "x", {})
        async with db.execute("SELECT user_id FROM events") as c:
            row = await c.fetchone()
        assert row["user_id"] == created_user

    async def test_log_writes_created_at(self, event_repo, created_user, db):
        await event_repo.log(created_user, "x", {})
        async with db.execute("SELECT created_at FROM events") as c:
            row = await c.fetchone()
        assert row["created_at"]  # non-empty (DEFAULT (datetime('now')))


class TestPropertiesSerialization:
    async def test_dict_serialized_as_json(self, event_repo, created_user, db):
        props = {"key1": "value1", "n": 42, "flag": True}
        await event_repo.log(created_user, "x", props)
        async with db.execute("SELECT properties FROM events") as c:
            row = await c.fetchone()
        parsed = json.loads(row["properties"])
        assert parsed == props

    async def test_none_properties_become_empty_dict(self, event_repo, created_user, db):
        await event_repo.log(created_user, "x", None)
        async with db.execute("SELECT properties FROM events") as c:
            row = await c.fetchone()
        assert json.loads(row["properties"]) == {}

    async def test_empty_dict_serialized_as_empty(self, event_repo, created_user, db):
        await event_repo.log(created_user, "x", {})
        async with db.execute("SELECT properties FROM events") as c:
            row = await c.fetchone()
        assert row["properties"] == "{}"

    async def test_unicode_preserved(self, event_repo, created_user, db):
        """ensure_ascii=False — кириллица должна сохраняться читабельно."""
        await event_repo.log(created_user, "subject_picked", {"subject_id": "Математика"})
        async with db.execute("SELECT properties FROM events") as c:
            row = await c.fetchone()
        # Должна быть кириллица, а не \uXXXX escape
        assert "Математика" in row["properties"]

    async def test_nested_properties_round_trip(self, event_repo, created_user, db):
        complex = {"a": [1, 2, 3], "b": {"nested": True}, "c": None}
        await event_repo.log(created_user, "x", complex)
        async with db.execute("SELECT properties FROM events") as c:
            row = await c.fetchone()
        assert json.loads(row["properties"]) == complex


class TestNullableUserId:
    async def test_log_with_null_user(self, event_repo, db):
        """System-level event без привязки к user."""
        await event_repo.log(None, "system_startup", {"version": "v0.7"})
        async with db.execute("SELECT user_id, event_name FROM events") as c:
            row = await c.fetchone()
        assert row["user_id"] is None
        assert row["event_name"] == "system_startup"


class TestMultipleEvents:
    async def test_each_log_creates_new_row(self, event_repo, created_user, db):
        """Append-only: каждый log = новая строка, не upsert."""
        for i in range(5):
            await event_repo.log(created_user, "x", {"i": i})
        async with db.execute("SELECT COUNT(*) FROM events") as c:
            row = await c.fetchone()
        assert row[0] == 5

    async def test_events_preserve_order(self, event_repo, created_user, db):
        """ID auto-increment → SELECT по id ASC даёт chronological order."""
        for i in range(3):
            await event_repo.log(created_user, "x", {"i": i})
        async with db.execute("SELECT properties FROM events ORDER BY id ASC") as c:
            rows = await c.fetchall()
        is_list = [json.loads(r["properties"])["i"] for r in rows]
        assert is_list == [0, 1, 2]

    async def test_separate_users_isolated(self, event_repo, user_repo, created_user, db):
        """user_id фильтр работает корректно."""
        other = 12345
        await user_repo.create_user(other)
        await event_repo.log(created_user, "a", {})
        await event_repo.log(other, "b", {})
        await event_repo.log(created_user, "c", {})
        async with db.execute(
            "SELECT event_name FROM events WHERE user_id=? ORDER BY id ASC",
            (created_user,),
        ) as c:
            rows = await c.fetchall()
        assert [r["event_name"] for r in rows] == ["a", "c"]


class TestErrorSwallowing:
    async def test_failure_doesnt_raise(self, event_repo, created_user):
        """
        Аналитика не должна ломать flow бота. Симулируем ошибку через
        не-JSON-serializable объект — log() должен silently swallow.
        """

        class Unserializable:
            pass

        # Не должно raise — пишет в лог и возвращает None
        await event_repo.log(created_user, "x", {"bad": Unserializable()})
        # Бот ещё жив — последующий log работает
        await event_repo.log(created_user, "y", {"ok": True})

    async def test_failure_doesnt_insert(self, event_repo, created_user, db):
        """Если props сериализация падает — INSERT не должен происходить."""

        class Bad:
            pass

        await event_repo.log(created_user, "fail", {"bad": Bad()})
        async with db.execute("SELECT COUNT(*) FROM events WHERE event_name='fail'") as c:
            row = await c.fetchone()
        assert row[0] == 0


class TestIndexedColumns:
    async def test_subject_id_from_properties(self, event_repo, created_user, db):
        await event_repo.log(
            created_user,
            "subject_picked",
            {"subject_id": "math"},
        )
        async with db.execute(
            "SELECT subject_id, mode, tip_id FROM events WHERE user_id=?",
            (created_user,),
        ) as c:
            row = await c.fetchone()
        assert row["subject_id"] == "math"
        assert row["mode"] is None
        assert row["tip_id"] is None

    async def test_explicit_dimensions_override_properties(self, event_repo, created_user, db):
        await event_repo.log(
            created_user,
            "tip_viewed",
            {"tip_id": "from-json", "category": "tm"},
            tip_id="explicit-id",
        )
        async with db.execute(
            "SELECT tip_id FROM events WHERE user_id=?", (created_user,)
        ) as c:
            row = await c.fetchone()
        assert row["tip_id"] == "explicit-id"

    async def test_mode_column(self, event_repo, created_user, db):
        await event_repo.log(
            created_user,
            "mode_picked",
            {"mode": "flashcards", "subject_id": "opm"},
            mode="flashcards",
            subject_id="opm",
        )
        async with db.execute(
            "SELECT subject_id, mode FROM events WHERE user_id=?", (created_user,)
        ) as c:
            row = await c.fetchone()
        assert row["subject_id"] == "opm"
        assert row["mode"] == "flashcards"
