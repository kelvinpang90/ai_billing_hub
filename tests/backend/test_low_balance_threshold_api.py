"""Admin low-balance threshold endpoints (AIH-TASK-109, design gate #433 v2 §7, first table).

`GET` / `PUT /api/v1/admin/customers/{customer_id}/low-balance-threshold`，以及
`post_transaction` 写 `tenant.low_balance` 只对 `PREPAID` 客户的那一处条件。

§7 第一张表里「鉴权」一行在 test_admin_customers_api.py 的路由枚举用例（两条路由与 PUT 的合法
请求体都补在那里）。任务契约的勘误：「改阈值不补发」与「租户越权」两行里断言客户钱包
`low_balance` / `low_balance_threshold` 的一半归 AIH-TASK-110（那两个键是它加的），这里只断言
事件与管理端 GET。

账本行由真实的调账接口（`post_transaction`）写入：
- SQLite：`create_all` 建表，再装上 test_usage_billing 那条代替触发器推进钱包的测试触发器；
- MySQL：表与触发器都走迁移，需要 `BILLING_TEST_DATABASE_URL`，没设时 MySQL 那一半与两条并发
  用例 skip —— **skip 不是 pass**，CI 里不许 skip。

并发用例只能对着真 MySQL 跑（SQLite 忽略 `FOR UPDATE`）：每个参与方用自己的引擎与应用，暂停点挂在
它自己引擎的 SQLAlchemy 事件上；「对方确实在等租户行锁」查 `performance_schema.data_lock_waits`
（写法同 test_session_revocation_concurrency.py）。匹配语句用 MySQL 编译出来的 SQL 文本，只在这两条
MySQL 用例里用。

示例 uuid 用全零占位值。
"""

from __future__ import annotations

import ast
import inspect
import itertools
import json
import textwrap
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, event, func, select, text, update
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from test_usage_billing import (
    _SQLITE_LEDGER_TRIGGER,
    T0,
    TEST_DATABASE_URL,
    TEST_EMAIL_DOMAIN,
    _mysql_factory,
)

from app.api import admin_customers
from app.core.config import Settings
from app.core.database import create_session_factory
from app.core.tokens import issue_access_token
from app.main import create_app
from app.models.auth import AuditAction, AuditLog, DomainOutbox, User, UserRole, UserStatus
from app.models.base import Base
from app.models.tenancy import Tenant
from app.models.wallet import Wallet, WalletTransaction
from app.repositories.wallet import EVENT_LOW_BALANCE
from app.services import customers as customers_service
from app.services.auth import utc_now

CUSTOMERS = "/api/v1/admin/customers"
PLACEHOLDER_ID = "00000000-0000-0000-0000-000000000000"
PLACEHOLDER_SESSION = "00000000-0000-0000-0000-000000000000"
FIELD = "low_balance_threshold"

_NUMBERS = itertools.count(1)


def settings_for(tmp_path) -> Settings:
    key = tmp_path / "jwt.key"
    key.write_text("a-test-signing-key-that-is-long-enough", encoding="utf-8")
    return Settings(jwt_secret_file=str(key), database_url="")


def _sqlite_factory() -> Iterator[sessionmaker[Session]]:
    # StaticPool：TestClient 在线程池里跑同步端点，内存库必须所有连接共用一个。
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql(_SQLITE_LEDGER_TRIGGER)
    yield create_session_factory(engine)
    engine.dispose()


def threshold_url(customer_id: str) -> str:
    return f"{CUSTOMERS}/{customer_id}/low-balance-threshold"


def adjustments_url(customer_id: str) -> str:
    return f"{CUSTOMERS}/{customer_id}/wallet/adjustments"


def adjustment_body(amount: str) -> dict[str, str]:
    """A valid adjustment; the idempotency key is a fresh zero-placeholder uuid."""
    return {
        "transaction_type": "ADJUSTMENT_DEBIT" if amount.startswith("-") else "ADJUSTMENT_CREDIT",
        "amount": amount,
        "reason": "Fictional adjustment for a threshold test",
        "idempotency_key": f"00000000-0000-4000-8000-{next(_NUMBERS):012d}",
    }


def error_of(response) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


class Console:
    """The app on one database, one admin, customers made over HTTP."""

    def __init__(self, factory: sessionmaker[Session], tmp_path) -> None:
        self.factory = factory
        self.settings = settings_for(tmp_path)
        self.app = self.app_on(factory)
        self.client = TestClient(self.app)
        now = utc_now().replace(microsecond=0)
        with factory() as session:
            admin = User(
                # 用 test_usage_billing 的域名：MySQL 清场按它删用户与租户。
                email=f"admin-{next(_NUMBERS)}{TEST_EMAIL_DOMAIN}",
                password_hash="not-a-real-hash",
                role=UserRole.ADMIN,
                status=UserStatus.ACTIVE,
                created_at=now,
                updated_at=now,
            )
            session.add(admin)
            session.commit()
            self.admin_id = int(admin.id)
        token = issue_access_token(
            self.settings,
            user_id=self.admin_id,
            role=UserRole.ADMIN.value,
            session_id=PLACEHOLDER_SESSION,
            now=utc_now(),
        )
        self.headers = {"Authorization": f"Bearer {token}"}

    @property
    def dialect(self) -> str:
        return self.factory.kw["bind"].dialect.name

    def app_on(self, factory: sessionmaker[Session]) -> FastAPI:
        app = create_app(self.settings)
        app.state.engine = factory.kw["bind"]
        app.state.session_factory = factory
        return app

    # -- calling --

    def customer(self) -> str:
        body = {
            "company_name": "Fictional Sdn Bhd",
            "email": f"threshold-{next(_NUMBERS)}{TEST_EMAIL_DOMAIN}",
        }
        response = self.client.post(CUSTOMERS, json=body, headers=self.headers)
        assert response.status_code == 201, response.text
        return response.json()["data"]["id"]

    def get(self, customer_id: str, client: TestClient | None = None):
        return (client or self.client).get(threshold_url(customer_id), headers=self.headers)

    def put(self, customer_id: str, body: object, client: TestClient | None = None):
        return (client or self.client).put(
            threshold_url(customer_id), json=body, headers=self.headers
        )

    def threshold(self, customer_id: str) -> str | None:
        response = self.get(customer_id)
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        # 恰好一个键（设计 §2「接口一」）。
        assert set(data) == {FIELD}
        return data[FIELD]

    def set(self, customer_id: str, value: str | None) -> str | None:
        response = self.put(customer_id, {FIELD: value})
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert set(data) == {FIELD}
        return data[FIELD]

    def adjust(self, customer_id: str, amount: str) -> str:
        """One admin adjustment through the real endpoint; the ledger row's public id."""
        response = self.client.post(
            adjustments_url(customer_id), json=adjustment_body(amount), headers=self.headers
        )
        assert response.status_code == 201, response.text
        return response.json()["data"]["id"]

    def enable_internal_metering(self, customer_id: str) -> None:
        response = self.client.post(
            f"{CUSTOMERS}/{customer_id}/internal-metering",
            json={"reason": "Fictional internal tenant"},
            headers=self.headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["billing_mode"] == "INTERNAL_METERED_ONLY"

    def backdate(self, customer_id: str) -> None:
        """`updated_at = T0`, so a write in the same second as the setup still shows."""
        with self.factory() as session:
            session.execute(
                update(Tenant).where(Tenant.public_id == customer_id).values(updated_at=T0)
            )
            session.commit()

    # -- reading back --

    def tenant(self, customer_id: str) -> Tenant:
        with self.factory() as session:
            found = session.execute(
                select(Tenant).where(Tenant.public_id == customer_id)
            ).scalar_one()
            session.expunge(found)
            return found

    def audits(self, customer_id: str | None = None) -> list[AuditLog]:
        statement = select(AuditLog).where(
            AuditLog.action == AuditAction.TENANT_LOW_BALANCE_THRESHOLD_SET
        )
        if customer_id is not None:
            statement = statement.where(AuditLog.entity_id == customer_id)
        with self.factory() as session:
            rows = list(session.execute(statement.order_by(AuditLog.id)).scalars())
            session.expunge_all()
            return rows

    def states(self, customer_id: str) -> list[tuple[str | None, str | None]]:
        """(before, after) threshold of each audit, oldest first; checks the two-key whitelist."""
        pairs = []
        for row in self.audits(customer_id):
            before = json.loads(row.before_state or "{}")
            after = json.loads(row.after_state or "{}")
            assert set(before) == set(after) == {"public_id", FIELD}
            assert before["public_id"] == after["public_id"] == customer_id
            pairs.append((before[FIELD], after[FIELD]))
        return pairs

    def low_balance_events(self, customer_id: str) -> list[dict]:
        statement = (
            select(DomainOutbox.payload_json)
            .where(
                DomainOutbox.event_type == EVENT_LOW_BALANCE,
                DomainOutbox.aggregate_id == customer_id,
            )
            .order_by(DomainOutbox.id)
        )
        with self.factory() as session:
            return [json.loads(payload or "{}") for payload in session.execute(statement).scalars()]

    def count(self, model: type) -> int:
        with self.factory() as session:
            return int(session.execute(select(func.count()).select_from(model)).scalar_one())

    def footprint(self, customer_id: str) -> dict[str, object]:
        """Everything a threshold write must leave alone (INV-4, INV-1)."""
        tenant = self.tenant(customer_id)
        with self.factory() as session:
            balance, version = session.execute(
                select(Wallet.balance, Wallet.version).where(Wallet.tenant_id == tenant.id)
            ).one()
        return {
            "balance": balance,
            "wallet_version": version,
            "ledger_rows": self.count(WalletTransaction),
            "outbox_rows": self.count(DomainOutbox),
            "status_version": tenant.status_version,
            "billing_status": tenant.billing_status,
            "status_changed_at": tenant.status_changed_at,
        }


def _console(factories: Iterator[sessionmaker[Session]], tmp_path) -> Iterator[Console]:
    factory = next(factories)
    try:
        yield Console(factory, tmp_path)
    finally:
        for _ in factories:
            pass


@pytest.fixture(params=["sqlite", "mysql"])
def both(request, monkeypatch, tmp_path) -> Iterator[Console]:
    """The same cases on SQLite and on a real MySQL (design §7「SQLite 与 MySQL 参数化」)."""
    factories = _mysql_factory(monkeypatch) if request.param == "mysql" else _sqlite_factory()
    yield from _console(factories, tmp_path)


# --- 正常路径、清除、同值不写 ---------------------------------------------------------------


def test_an_admin_reads_and_sets_the_threshold_with_one_audit(both: Console) -> None:
    """§7「正常路径」：先 null；PUT "50.00" 返回 "50.00000000"；GET 同值。

    一条审计；updated_at 前进。
    """
    customer_id = both.customer()
    both.backdate(customer_id)

    assert both.threshold(customer_id) is None
    response = both.put(customer_id, {FIELD: "50.00"})

    assert response.status_code == 200, response.text
    envelope = response.json()
    assert (envelope["success"], envelope["error"]) == (True, None)
    assert envelope["data"] == {FIELD: "50.00000000"}
    assert both.threshold(customer_id) == "50.00000000"
    [audit] = both.audits(customer_id)
    assert (audit.actor_user_id, audit.actor_role) == (both.admin_id, "ADMIN")
    assert (audit.entity_type, audit.entity_id) == ("tenant", customer_id)
    assert both.states(customer_id) == [(None, "50.00000000")]
    assert both.tenant(customer_id).updated_at > T0


def test_null_clears_the_threshold_with_a_second_audit(both: Console) -> None:
    """§7「清除」：设 "50" 后 PUT null → 200 null；第二条审计前 "50.00000000"、后 null。"""
    customer_id = both.customer()
    assert both.set(customer_id, "50") == "50.00000000"

    assert both.set(customer_id, None) is None

    assert both.threshold(customer_id) is None
    assert both.states(customer_id) == [(None, "50.00000000"), ("50.00000000", None)]


def test_the_same_value_writes_nothing(both: Console) -> None:
    """§7「同值不写」「幂等重放」：第二次 200、审计仍一条、updated_at 不变。

    "50" 与 "50.00" 同值。
    """
    customer_id = both.customer()
    both.set(customer_id, "50.00")
    both.backdate(customer_id)

    assert both.set(customer_id, "50.00") == "50.00000000"
    assert both.set(customer_id, "50") == "50.00000000"

    assert len(both.audits(customer_id)) == 1
    assert both.tenant(customer_id).updated_at == T0


def test_null_on_an_unset_customer_writes_nothing(both: Console) -> None:
    """§7「同值不写」：未设置的客户 PUT null → 200 null，不写审计，updated_at 不变。"""
    customer_id = both.customer()
    both.backdate(customer_id)

    assert both.set(customer_id, None) is None

    assert both.audits(customer_id) == []
    assert both.tenant(customer_id).updated_at == T0


# --- 边界值与校验 -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "stored"),
    [
        ("0.01", "0.01000000"),
        ("999999999999.99", "999999999999.99000000"),
        ("7", "7.00000000"),
        ("7.5", "7.50000000"),
    ],
)
def test_boundary_values_are_stored_at_8_places(both: Console, value: str, stored: str) -> None:
    """§7「边界值」：都 200，存为对应的 8 位值。

    ⚠️ 上界读回只在 MySQL 上断言：SQLite 把 NUMERIC 存成浮点，14 位有效数字的值读回来带误差，
    测的是 SQLite 而不是接口。PUT 的响应与审计在两边都逐字比对。
    """
    customer_id = both.customer()

    assert both.set(customer_id, value) == stored

    assert both.states(customer_id) == [(None, stored)]
    if both.dialect == "mysql" or value != "999999999999.99":
        assert both.threshold(customer_id) == stored


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({}, FIELD),
        ({FIELD: 50}, FIELD),
        ({FIELD: 50.5}, FIELD),
        ({FIELD: "0"}, FIELD),
        ({FIELD: "0.00"}, FIELD),
        ({FIELD: "-1"}, FIELD),
        ({FIELD: "1.001"}, FIELD),
        ({FIELD: "1000000000000"}, FIELD),
        ({FIELD: ""}, FIELD),
        ({FIELD: " 5"}, FIELD),
        ({FIELD: "5."}, FIELD),
        ({FIELD: ".5"}, FIELD),
        ({FIELD: "１"}, FIELD),
        ({FIELD: "50", "tenant_id": "probe-tenant-7c41"}, "tenant_id"),
        ({FIELD: "50", "customer_id": PLACEHOLDER_ID}, "customer_id"),
    ],
    ids=[
        "missing",
        "json-int",
        "json-float",
        "zero",
        "zero-2-places",
        "negative",
        "3-places",
        "13-integer-digits",
        "empty",
        "leading-space",
        "trailing-dot",
        "leading-dot",
        "fullwidth-digit",
        "extra-tenant_id",
        "extra-customer_id",
    ],
)
def test_bad_bodies_are_422_without_echoing_and_write_nothing(
    both: Console, body: dict, field: str
) -> None:
    """§7「校验」（INV-8、INV-10）：422 VALIDATION_ERROR，只列字段名、不回显值。

    阈值与审计都不变。
    """
    customer_id = both.customer()
    both.backdate(customer_id)

    response = both.put(customer_id, body)

    assert error_of(response) == (422, "VALIDATION_ERROR")
    # 逐字比对 app/core/errors.py 的文案：只有字段名，没有收到的值。
    assert response.json()["error"]["message"] == f"Invalid request fields: body.{field}"
    for value in body.values():
        if isinstance(value, str):
            assert f'"{value}"' not in response.text
    assert both.threshold(customer_id) is None
    assert both.audits() == []
    assert both.tenant(customer_id).updated_at == T0


def test_an_unknown_customer_is_404_on_both_methods_and_writes_nothing(both: Console) -> None:
    """§7「客户不存在」：全零占位 public_id 的 GET 与 PUT 都 404 CUSTOMER_NOT_FOUND，不写审计。"""
    both.customer()
    audits_before = both.count(AuditLog)

    read = both.get(PLACEHOLDER_ID)
    written = both.put(PLACEHOLDER_ID, {FIELD: "50"})

    assert error_of(read) == (404, "CUSTOMER_NOT_FOUND")
    assert error_of(written) == (404, "CUSTOMER_NOT_FOUND")
    assert both.count(AuditLog) == audits_before


# --- 余额不动、不补发、重新武装 ---------------------------------------------------------------


def test_setting_the_threshold_moves_no_money_and_no_status(both: Console) -> None:
    """§7「余额不动（INV-4）」：钱包余额、version、账本行数、status_version、outbox 行数都不变。"""
    customer_id = both.customer()
    both.adjust(customer_id, "100")
    before = both.footprint(customer_id)

    both.set(customer_id, "50")
    both.set(customer_id, "150")
    both.set(customer_id, None)

    assert both.footprint(customer_id) == before
    assert len(both.audits(customer_id)) == 3


def test_a_posting_after_the_setting_crosses_the_new_threshold(both: Console) -> None:
    """§7「设置后跨越用新值」：余额 100；PUT "50"；扣 60 → 恰好一条 tenant.low_balance。"""
    customer_id = both.customer()
    both.adjust(customer_id, "100")
    both.set(customer_id, "50")

    debit = both.adjust(customer_id, "-60")

    assert both.low_balance_events(customer_id) == [
        {"balance": "40.00000000", "threshold": "50.00000000", "wallet_transaction": debit}
    ]


def test_raising_the_threshold_above_the_balance_emits_nothing_and_rearms(both: Console) -> None:
    """§7「改阈值不补发」「重新武装」。

    余额 30；PUT "50"：没有事件，管理端 GET 是新值（客户钱包那一半归 AIH-TASK-110）。接着加 30
    （余额 60）再扣 20（余额 40）：只在扣到 40 那一笔多出一条。
    """
    customer_id = both.customer()
    both.adjust(customer_id, "30")

    both.set(customer_id, "50")

    assert both.low_balance_events(customer_id) == []
    assert both.threshold(customer_id) == "50.00000000"

    both.adjust(customer_id, "30")
    assert both.low_balance_events(customer_id) == []
    debit = both.adjust(customer_id, "-20")

    assert both.low_balance_events(customer_id) == [
        {"balance": "40.00000000", "threshold": "50.00000000", "wallet_transaction": debit}
    ]


# --- 低余额事件只对预付客户 -----------------------------------------------------------------


def test_an_internal_metered_customer_never_gets_a_low_balance_event(both: Console) -> None:
    """§7「内部计量不发事件」：转内部计量、加 100、PUT "50"、扣 60 → 没有事件。

    PREPAID 对照恰好一条。
    """
    internal = both.customer()
    prepaid = both.customer()
    both.enable_internal_metering(internal)

    for customer_id in (internal, prepaid):
        both.adjust(customer_id, "100")
        both.set(customer_id, "50")
        both.adjust(customer_id, "-60")

    assert both.low_balance_events(internal) == []
    [payload] = both.low_balance_events(prepaid)
    assert (payload["balance"], payload["threshold"]) == ("40.00000000", "50.00000000")


def test_a_threshold_set_before_internal_metering_emits_nothing(both: Console) -> None:
    """§7「先设阈值后转内部计量」：PUT "50"；转内部计量；加 100 再扣 60 → 没有事件。"""
    customer_id = both.customer()
    both.set(customer_id, "50")
    both.enable_internal_metering(customer_id)

    both.adjust(customer_id, "100")
    both.adjust(customer_id, "-60")

    assert both.low_balance_events(customer_id) == []
    assert both.threshold(customer_id) == "50.00000000"


# --- 事务中途失败与租户越权 -----------------------------------------------------------------


def test_a_failing_audit_rolls_the_threshold_back(both: Console, monkeypatch) -> None:
    """§7「事务中途失败」（INV-13）：审计写入抛错 → 500 带 request_id；阈值仍是旧值、无新审计。"""
    customer_id = both.customer()
    both.set(customer_id, "20")
    both.backdate(customer_id)

    def broken(*_args, **_kwargs):
        raise RuntimeError("fictional audit failure")

    monkeypatch.setattr(customers_service, "record_audit", broken)
    response = both.put(
        customer_id, {FIELD: "30"}, client=TestClient(both.app, raise_server_exceptions=False)
    )

    assert error_of(response) == (500, "INTERNAL_ERROR")
    assert response.json()["request_id"]
    assert "fictional audit failure" not in response.text
    assert both.threshold(customer_id) == "20.00000000"
    assert both.states(customer_id) == [(None, "20.00000000")]
    assert both.tenant(customer_id).updated_at == T0


def test_a_threshold_belongs_to_the_customer_in_the_path_only(both: Console) -> None:
    """§7「租户越权」（INV-8）：给 A 设阈值，B 的管理端 GET 为 null；改 B 不碰 A。"""
    mine = both.customer()
    theirs = both.customer()

    both.set(mine, "50")

    assert both.threshold(theirs) is None
    assert both.audits(theirs) == []
    both.set(theirs, "20")
    assert both.threshold(mine) == "50.00000000"
    assert both.states(mine) == [(None, "50.00000000")]


# --- 鉴权：处理函数第一条语句 -------------------------------------------------------------


@pytest.mark.parametrize(
    "handler",
    [admin_customers.get_low_balance_threshold, admin_customers.set_low_balance_threshold],
    ids=["GET", "PUT"],
)
def test_the_handlers_call_require_admin_first(handler: Callable[..., Any]) -> None:
    """设计 §2：函数体第一条语句（跳过 docstring）是对 `require_admin` 的调用。"""
    source = textwrap.dedent(inspect.getsource(handler))
    [function] = ast.parse(source).body
    assert isinstance(function, ast.FunctionDef)
    body = function.body
    if ast.get_docstring(function) is not None:
        body = body[1:]
    first = body[0]
    call = first.value if isinstance(first, ast.Assign | ast.Expr) else None
    assert isinstance(call, ast.Call)
    assert isinstance(call.func, ast.Name) and call.func.id == "require_admin"


# --- 并发（只在真 MySQL 上） ----------------------------------------------------------------

# 暂停点最多停这么久；到点还没人放行，被停住的那一方报错退出。
GATE_TIMEOUT_SECONDS = 15.0
# 「对方在等行锁」的轮询：间隔 50 毫秒、最多 10 秒。
LOCK_POLL_INTERVAL_SECONDS = 0.05
LOCK_POLL_LIMIT_SECONDS = 10.0
# 参与方的连接都把 innodb_lock_wait_timeout 调到这个值：真卡住时以 1205 收场，不等默认的 50 秒。
LOCK_WAIT_TIMEOUT_SECONDS = 20
# 等一个参与方结束：比锁等待超时长，锁等待超时会先以错误的形式出现。
JOIN_TIMEOUT_SECONDS = 40.0

_CONNECTION_ID = "connection_id"

# ⚠️ 不用 information_schema.INNODB_TRX：它是不满 0.1 秒不刷新的缓存（见
# test_session_revocation_concurrency.py）。
_WAITING_ON_TABLE = text(
    "SELECT COUNT(*) FROM performance_schema.data_lock_waits AS w"
    " JOIN performance_schema.threads AS t ON t.THREAD_ID = w.REQUESTING_THREAD_ID"
    " JOIN performance_schema.data_locks AS l"
    " ON l.ENGINE = w.ENGINE AND l.ENGINE_LOCK_ID = w.REQUESTING_ENGINE_LOCK_ID"
    " WHERE t.PROCESSLIST_ID = :connection_id"
    " AND l.OBJECT_SCHEMA = DATABASE() AND l.OBJECT_NAME = :table_name"
)
_LOCK_REPORT = text(
    "SELECT THREAD_ID, OBJECT_SCHEMA, OBJECT_NAME, INDEX_NAME, LOCK_TYPE, LOCK_MODE,"
    " LOCK_STATUS, LOCK_DATA FROM performance_schema.data_locks"
)


def _sql(statement: str) -> str:
    return " ".join(statement.replace("`", "").lower().split())


def locks_tenant(statement: str) -> bool:
    """`SELECT … FROM tenants … FOR UPDATE`: the setting's only lock, the posting's second."""
    sql = _sql(statement)
    return sql.startswith("select ") and " from tenants " in sql and sql.endswith(" for update")


def updates_threshold(statement: str) -> bool:
    """The UPDATE of `set_low_balance_threshold`, issued with the tenant row already locked."""
    sql = _sql(statement)
    return sql.startswith("update tenants set ") and FIELD in sql


def _prepare_connection(dbapi_connection: Any, connection_record: Any) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("SELECT CONNECTION_ID()")
        connection_record.info[_CONNECTION_ID] = int(cursor.fetchone()[0])
        cursor.execute(f"SET SESSION innodb_lock_wait_timeout = {LOCK_WAIT_TIMEOUT_SECONDS}")
    finally:
        cursor.close()
    dbapi_connection.rollback()


class Gate:
    """One pause point: the first matching statement waits here until `release()`."""

    def __init__(self, step: str) -> None:
        self.step = step
        self.reached = threading.Event()
        self.released = threading.Event()
        self._fired = False
        self._lock = threading.Lock()

    def claim(self) -> bool:
        with self._lock:
            if self._fired:
                return False
            self._fired = True
            return True

    def hold(self) -> None:
        self.reached.set()
        if not self.released.wait(GATE_TIMEOUT_SECONDS):
            raise AssertionError(f"nobody released the gate {self.step!r}")

    def wait_reached(self) -> None:
        if not self.reached.wait(GATE_TIMEOUT_SECONDS):
            pytest.fail(f"never reached the gate {self.step!r} within {GATE_TIMEOUT_SECONDS}s")

    def release(self) -> None:
        self.released.set()


class Racer:
    """One side of a race: its own engine and app, and the thread it runs on."""

    def __init__(self, console: Console, name: str) -> None:
        self.name = name
        self.engine: Engine = create_engine(TEST_DATABASE_URL, pool_size=2, max_overflow=2)
        event.listen(self.engine, "connect", _prepare_connection)
        # `CONNECTION_ID()` of each connection that sent `SELECT … FROM tenants … FOR UPDATE`.
        self.lockers: list[int | None] = []
        self.errors: list[BaseException] = []
        self.gates: list[Gate] = []
        self.result: Any = None
        self.error: BaseException | None = None
        self._thread: threading.Thread | None = None

        def note_locker(conn, cursor, statement, parameters, context, executemany) -> None:
            if locks_tenant(statement):
                self.lockers.append(conn.info.get(_CONNECTION_ID))

        def note_error(exception_context) -> None:
            self.errors.append(exception_context.original_exception)

        event.listen(self.engine, "before_cursor_execute", note_locker)
        event.listen(self.engine, "handle_error", note_error)
        self.client = TestClient(
            console.app_on(create_session_factory(self.engine)), raise_server_exceptions=False
        )

    def pause_after(self, step: str, matches: Callable[[str], bool]) -> Gate:
        gate = Gate(f"{self.name}: {step}")
        self.gates.append(gate)

        def listener(conn, cursor, statement, parameters, context, executemany) -> None:
            if matches(statement) and gate.claim():
                gate.hold()

        event.listen(self.engine, "after_cursor_execute", listener)
        return gate

    def start(self, work: Callable[[], Any]) -> Racer:
        def run() -> None:
            try:
                self.result = work()
            except BaseException as exc:
                self.error = exc

        self._thread = threading.Thread(target=run, name=self.name, daemon=True)
        self._thread.start()
        return self

    def value(self) -> Any:
        assert self._thread is not None
        self._thread.join(JOIN_TIMEOUT_SECONDS)
        if self._thread.is_alive():
            pytest.fail(f"{self.name!r} did not finish within {JOIN_TIMEOUT_SECONDS}s")
        if self.error is not None:
            raise AssertionError(f"{self.name!r} failed: {self.error!r}") from self.error
        assert self.errors == [], f"database errors on {self.name!r}: {self.errors!r}"
        return self.result

    def close(self) -> None:
        for gate in self.gates:
            gate.release()
        if self._thread is not None:
            self._thread.join(5)
        self.engine.dispose()


class Race:
    """Racers on the MySQL database of `console`, and an observer for the lock tables."""

    def __init__(self, console: Console) -> None:
        self.console = console
        self.observer = create_engine(TEST_DATABASE_URL, isolation_level="AUTOCOMMIT", pool_size=1)
        self._racers: list[Racer] = []

    def racer(self, name: str) -> Racer:
        racer = Racer(self.console, name)
        self._racers.append(racer)
        return racer

    def wait_until_waiting_for_tenants(self, racer: Racer) -> None:
        """Poll until `racer`'s own connection is waiting for a row lock on `tenants`."""
        deadline = time.monotonic() + LOCK_POLL_LIMIT_SECONDS
        connection_id = None
        with self.observer.connect() as connection:
            while True:
                connection_id = racer.lockers[-1] if racer.lockers else None
                if connection_id is not None:
                    waiting = connection.execute(
                        _WAITING_ON_TABLE, {"connection_id": connection_id, "table_name": "tenants"}
                    ).scalar_one()
                    if waiting:
                        return
                if time.monotonic() >= deadline:
                    break
                time.sleep(LOCK_POLL_INTERVAL_SECONDS)
            report = [tuple(row) for row in connection.execute(_LOCK_REPORT).all()]
        pytest.fail(
            f"{racer.name} (connection {connection_id}) was not seen waiting for a tenants row"
            f" lock within {LOCK_POLL_LIMIT_SECONDS}s\ndata_locks: {report}"
        )

    def close(self) -> None:
        for racer in self._racers:
            racer.close()
        self.observer.dispose()


@pytest.fixture
def race(monkeypatch, tmp_path) -> Iterator[Race]:
    for console in _console(_mysql_factory(monkeypatch), tmp_path):
        created = Race(console)
        try:
            yield created
        finally:
            created.close()


def test_a_posting_waits_for_an_uncommitted_setting_and_uses_the_new_threshold(
    race: Race,
) -> None:
    """§7「并发：与记账」（MySQL）。

    事务 A 锁住客户行、把阈值改为 "50"、暂不提交；B 对余额 100 的客户扣 60，确认 B 在等租户行锁
    之后放行 A。B 完成后恰好一条 tenant.low_balance，载荷阈值为 "50.00000000"；无死锁。
    """
    console = race.console
    customer_id = console.customer()
    console.adjust(customer_id, "100")
    setter, poster = race.racer("set threshold"), race.racer("debit")
    gate = setter.pause_after("threshold updated, not committed", updates_threshold)

    setter.start(lambda: console.put(customer_id, {FIELD: "50"}, client=setter.client))
    gate.wait_reached()
    poster.start(
        lambda: poster.client.post(
            adjustments_url(customer_id), json=adjustment_body("-60"), headers=console.headers
        )
    )
    race.wait_until_waiting_for_tenants(poster)
    gate.release()
    setting, posting = setter.value(), poster.value()

    assert setting.status_code == 200, setting.text
    assert setting.json()["data"] == {FIELD: "50.00000000"}
    assert posting.status_code == 201, posting.text
    assert console.low_balance_events(customer_id) == [
        {
            "balance": "40.00000000",
            "threshold": "50.00000000",
            "wallet_transaction": posting.json()["data"]["id"],
        }
    ]
    assert console.states(customer_id) == [(None, "50.00000000")]


def test_two_settings_serialize_on_the_tenant_row(race: Race) -> None:
    """§7「并发：两次设置」（MySQL）。

    PUT "20" 持锁未提交时 PUT "30" 开始并等锁；放行。两条审计，第二条的 before 等于第一条的 after；
    最终值等于后提交的那条的 after。
    """
    console = race.console
    customer_id = console.customer()
    first, second = race.racer("PUT 20"), race.racer("PUT 30")
    gate = first.pause_after("threshold updated, not committed", updates_threshold)

    first.start(lambda: console.put(customer_id, {FIELD: "20"}, client=first.client))
    gate.wait_reached()
    second.start(lambda: console.put(customer_id, {FIELD: "30"}, client=second.client))
    race.wait_until_waiting_for_tenants(second)
    gate.release()
    earlier, later = first.value(), second.value()

    assert (earlier.status_code, later.status_code) == (200, 200)
    assert earlier.json()["data"] == {FIELD: "20.00000000"}
    assert later.json()["data"] == {FIELD: "30.00000000"}
    states = console.states(customer_id)
    assert states == [(None, "20.00000000"), ("20.00000000", "30.00000000")]
    assert states[1][0] == states[0][1]
    assert console.threshold(customer_id) == states[-1][1]
