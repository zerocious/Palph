"""
Тесты для `services.wilson_interval` — PA-roadmap #7.

Покрываем три блока:
1. Closed-form sanity: known values from references / textbook.
2. Boundary cases: n=0, k=0, k=n, n=1.
3. Properties: monotone narrowing with n; wider z → wider CI; clamp [0,1].

Pure function, нет async, нет фикстур.
"""
import math

import pytest

from services import wilson_interval


# Reference values cross-checked against R `prop.test(k, n, correct=FALSE)`
# and Wikipedia "Binomial proportion confidence interval > Wilson score".
# Tolerance 1e-3 — Wilson — closed-form, должен совпадать с точностью до
# floating-point дрожания.
_TOL = 1e-3


class TestKnownValues:
    def test_50_of_100_z_1_96(self):
        """p̂=0.5, n=100, 95% — каноничный пример."""
        low, high = wilson_interval(50, 100)
        assert math.isclose(low, 0.404, abs_tol=_TOL)
        assert math.isclose(high, 0.596, abs_tol=_TOL)

    def test_5_of_10_z_1_96(self):
        """Маленькая выборка — широкий CI."""
        low, high = wilson_interval(5, 10)
        # Wilson 95% для k=5/n=10: ~[0.237, 0.763]
        assert math.isclose(low, 0.237, abs_tol=_TOL)
        assert math.isclose(high, 0.763, abs_tol=_TOL)

    def test_90_of_100_z_1_96(self):
        """p̂=0.9 — асимметричный CI (нормальное приближение тут падает)."""
        low, high = wilson_interval(90, 100)
        # Wilson 95% для k=90/n=100: ~[0.825, 0.945]
        assert math.isclose(low, 0.825, abs_tol=_TOL)
        assert math.isclose(high, 0.945, abs_tol=_TOL)


class TestBoundaries:
    def test_n_zero_returns_none(self):
        assert wilson_interval(0, 0) == (None, None)

    def test_negative_n_returns_none(self):
        assert wilson_interval(0, -5) == (None, None)

    def test_zero_successes_low_is_zero(self):
        """k=0 → p̂=0 → lower bound должен зажаться к 0, upper>0."""
        low, high = wilson_interval(0, 20)
        assert low == 0.0  # exact clamp
        assert 0.0 < high < 1.0

    def test_full_successes_high_is_one(self):
        """k=n → p̂=1 → upper bound зажат к 1, lower<1."""
        low, high = wilson_interval(20, 20)
        assert high == 1.0
        assert 0.0 < low < 1.0

    def test_n_equals_one_k_zero(self):
        """n=1, k=0 — extreme small sample, должен вернуть valid (0, high)."""
        low, high = wilson_interval(0, 1)
        assert low == 0.0
        assert 0.0 < high < 1.0

    def test_n_equals_one_k_one(self):
        """n=1, k=1 — extreme, должен вернуть (low, 1) с low > 0."""
        low, high = wilson_interval(1, 1)
        assert high == 1.0
        assert 0.0 < low < 1.0

    def test_successes_exceeding_n_raises(self):
        with pytest.raises(ValueError):
            wilson_interval(11, 10)

    def test_negative_successes_raises(self):
        with pytest.raises(ValueError):
            wilson_interval(-1, 10)


class TestMonotonicity:
    def test_wider_n_narrower_ci(self):
        """Тот же p̂=0.5 при большем n — CI должен сужаться."""
        for (n_small, n_big) in [(10, 100), (100, 1000), (50, 500)]:
            l_s, h_s = wilson_interval(n_small // 2, n_small)
            l_b, h_b = wilson_interval(n_big // 2, n_big)
            width_s = h_s - l_s
            width_b = h_b - l_b
            assert width_b < width_s, (
                f"width @ n={n_big} ({width_b:.4f}) "
                f"должен быть < width @ n={n_small} ({width_s:.4f})"
            )

    def test_higher_z_wider_ci(self):
        """z=2.576 (99%) даёт CI шире z=1.96 (95%)."""
        low_95, high_95 = wilson_interval(50, 100, z=1.96)
        low_99, high_99 = wilson_interval(50, 100, z=2.576)
        assert low_99 < low_95
        assert high_99 > high_95

    def test_lower_z_narrower_ci(self):
        """z=1.645 (90%) уже z=1.96 (95%)."""
        low_90, high_90 = wilson_interval(50, 100, z=1.645)
        low_95, high_95 = wilson_interval(50, 100, z=1.96)
        assert low_90 > low_95
        assert high_90 < high_95


class TestProperties:
    def test_interval_contains_point_estimate(self):
        """p̂ ∈ [low, high] для не-крайних p̂."""
        for k, n in [(50, 100), (25, 100), (75, 100), (3, 10), (7, 10)]:
            low, high = wilson_interval(k, n)
            p_hat = k / n
            assert low <= p_hat <= high, (
                f"p̂={p_hat} not in [{low}, {high}] для k={k} n={n}"
            )

    def test_output_within_unit_interval(self):
        """Любой [low, high] ⊆ [0, 1]."""
        for k, n in [(0, 5), (5, 5), (1, 1000), (999, 1000), (3, 7)]:
            low, high = wilson_interval(k, n)
            assert 0.0 <= low <= high <= 1.0
