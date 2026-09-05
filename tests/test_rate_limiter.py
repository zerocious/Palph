"""
Тесты UserRateLimiter — sliding-window rate-limit per user.

Использует time.monotonic() внутри сервиса; в тестах мы манипулируем
поведением через `time.sleep` (для real-time проверок) и через прямое
тыкание во внутренние deque'и (для проверки логики окна без ожидания).
"""
import time

import pytest

from services import UserRateLimiter


class TestBasicLimiting:
    def test_first_action_is_ok(self):
        rl = UserRateLimiter(max_actions=5, window_seconds=60, warn_threshold=0.8)
        assert rl.check(user_id=1) == "ok"

    def test_under_threshold_all_ok(self):
        rl = UserRateLimiter(max_actions=10, window_seconds=60, warn_threshold=0.8)
        for _ in range(7):
            assert rl.check(user_id=1) == "ok"

    def test_over_hard_limit_blocked(self):
        rl = UserRateLimiter(max_actions=5, window_seconds=60, warn_threshold=1.0)
        # First 5 OK (threshold = 1.0 disables warn)
        for _ in range(5):
            assert rl.check(user_id=1) == "ok"
        # 6th → block
        assert rl.check(user_id=1) == "block"
        # And subsequent — also blocked
        assert rl.check(user_id=1) == "block"


class TestWarning:
    def test_warn_triggers_at_threshold(self):
        rl = UserRateLimiter(
            max_actions=10, window_seconds=60,
            warn_threshold=0.7, warn_cooldown_seconds=0,
        )
        # First 6 below 70% → OK (7th = 70% triggers warn)
        for _ in range(6):
            assert rl.check(user_id=1) == "ok"
        # 7th request: current_count was 6, current_count+1=7, 70% of 10 = 7 ⇒ warn
        assert rl.check(user_id=1) == "warn"

    def test_warn_cooldown_prevents_spam(self):
        """После warn в течение cooldown_seconds последующие проверки в warn-зоне → ok."""
        rl = UserRateLimiter(
            max_actions=10, window_seconds=60,
            warn_threshold=0.5, warn_cooldown_seconds=60,
        )
        for _ in range(4):
            rl.check(user_id=1)  # 4 ok
        # 5-я попытка должна быть warn (50% threshold)
        assert rl.check(user_id=1) == "warn"
        # 6-я — в warn-зоне, но cooldown ещё активен → ok
        assert rl.check(user_id=1) == "ok"


def _backdate(rl, seconds):
    """
    Сдвигает всё состояние лимитера на `seconds` в прошлое.

    Второй приём из докстринга модуля — прямое тыкание во внутренности
    вместо time.sleep. Для sweep-тестов это принципиально: иначе каждый
    из них стоил бы секунду реального ожидания.
    """
    for bucket in rl._buckets.values():
        for i in range(len(bucket)):
            bucket[i] -= seconds
    for uid in rl._warned_at:
        rl._warned_at[uid] -= seconds
    rl._last_sweep -= seconds


class TestBucketSweep:
    """
    Уборка протухших бакетов. Без неё _buckets/_warned_at растут монотонно:
    запись заводится на каждый user_id, прошедший через middleware, и живёт
    до рестарта бота (~864 байта на пользователя).
    """

    def test_expired_buckets_are_evicted(self):
        rl = UserRateLimiter(max_actions=5, window_seconds=60, warn_threshold=1.0)
        for uid in range(100):
            rl.check(user_id=uid)
        assert len(rl._buckets) == 100

        _backdate(rl, 120)  # всё окно истекло
        rl.check(user_id=999)  # любой check после окна запускает уборку

        # Остался только тот, кто активен прямо сейчас
        assert len(rl._buckets) == 1
        assert 999 in rl._buckets

    def test_active_user_survives_sweep(self):
        rl = UserRateLimiter(max_actions=5, window_seconds=60, warn_threshold=1.0)
        rl.check(user_id=1)
        _backdate(rl, 120)
        rl.check(user_id=1)  # снова активен — уборка не должна его тронуть
        assert 1 in rl._buckets

    def test_sweep_preserves_warn_cooldown(self):
        """
        Ключевая семантика: бакет протухает через окно (60с), а cooldown
        длится 3600с. Если уборка выметет _warned_at вместе с бакетом,
        пользователь получит повторный warn раньше срока.
        """
        rl = UserRateLimiter(
            max_actions=4, window_seconds=60,
            warn_threshold=0.5, warn_cooldown_seconds=3600,
        )
        assert rl.check(user_id=1) == "ok"
        assert rl.check(user_id=1) == "warn"  # warn_at = int(4 * 0.5) = 2

        _backdate(rl, 120)  # окно истекло, cooldown — нет
        rl.check(user_id=2)  # триггерим уборку чужим запросом

        assert 1 in rl._warned_at, "cooldown-состояние вымыто уборкой"
        # И повторного warn пользователь не получает — cooldown ещё идёт
        rl.check(user_id=1)
        assert rl.check(user_id=1) == "ok"

    def test_sweep_does_not_change_check_semantics(self):
        """После уборки вернувшийся пользователь считается с нуля, как и должен."""
        rl = UserRateLimiter(max_actions=3, window_seconds=60, warn_threshold=1.0)
        for _ in range(3):
            rl.check(user_id=1)
        assert rl.check(user_id=1) == "block"

        _backdate(rl, 120)
        rl.check(user_id=2)  # уборка
        assert 1 not in rl._buckets

        # Возврат: окно чистое, снова 3 попытки до блока
        for _ in range(3):
            assert rl.check(user_id=1) == "ok"
        assert rl.check(user_id=1) == "block"


class TestUserIsolation:
    def test_separate_users_independent(self):
        rl = UserRateLimiter(max_actions=3, window_seconds=60, warn_threshold=1.0)
        for _ in range(3):
            assert rl.check(user_id=1) == "ok"
        assert rl.check(user_id=1) == "block"
        # Второй пользователь не затронут
        for _ in range(3):
            assert rl.check(user_id=2) == "ok"

    def test_reset_only_for_target_user(self):
        rl = UserRateLimiter(max_actions=3, window_seconds=60, warn_threshold=1.0)
        for _ in range(3):
            rl.check(user_id=1)
            rl.check(user_id=2)
        # Both blocked
        assert rl.check(user_id=1) == "block"
        assert rl.check(user_id=2) == "block"
        # Reset user 1
        rl.reset(user_id=1)
        # User 1 — clean; user 2 — still blocked
        assert rl.check(user_id=1) == "ok"
        assert rl.check(user_id=2) == "block"


class TestSlidingWindow:
    def test_old_timestamps_expire(self):
        """Если window 1 секунда — через >1 сек бакет очищается."""
        rl = UserRateLimiter(max_actions=3, window_seconds=1, warn_threshold=1.0)
        for _ in range(3):
            rl.check(user_id=1)
        assert rl.check(user_id=1) == "block"
        # Ждём чтобы окно прошло
        time.sleep(1.1)
        # После очистки протухших — снова OK
        assert rl.check(user_id=1) == "ok"


class TestEdgeCases:
    def test_zero_max_actions_always_blocks(self):
        """Пограничный кейс: max_actions=0 → всё блокируется."""
        rl = UserRateLimiter(max_actions=0, window_seconds=60, warn_threshold=1.0)
        # 0 ≥ 0 → block немедленно
        assert rl.check(user_id=1) == "block"

    def test_high_threshold_disables_warning(self):
        """warn_threshold=1.0 (или выше) → warn никогда не триггерится."""
        rl = UserRateLimiter(max_actions=5, window_seconds=60, warn_threshold=1.0)
        for _ in range(5):
            assert rl.check(user_id=1) == "ok"
        # Никаких "warn" в этой последовательности
        assert rl.check(user_id=1) == "block"

    def test_unknown_user_id_works_fresh(self):
        """check(user_id=N) для нового пользователя возвращает ok."""
        rl = UserRateLimiter()
        assert rl.check(user_id=999_999) == "ok"
        assert rl.check(user_id=10_000_000) == "ok"

    def test_reset_unknown_user_no_error(self):
        """reset() на несуществующего user_id — silent no-op."""
        rl = UserRateLimiter()
        rl.reset(user_id=42)  # should not raise
        assert rl.check(user_id=42) == "ok"


class TestDefaultsAreReasonable:
    def test_default_thresholds(self):
        """Поведение с дефолтами: 30 actions / 60s, warn at 70%."""
        rl = UserRateLimiter()
        assert rl.max_actions == 30
        assert rl.window_seconds == 60
        assert rl.warn_threshold == 0.7
        assert rl.warn_cooldown_seconds == 30

    def test_default_first_20_actions_all_ok(self):
        """С дефолтами (max=30, threshold=0.7): первые 20 запросов под warn_at=21 — все OK."""
        rl = UserRateLimiter()
        for i in range(20):
            assert rl.check(user_id=1) == "ok", f"action {i+1} unexpectedly not ok"
        # 21-й — попадает в warn-зону (warn_at = int(30 * 0.7) = 21)
        assert rl.check(user_id=1) == "warn"

    def test_spam_50_actions_gets_blocked(self):
        """С дефолтами: 50 actions подряд → warn → ok → block."""
        rl = UserRateLimiter()
        outcomes = [rl.check(user_id=1) for _ in range(50)]
        assert outcomes.count("block") > 0, "50 actions should produce at least some blocks"
        assert outcomes.count("warn") >= 1, "should produce at least one warn"
