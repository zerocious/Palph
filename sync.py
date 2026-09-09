"""
Ядро синхронизации бота и десктоп-клиента (DESKTOP.md §4).

Фаза 0 по спеке: единица обмена, тотальный порядок, правила слияния и
реплей. Транспорт (sync_api.py) и таблицы devices/pair_codes — фаза 1.

Почему событие, а не строка таблицы: «total_coins = 340» с двух устройств
— два несовместимых утверждения, и любое правило слияния строк теряет
данные. Факты («сессия 25 минут завершена в 14:03») не конфликтуют, они
просто все произошли, а состояние из них выводится.

Модуль намеренно чистый: никакого SQL и ввода-вывода. Это позволяет
гонять реплей на перестановках в тестах и одинаково применять его на
сервере и на клиенте.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable

from services import sm2_update


class MergeClass(str, Enum):
    """Как значение сливается при расхождении реплик (DESKTOP.md §4.3)."""

    APPEND_ONLY = "append-only"   # объединение по uuid, конфликтов не бывает
    DERIVED = "derived"           # пересчёт реплеем в тотальном порядке
    REGISTER = "register"         # last-writer-wins по ключу порядка
    SERVER_ONLY = "server-only"   # локально read-only, менять можно только онлайн


# Реестр видов событий. Новый вид ОБЯЗАН получить класс слияния здесь —
# иначе replay() его отвергнет, и это осознанно: молча проигнорированное
# событие означало бы тихое расхождение реплик.
EVENT_KINDS: dict[str, MergeClass] = {
    "session.finished":         MergeClass.DERIVED,
    "flashcard.reviewed":       MergeClass.DERIVED,
    "mcq.answered":             MergeClass.DERIVED,
    "task.attempted":           MergeClass.DERIVED,
    "quiz.answered":            MergeClass.DERIVED,
    "subject.visited":          MergeClass.DERIVED,
    "coins.granted":            MergeClass.DERIVED,
    "purchase.made":            MergeClass.DERIVED,
    "purchase.rejected":        MergeClass.DERIVED,   # эмитит только сервер
    "streak.freeze_purchased":  MergeClass.DERIVED,
    "streak.freeze_consumed":   MergeClass.DERIVED,
    "tip.seen":                 MergeClass.APPEND_ONLY,
    "flashcard.created":        MergeClass.APPEND_ONLY,
    "flashcard.deleted":        MergeClass.APPEND_ONLY,
    "usertask.created":         MergeClass.APPEND_ONLY,
    "usertask.deleted":         MergeClass.APPEND_ONLY,
    "pet.customized":           MergeClass.REGISTER,
    "pet.renamed":              MergeClass.REGISTER,
    "settings.changed":         MergeClass.REGISTER,
    "timezone.changed":         MergeClass.REGISTER,
    "locale.changed":           MergeClass.REGISTER,
    "plan.updated":             MergeClass.REGISTER,
}

# Поля-регистры: имя → вид события, которым оно меняется.
REGISTER_FIELDS: dict[str, str] = {
    "timezone":                 "timezone.changed",
    "locale":                   "locale.changed",
    "pet_name":                 "pet.renamed",
    "pet_color":                "pet.customized",
    "pet_accessory":            "pet.customized",
}


class UnknownEventKind(ValueError):
    """Вид события отсутствует в EVENT_KINDS — реплей остановлен."""


@dataclass(frozen=True)
class SyncEvent:
    """
    Единица обмена между репликами.

    occurred_at — UTC ISO-8601 фиксированной ширины, чтобы лексикографическое
    сравнение строк совпадало с хронологическим. lamport — монотонный счётчик
    устройства: переживает перевод часов назад и совпадение таймстампов до
    миллисекунды. device_id — финальный детерминированный тай-брейк.
    """

    event_uuid: str
    user_id: int
    kind: str
    payload: dict[str, Any]
    occurred_at: str
    lamport: int
    device_id: str

    @property
    def order_key(self) -> tuple[str, int, str]:
        return (self.occurred_at, self.lamport, self.device_id)


@dataclass
class SM2State:
    repetitions: int = 0
    ease_factor: float = 2.5
    interval_days: int = 0


@dataclass
class UserState:
    """Проекция, выведенная из журнала событий."""

    coins: int = 0
    xp: int = 0
    total_sessions: int = 0
    total_minutes: int = 0
    active_dates: set[str] = field(default_factory=set)
    inventory: set[tuple[str, str]] = field(default_factory=set)
    flashcards: dict[str, SM2State] = field(default_factory=dict)
    mcq_progress: dict[str, tuple[int, int]] = field(default_factory=dict)   # hash → (верных, всего)
    task_progress: dict[str, bool] = field(default_factory=dict)             # task_id → решена
    quiz_progress: dict[str, int] = field(default_factory=dict)              # hash → верных подряд
    subject_visits: dict[str, int] = field(default_factory=dict)
    tips_seen: set[str] = field(default_factory=set)
    user_flashcards: set[str] = field(default_factory=set)
    user_tasks: set[str] = field(default_factory=set)
    freezes_available: int = 0
    registers: dict[str, Any] = field(default_factory=dict)
    plans: dict[str, Any] = field(default_factory=dict)                      # subject_id → plan_json
    rejected_purchases: set[str] = field(default_factory=set)

    def snapshot(self) -> dict[str, Any]:
        """Сравнимое представление — для тестов сходимости и контрольных сумм."""
        return {
            "coins": self.coins,
            "xp": self.xp,
            "total_sessions": self.total_sessions,
            "total_minutes": self.total_minutes,
            "active_dates": sorted(self.active_dates),
            "inventory": sorted(self.inventory),
            "flashcards": {
                k: (v.repetitions, round(v.ease_factor, 6), v.interval_days)
                for k, v in sorted(self.flashcards.items())
            },
            "mcq_progress": dict(sorted(self.mcq_progress.items())),
            "task_progress": dict(sorted(self.task_progress.items())),
            "quiz_progress": dict(sorted(self.quiz_progress.items())),
            "subject_visits": dict(sorted(self.subject_visits.items())),
            "tips_seen": sorted(self.tips_seen),
            "user_flashcards": sorted(self.user_flashcards),
            "user_tasks": sorted(self.user_tasks),
            "freezes_available": self.freezes_available,
            "registers": dict(sorted(self.registers.items())),
            "plans": dict(sorted(self.plans.items())),
            "rejected_purchases": sorted(self.rejected_purchases),
        }


def total_order(events: Iterable[SyncEvent]) -> list[SyncEvent]:
    """
    Канонический порядок: (occurred_at, lamport, device_id).

    Дубли по event_uuid схлопываются — одно и то же событие может прийти
    и из outbox'а, и из pull'а.
    """
    unique: dict[str, SyncEvent] = {}
    for e in events:
        unique.setdefault(e.event_uuid, e)
    return sorted(unique.values(), key=lambda e: e.order_key)


def _apply_register(state: UserState, event: SyncEvent) -> None:
    """LWW: побеждает событие с большим ключом порядка."""
    for field_name, value in event.payload.items():
        if value is None:
            continue
        key = f"__order__{field_name}"
        previous = state.registers.get(key)
        if previous is not None and previous >= event.order_key:
            continue
        state.registers[field_name] = value
        state.registers[key] = event.order_key


def replay(events: Iterable[SyncEvent]) -> UserState:
    """
    Сворачивает журнал в состояние. Детерминирован: результат зависит от
    множества событий, но не от порядка их поступления.

    Траты арбитрируются по ходу: покупка, уводящая баланс ниже нуля,
    отклоняется — это единственный настоящий конфликт в модели (DESKTOP.md
    §4.5). Решение сервера, пришедшее как purchase.rejected, имеет
    приоритет над локальным расчётом, иначе реплики разошлись бы.
    """
    ordered = total_order(events)

    # Отклонённые сервером покупки известны заранее — иначе локальный
    # баланс мог бы разрешить то, что сервер уже отверг.
    server_rejected = {
        e.payload["ref_uuid"]
        for e in ordered
        if e.kind == "purchase.rejected" and e.payload.get("ref_uuid")
    }

    state = UserState()
    state.rejected_purchases |= server_rejected

    for e in ordered:
        if e.kind not in EVENT_KINDS:
            raise UnknownEventKind(
                f"{e.kind!r} нет в EVENT_KINDS: у вида события должен быть "
                f"класс слияния, иначе реплики разойдутся молча"
            )
        p = e.payload

        if e.kind == "session.finished":
            minutes = int(p.get("duration_minutes", 0))
            state.total_sessions += 1
            state.total_minutes += minutes
            state.xp += minutes
            if p.get("local_date"):
                state.active_dates.add(p["local_date"])

        elif e.kind == "coins.granted":
            state.coins += int(p.get("delta", 0))

        elif e.kind == "purchase.made":
            price = int(p.get("price", 0))
            if e.event_uuid in server_rejected or price > state.coins:
                state.rejected_purchases.add(e.event_uuid)
                continue
            state.coins -= price
            state.inventory.add((p["item_type"], p["item_value"]))

        elif e.kind == "purchase.rejected":
            pass  # уже учтено через server_rejected

        elif e.kind == "streak.freeze_purchased":
            price = int(p.get("price", 0))
            if price > state.coins:
                state.rejected_purchases.add(e.event_uuid)
                continue
            state.coins -= price
            state.freezes_available += 1

        elif e.kind == "streak.freeze_consumed":
            if state.freezes_available > 0:
                state.freezes_available -= 1

        elif e.kind == "flashcard.reviewed":
            card = p["card_hash"]
            cur = state.flashcards.setdefault(card, SM2State())
            reps, ef, interval = sm2_update(
                int(p["quality"]), cur.repetitions, cur.ease_factor, cur.interval_days
            )
            state.flashcards[card] = SM2State(reps, ef, interval)
            if p.get("local_date"):
                state.active_dates.add(p["local_date"])

        elif e.kind == "mcq.answered":
            h = p["question_hash"]
            correct, total = state.mcq_progress.get(h, (0, 0))
            state.mcq_progress[h] = (correct + (1 if p.get("correct") else 0), total + 1)

        elif e.kind == "task.attempted":
            tid = p["task_id"]
            state.task_progress[tid] = state.task_progress.get(tid, False) or bool(
                p.get("succeeded")
            )

        elif e.kind == "quiz.answered":
            h = p["term_hash"]
            state.quiz_progress[h] = (
                state.quiz_progress.get(h, 0) + 1 if p.get("is_correct") else 0
            )

        elif e.kind == "subject.visited":
            sid = p["subject_id"]
            state.subject_visits[sid] = state.subject_visits.get(sid, 0) + 1

        elif e.kind == "tip.seen":
            state.tips_seen.add(p["tip_id"])

        elif e.kind == "flashcard.created":
            state.user_flashcards.add(p["card_id"])
        elif e.kind == "flashcard.deleted":
            state.user_flashcards.discard(p["card_id"])
        elif e.kind == "usertask.created":
            state.user_tasks.add(p["task_id"])
        elif e.kind == "usertask.deleted":
            state.user_tasks.discard(p["task_id"])

        elif e.kind == "plan.updated":
            state.plans[p["subject_id"]] = p["plan_json"]

        elif EVENT_KINDS[e.kind] is MergeClass.REGISTER:
            _apply_register(state, e)

    # Служебные ключи порядка наружу не отдаём
    state.registers = {
        k: v for k, v in state.registers.items() if not k.startswith("__order__")
    }
    return state
