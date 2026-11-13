"""Customer top-up options, payment list and payment detail end to end (AIH-TASK-190).

设计闸门 #587 v3 §2「客户接口」「最小恢复额」、§6；§7 标 190 的各行：

- **充值选项**：五种不可充值原因的顺序（每一行都让排在它后面的原因同时成立）、可充值、允许名单、
  挂起租户的最小恢复额（余额 0、−12.345、正余额）、最高额为空而恢复额超过 999999.99、恢复额超过
  设了的最高额、字段与顺序、两位小数的金额；设置行缺失时充值选项 503 `PAYMENTS_MISCONFIGURED` 而
  列表与详情照常；不查 Stripe、不读密钥文件；
- **支付列表与详情**：七种客户侧状态的映射（自动入账、人工按实付入账、不入账结案经 B 的
  `resolve_review`）、人工按实付入账的 `credited_amount`（99.99 而 `amount` 100.00）、分页与
  `total`、排序（`created_at` 降序、`id` 降序）、页码越界 422；
- **客户接口字段**：只有设计列出的十个键（#587 的八个加 #589 v2 的 `receipt_id`、
  `receipt_number`，AIH-TASK-202），手续费、原因、网关编号、结案字段、`confirmed_*` 不在
  响应里（INV-7）；
- **跨租户**：别的租户的支付 404、列表里没有；格式不像 uuid 的 id 与内部自增 id 都是 404（INV-8）；
  `DISABLED` / `CLOSED` 照常可读。

管理员与匿名调用者的拒绝由 test_customer_api.py 的路由枚举覆盖（三个接口已补进客户路由集合）。

世界用共用的 payment_world.py（SQLite 与真 MySQL 各跑一次）；本文件的夹具只在这里用，定义在这里。
SQLite 用 `StaticPool`（TestClient 在线程池里跑处理函数）并装上 test_usage_billing 那条代替触发器
推进钱包的测试触发器；余额只经真实的 `post_transaction` 变动。Stripe 一律是注入了假传输的
`StripeGateway`，不联网；确认与结案的时刻是固定值，不 sleep。

MySQL 那部分需要 `BILLING_TEST_DATABASE_URL`，没设时 skip ——
**不要把 skipped 读成 passed**，CI 设了它并把 skipped 判成失败。
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable, Iterator
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from payment_world import (
    CREATED,
    EXPIRES,
    NOW,
    TEST_EMAIL_DOMAIN,
    Customer,
    PaymentRef,
    World,
    _populate,
    build_mysql_world,
    remove_payment_settings,
    seed_payment_settings,
)
from sqlalchemy import create_engine, delete, select, update
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from test_usage_billing import _SQLITE_LEDGER_TRIGGER

from app.core import config as config_module
from app.core.config import Settings
from app.core.database import create_session_factory
from app.core.tokens import issue_access_token
from app.main import create_app
from app.models.auth import User, UserRole, UserStatus
from app.models.base import Base
from app.models.payments import Payment, PaymentStatus
from app.models.tenancy import AccountStatus, BillingMode
from app.models.wallet import ReferenceType, TransactionType
from app.payments.base import GatewayState
from app.repositories.wallet import post_transaction
from app.services.auth import RequestContext, utc_now
from app.services.payment_review import ReviewAction, resolve_review
from app.services.payments import AMOUNT_MISMATCH

TOPUP_OPTIONS = "/api/v1/customer/topup-options"
PAYMENTS = "/api/v1/customer/payments"

PLACEHOLDER_SESSION = "00000000-0000-0000-0000-000000000000"
CONTEXT = RequestContext(ip_address="10.0.0.1", user_agent="customer-payments-test")
# 固定值而不是 utc_now()：断言「存进去的就是调用方给的那个时刻」要能逐字比对。
RESOLVED_AT = dt.datetime(2026, 11, 9, 10, 0, 0)
# 足够独特，响应里出现任何一段都认得出来。
GATEWAY_FEE = Decimal("7.77")
RESOLUTION_REASON = "Fictional review note for a customer payments test"

# 设计 §2 的字段与顺序：断言「恰好等于」，按顺序比。
OPTION_KEYS = [
    "currency",
    "available",
    "unavailable_reason",
    "min_amount",
    "max_amount",
    "preset_amounts",
    "recovery_minimum",
    "effective_minimum",
]
PAYMENT_KEYS = [
    "id",
    "amount",
    "currency",
    "status",
    "credited_amount",
    "created_at",
    "paid_at",
    "expires_at",
    # 设计闸门 #589 v2 §2「客户接口」（AIH-TASK-202）追加的收据字段。
    "receipt_id",
    "receipt_number",
]
PAGE_KEYS = {"items", "page", "page_size", "total"}
ENVELOPE_KEYS = {"success", "data", "error", "request_id"}
SEED_PRESETS = ["50.00", "100.00", "300.00", "500.00", "1000.00"]


# --- 夹具 ---------------------------------------------------------------


def _sqlite_world() -> Iterator[World]:
    # StaticPool：TestClient 在线程池里跑同步端点，内存库必须所有连接共用一个。
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql(_SQLITE_LEDGER_TRIGGER)
    try:
        yield _populate(create_session_factory(engine))
    finally:
        engine.dispose()


def _delete_portal_users(factory: sessionmaker[Session]) -> None:
    # 门户账号挂在租户上（RESTRICT）：先删它们，payment_world 的清场才删得掉租户。
    with factory() as session:
        session.execute(
            delete(User).where(
                User.role == UserRole.CUSTOMER, User.email.like(f"%{TEST_EMAIL_DOMAIN}")
            )
        )
        session.commit()


@pytest.fixture(params=["sqlite", "mysql"])
def world(request, monkeypatch, payment_settings_seeder) -> Iterator[World]:
    """The world with the seed payment settings of migration 0038."""
    source = build_mysql_world(monkeypatch) if request.param == "mysql" else _sqlite_world()
    built = next(source)
    try:
        payment_settings_seeder(built.factory)
        yield built
    finally:
        _delete_portal_users(built.factory)
        for _ in source:
            pass


class Portal:
    """An app over the world's database, and the customers' signed-in headers."""

    def __init__(self, world: World, key_file: str, **overrides: Any) -> None:
        self.world = world
        options: dict[str, Any] = {"payments_enabled": True, "payment_topups_open": True}
        options.update(overrides)
        self.app = create_app(Settings(jwt_secret_file=key_file, database_url="", **options))
        self.app.state.session_factory = world.factory
        # 500 要以统一错误体返回，而不是在测试里抛出来。
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def headers(self, tenant: Customer) -> dict[str, str]:
        """A portal user of `tenant` and its access token (no 2FA: not required)."""
        with self.world.factory() as session:
            user = User(
                email=f"{uuid.uuid4().hex}{TEST_EMAIL_DOMAIN}",
                password_hash="not-a-real-hash",
                role=UserRole.CUSTOMER,
                status=UserStatus.ACTIVE,
                tenant_id=tenant.id,
                activated_at=CREATED,
                created_at=CREATED,
                updated_at=CREATED,
            )
            session.add(user)
            session.commit()
            user_id = int(user.id)
        token = issue_access_token(
            self.app.state.settings,
            user_id=user_id,
            role=UserRole.CUSTOMER.value,
            session_id=PLACEHOLDER_SESSION,
            now=utc_now(),
        )
        return {"Authorization": f"Bearer {token}"}

    def get(self, path: str, headers: dict[str, str], **params: Any):
        return self.client.get(path, params=params, headers=headers)


@pytest.fixture
def make_portal(world, tmp_path) -> Callable[..., Portal]:
    key = tmp_path / "jwt.key"
    key.write_text("a-test-signing-key-that-is-long-enough", encoding="utf-8")

    def build(**overrides: Any) -> Portal:
        return Portal(world, str(key), **overrides)

    return build


@pytest.fixture
def portal(make_portal) -> Portal:
    return make_portal()


# --- 构件 ---------------------------------------------------------------


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


def set_balance(world: World, tenant: Customer, balance: str) -> None:
    """Move the wallet (which starts at 0) to `balance` through one real ledger posting."""
    with world.factory() as session:
        post_transaction(
            session,
            tenant_id=tenant.id,
            transaction_type=TransactionType.SYSTEM_CORRECTION,
            amount=Decimal(balance),
            reference_type=ReferenceType.SYSTEM,
            reference_id=f"customer-payments-test-{uuid.uuid4().hex}",
            now=CREATED,
            description="Fictional correction for a customer payments test",
        )
        session.commit()
    assert world.wallet(tenant)[0] == Decimal(balance)


def set_columns(world: World, payment: PaymentRef, **values: Any) -> None:
    with world.factory() as session:
        session.execute(
            update(Payment).where(Payment.public_id == payment.public_id).values(**values)
        )
        session.commit()


def created_at(world: World, tenant: Customer, moment: dt.datetime) -> PaymentRef:
    payment = world.payment(tenant)
    set_columns(world, payment, created_at=moment)
    return payment


def internal_id(world: World, payment: PaymentRef) -> int:
    with world.factory() as session:
        return session.execute(
            select(Payment.id).where(Payment.public_id == payment.public_id)
        ).scalar_one()


def admin(world: World) -> User:
    with world.factory() as session:
        user = session.get(User, world.admin_id)
        assert user is not None
        session.expunge(user)
        return user


def in_review(world: World, tenant: Customer) -> PaymentRef:
    """A `REVIEW_REQUIRED` payment (应收 100.00, Stripe reported 99.99) as A would leave it."""
    payment = world.payment(tenant, status=PaymentStatus.REVIEW_REQUIRED, reason=AMOUNT_MISMATCH)
    set_columns(world, payment, confirmed_amount=Decimal("99.99"), confirmed_currency="MYR")
    return payment


def resolve(world: World, payment: PaymentRef, action: ReviewAction) -> None:
    """B's real resolution, against the fake Stripe."""
    if action is ReviewAction.CREDIT:
        body = world.serve(payment, GatewayState.PAID, amount_total=9999)
        latest = body["payment_intent"]["latest_charge"]
        latest["amount_refunded"] = 0
        latest["disputed"] = False
    resolve_review(
        world.factory,
        world.stripe.gateway(),
        actor=admin(world),
        payment_id=payment.public_id,
        action=action,
        reason=RESOLUTION_REASON,
        context=CONTEXT,
        now=RESOLVED_AT,
    )


def listed_ids(portal: Portal, headers: dict[str, str], **params: Any) -> list[str]:
    data = data_of(portal.get(PAYMENTS, headers, **params))
    assert set(data) == PAGE_KEYS
    return [item["id"] for item in data["items"]]


# --- 充值选项 -----------------------------------------------------------


def test_options_for_a_customer_who_can_top_up(world, portal) -> None:
    tenant = world.customer()
    set_balance(world, tenant, "5.00")

    data = data_of(portal.get(TOPUP_OPTIONS, portal.headers(tenant)))

    assert list(data) == OPTION_KEYS
    assert data == {
        "currency": "MYR",
        "available": True,
        "unavailable_reason": None,
        # 金额：两位小数的定点字符串。
        "min_amount": "10.00",
        "max_amount": None,
        "preset_amounts": SEED_PRESETS,
        "recovery_minimum": None,
        "effective_minimum": "10.00",
    }


@pytest.mark.parametrize(
    ("overrides", "account_status", "billing_mode", "allowlisted", "expected"),
    [
        # 每一行都让排在它后面的原因同时成立：只有顺序决定取哪一个。
        (
            {"payments_enabled": False, "payment_topups_open": False},
            AccountStatus.DISABLED,
            BillingMode.INTERNAL_METERED_ONLY,
            False,
            "PAYMENTS_DISABLED",
        ),
        (
            {"payment_topups_open": False},
            AccountStatus.DISABLED,
            BillingMode.INTERNAL_METERED_ONLY,
            False,
            "ACCOUNT_READ_ONLY",
        ),
        (
            {"payment_topups_open": False},
            AccountStatus.CLOSED,
            BillingMode.PREPAID,
            False,
            "ACCOUNT_READ_ONLY",
        ),
        (
            {"payment_topups_open": False},
            AccountStatus.ENABLED,
            BillingMode.INTERNAL_METERED_ONLY,
            False,
            "TOPUPS_NOT_OPEN",
        ),
        # 允许名单里的租户越过开放检查，落到计费方式。
        (
            {"payment_topups_open": False},
            AccountStatus.ENABLED,
            BillingMode.INTERNAL_METERED_ONLY,
            True,
            "TOPUP_NOT_ALLOWED",
        ),
        ({}, AccountStatus.ENABLED, BillingMode.INTERNAL_METERED_ONLY, False, "TOPUP_NOT_ALLOWED"),
        ({}, AccountStatus.ENABLED, BillingMode.PREPAID, False, "RECOVERY_EXCEEDS_MAXIMUM"),
    ],
    ids=[
        "switch-off-first",
        "disabled-before-not-open",
        "closed",
        "not-open-before-internal",
        "allowlisted-internal",
        "internal-before-recovery",
        "recovery-exceeds",
    ],
)
def test_the_first_unavailable_reason_wins(
    world, make_portal, overrides, account_status, billing_mode, allowlisted, expected
) -> None:
    """欠款 999990.00、缓冲 10.00：恢复额 1000000.00 超过单笔上限 999999.99（最高额为空）。"""
    tenant = world.customer(account_status=account_status, billing_mode=billing_mode)
    set_balance(world, tenant, "-999990.00")
    if allowlisted:
        overrides = {**overrides, "payment_topup_tenant_allowlist": tenant.public_id.upper()}
    portal = make_portal(**overrides)

    data = data_of(portal.get(TOPUP_OPTIONS, portal.headers(tenant)))

    assert list(data) == OPTION_KEYS
    assert (data["available"], data["unavailable_reason"]) == (False, expected)
    # 不可充值时金额字段照样返回。
    assert (data["min_amount"], data["max_amount"], data["preset_amounts"]) == (
        "10.00",
        None,
        SEED_PRESETS,
    )
    if billing_mode is BillingMode.PREPAID:
        assert (data["recovery_minimum"], data["effective_minimum"]) == (
            "1000000.00",
            "1000000.00",
        )
    else:
        # 不是预付：没有恢复额。
        assert (data["recovery_minimum"], data["effective_minimum"]) == (None, "10.00")


def test_an_allowlisted_customer_can_top_up_before_the_opening(world, make_portal) -> None:
    listed = world.customer()
    other = world.customer()
    portal = make_portal(payment_topups_open=False, payment_topup_tenant_allowlist=listed.public_id)

    allowed = data_of(portal.get(TOPUP_OPTIONS, portal.headers(listed)))
    refused = data_of(portal.get(TOPUP_OPTIONS, portal.headers(other)))

    assert (allowed["available"], allowed["unavailable_reason"]) == (True, None)
    assert (refused["available"], refused["unavailable_reason"]) == (False, "TOPUPS_NOT_OPEN")


@pytest.mark.parametrize(
    ("balance", "recovery", "effective"),
    [
        # 余额恰好 0 也算挂起：恢复额 = 0 + 10.00。
        (None, "10.00", "10.00"),
        # 欠款三位小数向上取到分：12.345 + 10.00 → 22.35。
        ("-12.345", "22.35", "22.35"),
        ("-0.00000001", "10.01", "10.01"),
        ("5.00", None, "10.00"),
    ],
    ids=["zero", "three-decimals", "one-unit", "positive"],
)
def test_a_suspended_customer_sees_the_recovery_minimum(
    world, portal, balance, recovery, effective
) -> None:
    tenant = world.customer()
    if balance is not None:
        set_balance(world, tenant, balance)

    data = data_of(portal.get(TOPUP_OPTIONS, portal.headers(tenant)))

    assert (data["available"], data["unavailable_reason"]) == (True, None)
    assert (data["recovery_minimum"], data["effective_minimum"]) == (recovery, effective)
    assert data["min_amount"] == "10.00"


def test_a_recovery_above_the_set_maximum_cannot_top_up(world, portal) -> None:
    """最高额 100.00、欠款 95.00：恢复额 105.00 大于最高额。"""
    seed_payment_settings(
        world.factory, max_amount=Decimal("100.00"), preset_amounts=["50.00", "100.00"]
    )
    tenant = world.customer()
    set_balance(world, tenant, "-95.00")
    fits = world.customer()
    set_balance(world, fits, "-90.00")

    over = data_of(portal.get(TOPUP_OPTIONS, portal.headers(tenant)))
    at_cap = data_of(portal.get(TOPUP_OPTIONS, portal.headers(fits)))

    assert (over["available"], over["unavailable_reason"]) == (False, "RECOVERY_EXCEEDS_MAXIMUM")
    assert (over["max_amount"], over["recovery_minimum"], over["effective_minimum"]) == (
        "100.00",
        "105.00",
        "105.00",
    )
    assert over["preset_amounts"] == ["50.00", "100.00"]
    # 恢复额恰好等于最高额：仍可充值。
    assert (at_cap["available"], at_cap["recovery_minimum"]) == (True, "100.00")


def test_options_follow_the_payment_settings(world, portal) -> None:
    seed_payment_settings(
        world.factory,
        min_amount=Decimal("25.50"),
        max_amount=Decimal("5000.00"),
        recovery_buffer=Decimal("0.01"),
        preset_amounts=["25.50", "4999.99"],
    )
    tenant = world.customer()
    set_balance(world, tenant, "-50.00")

    data = data_of(portal.get(TOPUP_OPTIONS, portal.headers(tenant)))

    assert data == {
        "currency": "MYR",
        "available": True,
        "unavailable_reason": None,
        "min_amount": "25.50",
        "max_amount": "5000.00",
        "preset_amounts": ["25.50", "4999.99"],
        # 缓冲 0.01：按它充值后余额为 0.01（> 0）。
        "recovery_minimum": "50.01",
        "effective_minimum": "50.01",
    }


def test_without_the_settings_row_options_are_503_and_payments_still_read(world, portal) -> None:
    tenant = world.customer()
    payment = world.payment(tenant)
    headers = portal.headers(tenant)
    remove_payment_settings(world.factory)

    options = portal.get(TOPUP_OPTIONS, headers)
    listing = data_of(portal.get(PAYMENTS, headers))
    detail = data_of(portal.get(f"{PAYMENTS}/{payment.public_id}", headers))

    assert error_of(options) == (503, "PAYMENTS_MISCONFIGURED")
    assert [item["id"] for item in listing["items"]] == [payment.public_id]
    assert detail["id"] == payment.public_id


def test_options_neither_ask_stripe_nor_read_the_key_files(world, make_portal, monkeypatch) -> None:
    def no_secret_file(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("the secret files must not be read")

    monkeypatch.setattr(config_module, "_read_secret_file", no_secret_file)
    portal = make_portal(
        stripe_secret_key_file="/nonexistent/stripe-secret-key",
        stripe_webhook_secret_file="/nonexistent/stripe-webhook-secret",
    )
    tenant = world.customer()

    data = data_of(portal.get(TOPUP_OPTIONS, portal.headers(tenant)))

    assert data["available"] is True
    assert world.stripe.asked == []


def test_the_routes_are_served_with_payments_switched_off(world, make_portal) -> None:
    portal = make_portal(payments_enabled=False)
    tenant = world.customer()
    payment = world.payment(tenant)
    headers = portal.headers(tenant)

    options = data_of(portal.get(TOPUP_OPTIONS, headers))

    assert (options["available"], options["unavailable_reason"]) == (False, "PAYMENTS_DISABLED")
    assert listed_ids(portal, headers) == [payment.public_id]
    assert data_of(portal.get(f"{PAYMENTS}/{payment.public_id}", headers))["status"] == "PENDING"


# --- 支付列表与详情 -------------------------------------------------------


def test_the_seven_customer_statuses_and_the_credited_amounts(world, portal) -> None:
    tenant = world.customer()
    pending = world.payment(tenant)
    auto_paid = world.payment(tenant)
    world.serve(auto_paid, GatewayState.PAID)
    assert world.confirm(auto_paid).posted
    credited = in_review(world, tenant)
    resolve(world, credited, ReviewAction.CREDIT)
    closed = in_review(world, tenant)
    resolve(world, closed, ReviewAction.CLOSE)
    reviewing = in_review(world, tenant)
    failed = world.payment(tenant, status=PaymentStatus.FAILED, reason="GATEWAY_FAILED")
    expired = world.payment(tenant, status=PaymentStatus.EXPIRED, reason="GATEWAY_EXPIRED")
    cancelled = world.payment(tenant, status=PaymentStatus.CANCELLED)
    headers = portal.headers(tenant)
    expected = {
        pending.public_id: ("PENDING", None, None),
        auto_paid.public_id: ("PAID", "100.00", NOW.isoformat()),
        # 人工按实付入账：`credited_amount` 取账本行，不等于 `amount`。
        credited.public_id: ("PAID", "99.99", RESOLVED_AT.isoformat()),
        closed.public_id: ("NOT_CREDITED", None, None),
        reviewing.public_id: ("IN_REVIEW", None, None),
        failed.public_id: ("FAILED", None, None),
        expired.public_id: ("EXPIRED", None, None),
        cancelled.public_id: ("CANCELLED", None, None),
    }

    listing = data_of(portal.get(PAYMENTS, headers))
    items = {item["id"]: item for item in listing["items"]}

    assert listing["total"] == len(expected)
    assert set(items) == set(expected)
    for public_id, (status, credited_amount, paid_at) in expected.items():
        item = items[public_id]
        assert list(item) == PAYMENT_KEYS
        assert item == {
            "id": public_id,
            "amount": "100.00",
            "currency": "MYR",
            "status": status,
            "credited_amount": credited_amount,
            "created_at": CREATED.isoformat(),
            "paid_at": paid_at,
            "expires_at": EXPIRES.isoformat(),
            # 世界自带的税务政策版本没有 `document_notice`，开具检查不过：没有收据。有收据时的
            # 两个字段由 test_customer_receipts_api.py 覆盖。
            "receipt_id": None,
            "receipt_number": None,
        }, public_id
        # 详情与列表项是同一组字段、同样的值。
        assert data_of(portal.get(f"{PAYMENTS}/{public_id}", headers)) == item


def test_responses_carry_only_the_whitelisted_fields(world, portal) -> None:
    """INV-7：手续费、原因、网关编号、结案字段与 `confirmed_*` 都不出客户接口。"""
    tenant = world.customer()
    paid = world.payment(tenant)
    body = world.serve(paid, GatewayState.PAID, fee=777)
    assert world.confirm(paid).posted
    closed = in_review(world, tenant)
    resolve(world, closed, ReviewAction.CLOSE)
    reviewing = in_review(world, tenant)
    headers = portal.headers(tenant)
    assert world.row(paid).gateway_fee == GATEWAY_FEE

    responses = [portal.get(PAYMENTS, headers)] + [
        portal.get(f"{PAYMENTS}/{payment.public_id}", headers)
        for payment in (paid, closed, reviewing)
    ]

    # 键的白名单：恰好十个（含两个收据字段）、按顺序
    # （手续费、原因、结案、网关、`confirmed_*` 的键都不在）。
    items = data_of(responses[0])["items"]
    assert len(items) == 3
    for item in items:
        assert list(item) == PAYMENT_KEYS
    for response in responses[1:]:
        assert list(data_of(response)) == PAYMENT_KEYS
    # 值也不出现。⚠️ 不查只由十六进制字母组成的短词：uuid 与 request_id 里可能碰巧有。
    text = "".join(response.text for response in responses)
    for forbidden in (
        str(GATEWAY_FEE),
        "99.99",
        AMOUNT_MISMATCH,
        RESOLUTION_REASON,
        "REVIEW_REQUIRED",
        "CLOSED",
        "STRIPE",
        "cs_test_",
        "pi_test_",
        body["payment_intent"]["id"],
        tenant.public_id,
    ):
        assert forbidden not in text, forbidden


def test_the_list_is_newest_first_then_by_descending_id_and_paged(world, portal) -> None:
    tenant = world.customer()
    same_moment = [created_at(world, tenant, CREATED) for _ in range(3)]
    newest = created_at(world, tenant, CREATED + dt.timedelta(seconds=1))
    oldest = created_at(world, tenant, CREATED - dt.timedelta(days=1))
    headers = portal.headers(tenant)
    # 同一 `created_at` 的三笔按插入顺序（id 递增）建，列表里倒过来。
    expected = [newest.public_id, *(p.public_id for p in reversed(same_moment)), oldest.public_id]

    assert listed_ids(portal, headers) == expected
    pages = [data_of(portal.get(PAYMENTS, headers, page=page, page_size=2)) for page in (1, 2, 3)]
    beyond = data_of(portal.get(PAYMENTS, headers, page=4, page_size=2))

    assert [[item["id"] for item in page["items"]] for page in pages] == [
        expected[:2],
        expected[2:4],
        expected[4:],
    ]
    assert [(page["page"], page["page_size"], page["total"]) for page in pages] == [
        (1, 2, 5),
        (2, 2, 5),
        (3, 2, 5),
    ]
    assert (beyond["items"], beyond["page"], beyond["total"]) == ([], 4, 5)


def test_the_default_page_is_20_and_total_counts_every_payment(world, portal) -> None:
    tenant = world.customer()
    for _ in range(21):
        world.payment(tenant)
    headers = portal.headers(tenant)

    first = data_of(portal.get(PAYMENTS, headers))
    second = data_of(portal.get(PAYMENTS, headers, page=2))

    assert (len(first["items"]), first["page"], first["page_size"], first["total"]) == (
        20,
        1,
        20,
        21,
    )
    assert (len(second["items"]), second["page"], second["total"]) == (1, 2, 21)
    assert {item["id"] for item in first["items"]}.isdisjoint(
        item["id"] for item in second["items"]
    )


@pytest.mark.parametrize(
    "params",
    [{"page": 0}, {"page": -1}, {"page_size": 0}, {"page_size": 101}, {"page": "x"}],
)
def test_page_bounds_are_422(world, portal, params) -> None:
    tenant = world.customer()

    response = portal.get(PAYMENTS, portal.headers(tenant), **params)

    assert error_of(response) == (422, "VALIDATION_ERROR")


def test_page_size_100_is_accepted(world, portal) -> None:
    tenant = world.customer()
    world.payment(tenant)

    data = data_of(portal.get(PAYMENTS, portal.headers(tenant), page_size=100))

    assert (data["page_size"], data["total"]) == (100, 1)


# --- 跨租户与 404 --------------------------------------------------------


def test_another_customers_payment_is_404_and_not_listed(world, portal) -> None:
    """INV-8：租户只来自令牌主体；查询串里的租户参数不读。"""
    mine = world.customer()
    theirs = world.customer()
    my_payment = world.payment(mine)
    their_payment = world.payment(theirs, status=PaymentStatus.PAID)
    headers = portal.headers(mine)

    plain = listed_ids(portal, headers)
    forged = listed_ids(
        portal,
        headers,
        tenant_id=str(theirs.id),
        customer_id=theirs.public_id,
    )
    detail = portal.get(f"{PAYMENTS}/{their_payment.public_id}", headers)

    assert plain == forged == [my_payment.public_id]
    assert error_of(detail) == (404, "PAYMENT_NOT_FOUND")
    assert their_payment.public_id not in detail.text
    # 对方自己照常看得到。
    other = portal.headers(theirs)
    assert data_of(portal.get(f"{PAYMENTS}/{their_payment.public_id}", other))["status"] == "PAID"


def test_unknown_or_malformed_payment_ids_are_404(world, portal) -> None:
    tenant = world.customer()
    payment = world.payment(tenant)
    headers = portal.headers(tenant)
    unknown = [
        str(uuid.uuid4()),
        "not-a-uuid",
        # 内部自增 id 不是对外的 id。
        str(internal_id(world, payment)),
        "x" * 300,
    ]

    for payment_id in unknown:
        response = portal.get(f"{PAYMENTS}/{payment_id}", headers)
        assert error_of(response) == (404, "PAYMENT_NOT_FOUND"), payment_id
    assert data_of(portal.get(f"{PAYMENTS}/{payment.public_id}", headers))["id"] == (
        payment.public_id
    )


@pytest.mark.parametrize("status", [AccountStatus.DISABLED, AccountStatus.CLOSED])
def test_read_only_accounts_still_read_all_three(world, portal, status) -> None:
    tenant = world.customer(account_status=status)
    payment = world.payment(tenant)
    headers = portal.headers(tenant)

    options = data_of(portal.get(TOPUP_OPTIONS, headers))

    assert (options["available"], options["unavailable_reason"]) == (False, "ACCOUNT_READ_ONLY")
    assert listed_ids(portal, headers) == [payment.public_id]
    assert data_of(portal.get(f"{PAYMENTS}/{payment.public_id}", headers))["id"] == (
        payment.public_id
    )
