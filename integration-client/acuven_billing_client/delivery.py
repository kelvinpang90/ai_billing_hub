"""One delivery round (design gate #571 v24 §2「投递」「时间语义」, §3 INV-2/11/13/14, §5).

`run_delivery_round` 一次调用只领取一次、处理一批：领取 → 拼批量请求体（各条 payload
原样拼接、不重新序列化）→ 签名发送 → 按回执逐条落状态（批层 413 / 422 三种码改为
同一轮逐条单发）→ 清理 → 送达至少一条时记 `last_delivery_ok_at`。是否连续多轮、
内存暂存与后台线程由门面（AIH-TASK-168）决定。

- 每次尝试（批量或单发）新的 Request-Id 与 `int(clock())` 时间戳，用当前配置重新签名。
- 进 DEAD 只限白名单的六个码且 `retryable` 为假；其余失败一律 PENDING 退避、记下码。
- `last_error_code` 为 `INTEGRATION_AUTH_FAILED` 的行固定 `now + 60` 秒，其余按指数退避，
  可重试的没有放弃上限。
- 落回执与续租都带租约栅栏；栅栏失效（行已被别人重领）的行计入 `fenced`。
- 落回执或续租抛 `sqlite3.Error` 时本轮立即中止、异常抛给调用方：不再单发回退、
  不清理、不记送达时刻；已领取的行停在 SENDING，租约到期后被重领。
- HTTP 请求不在任何事务里：领取提交后才发送，回来再开新事务落状态。

⚠️ 日志（logger `acuven_billing_client`）只带 `event_id`、`error_code`、条数与状态码；
不记密钥、签名、请求头原文、载荷与响应体（§6「日志」）。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Final, Protocol
from urllib.parse import urlsplit

from .config import BillingClientConfig
from .signing import request_headers
from .store import AUTH_FAILED_CODE, ClaimedEvent, Fence, RetryRow, Store
from .transport import Response, Transport, TransportError

logger = logging.getLogger("acuven_billing_client")

BATCH_PATH: Final = "/api/v1/integration/usage-events/batch"
SINGLE_PATH: Final = "/api/v1/integration/usage-events"
# `docs/api.md` 单条摄取错误表里 retryable = false 且由事件内容决定的码（§2「投递」）。
DEAD_LETTER_CODES: Final = frozenset(
    {
        "VALIDATION_ERROR",
        "UNSUPPORTED_SCHEMA_VERSION",
        "OCCURRED_AT_IN_FUTURE",
        "IDEMPOTENCY_CONFLICT",
        "PAYLOAD_TOO_LARGE",
        "CREDENTIAL_SCOPE_MISMATCH",
    }
)
# 批层面的这三种 (状态码, 码) 且 retryable 为假时，改为同一轮逐条单发。
_BATCH_FALLBACK: Final = frozenset(
    {(413, "PAYLOAD_TOO_LARGE"), (422, "VALIDATION_ERROR"), (422, "BATCH_TOO_LARGE")}
)
# 单发回退里可重试、但只取决于这一条 usage_type 的码：只退避这一条，继续下一条。
UNKNOWN_USAGE_TYPE: Final = "UNKNOWN_USAGE_TYPE"
_DELIVERED_STATUSES: Final = frozenset({"accepted", "already_received", "already_processed"})

INVALID_RECEIPT_CODE: Final = "INVALID_BILLING_RECEIPT"
INVALID_RESPONSE_CODE: Final = "INVALID_BILLING_RESPONSE"
UNAVAILABLE_CODE: Final = "BILLING_UNAVAILABLE"
NETWORK_ERROR_CODE: Final = "NETWORK_ERROR"

AUTH_RETRY_SECONDS: Final = 60
BACKOFF_MAX_SECONDS: Final = 3600
BACKOFF_MAX_EXPONENT: Final = 12
PURGE_LIMIT: Final = 1000

_BATCH_PREFIX: Final = b'{"events":['
_BATCH_SUFFIX: Final = b"]}"


class Uniform(Protocol):
    """`random.Random` or anything with the same `uniform(a, b)`."""

    def uniform(self, a: float, b: float) -> float: ...


@dataclass(frozen=True, slots=True)
class DeliverySummary:
    claimed: int
    delivered: int
    retried: int
    dead: int
    # 续租不足而放弃的行，与落回执时栅栏失效（已被别人重领）的行。
    fenced: int

    @classmethod
    def zero(cls) -> DeliverySummary:
        return cls(claimed=0, delivered=0, retried=0, dead=0, fenced=0)


class _Kind(Enum):
    DELIVERED = "delivered"
    RETRY = "retry"
    DEAD = "dead"
    # 只用于批量回执：改为逐条单发。
    FALLBACK = "fallback"


@dataclass(frozen=True, slots=True)
class _Outcome:
    kind: _Kind
    error_code: str | None = None
    # 单发回退时：结果只取决于这一条事件（继续下一条），还是与它无关（停止回退）。
    event_related: bool = True


_DELIVERED: Final = _Outcome(_Kind.DELIVERED)


def _retry(code: str, *, event_related: bool = True) -> _Outcome:
    return _Outcome(_Kind.RETRY, code, event_related)


@dataclass(slots=True)
class _Tally:
    claimed: int = 0
    delivered: int = 0
    retried: int = 0
    dead: int = 0
    fenced: int = 0

    def summary(self) -> DeliverySummary:
        return DeliverySummary(
            claimed=self.claimed,
            delivered=self.delivered,
            retried=self.retried,
            dead=self.dead,
            fenced=self.fenced,
        )


# --- 退避 ------------------------------------------------------------------------------


def backoff_seconds(attempts: int, rng: Uniform) -> float:
    """`min(3600, 2 ** min(attempts, 12)) × uniform(0.5, 1.0)`; `attempts` is after the claim."""
    return min(BACKOFF_MAX_SECONDS, 2 ** min(attempts, BACKOFF_MAX_EXPONENT)) * rng.uniform(
        0.5, 1.0
    )


def next_attempt_at(now: float, attempts: int, error_code: str, rng: Uniform) -> float:
    """Credential errors retry at a fixed `now + 60`; everything else backs off exponentially."""
    if error_code == AUTH_FAILED_CODE:
        return now + AUTH_RETRY_SECONDS
    return now + backoff_seconds(attempts, rng)


# --- 地址与请求 -------------------------------------------------------------------------


def endpoint_url(base_url: str, path: str) -> str:
    """`base_url` without its trailing `/`, then the endpoint path; a path prefix is kept."""
    return base_url.rstrip("/") + path


def batch_body(payloads: Sequence[bytes]) -> bytes:
    """`{"events":[` + payloads joined by `,` + `]}`; payloads are not re-serialized."""
    return _BATCH_PREFIX + b",".join(payloads) + _BATCH_SUFFIX


def _post(
    transport: Transport,
    config: BillingClientConfig,
    path: str,
    body: bytes,
    *,
    clock: Callable[[], float],
    new_request_id: Callable[[], str],
) -> Response | None:
    """One signed POST; `None` on a network error, timeout or redirect."""
    url = endpoint_url(config.base_url, path)
    # 签名用实际请求 URL 的路径部分（含 base_url 的前缀），没有查询串。
    headers = request_headers(
        config,
        "POST",
        urlsplit(url).path,
        body,
        timestamp=int(clock()),
        request_id=new_request_id(),
    )
    try:
        return transport.send("POST", url, headers, body, config.http_timeout_seconds)
    except TransportError:
        return None


# --- 回执判定 ---------------------------------------------------------------------------


def _parse_json(body: bytes) -> Any:
    try:
        return json.loads(body)
    except (ValueError, RecursionError):
        return None


def _envelope(document: Any) -> tuple[str, bool] | None:
    """`(code, retryable)` of an error envelope; `None` when any part is missing."""
    if not isinstance(document, dict):
        return None
    error = document.get("error")
    retryable = document.get("retryable")
    if not isinstance(error, dict) or not isinstance(retryable, bool):
        return None
    code = error.get("code")
    if not isinstance(code, str):
        return None
    return code, retryable


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_delivered_status(value: object) -> bool:
    # 先判类型：JSON 里的数组、对象不可哈希，直接查集合会抛 TypeError。
    return isinstance(value, str) and value in _DELIVERED_STATUSES


def _failure_outcome(status: int, document: Any, *, single: bool) -> _Outcome:
    """Non-200 batch responses, and single responses other than 200 / 202."""
    envelope = _envelope(document)
    if envelope is None:
        code = UNAVAILABLE_CODE if status >= 500 else INVALID_RESPONSE_CODE
        return _retry(code, event_related=False)
    code, retryable = envelope
    if retryable:
        return _retry(code, event_related=single and code == UNKNOWN_USAGE_TYPE)
    if single:
        if code in DEAD_LETTER_CODES:
            return _Outcome(_Kind.DEAD, code)
        # 不能判断是事件本身的问题（例如 base_url 配错的 404 / 405、401）：不进死信。
        return _retry(code, event_related=False)
    if (status, code) in _BATCH_FALLBACK:
        return _Outcome(_Kind.FALLBACK, code)
    return _retry(code, event_related=False)


def _item_outcome(item: Mapping[str, Any]) -> _Outcome:
    status = item.get("status")
    if _is_delivered_status(status):
        return _DELIVERED
    if status != "rejected":
        return _retry(INVALID_RECEIPT_CODE)
    retryable = item.get("retryable")
    code = item.get("error_code")
    if not isinstance(retryable, bool) or not isinstance(code, str):
        return _retry(INVALID_RECEIPT_CODE)
    if not retryable and code in DEAD_LETTER_CODES:
        return _Outcome(_Kind.DEAD, code)
    return _retry(code)


def _batch_outcomes(
    response: Response | None, events: Sequence[ClaimedEvent]
) -> list[_Outcome] | _Outcome:
    """Per-event outcomes for a well-formed 200, else one outcome for the whole batch."""
    if response is None:
        return _retry(NETWORK_ERROR_CODE, event_related=False)
    document = _parse_json(response.body)
    if response.status != 200:
        return _failure_outcome(response.status, document, single=False)
    data = document.get("data") if isinstance(document, dict) else None
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list) or len(results) != len(events):
        return _retry(INVALID_RECEIPT_CODE)
    for position, (item, event) in enumerate(zip(results, events, strict=True)):
        if (
            not isinstance(item, dict)
            or not _is_int(item.get("index"))
            or item["index"] != position
            or item.get("event_id") != event.event_id
        ):
            return _retry(INVALID_RECEIPT_CODE)
    return [_item_outcome(item) for item in results]


def _single_outcome(response: Response | None, event_id: str) -> _Outcome:
    """Outcome of the single-event endpoint for one event."""
    if response is None:
        return _retry(NETWORK_ERROR_CODE, event_related=False)
    document = _parse_json(response.body)
    if response.status in (200, 202):
        # 不要求状态码与 status 的对应关系；event_id 必须是发出的那条。
        data = document.get("data") if isinstance(document, dict) else None
        if (
            isinstance(data, dict)
            and data.get("event_id") == event_id
            and _is_delivered_status(data.get("status"))
        ):
            return _DELIVERED
        return _retry(INVALID_RECEIPT_CODE)
    return _failure_outcome(response.status, document, single=True)


# --- 落状态 -----------------------------------------------------------------------------


def _settle(
    store: Store,
    rows: Sequence[tuple[ClaimedEvent, float, _Outcome]],
    *,
    clock: Callable[[], float],
    rng: Uniform,
    tally: _Tally,
) -> None:
    """Write outcomes behind the lease fence; `sqlite3.Error` propagates to the caller."""
    delivered = [
        Fence(event.event_id, lease)
        for event, lease, outcome in rows
        if outcome.kind is _Kind.DELIVERED
    ]
    if delivered:
        updated = store.mark_delivered(delivered)
        tally.delivered += len(updated)
        tally.fenced += len(delivered) - len(updated)

    now = float(clock())
    retries = [
        RetryRow(
            event.event_id,
            lease,
            next_attempt_at(now, event.attempts, outcome.error_code, rng),
            outcome.error_code,
        )
        for event, lease, outcome in rows
        if outcome.kind is _Kind.RETRY and outcome.error_code is not None
    ]
    if retries:
        updated = store.mark_retry(retries)
        tally.retried += len(updated)
        tally.fenced += len(retries) - len(updated)

    dead_by_code: dict[str, list[Fence]] = {}
    for event, lease, outcome in rows:
        if outcome.kind is _Kind.DEAD and outcome.error_code is not None:
            dead_by_code.setdefault(outcome.error_code, []).append(Fence(event.event_id, lease))
    for code, fences in dead_by_code.items():
        updated = store.mark_dead(fences, code)
        tally.dead += len(updated)
        tally.fenced += len(fences) - len(updated)
        for fence in fences:
            if fence.event_id in updated:
                logger.error(
                    "usage event moved to dead letter: event_id=%s error_code=%s",
                    fence.event_id,
                    code,
                )


def _send_singly(
    store: Store,
    transport: Transport,
    config: BillingClientConfig,
    events: Sequence[ClaimedEvent],
    *,
    clock: Callable[[], float],
    rng: Uniform,
    new_request_id: Callable[[], str],
    tally: _Tally,
) -> None:
    """Batch-level 413 / 422: resend one by one through the single-event endpoint."""
    # 领取时整批写的是同一个租约；之后每次续租都记下新值，作为下一次的栅栏。
    lease = events[0].lease_until
    for position, event in enumerate(events):
        remaining = events[position:]
        new_lease = float(clock()) + config.lease_seconds
        renewed = store.renew_lease([row.event_id for row in remaining], lease, new_lease)
        if renewed < len(remaining):
            # 有行已被别人重领：本进程不再处理这批剩下的行（续租成功的那些等租约到期）。
            tally.fenced += len(remaining)
            logger.warning(
                "single-event fallback stopped: lease lost on %d of %d events",
                len(remaining) - renewed,
                len(remaining),
            )
            return
        lease = new_lease
        response = _post(
            transport,
            config,
            SINGLE_PATH,
            event.payload,
            clock=clock,
            new_request_id=new_request_id,
        )
        outcome = _single_outcome(response, event.event_id)
        if outcome.event_related:
            _settle(store, [(event, lease, outcome)], clock=clock, rng=rng, tally=tally)
            continue
        # 与这条事件无关的结果：这一条与剩下还没发的行都按它落状态，停止回退。
        _settle(
            store,
            [(row, lease, outcome) for row in remaining],
            clock=clock,
            rng=rng,
            tally=tally,
        )
        return


def run_delivery_round(
    store: Store,
    transport: Transport,
    config: BillingClientConfig,
    *,
    clock: Callable[[], float],
    rng: Uniform,
    new_request_id: Callable[[], str],
) -> DeliverySummary:
    """Claim once, send one batch, settle every row, purge old delivered rows.

    `clock` 给签名时间戳、退避与清理的 `now`；`rng` 给退避的 `uniform(0.5, 1.0)`；
    `new_request_id` 每次尝试生成一个新的 Request-Id。
    """
    tally = _Tally()
    events = store.claim(
        max_events=config.batch_max_events,
        max_bytes=config.batch_max_bytes,
        lease_seconds=config.lease_seconds,
    )
    tally.claimed = len(events)
    if events:
        response = _post(
            transport,
            config,
            BATCH_PATH,
            batch_body([event.payload for event in events]),
            clock=clock,
            new_request_id=new_request_id,
        )
        outcomes = _batch_outcomes(response, events)
        if isinstance(outcomes, list):
            rows = [
                (event, event.lease_until, outcome)
                for event, outcome in zip(events, outcomes, strict=True)
            ]
            _settle(store, rows, clock=clock, rng=rng, tally=tally)
        elif outcomes.kind is _Kind.FALLBACK:
            logger.warning(
                "batch rejected with %s; resending %d events one by one",
                outcomes.error_code,
                len(events),
            )
            _send_singly(
                store,
                transport,
                config,
                events,
                clock=clock,
                rng=rng,
                new_request_id=new_request_id,
                tally=tally,
            )
        else:
            rows = [(event, event.lease_until, outcomes) for event in events]
            _settle(store, rows, clock=clock, rng=rng, tally=tally)

    store.purge_delivered(
        older_than=float(clock()) - config.delivered_retention_seconds, limit=PURGE_LIMIT
    )
    if tally.delivered:
        store.record_delivery_ok()
    return tally.summary()


__all__ = [
    "AUTH_RETRY_SECONDS",
    "BATCH_PATH",
    "DEAD_LETTER_CODES",
    "INVALID_RECEIPT_CODE",
    "INVALID_RESPONSE_CODE",
    "NETWORK_ERROR_CODE",
    "PURGE_LIMIT",
    "SINGLE_PATH",
    "UNAVAILABLE_CODE",
    "UNKNOWN_USAGE_TYPE",
    "DeliverySummary",
    "backoff_seconds",
    "batch_body",
    "endpoint_url",
    "next_attempt_at",
    "run_delivery_round",
]
