"""
Тесты friend invite-link feature: FriendRepository invite methods +
end-to-end deep-link integration through repo level.

Покрывает:
- create_invite_token: возвращает уникальные токены, ~16 символов
- find_invite_token: валидный → from_uid; неизвестный → None; истёкший → None
- accept_invite: happy path / self-invite / already_friends
- Multi-use токена: один токен может принять несколько invitee
- accept_invite чистит pending friend_requests
"""
import pytest
import pytest_asyncio

from repository import FriendRepository


@pytest_asyncio.fixture
async def friend_repo(db):
    return FriendRepository(db)


async def _make_users(user_repo, *uids):
    """Создаёт пользователей с переданными ID."""
    for uid in uids:
        await user_repo.create_user(uid)


# ============================================================
# create_invite_token
# ============================================================
class TestCreateInviteToken:
    async def test_returns_string(self, friend_repo, user_repo):
        await _make_users(user_repo, 1)
        token = await friend_repo.create_invite_token(1)
        assert isinstance(token, str)
        # secrets.token_urlsafe(12) даёт ~16 символов
        assert 10 <= len(token) <= 32

    async def test_tokens_are_unique(self, friend_repo, user_repo):
        """Двукратный вызов даёт два разных токена (random source)."""
        await _make_users(user_repo, 1)
        t1 = await friend_repo.create_invite_token(1)
        t2 = await friend_repo.create_invite_token(1)
        assert t1 != t2

    async def test_token_stored_with_creator(
        self, friend_repo, user_repo, db
    ):
        await _make_users(user_repo, 7)
        token = await friend_repo.create_invite_token(7)
        async with db.execute(
            "SELECT from_user_id FROM friend_invite_tokens WHERE token=?",
            (token,),
        ) as c:
            row = await c.fetchone()
        assert row["from_user_id"] == 7

    async def test_token_has_3day_expiry(
        self, friend_repo, user_repo, db
    ):
        await _make_users(user_repo, 1)
        token = await friend_repo.create_invite_token(1)
        # expires_at должен быть ~3 days в будущем (julianday-разница в днях)
        async with db.execute(
            "SELECT julianday(expires_at) - julianday('now') AS days_remaining "
            "FROM friend_invite_tokens WHERE token=?",
            (token,),
        ) as c:
            row = await c.fetchone()
        # Допуск ±0.1 дня (rounding/clock)
        assert 2.9 < row["days_remaining"] < 3.1


# ============================================================
# find_invite_token
# ============================================================
class TestFindInviteToken:
    async def test_valid_token_resolves(self, friend_repo, user_repo):
        await _make_users(user_repo, 42)
        token = await friend_repo.create_invite_token(42)
        assert await friend_repo.find_invite_token(token) == 42

    async def test_unknown_token_returns_none(self, friend_repo):
        assert await friend_repo.find_invite_token("nonexistent") is None

    async def test_empty_token_returns_none(self, friend_repo):
        assert await friend_repo.find_invite_token("") is None

    async def test_none_token_returns_none(self, friend_repo):
        assert await friend_repo.find_invite_token(None) is None

    async def test_expired_token_returns_none(
        self, friend_repo, user_repo, db
    ):
        """Симулируем истёкший токен через UPDATE expires_at в прошлое."""
        await _make_users(user_repo, 1)
        token = await friend_repo.create_invite_token(1)
        # Двигаем expires_at в прошлое
        await db.execute(
            "UPDATE friend_invite_tokens SET expires_at = datetime('now', '-1 day') "
            "WHERE token=?",
            (token,),
        )
        await db.commit()
        assert await friend_repo.find_invite_token(token) is None


# ============================================================
# accept_invite
# ============================================================
class TestAcceptInvite:
    async def test_happy_path_creates_friendship(
        self, friend_repo, user_repo, db
    ):
        await _make_users(user_repo, 1, 2)
        result = await friend_repo.accept_invite(1, 2)
        assert result == "accepted"
        # Нормализованная дружба создана
        ua, ub = friend_repo._norm_pair(1, 2)
        async with db.execute(
            "SELECT 1 FROM friendships WHERE user_a=? AND user_b=?",
            (ua, ub),
        ) as c:
            assert await c.fetchone() is not None
        # are_friends подтверждает
        assert await friend_repo.are_friends(1, 2) is True

    async def test_self_invite_rejected(self, friend_repo, user_repo):
        await _make_users(user_repo, 1)
        assert await friend_repo.accept_invite(1, 1) == "self"
        assert await friend_repo.get_friends(1) == []

    async def test_already_friends_returns_already_friends(
        self, friend_repo, user_repo
    ):
        await _make_users(user_repo, 1, 2)
        await friend_repo.accept_invite(1, 2)
        # Повторный accept — already_friends
        assert await friend_repo.accept_invite(1, 2) == "already_friends"
        # Тот же результат с обратным порядком (normalized)
        assert await friend_repo.accept_invite(2, 1) == "already_friends"

    async def test_normalized_storage_regardless_of_order(
        self, friend_repo, user_repo, db
    ):
        """accept_invite(5, 3) и accept_invite(3, 5) дают одну строку (3, 5)."""
        await _make_users(user_repo, 3, 5)
        await friend_repo.accept_invite(5, 3)  # creator=5, invitee=3
        async with db.execute(
            "SELECT user_a, user_b FROM friendships"
        ) as c:
            row = await c.fetchone()
        assert row["user_a"] == 3
        assert row["user_b"] == 5

    async def test_pending_request_cleaned_up(
        self, friend_repo, user_repo, db
    ):
        """
        Если был pending friend_request от creator → invitee (или наоборот),
        accept_invite через deep-link его удаляет (deep-link обходит pending).
        """
        await _make_users(user_repo, 1, 2)
        # Сначала обычный pending request 1→2
        await friend_repo.send_request(1, 2)
        async with db.execute(
            "SELECT COUNT(*) AS n FROM friend_requests"
        ) as c:
            assert (await c.fetchone())["n"] == 1

        # Теперь deep-link от 1 → 2 кликает
        await friend_repo.accept_invite(1, 2)

        # pending был очищен; friendship создан
        async with db.execute(
            "SELECT COUNT(*) AS n FROM friend_requests"
        ) as c:
            assert (await c.fetchone())["n"] == 0
        assert await friend_repo.are_friends(1, 2) is True


# ============================================================
# Multi-use (один токен → много друзей)
# ============================================================
class TestMultiUseToken:
    async def test_same_token_creates_multiple_friendships(
        self, friend_repo, user_repo
    ):
        """Alice (1) создаёт токен; Bob (2), Carol (3), Dave (4) кликают —
        все становятся друзьями Alice."""
        await _make_users(user_repo, 1, 2, 3, 4)
        token = await friend_repo.create_invite_token(1)

        for invitee in (2, 3, 4):
            creator = await friend_repo.find_invite_token(token)
            assert creator == 1
            assert await friend_repo.accept_invite(creator, invitee) == "accepted"

        # Alice friends [2, 3, 4]
        assert sorted(await friend_repo.get_friends(1)) == [2, 3, 4]


# ============================================================
# End-to-end: create → resolve → accept (через repo level)
# ============================================================
class TestInviteFlowEndToEnd:
    async def test_full_create_resolve_accept_flow(
        self, friend_repo, user_repo
    ):
        await _make_users(user_repo, 10, 20)
        # Alice (10) создаёт токен
        token = await friend_repo.create_invite_token(10)
        # Bob (20) клик резолвит → Alice
        creator = await friend_repo.find_invite_token(token)
        assert creator == 10
        # Auto-friendship
        assert await friend_repo.accept_invite(creator, 20) == "accepted"
        # Bidirectional friendship visible
        assert await friend_repo.are_friends(10, 20) is True
        assert 20 in await friend_repo.get_friends(10)
        assert 10 in await friend_repo.get_friends(20)

    async def test_expired_token_does_not_create_friendship(
        self, friend_repo, user_repo, db
    ):
        await _make_users(user_repo, 1, 2)
        token = await friend_repo.create_invite_token(1)
        await db.execute(
            "UPDATE friend_invite_tokens SET expires_at = datetime('now', '-1 day') "
            "WHERE token=?",
            (token,),
        )
        await db.commit()
        # find возвращает None — caller не должен звать accept_invite
        assert await friend_repo.find_invite_token(token) is None
        # Friendship не создалась
        assert await friend_repo.are_friends(1, 2) is False
