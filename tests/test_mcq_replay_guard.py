"""
Защита от повторного ответа в MCQ.

callback_data кнопок содержал только индекс варианта («mcq:2»), без номера
вопроса. Поэтому повторно отправленный ответ был неотличим от свежего:
он засчитывался СЛЕДУЮЩЕМУ вопросу — записывалась попытка, начислялись
монета и очки лидерборда, сессия проматывалась дальше.

Достижимо не только поддельным клиентом: обычный двойной тап успевает
пройти до того, как edit_text уберёт клавиатуру, потому что она снимается
уже ПОСЛЕ начисления.
"""
from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

os.environ.setdefault("BOT_TOKEN", "test-token-for-pytest-imports")

import bot  # noqa: E402


@pytest.fixture
def wiring(monkeypatch):
    """Мокаем всё, что трогает хендлер, и считаем начисления."""
    spy = SimpleNamespace(
        coins=AsyncMock(), quiz_pts=AsyncMock(), reset_series=AsyncMock(),
        attempts=AsyncMock(), events=AsyncMock(), next_question=AsyncMock(),
    )
    # raising=False: репозитории присваиваются глобалам внутри main(),
    # до её вызова атрибутов модуля просто нет.
    monkeypatch.setattr(
        bot, "user_repo", SimpleNamespace(add_coins=spy.coins), raising=False
    )
    monkeypatch.setattr(
        bot, "leaderboard_repo",
        SimpleNamespace(grant_quiz_pts_correct=spy.quiz_pts,
                        reset_quiz_series=spy.reset_series),
        raising=False,
    )
    monkeypatch.setattr(
        bot, "mcq_repo", SimpleNamespace(record_attempt=spy.attempts), raising=False
    )
    monkeypatch.setattr(
        bot, "event_repo", SimpleNamespace(log=spy.events), raising=False
    )
    monkeypatch.setattr(bot, "_send_next_mcq_question", spy.next_question)
    monkeypatch.setattr(bot, "loc", AsyncMock(return_value="ru"))
    monkeypatch.setattr(bot, "PLAN_UI_ENABLED", False)
    monkeypatch.setattr(bot.asyncio, "sleep", AsyncMock())
    return spy


def _state(mcq_index: int, correct_idx: int = 1):
    return SimpleNamespace(
        get_state=AsyncMock(return_value=bot.QuizStates.answering_mcq.state),
        get_data=AsyncMock(return_value={
            "mcq_index": mcq_index,
            "mcq_current_correct_idx": correct_idx,
            "mcq_current_correct_text": "верный",
            "mcq_questions": [{"question": f"q{i}"} for i in range(5)],
            "mcq_correct_count": 0,
            "subject_id": "math",
        }),
        update_data=AsyncMock(),
    )


def _callback(data: str):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=42),
        message=SimpleNamespace(
            text="вопрос", chat=SimpleNamespace(id=42), edit_text=AsyncMock(),
        ),
        answer=AsyncMock(),
    )


class TestReplayGuard:
    async def test_fresh_answer_for_current_question_is_counted(self, wiring):
        cb = _callback("mcq:3:1")          # вопрос 3, вариант 1 — он же верный
        await bot.handle_mcq_callback(cb, _state(mcq_index=3, correct_idx=1))

        wiring.coins.assert_awaited_once()
        wiring.quiz_pts.assert_awaited_once()

    async def test_replayed_answer_for_past_question_is_rejected(self, wiring):
        """Ядро фикса: ответ на вопрос 2, когда сессия уже на вопросе 3."""
        cb = _callback("mcq:2:1")
        await bot.handle_mcq_callback(cb, _state(mcq_index=3, correct_idx=1))

        wiring.coins.assert_not_awaited()
        wiring.quiz_pts.assert_not_awaited()
        wiring.attempts.assert_not_awaited()
        wiring.next_question.assert_not_awaited()

    async def test_replay_does_not_advance_the_session(self, wiring):
        cb = _callback("mcq:0:1")
        st = _state(mcq_index=4, correct_idx=1)
        await bot.handle_mcq_callback(cb, st)

        st.update_data.assert_not_awaited()

    async def test_legacy_two_part_format_is_rejected(self, wiring):
        """Старый формат без номера вопроса — тоже путь для реплея."""
        cb = _callback("mcq:1")
        await bot.handle_mcq_callback(cb, _state(mcq_index=0, correct_idx=1))

        wiring.coins.assert_not_awaited()
        wiring.attempts.assert_not_awaited()

    async def test_wrong_answer_for_current_question_still_processed(self, wiring):
        cb = _callback("mcq:3:0")          # вариант 0, верный — 1
        await bot.handle_mcq_callback(cb, _state(mcq_index=3, correct_idx=1))

        wiring.coins.assert_not_awaited()
        wiring.reset_series.assert_awaited_once()
        wiring.attempts.assert_awaited_once()


class TestCallbackDataCarriesQuestionIndex:
    def test_buttons_include_the_question_number(self):
        """Регрессия: без номера вопроса реплей снова станет неотличим."""
        import ast
        from pathlib import Path

        src = (Path(__file__).resolve().parent.parent / "bot.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next(
            f for f in ast.walk(tree)
            if isinstance(f, ast.AsyncFunctionDef) and f.name == "_send_next_mcq_question"
        )
        payloads = [
            ast.unparse(kw.value)
            for call in ast.walk(fn)
            if isinstance(call, ast.Call)
            for kw in call.keywords
            if kw.arg == "callback_data"
        ]
        assert payloads, "не найден callback_data в _send_next_mcq_question"
        for p in payloads:
            assert p.count("{") >= 2, (
                f"callback_data {p} не несёт номер вопроса — повторный ответ "
                "снова будет засчитан следующему вопросу"
            )
