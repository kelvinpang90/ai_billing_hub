"""Admin wallet reconciliation views end to end (AIH-TASK-134).

设计闸门 #474 v13 §2「管理端接口」与 §7。
`GET /api/v1/admin/wallet-reconciliation/runs` 与 `GET …/runs/{run_id}/findings`。
§7 的四行在这里：管理端列表、管理端 findings、路径参数、鉴权
（路由守卫的清单在 test_admin_customers_api.py）。
另有：分页越界 422、只读（只发 SELECT、不提交、不写审计）。

运行与 findings 直接用模型插入，不经核对任务；
客户由 test_usage_billing.World 经真实的建客户服务写入。
两库参数化：SQLite 用 `create_all` 建表（StaticPool：TestClient 在线程池里跑同步端点）；
MySQL 那一半需要 `BILLING_TEST_DATABASE_URL`，没设时 skip ——
**不要把 skipped 读成 passed**，CI 里不许 skip。
MySQL 库是共用的：核对表前后清空（test_wallet_reconciliation_start._mysql）。

示例 uuid 用全零占位值（只有末几位不同）。
⚠️ SQL 文本只用来判断语句种类，不写死方言字面量；
只有 MySQL 才成立的断言（`DECIMAL(20,8)` 逐字往返）放在 MySQL 分支里。
"""

from __future__ import annotations

import base64
import datetime as dt
import itertools
import os
from collections.abc import Iterator
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, event, func, insert, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from test_admin_customers_api import ADMIN_ROUTES, EXPECTED_ADMIN_ROUTES
from test_usage_billing import T0, TEST_EMAIL_DOMAIN, Tenancy, seed_meter_types
from test_usage_billing import World as BillingWorld
from test_wallet_reconciliation_start import _mysql, myt, run_values

from app.core.config import Settings
from app.core.database import create_session_factory
from app.core.tokens import issue_access_token
from app.main import create_app
from app.models.auth import AuditLog, User, UserRole, UserStatus
from app.models.base import Base
from app.models.wallet_reconciliation import (
    ReconciliationRunStatus,
    WalletReconciliationFinding,
    WalletReconciliationRun,
)
from app.services.auth import utc_now

Run = WalletReconciliationRun
Finding = WalletReconciliationFinding
RUNNING = ReconciliationRunStatus.RUNNING
COMPLETED = ReconciliationRunStatus.COMPLETED
ABANDONED = ReconciliationRunStatus.ABANDONED

RUNS = "/api/v1/admin/wallet-reconciliation/runs"
FINDINGS_ROUTE = RUNS + "/{run_id}/findings"
ZERO_UUID = "00000000-0000-0000-0000-000000000000"
DAY = dt.timedelta(days=1)
FIRST_DAY = dt.date(2026, 10, 1)
TODAY = dt.date(2026, 10, 29)

# 设计 §2：分页外壳与现有管理端列表相同；条目恰好 7 个键。多一个（内部 id、游标、租约）就红。
PAGE_FIELDS = {"items", "page", "page_size", "total"}
RUN_FIELDS = {
    "run_id",
    "run_date",
    "status",
    "started_at",
    "finished_at",
    "wallets_checked",
    "wallets_inconsistent",
}
FINDING_FIELDS = {
    "customer_id",
    "company_name",
    "problem_codes",
    "balance",
    "wallet_version",
    "ledger_rows",
    "checked_at",
}

_NUMBERS = itertools.count(1)


def sample_uuid() -> str:
    """Mostly-zero placeholder uuids; all zeros is kept for "no such run"."""
    return f"00000000-0000-0000-0000-{next(_NUMBERS):012d}"


def settings_for(tmp_path) -> Settings:
    jwt, master = tmp_path / "jwt.key", tmp_path / "master.key"
    jwt.write_text("a-test-signing-key-that-is-long-enough", encoding="utf-8")
    master.write_text(f"1:{base64.b64encode(os.urandom(32)).decode()}\n", encoding="utf-8")
    return Settings(jwt_secret_file=str(jwt), master_key_file=str(master), database_url="")


# --- fixtures --------------------------------------------------------------------------


def _sqlite_factory() -> Iterator[sessionmaker[Session]]:
    # StaticPool：TestClient 在线程池里跑同步端点，内存库必须所有连接共用一个。
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    seed_meter_types(factory)
    yield factory
    engine.dispose()


class Console:
    """The app on one database, an admin, and helpers to seed runs and findings directly."""

    def __init__(self, factory: sessionmaker[Session], dialect: str, tmp_path) -> None:
        self.factory = factory
        self.dialect = dialect
        self.app = create_app(settings_for(tmp_path))
        self.app.state.engine = factory.kw["bind"]
        self.app.state.session_factory = factory
        self.client = TestClient(self.app)
        self.world = BillingWorld(factory)
        self.admin = self.token(self.world.admin.id, UserRole.ADMIN)

    # -- callers --

    def token(self, user_id: int, role: UserRole) -> dict[str, str]:
        token = issue_access_token(
            self.app.state.settings,
            user_id=user_id,
            role=role.value,
            session_id="0" * 32,
            now=utc_now(),
        )
        return {"Authorization": f"Bearer {token}"}

    def customer_caller(self) -> dict[str, str]:
        with self.factory() as session:
            user = User(
                # 用 test_usage_billing 的域名：MySQL 清场按它删用户。
                email=f"customer-{self.world.admin.id}{TEST_EMAIL_DOMAIN}",
                password_hash="not-a-real-hash",
                role=UserRole.CUSTOMER,
                status=UserStatus.ACTIVE,
                created_at=T0,
                updated_at=T0,
            )
            session.add(user)
            session.commit()
            return self.token(user.id, UserRole.CUSTOMER)

    # -- seeding --

    def run(
        self,
        run_date: dt.date,
        status: ReconciliationRunStatus,
        *,
        public_id: str | None = None,
        **overrides,
    ) -> tuple[int, str]:
        """(internal id, public id) of a run inserted through the model."""
        public_id = public_id or sample_uuid()
        values = run_values(run_date, status, public_id=public_id, **overrides)
        with self.factory() as session:
            run_id = session.execute(insert(Run).values(**values)).inserted_primary_key[0]
            session.commit()
        return int(run_id), public_id

    def finding(
        self,
        run_id: int,
        tenant: Tenancy,
        codes: str,
        *,
        balance: Decimal | None,
        version: int | None,
        rows: int,
        checked_at: dt.datetime,
    ) -> None:
        with self.factory() as session:
            session.add(
                Finding(
                    run_id=run_id,
                    tenant_id=tenant.id,
                    problem_codes=codes,
                    balance=balance,
                    wallet_version=version,
                    ledger_rows=rows,
                    checked_at=checked_at,
                )
            )
            session.commit()

    # -- reading --

    def get(self, url: str, headers: dict[str, str] | None = None, **params):
        return self.client.get(
            url, params=params, headers=self.admin if headers is None else headers
        )

    def page(self, url: str, fields: set[str], **params) -> dict:
        response = self.get(url, **params)
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert set(data) == PAGE_FIELDS
        assert all(set(item) == fields for item in data["items"])
        return data

    def audit_rows(self) -> int:
        with self.factory() as session:
            return int(session.execute(select(func.count()).select_from(AuditLog)).scalar_one())


def _console(dialect: str, monkeypatch, tmp_path) -> Iterator[Console]:
    factories = _mysql(monkeypatch) if dialect == "mysql" else _sqlite_factory()
    factory = next(factories)
    try:
        yield Console(factory, dialect, tmp_path)
    finally:
        factories.close()


@pytest.fixture(params=["sqlite", "mysql"])
def console(request, monkeypatch, tmp_path) -> Iterator[Console]:
    """The same cases on SQLite and on a real MySQL."""
    yield from _console(request.param, monkeypatch, tmp_path)


@pytest.fixture
def mysql_console(monkeypatch, tmp_path) -> Iterator[Console]:
    yield from _console("mysql", monkeypatch, tmp_path)


def findings_url(run_id: str) -> str:
    return f"{RUNS}/{run_id}/findings"


def error_code(response) -> str | None:
    return (response.json().get("error") or {}).get("code")


# --- 管理端列表 -------------------------------------------------------------------------


def test_runs_are_newest_start_first_ties_by_id_and_paged(console) -> None:
    """25 runs, `page_size` 10; two share a `started_at` (§7「管理端列表」)."""
    starts = [myt(FIRST_DAY + index * DAY, 3, 5) for index in range(25)]
    # 第 12、13 条同一时刻开始：只能靠 id 倒序分先后。
    starts[13] = starts[12]
    # 插入顺序打乱（7 与 25 互质，是一个排列）：id 的顺序不等于开始时刻的顺序。
    inserted: list[tuple[dt.datetime, int, str]] = []
    for index in ((step * 7) % 25 for step in range(25)):
        if index == 24:
            status = RUNNING
        elif index % 5 == 0:
            status = ABANDONED
        else:
            status = COMPLETED
        run_id, public_id = console.run(
            starts[index].date(),
            status,
            started_at=starts[index],
            finished_at=None if status is RUNNING else starts[index] + dt.timedelta(hours=1),
        )
        inserted.append((starts[index], run_id, public_id))
    expected = [public_id for _, _, public_id in sorted(inserted, reverse=True)]

    pages = [console.page(RUNS, RUN_FIELDS, page=number, page_size=10) for number in (1, 2, 3)]

    assert [len(data["items"]) for data in pages] == [10, 10, 5]
    assert {(data["page"], data["page_size"], data["total"]) for data in pages} == {
        (1, 10, 25),
        (2, 10, 25),
        (3, 10, 25),
    }
    assert [item["run_id"] for data in pages for item in data["items"]] == expected
    # 同一时刻的两条：后插入（id 大）的在前。
    tied = [run for run in inserted if run[0] == starts[12]]
    tied_ids = [public_id for _, _, public_id in sorted(tied, key=lambda run: -run[1])]
    listed = [item["run_id"] for data in pages for item in data["items"]]
    assert [run_id for run_id in listed if run_id in tied_ids] == tied_ids
    # 页码超过末页：`items` 为空，`total` 照报。
    beyond = console.page(RUNS, RUN_FIELDS, page=4, page_size=10)
    assert (beyond["items"], beyond["total"]) == ([], 25)


def test_a_run_item_carries_the_seven_values(console) -> None:
    started = myt(TODAY, 3, 5)
    _, done = console.run(
        TODAY,
        COMPLETED,
        started_at=started,
        finished_at=started + dt.timedelta(minutes=7),
        cursor_tenant_id=9,
        wallets_checked=4,
        wallets_inconsistent=1,
    )
    _, going = console.run(
        TODAY,
        RUNNING,
        started_at=started + dt.timedelta(hours=1),
        cursor_tenant_id=2,
        wallets_checked=2,
    )

    items = console.page(RUNS, RUN_FIELDS)["items"]

    assert items == [
        {
            "run_id": going,
            "run_date": "2026-10-29",
            "status": "RUNNING",
            "started_at": "2026-10-28T20:05:00",
            "finished_at": None,
            "wallets_checked": 2,
            "wallets_inconsistent": 0,
        },
        {
            "run_id": done,
            "run_date": "2026-10-29",
            "status": "COMPLETED",
            "started_at": "2026-10-28T19:05:00",
            "finished_at": "2026-10-28T19:12:00",
            "wallets_checked": 4,
            "wallets_inconsistent": 1,
        },
    ]


def test_without_any_run_the_list_is_empty(console) -> None:
    data = console.page(RUNS, RUN_FIELDS)

    assert data == {"items": [], "page": 1, "page_size": 20, "total": 0}


# --- 管理端 findings --------------------------------------------------------------------


def test_findings_are_by_tenant_ascending_with_seven_keys(console) -> None:
    """3 findings, one `WALLET_MISSING`; another run's finding stays out (§7)."""
    first, second, third = (console.world.tenant(name) for name in ("a", "b", "c"))
    run_id, public_id = console.run(
        TODAY, COMPLETED, cursor_tenant_id=third.id, wallets_checked=3, wallets_inconsistent=3
    )
    other_id, _ = console.run(
        TODAY - DAY, ABANDONED, cursor_tenant_id=third.id, wallets_checked=1, wallets_inconsistent=1
    )
    checked = myt(TODAY, 3, 6)
    # 插入顺序与租户顺序相反：排序只能来自 ORDER BY。
    console.finding(
        run_id,
        third,
        "LEDGER_MISSING,BALANCE_NOT_LEDGER_SUM,VERSION_NOT_LAST_SEQUENCE",
        balance=Decimal("12.5"),
        version=4,
        rows=0,
        checked_at=checked + dt.timedelta(seconds=2),
    )
    console.finding(
        run_id,
        second,
        "BALANCE_NOT_LEDGER_SUM",
        balance=Decimal("-3.25"),
        version=2,
        rows=2,
        checked_at=checked + dt.timedelta(seconds=1),
    )
    console.finding(
        run_id, first, "WALLET_MISSING", balance=None, version=None, rows=0, checked_at=checked
    )
    console.finding(
        other_id, first, "CHAIN_BROKEN", balance=Decimal("1"), version=1, rows=1, checked_at=checked
    )

    data = console.page(findings_url(public_id), FINDING_FIELDS)

    assert (data["page"], data["page_size"], data["total"]) == (1, 20, 3)
    assert data["items"] == [
        {
            "customer_id": first.public_id,
            "company_name": "Fictional a Sdn Bhd",
            "problem_codes": ["WALLET_MISSING"],
            "balance": None,
            "wallet_version": None,
            "ledger_rows": 0,
            "checked_at": "2026-10-28T19:06:00",
        },
        {
            "customer_id": second.public_id,
            "company_name": "Fictional b Sdn Bhd",
            "problem_codes": ["BALANCE_NOT_LEDGER_SUM"],
            "balance": "-3.25000000",
            "wallet_version": 2,
            "ledger_rows": 2,
            "checked_at": "2026-10-28T19:06:01",
        },
        {
            "customer_id": third.public_id,
            "company_name": "Fictional c Sdn Bhd",
            "problem_codes": [
                "LEDGER_MISSING",
                "BALANCE_NOT_LEDGER_SUM",
                "VERSION_NOT_LAST_SEQUENCE",
            ],
            "balance": "12.50000000",
            "wallet_version": 4,
            "ledger_rows": 0,
            "checked_at": "2026-10-28T19:06:02",
        },
    ]


def test_findings_are_paged(console) -> None:
    tenants = [console.world.tenant(name) for name in ("a", "b", "c")]
    run_id, public_id = console.run(
        TODAY, COMPLETED, cursor_tenant_id=tenants[-1].id, wallets_checked=3, wallets_inconsistent=3
    )
    for tenant in reversed(tenants):
        console.finding(
            run_id,
            tenant,
            "BALANCE_NOT_LEDGER_SUM",
            balance=Decimal("1"),
            version=1,
            rows=1,
            checked_at=myt(TODAY, 3, 6),
        )

    pages = [
        console.page(findings_url(public_id), FINDING_FIELDS, page=number, page_size=2)
        for number in (1, 2, 3)
    ]

    assert [[item["customer_id"] for item in data["items"]] for data in pages] == [
        [tenants[0].public_id, tenants[1].public_id],
        [tenants[2].public_id],
        [],
    ]
    assert {data["total"] for data in pages} == {3}


def test_a_consistent_run_has_no_findings(console) -> None:
    _, public_id = console.run(TODAY, COMPLETED, wallets_checked=3)

    data = console.page(findings_url(public_id), FINDING_FIELDS)

    assert data == {"items": [], "page": 1, "page_size": 20, "total": 0}


def test_the_balance_is_read_back_digit_for_digit(mysql_console) -> None:
    """MySQL only: `DECIMAL(20,8)` keeps all 20 digits; SQLite does not promise that."""
    console = mysql_console
    tenant = console.world.tenant()
    run_id, public_id = console.run(
        TODAY, COMPLETED, cursor_tenant_id=tenant.id, wallets_checked=1, wallets_inconsistent=1
    )
    exact = Decimal("999999999999.99999999")
    # 有区分力：这个值经 float 往返会变。
    assert Decimal(float(exact)) != exact
    console.finding(
        run_id,
        tenant,
        "LEDGER_MISSING",
        balance=exact,
        version=2,
        rows=0,
        checked_at=myt(TODAY, 3, 6),
    )

    items = console.page(findings_url(public_id), FINDING_FIELDS)["items"]

    assert [item["balance"] for item in items] == ["999999999999.99999999"]


# --- 路径参数 ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "run_id",
    [
        "not-a-uuid",
        "{run_id}",
        ZERO_UUID + "0",
        " " + ZERO_UUID,
        "00000000000000000000000000000000",
    ],
    ids=["not-a-uuid", "literal-placeholder", "too-long", "leading-space", "no-hyphens"],
)
def test_a_malformed_run_id_is_404(console, run_id: str) -> None:
    response = console.get(findings_url(run_id))

    assert (response.status_code, error_code(response)) == (404, "RECONCILIATION_RUN_NOT_FOUND")


def test_an_uppercase_run_id_is_404_even_when_the_run_exists(console) -> None:
    _, public_id = console.run(TODAY, COMPLETED, public_id="00000000-0000-0000-0000-00000000000a")
    assert console.get(findings_url(public_id)).status_code == 200

    response = console.get(findings_url(public_id.upper()))

    assert (response.status_code, error_code(response)) == (404, "RECONCILIATION_RUN_NOT_FOUND")


def test_an_unknown_run_id_is_404(console) -> None:
    console.run(TODAY, COMPLETED)

    response = console.get(findings_url(ZERO_UUID))

    assert (response.status_code, error_code(response)) == (404, "RECONCILIATION_RUN_NOT_FOUND")


# --- 鉴权 -------------------------------------------------------------------------------


@pytest.mark.parametrize("caller", ["anonymous", "customer"])
@pytest.mark.parametrize(
    "url",
    [RUNS, FINDINGS_ROUTE, findings_url(ZERO_UUID), "existing"],
    ids=["runs", "literal-findings", "unknown-findings", "existing-findings"],
)
def test_non_admins_are_refused_before_the_run_id_is_looked_at(
    console, caller: str, url: str
) -> None:
    """匿名 401、CUSTOMER 403；字面路径与不存在的 run_id 同样（§7）."""
    if url == "existing":
        _, public_id = console.run(TODAY, COMPLETED)
        url = findings_url(public_id)
    headers = {} if caller == "anonymous" else console.customer_caller()

    response = console.get(url, headers=headers)

    expected = (401, "TOKEN_INVALID") if caller == "anonymous" else (403, "ADMIN_REQUIRED")
    assert (response.status_code, error_code(response)) == expected


def test_the_route_guard_lists_both_routes() -> None:
    """test_admin_customers_api.py 的守卫逐个验 401 / 403；这里只确认清单里有两个接口。"""
    ours = {("GET", RUNS), ("GET", FINDINGS_ROUTE)}

    assert ours <= set(ADMIN_ROUTES)
    assert ours <= EXPECTED_ADMIN_ROUTES


# --- 分页越界 ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "params",
    [{"page": 0}, {"page": 10_001}, {"page_size": 0}, {"page_size": 101}],
    ids=["page-0", "page-10001", "page-size-0", "page-size-101"],
)
@pytest.mark.parametrize("which", ["runs", "findings"])
def test_out_of_range_paging_is_422_before_auth(console, params: dict, which: str) -> None:
    url = RUNS if which == "runs" else findings_url(ZERO_UUID)

    for headers in (console.admin, {}):
        response = console.get(url, headers=headers, **params)

        assert (response.status_code, error_code(response)) == (422, "VALIDATION_ERROR")


# --- 只读 -------------------------------------------------------------------------------


def test_both_views_only_read(console) -> None:
    """只发 SELECT、不提交、不写审计（设计 §2「管理端接口」、§6「审计」）."""
    tenant = console.world.tenant()
    run_id, public_id = console.run(
        TODAY, COMPLETED, cursor_tenant_id=tenant.id, wallets_checked=1, wallets_inconsistent=1
    )
    console.finding(
        run_id,
        tenant,
        "WALLET_MISSING",
        balance=None,
        version=None,
        rows=0,
        checked_at=myt(TODAY, 3, 6),
    )
    audits = console.audit_rows()
    engine = console.factory.kw["bind"]
    assert isinstance(engine, Engine)
    statements: list[str] = []
    commits: list[bool] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:
        statements.append(statement)

    def committed(conn) -> None:
        commits.append(True)

    event.listen(engine, "before_cursor_execute", record)
    event.listen(engine, "commit", committed)
    try:
        assert console.get(RUNS).status_code == 200
        assert console.get(findings_url(public_id)).status_code == 200
        assert console.get(findings_url(ZERO_UUID)).status_code == 404
    finally:
        event.remove(engine, "before_cursor_execute", record)
        event.remove(engine, "commit", committed)

    assert statements
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    assert commits == []
    assert console.audit_rows() == audits
