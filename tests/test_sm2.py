"""
Тесты для чистой функции SM-2 (SuperMemo-2).

Алгоритм public-domain (Wozniak 1985), реализация в services.sm2_update.
Тесты проверяют каждое ребро формулы.
"""
import pytest

from services import sm2_update, EF_FLOOR


class TestStandardProgression:
    """Стандартный успешный путь: новая карта → 1 день → 6 дней → 16 дней..."""

    def test_first_perfect_review(self):
        """q=5 на новой карте: reps→1, interval=1, EF +0.1."""
        interval, reps, ef = sm2_update(quality=5, repetitions=0, ease_factor=2.5, interval_days=0)
        assert interval == 1
        assert reps == 1
        assert ef == pytest.approx(2.6)

    def test_second_successful_review(self):
        """Вторая подряд успешная: интервал прыгает на 6 дней."""
        interval, reps, ef = sm2_update(quality=5, repetitions=1, ease_factor=2.6, interval_days=1)
        assert interval == 6
        assert reps == 2
        assert ef == pytest.approx(2.7)

    def test_third_review_uses_interval_times_ef(self):
        """Третья+ повтор: новый интервал = round(prev_interval * EF)."""
        interval, reps, ef = sm2_update(quality=5, repetitions=2, ease_factor=2.7, interval_days=6)
        assert interval == round(6 * 2.7)
        assert reps == 3
        assert ef > 2.7  # q=5 поднимает EF


class TestFailurePath:
    """quality < 3 — сброс прогресса карты."""

    def test_quality_1_resets_reps_and_interval(self):
        interval, reps, ef = sm2_update(quality=1, repetitions=4, ease_factor=2.5, interval_days=20)
        assert interval == 1
        assert reps == 0

    def test_quality_0_also_resets(self):
        interval, reps, ef = sm2_update(quality=0, repetitions=3, ease_factor=2.5, interval_days=10)
        assert interval == 1
        assert reps == 0

    def test_quality_2_still_resets(self):
        """q=2 ниже порога 3 → тоже сброс."""
        interval, reps, _ = sm2_update(quality=2, repetitions=5, ease_factor=2.5, interval_days=30)
        assert interval == 1
        assert reps == 0


class TestEaseFactorMovement:
    """EF — единственное «long-term memory» карты; меняется на каждом ревью."""

    def test_q5_raises_ef_by_0_1(self):
        _, _, ef = sm2_update(quality=5, repetitions=0, ease_factor=2.5, interval_days=0)
        assert ef == pytest.approx(2.6)

    def test_q4_leaves_ef_unchanged(self):
        """q=4 — «balance point»: EF не меняется."""
        _, _, ef = sm2_update(quality=4, repetitions=5, ease_factor=2.5, interval_days=20)
        assert ef == pytest.approx(2.5)

    def test_q3_hard_correct_drops_ef_by_0_14(self):
        """q=3 — правильно, но с усилием; EF падает."""
        _, _, ef = sm2_update(quality=3, repetitions=0, ease_factor=2.5, interval_days=0)
        assert ef == pytest.approx(2.36)

    def test_q1_failure_drops_ef_by_0_54(self):
        _, _, ef = sm2_update(quality=1, repetitions=3, ease_factor=2.5, interval_days=15)
        assert ef == pytest.approx(1.96)


class TestEaseFactorFloor:
    """EF не может упасть ниже EF_FLOOR=1.3."""

    def test_ef_clamped_to_floor(self):
        # Сырой EF: 1.5 + 0.1 - 4*(0.08 + 4*0.02) = 1.5 - 0.54 = 0.96 → клампится до 1.3
        _, _, ef = sm2_update(quality=1, repetitions=0, ease_factor=1.5, interval_days=1)
        assert ef == pytest.approx(EF_FLOOR)

    def test_repeated_failures_dont_go_negative(self):
        """Безопасность: EF никогда не отрицательный, не nan, не inf."""
        ef = 2.5
        for _ in range(20):
            _, _, ef = sm2_update(quality=0, repetitions=0, ease_factor=ef, interval_days=0)
        assert ef >= EF_FLOOR
        assert ef == pytest.approx(EF_FLOOR)

    def test_q5_recovers_ef_from_floor(self):
        """Из пола EF q=5 поднимает обратно (нет «застревания» на дне)."""
        _, _, ef = sm2_update(quality=5, repetitions=0, ease_factor=EF_FLOOR, interval_days=0)
        assert ef > EF_FLOOR
        assert ef == pytest.approx(EF_FLOOR + 0.1)


class TestPropertyBased:
    """Свойства алгоритма, которые должны держаться при любых входах."""

    @pytest.mark.parametrize("q", [0, 1, 2, 3, 4, 5])
    def test_ef_never_below_floor(self, q):
        _, _, ef = sm2_update(quality=q, repetitions=10, ease_factor=EF_FLOOR, interval_days=30)
        assert ef >= EF_FLOOR

    @pytest.mark.parametrize("q", [3, 4, 5])
    def test_successful_review_increments_reps(self, q):
        _, reps, _ = sm2_update(quality=q, repetitions=5, ease_factor=2.5, interval_days=20)
        assert reps == 6

    @pytest.mark.parametrize("q", [0, 1, 2])
    def test_failed_review_resets_reps_to_zero(self, q):
        _, reps, _ = sm2_update(quality=q, repetitions=5, ease_factor=2.5, interval_days=20)
        assert reps == 0

    def test_function_is_pure(self):
        """Чистая функция: один и тот же вход → один и тот же выход."""
        args = (5, 2, 2.7, 6)
        result1 = sm2_update(*args)
        result2 = sm2_update(*args)
        assert result1 == result2
