"""Acuven Billing Client (design gate #571 v24).

本版有配置、ID、事件校验与序列化、请求签名与 SQLite 存储（outbox 核心与运维、状态部分）；
投递、门面、状态与指标在 AIH-TASK-167..171。
"""

from .config import BillingClientConfig
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

# 与 integration-client/pyproject.toml 的 version 一致。
__version__ = "0.1.0"

__all__ = [
    "BillingClientConfig",
    "ClaimedEvent",
    "DeadEvent",
    "DeliveryCounts",
    "EventConflictError",
    "Fence",
    "Incident",
    "IncidentItem",
    "OverflowCount",
    "ReconcileTimes",
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
    "__version__",
    "build_llm_event",
    "build_quantity_event",
    "canonical_request",
    "new_ulid",
    "request_headers",
    "sign",
    "verify_webhook_signature",
]
