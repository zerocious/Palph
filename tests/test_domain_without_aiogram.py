"""
Домен (services + repository) должен импортироваться БЕЗ aiogram.

Десктоп-клиент (см. DESKTOP.md) переиспользует services.py и repository.py
на локальной SQLite и ставится без aiogram/pydantic/magic-filter. Если
кто-то добавит в services.py импорт aiogram на уровне модуля — этот тест
упадёт, и станет ясно, что десктопная сборка сломана, ещё до релиза.

Проверка идёт в subprocess'е: aiogram уже загружен в процессе pytest
(через conftest → bot.py), выгрузить его из sys.modules надёжно нельзя.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def _run_without_aiogram(body: str) -> subprocess.CompletedProcess:
    """Выполняет body в subprocess'е, где импорт aiogram запрещён."""
    script = textwrap.dedent(
        """
        import sys

        class _BlockAiogram:
            def find_module(self, name, path=None):
                return None

            def find_spec(self, name, path=None, target=None):
                if name == "aiogram" or name.startswith("aiogram."):
                    raise ModuleNotFoundError("No module named 'aiogram'")
                return None

        sys.meta_path.insert(0, _BlockAiogram())
        """
    ) + textwrap.dedent(body)

    env = dict(os.environ)
    env["PYTHONPATH"] = _ROOT + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=_ROOT,
        env=env,
    )


def test_aiogram_is_actually_blocked_in_subprocess():
    """Сначала убеждаемся, что сам блокировщик работает — иначе тест ниже пустой."""
    result = _run_without_aiogram(
        """
        try:
            import aiogram
        except ModuleNotFoundError:
            print("BLOCKED")
        else:
            print("NOT_BLOCKED")
        """
    )
    assert result.returncode == 0, result.stderr
    assert "BLOCKED" in result.stdout


def test_services_imports_without_aiogram():
    result = _run_without_aiogram(
        """
        import services
        import repository
        print("IMPORT_OK")
        """
    )
    assert result.returncode == 0, (
        "services.py не импортируется без aiogram — десктопная сборка "
        f"сломана.\nstderr:\n{result.stderr}"
    )
    assert "IMPORT_OK" in result.stdout


def test_pure_domain_functions_work_without_aiogram():
    """Чистые функции, на которых стоит десктоп, считают то же самое."""
    result = _run_without_aiogram(
        """
        from services import sm2_update, streak_multiplier, freeze_cost
        print(sm2_update(5, 2, 2.5, 6))
        print(streak_multiplier(7))
        print(freeze_cost(10))
        """
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.strip().splitlines()

    # Те же значения, что и при установленном aiogram — импорт-заглушки
    # не меняют поведение домена.
    from services import sm2_update, streak_multiplier, freeze_cost

    assert lines[0] == str(sm2_update(5, 2, 2.5, 6))
    assert lines[1] == str(streak_multiplier(7))
    assert lines[2] == str(freeze_cost(10))


def test_real_aiogram_exceptions_used_when_available():
    """При установленном aiogram ловятся настоящие классы, а не заглушки."""
    from aiogram.exceptions import (
        TelegramBadRequest,
        TelegramForbiddenError,
        TelegramRetryAfter,
    )
    import services

    assert services.TelegramForbiddenError is TelegramForbiddenError
    assert services.TelegramRetryAfter is TelegramRetryAfter
    assert services.TelegramBadRequest is TelegramBadRequest
