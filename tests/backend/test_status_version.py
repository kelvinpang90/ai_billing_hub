"""Effective status composition and the single `status_version` entry (design gate #241 v6).

覆盖设计 §7 的这几行，以及 §2「有效状态合成」「`status_version` 递增点收拢」：

- **合成规则 / 优先级 / 公式只有一份**：纯函数，四个维度全组合（4×2×2×2 = 32 组），不连库；
- **只有一个递增入口**：扫描 `app/` 源码，递增语句只出现在 `bump_status_version` 里；
- **三个递增点**（余额、账户状态、内部计量）：各恰好一条对应类型的 outbox 行，payload 的版本
  等于新版本，`status_changed_at` 已写，投递行的退避已作废；SQLite 与 MySQL 各一次；
- **bump 只改未来时间的行**：过去与恰好等于 `now` 的行不动，别的租户的行不动；
- **回滚**：审计或 outbox 写入失败时状态、版本、`status_changed_at`、退避全部不变；SQLite 与
  MySQL 各一次；
- **退避重排只锁本租户**：A 租户的 bump 不提交，B 租户的 bump 与按主键 UPDATE 不被阻塞，只在
  真 MySQL 上（SQLite 没有行锁）。

MySQL 那部分需要 `BILLING_TEST_DATABASE_URL` 指向一个**可以被清空的**库，没设时 skip ——
**不要把 skipped 读成 passed**，CI 设了它并把 skipped 判成失败。
"""

from __future__ import annotations

import ast
import datetime as dt
import inspect
import itertools
import json
import os
import re
import time
import uuid
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, delete, event, func, select, text, update
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
    BillingMode,
    BillingStatus,
    EffectiveStatus,
    IntegrationStatus,
    Project,
    ReasonCode,
    Tenant,
    ai_service_enabled,
    compose_effective_status,
)
from app.models.wallet import TransactionType, Wallet, WalletTransaction
from app.repositories import tenancy
from app.repositories import wallet as wallet_repository
from app.repositories.tenancy import bump_status_version
from app.repositories.wallet import EVENT_BILLING_STATUS_CHANGED, create_wallet
from app.services import account_status, internal_billing
from app.services.account_status import EVENT_ACCOUNT_STATUS_CHANGED, change_account_status
from app.services.auth import RequestContext
from app.services.customers import add_status_delivery
from app.services.internal_billing import EVENT_BILLING_MODE_CHANGED, enable_internal_metering
from app.services.wallet_adjustments import post_adjustment

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")
TEST_EMAIL_DOMAIN = "@status-version-test.example.com"
COMPANY = "Status Version Test Sdn Bhd"

NOW = dt.datetime(2026, 10, 12, 9, 0, 0)
PAST = NOW - dt.timedelta(hours=1)
FUTURE = NOW + dt.timedelta(hours=1)
CONTEXT = RequestContext(ip_address="203.0.113.48", user_agent="status-version-test")
REASON = "Status version test reason"
TEST_EVENT = "test.status_bumped"

APP_DIR = Path(__file__).resolve().parents[2] / "app"
BUMP_FILE = APP_DIR / "repositories" / "tenancy.py"
# 设计 §3 INV-13 的「验证方式」逐字给出的模式。
INCREMENT = re.compile(r"status_version\s*(\+=|=\s*\S*status_version\s*\+)")

ALLOW = EffectiveStatus.ALLOW_AI
BLOCK = EffectiveStatus.BLOCK_AI

POINTS = ["balance", "account", "internal"]
EVENT_FOR = {
    "balance": EVENT_BILLING_STATUS_CHANGED,
    "account": EVENT_ACCOUNT_STATUS_CHANGED,
    "internal": EVENT_BILLING_MODE_CHANGED,
}
AUDIT_FOR = {
    "balance": AuditAction.TENANT_BILLING_STATUS_CHANGED,
    "account": AuditAction.TENANT_ACCOUNT_STATUS_CHANGED,
    "internal": AuditAction.INTERNAL_BILLING_MODE_SET,
}


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
    """⚠️ 库是共享的，每个用例前后都清场（与 test_account_status_service.py 同一做法）。"""
    ours = select(Tenant.id).where(Tenant.company_name == COMPANY)
    our_public_ids = select(Tenant.public_id).where(Tenant.company_name == COMPANY)
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
            delete(DomainOutbox).where(DomainOutbox.aggregate_id.in_(our_public_ids))
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


def make_project(factory, tenant_id: int, next_attempt_at: dt.datetime) -> int:
    """A project and its one delivery row, the row's backoff set to `next_attempt_at`."""
    with factory() as session:
        project = tenancy.create_project(session, tenant_id=tenant_id, name="Project", now=NOW)
        delivery = add_status_delivery(session, project, now=NOW)
        delivery.next_attempt_at = next_attempt_at
        session.commit()
        return int(project.id)


def next_attempt(factory, project_id: int) -> dt.datetime:
    with factory() as session:
        delivery = session.get(ProjectStatusDelivery, project_id)
        assert delivery is not None
        return delivery.next_attempt_at


def tenant_state(factory, tenant_id: int) -> tuple[object, ...]:
    with factory() as session:
        tenant = session.get(Tenant, tenant_id)
        assert tenant is not None
        return (
            tenant.account_status,
            tenant.billing_status,
            tenant.billing_mode,
            tenant.status_version,
            tenant.status_changed_at,
            tenant.updated_at,
        )


def events(factory, event_type: str, aggregate_id: str) -> list[DomainOutbox]:
    statement = (
        select(DomainOutbox)
        .where(DomainOutbox.event_type == event_type, DomainOutbox.aggregate_id == aggregate_id)
        .order_by(DomainOutbox.id)
    )
    with factory() as session:
        return list(session.execute(statement).scalars())


def snapshot(factory, tenant_id: int, public_id: str, project_id: int) -> dict[str, object]:
    """Everything a transition may write, as committed."""
    with factory() as session:
        outbox = select(func.count()).select_from(DomainOutbox)
        outbox = outbox.where(DomainOutbox.aggregate_id == public_id)
        audit = select(func.count()).select_from(AuditLog).where(AuditLog.entity_id == public_id)
        counts = (session.execute(outbox).scalar_one(), session.execute(audit).scalar_one())
    return {
        "tenant": tenant_state(factory, tenant_id),
        "next_attempt_at": next_attempt(factory, project_id),
        "outbox_and_audits": counts,
    }


def trigger(factory, admin: User, point: str, public_id: str) -> None:
    """One real transition through each of the three existing increment points."""
    if point == "balance":
        # 0 → 20：SUSPENDED → ACTIVE，一次计费跃迁。
        post_adjustment(
            factory,
            actor=admin,
            customer_id=public_id,
            transaction_type=TransactionType.ADJUSTMENT_CREDIT,
            amount=Decimal("20"),
            reason="Status version test top-up",
            idempotency_key=str(uuid.uuid4()),
            context=CONTEXT,
            now=NOW,
        )
    elif point == "account":
        change_account_status(
            factory,
            actor=admin,
            customer_id=public_id,
            account_status=AccountStatus.DISABLED,
            reason=REASON,
            context=CONTEXT,
            now=NOW,
        )
    else:
        enable_internal_metering(
            factory, actor=admin, customer_id=public_id, reason=REASON, context=CONTEXT, now=NOW
        )


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


def bump(factory, public_id: str) -> None:
    """Lock the tenant row, bump once with a test event, commit."""
    with factory() as session:
        tenant = tenancy.get_tenant_by_public_id(session, public_id, for_update=True)
        assert tenant is not None
        bump_status_version(session, tenant, NOW, make_event=bumped_event)
        session.commit()


# --- 合成规则：纯函数，全组合 -------------------------------------------------------


def _expected(
    account: AccountStatus,
    billing: BillingStatus,
    mode: BillingMode,
    integration: IntegrationStatus,
) -> tuple[EffectiveStatus, ReasonCode | None]:
    """设计 §2 的表，按顺序逐行照抄：第一条命中即 BLOCK_AI。"""
    rows = [
        (account == "CLOSED", ReasonCode.ACCOUNT_CLOSED),
        (account == "DISABLED", ReasonCode.ACCOUNT_DISABLED),
        (account == "PENDING_ACTIVATION", ReasonCode.ACCOUNT_PENDING_ACTIVATION),
        (integration == "DISABLED", ReasonCode.PROJECT_INTEGRATION_DISABLED),
        (mode == "PREPAID" and billing != "ACTIVE", ReasonCode.BALANCE_NOT_POSITIVE),
    ]
    for hit, reason in rows:
        if hit:
            return BLOCK, reason
    return ALLOW, None


ALL_COMBINATIONS = list(
    itertools.product(AccountStatus, BillingStatus, BillingMode, IntegrationStatus)
)


def test_the_combinations_are_all_thirty_two() -> None:
    assert len(ALL_COMBINATIONS) == 4 * 2 * 2 * 2 == len(set(ALL_COMBINATIONS))


@pytest.mark.parametrize(("account", "billing", "mode", "integration"), ALL_COMBINATIONS)
def test_every_combination_follows_the_table(account, billing, mode, integration) -> None:
    effective, reason = compose_effective_status(account, billing, mode, integration)
    assert (effective, reason) == _expected(account, billing, mode, integration)
    # ALLOW_AI 时没有原因码；BLOCK_AI 时一定有。
    assert (reason is None) == (effective is ALLOW)


@pytest.mark.parametrize(
    ("account", "billing", "mode", "integration", "expected"),
    [
        # 内部计量 + 余额停用 → ALLOW_AI（设计 §7「合成规则」）。
        ("ENABLED", "SUSPENDED", "INTERNAL_METERED_ONLY", "ENABLED", (ALLOW, None)),
        ("ENABLED", "ACTIVE", "PREPAID", "ENABLED", (ALLOW, None)),
        ("ENABLED", "SUSPENDED", "PREPAID", "ENABLED", (BLOCK, ReasonCode.BALANCE_NOT_POSITIVE)),
        (
            "ENABLED",
            "ACTIVE",
            "PREPAID",
            "DISABLED",
            (BLOCK, ReasonCode.PROJECT_INTEGRATION_DISABLED),
        ),
        (
            "PENDING_ACTIVATION",
            "ACTIVE",
            "INTERNAL_METERED_ONLY",
            "ENABLED",
            (BLOCK, ReasonCode.ACCOUNT_PENDING_ACTIVATION),
        ),
        ("CLOSED", "SUSPENDED", "PREPAID", "DISABLED", (BLOCK, ReasonCode.ACCOUNT_CLOSED)),
    ],
)
def test_spot_checks_written_out(account, billing, mode, integration, expected) -> None:
    result = compose_effective_status(
        AccountStatus(account),
        BillingStatus(billing),
        BillingMode(mode),
        IntegrationStatus(integration),
    )
    assert result == expected


def test_admin_actions_outrank_the_balance() -> None:
    """设计 §7「优先级」：账户 DISABLED + 项目 DISABLED + 余额停用 → ACCOUNT_DISABLED。"""
    result = compose_effective_status(
        AccountStatus.DISABLED,
        BillingStatus.SUSPENDED,
        BillingMode.PREPAID,
        IntegrationStatus.DISABLED,
    )
    assert result == (BLOCK, ReasonCode.ACCOUNT_DISABLED)


def test_the_values_are_the_wire_strings() -> None:
    assert {member.value for member in EffectiveStatus} == {"ALLOW_AI", "BLOCK_AI"}
    assert {member.value for member in ReasonCode} == {
        "ACCOUNT_CLOSED",
        "ACCOUNT_DISABLED",
        "ACCOUNT_PENDING_ACTIVATION",
        "PROJECT_INTEGRATION_DISABLED",
        "BALANCE_NOT_POSITIVE",
    }


# --- 公式只有一份 -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("account", "billing", "mode"),
    list(itertools.product(AccountStatus, BillingStatus, BillingMode)),
)
def test_ai_service_enabled_agrees_with_the_composition(account, billing, mode) -> None:
    """设计 §7「公式只有一份」：项目维度按 ENABLED，逐组全等。"""
    tenant = Tenant(account_status=account, billing_status=billing, billing_mode=mode)
    expected = compose_effective_status(account, billing, mode, IntegrationStatus.ENABLED)[0]
    assert ai_service_enabled(tenant) is (expected is ALLOW)


def test_no_inline_formula_is_left_behind() -> None:
    """`ai_service_enabled` 与三处事件构造都调用合成函数，没有各写一份的三元式。"""
    assert "compose_effective_status(" in inspect.getsource(ai_service_enabled)
    for module in (wallet_repository, account_status, internal_billing):
        source = inspect.getsource(module)
        assert '"ALLOW_AI" if' not in source
        assert "ai_service_enabled" not in source
        assert "compose_effective_status(" in source


# --- 只有一个递增入口 --------------------------------------------------------------


def test_only_bump_status_version_increments_the_version() -> None:
    """设计 §7「只有一个递增入口」：扫描 `app/`（不含 tests/）的源码。"""
    hits: list[tuple[Path, int]] = []
    for path in sorted(APP_DIR.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for match in INCREMENT.finditer(source):
            hits.append((path, source.count("\n", 0, match.start()) + 1))
    assert hits, "一条都没扫到：模式或目录写错了，下面的断言在空列表上空转"
    assert {path for path, _line in hits} == {BUMP_FILE}

    tree = ast.parse(BUMP_FILE.read_text(encoding="utf-8"))
    [function] = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "bump_status_version"
    ]
    assert function.end_lineno is not None
    for _path, line in hits:
        assert function.lineno <= line <= function.end_lineno


def test_make_event_is_required() -> None:
    parameter = inspect.signature(bump_status_version).parameters["make_event"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty


# --- bump 本身 ---------------------------------------------------------------------


def test_bump_does_four_things_in_order(sqlite_factory) -> None:
    """版本 +1 与 `status_changed_at` 先于 `make_event`；事件进同一个 session。"""
    tenant_id, public_id = make_customer(sqlite_factory)
    seen: list[tuple[int, dt.datetime | None]] = []

    def make_event(tenant: Tenant) -> DomainOutbox:
        seen.append((tenant.status_version, tenant.status_changed_at))
        return bumped_event(tenant)

    with sqlite_factory() as session:
        tenant = tenancy.get_tenant_by_public_id(session, public_id, for_update=True)
        assert tenant is not None
        bump_status_version(session, tenant, NOW, make_event=make_event)
        session.commit()

    assert seen == [(1, NOW)]
    assert tenant_state(sqlite_factory, tenant_id)[3:5] == (1, NOW)
    [row] = events(sqlite_factory, TEST_EVENT, public_id)
    assert json.loads(row.payload_json or "{}") == {"status_version": 1}


def test_bump_only_moves_backoffs_later_than_now(factory) -> None:
    """过去与恰好等于 now 的行不动，晚于 now 的提前到 now；别的租户的行不动。"""
    tenant_id, public_id = make_customer(factory)
    other_id, _other_public_id = make_customer(factory)
    past = make_project(factory, tenant_id, PAST)
    exact = make_project(factory, tenant_id, NOW)
    later = make_project(factory, tenant_id, FUTURE)
    much_later = make_project(factory, tenant_id, FUTURE + dt.timedelta(days=1))
    others = make_project(factory, other_id, FUTURE)

    bump(factory, public_id)

    assert next_attempt(factory, past) == PAST
    assert next_attempt(factory, exact) == NOW
    assert next_attempt(factory, later) == NOW
    assert next_attempt(factory, much_later) == NOW
    assert next_attempt(factory, others) == FUTURE
    assert tenant_state(factory, other_id)[3:5] == (0, None)


def test_bump_without_projects_skips_the_delivery_update(sqlite_factory) -> None:
    _tenant_id, public_id = make_customer(sqlite_factory)
    statements = _capture(sqlite_factory, lambda: bump(sqlite_factory, public_id))

    assert any("FROM PROJECTS" in statement for statement in statements)
    assert not [s for s in statements if "PROJECT_STATUS_DELIVERIES" in s]


def test_bump_updates_delivery_rows_by_primary_key(sqlite_factory) -> None:
    """设计 §2：经 `projects.tenant_id` 取 id，再按主键条件 UPDATE；不对投递表做范围扫描。"""
    tenant_id, public_id = make_customer(sqlite_factory)
    make_project(sqlite_factory, tenant_id, FUTURE)
    statements = _capture(sqlite_factory, lambda: bump(sqlite_factory, public_id))

    [project_read] = [s for s in statements if s.startswith("SELECT") and "FROM PROJECTS" in s]
    assert "PROJECTS.TENANT_ID =" in project_read
    assert "FOR UPDATE" not in project_read
    [delivery_update] = [s for s in statements if "PROJECT_STATUS_DELIVERIES" in s]
    assert delivery_update.startswith("UPDATE PROJECT_STATUS_DELIVERIES")
    assert "PROJECT_STATUS_DELIVERIES.PROJECT_ID IN" in delivery_update
    assert "PROJECT_STATUS_DELIVERIES.NEXT_ATTEMPT_AT >" in delivery_update
    assert "TENANT_ID" not in delivery_update


def _capture(factory, action) -> list[str]:
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


# --- 三个递增点：SQLite 与 MySQL 各一次 ----------------------------------------------


@pytest.mark.parametrize("point", POINTS)
def test_each_increment_point_writes_one_event_at_the_new_version(factory, point) -> None:
    admin = make_admin(factory)
    tenant_id, public_id = make_customer(factory)
    project_id = make_project(factory, tenant_id, FUTURE)

    trigger(factory, admin, point, public_id)

    assert tenant_state(factory, tenant_id)[3:5] == (1, NOW)
    [row] = events(factory, EVENT_FOR[point], public_id)
    assert (row.aggregate_type, row.status) == ("tenant", OutboxStatus.PENDING)
    assert (row.created_at, row.next_retry_at) == (NOW, NOW)
    payload = json.loads(row.payload_json or "{}")
    assert payload["status_version"] == 1
    assert REASON not in (row.payload_json or "")
    # 别的状态事件类型一条都没多写。
    for other in set(EVENT_FOR.values()) - {EVENT_FOR[point]}:
        assert events(factory, other, public_id) == []
    # bump 作废了这个租户投递行的退避。
    assert next_attempt(factory, project_id) == NOW


def test_the_billing_status_event_payload_is_unchanged(factory) -> None:
    admin = make_admin(factory)
    tenant_id, public_id = make_customer(factory)

    trigger(factory, admin, "balance", public_id)

    [row] = events(factory, EVENT_BILLING_STATUS_CHANGED, public_id)
    payload = json.loads(row.payload_json or "{}")
    with factory() as session:
        statement = select(WalletTransaction).where(WalletTransaction.tenant_id == tenant_id)
        [ledger] = session.execute(statement).scalars()
        ledger_id = ledger.public_id
    assert Decimal(payload.pop("balance")) == Decimal("20")
    assert payload == {
        "billing_status": "ACTIVE",
        "billing_mode": "PREPAID",
        "effective_status": "ALLOW_AI",
        "status_version": 1,
        "reason": "BALANCE_POSITIVE",
        "wallet_transaction": ledger_id,
    }


def test_the_account_status_event_payload_is_unchanged(sqlite_factory) -> None:
    admin = make_admin(sqlite_factory)
    _tenant_id, public_id = make_customer(sqlite_factory)

    trigger(sqlite_factory, admin, "account", public_id)

    [row] = events(sqlite_factory, EVENT_ACCOUNT_STATUS_CHANGED, public_id)
    assert json.loads(row.payload_json or "{}") == {
        "account_status": "DISABLED",
        "previous_account_status": "ENABLED",
        "billing_status": "SUSPENDED",
        "billing_mode": "PREPAID",
        "effective_status": "BLOCK_AI",
        "status_version": 1,
        "changed_at": "2026-10-12T09:00:00",
    }


def test_the_billing_mode_event_follows_the_design_table(factory) -> None:
    """设计 §2 的表：payload 五个键，聚合是租户 public_id，PENDING 且立刻可投。"""
    admin = make_admin(factory)
    _tenant_id, public_id = make_customer(factory)

    trigger(factory, admin, "internal", public_id)

    [row] = events(factory, EVENT_BILLING_MODE_CHANGED, public_id)
    assert (row.aggregate_type, row.aggregate_id) == ("tenant", public_id)
    assert (row.status, row.attempt_count) == (OutboxStatus.PENDING, 0)
    assert (row.next_retry_at, row.created_at, row.processed_at) == (NOW, NOW, None)
    assert json.loads(row.payload_json or "{}") == {
        "billing_mode": "INTERNAL_METERED_ONLY",
        "previous_billing_mode": "PREPAID",
        # 内部计量不受余额停用影响：钱包为 0、SUSPENDED，仍是 ALLOW_AI。
        "effective_status": "ALLOW_AI",
        "status_version": 1,
        "changed_at": "2026-10-12T09:00:00",
    }


def test_replacing_the_account_status_constructor_still_takes_effect(
    sqlite_factory, monkeypatch
) -> None:
    """闭包按名字查找 `_status_event`：test_account_status_service.py 的替换照常生效。"""
    admin = make_admin(sqlite_factory)
    _tenant_id, public_id = make_customer(sqlite_factory)
    real = account_status._status_event
    calls: list[int] = []

    def spy(tenant: Tenant, payload: dict[str, object], now: dt.datetime) -> DomainOutbox:
        calls.append(tenant.status_version)
        return real(tenant, payload, now)

    monkeypatch.setattr(account_status, "_status_event", spy)

    trigger(sqlite_factory, admin, "account", public_id)

    assert calls == [1]


# --- 回滚：SQLite 与 MySQL 各一次（INV-13） ---------------------------------------


def _break_audit(monkeypatch, point: str) -> None:
    """The transition's audit row fails at flush: `created_at` is NULL (NOT NULL)."""
    if point == "balance":
        real_audit = wallet_repository.AuditLog

        def broken_audit(**columns: object) -> AuditLog:
            row = real_audit(**columns)
            if row.action is AuditAction.TENANT_BILLING_STATUS_CHANGED:
                row.created_at = None  # type: ignore[assignment]
            return row

        monkeypatch.setattr(wallet_repository, "AuditLog", broken_audit)
        return
    module = account_status if point == "account" else internal_billing
    real_record = module.record_audit

    def broken_record(session: Session, **options: object) -> None:
        real_record(session, **{**options, "now": None})  # type: ignore[arg-type]

    monkeypatch.setattr(module, "record_audit", broken_record)


def _break_outbox(monkeypatch, point: str) -> None:
    """The transition's outbox row fails at flush: `created_at` is NULL (NOT NULL)."""
    if point == "balance":
        real_event = wallet_repository._outbox_event

        def broken_event(event_type, tenant, payload, now) -> DomainOutbox:
            row = real_event(event_type, tenant, payload, now)
            if event_type == EVENT_BILLING_STATUS_CHANGED:
                row.created_at = None  # type: ignore[assignment]
            return row

        monkeypatch.setattr(wallet_repository, "_outbox_event", broken_event)
        return
    if point == "account":
        module, name = account_status, "_status_event"
    else:
        module, name = internal_billing, "_mode_event"
    real = getattr(module, name)

    def broken(tenant: Tenant, payload: dict[str, object], now: dt.datetime) -> DomainOutbox:
        row = real(tenant, payload, now)
        row.created_at = None  # type: ignore[assignment]
        return row

    monkeypatch.setattr(module, name, broken)


@pytest.mark.parametrize("failure", ["audit", "outbox"])
@pytest.mark.parametrize("point", POINTS)
def test_a_failed_write_rolls_back_state_version_and_time(
    factory, monkeypatch, point: str, failure: str
) -> None:
    """设计 §7「事务中途失败」：状态、版本、`status_changed_at`、退避与写入全部不变。"""
    admin = make_admin(factory)
    tenant_id, public_id = make_customer(factory)
    project_id = make_project(factory, tenant_id, FUTURE)
    before = snapshot(factory, tenant_id, public_id, project_id)
    if failure == "audit":
        _break_audit(monkeypatch, point)
    else:
        _break_outbox(monkeypatch, point)

    with pytest.raises(IntegrityError):
        trigger(factory, admin, point, public_id)

    assert snapshot(factory, tenant_id, public_id, project_id) == before
    assert before["tenant"] == (
        AccountStatus.ENABLED,
        BillingStatus.SUSPENDED,
        BillingMode.PREPAID,
        0,
        None,
        NOW,
    )
    assert before["next_attempt_at"] == FUTURE
    assert events(factory, EVENT_FOR[point], public_id) == []
    with factory() as session:
        statement = select(AuditLog).where(AuditLog.action == AUDIT_FOR[point])
        assert session.execute(statement.where(AuditLog.entity_id == public_id)).first() is None


# --- 退避重排只锁本租户：只在真 MySQL 上 ---------------------------------------------


def test_rearming_backoff_locks_only_this_tenant(mysql_factory) -> None:
    """设计 §7「退避重排只锁本租户」：A 的 bump 未提交，B 的 bump 与主键 UPDATE 照常。"""
    factory = mysql_factory
    a_id, a_public = make_customer(factory)
    b_id, b_public = make_customer(factory)
    a_project = make_project(factory, a_id, FUTURE)
    b_project = make_project(factory, b_id, FUTURE)
    b_later = FUTURE + dt.timedelta(minutes=5)

    with factory() as session_a:
        tenant_a = tenancy.get_tenant_by_public_id(session_a, a_public, for_update=True)
        assert tenant_a is not None
        bump_status_version(session_a, tenant_a, NOW, make_event=bumped_event)
        session_a.flush()
        try:
            with factory() as session_b:
                # 被阻塞就在 2 秒后报 1205，而不是挂住整个用例。
                session_b.execute(text("SET SESSION innodb_lock_wait_timeout = 2"))
                started = time.monotonic()
                tenant_b = tenancy.get_tenant_by_public_id(session_b, b_public, for_update=True)
                assert tenant_b is not None
                bump_status_version(session_b, tenant_b, NOW, make_event=bumped_event)
                # 模拟设置地址：按主键直接改 B 自己的投递行。
                session_b.execute(
                    update(ProjectStatusDelivery)
                    .where(ProjectStatusDelivery.project_id == b_project)
                    .values(next_attempt_at=b_later, delivered_version=None)
                )
                session_b.commit()
                elapsed = time.monotonic() - started
                session_b.execute(text("SET SESSION innodb_lock_wait_timeout = 50"))
        finally:
            session_a.rollback()

    assert elapsed < 2
    assert next_attempt(factory, b_project) == b_later
    assert tenant_state(factory, b_id)[3:5] == (1, NOW)
    # A 回滚了：版本、时间与退避都没动。
    assert tenant_state(factory, a_id)[3:5] == (0, None)
    assert next_attempt(factory, a_project) == FUTURE
    assert events(factory, TEST_EVENT, a_public) == []
    assert len(events(factory, TEST_EVENT, b_public)) == 1
