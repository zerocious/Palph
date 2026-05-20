"""
Тесты FriendRepository — Phase 4 friends system.

Покрывает:
- Lifecycle: send / accept / reject / cancel
- send_request status-paths: sent / self_target / user_not_found /
  already_friends / already_pending / auto_accepted (reverse pending)
- Normalized friendships (user_a < user_b — одна строка)
- get_friends UNION над обеими сторонами PK
- are_friends bidirectional + symmetric
- remove_friend bidirectional + symmetric
"""
import pytest
import pytest_asyncio

from repository import FriendRepository


@pytest_asyncio.fixture
async def friend_repo(db):
    return FriendRepository(db)


async def _two_users(user_repo, db, a=1, b=2):
    """Создаёт двух пользователей. Возвращает (a, b) для удобства."""
    await user_repo.create_user(a)
    await user_repo.create_user(b)
    return a, b


# ============================================================
# send_request — статусы
# ============================================================
class TestSendRequest:
    async def test_sent_happy_path(self, friend_repo, user_repo, db):
        a, b = await _two_users(user_repo, db)
        result = await friend_repo.send_request(a, b)
        assert result == "sent"
        # Row exists in friend_requests
        async with db.execute(
            "SELECT 1 FROM friend_requests WHERE from_user_id=? AND to_user_id=?",
            (a, b),
        ) as c:
            assert (await c.fetchone()) is not None

    async def test_self_target_rejected(self, friend_repo, user_repo, db):
        await user_repo.create_user(1)
        result = await friend_repo.send_request(1, 1)
        assert result == "self_target"
        async with db.execute(
            "SELECT COUNT(*) AS n FROM friend_requests"
        ) as c:
            assert (await c.fetchone())["n"] == 0

    async def test_user_not_found(self, friend_repo, user_repo, db):
        await user_repo.create_user(1)
        # User 99 doesn't exist
        result = await friend_repo.send_request(1, 99)
        assert result == "user_not_found"

    async def test_already_friends(self, friend_repo, user_repo, db):
        a, b = await _two_users(user_repo, db)
        await friend_repo.send_request(a, b)
        await friend_repo.accept_request(a, b)
        # Now send again → already_friends
        result = await friend_repo.send_request(a, b)
        assert result == "already_friends"
        # Также reverse direction
        result_rev = await friend_repo.send_request(b, a)
        assert result_rev == "already_friends"

    async def test_already_pending(self, friend_repo, user_repo, db):
        a, b = await _two_users(user_repo, db)
        await friend_repo.send_request(a, b)
        result = await friend_repo.send_request(a, b)
        assert result == "already_pending"

    async def test_auto_accepted_via_reverse_pending(
        self, friend_repo, user_repo, db
    ):
        """B → A в pending; A отправляет → автоматически принимается."""
        a, b = await _two_users(user_repo, db)
        # B sends to A first
        await friend_repo.send_request(b, a)
        # Now A sends to B → auto-accept
        result = await friend_repo.send_request(a, b)
        assert result == "auto_accepted"
        # Friendship существует
        assert await friend_repo.are_friends(a, b) is True
        # Reverse pending удалён
        async with db.execute(
            "SELECT COUNT(*) AS n FROM friend_requests"
        ) as c:
            assert (await c.fetchone())["n"] == 0


# ============================================================
# accept_request
# ============================================================
class TestAcceptRequest:
    async def test_accept_creates_normalized_friendship(
        self, friend_repo, user_repo, db
    ):
        """Дружба хранится с user_a < user_b независимо от направления request'а."""
        a, b = await _two_users(user_repo, db, a=5, b=3)  # b < a numerically
        await friend_repo.send_request(a, b)  # request от 5 к 3
        accepted = await friend_repo.accept_request(a, b)
        assert accepted is True
        # Нормализация: row должен быть (3, 5) — smaller first
        async with db.execute(
            "SELECT user_a, user_b FROM friendships"
        ) as c:
            row = await c.fetchone()
        assert row["user_a"] == 3
        assert row["user_b"] == 5

    async def test_accept_deletes_request(self, friend_repo, user_repo, db):
        a, b = await _two_users(user_repo, db)
        await friend_repo.send_request(a, b)
        await friend_repo.accept_request(a, b)
        async with db.execute(
            "SELECT COUNT(*) AS n FROM friend_requests"
        ) as c:
            assert (await c.fetchone())["n"] == 0

    async def test_accept_nonexistent_request(self, friend_repo, user_repo, db):
        a, b = await _two_users(user_repo, db)
        accepted = await friend_repo.accept_request(a, b)
        assert accepted is False


# ============================================================
# reject + cancel
# ============================================================
class TestRejectAndCancel:
    async def test_reject_deletes_request(self, friend_repo, user_repo, db):
        a, b = await _two_users(user_repo, db)
        await friend_repo.send_request(a, b)
        rejected = await friend_repo.reject_request(a, b)
        assert rejected is True
        assert await friend_repo.are_friends(a, b) is False

    async def test_reject_nonexistent(self, friend_repo, user_repo, db):
        a, b = await _two_users(user_repo, db)
        rejected = await friend_repo.reject_request(a, b)
        assert rejected is False

    async def test_cancel_is_same_as_reject(self, friend_repo, user_repo, db):
        """cancel_request = reject_request по семантике — sender отменяет."""
        a, b = await _two_users(user_repo, db)
        await friend_repo.send_request(a, b)
        canceled = await friend_repo.cancel_request(a, b)
        assert canceled is True


# ============================================================
# get_friends — UNION over both sides
# ============================================================
class TestGetFriends:
    async def test_empty(self, friend_repo, user_repo, db):
        await user_repo.create_user(1)
        assert await friend_repo.get_friends(1) == []

    async def test_one_friend_visible_from_both_sides(
        self, friend_repo, user_repo, db
    ):
        a, b = await _two_users(user_repo, db, a=1, b=2)
        await friend_repo.send_request(a, b)
        await friend_repo.accept_request(a, b)
        assert await friend_repo.get_friends(a) == [b]
        assert await friend_repo.get_friends(b) == [a]

    async def test_multiple_friends(self, friend_repo, user_repo, db):
        """User 1 дружит с 2, 3, 5 (3 add'ил 1, 5 add'ил 1, 1 add'ил 2)."""
        for uid in (1, 2, 3, 5):
            await user_repo.create_user(uid)
        # 1 → 2 (1 запросил, 2 принял)
        await friend_repo.send_request(1, 2)
        await friend_repo.accept_request(1, 2)
        # 3 → 1 (3 запросил, 1 принял)
        await friend_repo.send_request(3, 1)
        await friend_repo.accept_request(3, 1)
        # 5 → 1
        await friend_repo.send_request(5, 1)
        await friend_repo.accept_request(5, 1)

        friends = sorted(await friend_repo.get_friends(1))
        assert friends == [2, 3, 5]


# ============================================================
# are_friends — symmetric
# ============================================================
class TestAreFriends:
    async def test_friends_symmetric(self, friend_repo, user_repo, db):
        a, b = await _two_users(user_repo, db)
        await friend_repo.send_request(a, b)
        await friend_repo.accept_request(a, b)
        assert await friend_repo.are_friends(a, b) is True
        assert await friend_repo.are_friends(b, a) is True

    async def test_not_friends(self, friend_repo, user_repo, db):
        a, b = await _two_users(user_repo, db)
        assert await friend_repo.are_friends(a, b) is False

    async def test_pending_is_not_friendship(self, friend_repo, user_repo, db):
        a, b = await _two_users(user_repo, db)
        await friend_repo.send_request(a, b)
        # Pending != friends
        assert await friend_repo.are_friends(a, b) is False

    async def test_self_not_friends(self, friend_repo, user_repo):
        await user_repo.create_user(1)
        assert await friend_repo.are_friends(1, 1) is False


# ============================================================
# remove_friend — symmetric
# ============================================================
class TestRemoveFriend:
    async def test_remove_bidirectional(self, friend_repo, user_repo, db):
        a, b = await _two_users(user_repo, db)
        await friend_repo.send_request(a, b)
        await friend_repo.accept_request(a, b)
        removed = await friend_repo.remove_friend(a, b)
        assert removed is True
        assert await friend_repo.are_friends(a, b) is False
        assert await friend_repo.get_friends(a) == []
        assert await friend_repo.get_friends(b) == []

    async def test_remove_with_reverse_order_works(
        self, friend_repo, user_repo, db
    ):
        """remove_friend(b, a) удаляет ту же дружбу, что и remove_friend(a, b)."""
        a, b = await _two_users(user_repo, db, a=1, b=5)
        await friend_repo.send_request(a, b)
        await friend_repo.accept_request(a, b)
        # Удаляем "обратным" порядком
        removed = await friend_repo.remove_friend(b, a)
        assert removed is True

    async def test_remove_nonexistent(self, friend_repo, user_repo, db):
        a, b = await _two_users(user_repo, db)
        removed = await friend_repo.remove_friend(a, b)
        assert removed is False


# ============================================================
# Pending lists
# ============================================================
class TestPendingLists:
    async def test_get_pending_received(self, friend_repo, user_repo, db):
        for uid in (1, 2, 3):
            await user_repo.create_user(uid)
        await friend_repo.send_request(2, 1)
        await friend_repo.send_request(3, 1)
        pending = await friend_repo.get_pending_received(1)
        from_ids = sorted(p["from_user_id"] for p in pending)
        assert from_ids == [2, 3]

    async def test_get_pending_sent(self, friend_repo, user_repo, db):
        for uid in (1, 2, 3):
            await user_repo.create_user(uid)
        await friend_repo.send_request(1, 2)
        await friend_repo.send_request(1, 3)
        pending = await friend_repo.get_pending_sent(1)
        to_ids = sorted(p["to_user_id"] for p in pending)
        assert to_ids == [2, 3]

    async def test_received_empty(self, friend_repo, user_repo):
        await user_repo.create_user(1)
        assert await friend_repo.get_pending_received(1) == []
