"""BillingClient facade: recording and delivery (design gate #571 v24 §2「存储」「投递」, §5).

AIH-TASK-168：`record_*`、进程内暂存与回灌、待记事故、降级运行与重新打开、投递线程、
`deliver_once`、死信运维与事故确认、四个进程内计数。
AIH-TASK-169：对账线程与 `reconcile_status_once`、`should_block_ai`、`current_status`、
`handle_status_webhook`、`reset_status`（§2「有效状态」「状态 webhook」）。

- **对账节奏**：手动调用与对账线程共用一份 `ReconcileSchedule`，由 `_reconcile_lock` 串行
  （手动调用遇到线程正在对账时等它做完再做自己的一轮）。对账线程用停止事件的 `wait`，
  每次最多等 `RECONCILE_WAIT_MAX_SECONDS`，醒来重新读节奏；存储不可用时跳过到期的那一轮
  （不算失败、不改节奏），存储恢复后的下一次唤醒立即对账。投递线程不对账。
- **放行**：`should_block_ai()` 只在配置那一对已有状态行且为 `BLOCK_AI` 时为真；降级、
  读库失败、没有行都返回假、不抛（INV-1）。

- **降级**：构造时打开失败、或运行中读写库抛出会转入降级的错误（`IntegrityError` 以外、
  错误名不以 `SQLITE_BUSY` / `SQLITE_LOCKED` 开头的 `sqlite3.Error`，`OSError`，
  `StoreReplacedError` 等）时 `Store.invalidate()`、`store_available` 为假；记账进暂存，
  处置函数抛 `StoreUnavailableError`。投递线程每个间隔、`deliver_once()` 每次先尝试重新打开。
- **坏文件**：因 `SQLITE_CORRUPT*` / `SQLITE_NOTADB` 降级时记下当时那个文件的身份
  `(st_dev, st_ino)`；路径上的文件身份未变就不重开（不回灌进正要隔离的文件）。
- **连接代号**：每次打开成功代号加一；只有当前代号上抛出的错误才转入降级，旧代号迟到的
  错误只记 WARNING，不作废新打开的库、不标记坏文件。
- **锁的次序**：`_maintenance_lock`（回灌与写事故串行）→ `_open_lock`（作废与重新打开串行）
  → `_state_lock`；`_reconcile_lock` 在 `_open_lock` 之前；`_held_lock`、`_incident_lock`
  与 `_schedule_lock` 只护内存数据，持有时不碰 SQLite。
- `close()` 之后不再重新打开（stop 超时残留的线程同样不重开），公开方法抛 `RuntimeError`。

⚠️ 日志（logger `acuven_billing_client`）只带 `event_id`、异常类型、文件路径与条数，
不记载荷（§6「日志」）。
"""

from __future__ import annotations

import logging
import os
import random
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final, TypeVar

from . import status as _status
from .config import BillingClientConfig
from .delivery import DeliverySummary, Uniform, run_delivery_round
from .events import build_llm_event, build_quantity_event
from .ids import new_ulid
from .status import RECONCILE_WAIT_MAX_SECONDS, ReconcileSchedule
from .store import (
    DeadEvent,
    EventConflictError,
    Incident,
    IncidentItem,
    OverflowCount,
    Store,
    StoreError,
    StoreIntegrityFault,
    StoreLockedError,
    StoreReplacedError,
    StoreResidueError,
    StoreUnavailableError,
)
from .transport import Transport, UrllibTransport
from .webhook import StatusStoreError, WebhookVerificationError, verify_status_webhook

logger = logging.getLogger("acuven_billing_client")

DEFAULT_LIST_LIMIT: Final = 50
DEFAULT_STOP_TIMEOUT: Final = 30.0
THREAD_NAME: Final = "acuven-billing-delivery"
RECONCILE_THREAD_NAME: Final = "acuven-billing-reconcile"
_TRANSIENT_PREFIXES: Final = ("SQLITE_BUSY", "SQLITE_LOCKED")
# 读写 Store 时按存储故障处理的异常；EventConflictError 不在其中。
_STORE_ERRORS: Final = (sqlite3.Error, OSError, StoreError)

_T = TypeVar("_T")


# --- 错误分类 ---------------------------------------------------------------------------


def _error_name(exc: BaseException) -> str:
    # sqlite3.Error 与 StoreForeignError 都可能带 sqlite_errorname。
    name = getattr(exc, "sqlite_errorname", None)
    return name if isinstance(name, str) else ""


def _is_transient(exc: BaseException) -> bool:
    """SQLITE_BUSY* / SQLITE_LOCKED*: lock wait timed out; never degrades."""
    return isinstance(exc, sqlite3.Error) and _error_name(exc).startswith(_TRANSIENT_PREFIXES)


def _is_bad_file(exc: BaseException) -> bool:
    name = _error_name(exc)
    return name.startswith("SQLITE_CORRUPT") or name == "SQLITE_NOTADB"


def _degrades(exc: BaseException) -> bool:
    """Whether a runtime store error moves the client into the degraded state."""
    if isinstance(exc, sqlite3.IntegrityError | StoreIntegrityFault):
        return False
    if isinstance(exc, sqlite3.Error):
        return not _is_transient(exc)
    return isinstance(exc, OSError | StoreError)


def _path_identity(path: str) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_dev, st.st_ino


def _aware_seconds(value: object, name: str) -> float:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.timestamp()


# --- 待记事故的溢出与并回 ---------------------------------------------------------------


def _add_overflow(overflow: OverflowCount | None, at: float) -> OverflowCount:
    if overflow is None:
        return OverflowCount(1, at, at)
    return OverflowCount(
        overflow.count + 1,
        min(overflow.first_occurred_at, at),
        max(overflow.last_occurred_at, at),
    )


def _combine_overflow(
    first: OverflowCount | None, second: OverflowCount | None
) -> OverflowCount | None:
    """Counts add up; the first time is the earlier one, the last time the later one."""
    if first is None or first.count == 0:
        return second if second is not None and second.count else None
    if second is None or second.count == 0:
        return first
    return OverflowCount(
        first.count + second.count,
        min(first.first_occurred_at, second.first_occurred_at),
        max(first.last_occurred_at, second.last_occurred_at),
    )


def _merge_pending(
    snapshot_items: Iterable[IncidentItem],
    snapshot_overflow: OverflowCount | None,
    later_items: Iterable[IncidentItem],
    later_overflow: OverflowCount | None,
    limit: int,
) -> tuple[list[IncidentItem], OverflowCount | None]:
    """Merge a failed write back: snapshot first, then what was added meanwhile.

    超出上限的 id 转为溢出（计入条数并更新首次与最近时刻）；两段溢出条数相加、首次取较早、
    最近取较晚。
    """
    items: list[IncidentItem] = []
    overflow: OverflowCount | None = None
    for item in (*snapshot_items, *later_items):
        if len(items) < limit:
            items.append(item)
        else:
            overflow = _add_overflow(overflow, item.occurred_at)
    overflow = _combine_overflow(overflow, snapshot_overflow)
    overflow = _combine_overflow(overflow, later_overflow)
    return items, overflow


# --- 门面 -------------------------------------------------------------------------------


class BillingClient:
    """One tenant × project, one outbox file, one process. Thread-safe."""

    def __init__(
        self,
        config: BillingClientConfig,
        *,
        transport: Transport | None = None,
        clock: Callable[[], float] | None = None,
        rng: Uniform | None = None,
    ) -> None:
        # 只有配置不合法才抛；存储打不开时降级运行。
        if not isinstance(config, BillingClientConfig):
            raise TypeError("config must be a BillingClientConfig")
        self._config = config
        self._transport: Transport = transport if transport is not None else UrllibTransport()
        self._clock: Callable[[], float] = clock if clock is not None else time.time
        self._rng: Uniform = rng if rng is not None else random.Random()
        self._path = os.path.abspath(config.outbox_path)

        self._state_lock = threading.Lock()
        self._open_lock = threading.Lock()
        self._maintenance_lock = threading.Lock()
        self._held_lock = threading.Lock()
        self._incident_lock = threading.Lock()
        self._thread_lock = threading.Lock()
        self._reconcile_lock = threading.Lock()
        self._schedule_lock = threading.Lock()

        self._store: Store | None = None
        self._available = False
        self._epoch = 0
        self._closed = False
        self._opened_identity: tuple[int, int] | None = None
        self._bad_identity: tuple[int, int] | None = None

        # 插入顺序即回灌顺序。
        self._held: dict[str, bytes] = {}
        self._pending_items: list[IncidentItem] = []
        self._pending_overflow: OverflowCount | None = None
        # 正在写库的那份待写快照；写成功后才从 incidents_pending 里减去。
        self._writing: tuple[list[IncidentItem], OverflowCount | None] | None = None
        self._dropped_events = 0
        self._conflicts_dropped = 0

        self._schedule = ReconcileSchedule(
            config.reconcile_interval_seconds, config.reconcile_retry_seconds
        )

        # 投递线程与对账线程共用一个停止事件。
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._reconcile_thread: threading.Thread | None = None

        self._try_reopen()

    # --- 只读属性 ---

    @property
    def store_available(self) -> bool:
        with self._state_lock:
            return self._available

    @property
    def held_in_memory(self) -> int:
        with self._held_lock:
            return len(self._held)

    @property
    def incidents_pending(self) -> int:
        """Pending incident ids plus the overflow count, including the snapshot being written."""
        with self._incident_lock:
            total = len(self._pending_items)
            if self._pending_overflow is not None:
                total += self._pending_overflow.count
            if self._writing is not None:
                items, overflow = self._writing
                total += len(items) + (overflow.count if overflow is not None else 0)
            return total

    @property
    def dropped_events(self) -> int:
        with self._incident_lock:
            return self._dropped_events

    @property
    def conflicts_dropped(self) -> int:
        with self._incident_lock:
            return self._conflicts_dropped

    @property
    def next_reconcile_at(self) -> float | None:
        """Unix seconds (injected clock) of the next reconcile; None before the first one."""
        with self._schedule_lock:
            return self._schedule.next_at

    # --- 记账 ---

    def record_llm_usage(
        self,
        *,
        provider: str,
        model: str,
        usage_type: str,
        input_tokens: int,
        output_tokens: int,
        cache_creation_input_tokens: int,
        cache_read_input_tokens: int,
        request_id: str,
        occurred_at: datetime | None = None,
        conversation_id: str | None = None,
        feature: str | None = None,
        event_id: str | None = None,
    ) -> str:
        """Validate, then persist (or hold in memory on a store failure). Returns `event_id`."""
        self._check_open()
        built_id, payload = build_llm_event(
            provider=provider,
            model=model,
            usage_type=usage_type,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_input_tokens=cache_creation_input_tokens,
            cache_read_input_tokens=cache_read_input_tokens,
            request_id=request_id,
            now=self._now(),
            new_id=self._new_ulid,
            occurred_at=occurred_at,
            conversation_id=conversation_id,
            feature=feature,
            event_id=event_id,
        )
        return self._record(built_id, payload)

    def record_quantity_usage(
        self,
        *,
        provider: str,
        model: str,
        usage_type: str,
        quantity: Decimal | str,
        unit: str,
        request_id: str,
        occurred_at: datetime | None = None,
        conversation_id: str | None = None,
        feature: str | None = None,
        event_id: str | None = None,
    ) -> str:
        """Validate, then persist (or hold in memory on a store failure). Returns `event_id`."""
        self._check_open()
        built_id, payload = build_quantity_event(
            provider=provider,
            model=model,
            usage_type=usage_type,
            quantity=quantity,
            unit=unit,
            request_id=request_id,
            now=self._now(),
            new_id=self._new_ulid,
            occurred_at=occurred_at,
            conversation_id=conversation_id,
            feature=feature,
            event_id=event_id,
        )
        return self._record(built_id, payload)

    def _record(self, event_id: str, payload: bytes) -> str:
        # 暂存里已有同 id：字节相同直接返回，不同即冲突；不写库、不重复放入。
        with self._held_lock:
            held = self._held.get(event_id)
        if held is not None:
            if held != payload:
                raise EventConflictError(event_id)
            return event_id
        current = self._current()
        if current is None:
            return self._hold(event_id, payload, "store unavailable")
        epoch, store = current
        try:
            return store.insert_event(event_id, payload)
        except StoreIntegrityFault:
            # 只这一条进暂存：不作废连接、不转降级。
            return self._hold(event_id, payload, "StoreIntegrityFault")
        except sqlite3.IntegrityError:
            raise
        except _STORE_ERRORS as exc:
            if _degrades(exc):
                self._on_store_error(exc, epoch)
            return self._hold(event_id, payload, type(exc).__name__)

    def _hold(self, event_id: str, payload: bytes, cause: str) -> str:
        at = float(self._clock())
        with self._held_lock:
            existing = self._held.get(event_id)
            if existing is not None:
                if existing != payload:
                    raise EventConflictError(event_id)
                return event_id
            full = len(self._held) >= self._config.held_max_events
            if not full:
                self._held[event_id] = payload
        if full:
            self._drop(event_id, at, "in-memory hold is full")
        else:
            logger.error(
                "outbox write failed (%s); usage event held in memory: event_id=%s",
                cause,
                event_id,
            )
        return event_id

    def _drop(self, event_id: str, at: float, why: str) -> None:
        with self._incident_lock:
            self._dropped_events += 1
        logger.error("usage event dropped (%s): event_id=%s", why, event_id)
        self._add_incident(event_id, "DROPPED", at)

    def _add_incident(self, event_id: str, kind: str, at: float) -> None:
        with self._incident_lock:
            if len(self._pending_items) < self._config.pending_incidents_max:
                self._pending_items.append(IncidentItem(event_id, kind, at))
                return
            # 列表满了：不再记 id，只累加溢出计数与首次、最近时刻。
            self._pending_overflow = _add_overflow(self._pending_overflow, at)
            count = self._pending_overflow.count
        logger.error("pending incident list is full; overflow count is now %d", count)

    # --- 暂存回灌与写事故 ---

    def _maintain(self) -> None:
        # 同一把专用锁：deliver_once 与后台线程不会同时回灌或同时写事故。
        with self._maintenance_lock:
            self._refill()
            self._flush_incidents()

    def _refill(self) -> None:
        # 锁内取快照、锁外写 SQLite、再在锁内移除已处理的条目：记账不被回灌写库锁住。
        with self._held_lock:
            snapshot = list(self._held.items())
        done: list[tuple[str, bytes]] = []
        try:
            for event_id, payload in snapshot:
                current = self._current()
                if current is None:
                    break
                epoch, store = current
                try:
                    store.insert_event(event_id, payload)
                except EventConflictError:
                    self._conflict_dropped(event_id)
                except StoreIntegrityFault:
                    self._drop(event_id, float(self._clock()), "integrity fault on refill")
                except _STORE_ERRORS as exc:
                    # 仍是存储故障：留在暂存，停止本轮回灌。
                    if _degrades(exc):
                        self._on_store_error(exc, epoch)
                    else:
                        logger.warning("refill stopped by a store error: %s", type(exc).__name__)
                    break
                done.append((event_id, payload))
        finally:
            with self._held_lock:
                for event_id, payload in done:
                    if self._held.get(event_id) == payload:
                        del self._held[event_id]

    def _conflict_dropped(self, event_id: str) -> None:
        with self._incident_lock:
            self._conflicts_dropped += 1
        logger.error("held event conflicts with the stored row; dropped: event_id=%s", event_id)
        self._add_incident(event_id, "CONFLICT_DROPPED", float(self._clock()))

    def _flush_incidents(self) -> None:
        current = self._current()
        if current is None:
            return
        epoch, store = current
        with self._incident_lock:
            if not self._pending_items and self._pending_overflow is None:
                return
            items, overflow = self._pending_items, self._pending_overflow
            self._pending_items, self._pending_overflow = [], None
            self._writing = (items, overflow)
        try:
            store.record_incidents(items, overflow=overflow)
        except Exception as exc:
            with self._incident_lock:
                self._pending_items, self._pending_overflow = _merge_pending(
                    items,
                    overflow,
                    self._pending_items,
                    self._pending_overflow,
                    self._config.pending_incidents_max,
                )
                self._writing = None
            if not isinstance(exc, _STORE_ERRORS):
                raise
            logger.error("writing pending incidents failed: %s", type(exc).__name__)
            if _degrades(exc):
                self._on_store_error(exc, epoch)
            return
        with self._incident_lock:
            self._writing = None

    # --- 降级与重新打开 ---

    def _current(self) -> tuple[int, Store] | None:
        with self._state_lock:
            if not self._available or self._store is None:
                return None
            return self._epoch, self._store

    def _on_store_error(self, exc: BaseException, epoch: int) -> None:
        """Degrade after a store error raised on connection generation `epoch`."""
        with self._open_lock:
            with self._state_lock:
                if epoch != self._epoch:
                    logger.warning(
                        "late outbox error from an old connection ignored: %s",
                        type(exc).__name__,
                    )
                    return
                if not self._available:
                    return
                self._available = False
                store = self._store
            logger.error(
                "outbox store failed, running degraded: %s at %s", type(exc).__name__, self._path
            )
            if _is_bad_file(exc):
                self._bad_identity = self._opened_identity
            if store is not None and not self._closed:
                store.invalidate()
        if isinstance(exc, StoreReplacedError):
            # 文件被移走或替换：立即尝试重新打开路径上的文件。
            self._try_reopen()

    def _try_reopen(self) -> bool:
        """Open (or reopen) the outbox unless closed or the path still holds the bad file."""
        with self._open_lock:
            if self._closed:
                return False
            with self._state_lock:
                if self._available:
                    return True
            if self._bad_identity is not None:
                current = _path_identity(self._path)
                if current is not None and current == self._bad_identity:
                    logger.debug("outbox file is still the corrupt one; not reopening")
                    return False
            try:
                if self._store is None:
                    store = Store(self._path, clock=self._clock)
                else:
                    store = self._store
                    store.reopen()
            except _STORE_ERRORS as exc:
                self._log_open_failure(exc)
                if _is_bad_file(exc):
                    self._bad_identity = _path_identity(self._path)
                return False
            identity = _path_identity(self._path)
            with self._state_lock:
                self._store = store
                self._available = True
                self._epoch += 1
                self._opened_identity = identity
                self._bad_identity = None
        logger.info("outbox store opened: %s", self._path)
        return True

    def _log_open_failure(self, exc: BaseException) -> None:
        # 只记异常类型与文件路径（被锁与残留文件另写明原因），不记文件内容。
        detail = ""
        if isinstance(exc, StoreLockedError):
            detail = " (locked by another process)"
        elif isinstance(exc, StoreResidueError):
            detail = " (residue files: " + ", ".join(exc.files) + ")"
        logger.error(
            "outbox store unavailable, running degraded: %s at %s%s",
            type(exc).__name__,
            self._path,
            detail,
        )

    # --- 投递 ---

    def deliver_once(self) -> DeliverySummary:
        """Reopen if degraded, refill, write pending incidents, then one delivery round."""
        self._check_open()
        return self._cycle()

    def _cycle(self) -> DeliverySummary:
        if not self.store_available:
            self._try_reopen()
        self._maintain()
        return self._deliver_round()

    def _deliver_round(self) -> DeliverySummary:
        current = self._current()
        if current is None:
            return DeliverySummary.zero()
        epoch, store = current
        try:
            return run_delivery_round(
                store,
                self._transport,
                self._config,
                clock=self._clock,
                rng=self._rng,
                new_request_id=self._new_ulid,
            )
        except _STORE_ERRORS as exc:
            # 已落状态的行以库为准，不另统计。
            if _degrades(exc):
                self._on_store_error(exc, epoch)
            else:
                logger.warning("delivery round stopped by a store error: %s", type(exc).__name__)
            return DeliverySummary.zero()

    def start(self) -> None:
        """Start the delivery and reconcile threads; both first rounds run at once.

        运行中再调是空操作。每次真正启动都把对账节奏重置为立即对账、并重置「本段连续失败
        已补查过」的标记。
        """
        with self._thread_lock:
            self._check_open()
            running = [
                thread
                for thread in (self._thread, self._reconcile_thread)
                if thread is not None and thread.is_alive()
            ]
            if running:
                if self._stop_event.is_set():
                    raise RuntimeError("previous stop timed out; old thread still running")
                return
            self._stop_event.clear()
            with self._schedule_lock:
                self._schedule.restart(float(self._clock()))
            delivery = threading.Thread(target=self._run, name=THREAD_NAME, daemon=True)
            reconcile = threading.Thread(
                target=self._run_reconcile, name=RECONCILE_THREAD_NAME, daemon=True
            )
            self._thread = delivery
            self._reconcile_thread = reconcile
            delivery.start()
            reconcile.start()

    def _run(self) -> None:
        stop_event = self._stop_event
        interval = self._config.delivery_interval_seconds
        while not stop_event.is_set():
            try:
                self._cycle()
            except Exception as exc:
                # 线程不退出；只记异常类型（消息里可能带外部内容）。
                logger.error("delivery round failed: %s", type(exc).__name__)
            if stop_event.wait(interval):
                return

    def _run_reconcile(self) -> None:
        stop_event = self._stop_event
        while not stop_event.is_set():
            try:
                self._reconcile_tick()
            except Exception as exc:
                logger.error("status reconcile round failed: %s", type(exc).__name__)
            if stop_event.wait(self._reconcile_wait()):
                return

    def stop(self, timeout: float | None = DEFAULT_STOP_TIMEOUT) -> bool:
        """Signal both threads and wait; False (with a WARNING) if either is still running."""
        with self._thread_lock:
            self._stop_event.set()
            delivery, reconcile = self._thread, self._reconcile_thread
        deadline = None if timeout is None else time.monotonic() + timeout
        stopped = True
        for thread, label in ((delivery, "delivery"), (reconcile, "reconcile")):
            if thread is None:
                continue
            thread.join(None if deadline is None else max(0.0, deadline - time.monotonic()))
            if thread.is_alive():
                logger.warning("%s thread did not stop within %s seconds", label, timeout)
                stopped = False
                continue
            with self._thread_lock:
                if self._thread is thread:
                    self._thread = None
                if self._reconcile_thread is thread:
                    self._reconcile_thread = None
        return stopped

    def close(self, timeout: float | None = DEFAULT_STOP_TIMEOUT) -> bool:
        """`stop(timeout)`, then close the store and release the single-process lock.

        返回 `stop` 的结果。之后不再重新打开；记账、投递与处置抛 `RuntimeError`。
        """
        stopped = self.stop(timeout)
        with self._open_lock:
            if self._closed:
                return stopped
            self._closed = True
            with self._state_lock:
                self._available = False
                store = self._store
            if store is not None:
                store.close()
        return stopped

    # --- 有效状态 ---

    def reconcile_status_once(self) -> bool:
        """One reconcile now, whatever the schedule; returns whether it succeeded. Never raises.

        降级时先尝试重新打开，仍不可用算一次失败。对账线程正在对账时等它做完再做自己的
        一轮；之后按结果更新 `next_reconcile_at`。
        """
        self._check_open()
        with self._reconcile_lock:
            if not self.store_available:
                self._try_reopen()
            current = self._current()
            if current is None:
                logger.warning("effective-status reconcile failed: outbox store unavailable")
                self._schedule_result(False)
                return False
            return self._reconcile_with(*current)

    def _reconcile_tick(self) -> None:
        """The reconcile thread's turn: reconcile only when due and the store is available."""
        with self._reconcile_lock:
            if self._stop_event.is_set():
                return
            with self._schedule_lock:
                due = self._schedule.due(float(self._clock()))
            if not due:
                return
            # 存储不可用：跳过这一轮，不算失败、不改节奏（重新打开由投递线程负责）。
            current = self._current()
            if current is None:
                return
            self._reconcile_with(*current)

    def _reconcile_with(self, epoch: int, store: Store) -> bool:
        # 调用方持有 _reconcile_lock。
        ok = _status.reconcile_status_once(
            store,
            self._transport,
            self._config,
            clock=self._clock,
            new_request_id=self._new_ulid,
            on_store_error=lambda exc: self._on_status_store_error(exc, epoch),
        )
        self._schedule_result(ok)
        return ok

    def _schedule_result(self, ok: bool) -> None:
        now = float(self._clock())
        with self._schedule_lock:
            if ok:
                self._schedule.succeeded(now)
            else:
                self._schedule.failed(now)

    def _reconcile_wait(self) -> float:
        with self._schedule_lock:
            return self._schedule.wait_seconds(float(self._clock()), RECONCILE_WAIT_MAX_SECONDS)

    def _on_status_store_error(self, exc: BaseException, epoch: int) -> None:
        if _degrades(exc):
            self._on_store_error(exc, epoch)

    def should_block_ai(self) -> bool:
        """True only if the configured pair has an applied status row that says BLOCK_AI.

        降级时直接返回假、不访问库；读库失败或没有行返回假并记 WARNING。从不抛异常（INV-1）。
        """
        current = self._current()
        if current is None:
            return False
        epoch, store = current
        try:
            row = store.read_status(self._config.tenant_id, self._config.project_id)
        except Exception as exc:
            logger.warning(
                "reading the effective status failed; allowing AI: %s", type(exc).__name__
            )
            if isinstance(exc, _STORE_ERRORS):
                self._on_status_store_error(exc, epoch)
            return False
        if row is None:
            logger.warning("no effective status applied yet; allowing AI")
            return False
        return row.effective_status == "BLOCK_AI"

    def current_status(self) -> dict[str, Any] | None:
        """The applied status: the 10 fields plus `applied_at`. None if none or unreadable."""
        current = self._current()
        if current is None:
            return None
        epoch, store = current
        try:
            row = store.read_status(self._config.tenant_id, self._config.project_id)
        except Exception as exc:
            logger.warning("reading the effective status failed: %s", type(exc).__name__)
            if isinstance(exc, _STORE_ERRORS):
                self._on_status_store_error(exc, epoch)
            return None
        return None if row is None else asdict(row)

    def handle_status_webhook(
        self,
        *,
        method: str,
        raw_path: str | bytes,
        query_string: str | bytes,
        headers: Mapping[Any, Any] | Iterable[Any],
        body: bytes,
        signed_path: str | None = None,
    ) -> bool:
        """Verify a status webhook, then apply it by version. Returns whether it was applied.

        验签失败抛 `WebhookVerificationError`（回 401）；验签通过而存储不可用或写库失败抛
        `StatusStoreError`（回 503）。旧版本或同版本返回假，不是错误。不写对账时刻。
        """
        self._check_open()
        fields = verify_status_webhook(
            self._config,
            now=float(self._clock()),
            method=method,
            raw_path=raw_path,
            query_string=query_string,
            headers=headers,
            body=body,
            signed_path=signed_path,
        )
        current = self._current()
        if current is None:
            raise StatusStoreError(f"outbox store is unavailable: {self._path}")
        epoch, store = current
        try:
            applied = store.apply_status(
                self._config.tenant_id, self._config.project_id, fields, now=float(self._clock())
            )
        except _STORE_ERRORS as exc:
            logger.error("storing the webhook status failed: %s", type(exc).__name__)
            self._on_status_store_error(exc, epoch)
            raise StatusStoreError(f"storing the status failed: {type(exc).__name__}") from exc
        logger.info(
            "status webhook verified: status_version=%d effective_status=%s applied=%s",
            fields["status_version"],
            fields["effective_status"],
            applied,
        )
        return applied

    def reset_status(self, *, reason: str) -> bool:
        """Delete the configured pair's status row (RESET_STATUS in ops_log); False if none."""
        return self._dispose(
            lambda store: store.reset_status(
                self._config.tenant_id, self._config.project_id, reason=reason
            )
        )

    # --- 死信运维、重放与事故 ---

    def requeue_dead(self, event_id: str, *, reason: str) -> bool:
        return self._dispose(lambda store: store.requeue_dead(event_id, reason=reason))

    def ack_dead(self, event_id: str, *, reason: str) -> bool:
        return self._dispose(lambda store: store.ack_dead(event_id, reason=reason))

    def list_dead(self, limit: int = DEFAULT_LIST_LIMIT) -> list[DeadEvent]:
        return self._dispose(lambda store: store.list_dead(limit))

    def replay_delivered(
        self, *, delivered_from: datetime, delivered_until: datetime, reason: str
    ) -> int:
        """DELIVERED rows delivered in `[delivered_from, delivered_until)` → PENDING.

        只收带时区的 `datetime`（不带时区抛 `ValueError`），换算成 Unix 秒再转调 Store。
        """
        self._check_open()
        start = _aware_seconds(delivered_from, "delivered_from")
        end = _aware_seconds(delivered_until, "delivered_until")
        return self._dispose(
            lambda store: store.replay_delivered(
                delivered_from=start, delivered_until=end, reason=reason
            )
        )

    def list_incidents(self, limit: int = DEFAULT_LIST_LIMIT) -> list[Incident]:
        return self._dispose(lambda store: store.list_incidents(limit))

    def ack_incident(self, incident_id: str, *, reason: str) -> bool:
        return self._dispose(lambda store: store.ack_incident(incident_id, reason=reason))

    def _dispose(self, operation: Callable[[Store], _T]) -> _T:
        # BUSY / LOCKED 原样抛出、不转降级；会转入降级的错误先转入降级，
        # 再抛 StoreUnavailableError。
        self._check_open()
        current = self._current()
        if current is None:
            raise StoreUnavailableError(f"outbox store is unavailable: {self._path}")
        epoch, store = current
        try:
            return operation(store)
        except _STORE_ERRORS as exc:
            if not _degrades(exc):
                raise
            self._on_store_error(exc, epoch)
            raise StoreUnavailableError(f"outbox store is unavailable: {self._path}") from exc

    # --- 工具 ---

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("BillingClient is closed")

    def _now(self) -> datetime:
        return datetime.fromtimestamp(float(self._clock()), UTC)

    def _new_ulid(self) -> str:
        return new_ulid(now_ms=lambda: int(float(self._clock()) * 1000))


__all__ = [
    "BillingClient",
    "StatusStoreError",
    "StoreUnavailableError",
    "WebhookVerificationError",
]
