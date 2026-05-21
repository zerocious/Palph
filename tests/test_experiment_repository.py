"""
Тесты ExperimentRepository + compute_variant + get_variant.

Покрываем:
- Pure compute_variant: детерминизм, равномерность распределения.
- Repo round-trip: get → None при miss, record → True/False idempotency.
- get_variant high-level: cache miss → assign + event, cache hit → no INSERT.
- Multi-experiment isolation, unknown name error.

Архитектура: pure SHA256-based assignment — нет рандомности и нет
race condition; repo INSERT OR IGNORE + cache-aside делает функцию
safe к concurrent вызовам.
"""
import pytest
import pytest_asyncio

from repository import EventRepository, ExperimentRepository
from services import EXPERIMENTS, compute_variant, get_variant


@pytest_asyncio.fixture
async def exp_repo(db):
    return ExperimentRepository(db)


@pytest_asyncio.fixture
async def event_repo(db):
    return EventRepository(db)


class TestComputeVariantPure:
    def test_same_input_same_variant(self):
        a = compute_variant(42, "exp1", ["control", "treatment"])
        b = compute_variant(42, "exp1", ["control", "treatment"])
        assert a == b

    def test_different_users_can_get_different(self):
        """Не строгое утверждение, но при двух наборах из 100 юзеров
        мы должны увидеть оба варианта хоть раз."""
        variants_seen = {
            compute_variant(uid, "exp1", ["a", "b"]) for uid in range(100)
        }
        assert variants_seen == {"a", "b"}

    def test_different_experiments_independent(self):
        """Один user_id, разные experiment_name → assignment'ы независимы."""
        # Не должно быть систематической корреляции; проверяем что хоть
        # для какого-то юзера два эксперимента дают разные варианты.
        mismatches = 0
        for uid in range(100):
            v1 = compute_variant(uid, "exp1", ["a", "b"])
            v2 = compute_variant(uid, "exp2", ["a", "b"])
            if v1 != v2:
                mismatches += 1
        # При независимых хэшах ожидаем ~50/100.
        assert 25 < mismatches < 75

    def test_distribution_50_50_within_tolerance(self):
        """1000 синтетических user_id → 50/50 ± 5%."""
        counts = {"a": 0, "b": 0}
        for uid in range(1000):
            counts[compute_variant(uid, "split_test", ["a", "b"])] += 1
        # |a - 500| < 50 → широкий доверительный коридор
        assert abs(counts["a"] - 500) < 50
        assert counts["a"] + counts["b"] == 1000

    def test_three_way_split(self):
        """Три варианта → каждый в 33% ± tolerance на 1000 user'ах."""
        counts = {"a": 0, "b": 0, "c": 0}
        for uid in range(3000):
            counts[compute_variant(uid, "three", ["a", "b", "c"])] += 1
        for v in counts.values():
            assert 900 < v < 1100  # 1000 ± 10%

    def test_empty_variants_raises(self):
        with pytest.raises(ValueError):
            compute_variant(1, "exp", [])

    def test_empty_name_raises(self):
        with pytest.raises(ValueError):
            compute_variant(1, "", ["a"])

    def test_negative_user_id_works(self):
        """user_id может быть отрицательным (групповые чаты в Telegram)."""
        v = compute_variant(-12345, "exp", ["a", "b"])
        assert v in {"a", "b"}


class TestRepositoryRoundTrip:
    async def test_get_missing_returns_none(self, exp_repo, created_user):
        assert await exp_repo.get_assignment(created_user, "exp1") is None

    async def test_record_then_get(self, exp_repo, created_user):
        await exp_repo.record_assignment(created_user, "exp1", "treatment")
        v = await exp_repo.get_assignment(created_user, "exp1")
        assert v == "treatment"

    async def test_record_returns_true_on_insert(self, exp_repo, created_user):
        assert await exp_repo.record_assignment(created_user, "exp1", "a")

    async def test_record_returns_false_on_duplicate(self, exp_repo, created_user):
        await exp_repo.record_assignment(created_user, "exp1", "a")
        assert not await exp_repo.record_assignment(created_user, "exp1", "a")

    async def test_record_duplicate_doesnt_overwrite(self, exp_repo, created_user):
        """Идемпотентность: повторный record с другим variant не должен
        переписывать существующее назначение."""
        await exp_repo.record_assignment(created_user, "exp1", "control")
        await exp_repo.record_assignment(created_user, "exp1", "treatment")
        v = await exp_repo.get_assignment(created_user, "exp1")
        assert v == "control"

    async def test_count_by_variant(self, exp_repo, user_repo):
        for uid in [101, 102, 103, 104]:
            await user_repo.create_user(uid)
        await exp_repo.record_assignment(101, "split", "a")
        await exp_repo.record_assignment(102, "split", "a")
        await exp_repo.record_assignment(103, "split", "b")
        await exp_repo.record_assignment(104, "other", "a")
        counts = await exp_repo.count_by_variant("split")
        assert counts == {"a": 2, "b": 1}


class TestGetVariantHighLevel:
    async def test_assigns_on_first_call(self, exp_repo, event_repo, created_user, monkeypatch):
        monkeypatch.setitem(EXPERIMENTS, "test_exp", ["control", "treatment"])
        v = await get_variant(exp_repo, created_user, "test_exp", event_repo)
        assert v in {"control", "treatment"}
        # Persisted in БД
        cached = await exp_repo.get_assignment(created_user, "test_exp")
        assert cached == v

    async def test_idempotent_across_calls(self, exp_repo, created_user, monkeypatch):
        monkeypatch.setitem(EXPERIMENTS, "test_exp", ["a", "b"])
        v1 = await get_variant(exp_repo, created_user, "test_exp")
        v2 = await get_variant(exp_repo, created_user, "test_exp")
        v3 = await get_variant(exp_repo, created_user, "test_exp")
        assert v1 == v2 == v3

    async def test_event_logged_on_first_assignment(
        self, exp_repo, event_repo, created_user, db, monkeypatch
    ):
        monkeypatch.setitem(EXPERIMENTS, "tracked_exp", ["a", "b"])
        await get_variant(exp_repo, created_user, "tracked_exp", event_repo)
        async with db.execute(
            "SELECT event_name, properties FROM events "
            "WHERE event_name='experiment.assigned'"
        ) as c:
            rows = await c.fetchall()
        assert len(rows) == 1
        import json
        props = json.loads(rows[0]["properties"])
        assert props["experiment"] == "tracked_exp"
        assert props["variant"] in {"a", "b"}

    async def test_event_not_logged_on_cache_hit(
        self, exp_repo, event_repo, created_user, db, monkeypatch
    ):
        monkeypatch.setitem(EXPERIMENTS, "tracked_exp", ["a", "b"])
        await get_variant(exp_repo, created_user, "tracked_exp", event_repo)
        await get_variant(exp_repo, created_user, "tracked_exp", event_repo)
        async with db.execute(
            "SELECT COUNT(*) FROM events WHERE event_name='experiment.assigned'"
        ) as c:
            row = await c.fetchone()
        assert row[0] == 1

    async def test_unknown_experiment_raises(self, exp_repo, created_user):
        with pytest.raises(KeyError):
            await get_variant(exp_repo, created_user, "not_registered")

    async def test_event_logging_optional(self, exp_repo, created_user, monkeypatch):
        """event_repo=None — get_variant работает, просто не логирует."""
        monkeypatch.setitem(EXPERIMENTS, "test_exp", ["a", "b"])
        v = await get_variant(exp_repo, created_user, "test_exp", event_repo=None)
        assert v in {"a", "b"}

    async def test_matches_compute_variant(self, exp_repo, created_user, monkeypatch):
        """Result от get_variant должен совпадать с pure compute_variant."""
        monkeypatch.setitem(EXPERIMENTS, "match_test", ["x", "y"])
        v = await get_variant(exp_repo, created_user, "match_test")
        expected = compute_variant(created_user, "match_test", ["x", "y"])
        assert v == expected


class TestNoopExperiment:
    """Sentinel experiment '_noop_v1' существует в EXPERIMENTS — smoke."""

    async def test_noop_always_control(self, exp_repo, user_repo):
        for uid in [1, 2, 3, 42, 999]:
            await user_repo.create_user(uid)
            v = await get_variant(exp_repo, uid, "_noop_v1")
            assert v == "control"
