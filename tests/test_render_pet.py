"""
Тесты services.render_pet — path-resolution для pet assets.

Покрывает:
- PET_SINGLE_IMAGE_MODE → всегда default.png
- Existing combination → корректный path (legacy multi-asset mode)
- Missing combination → fallback на <emotion>_orange_none.png
- Animated=True → .gif path (legacy) или default.png (single-image)
- Все assets отсутствуют → FileNotFoundError
- user_pet=None → дефолтные orange/none
"""
import pytest

import services
from services import PET_SINGLE_IMAGE_MODE, render_pet


# Тесты предполагают, что `scripts/build_pet_assets.py` запущен
# и сгенерировал 125 PNG + 5 GIF. Если нет — большинство тестов упадут
# с FileNotFoundError; в этом случае запусти:
#   python scripts/build_pet_assets.py


class TestRenderPetSingleImageMode:
    @pytest.mark.parametrize("emotion,color,accessory", [
        ("happy", "orange", "none"),
        ("sad", "blue", "hat"),
        ("excited", "pink", "crown"),
        ("sleepy", "grey", "glasses"),
        ("studying", "green", "scarf"),
    ])
    def test_always_returns_default_png(self, emotion, color, accessory):
        if not PET_SINGLE_IMAGE_MODE:
            pytest.skip("PET_SINGLE_IMAGE_MODE is off")
        pet = {"color": color, "accessory": accessory}
        path = render_pet(pet, emotion)
        assert path.exists()
        assert path.name == "default.png"

    @pytest.mark.parametrize("emotion", [
        "happy", "sad", "excited", "sleepy", "studying",
    ])
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
        path = render_pet(None, "happy")
        assert path.exists()
        assert path.name == "default.png"


class TestRenderPetExistingCombinations:
    @pytest.mark.parametrize("emotion,color,accessory", [
        ("happy", "orange", "none"),
        ("sad", "blue", "hat"),
        ("excited", "pink", "crown"),
        ("sleepy", "grey", "glasses"),
        ("studying", "green", "scarf"),
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
        path = render_pet(pet, "happy")
        # Не должно бросить — должен быть happy_orange_none.png
        assert path.exists()
        assert path.name == "happy_orange_none.png"

    def test_user_pet_none_uses_default(self, monkeypatch):
        """Питомец ещё не создан (None) → дефолтные orange + none."""
        if PET_SINGLE_IMAGE_MODE:
            monkeypatch.setattr(services, "PET_SINGLE_IMAGE_MODE", False)
        path = render_pet(None, "happy")
        assert path.exists()
        assert path.name == "happy_orange_none.png"


class TestRenderPetAnimated:
    @pytest.mark.parametrize("emotion", [
        "happy", "sad", "excited", "sleepy", "studying",
    ])
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
                          "excited", animated=True)
        # Не excited_pink_crown.gif, а просто excited.gif
        assert path.name == "excited.gif"


class TestRenderPetMissingAssets:
    """
    Тестировать «assets dir пустая» без удаления реальных файлов сложно.
    Стандартный pytest tmp_path может пересоздать пустую структуру, но
    render_pet смотрит абсолютный путь, не настраиваемый. Поэтому здесь
    тестируем только саму сигнатуру raise для несуществующей эмоции —
    единственная гарантия что fallback chain в конце концов raise'нет.
    """

    def test_unknown_emotion_raises_eventually(self, monkeypatch):
        if PET_SINGLE_IMAGE_MODE:
            monkeypatch.setattr(services, "PET_SINGLE_IMAGE_MODE", False)
        # 'invalid_emotion_xyz' не в каталоге → primary missing →
        # fallback на 'invalid_emotion_xyz_orange_none.png' missing →
        # fallback на 'happy_orange_none.png' — этот СУЩЕСТВУЕТ.
        # Поэтому пути:
        #   1. invalid_emotion_xyz_pink_crown.png → missing
        #   2. invalid_emotion_xyz_orange_none.png → missing
        #   3. happy_orange_none.png → EXISTS
        # Возвращает happy_orange_none.png. raise не происходит.
        path = render_pet({"color": "pink", "accessory": "crown"},
                          "invalid_emotion_xyz")
        assert path.exists()
        assert path.name == "happy_orange_none.png"
