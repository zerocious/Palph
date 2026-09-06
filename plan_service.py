"""
Sprint exam plan (v0.9): content catalog, diagnostic scoring, plan generation.

Pure functions here are unit-tested without DB. File I/O uses STUDY_MATERIALS_PATH.
"""
from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

SPRINT_DAYS = 14
DEFAULT_TOPIC = "general"
MIN_PLAN_ITEMS = 10
MINUTES_TO_DAILY_ITEMS = {60: 8, 120: 16, 180: 24, 240: 32}
ITEM_MINUTES = {
    "flashcards": 2,
    "mcq": 1,
    "tasks": 5,
    "situational": 2,
}
MODE_ROTATION = ("flashcards", "mcq", "situational", "tasks", "mcq", "flashcards")
BADGE_KEYS = ("review", "weak", "new", "progress")

STUDY_MATERIALS_PATH = Path(__file__).parent / "study_materials"


def _flashcard_hash(term: str) -> str:
    return hashlib.md5(term.encode("utf-8")).hexdigest()[:8]


def _mcq_hash(question: str) -> str:
    return hashlib.md5(question.encode("utf-8")).hexdigest()[:8]


def _term_hash(term: str) -> str:
    return hashlib.md5(term.encode("utf-8")).hexdigest()[:8]


def _parse_topics(raw: str | None) -> list[str]:
    if not raw or not raw.strip():
        return [DEFAULT_TOPIC]
    parts = [p.strip() for p in raw.replace("|", ",").split(",") if p.strip()]
    return parts or [DEFAULT_TOPIC]


@dataclass
class CatalogItem:
    mode: str
    ref: str
    topic: str
    section: str | None = None
    label: str = ""

    def to_plan_item(self, badge: str = "new") -> dict[str, Any]:
        item: dict[str, Any] = {
            "mode": self.mode,
            "ref": self.ref,
            "topic": self.topic,
            "status": "pending",
            "badge": badge,
        }
        if self.section:
            item["section"] = self.section
        return item

    def item_key(self) -> tuple:
        if self.section:
            return (self.mode, self.ref, self.section)
        return (self.mode, self.ref)


@dataclass
class ProgressSnapshot:
    flashcard_due: set[str] = field(default_factory=set)
    quiz_due: set[str] = field(default_factory=set)
    mcq_seen: set[str] = field(default_factory=set)
    mcq_mastered: set[str] = field(default_factory=set)
    tasks_done: set[str] = field(default_factory=set)
    flashcard_reviewed: set[str] = field(default_factory=set)
    quiz_reviewed: set[str] = field(default_factory=set)


def load_topics_order(subject_id: str, materials_path: Path | None = None) -> list[str]:
    """Optional topics.json defines topic progression order."""
    base = (materials_path or STUDY_MATERIALS_PATH) / subject_id
    path = base / "topics.json"
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    order = data.get("order") or data.get("topics") or []
    return [str(t) for t in order]


def build_content_catalog(
    subject_id: str,
    materials_path: Path | None = None,
) -> list[CatalogItem]:
    """Scan subject folder; untagged items get topic ``general``."""
    base = (materials_path or STUDY_MATERIALS_PATH) / subject_id
    if not base.is_dir():
        return []

    items: list[CatalogItem] = []

    fc_path = base / "flashcards.txt"
    if fc_path.exists():
        with open(fc_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = [p.strip() for p in line.split("||")]
                if len(parts) >= 2:
                    term, definition = parts[0], parts[1]
                    topics = _parse_topics(parts[2] if len(parts) >= 3 else None)
                    for topic in topics:
                        items.append(CatalogItem(
                            mode="flashcards",
                            ref=_flashcard_hash(term),
                            topic=topic,
                            label=term,
                        ))

    mcq_path = base / "mcq.txt"
    if mcq_path.exists():
        with open(mcq_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = [p.strip() for p in line.split("||")]
                if len(parts) >= 5:
                    question = parts[0]
                    topics = _parse_topics(parts[5] if len(parts) >= 6 else None)
                    for topic in topics:
                        items.append(CatalogItem(
                            mode="mcq",
                            ref=_mcq_hash(question),
                            topic=topic,
                            label=question[:60],
                        ))

    tasks_dir = base / "tasks"
    if tasks_dir.is_dir():
        for json_file in sorted(tasks_dir.glob("task-*.json")):
            task_id = json_file.stem
            png = tasks_dir / f"{task_id}.png"
            with open(json_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            text_only = bool(data.get("text_only"))
            if not text_only and not png.exists():
                continue
            accepted = data.get("accepted") or []
            if not accepted:
                continue
            raw_topics = data.get("topics") or [DEFAULT_TOPIC]
            if isinstance(raw_topics, str):
                raw_topics = _parse_topics(raw_topics)
            problem = (data.get("problem") or task_id)[:60]
            for topic in raw_topics:
                items.append(CatalogItem(
                    mode="tasks",
                    ref=task_id,
                    topic=str(topic),
                    label=problem,
                ))

    situational_dir = base / "situational"
    if situational_dir.is_dir():
        for txt in sorted(situational_dir.glob("section-*.txt")):
            section = txt.stem
            topic = section
            with open(txt, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = [p.strip() for p in line.split("||")]
                    if len(parts) >= 4:
                        term = parts[0]
                        items.append(CatalogItem(
                            mode="situational",
                            ref=_term_hash(term),
                            topic=topic,
                            section=section,
                            label=term,
                        ))

    return items


def catalog_has_minimum(catalog: list[CatalogItem], minimum: int = MIN_PLAN_ITEMS) -> bool:
    return len(catalog) >= minimum


def load_diagnostic(
    subject_id: str,
    test_id: str = "default",
    materials_path: Path | None = None,
) -> list[dict[str, Any]]:
    base = (materials_path or STUDY_MATERIALS_PATH) / subject_id / "diagnostic"
    path = base / f"{test_id}.json"
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return list(data.get("questions") or [])


def compute_skill_from_diagnostic(
    questions: list[dict[str, Any]],
    answers: list[bool],
) -> dict[str, int]:
    """
    Binary skill per topic: skill=1 if ≥50% correct in that topic.
    """
    by_topic: dict[str, list[bool]] = {}
    for q, ok in zip(questions, answers):
        topic = q.get("topic") or DEFAULT_TOPIC
        by_topic.setdefault(topic, []).append(bool(ok))

    skills: dict[str, int] = {}
    for topic, results in by_topic.items():
        if not results:
            skills[topic] = 0
        else:
            skills[topic] = 1 if sum(results) / len(results) >= 0.5 else 0
    return skills


def items_per_day(day_minutes: int) -> int:
    return MINUTES_TO_DAILY_ITEMS.get(day_minutes, MINUTES_TO_DAILY_ITEMS[60])


def topic_order(
    catalog: list[CatalogItem],
    subject_id: str,
    materials_path: Path | None = None,
) -> list[str]:
    configured = load_topics_order(subject_id, materials_path)
    discovered: list[str] = []
    for item in catalog:
        if item.topic not in discovered:
            discovered.append(item.topic)
    if not configured:
        sections = [t for t in discovered if t.startswith("section-")]
        rest = [t for t in discovered if not t.startswith("section-")]
        sections.sort()
        rest.sort(key=lambda x: (x != DEFAULT_TOPIC, x))
        return sections + rest
    ordered = [t for t in configured if t in discovered]
    for t in discovered:
        if t not in ordered:
            ordered.append(t)
    return ordered


def _is_due(ref: str, mode: str, progress: ProgressSnapshot) -> bool:
    if mode == "flashcards":
        return ref in progress.flashcard_due
    if mode == "situational":
        return ref in progress.quiz_due
    return False


def _is_new(item: CatalogItem, progress: ProgressSnapshot) -> bool:
    if item.mode == "flashcards":
        return item.ref not in progress.flashcard_reviewed
    if item.mode == "mcq":
        return item.ref not in progress.mcq_seen
    if item.mode == "tasks":
        return item.ref not in progress.tasks_done
    if item.mode == "situational":
        return item.ref not in progress.quiz_reviewed
    return True


def _score_item(
    item: CatalogItem,
    skill_map: dict[str, int],
    progress: ProgressSnapshot,
    topic_rank: dict[str, int],
) -> tuple[int, int, int, int]:
    """Lower tuple = higher priority: (overdue, weak, new, topic_rank)."""
    overdue = 0 if _is_due(item.ref, item.mode, progress) else 1
    weak = 0 if skill_map.get(item.topic, 0) == 0 else 1
    new = 0 if _is_new(item, progress) else 1
    rank = topic_rank.get(item.topic, 999)
    return (overdue, weak, new, rank)


def _pick_badge(
    item: CatalogItem,
    skill_map: dict[str, int],
    progress: ProgressSnapshot,
) -> str:
    if _is_due(item.ref, item.mode, progress):
        return "review"
    if skill_map.get(item.topic, 0) == 0:
        return "weak"
    if _is_new(item, progress):
        return "new"
    return "progress"


def _dedupe_catalog(catalog: list[CatalogItem]) -> list[CatalogItem]:
    seen: set[tuple] = set()
    out: list[CatalogItem] = []
    for item in catalog:
        key = item.item_key()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def generate_sprint_plan(
    catalog: list[CatalogItem],
    skill_map: dict[str, int],
    progress: ProgressSnapshot,
    day_minutes: int,
    subject_id: str = "",
    materials_path: Path | None = None,
) -> dict[str, Any]:
    """
    Pre-compute a 14-day plan. Each day gets ``items_per_day(day_minutes)`` slots.
    """
    catalog = _dedupe_catalog(catalog)
    if not catalog:
        return {"version": 1, "days": []}

    order = topic_order(catalog, subject_id, materials_path)
    topic_rank = {t: i for i, t in enumerate(order)}
    daily_count = items_per_day(day_minutes)
    total_needed = daily_count * SPRINT_DAYS

    scored = sorted(
        catalog,
        key=lambda c: _score_item(c, skill_map, progress, topic_rank),
    )

    pool: list[CatalogItem] = list(scored)
    if len(pool) < total_needed:
        extra: list[CatalogItem] = []
        idx = 0
        while len(pool) + len(extra) < total_needed:
            extra.append(scored[idx % len(scored)])
            idx += 1
        pool.extend(extra)

    days: list[dict[str, Any]] = []
    pool_idx = 0
    last_modes: list[str] = []

    for day_num in range(1, SPRINT_DAYS + 1):
        day_items: list[dict[str, Any]] = []
        day_keys: set[tuple] = set()
        attempts = 0
        max_attempts = max(len(pool), 1) * 3
        while len(day_items) < daily_count and pool_idx < len(pool) and attempts < max_attempts:
            candidate = pool[pool_idx]
            pool_idx += 1
            attempts += 1
            key = candidate.item_key()
            if key in day_keys:
                continue
            mode = candidate.mode
            if last_modes[-5:].count(mode) >= 5:
                continue
            badge = _pick_badge(candidate, skill_map, progress)
            day_items.append(candidate.to_plan_item(badge))
            day_keys.add(key)
            last_modes.append(mode)
        days.append({"day": day_num, "items": day_items})

    return {"version": 1, "days": days}


def iter_plan_items(plan: dict[str, Any]):
    for day_block in plan.get("days") or []:
        day_num = day_block.get("day", 0)
        for idx, item in enumerate(day_block.get("items") or []):
            yield day_num, idx, item


def get_today_pool(
    plan: dict[str, Any],
    logical_day: int,
    day_minutes: int | None = None,
) -> list[tuple[int, int, dict[str, Any]]]:
    """All items in catch-up window (done + pending), sorted by priority."""
    cap = items_per_day(day_minutes or 60)
    all_in_window: list[tuple[int, int, dict[str, Any], tuple]] = []

    for day_num, idx, item in iter_plan_items(plan):
        if day_num > logical_day:
            break
        badge = item.get("badge", "new")
        badge_rank = BADGE_KEYS.index(badge) if badge in BADGE_KEYS else 99
        done_rank = 1 if item.get("status") == "done" else 0
        all_in_window.append((day_num, idx, item, (done_rank, badge_rank, day_num, idx)))

    all_in_window.sort(key=lambda x: x[3])
    selected = all_in_window[:cap]
    return [(d, i, item) for d, i, item, _ in selected]


def get_today_items(
    plan: dict[str, Any],
    logical_day: int,
    day_minutes: int | None = None,
) -> list[tuple[int, int, dict[str, Any]]]:
    """
    Catch-up pool: pending items from days 1..logical_day, capped by daily budget.
    """
    return [
        (d, i, item)
        for d, i, item in get_today_pool(plan, logical_day, day_minutes)
        if item.get("status") != "done"
    ]


def count_today_pending(
    plan: dict[str, Any],
    logical_day: int,
    day_minutes: int | None = None,
) -> tuple[int, int]:
    """Return (done_in_pool, total_in_pool) for today's catch-up set."""
    pool = get_today_pool(plan, logical_day, day_minutes)
    if not pool:
        return 0, 0
    done = sum(1 for _, _, item in pool if item.get("status") == "done")
    return done, len(pool)


def mark_item_done(plan: dict[str, Any], day: int, idx: int) -> dict[str, Any]:
    updated = deepcopy(plan)
    for day_block in updated.get("days") or []:
        if day_block.get("day") != day:
            continue
        items = day_block.get("items") or []
        if 0 <= idx < len(items):
            items[idx]["status"] = "done"
        break
    return updated


def is_today_complete(
    plan: dict[str, Any],
    logical_day: int,
    day_minutes: int,
) -> bool:
    """True when every pending item in days 1..logical_day is done."""
    has_items = False
    for day_num, _, item in iter_plan_items(plan):
        if day_num > logical_day:
            break
        has_items = True
        if item.get("status") != "done":
            return False
    return has_items


def plan_progress_summary(plan: dict[str, Any]) -> tuple[int, int]:
    total = done = 0
    for _, _, item in iter_plan_items(plan):
        total += 1
        if item.get("status") == "done":
            done += 1
    return done, total


def build_progress_snapshot(
    *,
    flashcard_rows: list[dict] | None = None,
    quiz_rows: list[dict] | None = None,
    mcq_rows: list[dict] | None = None,
    task_rows: list[dict] | None = None,
    today: date | None = None,
) -> ProgressSnapshot:
    today = today or date.today()
    today_str = today.isoformat()
    snap = ProgressSnapshot()

    for row in flashcard_rows or []:
        h = row.get("card_hash") or row.get("hash")
        if not h:
            continue
        snap.flashcard_reviewed.add(h)
        nr = row.get("next_review")
        if nr and str(nr)[:10] <= today_str:
            snap.flashcard_due.add(h)

    for row in quiz_rows or []:
        h = row.get("term_hash") or row.get("hash")
        if not h:
            continue
        snap.quiz_reviewed.add(h)
        nr = row.get("next_review")
        if nr and str(nr)[:10] <= today_str:
            snap.quiz_due.add(h)

    for row in mcq_rows or []:
        h = row.get("question_hash")
        if not h:
            continue
        snap.mcq_seen.add(h)
        if int(row.get("correct_count") or 0) >= 1:
            snap.mcq_mastered.add(h)

    for row in task_rows or []:
        tid = row.get("task_id")
        if tid and int(row.get("succeeded") or 0) == 1:
            snap.tasks_done.add(str(tid))

    return snap


def resolve_diagnostic_question(
    subject_id: str,
    question: dict[str, Any],
    catalog: list[CatalogItem] | None = None,
) -> CatalogItem | None:
    """Match diagnostic question spec to a catalog item."""
    catalog = catalog or build_content_catalog(subject_id)
    mode = question.get("mode")
    ref = question.get("ref")
    section = question.get("section")
    for item in catalog:
        if item.mode != mode:
            continue
        if item.ref != ref:
            continue
        if section and item.section != section:
            continue
        return item
    return None
