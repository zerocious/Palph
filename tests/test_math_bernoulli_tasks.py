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
    assert tasks[0]["hint"] == ""


def test_load_tasks_includes_hint_field(math_materials, monkeypatch):
    monkeypatch.setattr("bot.STUDY_MATERIALS_PATH", math_materials)
    tasks_dir = math_materials / "math" / "tasks"
    with open(tasks_dir / "task-01.json", encoding="utf-8") as f:
        data = json.load(f)
    data["hint"] = "Think about n and p."
    with open(tasks_dir / "task-01.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tasks = load_tasks("math")
    assert tasks[0]["hint"] == "Think about n and p."


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
    all_tasks = load_tasks("math")
    assert len(all_tasks) == 71
    assert all(t.get("text_only") for t in all_tasks)
    assert all(t.get("accepted") for t in all_tasks)
    assert all(t.get("solution_text") for t in all_tasks)
    assert all("hint" in t for t in all_tasks)
    groups = load_task_groups("math")
    assert len(groups) == 6
    assert "exam-task-2" in groups
    bernoulli = load_tasks("math", group_id="exam-task-1")
    assert len(bernoulli) == 41
    catalog = build_content_catalog("math")
    assert catalog_has_minimum(catalog, 10)
    diag = load_diagnostic("math")
    assert len(diag) >= 5


ETALON_HINT_TASK_IDS = {
    *(f"task-{n:02d}" for n in range(42, 73)),
    "task-16",
    "task-17",
    "task-18",
    "task-20",
    "task-25",
}


def test_etalon_tasks_have_hints():
    root = Path(__file__).resolve().parent.parent / "study_materials" / "math" / "tasks"
    if not root.is_dir():
        pytest.skip("math materials not generated")
    missing = []
    for task_id in sorted(ETALON_HINT_TASK_IDS):
        path = root / f"{task_id}.json"
        if not path.is_file():
            missing.append(f"{task_id} (file missing)")
            continue
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        hint = (data.get("hint") or "").strip()
        if not hint:
            missing.append(task_id)
        else:
            assert "$" not in hint
            assert "\\dfrac" not in hint
    assert not missing, f"etalon tasks without hints: {missing}"


def test_apply_math_task_hints_script():
    from scripts.apply_math_task_hints import apply_hints, latex_to_plain

    assert "√" in latex_to_plain(r"$\sqrt{D(X)/n}$")
    assert "0,2" in latex_to_plain(r"$p{=}0{,}2$")
    md = Path(__file__).resolve().parent.parent / "study_materials" / "math" / "source" / "top3_tasks_etalon_1.md"
    if not md.is_file():
        pytest.skip("top3_tasks_etalon_1.md not present")
    apply_hints(md, dry_run=True)
