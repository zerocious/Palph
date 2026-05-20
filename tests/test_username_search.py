"""
Тесты username-search для /friends:
- services.parse_friend_query — pure parser FSM-ввода
- UserRepository.refresh_username — обновление кеша
- UserRepository.find_user_id_by_username — case-insensitive lookup
"""
import pytest
import pytest_asyncio

from services import parse_friend_query


# ============================================================
# parse_friend_query — pure function
# ============================================================
class TestParseFriendQuery:
    def test_at_username(self):
        assert parse_friend_query("@alice") == ("alice", None)

    def test_bare_username_no_at(self):
        """alphanumeric без @ трактуется как username."""
        assert parse_friend_query("alice") == ("alice", None)

    def test_numeric_id(self):
        assert parse_friend_query("12345") == (None, 12345)

    def test_negative_id(self):
        """Defensive: отрицательные ID (Telegram чаты)."""
        assert parse_friend_query("-12345") == (None, -12345)

    def test_empty(self):
        assert parse_friend_query("") == (None, None)

    def test_whitespace_only(self):
        assert parse_friend_query("   ") == (None, None)

    def test_strips_outer_whitespace(self):
        assert parse_friend_query("  @alice  ") == ("alice", None)
        assert parse_friend_query("  123  ") == (None, 123)

    def test_at_with_no_username(self):
        """'@' пустой — никакого username, никакой ID."""
        assert parse_friend_query("@") == (None, None)
        assert parse_friend_query("@   ") == (None, None)

    def test_internal_whitespace_kept_as_username(self):
        """'foo bar' — username path, но lookup всё равно не найдёт."""
        username, target = parse_friend_query("foo bar")
        assert username == "foo bar"
        assert target is None

    def test_mixed_alphanumeric_treated_as_username(self):
        """user_123 — это username (буква в начале)."""
        assert parse_friend_query("user_123") == ("user_123", None)

    def test_none_input(self):
        """Defensive: None instead of str."""
        assert parse_friend_query(None) == (None, None)


# ============================================================
# UserRepository.refresh_username
# ============================================================
class TestRefreshUsername:
    async def test_set_initial_username(self, user_repo, created_user, db):
        await user_repo.refresh_username(created_user, "alice")
        async with db.execute(
            "SELECT username FROM users WHERE user_id=?", (created_user,)
        ) as c:
            assert (await c.fetchone())["username"] == "alice"

    async def test_change_username(self, user_repo, created_user, db):
        await user_repo.refresh_username(created_user, "alice")
        await user_repo.refresh_username(created_user, "alice2")
        async with db.execute(
            "SELECT username FROM users WHERE user_id=?", (created_user,)
        ) as c:
            assert (await c.fetchone())["username"] == "alice2"

    async def test_clear_to_none(self, user_repo, created_user, db):
        """Если Telegram-пользователь удалил публичный @handle — сохраняем NULL."""
        await user_repo.refresh_username(created_user, "alice")
        await user_repo.refresh_username(created_user, None)
        async with db.execute(
            "SELECT username FROM users WHERE user_id=?", (created_user,)
        ) as c:
            assert (await c.fetchone())["username"] is None

    async def test_no_op_on_missing_user(self, user_repo, db):
        """User не существует — refresh не должен ломаться."""
        await user_repo.refresh_username(99999, "ghost")
        # Ничего не вставилось
        async with db.execute(
            "SELECT COUNT(*) AS n FROM users WHERE user_id=?", (99999,)
        ) as c:
            assert (await c.fetchone())["n"] == 0


# ============================================================
# UserRepository.find_user_id_by_username
# ============================================================
class TestFindUserIdByUsername:
    async def test_found(self, user_repo, created_user):
        await user_repo.refresh_username(created_user, "alice")
        assert await user_repo.find_user_id_by_username("alice") == created_user

    async def test_not_found(self, user_repo, created_user):
        await user_repo.refresh_username(created_user, "alice")
        assert await user_repo.find_user_id_by_username("bob") is None

    async def test_case_insensitive(self, user_repo, created_user):
        """COLLATE NOCASE — 'Alice' == 'alice' == 'ALICE'."""
        await user_repo.refresh_username(created_user, "Alice")
        assert await user_repo.find_user_id_by_username("alice") == created_user
        assert await user_repo.find_user_id_by_username("ALICE") == created_user
        assert await user_repo.find_user_id_by_username("AlIcE") == created_user

    async def test_empty_input(self, user_repo, created_user):
        await user_repo.refresh_username(created_user, "alice")
        assert await user_repo.find_user_id_by_username("") is None

    async def test_none_input(self, user_repo, created_user):
        await user_repo.refresh_username(created_user, "alice")
        assert await user_repo.find_user_id_by_username(None) is None

    async def test_after_username_cleared(self, user_repo, created_user):
        """Если username сброшен в NULL — lookup больше не находит."""
        await user_repo.refresh_username(created_user, "alice")
        await user_repo.refresh_username(created_user, None)
        assert await user_repo.find_user_id_by_username("alice") is None

    async def test_multiple_users(self, user_repo, db):
        """Lookup корректен среди нескольких юзеров."""
        for uid, handle in [(1, "alice"), (2, "bob"), (3, "carol")]:
            await user_repo.create_user(uid)
            await user_repo.refresh_username(uid, handle)
        assert await user_repo.find_user_id_by_username("alice") == 1
        assert await user_repo.find_user_id_by_username("bob") == 2
        assert await user_repo.find_user_id_by_username("carol") == 3
        assert await user_repo.find_user_id_by_username("dave") is None


# ============================================================
# create_user with username kwarg — закрывает first-message gap
# ============================================================
class TestCreateUserWithUsername:
    """
    UsernameSyncMiddleware фактически делает UPDATE до того как handler
    создаёт строку user'а (см. cmd_start). Чтобы новый user сразу был
    findable по @handle, create_user принимает username и сидит его
    при INSERT'е.
    """

    async def test_creates_with_username(self, user_repo, db):
        await user_repo.create_user(42, username="alice")
        assert await user_repo.find_user_id_by_username("alice") == 42

    async def test_default_username_is_none(self, user_repo, db):
        """Без передачи username — NULL (backwards-compat с конфтест-фикстурой)."""
        await user_repo.create_user(42)
        async with db.execute(
            "SELECT username FROM users WHERE user_id=?", (42,)
        ) as c:
            assert (await c.fetchone())["username"] is None

    async def test_existing_user_not_overwritten(self, user_repo, db):
        """INSERT OR IGNORE: если user уже есть, повторный create
        с другим username не должен перетереть существующий handle."""
        await user_repo.create_user(42, username="alice")
        await user_repo.create_user(42, username="bob")  # ignored
        async with db.execute(
            "SELECT username FROM users WHERE user_id=?", (42,)
        ) as c:
            assert (await c.fetchone())["username"] == "alice"
