"""
Тесты загрузчика ситуационных квизов и резолва предмета из FSM.

Регрессия, которую они держат: у load_quiz_section был дефолт
subject_id='industrial-management'. handle_quiz_answer вызывал его без
предмета, поэтому первый вопрос приходил из выбранного предмета, а все
последующие — из ОПМ. Сейчас это не видно только потому, что ОПМ —
единственный предмет с непустым situational/, и он скрыт из меню; любой
новый предмет с секциями немедленно воспроизвёл бы баг.
"""
import inspect

import pytest

from bot import (
    QUIZ_SECTION_KEYS,
    _quiz_section_cache,
    _quiz_subject_from_state,
    load_quiz_section,
)


@pytest.fixture(autouse=True)
def _clean_section_cache():
    """
    Кеш секций — модульный global. Тесты подменяют STUDY_MATERIALS_PATH
    на tmp_path, и ключи вида ("math", "i") столкнулись бы с реальными
    предметами в других тестах. Чистим до и после КАЖДОГО теста, а не
    вручную в конце — иначе упавший тест оставит кеш отравленным.
    """
    _quiz_section_cache.clear()
    yield
    _quiz_section_cache.clear()


class TestSubjectIsRequired:
    def test_subject_id_has_no_default(self):
        """
        Дефолт у subject_id — та самая ловушка. Если он вернётся,
        вызов без предмета снова начнёт молча отдавать чужой контент.
        """
        param = inspect.signature(load_quiz_section).parameters["subject_id"]
        assert param.default is inspect.Parameter.empty

    def test_call_without_subject_raises(self):
        with pytest.raises(TypeError):
            load_quiz_section("i")


class TestNoCrossSubjectLeak:
    def test_subject_without_sections_returns_empty(self):
        """
        У math нет situational/section-*.txt — загрузчик обязан вернуть
        пусто, а не подставить секции другого предмета.
        """
        for key in QUIZ_SECTION_KEYS:
            assert load_quiz_section(key, "math") == []

    def test_each_subject_reads_its_own_directory(self):
        """Термины ОПМ не должны появляться под другим предметом."""
        opm = load_quiz_section("i", "industrial-management")
        assert opm, "ожидали непустой section-i у industrial-management"
        opm_terms = {t.term for t in opm}
        for other in ("math", "accounting", "english"):
            leaked = {t.term for t in load_quiz_section("i", other)}
            assert not (leaked & opm_terms), f"утечка контента ОПМ в {other}"


class TestSectionCache:
    def test_repeated_load_returns_equal_content(self):
        first = load_quiz_section("i", "industrial-management")
        second = load_quiz_section("i", "industrial-management")
        assert [t.term for t in first] == [t.term for t in second]

    def test_returned_list_is_a_copy(self):
        """
        Кеш отдаёт копию: рядом в коде принято шаффлить результат
        загрузчика (random.shuffle над load_mcq), и мутация общего
        списка испортила бы контент всем пользователям.
        """
        first = load_quiz_section("i", "industrial-management")
        assert len(first) > 1
        first.clear()
        second = load_quiz_section("i", "industrial-management")
        assert len(second) > 1

    def test_cache_invalidated_when_file_changes(self, tmp_path, monkeypatch):
        import bot

        subject = "math"  # в каталоге, но своих секций не имеет
        section_dir = tmp_path / subject / "situational"
        section_dir.mkdir(parents=True)
        target = section_dir / "section-i.txt"
        target.write_text(
            "Термин А || Определение || кв1, кв2 || Ситуация\n", encoding="utf-8"
        )
        monkeypatch.setattr(bot, "STUDY_MATERIALS_PATH", tmp_path)
        assert [t.term for t in load_quiz_section("i", subject)] == ["Термин А"]

        # Правка контента должна подхватываться без перезапуска.
        target.write_text(
            "Термин А || Определение || кв1, кв2 || Ситуация\n"
            "Термин Б || Определение || кв3, кв4 || Ситуация\n",
            encoding="utf-8",
        )
        # Даже если mtime не успел измениться в пределах разрешения ФС,
        # размер файла другой — инвалидация обязана сработать по нему.
        assert [t.term for t in load_quiz_section("i", subject)] == [
            "Термин А",
            "Термин Б",
        ]

    def test_missing_file_is_not_cached_as_empty(self, tmp_path, monkeypatch):
        """Отсутствующий файл не должен «залипать» пустым после появления."""
        import bot

        subject = "math"
        section_dir = tmp_path / subject / "situational"
        section_dir.mkdir(parents=True)
        monkeypatch.setattr(bot, "STUDY_MATERIALS_PATH", tmp_path)
        assert load_quiz_section("i", subject) == []

        (section_dir / "section-i.txt").write_text(
            "Термин В || Определение || кв || Ситуация\n", encoding="utf-8"
        )
        assert [t.term for t in load_quiz_section("i", subject)] == ["Термин В"]


class TestQuizSubjectFromState:
    def test_returns_subject_from_state(self):
        assert _quiz_subject_from_state({"subject_id": "math"}) == "math"

    def test_missing_subject_is_none(self):
        assert _quiz_subject_from_state({}) is None
        assert _quiz_subject_from_state({"subject_id": None}) is None
        assert _quiz_subject_from_state({"subject_id": ""}) is None

    def test_subject_outside_catalog_is_none(self):
        """Мусор в состоянии не должен превращаться в чтение чужой папки."""
        assert _quiz_subject_from_state({"subject_id": ".."}) is None
        assert _quiz_subject_from_state({"subject_id": "evil"}) is None
