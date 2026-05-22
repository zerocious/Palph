"""Тесты пользовательских задач: парсер .txt и репозиторий."""
import pytest
import pytest_asyncio

from repository import TaskProgressRepository, UserTaskRepository
from user_task_txt import (
    USER_TASK_REFORMAT_PROMPT,
    USER_TASK_TXT_INSTRUCTION,
    parse_user_tasks_txt,
)


class TestUserTaskInstruction:
    def test_instruction_contains_reformat_prompt(self):
        assert "ВСТАВЬ_СЮДА_СВОЙ_ТЕКСТ" in USER_TASK_REFORMAT_PROMPT
        assert "вопрос || ответ" in USER_TASK_REFORMAT_PROMPT
        assert "<pre>" in USER_TASK_TXT_INSTRUCTION
        assert "ВСТАВЬ_СЮДА_СВОЙ_ТЕКСТ" in USER_TASK_TXT_INSTRUCTION


class TestParseUserTasksTxt:
    def test_single_task(self):
        tasks, errors = parse_user_tasks_txt(
            "2+2? || 4 | четыре\n"
        )
        assert not errors
        assert len(tasks) == 1
        assert tasks[0]["problem"] == "2+2?"
        assert tasks[0]["accepted"] == ["4", "четыре"]

    def test_comment_and_empty_lines(self):
        raw = """
# комментарий

Вопрос? || ответ
"""
        tasks, errors = parse_user_tasks_txt(raw)
        assert not errors
        assert len(tasks) == 1

    def test_hint_after_answers(self):
        tasks, errors = parse_user_tasks_txt(
            "Планеты? || 8 ## В Солнечной системе 8 планет"
        )
        assert not errors
        assert tasks[0]["hint"] == "В Солнечной системе 8 планет"

    def test_missing_separator_error(self):
        tasks, errors = parse_user_tasks_txt("только вопрос без ответа")
        assert not tasks
        assert len(errors) == 1


@pytest_asyncio.fixture
async def ut_repo(db):
    return UserTaskRepository(db)


@pytest_asyncio.fixture
async def task_repo(db):
    return TaskProgressRepository(db)


class TestUserTaskRepository:
    async def test_bulk_create_and_list(self, ut_repo, created_user):
        tasks = [
            {"problem": "Q1?", "accepted": ["a"], "hint": ""},
            {"problem": "Q2?", "accepted": ["b", "c"], "hint": "hint"},
        ]
        added, err = await ut_repo.bulk_create(created_user, "math", tasks)
        assert err is None
        assert added == 2
        listed = await ut_repo.list_by_subject(created_user, "math")
        assert len(listed) == 2
        assert listed[0]["kind"] == "user"
        assert listed[0]["id"].startswith("t")

    async def test_delete_cleans_progress(self, ut_repo, created_user, task_repo):
        added, _ = await ut_repo.bulk_create(
            created_user,
            "math",
            [{"problem": "X?", "accepted": ["x"], "hint": ""}],
        )
        listed = await ut_repo.list_by_subject(created_user, "math")
        db_id = int(listed[0]["id"][1:], 16)
        await task_repo.record_attempt(
            created_user, listed[0]["id"], attempts_used=1, succeeded=True
        )
        assert await ut_repo.delete(created_user, db_id) is True
        assert await ut_repo.count_by_subject(created_user, "math") == 0

    async def test_limit_per_subject(self, ut_repo, created_user):
        batch = [
            {"problem": f"Q{i}?", "accepted": ["a"], "hint": ""}
            for i in range(UserTaskRepository.MAX_PER_SUBJECT)
        ]
        await ut_repo.bulk_create(created_user, "eng", batch)
        added, err = await ut_repo.bulk_create(
            created_user,
            "eng",
            [{"problem": "extra?", "accepted": ["x"], "hint": ""}],
        )
        assert added == 0
        assert err == "limit_exceeded"
