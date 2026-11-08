"""Effective-status reconcile and the version rules (design gate #571 v24 §2「有效状态」, §4).

AIH-TASK-169：签名 `GET /api/v1/integration/effective-status`、校验应答、按版本应用、写对账
时刻，以及对账节奏。应答校验 `validate_status` 与状态 webhook（`webhook.py`）共用。

- **校验**：`data` 是对象且含服务端的 10 个字段、类型对；`tenant_id` 与 `project_id` 等于
  配置；`status_version` 为 0 到 2^63 − 1 的整数（`bool` 不算，更大的值 SQLite 存不下）；
  `effective_status` 只能是 `ALLOW_AI` / `BLOCK_AI`。其余字符串字段不校验取值范围（服务端
  可能新增取值）；多出的字段忽略、不存（INV-7）。
- **成功**：应答通过校验后 `apply_status`，再 `record_reconcile_ok`（不论是否覆盖）。
- **失败**：HTTP 失败、应答不过校验、读写库抛错都算一次失败：记 WARNING、尽力
  `record_reconcile_error`、不应用。存储错误交给调用方（门面）按降级规则处理。
- **节奏**：成功后 `reconcile_interval_seconds`（240 秒）；一段连续失败只在第一次失败后
  `reconcile_retry_seconds`（30 秒）补查一次，之后每次失败 240 秒。

⚠️ 日志只带状态码、异常类型、字段名、`status_version` 与 `effective_status`，不记应答原文
与租户、项目的值（§6「日志」）。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Callable
from typing import Any, Final
from urllib.parse import urlsplit

from .config import BillingClientConfig
from .delivery import endpoint_url
from .signing import request_headers
from .store import STATUS_FIELDS, Store, StoreError
from .transport import Transport

logger = logging.getLogger("acuven_billing_client")

EFFECTIVE_STATUS_PATH: Final = "/api/v1/integration/effective-status"
EFFECTIVE_STATUSES: Final = frozenset({"ALLOW_AI", "BLOCK_AI"})
# SQLite INTEGER 的上限；更大的版本按字段校验不通过处理，不让 OverflowError 漏出。
MAX_STATUS_VERSION: Final = 2**63 - 1
# 服务端有效状态应答的 10 个字段（webhook 载荷另加 event_id）。
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
_STORE_ERRORS: Final = (sqlite3.Error, OSError, StoreError)

StoreErrorHook = Callable[[BaseException], None]


class StatusValidationError(ValueError):
    """The status object failed the check; the message names the field, never a value."""


def validate_status(data: object, config: BillingClientConfig) -> dict[str, Any]:
    """Check an effective-status object; return only the 10 known fields."""
    if not isinstance(data, dict):
        raise StatusValidationError("status is not an object")
    missing = [name for name in STATUS_RESPONSE_FIELDS if name not in data]
    if missing:
        raise StatusValidationError("status fields missing: " + ", ".join(missing))
    for name in _STRING_FIELDS:
        if not isinstance(data[name], str):
            raise StatusValidationError(f"{name} must be a string")
    reason_code = data["reason_code"]
    if reason_code is not None and not isinstance(reason_code, str):
        raise StatusValidationError("reason_code must be a string or null")
    version = data["status_version"]
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or not 0 <= version <= MAX_STATUS_VERSION
    ):
        raise StatusValidationError("status_version must be a non-negative 64-bit integer")
    if data["effective_status"] not in EFFECTIVE_STATUSES:
        raise StatusValidationError("effective_status must be ALLOW_AI or BLOCK_AI")
    if data["tenant_id"] != config.tenant_id or data["project_id"] != config.project_id:
        raise StatusValidationError("tenant_id or project_id does not match the configuration")
    return {name: data[name] for name in STATUS_RESPONSE_FIELDS}


# --- 节奏 -------------------------------------------------------------------------------


class ReconcileSchedule:
    """When the next reconcile is due; shared by the reconcile thread and manual calls.

    第一次 `reset`（start）与第一次 `record`（手动对账）之前 `next_at` 为 None。
    对账轮次的串行由门面的锁负责，这里只护自己的两个值。
    """

    def __init__(self, *, interval_seconds: float, retry_seconds: float) -> None:
        self._interval = float(interval_seconds)
        self._retry = float(retry_seconds)
        self._lock = threading.Lock()
        self._next_at: float | None = None
        # 本段连续失败是否已经用过那一次补查。
        self._retry_used = False

    @property
    def next_at(self) -> float | None:
        with self._lock:
            return self._next_at

    def reset(self, *, now: float) -> None:
        """Due at once; the next failure streak gets its retry again."""
        with self._lock:
            self._next_at = float(now)
            self._retry_used = False

    def record(self, ok: bool, *, now: float) -> float:
        """Set the next time from this round's result; returns it."""
        with self._lock:
            if ok:
                self._retry_used = False
                delay = self._interval
            elif not self._retry_used:
                self._retry_used = True
                delay = self._retry
            else:
                delay = self._interval
            self._next_at = float(now) + delay
            return self._next_at


# --- 一次对账 ---------------------------------------------------------------------------


def _fetch(
    transport: Transport,
    config: BillingClientConfig,
    *,
    clock: Callable[[], float],
    new_request_id: Callable[[], str],
) -> dict[str, Any] | None:
    """One signed GET; the validated fields, or None (logged) when anything is off."""
    url = endpoint_url(config.base_url, EFFECTIVE_STATUS_PATH)
    try:
        # 签名用实际请求 URL 的路径部分（含 base_url 的前缀），没有查询串；空请求体。
        headers = request_headers(
            config,
            "GET",
            urlsplit(url).path,
            b"",
            timestamp=int(clock()),
            request_id=new_request_id(),
        )
        response = transport.send("GET", url, headers, b"", config.http_timeout_seconds)
    except Exception as exc:
        logger.warning("status reconcile request failed: %s", type(exc).__name__)
        return None
    if response.status != 200:
        logger.warning("status reconcile got HTTP %s", response.status)
        return None
    try:
        document = json.loads(response.body)
    except (ValueError, RecursionError):
        logger.warning("status reconcile response is not JSON")
        return None
    if not isinstance(document, dict):
        logger.warning("status reconcile response is not an object")
        return None
    try:
        return validate_status(document.get("data"), config)
    except StatusValidationError as exc:
        logger.warning("status reconcile response rejected: %s", exc)
        return None


def _record_error(
    store: Store,
    config: BillingClientConfig,
    clock: Callable[[], float],
    on_store_error: StoreErrorHook,
) -> None:
    # 尽力而为：写不进去只记 WARNING，并照样交给降级规则。
    try:
        store.record_reconcile_error(config.tenant_id, config.project_id, now=float(clock()))
    except _STORE_ERRORS as exc:
        logger.warning("recording the reconcile failure failed: %s", type(exc).__name__)
        on_store_error(exc)


def reconcile_status_once(
    store: Store,
    transport: Transport,
    config: BillingClientConfig,
    *,
    clock: Callable[[], float],
    new_request_id: Callable[[], str],
    on_store_error: StoreErrorHook,
) -> bool:
    """Fetch, check, apply by version, record the reconcile time. Returns whether it succeeded.

    读写库的错误不抛出：记 WARNING、算一次失败，并交给 `on_store_error`（门面据此转入降级）。
    """
    fields = _fetch(transport, config, clock=clock, new_request_id=new_request_id)
    if fields is None:
        _record_error(store, config, clock, on_store_error)
        return False
    try:
        applied = store.apply_status(
            config.tenant_id, config.project_id, fields, now=float(clock())
        )
        store.record_reconcile_ok(config.tenant_id, config.project_id, now=float(clock()))
    except _STORE_ERRORS as exc:
        logger.warning("status reconcile failed on the store: %s", type(exc).__name__)
        on_store_error(exc)
        _record_error(store, config, clock, on_store_error)
        return False
    if applied:
        logger.info(
            "effective status applied by reconcile: status_version=%d effective_status=%s",
            fields["status_version"],
            fields["effective_status"],
        )
    return True


__all__ = [
    "EFFECTIVE_STATUSES",
    "EFFECTIVE_STATUS_PATH",
    "MAX_STATUS_VERSION",
    "STATUS_RESPONSE_FIELDS",
    "ReconcileSchedule",
    "StatusValidationError",
    "reconcile_status_once",
    "validate_status",
]
