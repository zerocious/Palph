"""
Тесты UserFlashcardRepository: CRUD, лимит, hash prefix, unique term.
"""
import sqlite3

import pytest
import pytest_asyncio

from repository import UserFlashcardRepository, UserRepository, FlashcardRepository


@pytest_asyncio.fixture
async def fc_repo(db):
    return UserFlashcardRepository(db)


@pytest_asyncio.fixture
async def flash_repo(db):
    return FlashcardRepository(db)


class TestUserFlashcardRepository:
    async def test_create_and_list(self, fc_repo, created_user):
        card = await fc_repo.create(
            created_user, "math", "Derivative", "Rate of change"
        )
        assert card["id"] > 0
        assert card["term"] == "Derivative"
        assert card["hash"] == fc_repo.card_hash(card["id"])
        assert card["hash"].startswith("u")

        cards = await fc_repo.list_by_subject(created_user, "math")
        assert len(cards) == 1
        assert cards[0]["term"] == "Derivative"

    async def test_hash_format(self, fc_repo, created_user):
        card = await fc_repo.create(created_user, "english", "Hello", "Greeting")
        assert len(card["hash"]) == 8
        assert card["hash"] == f"u{card['id']:07x}"

    async def test_unique_term_per_subject(self, fc_repo, created_user):
        await fc_repo.create(created_user, "math", "Pi", "3.14…")
        with pytest.raises(sqlite3.IntegrityError):
            await fc_repo.create(created_user, "math", "Pi", "Another definition")

    async def test_same_term_different_subjects_allowed(self, fc_repo, created_user):
        await fc_repo.create(created_user, "math", "Set", "Collection")
        card = await fc_repo.create(created_user, "english", "Set", "Group of things")
        assert card["term"] == "Set"

    async def test_delete_cleans_flashcard_progress(
        self, fc_repo, flash_repo, created_user, db
    ):
        card = await fc_repo.create(created_user, "math", "Limit", "Approach value")
        await flash_repo.upsert_progress(
            created_user,
            card["hash"],
            ease_factor=2.5,
            interval_days=1,
            repetitions=1,
            last_review="2026-05-01 10:00:00",
            next_review="2026-05-02 10:00:00",
        )
        assert await fc_repo.delete(created_user, card["id"]) is True

        progress = await flash_repo.get_progress(created_user, card["hash"])
        assert progress is None
        assert await fc_repo.count_by_subject(created_user, "math") == 0

    async def test_delete_missing_returns_false(self, fc_repo, created_user):
        assert await fc_repo.delete(created_user, 99999) is False

    async def test_count_by_subject(self, fc_repo, created_user):
        assert await fc_repo.count_by_subject(created_user, "math") == 0
        await fc_repo.create(created_user, "math", "A", "1")
        await fc_repo.create(created_user, "math", "B", "2")
        assert await fc_repo.count_by_subject(created_user, "math") == 2

    async def test_limit_100_per_subject(self, fc_repo, created_user):
        for i in range(UserFlashcardRepository.MAX_PER_SUBJECT):
            await fc_repo.create(created_user, "math", f"term{i}", f"def{i}")
        with pytest.raises(ValueError, match="limit_exceeded"):
            await fc_repo.create(
                created_user, "math", "overflow", "should fail"
            )

    async def test_user_isolation(self, fc_repo, user_repo, created_user):
        other = 1001
        await user_repo.create_user(other)
        await fc_repo.create(created_user, "math", "Secret", "Mine")
        assert await fc_repo.count_by_subject(other, "math") == 0
        assert await fc_repo.list_by_subject(other, "math") == []


class TestNotificationSettingsFlashcardSource:
    async def test_default_flashcard_source_mix(self, user_repo, created_user):
        settings = await user_repo.get_notification_settings(created_user)
        assert settings["flashcard_source"] == "mix"

    async def test_update_flashcard_source(self, user_repo, created_user):
        await user_repo.update_notification_settings(
            created_user,
            {
                "morning_enabled": 1,
                "morning_time": "09:00",
                "evening_enabled": 1,
                "evening_time": "21:00",
                "streak_enabled": 1,
                "achievements_enabled": 1,
                "flashcard_source": "own",
            },
        )
        settings = await user_repo.get_notification_settings(created_user)
        assert settings["flashcard_source"] == "own"
