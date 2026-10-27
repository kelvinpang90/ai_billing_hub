"""Customer conversation list end to end (AIH-TASK-121, design gate #460 v2 §7, 接口一).

`GET /api/v1/customer/conversations`：只读、租户只取自会话。
§7 后端表格里属于接口二（导出）的行在 AIH-TASK-122；「路由守卫」在
test_customer_api.py（显式清单里加了新路由），不在这里。

用量事件照 test_customer_usage_api 的 `UsageEventPortal.put` 直接按库里的约束写成各种状态；
SQLite 上的金额只用二进制可精确表示的值（SQLite 把 NUMERIC 存成浮点），
能判别 float 的精度用例只在 MySQL 上跑（设计 §2「查询」）。
- SQLite：`create_all` 建表；
- MySQL：表与触发器都走迁移，需要 `BILLING_TEST_DATABASE_URL`，没设时 MySQL 那一半 skip ——
  **不要把 skipped 读成 passed**，CI 里不许 skip。

⚠️ 鉴权一行沿用 AIH-TASK-104 的勘误：门户账号被停用后的旧令牌是 401 `TOKEN_INVALID`。

⚠️ SQL 文本断言不写死方言字面量；只有 MySQL 才有的断言放在方言分支里（或只对 MySQL 方言编译）。
示例 uuid 用全零占位值。
"""

from __future__ import annotations

import base64
import datetime as dt
import functools
import json
import uuid
from collections.abc import Iterator
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, insert
from sqlalchemy.dialects import mysql, sqlite
from sqlalchemy.exc import OperationalError
from test_customer_usage_api import (
    UNBILLED,
    ProjectRef,
    UsageEventPortal,
    _usage_portal,
    raw_cursor,
    record_statements,
    usage_statements,
)
from test_customer_usage_summary_api import capture_server_log, timeout_lines
from test_customer_wallet_api import CONTEXT, _sqlite_factory, error_of
from test_usage_billing import _NUMBERS as BILLING_NUMBERS
from test_usage_billing import TOKENS, Tenancy, _mysql_factory

from app.models.auth import UserRole
from app.models.tenancy import AccountStatus, BillingMode
from app.models.usage import UsageEvent, UsageEventStatus
from app.repositories import usage_events as usage_repository
from app.repositories.usage_events import (
    CUSTOMER_USAGE_QUERY_TIMEOUT_MS,
    QueryTooBroad,
    customer_conversation_statement,
    list_conversations_for_customer,
)
from app.schemas.customer_conversations import (
    decode_conversation_cursor,
    encode_conversation_cursor,
)
from app.schemas.customer_usage import InvalidCustomerUsageQuery, encode_cursor
from app.schemas.customers import money_text
from app.services.portal_accounts import disable_portal_account

CONVERSATIONS = "/api/v1/customer/conversations"
USAGE = "/api/v1/customer/usage-events"

# 设计 §2：`data` 恰好 3 个键，每项恰好 9 个键。多一个就红。
PAGE_FIELDS = {"items", "page_size", "next_cursor"}
ITEM_FIELDS = {
    "conversation_id",
    "project_id",
    "project_name",
    "first_occurred_at",
    "last_occurred_at",
    "request_count",
    "input_tokens",
    "output_tokens",
    "charged_amount",
}

# 一个马来西亚日历日：UTC [2026-09-30 16:00, 2026-10-01 16:00)。
DAY = "2026-10-01"
ON_DAY = dt.datetime(2026, 10, 1, 2, 0, 0, 123456)
MINUTE = dt.timedelta(minutes=1)
PERIOD = {
    "occurred_from": dt.datetime(2026, 9, 30, 16, 0, 0),
    "occurred_to": dt.datetime(2026, 10, 1, 16, 0, 0),
}
ZERO = "0.00000000"
ZERO_UUID = "00000000-0000-0000-0000-000000000000"

# INV-7 的哨兵：写进事件与项目，断言它们不出现在原始响应文本里。
SENTINEL_COSTS = {
    "provider_source_cost": Decimal("0.13579246"),
    "estimated_provider_cost_myr": Decimal("0.24681357"),
    "fx_rate_applied": Decimal("0.97531864"),
}
SENTINEL_REFERENCE_PRICE = Decimal("0.86420975")
SENTINEL_TEXTS = ("0.13579246", "0.24681357", "0.97531864", "0.86420975")
SENTINEL_DESCRIPTION = "Sentinel project note 0000"
SENTINEL_WEBHOOK = "https://hooks.example.com/sentinel-webhook-token-0000"
COST_COLUMNS = (
    "provider_source_cost",
    "estimated_provider_cost_myr",
    "fx_rate_applied",
    "reference_customer_price",
    "provider_source_currency",
)


@pytest.fixture
def portal(tmp_path) -> Iterator[UsageEventPortal]:
    yield from _usage_portal(_sqlite_factory(), tmp_path)


@pytest.fixture(params=["sqlite", "mysql"])
def both(request, monkeypatch, tmp_path) -> Iterator[UsageEventPortal]:
    """The same cases on SQLite and on a real MySQL (design §7「SQLite + MySQL 参数化」)."""
    factories = _mysql_factory(monkeypatch) if request.param == "mysql" else _sqlite_factory()
    yield from _usage_portal(factories, tmp_path)


@pytest.fixture
def mysql_portal(monkeypatch, tmp_path) -> Iterator[UsageEventPortal]:
    yield from _usage_portal(_mysql_factory(monkeypatch), tmp_path)


def get_conversations(portal: UsageEventPortal, headers: dict[str, str], **params):
    return portal.client.get(CONVERSATIONS, params=params, headers=headers)


def conversations(portal: UsageEventPortal, headers: dict[str, str], **params) -> dict:
    response = get_conversations(portal, headers, **params)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert set(data) == PAGE_FIELDS
    assert all(set(item) == ITEM_FIELDS for item in data["items"])
    return data


def walk(portal: UsageEventPortal, headers: dict[str, str], **params) -> list[list[dict]]:
    """Every page from the newest one, following `next_cursor`."""
    pages: list[list[dict]] = []
    cursor: str | None = None
    while True:
        extra = {} if cursor is None else {"cursor": cursor}
        data = conversations(portal, headers, **params, **extra)
        pages.append(data["items"])
        cursor = data["next_cursor"]
        if cursor is None:
            return pages


def keys(items: list[dict]) -> list[tuple[str, str]]:
    return [(item["project_id"], item["conversation_id"]) for item in items]


def compiled(statement, dialect) -> str:
    return " ".join(str(statement.compile(dialect=dialect)).split())


def seed_normal(portal: UsageEventPortal, tenant: Tenancy) -> tuple[ProjectRef, ProjectRef]:
    """§7「正常路径」的数据：两个项目、三个会话、另有 2 条没有 `conversation_id` 的请求。

    `conv-1`（主项目）4 条，其中 1 条 `QUANTITY` 形态；`conv-2`（另一项目）1 条；
    `conv-3`（主项目）2 条，其中 1 条 token 是自定义值。金额都是二进制可精确表示的值。
    """
    main = portal.main_project(tenant)
    other = portal.project(tenant, "Support desk")
    portal.put(tenant, occurred_at=ON_DAY, conversation_id="conv-1", billable="0.0625")
    portal.put(tenant, occurred_at=ON_DAY + MINUTE, conversation_id="conv-1", billable="0.125")
    portal.put(
        tenant,
        occurred_at=ON_DAY + 2 * MINUTE,
        conversation_id="conv-1",
        seconds="12.5",
        billable="0.25",
    )
    portal.put(tenant, occurred_at=ON_DAY + 3 * MINUTE, conversation_id="conv-1", billable="0.0625")
    portal.put(
        tenant,
        project=other,
        occurred_at=ON_DAY + 4 * MINUTE,
        conversation_id="conv-2",
        billable="0.5",
    )
    portal.put(tenant, occurred_at=ON_DAY + 5 * MINUTE, conversation_id="conv-3", billable="0.0625")
    portal.put(
        tenant,
        occurred_at=ON_DAY + 6 * MINUTE,
        conversation_id="conv-3",
        tokens=dict(TOKENS, input_tokens=7, output_tokens=9),
        billable="0.125",
    )
    portal.put(tenant, occurred_at=ON_DAY + 7 * MINUTE, billable="0.25")
    portal.put(tenant, occurred_at=ON_DAY + 8 * MINUTE, billable="0.25")
    return main, other


def bulk_conversations(
    portal: UsageEventPortal, tenant: Tenancy, count: int, spread: int, start: dt.datetime
) -> None:
    """`count` PROCESSED events at 0, one second apart from `start`, over `spread` conversations."""
    portal.snapshot()
    billed = portal._billed(
        billable=Decimal("0"),
        mode=BillingMode.PREPAID,
        reference=None,
        costs=None,
        ledger_id=None,
    )
    where = portal.main_project(tenant)
    batch: list[dict[str, object]] = []
    with portal.factory() as session:
        template = portal._row_values(
            session,
            tenant,
            where,
            status=UsageEventStatus.PROCESSED,
            occurred_at=start,
            provider="anthropic",
            model="claude-x",
            conversation_id=None,
            request_id=None,
            feature=None,
            tokens=None,
            seconds=None,
        )
        for offset in range(count):
            number = next(BILLING_NUMBERS)
            batch.append(
                {
                    **template,
                    **billed,
                    "public_id": str(uuid.uuid4()),
                    "event_id": f"00000000-0000-7000-8000-{number:012d}",
                    "request_id": f"call-{number}",
                    "conversation_id": f"conv-{offset % spread:05d}",
                    "occurred_at": start + dt.timedelta(seconds=offset),
                }
            )
            if len(batch) == 1000:
                session.execute(insert(UsageEvent), batch)
                batch = []
        if batch:
            session.execute(insert(UsageEvent), batch)
        session.commit()


# --- 正常路径与对账 ---------------------------------------------------------------------


def test_three_conversations_latest_first_with_their_sums(both: UsageEventPortal) -> None:
    """§7「正常路径」：3 行、按 `last_occurred_at` 倒序、每行恰好 9 个键、合计逐字等于手算值。

    `QUANTITY` 那条计入请求数与金额、不计 token；没有会话 ID 的 2 条不出现、不计入任何行。
    """
    tenant = both.tenant()
    main, other = seed_normal(both, tenant)
    headers = both.customer(tenant)
    before = both.tables()

    data = conversations(both, headers, from_date=DAY, to_date=DAY)

    assert data["page_size"] == 20
    assert data["next_cursor"] is None
    assert data["items"] == [
        {
            "conversation_id": "conv-3",
            "project_id": main.public_id,
            "project_name": "Chatbot",
            "first_occurred_at": (ON_DAY + 5 * MINUTE).isoformat(),
            "last_occurred_at": (ON_DAY + 6 * MINUTE).isoformat(),
            "request_count": 2,
            "input_tokens": TOKENS["input_tokens"] + 7,
            "output_tokens": TOKENS["output_tokens"] + 9,
            "charged_amount": "0.18750000",
        },
        {
            "conversation_id": "conv-2",
            "project_id": other.public_id,
            "project_name": "Support desk",
            "first_occurred_at": (ON_DAY + 4 * MINUTE).isoformat(),
            "last_occurred_at": (ON_DAY + 4 * MINUTE).isoformat(),
            "request_count": 1,
            "input_tokens": TOKENS["input_tokens"],
            "output_tokens": TOKENS["output_tokens"],
            "charged_amount": "0.50000000",
        },
        {
            "conversation_id": "conv-1",
            "project_id": main.public_id,
            "project_name": "Chatbot",
            "first_occurred_at": ON_DAY.isoformat(),
            "last_occurred_at": (ON_DAY + 3 * MINUTE).isoformat(),
            # 4 条请求，只有 3 条是 token 形态。
            "request_count": 4,
            "input_tokens": 3 * TOKENS["input_tokens"],
            "output_tokens": 3 * TOKENS["output_tokens"],
            "charged_amount": "0.50000000",
        },
    ]
    assert sum(item["request_count"] for item in data["items"]) == 7
    assert both.tables() == before


def test_every_row_matches_the_request_level_list(both: UsageEventPortal) -> None:
    """§7「与请求级明细对账」：对每一行调 `GET /usage-events`（同一区间、该行的项目与会话）。

    条数、token 和、计费额（Decimal 相加后 `money_text`）逐字相等；
    首末时刻是 items 的最小 / 最大值。
    """
    tenant = both.tenant()
    seed_normal(both, tenant)
    headers = both.customer(tenant)

    rows = conversations(both, headers, from_date=DAY, to_date=DAY)["items"]

    assert len(rows) == 3
    for row in rows:
        response = both.client.get(
            USAGE,
            params={
                "from_date": DAY,
                "to_date": DAY,
                "project_id": row["project_id"],
                "conversation_id": row["conversation_id"],
                "page_size": 100,
            },
            headers=headers,
        )
        assert response.status_code == 200, response.text
        usage = response.json()["data"]
        assert usage["next_cursor"] is None
        items = usage["items"]
        assert row["request_count"] == len(items)
        assert row["input_tokens"] == sum(item["input_tokens"] or 0 for item in items)
        assert row["output_tokens"] == sum(item["output_tokens"] or 0 for item in items)
        assert row["charged_amount"] == money_text(
            sum((Decimal(item["charged_amount"]) for item in items), Decimal(0))
        )
        moments = sorted(items, key=lambda item: dt.datetime.fromisoformat(item["occurred_at"]))
        assert row["first_occurred_at"] == moments[0]["occurred_at"]
        assert row["last_occurred_at"] == moments[-1]["occurred_at"]


def test_the_key_is_the_project_and_the_conversation_id_byte_for_byte(
    both: UsageEventPortal,
) -> None:
    """§7「分组键」：同一 id 在两个项目里是两行。

    `conv_A` 与 `conv_a` 在同一项目里也是两行（按字节区分大小写）。
    """
    tenant = both.tenant()
    main = both.main_project(tenant)
    other = both.project(tenant, "Support desk")
    both.put(tenant, occurred_at=ON_DAY, conversation_id="conv-same")
    both.put(tenant, project=other, occurred_at=ON_DAY + MINUTE, conversation_id="conv-same")
    both.put(tenant, occurred_at=ON_DAY + 2 * MINUTE, conversation_id="conv_A")
    both.put(tenant, occurred_at=ON_DAY + 3 * MINUTE, conversation_id="conv_a")
    both.put(tenant, occurred_at=ON_DAY + 4 * MINUTE, conversation_id="conv_a")
    headers = both.customer(tenant)

    items = conversations(both, headers, from_date=DAY, to_date=DAY)["items"]

    assert [
        (*key, item["request_count"]) for key, item in zip(keys(items), items, strict=True)
    ] == [
        (main.public_id, "conv_a", 2),
        (main.public_id, "conv_A", 1),
        (other.public_id, "conv-same", 1),
        (main.public_id, "conv-same", 1),
    ]


def test_only_the_part_inside_the_period_is_counted(both: UsageEventPortal) -> None:
    """§7「区间裁剪」：`from_date = to_date = 2026-10-01` 时只计中间两条。"""
    tenant = both.tenant()
    moments = (
        dt.datetime(2026, 9, 30, 15, 59, 59, 999999),
        dt.datetime(2026, 9, 30, 16, 0, 0),
        dt.datetime(2026, 10, 1, 15, 59, 59, 999999),
        dt.datetime(2026, 10, 1, 16, 0, 0),
    )
    for moment in moments:
        both.put(tenant, occurred_at=moment, conversation_id="conv-edge")
    headers = both.customer(tenant)

    [item] = conversations(both, headers, from_date=DAY, to_date=DAY)["items"]

    assert item["conversation_id"] == "conv-edge"
    assert item["request_count"] == 2
    assert item["charged_amount"] == "0.12500000"
    assert item["first_occurred_at"] == moments[1].isoformat()
    assert item["last_occurred_at"] == moments[2].isoformat()


def test_only_billed_events_are_counted(both: UsageEventPortal) -> None:
    """§7「只算已计费」：未计费 / 错误状态的事件都不计入；只有未计费事件的会话不出现。"""
    tenant = both.tenant()
    both.put(tenant, occurred_at=ON_DAY, conversation_id="conv-billed")
    for number, status in enumerate(UNBILLED, start=1):
        both.put(
            tenant,
            status=status,
            occurred_at=ON_DAY + number * MINUTE,
            conversation_id="conv-billed",
        )
        both.put(
            tenant,
            status=status,
            occurred_at=ON_DAY + number * MINUTE,
            conversation_id="conv-unbilled",
        )
    headers = both.customer(tenant)

    [item] = conversations(both, headers, from_date=DAY, to_date=DAY)["items"]

    assert item["conversation_id"] == "conv-billed"
    assert item["request_count"] == 1
    assert item["last_occurred_at"] == ON_DAY.isoformat()
    assert item["charged_amount"] == "0.06250000"


# --- 排序与翻页 -------------------------------------------------------------------------


def test_ties_on_the_last_activity_page_by_project_then_conversation(
    both: UsageEventPortal,
) -> None:
    """§7「排序与平局」：3 个会话最后活动完全相同（微秒相同），分属两个项目。

    `page_size=1` 翻完：按（项目、`conversation_id`）倒序逐个出现，不重不漏。
    """
    tenant = both.tenant()
    main = both.main_project(tenant)
    other = both.project(tenant, "Support desk")
    tied = [(main, "conv-a"), (main, "conv-b"), (other, "conv-a")]
    for where, conversation in tied:
        both.put(tenant, project=where, occurred_at=ON_DAY - MINUTE, conversation_id=conversation)
        both.put(tenant, project=where, occurred_at=ON_DAY, conversation_id=conversation)
    headers = both.customer(tenant)

    pages = walk(both, headers, from_date=DAY, to_date=DAY, page_size=1)

    assert [len(page) for page in pages] == [1, 1, 1]
    seen = [key for page in pages for key in keys(page)]
    expected = sorted(((where.public_id, name) for where, name in tied), reverse=True)
    assert seen == expected
    assert all(item["last_occurred_at"] == ON_DAY.isoformat() for page in pages for item in page)


def test_five_conversations_in_three_pages(both: UsageEventPortal) -> None:
    """§7「翻页」：5 个会话，`page_size=2` 连翻三页 2 / 2 / 1，前两页 `next_cursor` 非空。"""
    tenant = both.tenant()
    for number in range(1, 6):
        both.put(tenant, occurred_at=ON_DAY + number * MINUTE, conversation_id=f"conv-{number}")
    headers = both.customer(tenant)
    params = {"from_date": DAY, "to_date": DAY, "page_size": 2}

    first = conversations(both, headers, **params)
    second = conversations(both, headers, **params, cursor=first["next_cursor"])
    third = conversations(both, headers, **params, cursor=second["next_cursor"])

    assert [len(page["items"]) for page in (first, second, third)] == [2, 2, 1]
    assert first["next_cursor"] is not None
    assert second["next_cursor"] is not None
    assert third["next_cursor"] is None
    seen = [item["conversation_id"] for page in (first, second, third) for item in page["items"]]
    assert seen == ["conv-5", "conv-4", "conv-3", "conv-2", "conv-1"]


def test_new_activity_between_pages_never_repeats_a_conversation(
    mysql_portal: UsageEventPortal,
) -> None:
    """§7「翻页中有新活动」（MySQL）：取第一页后，另一事务插入两条事件。

    一条给第一页里的 X，一条给尚未返回的 Y（比所有现有事件都新）。全程没有会话出现两次；
    Y 不在后面的页里；重取第一页时 Y 与 X 都在、合计含新事件（设计 §4）。
    """
    portal = mysql_portal
    tenant = portal.tenant()
    for number in range(1, 6):
        portal.put(tenant, occurred_at=ON_DAY + number * MINUTE, conversation_id=f"conv-{number}")
    headers = portal.customer(tenant)
    params = {"from_date": DAY, "to_date": DAY, "page_size": 2}

    first = conversations(portal, headers, **params)
    seen = [item["conversation_id"] for item in first["items"]]
    assert seen == ["conv-5", "conv-4"]
    # 另一个会话、另一个事务里提交：X = conv-5（已返回），Y = conv-2（尚未返回、变成最新）。
    portal.put(tenant, occurred_at=ON_DAY + 10 * MINUTE, conversation_id="conv-5")
    portal.put(tenant, occurred_at=ON_DAY + 20 * MINUTE, conversation_id="conv-2")
    cursor = first["next_cursor"]
    while cursor is not None:
        data = conversations(portal, headers, **params, cursor=cursor)
        seen.extend(item["conversation_id"] for item in data["items"])
        cursor = data["next_cursor"]

    assert len(seen) == len(set(seen))
    assert seen == ["conv-5", "conv-4", "conv-3", "conv-1"]
    newest = conversations(portal, headers, **params)["items"]
    assert [(item["conversation_id"], item["request_count"]) for item in newest] == [
        ("conv-2", 2),
        ("conv-5", 2),
    ]
    assert newest[0]["last_occurred_at"] == (ON_DAY + 20 * MINUTE).isoformat()
    assert newest[1]["last_occurred_at"] == (ON_DAY + 10 * MINUTE).isoformat()
    assert newest[0]["charged_amount"] == "0.12500000"


def test_the_same_request_twice_returns_the_same_response(portal: UsageEventPortal) -> None:
    """§5「重复请求」：期间没有新计费的事件时两次的 data 相同（带游标也一样）。"""
    tenant = portal.tenant()
    for number in range(3):
        portal.put(tenant, occurred_at=ON_DAY + number * MINUTE, conversation_id=f"conv-{number}")
    headers = portal.customer(tenant)
    params = {"from_date": DAY, "to_date": DAY, "page_size": 1}
    cursor = conversations(portal, headers, **params)["next_cursor"]

    first = conversations(portal, headers, **params, cursor=cursor)
    again = conversations(portal, headers, **params, cursor=cursor)

    assert first == again


# --- 日期与参数规则 ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("params", "fields"),
    [
        ({"to_date": DAY}, "query.from_date"),
        ({"from_date": DAY}, "query.to_date"),
        ({}, "query.from_date, query.to_date"),
        ({"from_date": "2026-10-02", "to_date": DAY}, "query.from_date, query.to_date"),
        ({"from_date": "2026-07-01", "to_date": "2026-10-01"}, "query.from_date, query.to_date"),
        ({"from_date": "2026-02-30", "to_date": "2026-03-01"}, "query.from_date"),
        ({"from_date": "2026/10/01", "to_date": DAY}, "query.from_date"),
        ({"from_date": DAY, "to_date": "2026-10-1"}, "query.to_date"),
        ({"from_date": "0001-01-01", "to_date": "0001-01-02"}, "query.from_date"),
        ({"from_date": "2099-12-31", "to_date": "2100-01-01"}, "query.to_date"),
    ],
)
def test_date_rules_are_422_without_echoing_the_value(
    portal: UsageEventPortal, params: dict, fields: str
) -> None:
    """§7「日期与参数规则」：缺、倒置、93 天、格式错、极值 → 422，文案只列字段名。"""
    headers = portal.customer(portal.tenant())
    before = portal.tables()

    response = get_conversations(portal, headers, **params)

    assert error_of(response) == (422, "VALIDATION_ERROR")
    assert response.json()["error"]["message"] == f"Invalid request fields: {fields}"
    assert response.json()["data"] is None
    for value in params.values():
        assert value not in response.text
    assert portal.tables() == before


@pytest.mark.parametrize("value", ["0", "101", "1.5", "twenty"])
def test_a_page_size_out_of_range_is_422_before_authentication(
    portal: UsageEventPortal, value: str
) -> None:
    """§7「日期与参数规则」：`page_size` 0 / 101 由框架在鉴权之前判，匿名调用同样 422。"""
    headers = portal.customer(portal.tenant())

    signed_in = get_conversations(portal, headers, from_date=DAY, to_date=DAY, page_size=value)
    anonymous = get_conversations(portal, {}, from_date=DAY, to_date=DAY, page_size=value)

    for refused in (signed_in, anonymous):
        assert error_of(refused) == (422, "VALIDATION_ERROR")
        assert refused.json()["error"]["message"] == "Invalid request fields: query.page_size"


def test_a_92_day_period_and_the_largest_page_are_200(portal: UsageEventPortal) -> None:
    """§7「日期与参数规则」：92 天（含两端）200，首尾两天的事件都在。

    `page_size=100` 可以；未知参数忽略。
    """
    tenant = portal.tenant()
    portal.put(tenant, occurred_at=dt.datetime(2026, 6, 30, 16, 0, 0), conversation_id="conv-first")
    portal.put(
        tenant,
        occurred_at=dt.datetime(2026, 9, 30, 15, 59, 59, 999999),
        conversation_id="conv-last",
    )
    headers = portal.customer(tenant)

    # 会话表不按 `conversation_id`、供应商筛选：带了也只是未知参数。
    data = conversations(
        portal,
        headers,
        from_date="2026-07-01",
        to_date="2026-09-30",
        page_size=100,
        sort="asc",
        total="yes",
        conversation_id="conv-first",
        provider="openai",
    )

    assert data["page_size"] == 100
    assert [item["conversation_id"] for item in data["items"]] == ["conv-last", "conv-first"]
    assert data["next_cursor"] is None


# --- 游标 -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "conversation_id",
    ["c" * 128, "conv-1", "A.b_c:d-E", "0"],
)
@pytest.mark.parametrize(
    "moment",
    [dt.datetime(2026, 10, 1, 2, 0, 0, 123456), dt.datetime(2026, 10, 1, 2, 0, 0)],
)
def test_the_cursor_round_trips(conversation_id: str, moment: dt.datetime) -> None:
    """§7「游标」（unit）：往返相等，含 128 字符的 `conversation_id`。

    编码后不带填充、不超过 256 个字符。
    """
    project = "00000000-0000-4000-8000-000000000001"

    cursor = encode_conversation_cursor(moment, project, conversation_id)

    assert "=" not in cursor
    assert len(cursor) <= 256
    assert decode_conversation_cursor(cursor) == (moment, project, conversation_id)


TAMPERED_CURSORS = [
    "not base64!",
    "A",
    "x" * 600,
    raw_cursor("garbage"),
    raw_cursor(f"2026-10-01T02:00:00.123456|{ZERO_UUID}"),
    raw_cursor(f"2026-10-01T02:00:00.123456|{ZERO_UUID}|conv-1|extra"),
    raw_cursor(f"2026-13-45T00:00:00.000000|{ZERO_UUID}|conv-1"),
    raw_cursor(f"2026-10-01T02:00:00|{ZERO_UUID}|conv-1"),
    raw_cursor("2026-10-01T02:00:00.123456|not-a-uuid|conv-1"),
    raw_cursor("2026-10-01T02:00:00.123456|00000000-0000-4000-8000-00000000000A|conv-1"),
    raw_cursor(f"2026-10-01T02:00:00.123456|{ZERO_UUID}|conv 1"),
    raw_cursor(f"2026-10-01T02:00:00.123456|{ZERO_UUID}|conv/1"),
    raw_cursor(f"2026-10-01T02:00:00.123456|{ZERO_UUID}|"),
    raw_cursor(f"2026-10-01T02:00:00.123456|{ZERO_UUID}|{'c' * 129}"),
    base64.urlsafe_b64encode(b"\xff\xfe\xfd").decode(),
]


@pytest.mark.parametrize("cursor", TAMPERED_CURSORS)
def test_a_tampered_cursor_does_not_decode(cursor: str) -> None:
    """§7「游标」（unit）：非 base64url、缺段、多段、非法时间、非法 uuid、非法字符、超长。"""
    with pytest.raises(InvalidCustomerUsageQuery) as raised:
        decode_conversation_cursor(cursor)

    assert raised.value.code == "VALIDATION_ERROR"
    assert raised.value.message == "Invalid request fields: query.cursor"


@pytest.mark.parametrize("cursor", TAMPERED_CURSORS)
def test_a_tampered_cursor_is_422_without_echoing_it(portal: UsageEventPortal, cursor: str) -> None:
    """§7「游标」（integration）：422 `VALIDATION_ERROR`，字段名 `cursor`，不回显。"""
    headers = portal.customer(portal.tenant())

    response = get_conversations(portal, headers, from_date=DAY, to_date=DAY, cursor=cursor)

    assert error_of(response) == (422, "VALIDATION_ERROR")
    assert response.json()["error"]["message"] == "Invalid request fields: query.cursor"
    # 太短的串随便就是别的文本的子串，只对够长的断言不回显。
    if len(cursor) > 8:
        assert cursor not in response.text


def test_an_event_cursor_is_refused_and_vice_versa(portal: UsageEventPortal) -> None:
    """§7「游标」：`GET /usage-events` 的 `next_cursor` 拿来翻会话得 422；反过来同样 422。"""
    tenant = portal.tenant()
    for number in range(3):
        portal.put(tenant, occurred_at=ON_DAY + number * MINUTE, conversation_id=f"conv-{number}")
    headers = portal.customer(tenant)
    day = {"from_date": DAY, "to_date": DAY, "page_size": 1}
    event_cursor = portal.usage(headers, **day)["next_cursor"]
    conversation_cursor = conversations(portal, headers, **day)["next_cursor"]
    assert event_cursor is not None and conversation_cursor is not None

    wrong_way = get_conversations(portal, headers, **day, cursor=event_cursor)
    other_way = portal.get_usage(headers, **day, cursor=conversation_cursor)

    for response in (wrong_way, other_way):
        assert error_of(response) == (422, "VALIDATION_ERROR")
        assert response.json()["error"]["message"] == "Invalid request fields: query.cursor"


def test_a_valid_cursor_only_narrows_to_strictly_older_keys(portal: UsageEventPortal) -> None:
    """游标是 `(last_occurred_at, 项目, conversation_id)` 的严格小于：平局里键更小的还在后面。"""
    tenant = portal.tenant()
    main = portal.main_project(tenant)
    portal.put(tenant, occurred_at=ON_DAY - MINUTE, conversation_id="conv-older")
    portal.put(tenant, occurred_at=ON_DAY, conversation_id="conv-a")
    portal.put(tenant, occurred_at=ON_DAY, conversation_id="conv-b")
    headers = portal.customer(tenant)

    after_top = conversations(
        portal,
        headers,
        from_date=DAY,
        to_date=DAY,
        cursor=encode_conversation_cursor(ON_DAY, main.public_id, "conv-b"),
    )

    assert [item["conversation_id"] for item in after_top["items"]] == ["conv-a", "conv-older"]


# --- 项目筛选 ---------------------------------------------------------------------------


def test_the_project_filter_lists_only_that_projects_conversations(both: UsageEventPortal) -> None:
    """§7「项目筛选」：按本客户的一个项目筛，只出现该项目的会话。"""
    tenant = both.tenant()
    main, other = seed_normal(both, tenant)
    headers = both.customer(tenant)

    on_other = conversations(both, headers, from_date=DAY, to_date=DAY, project_id=other.public_id)
    on_main = conversations(both, headers, from_date=DAY, to_date=DAY, project_id=main.public_id)

    assert keys(on_other["items"]) == [(other.public_id, "conv-2")]
    assert keys(on_main["items"]) == [(main.public_id, "conv-3"), (main.public_id, "conv-1")]


def test_an_unknown_or_foreign_project_is_an_empty_page_without_an_aggregate_query(
    portal: UsageEventPortal, monkeypatch
) -> None:
    """§7「项目筛选」：不存在的、别的客户的项目 id → 空页。

    且不发聚合查询（monkeypatch 计数）。
    """
    mine = portal.tenant()
    theirs = portal.tenant()
    portal.put(mine, conversation_id="conv-mine")
    portal.put(theirs, conversation_id="conv-theirs")
    their_project = portal.main_project(theirs)
    headers = portal.customer(mine)
    built: list[object] = []
    executed: list[object] = []
    real_build = usage_repository.customer_conversation_statement
    real_run = usage_repository.run_bounded

    def counting_build(*args, **kwargs):
        built.append(args)
        return real_build(*args, **kwargs)

    def counting_run(*args, **kwargs):
        executed.append(args)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(usage_repository, "customer_conversation_statement", counting_build)
    monkeypatch.setattr(usage_repository, "run_bounded", counting_run)
    engine = portal.app.state.engine
    statements, record = record_statements(engine)
    try:
        foreign = get_conversations(
            portal, headers, from_date=DAY, to_date=DAY, project_id=their_project.public_id
        )
        unknown = get_conversations(
            portal, headers, from_date=DAY, to_date=DAY, project_id=ZERO_UUID
        )
    finally:
        event.remove(engine, "before_cursor_execute", record)

    for response in (foreign, unknown):
        assert response.status_code == 200
        assert response.json()["data"] == {"items": [], "page_size": 20, "next_cursor": None}
    assert built == []
    assert executed == []
    assert statements, "监听落空"
    assert usage_statements(statements) == []
    # 计数本身有效：不带项目时恰好一次。
    conversations(portal, headers, from_date=DAY, to_date=DAY)
    assert (len(built), len(executed)) == (1, 1)


# --- 扫描上限与执行时间提示 -------------------------------------------------------------


def test_the_hint_is_rendered_once_on_the_outermost_select_on_mysql_only() -> None:
    """§7「扫描上限」的编译断言：MySQL 上恰好一次、在最外层 SELECT；SQLite 上不带（归一后比较）。

    内层只按 `usage_events` 的两列分组，不读任何成本列；租户、状态、区间与 `conversation_id`
    条件都在。
    """
    assert CUSTOMER_USAGE_QUERY_TIMEOUT_MS == 2000
    variants = [
        customer_conversation_statement(1, limit=21, **PERIOD),
        customer_conversation_statement(
            1,
            limit=21,
            project_row_id=7,
            before=(ON_DAY, ZERO_UUID, "conv-1"),
            **PERIOD,
        ),
    ]
    short = customer_conversation_statement(1, limit=21, timeout_ms=1, **PERIOD)
    for statement in [*variants, short]:
        assert "MAX_EXECUTION_TIME" not in compiled(statement, sqlite.dialect())
    # 只对 MySQL 方言编译的断言：提示紧跟最外层的 SELECT 关键字，整条语句里只有这一处。
    for statement in variants:
        text = compiled(statement, mysql.dialect())
        assert text.startswith("SELECT /*+ MAX_EXECUTION_TIME(2000) */ ")
        assert text.count("MAX_EXECUTION_TIME") == 1
    short_text = compiled(short, mysql.dialect())
    assert short_text.startswith("SELECT /*+ MAX_EXECUTION_TIME(1) */ ")
    assert short_text.count("MAX_EXECUTION_TIME") == 1
    for statement in variants:
        for dialect in (sqlite.dialect(), mysql.dialect()):
            text = compiled(statement, dialect)
            for column in COST_COLUMNS:
                assert column not in text
            # 外层不再分组：分组只出现一次（在派生表里）。
            assert text.upper().count("GROUP BY") == 1
            for column in ("tenant_id", "status", "occurred_at", "conversation_id", "project_id"):
                assert f"usage_events.{column}" in text


def test_the_executed_query_carries_the_hint_on_mysql_only(both: UsageEventPortal) -> None:
    """真正发出的那条聚合查询：恰好一条；MySQL 上带一次 2 秒提示，SQLite 上不带；不加锁。"""
    tenant = both.tenant()
    both.put(tenant, conversation_id="conv-1")
    headers = both.customer(tenant)
    engine = both.app.state.engine
    statements, record = record_statements(engine)
    try:
        assert len(conversations(both, headers, from_date=DAY, to_date=DAY)["items"]) == 1
    finally:
        event.remove(engine, "before_cursor_execute", record)

    [query] = usage_statements(statements)
    normalized = " ".join(query.split())
    for lock in ("FOR UPDATE", "FOR SHARE", "LOCK IN SHARE MODE"):
        assert lock not in normalized.upper()
    for column in COST_COLUMNS:
        assert column not in normalized
    if engine.dialect.name == "mysql":
        assert normalized.startswith("SELECT /*+ MAX_EXECUTION_TIME(2000) */ ")
        assert normalized.count("MAX_EXECUTION_TIME") == 1
    else:
        assert "MAX_EXECUTION_TIME" not in normalized


def test_a_scan_past_the_budget_is_query_too_broad(
    mysql_portal: UsageEventPortal, monkeypatch
) -> None:
    """§7「扫描上限」（MySQL）：30 000 条区间内的已计费事件分布在 3 000 个会话里。

    ① 测试专用 1 毫秒超时：repository 抛 `QueryTooBroad`，接口 422 `QUERY_TOO_BROAD`，一条
    `customer_usage_query_timeout` 日志，不带任何参数值；② 默认 2 秒：200。
    """
    portal = mysql_portal
    tenant = portal.tenant()
    # 马来西亚 10 月 1 日 00:00 起每秒一条：全部落在 10 月 1 日（8 小时 20 分钟）。
    start = dt.datetime(2026, 9, 30, 16, 0, 0)
    bulk_conversations(portal, tenant, 30_000, 3_000, start)
    headers = portal.customer(tenant)

    with portal.factory() as session, pytest.raises(QueryTooBroad):
        list_conversations_for_customer(session, tenant.id, limit=21, timeout_ms=1, **PERIOD)
    data = conversations(portal, headers, from_date=DAY, to_date=DAY)
    assert len(data["items"]) == 20
    assert data["next_cursor"] is not None
    top = data["items"][0]
    assert (top["conversation_id"], top["request_count"]) == ("conv-02999", 10)
    assert top["last_occurred_at"] == (start + dt.timedelta(seconds=29_999)).isoformat()

    monkeypatch.setattr(
        usage_repository,
        "list_conversations_for_customer",
        functools.partial(list_conversations_for_customer, timeout_ms=1),
    )
    params = {"from_date": DAY, "to_date": DAY, "project_id": portal.main_project(tenant).public_id}
    response, lines = capture_server_log(lambda: get_conversations(portal, headers, **params))

    assert error_of(response) == (422, "QUERY_TOO_BROAD")
    assert response.json()["data"] is None
    assert response.json()["request_id"]
    [timeout] = timeout_lines(lines)
    assert timeout["request_id"] == response.json()["request_id"]
    assert timeout["customer_id"] == tenant.public_id
    server_log = json.dumps(lines)
    for value in params.values():
        assert value not in server_log


def test_query_too_broad_is_a_422_with_one_warning_and_no_parameter_values(
    portal: UsageEventPortal, monkeypatch
) -> None:
    """超时映射：repository 抛 `QueryTooBroad` → 422 `QUERY_TOO_BROAD`。

    一条日志，响应与日志里都不带参数值。
    """
    tenant = portal.tenant()
    portal.put(tenant, conversation_id="conv-secret-0000")
    headers = portal.customer(tenant)
    before = portal.tables()

    def too_broad(*_args, **_kwargs):
        raise QueryTooBroad

    monkeypatch.setattr(usage_repository, "list_conversations_for_customer", too_broad)
    params = {
        "from_date": "2026-09-17",
        "to_date": "2026-10-03",
        "project_id": portal.main_project(tenant).public_id,
        "cursor": encode_conversation_cursor(ON_DAY, ZERO_UUID, "conv-secret-0000"),
    }
    response, lines = capture_server_log(lambda: get_conversations(portal, headers, **params))

    assert error_of(response) == (422, "QUERY_TOO_BROAD")
    body = response.json()
    assert body["data"] is None
    assert body["request_id"]
    [timeout] = timeout_lines(lines)
    assert timeout["level"] == "WARNING"
    assert timeout["request_id"] == body["request_id"]
    assert timeout["customer_id"] == tenant.public_id
    server_log = json.dumps(lines)
    for value in params.values():
        assert value not in response.text
        assert value not in server_log
    assert "conv-secret-0000" not in server_log
    assert portal.tables() == before


# --- 内部计量、无成本毛利、金额精度 -----------------------------------------------------


def test_an_internal_metered_tenant_is_charged_zero(both: UsageEventPortal) -> None:
    """§7「内部计量租户」（INV-7）：计费额恒为 0，请求数与 token 照常。"""
    tenant = both.tenant()
    both.set_billing_mode(tenant, BillingMode.INTERNAL_METERED_ONLY)
    for number in range(2):
        both.put(
            tenant,
            occurred_at=ON_DAY + number * MINUTE,
            billable="0",
            mode=BillingMode.INTERNAL_METERED_ONLY,
            reference=SENTINEL_REFERENCE_PRICE,
            conversation_id="conv-internal",
        )
    headers = both.customer(tenant)

    response = get_conversations(both, headers, from_date=DAY, to_date=DAY)

    assert response.status_code == 200
    [item] = response.json()["data"]["items"]
    assert set(item) == ITEM_FIELDS
    assert item["charged_amount"] == ZERO
    assert item["request_count"] == 2
    assert item["input_tokens"] == 2 * TOKENS["input_tokens"]
    assert item["output_tokens"] == 2 * TOKENS["output_tokens"]
    for value in SENTINEL_TEXTS:
        assert value not in response.text


def test_no_cost_rate_reference_price_or_project_secret_leaks(both: UsageEventPortal) -> None:
    """§7「无成本毛利」（INV-7）：成本、汇率、参考价与项目的哨兵都不在原始响应里。

    `ck_usage_events_prepaid_reference` 不许 PREPAID 事件带参考价：参考价的哨兵写在一个内部计量
    客户的事件上。
    """
    tenant = both.tenant()
    secret = both.project(
        tenant, "Support desk", description=SENTINEL_DESCRIPTION, webhook=SENTINEL_WEBHOOK
    )
    both.put(tenant, project=secret, costs=SENTINEL_COSTS, conversation_id="conv-1")
    internal = both.tenant()
    both.set_billing_mode(internal, BillingMode.INTERNAL_METERED_ONLY)
    both.put(
        internal,
        billable="0",
        mode=BillingMode.INTERNAL_METERED_ONLY,
        reference=SENTINEL_REFERENCE_PRICE,
        costs=SENTINEL_COSTS,
        conversation_id="conv-2",
    )

    responses = [
        get_conversations(both, both.customer(tenant), from_date=DAY, to_date=DAY),
        get_conversations(both, both.customer(internal), from_date=DAY, to_date=DAY),
    ]

    for response in responses:
        assert response.status_code == 200
        assert len(response.json()["data"]["items"]) == 1
    forbidden = (
        *SENTINEL_TEXTS,
        SENTINEL_DESCRIPTION,
        SENTINEL_WEBHOOK,
        "sentinel-webhook-token",
        '"status"',
        "provider_source",
        "estimated_provider_cost",
        "fx_rate",
        "reference_customer_price",
        "billing_mode",
        "integration_status",
        "status_webhook_url",
        "description",
        "cache_",
        "quantity",
        "tenant_id",
        "margin",
        "total",
        "provider",
        "model",
    )
    for response in responses:
        for value in forbidden:
            assert value not in response.text, value


def test_sums_are_exact_decimals(mysql_portal: UsageEventPortal) -> None:
    """§7「金额精度」（只在 MySQL；SQLite 把 NUMERIC 存成浮点，判别不出）。

    ① 三条 `0.00000001`；
    ② `999999999999.00000000 + 0.00000001`；
    ③ `1234567890.12345678 + 0.00000001`；
    ④ `999999999999.99999999 + 0.00000001`（Decimal 不溢出）。
    两笔大额扣费加起来会超出余额列的范围，所以 ② 与 ④ 各用一个先记了 1 的客户。
    """
    portal = mysql_portal
    cases = {
        "conv-tiny": (("0.00000001",) * 3, "0.00000003"),
        "conv-whole": (("999999999999.00000000", "0.00000001"), "999999999999.00000001"),
        "conv-mixed": (("1234567890.12345678", "0.00000001"), "1234567890.12345679"),
        "conv-largest": (("999999999999.99999999", "0.00000001"), "1000000000000.00000000"),
    }
    # 前提：②③ 的期望值经 float 往返后与期望值不同。
    for name in ("conv-whole", "conv-mixed"):
        expected = Decimal(cases[name][1])
        assert Decimal(float(expected)).quantize(Decimal("0.00000001")) != expected
    shared = portal.tenant()
    owners = {"conv-tiny": shared, "conv-mixed": shared}
    for name in ("conv-whole", "conv-largest"):
        owners[name] = portal.tenant()
        portal.world.fund(owners[name], "1")
    for name, (amounts, _expected) in cases.items():
        for number, amount in enumerate(amounts):
            portal.put(
                owners[name],
                occurred_at=ON_DAY + number * MINUTE,
                billable=amount,
                conversation_id=name,
            )

    found: dict[str, str] = {}
    for tenant in {id(owner): owner for owner in owners.values()}.values():
        for item in conversations(portal, portal.customer(tenant), from_date=DAY, to_date=DAY)[
            "items"
        ]:
            found[item["conversation_id"]] = item["charged_amount"]

    assert found == {name: expected for name, (_amounts, expected) in cases.items()}


# --- 租户越权 ---------------------------------------------------------------------------


def test_a_customer_only_ever_sees_its_own_conversations(both: UsageEventPortal) -> None:
    """§7「租户越权」（INV-8）：A、B 共用同一个 `conversation_id` 值。

    A 用 B 的项目 id、B 的最后一页游标，并带 B 的标识，只得到 A 的行或空页；
    A 的合计不含 B 的事件；A 的任何响应里不出现 B 的项目名与金额。
    """
    mine = both.tenant()
    theirs = both.tenant()
    their_project = both.project(theirs, "Theirs secret project")
    both.put(mine, occurred_at=ON_DAY, conversation_id="conv-shared")
    both.put(mine, occurred_at=ON_DAY + MINUTE, conversation_id="conv-shared")
    both.put(mine, occurred_at=ON_DAY + 2 * MINUTE, conversation_id="conv-mine")
    both.put(theirs, occurred_at=ON_DAY + 5 * MINUTE, conversation_id="conv-shared", billable="0.5")
    both.put(
        theirs,
        project=their_project,
        occurred_at=ON_DAY + 6 * MINUTE,
        conversation_id="conv-theirs",
        billable="0.5",
    )
    headers = both.customer(mine)
    their_headers = both.customer(theirs)
    day = {"from_date": DAY, "to_date": DAY}
    forged = {"customer_id": theirs.public_id, "tenant_id": str(theirs.id)}
    their_cursor = conversations(both, their_headers, **day, page_size=1)["next_cursor"]
    assert their_cursor is not None
    my_main = both.main_project(mine)
    their_main = both.main_project(theirs)
    expected = [
        {
            "conversation_id": "conv-mine",
            "project_id": my_main.public_id,
            "project_name": "Chatbot",
            "first_occurred_at": (ON_DAY + 2 * MINUTE).isoformat(),
            "last_occurred_at": (ON_DAY + 2 * MINUTE).isoformat(),
            "request_count": 1,
            "input_tokens": TOKENS["input_tokens"],
            "output_tokens": TOKENS["output_tokens"],
            "charged_amount": "0.06250000",
        },
        {
            "conversation_id": "conv-shared",
            "project_id": my_main.public_id,
            "project_name": "Chatbot",
            "first_occurred_at": ON_DAY.isoformat(),
            "last_occurred_at": (ON_DAY + MINUTE).isoformat(),
            "request_count": 2,
            "input_tokens": 2 * TOKENS["input_tokens"],
            "output_tokens": 2 * TOKENS["output_tokens"],
            "charged_amount": "0.12500000",
        },
    ]

    responses = [
        get_conversations(both, headers, **day, **forged),
        get_conversations(both, headers, **day, project_id=their_project.public_id, **forged),
        get_conversations(both, headers, **day, project_id=their_main.public_id, **forged),
        get_conversations(both, headers, **day, cursor=their_cursor, **forged),
    ]

    for response in responses:
        assert response.status_code == 200
    data = [response.json()["data"] for response in responses]
    assert data[0]["items"] == expected
    assert data[1]["items"] == []
    assert data[2]["items"] == []
    # B 的游标只用于 `<` 比较：A 得到的仍是 A 自己更旧的行（这里是 A 的全部两行）。
    assert data[3]["items"] == expected
    for response in responses:
        for leaked in (
            theirs.public_id,
            their_project.public_id,
            their_main.public_id,
            "Theirs secret project",
            "conv-theirs",
            "0.50000000",
        ):
            assert leaked not in response.text


# --- 鉴权、只读契约、失败 ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("caller", "expected"),
    [
        ("anonymous", (401, "TOKEN_INVALID")),
        ("bad", (401, "TOKEN_INVALID")),
        ("admin", (403, "CUSTOMER_REQUIRED")),
        # 契约勘误：非 ACTIVE 用户由 `require_current_user` 拒绝，是 401。
        ("disabled", (401, "TOKEN_INVALID")),
        ("no_tenant", (403, "CUSTOMER_REQUIRED")),
    ],
)
def test_non_customers_are_refused_before_any_aggregate_query(
    portal: UsageEventPortal, caller: str, expected: tuple[int, str]
) -> None:
    """§7「鉴权」：拒绝的同时一条聚合查询都没发。"""
    tenant = portal.tenant()
    portal.put(tenant, conversation_id="conv-1")
    if caller == "anonymous":
        headers: dict[str, str] = {}
    elif caller == "bad":
        headers = {"Authorization": "Bearer not-a-token"}
    elif caller == "admin":
        headers = portal.admin_caller()
    elif caller == "disabled":
        headers = portal.customer(tenant)
        disable_portal_account(
            portal.factory,
            actor=portal.world.admin,
            customer_id=tenant.public_id,
            context=CONTEXT,
        )
    else:
        orphan = portal.make_customer_user(None)
        headers = portal.token(orphan.id, UserRole.CUSTOMER)
    engine = portal.app.state.engine
    statements, record = record_statements(engine)
    try:
        response = get_conversations(portal, headers, from_date=DAY, to_date=DAY)
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert error_of(response) == expected
    assert response.json()["data"] is None
    assert usage_statements(statements) == []


@pytest.mark.parametrize(
    "status",
    [
        AccountStatus.PENDING_ACTIVATION,
        AccountStatus.ENABLED,
        AccountStatus.DISABLED,
        AccountStatus.CLOSED,
    ],
)
def test_the_read_only_contract(both: UsageEventPortal, status: AccountStatus) -> None:
    """§7「只读契约」「无写入」：四种账户状态下都 200，且不写任何表。"""
    tenant = both.tenant()
    both.put(tenant, conversation_id="conv-1")
    both.set_account_status(tenant, status)
    headers = both.customer(tenant)
    before = both.tables()

    data = conversations(both, headers, from_date=DAY, to_date=DAY)

    assert [item["request_count"] for item in data["items"]] == [1]
    assert both.tables() == before


def test_a_failing_query_is_a_500_with_a_request_id_and_then_recovers(
    portal: UsageEventPortal, monkeypatch
) -> None:
    """§7「数据库断连」：repository 抛 `OperationalError`。

    500 `INTERNAL_ERROR` 带 `request_id`；各表不变；之后正常请求 200。
    """
    tenant = portal.tenant()
    portal.put(tenant, conversation_id="conv-1")
    headers = portal.customer(tenant)
    before = portal.tables()

    def broken(*_args, **_kwargs):
        raise OperationalError("SELECT", {}, Exception("fictional database failure"))

    with monkeypatch.context() as patch:
        patch.setattr(usage_repository, "list_conversations_for_customer", broken)
        client = TestClient(portal.app, raise_server_exceptions=False)
        response = client.get(
            CONVERSATIONS, params={"from_date": DAY, "to_date": DAY}, headers=headers
        )

    assert error_of(response) == (500, "INTERNAL_ERROR")
    body = response.json()
    assert body["data"] is None
    assert body["request_id"]
    assert "fictional database failure" not in response.text
    assert portal.tables() == before
    assert len(conversations(portal, headers, from_date=DAY, to_date=DAY)["items"]) == 1


def test_the_usage_page_is_unchanged(portal: UsageEventPortal) -> None:
    """既有客户接口行为不变：用量接口与它自己的游标照旧。"""
    tenant = portal.tenant()
    older = portal.put(tenant, occurred_at=ON_DAY, conversation_id="conv-1")
    newer = portal.put(tenant, occurred_at=ON_DAY + MINUTE, conversation_id="conv-1")
    headers = portal.customer(tenant)

    first = portal.usage(headers, from_date=DAY, to_date=DAY, page_size=1)
    second = portal.usage(
        headers, from_date=DAY, to_date=DAY, page_size=1, cursor=first["next_cursor"]
    )

    assert [item["id"] for item in first["items"]] == [newer.public_id]
    assert first["next_cursor"] == encode_cursor(ON_DAY + MINUTE, newer.public_id)
    assert [item["id"] for item in second["items"]] == [older.public_id]
