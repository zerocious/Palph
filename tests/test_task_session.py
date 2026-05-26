"""Regression tests for task loading, grouping, and session helpers."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot import (
    _file_based_mode_ids,
    _official_task_has_solution,
    _show_task_group_picker,
    load_tasks,
    start_task_session,
)


def test_official_task_has_solution_text_only(tmp_path, monkeypatch):
    subject = tmp_path / "math"
    tasks_dir = subject / "tasks"
    tasks_dir.mkdir(parents=True)
    monkeypatch.setattr("bot.STUDY_MATERIALS_PATH", tmp_path)

    task = {
        "id": "task-01",
        "solution_text": "Because.",
        "solution_filename": "task-01-solution.png",
    }
    assert _official_task_has_solution("math", task) is True


def test_official_task_has_solution_png_only(tmp_path, monkeypatch):
    subject = tmp_path / "math"
    tasks_dir = subject / "tasks"
    tasks_dir.mkdir(parents=True)
    (tasks_dir / "task-01-solution.png").write_bytes(b"png")
    monkeypatch.setattr("bot.STUDY_MATERIALS_PATH", tmp_path)

    task = {
        "id": "task-01",
        "solution_text": "",
        "solution_filename": "task-01-solution.png",
    }
    assert _official_task_has_solution("math", task) is True


def test_official_task_has_solution_none(tmp_path, monkeypatch):
    subject = tmp_path / "math"
    tasks_dir = subject / "tasks"
    tasks_dir.mkdir(parents=True)
    monkeypatch.setattr("bot.STUDY_MATERIALS_PATH", tmp_path)

    task = {"id": "task-01", "solution_text": "", "solution_filename": "task-01-solution.png"}
    assert _official_task_has_solution("math", task) is False


def test_official_task_solution_path_blocks_traversal(tmp_path, monkeypatch):
    subject = tmp_path / "math"
    tasks_dir = subject / "tasks"
    tasks_dir.mkdir(parents=True)
    monkeypatch.setattr("bot.STUDY_MATERIALS_PATH", tmp_path)

    from bot import _official_task_solution_path

    task = {"id": "task-01", "solution_filename": "../../../outside.png"}
    path = _official_task_solution_path("math", task)
    assert path is not None
    assert path.resolve().parent == tasks_dir.resolve()
    assert path.name == "outside.png"


def test_file_based_mode_ids_skips_unloadable_tasks(tmp_path, monkeypatch):
    subject = tmp_path / "math"
    tasks_dir = subject / "tasks"
    tasks_dir.mkdir(parents=True)
    (tasks_dir / "task-01.json").write_text(
        json.dumps({"accepted": []}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr("bot.STUDY_MATERIALS_PATH", tmp_path)
    assert "tasks" not in _file_based_mode_ids("math")


def test_file_based_mode_ids_includes_loadable_tasks(tmp_path, monkeypatch):
    subject = tmp_path / "math"
    tasks_dir = subject / "tasks"
    tasks_dir.mkdir(parents=True)
    (tasks_dir / "task-01.json").write_text(
        json.dumps({"text_only": True, "accepted": ["1"]}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr("bot.STUDY_MATERIALS_PATH", tmp_path)
    assert "tasks" in _file_based_mode_ids("math")


@pytest.mark.asyncio
async def test_empty_group_picker_falls_back_to_session(tmp_path, monkeypatch):
    subject = tmp_path / "math"
    tasks_dir = subject / "tasks"
    tasks_dir.mkdir(parents=True)
    (subject / "groups.json").write_text(
        json.dumps({"empty-group": {"title": "Empty"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (tasks_dir / "task-01.json").write_text(
        json.dumps(
            {
                "text_only": True,
                "group": "other-group",
                "accepted": ["1"],
                "problem": "Q?",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("bot.STUDY_MATERIALS_PATH", tmp_path)

    message = MagicMock()
    message.from_user.id = 42
    message.chat.id = 42
    message.answer = AsyncMock()

    state = AsyncMock()
    state.set_state = AsyncMock()
    state.update_data = AsyncMock()

    with patch("bot.start_task_session", new=AsyncMock()) as start_mock:
        await _show_task_group_picker(
            message,
            state,
            "math",
            "Math",
            {"empty-group": {"title": "Empty"}},
            "ru",
        )
        start_mock.assert_awaited_once()
        message.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_task_session_merges_user_tasks(db, created_user, monkeypatch):
    from repository import UserTaskRepository

    subject = Path(__file__).resolve().parent.parent / "study_materials" / "math"
    if not (subject / "tasks").exists():
        pytest.skip("math materials not present")

    user_id = created_user
    monkeypatch.setattr("bot.user_task_repo", UserTaskRepository(db))
    stats_repo = AsyncMock()
    stats_repo.bump_visit = AsyncMock()
    monkeypatch.setattr("bot.subject_stats_repo", stats_repo)

    ut_repo = UserTaskRepository(db)
    await ut_repo.bulk_create(
        user_id,
        "math",
        [{"problem": "User Q?", "accepted": ["42"], "hint": ""}],
    )

    message = MagicMock()
    message.from_user.id = user_id
    message.chat.id = user_id
    message.answer = AsyncMock()

    state = AsyncMock()
    state.set_state = AsyncMock()
    state.update_data = AsyncMock()

    with patch("bot._send_next_task", new=AsyncMock()) as send_mock, patch(
        "bot.loc", new=AsyncMock(return_value="ru")
    ):
        await start_task_session(message, state, "math", "Math")
        send_mock.assert_awaited_once()
        call_kwargs = state.update_data.await_args.kwargs
        tasks = call_kwargs["task_questions"]
        kinds = {t["kind"] for t in tasks}
        assert "user" in kinds
        assert "official" in kinds
