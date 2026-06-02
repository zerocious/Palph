"""
Тесты services.render_pet — path-resolution для pet assets.

Покрывает:
- PET_SINGLE_IMAGE_MODE → всегда default.png
- Time-of-day subdirs → period default, then root default
- Existing combination → корректный path (legacy multi-asset mode)
- Missing combination → fallback на <emotion>_orange_none.png
- Animated=True → .gif path (legacy) или default.png (single-image)
- Все assets отсутствуют → FileNotFoundError
- user_pet=None → дефолтные orange/none
"""
from datetime import datetime
from pathlib import Path

import pytest

import services
from services import PET_SINGLE_IMAGE_MODE, render_pet


# Тесты предполагают, что `scripts/build_pet_assets.py` запущен
# и сгенерировал 75 PNG + 3 GIF. Если нет — большинство тестов упадут
# с FileNotFoundError; в этом случае запусти:
#   python scripts/build_pet_assets.py


class TestRenderPetSingleImageMode:
    @pytest.mark.parametrize("emotion,color,accessory", [
        ("neutral", "orange", "none"),
        ("sad", "blue", "hat"),
        ("joy", "pink", "crown"),
    ])
    def test_always_returns_default_png(self, emotion, color, accessory):
        if not PET_SINGLE_IMAGE_MODE:
            pytest.skip("PET_SINGLE_IMAGE_MODE is off")
        pet = {"color": color, "accessory": accessory}
        path = render_pet(pet, emotion)
        assert path.exists()
        assert path.name == "default.png"

    @pytest.mark.parametrize("emotion", ["neutral", "joy", "sad"])
    def test_animated_also_returns_default_png(self, emotion):
        if not PET_SINGLE_IMAGE_MODE:
            pytest.skip("PET_SINGLE_IMAGE_MODE is off")
        path = render_pet({"color": "pink", "accessory": "crown"},
                          emotion, animated=True)
        assert path.exists()
        assert path.name == "default.png"

    def test_user_pet_none_uses_default(self):
        if not PET_SINGLE_IMAGE_MODE:
            pytest.skip("PET_SINGLE_IMAGE_MODE is off")
        path = render_pet(None, "neutral")
        assert path.exists()
        assert path.name == "default.png"


class TestRenderPetExistingCombinations:
    @pytest.mark.parametrize("emotion,color,accessory", [
        ("neutral", "orange", "none"),
        ("sad", "blue", "hat"),
        ("joy", "pink", "crown"),
    ])
    def test_resolves_to_existing_png(self, emotion, color, accessory, monkeypatch):
        if PET_SINGLE_IMAGE_MODE:
            monkeypatch.setattr(services, "PET_SINGLE_IMAGE_MODE", False)
        pet = {"color": color, "accessory": accessory}
        path = render_pet(pet, emotion)
        assert path.exists()
        assert path.name == f"{emotion}_{color}_{accessory}.png"


class TestRenderPetFallback:
    def test_unknown_color_falls_back_to_orange_none(self, monkeypatch):
        """Если запросить цвет/аксессуар, которых build-script не генерил,
        fallback на дефолтную комбинацию."""
        if PET_SINGLE_IMAGE_MODE:
            monkeypatch.setattr(services, "PET_SINGLE_IMAGE_MODE", False)
        pet = {"color": "rainbow", "accessory": "topHat"}  # не в каталогах
        path = render_pet(pet, "neutral")
        assert path.exists()
        assert path.name == "neutral_orange_none.png"

    def test_user_pet_none_uses_default(self, monkeypatch):
        """Питомец ещё не создан (None) → дефолтные orange + none."""
        if PET_SINGLE_IMAGE_MODE:
            monkeypatch.setattr(services, "PET_SINGLE_IMAGE_MODE", False)
        path = render_pet(None, "neutral")
        assert path.exists()
        assert path.name == "neutral_orange_none.png"


class TestRenderPetAnimated:
    @pytest.mark.parametrize("emotion", ["neutral", "joy", "sad"])
    def test_resolves_to_gif(self, emotion, monkeypatch):
        if PET_SINGLE_IMAGE_MODE:
            monkeypatch.setattr(services, "PET_SINGLE_IMAGE_MODE", False)
        path = render_pet({"color": "orange", "accessory": "none"},
                          emotion, animated=True)
        assert path.exists()
        assert path.name == f"{emotion}.gif"
        assert path.suffix == ".gif"

    def test_animated_ignores_color_accessory(self, monkeypatch):
        """GIF универсален per emotion — color/accessory из user_pet
        игнорируются."""
        if PET_SINGLE_IMAGE_MODE:
            monkeypatch.setattr(services, "PET_SINGLE_IMAGE_MODE", False)
        path = render_pet({"color": "pink", "accessory": "crown"},
                          "joy", animated=True)
        assert path.name == "joy.gif"


class TestRenderPetLegacyFallback:
    def test_joy_falls_back_to_happy_gif(self, tmp_path, monkeypatch):
        monkeypatch.setattr(services, "PET_SINGLE_IMAGE_MODE", False)
        monkeypatch.setattr(services, "_ASSETS_PET_DIR", tmp_path)
        monkeypatch.setattr(services, "_PET_DEFAULT_IMAGE", tmp_path / "default.png")
        legacy = tmp_path / "happy.gif"
        legacy.write_bytes(b"gif")
        path = render_pet(None, "joy", animated=True)
        assert path == legacy

    def test_neutral_falls_back_to_sleepy_png(self, tmp_path, monkeypatch):
        monkeypatch.setattr(services, "PET_SINGLE_IMAGE_MODE", False)
        monkeypatch.setattr(services, "_ASSETS_PET_DIR", tmp_path)
        monkeypatch.setattr(services, "_PET_DEFAULT_IMAGE", tmp_path / "default.png")
        legacy = tmp_path / "sleepy_orange_none.png"
        legacy.write_bytes(b"png")
        path = render_pet(None, "neutral")
        assert path == legacy

    def test_legacy_emotion_input_maps_to_new_assets(self, monkeypatch):
        if PET_SINGLE_IMAGE_MODE:
            monkeypatch.setattr(services, "PET_SINGLE_IMAGE_MODE", False)
        path = render_pet(None, "happy")
        assert path.exists()
        assert path.name == "joy_orange_none.png"


class TestRenderPetTimePeriod:
    def test_period_subdir_png_before_flat_fallback(self, tmp_path, monkeypatch):
        monkeypatch.setattr(services, "PET_SINGLE_IMAGE_MODE", False)
        monkeypatch.setattr(services, "_ASSETS_PET_DIR", tmp_path)
        monkeypatch.setattr(services, "_PET_DEFAULT_IMAGE", tmp_path / "default.png")

        evening_dir = tmp_path / "evening"
        evening_dir.mkdir()
        target = evening_dir / "neutral_blue_hat.png"
        target.write_bytes(b"png")

        path = render_pet(
            {"color": "blue", "accessory": "hat"},
            "neutral",
            now_local=datetime(2026, 6, 2, 19, 0),
        )
        assert path == target

    def test_period_default_png_in_single_image_mode(self, tmp_path, monkeypatch):
        monkeypatch.setattr(services, "PET_SINGLE_IMAGE_MODE", True)
        monkeypatch.setattr(services, "_ASSETS_PET_DIR", tmp_path)
        root_default = tmp_path / "default.png"
        root_default.write_bytes(b"root")
        period_default = tmp_path / "morning" / "default.png"
        period_default.parent.mkdir()
        period_default.write_bytes(b"morning")
        monkeypatch.setattr(services, "_PET_DEFAULT_IMAGE", root_default)

        path = render_pet(
            None, "neutral",
            now_local=datetime(2026, 6, 2, 8, 0),
        )
        assert path == period_default

    def test_missing_period_asset_falls_back_to_root_default(self, tmp_path, monkeypatch):
        monkeypatch.setattr(services, "PET_SINGLE_IMAGE_MODE", True)
        monkeypatch.setattr(services, "_ASSETS_PET_DIR", tmp_path)
        root_default = tmp_path / "default.png"
        root_default.write_bytes(b"root")
        monkeypatch.setattr(services, "_PET_DEFAULT_IMAGE", root_default)

        path = render_pet(
            None, "neutral",
            now_local=datetime(2026, 6, 2, 14, 0),
        )
        assert path == root_default

    def test_explicit_time_period_overrides_now_local(self, tmp_path, monkeypatch):
        monkeypatch.setattr(services, "PET_SINGLE_IMAGE_MODE", False)
        monkeypatch.setattr(services, "_ASSETS_PET_DIR", tmp_path)
        monkeypatch.setattr(services, "_PET_DEFAULT_IMAGE", tmp_path / "default.png")

        night_dir = tmp_path / "night"
        night_dir.mkdir()
        night_png = night_dir / "sad_orange_none.png"
        night_png.write_bytes(b"png")

        path = render_pet(
            None, "sad",
            now_local=datetime(2026, 6, 2, 14, 0),
            time_period="night",
        )
        assert path == night_png


class TestRenderPetMissingAssets:
    def test_unknown_emotion_falls_back_to_neutral_orange_none(self, monkeypatch):
        if PET_SINGLE_IMAGE_MODE:
            monkeypatch.setattr(services, "PET_SINGLE_IMAGE_MODE", False)
        path = render_pet({"color": "rainbow", "accessory": "topHat"},
                          "invalid_emotion_xyz")
        assert path.exists()
        assert path.name == "neutral_orange_none.png"
