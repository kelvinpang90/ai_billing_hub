"""Customer wallet ledger CSV export end to end (AIH-TASK-116, design gate #435 v10 §7).

`GET /api/v1/customer/wallet/transactions/export`：必填的马来西亚日期区间按流水的
`created_at`（记账时间）取，按 `wallet_sequence` 倒序；查询是
`list_transactions_for_customer_export`，投影复用 #393 的 7 键白名单
（`customer_ledger_line_view`），用 AIH-TASK-114 的 CSV 模块、导出租约与
`ExportCsvResponse`。

§7 钱包导出表的每一行都在这里；「鉴权」的路由守卫在 test_customer_api.py。CSV 格式、
租约与中间件本身的 unit 行在 test_csv_export.py，经真实中间件栈与真实 uvicorn 的行
在 test_customer_usage_export_api.py（两个导出接口共用同一个租约与响应类）。

账本行由真实的 `post_transaction` 写入，`created_at` 取各用例给的时刻；
「扫描上限」的 30 000 笔流水直接批量插入：序号、前后余额逐行接上，
MySQL 的账本触发器照常校验。
- SQLite：`create_all` 建表，再装上 test_usage_billing 那条代替触发器推进钱包的测试触发器；
- MySQL：表与触发器都走迁移，需要 `BILLING_TEST_DATABASE_URL`，没设时 MySQL 那一半
  skip —— **不要把 skipped 读成 passed**，CI 里不许 skip。

⚠️ 导出租约是进程级的：每条用例开始与结束时都必须空闲（`lease_is_free`）。

⚠️ SQL 文本断言不写死方言字面量；只有 MySQL 才有的断言放在方言分支里。
示例 uuid 用全零占位值。
"""

from __future__ import annotations

import datetime as dt
import functools
import itertools
import json
import re
import uuid
from collections.abc import Iterator
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, insert, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from test_customer_usage_api import record_statements
from test_customer_usage_export_api import (
    BOM,
    assert_generic_500,
    messages,
    read_csv,
    server_log,
)
from test_customer_wallet_api import (
    SENTINEL_DESCRIPTION,
    SENTINEL_IDEMPOTENCY_KEY,
    Portal,
    _portal,
    _sqlite_factory,
    error_of,
)
from test_usage_billing import Tenancy, _mysql_factory

from app.api import customer as customer_api
from app.core.csv_export import (
    EXPORT_MAX_ROWS,
    export_lease_held,
    release_export_lease,
    try_acquire_export_lease,
)
from app.models.tenancy import AccountStatus
from app.models.wallet import (
    ADJUSTMENT_REFERENCE_TYPES,
    REFERENCE_TYPE_FOR,
    ReferenceType,
    TransactionType,
    Wallet,
    WalletTransaction,
)
from app.repositories import usage_events as usage_repository
from app.repositories import wallet as wallet_repository
from app.repositories.wallet import (
    customer_ledger_export_statement,
    list_transactions_for_customer_export,
)
from app.schemas.customer_wallet import WALLET_EXPORT_COLUMNS, WALLET_EXPORT_TEXT_COLUMNS

EXPORT = "/api/v1/customer/wallet/transactions/export"
USAGE_EXPORT = "/api/v1/customer/usage-events/export"
DAY = "2026-10-01"
PERIOD = {"from_date": DAY, "to_date": DAY}
FILENAME = f'attachment; filename="wallet-transactions_{DAY}_{DAY}.csv"'
# 马来西亚 2026-10-01 10:00（UTC 02:00）。整秒：MySQL 的 `DATETIME` 不存微秒。
ON_DAY = dt.datetime(2026, 10, 1, 2, 0, 0)
MINUTE = dt.timedelta(minutes=1)
EVENT_A = "00000000-0000-7000-8000-000000000001"
EVENT_B = "00000000-0000-7000-8000-000000000002"

# 设计 §2「接口二」：表头恰好这 7 列，按这个顺序。多一列、少一列、换顺序都红。
COLUMNS = [
    "id",
    "wallet_sequence",
    "transaction_type",
    "amount",
    "balance_after",
    "event_id",
    "created_at_utc",
]
TEXT_COLUMNS = {"id", "transaction_type", "event_id"}

_NUMBERS = itertools.count(1)


@pytest.fixture(autouse=True)
def lease_is_free() -> Iterator[None]:
    # 进程级租约：每条用例开始与结束时都必须空闲，用例之间不互相挡住。
    assert not export_lease_held()
    yield
    assert not export_lease_held()


@pytest.fixture
def portal(tmp_path) -> Iterator[Portal]:
    yield from _portal(_sqlite_factory(), tmp_path)


@pytest.fixture(params=["sqlite", "mysql"])
def both(request, monkeypatch, tmp_path) -> Iterator[Portal]:
    """The same cases on SQLite and on a real MySQL (design §7「SQLite 与 MySQL 参数化」)."""
    factories: Iterator[sessionmaker[Session]] = (
        _mysql_factory(monkeypatch) if request.param == "mysql" else _sqlite_factory()
    )
    yield from _portal(factories, tmp_path)


@pytest.fixture
def mysql_portal(monkeypatch, tmp_path) -> Iterator[Portal]:
    yield from _portal(_mysql_factory(monkeypatch), tmp_path)


# --- 辅助 -------------------------------------------------------------------------------


def post_at(
    portal: Portal,
    tenant: Tenancy,
    kind: TransactionType,
    amount: str,
    *,
    at: dt.datetime,
    reference_id: str | None = None,
    description: str | None = None,
) -> str:
    """One posting through the real repository, booked at `at` (naive UTC); its `public_id`.

    调账由管理员记、带原因；系统更正带原因；其余来源由系统记。
    """
    source = REFERENCE_TYPE_FOR[kind]
    by_admin = source is ReferenceType.ADMIN_ADJUSTMENT
    if description is None and source in ADJUSTMENT_REFERENCE_TYPES:
        description = "Fictional adjustment"
    reference = reference_id or f"{kind.value.lower()}-export-{next(_NUMBERS)}"
    with portal.factory() as session:
        result = wallet_repository.post_transaction(
            session,
            tenant_id=tenant.id,
            transaction_type=kind,
            amount=Decimal(amount),
            reference_type=source,
            reference_id=reference,
            now=at,
            created_by=portal.world.admin.id if by_admin else None,
            description=description,
            actor_role="ADMIN" if by_admin else None,
        )
        public_id = result.transaction.public_id
        session.commit()
    return public_id


def bulk(portal: Portal, tenant: Tenancy, count: int, start: dt.datetime) -> None:
    """`count` AI usage rows of −0.01, one second apart from `start`, inserted in batches.

    序号与前后余额逐行接上钱包当前状态：MySQL 的 BEFORE INSERT 触发器逐行校验，
    AFTER INSERT 触发器推进钱包。
    """
    step = Decimal("-0.01")
    with portal.factory() as session:
        wallet = session.execute(select(Wallet).where(Wallet.tenant_id == tenant.id)).scalar_one()
        wallet_id, version, balance = wallet.id, wallet.version, wallet.balance
        batch: list[dict[str, object]] = []
        for offset in range(count):
            sequence = version + offset + 1
            before = balance + step * offset
            batch.append(
                {
                    "public_id": str(uuid.uuid4()),
                    "wallet_id": wallet_id,
                    "tenant_id": tenant.id,
                    "wallet_sequence": sequence,
                    "transaction_type": TransactionType.AI_USAGE,
                    "amount": step,
                    "balance_before": before,
                    "balance_after": before + step,
                    "reference_type": ReferenceType.USAGE_EVENT,
                    "reference_id": f"bulk-{tenant.id}-{sequence}",
                    "created_at": start + dt.timedelta(seconds=offset),
                }
            )
            if len(batch) == 1000:
                session.execute(insert(WalletTransaction), batch)
                batch = []
        if batch:
            session.execute(insert(WalletTransaction), batch)
        session.commit()


def export(portal: Portal, headers: dict[str, str], **params):
    return portal.client.get(EXPORT, params=params, headers=headers)


def exported(portal: Portal, headers: dict[str, str], **params) -> list[list[str]]:
    """The data rows of a 200 export; the header row is checked here."""
    response = export(portal, headers, **params)
    assert response.status_code == 200, response.text
    table = read_csv(response.content)
    assert table[0] == COLUMNS
    return table[1:]


def cells_of(item: dict) -> list[str]:
    """What one JSON item becomes in the file (for values that need no formula prefix)."""
    values = {**item, "created_at_utc": item["created_at"]}
    return ["" if values[name] is None else str(values[name]) for name in COLUMNS]


def column(rows: list[list[str]], name: str) -> list[str]:
    index = COLUMNS.index(name)
    return [row[index] for row in rows]


def ledger_statements(statements: list[str]) -> list[str]:
    table = re.compile(r"\bwallet_transactions\b", re.IGNORECASE)
    return [statement for statement in statements if table.search(statement)]


def four_rows(portal: Portal) -> tuple[Tenancy, dict[str, str]]:
    """A credit, two AI usage debits and a debit adjustment on `DAY`; the signed-in headers."""
    tenant = portal.tenant()
    post_at(portal, tenant, TransactionType.ADJUSTMENT_CREDIT, "100", at=ON_DAY)
    post_at(
        portal,
        tenant,
        TransactionType.AI_USAGE,
        "-0.0625",
        at=ON_DAY + MINUTE,
        reference_id=EVENT_A,
    )
    post_at(
        portal,
        tenant,
        TransactionType.AI_USAGE,
        "-1.5",
        at=ON_DAY + 2 * MINUTE,
        reference_id=EVENT_B,
    )
    post_at(portal, tenant, TransactionType.ADJUSTMENT_DEBIT, "-2", at=ON_DAY + 3 * MINUTE)
    return tenant, portal.customer(tenant)


# --- 正常路径 ---------------------------------------------------------------------------


def test_the_export_matches_the_ledger_endpoint_row_for_row(both: Portal) -> None:
    """§7「正常路径」：充值（调账贷方）、两笔用量扣费、一笔调账借方。

    200、表头恰好 7 列、4 行按 `wallet_sequence` 倒序；每行与
    `GET /customer/wallet/transactions` 的同名键逐字相等；用量行 `event_id` 有值，调账行为空。
    """
    tenant, headers = four_rows(both)
    before = both.footprint()

    response = export(both, headers, **PERIOD)

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert response.headers["content-disposition"] == FILENAME
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["content-length"] == str(len(response.content))
    assert response.content.startswith(BOM)
    assert response.content.endswith(b"\r\n")
    table = read_csv(response.content)
    assert table[0] == COLUMNS
    rows = table[1:]
    assert column(rows, "wallet_sequence") == ["4", "3", "2", "1"]
    assert rows == [cells_of(item) for item in both.walk(headers, page_size=100)]
    assert column(rows, "transaction_type") == [
        "ADJUSTMENT_DEBIT",
        "AI_USAGE",
        "AI_USAGE",
        "ADJUSTMENT_CREDIT",
    ]
    assert column(rows, "event_id") == ["", EVENT_B, EVENT_A, ""]
    assert column(rows, "amount") == ["-2.00000000", "-1.50000000", "-0.06250000", "100.00000000"]
    assert column(rows, "balance_after") == [
        "96.43750000",
        "98.43750000",
        "99.93750000",
        "100.00000000",
    ]
    assert rows[3][COLUMNS.index("created_at_utc")] == "2026-10-01T02:00:00"
    # 只读：调用前后账本、钱包、租户、审计、outbox 的行数与每个钱包的余额、版本不变。
    assert both.footprint() == before
    assert tenant.public_id.encode() not in response.content


def test_the_column_lists_are_the_designed_ones() -> None:
    """§2「接口二」：列清单与文本列就是设计写的那些（INV-7：列清单固定）。"""
    assert list(WALLET_EXPORT_COLUMNS) == COLUMNS
    assert set(WALLET_EXPORT_TEXT_COLUMNS) == TEXT_COLUMNS


def test_rows_are_taken_by_their_malaysian_booking_day(both: Portal) -> None:
    """§7「按记账日期取」：三笔流水的 `created_at`（UTC 写库）。

    分别在马来西亚 1 日 23:59、2 日 00:00、2 日 23:59。导出 2 日..2 日恰好后两笔；
    3 日 00:00 的那笔不在里面，1 日..1 日只有第一笔。
    """
    tenant = both.tenant()
    late_first = post_at(
        both,
        tenant,
        TransactionType.ADJUSTMENT_CREDIT,
        "10",
        at=dt.datetime(2026, 10, 1, 15, 59, 59),
    )
    start_second = post_at(
        both, tenant, TransactionType.ADJUSTMENT_CREDIT, "10", at=dt.datetime(2026, 10, 1, 16, 0, 0)
    )
    late_second = post_at(
        both,
        tenant,
        TransactionType.ADJUSTMENT_CREDIT,
        "10",
        at=dt.datetime(2026, 10, 2, 15, 59, 59),
    )
    post_at(
        both, tenant, TransactionType.ADJUSTMENT_CREDIT, "10", at=dt.datetime(2026, 10, 2, 16, 0, 0)
    )
    headers = both.customer(tenant)

    second_day = exported(both, headers, from_date="2026-10-02", to_date="2026-10-02")
    first_day = exported(both, headers, from_date="2026-10-01", to_date="2026-10-01")

    assert column(second_day, "id") == [late_second, start_second]
    assert column(second_day, "created_at_utc") == ["2026-10-02T15:59:59", "2026-10-01T16:00:00"]
    assert column(first_day, "id") == [late_first]


def test_an_empty_period_is_the_header_only(both: Portal) -> None:
    """§2「接口二」：区间内没有流水 → 200，只有 BOM 与表头一行。"""
    _, headers = four_rows(both)
    header_only = BOM + (",".join(COLUMNS) + "\r\n").encode("ascii")

    response = export(both, headers, from_date="2026-10-02", to_date="2026-10-31")

    assert response.status_code == 200
    assert response.content == header_only
    assert response.headers["content-disposition"] == (
        'attachment; filename="wallet-transactions_2026-10-02_2026-10-31.csv"'
    )


# --- 区间边界 ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("params", "fields"),
    [
        ({"from_date": "2026-07-01", "to_date": "2026-10-01"}, "query.from_date, query.to_date"),
        ({"from_date": DAY}, "query.to_date"),
        ({"to_date": DAY}, "query.from_date"),
        ({}, "query.from_date, query.to_date"),
        ({"from_date": "2026-10-02", "to_date": DAY}, "query.from_date, query.to_date"),
        ({"from_date": "0001-01-01", "to_date": "0001-01-02"}, "query.from_date"),
        ({"from_date": "2026-02-30", "to_date": "2026-03-01"}, "query.from_date"),
        ({"from_date": "2026/10/01", "to_date": DAY}, "query.from_date"),
    ],
)
def test_the_period_rules_are_the_usage_exports(portal: Portal, params: dict, fields: str) -> None:
    """§7「区间边界」：区间 93 天、缺 `to_date`、`to_date` 早于 `from_date` 等 → 422。

    与用量导出同样的参数得到同样的 422 与文案，不回显值。
    """
    tenant = portal.tenant()
    post_at(portal, tenant, TransactionType.ADJUSTMENT_CREDIT, "10", at=ON_DAY)
    headers = portal.customer(tenant)

    response = export(portal, headers, **params)
    usage = portal.client.get(USAGE_EXPORT, params=params, headers=headers)

    assert error_of(response) == (422, "VALIDATION_ERROR")
    body = response.json()
    assert body["error"]["message"] == f"Invalid request fields: {fields}"
    assert body == {**usage.json(), "request_id": body["request_id"]}
    assert body["data"] is None
    for value in params.values():
        if value != DAY:
            assert value not in body["error"]["message"]


def test_unknown_parameters_are_ignored(portal: Portal) -> None:
    """§2「接口二」：只有日期两个参数；分页、来源筛选与租户标识给了按未知参数忽略。"""
    _, headers = four_rows(portal)
    other = portal.tenant()

    rows = exported(
        portal,
        headers,
        **PERIOD,
        page_size="0",
        before_sequence="2",
        reference_type="ADMIN_ADJUSTMENT",
        customer_id=other.public_id,
    )

    assert column(rows, "wallet_sequence") == ["4", "3", "2", "1"]
    assert column(rows, "id") == [item["id"] for item in portal.walk(headers, page_size=100)]


# --- 上限 -------------------------------------------------------------------------------


def test_more_rows_than_the_cap_is_export_too_large_without_any_row(
    both: Portal, monkeypatch
) -> None:
    """§7「上限」：上限 monkeypatch 成 3。

    4 笔 → 422 `EXPORT_TOO_LARGE`，响应是 JSON 信封、没有 CSV 行；3 笔 → 200、3 行。
    """
    monkeypatch.setattr(customer_api, "EXPORT_MAX_ROWS", 3)
    four = both.tenant()
    four_ids = [
        post_at(both, four, TransactionType.ADJUSTMENT_CREDIT, "1", at=ON_DAY + n * MINUTE)
        for n in range(4)
    ]
    three = both.tenant()
    for number in range(3):
        post_at(both, three, TransactionType.ADJUSTMENT_CREDIT, "1", at=ON_DAY + number * MINUTE)
    four_headers = both.customer(four)
    three_headers = both.customer(three)

    too_large = export(both, four_headers, **PERIOD)
    fits = exported(both, three_headers, **PERIOD)

    assert error_of(too_large) == (422, "EXPORT_TOO_LARGE")
    assert too_large.headers["content-type"].startswith("application/json")
    assert "content-disposition" not in too_large.headers
    assert not too_large.content.startswith(BOM)
    body = too_large.json()
    assert body["data"] is None
    assert body["request_id"]
    assert "20,000" in body["error"]["message"]
    for public_id in four_ids:
        assert public_id not in too_large.text
    assert len(fits) == 3


def test_the_query_asks_for_one_row_more_than_the_cap(portal: Portal, monkeypatch) -> None:
    """§2「接口二」：`limit = EXPORT_MAX_ROWS + 1`，`EXPORT_MAX_ROWS` 是 20 000。"""
    _, headers = four_rows(portal)
    limits: list[int] = []
    original = wallet_repository.list_transactions_for_customer_export

    def spy(*args, **kwargs):
        limits.append(kwargs["limit"])
        return original(*args, **kwargs)

    monkeypatch.setattr(wallet_repository, "list_transactions_for_customer_export", spy)

    assert len(exported(portal, headers, **PERIOD)) == 4
    assert EXPORT_MAX_ROWS == 20_000
    assert limits == [EXPORT_MAX_ROWS + 1]


def test_a_scan_past_the_budget_is_query_too_broad(mysql_portal: Portal, monkeypatch) -> None:
    """§7「扫描上限」（MySQL）：30 000 笔流水，测试专用 1 毫秒超时。

    422 `QUERY_TOO_BROAD`；一条 `customer_usage_query_timeout` 日志，
    只带 `request_id` 与客户 id。
    """
    portal = mysql_portal
    tenant = portal.tenant()
    bulk(portal, tenant, 30_000, dt.datetime(2026, 9, 30, 16, 0, 0))
    headers = portal.customer(tenant)
    monkeypatch.setattr(
        wallet_repository,
        "list_transactions_for_customer_export",
        functools.partial(list_transactions_for_customer_export, timeout_ms=1),
    )

    with server_log() as lines:
        response = export(portal, headers, from_date="2026-10-01", to_date="2026-10-31")

    assert error_of(response) == (422, "QUERY_TOO_BROAD")
    body = response.json()
    assert body["data"] is None
    assert body["request_id"]
    timeouts = messages(lines, "customer_usage_query_timeout")
    assert len(timeouts) == 1
    assert timeouts[0]["request_id"] == body["request_id"]
    assert timeouts[0]["customer_id"] == tenant.public_id
    assert "2026-10-31" not in json.dumps(lines)
    assert not messages(lines, "customer_export_failed")


def test_query_too_broad_keeps_its_422_and_its_warning(portal: Portal, monkeypatch) -> None:
    """`QueryTooBroad` 照旧转换（与用量导出相同），不被当成意外错误变成 500。"""
    _, headers = four_rows(portal)

    def too_broad(*_args, **_kwargs):
        raise usage_repository.QueryTooBroad

    monkeypatch.setattr(wallet_repository, "list_transactions_for_customer_export", too_broad)
    with server_log() as lines:
        response = export(portal, headers, from_date="2026-10-01", to_date="2026-10-29")

    assert error_of(response) == (422, "QUERY_TOO_BROAD")
    assert len(messages(lines, "customer_usage_query_timeout")) == 1
    assert not messages(lines, "customer_export_failed")
    assert "2026-10-29" not in json.dumps(lines)


# --- 不读敏感列 -------------------------------------------------------------------------


def test_the_export_select_reads_no_sensitive_column() -> None:
    """§7「不读敏感列」：导出的 SELECT 不含 `users` 表、`description`、`metadata_json`、
    `balance_before`（只断言方言无关的片段）；租户与区间条件恒在、按序号倒序、不加锁。
    """
    statement = customer_ledger_export_statement(
        1,
        created_from=dt.datetime(2026, 9, 30, 16, 0, 0),
        created_to=dt.datetime(2026, 10, 1, 16, 0, 0),
        limit=EXPORT_MAX_ROWS + 1,
    )
    sql = " ".join(str(statement.compile()).lower().split())

    assert not re.search(r"\busers\b", sql)
    for fragment in ("description", "metadata_json", "balance_before", "join", "for update"):
        assert fragment not in sql, fragment
    for fragment in (
        "wallet_transactions.tenant_id =",
        "wallet_transactions.created_at >=",
        "wallet_transactions.created_at <",
        "order by wallet_transactions.wallet_sequence desc",
        "limit",
    ):
        assert fragment in sql, fragment
    selected = sql.split(" from ", 1)[0]
    assert selected.count("wallet_transactions.") == 8


def test_the_executed_export_select_is_the_bounded_one(both: Portal) -> None:
    """同一条 SELECT 在真实库上：只有一条读流水的语句，不含敏感列。

    执行预算的提示只在 MySQL 上渲染（方言分支）。
    """
    _, headers = four_rows(both)
    engine = both.factory.kw["bind"]
    statements, record = record_statements(engine)
    try:
        assert len(exported(both, headers, **PERIOD)) == 4
    finally:
        event.remove(engine, "before_cursor_execute", record)

    [statement] = ledger_statements(statements)
    lowered = statement.lower()
    assert not re.search(r"\busers\b", lowered)
    for fragment in ("description", "metadata_json", "balance_before", "join", "for update"):
        assert fragment not in lowered, fragment
    if engine.dialect.name == "mysql":
        assert "/*+ MAX_EXECUTION_TIME(2000) */" in statement
    else:
        assert "MAX_EXECUTION_TIME" not in statement


# --- 泄漏、金额、公式注入 ---------------------------------------------------------------


def test_no_internal_note_operator_or_idempotency_key_reaches_the_file(portal: Portal) -> None:
    """§7「泄漏」（INV-7）：调账带 `description` 哨兵串、操作者是管理员、幂等键已知。

    文件里不出现哨兵串、管理员邮箱、幂等键，也没有来源类型与 `balance_before`。
    """
    tenant = portal.tenant()
    post_at(
        portal,
        tenant,
        TransactionType.ADJUSTMENT_CREDIT,
        "50",
        at=ON_DAY,
        description=SENTINEL_DESCRIPTION,
        reference_id=SENTINEL_IDEMPOTENCY_KEY,
    )
    post_at(
        portal,
        tenant,
        TransactionType.SYSTEM_CORRECTION,
        "-0.5",
        at=ON_DAY + MINUTE,
        description=SENTINEL_DESCRIPTION,
        reference_id="system-correction-sentinel-0000",
    )
    post_at(
        portal, tenant, TransactionType.AI_USAGE, "-1", at=ON_DAY + 2 * MINUTE, reference_id=EVENT_A
    )
    headers = portal.customer(tenant)

    response = export(portal, headers, **PERIOD)

    assert response.status_code == 200
    rows = read_csv(response.content)[1:]
    assert column(rows, "event_id") == [EVENT_A, "", ""]
    for value in (
        SENTINEL_DESCRIPTION,
        SENTINEL_IDEMPOTENCY_KEY,
        "system-correction-sentinel-0000",
        portal.world.admin.email,
        "ADMIN_ADJUSTMENT",
        "USAGE_EVENT",
        "description",
        "reference",
        "balance_before",
        "created_by",
        "metadata",
    ):
        assert value.encode() not in response.content, value


def test_amounts_are_written_exactly(both: Portal) -> None:
    """§7「金额精度」（INV-10）：余额到上界的一笔，`amount`、`balance_after` 原样。

    ⚠️ 上界只在 MySQL 上断言：SQLite 把 NUMERIC 存成浮点，20 位有效数字在库里就变了。
    """
    if both.factory.kw["bind"].dialect.name == "mysql":
        top, top_less_one = "999999999999.99999999", "999999999999.99999998"
    else:
        top, top_less_one = "1234567.12345678", "1234567.12345677"
    tenant = both.tenant()
    post_at(both, tenant, TransactionType.ADJUSTMENT_CREDIT, top, at=ON_DAY)
    post_at(both, tenant, TransactionType.AI_USAGE, "-0.00000001", at=ON_DAY + MINUTE)
    headers = both.customer(tenant)

    response = export(both, headers, **PERIOD)

    rows = read_csv(response.content)[1:]
    assert [
        (row[COLUMNS.index("amount")], row[COLUMNS.index("balance_after")]) for row in rows
    ] == [
        ("-0.00000001", top_less_one),
        (top, top),
    ]
    assert f",{top},{top},".encode() in response.content
    assert b",-0.00000001," in response.content


def test_formula_prefixes_are_quoted_in_text_columns_only(both: Portal) -> None:
    """§2「CSV 格式」：文本列 `event_id` 以 `=` 开头时加 `'`。

    负金额与其他数值、时间列不加。
    """
    tenant = both.tenant()
    post_at(both, tenant, TransactionType.AI_USAGE, "-1", at=ON_DAY, reference_id="=1+1")
    headers = both.customer(tenant)

    [row] = exported(both, headers, **PERIOD)

    assert row[COLUMNS.index("event_id")] == "'=1+1"
    assert row[COLUMNS.index("amount")] == "-1.00000000"
    assert row[COLUMNS.index("balance_after")] == "-1.00000000"
    for name in set(COLUMNS) - {"event_id"}:
        assert not row[COLUMNS.index(name)].startswith("'"), name


# --- 缺钱包、租户越权、只读 -------------------------------------------------------------


def test_a_missing_wallet_is_a_500_before_the_lease(portal: Portal, monkeypatch) -> None:
    """§7「缺钱包」（SQLite，照流水接口用例的写法删钱包行）：500 `INTERNAL_ERROR`。

    契约勘误 ①：存在性检查在获取租约之前，照 #393 抛 `RuntimeError(WALLET_MISSING)`，
    不经 `CustomerExportFailed`、不记 `customer_export_failed`、不获取租约。
    """
    tenant = portal.tenant()
    stored = post_at(portal, tenant, TransactionType.ADJUSTMENT_CREDIT, "7.5", at=ON_DAY)
    headers = portal.customer(tenant)
    with portal.factory() as session:
        session.execute(delete(Wallet).where(Wallet.tenant_id == tenant.id))
        session.commit()
    acquired: list[object] = []

    def spy():
        acquired.append(True)
        return try_acquire_export_lease()

    monkeypatch.setattr(customer_api, "try_acquire_export_lease", spy)

    # 默认的 TestClient 把服务端异常原样抛出来：看得到处理函数抛的是什么、消息是什么。
    with server_log() as lines:
        with pytest.raises((RuntimeError, ExceptionGroup)) as raised:
            export(portal, headers, **PERIOD)
        response = TestClient(portal.app, raise_server_exceptions=False).get(
            EXPORT, params=PERIOD, headers=headers
        )

    error = raised.value
    while isinstance(error, BaseExceptionGroup):
        error = error.exceptions[0]
    assert type(error) is RuntimeError
    assert str(error) == "WALLET_MISSING"
    assert error_of(response) == (500, "INTERNAL_ERROR")
    assert response.json()["data"] is None
    assert response.json()["request_id"]
    assert acquired == []
    assert not messages(lines, "customer_export_failed")
    for leaked in (tenant.public_id, stored, "7.50000000"):
        assert leaked not in response.text


def test_a_customer_only_ever_exports_its_own_ledger(both: Portal) -> None:
    """§7「租户越权」（INV-8）：A、B 各有流水；A 带 B 的标识导出，文件里只有 A 的行。"""
    mine = both.tenant()
    theirs = both.tenant()
    mine_ids = [
        post_at(both, mine, TransactionType.ADJUSTMENT_CREDIT, "1", at=ON_DAY + n * MINUTE)
        for n in range(2)
    ]
    their_ids = [
        post_at(both, theirs, TransactionType.ADJUSTMENT_CREDIT, "3", at=ON_DAY + n * MINUTE)
        for n in range(3)
    ]
    post_at(
        both, theirs, TransactionType.AI_USAGE, "-1", at=ON_DAY + 4 * MINUTE, reference_id=EVENT_B
    )
    headers = both.customer(mine)
    forged = {"customer_id": theirs.public_id, "tenant_id": str(theirs.id)}

    response = export(both, headers, **PERIOD, **forged)

    assert response.status_code == 200
    rows = read_csv(response.content)[1:]
    assert column(rows, "id") == list(reversed(mine_ids))
    assert set(column(rows, "amount")) == {"1.00000000"}
    for leaked in (theirs.public_id, EVENT_B, "3.00000000", *their_ids):
        assert leaked.encode() not in response.content


@pytest.mark.parametrize("status", [AccountStatus.DISABLED, AccountStatus.CLOSED])
def test_a_read_only_customer_can_export(both: Portal, status: AccountStatus) -> None:
    """§6「鉴权主体」：只读客户可以导出，导出是读。"""
    tenant = both.tenant()
    stored = post_at(both, tenant, TransactionType.ADJUSTMENT_CREDIT, "5", at=ON_DAY)
    both.set_account_status(tenant, status)
    headers = both.customer(tenant)

    assert column(exported(both, headers, **PERIOD), "id") == [stored]


# --- 并发上限、意外错误、数据库断连 -----------------------------------------------------


def test_a_held_lease_is_export_busy_without_any_ledger_query(both: Portal) -> None:
    """§7「并发上限」（接口部分）：占住租约时 503 `EXPORT_BUSY`、`Retry-After: 5`。

    钱包存在性检查照常（早于租约），但没有发出读流水的查询；释放后照常 200。
    """
    _, headers = four_rows(both)
    engine = both.factory.kw["bind"]
    holder = try_acquire_export_lease()
    assert holder is not None
    statements, record = record_statements(engine)
    try:
        busy = export(both, headers, **PERIOD)
    finally:
        event.remove(engine, "before_cursor_execute", record)
        release_export_lease(holder)

    assert error_of(busy) == (503, "EXPORT_BUSY")
    assert busy.headers["retry-after"] == "5"
    assert busy.json()["data"] is None
    assert busy.json()["request_id"]
    assert statements, "监听落空"
    wallets = re.compile(r"\bwallets\b", re.IGNORECASE)
    assert [statement for statement in statements if wallets.search(statement)]
    assert ledger_statements(statements) == []
    assert len(exported(both, headers, **PERIOD)) == 4


def test_a_failure_while_projecting_frees_the_lease_for_the_next_export(
    both: Portal, monkeypatch
) -> None:
    """§7「并发上限」：处理中抛错（投影函数在第 2 行抛异常）。

    之后租约已释放、下一次导出 200。
    """
    _, headers = four_rows(both)
    original = customer_api.customer_wallet_export_cells
    seen = [0]

    def projection(row):
        seen[0] += 1
        if seen[0] == 2:
            raise RuntimeError("fictional projection failure")
        return original(row)

    with monkeypatch.context() as patch, server_log() as lines:
        patch.setattr(customer_api, "customer_wallet_export_cells", projection)
        failed = export(both, headers, **PERIOD)

    assert_generic_500(failed.status_code, failed.json())
    assert "fictional projection failure" not in failed.text
    assert len(messages(lines, "customer_export_failed")) == 1
    assert not messages(lines, "Unhandled exception")
    assert not export_lease_held()
    assert len(exported(both, headers, **PERIOD)) == 4


def test_a_lost_database_connection_is_a_500_sent_inside_the_lease(
    both: Portal, monkeypatch
) -> None:
    """§7「数据库断连（钱包导出）」：钱包检查通过、拿到租约之后流水查询断连。

    流水查询抛 `OperationalError` → 500 `INTERNAL_ERROR`（不是 503）、带 `request_id`；
    由内层处理器在租约内发出：有 `customer_export_failed`，没有 `Unhandled exception`
    （默认的 `TestClient` 遇到未处理的异常会直接抛出）；之后租约空闲，下一次导出 200。
    """
    _, headers = four_rows(both)

    def disconnected(*_args, **_kwargs):
        assert export_lease_held()
        raise OperationalError("SELECT", {}, Exception("fictional lost connection"))

    with monkeypatch.context() as patch, server_log() as lines:
        patch.setattr(wallet_repository, "list_transactions_for_customer_export", disconnected)
        response = export(both, headers, from_date="2026-10-01", to_date="2026-10-29")

    body = response.json()
    assert_generic_500(response.status_code, body)
    assert "fictional lost connection" not in response.text
    failures = messages(lines, "customer_export_failed")
    assert len(failures) == 1
    assert failures[0]["level"] == "ERROR"
    assert failures[0]["request_id"] == body["request_id"]
    assert "OperationalError" in failures[0]["exception"]
    assert not messages(lines, "Unhandled exception")
    assert "2026-10-29" not in json.dumps(lines)
    assert not export_lease_held()
    assert len(exported(both, headers, **PERIOD)) == 4
