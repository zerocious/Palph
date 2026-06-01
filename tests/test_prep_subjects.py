"""Prep menu subject visibility."""
from __future__ import annotations

import pytest

from bot import PREP_HIDDEN_SUBJECT_IDS, available_subjects, subjects_with_mode


@pytest.mark.asyncio
async def test_industrial_management_hidden_from_prep_menu(monkeypatch):
    async def fake_modes(subject_id: str, user_id=None, locale: str = "ru"):
        return [("mcq", "MCQ")]

    monkeypatch.setattr("bot.available_modes", fake_modes)
    subjects = await available_subjects(42, "ru")
    subject_ids = {sid for sid, _ in subjects}
    assert "industrial-management" not in subject_ids
    assert "math" in subject_ids


def test_industrial_management_hidden_from_mode_subject_picker():
    for mode_id in ("situational", "flashcards", "mcq", "tasks"):
        ids = {sid for sid, _ in subjects_with_mode(mode_id, "ru")}
        assert "industrial-management" not in ids
