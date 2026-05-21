"""
Тесты load_flashcards_for_study: mix / official / own + hash isolation.
"""
import pytest
import pytest_asyncio

import bot
from bot import load_flashcards_for_study, load_flashcards
from repository import UserFlashcardRepository, UserRepository, FlashcardRepository


@pytest_asyncio.fixture
async def fc_repo(db):
    return UserFlashcardRepository(db)


@pytest_asyncio.fixture
async def wired_bot_repos(db, user_repo, fc_repo):
    """Подключает глобальные repo bot.py для async loader-тестов."""
    bot.user_flashcard_repo = fc_repo
    bot.user_repo = user_repo
    bot.db = db
    yield
    bot.user_flashcard_repo = None
    bot.user_repo = None
    bot.db = None


class TestLoadFlashcardsForStudy:
    async def test_official_only(self, wired_bot_repos, fc_repo, created_user):
        official = load_flashcards("math")
        cards = await load_flashcards_for_study(created_user, "math", "official")
        assert cards == official
        assert all(not c["hash"].startswith("u") for c in cards)

    async def test_own_only(self, wired_bot_repos, fc_repo, created_user):
        user_card = await fc_repo.create(
            created_user, "math", "My term", "My definition"
        )
        cards = await load_flashcards_for_study(created_user, "math", "own")
        assert len(cards) == 1
        assert cards[0]["hash"] == user_card["hash"]
        assert cards[0]["hash"].startswith("u")

    async def test_mix_combines_both(self, wired_bot_repos, fc_repo, created_user):
        official = load_flashcards("math")
        user_card = await fc_repo.create(
            created_user, "math", "Custom", "User card"
        )
        cards = await load_flashcards_for_study(created_user, "math", "mix")
        hashes = {c["hash"] for c in cards}
        assert user_card["hash"] in hashes
        for c in official:
            assert c["hash"] in hashes
        assert len(cards) == len(official) + 1

    async def test_own_empty_when_no_user_cards(
        self, wired_bot_repos, created_user
    ):
        cards = await load_flashcards_for_study(created_user, "math", "own")
        assert cards == []

    async def test_user_card_sm2_progress_isolated(
        self, wired_bot_repos, fc_repo, created_user, db
    ):
        """Smoke: user card hash works with flashcard_progress."""
        flash_repo = FlashcardRepository(db)
        card = await fc_repo.create(created_user, "english", "Test", "Answer")
        await flash_repo.upsert_progress(
            created_user,
            card["hash"],
            ease_factor=2.5,
            interval_days=3,
            repetitions=3,
            last_review="2026-05-20 10:00:00",
            next_review="2099-05-04 10:00:00",
        )
        progress = await flash_repo.get_progress(created_user, card["hash"])
        assert progress is not None
        assert progress["repetitions"] == 3
        next_hash = await flash_repo.get_next_card_hash(
            created_user, [card["hash"]]
        )
        assert next_hash is None  # not due yet
