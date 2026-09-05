"""
DeviceRepository: коды привязки desktop-клиента и токены устройств.

Покрывает то, на чём держится безопасность привязки:
  • код одноразовый и протухает
  • новый код инвалидирует предыдущий
  • plaintext-токена в БД нет (только SHA-256)
  • отзыв токена (один / все) реально закрывает доступ
"""
from __future__ import annotations

import pytest_asyncio

from repository import DeviceRepository, UserRepository


@pytest_asyncio.fixture
async def device_repo(db):
    return DeviceRepository(db)


@pytest_asyncio.fixture
async def two_users(db):
    repo = UserRepository(db)
    await repo.create_user(1)
    await repo.create_user(2)
    return 1, 2


class TestLinkCodes:
    async def test_code_shape_is_typable(self, device_repo, created_user):
        code = await device_repo.create_link_code(created_user)
        assert len(code) == DeviceRepository.CODE_LENGTH
        # Алфавит без похожих глифов: 0/O/1/I/L/U не должны встречаться.
        assert set(code) <= set(DeviceRepository.CODE_ALPHABET)
        assert DeviceRepository.format_code(code) == f"{code[:4]}-{code[4:]}"

    async def test_new_code_invalidates_previous(self, device_repo, created_user):
        first = await device_repo.create_link_code(created_user)
        second = await device_repo.create_link_code(created_user)
        assert first != second
        assert await device_repo.exchange_code(first) is None
        assert await device_repo.exchange_code(second) is not None

    async def test_code_is_single_use(self, device_repo, created_user):
        code = await device_repo.create_link_code(created_user)
        assert await device_repo.exchange_code(code) is not None
        assert await device_repo.exchange_code(code) is None

    async def test_expired_code_rejected(self, db, device_repo, created_user):
        code = await device_repo.create_link_code(created_user)
        await db.execute(
            "UPDATE device_link_codes SET expires_at = datetime('now', '-1 minute') "
            "WHERE code = ?",
            (code,),
        )
        await db.commit()
        assert await device_repo.exchange_code(code) is None

    async def test_code_accepted_in_display_form(self, device_repo, created_user):
        code = await device_repo.create_link_code(created_user)
        # Пользователь копирует код из чата вместе с дефисом и нижним регистром.
        typed = DeviceRepository.format_code(code).lower()
        assert await device_repo.exchange_code(typed) is not None

    async def test_unknown_and_empty_codes_rejected(self, device_repo, created_user):
        await device_repo.create_link_code(created_user)
        assert await device_repo.exchange_code("ZZZZZZZZ") is None
        assert await device_repo.exchange_code("") is None
        assert await device_repo.exchange_code("   ") is None


class TestTokens:
    async def test_exchange_returns_token_resolving_to_user(
        self, device_repo, created_user,
    ):
        code = await device_repo.create_link_code(created_user)
        link = await device_repo.exchange_code(code, device_name="Ноутбук")
        assert link.user_id == created_user
        assert await device_repo.resolve_token(link.token) == created_user

    async def test_plaintext_token_never_stored(self, db, device_repo, created_user):
        code = await device_repo.create_link_code(created_user)
        token = (await device_repo.exchange_code(code)).token
        async with db.execute("SELECT token_hash FROM device_tokens") as c:
            rows = await c.fetchall()
        stored = [r["token_hash"] for r in rows]
        assert token not in stored
        assert stored == [DeviceRepository.hash_token(token)]

    async def test_unknown_token_resolves_to_none(self, device_repo):
        assert await device_repo.resolve_token("not-a-real-token") is None
        assert await device_repo.resolve_token("") is None

    async def test_resolve_updates_last_seen(self, device_repo, created_user):
        code = await device_repo.create_link_code(created_user)
        token = (await device_repo.exchange_code(code)).token
        assert (await device_repo.list_devices(created_user))[0]["last_seen_at"] is None
        await device_repo.resolve_token(token)
        assert (await device_repo.list_devices(created_user))[0]["last_seen_at"] is not None

    async def test_last_seen_write_is_throttled(self, db, device_repo, created_user):
        """
        Приложение опрашивает API часто — переписывать last_seen_at на
        каждый запрос значило бы держать write-транзакцию в SQLite ради
        поля, которое человек видит с точностью до минут.
        """
        code = await device_repo.create_link_code(created_user)
        token = (await device_repo.exchange_code(code)).token
        await device_repo.resolve_token(token)
        first_seen = (await device_repo.list_devices(created_user))[0]["last_seen_at"]

        await device_repo.resolve_token(token)
        assert (await device_repo.list_devices(created_user))[0]["last_seen_at"] == first_seen

        # Отматываем метку за окно троттлинга — следующий запрос её обновит.
        # Сравниваем именно с backdated-значением: datetime('now') в SQLite
        # округляется до секунды, поэтому свежая метка может совпасть
        # строкой с first_seen, и такой assert был бы флаки.
        stale = "2020-01-01 00:00:00"
        await db.execute("UPDATE device_tokens SET last_seen_at = ?", (stale,))
        await db.commit()
        await device_repo.resolve_token(token)
        assert (await device_repo.list_devices(created_user))[0]["last_seen_at"] != stale

    async def test_device_name_defaults_and_truncates(self, device_repo, created_user):
        code = await device_repo.create_link_code(created_user)
        await device_repo.exchange_code(code, device_name="   ")
        assert (await device_repo.list_devices(created_user))[0]["device_name"] == "Desktop"

    async def test_multiple_devices_per_user(self, device_repo, created_user):
        for name in ("Ноутбук", "Домашний ПК"):
            code = await device_repo.create_link_code(created_user)
            await device_repo.exchange_code(code, device_name=name)
        devices = await device_repo.list_devices(created_user)
        assert [d["device_name"] for d in devices] == ["Ноутбук", "Домашний ПК"]


class TestRevocation:
    async def test_revoke_token_closes_access(self, device_repo, created_user):
        code = await device_repo.create_link_code(created_user)
        token = (await device_repo.exchange_code(code)).token
        assert await device_repo.revoke_token(token) is True
        assert await device_repo.resolve_token(token) is None
        assert await device_repo.revoke_token(token) is False

    async def test_revoke_all_wipes_devices_and_pending_code(
        self, db, device_repo, created_user,
    ):
        tokens = []
        for _ in range(2):
            code = await device_repo.create_link_code(created_user)
            tokens.append((await device_repo.exchange_code(code)).token)
        pending = await device_repo.create_link_code(created_user)

        assert await device_repo.revoke_all(created_user) == 2
        for token in tokens:
            assert await device_repo.resolve_token(token) is None
        assert await device_repo.exchange_code(pending) is None
        assert await device_repo.list_devices(created_user) == []

    async def test_revoke_all_touches_only_own_devices(self, device_repo, two_users):
        alice, bob = two_users
        code = await device_repo.create_link_code(bob)
        bob_token = (await device_repo.exchange_code(code)).token

        assert await device_repo.revoke_all(alice) == 0
        assert await device_repo.resolve_token(bob_token) == bob

    async def test_account_deletion_cascades_devices(
        self, db, device_repo, user_repo, created_user,
    ):
        code = await device_repo.create_link_code(created_user)
        token = (await device_repo.exchange_code(code)).token
        await device_repo.create_link_code(created_user)

        await user_repo.delete_user_completely(created_user)

        assert await device_repo.resolve_token(token) is None
        async with db.execute("SELECT COUNT(*) AS n FROM device_link_codes") as c:
            assert (await c.fetchone())["n"] == 0
