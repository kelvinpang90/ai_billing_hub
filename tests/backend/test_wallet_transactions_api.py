"""Admin wallet ledger view end to end (AIH-TASK-084, design gate #351 v2 §7).

`GET /api/v1/admin/customers/{customer_id}/wallet/transactions`：只读、序号游标、最新在前。
§7 的「索引存在且可用」「迁移往返」两行属于 AIH-TASK-083（test_migrations.py），不在这里。

账本行由真实的 `post_transaction` 与计费 worker 写入（test_usage_billing.World）：
- SQLite：`create_all` 建表，再装上 test_usage_billing 那条代替触发器推进钱包的测试触发器；
- MySQL：表与触发器都走迁移，需要 `BILLING_TEST_DATABASE_URL`，没设时 MySQL 那一半 skip ——
  **不要把 skipped 读成 passed**，CI 里不许 skip。

示例 uuid 用全零占位值。
"""

from __future__ import annotations

import base64
import os
from collections.abc import Iterator
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from test_usage_billing import (
    _SQLITE_LEDGER_TRIGGER,
    T0,
    TEST_EMAIL_DOMAIN,
    Tenancy,
    _mysql_factory,
    seed_meter_types,
)
from test_usage_billing import World as BillingWorld

from app.core.config import Settings
from app.core.database import create_session_factory
from app.core.tokens import issue_access_token
from app.main import create_app
from app.models.auth import AuditLog, DomainOutbox, User, UserRole, UserStatus
from app.models.base import Base
from app.models.tenancy import Tenant
from app.models.usage import UsageEvent
from app.models.wallet import (
    REFERENCE_TYPE_FOR,
    ReferenceType,
    TransactionType,
    Wallet,
    WalletTransaction,
)
from app.repositories import wallet as wallet_repository
from app.schemas.customers import money_text
from app.services.auth import utc_now

CUSTOMERS = "/api/v1/admin/customers"
MISSING_CUSTOMER = "00000000-0000-4000-8000-000000000000"

# 设计 §2：`data` 恰好三个键，每项恰好十二个键。多一个（内部 id、成本、metadata_json）就红。
PAGE_FIELDS = {"items", "page_size", "next_before_sequence"}
ITEM_FIELDS = {
    "id",
    "wallet_sequence",
    "transaction_type",
    "amount",
    "balance_before",
    "balance_after",
    "reference_type",
    "reference_id",
    "usage_event_id",
    "description",
    "created_by_email",
    "created_at",
}
MAX_BIGINT = 2**63 - 1


def settings_for(tmp_path) -> Settings:
    jwt, master = tmp_path / "jwt.key", tmp_path / "master.key"
    jwt.write_text("a-test-signing-key-that-is-long-enough", encoding="utf-8")
    master.write_text(f"1:{base64.b64encode(os.urandom(32)).decode()}\n", encoding="utf-8")
    return Settings(jwt_secret_file=str(jwt), master_key_file=str(master), database_url="")


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
    factory = create_session_factory(engine)
    seed_meter_types(factory)
    yield factory
    engine.dispose()


class Ledger:
    """The app and the billing world on one database, plus helpers to write and read the ledger."""

    def __init__(self, factory: sessionmaker[Session], tmp_path) -> None:
        self.factory = factory
        self.app = create_app(settings_for(tmp_path))
        self.app.state.engine = factory.kw["bind"]
        self.app.state.session_factory = factory
        self.client = TestClient(self.app)
        self.world = BillingWorld(factory)
        self.admin = self.token(self.world.admin.id, UserRole.ADMIN)
        self._priced = False

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

    # -- writing the ledger --

    def tenant(self) -> Tenancy:
        return self.world.tenant()

    def post(
        self,
        tenant: Tenancy,
        kind: TransactionType,
        amount: str,
        *,
        by_admin: bool = False,
        description: str | None = None,
    ) -> None:
        """One posting through the real repository; the source type follows the type."""
        with self.factory() as session:
            wallet_repository.post_transaction(
                session,
                tenant_id=tenant.id,
                transaction_type=kind,
                amount=Decimal(amount),
                reference_type=REFERENCE_TYPE_FOR[kind],
                reference_id=f"{kind.value.lower()}-{tenant.id}-{self.sequence(tenant) + 1}",
                now=T0,
                created_by=self.world.admin.id if by_admin else None,
                description=description,
                actor_role="ADMIN" if by_admin else None,
            )
            session.commit()

    def adjust(self, tenant: Tenancy, kind: TransactionType, amount: str) -> None:
        self.post(tenant, kind, amount, by_admin=True, description="Fictional adjustment")

    def bill_one(self, tenant: Tenancy) -> str:
        """One usage event priced and posted by the real billing worker; its `public_id`."""
        if not self._priced:
            self.world.publish_markup_usd()
            self._priced = True
        event_id = self.world.event(tenant)
        assert self.world.bill().claimed == 1
        row = self.world.row(event_id)
        assert row.wallet_transaction_id is not None
        return row.public_id

    # -- reading --

    def get(self, customer_id: str, headers: dict[str, str] | None = None, **params):
        return self.client.get(
            f"{CUSTOMERS}/{customer_id}/wallet/transactions",
            params=params,
            headers=self.admin if headers is None else headers,
        )

    def page(self, tenant: Tenancy, **params) -> dict:
        response = self.get(tenant.public_id, **params)
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert set(data) == PAGE_FIELDS
        assert all(set(item) == ITEM_FIELDS for item in data["items"])
        return data

    def walk(self, tenant: Tenancy, **params) -> tuple[list[list[int]], list[int | None], list]:
        """Every page from the newest, following `next_before_sequence`."""
        sequences: list[list[int]] = []
        cursors: list[int | None] = []
        items: list = []
        cursor: int | None = None
        while True:
            extra = {} if cursor is None else {"before_sequence": cursor}
            data = self.page(tenant, **params, **extra)
            sequences.append([item["wallet_sequence"] for item in data["items"]])
            items.extend(data["items"])
            cursor = data["next_before_sequence"]
            cursors.append(cursor)
            if cursor is None:
                return sequences, cursors, items

    def sequence(self, tenant: Tenancy) -> int:
        with self.factory() as session:
            statement = select(Wallet.version).where(Wallet.tenant_id == tenant.id)
            return int(session.execute(statement).scalar_one())

    def stored(self, tenant: Tenancy) -> dict[int, dict]:
        """What each row should look like, read straight from the tables, not via the repository."""
        with self.factory() as session:
            rows = session.execute(
                select(WalletTransaction).where(WalletTransaction.tenant_id == tenant.id)
            ).scalars()
            expected = {}
            for row in rows:
                event_id = session.execute(
                    select(UsageEvent.public_id).where(UsageEvent.wallet_transaction_id == row.id)
                ).scalar_one_or_none()
                email = None
                if row.created_by is not None:
                    email = session.get(User, row.created_by).email
                expected[row.wallet_sequence] = {
                    "id": row.public_id,
                    "wallet_sequence": row.wallet_sequence,
                    "transaction_type": TransactionType(row.transaction_type).value,
                    "amount": money_text(row.amount),
                    "balance_before": money_text(row.balance_before),
                    "balance_after": money_text(row.balance_after),
                    "reference_type": ReferenceType(row.reference_type).value,
                    "reference_id": row.reference_id,
                    "usage_event_id": event_id,
                    "description": row.description,
                    "created_by_email": email,
                    "created_at": row.created_at.isoformat(),
                }
            return expected

    def footprint(self) -> tuple[dict[str, int], list[tuple[int, Decimal, int]]]:
        """Row counts of every table a write would touch, and every wallet's balance and version."""
        with self.factory() as session:
            counts = {
                model.__tablename__: int(
                    session.execute(select(func.count()).select_from(model)).scalar_one()
                )
                for model in (WalletTransaction, Wallet, Tenant, AuditLog, DomainOutbox)
            }
            wallets = session.execute(
                select(Wallet.tenant_id, Wallet.balance, Wallet.version).order_by(Wallet.id)
            ).all()
            return counts, [tuple(row) for row in wallets]


def _ledger(factories: Iterator[sessionmaker[Session]], tmp_path) -> Iterator[Ledger]:
    factory = next(factories)
    try:
        yield Ledger(factory, tmp_path)
    finally:
        for _ in factories:
            pass


@pytest.fixture
def ledger(tmp_path) -> Iterator[Ledger]:
    yield from _ledger(_sqlite_factory(), tmp_path)


@pytest.fixture(params=["sqlite", "mysql"])
def both(request, monkeypatch, tmp_path) -> Iterator[Ledger]:
    """The same cases on SQLite and on a real MySQL (design §7「API（SQLite + MySQL）」)."""
    factories = _mysql_factory(monkeypatch) if request.param == "mysql" else _sqlite_factory()
    yield from _ledger(factories, tmp_path)


@pytest.fixture
def mysql_ledger(monkeypatch, tmp_path) -> Iterator[Ledger]:
    yield from _ledger(_mysql_factory(monkeypatch), tmp_path)


def five_rows(ledger: Ledger) -> tuple[Tenancy, str]:
    """Sequences 1–5: an adjustment, a billed usage row, a rebill, a system correction, a debit.

    充值（PAYMENT）以外的四种来源各至少一行；返回客户与用量事件的 `public_id`。
    """
    tenant = ledger.tenant()
    ledger.adjust(tenant, TransactionType.ADJUSTMENT_CREDIT, "100")
    usage_event_id = ledger.bill_one(tenant)
    ledger.post(tenant, TransactionType.REBILL_CREDIT, "1.5")
    ledger.post(
        tenant, TransactionType.SYSTEM_CORRECTION, "-0.25", description="Fictional correction"
    )
    ledger.adjust(tenant, TransactionType.ADJUSTMENT_DEBIT, "-2")
    assert ledger.sequence(tenant) == 5
    return tenant, usage_event_id


# --- 正常路径、只读、无成本毛利 --------------------------------------------------------


def test_three_pages_newest_first_match_the_stored_rows(both: Ledger) -> None:
    """§7「正常路径」：page_size=2 连翻三页，序号 5,4 / 3,2 / 1，游标 4、2、null（v2）。"""
    tenant, usage_event_id = five_rows(both)
    stored = both.stored(tenant)
    before = both.footprint()

    first = both.page(tenant, page_size=2)
    second = both.page(tenant, page_size=2, before_sequence=first["next_before_sequence"])
    third = both.page(tenant, page_size=2, before_sequence=second["next_before_sequence"])

    pages = (first, second, third)
    assert [[item["wallet_sequence"] for item in page["items"]] for page in pages] == [
        [5, 4],
        [3, 2],
        [1],
    ]
    assert [page["next_before_sequence"] for page in pages] == [4, 2, None]
    assert {page["page_size"] for page in pages} == {2}
    items = {item["wallet_sequence"]: item for page in pages for item in page["items"]}
    assert items == stored
    email = both.world.admin.email
    assert (items[2]["usage_event_id"], items[2]["created_by_email"]) == (usage_event_id, None)
    assert (items[1]["usage_event_id"], items[1]["created_by_email"]) == (None, email)
    assert (items[5]["usage_event_id"], items[5]["created_by_email"]) == (None, email)
    for system in (items[3], items[4]):
        assert (system["usage_event_id"], system["created_by_email"]) == (None, None)
    assert [items[n]["reference_type"] for n in (1, 2, 3, 4, 5)] == [
        "ADMIN_ADJUSTMENT",
        "USAGE_EVENT",
        "REBILL",
        "SYSTEM",
        "ADMIN_ADJUSTMENT",
    ]
    # 只读（INV-2/3/4）：调用前后账本、钱包、租户、审计、outbox 的行数与每个钱包的余额、版本不变。
    assert both.footprint() == before


def test_items_carry_no_cost_margin_or_metadata(ledger: Ledger) -> None:
    """§7「无成本毛利」（INV-7）：用量事件带成本与毛利快照，响应里只有设计 §2 的 12 个键。"""
    tenant, usage_event_id = five_rows(ledger)
    with ledger.factory() as session:
        snapshot = session.execute(
            select(UsageEvent.estimated_provider_cost_myr, UsageEvent.billable_cost).where(
                UsageEvent.public_id == usage_event_id
            )
        ).one()
    assert all(value > 0 for value in snapshot)

    response = ledger.get(tenant.public_id, page_size=100)

    assert response.status_code == 200
    items = response.json()["data"]["items"]
    assert len(items) == 5
    assert all(set(item) == ITEM_FIELDS for item in items)
    for forbidden in ("cost", "margin", "metadata", "tenant_id", "wallet_id", 'created_by"'):
        assert forbidden not in response.text


def test_an_empty_ledger_is_an_empty_last_page(ledger: Ledger) -> None:
    """§7「空账本」。"""
    tenant = ledger.tenant()

    data = ledger.page(tenant)

    assert data == {"items": [], "page_size": 20, "next_before_sequence": None}


def test_a_usage_row_without_its_event_is_still_listed(ledger: Ledger) -> None:
    """审查提出的场景（设计 §5 最后一行）：查不到用量事件时 `usage_event_id` 为 null，其余照常。"""
    tenant = ledger.tenant()
    ledger.adjust(tenant, TransactionType.ADJUSTMENT_CREDIT, "10")
    ledger.post(tenant, TransactionType.AI_USAGE, "-1")

    data = ledger.page(tenant)

    assert [item["wallet_sequence"] for item in data["items"]] == [2, 1]
    orphan = data["items"][0]
    assert orphan == ledger.stored(tenant)[2]
    assert (orphan["reference_type"], orphan["usage_event_id"]) == ("USAGE_EVENT", None)
    assert orphan["amount"] == "-1.00000000"


# --- 筛选 ---------------------------------------------------------------------------


def test_filtering_by_source_pages_through_only_that_source(ledger: Ledger) -> None:
    """§7「筛选」：交错写入调账与用量，`reference_type=ADMIN_ADJUSTMENT&page_size=1` 翻到底。"""
    tenant = ledger.tenant()
    for _ in range(3):
        ledger.adjust(tenant, TransactionType.ADJUSTMENT_CREDIT, "10")
        ledger.post(tenant, TransactionType.AI_USAGE, "-1")

    adjustments, cursors, items = ledger.walk(
        tenant, reference_type="ADMIN_ADJUSTMENT", page_size=1
    )
    usage, _, usage_items = ledger.walk(tenant, reference_type="USAGE_EVENT", page_size=2)

    # 带筛选时序号不连续：游标只能是本页最后一行的序号（设计 v2 §2）。
    assert adjustments == [[5], [3], [1]]
    assert cursors == [5, 3, None]
    assert {item["reference_type"] for item in items} == {"ADMIN_ADJUSTMENT"}
    assert usage == [[6, 4], [2]]
    assert {item["reference_type"] for item in usage_items} == {"USAGE_EVENT"}


# --- 边界值与未知参数 -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("page_size", "0"),
        ("page_size", "101"),
        ("page_size", "1.5"),
        ("page_size", "twenty"),
        ("before_sequence", "0"),
        ("before_sequence", "-1"),
        ("before_sequence", str(MAX_BIGINT + 1)),
        ("before_sequence", "ten"),
        ("reference_type", "admin_adjustment"),
        ("reference_type", "TOPUP"),
    ],
)
def test_out_of_range_parameters_are_422_without_echoing_the_value(
    ledger: Ledger, name: str, value: str
) -> None:
    """§7「边界值」：422 VALIDATION_ERROR，文案只列字段名、不回显值。"""
    tenant = ledger.tenant()

    response = ledger.get(tenant.public_id, **{name: value})

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert name in error["message"]
    assert value not in error["message"]


def test_the_largest_page_and_the_edges_of_the_cursor_are_200(ledger: Ledger) -> None:
    """§7「边界值」：page_size=100、before_sequence=1（空页）与 BIGINT 上界都是 200。"""
    tenant = ledger.tenant()
    ledger.adjust(tenant, TransactionType.ADJUSTMENT_CREDIT, "10")

    assert [item["wallet_sequence"] for item in ledger.page(tenant, page_size=100)["items"]] == [1]
    assert ledger.page(tenant, before_sequence=1) == {
        "items": [],
        "page_size": 20,
        "next_before_sequence": None,
    }
    newest = ledger.page(tenant, before_sequence=MAX_BIGINT)
    assert [item["wallet_sequence"] for item in newest["items"]] == [1]


def test_unknown_query_parameters_are_ignored(ledger: Ledger) -> None:
    """设计 §2：未知查询参数不报错（与现有列表接口一致）。"""
    tenant = ledger.tenant()
    ledger.adjust(tenant, TransactionType.ADJUSTMENT_CREDIT, "10")

    data = ledger.page(tenant, page=3, total="yes")

    assert [item["wallet_sequence"] for item in data["items"]] == [1]


# --- 并发、重放、失败 -------------------------------------------------------------------


def test_postings_between_pages_neither_repeat_nor_skip_older_rows(mysql_ledger: Ledger) -> None:
    """§7「并发」（MySQL）：取第一页后另一事务记两笔，第二页与没有新记账时逐字相同。"""
    ledger = mysql_ledger
    tenant = ledger.tenant()
    ledger.adjust(tenant, TransactionType.ADJUSTMENT_CREDIT, "100")
    for _ in range(4):
        ledger.post(tenant, TransactionType.AI_USAGE, "-1")
    first = ledger.page(tenant, page_size=2)
    quiet_second = ledger.page(tenant, page_size=2, before_sequence=first["next_before_sequence"])

    # 另一个会话、另一个事务里记两笔新账并提交。
    ledger.post(tenant, TransactionType.AI_USAGE, "-1")
    ledger.adjust(tenant, TransactionType.ADJUSTMENT_CREDIT, "5")
    busy_second = ledger.page(tenant, page_size=2, before_sequence=first["next_before_sequence"])
    refetched_first = ledger.page(tenant, page_size=2)

    assert [item["wallet_sequence"] for item in first["items"]] == [5, 4]
    assert busy_second == quiet_second
    assert [item["wallet_sequence"] for item in quiet_second["items"]] == [3, 2]
    assert [item["wallet_sequence"] for item in refetched_first["items"]] == [7, 6]


def test_the_same_request_twice_returns_the_same_page(ledger: Ledger) -> None:
    """§7「幂等重放」：GET 天然可重复；期间没有新记账时两次的 data 相同。"""
    tenant, _ = five_rows(ledger)

    first = ledger.get(tenant.public_id, page_size=2, before_sequence=4)
    again = ledger.get(tenant.public_id, page_size=2, before_sequence=4)

    assert (first.status_code, again.status_code) == (200, 200)
    assert first.json()["data"] == again.json()["data"]


def test_a_failing_query_is_a_500_with_a_request_id_and_writes_nothing(
    ledger: Ledger, monkeypatch
) -> None:
    """§7「事务中途失败」：repository 抛数据库异常 → 500 带 request_id，各表行数不变。"""
    tenant = ledger.tenant()
    ledger.adjust(tenant, TransactionType.ADJUSTMENT_CREDIT, "10")
    before = ledger.footprint()

    def broken(*_args, **_kwargs):
        raise OperationalError("SELECT", {}, Exception("fictional database failure"))

    monkeypatch.setattr(wallet_repository, "list_transactions_for_tenant", broken)
    response = TestClient(ledger.app, raise_server_exceptions=False).get(
        f"{CUSTOMERS}/{tenant.public_id}/wallet/transactions", headers=ledger.admin
    )

    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert body["request_id"]
    assert "fictional database failure" not in response.text
    assert ledger.footprint() == before


# --- 金额精度 -------------------------------------------------------------------------


def test_amounts_round_trip_exactly_at_the_extremes(both: Ledger) -> None:
    """§7「金额精度」（INV-10）：最小金额、`DECIMAL(20,8)` 上界与负余额，字符串逐字相等。

    ⚠️ 上界只在 MySQL 上断言：SQLite 把 NUMERIC 存成浮点（约 15–17 位有效数字），20 位有效数字
    的上界在库里就已经变了，测不到接口。SQLite 上换一个它能精确存下的大数，照样逐字比对。
    """
    if both.factory.kw["bind"].dialect.name == "mysql":
        top, top_less_one = "999999999999.99999999", "999999999999.99999998"
    else:
        top, top_less_one = "1234567.12345678", "1234567.12345677"
    rich = both.tenant()
    both.adjust(rich, TransactionType.ADJUSTMENT_CREDIT, top)
    both.post(rich, TransactionType.AI_USAGE, "-0.00000001")
    poor = both.tenant()
    both.post(poor, TransactionType.AI_USAGE, "-0.00000001")

    rich_items = both.page(rich)["items"]
    [poor_item] = both.page(poor)["items"]

    assert [
        (item["amount"], item["balance_before"], item["balance_after"]) for item in rich_items
    ] == [
        ("-0.00000001", top, top_less_one),
        (top, "0.00000000", top),
    ]
    assert (poor_item["amount"], poor_item["balance_before"], poor_item["balance_after"]) == (
        "-0.00000001",
        "0.00000000",
        "-0.00000001",
    )
    stored = both.stored(rich)
    assert {item["wallet_sequence"]: item for item in rich_items} == stored


# --- 租户与鉴权 -----------------------------------------------------------------------


def test_another_customers_cursor_only_pages_through_this_customer(ledger: Ledger) -> None:
    """§7「租户越权」（INV-8）：以 A 的路径带 B 的序号，只返回 A 的行；A 的任何页都不含 B 的行。"""
    mine = ledger.tenant()
    theirs = ledger.tenant()
    for _ in range(3):
        ledger.adjust(mine, TransactionType.ADJUSTMENT_CREDIT, "1")
    for _ in range(5):
        ledger.adjust(theirs, TransactionType.ADJUSTMENT_CREDIT, "2")
    their_ids = {item["id"] for item in ledger.stored(theirs).values()}

    borrowed = ledger.page(mine, before_sequence=5)
    _, _, every_page = ledger.walk(mine, page_size=1)

    assert [item["wallet_sequence"] for item in borrowed["items"]] == [3, 2, 1]
    assert {item["id"] for item in borrowed["items"]} == {
        item["id"] for item in ledger.stored(mine).values()
    }
    assert not {item["id"] for item in every_page} & their_ids
    assert {item["amount"] for item in every_page} == {"1.00000000"}


def test_an_unknown_customer_is_404(ledger: Ledger) -> None:
    response = ledger.get(MISSING_CUSTOMER)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "CUSTOMER_NOT_FOUND"


@pytest.mark.parametrize(
    ("caller", "expected"),
    [
        ("anonymous", (401, "TOKEN_INVALID")),
        ("customer", (403, "ADMIN_REQUIRED")),
        ("bad", (401, "TOKEN_INVALID")),
    ],
)
def test_non_admins_are_refused_before_the_ledger_is_read(
    ledger: Ledger, caller: str, expected: tuple[int, str]
) -> None:
    """§7「鉴权」：401 / 403 / 401，且一条读账本的语句都没发。"""
    tenant = ledger.tenant()
    ledger.adjust(tenant, TransactionType.ADJUSTMENT_CREDIT, "10")
    headers = {
        "anonymous": {},
        "customer": ledger.customer_caller(),
        "bad": {"Authorization": "Bearer not-a-token"},
    }[caller]
    statements: list[str] = []

    def record(_conn, _cursor, statement, *_args) -> None:
        statements.append(statement)

    engine = ledger.app.state.engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        response = ledger.get(tenant.public_id, headers=headers)
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert (response.status_code, response.json()["error"]["code"]) == expected
    assert not [sql for sql in statements if "wallet_transactions" in sql]


# --- repository：一条 SELECT、不加锁、不读 metadata_json ---------------------------------


def test_a_page_is_one_unlocked_select_without_metadata(ledger: Ledger) -> None:
    """设计 §2「查询」与 §2「事务边界」：取一页是一条 SELECT，不加锁，不读 metadata_json。

    只断言两种方言相同的片段（CLAUDE.md「写后端测试的约定」）。
    """
    tenant, _ = five_rows(ledger)
    statements: list[str] = []

    def record(_conn, _cursor, statement, *_args) -> None:
        statements.append(statement)

    engine = ledger.factory.kw["bind"]
    with ledger.factory() as session:
        event.listen(engine, "before_cursor_execute", record)
        try:
            rows = wallet_repository.list_transactions_for_tenant(
                session, tenant.id, limit=3, reference_type=ReferenceType.ADMIN_ADJUSTMENT
            )
        finally:
            event.remove(engine, "before_cursor_execute", record)
        assert not session.new and not session.dirty

    assert [row.wallet_sequence for row in rows] == [5, 1]
    [statement] = statements
    normalized = " ".join(statement.upper().split())
    assert normalized.startswith("SELECT ")
    assert "METADATA_JSON" not in normalized
    assert "FOR UPDATE" not in normalized and "FOR SHARE" not in normalized
    assert "ORDER BY WALLET_TRANSACTIONS.WALLET_SEQUENCE DESC" in normalized
