"""Payment gateway protocol, data classes and errors (design gate #580 v3 §2, AIH-TASK-173).

ADR-0006：业务代码只依赖 `PaymentGateway` 协议，实现由 `app.payments.get_payment_gateway` 一处
决定。**这一层不读写数据库**：确认与入账属于 AIH-TASK-174。

⚠️ 异常只带错误码与 HTTP 状态，**不带请求体、响应体与密钥**（设计 §6）。
"""

from __future__ import annotations

import datetime as dt
import enum
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final, Protocol

if TYPE_CHECKING:
    from app.models.payments import Payment

# 金额两位小数，与 Stripe 的整数分互换（设计 §2「金额与精度」）。
MINOR_UNIT_PLACES: Final = 2


def amount_to_minor(amount: Decimal) -> int:
    """`100.00` → `10000`: `amount × 100`, exactly an integer, never via `float`.

    不是有限值、或乘以 100 后不是整数（多于两位小数）时抛 `ValueError`，不舍入。
    """
    if not isinstance(amount, Decimal) or not amount.is_finite():
        raise ValueError("amount must be a finite Decimal")
    scaled = amount.scaleb(MINOR_UNIT_PLACES)
    minor = int(scaled)
    if minor != scaled:
        raise ValueError("amount has more than two decimal places")
    return minor


def minor_to_amount(minor: int) -> Decimal:
    """`9999` → `Decimal("99.99")`, exactly."""
    if type(minor) is not int:
        raise ValueError("minor amount must be an integer")
    return Decimal(minor).scaleb(-MINOR_UNIT_PLACES)


class GatewayState(enum.StrEnum):
    """The authoritative state of one gateway payment (设计 §2 映射表，spec §63)."""

    PAID = "PAID"
    OPEN = "OPEN"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    # 按不可信处理，确认时走 `REVIEW_REQUIRED`。
    INCONSISTENT = "INCONSISTENT"
    # 不改状态，事件按可重试处理。
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class CreatedPayment:
    """The gateway object a top-up was created as, and where the customer pays."""

    gateway_payment_id: str
    redirect_url: str


@dataclass(frozen=True)
class GatewayPaymentStatus:
    """What the gateway reports for one payment (设计 §2「GatewayPaymentStatus 的字段」).

    金额都是整数分；币种都已转成大写。`redirect_url` 只在会话还开着时有值。

    `charge_amount_refunded_minor` 与 `charge_disputed` 取自已展开的
    `payment_intent.latest_charge`（设计闸门 #585 v3 §2「对 A 的适配器的扩展」），没有
    `latest_charge` 时为 `None`。A 的状态映射与可信确认不用它们，只有人工按实付入账
    （app/services/payment_review.py）用。

    `payment_method_type` 取自同一个 `latest_charge` 的 `payment_method_details.type`
    （设计闸门 #589 v2 §2「付款方式」），只供收据快照用；拿不到或不合规时为 `None`。
    """

    state: GatewayState
    session_id: str
    amount_minor: int | None
    currency: str | None
    client_reference_id: str | None
    payment_intent_id: str | None
    payment_intent_status: str | None
    amount_received_minor: int | None
    fee_minor: int | None
    fee_currency: str | None
    redirect_url: str | None
    charge_amount_refunded_minor: int | None = None
    charge_disputed: bool | None = None
    payment_method_type: str | None = None


@dataclass(frozen=True)
class ParsedEvent:
    """A verified webhook body, reduced to what may be stored.

    `fields` 只含设计 §6 的白名单字段（直接写进 `metadata_json`），不含原始请求体，也不含
    Stripe 对象自己的 `metadata`。
    """

    gateway_event_id: str
    event_type: str
    livemode: bool
    fields: Mapping[str, Any]


class GatewayUnavailable(Exception):
    """Network error, timeout, 5xx, 429 or an unusable 2xx: retryable.

    `code` 是本系统的原因码（例如 `TIMEOUT`、`HTTP_503`、`BAD_RESPONSE`），不带响应体。
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class GatewayRequestRejected(Exception):
    """The gateway refused the request (4xx other than 429, or a 3xx): not retryable.

    `code` 是 Stripe 的错误码（例如 `resource_missing`、`idempotency_key_in_use`）；响应里
    没有可用的错误码时为 `HTTP_<status>`；3xx 为 `UNEXPECTED_REDIRECT`。不带请求体与密钥。
    """

    def __init__(self, code: str, status_code: int) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


class WebhookSignatureError(Exception):
    """Missing or malformed signature header, wrong signature, or outside the time window."""


class WebhookPayloadError(Exception):
    """A verified body that is not a usable event (not JSON, no `id` / `type` / `livemode`)."""


class PaymentGateway(Protocol):
    """A payment gateway (ADR-0006). Synchronous: the callers are sync services and tasks.

    `list_settlements` 本批不实现（B / 对账另定）。
    """

    @property
    def livemode(self) -> bool:
        """Whether the configured secret key is a live-mode key."""
        ...

    def create_payment(
        self, payment: Payment, *, success_url: str, cancel_url: str
    ) -> CreatedPayment:
        """Create the hosted payment page for `payment`.

        失败抛 `GatewayUnavailable` 或 `GatewayRequestRejected`。
        """
        ...

    def get_payment_status(self, gateway_payment_id: str) -> GatewayPaymentStatus:
        """Ask the gateway for the authoritative state of one payment."""
        ...

    def verify_webhook(
        self, raw_body: bytes, headers: Mapping[str, str], *, now: dt.datetime
    ) -> None:
        """Return when the signature is valid; otherwise raise `WebhookSignatureError`."""
        ...

    def parse_webhook(self, raw_body: bytes) -> ParsedEvent:
        """Parse a verified body; raise `WebhookPayloadError` when it is not a usable event."""
        ...


__all__ = [
    "MINOR_UNIT_PLACES",
    "CreatedPayment",
    "GatewayPaymentStatus",
    "GatewayRequestRejected",
    "GatewayState",
    "GatewayUnavailable",
    "ParsedEvent",
    "PaymentGateway",
    "WebhookPayloadError",
    "WebhookSignatureError",
    "amount_to_minor",
    "minor_to_amount",
]
