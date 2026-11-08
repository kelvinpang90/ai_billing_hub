"""Local health metrics (design gate #571 v24 §2「健康指标」).

AIH-TASK-170：`health()` 的十五个键与 `level` 判定；命令行 `stats` 也用这里的库内指标。

- 库里算出的键只经 Store 的只读函数读（`delivery_counts`、`count_unacked_incidents`、
  `read_last_delivery_ok_at`、`read_reconcile`），不直接写 SQL。
- `store_available` 为假与读库失败统一处理：库里的九个键为 None，`store_available` 与四个
  进程内计数照实返回，`level` 照常计算（此时一定是 P1）。
- `dropped_events` 与 `conflicts_dropped` 是进程累计数，只作展示、不参与 `level`。
- 时刻都是 Unix 秒，与库里存的一致。
"""

from __future__ import annotations

from typing import Any, Final

from .store import Store

# 最老待发超过这么久 → P2。
OLDEST_PENDING_P2_SECONDS: Final = 900
# 最近一次对账成功早于这么久（或从未成功）→ status_reconcile_stale → P2。
RECONCILE_STALE_SECONDS: Final = 300

LEVEL_OK: Final = "OK"
LEVEL_P1: Final = "P1"
LEVEL_P2: Final = "P2"

# 从库里算出的键；存储不可用或读库失败时为 None。
STORE_KEYS: Final = (
    "pending",
    "sending",
    "dead_unacked",
    "incidents_unacked",
    "oldest_pending_age_seconds",
    "last_delivery_ok_at",
    "last_status_reconcile_ok_at",
    "status_reconcile_stale",
    "auth_failing",
)
# 命令行 `stats` 输出的库内键（不含对账，对账按每一对租户 × 项目单列）。
OUTBOX_KEYS: Final = (
    "pending",
    "sending",
    "dead_unacked",
    "incidents_unacked",
    "oldest_pending_age_seconds",
    "auth_failing",
    "last_delivery_ok_at",
)
HEALTH_KEYS: Final = (
    "store_available",
    "pending",
    "sending",
    "dead_unacked",
    "incidents_unacked",
    "oldest_pending_age_seconds",
    "held_in_memory",
    "incidents_pending",
    "dropped_events",
    "conflicts_dropped",
    "last_delivery_ok_at",
    "last_status_reconcile_ok_at",
    "status_reconcile_stale",
    "auth_failing",
    "level",
)


def age_seconds(oldest_created_at: float | None, now: float) -> float | None:
    """`now` minus the oldest created_at; never below 0 (an injected clock may lag behind)."""
    if oldest_created_at is None:
        return None
    return max(0.0, float(now) - float(oldest_created_at))


def read_outbox_metrics(store: Store, *, now: float) -> dict[str, Any]:
    """The outbox keys shared by `health()` and the command line `stats`. Raises on read errors."""
    counts = store.delivery_counts()
    incidents_unacked = store.count_unacked_incidents()
    last_delivery_ok_at = store.read_last_delivery_ok_at()
    return {
        "pending": counts.pending,
        "sending": counts.sending,
        "dead_unacked": counts.dead_unacked,
        "incidents_unacked": incidents_unacked,
        "oldest_pending_age_seconds": age_seconds(counts.oldest_unsent_created_at, now),
        "auth_failing": counts.auth_failing,
        "last_delivery_ok_at": last_delivery_ok_at,
    }


def read_store_metrics(
    store: Store, *, tenant_id: str, project_id: str, now: float
) -> dict[str, Any]:
    """The nine keys computed from the database. Raises on read errors."""
    metrics = read_outbox_metrics(store, now=now)
    last_ok_at = store.read_reconcile(tenant_id, project_id).last_ok_at
    metrics["last_status_reconcile_ok_at"] = last_ok_at
    metrics["status_reconcile_stale"] = (
        last_ok_at is None or float(now) - float(last_ok_at) > RECONCILE_STALE_SECONDS
    )
    return {key: metrics[key] for key in STORE_KEYS}


def compute_level(
    store_metrics: dict[str, Any] | None,
    *,
    store_available: bool,
    held_in_memory: int,
    incidents_pending: int,
) -> str:
    """P1 / P2 / OK, same scope as the server's `ai_usage_outbox` dimension."""
    if not store_available or store_metrics is None:
        return LEVEL_P1
    if (
        store_metrics["dead_unacked"] > 0
        or store_metrics["incidents_unacked"] > 0
        or incidents_pending > 0
        or held_in_memory > 0
        or store_metrics["auth_failing"]
    ):
        return LEVEL_P1
    age = store_metrics["oldest_pending_age_seconds"]
    if (age is not None and age > OLDEST_PENDING_P2_SECONDS) or store_metrics[
        "status_reconcile_stale"
    ]:
        return LEVEL_P2
    return LEVEL_OK


def build_health(
    store_metrics: dict[str, Any] | None,
    *,
    store_available: bool,
    held_in_memory: int,
    incidents_pending: int,
    dropped_events: int,
    conflicts_dropped: int,
) -> dict[str, Any]:
    """Assemble the fifteen keys; `store_metrics` is None when the store was not readable."""
    values: dict[str, Any] = dict.fromkeys(STORE_KEYS)
    if store_available and store_metrics is not None:
        values.update(store_metrics)
    else:
        store_metrics = None
    values.update(
        store_available=bool(store_available),
        held_in_memory=held_in_memory,
        incidents_pending=incidents_pending,
        dropped_events=dropped_events,
        conflicts_dropped=conflicts_dropped,
        level=compute_level(
            store_metrics,
            store_available=store_available,
            held_in_memory=held_in_memory,
            incidents_pending=incidents_pending,
        ),
    )
    return {key: values[key] for key in HEALTH_KEYS}


__all__ = [
    "HEALTH_KEYS",
    "LEVEL_OK",
    "LEVEL_P1",
    "LEVEL_P2",
    "OLDEST_PENDING_P2_SECONDS",
    "OUTBOX_KEYS",
    "RECONCILE_STALE_SECONDS",
    "STORE_KEYS",
    "age_seconds",
    "build_health",
    "compute_level",
    "read_outbox_metrics",
    "read_store_metrics",
]
