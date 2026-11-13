"""Admin receipts end to end: the download and the payments' receipt fields (AIH-TASK-203).

设计闸门 #589 v2 §2「管理端接口」、§5 下载的两行、§6「鉴权」；§7「管理端接口」一行：

- **鉴权**：匿名 401 `TOKEN_INVALID`、客户令牌（即使是收据所属租户的）403 `ADMIN_REQUIRED`，都不给
  出 PDF；路由枚举（test_admin_customers_api.py 的 `EXPECTED_ADMIN_ROUTES`）另外覆盖；
- **字段**：B 的支付列表项在 B 定的字段之后多 `receipt_number`，详情在 `events` 之前依次多
  `receipt_id`、`receipt_number`、`receipt_issued_at`（有收据、已付而没有收据、未付三种）；
- **下载**：200 `application/pdf` 附件（文件名、`Cache-Control`、与按快照渲染的字节相同），不按租户
  过滤（任一租户的收据都下载得到）；不存在、不像 uuid、内部自增 id、收据号与支付 id 都是 404
  `RECEIPT_NOT_FOUND`；快照被破坏（SQLite 上直接改行）500 `INTERNAL_ERROR`，与客户的下载接口相同。

世界用共用的 payment_world.py（SQLite 与真 MySQL 各跑一次；快照被破坏只在 SQLite 上造，MySQL 的
触发器拒绝改收据行）；收据经真实的 `issue_receipt` 开具（AIH-TASK-197），MySQL 上的清场沿用
payment_world 的 `clean_receipts`（先清收据、系列归零，再删支付）。本文件的夹具只在这里用，定义在
这里。SQLite 用 `StaticPool`（TestClient 在线程池里跑处理函数）。开具时刻是固定值，不 sleep；
不调 Stripe。

MySQL 那部分需要 `BILLING_TEST_DATABASE_URL`，没设时 skip ——
**不要把 skipped 读成 passed**，CI 设了它并把 skipped 判成失败。
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import uuid
from collections.abc import Iterator
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
from app.services.auth import utc_now
from app.services.receipt_pdf import render_receipt_pdf
from app.services.receipts import IssuedReceipt, issue_receipt

PAYMENTS = "/api/v1/admin/payments"
RECEIPTS = "/api/v1/admin/receipts"

PLACEHOLDER_SESSION = "00000000-0000-0000-0000-000000000000"
# 固定值而不是 utc_now()：断言「存进去的就是调用方给的那个时刻」要能逐字比对。
ISSUE_NOW = dt.datetime(2026, 11, 10, 6, 5, 9)
# world.customer() 的公司名；日志里不许出现。
COMPANY = "Payment Confirm Test Sdn Bhd"

RECEIPT_KEYS = ["receipt_id", "receipt_number", "receipt_issued_at"]
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


class Console:
    """An app over the world's database, an admin's headers and a portal user's."""

    def __init__(self, world: World, key_file: str) -> None:
        self.world = world
        self.app = create_app(Settings(jwt_secret_file=key_file, database_url=""))
        self.app.state.session_factory = world.factory
        # 500 要以统一错误体返回，而不是在测试里抛出来。
        self.client = TestClient(self.app, raise_server_exceptions=False)
        # ADMIN 的令牌只在过了 2FA 之后才签发；那条路径在别处测。
        token = issue_access_token(
            self.app.state.settings,
            user_id=world.admin_id,
            role=UserRole.ADMIN.value,
            session_id=uuid.uuid4().hex,
            now=utc_now(),
        )
        self.admin = {"Authorization": f"Bearer {token}"}

    def customer_headers(self, tenant: Customer) -> dict[str, str]:
        """A portal user of `tenant` and its access token."""
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

    def get(self, path: str, headers: dict[str, str] | None = None):
        return self.client.get(path, headers=self.admin if headers is None else headers)

    def download(self, receipt_id: str, headers: dict[str, str] | None = None):
        return self.get(f"{RECEIPTS}/{receipt_id}/download", headers)


def _key_file(tmp_path: Path) -> str:
    key = tmp_path / "jwt.key"
    key.write_text("a-test-signing-key-that-is-long-enough", encoding="utf-8")
    return str(key)


@pytest.fixture
def console(world, tmp_path) -> Console:
    return Console(world, _key_file(tmp_path))


@pytest.fixture
def sqlite_console(sqlite_world, tmp_path) -> Console:
    return Console(sqlite_world, _key_file(tmp_path))


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
    world: World, tenant: Customer, *, now: dt.datetime = ISSUE_NOW
) -> tuple[PaymentRef, IssuedReceipt]:
    """A `PAID` payment with its `TOPUP` row and a receipt issued the way the credit paths do."""
    tax_version_id = insert_tax_version(world)
    payment = make_paid_payment(world, tenant, tax_version_id=tax_version_id)
    with world.factory() as session:
        row = session.execute(
            select(Payment).where(Payment.public_id == payment.public_id).with_for_update()
        ).scalar_one()
        issued = issue_receipt(
            session, payment=row, payment_method="card", now=now, send_email=False
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


# --- 鉴权 ---------------------------------------------------------------


def test_anonymous_and_customer_callers_are_refused(world, console) -> None:
    tenant = world.customer()
    _, issued = receipted(world, tenant)
    # 收据所属租户自己的门户令牌也不行：管理端接口只认 ADMIN。
    owner = console.customer_headers(tenant)

    anonymous = console.download(issued.public_id, headers={})
    customer = console.download(issued.public_id, headers=owner)

    assert error_of(anonymous) == (401, "TOKEN_INVALID")
    assert error_of(customer) == (403, "ADMIN_REQUIRED")
    for response in (anonymous, customer):
        assert response.headers["content-type"].startswith("application/json")
        assert "content-disposition" not in response.headers
        assert not response.content.startswith(b"%PDF-")
        assert issued.receipt_number not in response.text
    # 同一张收据，管理员下载得到。
    assert console.download(issued.public_id).status_code == 200


# --- 支付列表与详情的收据字段 ----------------------------------------------


def test_payments_carry_their_receipt_or_null(world, console) -> None:
    tenant = world.customer()
    with_receipt, issued = receipted(world, tenant)
    # 已付而还没有收据（例如开具检查不过、等补发扫描）。
    without_receipt = make_paid_payment(world, tenant, tax_version_id=world.tax_version_id)
    pending = world.payment(tenant)
    expected = {
        with_receipt.public_id: [issued.public_id, issued.receipt_number, ISSUE_NOW.isoformat()],
        without_receipt.public_id: [None, None, None],
        pending.public_id: [None, None, None],
    }

    listing = data_of(console.get(PAYMENTS))
    items = {item["id"]: item for item in listing["items"]}

    assert set(items) == set(expected)
    assert RECEIPT_NUMBER.fullmatch(issued.receipt_number)
    for public_id, receipt in expected.items():
        item = items[public_id]
        # 列表项：收据号是最后一个键。
        assert list(item)[-1] == "receipt_number"
        assert item["receipt_number"] == receipt[1], public_id
        detail = data_of(console.get(f"{PAYMENTS}/{public_id}"))
        # 详情：三个收据字段依次排在 `events` 之前。
        assert list(detail)[-4:] == [*RECEIPT_KEYS, "events"]
        assert [detail[key] for key in RECEIPT_KEYS] == receipt, public_id
        # 列表项的其他字段在详情里取值相同。
        assert {key: detail[key] for key in item} == item
    # 支付的状态与金额不因收据而变。
    assert (items[with_receipt.public_id]["status"], items[with_receipt.public_id]["amount"]) == (
        "PAID",
        "100.00000000",
    )


# --- 下载 ---------------------------------------------------------------


def test_the_download_is_the_pdf_rendered_from_the_snapshot(world, console) -> None:
    tenant = world.customer()
    _, issued = receipted(world, tenant)

    response = console.download(issued.public_id)

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"] == (
        f'attachment; filename="{issued.receipt_number}.pdf"'
    )
    assert response.headers["cache-control"] == "no-store"
    body = response.content
    assert body.startswith(b"%PDF-")
    streams = PDF_STREAM.findall(body)
    assert any(issued.receipt_number.encode("ascii") in stream for stream in streams)
    # 只来自快照：与按存下的快照渲染的字节完全相同；再下载一次也相同。
    assert body == render_receipt_pdf(stored_snapshot(world, issued.public_id))
    assert console.download(issued.public_id).content == body


def test_the_download_is_not_filtered_by_tenant(world, console) -> None:
    first, second = world.customer(), world.customer()
    _, first_receipt = receipted(world, first)
    _, second_receipt = receipted(world, second)

    for issued in (first_receipt, second_receipt):
        response = console.download(issued.public_id)
        assert response.status_code == 200, response.text
        assert response.headers["content-disposition"] == (
            f'attachment; filename="{issued.receipt_number}.pdf"'
        )
    assert first_receipt.receipt_number != second_receipt.receipt_number


def test_unknown_or_malformed_receipt_ids_are_404(world, console) -> None:
    tenant = world.customer()
    payment, issued = receipted(world, tenant)
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
        response = console.download(receipt_id)
        assert error_of(response) == (404, "RECEIPT_NOT_FOUND"), receipt_id
        assert response.headers["content-type"].startswith("application/json")
    assert console.download(issued.public_id).status_code == 200


def test_a_corrupted_snapshot_is_500_with_one_error_log(
    sqlite_world, sqlite_console, caplog
) -> None:
    """SQLite 上没有拒绝改收据行的触发器：直接改行造出不合规的快照。"""
    world, console = sqlite_world, sqlite_console
    tenant = world.customer()
    _, issued = receipted(world, tenant)
    snapshot = stored_snapshot(world, issued.public_id)
    with world.factory() as session:
        session.execute(
            update(PaymentReceipt)
            .where(PaymentReceipt.public_id == issued.public_id)
            .values(snapshot_json={**snapshot, "schema_version": 2})
        )
        session.commit()

    with caplog.at_level(logging.ERROR, logger=receipt_download.__name__):
        response = console.download(issued.public_id)

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
    assert getattr(errors[0], "receipt_id", None) == issued.public_id
    for forbidden in (COMPANY, issued.receipt_number, snapshot["tax_notice"]["en"]):
        assert forbidden not in errors[0].getMessage(), forbidden
