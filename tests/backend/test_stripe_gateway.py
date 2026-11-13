"""Stripe 适配器（设计闸门 #580 v3 §2「Stripe 适配器」「金额与精度」、§6、§7 标 173 的各行）。

一律用注入的假传输（`httpx2.MockTransport`），不联网。
"""

from __future__ import annotations

import calendar
import datetime as dt
import hashlib
import hmac
import json
from collections.abc import Callable
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs

import httpx2
import pytest

from app.models.payments import Payment
from app.payments import (
    CreatedPayment,
    GatewayRequestRejected,
    GatewayState,
    GatewayUnavailable,
    StripeGateway,
    WebhookPayloadError,
    WebhookSignatureError,
    amount_to_minor,
    minor_to_amount,
)
from app.payments.base import GatewayPaymentStatus
from app.payments.stripe_gateway import PRODUCT_NAME, STATUS_EXPAND

SECRET_KEY = "sk_test_gateway_secret_value_0123456789"
WEBHOOK_SECRET = "whsec_gateway_signing_value_0123456789"
API_BASE_URL = "https://stripe-api.example.test"
PUBLIC_ID = "6f1d3c2b-5a4e-4f6d-9c8b-7a6e5d4c3b2a"
SESSION_ID = "cs_test_session_0001"
EXPIRES_AT = dt.datetime(2026, 11, 9, 12, 0, 0)
NOW = dt.datetime(2026, 11, 9, 10, 0, 0)
SUCCESS_URL = f"https://portal.example.test/portal/payments/{PUBLIC_ID}?result=success"
CANCEL_URL = f"https://portal.example.test/portal/payments/{PUBLIC_ID}?result=cancelled"

Handler = Callable[[httpx2.Request], httpx2.Response]


class Recorder:
    """A fake Stripe: answers with `respond` and keeps every request it saw."""

    def __init__(self, respond: Handler) -> None:
        self.respond = respond
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self.respond(request)


def gateway_with(respond: Handler) -> tuple[StripeGateway, Recorder]:
    recorder = Recorder(respond)
    gateway = StripeGateway(
        SECRET_KEY,
        WEBHOOK_SECRET,
        timeout_seconds=10,
        webhook_tolerance_seconds=300,
        transport=httpx2.MockTransport(recorder),
        api_base_url=API_BASE_URL,
    )
    return gateway, recorder


def payment(amount: str = "100.00") -> Payment:
    return Payment(
        public_id=PUBLIC_ID,
        expected_amount=Decimal(amount),
        expected_currency="MYR",
        expires_at=EXPIRES_AT,
    )


def created_session(**overrides: Any) -> dict[str, Any]:
    body = {
        "id": SESSION_ID,
        "object": "checkout.session",
        "url": f"https://checkout.example.test/pay/{SESSION_ID}",
        "status": "open",
        "payment_status": "unpaid",
    }
    body.update(overrides)
    return body


def session(
    status: str = "complete",
    payment_status: str = "paid",
    intent: Any = "default",
    **overrides: Any,
) -> dict[str, Any]:
    if intent == "default":
        intent = payment_intent()
    body: dict[str, Any] = {
        "id": SESSION_ID,
        "object": "checkout.session",
        "status": status,
        "payment_status": payment_status,
        "amount_total": 10000,
        "currency": "myr",
        "client_reference_id": PUBLIC_ID,
        "payment_intent": intent,
        "url": None,
        "customer_details": {"email": "payer@example.test", "name": "Payer"},
    }
    body.update(overrides)
    return body


def payment_intent(
    status: str = "succeeded", amount_received: int | None = 10000, **overrides: Any
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "pi_test_0001",
        "object": "payment_intent",
        "status": status,
        "amount_received": amount_received,
        "latest_charge": {
            "id": "ch_test_0001",
            "balance_transaction": {"id": "txn_test_0001", "fee": 280, "currency": "myr"},
        },
    }
    body.update(overrides)
    return body


def ok(body: Any) -> Handler:
    return lambda request: httpx2.Response(200, json=body)


# --- 金额与精度 ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("amount", "minor"),
    [("100.00", 10000), ("99.99", 9999), ("2", 200), ("999999.99", 99999999), ("0.01", 1)],
)
def test_amounts_and_minor_units_convert_exactly(amount: str, minor: int) -> None:
    assert amount_to_minor(Decimal(amount)) == minor
    assert minor_to_amount(minor) == Decimal(amount)
    # 存进 DECIMAL(20,8) 后读回来的写法也一样。
    assert amount_to_minor(Decimal(amount).quantize(Decimal("0.00000001"))) == minor


@pytest.mark.parametrize("bad", [Decimal("100.001"), Decimal("NaN"), Decimal("Infinity"), 100.0])
def test_amounts_that_are_not_whole_minor_units_are_refused(bad: Any) -> None:
    with pytest.raises(ValueError):
        amount_to_minor(bad)


def test_minor_amounts_must_be_integers() -> None:
    for bad in (True, 1.0, "100"):
        with pytest.raises(ValueError):
            minor_to_amount(bad)  # type: ignore[arg-type]


# --- 建单 ---------------------------------------------------------------------


def test_create_payment_posts_the_checkout_session_the_design_describes() -> None:
    gateway, recorder = gateway_with(ok(created_session()))

    created = gateway.create_payment(payment(), success_url=SUCCESS_URL, cancel_url=CANCEL_URL)

    assert created == CreatedPayment(
        gateway_payment_id=SESSION_ID,
        redirect_url=f"https://checkout.example.test/pay/{SESSION_ID}",
    )
    [request] = recorder.requests
    assert request.method == "POST"
    assert str(request.url) == f"{API_BASE_URL}/v1/checkout/sessions"
    assert request.headers["Idempotency-Key"] == f"topup-{PUBLIC_ID}"
    assert request.headers["Authorization"] == f"Bearer {SECRET_KEY}"
    assert request.headers["Content-Type"].startswith("application/x-www-form-urlencoded")
    form = {key: values for key, values in parse_qs(request.content.decode("utf-8")).items()}
    assert form == {
        "mode": ["payment"],
        "line_items[0][quantity]": ["1"],
        "line_items[0][price_data][currency]": ["myr"],
        "line_items[0][price_data][unit_amount]": ["10000"],
        "line_items[0][price_data][product_data][name]": [PRODUCT_NAME],
        "client_reference_id": [PUBLIC_ID],
        "metadata[payment_id]": [PUBLIC_ID],
        "expires_at": [str(calendar.timegm(EXPIRES_AT.utctimetuple()))],
        "success_url": [SUCCESS_URL],
        "cancel_url": [CANCEL_URL],
    }
    assert not any(key.startswith("payment_method_types") for key in form)


def test_create_payment_sends_whole_minor_units() -> None:
    gateway, recorder = gateway_with(ok(created_session()))

    gateway.create_payment(payment("99.99"), success_url=SUCCESS_URL, cancel_url=CANCEL_URL)

    form = parse_qs(recorder.requests[0].content.decode("utf-8"))
    assert form["line_items[0][price_data][unit_amount]"] == ["9999"]


@pytest.mark.parametrize("missing", ["id", "url", "status"])
def test_create_payment_needs_id_url_and_status(missing: str) -> None:
    body = created_session()
    del body[missing]
    gateway, _ = gateway_with(ok(body))

    with pytest.raises(GatewayUnavailable):
        gateway.create_payment(payment(), success_url=SUCCESS_URL, cancel_url=CANCEL_URL)


def test_create_payment_keeps_the_idempotency_key_in_use_code() -> None:
    gateway, _ = gateway_with(
        lambda request: httpx2.Response(
            409,
            json={"error": {"code": "idempotency_key_in_use", "type": "idempotency_error"}},
        )
    )

    with pytest.raises(GatewayRequestRejected) as raised:
        gateway.create_payment(payment(), success_url=SUCCESS_URL, cancel_url=CANCEL_URL)
    assert raised.value.code == "idempotency_key_in_use"
    assert raised.value.status_code == 409


# --- 查询与状态映射 -----------------------------------------------------------


def test_get_payment_status_expands_the_payment_intent_down_to_the_fee() -> None:
    gateway, recorder = gateway_with(ok(session()))

    status = gateway.get_payment_status(SESSION_ID)

    [request] = recorder.requests
    assert request.method == "GET"
    assert request.url.path == f"/v1/checkout/sessions/{SESSION_ID}"
    assert str(request.url).startswith(f"{API_BASE_URL}/v1/checkout/sessions/")
    assert request.url.params.get_list("expand[]") == [STATUS_EXPAND]
    assert request.headers["Authorization"] == f"Bearer {SECRET_KEY}"
    assert "Idempotency-Key" not in request.headers
    assert status.state is GatewayState.PAID
    assert status.session_id == SESSION_ID
    assert status.amount_minor == 10000
    assert status.currency == "MYR"
    assert status.client_reference_id == PUBLIC_ID
    assert status.payment_intent_id == "pi_test_0001"
    assert status.payment_intent_status == "succeeded"
    assert status.amount_received_minor == 10000
    assert status.fee_minor == 280
    assert status.fee_currency == "MYR"
    assert status.redirect_url is None


def test_an_open_session_reports_its_redirect_url_and_no_payment_intent() -> None:
    url = f"https://checkout.example.test/pay/{SESSION_ID}"
    gateway, _ = gateway_with(
        ok(session(status="open", payment_status="unpaid", intent=None, url=url))
    )

    status = gateway.get_payment_status(SESSION_ID)

    assert status.state is GatewayState.OPEN
    assert status.redirect_url == url
    assert status.payment_intent_id is None
    assert status.payment_intent_status is None
    assert status.amount_received_minor is None
    assert status.fee_minor is None
    assert status.fee_currency is None


def test_a_payment_intent_without_a_balance_transaction_has_no_fee() -> None:
    intent = payment_intent(latest_charge={"id": "ch_test_0001", "balance_transaction": None})
    gateway, _ = gateway_with(ok(session(intent=intent)))

    status = gateway.get_payment_status(SESSION_ID)

    assert status.state is GatewayState.PAID
    assert status.fee_minor is None
    assert status.fee_currency is None


def test_an_unexpanded_payment_intent_is_not_trusted() -> None:
    gateway, _ = gateway_with(ok(session(intent="pi_test_0001")))

    status = gateway.get_payment_status(SESSION_ID)

    assert status.state is GatewayState.INCONSISTENT
    assert status.payment_intent_id == "pi_test_0001"
    assert status.payment_intent_status is None


@pytest.mark.parametrize(
    ("session_status", "payment_status", "intent", "expected"),
    [
        ("complete", "paid", payment_intent(), GatewayState.PAID),
        ("complete", "paid", payment_intent(amount_received=9999), GatewayState.INCONSISTENT),
        ("complete", "paid", payment_intent(amount_received=None), GatewayState.INCONSISTENT),
        ("complete", "paid", payment_intent(status="processing"), GatewayState.INCONSISTENT),
        ("complete", "paid", None, GatewayState.INCONSISTENT),
        ("complete", "no_payment_required", payment_intent(), GatewayState.INCONSISTENT),
        ("complete", "no_payment_required", None, GatewayState.INCONSISTENT),
        (
            "complete",
            "unpaid",
            payment_intent(status="requires_payment_method", amount_received=0),
            GatewayState.FAILED,
        ),
        (
            "complete",
            "unpaid",
            payment_intent(status="canceled", amount_received=0),
            GatewayState.FAILED,
        ),
        (
            "complete",
            "unpaid",
            payment_intent(status="processing", amount_received=0),
            GatewayState.OPEN,
        ),
        ("complete", "unpaid", None, GatewayState.OPEN),
        ("complete", "something_new", payment_intent(), GatewayState.UNKNOWN),
        ("open", "unpaid", None, GatewayState.OPEN),
        ("open", "paid", payment_intent(), GatewayState.OPEN),
        ("expired", "unpaid", None, GatewayState.EXPIRED),
        ("expired", "paid", payment_intent(), GatewayState.EXPIRED),
        ("archived", "paid", payment_intent(), GatewayState.UNKNOWN),
    ],
)
def test_every_row_of_the_session_state_mapping(
    session_status: str, payment_status: str, intent: Any, expected: GatewayState
) -> None:
    gateway, _ = gateway_with(
        ok(session(status=session_status, payment_status=payment_status, intent=intent))
    )

    assert gateway.get_payment_status(SESSION_ID).state is expected


CHARGE_WITH_FEE = {
    "id": "ch_test_0001",
    "balance_transaction": {"id": "txn_test_0001", "fee": 280, "currency": "myr"},
}


@pytest.mark.parametrize(
    ("latest_charge", "refunded", "disputed"),
    [
        ({**CHARGE_WITH_FEE, "amount_refunded": 2500, "disputed": False}, 2500, False),
        ({**CHARGE_WITH_FEE, "amount_refunded": 0, "disputed": True}, 0, True),
        ({**CHARGE_WITH_FEE, "amount_refunded": 0, "disputed": False}, 0, False),
        ("missing", None, None),
    ],
    ids=["refunded", "disputed", "neither", "no-latest-charge"],
)
def test_the_charge_refund_and_dispute_fields_come_from_the_latest_charge(
    latest_charge: Any, refunded: int | None, disputed: bool | None
) -> None:
    """设计闸门 #585 v3 §2「对 A 的适配器的扩展」。

    取自已展开的 `latest_charge`，没有时为 None；不多发请求、不改请求参数；
    A 的状态映射结果不变。
    """
    intent = payment_intent()
    if latest_charge == "missing":
        del intent["latest_charge"]
    else:
        intent["latest_charge"] = latest_charge
    gateway, recorder = gateway_with(ok(session(intent=intent)))

    status = gateway.get_payment_status(SESSION_ID)

    assert status.charge_amount_refunded_minor == refunded
    assert status.charge_disputed is disputed
    # A 的映射不看这两个字段：已退款或有争议的会话照样是 PAID。
    assert status.state is GatewayState.PAID
    assert status.amount_received_minor == 10000
    assert status.payment_intent_id == "pi_test_0001"
    if latest_charge == "missing":
        assert (status.fee_minor, status.fee_currency) == (None, None)
    else:
        assert (status.fee_minor, status.fee_currency) == (280, "MYR")
    [request] = recorder.requests
    assert request.url.params.get_list("expand[]") == [STATUS_EXPAND]


@pytest.mark.parametrize(
    "latest_charge",
    [
        "ch_test_0001",
        {"id": "ch_test_0001", "amount_refunded": "0", "disputed": "false"},
        {"id": "ch_test_0001", "amount_refunded": True, "disputed": 0},
    ],
    ids=["unexpanded", "strings", "wrong-types"],
)
def test_unusable_charge_fields_are_none(latest_charge: Any) -> None:
    """取不到合法值时为 None（`True` 不算整数，`0` 不算布尔）。"""
    gateway, _ = gateway_with(ok(session(intent=payment_intent(latest_charge=latest_charge))))

    status = gateway.get_payment_status(SESSION_ID)

    assert (status.charge_amount_refunded_minor, status.charge_disputed) == (None, None)
    assert status.state is GatewayState.PAID


@pytest.mark.parametrize(
    ("latest_charge", "expected"),
    [
        ({**CHARGE_WITH_FEE, "payment_method_details": {"type": "card"}}, "card"),
        ({**CHARGE_WITH_FEE, "payment_method_details": {"type": "fpx"}}, "fpx"),
        ({**CHARGE_WITH_FEE, "payment_method_details": {"type": "grabpay_2"}}, "grabpay_2"),
        ({**CHARGE_WITH_FEE, "payment_method_details": {"type": "x" * 32}}, "x" * 32),
        (CHARGE_WITH_FEE, None),
        ({**CHARGE_WITH_FEE, "payment_method_details": {}}, None),
        ({**CHARGE_WITH_FEE, "payment_method_details": None}, None),
        ({**CHARGE_WITH_FEE, "payment_method_details": "card"}, None),
        ({**CHARGE_WITH_FEE, "payment_method_details": {"type": None}}, None),
        ({**CHARGE_WITH_FEE, "payment_method_details": {"type": 1}}, None),
        ({**CHARGE_WITH_FEE, "payment_method_details": {"type": True}}, None),
        ({**CHARGE_WITH_FEE, "payment_method_details": {"type": ["card"]}}, None),
        ({**CHARGE_WITH_FEE, "payment_method_details": {"type": "Card"}}, None),
        ({**CHARGE_WITH_FEE, "payment_method_details": {"type": "FPX"}}, None),
        ({**CHARGE_WITH_FEE, "payment_method_details": {"type": "x" * 33}}, None),
        ({**CHARGE_WITH_FEE, "payment_method_details": {"type": ""}}, None),
        ({**CHARGE_WITH_FEE, "payment_method_details": {"type": "card\n"}}, None),
        ({**CHARGE_WITH_FEE, "payment_method_details": {"type": "us-bank"}}, None),
        ("ch_test_0001", None),
        ("missing", None),
    ],
    ids=[
        "card",
        "fpx",
        "digits-and-underscore",
        "32-chars",
        "no-details",
        "details-without-type",
        "details-null",
        "details-not-an-object",
        "type-null",
        "type-integer",
        "type-boolean",
        "type-list",
        "uppercase-card",
        "uppercase-fpx",
        "33-chars",
        "empty",
        "trailing-newline",
        "hyphen",
        "unexpanded-charge",
        "no-latest-charge",
    ],
)
def test_the_payment_method_type_comes_from_the_latest_charge(
    latest_charge: Any, expected: str | None
) -> None:
    """设计闸门 #589 v2 §2「付款方式」、§7「付款方式字段」。

    `payment_method_details.type` 是字符串且匹配 `^[a-z0-9_]{1,32}$` 时原样返回，否则为 None；
    请求不变，A 的状态映射与 B 的两个字段不变。
    """
    intent = payment_intent()
    if latest_charge == "missing":
        del intent["latest_charge"]
    else:
        intent["latest_charge"] = latest_charge
    gateway, recorder = gateway_with(ok(session(intent=intent)))

    status = gateway.get_payment_status(SESSION_ID)

    assert status.payment_method_type == expected
    assert status.state is GatewayState.PAID
    assert status.amount_received_minor == 10000
    assert status.payment_intent_id == "pi_test_0001"
    assert (status.charge_amount_refunded_minor, status.charge_disputed) == (None, None)
    [request] = recorder.requests
    assert request.url.params.get_list("expand[]") == [STATUS_EXPAND]


def test_the_payment_method_type_does_not_change_the_refund_and_dispute_fields() -> None:
    charge = {
        **CHARGE_WITH_FEE,
        "amount_refunded": 2500,
        "disputed": True,
        "payment_method_details": {"type": "fpx", "fpx": {"bank": "maybank2u"}},
    }
    gateway, _ = gateway_with(ok(session(intent=payment_intent(latest_charge=charge))))

    status = gateway.get_payment_status(SESSION_ID)

    assert status.payment_method_type == "fpx"
    assert (status.charge_amount_refunded_minor, status.charge_disputed) == (2500, True)
    assert (status.fee_minor, status.fee_currency) == (280, "MYR")
    assert status.state is GatewayState.PAID


@pytest.mark.parametrize(
    ("session_status", "payment_status", "intent", "expected"),
    [
        ("open", "unpaid", None, GatewayState.OPEN),
        ("expired", "unpaid", None, GatewayState.EXPIRED),
        ("complete", "paid", "pi_test_0001", GatewayState.INCONSISTENT),
    ],
)
def test_a_session_without_an_expanded_charge_has_no_payment_method_type(
    session_status: str, payment_status: str, intent: Any, expected: GatewayState
) -> None:
    gateway, _ = gateway_with(
        ok(session(status=session_status, payment_status=payment_status, intent=intent))
    )

    status = gateway.get_payment_status(SESSION_ID)

    assert status.payment_method_type is None
    assert status.state is expected


def test_a_status_built_without_the_payment_method_type_defaults_to_none() -> None:
    """其他构造 `GatewayPaymentStatus` 的地方（例如测试里的假网关）不传它也照常工作。"""
    status = GatewayPaymentStatus(
        state=GatewayState.PAID,
        session_id=SESSION_ID,
        amount_minor=10000,
        currency="MYR",
        client_reference_id=PUBLIC_ID,
        payment_intent_id="pi_test_0001",
        payment_intent_status="succeeded",
        amount_received_minor=10000,
        fee_minor=None,
        fee_currency=None,
        redirect_url=None,
    )

    assert status.payment_method_type is None


@pytest.mark.parametrize("missing", ["id", "status", "payment_status"])
def test_get_payment_status_needs_id_status_and_payment_status(missing: str) -> None:
    body = session()
    del body[missing]
    gateway, _ = gateway_with(ok(body))

    with pytest.raises(GatewayUnavailable):
        gateway.get_payment_status(SESSION_ID)


def test_get_payment_status_does_not_need_a_url() -> None:
    body = session()
    del body["url"]
    gateway, _ = gateway_with(ok(body))

    assert gateway.get_payment_status(SESSION_ID).redirect_url is None


# --- 错误分类 -----------------------------------------------------------------


def raises(error: type[httpx2.RequestError]) -> Handler:
    def respond(request: httpx2.Request) -> httpx2.Response:
        raise error("failed", request=request)

    return respond


@pytest.mark.parametrize(
    "respond",
    [
        raises(httpx2.ReadTimeout),
        raises(httpx2.ConnectTimeout),
        raises(httpx2.ConnectError),
        lambda request: httpx2.Response(500, json={"error": {"code": "api_error"}}),
        lambda request: httpx2.Response(502),
        lambda request: httpx2.Response(503),
        lambda request: httpx2.Response(429, json={"error": {"code": "rate_limit"}}),
        lambda request: httpx2.Response(200, content=b"<html>not json</html>"),
        lambda request: httpx2.Response(200, json=["not", "an", "object"]),
        lambda request: httpx2.Response(200, content=b'{"id": NaN}'),
    ],
)
def test_retryable_failures_are_gateway_unavailable(respond: Handler) -> None:
    gateway, _ = gateway_with(respond)

    with pytest.raises(GatewayUnavailable):
        gateway.get_payment_status(SESSION_ID)
    with pytest.raises(GatewayUnavailable):
        gateway.create_payment(payment(), success_url=SUCCESS_URL, cancel_url=CANCEL_URL)


def test_timeouts_and_network_errors_are_told_apart() -> None:
    for respond, code in (
        (raises(httpx2.ReadTimeout), "TIMEOUT"),
        (raises(httpx2.ConnectError), "NETWORK"),
    ):
        gateway, _ = gateway_with(respond)
        with pytest.raises(GatewayUnavailable) as raised:
            gateway.get_payment_status(SESSION_ID)
        assert raised.value.code == code


@pytest.mark.parametrize(
    ("status_code", "body", "code"),
    [
        (
            404,
            {"error": {"code": "resource_missing", "type": "invalid_request_error"}},
            "resource_missing",
        ),
        (400, {"error": {"code": "parameter_invalid_integer"}}, "parameter_invalid_integer"),
        (401, {"error": {"type": "invalid_request_error"}}, "HTTP_401"),
        (403, None, "HTTP_403"),
        (400, {"error": {"code": "Not A Code!"}}, "HTTP_400"),
    ],
)
def test_other_client_errors_are_rejected_with_the_stripe_code(
    status_code: int, body: Any, code: str
) -> None:
    gateway, _ = gateway_with(
        lambda request: (
            httpx2.Response(status_code, json=body)
            if body is not None
            else httpx2.Response(status_code, content=b"denied")
        )
    )

    with pytest.raises(GatewayRequestRejected) as raised:
        gateway.get_payment_status(SESSION_ID)
    assert raised.value.code == code
    assert raised.value.status_code == status_code


@pytest.mark.parametrize("status_code", [301, 302, 307, 308])
def test_redirects_are_rejected_and_not_followed(status_code: int) -> None:
    gateway, recorder = gateway_with(
        lambda request: httpx2.Response(
            status_code, headers={"Location": f"{API_BASE_URL}/elsewhere"}
        )
    )

    with pytest.raises(GatewayRequestRejected) as raised:
        gateway.get_payment_status(SESSION_ID)
    assert raised.value.code == "UNEXPECTED_REDIRECT"
    assert raised.value.status_code == status_code
    assert len(recorder.requests) == 1


def test_errors_carry_no_secret_and_no_request_body() -> None:
    sensitive = (SECRET_KEY, WEBHOOK_SECRET, PUBLIC_ID, SUCCESS_URL, "payer@example.test")
    for respond in (
        raises(httpx2.ReadTimeout),
        raises(httpx2.ConnectError),
        lambda request: httpx2.Response(500, json={"error": {"message": SECRET_KEY}}),
        lambda request: httpx2.Response(
            400, json={"error": {"code": "parameter_invalid", "message": PUBLIC_ID}}
        ),
        lambda request: httpx2.Response(302, headers={"Location": SUCCESS_URL}),
        lambda request: httpx2.Response(200, json={"email": "payer@example.test"}),
    ):
        gateway, _ = gateway_with(respond)
        with pytest.raises((GatewayUnavailable, GatewayRequestRejected)) as raised:
            gateway.create_payment(payment(), success_url=SUCCESS_URL, cancel_url=CANCEL_URL)
        error = raised.value
        shown = f"{error!s} {error!r} {error.args!r}"
        for value in sensitive:
            assert value not in shown
        # 不链着带 Authorization 头的传输层异常。
        assert error.__cause__ is None
        assert error.__suppress_context__ is True or error.__context__ is None


def test_the_gateway_needs_an_https_api_base_url() -> None:
    for bad in ("", "  ", "http://stripe-api.example.test"):
        with pytest.raises(ValueError):
            StripeGateway(
                SECRET_KEY,
                WEBHOOK_SECRET,
                timeout_seconds=10,
                webhook_tolerance_seconds=300,
                api_base_url=bad,
            )


def test_the_gateway_repr_hides_the_keys() -> None:
    gateway, _ = gateway_with(ok({}))

    assert SECRET_KEY not in repr(gateway)
    assert WEBHOOK_SECRET not in repr(gateway)
    assert gateway.livemode is False


# --- 回调验签 -----------------------------------------------------------------

BODY = json.dumps(
    {
        "id": "evt_test_0001",
        "type": "checkout.session.completed",
        "livemode": False,
        "created": 1_762_680_000,
        "data": {"object": session()},
    }
).encode("utf-8")


def unix(moment: dt.datetime) -> int:
    return calendar.timegm(moment.utctimetuple())


def sign(body: bytes, timestamp: int, secret: str = WEBHOOK_SECRET) -> str:
    payload = f"{timestamp}.".encode("ascii") + body
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def header(body: bytes = BODY, *, at: dt.datetime = NOW) -> dict[str, str]:
    timestamp = unix(at)
    return {"Stripe-Signature": f"t={timestamp},v1={sign(body, timestamp)}"}


def verifier() -> StripeGateway:
    gateway, _ = gateway_with(ok({}))
    return gateway


def test_a_correct_signature_passes() -> None:
    assert verifier().verify_webhook(BODY, header(), now=NOW) is None


def test_the_header_name_is_case_insensitive() -> None:
    [(name, value)] = header().items()
    verifier().verify_webhook(BODY, {name.lower(): value}, now=NOW)


def test_changing_one_byte_of_the_body_fails() -> None:
    tampered = bytearray(BODY)
    tampered[10] ^= 0x01

    with pytest.raises(WebhookSignatureError):
        verifier().verify_webhook(bytes(tampered), header(), now=NOW)


def test_a_signature_made_with_another_secret_fails() -> None:
    timestamp = unix(NOW)
    forged = {"Stripe-Signature": f"t={timestamp},v1={sign(BODY, timestamp, 'whsec_other')}"}

    with pytest.raises(WebhookSignatureError):
        verifier().verify_webhook(BODY, forged, now=NOW)


def test_one_matching_v1_among_several_passes() -> None:
    timestamp = unix(NOW)
    good = sign(BODY, timestamp)
    bad = sign(BODY, timestamp, "whsec_rotated_out")
    value = f"t={timestamp},v1={bad},v0={'0' * 64},v1={good}"

    verifier().verify_webhook(BODY, {"Stripe-Signature": value}, now=NOW)

    with pytest.raises(WebhookSignatureError):
        verifier().verify_webhook(
            BODY, {"Stripe-Signature": f"t={timestamp},v1={bad},v0={good}"}, now=NOW
        )


@pytest.mark.parametrize("offset", [300, -300])
def test_exactly_300_seconds_apart_passes(offset: int) -> None:
    signed_at = NOW + dt.timedelta(seconds=offset)

    verifier().verify_webhook(BODY, header(at=signed_at), now=NOW)


@pytest.mark.parametrize("offset", [301, -301])
def test_301_seconds_apart_fails(offset: int) -> None:
    signed_at = NOW + dt.timedelta(seconds=offset)

    with pytest.raises(WebhookSignatureError):
        verifier().verify_webhook(BODY, header(at=signed_at), now=NOW)


def test_the_window_follows_the_configured_tolerance() -> None:
    gateway = StripeGateway(
        SECRET_KEY,
        WEBHOOK_SECRET,
        timeout_seconds=10,
        webhook_tolerance_seconds=60,
        transport=httpx2.MockTransport(ok({})),
        api_base_url=API_BASE_URL,
    )
    gateway.verify_webhook(BODY, header(at=NOW - dt.timedelta(seconds=60)), now=NOW)
    with pytest.raises(WebhookSignatureError):
        gateway.verify_webhook(BODY, header(at=NOW - dt.timedelta(seconds=61)), now=NOW)


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Content-Type": "application/json"},
        {"Stripe-Signature": ""},
        {"Stripe-Signature": "garbage"},
        {"Stripe-Signature": f"v1={sign(BODY, unix(NOW))}"},
        {"Stripe-Signature": f"t={unix(NOW)}"},
        {"Stripe-Signature": f"t={unix(NOW)},v0={sign(BODY, unix(NOW))}"},
        {"Stripe-Signature": f"t=abc,v1={sign(BODY, unix(NOW))}"},
        {"Stripe-Signature": f"t=-{unix(NOW)},v1={sign(BODY, unix(NOW))}"},
        {"Stripe-Signature": f"t={unix(NOW)},t={unix(NOW)},v1={sign(BODY, unix(NOW))}"},
        {"Stripe-Signature": f"t={unix(NOW)},v1"},
        {"Stripe-Signature": f"t={unix(NOW)},v1=é"},
    ],
)
def test_missing_or_malformed_headers_fail(headers: dict[str, str]) -> None:
    with pytest.raises(WebhookSignatureError):
        verifier().verify_webhook(BODY, headers, now=NOW)


def test_signature_errors_do_not_echo_the_header_or_the_secret() -> None:
    with pytest.raises(WebhookSignatureError) as raised:
        verifier().verify_webhook(BODY + b" ", header(), now=NOW)

    shown = f"{raised.value!s} {raised.value!r}"
    assert WEBHOOK_SECRET not in shown
    assert header()["Stripe-Signature"] not in shown


# --- 回调解析 -----------------------------------------------------------------


def test_parse_webhook_keeps_only_the_whitelisted_fields() -> None:
    raw = {
        "id": "evt_test_0001",
        "type": "checkout.session.completed",
        "livemode": False,
        "created": 1_762_680_000,
        "data": {
            "object": session(
                metadata={"payment_id": PUBLIC_ID, "note": "private"},
                customer_email="payer@example.test",
            )
        },
    }

    event = verifier().parse_webhook(json.dumps(raw).encode("utf-8"))

    assert event.gateway_event_id == "evt_test_0001"
    assert event.event_type == "checkout.session.completed"
    assert event.livemode is False
    assert dict(event.fields) == {
        "type": "checkout.session.completed",
        "livemode": False,
        "created": 1_762_680_000,
        "object_id": SESSION_ID,
        "client_reference_id": PUBLIC_ID,
        "payment_status": "paid",
        "status": "complete",
        "amount_total": 10000,
        "currency": "myr",
        "payment_intent": "pi_test_0001",
    }
    assert "payer@example.test" not in json.dumps(dict(event.fields))
    assert "private" not in json.dumps(dict(event.fields))


def test_parse_webhook_tolerates_an_event_without_an_object() -> None:
    raw = {"id": "evt_test_0002", "type": "checkout.session.expired", "livemode": True}

    event = verifier().parse_webhook(json.dumps(raw).encode("utf-8"))

    assert event.livemode is True
    assert event.fields["object_id"] is None
    assert event.fields["client_reference_id"] is None
    assert event.fields["payment_intent"] is None


# --- 争议与退款的解析（设计闸门 #585 v3 §2「争议与退款」，AIH-TASK-181） ----------------

PAYER_DATA = ("payer@example.test", "Fictional Payer", "1 Fictional Street")


def refunded_charge(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "ch_test_0001",
        "object": "charge",
        "amount": 10000,
        "amount_refunded": 10000,
        "currency": "myr",
        "payment_intent": "pi_test_0001",
        "status": "succeeded",
        "refunded": True,
        "customer": "cus_test_0001",
        "billing_details": {
            "email": PAYER_DATA[0],
            "name": PAYER_DATA[1],
            "address": {"line1": PAYER_DATA[2]},
        },
        "receipt_email": PAYER_DATA[0],
        "metadata": {"payment_id": PUBLIC_ID, "note": "private"},
    }
    body.update(overrides)
    return body


def created_dispute(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "dp_test_0001",
        "object": "dispute",
        "amount": 10000,
        "currency": "myr",
        "status": "needs_response",
        "reason": "fraudulent",
        "charge": "ch_test_0001",
        "payment_intent": "pi_test_0001",
        "evidence": {"customer_email_address": PAYER_DATA[0], "customer_name": PAYER_DATA[1]},
        "metadata": {"payment_id": PUBLIC_ID, "note": "private"},
    }
    body.update(overrides)
    return body


def charge_event(event_type: str, obj: Any, **extra: Any) -> bytes:
    raw: dict[str, Any] = {
        "id": "evt_test_0003",
        "type": event_type,
        "livemode": False,
        "created": 1_762_680_000,
        "data": {"object": obj},
    }
    raw.update(extra)
    return json.dumps(raw).encode("utf-8")


def assert_no_personal_data(fields: dict[str, Any]) -> None:
    stored = json.dumps(fields)
    for value in (*PAYER_DATA, "private", "cus_test_0001"):
        assert value not in stored
    for key in ("billing_details", "receipt_email", "evidence", "metadata", "customer", "reason"):
        assert key not in fields


def test_a_refund_keeps_the_common_fields_and_the_charge_whitelist() -> None:
    event = verifier().parse_webhook(charge_event("charge.refunded", refunded_charge()))

    assert (event.gateway_event_id, event.event_type, event.livemode) == (
        "evt_test_0003",
        "charge.refunded",
        False,
    )
    assert dict(event.fields) == {
        "type": "charge.refunded",
        "livemode": False,
        "created": 1_762_680_000,
        "object_id": "ch_test_0001",
        "payment_intent": "pi_test_0001",
        "amount": 10000,
        "amount_refunded": 10000,
        "currency": "myr",
    }
    assert_no_personal_data(dict(event.fields))


def test_a_dispute_keeps_the_common_fields_and_the_dispute_whitelist() -> None:
    event = verifier().parse_webhook(charge_event("charge.dispute.created", created_dispute()))

    assert event.event_type == "charge.dispute.created"
    assert dict(event.fields) == {
        "type": "charge.dispute.created",
        "livemode": False,
        "created": 1_762_680_000,
        "object_id": "dp_test_0001",
        "payment_intent": "pi_test_0001",
        "amount": 10000,
        "currency": "myr",
        "status": "needs_response",
        "charge": "ch_test_0001",
    }
    assert_no_personal_data(dict(event.fields))


def test_expanded_references_are_kept_as_their_ids() -> None:
    dispute = created_dispute(
        payment_intent={"id": "pi_test_0002", "object": "payment_intent"},
        charge={"id": "ch_test_0002", "billing_details": {"email": PAYER_DATA[0]}},
    )

    event = verifier().parse_webhook(charge_event("charge.dispute.created", dispute))

    assert event.fields["payment_intent"] == "pi_test_0002"
    assert event.fields["charge"] == "ch_test_0002"
    assert_no_personal_data(dict(event.fields))


@pytest.mark.parametrize(
    ("event_type", "obj"),
    [
        ("charge.refunded", {"id": "ch_test_0001", "object": "charge"}),
        ("charge.dispute.created", {"id": "dp_test_0001", "object": "dispute"}),
        ("charge.refunded", {"id": "ch_test_0001", "payment_intent": None, "amount": "100"}),
        ("charge.dispute.created", {"id": "dp_test_0001", "charge": None, "status": 3}),
    ],
    ids=["charge-missing", "dispute-missing", "charge-null-and-wrong-type", "dispute-null"],
)
def test_keys_the_object_lacks_are_not_written(event_type: str, obj: dict[str, Any]) -> None:
    event = verifier().parse_webhook(charge_event(event_type, obj))

    expected_id = obj["id"]
    assert dict(event.fields) == {
        "type": event_type,
        "livemode": False,
        "created": 1_762_680_000,
        "object_id": expected_id,
    }


@pytest.mark.parametrize("event_type", ["charge.refunded", "charge.dispute.created"])
def test_a_charge_event_without_an_object_keeps_only_the_common_fields(event_type: str) -> None:
    raw = {"id": "evt_test_0004", "type": event_type, "livemode": True}

    event = verifier().parse_webhook(json.dumps(raw).encode("utf-8"))

    assert dict(event.fields) == {"type": event_type, "livemode": True, "created": None}


def test_a_refund_does_not_take_the_dispute_only_fields_and_vice_versa() -> None:
    """Charge 不取 `status` 与 `charge`；Dispute 不取 `amount_refunded`。"""
    refund = verifier().parse_webhook(
        charge_event("charge.refunded", refunded_charge(charge="ch_other"))
    )
    dispute = verifier().parse_webhook(
        charge_event("charge.dispute.created", created_dispute(amount_refunded=500))
    )

    assert "status" not in refund.fields and "charge" not in refund.fields
    assert "amount_refunded" not in dispute.fields


def test_the_checkout_whitelist_is_unchanged_by_the_charge_types() -> None:
    """A 的四种事件的解析不变：仍是固定的十个键（缺的值为 None）。"""
    raw = {"id": "evt_test_0005", "type": "checkout.session.completed", "livemode": False}

    event = verifier().parse_webhook(json.dumps(raw).encode("utf-8"))

    assert set(event.fields) == {
        "type",
        "livemode",
        "created",
        "object_id",
        "client_reference_id",
        "payment_status",
        "status",
        "amount_total",
        "currency",
        "payment_intent",
    }


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"\xff\xfe",
        b"[]",
        b'"evt"',
        json.dumps({"type": "checkout.session.completed", "livemode": False}).encode(),
        json.dumps({"id": "evt_1", "livemode": False}).encode(),
        json.dumps({"id": "", "type": "checkout.session.completed", "livemode": False}).encode(),
        json.dumps({"id": 1, "type": "checkout.session.completed", "livemode": False}).encode(),
        json.dumps({"id": "evt_1", "type": None, "livemode": False}).encode(),
        json.dumps({"id": "evt_1", "type": "checkout.session.completed"}).encode(),
        json.dumps(
            {"id": "evt_1", "type": "checkout.session.completed", "livemode": "false"}
        ).encode(),
        json.dumps({"id": "evt_1", "type": "checkout.session.completed", "livemode": 0}).encode(),
        json.dumps(
            {"id": "evt_1", "type": "checkout.session.completed", "livemode": None}
        ).encode(),
        json.dumps(
            {"id": "e" * 256, "type": "checkout.session.completed", "livemode": False}
        ).encode(),
        json.dumps({"id": "evt_1", "type": "t" * 65, "livemode": False}).encode(),
    ],
)
def test_unusable_payloads_are_payload_errors(raw: bytes) -> None:
    with pytest.raises(WebhookPayloadError):
        verifier().parse_webhook(raw)
