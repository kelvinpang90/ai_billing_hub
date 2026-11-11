"""Admin payments end to end (design gate #585 v3 §2「管理端接口」, AIH-TASK-183).

设计 §7 标 183 的各行：

- **管理端鉴权**（匿名 401、客户 403）：由 test_admin_customers_api.py 的路由枚举用例覆盖
  （三个接口已补进 `EXPECTED_ADMIN_ROUTES`）；这里另验日期在鉴权**之后**解析；
- **列表与筛选**：字段与顺序、状态、客户（不存在时空页）、未知参数忽略、排序（`created_at`
  降序、`id` 降序）与分页、日期区间（马来西亚时间日界前后各一秒）、格式不合法与年份越界与
  起晚于止的 422；
- **详情**：字段与顺序、事件（同一时刻两条按 id、最多 100 条、不含 `metadata_json`）、
  人工入账后的 `credited_amount`、404；
- **结案接口**：两种动作（200 返回与 GET 相同的详情）、多余字段与原因为空或超长与动作不合法
  的 422、开关关闭时 CREDIT 503 而 CLOSE 200（读接口照常）、凭据读不到时 CREDIT 503、
  `resolve_review` 的错误码经接口原样返回、原因不进日志。

支付、租户与假 Stripe 用共用的 payment_world.py；本文件的夹具只在这里用，定义在这里。
Stripe 一律是注入了假传输的 `StripeGateway`（monkeypatch `admin_payments.get_payment_gateway`），
不联网；结案时刻经 `admin_payments.utc_now` 注入，不 sleep。

MySQL 那部分需要 `BILLING_TEST_DATABASE_URL`，没设时 skip ——
**不要把 skipped 读成 passed**，CI 设了它并把 skipped 判成失败。
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import Callable, Iterator
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from payment_world import (
    CREATED,
    EXPIRES,
    PaymentRef,
    World,
    _populate,
    build_mysql_world,
)
from sqlalchemy import create_engine, select, update
from sqlalchemy.pool import StaticPool

from app.api import admin_payments
from app.core.config import Settings
from app.core.database import create_session_factory
from app.core.tokens import issue_access_token
from app.main import create_app
from app.models.auth import User
from app.models.base import Base
from app.models.payments import (
    Payment,
    PaymentEventProcessingStatus,
    PaymentEventVerificationStatus,
    PaymentGateway,
    PaymentGatewayEvent,
    PaymentStatus,
)
from app.payments.base import GatewayState
from app.services.auth import utc_now
from app.services.payments import AMOUNT_MISMATCH

PAYMENTS = "/api/v1/admin/payments"

# 固定值而不是 utc_now()：断言「存进去的就是调用方给的那个时刻」要能逐字比对。
RESOLVED_AT = dt.datetime(2026, 11, 9, 10, 0, 0)
# 足够独特，日志与响应里出现任何一段都认得出来。
REASON = "Fictional review note: payer Aminah confirmed by phone, ref Z4PQ8"
# 事件 `metadata_json` 里的标记：响应里不许出现。
METADATA_MARKER = "fictional-metadata-marker-7c1d"

# 设计 §2 的字段与顺序：断言「恰好等于」，按顺序比。
SUMMARY_KEYS = [
    "id",
    "customer_id",
    "customer_name",
    "amount",
    "currency",
    "gateway",
    "gateway_reference",
    "gateway_fee",
    "status",
    "status_reason",
    "resolution",
    "created_at",
    "paid_at",
    "review_required_at",
]
DETAIL_KEYS = [
    *SUMMARY_KEYS,
    "confirmed_amount",
    "confirmed_currency",
    "credited_amount",
    "gateway_payment_intent_id",
    "expires_at",
    "last_reconciled_at",
    "resolved_at",
    "resolved_by",
    "resolution_reason",
    "events",
]
EVENT_KEYS = [
    "gateway_event_id",
    "event_type",
    "processing_status",
    "last_error_code",
    "received_at",
    "processed_at",
    "fingerprint_conflict_at",
]
PAGE_KEYS = {"items", "page", "page_size", "total"}
ENVELOPE_KEYS = {"success", "data", "error", "request_id"}


# --- 夹具 ---------------------------------------------------------------


def _sqlite_world() -> Iterator[World]:
    # StaticPool：TestClient 在线程池里跑同步端点，内存库必须所有连接共用一个
    # （与 test_admin_customers_api.py 的 app 夹具同理）。
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    try:
        yield _populate(create_session_factory(engine))
    finally:
        engine.dispose()


@pytest.fixture(params=["sqlite", "mysql"])
def world(request, monkeypatch) -> Iterator[World]:
    if request.param == "mysql":
        yield from build_mysql_world(monkeypatch)
        return
    yield from _sqlite_world()


@pytest.fixture
def make_client(tmp_path, world, monkeypatch) -> Callable[..., TestClient]:
    """A client on an app with the switch on or off; Stripe and the clock are injected."""
    monkeypatch.setattr(admin_payments, "utc_now", lambda: RESOLVED_AT)
    monkeypatch.setattr(
        admin_payments, "get_payment_gateway", lambda settings: world.stripe.gateway()
    )
    key = tmp_path / "jwt.key"
    key.write_text("a-test-signing-key-that-is-long-enough", encoding="utf-8")

    def build(*, payments_enabled: bool = True) -> TestClient:
        application = create_app(
            Settings(jwt_secret_file=str(key), database_url="", payments_enabled=payments_enabled)
        )
        application.state.session_factory = world.factory
        # 500 要以统一错误体返回，而不是在测试里抛出来。
        return TestClient(
            application,
            raise_server_exceptions=False,
            headers={**admin_headers(application, world), "User-Agent": "admin-payments-test"},
        )

    return build


@pytest.fixture
def client(make_client) -> TestClient:
    return make_client()


# --- 构件 ---------------------------------------------------------------


def admin_headers(application: FastAPI, world: World) -> dict[str, str]:
    """A signed ADMIN access token (ADMIN gets one only after 2FA; that path is elsewhere)."""
    token = issue_access_token(
        application.state.settings,
        user_id=world.admin_id,
        role="ADMIN",
        session_id=uuid.uuid4().hex,
        now=utc_now(),
    )
    return {"Authorization": f"Bearer {token}"}


def admin_email(world: World) -> str:
    with world.factory() as session:
        user = session.get(User, world.admin_id)
        assert user is not None
        return user.email


def set_columns(world: World, payment: PaymentRef, **values: Any) -> None:
    with world.factory() as session:
        session.execute(
            update(Payment).where(Payment.public_id == payment.public_id).values(**values)
        )
        session.commit()


def created_at(world: World, tenant, moment: dt.datetime, **options: Any) -> PaymentRef:
    payment = world.payment(tenant, **options)
    set_columns(world, payment, created_at=moment)
    return payment


def in_review(world: World, tenant=None) -> PaymentRef:
    """A `REVIEW_REQUIRED` payment (应收 100.00, Stripe reported 99.99) as A would leave it."""
    payment = world.payment(
        tenant or world.customer(), status=PaymentStatus.REVIEW_REQUIRED, reason=AMOUNT_MISMATCH
    )
    set_columns(world, payment, confirmed_amount=Decimal("99.99"), confirmed_currency="MYR")
    return payment


def serve_paid(
    world: World, payment: PaymentRef, *, refunded: int = 0, disputed: bool = False, **overrides
) -> dict[str, Any]:
    """What Stripe answers, with the latest charge's refund and dispute fields."""
    body = world.serve(payment, GatewayState.PAID, **overrides)
    latest = body["payment_intent"]["latest_charge"]
    latest["amount_refunded"] = refunded
    latest["disputed"] = disputed
    return body


def add_event(
    world: World,
    payment: PaymentRef,
    *,
    received: dt.datetime,
    event_type: str = "checkout.session.completed",
    status: PaymentEventProcessingStatus = PaymentEventProcessingStatus.PROCESSED,
    last_error_code: str | None = None,
    conflict_at: dt.datetime | None = None,
) -> str:
    """Insert one event linked to `payment`; return its `gateway_event_id`."""
    gateway_event_id = f"evt_test_{uuid.uuid4().hex}"
    with world.factory() as session:
        payment_row_id = session.execute(
            select(Payment.id).where(Payment.public_id == payment.public_id)
        ).scalar_one()
        session.add(
            PaymentGatewayEvent(
                gateway=PaymentGateway.STRIPE,
                gateway_event_id=gateway_event_id,
                event_type=event_type,
                payment_id=payment_row_id,
                payload_fingerprint="0" * 64,
                verification_status=PaymentEventVerificationStatus.VERIFIED,
                processing_status=status,
                attempts=1,
                next_attempt_at=received,
                lease_until=None,
                last_error_code=last_error_code,
                fingerprint_conflict_at=conflict_at,
                received_at=received,
                processed_at=received + dt.timedelta(seconds=5)
                if status is PaymentEventProcessingStatus.PROCESSED
                else None,
                metadata_json={"type": event_type, "marker": METADATA_MARKER},
            )
        )
        session.commit()
    return gateway_event_id


def data_of(response) -> Any:
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == ENVELOPE_KEYS
    assert body["success"] is True
    return body["data"]


def error_of(response) -> tuple[int, str]:
    body = response.json()
    assert set(body) == ENVELOPE_KEYS
    assert body["success"] is False
    assert body["data"] is None
    return response.status_code, body["error"]["code"]


def listed(client: TestClient, **params: Any) -> list[str]:
    data = data_of(client.get(PAYMENTS, params=params))
    assert set(data) == PAGE_KEYS
    return [item["id"] for item in data["items"]]


def resolve(client: TestClient, payment: PaymentRef | str, body: Any):
    public_id = payment if isinstance(payment, str) else payment.public_id
    return client.post(f"{PAYMENTS}/{public_id}/resolve", json=body)


def unchanged(world: World, payment: PaymentRef) -> dict[str, object]:
    """Everything the resolution may write, as committed."""
    return world.snapshot(payment)


# --- 列表 ---------------------------------------------------------------


def test_list_items_have_exactly_the_design_fields_in_order(world, client) -> None:
    tenant = world.customer()
    payment = world.payment(tenant, status=PaymentStatus.PAID)
    set_columns(world, payment, gateway_fee=Decimal("2.90"))

    data = data_of(client.get(PAYMENTS))

    assert (data["page"], data["page_size"], data["total"]) == (1, 20, 1)
    [item] = data["items"]
    assert list(item) == SUMMARY_KEYS
    assert item == {
        "id": payment.public_id,
        "customer_id": tenant.public_id,
        "customer_name": "Payment Confirm Test Sdn Bhd",
        # 金额与手续费：恰好 8 位小数的定点字符串。
        "amount": "100.00000000",
        "currency": "MYR",
        "gateway": "STRIPE",
        "gateway_reference": payment.session_id,
        "gateway_fee": "2.90000000",
        "status": "PAID",
        "status_reason": None,
        "resolution": None,
        "created_at": CREATED.isoformat(),
        "paid_at": CREATED.isoformat(),
        "review_required_at": None,
    }


def test_list_filters_by_status_and_customer(world, client) -> None:
    first, second = world.customer(), world.customer()
    review_first = world.payment(first, status=PaymentStatus.REVIEW_REQUIRED, reason="X")
    pending_first = world.payment(first)
    review_second = world.payment(second, status=PaymentStatus.REVIEW_REQUIRED, reason="X")
    paid_second = world.payment(second, status=PaymentStatus.PAID)

    everything = listed(client)
    by_status = listed(client, status="REVIEW_REQUIRED")
    by_customer = listed(client, customer_id=first.public_id)
    both = listed(client, status="REVIEW_REQUIRED", customer_id=second.public_id)
    unknown_customer = data_of(client.get(PAYMENTS, params={"customer_id": str(uuid.uuid4())}))
    unknown_param = listed(client, tenant_id="1", status="PAID", sort="asc")

    assert set(everything) == {
        review_first.public_id,
        pending_first.public_id,
        review_second.public_id,
        paid_second.public_id,
    }
    assert set(by_status) == {review_first.public_id, review_second.public_id}
    assert set(by_customer) == {review_first.public_id, pending_first.public_id}
    assert both == [review_second.public_id]
    # 没有这个客户：空页，不是 404。
    assert (unknown_customer["items"], unknown_customer["total"]) == ([], 0)
    # 未知查询参数忽略。
    assert unknown_param == [paid_second.public_id]


def test_status_must_be_one_of_the_six(world, client) -> None:
    for value in ("paid", "RESOLVED", "CREDITED"):
        assert error_of(client.get(PAYMENTS, params={"status": value})) == (
            422,
            "VALIDATION_ERROR",
        )
    for value in PaymentStatus:
        assert listed(client, status=value.value) == []


def test_list_is_newest_first_then_by_descending_id_and_paged(world, client) -> None:
    tenant = world.customer()
    older = [created_at(world, tenant, CREATED) for _ in range(3)]
    newest = created_at(world, tenant, CREATED + dt.timedelta(seconds=1))
    oldest = created_at(world, tenant, CREATED - dt.timedelta(days=1))
    # 同一 `created_at` 的三笔按插入顺序（id 递增）建，列表里倒过来。
    expected = [newest.public_id, *(p.public_id for p in reversed(older)), oldest.public_id]

    assert listed(client) == expected
    first = data_of(client.get(PAYMENTS, params={"page": 1, "page_size": 2}))
    second = data_of(client.get(PAYMENTS, params={"page": 2, "page_size": 2}))
    third = data_of(client.get(PAYMENTS, params={"page": 3, "page_size": 2}))
    beyond = data_of(client.get(PAYMENTS, params={"page": 4, "page_size": 2}))

    assert [item["id"] for item in first["items"]] == expected[:2]
    assert [item["id"] for item in second["items"]] == expected[2:4]
    assert [item["id"] for item in third["items"]] == expected[4:]
    assert (beyond["items"], beyond["total"]) == ([], 5)
    assert {first["total"], second["total"], third["total"]} == {5}
    assert (second["page"], second["page_size"]) == (2, 2)


@pytest.mark.parametrize(
    "params",
    [{"page": 0}, {"page": 10_001}, {"page_size": 0}, {"page_size": 101}, {"page": "x"}],
)
def test_page_bounds_match_the_other_admin_lists(world, client, params) -> None:
    assert error_of(client.get(PAYMENTS, params=params)) == (422, "VALIDATION_ERROR")


def test_created_dates_are_inclusive_malaysian_calendar_days(world, client) -> None:
    """2026-11-08 MYT = [2026-11-07 16:00:00, 2026-11-08 16:00:00) UTC；日界前后各一秒。"""
    tenant = world.customer()
    start = dt.datetime(2026, 11, 7, 16, 0, 0)
    end = dt.datetime(2026, 11, 8, 16, 0, 0)
    second = dt.timedelta(seconds=1)
    before_start = created_at(world, tenant, start - second)
    at_start = created_at(world, tenant, start)
    after_start = created_at(world, tenant, start + second)
    before_end = created_at(world, tenant, end - second)
    at_end = created_at(world, tenant, end)
    after_end = created_at(world, tenant, end + second)

    one_day = listed(client, created_from="2026-11-08", created_to="2026-11-08")
    from_only = listed(client, created_from="2026-11-08")
    to_only = listed(client, created_to="2026-11-08")
    two_days = listed(client, created_from="2026-11-08", created_to="2026-11-09")

    assert one_day == [before_end.public_id, after_start.public_id, at_start.public_id]
    assert from_only == [
        after_end.public_id,
        at_end.public_id,
        before_end.public_id,
        after_start.public_id,
        at_start.public_id,
    ]
    assert to_only == [
        before_end.public_id,
        after_start.public_id,
        at_start.public_id,
        before_start.public_id,
    ]
    assert two_days == [
        after_end.public_id,
        at_end.public_id,
        before_end.public_id,
        after_start.public_id,
        at_start.public_id,
    ]


def test_the_extreme_years_are_accepted(world, client) -> None:
    world.payment(world.customer())

    assert len(listed(client, created_from="2000-01-01", created_to="2099-12-31")) == 1


@pytest.mark.parametrize("field", ["created_from", "created_to"])
@pytest.mark.parametrize(
    "value",
    [
        "2026-1-08",
        "2026-11-8",
        "20261108",
        "2026/11/08",
        "2026-11-08T00:00:00",
        " 2026-11-08",
        "",
        # 全角数字：`[0-9]` 只认 ASCII。
        "２０２６-１１-０８",
        "2026-02-30",
        "2026-13-01",
        "2026-00-10",
        "1999-12-31",
        "2100-01-01",
        "0000-01-01",
        "9999-12-31",
    ],
)
def test_malformed_or_out_of_range_dates_are_422(world, client, field, value) -> None:
    response = client.get(PAYMENTS, params={field: value})

    assert error_of(response) == (422, "VALIDATION_ERROR")
    # 只列字段名，不回显值。
    assert f"query.{field}" in response.json()["error"]["message"]


def test_created_from_after_created_to_is_422(world, client) -> None:
    response = client.get(
        PAYMENTS, params={"created_from": "2026-11-09", "created_to": "2026-11-08"}
    )

    assert error_of(response) == (422, "VALIDATION_ERROR")
    message = response.json()["error"]["message"]
    assert "query.created_from" in message and "query.created_to" in message


def test_dates_are_judged_after_authentication(world, client) -> None:
    anonymous = TestClient(client.app, raise_server_exceptions=False)

    response = anonymous.get(PAYMENTS, params={"created_from": "nonsense"})

    assert error_of(response) == (401, "TOKEN_INVALID")


# --- 详情 ---------------------------------------------------------------


def test_detail_has_exactly_the_design_fields_and_ordered_events(world, client) -> None:
    tenant = world.customer()
    payment = world.payment(tenant, status=PaymentStatus.REVIEW_REQUIRED, reason="X")
    other = world.payment(tenant)
    early = dt.datetime(2026, 11, 8, 8, 5, 0)
    late = dt.datetime(2026, 11, 8, 8, 6, 0)
    # 插入顺序：同一时刻的两条中先插入的 id 小。
    late_first = add_event(world, payment, received=late)
    early_one = add_event(
        world,
        payment,
        received=early,
        event_type="checkout.session.async_payment_succeeded",
        status=PaymentEventProcessingStatus.RETRY,
        last_error_code="HTTP_503",
        conflict_at=late,
    )
    late_second = add_event(world, payment, received=late, event_type="charge.refunded")
    add_event(world, other, received=early)

    response = client.get(f"{PAYMENTS}/{payment.public_id}")
    data = data_of(response)

    assert list(data) == DETAIL_KEYS
    assert data["id"] == payment.public_id
    assert data["status"] == "REVIEW_REQUIRED"
    assert data["review_required_at"] == CREATED.isoformat()
    assert data["expires_at"] == EXPIRES.isoformat()
    for key in (
        "confirmed_amount",
        "confirmed_currency",
        "credited_amount",
        "gateway_payment_intent_id",
        "last_reconciled_at",
        "resolved_at",
        "resolved_by",
        "resolution_reason",
    ):
        assert data[key] is None, key
    assert [event["gateway_event_id"] for event in data["events"]] == [
        early_one,
        late_first,
        late_second,
    ]
    for event in data["events"]:
        assert list(event) == EVENT_KEYS
    assert data["events"][0] == {
        "gateway_event_id": early_one,
        "event_type": "checkout.session.async_payment_succeeded",
        "processing_status": "RETRY",
        "last_error_code": "HTTP_503",
        "received_at": early.isoformat(),
        "processed_at": None,
        "fingerprint_conflict_at": late.isoformat(),
    }
    assert data["events"][1]["processed_at"] == (late + dt.timedelta(seconds=5)).isoformat()
    # `metadata_json` 不出响应。
    assert METADATA_MARKER not in response.text
    assert "metadata" not in response.text


def test_detail_lists_at_most_100_events_oldest_first(world, client) -> None:
    payment = world.payment(world.customer())
    ids = [
        add_event(world, payment, received=CREATED + dt.timedelta(seconds=index))
        for index in range(101)
    ]

    data = data_of(client.get(f"{PAYMENTS}/{payment.public_id}"))

    assert [event["gateway_event_id"] for event in data["events"]] == ids[:100]


def test_an_unknown_payment_is_404(world, client) -> None:
    for payment_id in (str(uuid.uuid4()), "not-a-uuid"):
        assert error_of(client.get(f"{PAYMENTS}/{payment_id}")) == (404, "PAYMENT_NOT_FOUND")


# --- 结案 ---------------------------------------------------------------


def test_credit_returns_the_detail_with_the_credited_amount(world, client, caplog) -> None:
    """应收 100.00、实收 99.99：TOPUP 99.99，详情的 `credited_amount` 取账本行。"""
    caplog.set_level(logging.DEBUG)
    payment = in_review(world)
    body = serve_paid(world, payment, amount_total=9999)

    response = resolve(client, payment, {"action": "CREDIT", "reason": f"  {REASON}  "})
    data = data_of(response)

    assert list(data) == DETAIL_KEYS
    assert (data["status"], data["status_reason"], data["resolution"]) == (
        "PAID",
        AMOUNT_MISMATCH,
        "CREDITED",
    )
    assert data["amount"] == "100.00000000"
    assert (data["confirmed_amount"], data["confirmed_currency"]) == ("99.99000000", "MYR")
    assert data["credited_amount"] == "99.99000000"
    assert data["gateway_payment_intent_id"] == body["payment_intent"]["id"]
    assert data["gateway_fee"] == "2.90000000"
    assert data["paid_at"] == data["resolved_at"] == RESOLVED_AT.isoformat()
    assert data["last_reconciled_at"] == RESOLVED_AT.isoformat()
    assert data["resolved_by"] == admin_email(world)
    # 请求层去掉了首尾空白。
    assert data["resolution_reason"] == REASON
    [topup] = world.topups(payment)
    assert topup.amount == Decimal("99.99")
    # 与 GET 详情是同一个查询。
    assert data_of(client.get(f"{PAYMENTS}/{payment.public_id}")) == data
    # 原因只进库与审计，不进日志（设计 §6）。
    assert REASON not in caplog.text
    assert "Z4PQ8" not in caplog.text


def test_close_returns_the_detail_without_a_credit(world, client) -> None:
    payment = in_review(world)

    data = data_of(resolve(client, payment, {"action": "CLOSE", "reason": REASON}))

    assert (data["status"], data["status_reason"], data["resolution"]) == (
        "FAILED",
        AMOUNT_MISMATCH,
        "CLOSED",
    )
    assert data["credited_amount"] is None
    assert (data["confirmed_amount"], data["confirmed_currency"]) == ("99.99000000", "MYR")
    assert data["resolved_at"] == RESOLVED_AT.isoformat()
    assert data["resolved_by"] == admin_email(world)
    assert data["resolution_reason"] == REASON
    assert data["paid_at"] is None
    assert world.topups(payment) == []
    # CLOSE 不查 Stripe。
    assert world.stripe.asked == []
    assert data_of(client.get(f"{PAYMENTS}/{payment.public_id}")) == data


def test_a_reason_of_255_characters_is_accepted(world, client) -> None:
    payment = in_review(world)

    data = data_of(resolve(client, payment, {"action": "CLOSE", "reason": "r" * 255}))

    assert data["resolution_reason"] == "r" * 255


@pytest.mark.parametrize(
    "body",
    [
        {"action": "CLOSE", "reason": "Probe", "amount": "100.00"},
        {"action": "CLOSE", "reason": "Probe", "customer_id": "x"},
        {"action": "CLOSE", "reason": ""},
        {"action": "CLOSE", "reason": "   "},
        {"action": "CLOSE", "reason": "r" * 256},
        {"action": "CLOSE", "reason": None},
        {"action": "CLOSE"},
        {"reason": "Probe"},
        {"action": "close", "reason": "Probe"},
        {"action": "REFUND", "reason": "Probe"},
        {"action": "CREDIT", "reason": 7},
        [],
    ],
    ids=[
        "extra-amount",
        "extra-customer",
        "empty-reason",
        "blank-reason",
        "long-reason",
        "null-reason",
        "no-reason",
        "no-action",
        "lowercase-action",
        "unknown-action",
        "number-reason",
        "not-an-object",
    ],
)
def test_invalid_bodies_are_422_and_change_nothing(world, client, body) -> None:
    payment = in_review(world)
    serve_paid(world, payment)
    before = unchanged(world, payment)

    assert error_of(resolve(client, payment, body)) == (422, "VALIDATION_ERROR")

    assert unchanged(world, payment) == before
    assert world.stripe.asked == []


def test_with_the_switch_off_credit_is_503_and_close_is_200(
    world, make_client, monkeypatch
) -> None:
    def no_gateway(settings):
        raise AssertionError("the gateway must not be read with the switch off")

    monkeypatch.setattr(admin_payments, "get_payment_gateway", no_gateway)
    client = make_client(payments_enabled=False)
    credited = in_review(world)
    closed = in_review(world)
    serve_paid(world, credited)
    before = unchanged(world, credited)

    refused = resolve(client, credited, {"action": "CREDIT", "reason": REASON})

    assert error_of(refused) == (503, "PAYMENTS_MISCONFIGURED")
    # 在 CLOSE 写入它自己的审计之前比较，拒绝的 CREDIT 不留下任何写入。
    assert unchanged(world, credited) == before
    assert world.stripe.asked == []

    accepted = resolve(client, closed, {"action": "CLOSE", "reason": REASON})

    assert data_of(accepted)["resolution"] == "CLOSED"
    # 读接口与开关无关。
    assert set(listed(client)) == {credited.public_id, closed.public_id}
    assert data_of(client.get(f"{PAYMENTS}/{credited.public_id}"))["status"] == "REVIEW_REQUIRED"


def test_with_the_switch_off_the_premises_come_before_the_503(world, make_client) -> None:
    client = make_client(payments_enabled=False)
    paid = world.payment(world.customer(), status=PaymentStatus.PAID)

    unknown = resolve(client, str(uuid.uuid4()), {"action": "CREDIT", "reason": REASON})
    not_in_review = resolve(client, paid, {"action": "CREDIT", "reason": REASON})

    assert error_of(unknown) == (404, "PAYMENT_NOT_FOUND")
    assert error_of(not_in_review) == (409, "PAYMENT_NOT_IN_REVIEW")


def test_without_usable_credentials_credit_is_503(world, make_client, monkeypatch) -> None:
    monkeypatch.setattr(admin_payments, "get_payment_gateway", lambda settings: None)
    client = make_client()
    payment = in_review(world)
    before = unchanged(world, payment)

    response = resolve(client, payment, {"action": "CREDIT", "reason": REASON})

    assert error_of(response) == (503, "PAYMENTS_MISCONFIGURED")
    assert unchanged(world, payment) == before


@pytest.mark.parametrize(
    ("arrange", "action", "expected"),
    [
        ("unknown", "CLOSE", (404, "PAYMENT_NOT_FOUND")),
        ("paid", "CLOSE", (409, "PAYMENT_NOT_IN_REVIEW")),
        ("paid", "CREDIT", (409, "PAYMENT_NOT_IN_REVIEW")),
        ("stripe-down", "CREDIT", (503, "PAYMENT_GATEWAY_UNAVAILABLE")),
        ("no-session", "CREDIT", (409, "GATEWAY_QUERY_REJECTED")),
        ("refunded", "CREDIT", (409, "GATEWAY_REFUNDED_OR_DISPUTED")),
        ("disputed", "CREDIT", (409, "GATEWAY_REFUNDED_OR_DISPUTED")),
        ("not-myr", "CREDIT", (409, "CURRENCY_NOT_MYR")),
        ("not-paid", "CREDIT", (409, "GATEWAY_NOT_PAID")),
    ],
)
def test_resolution_errors_reach_the_envelope_unchanged(
    world, client, arrange, action, expected
) -> None:
    payment: PaymentRef | str
    if arrange == "unknown":
        payment = str(uuid.uuid4())
    elif arrange == "paid":
        payment = world.payment(world.customer(), status=PaymentStatus.PAID)
    else:
        payment = in_review(world)
        if arrange == "stripe-down":
            serve_paid(world, payment)
            world.stripe.status_code = 503
        elif arrange == "refunded":
            serve_paid(world, payment, refunded=100)
        elif arrange == "disputed":
            serve_paid(world, payment, disputed=True)
        elif arrange == "not-myr":
            serve_paid(world, payment, currency="usd")
        elif arrange == "not-paid":
            world.serve(payment, GatewayState.OPEN)
        # "no-session"：假 Stripe 没有这个会话，回 404 `resource_missing`。
    before = unchanged(world, payment) if isinstance(payment, PaymentRef) else None

    response = resolve(client, payment, {"action": action, "reason": REASON})

    assert error_of(response) == expected
    assert REASON not in response.text
    if isinstance(payment, PaymentRef):
        assert unchanged(world, payment) == before


def test_a_second_resolution_is_refused(world, client) -> None:
    payment = in_review(world)
    data_of(resolve(client, payment, {"action": "CLOSE", "reason": REASON}))
    after_first = unchanged(world, payment)

    again = resolve(client, payment, {"action": "CLOSE", "reason": "Another reason"})
    credit = resolve(client, payment, {"action": "CREDIT", "reason": "Another reason"})

    assert error_of(again) == (409, "PAYMENT_NOT_IN_REVIEW")
    assert error_of(credit) == (409, "PAYMENT_NOT_IN_REVIEW")
    assert unchanged(world, payment) == after_first
