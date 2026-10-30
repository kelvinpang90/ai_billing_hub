"""用量计费单事件耗时剖析：每条事件锁外约 85 毫秒花在哪里（AIH-TASK-055）。

只做测量，承接 docs/perf-baseline.md 10.5：2026-10-09 那一轮 2 个 worker 合计每秒只处理约 21 条，
每条约 95 ms，其中持钱包锁只占 9–10 ms。按 10.2 的规则要先找出锁外的瓶颈，再决定是否做方案 C。

负载：单个租户预先积压 `--events` 条（默认 2000），依次在 `--workers` 的每一种配置下（默认 1 与 2）
各清空一轮，每轮自己的模型、租户与积压。worker 是子进程，派发节奏与 perf_usage.py 相同（每 10 秒派
`USAGE_BILLING_SWEEP_PARALLELISM` 个扫描，与生产 Beat 相同；8 秒预算、每批 20 条）；每个扫描是
一次 `bill_pending_events(..., parallelism=USAGE_BILLING_SWEEP_PARALLELISM)`，与生产任务一样
先抢扫描名额。`--continuous` 不等 Beat，任何 worker 一空闲就派下一次 —— 用来把
「Beat 节奏造成的空转」与「单条处理成本」分开。

每条事件在 worker 子进程里记：`process_event` 的墙钟、进程 CPU 时间、SQL 条数与数据库耗时、等钱包锁
与持锁时间、计价（`resolve_charge_inputs` / `price_resolved`）与记账（`post_transaction`）耗时；
每次 `claim_events` 另记耗时与 SQL 条数。包装只记时刻与计数，返回值与异常原样交回，不改 app/。

⚠️ 护栏与造数据**全部复用** scripts/perf_usage.py（按文件加载，与 tests/backend/test_perf_usage.py
相同），不复制一份：`--database-url` 必填、库名只能是 `billing_perf`、URL 比对 + 连上后比
`@@server_uuid` 与 `DATABASE()`、生产环境只允许 `billing_perf`。数据经服务函数与 `ingest_one` 产生，
每轮唯一前缀，**不删除任何行**，结束时逐项打印造了哪些数据。

⚠️ 每轮开始前要求 `billing_perf` 里**没有任何**非终态的用量事件：\
worker 认领不分租户、按接收先后处理，
残留积压会混进测量（10.5 的复核）。有就报错退出、不开始负载。

用法（`scripts/` 不在镜像里，要挂进去；命令见 docs/perf-baseline.md 10.6）：

    docker compose run --rm \\
      -v "$PWD/scripts:/srv/billing/scripts:ro" \\
      api python scripts/perf_usage_profile.py --database-url "$PERF_URL"
"""

from __future__ import annotations

import argparse
import functools
import importlib.util
import json
import logging
import math
import os
import platform
import sys
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from multiprocessing.connection import Connection
from pathlib import Path

from sqlalchemy import event, func, select, text

from app.core.celery_app import USAGE_BILLING_SWEEP_PARALLELISM, USAGE_BILLING_SWEEP_SECONDS
from app.core.config import get_settings
from app.core.database import create_database_engine, create_session_factory
from app.models.usage import UsageEvent, UsageEventStatus
from app.models.wallet import WalletTransaction
from app.repositories import wallet as wallet_repository
from app.services import usage_billing

PERF_USAGE_SCRIPT = Path(__file__).resolve().with_name("perf_usage.py")


def _load_perf_usage():
    """按文件加载 scripts/perf_usage.py（`scripts/` 不是包）。已加载过同一个文件就复用。"""
    loaded = sys.modules.get("perf_usage")
    if loaded is not None and Path(getattr(loaded, "__file__", "")).resolve() == PERF_USAGE_SCRIPT:
        return loaded
    spec = importlib.util.spec_from_file_location("perf_usage", PERF_USAGE_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # 先登记再 exec：模块里的 `@dataclass` 会反查 `sys.modules[cls.__module__]`。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


perf = _load_perf_usage()

DEFAULT_EVENTS = 2000
DEFAULT_WORKERS = (1, 2)

# 四类占比（报告最后一段）。
DB = "数据库往返"
CPU = "CPU 或节流"
IDLE = "Beat 节奏空转"
LOCK = "锁"
CATEGORIES = (DB, CPU, IDLE, LOCK)

VALID = "VALID"
INVALID = "INVALID"

# cgroup v2；文件不存在（cgroup v1、非 Linux）时报告「不可用」。
CPU_STAT_PATH = Path("/sys/fs/cgroup/cpu.stat")
THROTTLE_KEYS = ("nr_throttled", "throttled_usec")
UNAVAILABLE = "不可用"

MYSQL_COUNTERS = ("Questions", "Innodb_row_lock_waits", "Innodb_row_lock_time")


# --------------------------------------------------------------------------
# 统计（纯函数，tests/backend/test_perf_usage_profile.py 测的就是这一段）
# --------------------------------------------------------------------------


def summarize_ms(values: Sequence[float]) -> dict[str, float] | None:
    """秒 → 毫秒的 平均 / p50 / p95（百分位用 perf_usage.py 的最近秩法）。没有样本返回 None。"""
    if not values:
        return None
    return {
        "n": len(values),
        "mean_ms": sum(values) / len(values) * 1000,
        "p50_ms": perf.percentile(values, 0.50) * 1000,
        "p95_ms": perf.percentile(values, 0.95) * 1000,
    }


def ratio(part: float, whole: float) -> float | None:
    return part / whole if whole > 0 else None


def breakdown(
    *,
    window_seconds: float,
    workers: int,
    busy_seconds: float,
    lock_seconds: float,
    db_seconds: float,
) -> dict[str, object]:
    """把 worker 的全部可用时间（清空用时 × worker 数）切成互不重叠的四类。

    - 锁：调用 `_lock_wallet` → `process_event` 返回（等锁 + 持锁；锁内的 SQL 与提交也算这里）
    - 数据库往返：锁外的 SQL 耗时（认领、事件行锁、目录 / 价格 / 规则读取等）
    - CPU 或节流：忙碌时间里剩下的部分（Python 本身、被 cgroup 节流、调度）
    - Beat 节奏空转：不在忙的时间（等下一次 Beat）

    测量噪声可能让 忙碌 > 可用：可用时间取二者较大者；锁与数据库各自夹在忙碌之内。
    """
    capacity = max(window_seconds * workers, busy_seconds)
    busy = max(busy_seconds, 0.0)
    lock = min(max(lock_seconds, 0.0), busy)
    db = min(max(db_seconds, 0.0), busy - lock)
    seconds = {DB: db, CPU: busy - lock - db, IDLE: capacity - busy, LOCK: lock}
    shares = {name: (value / capacity if capacity > 0 else 0.0) for name, value in seconds.items()}
    largest = max(CATEGORIES, key=lambda name: shares[name]) if capacity > 0 else None
    return {"seconds": seconds, "shares": shares, "largest": largest}


def stage_table(samples: Sequence[dict], claims: Sequence[dict]) -> dict[str, object]:
    """逐阶段的 平均 / p50 / p95，只统计走到 PROCESSED 的事件（其余是错误路径）。"""
    done = [s for s in samples if s["outcome"] == usage_billing.Outcome.PROCESSED.value]
    locked = [s for s in done if s["lock_hold"] is not None]

    def column(key: str, rows: Sequence[dict] = done) -> list[float]:
        return [row[key] for row in rows if row[key] is not None]

    wall = sum(column("wall"))
    stages = {
        "process_event 墙钟": summarize_ms(column("wall")),
        "CPU 时间": summarize_ms(column("cpu")),
        "数据库耗时（SQL 合计）": summarize_ms(column("sql_seconds")),
        "锁外的数据库耗时": summarize_ms([row["sql_seconds"] - row["sql_in_lock"] for row in done]),
        "等钱包锁": summarize_ms(column("lock_wait", locked)),
        "持钱包锁": summarize_ms(column("lock_hold", locked)),
        "锁外（墙钟 − 持锁）": summarize_ms([row["wall"] - row["lock_hold"] for row in locked]),
        "计价 resolve_charge_inputs": summarize_ms(column("resolve")),
        "计价 price_resolved": summarize_ms(column("price")),
        "记账 post_transaction（含等锁）": summarize_ms(column("posting")),
        "claim_events（每次）": summarize_ms([claim["wall"] for claim in claims]),
    }
    sql_counts = [float(row["sql_count"]) for row in done]
    claim_sql = [float(claim["sql_count"]) for claim in claims]
    return {
        "events": len(done),
        "stages": stages,
        "sql_per_event": _plain(sql_counts),
        "sql_per_claim": _plain(claim_sql),
        "claims": len(claims),
        "empty_claims": sum(1 for claim in claims if claim["claimed"] == 0),
        "db_share_of_wall": ratio(sum(column("sql_seconds")), wall),
        "cpu_share_of_wall": ratio(sum(column("cpu")), wall),
    }


def _plain(values: Sequence[float]) -> dict[str, float] | None:
    """不换算单位的 平均 / p50 / p95（SQL 条数）。"""
    if not values:
        return None
    return {
        "mean": sum(values) / len(values),
        "p50": perf.percentile(values, 0.50),
        "p95": perf.percentile(values, 0.95),
    }


def parse_cpu_stat(content: str) -> dict[str, int]:
    """cgroup v2 的 `cpu.stat`：每行「键 整数」。不认识的行跳过。"""
    values: dict[str, int] = {}
    for line in content.splitlines():
        parts = line.split()
        if len(parts) == 2:
            try:
                values[parts[0]] = int(parts[1])
            except ValueError:
                continue
    return values


def read_throttle(path: Path = CPU_STAT_PATH) -> dict[str, int] | None:
    "`nr_throttled` 与 `throttled_usec`；文件不存在、读不了或缺键时返回 None（报告「不可用」）。"
    try:
        content = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    values = parse_cpu_stat(content)
    if not all(key in values for key in THROTTLE_KEYS):
        return None
    return {key: values[key] for key in THROTTLE_KEYS}


def counter_delta(
    before: dict[str, int] | None, after: dict[str, int] | None
) -> dict[str, int] | None:
    if before is None or after is None:
        return None
    return {key: after[key] - before[key] for key in before if key in after}


# --------------------------------------------------------------------------
# 参数与护栏
# --------------------------------------------------------------------------


def _worker_counts(value: str) -> tuple[int, ...]:
    try:
        counts = tuple(dict.fromkeys(int(part) for part in value.split(",") if part.strip()))
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            f"workers are a comma list of integers: {value}"
        ) from error
    if not counts or any(count <= 0 for count in counts):
        raise argparse.ArgumentTypeError(f"workers are a comma list of positive integers: {value}")
    return counts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python scripts/perf_usage_profile.py")
    parser.add_argument(
        "--database-url",
        default="",
        help=(
            f"测量专用的库。⚠️ 必填，库名必须是 {perf.PERF_DATABASE}，不回落到 BILLING_DATABASE_URL"
        ),
    )
    parser.add_argument(
        "--events", type=perf._positive_int, default=DEFAULT_EVENTS, help="每轮预先积压的条数"
    )
    parser.add_argument(
        "--workers",
        type=_worker_counts,
        default=DEFAULT_WORKERS,
        help="依次跑的 worker 进程数，逗号分隔（默认 1,2；每种一轮）",
    )
    parser.add_argument(
        "--continuous",
        action="store_true",
        help="不等 Beat：扫描结束立即派下一次（把 Beat 节奏的空转与单条成本分开）",
    )
    parser.add_argument("--ingest-threads", type=perf._positive_int, default=8)
    parser.add_argument(
        "--drain-timeout", type=perf._positive_float, default=900.0, help="每轮等清空的上限（秒）"
    )
    parser.add_argument(
        "--json-out", default=None, help="把结果另存成 JSON（负载开始前先确认可写）"
    )
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def check_json_out(path: str | None) -> None:
    """`--json-out` 写不进去就在负载之前退出（10.5 那一轮跑完才在最后一步 PermissionError）。

    以追加方式打开：不截断已有的文件，只确认能写；真正的结果最后整份覆盖。
    """
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8"):
            pass
    except OSError as error:
        raise SystemExit(
            f"--json-out {path!r} is not writable ({type(error).__name__}: {error}); "
            "nothing was measured. In a one-off container the user must be able to write "
            "the mounted directory (docs/perf-baseline.md 10.6)."
        ) from error


def guarded_settings(raw_url: str, stack_settings):
    """perf_usage.py 的两道不连库的护栏，原样调用。连上之后的那一道见 `main`。"""
    perf._check_environment(stack_settings, raw_url)
    return perf._measurement_settings(raw_url, stack_settings)


def unsettled_anywhere(factory) -> int:
    """`billing_perf` 里**所有租户**还没走到终态的用量事件数。"""
    statement = (
        select(func.count())
        .select_from(UsageEvent)
        .where(UsageEvent.status.not_in(perf.TERMINAL_STATUSES))
    )
    with factory() as session:
        return int(session.execute(statement).scalar_one())


class DirtyDatabase(RuntimeError):
    """库里有非终态的用量事件：这时开始负载，量到的会混进别人的积压。"""


def require_clean(factory) -> None:
    count = unsettled_anywhere(factory)
    if count:
        raise DirtyDatabase(
            f"{perf.PERF_DATABASE} 里有 {count} 条非终态的用量事件（RECEIVED / PROCESSING / "
            "FAILED_RETRYABLE）：worker 会跨租户按接收先后处理它们，测量会混进去。"
            "不开始负载；处置办法见 docs/perf-baseline.md 10.6"
        )


def mysql_counters(factory) -> dict[str, int] | None:
    names = ", ".join(f"'{name}'" for name in MYSQL_COUNTERS)
    try:
        with factory() as session:
            rows = session.execute(
                text(f"SHOW GLOBAL STATUS WHERE Variable_name IN ({names})")
            ).all()
    except Exception:  # noqa: BLE001 —— 量不到就报告不可用，不影响测量本身
        return None
    values = {str(name): int(value) for name, value in rows}
    return values if all(name in values for name in MYSQL_COUNTERS) else None


# --------------------------------------------------------------------------
# worker 进程：包装只记时刻与计数，返回值与异常原样交回
# --------------------------------------------------------------------------


# 当前这一次 claim / process_event / 回收里累计的计数（每次开始前清零）。
_PROBE: dict[str, float] = {}


def _reset_probe() -> None:
    _PROBE.clear()
    _PROBE.update(
        sql_count=0,
        sql_seconds=0.0,
        sql_in_lock=0.0,
        resolve=0.0,
        price=0.0,
        posting=0.0,
    )


def _instrument_sql(engine) -> None:
    """SQLAlchemy 引擎事件：每条语句的条数与耗时。拿到钱包锁调用之后的语句另记一份（锁内）。"""

    def before(conn, _cursor, _statement, _parameters, _context, _executemany):
        conn.info["profile_started"] = time.perf_counter()

    def after(conn, _cursor, _statement, _parameters, _context, _executemany):
        started = conn.info.pop("profile_started", None)
        if started is None:
            return
        elapsed = time.perf_counter() - started
        _PROBE["sql_count"] = _PROBE.get("sql_count", 0) + 1
        _PROBE["sql_seconds"] = _PROBE.get("sql_seconds", 0.0) + elapsed
        if "lock_called" in _PROBE:
            _PROBE["sql_in_lock"] = _PROBE.get("sql_in_lock", 0.0) + elapsed

    event.listen(engine, "before_cursor_execute", before)
    event.listen(engine, "after_cursor_execute", after)


def _timed(key: str, original):
    @functools.wraps(original)
    def wrapper(*args, **kwargs):
        started = time.perf_counter()
        try:
            return original(*args, **kwargs)
        finally:
            _PROBE[key] = _PROBE.get(key, 0.0) + time.perf_counter() - started

    return wrapper


def _instrument_billing() -> None:
    """按模块全局名包装：`post_transaction` 调 `_lock_wallet`、`_process` 调计价与记账，都经全局名。

    钱包锁照 perf_usage.py 的 `_instrument_wallet_lock`，另记调用前的时刻（等锁 = 拿到 − 调用）。
    """
    original_lock = wallet_repository._lock_wallet

    def timed_lock(session, tenant_id):
        _PROBE["lock_called"] = time.perf_counter()
        wallet = original_lock(session, tenant_id)
        _PROBE["lock_acquired"] = time.perf_counter()
        return wallet

    wallet_repository._lock_wallet = timed_lock  # type: ignore[assignment]
    usage_billing.resolve_charge_inputs = _timed(  # type: ignore[assignment]
        "resolve", usage_billing.resolve_charge_inputs
    )
    usage_billing.price_resolved = _timed(  # type: ignore[assignment]
        "price", usage_billing.price_resolved
    )
    usage_billing.post_transaction = _timed(  # type: ignore[assignment]
        "posting", usage_billing.post_transaction
    )


def _probe_snapshot(wall: float) -> dict[str, object]:
    return {
        "finished": time.time(),
        "wall": wall,
        "sql_count": int(_PROBE.get("sql_count", 0)),
        "sql_seconds": _PROBE.get("sql_seconds", 0.0),
    }


def _profile_sweep(factory, connection: Connection, *, lease: int, attempts: int) -> None:
    """一次 `usage_billing.bill_pending_events`，与生产任务一样传
    `parallelism=USAGE_BILLING_SWEEP_PARALLELISM`，逐次认领、逐条事件记各阶段。

    这一次调用期间把 `usage_billing` 的 `claim_events` / `process_event` 换成记录用的包装（它的循环
    按模块全局名调用这两个函数），返回值原样交回，结束后换回。每批认领之前把上一批送回主进程。
    抢名额与确认名额的语句不计入任何一次认领或事件（每条在 1 ms 以内）；抢不到名额的扫描不认领，
    什么也不送回。
    """
    original_claim, original_process = usage_billing.claim_events, usage_billing.process_event
    batch: list[tuple[dict, list[dict]]] = []

    def flush() -> None:
        if batch:
            claim, rows = batch.pop()
            connection.send(("batch", claim, rows))

    def claim_events(*args, **kwargs):
        flush()
        _reset_probe()
        begun = time.perf_counter()
        claims = original_claim(*args, **kwargs)
        batch.append(({**_probe_snapshot(time.perf_counter() - begun), "claimed": len(claims)}, []))
        return claims

    def process_event(factory, item, **kwargs):
        _reset_probe()
        cpu_before = time.process_time()
        begun = time.perf_counter()
        outcome = original_process(factory, item, **kwargs)
        done = time.perf_counter()
        cpu = time.process_time() - cpu_before
        called = _PROBE.get("lock_called")
        acquired = _PROBE.get("lock_acquired")
        batch[-1][1].append(
            {
                **_probe_snapshot(done - begun),
                "event_pk": item.id,
                "outcome": outcome.value,
                "cpu": cpu,
                "sql_in_lock": _PROBE.get("sql_in_lock", 0.0),
                "lock_wait": acquired - called if called and acquired else None,
                "lock_hold": done - acquired if acquired else None,
                "lock_window": done - called if called else None,
                "resolve": _PROBE.get("resolve", 0.0),
                "price": _PROBE.get("price", 0.0),
                "posting": _PROBE.get("posting", 0.0),
            }
        )
        return outcome

    usage_billing.claim_events = claim_events  # type: ignore[assignment]
    usage_billing.process_event = process_event  # type: ignore[assignment]
    try:
        usage_billing.bill_pending_events(
            factory,
            lease_seconds=lease,
            max_attempts=attempts,
            parallelism=USAGE_BILLING_SWEEP_PARALLELISM,
        )
    finally:
        usage_billing.claim_events = original_claim  # type: ignore[assignment]
        usage_billing.process_event = original_process  # type: ignore[assignment]
    flush()


def _profile_worker_main(database_url: str, connection: Connection, lease: int, attempts: int):
    """一个计费 worker 进程：装好包装、回一声 ready，然后收主进程派来的任务。"""
    logging.basicConfig(level=logging.ERROR)
    settings = get_settings().model_copy(update={"database_url": database_url})
    engine = create_database_engine(settings)
    factory = create_session_factory(engine)
    _instrument_sql(engine)
    _instrument_billing()
    connection.send(("ready",))
    while True:
        message = connection.recv()
        if message[0] == "stop":
            return
        if message[0] == "sweep":
            _profile_sweep(factory, connection, lease=lease, attempts=attempts)
        elif message[0] == "recover":
            _reset_probe()
            begun = time.perf_counter()
            recovered = usage_billing.recover_stale_processing(factory, max_attempts=attempts)
            snapshot = _probe_snapshot(time.perf_counter() - begun)
            connection.send(("recover", {**snapshot, "recovered": recovered}))
        connection.send(("idle",))


class ProfilePool(perf.WorkerPool):
    """perf_usage.py 的 WorkerPool，换成剖析用的子进程；派发节奏不变，另有 `continuous`。

    ⚠️ 等所有子进程回了 ready 才开始派发：进程启动与 import 的几秒不算进清空用时。
    """

    def __init__(self, settings, size: int, *, continuous: bool) -> None:
        super().__init__(settings, size)
        self.continuous = continuous
        self.samples: list[dict] = []
        self.claims: list[dict] = []
        self.recoveries: list[dict] = []
        self.started: float | None = None

    def _spawn(self):
        parent, child = self.context.Pipe()
        process = self.context.Process(
            target=_profile_worker_main,
            args=(
                self.settings.database_url,
                child,
                self.settings.usage_lease_seconds,
                self.settings.usage_max_attempts,
            ),
            daemon=True,
        )
        process.start()
        child.close()
        return perf._Worker(process, parent)

    def start(self) -> None:
        self.workers = [self._spawn() for _ in range(self.size)]
        for worker in self.workers:
            if not worker.connection.poll(120):
                raise SystemExit("a profiling worker did not start within 120 seconds")
            message = worker.connection.recv()
            if message[0] != "ready":
                raise SystemExit(f"unexpected first message from a worker: {message[0]!r}")
        self.started = time.time()
        self._thread = threading.Thread(target=self._run, name="beat", daemon=True)
        self._thread.start()

    def _handle(self, worker) -> None:
        try:
            message = worker.connection.recv()
        except (EOFError, OSError):
            self._lost(worker)
            return
        kind = message[0]
        if kind == "batch":
            self.claims.append(message[1])
            for row in message[2]:
                self.samples.append(row)
                self.outcomes[row["outcome"]] += 1
                if row["outcome"] == usage_billing.Outcome.PROCESSED.value:
                    self.finished[row["event_pk"]] = row["finished"]
        elif kind == "recover":
            self.recoveries.append(message[1])
            self.recovered += message[1]["recovered"]
        elif kind == "idle":
            worker.busy = False
        # "ready"：中途补的进程报到，无事可做。

    def _dispatch(self, now: float) -> None:
        """Beat 节奏照 perf_usage.py（每 10 秒 N 个扫描）；`continuous` 时只换掉扫描的派发。

        派发循环本身（过期丢弃、交给空闲 worker、收结果）是 perf_usage.py 的 `_run`。
        """
        if self.continuous:
            # 不等 Beat：每个空闲的 worker 都立刻有一次扫描可拿。
            idle = sum(1 for worker in self.workers if not worker.busy)
            queued = sum(1 for kind, _expires in self._pending if kind == "sweep")
            for _ in range(idle - queued):
                self._pending.append(("sweep", math.inf))
        else:
            self._beat_sweeps(now)
        self._beat_recovery(now)


# --------------------------------------------------------------------------
# 一轮
# --------------------------------------------------------------------------


@dataclass
class RoundResult:
    label: str
    workers: int
    status: str = INVALID
    reasons: list[str] = field(default_factory=list)
    measured: dict[str, object] = field(default_factory=dict)


def summarize_round(pool: ProfilePool, workers: int, drain_end: float) -> dict[str, object]:
    """清空窗口（第一次派发 → 最后一条事件返回）内的全部 worker 活动。"""
    assert pool.started is not None
    window = drain_end - pool.started

    def inside(rows: Sequence[dict]) -> list[dict]:
        return [row for row in rows if row["finished"] <= drain_end]

    samples, claims, recoveries = (
        inside(pool.samples),
        inside(pool.claims),
        inside(pool.recoveries),
    )
    busy = sum(row["wall"] for row in (*samples, *claims, *recoveries))
    lock = sum(row["lock_window"] for row in samples if row["lock_window"] is not None)
    db_outside = sum(row["sql_seconds"] - row["sql_in_lock"] for row in samples) + sum(
        row["sql_seconds"] for row in (*claims, *recoveries)
    )
    return {
        "drain_seconds": window,
        "busy_seconds": busy,
        "busy_share": ratio(busy, window * workers),
        "table": stage_table(samples, claims),
        "categories": breakdown(
            window_seconds=window,
            workers=workers,
            busy_seconds=busy,
            lock_seconds=lock,
            db_seconds=db_outside,
        ),
        "recoveries": len(recoveries),
    }


def run_round(world, ingestor, settings, args, workers: int) -> RoundResult:
    label = f"w{workers}"
    result = RoundResult(label, workers)
    require_clean(world.factory)
    model = world.model(label)
    tenant = world.tenant(label, "hot")
    perf._settle_wait()
    ingest_seconds = ingestor.burst(tenant, model, args.events)
    ids = [tenant.tenant_id]
    before = perf.status_counts(world.factory, ids)
    backlog = before.get(UsageEventStatus.RECEIVED.value, 0)
    result.measured.update({"ingest_seconds": ingest_seconds, "statuses_before_drain": before})

    pool = ProfilePool(settings, workers, continuous=args.continuous)
    throttle_before = read_throttle()
    mysql_before = mysql_counters(world.factory)
    pool.start()
    waited = perf.wait_until_settled(world.factory, ids, args.drain_timeout)
    pool.stop()
    throttle_after = read_throttle()
    mysql_after = mysql_counters(world.factory)

    statuses = perf.status_counts(world.factory, ids)
    processed = statuses.get(UsageEventStatus.PROCESSED.value, 0)
    result.measured.update(
        {
            "backlog": backlog,
            "statuses": statuses,
            "worker_outcomes": dict(pool.outcomes),
            "dropped_beat_tasks": pool.dropped_tasks,
            "cpu_throttle": counter_delta(throttle_before, throttle_after),
            "mysql_status": counter_delta(mysql_before, mysql_after),
        }
    )
    if backlog < args.events:
        result.reasons.append(f"积压只造出 {backlog} / {args.events} 条")
    if waited is None or perf._unsettled(world.factory, ids):
        result.reasons.append("本轮的事件没有在超时内全部走到终态")
    if processed < backlog:
        result.reasons.append(f"只有 {processed} / {backlog} 条走到 PROCESSED，见 statuses")
    if pool.samples and pool.finished:
        drain_end = max(pool.finished.values())
        summary = summarize_round(pool, workers, drain_end)
        drain = summary["drain_seconds"]
        summary["events_per_second"] = processed / drain if drain > 0 else None
        result.measured.update(summary)
    else:
        result.reasons.append("一条事件也没有处理")
    result.status = INVALID if result.reasons else VALID
    return result


# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------


def _ms(value: float | None) -> str:
    return "—" if value is None else f"{value:8.2f}"


def _percent(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def print_round(result: RoundResult, continuous: bool) -> None:
    mode = (
        "不等 Beat（--continuous）"
        if continuous
        else (
            f"Beat 节奏：每 {USAGE_BILLING_SWEEP_SECONDS:g} 秒"
            f" {USAGE_BILLING_SWEEP_PARALLELISM} 个扫描"
        )
    )
    print(f"--- --workers {result.workers}（{mode}）：{result.status} ---")
    for reason in result.reasons:
        print(f"  ✗ {reason}")
    measured = result.measured
    if "table" not in measured:
        return
    table = measured["table"]
    rate = measured.get("events_per_second")
    rate_text = "—" if rate is None else f"{rate:.1f}"
    print(
        f"  积压 {measured['backlog']} 条，清空用时 {measured['drain_seconds']:.1f} s，"
        f"速率 {rate_text} 条/秒"
    )
    print(
        f"  worker 忙碌占比（处理时间 ÷ 清空用时 × worker 数）：{_percent(measured['busy_share'])}"
    )
    print(f"  {'阶段（毫秒）':<34}{'平均':>8} {'p50':>8} {'p95':>8}   样本")
    for name, stats in table["stages"].items():
        if stats is None:
            print(f"  {name:<36}{'—':>8} {'—':>8} {'—':>8}   0")
            continue
        print(
            f"  {name:<36}{_ms(stats['mean_ms'])} {_ms(stats['p50_ms'])} "
            f"{_ms(stats['p95_ms'])}   {stats['n']}"
        )
    for name, key in (
        ("每条事件 SQL 条数", "sql_per_event"),
        ("每次认领 SQL 条数", "sql_per_claim"),
    ):
        stats = table[key]
        if stats is not None:
            print(f"  {name:<36}{stats['mean']:8.2f} {stats['p50']:8.0f} {stats['p95']:8.0f}")
    print(f"  数据库耗时 / 墙钟：{_percent(table['db_share_of_wall'])}")
    print(f"  CPU 时间 / 墙钟：{_percent(table['cpu_share_of_wall'])}")
    print(f"  认领 {table['claims']} 次，其中空认领 {table['empty_claims']} 次")
    throttle = measured["cpu_throttle"]
    if throttle is None:
        print(f"  容器 CPU 节流：{UNAVAILABLE}（{CPU_STAT_PATH} 不存在或读不了）")
    else:
        print(
            f"  容器 CPU 节流：nr_throttled +{throttle['nr_throttled']}，"
            f"throttled_usec +{throttle['throttled_usec']}"
        )
    status = measured["mysql_status"]
    if status is None:
        print(f"  MySQL SHOW GLOBAL STATUS：{UNAVAILABLE}")
    else:
        print(
            "  MySQL（整个实例，含栈自己的流量）："
            + "，".join(f"{name} +{status[name]}" for name in MYSQL_COUNTERS if name in status)
        )
    categories = measured["categories"]
    print(
        "  四类占比（清空用时 × worker 数）："
        + " / ".join(f"{name} {_percent(categories['shares'][name])}" for name in CATEGORIES)
    )
    print(f"  最大的一类：{categories['largest']}")


def describe_environment(factory, settings, args) -> dict[str, object]:
    with factory() as session:
        mysql_version = session.execute(text("SELECT VERSION()")).scalar_one()
        events = session.execute(select(func.count()).select_from(UsageEvent)).scalar_one()
        ledger = session.execute(select(func.count()).select_from(WalletTransaction)).scalar_one()
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "mysql": mysql_version,
        "environment": settings.environment,
        "events_per_round": args.events,
        "workers": list(args.workers),
        "continuous": args.continuous,
        "ingest_threads": args.ingest_threads,
        "lease_seconds": settings.usage_lease_seconds,
        "max_attempts": settings.usage_max_attempts,
        "beat_sweep_seconds": USAGE_BILLING_SWEEP_SECONDS,
        "beat_sweeps_per_tick": USAGE_BILLING_SWEEP_PARALLELISM,
        "sweep_budget_seconds": usage_billing.SWEEP_BUDGET_SECONDS,
        "claim_batch": usage_billing.CLAIM_BATCH,
        "cpu_stat": str(CPU_STAT_PATH) if read_throttle() is not None else UNAVAILABLE,
        "dataset_before_run": {"usage_events": events, "wallet_transactions": ledger},
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    # 写不进去就现在失败，而不是跑完才失败。
    check_json_out(args.json_out)
    logging.basicConfig(level=logging.ERROR)

    stack_settings = get_settings()
    settings = guarded_settings(args.database_url, stack_settings)
    engine = create_database_engine(settings)
    perf._assert_measurement_server(engine, stack_settings)
    factory = create_session_factory(engine)
    try:
        # 第一轮之前就查：库不干净时连 ADMIN 账号与供应商都不造。
        require_clean(factory)
    except DirtyDatabase as error:
        raise SystemExit(str(error)) from error

    environment = describe_environment(factory, settings, args)
    prefix = perf.new_run_prefix()
    environment["run_prefix"] = prefix
    print(json.dumps(environment, indent=2, ensure_ascii=False, default=perf._json_default))
    print(flush=True)

    world = perf.World(factory, settings, prefix, perf.DEFAULT_OPENING_BALANCE)
    ingestor = perf.Ingestor(factory, prefix, args.ingest_threads)
    results: list[RoundResult] = []
    aborted: str | None = None
    for workers in args.workers:
        try:
            result = run_round(world, ingestor, settings, args, workers)
        except DirtyDatabase as error:
            aborted = str(error)
            break
        results.append(result)
        print_round(result, args.continuous)
        print(flush=True)

    if aborted:
        print(f"✗ 中止：{aborted}")
        print()
    print(f"这一轮造的数据（前缀 {prefix}；脚本不删除任何行，账本只增）：")
    for line in world.created:
        print(f"  - {line}")
    print(
        f"  - 经 ingest_one 接收的用量事件 {len(ingestor.accepted)} 条，"
        f"被拒 {dict(ingestor.rejected)}"
    )

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump(
                perf._infinite_to_text(
                    {
                        "environment": environment,
                        "rounds": [
                            {
                                "label": result.label,
                                "workers": result.workers,
                                "status": result.status,
                                "reasons": result.reasons,
                                "measured": result.measured,
                            }
                            for result in results
                        ],
                        "aborted": aborted,
                        "created": world.created,
                        "events_accepted": len(ingestor.accepted),
                        "events_rejected": dict(ingestor.rejected),
                    }
                ),
                handle,
                indent=2,
                ensure_ascii=False,
                default=perf._json_default,
            )
    failed = aborted is not None or any(result.status != VALID for result in results)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
