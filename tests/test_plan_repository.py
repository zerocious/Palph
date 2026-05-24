"""Tests for PlanRepository."""
from __future__ import annotations

import pytest
import pytest_asyncio

from repository import PlanRepository, UserRepository
from plan_service import generate_sprint_plan, build_content_catalog, ProgressSnapshot


@pytest_asyncio.fixture
async def plan_repo(db):
    return PlanRepository(db)


@pytest_asyncio.fixture
async def uid(user_repo: UserRepository):
    u = 9001
    await user_repo.create_user(u)
    return u


@pytest.mark.asyncio
async def test_save_and_load_plan(plan_repo, uid):
    plan = {"version": 1, "days": [{"day": 1, "items": []}]}
    await plan_repo.save_plan(uid, "industrial-management", plan, 60, logical_day=1)
    row = await plan_repo.get_active_plan(uid, "industrial-management")
    assert row is not None
    assert row["day_minutes"] == 60
    assert row["logical_day"] == 1
    assert row["plan_json"]["version"] == 1


@pytest.mark.asyncio
async def test_skill_map_upsert(plan_repo, uid):
    await plan_repo.upsert_skill(uid, "subj", "general", 0)
    await plan_repo.upsert_skill(uid, "subj", "section-i", 1)
    skills = await plan_repo.get_skill_map(uid, "subj")
    assert skills["general"] == 0
    assert skills["section-i"] == 1


@pytest.mark.asyncio
async def test_plan_meta(plan_repo, uid):
    await plan_repo.upsert_meta(uid, "subj", first_prompt_shown=1)
    meta = await plan_repo.get_meta(uid, "subj")
    assert meta["first_prompt_shown"] == 1
    assert meta["diagnostic_done"] == 0


@pytest.mark.asyncio
async def test_update_plan_json(plan_repo, uid):
    plan = generate_sprint_plan(
        build_content_catalog("industrial-management"),
        {},
        ProgressSnapshot(),
        60,
        "industrial-management",
    )
    await plan_repo.save_plan(uid, "industrial-management", plan, 60)
    row = await plan_repo.get_active_plan(uid, "industrial-management")
    updated = row["plan_json"]
    assert updated["days"][0]["items"]
    updated["days"][0]["items"][0]["status"] = "done"
    await plan_repo.update_plan_json(uid, "industrial-management", updated)
    row2 = await plan_repo.get_active_plan(uid, "industrial-management")
    assert row2["plan_json"]["days"][0]["items"][0]["status"] == "done"
