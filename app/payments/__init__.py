"""Payment gateways (design gate #580 v3 §2「Stripe 适配器」, AIH-TASK-173).

路由、建单与事件任务都经 `get_payment_gateway` 取得网关；测试 monkeypatch 它注入假传输。
这一层不读写数据库。
"""

from __future__ import annotations

from app.core.config import Settings, read_stripe_credentials
from app.payments.base import (
    CreatedPayment,
    GatewayPaymentStatus,
    GatewayRequestRejected,
    GatewayState,
    GatewayUnavailable,
    ParsedEvent,
    PaymentGateway,
    WebhookPayloadError,
    WebhookSignatureError,
    amount_to_minor,
    minor_to_amount,
)
from app.payments.stripe_gateway import StripeGateway


def get_payment_gateway(settings: Settings) -> PaymentGateway | None:
    """The configured gateway, or `None` when payments are off or the credentials are unusable.

    凭据读不到时 `read_stripe_credentials` 已记 ERROR；调用方据 `None` 返回 503 或不领取事件。
    """
    credentials = read_stripe_credentials(settings)
    if credentials is None:
        return None
    return StripeGateway(
        credentials.secret_key,
        credentials.webhook_secret,
        timeout_seconds=settings.stripe_api_timeout_seconds,
        webhook_tolerance_seconds=settings.payment_webhook_tolerance_seconds,
        api_base_url=settings.stripe_api_base_url,
    )


__all__ = [
    "CreatedPayment",
    "GatewayPaymentStatus",
    "GatewayRequestRejected",
    "GatewayState",
    "GatewayUnavailable",
    "ParsedEvent",
    "PaymentGateway",
    "StripeGateway",
    "WebhookPayloadError",
    "WebhookSignatureError",
    "amount_to_minor",
    "get_payment_gateway",
    "minor_to_amount",
]
