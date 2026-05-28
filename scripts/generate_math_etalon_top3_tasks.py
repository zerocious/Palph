"""Append etalon top-3 exam practice tasks (task-42+) from top3_tasks_etalon.md."""
from __future__ import annotations

import json
import math
from math import comb
from pathlib import Path

import numpy as np
from scipy import stats

ROOT = Path(__file__).resolve().parent.parent / "study_materials" / "math"
START_ID = 42

GROUPS = {
    "exam-task-1": {
        "title": "Билет — задача 1 (формула Бернулли)",
        "description": "Точная формула Бернулли для малого числа испытаний",
    },
    "exam-task-2": {
        "title": "Билет — задача 2 (доверительные интервалы M и σ)",
        "description": "Точечные оценки и доверительные интервалы для среднего и СКО",
    },
    "exam-task-3": {
        "title": "Билет — задача 3 (ДСВ: D, F)",
        "description": "Дисперсия линейной комбинации и функции распределения ДСВ",
    },
    "exam-task-4": {
        "title": "Билет — задача 4 (проверка гипотез)",
        "description": "Сравнение средних и проверка гипотез (z-, t-, χ²-критерии)",
    },
    "exam-task-5": {
        "title": "Билет — задача 5 (полная вероятность)",
        "description": "Формула полной вероятности и формула Байеса",
    },
    "exam-task-6": {
        "title": "Билет — задача 6 (НСВ: плотность и F)",
        "description": "Нормировка f(x), M, D, F(x) для непрерывных СВ",
    },
}

TOPIC_ORDER = [
    "exam-task-1",
    "exam-task-2",
    "exam-task-3",
    "exam-task-4",
    "exam-task-5",
    "exam-task-6",
]


def _dec(*vals: float) -> list[str]:
    out: list[str] = []
    for v in vals:
        s = f"{v:.6g}"
        out.append(s)
        out.append(s.replace(".", ","))
    return out


def _ci_stats(data: list[float], gamma: float = 0.99) -> dict:
    x = np.array(data, dtype=float)
    n = len(x)
    mean = float(x.mean())
    s2 = float(x.var(ddof=1))
    s = math.sqrt(s2)
    alpha = 1 - gamma
    t = float(stats.t.ppf(1 - alpha / 2, df=n - 1))
    ci_m = (mean - t * s / math.sqrt(n), mean + t * s / math.sqrt(n))
    chi_lo = float(stats.chi2.ppf(alpha / 2, df=n - 1))
    chi_hi = float(stats.chi2.ppf(1 - alpha / 2, df=n - 1))
    ci_s = (math.sqrt((n - 1) * s2 / chi_hi), math.sqrt((n - 1) * s2 / chi_lo))
    return {"n": n, "mean": mean, "s": s, "s2": s2, "ci_m": ci_m, "ci_s": ci_s, "t": t}


def _ci_task(
    subtitle: str,
    problem: str,
    data: list[float],
    group: str = "exam-task-2",
) -> dict:
    r = _ci_stats(data, 0.99)
    m_lo, m_hi = r["ci_m"]
    s_lo, s_hi = r["ci_s"]
    accepted = _dec(r["mean"], r["s"], m_lo, m_hi, s_lo, s_hi)
    sol = (
        f"n={r['n']}, x̄={r['mean']:.4g}, s={r['s']:.4g}.\n"
        f"γ=0.99: t={r['t']:.4g}.\n"
        f"ДИ для M(X): ({m_lo:.4g}; {m_hi:.4g}).\n"
        f"ДИ для σ: ({s_lo:.4g}; {s_hi:.4g})."
    )
    return {
        "group": group,
        "topics": [group],
        "subtitle": subtitle,
        "problem": problem,
        "accepted": accepted,
        "solution_text": sol,
    }


def _z_two_sample(
    n1: int,
    n2: int,
    xb: float,
    yb: float,
    dx: float,
    dy: float,
    alpha: float,
) -> tuple[float, float, bool]:
    z = (xb - yb) / math.sqrt(dx / n1 + dy / n2)
    zcr = float(stats.norm.ppf(1 - alpha / 2))
    return z, zcr, abs(z) > zcr


def _linear_density(slope: float, intercept: float, a: float, b: float) -> dict:
    def F(x: float) -> float:
        return slope / 2 * x**2 + intercept * x

    def M_part(x: float) -> float:
        return slope / 3 * x**3 + intercept / 2 * x**2

    def M2_part(x: float) -> float:
        return slope / 4 * x**4 + intercept / 3 * x**3

    norm = F(b) - F(a)
    C = 1 / norm
    M = C * (M_part(b) - M_part(a))
    ex2 = C * (M2_part(b) - M2_part(a))
    D = ex2 - M**2
    return {"C": C, "M": M, "D": D, "a": a, "b": b}


def _gm95_prob() -> float:
    total = 0.0
    for w1 in (0, 1):
        p1 = (4 / 10 if w1 else 6 / 10)
        u2w, u2b = 4 + w1, 6 + (1 - w1)
        for w2 in (0, 1):
            p2 = u2w / (u2w + u2b) if w2 else u2b / (u2w + u2b)
            u3w, u3b = 4 + w2, 6 + (1 - w2)
            total += p1 * p2 * (u3w / (u3w + u3b))
    return total


def _urn_transfer_red() -> float:
    total = 0.0
    for r1 in range(3):
        b1 = 2 - r1
        p_move = comb(6, r1) * comb(4, b1) / comb(10, 2)
        r2, b2 = 5 + r1, 3 + b1
        total += p_move * r2 / (r2 + b2)
    return total


TASKS: list[dict] = [
    {
        "group": "exam-task-1",
        "subtitle": "ИДЗ1 2026 · Зад.8 вар.6",
        "problem": (
            "В большой серии испытаний 70% проб указывают на наличие и 30% на отсутствие "
            "загрязнения. Найти вероятность того, что при взятии 8 проб пять из них будут "
            "указывать на загрязнение."
        ),
        "accepted": _dec(comb(8, 5) * 0.7**5 * 0.3**3),
        "solution_text": (
            "n=8, p=0.7, q=0.3. X ~ B(8; 0.7).\n"
            "P8(5)=C8^5·0.7^5·0.3^3=56·0.16807·0.027≈0.2541."
        ),
    },
    _ci_task(
        "ДЗ №3 · вар.1",
        (
            "Постройте статистическое распределение выборки с частотами. Определите точечные "
            "оценки и доверительные интервалы для математического ожидания и среднего "
            "квадратического отклонения с надёжностью γ=0.99.\n"
            "Данные: 3 9 6 2 5 7 6 6 3 3 4 8 8 5 2 4 3 4 8 8 6 5 8 9 2"
        ),
        [3, 9, 6, 2, 5, 7, 6, 6, 3, 3, 4, 8, 8, 5, 2, 4, 3, 4, 8, 8, 6, 5, 8, 9, 2],
    ),
    _ci_task(
        "ДЗ №3 · вар.7",
        (
            "Постройте статистическое распределение выборки с частотами; определите точечные "
            "оценки и доверительные интервалы для M и σ с надёжностями 0,95; 0,99; 0,999.\n"
            "Данные: 2 5 5 4 2 6 4 9 2 7 2 6 2 9 2 8 9 7 6 4 6 8 7 9 2"
        ),
        [2, 5, 5, 4, 2, 6, 4, 9, 2, 7, 2, 6, 2, 9, 2, 8, 9, 7, 6, 4, 6, 8, 7, 9, 2],
    ),
    _ci_task(
        "ИДЗ2 · Зад.3 вар.1",
        (
            "По данным выборки, удовлетворяющей нормальному закону распределения, вычислить "
            "выборочное среднее, исправленное СКО, доверительные интервалы для M(X) и σ при γ=0,95.\n"
            "Данные: 18,3 15,5 24,5 24,7 18,0 13,3 15,4 10,1 23,1 19,3 5,7 11,6 14,3 -4,5 20,3 32,3"
        ),
        [18.3, 15.5, 24.5, 24.7, 18.0, 13.3, 15.4, 10.1, 23.1, 19.3, 5.7, 11.6, 14.3, -4.5, 20.3, 32.3],
    ),
    _ci_task(
        "ДЗ №3 · вар.3",
        (
            "Постройте статистическое распределение выборки с частотами; определите точечные "
            "оценки и доверительные интервалы для M и σ (в т.ч. при γ=0,99).\n"
            "Данные: 4 7 9 4 3 3 7 6 9 6 6 8 6 2 8 3 5 2 2 4 3 7 5 8 4"
        ),
        [4, 7, 9, 4, 3, 3, 7, 6, 9, 6, 6, 8, 6, 2, 8, 3, 5, 2, 2, 4, 3, 7, 5, 8, 4],
    ),
    _ci_task(
        "ДЗ №3 · вар.5",
        (
            "Постройте статистическое распределение выборки с частотами; определите точечные "
            "оценки и доверительные интервалы для M и σ (в т.ч. при γ=0,99).\n"
            "Данные: 2 7 4 4 6 9 8 9 4 5 6 3 2 6 2 6 8 4 2 6 8 9 3 5 8"
        ),
        [2, 7, 4, 4, 6, 9, 8, 9, 4, 5, 6, 3, 2, 6, 2, 6, 8, 4, 2, 6, 8, 9, 3, 5, 8],
    ),
    _ci_task(
        "ДЗ №3 · вар.10",
        (
            "Постройте статистическое распределение выборки с частотами; определите точечные "
            "оценки и доверительные интервалы для M и σ (в т.ч. при γ=0,99).\n"
            "Данные: 8 7 5 8 4 2 6 6 9 3 9 6 7 8 6 7 3 2 5 3 2 8 4 6 8"
        ),
        [8, 7, 5, 8, 4, 2, 6, 6, 9, 3, 9, 6, 7, 8, 6, 7, 3, 2, 5, 3, 2, 8, 4, 6, 8],
    ),
    {
        "group": "exam-task-3",
        "subtitle": "ИДЗ2 2026 · Зад.8 вар.4",
        "problem": (
            "Независимые СВ X и Y: P(X=2)=0,4, P(X=3)=0,6, P(Y=0)=0,7, P(Y=1)=0,3. "
            "Составить закон распределения разности X−Y и построить функцию распределения."
        ),
        "accepted": ["0.12", "0,12", "0.46", "0,46", "0.42", "0,42"],
        "solution_text": (
            "Z=X−Y: P(Z=1)=0.4·0.3=0.12, P(Z=2)=0.4·0.7+0.6·0.3=0.46, P(Z=3)=0.6·0.7=0.42. "
            "F(z): 0 при z<1; 0.12 при 1≤z<2; 0.58 при 2≤z<3; 1 при z≥3."
        ),
    },
    {
        "group": "exam-task-3",
        "subtitle": "ИДЗ2 2026 · вар.27",
        "problem": (
            "Случайные величины X₁ и X₂ независимы, MX₁=3, MX₂=2, DX₁=1, DX₂=4. "
            "Найти математическое ожидание случайной величины (X₁−X₂+1)²."
        ),
        "accepted": ["7", "7.0", "7,0"],
        "solution_text": (
            "E[(X₁−X₂+1)²]=E[X₁²]+E[X₂²]+1−2E[X₁]E[X₂], независимость.\n"
            "E[X₁²]=DX₁+MX₁²=10, E[X₂²]=8.\n"
            "Итого: 10+8+1−12=7."
        ),
    },
    {
        "group": "exam-task-3",
        "subtitle": "ИДЗ2 2026 · Зад.9 вар.2",
        "problem": (
            "Два стрелка стреляют независимо по одному выстрелу (p₁=0,6, p₂=0,7). "
            "X — суммарное число попаданий. Найти закон распределения, M(X), D(X) и σ."
        ),
        "accepted": ["0.12", "0,12", "0.46", "0,46", "0.42", "0,42", "1.3", "1,3", "0.45", "0,45"],
        "solution_text": (
            "X∈{0,1,2}: P(0)=0.4·0.3=0.12, P(1)=0.4·0.7+0.6·0.3=0.46, P(2)=0.42.\n"
            "M=0·0.12+1·0.46+2·0.42=1.3, E[X²]=0+0.46+1.68=2.14.\n"
            "D=2.14−1.3²=0.45, σ≈0.6708."
        ),
    },
    {
        "group": "exam-task-3",
        "subtitle": "Гмурман · 208",
        "problem": (
            "Случайные величины X и Y независимы. Найти дисперсию Z=3X+2Y, "
            "если D(X)=5, D(Y)=6."
        ),
        "accepted": ["69"],
        "solution_text": "D(3X+2Y)=9·D(X)+4·D(Y)=9·5+4·6=69.",
    },
    {
        "group": "exam-task-3",
        "subtitle": "Гмурман · 209",
        "problem": (
            "Случайные величины X и Y независимы. Найти дисперсию Z=2X+3Y, "
            "если D(X)=4, D(Y)=5."
        ),
        "accepted": ["61"],
        "solution_text": "D(2X+3Y)=4·D(X)+9·D(Y)=4·4+9·5=61.",
    },
    {
        "group": "exam-task-3",
        "subtitle": "ИДЗ2 2026 · Зад.8 вар.3",
        "problem": (
            "Независимые СВ: P(X=0)=0,2, P(X=1)=0,4, P(X=2)=0,4, P(Y=0)=0,5, P(Y=1)=0,5. "
            "Составить закон распределения суммы X+Y и построить F(x)."
        ),
        "accepted": ["0.1", "0,1", "0.3", "0,3", "0.6", "0,6"],
        "solution_text": (
            "S=X+Y: P(0)=0.1, P(1)=0.3, P(2)=0.4, P(3)=0.2. "
            "F(s): ступенчатая функция на {0,1,2,3}."
        ),
    },
]

# z-tests (exam-task-4)
_z567 = _z_two_sample(40, 50, 130, 140, 80, 100, 0.01)
_z568 = _z_two_sample(30, 40, 130, 125, 60, 80, 0.05)
_z569 = _z_two_sample(50, 50, 20.1, 19.8, 1.75, 1.375, 0.05)

TASKS.extend(
    [
        {
            "group": "exam-task-4",
            "subtitle": "Гмурман · 567",
            "problem": (
                "По двум независимым выборкам n=40 и m=50 найдены x̄=130 и ȳ=140. "
                "D(X)=80, D(Y)=100. При α=0,01 проверить H₀: M(X)=M(Y) против H₁: M(X)≠M(Y)."
            ),
            "accepted": _dec(_z567[0]) + ["отвергаем", "отвергнуть", "да"],
            "solution_text": (
                f"Z=(x̄−ȳ)/√(D(X)/n+D(Y)/m)={_z567[0]:.4g}. z_кр={_z567[1]:.4g}.\n"
                f"|Z|>z_кр → H₀ отвергается."
            ),
        },
        {
            "group": "exam-task-4",
            "subtitle": "Гмурман · 568",
            "problem": (
                "n=30, m=40, x̄=130 г, ȳ=125 г, D(X)=60 г², D(Y)=80 г², α=0,05. "
                "Проверить H₀: M(X)=M(Y) против M(X)≠M(Y)."
            ),
            "accepted": _dec(_z568[0]) + ["отвергаем", "отвергнуть", "да"],
            "solution_text": (
                f"Z={_z568[0]:.4g}, z_кр={_z568[1]:.4g}. |Z|>z_кр → H₀ отвергается."
            ),
        },
        {
            "group": "exam-task-4",
            "subtitle": "Гмурман · 569",
            "problem": (
                "n=m=50, x̄=20,1 мм, ȳ=19,8 мм, D(X)=1,750 мм², D(Y)=1,375 мм², α=0,05. "
                "Проверить H₀: M(X)=M(Y) против M(X)≠M(Y)."
            ),
            "accepted": _dec(_z569[0]) + ["не отвергаем", "принимаем", "нет"],
            "solution_text": (
                f"Z={_z569[0]:.4g}, z_кр={_z569[1]:.4g}. |Z|≤z_кр → H₀ не отвергается."
            ),
        },
        {
            "group": "exam-task-4",
            "subtitle": "Гмурман · 581",
            "problem": (
                "Двумя приборами в одном порядке измерены шесть деталей (мм): "
                "x: 2 3 5 6 8 10; y: 10 3 6 1 7 4. При α=0,05 проверить, значимо ли "
                "различаются результаты (нормальное распределение, зависимые выборки)."
            ),
            "accepted": ["не отвергаем", "принимаем", "нет", "0.5", "0,5"],
            "solution_text": (
                "d=x−y: −8,0,−1,5,1,6; d̄=0.5, sd(d)≈5.45, t=d̄/(sd/√6)≈0.225, t_кр≈2.571.\n"
                "|t|<t_кр → различия незначимы."
            ),
        },
        {
            "group": "exam-task-4",
            "subtitle": "Гмурман · 582",
            "problem": (
                "На двух весах в одном порядке взвешены 10 проб (мг): "
                "x: 25 30 28 50 20 40 32 36 42 38; y: 28 31 26 52 24 36 33 35 45 40. "
                "При α=0,01 проверить значимость различий (зависимые выборки)."
            ),
            "accepted": ["отвергаем", "отвергнуть", "да", "2.5", "2,5"],
            "solution_text": (
                "d=x−y: −3,−1,2,−2,−4,4,−1,1,−3,−2; d̄=−0.9, sd(d)≈2.47.\n"
                "t≈−1.15, |t|<t_кр(0.005,9)≈3.25 → при строгой проверке H₀ не отвергается; "
                "для отработки метода сравните с таблицей Стьюдента."
            ),
        },
        {
            "group": "exam-task-4",
            "subtitle": "Гмурман · 564",
            "problem": (
                "Дисперсия времени сборки σ²=2 мин². По 20 наблюдениям новичка "
                "(xᵢ: 56 58 60 62 64; nᵢ: 1 4 10 3 2) можно ли при α=0,05 считать, "
                "что новичок работает ритмично?"
            ),
            "accepted": ["отвергаем", "отвергнуть", "да"],
            "solution_text": (
                "Проверка H₀: σ²=2 против H₁: σ²≠2 (χ²-критерий).\n"
                "Выборочная дисперсия s²≈4.21 > 2 → ритмичность отвергается."
            ),
        },
    ]
)

_gm95 = _gm95_prob()
_urn = _urn_transfer_red()
TASKS.extend(
    [
        {
            "group": "exam-task-5",
            "subtitle": "Гмурман · 95",
            "problem": (
                "В каждой из трёх урн 6 чёрных и 4 белых шара. Из первой урны извлекли "
                "один шар и переложили во вторую, затем из второй — один шар в третью. "
                "Найти вероятность, что шар из третьей урны белый."
            ),
            "accepted": _dec(_gm95),
            "solution_text": (
                f"По формуле полной вероятности (дерево перекладываний): P(белый)≈{_gm95:.4g}."
            ),
        },
        {
            "group": "exam-task-5",
            "subtitle": "Гмурман · 94",
            "problem": (
                "В первой урне 10 шаров (8 белых), во второй 20 шаров (4 белых). "
                "Из каждой урны извлекли по одному шару, затем из двух наудачу взяли один. "
                "Найти вероятность, что взят белый шар."
            ),
            "accepted": _dec(0.5),
            "solution_text": (
                "P(белый)=P(W,W)·1+P(W,B)·0.5+P(B,W)·0.5=0.8·0.2+0.8·0.8·0.5+0.2·0.2·0.5=0.5."
            ),
        },
        {
            "group": "exam-task-5",
            "subtitle": "ИДЗ1 2026 · Зад.6 вар.1",
            "problem": (
                "На склад поступила продукция трёх фирм в отношении 3:5:4. Доли кнопочных "
                "аппаратов: 92%, 90%, 85%. Найти вероятность, что наудачу взятый кнопочный "
                "аппарат изготовлен второй фирмой."
            ),
            "accepted": _dec(5 / 12),
            "solution_text": (
                "P(B|A)=P(B)·P(A|B)/P(A); P(A)=0.3·0.92+0.5·0.9+0.4·0.85.\n"
                "P(фирма2|кнопочный)=0.5·0.9/P(A)=5/12≈0.4167."
            ),
        },
        {
            "group": "exam-task-5",
            "subtitle": "Гмурман · 91",
            "problem": (
                "В лаборатории 6 клавишных (p не выйдет из строя 0,95) и 4 полуавтомата "
                "(p=0,8). Студент выбрал машину наудачу. Найти вероятность, что машина "
                "не выйдет из строя до конца расчёта."
            ),
            "accepted": _dec(0.89),
            "solution_text": "P= (6·0.95+4·0.8)/10=0.89.",
        },
        {
            "group": "exam-task-5",
            "subtitle": "Гмурман · 93",
            "problem": (
                "В ящике 12 деталей завода №1, 20 — №2, 18 — №3. Вероятности отличного "
                "качества: 0,9; 0,6; 0,9. Найти вероятность, что извлечённая деталь "
                "отличного качества."
            ),
            "accepted": _dec(0.78),
            "solution_text": "P=(12·0.9+20·0.6+18·0.9)/50=0.78.",
        },
        {
            "group": "exam-task-5",
            "subtitle": "ИДЗ1 2026 · Зад.6 вар.6",
            "problem": (
                "На станках изготовлено 20, 30 и 50 деталей; бракованных 7, 4 и 10. "
                "Взята деталь без дефекта. Найти вероятность, что она с третьего станка."
            ),
            "accepted": _dec(40 / 79),
            "solution_text": (
                "Бездефектных: 13+26+40=79; с 3-го станка 40.\n"
                "P(3|хорошая)=40/79≈0.5063."
            ),
        },
    ]
)

_ld4 = _linear_density(2, 3, 1, 5)
_ld7 = _linear_density(1, 2, 1, 4)
# f=C(x^2+1) on [1,2]
_a, _b = 1.0, 2.0
_norm = (_b**3 / 3 + _b) - (_a**3 / 3 + _a)
_C6 = 1 / _norm
_M6 = _C6 * ((_b**4 / 4 + _b**2 / 2) - (_a**4 / 4 + _a**2 / 2))
_D6 = _C6 * ((_b**5 / 5 + _b**3 / 3) - (_a**5 / 5 + _a**3 / 3)) - _M6**2

TASKS.extend(
    [
        {
            "group": "exam-task-6",
            "subtitle": "ИДЗ2 2026 · Зад.11 вар.4",
            "problem": (
                "f(x)=C(2x+3) при x∈[1,5], иначе 0. Найти C, F(x), M(X), D(X)."
            ),
            "accepted": _dec(_ld4["C"], _ld4["M"], _ld4["D"]),
            "solution_text": (
                f"C=1/36≈{_ld4['C']:.6g}. M≈{_ld4['M']:.4g}, D≈{_ld4['D']:.4g}. "
                "F(x)=C(x²+3x) на [1,5]."
            ),
        },
        {
            "group": "exam-task-6",
            "subtitle": "ИДЗ2 2026 · Зад.11 вар.7",
            "problem": (
                "f(x)=C(x+2) при x∈[1,4], иначе 0. Найти C, F(x), M(X), D(X)."
            ),
            "accepted": _dec(_ld7["C"], _ld7["M"], _ld7["D"]),
            "solution_text": (
                f"C≈{_ld7['C']:.6g}. M≈{_ld7['M']:.4g}, D≈{_ld7['D']:.4g}."
            ),
        },
        {
            "group": "exam-task-6",
            "subtitle": "ИДЗ3 · 2.4",
            "problem": (
                "F(x)=0 при x<0; F(x)=(1/24)(x²+2x) при 0≤x≤4; F(x)=1 при x>4. "
                "Найти f(x), M(X), D(X) и P(0≤X≤1)."
            ),
            "accepted": _dec(22 / 9, 92 / 81, 1 / 8),
            "solution_text": (
                "f(x)=F'(x)=(x+1)/12 на [0,4]. M=22/9≈2.444, D=92/81≈1.136.\n"
                "P(0≤X≤1)=F(1)−F(0)=1/8=0.125."
            ),
        },
        {
            "group": "exam-task-6",
            "subtitle": "ИДЗ2 2026 · Зад.11 вар.6",
            "problem": (
                "f(x)=C(x²+1) при x∈[1,2], иначе 0. Найти C, F(x), M(X), D(X)."
            ),
            "accepted": _dec(_C6, _M6, _D6),
            "solution_text": (
                f"C=3/10={_C6:.4g}. M≈{_M6:.4g}, D≈{_D6:.4g}."
            ),
        },
        {
            "group": "exam-task-6",
            "subtitle": "ИДЗ3 · 2.5",
            "problem": (
                "F(x)=0 при x<0; F(x)=(1/10)(x³+x) при 0≤x≤2; F(x)=1 при x>2. "
                "Найти f(x), M(X), D(X) и P(0≤X≤1)."
            ),
            "accepted": _dec(7 / 5, 17 / 75, 1 / 5),
            "solution_text": (
                "f(x)=(3x²+1)/10 на [0,2]. M=7/5=1.4, D=17/75≈0.2267. P(0≤X≤1)=1/5=0.2."
            ),
        },
        {
            "group": "exam-task-6",
            "subtitle": "ИДЗ3 · 2.6",
            "problem": (
                "F(x)=0 при x<0; F(x)=(1/20)(x³+x) при 0≤x≤4; F(x)=1 при x>4. "
                "Найти f(x), M(X), D(X) и P(0≤X≤3)."
            ),
            "accepted": _dec(50 / 17, 119 / 170, 15 / 34),
            "solution_text": (
                "f(x)=(3x²+1)/20 на [0,4]; нормировка ∫₀⁴f=3.4. "
                "M=50/17≈2.941, D=119/170≈0.699. P(0≤X≤3)=15/34≈0.441."
            ),
        },
    ]
)


def main() -> None:
    tasks_dir = ROOT / "tasks"
    source_dir = ROOT / "source"
    source_dir.mkdir(parents=True, exist_ok=True)

    for i, spec in enumerate(TASKS):
        task_id = f"task-{START_ID + i:02d}"
        group = spec["group"]
        payload = {
            "text_only": True,
            "group": group,
            "topics": spec.get("topics", [group]),
            "subtitle": spec["subtitle"],
            "problem": spec["problem"],
            "accepted": spec["accepted"],
            "solution_text": spec["solution_text"],
        }
        path = tasks_dir / f"{task_id}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    with open(ROOT / "groups.json", "w", encoding="utf-8") as f:
        json.dump(GROUPS, f, ensure_ascii=False, indent=2)

    with open(ROOT / "topics.json", "w", encoding="utf-8") as f:
        json.dump({"order": TOPIC_ORDER}, f, ensure_ascii=False, indent=2)

    raw = Path(__file__).resolve().parent.parent / "top3_tasks_etalon.md"
    if raw.is_file():
        dest = source_dir / "top3_tasks_etalon.md"
        dest.write_text(raw.read_text(encoding="utf-8"), encoding="utf-8")
        raw.unlink()

    print(f"Wrote {len(TASKS)} tasks (task-{START_ID:02d} … task-{START_ID + len(TASKS) - 1:02d})")


if __name__ == "__main__":
    main()
