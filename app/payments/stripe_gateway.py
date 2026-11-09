"""Stripe Checkout Session adapter (design gate #580 v3 §2「Stripe 适配器」, AIH-TASK-173).

同步实现，HTTP 用 `httpx2` 的同步客户端；可注入 `transport`，测试据此不联网。请求地址取配置的
`stripe_api_base_url`：**仓库里不写 Stripe 的主机名**（ADR-0001）。

错误分类（设计 §2）：

- 网络错误、超时、5xx、429，以及 2xx 但响应不是 JSON 或缺必需字段 → `GatewayUnavailable`；
- 3xx → `GatewayRequestRejected("UNEXPECTED_REDIRECT")`，**不跟随**；
- 其余 4xx → `GatewayRequestRejected`，带 Stripe 的错误码（`idempotency_key_in_use` 照样保留，
  供建单区分）。

⚠️ 异常与日志都不带请求体、响应体、签名头原文与密钥（设计 §6）；`httpx2` 的异常里带着请求
（含 `Authorization` 头），所以一律 `from None` 丢掉。
"""

from __future__ import annotations

import calendar
import datetime as dt
import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import quote

import httpx2

from app.core.config import (
    LIVE_SECRET_KEY_PREFIX,
    TEST_SECRET_KEY_PREFIX,
    WEBHOOK_SECRET_PREFIX,
    get_settings,
    require_https_base_url,
)
from app.payments.base import (
    CreatedPayment,
    GatewayPaymentStatus,
    GatewayRequestRejected,
    GatewayState,
    GatewayUnavailable,
    ParsedEvent,
    WebhookPayloadError,
    WebhookSignatureError,
    amount_to_minor,
)

if TYPE_CHECKING:
    from app.models.payments import Payment

CHECKOUT_SESSIONS_PATH: Final = "/v1/checkout/sessions"
STATUS_EXPAND: Final = "payment_intent.latest_charge.balance_transaction"
PRODUCT_NAME: Final = "Acuven AI 预付充值"
STRIPE_CURRENCY: Final = "myr"
SIGNATURE_HEADER: Final = "stripe-signature"

# 原因码（`GatewayUnavailable.code`）。
TIMEOUT: Final = "TIMEOUT"
NETWORK: Final = "NETWORK"
BAD_RESPONSE: Final = "BAD_RESPONSE"
UNEXPECTED_REDIRECT: Final = "UNEXPECTED_REDIRECT"

# 事件 id 与类型要放进 `payment_gateway_events` 的列里（设计 §2「数据库」）。
_EVENT_ID_MAX_LENGTH: Final = 255
_EVENT_TYPE_MAX_LENGTH: Final = 64
_ERROR_CODE_PATTERN: Final = re.compile(r"[a-z0-9_]{1,64}")
_TIMESTAMP_PATTERN: Final = re.compile(r"[0-9]{1,12}")
_TOO_MANY_REQUESTS: Final = 429


def _int_or_none(value: object) -> int | None:
    # JSON 的 true / false 解析成 bool，而 bool 是 int 的子类：不算数字。
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _upper_or_none(value: object) -> str | None:
    return value.upper() if isinstance(value, str) else None


def _object_id(value: object) -> str | None:
    """An expandable Stripe reference: either the id string or the expanded object's `id`."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return _str_or_none(value.get("id"))
    return None


def _reject_constant(_name: str) -> object:
    # `NaN`、`Infinity` 不是合法 JSON，json 模块默认却收下它们。
    raise ValueError("not a JSON number")


def _load_object(body: bytes) -> dict[str, Any] | None:
    """The body as a JSON object, or `None` when it is not one."""
    try:
        payload = json.loads(body, parse_constant=_reject_constant)
    except ValueError:
        # JSONDecodeError 与 UnicodeDecodeError 都是 ValueError。
        return None
    return payload if isinstance(payload, dict) else None


def _unix_seconds(moment: dt.datetime) -> int:
    """Seconds since the epoch; a naive datetime is UTC (仓库的时间语义)."""
    return calendar.timegm(moment.utctimetuple())


def _stripe_error_code(response: httpx2.Response) -> str:
    payload = _load_object(response.content)
    error = payload.get("error") if payload is not None else None
    code = error.get("code") if isinstance(error, dict) else None
    if isinstance(code, str) and _ERROR_CODE_PATTERN.fullmatch(code):
        return code
    return f"HTTP_{response.status_code}"


def _required_strings(payload: dict[str, Any], names: tuple[str, ...]) -> None:
    for name in names:
        value = payload.get(name)
        if not isinstance(value, str) or not value:
            raise GatewayUnavailable(BAD_RESPONSE)


def map_session_state(
    session_status: str,
    payment_status: str,
    payment_intent_status: str | None,
    amount_total: int | None,
    amount_received: int | None,
) -> GatewayState:
    """Checkout Session → `GatewayState` (设计 §2 映射表)."""
    if session_status == "open":
        return GatewayState.OPEN
    if session_status == "expired":
        return GatewayState.EXPIRED
    if session_status != "complete":
        return GatewayState.UNKNOWN
    if payment_status == "paid":
        if (
            payment_intent_status == "succeeded"
            and amount_total is not None
            and amount_received is not None
            and amount_received == amount_total
        ):
            return GatewayState.PAID
        return GatewayState.INCONSISTENT
    if payment_status == "no_payment_required":
        # 本系统的金额都大于 0，不该出现。
        return GatewayState.INCONSISTENT
    if payment_status == "unpaid":
        if payment_intent_status in ("requires_payment_method", "canceled"):
            return GatewayState.FAILED
        return GatewayState.OPEN
    # complete 而 payment_status 是表外取值。
    return GatewayState.UNKNOWN


def parse_session(payload: dict[str, Any]) -> GatewayPaymentStatus:
    """A Checkout Session (with the payment intent expanded) → `GatewayPaymentStatus`.

    必需字段 `id`、`status`、`payment_status`，缺了是 `GatewayUnavailable`；`url` 不必需
    （complete 的会话上为 null）。
    """
    _required_strings(payload, ("id", "status", "payment_status"))
    amount_total = _int_or_none(payload.get("amount_total"))

    intent = payload.get("payment_intent")
    payment_intent_id = _object_id(intent)
    payment_intent_status: str | None = None
    amount_received: int | None = None
    fee_minor: int | None = None
    fee_currency: str | None = None
    if isinstance(intent, dict):
        payment_intent_status = _str_or_none(intent.get("status"))
        amount_received = _int_or_none(intent.get("amount_received"))
        charge = intent.get("latest_charge")
        balance = charge.get("balance_transaction") if isinstance(charge, dict) else None
        if isinstance(balance, dict):
            fee_minor = _int_or_none(balance.get("fee"))
            fee_currency = _upper_or_none(balance.get("currency"))

    state = map_session_state(
        payload["status"],
        payload["payment_status"],
        payment_intent_status,
        amount_total,
        amount_received,
    )
    return GatewayPaymentStatus(
        state=state,
        session_id=payload["id"],
        amount_minor=amount_total,
        currency=_upper_or_none(payload.get("currency")),
        client_reference_id=_str_or_none(payload.get("client_reference_id")),
        payment_intent_id=payment_intent_id,
        payment_intent_status=payment_intent_status,
        amount_received_minor=amount_received,
        fee_minor=fee_minor,
        fee_currency=fee_currency,
        redirect_url=_str_or_none(payload.get("url")),
    )


def _signature_header(headers: Mapping[str, str]) -> str | None:
    # 请求头名不区分大小写；`dict` 与 Starlette 的 `Headers` 都按这里的写法找。
    for name, value in headers.items():
        if name.lower() == SIGNATURE_HEADER:
            return value
    return None


def _parse_signature_header(header: str) -> tuple[int, list[str]]:
    """`t=<unix>,v1=<hex>,v1=<hex>,…` → (t, all v1). Malformed → `WebhookSignatureError`."""
    timestamps: list[str] = []
    signatures: list[str] = []
    for item in header.split(","):
        key, separator, value = item.strip().partition("=")
        if not separator:
            raise WebhookSignatureError
        if key == "t":
            timestamps.append(value)
        elif key == "v1":
            signatures.append(value)
    if len(timestamps) != 1 or not _TIMESTAMP_PATTERN.fullmatch(timestamps[0]) or not signatures:
        raise WebhookSignatureError
    return int(timestamps[0]), signatures


def _event_fields(event: dict[str, Any]) -> dict[str, Any]:
    """The §6 whitelist. Never the raw body, never the Stripe object's own `metadata`."""
    data = event.get("data")
    obj = data.get("object") if isinstance(data, dict) else None
    if not isinstance(obj, dict):
        obj = {}
    return {
        "type": event["type"],
        "livemode": event["livemode"],
        "created": _int_or_none(event.get("created")),
        "object_id": _str_or_none(obj.get("id")),
        "client_reference_id": _str_or_none(obj.get("client_reference_id")),
        "payment_status": _str_or_none(obj.get("payment_status")),
        "status": _str_or_none(obj.get("status")),
        "amount_total": _int_or_none(obj.get("amount_total")),
        "currency": _str_or_none(obj.get("currency")),
        "payment_intent": _object_id(obj.get("payment_intent")),
    }


class StripeGateway:
    """Stripe Checkout Session (托管付款页) as a `PaymentGateway`.

    `api_base_url` 不传时取 `get_settings().stripe_api_base_url`；为空时构造失败
    （正常路径经 `get_payment_gateway`，那里已判定它不为空）。
    """

    def __init__(
        self,
        secret_key: str,
        webhook_secret: str,
        *,
        timeout_seconds: float,
        webhook_tolerance_seconds: int,
        transport: httpx2.BaseTransport | None = None,
        api_base_url: str | None = None,
    ) -> None:
        if api_base_url is None:
            api_base_url = get_settings().stripe_api_base_url
        if not api_base_url.strip():
            raise ValueError("stripe_api_base_url is not configured")
        self._api_base_url = require_https_base_url(api_base_url)
        self._secret_key = secret_key
        self._webhook_secret = webhook_secret
        self._timeout = timeout_seconds
        self._tolerance = webhook_tolerance_seconds
        self._transport = transport

    def __repr__(self) -> str:
        # ⚠️ 不带密钥。
        return f"StripeGateway(livemode={self.livemode})"

    @property
    def livemode(self) -> bool:
        return self._secret_key.startswith(LIVE_SECRET_KEY_PREFIX)

    # --- Stripe API ---------------------------------------------------------

    def create_payment(
        self, payment: Payment, *, success_url: str, cancel_url: str
    ) -> CreatedPayment:
        """`POST /v1/checkout/sessions` (设计 §2)。

        不传 `payment_method_types`：付款方式由 Dashboard 的开关决定（ADR-0015）。
        """
        public_id = payment.public_id
        form = {
            "mode": "payment",
            "line_items[0][quantity]": "1",
            "line_items[0][price_data][currency]": STRIPE_CURRENCY,
            "line_items[0][price_data][unit_amount]": str(amount_to_minor(payment.expected_amount)),
            "line_items[0][price_data][product_data][name]": PRODUCT_NAME,
            "client_reference_id": public_id,
            "metadata[payment_id]": public_id,
            "expires_at": str(_unix_seconds(payment.expires_at)),
            "success_url": success_url,
            "cancel_url": cancel_url,
        }
        payload = self._request(
            "POST",
            CHECKOUT_SESSIONS_PATH,
            data=form,
            headers={"Idempotency-Key": f"topup-{public_id}"},
        )
        _required_strings(payload, ("id", "url", "status"))
        return CreatedPayment(gateway_payment_id=payload["id"], redirect_url=payload["url"])

    def get_payment_status(self, gateway_payment_id: str) -> GatewayPaymentStatus:
        """`GET /v1/checkout/sessions/{id}`, expanding the payment intent down to its fee."""
        if not gateway_payment_id:
            raise ValueError("gateway_payment_id is required")
        payload = self._request(
            "GET",
            f"{CHECKOUT_SESSIONS_PATH}/{quote(gateway_payment_id, safe='')}",
            params={"expand[]": STATUS_EXPAND},
        )
        return parse_session(payload)

    def _request(
        self,
        method: str,
        path: str,
        *,
        data: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Send one request; return the JSON object of a 2xx, else raise the classified error."""
        request_headers = {"Authorization": f"Bearer {self._secret_key}", **(headers or {})}
        try:
            with httpx2.Client(
                timeout=self._timeout, transport=self._transport, follow_redirects=False
            ) as client:
                response = client.request(
                    method,
                    f"{self._api_base_url}{path}",
                    data=data,
                    params=params,
                    headers=request_headers,
                )
        except httpx2.TimeoutException:
            raise GatewayUnavailable(TIMEOUT) from None
        except (httpx2.RequestError, OSError):
            raise GatewayUnavailable(NETWORK) from None
        status = response.status_code
        if 300 <= status < 400:
            raise GatewayRequestRejected(UNEXPECTED_REDIRECT, status)
        if status == _TOO_MANY_REQUESTS or status >= 500 or status < 200:
            raise GatewayUnavailable(f"HTTP_{status}")
        if 400 <= status < 500:
            raise GatewayRequestRejected(_stripe_error_code(response), status)
        payload = _load_object(response.content)
        if payload is None:
            raise GatewayUnavailable(BAD_RESPONSE)
        return payload

    # --- Webhook -------------------------------------------------------------

    def verify_webhook(
        self, raw_body: bytes, headers: Mapping[str, str], *, now: dt.datetime
    ) -> None:
        """Check `HMAC-SHA256(webhook_secret, t + "." + raw_body)` against every `v1`.

        `|now − t|` 不得大于 `webhook_tolerance_seconds`。

        请求头缺失、格式不对、签名都不对或在时间窗外，一律 `WebhookSignatureError`（不带原文）。
        """
        header = _signature_header(headers)
        if header is None:
            raise WebhookSignatureError
        timestamp, signatures = _parse_signature_header(header)
        expected = hmac.new(
            self._webhook_secret.encode("utf-8"),
            str(timestamp).encode("ascii") + b"." + raw_body,
            hashlib.sha256,
        ).hexdigest()
        matched = False
        for signature in signatures:
            # 逐个比完，不在第一个命中时提前返回。
            if hmac.compare_digest(expected.encode("ascii"), signature.encode("utf-8")):
                matched = True
        if not matched:
            raise WebhookSignatureError
        if abs(_unix_seconds(now) - timestamp) > self._tolerance:
            raise WebhookSignatureError

    def parse_webhook(self, raw_body: bytes) -> ParsedEvent:
        """A verified body → `ParsedEvent`, keeping only the §6 whitelist."""
        event = _load_object(raw_body)
        if event is None:
            raise WebhookPayloadError
        event_id = event.get("id")
        event_type = event.get("type")
        livemode = event.get("livemode")
        if (
            not isinstance(event_id, str)
            or not event_id
            or len(event_id) > _EVENT_ID_MAX_LENGTH
            or not isinstance(event_type, str)
            or not event_type
            or len(event_type) > _EVENT_TYPE_MAX_LENGTH
            or not isinstance(livemode, bool)
        ):
            raise WebhookPayloadError
        return ParsedEvent(
            gateway_event_id=event_id,
            event_type=event_type,
            livemode=livemode,
            fields=_event_fields(event),
        )


__all__ = [
    "BAD_RESPONSE",
    "CHECKOUT_SESSIONS_PATH",
    "LIVE_SECRET_KEY_PREFIX",
    "NETWORK",
    "PRODUCT_NAME",
    "STATUS_EXPAND",
    "TEST_SECRET_KEY_PREFIX",
    "TIMEOUT",
    "UNEXPECTED_REDIRECT",
    "WEBHOOK_SECRET_PREFIX",
    "StripeGateway",
    "map_session_state",
    "parse_session",
]
