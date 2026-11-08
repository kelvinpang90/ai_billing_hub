"""Acuven Billing Client (design gate #571 v24).

本版有配置、ID、事件校验与序列化、请求签名、SQLite 存储（outbox 核心与运维、状态部分）、
HTTP 传输、一轮投递、有效状态对账与状态 webhook 验签，以及门面的记账、投递与状态部分；
指标与命令行在 AIH-TASK-170。
"""

from .client import BillingClient
from .config import BillingClientConfig
from .delivery import DeliverySummary, run_delivery_round
from .events import build_llm_event, build_quantity_event
from .ids import new_ulid
from .signing import canonical_request, request_headers, sign, verify_webhook_signature
from .status import ReconcileSchedule, StatusValidationError, parse_status, reconcile_status_once
from .store import (
    ClaimedEvent,
    DeadEvent,
    DeliveryCounts,
    EventConflictError,
    Fence,
    Incident,
    IncidentItem,
    OverflowCount,
    ReconcileTimes,
    RetryRow,
    StatusPair,
    Store,
    StoredStatus,
    StoreError,
    StoreForeignError,
    StoreIntegrityFault,
    StoreLockedError,
    StoreMissingError,
    StoreReplacedError,
    StoreResidueError,
    StoreUnavailableError,
    StoreVersionError,
)
from .transport import Response, Transport, TransportError, UrllibTransport
from .webhook import StatusStoreError, WebhookVerificationError, verify_status_webhook

# 与 integration-client/pyproject.toml 的 version 一致。
__version__ = "0.1.0"

__all__ = [
    "BillingClient",
    "BillingClientConfig",
    "ClaimedEvent",
    "DeadEvent",
    "DeliveryCounts",
    "DeliverySummary",
    "EventConflictError",
    "Fence",
    "Incident",
    "IncidentItem",
    "OverflowCount",
    "ReconcileSchedule",
    "ReconcileTimes",
    "Response",
    "RetryRow",
    "StatusPair",
    "StatusStoreError",
    "StatusValidationError",
    "Store",
    "StoreError",
    "StoreForeignError",
    "StoreIntegrityFault",
    "StoreLockedError",
    "StoreMissingError",
    "StoreReplacedError",
    "StoreResidueError",
    "StoreUnavailableError",
    "StoreVersionError",
    "StoredStatus",
    "Transport",
    "TransportError",
    "UrllibTransport",
    "WebhookVerificationError",
    "__version__",
    "build_llm_event",
    "build_quantity_event",
    "canonical_request",
    "new_ulid",
    "parse_status",
    "reconcile_status_once",
    "request_headers",
    "run_delivery_round",
    "sign",
    "verify_status_webhook",
    "verify_webhook_signature",
]
