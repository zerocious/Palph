"""
Тесты для `system.deploy` event (PA-roadmap #6).

Покрываем две вещи:
1. `resolve_deploy_version()` — pure-ish helper, fallback chain
   (env var → git → "unknown"). Subprocess изолируется через monkeypatch.
2. `log_deploy_event()` — пишет одну строку в events с правильным
   event_name, nullable user_id, и схемой properties.
"""
import json
import re

import pytest
import pytest_asyncio

from repository import EventRepository
from services import log_deploy_event, resolve_deploy_version


@pytest_asyncio.fixture
async def event_repo(db):
    return EventRepository(db)


class TestResolveDeployVersion:
    def test_env_var_takes_precedence(self, monkeypatch):
        monkeypatch.setenv("BOT_VERSION", "v1.2.3-rc4")
        assert resolve_deploy_version() == "v1.2.3-rc4"

    def test_env_var_stripped(self, monkeypatch):
        monkeypatch.setenv("BOT_VERSION", "  v1.0  \n")
        assert resolve_deploy_version() == "v1.0"

    def test_empty_env_falls_through_to_git(self, monkeypatch):
        """Пустой BOT_VERSION не должен скрывать git fallback."""
        monkeypatch.setenv("BOT_VERSION", "")
        # Поскольку мы в git-репо, git rev-parse должен вернуть short hash.
        v = resolve_deploy_version()
        # 7+-char hex (короткий sha) ИЛИ 'unknown' (если git недоступен — на CI с urocked sandbox).
        assert v == "unknown" or re.fullmatch(r"[0-9a-f]{7,40}", v)

    def test_git_unavailable_returns_unknown(self, monkeypatch):
        """Симулируем отсутствие git: FileNotFoundError из subprocess.run."""
        monkeypatch.delenv("BOT_VERSION", raising=False)
        import subprocess
        def boom(*a, **kw):
            raise FileNotFoundError("git not installed")
        monkeypatch.setattr(subprocess, "run", boom)
        assert resolve_deploy_version() == "unknown"

    def test_git_returns_nonzero_returns_unknown(self, monkeypatch):
        """Симулируем `git rev-parse` non-zero (не git-repo)."""
        monkeypatch.delenv("BOT_VERSION", raising=False)
        import subprocess
        class FakeResult:
            returncode = 128
            stdout = ""
            stderr = "fatal: not a git repository"
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: FakeResult())
        assert resolve_deploy_version() == "unknown"

    def test_git_timeout_returns_unknown(self, monkeypatch):
        """Симулируем TimeoutExpired."""
        monkeypatch.delenv("BOT_VERSION", raising=False)
        import subprocess
        def slow(*a, **kw):
            raise subprocess.TimeoutExpired(cmd="git", timeout=2.0)
        monkeypatch.setattr(subprocess, "run", slow)
        assert resolve_deploy_version() == "unknown"


class TestLogDeployEvent:
    async def test_inserts_one_row(self, event_repo, db):
        await log_deploy_event(event_repo, version="test-v1")
        async with db.execute(
            "SELECT COUNT(*) FROM events WHERE event_name='system.deploy'"
        ) as c:
            row = await c.fetchone()
        assert row[0] == 1

    async def test_user_id_is_null(self, event_repo, db):
        """system.deploy — system-level event, не привязан к user."""
        await log_deploy_event(event_repo, version="test-v1")
        async with db.execute(
            "SELECT user_id FROM events WHERE event_name='system.deploy'"
        ) as c:
            row = await c.fetchone()
        assert row["user_id"] is None

    async def test_properties_schema(self, event_repo, db):
        """props должны содержать version, started_at_utc, python_version."""
        await log_deploy_event(event_repo, version="abc1234")
        async with db.execute(
            "SELECT properties FROM events WHERE event_name='system.deploy'"
        ) as c:
            row = await c.fetchone()
        props = json.loads(row["properties"])
        assert props["version"] == "abc1234"
        assert "started_at_utc" in props
        # ISO-8601 UTC: '2026-05-21T19:23:45Z'
        assert re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", props["started_at_utc"]
        )
        assert "python_version" in props
        # major.minor.patch, без build-info типа '+'
        assert re.fullmatch(r"\d+\.\d+\.\d+", props["python_version"])

    async def test_returns_props_dict(self, event_repo):
        """Caller использует возвращённые props для логов — проверяем shape."""
        props = await log_deploy_event(event_repo, version="v1")
        assert isinstance(props, dict)
        assert set(props.keys()) == {"version", "started_at_utc", "python_version"}

    async def test_uses_resolve_when_version_omitted(
        self, event_repo, db, monkeypatch
    ):
        """Без явной version → fallback на resolve_deploy_version (env var)."""
        monkeypatch.setenv("BOT_VERSION", "resolved-via-env")
        await log_deploy_event(event_repo)  # NO version arg
        async with db.execute(
            "SELECT properties FROM events WHERE event_name='system.deploy'"
        ) as c:
            row = await c.fetchone()
        assert json.loads(row["properties"])["version"] == "resolved-via-env"

    async def test_multiple_deploys_each_logged(self, event_repo, db):
        """Append-only: каждый старт = новая строка."""
        for i in range(3):
            await log_deploy_event(event_repo, version=f"v{i}")
        async with db.execute(
            "SELECT properties FROM events WHERE event_name='system.deploy' ORDER BY id ASC"
        ) as c:
            rows = await c.fetchall()
        assert len(rows) == 3
        versions = [json.loads(r["properties"])["version"] for r in rows]
        assert versions == ["v0", "v1", "v2"]

    async def test_appears_in_event_timeline_query(self, event_repo, db):
        """
        events_name_time index должен сделать lookup 'system.deploy' over time
        быстрым; smoke-test что SELECT с фильтром по event_name работает.
        """
        await log_deploy_event(event_repo, version="x")
        async with db.execute(
            "SELECT event_name, created_at FROM events "
            "WHERE event_name='system.deploy' "
            "ORDER BY created_at DESC LIMIT 10"
        ) as c:
            rows = await c.fetchall()
        assert len(rows) == 1
        assert rows[0]["event_name"] == "system.deploy"
