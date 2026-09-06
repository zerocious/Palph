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


class TestOversizedNumericInput:
    """
    Ответ парсится регуляркой (-?\d+) — число цифр ничем не ограничено, а
    Telegram пропускает сообщение до 4096 символов. Раньше допуск считался
    через abs(float(a) - float(b)), и примерно с 400 цифр float() падал с
    OverflowError прямо в handle_task_answer. Обработчик исключение не
    ловит, поэтому пользователь не получал на свою попытку вообще ничего.
    """

    @pytest.mark.parametrize("digits", [400, 1000, 4000])
    def test_huge_integer_answer_does_not_raise(self, digits: int) -> None:
        assert task_answer_matches("9" * digits, ["4"]) is False

    def test_huge_denominator_does_not_raise(self) -> None:
        assert task_answer_matches("1/" + "9" * 4000, ["0.5"]) is False

    def test_huge_answer_still_matches_itself(self) -> None:
        """Точная арифметика: одинаковые большие числа обязаны совпасть."""
        huge = "9" * 400
        assert task_answer_matches(huge, [huge]) is True

    def test_large_integers_differing_by_one_are_not_equal(self) -> None:
        """
        На таких числах float терял точность и объявлял их равными.
        Fraction сравнивает точно.
        """
        assert task_answer_matches("10000000000000000001", ["10000000000000000000"]) is False

    def test_tolerance_semantics_preserved(self) -> None:
        """Допуск 0.5e-6 остался прежним — округлённые десятичные засчитываются."""
        assert task_answer_matches("0.3333333", ["1/3"]) is True
        assert task_answer_matches("0.33", ["1/3"]) is False
