"""Operations command line `python -m acuven_billing_client` (design gate #571 v24).

覆盖 §7 标 170 的「命令行」「命令行对着被锁的 outbox」两行：每个子命令的成功、失败与
参数错（含 `reset-status`），打不开的文件（不存在、零字节、非 SQLite、别的应用的库、版本
更新）、读写错误退出 4，以及另一个进程（multiprocessing 子进程）持有 BillingClient 时退出 3。

SQLite 用 tmp_path 下的文件，时间用注入的时钟；不联网、不 sleep。库里的行用测试自己的
sqlite3 连接准备（命令行本身只经 Store 读写）。
"""

from __future__ import annotations

import errno
import json
import multiprocessing
import os
import random
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import acuven_billing_client
import pytest
from acuven_billing_client import (
    BillingClient,
    BillingClientConfig,
    Response,
    Store,
    StoreReplacedError,
)
from acuven_billing_client.__main__ import main
from acuven_billing_client.store import _ProcessLock

T0 = 1_790_000_000.0
T0_ISO = "2026-09-21T14:13:20+00:00"
TENANT = "ten_a"
PROJECT = "prj_a"
STATS_KEYS = [
    "pending",
    "sending",
    "dead_unacked",
    "incidents_unacked",
    "oldest_pending_age_seconds",
    "auth_failing",
    "last_delivery_ok_at",
    "status",
]
STATUS_KEYS = [
    "tenant_id",
    "project_id",
    "effective_status",
    "status_version",
    "applied_at",
    "last_ok_at",
    "last_error_at",
]
DEAD_KEYS = ["event_id", "last_error_code", "attempts", "dead_at", "dead_acked_at"]
INCIDENT_KEYS = [
    "incident_id",
    "kind",
    "event_id",
    "dropped_count",
    "first_occurred_at",
    "last_occurred_at",
    "recorded_at",
    "acked_at",
]


class FakeClock:
    def __init__(self, now: float = T0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class OfflineTransport:
    def send(self, method: str, url: str, headers: Any, body: bytes, timeout: float) -> Response:
        return Response(503, b"")


def sqlite_error(name: str, cls: type[sqlite3.Error] = sqlite3.DatabaseError) -> sqlite3.Error:
    error = cls(f"simulated {name}")
    error.sqlite_errorname = name  # type: ignore[attr-defined]
    return error


# --- 库 ---------------------------------------------------------------------------------


def eid(n: int) -> str:
    return f"01J{n:023d}"


def raw(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def execute(path: Path, sql: str, params: tuple[Any, ...] = ()) -> None:
    conn = raw(path)
    try:
        conn.execute(sql, params)
    finally:
        conn.close()


def select(path: Path, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    conn = raw(path)
    try:
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def dump(path: Path) -> dict[str, list[dict[str, Any]]]:
    tables = ("meta", "usage_outbox", "status_state", "reconcile_state", "ops_log", "incidents")
    return {table: select(path, f"SELECT * FROM {table} ORDER BY 1") for table in tables}


def add_event(path: Path, n: int, status: str, created_at: float = T0, **columns: Any) -> str:
    values: dict[str, Any] = {
        "event_id": eid(n),
        "payload": b'{"n":%d}' % n,
        "status": status,
        "attempts": 1,
        "next_attempt_at": created_at,
        "created_at": created_at,
        **columns,
    }
    names = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    execute(path, f"INSERT INTO usage_outbox ({names}) VALUES ({marks})", tuple(values.values()))
    return eid(n)


def add_incident(path: Path, n: int, acked_at: float | None = None) -> str:
    incident_id = f"inc_{n:04d}"
    execute(
        path,
        "INSERT INTO incidents (incident_id, kind, event_id, dropped_count,"
        " first_occurred_at, last_occurred_at, recorded_at, acked_at)"
        " VALUES (?, 'DROPPED', ?, 1, ?, ?, ?, ?)",
        (incident_id, eid(n), T0 - 20, T0 - 10, T0 + n, acked_at),
    )
    return incident_id


def add_status(path: Path, tenant_id: str, project_id: str, version: int = 7) -> None:
    execute(
        path,
        "INSERT INTO status_state (tenant_id, project_id, billing_mode, billing_status,"
        " account_status, project_integration_status, effective_status, status_version,"
        " reason_code, effective_at, applied_at)"
        " VALUES (?, ?, 'PREPAID', 'ACTIVE', 'ACTIVE', 'ENABLED', 'BLOCK_AI', ?, NULL,"
        " '2026-09-01T12:00:00Z', ?)",
        (tenant_id, project_id, version, T0 - 30),
    )


def add_reconcile(
    path: Path, tenant_id: str, project_id: str, ok: float | None, error: float | None
) -> None:
    execute(
        path,
        "INSERT INTO reconcile_state (tenant_id, project_id, last_ok_at, last_error_at)"
        " VALUES (?, ?, ?, ?)",
        (tenant_id, project_id, ok, error),
    )


def ops(path: Path) -> list[dict[str, Any]]:
    return select(path, "SELECT action, target_id, reason, at FROM ops_log ORDER BY id")


def make_config(path: Path) -> BillingClientConfig:
    return BillingClientConfig(
        base_url="https://billing.example.com",
        api_key="ak_test",
        key_version=3,
        secret="sk_" + "0" * 64,
        tenant_id=TENANT,
        project_id=PROJECT,
        outbox_path=str(path),
    )


# --- 夹具与运行 -------------------------------------------------------------------------


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def db_path(tmp_path: Path, clock: FakeClock) -> Path:
    path = tmp_path / "outbox.db"
    Store(path, clock=clock).close()
    return path


class Run:
    def __init__(self, code: int, out: str, err: str) -> None:
        self.code = code
        self.out = out
        self.err = err

    @property
    def lines(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.out.splitlines()]

    @property
    def json(self) -> Any:
        lines = self.out.splitlines()
        assert len(lines) == 1
        return json.loads(lines[0])

    @property
    def error(self) -> dict[str, Any]:
        lines = self.err.splitlines()
        assert len(lines) == 1
        return json.loads(lines[0])


@pytest.fixture
def cli(capsys: pytest.CaptureFixture[str], clock: FakeClock, db_path: Path) -> Callable[..., Run]:
    def run(*args: str, outbox: Path | None = None) -> Run:
        target = db_path if outbox is None else outbox
        code = main(["--outbox", str(target), *args], clock=clock)
        captured = capsys.readouterr()
        return Run(code, captured.out, captured.err)

    return run


def assert_usage_error(run: Run) -> None:
    assert run.code == 2
    assert run.out == ""
    assert run.error["error"] == "usage"
    assert "usage:" not in run.err


# --- stats ------------------------------------------------------------------------------


def test_stats(cli: Callable[..., Run], db_path: Path, clock: FakeClock) -> None:
    add_event(db_path, 1, "PENDING", created_at=T0 - 100)
    add_event(db_path, 2, "SENDING", created_at=T0 - 300, lease_until=T0 - 1)
    add_event(db_path, 3, "PENDING", last_error_code="INTEGRATION_AUTH_FAILED")
    add_event(db_path, 4, "DEAD", dead_at=T0)
    add_event(db_path, 5, "DEAD", dead_at=T0, dead_acked_at=T0)
    add_event(db_path, 6, "DELIVERED", delivered_at=T0)
    add_incident(db_path, 1)
    add_incident(db_path, 2, acked_at=T0)
    execute(db_path, "INSERT INTO meta (key, value) VALUES ('last_delivery_ok_at', ?)", (repr(T0),))
    add_status(db_path, TENANT, PROJECT)
    add_reconcile(db_path, TENANT, PROJECT, T0 - 5, None)
    # 只在 reconcile_state 出现过的一对。
    add_reconcile(db_path, "ten_b", "prj_b", None, T0 - 1)
    clock.now = T0 + 50

    run = cli("stats")

    assert run.code == 0
    assert run.err == ""
    document = run.json
    assert list(document) == STATS_KEYS
    assert document["pending"] == 2
    assert document["sending"] == 1
    assert document["dead_unacked"] == 1
    assert document["incidents_unacked"] == 1
    assert document["oldest_pending_age_seconds"] == 350.0
    assert document["auth_failing"] is True
    assert document["last_delivery_ok_at"] == T0
    assert document["status"] == [
        {
            "tenant_id": TENANT,
            "project_id": PROJECT,
            "effective_status": "BLOCK_AI",
            "status_version": 7,
            "applied_at": T0 - 30,
            "last_ok_at": T0 - 5,
            "last_error_at": None,
        },
        {
            "tenant_id": "ten_b",
            "project_id": "prj_b",
            "effective_status": None,
            "status_version": None,
            "applied_at": None,
            "last_ok_at": None,
            "last_error_at": T0 - 1,
        },
    ]
    for key in ("level", "held_in_memory", "incidents_pending", "dropped_events"):
        assert key not in document


def test_stats_of_an_empty_outbox(cli: Callable[..., Run]) -> None:
    run = cli("stats")

    assert run.code == 0
    assert run.json == {
        "pending": 0,
        "sending": 0,
        "dead_unacked": 0,
        "incidents_unacked": 0,
        "oldest_pending_age_seconds": None,
        "auth_failing": False,
        "last_delivery_ok_at": None,
        "status": [],
    }


def test_stats_age_is_never_negative(
    cli: Callable[..., Run], db_path: Path, clock: FakeClock
) -> None:
    add_event(db_path, 1, "PENDING", created_at=T0 + 100)

    assert cli("stats").json["oldest_pending_age_seconds"] == 0.0


def test_stats_does_not_change_the_file(cli: Callable[..., Run], db_path: Path) -> None:
    add_event(db_path, 1, "PENDING")
    before = dump(db_path)
    mode = select(db_path, "PRAGMA journal_mode")

    assert cli("stats").code == 0

    assert dump(db_path) == before
    assert select(db_path, "PRAGMA journal_mode") == mode


# --- dead -------------------------------------------------------------------------------


def test_dead_default_limit_and_format(cli: Callable[..., Run], db_path: Path) -> None:
    for n in range(1, 53):
        add_event(db_path, n, "DEAD", dead_at=T0 + n, last_error_code="VALIDATION_ERROR")
    execute(db_path, "UPDATE usage_outbox SET dead_acked_at = ? WHERE event_id = ?", (T0, eid(1)))

    run = cli("dead")

    assert run.code == 0
    lines = run.lines
    assert len(lines) == 50
    assert all(list(line) == DEAD_KEYS for line in lines)
    assert lines[0] == {
        "event_id": eid(1),
        "last_error_code": "VALIDATION_ERROR",
        "attempts": 1,
        "dead_at": T0 + 1,
        "dead_acked_at": T0,
    }
    assert [line["event_id"] for line in lines] == [eid(n) for n in range(1, 51)]


def test_dead_with_limit(cli: Callable[..., Run], db_path: Path) -> None:
    for n in range(1, 4):
        add_event(db_path, n, "DEAD", dead_at=T0 + n)

    run = cli("dead", "--limit", "2")

    assert run.code == 0
    assert [line["event_id"] for line in run.lines] == [eid(1), eid(2)]


def test_dead_when_there_are_none(cli: Callable[..., Run]) -> None:
    run = cli("dead")

    assert run.code == 0
    assert run.out == ""


@pytest.mark.parametrize("limit", ["0", "-1", "abc", "1.5"])
@pytest.mark.parametrize("command", ["dead", "incidents"])
def test_limit_must_be_a_positive_integer(
    command: str, limit: str, cli: Callable[..., Run]
) -> None:
    assert_usage_error(cli(command, "--limit", limit))


# --- requeue 与 ack ---------------------------------------------------------------------


def test_requeue(cli: Callable[..., Run], db_path: Path, clock: FakeClock) -> None:
    add_event(db_path, 1, "DEAD", dead_at=T0, last_error_code="VALIDATION_ERROR")
    clock.now = T0 + 7

    run = cli("requeue", eid(1), "--reason", "  fixed upstream  ")

    assert run.code == 0
    assert run.err == ""
    assert run.json == {"event_id": eid(1), "ok": True}
    row = select(db_path, "SELECT * FROM usage_outbox")[0]
    assert row["status"] == "PENDING"
    assert row["attempts"] == 0
    assert row["next_attempt_at"] == T0 + 7
    assert row["last_error_code"] is None
    assert ops(db_path) == [
        {"action": "REQUEUE_DEAD", "target_id": eid(1), "reason": "fixed upstream", "at": T0 + 7}
    ]


@pytest.mark.parametrize("command", ["requeue", "ack"])
def test_requeue_and_ack_of_a_row_that_is_not_dead(
    command: str, cli: Callable[..., Run], db_path: Path
) -> None:
    add_event(db_path, 1, "PENDING")
    before = dump(db_path)

    for event_id in (eid(1), eid(99)):
        run = cli(command, event_id, "--reason", "r")
        assert run.code == 1
        assert run.err == ""
        assert run.json == {"event_id": event_id, "ok": False}

    assert dump(db_path) == before


def test_ack(cli: Callable[..., Run], db_path: Path, clock: FakeClock) -> None:
    add_event(db_path, 1, "DEAD", dead_at=T0)
    clock.now = T0 + 3

    run = cli("ack", eid(1), "--reason", "given up")

    assert run.code == 0
    assert run.json == {"event_id": eid(1), "ok": True}
    assert select(db_path, "SELECT dead_acked_at FROM usage_outbox") == [{"dead_acked_at": T0 + 3}]
    assert [row["action"] for row in ops(db_path)] == ["ACK_DEAD"]

    again = cli("ack", eid(1), "--reason", "given up")
    assert again.code == 1
    assert again.json == {"event_id": eid(1), "ok": False}
    assert len(ops(db_path)) == 1


@pytest.mark.parametrize("command", ["requeue", "ack"])
def test_requeue_and_ack_reason(command: str, cli: Callable[..., Run], db_path: Path) -> None:
    add_event(db_path, 1, "DEAD", dead_at=T0)
    before = dump(db_path)

    assert_usage_error(cli(command, eid(1)))
    assert_usage_error(cli(command, eid(1), "--reason", "   "))
    assert_usage_error(cli(command, eid(1), "--reason", "x" * 501))
    assert_usage_error(cli(command, "--reason", "r"))

    assert dump(db_path) == before
    assert cli(command, eid(1), "--reason", "x" * 500).code == 0


# --- replay -----------------------------------------------------------------------------


def test_replay(cli: Callable[..., Run], db_path: Path, clock: FakeClock) -> None:
    add_event(db_path, 1, "DELIVERED", delivered_at=T0 - 1)
    add_event(db_path, 2, "DELIVERED", delivered_at=T0 + 10)
    add_event(db_path, 3, "DELIVERED", delivered_at=T0 + 60)
    clock.now = T0 + 100

    run = cli(
        "replay",
        "--delivered-from",
        T0_ISO,
        # 同一时刻的另一种写法：+08:00。
        "--delivered-until",
        "2026-09-21T22:14:20+08:00",
        "--reason",
        "center restored",
    )

    assert run.code == 0
    assert run.json == {"replayed": 1}
    statuses = {r["event_id"]: r["status"] for r in select(db_path, "SELECT * FROM usage_outbox")}
    assert statuses == {eid(1): "DELIVERED", eid(2): "PENDING", eid(3): "DELIVERED"}
    assert ops(db_path) == [
        {
            "action": "REPLAY_DELIVERED",
            "target_id": eid(2),
            "reason": "center restored",
            "at": T0 + 100,
        }
    ]


def test_replay_of_nothing_succeeds(cli: Callable[..., Run]) -> None:
    run = cli(
        "replay",
        "--delivered-from",
        "2026-09-21T00:00:00Z",
        "--delivered-until",
        "2026-09-22T00:00:00Z",
        "--reason",
        "r",
    )

    assert run.code == 0
    assert run.json == {"replayed": 0}


@pytest.mark.parametrize(
    "args",
    [
        # 倒置与空时间段。
        ["--delivered-from", "2026-09-22T00:00:00Z", "--delivered-until", "2026-09-21T00:00:00Z"],
        ["--delivered-from", T0_ISO, "--delivered-until", T0_ISO],
        # 没有时区。
        ["--delivered-from", "2026-09-21T00:00:00", "--delivered-until", "2026-09-22T00:00:00Z"],
        ["--delivered-from", "2026-09-21T00:00:00Z", "--delivered-until", "2026-09-22"],
        ["--delivered-from", "yesterday", "--delivered-until", "2026-09-22T00:00:00Z"],
        ["--delivered-until", "2026-09-22T00:00:00Z"],
    ],
)
def test_replay_argument_errors(args: list[str], cli: Callable[..., Run], db_path: Path) -> None:
    add_event(db_path, 1, "DELIVERED", delivered_at=T0)
    before = dump(db_path)

    assert_usage_error(cli("replay", *args, "--reason", "r"))

    assert dump(db_path) == before


def test_replay_reason(cli: Callable[..., Run], db_path: Path) -> None:
    add_event(db_path, 1, "DELIVERED", delivered_at=T0)
    window = ["--delivered-from", "2026-09-21T00:00:00Z", "--delivered-until", "2026-09-22T00:00Z"]
    before = dump(db_path)

    assert_usage_error(cli("replay", *window))
    assert_usage_error(cli("replay", *window, "--reason", ""))

    assert dump(db_path) == before


# --- incidents 与 ack-incident ----------------------------------------------------------


def test_incidents(cli: Callable[..., Run], db_path: Path) -> None:
    first = add_incident(db_path, 1)
    second = add_incident(db_path, 2, acked_at=T0 + 9)
    add_incident(db_path, 3)

    run = cli("incidents")

    assert run.code == 0
    lines = run.lines
    assert len(lines) == 3
    assert all(list(line) == INCIDENT_KEYS for line in lines)
    assert lines[0] == {
        "incident_id": first,
        "kind": "DROPPED",
        "event_id": eid(1),
        "dropped_count": 1,
        "first_occurred_at": T0 - 20,
        "last_occurred_at": T0 - 10,
        "recorded_at": T0 + 1,
        "acked_at": None,
    }
    assert lines[1]["incident_id"] == second
    assert lines[1]["acked_at"] == T0 + 9

    limited = cli("incidents", "--limit", "1")
    assert [line["incident_id"] for line in limited.lines] == [first]


def test_ack_incident(cli: Callable[..., Run], db_path: Path, clock: FakeClock) -> None:
    incident = add_incident(db_path, 1)
    clock.now = T0 + 4

    run = cli("ack-incident", incident, "--reason", "reconciled by hand")

    assert run.code == 0
    assert run.json == {"incident_id": incident, "ok": True}
    row = select(db_path, "SELECT acked_at, ack_reason FROM incidents")[0]
    assert row == {"acked_at": T0 + 4, "ack_reason": "reconciled by hand"}
    assert ops(db_path) == [
        {
            "action": "ACK_INCIDENT",
            "target_id": incident,
            "reason": "reconciled by hand",
            "at": T0 + 4,
        }
    ]

    for target in (incident, "inc_missing"):
        again = cli("ack-incident", target, "--reason", "r")
        assert again.code == 1
        assert again.err == ""
        assert again.json == {"incident_id": target, "ok": False}
    assert len(ops(db_path)) == 1


def test_ack_incident_reason(cli: Callable[..., Run], db_path: Path) -> None:
    incident = add_incident(db_path, 1)
    before = dump(db_path)

    assert_usage_error(cli("ack-incident", incident))
    assert_usage_error(cli("ack-incident", incident, "--reason", " \t "))
    assert_usage_error(cli("ack-incident", "--reason", "r"))

    assert dump(db_path) == before


# --- reset-status -----------------------------------------------------------------------


def test_reset_status(cli: Callable[..., Run], db_path: Path, clock: FakeClock) -> None:
    add_status(db_path, TENANT, PROJECT)
    add_status(db_path, "ten_b", "prj_b")
    add_reconcile(db_path, TENANT, PROJECT, T0, None)
    clock.now = T0 + 2

    run = cli(
        "reset-status", "--tenant-id", "ten_b", "--project-id", "prj_b", "--reason", "restored"
    )

    assert run.code == 0
    assert run.json == {"tenant_id": "ten_b", "project_id": "prj_b", "ok": True}
    remaining = select(db_path, "SELECT tenant_id, project_id FROM status_state")
    assert remaining == [{"tenant_id": TENANT, "project_id": PROJECT}]
    # reconcile_state 不动。
    assert len(select(db_path, "SELECT * FROM reconcile_state")) == 1
    assert ops(db_path) == [
        {"action": "RESET_STATUS", "target_id": "ten_b/prj_b", "reason": "restored", "at": T0 + 2}
    ]


def test_reset_status_without_a_row(cli: Callable[..., Run], db_path: Path) -> None:
    # 这一对只在 reconcile_state 里，没有状态行。
    add_reconcile(db_path, TENANT, PROJECT, T0, None)
    before = dump(db_path)

    run = cli("reset-status", "--tenant-id", TENANT, "--project-id", PROJECT, "--reason", "r")

    assert run.code == 1
    assert run.err == ""
    assert run.json == {"tenant_id": TENANT, "project_id": PROJECT, "ok": False}
    assert dump(db_path) == before


def test_reset_status_argument_errors(cli: Callable[..., Run], db_path: Path) -> None:
    add_status(db_path, TENANT, PROJECT)
    before = dump(db_path)

    assert_usage_error(cli("reset-status", "--tenant-id", TENANT, "--project-id", PROJECT))
    assert_usage_error(
        cli("reset-status", "--tenant-id", TENANT, "--project-id", PROJECT, "--reason", "  ")
    )
    assert_usage_error(cli("reset-status", "--project-id", PROJECT, "--reason", "r"))
    assert_usage_error(cli("reset-status", "--tenant-id", TENANT, "--reason", "r"))

    assert dump(db_path) == before


# --- 参数错 -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["stats"],
        ["--outbox", "x.db"],
        ["--outbox", "x.db", "bogus"],
        ["--outbox", "x.db", "stats", "--extra"],
        ["--outbox", "x.db", "dead", "--lim", "2"],
    ],
)
def test_parse_errors_print_one_json_line(
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    code = main(argv, clock=FakeClock())

    captured = capsys.readouterr()
    run = Run(code, captured.out, captured.err)
    assert_usage_error(run)
    assert isinstance(run.error["message"], str)
    # 参数错不碰文件。
    assert list(tmp_path.iterdir()) == []


# --- 打不开的文件 -----------------------------------------------------------------------


def test_missing_file_is_not_created(cli: Callable[..., Run], tmp_path: Path) -> None:
    missing = tmp_path / "nothing.db"

    for args in (["stats"], ["requeue", eid(1), "--reason", "r"]):
        run = cli(*args, outbox=missing)
        assert run.code == 1
        assert run.out == ""
        assert run.error == {"error": "StoreMissingError"}

    # 不建文件，也不建 <path>.lock。
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith("nothing")] == []


def foreign_database(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE notes (body TEXT)")
        conn.execute("INSERT INTO notes VALUES ('secret note')")
        conn.commit()
    finally:
        conn.close()


def newer_database(path: Path) -> None:
    Store(path, clock=FakeClock()).close()
    execute(path, "UPDATE meta SET value = '2' WHERE key = 'schema_version'")


@pytest.mark.parametrize(
    ("prepare", "error"),
    [
        (lambda path: path.write_bytes(b""), "StoreMissingError"),
        (lambda path: path.write_bytes(random.Random(1).randbytes(4096)), "StoreForeignError"),
        (foreign_database, "StoreForeignError"),
        (newer_database, "StoreVersionError"),
    ],
    ids=["zero-bytes", "not-sqlite", "other-application", "newer-schema"],
)
def test_unusable_files_exit_1_and_are_not_changed(
    prepare: Callable[[Path], object], error: str, cli: Callable[..., Run], tmp_path: Path
) -> None:
    path = tmp_path / "other.db"
    prepare(path)
    content = path.read_bytes()

    run = cli("stats", outbox=path)

    assert run.code == 1
    assert run.out == ""
    assert run.error == {"error": error}
    assert "secret note" not in run.err
    assert path.read_bytes() == content
    for suffix in ("-wal", "-shm"):
        assert not Path(str(path) + suffix).exists()


# --- 读写错误退出 4 ---------------------------------------------------------------------


def test_open_failure_outside_the_store_errors_exits_4(
    cli: Callable[..., Run], db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def denied(self: _ProcessLock) -> None:
        raise PermissionError(errno.EPERM, "permission denied")

    monkeypatch.setattr(_ProcessLock, "acquire", denied)
    before = dump(db_path)

    run = cli("stats")

    assert run.code == 4
    assert run.out == ""
    assert run.error == {"error": "PermissionError"}
    assert dump(db_path) == before


@pytest.mark.parametrize(
    ("method", "args", "make_error"),
    [
        ("delivery_counts", ["stats"], lambda: sqlite_error("SQLITE_CORRUPT")),
        ("list_dead", ["dead"], lambda: OSError(errno.EIO, "I/O error")),
        ("list_incidents", ["incidents"], lambda: sqlite_error("SQLITE_IOERR")),
        (
            "requeue_dead",
            ["requeue", eid(1), "--reason", "r"],
            lambda: StoreReplacedError("gone"),
        ),
        ("ack_dead", ["ack", eid(1), "--reason", "r"], lambda: sqlite_error("SQLITE_FULL")),
        (
            "replay_delivered",
            [
                "replay",
                "--delivered-from",
                "2026-09-21T00:00:00Z",
                "--delivered-until",
                "2026-09-22T00:00:00Z",
                "--reason",
                "r",
            ],
            lambda: sqlite_error("SQLITE_CORRUPT"),
        ),
        (
            "ack_incident",
            ["ack-incident", "inc_x", "--reason", "r"],
            lambda: sqlite_error("SQLITE_READONLY"),
        ),
        (
            "reset_status",
            ["reset-status", "--tenant-id", "t", "--project-id", "p", "--reason", "r"],
            lambda: sqlite_error("SQLITE_IOERR"),
        ),
    ],
)
def test_read_write_errors_exit_4(
    method: str,
    args: list[str],
    make_error: Callable[[], BaseException],
    cli: Callable[..., Run],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing(self: Store, *a: Any, **k: Any) -> Any:
        raise make_error()

    monkeypatch.setattr(Store, method, failing)

    run = cli(*args)

    assert run.code == 4
    assert run.out == ""
    assert run.error == {"error": type(make_error()).__name__}


def test_store_is_closed_after_each_command(
    cli: Callable[..., Run], db_path: Path, clock: FakeClock
) -> None:
    assert cli("stats").code == 0
    assert cli("requeue", eid(1), "--reason", "r").code == 1

    # 单进程锁已释放。
    Store(db_path, clock=clock).close()


# --- 被锁的 outbox ----------------------------------------------------------------------


def _hold_client(path: str, ready: Any, release: Any) -> None:
    client = BillingClient(make_config(Path(path)), transport=OfflineTransport(), clock=FakeClock())
    ready.set()
    release.wait(60)
    client.close(timeout=2)


LOCKED_COMMANDS = [
    ["stats"],
    ["requeue", eid(1), "--reason", "fixed"],
    ["reset-status", "--tenant-id", TENANT, "--project-id", PROJECT, "--reason", "restored"],
]


def test_locked_outbox_exits_3(cli: Callable[..., Run], db_path: Path) -> None:
    add_event(db_path, 1, "DEAD", dead_at=T0)
    add_status(db_path, TENANT, PROJECT)
    context = multiprocessing.get_context("spawn")
    ready, release = context.Event(), context.Event()
    process = context.Process(target=_hold_client, args=(str(db_path), ready, release))
    process.start()
    try:
        assert ready.wait(60)
        before = dump(db_path)
        for args in LOCKED_COMMANDS:
            run = cli(*args)
            assert run.code == 3
            assert run.out == ""
            error = run.error
            assert error["error"] == "StoreLockedError"
            assert "BillingClient" in error["message"]
        assert dump(db_path) == before
    finally:
        release.set()
        process.join(timeout=60)
    assert process.exitcode == 0

    # 子进程退出后同样的命令正常执行。
    results = [cli(*args) for args in LOCKED_COMMANDS]
    assert [run.code for run in results] == [0, 0, 0]
    assert results[1].json == {"event_id": eid(1), "ok": True}
    assert results[2].json == {"tenant_id": TENANT, "project_id": PROJECT, "ok": True}
    assert [row["action"] for row in ops(db_path)] == ["REQUEUE_DEAD", "RESET_STATUS"]


# --- python -m --------------------------------------------------------------------------


def test_python_dash_m_runs_main(tmp_path: Path) -> None:
    package_root = Path(acuven_billing_client.__file__).resolve().parent.parent
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(package_root), *filter(None, [env.get("PYTHONPATH")])])
    missing = tmp_path / "nothing.db"

    result = subprocess.run(
        [sys.executable, "-m", "acuven_billing_client", "--outbox", str(missing), "stats"],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=60,
        check=False,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert json.loads(result.stderr.strip()) == {"error": "StoreMissingError"}
    assert not missing.exists()
