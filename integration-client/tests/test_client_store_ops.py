"""Store ops and status (design gate #571 v24 §2, §4, §7 rows marked 166).

死信运维、已送达重放、处置原因与 `ops_log`、事故记录与确认、`status_state` 与
`reconcile_state` 的读写与状态重置。SQLite 用 tmp_path 下的文件，时间用注入的时钟；
载荷是测试里直接构造的字节，不经 events.py。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from acuven_billing_client import (
    DeadEvent,
    Fence,
    IncidentItem,
    OverflowCount,
    ReconcileTimes,
    StatusPair,
    Store,
)

T0 = 1_790_000_000.0
LEASE = 60.0
TENANT = "ten_a"
PROJECT = "prj_a"


class FakeClock:
    def __init__(self, now: float = T0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def payload(event_id: str) -> bytes:
    return f'{{"event_id":"{event_id}"}}'.encode()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "outbox.db"


@pytest.fixture
def store(db_path: Path, clock: FakeClock) -> Iterator[Store]:
    opened = Store(db_path, clock=clock)
    yield opened
    opened.close()


def raw(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def query(path: Path, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    conn = raw(path)
    try:
        return [dict(row) for row in conn.execute(sql, params)]
    finally:
        conn.close()


def execute(path: Path, sql: str, params: tuple[Any, ...] = ()) -> None:
    conn = raw(path)
    try:
        conn.execute(sql, params)
    finally:
        conn.close()


def outbox_rows(path: Path) -> dict[str, dict[str, Any]]:
    return {r["event_id"]: r for r in query(path, "SELECT * FROM usage_outbox")}


def ops_log(path: Path) -> list[tuple[str, str, str, float]]:
    rows = query(path, "SELECT action, target_id, reason, at FROM ops_log ORDER BY id")
    return [(r["action"], r["target_id"], r["reason"], r["at"]) for r in rows]


def incidents(path: Path) -> list[dict[str, Any]]:
    return query(path, "SELECT * FROM incidents ORDER BY recorded_at, incident_id")


def status_rows(path: Path) -> list[dict[str, Any]]:
    return query(path, "SELECT * FROM status_state ORDER BY tenant_id, project_id")


def reconcile_rows(path: Path) -> list[dict[str, Any]]:
    return query(path, "SELECT * FROM reconcile_state ORDER BY tenant_id, project_id")


def snapshot(path: Path) -> tuple[Any, ...]:
    return (
        outbox_rows(path),
        ops_log(path),
        incidents(path),
        status_rows(path),
        reconcile_rows(path),
    )


def claim_one(store: Store, event_id: str) -> Fence:
    claimed = store.claim(max_events=100, max_bytes=1_000_000, lease_seconds=LEASE)
    assert [c.event_id for c in claimed] == [event_id]
    return Fence(event_id, claimed[0].lease_until)


def make_dead(store: Store, event_id: str, code: str = "VALIDATION_ERROR") -> None:
    store.insert_event(event_id, payload(event_id))
    fence = claim_one(store, event_id)
    assert store.mark_dead([fence], code) == {event_id}


def make_delivered(store: Store, clock: FakeClock, event_id: str, at: float) -> None:
    clock.now = at
    store.insert_event(event_id, payload(event_id))
    fence = claim_one(store, event_id)
    assert store.mark_delivered([fence]) == {event_id}


def make_sending(store: Store, event_id: str) -> None:
    store.insert_event(event_id, payload(event_id))
    claim_one(store, event_id)


def status_fields(version: int, effective: str = "ALLOW_AI", **extra: Any) -> dict[str, Any]:
    return {
        "billing_mode": "PREPAID",
        "billing_status": "ACTIVE",
        "account_status": "ACTIVE",
        "project_integration_status": "ACTIVE",
        "effective_status": effective,
        "status_version": version,
        "reason_code": None,
        "effective_at": "2026-11-05T00:00:00Z",
        **extra,
    }


# --- 处置必填原因 -----------------------------------------------------------------


def _dispositions(store: Store) -> dict[str, Callable[[Any], Any]]:
    return {
        "requeue_dead": lambda reason: store.requeue_dead("EV_DEAD", reason=reason),
        "ack_dead": lambda reason: store.ack_dead("EV_DEAD", reason=reason),
        "replay_delivered": lambda reason: store.replay_delivered(
            delivered_from=T0 - 100, delivered_until=T0 + 100, reason=reason
        ),
        "ack_incident": lambda reason: store.ack_incident("inc_X", reason=reason),
        "reset_status": lambda reason: store.reset_status(TENANT, PROJECT, reason=reason),
    }


@pytest.fixture
def seeded(store: Store, clock: FakeClock, db_path: Path) -> Store:
    """One DEAD row, one DELIVERED row, one unacked incident and one status row."""
    make_delivered(store, clock, "EV_DELIVERED", T0)
    make_dead(store, "EV_DEAD")
    execute(
        db_path,
        "INSERT INTO incidents (incident_id, kind, event_id, dropped_count, first_occurred_at,"
        " last_occurred_at, recorded_at) VALUES ('inc_X', 'DROPPED', 'EV_GONE', 1, ?, ?, ?)",
        (T0, T0, T0),
    )
    assert store.apply_status(TENANT, PROJECT, status_fields(5, "BLOCK_AI"), now=T0)
    return store


@pytest.mark.parametrize(
    "action", ["requeue_dead", "ack_dead", "replay_delivered", "ack_incident", "reset_status"]
)
@pytest.mark.parametrize("reason", [None, 42, b"bytes"])
def test_reason_must_be_a_string(seeded: Store, db_path: Path, action: str, reason: object) -> None:
    before = snapshot(db_path)
    with pytest.raises(TypeError):
        _dispositions(seeded)[action](reason)
    assert snapshot(db_path) == before


@pytest.mark.parametrize(
    "action", ["requeue_dead", "ack_dead", "replay_delivered", "ack_incident", "reset_status"]
)
@pytest.mark.parametrize("reason", ["", "   ", "\t\n", "x" * 501, "  " + "y" * 501 + "  "])
def test_blank_or_long_reason_is_rejected_without_changes(
    seeded: Store, db_path: Path, action: str, reason: str
) -> None:
    before = snapshot(db_path)
    with pytest.raises(ValueError):
        _dispositions(seeded)[action](reason)
    assert snapshot(db_path) == before


@pytest.mark.parametrize(
    "action", ["requeue_dead", "ack_dead", "replay_delivered", "ack_incident", "reset_status"]
)
def test_reason_is_stored_stripped_with_injected_clock(
    seeded: Store, clock: FakeClock, db_path: Path, action: str
) -> None:
    clock.now = T0 + 1234.5
    reason = "  " + "r" * 500 + " \n"
    result = _dispositions(seeded)[action](reason)
    assert result in (True, 1)
    expected_action = {
        "requeue_dead": ("REQUEUE_DEAD", "EV_DEAD"),
        "ack_dead": ("ACK_DEAD", "EV_DEAD"),
        "replay_delivered": ("REPLAY_DELIVERED", "EV_DELIVERED"),
        "ack_incident": ("ACK_INCIDENT", "inc_X"),
        "reset_status": ("RESET_STATUS", f"{TENANT}/{PROJECT}"),
    }[action]
    assert ops_log(db_path) == [(*expected_action, "r" * 500, T0 + 1234.5)]


# --- ops_log 与状态改动同一事务 ----------------------------------------------------


@pytest.mark.parametrize(
    "action", ["requeue_dead", "ack_dead", "replay_delivered", "ack_incident", "reset_status"]
)
def test_ops_log_failure_rolls_back_the_change(seeded: Store, db_path: Path, action: str) -> None:
    execute(
        db_path,
        "CREATE TRIGGER ops_log_fails BEFORE INSERT ON ops_log"
        " BEGIN SELECT RAISE(ABORT, 'ops_log write failed'); END",
    )
    before = snapshot(db_path)
    with pytest.raises(sqlite3.DatabaseError):
        _dispositions(seeded)[action]("operator reason")
    assert snapshot(db_path) == before
    execute(db_path, "DROP TRIGGER ops_log_fails")
    # 同一个 Store 之后照常可用，改动与 ops_log 一起写入。
    assert _dispositions(seeded)[action]("operator reason") in (True, 1)
    assert len(ops_log(db_path)) == 1


# --- 死信运维（§4） --------------------------------------------------------------


def test_requeue_dead_resets_row_and_clears_error_code(
    store: Store, clock: FakeClock, db_path: Path
) -> None:
    make_dead(store, "EV1", "IDEMPOTENCY_CONFLICT")
    assert store.ack_dead("EV1", reason="looked at it")
    before = outbox_rows(db_path)["EV1"]
    assert before["last_error_code"] == "IDEMPOTENCY_CONFLICT"
    assert before["dead_at"] == T0
    assert before["dead_acked_at"] == T0
    clock.now = T0 + 500
    assert store.requeue_dead("EV1", reason="server fixed") is True
    row = outbox_rows(db_path)["EV1"]
    assert row["status"] == "PENDING"
    assert row["attempts"] == 0
    assert row["next_attempt_at"] == T0 + 500
    assert row["last_error_code"] is None
    assert row["dead_at"] is None
    assert row["dead_acked_at"] is None
    assert row["lease_until"] is None
    assert row["payload"] == payload("EV1")
    assert ops_log(db_path)[-1] == ("REQUEUE_DEAD", "EV1", "server fixed", T0 + 500)
    # 改回后到点就能被领取。
    claimed = store.claim(max_events=10, max_bytes=1_000_000, lease_seconds=LEASE)
    assert [c.event_id for c in claimed] == ["EV1"]
    assert claimed[0].attempts == 1


def _non_dead_rows(store: Store, clock: FakeClock) -> None:
    make_delivered(store, clock, "EV_DELIVERED", T0)
    make_sending(store, "EV_SENDING")
    store.insert_event("EV_PENDING", payload("EV_PENDING"))


@pytest.mark.parametrize("event_id", ["EV_DELIVERED", "EV_SENDING", "EV_PENDING", "EV_MISSING"])
def test_requeue_dead_in_other_states_returns_false(
    store: Store, clock: FakeClock, db_path: Path, event_id: str
) -> None:
    _non_dead_rows(store, clock)
    before = snapshot(db_path)
    assert store.requeue_dead(event_id, reason="try") is False
    assert snapshot(db_path) == before
    assert ops_log(db_path) == []


@pytest.mark.parametrize("event_id", ["EV_DELIVERED", "EV_SENDING", "EV_PENDING", "EV_MISSING"])
def test_ack_dead_in_other_states_returns_false(
    store: Store, clock: FakeClock, db_path: Path, event_id: str
) -> None:
    _non_dead_rows(store, clock)
    before = snapshot(db_path)
    assert store.ack_dead(event_id, reason="try") is False
    assert snapshot(db_path) == before


def test_ack_dead_once_then_false(store: Store, clock: FakeClock, db_path: Path) -> None:
    make_dead(store, "EV1")
    clock.now = T0 + 10
    assert store.ack_dead("EV1", reason="give up") is True
    row = outbox_rows(db_path)["EV1"]
    assert row["status"] == "DEAD"
    assert row["dead_acked_at"] == T0 + 10
    assert row["last_error_code"] == "VALIDATION_ERROR"
    assert ops_log(db_path) == [("ACK_DEAD", "EV1", "give up", T0 + 10)]
    before = snapshot(db_path)
    clock.now = T0 + 20
    assert store.ack_dead("EV1", reason="again") is False
    assert snapshot(db_path) == before


def test_list_dead_order_limit_and_acked_filter(store: Store, clock: FakeClock) -> None:
    clock.now = T0 + 30
    make_dead(store, "EV_C", "VALIDATION_ERROR")
    clock.now = T0 + 10
    make_dead(store, "EV_B", "PAYLOAD_TOO_LARGE")
    make_dead(store, "EV_A", "IDEMPOTENCY_CONFLICT")
    store.insert_event("EV_PENDING", payload("EV_PENDING"))
    clock.now = T0 + 40
    assert store.ack_dead("EV_C", reason="ok")
    listed = store.list_dead(10)
    assert listed == [
        DeadEvent("EV_A", "IDEMPOTENCY_CONFLICT", 1, T0 + 10, None),
        DeadEvent("EV_B", "PAYLOAD_TOO_LARGE", 1, T0 + 10, None),
        DeadEvent("EV_C", "VALIDATION_ERROR", 1, T0 + 30, T0 + 40),
    ]
    assert [d.event_id for d in store.list_dead(2)] == ["EV_A", "EV_B"]
    assert [d.event_id for d in store.list_dead(10, include_acked=False)] == ["EV_A", "EV_B"]
    with pytest.raises(ValueError):
        store.list_dead(0)


# --- 中心恢复后的重放 -------------------------------------------------------------


def test_replay_delivered_only_rows_inside_the_window(
    store: Store, clock: FakeClock, db_path: Path
) -> None:
    make_delivered(store, clock, "EV_BEFORE", T0 + 99)
    make_delivered(store, clock, "EV_INSIDE", T0 + 150)
    make_delivered(store, clock, "EV_AFTER", T0 + 200)
    make_dead(store, "EV_DEAD")
    # 已送达的行 last_error_code 本来就空；这里写一个，验证重放会清掉。
    execute(db_path, "UPDATE usage_outbox SET last_error_code = 'X' WHERE event_id = 'EV_INSIDE'")
    before = outbox_rows(db_path)
    clock.now = T0 + 1000
    replayed = store.replay_delivered(
        delivered_from=T0 + 100, delivered_until=T0 + 200, reason=" center restored "
    )
    assert replayed == 1
    after = outbox_rows(db_path)
    inside = after["EV_INSIDE"]
    assert inside["status"] == "PENDING"
    assert inside["attempts"] == 0
    assert inside["next_attempt_at"] == T0 + 1000
    assert inside["delivered_at"] is None
    assert inside["last_error_code"] is None
    assert inside["payload"] == before["EV_INSIDE"]["payload"]
    for untouched in ("EV_BEFORE", "EV_AFTER", "EV_DEAD"):
        assert after[untouched] == before[untouched]
    assert ops_log(db_path) == [("REPLAY_DELIVERED", "EV_INSIDE", "center restored", T0 + 1000)]


def test_replay_delivered_writes_one_ops_log_row_per_event(
    store: Store, clock: FakeClock, db_path: Path
) -> None:
    for i, event_id in enumerate(["EV1", "EV2", "EV3"]):
        make_delivered(store, clock, event_id, T0 + i)
    clock.now = T0 + 50
    assert store.replay_delivered(delivered_from=T0, delivered_until=T0 + 10, reason="r") == 3
    assert ops_log(db_path) == [
        ("REPLAY_DELIVERED", "EV1", "r", T0 + 50),
        ("REPLAY_DELIVERED", "EV2", "r", T0 + 50),
        ("REPLAY_DELIVERED", "EV3", "r", T0 + 50),
    ]
    # 第二次没有 DELIVERED 行可改：返回 0、不写 ops_log。
    assert store.replay_delivered(delivered_from=T0, delivered_until=T0 + 10, reason="r") == 0
    assert len(ops_log(db_path)) == 3


@pytest.mark.parametrize(("start", "end"), [(T0 + 200, T0 + 100), (T0 + 100, T0 + 100)])
def test_replay_delivered_inverted_or_empty_window(
    store: Store, clock: FakeClock, db_path: Path, start: float, end: float
) -> None:
    make_delivered(store, clock, "EV1", T0 + 100)
    before = snapshot(db_path)
    with pytest.raises(ValueError):
        store.replay_delivered(delivered_from=start, delivered_until=end, reason="r")
    assert snapshot(db_path) == before


@pytest.mark.parametrize(
    ("start", "end", "error"),
    [
        ("2026-11-05T00:00:00Z", T0, TypeError),
        (T0, True, TypeError),
        (float("nan"), T0, ValueError),
        (T0, float("inf"), ValueError),
    ],
)
def test_replay_delivered_window_must_be_unix_seconds(
    store: Store, clock: FakeClock, db_path: Path, start: Any, end: Any, error: type[Exception]
) -> None:
    make_delivered(store, clock, "EV1", T0)
    before = snapshot(db_path)
    with pytest.raises(error):
        store.replay_delivered(delivered_from=start, delivered_until=end, reason="r")
    assert snapshot(db_path) == before


# --- 事故 -------------------------------------------------------------------------


def test_record_incidents_writes_items_and_skips_unacked_duplicates(
    store: Store, clock: FakeClock, db_path: Path
) -> None:
    clock.now = T0 + 60
    inserted = store.record_incidents(
        [
            IncidentItem("EV1", "DROPPED", T0 + 1),
            IncidentItem("EV2", "DROPPED", T0 + 2),
            IncidentItem("EV3", "CONFLICT_DROPPED", T0 + 3),
            # 同一事务里重复的同 id 同种类：跳过。
            IncidentItem("EV1", "DROPPED", T0 + 4),
            # 同 id、不同种类：另记一条。
            IncidentItem("EV1", "CONFLICT_DROPPED", T0 + 5),
        ],
        overflow=None,
    )
    assert inserted == 4
    rows = incidents(db_path)
    assert sorted((r["event_id"], r["kind"]) for r in rows) == [
        ("EV1", "CONFLICT_DROPPED"),
        ("EV1", "DROPPED"),
        ("EV2", "DROPPED"),
        ("EV3", "CONFLICT_DROPPED"),
    ]
    for row in rows:
        assert row["incident_id"].startswith("inc_")
        assert len(row["incident_id"]) == 4 + 26
        assert row["dropped_count"] == 1
        assert row["first_occurred_at"] == row["last_occurred_at"]
        assert row["recorded_at"] == T0 + 60
        assert row["acked_at"] is None
        assert row["ack_reason"] is None
    ev1 = next(r for r in rows if r["event_id"] == "EV1" and r["kind"] == "DROPPED")
    assert ev1["first_occurred_at"] == T0 + 1
    assert len({r["incident_id"] for r in rows}) == 4
    # 只记 id 与条数，不记载荷：表里没有载荷列。
    assert set(rows[0]) == {
        "incident_id",
        "kind",
        "event_id",
        "dropped_count",
        "first_occurred_at",
        "last_occurred_at",
        "recorded_at",
        "acked_at",
        "ack_reason",
    }
    assert store.count_unacked_incidents() == 4


def test_incident_recorded_again_after_ack(store: Store, clock: FakeClock, db_path: Path) -> None:
    store.record_incidents([IncidentItem("EV_OWN", "DROPPED", T0)], overflow=None)
    # 未确认期间重复丢弃不新增。
    assert store.record_incidents([IncidentItem("EV_OWN", "DROPPED", T0 + 1)], overflow=None) == 0
    assert store.count_unacked_incidents() == 1
    [first] = incidents(db_path)
    clock.now = T0 + 10
    assert store.ack_incident(first["incident_id"], reason=" reconciled by hand ") is True
    assert store.count_unacked_incidents() == 0
    acked = incidents(db_path)[0]
    assert acked["acked_at"] == T0 + 10
    assert acked["ack_reason"] == "reconciled by hand"
    assert ops_log(db_path) == [
        ("ACK_INCIDENT", first["incident_id"], "reconciled by hand", T0 + 10)
    ]
    clock.now = T0 + 20
    assert store.record_incidents([IncidentItem("EV_OWN", "DROPPED", T0 + 15)], overflow=None) == 1
    rows = incidents(db_path)
    assert len(rows) == 2
    assert rows[1]["incident_id"] != first["incident_id"]
    assert rows[1]["acked_at"] is None
    assert store.count_unacked_incidents() == 1


def test_ack_incident_already_acked_or_missing_returns_false(
    store: Store, clock: FakeClock, db_path: Path
) -> None:
    store.record_incidents([IncidentItem("EV1", "DROPPED", T0)], overflow=None)
    [row] = incidents(db_path)
    assert store.ack_incident(row["incident_id"], reason="first")
    before = snapshot(db_path)
    clock.now = T0 + 5
    assert store.ack_incident(row["incident_id"], reason="second") is False
    assert store.ack_incident("inc_MISSING", reason="second") is False
    assert snapshot(db_path) == before


def test_overflow_writes_one_overflow_dropped_row(
    store: Store, clock: FakeClock, db_path: Path
) -> None:
    clock.now = T0 + 100
    inserted = store.record_incidents(
        [IncidentItem("EV1", "DROPPED", T0 + 1), IncidentItem("EV2", "DROPPED", T0 + 2)],
        overflow=OverflowCount(3, T0 + 3, T0 + 9),
    )
    assert inserted == 3
    rows = incidents(db_path)
    overflow = [r for r in rows if r["kind"] == "OVERFLOW_DROPPED"]
    assert len(overflow) == 1
    assert overflow[0]["event_id"] is None
    assert overflow[0]["dropped_count"] == 3
    assert overflow[0]["first_occurred_at"] == T0 + 3
    assert overflow[0]["last_occurred_at"] == T0 + 9
    assert overflow[0]["recorded_at"] == T0 + 100
    assert overflow[0]["incident_id"].startswith("inc_")
    assert [r["kind"] for r in rows].count("DROPPED") == 2
    # OVERFLOW_DROPPED 不去重：再来一段溢出另写一行。
    assert store.record_incidents([], overflow=OverflowCount(1, T0 + 20, T0 + 20)) == 1
    assert [r["kind"] for r in incidents(db_path)].count("OVERFLOW_DROPPED") == 2
    assert store.count_unacked_incidents() == 4
    for row in incidents(db_path):
        assert store.ack_incident(row["incident_id"], reason="done")
    assert store.count_unacked_incidents() == 0
    assert [a for a, *_ in ops_log(db_path)] == ["ACK_INCIDENT"] * 4


@pytest.mark.parametrize("overflow", [None, OverflowCount(0, T0, T0)])
def test_zero_or_no_overflow_writes_nothing(
    store: Store, db_path: Path, overflow: OverflowCount | None
) -> None:
    assert store.record_incidents([], overflow=overflow) == 0
    assert incidents(db_path) == []


@pytest.mark.parametrize(
    ("items", "overflow", "error"),
    [
        ([IncidentItem("EV2", "OVERFLOW_DROPPED", T0)], None, ValueError),
        ([IncidentItem("EV2", "SOMETHING", T0)], None, ValueError),
        ([IncidentItem(None, "DROPPED", T0)], None, TypeError),  # type: ignore[arg-type]
        ([], OverflowCount(-1, T0, T0), ValueError),
        ([], OverflowCount(True, T0, T0), TypeError),
    ],
)
def test_invalid_incident_input_writes_nothing(
    store: Store, db_path: Path, items: list[IncidentItem], overflow: Any, error: type[Exception]
) -> None:
    with pytest.raises(error):
        store.record_incidents([IncidentItem("EV1", "DROPPED", T0), *items], overflow=overflow)
    assert incidents(db_path) == []


def test_list_incidents_order_limit_and_acked_filter(store: Store, clock: FakeClock) -> None:
    clock.now = T0 + 20
    store.record_incidents([IncidentItem("EV_LATE", "DROPPED", T0)], overflow=None)
    clock.now = T0 + 10
    store.record_incidents(
        [IncidentItem("EV_A", "DROPPED", T0 + 1), IncidentItem("EV_B", "CONFLICT_DROPPED", T0 + 2)],
        overflow=OverflowCount(2, T0 + 3, T0 + 4),
    )
    listed = store.list_incidents(10)
    assert [i.recorded_at for i in listed] == [T0 + 10] * 3 + [T0 + 20]
    same_time = [i.incident_id for i in listed[:3]]
    assert same_time == sorted(same_time)
    assert listed[3].event_id == "EV_LATE"
    overflow = next(i for i in listed if i.kind == "OVERFLOW_DROPPED")
    assert (overflow.event_id, overflow.dropped_count) == (None, 2)
    assert (overflow.first_occurred_at, overflow.last_occurred_at) == (T0 + 3, T0 + 4)
    assert overflow.acked_at is None
    assert len(store.list_incidents(2)) == 2
    clock.now = T0 + 30
    assert store.ack_incident(listed[0].incident_id, reason="ok")
    assert store.list_incidents(10)[0].acked_at == T0 + 30
    unacked = store.list_incidents(10, include_acked=False)
    assert [i.incident_id for i in unacked] == [i.incident_id for i in listed[1:]]
    with pytest.raises(ValueError):
        store.list_incidents(0)


# --- 有效状态与对账时刻 ------------------------------------------------------------


def test_apply_status_version_rules(store: Store, db_path: Path) -> None:
    assert store.read_status(TENANT, PROJECT) is None
    assert store.apply_status(TENANT, PROJECT, status_fields(3), now=T0) is True
    stored = store.read_status(TENANT, PROJECT)
    assert stored is not None
    assert (stored.status_version, stored.effective_status, stored.applied_at) == (
        3,
        "ALLOW_AI",
        T0,
    )
    assert (
        store.apply_status(
            TENANT,
            PROJECT,
            status_fields(5, "BLOCK_AI", reason_code="NEGATIVE_BALANCE"),
            now=T0 + 1,
        )
        is True
    )
    stored = store.read_status(TENANT, PROJECT)
    assert stored is not None
    assert stored.status_version == 5
    assert stored.effective_status == "BLOCK_AI"
    assert stored.reason_code == "NEGATIVE_BALANCE"
    assert stored.applied_at == T0 + 1
    before = status_rows(db_path)
    assert store.apply_status(TENANT, PROJECT, status_fields(4), now=T0 + 2) is False
    assert store.apply_status(TENANT, PROJECT, status_fields(5), now=T0 + 3) is False
    assert status_rows(db_path) == before
    # apply_status 不写对账时刻。
    assert reconcile_rows(db_path) == []


def test_apply_status_stores_only_known_fields(store: Store, db_path: Path) -> None:
    fields = status_fields(1, tenant_id=TENANT, project_id=PROJECT, unknown_field="ignored")
    assert store.apply_status(TENANT, PROJECT, fields, now=T0)
    [row] = status_rows(db_path)
    assert row == {
        "tenant_id": TENANT,
        "project_id": PROJECT,
        "billing_mode": "PREPAID",
        "billing_status": "ACTIVE",
        "account_status": "ACTIVE",
        "project_integration_status": "ACTIVE",
        "effective_status": "ALLOW_AI",
        "status_version": 1,
        "reason_code": None,
        "effective_at": "2026-11-05T00:00:00Z",
        "applied_at": T0,
    }


def test_apply_status_missing_field_is_rejected(store: Store, db_path: Path) -> None:
    fields = status_fields(1)
    del fields["effective_at"]
    with pytest.raises(ValueError):
        store.apply_status(TENANT, PROJECT, fields, now=T0)
    with pytest.raises(TypeError):
        store.apply_status(TENANT, PROJECT, status_fields("2"), now=T0)  # type: ignore[arg-type]
    assert status_rows(db_path) == []


def test_reconcile_error_first_leaves_status_empty(
    store: Store, clock: FakeClock, db_path: Path
) -> None:
    assert store.read_reconcile(TENANT, PROJECT) == ReconcileTimes(None, None)
    clock.now = T0 + 999
    store.record_reconcile_error(TENANT, PROJECT, now=T0)
    assert store.read_reconcile(TENANT, PROJECT) == ReconcileTimes(None, T0)
    assert status_rows(db_path) == []
    assert store.read_status(TENANT, PROJECT) is None
    # 之后 apply_status 能插入，且不动对账时刻。
    assert store.apply_status(TENANT, PROJECT, status_fields(1, "BLOCK_AI"), now=T0 + 30)
    assert store.read_reconcile(TENANT, PROJECT) == ReconcileTimes(None, T0)
    store.record_reconcile_ok(TENANT, PROJECT, now=T0 + 30)
    assert store.read_reconcile(TENANT, PROJECT) == ReconcileTimes(T0 + 30, T0)
    store.record_reconcile_error(TENANT, PROJECT, now=T0 + 270)
    assert store.read_reconcile(TENANT, PROJECT) == ReconcileTimes(T0 + 30, T0 + 270)
    stored = store.read_status(TENANT, PROJECT)
    assert stored is not None
    assert (stored.status_version, stored.applied_at) == (1, T0 + 30)
    assert len(status_rows(db_path)) == 1


def test_reconcile_ok_does_not_touch_status_state(store: Store, db_path: Path) -> None:
    store.record_reconcile_ok(TENANT, PROJECT, now=T0)
    assert status_rows(db_path) == []
    assert store.read_reconcile(TENANT, PROJECT) == ReconcileTimes(T0, None)


def test_other_project_state_is_not_reused(store: Store, db_path: Path) -> None:
    assert store.apply_status("ten_old", "prj_old", status_fields(99, "BLOCK_AI"), now=T0)
    store.record_reconcile_ok("ten_old", "prj_old", now=T0)
    old = status_rows(db_path)
    assert store.read_status(TENANT, PROJECT) is None
    assert store.read_reconcile(TENANT, PROJECT) == ReconcileTimes(None, None)
    assert store.apply_status(TENANT, PROJECT, status_fields(1, "ALLOW_AI"), now=T0 + 1) is True
    current = store.read_status(TENANT, PROJECT)
    assert current is not None
    assert (current.status_version, current.effective_status) == (1, "ALLOW_AI")
    # 版本只和同一对比较。
    assert store.apply_status(TENANT, PROJECT, status_fields(2), now=T0 + 2) is True
    assert [r for r in status_rows(db_path) if r["tenant_id"] == "ten_old"] == old


def test_reset_status_after_center_restore(store: Store, clock: FakeClock, db_path: Path) -> None:
    assert store.apply_status(TENANT, PROJECT, status_fields(50, "BLOCK_AI"), now=T0)
    assert store.apply_status("ten_other", PROJECT, status_fields(7, "BLOCK_AI"), now=T0)
    store.record_reconcile_ok(TENANT, PROJECT, now=T0)
    # 中心恢复后版本回到 3：按版本规则被忽略，仍拦截。
    assert store.apply_status(TENANT, PROJECT, status_fields(3, "ALLOW_AI"), now=T0 + 1) is False
    stored = store.read_status(TENANT, PROJECT)
    assert stored is not None
    assert stored.effective_status == "BLOCK_AI"
    with pytest.raises(ValueError):
        store.reset_status(TENANT, PROJECT, reason=" ")
    assert store.read_status(TENANT, PROJECT) is not None
    clock.now = T0 + 60
    assert store.reset_status(TENANT, PROJECT, reason="center restored from backup") is True
    assert store.read_status(TENANT, PROJECT) is None
    assert ops_log(db_path) == [
        ("RESET_STATUS", f"{TENANT}/{PROJECT}", "center restored from backup", T0 + 60)
    ]
    # reconcile_state 与别的那一对不动。
    assert store.read_reconcile(TENANT, PROJECT) == ReconcileTimes(T0, None)
    assert store.read_status("ten_other", PROJECT) is not None
    # 下一次应用按「还没有行」直接插入。
    assert store.apply_status(TENANT, PROJECT, status_fields(3, "ALLOW_AI"), now=T0 + 61) is True
    stored = store.read_status(TENANT, PROJECT)
    assert stored is not None
    assert (stored.status_version, stored.effective_status) == (3, "ALLOW_AI")


def test_reset_status_without_row_returns_false(store: Store, db_path: Path) -> None:
    store.record_reconcile_ok(TENANT, PROJECT, now=T0)
    before = snapshot(db_path)
    assert store.reset_status(TENANT, PROJECT, reason="nothing there") is False
    assert snapshot(db_path) == before
    assert ops_log(db_path) == []


def test_list_status_pairs_joins_both_tables(store: Store) -> None:
    assert store.list_status_pairs() == []
    assert store.apply_status("ten_a", "prj_both", status_fields(4, "BLOCK_AI"), now=T0)
    store.record_reconcile_ok("ten_a", "prj_both", now=T0 + 1)
    store.record_reconcile_error("ten_a", "prj_both", now=T0 + 2)
    assert store.apply_status("ten_a", "prj_status", status_fields(1), now=T0 + 3)
    store.record_reconcile_error("ten_b", "prj_reconcile", now=T0 + 4)
    pairs = store.list_status_pairs()
    assert [(p.tenant_id, p.project_id) for p in pairs] == [
        ("ten_a", "prj_both"),
        ("ten_a", "prj_status"),
        ("ten_b", "prj_reconcile"),
    ]
    both, status_only, reconcile_only = pairs
    assert both.effective_status == "BLOCK_AI"
    assert both.status_version == 4
    assert both.applied_at == T0
    assert (both.last_ok_at, both.last_error_at) == (T0 + 1, T0 + 2)
    assert status_only.status_version == 1
    assert (status_only.last_ok_at, status_only.last_error_at) == (None, None)
    assert reconcile_only == StatusPair(
        "ten_b",
        "prj_reconcile",
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        T0 + 4,
    )


def test_status_survives_reopen(db_path: Path, clock: FakeClock) -> None:
    with Store(db_path, clock=clock) as first:
        assert first.apply_status(TENANT, PROJECT, status_fields(8, "BLOCK_AI"), now=T0)
        first.record_reconcile_ok(TENANT, PROJECT, now=T0)
        first.record_incidents([IncidentItem("EV1", "DROPPED", T0)], overflow=None)
    with Store(db_path, clock=clock) as second:
        stored = second.read_status(TENANT, PROJECT)
        assert stored is not None
        assert stored.effective_status == "BLOCK_AI"
        assert second.read_reconcile(TENANT, PROJECT) == ReconcileTimes(T0, None)
        assert second.count_unacked_incidents() == 1
