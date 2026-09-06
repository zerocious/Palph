"""
Numeric answer matching for photo/text tasks.

Accepts equivalent forms: fractions (a/b), decimals with comma or dot,
integers. Compares via Fraction with optional tolerance for rounded decimals.
"""
from __future__ import annotations

import re
from fractions import Fraction


_NUMERIC_TOKEN_RE = re.compile(
    r"^\s*(-?\d+)\s*(?:[/\\]\s*(-?\d+))?\s*$"
)
_DECIMAL_RE = re.compile(
    r"^\s*(-?\d+)[.,](\d+)\s*$"
)


def _parse_numeric_token(text: str) -> Fraction | None:
    """Parse a single numeric token as exact Fraction, or None."""
    raw = text.strip()
    if not raw:
        return None

    dec = _DECIMAL_RE.match(raw)
    if dec:
        whole, frac = dec.group(1), dec.group(2)
        sign = -1 if whole.startswith("-") else 1
        whole_abs = whole.lstrip("-")
        try:
            return Fraction(int(whole_abs) * sign, 1) + Fraction(int(frac) * sign, 10 ** len(frac))
        except (ValueError, ZeroDivisionError):
            return None

    m = _NUMERIC_TOKEN_RE.match(raw)
    if not m:
        return None
    num = int(m.group(1))
    den = m.group(2)
    try:
        if den is None:
            return Fraction(num, 1)
        den_i = int(den)
        if den_i == 0:
            return None
        return Fraction(num, den_i)
    except (ValueError, ZeroDivisionError):
        return None


def _normalize_text_answer(text: str) -> str:
    no_punct = re.sub(r"[^\w\s]", " ", text.lower())
    return re.sub(r"\s+", " ", no_punct).strip()


def _fractions_from_accepted(accepted: list[str]) -> list[Fraction]:
    values: list[Fraction] = []
    for ans in accepted:
        frac = _parse_numeric_token(ans)
        if frac is not None:
            values.append(frac)
    return values


def _fraction_close(a: Fraction, b: Fraction, *, places: int = 6) -> bool:
    """
    Сравнение с допуском 0.5 * 10^-places, но в точной арифметике.

    Раньше допуск считался как abs(float(a) - float(b)). Ответ пользователя
    парсится регуляркой (-?\d+), то есть число цифр ничем не ограничено, а
    Telegram пропускает сообщение до 4096 символов. На ответе примерно от
    400 цифр float() падал с OverflowError прямо в handle_task_answer —
    обработчик его не ловит, и пользователь не получал вообще никакого
    ответа на свою попытку.

    Fraction считает точно и не переполняется, а заодно даёт корректный
    результат там, где float терял точность на больших числах.
    """
    if a == b:
        return True
    return abs(a - b) <= Fraction(1, 2 * 10 ** places)


def task_answer_matches(user_text: str, accepted: list[str]) -> bool:
    """
    Return True if user_text matches any accepted answer.
    Falls back to normalized string equality when parsing fails.
    """
    if not accepted:
        return False

    user_norm = _normalize_text_answer(user_text)
    accepted_norm = {_normalize_text_answer(a) for a in accepted}
    if user_norm in accepted_norm:
        return True

    user_frac = _parse_numeric_token(user_text.strip())
    if user_frac is None:
        return False

    ref_fracs = _fractions_from_accepted(accepted)
    if not ref_fracs:
        return False

    return any(_fraction_close(user_frac, ref) for ref in ref_fracs)
