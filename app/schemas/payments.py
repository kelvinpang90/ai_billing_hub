"""Request and response shapes of the payment routes (design gate #580 v3 §2, AIH-TASK-177).

⚠️ **建单请求体只有 `amount`**（`extra="forbid"`：带 `tenant_id`、`customer_id` 等多余字段是 422
`VALIDATION_ERROR`，INV-8）。`amount` **不声明成数字类型**：原值交给 `create_topup`，缺省、`null`、
数字、布尔都按设计 §2「建单」校验 3 报 422 `TOPUP_AMOUNT_INVALID`，在它的有序校验里判。

⚠️ **建单响应是客户侧的字段白名单**（INV-7）：恰好 `payment_id`、`redirect_url`、`expires_at`。
手续费、状态原因与 Stripe 对象 id 都不作为字段返回（设计 §6）；`redirect_url` 是 Stripe 托管页的
地址，其中自带会话 id，这是付款所必需的。

回调的错误码见设计 §2「支付 Webhook」；响应体用仓库现有的错误信封。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.core.errors import AppError


class TopupRequest(BaseModel):
    """`{"amount": "100.00"}`. Anything but `amount` is refused."""

    model_config = ConfigDict(extra="forbid")

    # 原值；缺省为 `None`，由 `create_topup` 报 `TOPUP_AMOUNT_INVALID`。
    amount: Any = None


class TopupCreated(BaseModel):
    """The top-up as the customer sees it: the payment, where to pay, until when."""

    # `payments.public_id`。
    payment_id: str
    redirect_url: str
    # 不带时区的 UTC，精确到秒。
    expires_at: dt.datetime


class WebhookReceipt(BaseModel):
    """The 200 body of a webhook delivery: recorded, a redelivery, or a conflicting body alike."""

    received: bool = True


class PaymentGatewayNotFound(AppError):
    """`{gateway}` is not a gateway this system takes webhooks from."""

    def __init__(self) -> None:
        super().__init__("Not found.", code="NOT_FOUND", http_status=404)


class WebhookSignatureInvalid(AppError):
    """Missing or malformed signature header, wrong signature, or outside the time window."""

    def __init__(self) -> None:
        super().__init__(
            "The webhook signature is invalid.", code="WEBHOOK_SIGNATURE_INVALID", http_status=400
        )


class WebhookPayloadInvalid(AppError):
    """A correctly signed body that is not a usable event (not JSON, no `id` / `type`)."""

    def __init__(self) -> None:
        super().__init__(
            "The webhook payload is invalid.", code="WEBHOOK_PAYLOAD_INVALID", http_status=400
        )


__all__ = [
    "PaymentGatewayNotFound",
    "TopupCreated",
    "TopupRequest",
    "WebhookPayloadInvalid",
    "WebhookReceipt",
    "WebhookSignatureInvalid",
]
