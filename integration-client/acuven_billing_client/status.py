"""Effective-status reconcile and its cadence (design gate #571 v24 §2「有效状态」, §4, §5).

`reconcile_status_once` 一次对账：签名 `GET /api/v1/integration/effective-status`（空请求体、
超时取配置）→ 校验应答 → `apply_status`（只管版本规则）→ 写 `reconcile_state.last_ok_at`。
任何一步失败（网络、非 200、应答校验不过、读写库出错）都只记 WARNING、尽力写
`last_error_at`、返回假，不抛异常、不改 `status_state`。

- 应答校验与状态 webhook 共用 `parse_status`：含服务端的 10 个字段且类型对，多出的字段
  忽略、不存；`tenant_id` / `project_id` 等于配置；`status_version` 是 0 到 2^63 − 1 的整数
  （`bool` 不算）；`effective_status` 只能是 `ALLOW_AI` / `BLOCK_AI`。其余字符串字段不校验
  取值范围（服务端可能新增取值）。
- 节奏 `ReconcileSchedule`：启动立即查；成功后 240 秒；一段连续失败只在第一次失败后 30 秒
  补查一次，之后每次失败 240 秒。本类不加锁，由门面（`client.py`）串行使用。

⚠️ 日志（logger `acuven_billing_client`）只带字段名、状态码、`status_version` 与
`effective_status`，不记响应体、请求头与密钥（§6「日志」）。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable, Mapping
from typing import Any, Final
from urllib.parse import urlsplit

from .config import BillingClientConfig
from .delivery import endpoint_url
from .signing import request_headers
from .store import STATUS_FIELDS, Store, StoreError
from .transport import Transport, TransportError

logger = logging.getLogger("acuven_billing_client")

EFFECTIVE_STATUS_PATH: Final = "/api/v1/integration/effective-status"
EFFECTIVE_STATUSES: Final = frozenset({"ALLOW_AI", "BLOCK_AI"})
# status_state.status_version 是 SQLite INTEGER（有符号 64 位）。
MAX_STATUS_VERSION: Final = 2**63 - 1
# 对账线程每次最多等这么久（秒）就醒来重新读节奏状态。
RECONCILE_WAIT_MAX_SECONDS: Final = 5.0

# 有效状态应答的 10 个字段：tenant_id、project_id 加 status_state 的 8 列。
STATUS_RESPONSE_FIELDS: Final = ("tenant_id", "project_id", *STATUS_FIELDS)
_STRING_FIELDS: Final = (
    "tenant_id",
    "project_id",
    "billing_mode",
    "billing_status",
    "account_status",
    "project_integration_status",
    "effective_at",
)
# 读写库时按存储故障处理的异常（与门面同一组）。
STORE_ERRORS: Final = (sqlite3.Error, OSError, StoreError)


class StatusValidationError(ValueError):
    """A status document failed validation. The message names the field, never its value."""


def parse_status(
    document: object, config: BillingClientConfig, *, extra_fields: tuple[str, ...] = ()
) -> dict[str, Any]:
    """Validate one status object; return only the known fields (plus `extra_fields`).

    `extra_fields` 是另外必须存在的字符串字段（webhook 的 `event_id`）。
    """
    if not isinstance(document, Mapping):
        raise StatusValidationError("status is not an object")
    missing = [name for name in (*STATUS_RESPONSE_FIELDS, *extra_fields) if name not in document]
    if missing:
        raise StatusValidationError("status fields missing: " + ", ".join(missing))
    for name in (*_STRING_FIELDS, *extra_fields):
        if not isinstance(document[name], str):
            raise StatusValidationError(f"{name} must be a string")
    reason_code = document["reason_code"]
    if reason_code is not None and not isinstance(reason_code, str):
        raise StatusValidationError("reason_code must be a string or null")
    version = document["status_version"]
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or not 0 <= version <= MAX_STATUS_VERSION
    ):
        raise StatusValidationError("status_version must be a non-negative 64-bit integer")
    if document["effective_status"] not in EFFECTIVE_STATUSES:
        raise StatusValidationError("effective_status must be ALLOW_AI or BLOCK_AI")
    if document["tenant_id"] != config.tenant_id:
        raise StatusValidationError("tenant_id does not match the configuration")
    if document["project_id"] != config.project_id:
        raise StatusValidationError("project_id does not match the configuration")
    return {name: document[name] for name in (*STATUS_RESPONSE_FIELDS, *extra_fields)}


def fetch_effective_status(
    transport: Transport,
    config: BillingClientConfig,
    *,
    clock: Callable[[], float],
    new_request_id: Callable[[], str],
) -> dict[str, Any]:
    """One signed GET; the validated 10 fields, or `StatusValidationError` naming why not."""
    url = endpoint_url(config.base_url, EFFECTIVE_STATUS_PATH)
    headers = request_headers(
        config,
        "GET",
        urlsplit(url).path,
        b"",
        timestamp=int(clock()),
        request_id=new_request_id(),
    )
    try:
        response = transport.send("GET", url, headers, b"", config.http_timeout_seconds)
    except TransportError:
        raise StatusValidationError("network error") from None
    if response.status != 200:
        raise StatusValidationError(f"HTTP {response.status}")
    try:
        document = json.loads(response.body)
    except (ValueError, RecursionError):
        raise StatusValidationError("response is not JSON") from None
    if not isinstance(document, Mapping) or not isinstance(document.get("data"), Mapping):
        raise StatusValidationError("response has no data object")
    return parse_status(document["data"], config)


def reconcile_status_once(
    store: Store,
    transport: Transport,
    config: BillingClientConfig,
    *,
    clock: Callable[[], float],
    new_request_id: Callable[[], str],
    on_store_error: Callable[[BaseException], None] | None = None,
) -> bool:
    """Fetch, validate and apply once. True only when the answer passed validation.

    不抛异常。读写库出错时先尽力写 `last_error_at`，再把每个存储异常交给
    `on_store_error`（门面据此转入降级）。
    """
    tenant_id, project_id = config.tenant_id, config.project_id
    try:
        fields = fetch_effective_status(
            transport, config, clock=clock, new_request_id=new_request_id
        )
    except StatusValidationError as exc:
        logger.warning("effective-status reconcile failed: %s", exc)
        _record_error(store, config, clock, on_store_error)
        return False
    except Exception as exc:
        # Transport 或时钟的实现错误也只算一次失败；只记异常类型。
        logger.warning("effective-status reconcile failed: %s", type(exc).__name__)
        _record_error(store, config, clock, on_store_error)
        return False
    try:
        applied = store.apply_status(tenant_id, project_id, fields, now=float(clock()))
        store.record_reconcile_ok(tenant_id, project_id, now=float(clock()))
    except Exception as exc:
        logger.warning("effective-status reconcile failed on a store error: %s", type(exc).__name__)
        _record_error(store, config, clock, on_store_error)
        _report(exc, on_store_error)
        return False
    logger.info(
        "effective status reconciled: status_version=%d effective_status=%s applied=%s",
        fields["status_version"],
        fields["effective_status"],
        applied,
    )
    return True


def _record_error(
    store: Store,
    config: BillingClientConfig,
    clock: Callable[[], float],
    on_store_error: Callable[[BaseException], None] | None,
) -> None:
    try:
        store.record_reconcile_error(config.tenant_id, config.project_id, now=float(clock()))
    except Exception as exc:
        logger.warning("recording the reconcile failure failed: %s", type(exc).__name__)
        _report(exc, on_store_error)


def _report(exc: BaseException, on_store_error: Callable[[BaseException], None] | None) -> None:
    if on_store_error is not None and isinstance(exc, STORE_ERRORS):
        on_store_error(exc)


class ReconcileSchedule:
    """When the next reconcile is due (Unix seconds of the injected clock). Not thread-safe."""

    def __init__(self, interval_seconds: float, retry_seconds: float) -> None:
        self._interval = float(interval_seconds)
        self._retry = float(retry_seconds)
        # 第一次 start() 与第一次手动对账之前为 None。
        self.next_at: float | None = None
        # 本段连续失败是否已经补查过。
        self._retried = False

    def restart(self, now: float) -> None:
        """`start()`: reconcile at once; a new failure streak gets its retry again."""
        self.next_at = float(now)
        self._retried = False

    def succeeded(self, now: float) -> None:
        self.next_at = float(now) + self._interval
        self._retried = False

    def failed(self, now: float) -> None:
        if self._retried:
            self.next_at = float(now) + self._interval
        else:
            self.next_at = float(now) + self._retry
            self._retried = True

    def due(self, now: float) -> bool:
        return self.next_at is not None and float(now) >= self.next_at

    def wait_seconds(self, now: float, maximum: float) -> float:
        """How long to wait before reading the schedule again: at most `maximum`.

        已到期（例如存储不可用而跳过的那一轮）或还没有时刻时等满 `maximum`，不空转。
        """
        if self.next_at is None:
            return maximum
        remaining = self.next_at - float(now)
        if remaining <= 0:
            return maximum
        return min(maximum, remaining)


__all__ = [
    "EFFECTIVE_STATUSES",
    "EFFECTIVE_STATUS_PATH",
    "MAX_STATUS_VERSION",
    "RECONCILE_WAIT_MAX_SECONDS",
    "STATUS_RESPONSE_FIELDS",
    "ReconcileSchedule",
    "StatusValidationError",
    "fetch_effective_status",
    "parse_status",
    "reconcile_status_once",
]
