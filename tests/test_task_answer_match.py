"""Tests for numeric task answer matching."""
from __future__ import annotations

import pytest

from task_answer_match import task_answer_matches


@pytest.mark.parametrize(
    ("user", "accepted", "expected"),
    [
        ("8/27", ["8/27", "0.2963"], True),
        ("0.2963", ["8/27", "0.2963"], True),
        ("0,2963", ["8/27", "0.2963"], True),
        ("13/16", ["13/16", "0.8125"], True),
        ("0,8125", ["13/16"], True),
        ("247/256", ["247/256", "0.9648"], True),
        ("992/3125", ["992/3125", "0.3174"], True),
        ("1/15625", ["1/15625", "0.000064"], True),
        ("0,000064", ["1/15625"], True),
        ("wrong", ["8/27"], False),
        ("0.5", ["8/27"], False),
    ],
)
def test_task_answer_matches(user: str, accepted: list[str], expected: bool) -> None:
    assert task_answer_matches(user, accepted) is expected


def test_normalized_string_fallback() -> None:
    assert task_answer_matches("  Фотография ", ["фотография"]) is True
