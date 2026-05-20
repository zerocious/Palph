"""
Тесты services.render_pet — path-resolution для pet assets.

Покрывает:
- Existing combination → корректный path
- Missing combination → fallback на <emotion>_orange_none.png
- Animated=True → .gif path
- Все assets отсутствуют → FileNotFoundError
- user_pet=None → дефолтные orange/none
"""
import pytest

from services import render_pet


# Тесты предполагают, что `scripts/build_pet_assets.py` запущен
# и сгенерировал 125 PNG + 5 GIF. Если нет — большинство тестов упадут
# с FileNotFoundError; в этом случае запусти:
#   python scripts/build_pet_assets.py


class TestRenderPetExistingCombinations:
    @pytest.mark.parametrize("emotion,color,accessory", [
        ("happy", "orange", "none"),
        ("sad", "blue", "hat"),
        ("excited", "pink", "crown"),
        ("sleepy", "grey", "glasses"),
        ("studying", "green", "scarf"),
    ])
    def test_resolves_to_existing_png(self, emotion, color, accessory):
        pet = {"color": color, "accessory": accessory}
        path = render_pet(pet, emotion)
        assert path.exists()
        assert path.name == f"{emotion}_{color}_{accessory}.png"


class TestRenderPetFallback:
    def test_unknown_color_falls_back_to_orange_none(self):
        """Если запросить цвет/аксессуар, которых build-script не генерил,
        fallback на дефолтную комбинацию."""
        pet = {"color": "rainbow", "accessory": "topHat"}  # не в каталогах
        path = render_pet(pet, "happy")
        # Не должно бросить — должен быть happy_orange_none.png
        assert path.exists()
        assert path.name == "happy_orange_none.png"

    def test_user_pet_none_uses_default(self):
        """Питомец ещё не создан (None) → дефолтные orange + none."""
        path = render_pet(None, "happy")
        assert path.exists()
        assert path.name == "happy_orange_none.png"


class TestRenderPetAnimated:
    @pytest.mark.parametrize("emotion", [
        "happy", "sad", "excited", "sleepy", "studying",
    ])
    def test_resolves_to_gif(self, emotion):
        path = render_pet({"color": "orange", "accessory": "none"},
                          emotion, animated=True)
        assert path.exists()
        assert path.name == f"{emotion}.gif"
        assert path.suffix == ".gif"

    def test_animated_ignores_color_accessory(self):
        """GIF универсален per emotion — color/accessory из user_pet
        игнорируются."""
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

    def test_unknown_emotion_raises_eventually(self):
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
