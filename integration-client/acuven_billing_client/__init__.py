"""Acuven Billing Client (design gate #571 v24).

本版只有配置、ID、事件校验与序列化、请求签名；
存储、投递、门面、状态与指标在 AIH-TASK-165..171。
"""

from .config import BillingClientConfig
from .events import build_llm_event, build_quantity_event
from .ids import new_ulid
from .signing import canonical_request, request_headers, sign, verify_webhook_signature

# 与 integration-client/pyproject.toml 的 version 一致。
__version__ = "0.1.0"

__all__ = [
    "BillingClientConfig",
    "__version__",
    "build_llm_event",
    "build_quantity_event",
    "canonical_request",
    "new_ulid",
    "request_headers",
    "sign",
    "verify_webhook_signature",
]
