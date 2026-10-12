"""Project integration status and status webhook address (design gate #241 v6 §2, §4, §6, §7).

覆盖设计 §7 的这几行，以及 §2「URL 规范化与校验」「事务边界」、§4 的跃迁表：

- **URL 规范化**：纯函数，不连库；接受的四类与各种 422（含 IP 字面量的简写、拆分前的空白与
  控制字符、主机名的 LDH 字符集、路径与查询串的字符集、`@` / `#` / 空端口、长度上限）；
- **跃迁表每一行**：版本 +1、`status_changed_at`、恰好一条集成状态事件、
  审计字段、投递退避作废；相同目标不写；租户 `CLOSED` 时任何目标都是 409（「CLOSED 优先」）；
- **同一地址 / 清除从未设过的地址 / 两次清除**：200 且什么都不写；
- **改地址清空游标与租约**；设为地址而 `CLOSED` 时 409，清除不受限；判定顺序 422 → 404 → 409；
- **回滚**：审计或 outbox 写入失败时整体不变，SQLite 与 MySQL 各一次；
- **加锁顺序**：租户 → 项目 → 投递行（改地址时租户是共享锁），语句顺序在两种库上各看一次；
  MySQL 上未提交的 `bump_status_version` 挡住两条路径，并与记账、账户状态并发而不死锁；
- **日志与审计里没有完整地址**。

MySQL 那部分需要 `BILLING_TEST_DATABASE_URL` 指向一个**可以被清空的**库，没设时 skip ——
**不要把 skipped 读成 passed**，CI 设了它并把 skipped 判成失败。
"""

from __future__ import annotations

import datetime as dt
import inspect
import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, delete, event, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.core.database import create_session_factory
from app.models.auth import (
    AuditAction,
    AuditLog,
    DomainOutbox,
    OutboxStatus,
    User,
    UserRole,
    UserStatus,
)
from app.models.base import Base
from app.models.integration import ProjectStatusDelivery
from app.models.tenancy import (
    AccountStatus,
    BillingStatus,
    IntegrationStatus,
    Project,
    Tenant,
)
from app.models.wallet import TransactionType, Wallet
from app.repositories import tenancy
from app.repositories.tenancy import bump_status_version
from app.repositories.wallet import EVENT_BILLING_STATUS_CHANGED, create_wallet
from app.services import project_integration
from app.services.account_status import EVENT_ACCOUNT_STATUS_CHANGED, change_account_status
from app.services.auth import RequestContext
from app.services.customers import CustomerNotFound, add_status_delivery
from app.services.integration_access import ProjectNotFound
from app.services.project_integration import (
    EVENT_INTEGRATION_STATUS_CHANGED,
    AccountClosed,
    IntegrationReasonInvalid,
    StatusWebhookUrlInvalid,
    StatusWebhookView,
    change_integration_status,
    get_status_webhook,
    normalize_status_webhook_url,
    set_status_webhook,
)
from app.services.wallet_adjustments import post_adjustment

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")
TEST_EMAIL_DOMAIN = "@project-integration-test.example.com"
COMPANY = "Project Integration Test Sdn Bhd"

NOW = dt.datetime(2026, 10, 12, 9, 0, 0)
LATER = NOW + dt.timedelta(minutes=5)
FUTURE = NOW + dt.timedelta(hours=1)
CONTEXT = RequestContext(ip_address="203.0.113.49", user_agent="project-integration-test")
REASON = "Integration paused for maintenance"
TEST_EVENT = "test.status_bumped"

# 路径里带一段像令牌的串：它不许出现在审计、日志与异常里（设计 §6）。全零占位值。
PATH_TOKEN = "tok_" + "0" * 32
HOOK = f"https://hooks.example.com/acuven/{PATH_TOKEN}?v=1"
HOOK_HOST = "hooks.example.com"
OTHER_HOOK = "https://status.example.net/receive"

ENABLED = IntegrationStatus.ENABLED
DISABLED = IntegrationStatus.DISABLED


# --- 夹具 -----------------------------------------------------------------------


@pytest.fixture(params=["sqlite", "mysql"])
def factory(request, monkeypatch) -> Iterator[sessionmaker[Session]]:
    if request.param == "mysql":
        yield from _mysql_factory(monkeypatch)
        return
    yield from _sqlite_factory()


@pytest.fixture
def sqlite_factory() -> Iterator[sessionmaker[Session]]:
    yield from _sqlite_factory()


@pytest.fixture
def mysql_factory(monkeypatch) -> Iterator[sessionmaker[Session]]:
    yield from _mysql_factory(monkeypatch)


def _sqlite_factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


def _mysql_factory(monkeypatch) -> Iterator[sessionmaker[Session]]:
    if not TEST_DATABASE_URL:
        pytest.skip("BILLING_TEST_DATABASE_URL is not set; the MySQL half needs a real MySQL")
    # 建表走 alembic：列与 CHECK 以迁移建出来的为准，钱包触发器只有迁移会建。
    from app.core.config import get_settings

    monkeypatch.setenv("BILLING_DATABASE_URL", TEST_DATABASE_URL)
    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")
    get_settings.cache_clear()

    engine = create_engine(TEST_DATABASE_URL, pool_size=10, max_overflow=4)
    _clean(engine)
    try:
        yield create_session_factory(engine)
    finally:
        _clean(engine)
        engine.dispose()


def _clean(engine: Engine) -> None:
    """⚠️ 库是共享的，每个用例前后都清场（与 test_status_version.py 同一做法）。"""
    ours = select(Tenant.id).where(Tenant.company_name == COMPANY)
    our_tenants = select(Tenant.public_id).where(Tenant.company_name == COMPANY)
    our_projects = select(Project.public_id).where(Project.tenant_id.in_(ours))
    with engine.begin() as connection:
        # 账本拒绝 DELETE，只能 TRUNCATE；0018 起 usage_events 有外键引用账本。
        connection.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        try:
            connection.execute(text("TRUNCATE TABLE wallet_transactions"))
        finally:
            connection.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
        # 审计表同理：0010 的触发器拒绝 DELETE，TRUNCATE 是 DDL、不经触发器。
        connection.execute(text("TRUNCATE TABLE audit_logs"))
    with engine.begin() as connection:
        connection.execute(
            delete(DomainOutbox).where(
                or_(
                    DomainOutbox.aggregate_id.in_(our_tenants),
                    DomainOutbox.aggregate_id.in_(our_projects),
                )
            )
        )
        # 投递行引用项目（RESTRICT），先删它。
        connection.execute(
            delete(ProjectStatusDelivery).where(ProjectStatusDelivery.tenant_id.in_(ours))
        )
        connection.execute(delete(Project).where(Project.tenant_id.in_(ours)))
        connection.execute(delete(Wallet).where(Wallet.tenant_id.in_(ours)))
        connection.execute(delete(Tenant).where(Tenant.company_name == COMPANY))
        connection.execute(delete(User).where(User.email.like(f"%{TEST_EMAIL_DOMAIN}")))


# --- 帮手 -----------------------------------------------------------------------


def make_admin(factory) -> User:
    """A committed ADMIN, detached with its columns loaded — what `require_admin` returns."""
    with factory() as session:
        user = User(
            email=f"{uuid.uuid4().hex}{TEST_EMAIL_DOMAIN}",
            password_hash="not-a-real-hash",
            role=UserRole.ADMIN,
            status=UserStatus.ACTIVE,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(user)
        session.commit()
        return user


def make_customer(factory) -> tuple[int, str]:
    """A tenant with an empty wallet: ENABLED, SUSPENDED, PREPAID, version 0."""
    with factory() as session:
        tenant = tenancy.create_tenant(
            session, company_name=COMPANY, email="ops@example.com", now=NOW
        )
        create_wallet(session, tenant_id=tenant.id, now=NOW)
        session.commit()
        return int(tenant.id), tenant.public_id


def make_project(factory, tenant_id: int) -> tuple[int, str]:
    """A project and its one delivery row, as `create_project` makes them."""
    with factory() as session:
        project = tenancy.create_project(session, tenant_id=tenant_id, name="Chatbot", now=NOW)
        add_status_delivery(session, project, now=NOW)
        session.commit()
        return int(project.id), project.public_id


class Scene:
    """One admin, one customer with one project and its delivery row."""

    def __init__(self, factory) -> None:
        self.factory = factory
        self.admin = make_admin(factory)
        self.tenant_id, self.customer_id = make_customer(factory)
        self.project_pk, self.project_id = make_project(factory, self.tenant_id)

    def change(
        self,
        target: IntegrationStatus,
        *,
        reason: str = REASON,
        now: dt.datetime = NOW,
        customer_id: str | None = None,
        project_id: str | None = None,
    ):
        return change_integration_status(
            self.factory,
            actor=self.admin,
            customer_id=customer_id or self.customer_id,
            project_id=project_id or self.project_id,
            integration_status=target,
            reason=reason,
            context=CONTEXT,
            now=now,
        )

    def set_url(
        self,
        url: str | None,
        *,
        now: dt.datetime = NOW,
        customer_id: str | None = None,
        project_id: str | None = None,
    ) -> StatusWebhookView:
        return set_status_webhook(
            self.factory,
            actor=self.admin,
            customer_id=customer_id or self.customer_id,
            project_id=project_id or self.project_id,
            url=url,
            context=CONTEXT,
            now=now,
        )

    def get_url(self, *, customer_id: str | None = None, project_id: str | None = None):
        return get_status_webhook(
            self.factory,
            customer_id=customer_id or self.customer_id,
            project_id=project_id or self.project_id,
        )

    def set_account_status(self, status: AccountStatus) -> None:
        """Direct db write: no service produces CLOSED yet (design §7「CLOSED 优先」)."""
        with self.factory() as session:
            statement = update(Tenant).where(Tenant.id == self.tenant_id)
            session.execute(statement.values(account_status=status))
            session.commit()

    def set_billing_status(self, status: BillingStatus) -> None:
        with self.factory() as session:
            statement = update(Tenant).where(Tenant.id == self.tenant_id)
            session.execute(statement.values(billing_status=status))
            session.commit()

    def set_integration_status(self, status: IntegrationStatus) -> None:
        with self.factory() as session:
            statement = update(Project).where(Project.id == self.project_pk)
            session.execute(statement.values(integration_status=status))
            session.commit()

    def dirty_delivery(self) -> None:
        """A delivery row mid-flight: delivered, leased, failing, backed off."""
        with self.factory() as session:
            session.execute(
                update(ProjectStatusDelivery)
                .where(ProjectStatusDelivery.project_id == self.project_pk)
                .values(
                    delivered_version=5,
                    attempted_version=6,
                    attempt_count=3,
                    next_attempt_at=FUTURE,
                    lease_token=str(uuid.uuid4()),
                    lease_expires_at=FUTURE,
                    last_attempt_at=NOW,
                    last_http_status=500,
                    last_error_code="HTTP_STATUS",
                )
            )
            session.commit()

    def delivery(self) -> dict[str, object]:
        return delivery_state(self.factory, self.project_pk)


DELIVERY_COLUMNS = (
    "delivered_version",
    "attempted_version",
    "attempt_count",
    "next_attempt_at",
    "lease_token",
    "lease_expires_at",
    "last_attempt_at",
    "delivered_at",
    "last_http_status",
    "last_error_code",
)


def delivery_state(factory, project_pk: int) -> dict[str, object]:
    with factory() as session:
        row = session.get(ProjectStatusDelivery, project_pk)
        assert row is not None
        return {name: getattr(row, name) for name in DELIVERY_COLUMNS}


def audits(factory, action: AuditAction, entity_id: str) -> list[AuditLog]:
    statement = (
        select(AuditLog)
        .where(AuditLog.action == action, AuditLog.entity_id == entity_id)
        .order_by(AuditLog.id)
    )
    with factory() as session:
        return list(session.execute(statement).scalars())


def events(factory, event_type: str, aggregate_id: str) -> list[DomainOutbox]:
    statement = (
        select(DomainOutbox)
        .where(DomainOutbox.event_type == event_type, DomainOutbox.aggregate_id == aggregate_id)
        .order_by(DomainOutbox.id)
    )
    with factory() as session:
        return list(session.execute(statement).scalars())


def snapshot(scene: Scene) -> dict[str, object]:
    """Everything either path may write, and the wallet neither may touch, as committed."""
    factory = scene.factory
    ids = (scene.customer_id, scene.project_id)
    with factory() as session:
        tenant = session.get(Tenant, scene.tenant_id)
        project = session.get(Project, scene.project_pk)
        wallet = session.execute(
            select(Wallet).where(Wallet.tenant_id == scene.tenant_id)
        ).scalar_one()
        assert tenant is not None and project is not None
        audit_count = select(func.count()).select_from(AuditLog)
        outbox_count = select(func.count()).select_from(DomainOutbox)
        return {
            "tenant": (
                tenant.account_status,
                tenant.billing_status,
                tenant.status_version,
                tenant.status_changed_at,
                tenant.updated_at,
            ),
            "project": (
                project.integration_status,
                project.status_webhook_url,
                project.updated_at,
            ),
            "delivery": delivery_state(factory, scene.project_pk),
            "wallet": (wallet.balance, wallet.version),
            "audits": session.execute(audit_count.where(AuditLog.entity_id.in_(ids))).scalar_one(),
            "outbox": session.execute(
                outbox_count.where(DomainOutbox.aggregate_id.in_(ids))
            ).scalar_one(),
        }


def bumped_event(tenant: Tenant) -> DomainOutbox:
    return DomainOutbox(
        event_type=TEST_EVENT,
        aggregate_type="tenant",
        aggregate_id=tenant.public_id,
        payload_json=json.dumps({"status_version": tenant.status_version}),
        status=OutboxStatus.PENDING,
        attempt_count=0,
        next_retry_at=NOW,
        created_at=NOW,
    )


def break_the_audit(monkeypatch) -> None:
    """The audit row fails at flush: `created_at` is NULL (NOT NULL → IntegrityError)."""
    real = project_integration.record_audit

    def broken(session: Session, **options: object) -> None:
        real(session, **{**options, "now": None})  # type: ignore[arg-type]

    monkeypatch.setattr(project_integration, "record_audit", broken)


def break_the_outbox(monkeypatch) -> None:
    """The outbox row fails at flush: `created_at` is NULL (NOT NULL → IntegrityError)."""
    real = project_integration._integration_event

    def broken(project: Project, payload: dict[str, object], now: dt.datetime) -> DomainOutbox:
        row = real(project, payload, now)
        row.created_at = None  # type: ignore[assignment]
        return row

    monkeypatch.setattr(project_integration, "_integration_event", broken)


def capture(factory, action) -> list[str]:
    engine = factory.kw["bind"]
    statements: list[str] = []

    def record(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        statements.append(" ".join(statement.split()).upper())

    event.listen(engine, "before_cursor_execute", record)
    try:
        action()
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert statements, "没有抓到任何语句，下面的断言在空列表上空转"
    return statements


def first_index(statements: list[str], predicate) -> int:
    for index, statement in enumerate(statements):
        if predicate(statement):
            return index
    raise AssertionError("no such statement")


def is_tenant_read(statement: str) -> bool:
    return statement.startswith("SELECT") and "FROM TENANTS" in statement


def is_project_lock(statement: str) -> bool:
    return (
        statement.startswith("SELECT")
        and "FROM PROJECTS" in statement
        and "PROJECTS.PUBLIC_ID =" in statement
    )


def is_delivery_update(statement: str) -> bool:
    return statement.startswith("UPDATE PROJECT_STATUS_DELIVERIES")


# --- URL 规范化：纯函数 --------------------------------------------------------------


LONG_HOST = ".".join(["a" * 63] * 3 + ["b" * 61])
BASE = "https://example.com/"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # 大写 scheme 与主机名；路径大小写保留。
        ("HTTPS://Hooks.Example.COM/Acuven", "https://hooks.example.com/Acuven"),
        # 末尾一个点。
        ("https://hooks.example.com./x", "https://hooks.example.com/x"),
        # IDN 主机名 → IDNA。
        ("https://Bücher.example/hook", "https://xn--bcher-kva.example/hook"),
        ("https://xn--bcher-kva.example/hook", "https://xn--bcher-kva.example/hook"),
        # 端口 443 省略。
        ("https://hooks.example.com:443/x", "https://hooks.example.com/x"),
        ("https://hooks.example.com.:443/x", "https://hooks.example.com/x"),
        # 空路径取 `/`；空查询串（含只有一个 `?`）去掉。
        ("https://hooks.example.com", "https://hooks.example.com/"),
        ("https://hooks.example.com/x?", "https://hooks.example.com/x"),
        ("https://hooks.example.com?", "https://hooks.example.com/"),
        # 百分号编码与查询串按原样保留，不做规范化。
        ("https://h.example/a%2fB?x=%41&y=1", "https://h.example/a%2fB?x=%41&y=1"),
        # 合法的 LDH 主机名：数字开头、中间带连字符。
        ("https://1e100.example/x", "https://1e100.example/x"),
        ("https://a-b.example/x", "https://a-b.example/x"),
        # 主机名恰好 253、总长恰好 2048。
        (f"https://{LONG_HOST}/x", f"https://{LONG_HOST}/x"),
        (BASE + "a" * 2028, BASE + "a" * 2028),
        (HOOK, HOOK),
    ],
)
def test_accepted_addresses_are_normalized(raw: str, expected: str) -> None:
    assert normalize_status_webhook_url(raw) == expected
    # 规范化是幂等的：存下来的值再规范化一次不变。
    assert normalize_status_webhook_url(expected) == expected


REJECTED = [
    # IP 字面量与各种简写。
    "https://127.1/x",
    "https://2130706433/x",
    "https://0x7f.1/x",
    "https://0X7F.1/x",
    "https://0x7f000001/x",
    "https://10.0.0.1/x",
    "https://127.0.0.1./x",
    "https://1.2.3.4.5/x",
    "https://[::1]/x",
    "https://[::1]:443/x",
    "https://[fe80::1/x",
    # scheme、端口。
    "http://hooks.example.com/x",
    "ftp://hooks.example.com/x",
    "hooks.example.com/x",
    "https:hooks.example.com/x",
    "https://hooks.example.com:8443/x",
    "https://hooks.example.com:80/x",
    "https://hooks.example.com:/x",
    "https://h:/x",
    "https://hooks.example.com:443:443/x",
    # 用户信息与 fragment（含空的）。
    "https://user:pw@hooks.example.com/x",
    "https://@hooks.example.com/x",
    "https://hooks.example.com/x?to=a@b",
    "https://hooks.example.com/x#frag",
    "https://hooks.example.com/x#",
    # 主机名为空或有空段。
    "https:///x",
    "https://./x",
    "https://:443/x",
    "https://a..b.example/x",
    "https://hooks.example.com../x",
    # 主机名的 LDH 字符集。
    "https://ex_ample.com/x",
    "https://ex%41mple.com/x",
    "https://ex<ample.com/x",
    "https://-bad.example/x",
    "https://bad-.example/x",
    "https://" + "a" * 64 + ".example/x",
    "https://" + LONG_HOST + "b/x",
    # 拆分之前的空白与控制字符（`urlsplit` 会悄悄去掉制表符与换行）。
    " https://hooks.example.com/x",
    "https://hooks.example.com/x ",
    "https://hooks.exa mple.com/x",
    "https://hooks.example.com/a\tb",
    "https://hooks.exam\nple.com/x",
    "https://hooks.example.com/x\r",
    "https://hooks.example.com/\x00",
    "https://hooks.example.com/\x1f",
    "https://hooks.example.com/\x7f",
    # 路径与查询串只允许可打印 ASCII。
    "https://hooks.example.com/café",
    "https://hooks.example.com/x?q=é",
    # 超长。
    BASE + "a" * 2029,
    "",
]


@pytest.mark.parametrize("raw", REJECTED)
def test_rejected_addresses_are_422(raw: str) -> None:
    with pytest.raises(StatusWebhookUrlInvalid) as raised:
        normalize_status_webhook_url(raw)
    assert (raised.value.code, raised.value.http_status) == ("VALIDATION_ERROR", 422)


def test_the_error_never_echoes_the_address() -> None:
    """设计 §6：异常信息不带 URL，路径里的令牌一个字符都不出现。"""
    bad = f"https://hooks.example.com:8443/acuven/{PATH_TOKEN}"
    with pytest.raises(StatusWebhookUrlInvalid) as raised:
        normalize_status_webhook_url(bad)
    for rendered in (str(raised.value), repr(raised.value), raised.value.message):
        assert PATH_TOKEN not in rendered
        assert "hooks.example.com" not in rendered


# --- 跃迁表：每一行 ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "target", "effective", "reason_code"),
    [
        # 默认租户：ENABLED、SUSPENDED（余额 0）、PREPAID。
        (ENABLED, DISABLED, "BLOCK_AI", "PROJECT_INTEGRATION_DISABLED"),
        (DISABLED, ENABLED, "BLOCK_AI", "BALANCE_NOT_POSITIVE"),
    ],
)
def test_each_transition_bumps_once_and_writes_one_event(
    factory, current, target, effective, reason_code
) -> None:
    """设计 §4 前两行：版本 +1、`status_changed_at`、恰好一条事件、审计；退避作废。"""
    scene = Scene(factory)
    if current is DISABLED:
        scene.set_integration_status(DISABLED)
    scene.dirty_delivery()
    before = snapshot(scene)

    view = scene.change(target, now=LATER)

    assert (view.id, view.updated_at) == (scene.project_id, LATER)
    after = snapshot(scene)
    assert after["tenant"] == (
        AccountStatus.ENABLED,
        BillingStatus.SUSPENDED,
        1,
        LATER,
        NOW,
    )
    assert after["project"] == (target, None, LATER)
    assert after["wallet"] == before["wallet"]
    # bump 只把晚于 now 的退避提前到 now，投递行的其余列不动。
    assert after["delivery"] == {**before["delivery"], "next_attempt_at": LATER}

    [row] = events(factory, EVENT_INTEGRATION_STATUS_CHANGED, scene.project_id)
    assert (row.aggregate_type, row.aggregate_id) == ("project", scene.project_id)
    assert (row.status, row.attempt_count) == (OutboxStatus.PENDING, 0)
    assert (row.created_at, row.next_retry_at, row.processed_at) == (LATER, LATER, None)
    assert json.loads(row.payload_json or "{}") == {
        "project_id": scene.project_id,
        "integration_status": target.value,
        "previous_integration_status": current.value,
        "effective_status": effective,
        "reason_code": reason_code,
        "status_version": 1,
        "changed_at": "2026-10-12T09:05:00",
    }
    assert REASON not in (row.payload_json or "")
    # 一次跃迁恰好一条 outbox：租户聚合上一条都没有。
    assert after["outbox"] == 1

    [audit] = audits(factory, AuditAction.PROJECT_INTEGRATION_STATUS_CHANGED, scene.project_id)
    assert (audit.actor_user_id, audit.actor_role) == (scene.admin.id, "ADMIN")
    assert (audit.entity_type, audit.entity_id) == ("project", scene.project_id)
    assert (audit.ip_address, audit.user_agent) == (CONTEXT.ip_address, CONTEXT.user_agent)
    assert (audit.reason, audit.created_at) == (REASON, LATER)
    assert json.loads(audit.before_state or "{}") == {
        "integration_status": current.value,
        "status_version": 0,
    }
    assert json.loads(audit.after_state or "{}") == {
        "integration_status": target.value,
        "status_version": 1,
    }


def test_reenabling_with_a_positive_balance_allows_ai(sqlite_factory) -> None:
    scene = Scene(sqlite_factory)
    scene.set_integration_status(DISABLED)
    scene.set_billing_status(BillingStatus.ACTIVE)

    scene.change(ENABLED)

    [row] = events(sqlite_factory, EVENT_INTEGRATION_STATUS_CHANGED, scene.project_id)
    payload = json.loads(row.payload_json or "{}")
    assert (payload["effective_status"], payload["reason_code"]) == ("ALLOW_AI", None)


def test_a_round_trip_counts_up_twice(sqlite_factory) -> None:
    scene = Scene(sqlite_factory)

    scene.change(DISABLED)
    scene.change(ENABLED, now=LATER)

    first, second = events(sqlite_factory, EVENT_INTEGRATION_STATUS_CHANGED, scene.project_id)
    versions = [json.loads(row.payload_json or "{}")["status_version"] for row in (first, second)]
    assert versions == [1, 2]
    assert snapshot(scene)["tenant"][2:4] == (2, LATER)


@pytest.mark.parametrize("status", [ENABLED, DISABLED])
def test_the_same_target_writes_nothing(sqlite_factory, status) -> None:
    """设计 §4 第四行：200，不加版本、不写 outbox 与审计、不碰投递行。"""
    scene = Scene(sqlite_factory)
    scene.set_integration_status(status)
    scene.dirty_delivery()
    before = snapshot(scene)

    view = scene.change(status, now=LATER, reason="Another reason")

    assert (view.id, view.updated_at) == (scene.project_id, NOW)
    assert snapshot(scene) == before


@pytest.mark.parametrize(
    ("current", "target"),
    [(ENABLED, DISABLED), (DISABLED, ENABLED), (ENABLED, ENABLED), (DISABLED, DISABLED)],
)
def test_closed_comes_first_even_for_the_same_target(sqlite_factory, current, target) -> None:
    """设计 §7「CLOSED 优先」与 §4 第三行：任意 → 任意（含相同）都是 409，不写。"""
    scene = Scene(sqlite_factory)
    scene.set_integration_status(current)
    scene.set_account_status(AccountStatus.CLOSED)
    scene.dirty_delivery()
    before = snapshot(scene)

    with pytest.raises(AccountClosed) as raised:
        scene.change(target, now=LATER)

    assert (raised.value.code, raised.value.http_status) == ("ACCOUNT_CLOSED", 409)
    assert snapshot(scene) == before


@pytest.mark.parametrize("status", [AccountStatus.DISABLED, AccountStatus.PENDING_ACTIVATION])
def test_other_account_statuses_do_not_block_the_transition(sqlite_factory, status) -> None:
    """只有 CLOSED 是前置条件（设计 §4）。"""
    scene = Scene(sqlite_factory)
    scene.set_account_status(status)

    scene.change(DISABLED)

    [row] = events(sqlite_factory, EVENT_INTEGRATION_STATUS_CHANGED, scene.project_id)
    assert json.loads(row.payload_json or "{}")["reason_code"] == f"ACCOUNT_{status.value}"


# --- 跃迁的判定顺序与原因 --------------------------------------------------------------


def test_lookup_comes_before_closed_and_ownership_is_by_path(sqlite_factory) -> None:
    """设计 §4 判定顺序与 §6：404 先于 409；别人的项目与不存在的一样 404。"""
    mine = Scene(sqlite_factory)
    theirs = Scene(sqlite_factory)
    mine.set_account_status(AccountStatus.CLOSED)
    before = (snapshot(mine), snapshot(theirs))

    with pytest.raises(CustomerNotFound):
        mine.change(DISABLED, customer_id=str(uuid.uuid4()))
    for project_id in (theirs.project_id, str(uuid.uuid4())):
        with pytest.raises(ProjectNotFound) as raised:
            mine.change(DISABLED, project_id=project_id)
        assert (raised.value.code, raised.value.http_status) == ("PROJECT_NOT_FOUND", 404)

    assert (snapshot(mine), snapshot(theirs)) == before


@pytest.mark.parametrize("reason", ["", "   ", "\t\n", "x" * 256, "  " + "x" * 256 + "  "])
def test_a_bad_reason_is_422_before_anything_else(sqlite_factory, reason: str) -> None:
    scene = Scene(sqlite_factory)
    before = snapshot(scene)

    # 客户不存在也先报 422：原因是请求体校验。
    for customer_id in (scene.customer_id, str(uuid.uuid4())):
        with pytest.raises(IntegrationReasonInvalid) as raised:
            scene.change(DISABLED, reason=reason, customer_id=customer_id)
        assert (raised.value.code, raised.value.http_status) == ("VALIDATION_ERROR", 422)

    assert snapshot(scene) == before


@pytest.mark.parametrize(
    ("reason", "stored"),
    [("  Paused  ", "Paused"), ("x" * 255, "x" * 255), (" " + "y" * 255 + "\n", "y" * 255)],
)
def test_the_reason_is_trimmed_into_the_audit(sqlite_factory, reason: str, stored: str) -> None:
    scene = Scene(sqlite_factory)

    scene.change(DISABLED, reason=reason)

    [audit] = audits(
        sqlite_factory, AuditAction.PROJECT_INTEGRATION_STATUS_CHANGED, scene.project_id
    )
    assert audit.reason == stored


# --- 跃迁的回滚：SQLite 与 MySQL 各一次（INV-13） ----------------------------------------


@pytest.mark.parametrize("failure", ["audit", "outbox"])
def test_a_failed_transition_leaves_nothing(factory, monkeypatch, failure: str) -> None:
    """设计 §7「事务中途失败」：状态、版本、`status_changed_at`、退避、写入全部不变。"""
    scene = Scene(factory)
    scene.dirty_delivery()
    before = snapshot(scene)
    if failure == "audit":
        break_the_audit(monkeypatch)
    else:
        break_the_outbox(monkeypatch)

    with pytest.raises(IntegrityError):
        scene.change(DISABLED, now=LATER)

    assert snapshot(scene) == before
    assert before["tenant"][2:4] == (0, None)
    assert before["project"][0] is ENABLED
    assert before["delivery"]["next_attempt_at"] == FUTURE
    assert events(factory, EVENT_INTEGRATION_STATUS_CHANGED, scene.project_id) == []


# --- 跃迁的加锁顺序 -------------------------------------------------------------------


def test_the_transition_locks_tenant_then_project_and_never_the_wallet(factory) -> None:
    """设计 §2「事务边界」：租户 → 项目（都 `FOR UPDATE`）→ 投递行；不碰钱包（INV-4）。"""
    scene = Scene(factory)
    engine = factory.kw["bind"]

    statements = capture(factory, lambda: scene.change(DISABLED))

    assert not [statement for statement in statements if "WALLET" in statement]
    tenant = first_index(statements, is_tenant_read)
    project = first_index(statements, is_project_lock)
    delivery = first_index(statements, is_delivery_update)
    assert tenant < project < delivery
    if engine.dialect.name == "mysql":
        assert statements[tenant].endswith("FOR UPDATE")
        assert statements[project].endswith("FOR UPDATE")


# --- 设置地址 -----------------------------------------------------------------------


def test_setting_an_address_resets_the_cursor_and_the_lease(factory) -> None:
    """设计 §7「改 URL」：游标、租约与失败计数清空，`next_attempt_at = now`。"""
    scene = Scene(factory)
    scene.dirty_delivery()
    before = snapshot(scene)

    view = scene.set_url(HOOK.replace("hooks.example.com", "Hooks.Example.COM."), now=LATER)

    assert view == StatusWebhookView(status_webhook_url=HOOK, status_webhook_configured=True)
    after = snapshot(scene)
    assert after["project"] == (ENABLED, HOOK, LATER)
    assert after["delivery"] == {
        **before["delivery"],
        "delivered_version": None,
        "attempted_version": None,
        "attempt_count": 0,
        "next_attempt_at": LATER,
        "lease_token": None,
        "lease_expires_at": None,
    }
    # 改地址不是状态跃迁：版本、时间、outbox、钱包都不动。
    assert after["tenant"] == before["tenant"]
    assert after["wallet"] == before["wallet"]
    assert after["outbox"] == 0

    [audit] = audits(factory, AuditAction.PROJECT_STATUS_WEBHOOK_SET, scene.project_id)
    assert (audit.actor_user_id, audit.actor_role) == (scene.admin.id, "ADMIN")
    assert (audit.entity_type, audit.entity_id) == ("project", scene.project_id)
    assert (audit.ip_address, audit.user_agent) == (CONTEXT.ip_address, CONTEXT.user_agent)
    assert (audit.reason, audit.created_at) == (None, LATER)
    assert json.loads(audit.before_state or "{}") == {"status_webhook_host": None}
    assert json.loads(audit.after_state or "{}") == {"status_webhook_host": HOOK_HOST}


def test_changing_the_address_records_both_hosts(sqlite_factory) -> None:
    scene = Scene(sqlite_factory)
    scene.set_url(HOOK)
    scene.dirty_delivery()

    view = scene.set_url(OTHER_HOOK, now=LATER)

    assert view == StatusWebhookView(status_webhook_url=OTHER_HOOK, status_webhook_configured=True)
    assert scene.delivery()["delivered_version"] is None
    assert scene.delivery()["lease_token"] is None
    assert scene.delivery()["next_attempt_at"] == LATER
    _first, second = audits(
        sqlite_factory, AuditAction.PROJECT_STATUS_WEBHOOK_SET, scene.project_id
    )
    assert json.loads(second.before_state or "{}") == {"status_webhook_host": HOOK_HOST}
    assert json.loads(second.after_state or "{}") == {"status_webhook_host": "status.example.net"}


@pytest.mark.parametrize(
    "again",
    [
        HOOK,
        HOOK.replace("https://hooks.example.com", "HTTPS://HOOKS.EXAMPLE.COM"),
        HOOK.replace("hooks.example.com", "hooks.example.com.:443"),
    ],
)
def test_the_same_address_writes_nothing(sqlite_factory, again: str) -> None:
    """设计 §7「同一地址」：规范化后相同（例如只差大小写）：200，不重置游标、不写审计。"""
    scene = Scene(sqlite_factory)
    scene.set_url(HOOK)
    scene.dirty_delivery()
    before = snapshot(scene)

    view = scene.set_url(again, now=LATER)

    assert view == StatusWebhookView(status_webhook_url=HOOK, status_webhook_configured=True)
    assert snapshot(scene) == before


def test_clearing_twice_writes_once(sqlite_factory) -> None:
    """设计 §7「同一地址」的两次清除：第一次重置投递行并审计，第二次什么都不写。"""
    scene = Scene(sqlite_factory)
    scene.set_url(HOOK)
    scene.dirty_delivery()

    first = scene.set_url(None, now=LATER)

    assert first == StatusWebhookView(status_webhook_url=None, status_webhook_configured=False)
    after_first = snapshot(scene)
    assert after_first["project"] == (ENABLED, None, LATER)
    # 行保留，只是不再被领取（没有地址）。
    assert after_first["delivery"]["delivered_version"] is None
    assert after_first["delivery"]["lease_token"] is None
    assert after_first["delivery"]["next_attempt_at"] == LATER
    _set, cleared = audits(sqlite_factory, AuditAction.PROJECT_STATUS_WEBHOOK_SET, scene.project_id)
    assert json.loads(cleared.before_state or "{}") == {"status_webhook_host": HOOK_HOST}
    assert json.loads(cleared.after_state or "{}") == {"status_webhook_host": None}

    second = scene.set_url(None, now=LATER + dt.timedelta(minutes=1))

    assert second == first
    assert snapshot(scene) == after_first


def test_clearing_an_address_never_set_writes_nothing(sqlite_factory) -> None:
    """设计 §7「清除从未设过的地址」：200，不写库、不写审计。"""
    scene = Scene(sqlite_factory)
    scene.dirty_delivery()
    before = snapshot(scene)

    view = scene.set_url(None, now=LATER)

    assert view == StatusWebhookView(status_webhook_url=None, status_webhook_configured=False)
    assert snapshot(scene) == before
    assert audits(sqlite_factory, AuditAction.PROJECT_STATUS_WEBHOOK_SET, scene.project_id) == []


def test_setting_on_a_closed_account_is_409_but_clearing_is_allowed(sqlite_factory) -> None:
    """设计 §2：设为地址而租户 CLOSED 时 409（先于「相同值」）；清除不受此限。"""
    scene = Scene(sqlite_factory)
    scene.set_url(HOOK)
    scene.set_account_status(AccountStatus.CLOSED)
    before = snapshot(scene)

    for url in (OTHER_HOOK, HOOK):
        with pytest.raises(AccountClosed) as raised:
            scene.set_url(url, now=LATER)
        assert (raised.value.code, raised.value.http_status) == ("ACCOUNT_CLOSED", 409)
    assert snapshot(scene) == before

    view = scene.set_url(None, now=LATER)

    assert view.status_webhook_configured is False
    assert snapshot(scene)["project"] == (ENABLED, None, LATER)
    written = audits(sqlite_factory, AuditAction.PROJECT_STATUS_WEBHOOK_SET, scene.project_id)
    assert len(written) == 2


def test_the_address_order_is_422_then_404_then_409(sqlite_factory) -> None:
    """设计 §2 判定顺序：校验（422）→ 客户与项目（404）→ CLOSED（409）。"""
    mine = Scene(sqlite_factory)
    theirs = Scene(sqlite_factory)
    mine.set_account_status(AccountStatus.CLOSED)
    before = (snapshot(mine), snapshot(theirs))

    with pytest.raises(StatusWebhookUrlInvalid):
        mine.set_url("http://hooks.example.com/x", customer_id=str(uuid.uuid4()))
    with pytest.raises(CustomerNotFound):
        mine.set_url(HOOK, customer_id=str(uuid.uuid4()))
    for project_id in (theirs.project_id, str(uuid.uuid4())):
        with pytest.raises(ProjectNotFound):
            mine.set_url(HOOK, project_id=project_id)
        with pytest.raises(ProjectNotFound):
            mine.set_url(None, project_id=project_id)
    with pytest.raises(AccountClosed):
        mine.set_url(HOOK)

    assert (snapshot(mine), snapshot(theirs)) == before


def test_reading_the_address_is_read_only(sqlite_factory) -> None:
    """设计 §7「地址读取」的服务一半：返回规范化 URL 与是否已设；不写审计；越权 404。"""
    mine = Scene(sqlite_factory)
    theirs = Scene(sqlite_factory)
    assert mine.get_url() == StatusWebhookView(
        status_webhook_url=None, status_webhook_configured=False
    )
    mine.set_url(HOOK.replace("https://hooks", "HTTPS://HOOKS"))
    before = (snapshot(mine), snapshot(theirs))

    stored = mine.get_url()

    assert stored == StatusWebhookView(status_webhook_url=HOOK, status_webhook_configured=True)
    assert mine.get_url() == stored
    with pytest.raises(CustomerNotFound):
        mine.get_url(customer_id=str(uuid.uuid4()))
    for project_id in (theirs.project_id, str(uuid.uuid4())):
        with pytest.raises(ProjectNotFound):
            mine.get_url(project_id=project_id)

    assert (snapshot(mine), snapshot(theirs)) == before


def test_the_address_is_per_project(sqlite_factory) -> None:
    mine = Scene(sqlite_factory)
    theirs = Scene(sqlite_factory)
    theirs.dirty_delivery()
    before = snapshot(theirs)

    mine.set_url(HOOK)

    assert snapshot(theirs) == before
    assert theirs.get_url().status_webhook_url is None


# --- 设置地址的回滚：SQLite 与 MySQL 各一次 ---------------------------------------------


def test_a_failed_address_change_leaves_nothing(factory, monkeypatch) -> None:
    scene = Scene(factory)
    scene.dirty_delivery()
    before = snapshot(scene)
    break_the_audit(monkeypatch)

    with pytest.raises(IntegrityError):
        scene.set_url(HOOK, now=LATER)

    assert snapshot(scene) == before
    assert before["project"][1] is None
    assert before["delivery"]["delivered_version"] == 5


# --- 设置地址的加锁顺序 -----------------------------------------------------------------


def test_the_address_locks_tenant_shared_then_project_then_delivery(factory) -> None:
    """设计 §2「事务边界」：租户（共享）→ 项目 → 改 URL → 投递行 → 审计；不碰钱包。"""
    scene = Scene(factory)
    engine = factory.kw["bind"]

    statements = capture(factory, lambda: scene.set_url(HOOK))

    assert not [statement for statement in statements if "WALLET" in statement]
    tenant = first_index(statements, is_tenant_read)
    project = first_index(statements, is_project_lock)
    url = first_index(statements, lambda s: s.startswith("UPDATE PROJECTS "))
    delivery = first_index(statements, is_delivery_update)
    audit = first_index(statements, lambda s: s.startswith("INSERT INTO AUDIT_LOGS"))
    assert tenant < project < url < delivery < audit
    # 投递行按主键改，不按租户扫描。
    assert "PROJECT_STATUS_DELIVERIES.PROJECT_ID =" in statements[delivery]
    assert "TENANT_ID" not in statements[delivery]
    if engine.dialect.name == "mysql":
        assert statements[tenant].endswith(("FOR SHARE", "LOCK IN SHARE MODE"))
        assert statements[project].endswith("FOR UPDATE")


# --- 日志与审计里没有完整地址 -------------------------------------------------------------


def test_no_full_address_in_logs_or_audits(sqlite_factory, caplog) -> None:
    """设计 §6 与 §7「日志脱敏」的写入一半：只有主机名进审计；日志里没有地址。"""
    caplog.set_level(logging.DEBUG)
    scene = Scene(sqlite_factory)

    scene.set_url(HOOK)
    scene.set_url(HOOK)
    with pytest.raises(StatusWebhookUrlInvalid):
        scene.set_url(HOOK.replace("hooks.example.com", "hooks.example.com:8443"))
    scene.change(DISABLED)
    scene.set_url(None)

    with sqlite_factory() as session:
        rows = list(session.execute(select(AuditLog)).scalars())
        outbox = list(session.execute(select(DomainOutbox)).scalars())
    assert rows
    for row in rows:
        for written in (row.before_state or "", row.after_state or "", row.reason or ""):
            assert PATH_TOKEN not in written
            assert "/acuven" not in written
            assert "https://" not in written
    for row in outbox:
        assert PATH_TOKEN not in (row.payload_json or "")
    assert PATH_TOKEN not in caplog.text
    assert HOOK not in caplog.text


def test_the_service_does_not_import_the_wallet_or_log() -> None:
    """INV-4：服务层不导入钱包；地址与原因不进应用日志（设计 §6）。"""
    source = inspect.getsource(project_integration)
    assert "wallet" not in source.lower()
    assert "import logging" not in source
    assert "logger" not in source


# --- 并发：只在真 MySQL 上 ------------------------------------------------------------


@pytest.mark.parametrize("path", ["integration", "address"])
def test_a_pending_bump_holds_both_paths_back(mysql_factory, path: str) -> None:
    """设计 §4「加锁顺序」：未提交的 `bump_status_version` 持有租户排他锁，两条路径都先锁
    租户，所以都等它提交；跃迁读到的是提交之后的版本。
    """
    factory = mysql_factory
    scene = Scene(factory)
    scene.dirty_delivery()

    def act() -> object:
        if path == "integration":
            return scene.change(DISABLED, now=LATER)
        return scene.set_url(HOOK, now=LATER)

    with factory() as session_a:
        tenant = tenancy.get_tenant_by_public_id(session_a, scene.customer_id, for_update=True)
        assert tenant is not None
        bump_status_version(session_a, tenant, NOW, make_event=bumped_event)
        session_a.flush()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(act)
            time.sleep(1.5)
            blocked = not future.done()
            session_a.commit()
            future.result(timeout=60)

    assert blocked, "第二条路径没有等租户锁"
    if path == "integration":
        [audit] = audits(factory, AuditAction.PROJECT_INTEGRATION_STATUS_CHANGED, scene.project_id)
        assert json.loads(audit.before_state or "{}")["status_version"] == 1
        assert json.loads(audit.after_state or "{}")["status_version"] == 2
        assert snapshot(scene)["tenant"][2] == 2
    else:
        assert snapshot(scene)["project"][1] == HOOK
        assert snapshot(scene)["tenant"][2] == 1
        assert scene.delivery()["lease_token"] is None
        assert scene.delivery()["next_attempt_at"] == LATER


def test_the_new_paths_run_alongside_billing_without_deadlock(mysql_factory) -> None:
    """设计 §7「加锁不成环」的本层一半：集成状态跃迁、改地址（同一项目与另一项目）、
    记账跨零、账户状态启停同时反复执行：没有死锁，版本严格递增、不重复。
    """
    factory = mysql_factory
    scene = Scene(factory)
    second_pk, second_project = make_project(factory, scene.tenant_id)
    rounds = 6
    start = threading.Barrier(5)

    def toggle_integration() -> None:
        start.wait(timeout=30)
        for number in range(rounds):
            scene.change(DISABLED if number % 2 == 0 else ENABLED)

    def toggle_address(project_id: str) -> None:
        start.wait(timeout=30)
        for number in range(rounds):
            scene.set_url(HOOK if number % 2 == 0 else OTHER_HOOK, project_id=project_id)

    def adjust() -> None:
        start.wait(timeout=30)
        # 0 → 20 → 0 → 20 …：每一笔都跨零，每一笔都是一次计费跃迁。
        for number in range(rounds):
            kind, amount = TransactionType.ADJUSTMENT_CREDIT, Decimal("20")
            if number % 2:
                kind, amount = TransactionType.ADJUSTMENT_DEBIT, Decimal("-20")
            post_adjustment(
                factory,
                actor=scene.admin,
                customer_id=scene.customer_id,
                transaction_type=kind,
                amount=amount,
                reason="Concurrency adjustment",
                idempotency_key=str(uuid.uuid4()),
                context=CONTEXT,
                now=NOW,
            )

    def toggle_account() -> None:
        start.wait(timeout=30)
        for number in range(rounds):
            target = AccountStatus.DISABLED if number % 2 == 0 else AccountStatus.ENABLED
            change_account_status(
                factory,
                actor=scene.admin,
                customer_id=scene.customer_id,
                account_status=target,
                reason=REASON,
                context=CONTEXT,
                now=NOW,
            )

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [
            pool.submit(toggle_integration),
            pool.submit(toggle_address, scene.project_id),
            pool.submit(toggle_address, second_project),
            pool.submit(adjust),
            pool.submit(toggle_account),
        ]
        for future in futures:
            # 死锁（1213）或锁等待超时（1205）会在这里抛出。
            future.result(timeout=180)

    integration = audits(factory, AuditAction.PROJECT_INTEGRATION_STATUS_CHANGED, scene.project_id)
    account = audits(factory, AuditAction.TENANT_ACCOUNT_STATUS_CHANGED, scene.customer_id)
    billing = audits(factory, AuditAction.TENANT_BILLING_STATUS_CHANGED, scene.customer_id)
    assert (len(integration), len(account), len(billing)) == (rounds, rounds, rounds)
    total = 3 * rounds
    assert snapshot(scene)["tenant"][2] == total

    versions = []
    for audit in integration + account + billing:
        before = json.loads(audit.before_state or "{}")["status_version"]
        after = json.loads(audit.after_state or "{}")["status_version"]
        assert after == before + 1
        versions.append(after)
    assert sorted(versions) == list(range(1, total + 1))

    rows = (
        events(factory, EVENT_INTEGRATION_STATUS_CHANGED, scene.project_id)
        + events(factory, EVENT_ACCOUNT_STATUS_CHANGED, scene.customer_id)
        + events(factory, EVENT_BILLING_STATUS_CHANGED, scene.customer_id)
    )
    event_versions = [json.loads(row.payload_json or "{}")["status_version"] for row in rows]
    assert sorted(event_versions) == list(range(1, total + 1))

    # 每次改地址都是真实变化：每个项目恰好 rounds 条审计，最后停在 OTHER_HOOK。
    both = [(scene.project_id, scene.project_pk), (second_project, second_pk)]
    for project_id, project_pk in both:
        assert len(audits(factory, AuditAction.PROJECT_STATUS_WEBHOOK_SET, project_id)) == rounds
        with factory() as session:
            project = session.get(Project, project_pk)
            assert project is not None
            assert project.status_webhook_url == OTHER_HOOK
