"""
Тесты bot._picker_button_label — pure helper для 4-state customization
picker (TODO #16 Phase B).

Состояния (по спеке):
- ⭐ equipped (надето) → callback_data='pet_locked:equipped'
- ✓ owned (куплено, не надето) → callback_data='pet_equip:<type>:<value>'
- 💰 N buyable (level OK, не в инвентаре) → 'pet_buy:<type>:<value>'
- 🔒 lv.N locked (level недостаточен) → 'pet_locked:level:N'
"""
import pytest

from bot import _picker_button_label  # conftest fallback BOT_TOKEN
from repository import PetRepository


# Catalog references (must match repository.py)
COLOR = PetRepository.COLOR_CATALOG
ACCESSORY = PetRepository.ACCESSORY_CATALOG


def _pet(level=1, color="orange", accessory="none"):
    return {"level": level, "color": color, "accessory": accessory}


class TestEquippedState:
    def test_color_equipped(self):
        pet = _pet(level=10, color="blue")
        text, cb = _picker_button_label("blue", COLOR, pet, set(), "color")
        assert "⭐" in text
        assert "blue" in text
        assert cb == "pet_locked:equipped"

    def test_accessory_equipped(self):
        pet = _pet(level=10, accessory="hat")
        text, cb = _picker_button_label("hat", ACCESSORY, pet, set(), "accessory")
        assert "⭐" in text
        assert cb == "pet_locked:equipped"


class TestOwnedNotEquipped:
    def test_color_owned(self):
        pet = _pet(level=10, color="orange")  # NOT blue
        text, cb = _picker_button_label(
            "blue", COLOR, pet, {"blue"}, "color"
        )
        assert "✓" in text
        assert "blue" in text
        assert cb == "pet_equip:color:blue"

    def test_accessory_owned(self):
        pet = _pet(level=10, accessory="none")  # NOT hat
        text, cb = _picker_button_label(
            "hat", ACCESSORY, pet, {"hat"}, "accessory"
        )
        assert "✓" in text
        assert cb == "pet_equip:accessory:hat"


class TestBuyable:
    def test_color_buyable(self):
        """Pet level >= unlock_level, not owned → buyable."""
        # blue: unlock_level=2, price=40 → user_level=5 → buyable
        pet = _pet(level=5, color="orange")
        text, cb = _picker_button_label("blue", COLOR, pet, set(), "color")
        assert "💰" in text
        assert "40" in text  # price
        assert "blue" in text
        assert cb == "pet_buy:color:blue"

    def test_accessory_buyable(self):
        # hat: unlock_level=1, price=30 → level 5 → buyable
        pet = _pet(level=5, accessory="none")
        text, cb = _picker_button_label("hat", ACCESSORY, pet, set(), "accessory")
        assert "💰" in text
        assert "30" in text
        assert cb == "pet_buy:accessory:hat"


class TestLocked:
    def test_color_locked_due_to_level(self):
        """pink: unlock_level=4 → user level 2 → locked."""
        pet = _pet(level=2, color="orange")
        text, cb = _picker_button_label("pink", COLOR, pet, set(), "color")
        assert "🔒" in text
        assert "4" in text  # required unlock level
        assert cb == "pet_locked:level:4"

    def test_accessory_locked_due_to_level(self):
        """crown: unlock_level=8 → user level 2 → locked."""
        pet = _pet(level=2, accessory="none")
        text, cb = _picker_button_label("crown", ACCESSORY, pet, set(), "accessory")
        assert "🔒" in text
        assert "8" in text  # required unlock level
        assert cb == "pet_locked:level:8"


class TestEdgeCases:
    def test_no_pet_uses_level_1(self):
        """user_pet=None → дефолтный level 1 → только lvl-1 предметы buyable."""
        # grey unlock_level=1 → buyable for level 1 user
        text, cb = _picker_button_label("grey", COLOR, None, set(), "color")
        assert cb == "pet_buy:color:grey"
        # blue unlock_level=2 → locked for level 1
        text2, cb2 = _picker_button_label("blue", COLOR, None, set(), "color")
        assert cb2 == "pet_locked:level:2"

    def test_owned_takes_precedence_over_buyable(self):
        """Owned + level OK → ✓ (not 💰)."""
        pet = _pet(level=10, color="orange")
        text, cb = _picker_button_label(
            "blue", COLOR, pet, {"blue"}, "color"
        )
        assert "✓" in text
        assert "💰" not in text

    def test_equipped_takes_precedence_over_owned(self):
        """Equipped + owned → ⭐ (not ✓). Inventory всё равно содержит."""
        pet = _pet(level=10, color="blue")
        text, cb = _picker_button_label(
            "blue", COLOR, pet, {"blue"}, "color"
        )
        assert "⭐" in text
        assert "✓" not in text
