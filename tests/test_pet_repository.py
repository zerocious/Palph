"""
Тесты PetRepository — data layer питомца (v0.7 TODO #16).

Ключевые инварианты:
- create_pet_with_defaults идемпотентен; сидит инвентарь (orange, none)
- add_xp auto-создаёт pet при первой сессии; level = floor(sqrt(xp/10))+1
- level-up автоматически обновляет last_excited_at
- purchase_item атомарен под db.lock: проверяет level + balance + ownership
- equip требует ownership (нельзя надеть некупленное)
"""
import pytest
import pytest_asyncio

from repository import PetRepository


@pytest_asyncio.fixture
async def pet_repo(db):
    return PetRepository(db)


# ============================================================
# create_pet_with_defaults
# ============================================================
class TestCreatePetWithDefaults:
    async def test_creates_pet_row_with_defaults(self, pet_repo, created_user, db):
        await pet_repo.create_pet_with_defaults(created_user)
        async with db.execute(
            "SELECT name, color, accessory, level, xp FROM user_pet "
            "WHERE user_id=?", (created_user,)
        ) as c:
            row = await c.fetchone()
        assert row["name"] == "Питомец"
        assert row["color"] == "orange"
        assert row["accessory"] == "none"
        assert row["level"] == 1
        assert row["xp"] == 0

    async def test_seeds_default_inventory(self, pet_repo, created_user, db):
        """Инвентарь содержит (color, orange) и (accessory, none) бесплатно."""
        await pet_repo.create_pet_with_defaults(created_user)
        async with db.execute(
            "SELECT item_type, item_value FROM user_pet_inventory "
            "WHERE user_id=? ORDER BY item_type", (created_user,)
        ) as c:
            rows = await c.fetchall()
        items = {(r["item_type"], r["item_value"]) for r in rows}
        assert ("color", "orange") in items
        assert ("accessory", "none") in items

    async def test_idempotent(self, pet_repo, created_user):
        """Второй вызов не raises и сообщает что create уже был."""
        first = await pet_repo.create_pet_with_defaults(created_user)
        second = await pet_repo.create_pet_with_defaults(created_user)
        assert first is True
        assert second is False

    async def test_custom_name(self, pet_repo, created_user, db):
        await pet_repo.create_pet_with_defaults(created_user, name="Барсик")
        pet = await pet_repo.get_pet(created_user)
        assert pet["name"] == "Барсик"


# ============================================================
# get_pet / get_inventory
# ============================================================
class TestRead:
    async def test_get_pet_returns_none_for_unknown(self, pet_repo):
        assert await pet_repo.get_pet(9999) is None

    async def test_get_inventory_empty_for_no_pet(self, pet_repo):
        assert await pet_repo.get_inventory(9999) == []


# ============================================================
# add_xp + level formula + auto-create
# ============================================================
class TestAddXp:
    async def test_auto_creates_pet_on_first_call(self, pet_repo, created_user, db):
        """Первая сессия создаёт pet без явного create_pet_with_defaults."""
        await pet_repo.add_xp(created_user, 25)
        pet = await pet_repo.get_pet(created_user)
        assert pet is not None
        assert pet["xp"] == 25
        # Инвентарь тоже сидится
        inv = await pet_repo.get_inventory(created_user)
        kinds = {(i["item_type"], i["item_value"]) for i in inv}
        assert ("color", "orange") in kinds
        assert ("accessory", "none") in kinds

    async def test_increments_xp_across_calls(self, pet_repo, created_user):
        await pet_repo.add_xp(created_user, 5)
        await pet_repo.add_xp(created_user, 7)
        pet = await pet_repo.get_pet(created_user)
        assert pet["xp"] == 12

    @pytest.mark.parametrize("xp,expected_level", [
        (0, 1),    # стартовый уровень
        (9, 1),    # ниже 10 XP — всё ещё уровень 1
        (10, 2),   # ровно на границе
        (39, 2),   # 40 XP — следующая граница
        (40, 3),
        (89, 3),
        (90, 4),   # sqrt(9) = 3, +1 = 4
        (160, 5),  # sqrt(16) = 4, +1 = 5
    ])
    def test_xp_to_level_formula(self, xp, expected_level):
        """floor(sqrt(xp/10)) + 1 — формула из спеки."""
        assert PetRepository.xp_to_level(xp) == expected_level

    async def test_level_up_marks_last_excited_at(self, pet_repo, created_user):
        """При level-up автоматически обновляется last_excited_at."""
        # 10 XP → уровень 2 (был 1)
        old, new = await pet_repo.add_xp(created_user, 10)
        assert old == 1
        assert new == 2
        pet = await pet_repo.get_pet(created_user)
        assert pet["last_excited_at"] is not None

    async def test_no_level_up_no_excited_stamp(self, pet_repo, created_user):
        """Если уровень не вырос — last_excited_at не пишется."""
        old, new = await pet_repo.add_xp(created_user, 5)  # xp=5, level stays 1
        assert old == new == 1
        pet = await pet_repo.get_pet(created_user)
        assert pet["last_excited_at"] is None

    async def test_negative_minutes_no_op(self, pet_repo, created_user):
        await pet_repo.add_xp(created_user, -1)
        # Pet даже не создан — no-op
        assert await pet_repo.get_pet(created_user) is None

    async def test_zero_minutes_no_op(self, pet_repo, created_user):
        await pet_repo.add_xp(created_user, 0)
        assert await pet_repo.get_pet(created_user) is None


# ============================================================
# purchase_item — атомарность
# ============================================================
class TestPurchaseItem:
    async def test_happy_path_deducts_inserts_equips(
        self, pet_repo, user_repo, created_user, db
    ):
        # Setup: pet существует, есть монеты, level достаточный
        await pet_repo.create_pet_with_defaults(created_user)
        await user_repo.add_coins(created_user, 100)
        # grey unlock_level=1, price=20 — должно пройти
        result = await pet_repo.purchase_item(created_user, "color", "grey")
        assert result == "purchased"
        # Coins списались
        async with db.execute(
            "SELECT total_coins FROM users WHERE user_id=?", (created_user,)
        ) as c:
            assert (await c.fetchone())["total_coins"] == 80
        # В инвентаре появился
        inv = await pet_repo.get_inventory(created_user)
        assert any(i["item_value"] == "grey" for i in inv)
        # Auto-equip
        pet = await pet_repo.get_pet(created_user)
        assert pet["color"] == "grey"

    async def test_insufficient_coins(
        self, pet_repo, user_repo, created_user, db
    ):
        await pet_repo.create_pet_with_defaults(created_user)
        await user_repo.add_coins(created_user, 10)  # < 20
        result = await pet_repo.purchase_item(created_user, "color", "grey")
        assert result == "insufficient_coins"
        # Никаких изменений
        async with db.execute(
            "SELECT total_coins FROM users WHERE user_id=?", (created_user,)
        ) as c:
            assert (await c.fetchone())["total_coins"] == 10
        pet = await pet_repo.get_pet(created_user)
        assert pet["color"] == "orange"  # не поменялся

    async def test_insufficient_level(
        self, pet_repo, user_repo, created_user
    ):
        await pet_repo.create_pet_with_defaults(created_user)
        await user_repo.add_coins(created_user, 1000)  # коинов хватает
        # crown требует level 8
        result = await pet_repo.purchase_item(created_user, "accessory", "crown")
        assert result == "insufficient_level"

    async def test_already_owned(self, pet_repo, created_user):
        """Дефолтные orange / none уже в инвентаре — повторная покупка идемпотентна."""
        await pet_repo.create_pet_with_defaults(created_user)
        result = await pet_repo.purchase_item(created_user, "color", "orange")
        assert result == "already_owned"

    async def test_unknown_item(self, pet_repo, created_user):
        await pet_repo.create_pet_with_defaults(created_user)
        assert await pet_repo.purchase_item(
            created_user, "color", "neon"
        ) == "unknown_item"
        assert await pet_repo.purchase_item(
            created_user, "shoes", "boots"
        ) == "unknown_item"

    async def test_no_pet(self, pet_repo, created_user):
        """Defensive: если pet ещё не создан — purchase возвращает 'no_pet'."""
        assert await pet_repo.purchase_item(
            created_user, "color", "grey"
        ) == "no_pet"


# ============================================================
# equip / rename / mark_excited
# ============================================================
class TestEquip:
    async def test_equip_owned_item(
        self, pet_repo, user_repo, created_user
    ):
        await pet_repo.create_pet_with_defaults(created_user)
        await user_repo.add_coins(created_user, 100)
        # Купим серый и переключимся на orange обратно через equip
        await pet_repo.purchase_item(created_user, "color", "grey")
        ok = await pet_repo.equip(created_user, "color", "orange")
        assert ok is True
        pet = await pet_repo.get_pet(created_user)
        assert pet["color"] == "orange"

    async def test_equip_unowned_fails(self, pet_repo, created_user):
        await pet_repo.create_pet_with_defaults(created_user)
        # blue не куплен — не должен equip'нуться
        ok = await pet_repo.equip(created_user, "color", "blue")
        assert ok is False
        pet = await pet_repo.get_pet(created_user)
        assert pet["color"] == "orange"  # unchanged

    async def test_equip_invalid_type(self, pet_repo, created_user):
        await pet_repo.create_pet_with_defaults(created_user)
        ok = await pet_repo.equip(created_user, "shoes", "boots")
        assert ok is False


class TestRename:
    async def test_rename_existing_pet(self, pet_repo, created_user):
        await pet_repo.create_pet_with_defaults(created_user)
        ok = await pet_repo.rename(created_user, "Шарик")
        assert ok is True
        pet = await pet_repo.get_pet(created_user)
        assert pet["name"] == "Шарик"

    async def test_rename_nonexistent_pet(self, pet_repo, created_user):
        ok = await pet_repo.rename(created_user, "Шарик")
        assert ok is False


class TestMarkExcited:
    async def test_sets_timestamp(self, pet_repo, created_user, db):
        await pet_repo.create_pet_with_defaults(created_user)
        # Изначально NULL
        assert (await pet_repo.get_pet(created_user))["last_excited_at"] is None
        await pet_repo.mark_excited(created_user)
        assert (await pet_repo.get_pet(created_user))["last_excited_at"] is not None
