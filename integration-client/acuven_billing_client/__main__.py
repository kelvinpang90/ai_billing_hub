"""Operations command line (design gate #571 v24 §2「模块与公开接口」`__main__.py`).

AIH-TASK-170：`python -m acuven_billing_client --outbox <路径> <子命令>`，子命令 `stats`、
`dead`、`requeue`、`ack`、`replay`、`incidents`、`ack-incident`、`reset-status`。

- 只用于没有进程在用的文件：一律用 `Store.open_existing` 打开（不建表、不改日志模式，
  文件不存在时不建任何文件），只经 Store 的函数读写，不联网、不需要凭据。
- 成功的 JSON 写 stdout；异常引起的错误写一行 JSON 到 stderr。处置命令不论 `ok` 真假都把
  结果写 stdout（假时退出 1）。不输出 `level` 与进程内计数（命令行看不到运行中进程的内存）。
- 退出码：成功 0；目标不存在或状态不对、打不开（不存在、零字节、不是 SQLite、别的应用的库、
  版本更新）1；参数错 2；被运行中的进程锁住 3；打开或读写时的其他 SQLite / 文件错误 4。
- 错误 JSON 只含错误类别（与参数错的说明），不含文件内容。时刻都是 Unix 秒。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict
from datetime import datetime
from typing import Any, Final, NoReturn

from .metrics import read_outbox_metrics
from .store import Store, StoreError, StoreLockedError

DEFAULT_LIMIT: Final = 50
PROG: Final = "python -m acuven_billing_client"

EXIT_OK: Final = 0
EXIT_NOT_FOUND: Final = 1
EXIT_USAGE: Final = 2
EXIT_LOCKED: Final = 3
EXIT_STORE_ERROR: Final = 4

LOCKED_HINT: Final = (
    "outbox is in use by a running process; use the BillingClient methods in that process"
)

# `stats` 里每一对租户 × 项目输出的列。
_STATUS_COLUMNS: Final = (
    "tenant_id",
    "project_id",
    "effective_status",
    "status_version",
    "applied_at",
    "last_ok_at",
    "last_error_at",
)
_DEAD_COLUMNS: Final = ("event_id", "last_error_code", "attempts", "dead_at", "dead_acked_at")
_INCIDENT_COLUMNS: Final = (
    "incident_id",
    "kind",
    "event_id",
    "dropped_count",
    "first_occurred_at",
    "last_occurred_at",
    "recorded_at",
    "acked_at",
)


class _UsageError(Exception):
    """Argument parsing failed; reported as one JSON line on stderr, exit 2."""


class _Parser(argparse.ArgumentParser):
    # 不输出 argparse 的用法文本，改由 main 写一行 JSON。
    def error(self, message: str) -> NoReturn:
        raise _UsageError(message)


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive integer") from None
    if value < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def _aware_time(text: str) -> float:
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError("must be an ISO 8601 time with a timezone") from None
    if value.utcoffset() is None:
        raise argparse.ArgumentTypeError("must be an ISO 8601 time with a timezone")
    try:
        return value.timestamp()
    except (OverflowError, ValueError):
        raise argparse.ArgumentTypeError("time is out of range") from None


def _build_parser() -> _Parser:
    parser = _Parser(prog=PROG, allow_abbrev=False)
    parser.add_argument("--outbox", required=True, help="path of the outbox SQLite file")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("stats", allow_abbrev=False)
    dead = commands.add_parser("dead", allow_abbrev=False)
    dead.add_argument("--limit", type=_positive_int, default=DEFAULT_LIMIT)
    for name in ("requeue", "ack"):
        sub = commands.add_parser(name, allow_abbrev=False)
        sub.add_argument("event_id")
        sub.add_argument("--reason", required=True)
    replay = commands.add_parser("replay", allow_abbrev=False)
    replay.add_argument("--delivered-from", type=_aware_time, required=True)
    replay.add_argument("--delivered-until", type=_aware_time, required=True)
    replay.add_argument("--reason", required=True)
    incidents = commands.add_parser("incidents", allow_abbrev=False)
    incidents.add_argument("--limit", type=_positive_int, default=DEFAULT_LIMIT)
    ack_incident = commands.add_parser("ack-incident", allow_abbrev=False)
    ack_incident.add_argument("incident_id")
    ack_incident.add_argument("--reason", required=True)
    reset = commands.add_parser("reset-status", allow_abbrev=False)
    reset.add_argument("--tenant-id", required=True)
    reset.add_argument("--project-id", required=True)
    reset.add_argument("--reason", required=True)
    return parser


# --- 输出 -------------------------------------------------------------------------------


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def _out(value: Any) -> None:
    sys.stdout.write(_dumps(value) + "\n")


def _err(error: str, message: str | None = None) -> None:
    document: dict[str, str] = {"error": error}
    if message is not None:
        document["message"] = message
    sys.stderr.write(_dumps(document) + "\n")


def _pick(row: Any, columns: Sequence[str]) -> dict[str, Any]:
    values = asdict(row)
    return {name: values[name] for name in columns}


# --- 子命令 -----------------------------------------------------------------------------
# 读写时抛的 sqlite3.Error、OSError 与 StoreError 由 main 统一按退出码 4 处理；
# ValueError 只在处置命令里按参数错处理（原因不合规、时间段倒置）。


def _stats(store: Store, args: argparse.Namespace, now: float) -> int:
    document = read_outbox_metrics(store, now=now)
    # 只在 reconcile_state 出现过的那一对，状态列为 None。
    document["status"] = [_pick(pair, _STATUS_COLUMNS) for pair in store.list_status_pairs()]
    _out(document)
    return EXIT_OK


def _dead(store: Store, args: argparse.Namespace, now: float) -> int:
    for row in store.list_dead(args.limit):
        _out(_pick(row, _DEAD_COLUMNS))
    return EXIT_OK


def _incidents(store: Store, args: argparse.Namespace, now: float) -> int:
    for row in store.list_incidents(args.limit):
        _out(_pick(row, _INCIDENT_COLUMNS))
    return EXIT_OK


def _result(document: dict[str, Any]) -> int:
    _out(document)
    return EXIT_OK if document["ok"] else EXIT_NOT_FOUND


def _requeue(store: Store, args: argparse.Namespace, now: float) -> int:
    ok = store.requeue_dead(args.event_id, reason=args.reason)
    return _result({"event_id": args.event_id, "ok": ok})


def _ack(store: Store, args: argparse.Namespace, now: float) -> int:
    ok = store.ack_dead(args.event_id, reason=args.reason)
    return _result({"event_id": args.event_id, "ok": ok})


def _replay(store: Store, args: argparse.Namespace, now: float) -> int:
    replayed = store.replay_delivered(
        delivered_from=args.delivered_from,
        delivered_until=args.delivered_until,
        reason=args.reason,
    )
    _out({"replayed": replayed})
    return EXIT_OK


def _ack_incident(store: Store, args: argparse.Namespace, now: float) -> int:
    ok = store.ack_incident(args.incident_id, reason=args.reason)
    return _result({"incident_id": args.incident_id, "ok": ok})


def _reset_status(store: Store, args: argparse.Namespace, now: float) -> int:
    ok = store.reset_status(args.tenant_id, args.project_id, reason=args.reason)
    return _result({"tenant_id": args.tenant_id, "project_id": args.project_id, "ok": ok})


_Command = Callable[[Store, argparse.Namespace, float], int]

_READ_COMMANDS: Final[dict[str, _Command]] = {
    "stats": _stats,
    "dead": _dead,
    "incidents": _incidents,
}
_WRITE_COMMANDS: Final[dict[str, _Command]] = {
    "requeue": _requeue,
    "ack": _ack,
    "replay": _replay,
    "ack-incident": _ack_incident,
    "reset-status": _reset_status,
}


# --- 入口 -------------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None, *, clock: Callable[[], float] | None = None) -> int:
    """Run one command; returns the exit code. `clock` defaults to `time.time`."""
    tick: Callable[[], float] = clock if clock is not None else time.time
    try:
        args = _build_parser().parse_args(argv)
    except _UsageError as exc:
        _err("usage", str(exc))
        return EXIT_USAGE

    try:
        store = Store.open_existing(args.outbox, clock=tick)
    except StoreLockedError:
        _err("StoreLockedError", LOCKED_HINT)
        return EXIT_LOCKED
    except StoreError as exc:
        # 不存在、零字节、不是 SQLite、别的应用的库、版本更新：不建也不改文件。
        _err(type(exc).__name__)
        return EXIT_NOT_FOUND
    except (sqlite3.Error, OSError) as exc:
        _err(type(exc).__name__)
        return EXIT_STORE_ERROR

    try:
        if args.command in _WRITE_COMMANDS:
            try:
                return _WRITE_COMMANDS[args.command](store, args, float(tick()))
            except ValueError as exc:
                _err("usage", str(exc))
                return EXIT_USAGE
        return _READ_COMMANDS[args.command](store, args, float(tick()))
    except (sqlite3.Error, OSError, StoreError) as exc:
        _err(type(exc).__name__)
        return EXIT_STORE_ERROR
    finally:
        try:
            store.close()
        except (sqlite3.Error, OSError):
            pass


if __name__ == "__main__":
    sys.exit(main())
