"""Customer notifications end to end (AIH-TASK-127, design gate #471 v7 §2「客户接口」, §7).

`GET /api/v1/customer/notifications`、`POST /api/v1/customer/notifications/{notification_id}/read`、
`POST /api/v1/customer/notifications/read-all`：租户只取自会话。覆盖设计 §7 里属于接口的行：
列表、投递顺序与记账顺序相反、时间与记账顺序相反、记账顺序即 outbox 顺序、游标、路径参数、
标已读、只读账户、租户越权四条、鉴权；另有 MySQL 上的金额精度。「路由守卫」在
test_customer_api.py（显式清单里加了三个新路由），不在这里。

通知行直接用 `Notification` 模型插入，不经 outbox 投递；只有「记账顺序即 outbox 顺序」一条走真实的
`post_transaction` 与 outbox 投递（它要证明的就是这两步的次序）。
- SQLite：`create_all` 建表；金额只用二进制可精确表示的值（SQLite 把 NUMERIC 存成浮点）；
- MySQL：表走迁移，需要 `BILLING_TEST_DATABASE_URL`，没设时 MySQL 那一半 skip ——
  **不要把 skipped 读成 passed**，CI 里不许 skip。

⚠️ SQL 文本断言不写死方言字面量；只有 MySQL 才有的断言放在 MySQL 专用用例里。
示例 uuid 用全零占位值。
"""

from __future__ import annotations

import datetime as dt
import itertools
import re
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from fastapi import Request
from sqlalchemy import create_engine, delete, event, inspect, select, update
from sqlalchemy.orm import Session, sessionmaker
from test_customer_wallet_api import Portal, _portal, _sqlite_factory, error_of
from test_notification_delivery import _wire
from test_usage_billing import TEST_DATABASE_URL, Tenancy, _mysql_factory

from app.api import customer as customer_api
from app.api.auth import CustomerPrincipal
from app.models.auth import DomainOutbox
from app.models.notifications import Notification, NotificationType
from app.models.tenancy import AccountStatus, Tenant
from app.models.wallet import REFERENCE_TYPE_FOR, TransactionType
from app.repositories import wallet as wallet_repository
from app.schemas.customers import money_text
from app.tasks import outbox as outbox_task

NOTIFICATIONS = "/api/v1/customer/notifications"
READ_ALL = "/api/v1/customer/notifications/read-all"
LITERAL_READ = "/api/v1/customer/notifications/{notification_id}/read"

# 设计 §2：页恰好 3 个键，每条恰好 6 个键，全部已读恰好 1 个键。多一个就红。
PAGE_FIELDS = {"items", "next_cursor", "unread_count"}
ITEM_FIELDS = {
    "notification_id",
    "notification_type",
    "occurred_at",
    "balance",
    "threshold",
    "read_at",
}
MARKED_FIELDS = {"marked"}

ZERO_UUID = "00000000-0000-0000-0000-000000000000"
OCCURRED = dt.datetime(2026, 10, 27, 5, 12, 9)
CREATED = dt.datetime(2026, 10, 27, 5, 13, 0)
EARLIER_READ = dt.datetime(2026, 10, 27, 6, 0, 0)
# 与客户钱包流水 `created_at` 同一格式：不带时区的 UTC，精确到秒，不带 `Z`。
SECOND_FORMAT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}")

_SOURCES = itertools.count(1_000)


# --- 数据库 -------------------------------------------------------------------------------


def _delete_notifications(engine) -> None:
    # `fk_notifications_tenant` 是 RESTRICT：先删通知，test_usage_billing 的清场才删得掉租户。
    if inspect(engine).has_table(Notification.__tablename__):
        with engine.begin() as connection:
            connection.execute(delete(Notification))


def _mysql_factories(monkeypatch: pytest.MonkeyPatch) -> Iterator[sessionmaker[Session]]:
    """test_usage_billing's MySQL, with the notifications cleared before its clean-ups run."""
    if not TEST_DATABASE_URL:
        pytest.skip("BILLING_TEST_DATABASE_URL is not set; the MySQL half needs a real MySQL")
    engine = create_engine(TEST_DATABASE_URL)
    try:
        _delete_notifications(engine)
    finally:
        engine.dispose()
    source = _mysql_factory(monkeypatch)
    factory = next(source)
    try:
        yield factory
    finally:
        _delete_notifications(factory.kw["bind"])
        for _ in source:
            pass


@pytest.fixture(params=["sqlite", "mysql"])
def both(request, monkeypatch, tmp_path) -> Iterator[Portal]:
    """The same cases on SQLite and on a real MySQL (design §7「SQLite + MySQL 参数化」)."""
    factories = _mysql_factories(monkeypatch) if request.param == "mysql" else _sqlite_factory()
    yield from _portal(factories, tmp_path)


@pytest.fixture
def mysql_portal(monkeypatch, tmp_path) -> Iterator[Portal]:
    yield from _portal(_mysql_factories(monkeypatch), tmp_path)


# --- 帮手 ---------------------------------------------------------------------------------


def add(
    portal: Portal,
    tenant: Tenancy,
    kind: NotificationType = NotificationType.SUSPENDED,
    *,
    source_outbox_id: int | None = None,
    occurred_at: dt.datetime = OCCURRED,
    balance: str = "12.5",
    threshold: str | None = None,
    read_at: dt.datetime | None = None,
) -> dict:
    """Insert one notification row directly; return the item the API should show for it."""
    if kind is NotificationType.LOW_BALANCE and threshold is None:
        threshold = "50"
    public_id = str(uuid.uuid4())
    with portal.factory() as session:
        session.add(
            Notification(
                public_id=public_id,
                tenant_id=tenant.id,
                notification_type=kind,
                source_outbox_id=next(_SOURCES) if source_outbox_id is None else source_outbox_id,
                balance=Decimal(balance),
                threshold=None if threshold is None else Decimal(threshold),
                occurred_at=occurred_at,
                created_at=CREATED,
                read_at=read_at,
            )
        )
        session.commit()
    return {
        "notification_id": public_id,
        "notification_type": kind.value,
        "occurred_at": occurred_at.isoformat(),
        "balance": money_text(Decimal(balance)),
        "threshold": None if threshold is None else money_text(Decimal(threshold)),
        "read_at": None if read_at is None else read_at.isoformat(),
    }


def read_path(notification_id: str) -> str:
    return f"{NOTIFICATIONS}/{notification_id}/read"


def page(portal: Portal, headers: dict[str, str], **params) -> dict:
    response = portal.client.get(NOTIFICATIONS, params=params, headers=headers)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert set(data) == PAGE_FIELDS
    assert all(set(item) == ITEM_FIELDS for item in data["items"])
    return data


def walk(portal: Portal, headers: dict[str, str], **params) -> list[dict]:
    """Every page from the newest one, following `next_cursor`."""
    pages: list[dict] = []
    cursor: str | None = None
    while True:
        extra = {} if cursor is None else {"cursor": cursor}
        data = page(portal, headers, **params, **extra)
        pages.append(data)
        cursor = data["next_cursor"]
        if cursor is None:
            return pages


def mark(portal: Portal, headers: dict[str, str], notification_id: str) -> dict:
    response = portal.client.post(read_path(notification_id), headers=headers)
    assert response.status_code == 200, response.text
    item = response.json()["data"]
    assert set(item) == ITEM_FIELDS
    return item


def mark_all(portal: Portal, headers: dict[str, str]) -> int:
    response = portal.client.post(READ_ALL, headers=headers)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert set(data) == MARKED_FIELDS
    return int(data["marked"])


def read_at_of(portal: Portal, public_id: str) -> dt.datetime | None:
    with portal.factory() as session:
        statement = select(Notification.read_at).where(Notification.public_id == public_id)
        return session.execute(statement).scalar_one()


def ids(items: list[dict]) -> list[str]:
    return [item["notification_id"] for item in items]


def cursor_error(response) -> None:
    assert error_of(response) == (422, "VALIDATION_ERROR")
    assert response.json()["error"]["message"] == "Invalid request fields: query.cursor"


# --- 列表 ---------------------------------------------------------------------------------


def test_25_notifications_in_three_pages_by_booking_order(both: Portal) -> None:
    """§7「列表」：10/10/5，按 `source_outbox_id` 倒序；最后 `next_cursor` 为 `null`；
    每页 `unread_count` 相同；条目恰好 6 个键、值与库里一致。

    插入次序（通知 `id`）与时间都刻意与 `source_outbox_id` 的次序不同。
    """
    tenant = both.tenant()
    headers = both.customer(tenant)
    kinds = itertools.cycle(list(NotificationType))
    expected: dict[int, dict] = {}
    for position in range(25):
        source = 2_000 + (position * 7) % 25
        expected[source] = add(
            both,
            tenant,
            next(kinds),
            source_outbox_id=source,
            occurred_at=OCCURRED + dt.timedelta(seconds=position),
            balance="-3.5" if position % 5 == 0 else "12.5",
            read_at=EARLIER_READ if position % 4 == 0 else None,
        )
    unread = sum(1 for item in expected.values() if item["read_at"] is None)

    pages = walk(both, headers, page_size=10)

    assert [len(data["items"]) for data in pages] == [10, 10, 5]
    assert [data["unread_count"] for data in pages] == [unread] * 3
    assert pages[-1]["next_cursor"] is None
    shown = [item for data in pages for item in data["items"]]
    assert shown == [expected[source] for source in sorted(expected, reverse=True)]
    for data in pages[:-1]:
        assert data["next_cursor"] == data["items"][-1]["notification_id"]
    for item in shown:
        assert SECOND_FORMAT.fullmatch(item["occurred_at"])
        assert item["read_at"] is None or SECOND_FORMAT.fullmatch(item["read_at"])
        assert (item["threshold"] is not None) == (item["notification_type"] == "LOW_BALANCE")


def test_the_default_page_is_20_and_an_empty_list_is_fine(both: Portal) -> None:
    tenant = both.tenant()
    headers = both.customer(tenant)
    assert page(both, headers) == {"items": [], "next_cursor": None, "unread_count": 0}

    for _ in range(21):
        add(both, tenant)

    data = page(both, headers)
    assert len(data["items"]) == 20
    assert data["next_cursor"] is not None
    assert data["unread_count"] == 21


@pytest.mark.parametrize("page_size", ["0", "101", "ten"])
def test_page_size_out_of_range_is_422(both: Portal, page_size: str) -> None:
    tenant = both.tenant()
    headers = both.customer(tenant)

    response = both.client.get(NOTIFICATIONS, params={"page_size": page_size}, headers=headers)

    assert error_of(response) == (422, "VALIDATION_ERROR")


def test_delivery_order_opposite_to_booking_order(both: Portal) -> None:
    """§7「投递顺序与记账顺序相反」：同一 `occurred_at`；`REACTIVATED` 的 `source_outbox_id` 大、
    通知 `id` 小（先插入）。按记账顺序，它在第一页；游标跨这一对不重复、不遗漏。"""
    tenant = both.tenant()
    headers = both.customer(tenant)
    reactivated = add(both, tenant, NotificationType.REACTIVATED, source_outbox_id=11)
    suspended = add(both, tenant, NotificationType.SUSPENDED, source_outbox_id=10)
    with both.factory() as session:
        rows = dict(
            session.execute(
                select(Notification.source_outbox_id, Notification.id).where(
                    Notification.tenant_id == tenant.id
                )
            ).all()
        )
    assert rows[11] < rows[10], "用例前提：通知 id 与记账顺序相反"

    pages = walk(both, headers, page_size=1)

    assert [data["items"] for data in pages] == [[reactivated], [suspended]]


def test_time_opposite_to_booking_order_is_not_reordered(both: Portal) -> None:
    """§7「时间与记账顺序相反（跨秒）」。

    先取时间的充值后入账，`REACTIVATED` 显示 10:00:03 仍在前。
    """
    tenant = both.tenant()
    headers = both.customer(tenant)
    suspended = add(
        both,
        tenant,
        NotificationType.SUSPENDED,
        source_outbox_id=10,
        occurred_at=dt.datetime(2026, 10, 27, 10, 0, 5),
        balance="0",
    )
    reactivated = add(
        both,
        tenant,
        NotificationType.REACTIVATED,
        source_outbox_id=11,
        occurred_at=dt.datetime(2026, 10, 27, 10, 0, 3),
        balance="5",
    )

    pages = walk(both, headers, page_size=1)

    assert [data["items"] for data in pages] == [[reactivated], [suspended]]
    assert pages[0]["items"][0]["occurred_at"] == "2026-10-27T10:00:03"


def test_booking_order_is_outbox_order(mysql_portal: Portal, monkeypatch) -> None:
    """§7「记账顺序即 outbox 顺序」（MySQL）：A 先取时间、等钱包锁；B 扣款停机并提交；A 再充值复机。

    `REACTIVATED` 的 `source_outbox_id` 大于 `SUSPENDED` 的；投递后列表第一条是 `REACTIVATED`，
    尽管它的 `occurred_at` 更早。
    """
    portal = mysql_portal
    tenant = portal.tenant()
    headers = portal.customer(tenant)
    portal.world.fund(tenant, "10")
    with portal.factory() as session:
        session.execute(delete(DomainOutbox).where(DomainOutbox.aggregate_id == tenant.public_id))
        session.commit()
    a_time = dt.datetime(2026, 10, 27, 10, 0, 3)
    b_time = dt.datetime(2026, 10, 27, 10, 0, 5)

    def post(session: Session, kind: TransactionType, amount: str, now: dt.datetime) -> None:
        wallet_repository.post_transaction(
            session,
            tenant_id=tenant.id,
            transaction_type=kind,
            amount=Decimal(amount),
            reference_type=REFERENCE_TYPE_FOR[kind],
            reference_id=str(uuid.uuid4()),
            now=now,
            created_by=portal.world.admin.id,
            description="Fictional adjustment for a notification test",
            actor_role="ADMIN",
        )

    def session_a() -> None:
        # A 的时间早已取好（`a_time`），这里只是等钱包锁。
        with portal.factory() as session:
            post(session, TransactionType.ADJUSTMENT_CREDIT, "5", a_time)
            session.commit()

    # 池在外层：断言失败时先关 B 的会话（回滚放锁），再等 A 结束。
    with ThreadPoolExecutor(max_workers=1) as pool, portal.factory() as session_b:
        post(session_b, TransactionType.ADJUSTMENT_DEBIT, "-10", b_time)
        waiting = pool.submit(session_a)
        time.sleep(0.5)
        assert not waiting.done(), "A 应当在等 B 持有的钱包锁"
        session_b.commit()
        waiting.result(timeout=30)

    with portal.factory() as session:
        events = session.execute(
            select(DomainOutbox.id, DomainOutbox.payload_json)
            .where(DomainOutbox.aggregate_id == tenant.public_id)
            .order_by(DomainOutbox.id)
        ).all()
    assert len(events) == 2
    assert '"SUSPENDED"' in (events[0].payload_json or "")
    assert '"ACTIVE"' in (events[1].payload_json or "")

    _wire(monkeypatch, portal.factory)
    # 先投递后记账的那条：投递顺序不影响列表。
    assert [outbox_task.deliver(row.id) for row in reversed(events)] == ["notified", "notified"]

    items = page(portal, headers)["items"]
    assert [item["notification_type"] for item in items] == ["REACTIVATED", "SUSPENDED"]
    assert [item["occurred_at"] for item in items] == [a_time.isoformat(), b_time.isoformat()]
    with portal.factory() as session:
        sources = dict(
            session.execute(
                select(Notification.notification_type, Notification.source_outbox_id).where(
                    Notification.tenant_id == tenant.id
                )
            ).all()
        )
    assert sources[NotificationType.REACTIVATED] > sources[NotificationType.SUSPENDED]


# --- 游标 ---------------------------------------------------------------------------------


def test_bad_cursors_are_422_after_authentication(both: Portal) -> None:
    """§7「游标」：非 UUID、大写 UUID、不存在的 UUID 都是 422 `cursor`。

    未登录带非法游标仍是 401。
    """
    tenant = both.tenant()
    headers = both.customer(tenant)
    existing = add(both, tenant)["notification_id"]
    add(both, tenant)

    for cursor in ("not-a-cursor", existing.upper(), ZERO_UUID, f"{existing} ", ""):
        response = both.client.get(NOTIFICATIONS, params={"cursor": cursor}, headers=headers)
        cursor_error(response)

    anonymous = both.client.get(NOTIFICATIONS, params={"cursor": "not-a-cursor"})
    assert error_of(anonymous) == (401, "TOKEN_INVALID")
    admin = both.client.get(
        NOTIFICATIONS, params={"cursor": "not-a-cursor"}, headers=both.admin_caller()
    )
    assert error_of(admin) == (403, "CUSTOMER_REQUIRED")

    # 合法的游标照常翻页。
    data = page(both, headers, cursor=existing)
    assert data["items"] == []


# --- 路径参数与标已读 ---------------------------------------------------------------------------


def test_a_malformed_path_parameter_is_404_not_422(both: Portal) -> None:
    """§7「路径参数」：鉴权之后，不是小写规范 UUID 与查不到同是 404；未登录的字面路径是 401。"""
    tenant = both.tenant()
    headers = both.customer(tenant)
    existing = add(both, tenant)["notification_id"]

    for value in ("not-a-uuid", existing.upper(), ZERO_UUID, "{notification_id}"):
        response = both.client.post(read_path(value), headers=headers)
        assert error_of(response) == (404, "NOTIFICATION_NOT_FOUND")
    assert read_at_of(both, existing) is None

    anonymous = both.client.post(LITERAL_READ)
    assert error_of(anonymous) == (401, "TOKEN_INVALID")


def test_marking_read_once_then_again_then_all(both: Portal) -> None:
    """§7「标已读」：`read_at` 首次写入后不变；`unread_count` 递减；`marked` 依次为 N、0。

    不写审计、不碰 outbox、钱包与租户（设计 §6）。
    """
    tenant = both.tenant()
    headers = both.customer(tenant)
    first = add(both, tenant, NotificationType.LOW_BALANCE, threshold="50")
    already = add(both, tenant, NotificationType.SUSPENDED, read_at=EARLIER_READ)
    others = [add(both, tenant, NotificationType.REACTIVATED) for _ in range(3)]
    assert page(both, headers)["unread_count"] == 4
    footprint = both.footprint()

    marked = mark(both, headers, first["notification_id"])
    assert marked == {**first, "read_at": marked["read_at"]}
    assert marked["read_at"] is not None and SECOND_FORMAT.fullmatch(marked["read_at"])
    stored = read_at_of(both, first["notification_id"])
    assert stored is not None and stored.isoformat() == marked["read_at"]
    assert page(both, headers)["unread_count"] == 3

    again = mark(both, headers, first["notification_id"])
    assert again == marked
    # 已读的再标一次：保留首次时刻（这一行的首次时刻是刻意写早的，任何重写都看得出来）。
    assert mark(both, headers, already["notification_id"]) == already
    assert read_at_of(both, already["notification_id"]) == EARLIER_READ
    assert page(both, headers)["unread_count"] == 3

    assert mark_all(both, headers) == len(others)
    data = page(both, headers)
    assert data["unread_count"] == 0
    assert all(item["read_at"] is not None for item in data["items"])
    assert read_at_of(both, already["notification_id"]) == EARLIER_READ
    assert read_at_of(both, first["notification_id"]) == stored

    assert mark_all(both, headers) == 0
    assert both.footprint() == footprint


def test_notifications_arriving_after_read_all_are_unread(both: Portal) -> None:
    tenant = both.tenant()
    headers = both.customer(tenant)
    add(both, tenant)
    assert mark_all(both, headers) == 1

    late = add(both, tenant, NotificationType.REACTIVATED)

    data = page(both, headers)
    assert data["unread_count"] == 1
    assert data["items"][0] == late


# --- 只读账户 -------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [AccountStatus.DISABLED, AccountStatus.CLOSED])
def test_a_read_only_account_can_list_but_not_mark(both: Portal, status: AccountStatus) -> None:
    """§7「只读账户」：列表 200；两个写接口 403 `ACCOUNT_READ_ONLY`，`read_at` 不变。"""
    tenant = both.tenant()
    headers = both.customer(tenant)
    item = add(both, tenant)
    both.set_account_status(tenant, status)

    assert page(both, headers)["items"] == [item]
    for response in (
        both.client.post(read_path(item["notification_id"]), headers=headers),
        both.client.post(READ_ALL, headers=headers),
    ):
        assert error_of(response) == (403, "ACCOUNT_READ_ONLY")
    assert read_at_of(both, item["notification_id"]) is None


@pytest.mark.parametrize("target", ["one", "all"])
def test_disabled_after_the_early_check_is_refused_by_the_lock(
    both: Portal, monkeypatch, target: str
) -> None:
    """依赖放行之后、写事务锁租户之前被停用：锁内重查拒绝，`read_at` 不变。

    设计 §4「客户写接口」。
    """
    tenant = both.tenant()
    headers = both.customer(tenant)
    item = add(both, tenant)
    real = customer_api.require_customer_writable

    def then_disable(request: Request) -> CustomerPrincipal:
        principal = real(request)
        with both.factory() as session:
            session.execute(
                update(Tenant)
                .where(Tenant.id == tenant.id)
                .values(account_status=AccountStatus.DISABLED)
            )
            session.commit()
        return principal

    monkeypatch.setattr(customer_api, "require_customer_writable", then_disable)
    path = read_path(item["notification_id"]) if target == "one" else READ_ALL

    response = both.client.post(path, headers=headers)

    assert error_of(response) == (403, "ACCOUNT_READ_ONLY")
    assert read_at_of(both, item["notification_id"]) is None


# --- 租户越权 -------------------------------------------------------------------------------


def test_another_customers_notifications_are_out_of_reach(both: Portal) -> None:
    """§7「租户越权」四条：A 用 B 的 id 标已读 404；A 用 B 的 id 当游标 422；A 全部已读不动 B；
    A 的列表里没有 B 的行。"""
    mine = both.world.tenant("mine")
    theirs = both.world.tenant("theirs")
    my_headers = both.customer(mine)
    their_headers = both.customer(theirs)
    my_item = add(both, mine, source_outbox_id=20)
    their_item = add(both, theirs, source_outbox_id=30)
    add(both, theirs, source_outbox_id=10)
    their_id = their_item["notification_id"]

    assert error_of(both.client.post(read_path(their_id), headers=my_headers)) == (
        404,
        "NOTIFICATION_NOT_FOUND",
    )
    cursor_error(both.client.get(NOTIFICATIONS, params={"cursor": their_id}, headers=my_headers))
    assert mark_all(both, my_headers) == 1
    assert read_at_of(both, their_id) is None

    mine_page = page(both, my_headers)
    assert ids(mine_page["items"]) == [my_item["notification_id"]]
    assert mine_page["unread_count"] == 0
    theirs_page = page(both, their_headers)
    assert theirs_page["unread_count"] == 2
    assert their_id in ids(theirs_page["items"])


def test_every_notification_statement_carries_the_tenant(both: Portal) -> None:
    """INV-8：三个接口发出的每条读写 `notifications` 的 SQL 都带 `tenant_id` 条件；不碰 outbox。"""
    tenant = both.tenant()
    headers = both.customer(tenant)
    first = add(both, tenant)["notification_id"]
    add(both, tenant)
    statements: list[str] = []

    def record(_conn, _cursor, statement, _params, _context, _many) -> None:
        statements.append(statement)

    engine = both.factory.kw["bind"]
    event.listen(engine, "before_cursor_execute", record)
    try:
        cursor = page(both, headers, page_size=1)["next_cursor"]
        page(both, headers, page_size=1, cursor=cursor)
        mark(both, headers, first)
        mark_all(both, headers)
    finally:
        event.remove(engine, "before_cursor_execute", record)

    table = re.compile(rf"\b{Notification.__tablename__}\b")
    touching = [statement for statement in statements if table.search(statement)]
    # 列表 2 次 × (列表 + 未读数) + 游标查找 + 标已读的 UPDATE 与读回 + 全部已读。
    assert len(touching) >= 8, touching
    for statement in touching:
        assert "tenant_id" in statement, statement
    outbox = re.compile(rf"\b{DomainOutbox.__tablename__}\b")
    assert not [statement for statement in statements if outbox.search(statement)]


# --- 鉴权 ---------------------------------------------------------------------------------


def test_admins_and_anonymous_callers_are_refused(both: Portal) -> None:
    """§7「鉴权」：ADMIN 403 `CUSTOMER_REQUIRED`，未登录 401；ADMIN 的写不改任何行。"""
    tenant = both.tenant()
    item = add(both, tenant)
    admin = both.admin_caller()
    calls = (
        ("GET", NOTIFICATIONS),
        ("POST", read_path(item["notification_id"])),
        ("POST", READ_ALL),
    )

    for method, path in calls:
        assert error_of(both.client.request(method, path, headers=admin)) == (
            403,
            "CUSTOMER_REQUIRED",
        )
        assert error_of(both.client.request(method, path)) == (401, "TOKEN_INVALID")
    assert read_at_of(both, item["notification_id"]) is None


# --- 金额精度（仅 MySQL） ---------------------------------------------------------------------


def test_amounts_round_trip_exactly(mysql_portal: Portal) -> None:
    """§7「金额精度」：DECIMAL(20,8) 的两端值原样回到接口；先证明它们经 float 往返会变。"""
    balance, threshold = "999999999999.99999999", "0.00000001"
    assert Decimal(float(balance)) != Decimal(balance)
    assert Decimal(float(threshold)) != Decimal(threshold)
    portal = mysql_portal
    tenant = portal.tenant()
    headers = portal.customer(tenant)
    item = add(portal, tenant, NotificationType.LOW_BALANCE, balance=balance, threshold=threshold)

    shown = page(portal, headers)["items"]

    assert shown == [item]
    assert shown[0]["balance"] == balance
    assert shown[0]["threshold"] == threshold
    with portal.factory() as session:
        stored = session.execute(
            select(Notification.balance, Notification.threshold).where(
                Notification.public_id == item["notification_id"]
            )
        ).one()
    assert money_text(stored.balance) == balance
    assert stored.threshold is not None and money_text(stored.threshold) == threshold
