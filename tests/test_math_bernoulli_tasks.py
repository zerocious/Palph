"""Tests for math Bernoulli study materials and text-only task loading."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from bot import load_task_groups, load_tasks
from plan_service import build_content_catalog, catalog_has_minimum, load_diagnostic


@pytest.fixture
def math_materials(tmp_path: Path):
    subject = tmp_path / "math"
    tasks_dir = subject / "tasks"
    diag_dir = subject / "diagnostic"
    tasks_dir.mkdir(parents=True)
    diag_dir.mkdir(parents=True)

    task = {
        "text_only": True,
        "group": "exam-task-1",
        "topics": ["exam-task-1"],
        "problem": "Test problem",
        "accepted": ["1/2", "0.5"],
        "solution_text": "Because.",
        "subtitle": "Пример",
    }
    with open(tasks_dir / "task-01.json", "w", encoding="utf-8") as f:
        json.dump(task, f)

    with open(subject / "groups.json", "w", encoding="utf-8") as f:
        json.dump(
            {"exam-task-1": {"title": "Билет — задача 1", "description": "Bernoulli"}},
            f,
        )

    with open(diag_dir / "default.json", "w", encoding="utf-8") as f:
        json.dump({"questions": [{"mode": "tasks", "ref": "task-01", "topic": "exam-task-1", "prompt": "Q"}]}, f)

    return tmp_path


def test_load_tasks_text_only_without_png(math_materials, monkeypatch):
    monkeypatch.setattr("bot.STUDY_MATERIALS_PATH", math_materials)
    tasks = load_tasks("math")
    assert len(tasks) == 1
    assert tasks[0]["text_only"] is True
    assert tasks[0]["solution_text"] == "Because."


def test_load_tasks_filter_by_group(math_materials, monkeypatch):
    monkeypatch.setattr("bot.STUDY_MATERIALS_PATH", math_materials)
    assert len(load_tasks("math", group_id="exam-task-1")) == 1
    assert len(load_tasks("math", group_id="other")) == 0


def test_load_task_groups(math_materials, monkeypatch):
    monkeypatch.setattr("bot.STUDY_MATERIALS_PATH", math_materials)
    groups = load_task_groups("math")
    assert "exam-task-1" in groups
    assert "Билет" in groups["exam-task-1"]["title"]


def test_build_content_catalog_text_only(math_materials):
    catalog = build_content_catalog("math", materials_path=math_materials)
    assert len(catalog) == 1
    assert catalog[0].mode == "tasks"
    assert catalog[0].ref == "task-01"
    assert catalog[0].topic == "exam-task-1"


def test_task_01_solution_image():
    root = Path(__file__).resolve().parent.parent / "study_materials" / "math"
    if not (root / "tasks" / "task-01.json").exists():
        pytest.skip("math task-01 not present")
    with open(root / "tasks" / "task-01.json", encoding="utf-8") as f:
        data = json.load(f)
    assert data.get("solution_image") == "task-01-solution.png"
    assert (root / "tasks" / "task-01-solution.png").is_file()
    tasks = load_tasks("math", group_id="exam-task-1")
    task_01 = next(t for t in tasks if t["id"] == "task-01")
    assert task_01["solution_filename"] == "task-01-solution.png"


def test_math_official_content_smoke():
    root = Path(__file__).resolve().parent.parent / "study_materials" / "math"
    if not (root / "tasks").exists():
        pytest.skip("math materials not generated")
    tasks = load_tasks("math", group_id="exam-task-1")
    assert len(tasks) == 33
    assert all(t.get("text_only") for t in tasks)
    assert all(t.get("accepted") for t in tasks)
    assert all(t.get("solution_text") for t in tasks)
    catalog = build_content_catalog("math")
    assert catalog_has_minimum(catalog, 10)
    diag = load_diagnostic("math")
    assert len(diag) >= 5
