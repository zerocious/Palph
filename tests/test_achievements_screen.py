"""
Экран «🏆 Достижения»: локализация, страницы, кеш каталога.

Регрессия: экран брал глобальный ACHIEVEMENTS (ru-fallback) вместо
load_achievements_catalog(locale), а подписи «ПОЛУЧЕНО!/ЗАБЛОКИРОВАНО» и
число страниц были зашиты в код. Англоязычный пользователь видел русские
названия — при том что achievements.en.json заполнен полностью и уже
использовался в уведомлениях о выдаче.
"""
import json

import pytest

import bot
import locale_bot
from bot import render_achievements_page
from i18n import SUPPORTED_LOCALES, t
from locale_bot import load_achievements_catalog


@pytest.fixture(autouse=True)
def _clean_catalog_cache():
    """Кеш каталога — модульный global; чистим вокруг каждого теста."""
    locale_bot._achievements_cache.clear()
    yield
    locale_bot._achievements_cache.clear()


class TestCatalogLocalisation:
    def test_en_catalog_is_not_russian(self):
        ru = load_achievements_catalog("ru")
        en = load_achievements_catalog("en")
        assert ru and en
        shared = set(ru) & set(en)
        assert shared, "каталоги не пересекаются по id — сравнивать нечего"
        differing = [k for k in shared if ru[k]["name"] != en[k]["name"]]
        assert differing, "en-каталог повторяет русские названия"

    def test_en_catalog_covers_every_id(self):
        ru = load_achievements_catalog("ru")
        en = load_achievements_catalog("en")
        assert set(ru) <= set(en), f"нет перевода: {set(ru) - set(en)}"

    def test_unknown_locale_falls_back(self):
        assert load_achievements_catalog("klingon") == load_achievements_catalog("ru")

    def test_every_entry_has_fields_screen_needs(self):
        for loc in SUPPORTED_LOCALES:
            for ach_id, data in load_achievements_catalog(loc).items():
                for field in ("name", "description", "reward", "icon", "page"):
                    assert field in data, f"{loc}/{ach_id}: нет поля {field}"


class TestScreenLabels:
    def test_labels_exist_in_every_locale(self):
        for loc in SUPPORTED_LOCALES:
            for key in ("title", "earned", "locked", "empty"):
                full = f"achievements_screen.{key}"
                assert t(full, loc) != full, f"{loc}: нет ключа {full}"

    def test_labels_differ_between_locales(self):
        """Ключ обязан быть реально переведён, а не скопирован из ru."""
        for key in ("earned", "locked"):
            ru = t(f"achievements_screen.{key}", "ru")
            en = t(f"achievements_screen.{key}", "en")
            assert ru != en, f"{key}: en повторяет ru"

    def test_title_formats_page_numbers(self):
        for loc in SUPPORTED_LOCALES:
            rendered = t("achievements_screen.title", loc, page=2, total=3)
            assert "2" in rendered and "3" in rendered
            assert "{" not in rendered


class TestPagination:
    def test_pages_are_derived_from_catalog(self):
        """
        Число страниц берётся из каталога. Раньше было зашито range(1, 4):
        ачивка с page=4 стала бы недостижимой.
        """
        catalog = load_achievements_catalog("ru")
        pages = sorted({int(v.get("page", 1)) for v in catalog.values()})
        assert pages, "в каталоге нет ни одной страницы"
        assert pages == list(range(1, len(pages) + 1)), (
            f"страницы не подряд: {pages} — экран покажет дырки"
        )

    def test_every_achievement_lands_on_some_page(self):
        catalog = load_achievements_catalog("ru")
        pages = {int(v.get("page", 1)) for v in catalog.values()}
        placed = sum(
            1 for v in catalog.values() if int(v.get("page", 1)) in pages
        )
        assert placed == len(catalog)

    def test_locales_agree_on_pagination(self):
        """Иначе у ru и en разное число страниц на одном и том же экране."""
        def pages_of(loc):
            return sorted({int(v.get("page", 1)) for v in load_achievements_catalog(loc).values()})
        assert pages_of("ru") == pages_of("en")


class TestCatalogCache:
    def test_repeated_loads_return_equal_content(self):
        first = load_achievements_catalog("ru")
        second = load_achievements_catalog("ru")
        assert first == second

    def test_returned_catalog_is_a_copy(self):
        """Мутация результата не должна портить каталог всем остальным."""
        first = load_achievements_catalog("ru")
        first.clear()
        assert load_achievements_catalog("ru"), "кеш отдал общий изменяемый dict"

    def test_cache_invalidated_on_file_change(self, tmp_path, monkeypatch):
        monkeypatch.setattr(locale_bot, "BOT_DIR", tmp_path)
        target = tmp_path / "achievements.ru.json"
        target.write_text(json.dumps({"a": {"name": "A"}}), encoding="utf-8")
        assert set(load_achievements_catalog("ru")) == {"a"}

        target.write_text(
            json.dumps({"a": {"name": "A"}, "b": {"name": "B"}}), encoding="utf-8"
        )
        assert set(load_achievements_catalog("ru")) == {"a", "b"}

    def test_missing_files_return_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(locale_bot, "BOT_DIR", tmp_path)
        assert load_achievements_catalog("ru") == {}


# ============================================================
# Сам рендер страницы — то, что видит пользователь
# ============================================================
def _catalog(pages=(1, 1, 2)):
    return {
        f"a{i}": {
            "name": f"Name{i}", "description": f"Desc{i}",
            "reward": 10 * i, "icon": "🏆", "page": pg,
        }
        for i, pg in enumerate(pages, start=1)
    }


class TestRenderAchievementsPage:
    def test_uses_catalog_it_is_given(self):
        """Ключевая регрессия: экран рисует переданный каталог, а не ru-глобал."""
        ru_text, _, _ = render_achievements_page(
            load_achievements_catalog("ru"), {}, 1, "ru"
        )
        en_text, _, _ = render_achievements_page(
            load_achievements_catalog("en"), {}, 1, "en"
        )
        assert ru_text != en_text
        en_names = [
            v["name"] for v in load_achievements_catalog("en").values()
            if int(v.get("page", 1)) == 1
        ]
        assert any(n in en_text for n in en_names)

    def test_en_screen_has_no_russian_labels(self):
        text, _, _ = render_achievements_page(
            load_achievements_catalog("en"), {}, 1, "en"
        )
        assert t("achievements_screen.locked", "ru") not in text
        assert t("achievements_screen.locked", "en") in text

    def test_three_states_rendered(self):
        catalog = _catalog(pages=(1, 1, 1))
        progress = {
            "a1": {"completed": True, "progress": 3, "target": 3},
            "a2": {"completed": False, "progress": 1, "target": 3},
            # a3 — строки нет вовсе
        }
        text, _, _ = render_achievements_page(catalog, progress, 1, "ru")
        assert t("achievements_screen.earned", "ru") in text
        assert "1/3" in text
        assert t("achievements_screen.locked", "ru") in text

    def test_only_requested_page_is_shown(self):
        catalog = _catalog(pages=(1, 1, 2))
        text, pages, page = render_achievements_page(catalog, {}, 2, "ru")
        assert pages == [1, 2] and page == 2
        assert "Name3" in text
        assert "Name1" not in text

    def test_out_of_range_page_falls_back(self):
        """Кривой callback_data не должен рисовать пустой экран."""
        catalog = _catalog(pages=(1, 1, 2))
        for bad in (0, 3, 999, -5):
            text, pages, page = render_achievements_page(catalog, {}, bad, "ru")
            assert page in pages
            assert "Name" in text, f"page={bad} дал пустой экран"

    def test_page_four_is_reachable(self):
        """Раньше число страниц было зашито range(1, 4) — page=4 терялась."""
        catalog = _catalog(pages=(1, 2, 3, 4))
        text, pages, page = render_achievements_page(catalog, {}, 4, "ru")
        assert pages == [1, 2, 3, 4] and page == 4
        assert "Name4" in text

    def test_empty_page_says_so(self):
        text, pages, page = render_achievements_page({}, {}, 1, "ru")
        assert t("achievements_screen.empty", "ru") in text

    def test_missing_fields_do_not_crash(self):
        """Неполная запись в каталоге не должна ронять экран."""
        text, _, _ = render_achievements_page({"x": {}}, {}, 1, "ru")
        assert "x" in text

    def test_real_catalog_renders_every_page(self):
        for loc in SUPPORTED_LOCALES:
            catalog = load_achievements_catalog(loc)
            _, pages, _ = render_achievements_page(catalog, {}, 1, loc)
            for p in pages:
                text, _, actual = render_achievements_page(catalog, {}, p, loc)
                assert actual == p
                assert t("achievements_screen.empty", loc) not in text

