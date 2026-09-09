"""
Ядро синхронизации (DESKTOP.md §4, фаза 0).

Главное свойство: реплей детерминирован. Результат зависит от МНОЖЕСТВА
событий, но не от порядка их поступления — иначе две реплики, получившие
одни и те же события разными путями, разошлись бы, и у пользователя
«пропали бы монеты».

Спека требует именно property-based проверку перестановок, а не пример:
hypothesis в зависимостях нет, поэтому перестановки генерируются
random.Random с фиксированными сидами — воспроизводимо и без новой
зависимости.
"""
from __future__ import annotations

import os
import random

import pytest

os.environ.setdefault("BOT_TOKEN", "test-token-for-pytest-imports")

from sync import (  # noqa: E402
    EVENT_KINDS,
    MergeClass,
    SyncEvent,
    UnknownEventKind,
    replay,
    total_order,
)

DEVICES = ("desktop-a", "phone-b", "bot")


def _ev(uuid, kind, payload=None, *, at="2026-05-18T10:00:00Z", lamport=1, device="desktop-a"):
    return SyncEvent(
        event_uuid=uuid, user_id=1, kind=kind, payload=payload or {},
        occurred_at=at, lamport=lamport, device_id=device,
    )


def _random_log(rng: random.Random, n: int) -> list[SyncEvent]:
    """Случайный, но правдоподобный журнал одного пользователя."""
    events = []
    for i in range(n):
        at = f"2026-05-{18 + i % 5:02d}T{i % 24:02d}:{i % 60:02d}:00Z"
        common = dict(at=at, lamport=rng.randint(1, 5), device=rng.choice(DEVICES))
        kind = rng.choice([
            "session.finished", "coins.granted", "flashcard.reviewed",
            "mcq.answered", "task.attempted", "quiz.answered",
            "subject.visited", "tip.seen", "purchase.made",
            "timezone.changed", "pet.renamed", "flashcard.created",
        ])
        if kind == "session.finished":
            p = {"duration_minutes": rng.choice([15, 25, 50]), "local_date": at[:10]}
        elif kind == "coins.granted":
            p = {"delta": rng.randint(1, 40), "reason": "session"}
        elif kind == "flashcard.reviewed":
            p = {"card_hash": f"c{rng.randint(1, 6)}", "quality": rng.randint(0, 5),
                 "local_date": at[:10]}
        elif kind == "mcq.answered":
            p = {"question_hash": f"q{rng.randint(1, 4)}", "correct": rng.random() > 0.4}
        elif kind == "task.attempted":
            p = {"task_id": f"t{rng.randint(1, 4)}", "succeeded": rng.random() > 0.5}
        elif kind == "quiz.answered":
            p = {"term_hash": f"z{rng.randint(1, 3)}", "is_correct": rng.random() > 0.3}
        elif kind == "subject.visited":
            p = {"subject_id": rng.choice(["math", "english"])}
        elif kind == "tip.seen":
            p = {"tip_id": f"tip{rng.randint(1, 5)}"}
        elif kind == "purchase.made":
            p = {"item_type": "color", "item_value": rng.choice(["blue", "pink"]),
                 "price": rng.choice([50, 120, 300])}
        elif kind == "timezone.changed":
            p = {"timezone": rng.choice(["Europe/Moscow", "Asia/Tokyo"])}
        elif kind == "pet.renamed":
            p = {"pet_name": f"имя{rng.randint(1, 3)}"}
        else:
            p = {"card_id": f"uc{rng.randint(1, 5)}"}
        events.append(_ev(f"e{i}", kind, p, **common))
    return events


class TestConvergence:
    """Ядро фазы 0: перестановка входа не меняет результат."""

    @pytest.mark.parametrize("seed", range(12))
    def test_replay_is_permutation_invariant(self, seed):
        rng = random.Random(seed)
        log = _random_log(rng, 60)
        expected = replay(log).snapshot()

        for _ in range(8):
            shuffled = log[:]
            rng.shuffle(shuffled)
            assert replay(shuffled).snapshot() == expected

    @pytest.mark.parametrize("seed", range(6))
    def test_split_delivery_converges(self, seed):
        """
        Две реплики получают журнал разными кусками и в разном порядке —
        как оно и происходит при push/pull.
        """
        rng = random.Random(1000 + seed)
        log = _random_log(rng, 40)
        whole = replay(log).snapshot()

        half = len(log) // 2
        replica_a = log[:half] + log[half:]
        replica_b = log[half:] + log[:half]
        rng.shuffle(replica_a)
        assert replay(replica_a).snapshot() == whole
        assert replay(replica_b).snapshot() == whole

    def test_duplicate_delivery_is_idempotent(self):
        """Событие приходит и из outbox'а, и из pull'а — засчитаться должно раз."""
        log = [
            _ev("a", "coins.granted", {"delta": 25}),
            _ev("b", "coins.granted", {"delta": 10}, lamport=2),
        ]
        assert replay(log).coins == 35
        assert replay(log + log).coins == 35


class TestTotalOrder:
    def test_tie_broken_by_lamport_then_device(self):
        same_time = "2026-05-18T10:00:00Z"
        e1 = _ev("x", "tip.seen", {"tip_id": "a"}, at=same_time, lamport=2, device="aaa")
        e2 = _ev("y", "tip.seen", {"tip_id": "b"}, at=same_time, lamport=1, device="zzz")
        e3 = _ev("z", "tip.seen", {"tip_id": "c"}, at=same_time, lamport=2, device="aab")
        assert [e.event_uuid for e in total_order([e1, e2, e3])] == ["y", "x", "z"]


class TestCoinArbitration:
    def test_two_offline_purchases_exceeding_balance(self):
        """
        150 монет, два устройства офлайн купили по 120. Локально обе
        валидны, вместе — минус 90. Вторая по тотальному порядку
        отклоняется, и отклоняется ОДНА И ТА ЖЕ при любом порядке прихода.
        """
        log = [
            _ev("grant", "coins.granted", {"delta": 150}, at="2026-05-18T09:00:00Z"),
            _ev("buy1", "purchase.made", {"item_type": "color", "item_value": "blue",
                                          "price": 120}, at="2026-05-18T10:00:00Z"),
            _ev("buy2", "purchase.made", {"item_type": "color", "item_value": "pink",
                                          "price": 120}, at="2026-05-18T11:00:00Z",
                device="phone-b"),
        ]
        state = replay(log)
        assert state.coins == 30
        assert state.inventory == {("color", "blue")}
        assert state.rejected_purchases == {"buy2"}

        for _ in range(10):
            shuffled = log[:]
            random.Random(7).shuffle(shuffled)
            assert replay(shuffled).rejected_purchases == {"buy2"}

    def test_server_rejection_overrides_local_balance(self):
        """
        Решение сервера приоритетнее локального расчёта: иначе клиент
        засчитал бы покупку, которую сервер уже отверг.
        """
        log = [
            _ev("grant", "coins.granted", {"delta": 500}, at="2026-05-18T09:00:00Z"),
            _ev("buy", "purchase.made", {"item_type": "color", "item_value": "blue",
                                         "price": 100}, at="2026-05-18T10:00:00Z"),
            _ev("rej", "purchase.rejected", {"ref_uuid": "buy", "reason": "insufficient"},
                at="2026-05-18T12:00:00Z", device="bot"),
        ]
        state = replay(log)
        assert state.inventory == set()
        assert state.coins == 500       # деньги не списаны
        assert "buy" in state.rejected_purchases

    def test_freeze_purchase_also_arbitrated(self):
        log = [
            _ev("g", "coins.granted", {"delta": 100}, at="2026-05-18T09:00:00Z"),
            _ev("f1", "streak.freeze_purchased", {"price": 500}, at="2026-05-18T10:00:00Z"),
        ]
        state = replay(log)
        assert state.freezes_available == 0
        assert state.coins == 100
        assert "f1" in state.rejected_purchases


class TestMergeRules:
    def test_active_dates_are_a_union(self):
        """Стрик считается от множества дат — объединение коммутативно."""
        log = [
            _ev("a", "session.finished", {"duration_minutes": 25, "local_date": "2026-05-18"}),
            _ev("b", "session.finished", {"duration_minutes": 25, "local_date": "2026-05-19"},
                at="2026-05-19T10:00:00Z", device="phone-b"),
            _ev("c", "session.finished", {"duration_minutes": 10, "local_date": "2026-05-18"},
                at="2026-05-18T20:00:00Z", device="phone-b"),
        ]
        assert replay(log).active_dates == {"2026-05-18", "2026-05-19"}
        assert replay(log).total_sessions == 3

    def test_register_last_writer_wins(self):
        log = [
            _ev("a", "timezone.changed", {"timezone": "Europe/Moscow"},
                at="2026-05-18T10:00:00Z"),
            _ev("b", "timezone.changed", {"timezone": "Asia/Tokyo"},
                at="2026-05-18T12:00:00Z", device="phone-b"),
        ]
        assert replay(log).registers["timezone"] == "Asia/Tokyo"
        assert replay(list(reversed(log))).registers["timezone"] == "Asia/Tokyo"

    def test_register_tie_broken_deterministically(self):
        """Одинаковый таймстамп — решает lamport, затем device_id."""
        t = "2026-05-18T10:00:00Z"
        log = [
            _ev("a", "locale.changed", {"locale": "ru"}, at=t, lamport=1, device="zzz"),
            _ev("b", "locale.changed", {"locale": "en"}, at=t, lamport=2, device="aaa"),
        ]
        assert replay(log).registers["locale"] == "en"
        assert replay(list(reversed(log))).registers["locale"] == "en"

    def test_sm2_replay_matches_sequential_application(self):
        """Реплей карточки обязан совпасть с последовательным sm2_update."""
        from services import sm2_update

        qualities = [5, 3, 4, 2, 5, 5]
        log = [
            _ev(f"r{i}", "flashcard.reviewed", {"card_hash": "c1", "quality": q},
                at=f"2026-05-{18+i:02d}T10:00:00Z")
            for i, q in enumerate(qualities)
        ]
        reps, ef, interval = 0, 2.5, 0
        for q in qualities:
            reps, ef, interval = sm2_update(q, reps, ef, interval)

        got = replay(log).flashcards["c1"]
        assert (got.repetitions, round(got.ease_factor, 6), got.interval_days) == (
            reps, round(ef, 6), interval
        )

    def test_deleting_own_card_is_order_independent(self):
        log = [
            _ev("a", "flashcard.created", {"card_id": "c1"}, at="2026-05-18T10:00:00Z"),
            _ev("b", "flashcard.deleted", {"card_id": "c1"}, at="2026-05-18T11:00:00Z"),
        ]
        assert replay(log).user_flashcards == set()
        assert replay(list(reversed(log))).user_flashcards == set()


class TestRegistryDiscipline:
    def test_every_kind_has_a_merge_class(self):
        assert all(isinstance(v, MergeClass) for v in EVENT_KINDS.values())

    def test_unknown_kind_stops_the_replay(self):
        """
        Молча проигнорированное событие означало бы тихое расхождение
        реплик — самый дорогой из возможных багов в этой модели.
        """
        with pytest.raises(UnknownEventKind):
            replay([_ev("x", "coins.stolen", {"delta": 999})])
