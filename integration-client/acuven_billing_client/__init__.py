"""Acuven Billing Client (design gate #571 v24).

本版有配置、ID、事件校验与序列化、请求签名、SQLite 存储（outbox 核心与运维、状态部分）、
HTTP 传输与一轮投递；门面、状态与指标在 AIH-TASK-168..171。
"""

from .config import BillingClientConfig
from .delivery import DeliverySummary, run_delivery_round
from .events import build_llm_event, build_quantity_event
from .ids import new_ulid
from .signing import canonical_request, request_headers, sign, verify_webhook_signature
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

# 与 integration-client/pyproject.toml 的 version 一致。
__version__ = "0.1.0"

__all__ = [
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
    "ReconcileTimes",
    "Response",
    "RetryRow",
    "StatusPair",
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
    "__version__",
    "build_llm_event",
    "build_quantity_event",
    "canonical_request",
    "new_ulid",
    "request_headers",
    "run_delivery_round",
    "sign",
    "verify_webhook_signature",
]
