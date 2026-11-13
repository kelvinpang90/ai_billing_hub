"""Customer receipts end to end: list, download and the payments' receipt fields (AIH-TASK-202).

设计闸门 #589 v2 §2「客户接口」「PDF」、§3 INV-7 与 INV-8、§5 下载的两行、§6；§7 标 202 的三行：

- **客户接口**：C 的支付列表与详情的 `receipt_id`、`receipt_number`（有、无）；收据列表的分页、排序
  （`issued_at` 降序、`id` 降序）与 `total`、页码越界 422；下载的状态码、媒体类型、文件名、
  `Cache-Control` 与内容（以 `%PDF-` 开头、内容流里有收据号、与按快照渲染的字节相同）；不像 uuid
  的 id 与内部自增 id 404；快照被破坏（SQLite 上直接改行）500，错误信封与一条只带收据 `public_id`
  的 ERROR；
- **客户接口字段**：收据列表项恰好七个键、按顺序；快照、税务政策、内部 id 与手续费都不在响应里
  （INV-7）；
- **跨租户**：租户 A 的令牌下载 B 的收据 404、列表里没有 B 的收据；查询串里的租户参数不读（INV-8）。
  `DISABLED` / `CLOSED` 照常可读。

管理员与匿名调用者的拒绝由 test_customer_api.py 的路由枚举覆盖（两个接口已补进客户路由集合）。

世界用共用的 payment_world.py（SQLite 与真 MySQL 各跑一次；快照被破坏只在 SQLite 上造，MySQL 的
触发器拒绝改收据行）；收据经真实的 `issue_receipt` 开具（AIH-TASK-197），MySQL 上的清场沿用
payment_world 的 `clean_receipts`。本文件的夹具只在这里用，定义在这里。SQLite 用 `StaticPool`
（TestClient 在线程池里跑处理函数）。开具时刻是固定值，不 sleep；不调 Stripe。

MySQL 那部分需要 `BILLING_TEST_DATABASE_URL`，没设时 skip ——
**不要把 skipped 读成 passed**，CI 设了它并把 skipped 判成失败。
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import uuid
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from payment_world import (
    CREATED,
    TEST_EMAIL_DOMAIN,
    Customer,
    PaymentRef,
    World,
    _populate,
    build_mysql_world,
    insert_tax_version,
    make_paid_payment,
    seed_receipt_series,
)
from sqlalchemy import create_engine, delete, select, update
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from test_usage_billing import _SQLITE_LEDGER_TRIGGER

from app.api import receipt_download
from app.core.config import Settings
from app.core.database import create_session_factory
from app.core.tokens import issue_access_token
from app.main import create_app
from app.models.auth import User, UserRole, UserStatus
from app.models.base import Base
from app.models.payments import Payment
from app.models.receipts import PaymentReceipt
from app.models.tenancy import AccountStatus
from app.services.auth import utc_now
from app.services.receipt_pdf import render_receipt_pdf
from app.services.receipts import IssuedReceipt, issue_receipt

PAYMENTS = "/api/v1/customer/payments"
RECEIPTS = "/api/v1/customer/receipts"

PLACEHOLDER_SESSION = "00000000-0000-0000-0000-000000000000"
# 固定值而不是 utc_now()：断言「存进去的就是调用方给的那个时刻」要能逐字比对。
ISSUE_NOW = dt.datetime(2026, 11, 10, 6, 5, 9)
# world.customer() 的公司名；日志里不许出现。
COMPANY = "Payment Confirm Test Sdn Bhd"

# 设计 §2 的字段与顺序：断言「恰好等于」，按顺序比。
RECEIPT_KEYS = [
    "id",
    "receipt_number",
    "payment_id",
    "amount",
    "currency",
    "paid_at",
    "issued_at",
]
PAGE_KEYS = {"items", "page", "page_size", "total"}
ENVELOPE_KEYS = {"success", "data", "error", "request_id"}
RECEIPT_NUMBER = re.compile(r"ACV-AIR-2026-[0-9]{6}")
# 未压缩的内容流（render_receipt_pdf 关掉了压缩）。
PDF_STREAM = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.DOTALL)


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


def _receipt_world(source: Iterator[World]) -> Iterator[World]:
    built = next(source)
    try:
        # `create_all` 建的 SQLite 库没有系列行；MySQL 上迁移 0039 已写过（这里什么都不做）。
        seed_receipt_series(built.factory)
        yield built
    finally:
        _delete_portal_users(built.factory)
        # 走完生成器：MySQL 上由 payment_world 清场（先清收据、系列归零，再删支付）。
        for _ in source:
            pass


@pytest.fixture(params=["sqlite", "mysql"])
def world(request, monkeypatch) -> Iterator[World]:
    source = build_mysql_world(monkeypatch) if request.param == "mysql" else _sqlite_world()
    yield from _receipt_world(source)


@pytest.fixture
def sqlite_world() -> Iterator[World]:
    """只在 SQLite 上：MySQL 的触发器拒绝改收据行，破坏快照只能在这里造。"""
    yield from _receipt_world(_sqlite_world())


class Portal:
    """An app over the world's database, and the customers' signed-in headers."""

    def __init__(self, world: World, key_file: str) -> None:
        self.world = world
        self.app = create_app(Settings(jwt_secret_file=key_file, database_url=""))
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


def _key_file(tmp_path: Path) -> str:
    key = tmp_path / "jwt.key"
    key.write_text("a-test-signing-key-that-is-long-enough", encoding="utf-8")
    return str(key)


@pytest.fixture
def portal(world, tmp_path) -> Portal:
    return Portal(world, _key_file(tmp_path))


@pytest.fixture
def sqlite_portal(sqlite_world, tmp_path) -> Portal:
    return Portal(sqlite_world, _key_file(tmp_path))


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


def receipted(
    world: World,
    tenant: Customer,
    *,
    amount: str = "100.00",
    now: dt.datetime = ISSUE_NOW,
) -> tuple[PaymentRef, IssuedReceipt]:
    """A `PAID` payment with its `TOPUP` row and a receipt issued the way the credit paths do."""
    tax_version_id = insert_tax_version(world)
    payment = make_paid_payment(world, tenant, tax_version_id=tax_version_id, amount=amount)
    with world.factory() as session:
        row = session.execute(
            select(Payment).where(Payment.public_id == payment.public_id).with_for_update()
        ).scalar_one()
        issued = issue_receipt(
            session, payment=row, payment_method="fpx", now=now, send_email=False
        )
        session.commit()
    assert issued is not None and issued.created
    return payment, issued


def stored_snapshot(world: World, receipt_id: str) -> dict[str, Any]:
    with world.factory() as session:
        return session.execute(
            select(PaymentReceipt.snapshot_json).where(PaymentReceipt.public_id == receipt_id)
        ).scalar_one()


def internal_receipt_id(world: World, receipt_id: str) -> int:
    with world.factory() as session:
        return session.execute(
            select(PaymentReceipt.id).where(PaymentReceipt.public_id == receipt_id)
        ).scalar_one()


def listed_receipt_ids(portal: Portal, headers: dict[str, str], **params: Any) -> list[str]:
    data = data_of(portal.get(RECEIPTS, headers, **params))
    assert set(data) == PAGE_KEYS
    return [item["id"] for item in data["items"]]


def download(portal: Portal, headers: dict[str, str], receipt_id: str):
    return portal.get(f"{RECEIPTS}/{receipt_id}/download", headers)


def content_streams(body: bytes) -> list[bytes]:
    return PDF_STREAM.findall(body)


# --- 支付列表与详情的收据字段 ----------------------------------------------


def test_payments_carry_their_receipt_or_null(world, portal) -> None:
    tenant = world.customer()
    with_receipt, issued = receipted(world, tenant)
    # 已付而还没有收据（例如开具检查不过、等补发扫描）。
    without_receipt = make_paid_payment(world, tenant, tax_version_id=world.tax_version_id)
    pending = world.payment(tenant)
    headers = portal.headers(tenant)
    expected = {
        with_receipt.public_id: (issued.public_id, issued.receipt_number),
        without_receipt.public_id: (None, None),
        pending.public_id: (None, None),
    }

    listing = data_of(portal.get(PAYMENTS, headers))
    items = {item["id"]: item for item in listing["items"]}

    assert set(items) == set(expected)
    assert RECEIPT_NUMBER.fullmatch(issued.receipt_number)
    for public_id, receipt in expected.items():
        item = items[public_id]
        assert (item["receipt_id"], item["receipt_number"]) == receipt, public_id
        # 详情是同一组字段、同样的值。
        assert data_of(portal.get(f"{PAYMENTS}/{public_id}", headers)) == item
    # 其他字段不因收据而变。
    assert (items[with_receipt.public_id]["status"], items[with_receipt.public_id]["amount"]) == (
        "PAID",
        "100.00",
    )


# --- 收据列表 -----------------------------------------------------------


def test_the_receipt_list_items_and_their_fields(world, portal) -> None:
    tenant = world.customer()
    payment, issued = receipted(world, tenant, amount="1234.50")
    headers = portal.headers(tenant)

    data = data_of(portal.get(RECEIPTS, headers))

    assert set(data) == PAGE_KEYS
    assert (data["page"], data["page_size"], data["total"]) == (1, 20, 1)
    (item,) = data["items"]
    # 键的白名单：恰好七个、按顺序。
    assert list(item) == RECEIPT_KEYS
    assert item == {
        "id": issued.public_id,
        "receipt_number": issued.receipt_number,
        "payment_id": payment.public_id,
        # 两位小数的定点字符串。
        "amount": "1234.50",
        "currency": "MYR",
        "paid_at": CREATED.isoformat(),
        "issued_at": ISSUE_NOW.isoformat(),
    }


def test_the_receipt_list_hides_the_snapshot_and_internal_fields(world, portal) -> None:
    """INV-7：快照、税务说明、网关编号、内部 id 与租户都不出收据列表。"""
    tenant = world.customer()
    _, issued = receipted(world, tenant)
    snapshot = stored_snapshot(world, issued.public_id)
    headers = portal.headers(tenant)

    response = portal.get(RECEIPTS, headers)

    text = response.text
    for forbidden in (
        "snapshot",
        "tax_notice",
        "tax_policy_version_id",
        "series_year",
        "sequence",
        "tenant_id",
        COMPANY,
        snapshot["tax_notice"]["en"],
        snapshot["payment"]["gateway_reference"],
        tenant.public_id,
    ):
        assert forbidden not in text, forbidden


def test_receipts_are_newest_issued_first_then_by_descending_id_and_paged(world, portal) -> None:
    tenant = world.customer()
    oldest = receipted(world, tenant, now=ISSUE_NOW - dt.timedelta(days=1))[1]
    # 同一 `issued_at` 的三张按开具顺序（id 递增）建，列表里倒过来。
    same_moment = [receipted(world, tenant)[1] for _ in range(3)]
    newest = receipted(world, tenant, now=ISSUE_NOW + dt.timedelta(seconds=1))[1]
    headers = portal.headers(tenant)
    expected = [
        newest.public_id,
        *(receipt.public_id for receipt in reversed(same_moment)),
        oldest.public_id,
    ]

    assert listed_receipt_ids(portal, headers) == expected
    pages = [data_of(portal.get(RECEIPTS, headers, page=page, page_size=2)) for page in (1, 2, 3)]
    beyond = data_of(portal.get(RECEIPTS, headers, page=4, page_size=2))

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


def test_a_customer_without_receipts_gets_an_empty_page(world, portal) -> None:
    tenant = world.customer()
    world.payment(tenant)

    data = data_of(portal.get(RECEIPTS, portal.headers(tenant)))

    assert data == {"items": [], "page": 1, "page_size": 20, "total": 0}


@pytest.mark.parametrize(
    "params",
    [{"page": 0}, {"page": -1}, {"page_size": 0}, {"page_size": 101}, {"page": "x"}],
)
def test_receipt_page_bounds_are_422(world, portal, params) -> None:
    tenant = world.customer()

    response = portal.get(RECEIPTS, portal.headers(tenant), **params)

    assert error_of(response) == (422, "VALIDATION_ERROR")


def test_receipt_page_size_100_is_accepted(world, portal) -> None:
    tenant = world.customer()
    receipted(world, tenant)

    data = data_of(portal.get(RECEIPTS, portal.headers(tenant), page_size=100))

    assert (data["page_size"], data["total"], len(data["items"])) == (100, 1, 1)


# --- 下载 ---------------------------------------------------------------


def test_the_download_is_the_pdf_rendered_from_the_snapshot(world, portal) -> None:
    tenant = world.customer()
    _, issued = receipted(world, tenant)
    headers = portal.headers(tenant)

    response = download(portal, headers, issued.public_id)

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"] == (
        f'attachment; filename="{issued.receipt_number}.pdf"'
    )
    assert response.headers["cache-control"] == "no-store"
    body = response.content
    assert body.startswith(b"%PDF-")
    assert any(issued.receipt_number.encode("ascii") in stream for stream in content_streams(body))
    # 只来自快照：与按存下的快照渲染的字节完全相同；再下载一次也相同。
    assert body == render_receipt_pdf(stored_snapshot(world, issued.public_id))
    assert download(portal, headers, issued.public_id).content == body


def test_unknown_or_malformed_receipt_ids_are_404(world, portal) -> None:
    tenant = world.customer()
    payment, issued = receipted(world, tenant)
    headers = portal.headers(tenant)
    unknown = [
        str(uuid.uuid4()),
        "not-a-uuid",
        # 内部自增 id 不是对外的 id。
        str(internal_receipt_id(world, issued.public_id)),
        # 收据号与支付 id 也不是收据的 id。
        issued.receipt_number,
        payment.public_id,
        "x" * 300,
    ]

    for receipt_id in unknown:
        response = download(portal, headers, receipt_id)
        assert error_of(response) == (404, "RECEIPT_NOT_FOUND"), receipt_id
        assert response.headers["content-type"].startswith("application/json")
    assert download(portal, headers, issued.public_id).status_code == 200


@pytest.mark.parametrize(
    "corrupt",
    [
        lambda snapshot: {**snapshot, "schema_version": 2},
        lambda snapshot: {key: value for key, value in snapshot.items() if key != "tax_notice"},
        lambda snapshot: {**snapshot, "payment": {**snapshot["payment"], "amount": "0.00"}},
    ],
    ids=["schema-version", "missing-key", "zero-amount"],
)
def test_a_corrupted_snapshot_is_500_with_one_error_log(
    sqlite_world, sqlite_portal, caplog, corrupt
) -> None:
    """SQLite 上没有拒绝改收据行的触发器：直接改行造出不合规的快照。"""
    world, portal = sqlite_world, sqlite_portal
    tenant = world.customer()
    _, issued = receipted(world, tenant)
    snapshot = stored_snapshot(world, issued.public_id)
    with world.factory() as session:
        session.execute(
            update(PaymentReceipt)
            .where(PaymentReceipt.public_id == issued.public_id)
            .values(snapshot_json=corrupt(snapshot))
        )
        session.commit()
    headers = portal.headers(tenant)

    with caplog.at_level(logging.ERROR, logger=receipt_download.__name__):
        response = download(portal, headers, issued.public_id)

    assert error_of(response) == (500, "INTERNAL_ERROR")
    body = response.json()
    assert body["error"]["message"] == "An internal error occurred."
    assert body["request_id"]
    assert "content-disposition" not in response.headers
    errors = [
        record
        for record in caplog.records
        if record.name == receipt_download.__name__ and record.levelno == logging.ERROR
    ]
    assert len(errors) == 1
    # 只带收据 public_id：不带公司名、收据号、金额与快照内容（设计 §6）。
    baseline = logging.LogRecord("x", logging.ERROR, "x", 0, "x", None, None).__dict__
    extras = {key: value for key, value in errors[0].__dict__.items() if key not in baseline}
    assert extras.pop("receipt_id") == issued.public_id
    logged = errors[0].getMessage() + repr(extras)
    for forbidden in (COMPANY, issued.receipt_number, "100.00", snapshot["tax_notice"]["en"]):
        assert forbidden not in logged, forbidden


# --- 跨租户与只读账户 -----------------------------------------------------


def test_another_customers_receipt_is_404_and_not_listed(world, portal) -> None:
    """INV-8：租户只来自令牌主体；查询串里的租户参数不读。"""
    mine = world.customer()
    theirs = world.customer()
    _, my_receipt = receipted(world, mine)
    their_payment, their_receipt = receipted(world, theirs)
    headers = portal.headers(mine)

    plain = listed_receipt_ids(portal, headers)
    forged = listed_receipt_ids(
        portal, headers, tenant_id=str(theirs.id), customer_id=theirs.public_id
    )
    response = download(portal, headers, their_receipt.public_id)
    unknown = download(portal, headers, str(uuid.uuid4()))

    assert plain == forged == [my_receipt.public_id]
    assert error_of(response) == (404, "RECEIPT_NOT_FOUND")
    # 与不存在的收据不可区分：同一个错误码与文案。
    assert response.json()["error"] == unknown.json()["error"]
    assert their_receipt.receipt_number not in response.text
    # 对方的支付详情同样 404，不露出收据字段。
    detail = portal.get(f"{PAYMENTS}/{their_payment.public_id}", headers)
    assert error_of(detail) == (404, "PAYMENT_NOT_FOUND")
    # 对方自己照常下载得到。
    other = portal.headers(theirs)
    assert download(portal, other, their_receipt.public_id).status_code == 200
    assert listed_receipt_ids(portal, other) == [their_receipt.public_id]


@pytest.mark.parametrize("status", [AccountStatus.DISABLED, AccountStatus.CLOSED])
def test_read_only_accounts_still_list_and_download(world, portal, status) -> None:
    tenant = world.customer(account_status=status)
    _, issued = receipted(world, tenant)
    headers = portal.headers(tenant)

    assert listed_receipt_ids(portal, headers) == [issued.public_id]
    response = download(portal, headers, issued.public_id)
    assert response.status_code == 200, response.text
    assert response.content.startswith(b"%PDF-")


def test_amounts_are_decimal_strings(world, portal) -> None:
    """INV-10：收据金额与快照、账本行相同，两位小数的字符串。"""
    tenant = world.customer()
    _, issued = receipted(world, tenant, amount="0.10")

    (item,) = data_of(portal.get(RECEIPTS, portal.headers(tenant)))["items"]

    assert item["amount"] == "0.10"
    assert Decimal(item["amount"]) == Decimal(
        stored_snapshot(world, issued.public_id)["payment"]["amount"]
    )
