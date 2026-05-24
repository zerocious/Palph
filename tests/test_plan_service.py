"""Unit tests for sprint plan generator (no DB)."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from plan_service import (
    CatalogItem,
    ProgressSnapshot,
    build_content_catalog,
    catalog_has_minimum,
    compute_skill_from_diagnostic,
    generate_sprint_plan,
    get_today_items,
    get_today_pool,
    is_today_complete,
    items_per_day,
    mark_item_done,
    plan_progress_summary,
    SPRINT_DAYS,
    count_today_pending,
    topic_order,
)


@pytest.fixture
def sample_catalog():
    return [
        CatalogItem("flashcards", "aaa11111", "section-i", label="Term A"),
        CatalogItem("flashcards", "bbb22222", "section-i", label="Term B"),
        CatalogItem("mcq", "ccc33333", "general", label="Q1"),
        CatalogItem("mcq", "ddd44444", "general", label="Q2"),
        CatalogItem("tasks", "task-01", "general", label="Task 1"),
        CatalogItem("situational", "eee55555", "section-i", section="section-i", label="Sit 1"),
        CatalogItem("situational", "fff66666", "section-i", section="section-i", label="Sit 2"),
        CatalogItem("flashcards", "ggg77777", "general", label="Term C"),
        CatalogItem("mcq", "hhh88888", "general", label="Q3"),
        CatalogItem("mcq", "iii99999", "general", label="Q4"),
        CatalogItem("flashcards", "jjj00000", "general", label="Term D"),
        CatalogItem("tasks", "task-02", "general", label="Task 2"),
    ]


@pytest.fixture
def empty_progress():
    return ProgressSnapshot()


def test_items_per_day_buckets():
    assert items_per_day(60) == 8
    assert items_per_day(120) == 16
    assert items_per_day(180) == 24
    assert items_per_day(240) == 32
    assert items_per_day(999) == 8


def test_generate_sprint_plan_structure(sample_catalog, empty_progress):
    plan = generate_sprint_plan(
        sample_catalog, {}, empty_progress, 60, subject_id="test",
    )
    assert plan["version"] == 1
    assert len(plan["days"]) == SPRINT_DAYS
    for day_block in plan["days"]:
        assert "day" in day_block
        assert "items" in day_block
        assert len(day_block["items"]) <= items_per_day(60)
        for item in day_block["items"]:
            assert item["status"] == "pending"
            assert item["mode"] in ("flashcards", "mcq", "tasks", "situational")
            assert "ref" in item
            assert "topic" in item


def test_generate_respects_daily_count(sample_catalog, empty_progress):
    plan = generate_sprint_plan(sample_catalog, {}, empty_progress, 120)
    for day_block in plan["days"]:
        assert len(day_block["items"]) <= items_per_day(120)


def test_generate_empty_catalog():
    plan = generate_sprint_plan([], {}, ProgressSnapshot(), 60)
    assert plan["days"] == []


def test_weak_topics_prioritized(sample_catalog, empty_progress):
    skill_map = {"section-i": 0, "general": 1}
    plan = generate_sprint_plan(
        sample_catalog, skill_map, empty_progress, 60, subject_id="test",
    )
    day1 = plan["days"][0]["items"]
    weak_count = sum(1 for i in day1 if i["topic"] == "section-i")
    assert weak_count >= len(day1) // 2


def test_mark_item_done(sample_catalog, empty_progress):
    plan = generate_sprint_plan(sample_catalog, {}, empty_progress, 60)
    updated = mark_item_done(plan, 1, 0)
    assert updated["days"][0]["items"][0]["status"] == "done"
    assert plan["days"][0]["items"][0]["status"] == "pending"


def test_get_today_items_catch_up(sample_catalog, empty_progress):
    plan = generate_sprint_plan(sample_catalog, {}, empty_progress, 60)
    today = get_today_items(plan, logical_day=3, day_minutes=60)
    assert len(today) <= items_per_day(60)
    days_seen = {d for d, _, _ in today}
    assert all(d <= 3 for d in days_seen)


def test_get_today_pool_includes_done(sample_catalog, empty_progress):
    plan = generate_sprint_plan(sample_catalog, {}, empty_progress, 60)
    plan = mark_item_done(plan, 1, 0)
    pool = get_today_pool(plan, logical_day=1, day_minutes=60)
    statuses = [item["status"] for _, _, item in pool]
    assert "done" in statuses


def test_count_today_pending(sample_catalog, empty_progress):
    plan = generate_sprint_plan(sample_catalog, {}, empty_progress, 60)
    done, total = count_today_pending(plan, 1, 60)
    assert done == 0
    assert total == min(items_per_day(60), sum(
        len(d["items"]) for d in plan["days"][:1]
    ))


def test_is_today_complete(sample_catalog, empty_progress):
    plan = generate_sprint_plan(sample_catalog, {}, empty_progress, 60)
    assert not is_today_complete(plan, 1, 60)
    day1 = next(d for d in plan["days"] if d["day"] == 1)
    for idx in range(len(day1["items"])):
        plan = mark_item_done(plan, 1, idx)
    assert is_today_complete(plan, 1, 60)


def test_is_today_complete_requires_full_window(sample_catalog, empty_progress):
    plan = generate_sprint_plan(sample_catalog, {}, empty_progress, 60)
    plan = mark_item_done(plan, 1, 0)
    assert not is_today_complete(plan, 3, 60)


def test_plan_progress_summary(sample_catalog, empty_progress):
    plan = generate_sprint_plan(sample_catalog, {}, empty_progress, 60)
    done, total = plan_progress_summary(plan)
    assert done == 0
    assert total > 0
    plan = mark_item_done(plan, 1, 0)
    done2, _ = plan_progress_summary(plan)
    assert done2 == 1


def test_compute_skill_from_diagnostic():
    questions = [
        {"topic": "section-i"},
        {"topic": "section-i"},
        {"topic": "general"},
        {"topic": "general"},
    ]
    answers = [True, False, True, True]
    skills = compute_skill_from_diagnostic(questions, answers)
    assert skills["section-i"] == 1  # 50% correct → skill=1 per spec
    assert skills["general"] == 1


def test_compute_skill_all_correct():
    questions = [{"topic": "a"}, {"topic": "a"}]
    skills = compute_skill_from_diagnostic(questions, [True, True])
    assert skills["a"] == 1


def test_compute_skill_empty():
    assert compute_skill_from_diagnostic([], []) == {}


def test_topic_order_sections_first(sample_catalog):
    order = topic_order(sample_catalog, "test")
    assert order[0] == "section-i"
    assert "general" in order


def test_build_content_catalog_real_materials():
    catalog = build_content_catalog("industrial-management")
    assert catalog_has_minimum(catalog, 10)
    modes = {c.mode for c in catalog}
    assert "flashcards" in modes
    assert "mcq" in modes


def test_build_content_catalog_with_topics_tmp():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "test-subject"
        base.mkdir()
        (base / "flashcards.txt").write_text(
            "Term || Def || section-i\nOther || Def2\n",
            encoding="utf-8",
        )
        (base / "mcq.txt").write_text(
            "Q? || A || W1 || W2 || W3 || general\n",
            encoding="utf-8",
        )
        catalog = build_content_catalog("test-subject", materials_path=Path(tmp))
        topics = {c.topic for c in catalog}
        assert "section-i" in topics
        assert "general" in topics


def test_build_content_catalog_tasks_json_topics():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "subj"
        tasks = base / "tasks"
        tasks.mkdir(parents=True)
        base.mkdir(exist_ok=True)
        (tasks / "task-01.json").write_text(
            json.dumps({
                "problem": "P",
                "accepted": ["x"],
                "topics": ["section-ii"],
            }),
            encoding="utf-8",
        )
        (tasks / "task-01.png").write_bytes(b"fake")
        catalog = build_content_catalog("subj", materials_path=Path(tmp))
        task_items = [c for c in catalog if c.mode == "tasks"]
        assert len(task_items) == 1
        assert task_items[0].topic == "section-ii"


def test_situational_section_topic():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "subj"
        sit = base / "situational"
        sit.mkdir(parents=True)
        (sit / "section-i.txt").write_text(
            "Term || Def || kw || Sit\n",
            encoding="utf-8",
        )
        catalog = build_content_catalog("subj", materials_path=Path(tmp))
        sit_items = [c for c in catalog if c.mode == "situational"]
        assert len(sit_items) == 1
        assert sit_items[0].section == "section-i"
        assert sit_items[0].topic == "section-i"


def test_overdue_items_get_review_badge(sample_catalog):
    progress = ProgressSnapshot(flashcard_due={"aaa11111"})
    plan = generate_sprint_plan(sample_catalog, {}, progress, 60)
    day1 = plan["days"][0]["items"]
    review_items = [i for i in day1 if i.get("badge") == "review"]
    assert any(i["ref"] == "aaa11111" for i in review_items)


def test_logical_day_expands_pool(sample_catalog, empty_progress):
    plan = generate_sprint_plan(sample_catalog, {}, empty_progress, 60)
    d1 = len(get_today_pool(plan, 1, 60))
    d5 = len(get_today_pool(plan, 5, 60))
    assert d5 >= d1


def test_mark_multiple_days(sample_catalog, empty_progress):
    plan = generate_sprint_plan(sample_catalog, {}, empty_progress, 60)
    for n in range(1, 4):
        plan = mark_item_done(plan, 1, n - 1) if n <= len(plan["days"][0]["items"]) else plan
    done, total = plan_progress_summary(plan)
    assert done >= 1


def test_dedupe_catalog():
    dup = [
        CatalogItem("mcq", "x", "general"),
        CatalogItem("mcq", "x", "general"),
    ]
    plan = generate_sprint_plan(dup, {}, ProgressSnapshot(), 60)
    refs_day1 = [i["ref"] for i in plan["days"][0]["items"]]
    assert refs_day1.count("x") <= 1


def test_mode_mix_not_all_same(sample_catalog, empty_progress):
    plan = generate_sprint_plan(sample_catalog, {}, empty_progress, 120)
    for day_block in plan["days"]:
        items = day_block["items"]
        if len(items) >= 6:
            modes = [i["mode"] for i in items[:6]]
            assert len(set(modes)) > 1


def test_items_per_day_scales_daily_slots(sample_catalog, empty_progress):
    plan60 = generate_sprint_plan(sample_catalog, {}, empty_progress, 60)
    plan240 = generate_sprint_plan(sample_catalog, {}, empty_progress, 240)
    assert items_per_day(240) > items_per_day(60)
    assert len(plan240["days"][0]["items"]) >= len(plan60["days"][0]["items"])


def test_get_today_items_excludes_done(sample_catalog, empty_progress):
    plan = generate_sprint_plan(sample_catalog, {}, empty_progress, 60)
    plan = mark_item_done(plan, 1, 0)
    pending = get_today_items(plan, 1, 60)
    assert all(item["status"] != "done" for _, _, item in pending)


def test_catalog_has_minimum():
    assert catalog_has_minimum([CatalogItem("mcq", "a", "t")] * 10)
    assert not catalog_has_minimum([CatalogItem("mcq", "a", "t")] * 5)


def test_load_topics_order(tmp_path):
    subj = tmp_path / "subj"
    subj.mkdir()
    (subj / "topics.json").write_text(
        json.dumps({"order": ["b", "a"]}), encoding="utf-8",
    )
    catalog = [
        CatalogItem("mcq", "1", "a"),
        CatalogItem("mcq", "2", "b"),
        CatalogItem("mcq", "3", "c"),
    ]
    order = topic_order(catalog, "subj", materials_path=tmp_path)
    assert order.index("b") < order.index("a")
    assert "c" in order


def test_build_content_catalog_text_only_task(tmp_path):
    subj = tmp_path / "math"
    tasks_dir = subj / "tasks"
    tasks_dir.mkdir(parents=True)
    (tasks_dir / "task-01.json").write_text(
        json.dumps(
            {
                "text_only": True,
                "accepted": ["1/2"],
                "topics": ["exam-task-1"],
                "problem": "Half?",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    catalog = build_content_catalog("math", materials_path=tmp_path)
    assert len(catalog) == 1
    assert catalog[0].mode == "tasks"
    assert catalog[0].ref == "task-01"
    assert catalog[0].topic == "exam-task-1"
