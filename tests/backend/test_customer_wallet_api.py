"""Customer wallet and ledger end to end (AIH-TASK-099, design gate #393 v3 §7).

`GET /api/v1/customer/wallet` 与 `GET /api/v1/customer/wallet/transactions`：
只读、租户只取自会话。§7 后端表格里「路由守卫」在 test_customer_api.py，
「单条快照」在 test_wallet_repository.py，不在这里。

账本行由真实的 `post_transaction` 与计费 worker 写入（test_usage_billing.World）：
- SQLite：`create_all` 建表，再装上 test_usage_billing 那条代替触发器推进钱包的测试触发器；
- MySQL：表与触发器都走迁移，需要 `BILLING_TEST_DATABASE_URL`，没设时 MySQL 那一半 skip ——
  **不要把 skipped 读成 passed**，CI 里不许 skip。

客户照 AIH-TASK-093（test_customer_api.py）的写法经 HTTP 登录拿访问令牌。

⚠️ 鉴权一行与设计 #393 §2「错误」的字面不同（任务契约的勘误）：`require_customer` 先调用的
`require_current_user` 对非 `ACTIVE` 用户抛 `InvalidToken`，所以门户账号被停用后的旧令牌是 401
`TOKEN_INVALID`；403 `CUSTOMER_REQUIRED` 只用于 ADMIN 令牌与没有租户的 CUSTOMER。

示例 uuid 用全零占位值。
"""

from __future__ import annotations

import base64
import itertools
import os
import re
from collections.abc import Iterator
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, event, func, select, update
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
from app.core.passwords import hash_password
from app.core.tokens import issue_access_token
from app.main import create_app
from app.models.auth import AuditLog, DomainOutbox, User, UserRole, UserStatus
from app.models.base import Base
from app.models.tenancy import AccountStatus, BillingMode, Tenant
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
from app.services.auth import RequestContext, utc_now
from app.services.portal_accounts import disable_portal_account

WALLET = "/api/v1/customer/wallet"
TRANSACTIONS = "/api/v1/customer/wallet/transactions"
LOGIN = "/api/v1/auth/login"
PASSWORD = "a-customer-perfectly-fine-passphrase"
PLACEHOLDER_SESSION = "00000000-0000-0000-0000-000000000000"
CONTEXT = RequestContext(ip_address="10.0.0.1", user_agent="pytest")

# 设计 §2：钱包恰好 6 个键；流水 `data` 恰好 3 个键，每项恰好 7 个键。多一个就红。
WALLET_FIELDS = {
    "currency",
    "balance",
    "billing_mode",
    "billing_status",
    "ai_service",
    "ai_service_reason",
}
PAGE_FIELDS = {"items", "page_size", "next_before_sequence"}
ITEM_FIELDS = {
    "id",
    "wallet_sequence",
    "transaction_type",
    "amount",
    "balance_after",
    "event_id",
    "created_at",
}
MAX_BIGINT = 2**63 - 1

# INV-7 的哨兵：写进用量事件的成本、汇率、参考价与调账的原因，
# 断言它们不出现在原始响应文本里。
SENTINEL_SOURCE_COST = Decimal("0.13579246")
SENTINEL_ESTIMATED_MYR = Decimal("0.24681357")
SENTINEL_FX_RATE = Decimal("0.97531864")
SENTINEL_REFERENCE_PRICE = Decimal("0.86420975")
SENTINEL_DESCRIPTION = "Sentinel internal note: provider cost 0.13579246"
SENTINEL_IDEMPOTENCY_KEY = "sentinel-adjustment-key-00000000"

_NUMBERS = itertools.count(1)


def settings_for(tmp_path) -> Settings:
    jwt, master = tmp_path / "jwt.key", tmp_path / "master.key"
    jwt.write_text("a-test-signing-key-that-is-long-enough", encoding="utf-8")
    master.write_text(f"1:{base64.b64encode(os.urandom(32)).decode()}\n", encoding="utf-8")
    return Settings(
        jwt_secret_file=str(jwt),
        master_key_file=str(master),
        database_url="",
        session_cookie_secure=False,
        auth_rate_limit_per_minute=600,
        auth_rate_limit_burst=200,
        frontend_base_url="https://billing.example.com",
    )


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


class Portal:
    """The app and the billing world on one database; customers sign in over HTTP."""

    def __init__(self, factory: sessionmaker[Session], tmp_path) -> None:
        self.factory = factory
        self.app = create_app(settings_for(tmp_path))
        self.app.state.engine = factory.kw["bind"]
        self.app.state.session_factory = factory
        self.client = TestClient(self.app)
        self.world = BillingWorld(factory)
        self._priced = False

    # -- callers --

    def token(self, user_id: int, role: UserRole) -> dict[str, str]:
        token = issue_access_token(
            self.app.state.settings,
            user_id=user_id,
            role=role.value,
            session_id=PLACEHOLDER_SESSION,
            now=utc_now(),
        )
        return {"Authorization": f"Bearer {token}"}

    def admin_caller(self) -> dict[str, str]:
        return self.token(self.world.admin.id, UserRole.ADMIN)

    def make_customer_user(self, tenant: Tenancy | None) -> User:
        now = utc_now().replace(microsecond=0)
        with self.factory() as session:
            user = User(
                # 用 test_usage_billing 的域名：MySQL 清场按它删用户。
                email=f"portal-{next(_NUMBERS)}{TEST_EMAIL_DOMAIN}",
                password_hash=hash_password(PASSWORD),
                role=UserRole.CUSTOMER,
                status=UserStatus.ACTIVE,
                tenant_id=tenant.id if tenant is not None else None,
                activated_at=now,
                created_at=now,
                updated_at=now,
            )
            session.add(user)
            session.commit()
            session.refresh(user)
            session.expunge(user)
            return user

    def customer(self, tenant: Tenancy) -> dict[str, str]:
        """A portal account for `tenant`, signed in over HTTP: the headers of the new session."""
        user = self.make_customer_user(tenant)
        response = self.client.post(LOGIN, json={"email": user.email, "password": PASSWORD})
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["stage"] is None
        assert data["access_token"]
        return {"Authorization": f"Bearer {data['access_token']}"}

    # -- writing the ledger and the tenant --

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
        reference_id: str | None = None,
    ) -> None:
        """One posting through the real repository; the source type follows the type."""
        with self.factory() as session:
            wallet_repository.post_transaction(
                session,
                tenant_id=tenant.id,
                transaction_type=kind,
                amount=Decimal(amount),
                reference_type=REFERENCE_TYPE_FOR[kind],
                reference_id=reference_id
                or f"{kind.value.lower()}-{tenant.id}-{self.sequence(tenant) + 1}",
                now=T0,
                created_by=self.world.admin.id if by_admin else None,
                description=description,
                actor_role="ADMIN" if by_admin else None,
            )
            session.commit()

    def adjust(
        self,
        tenant: Tenancy,
        kind: TransactionType,
        amount: str,
        *,
        description: str = "Fictional adjustment",
        reference_id: str | None = None,
    ) -> None:
        self.post(
            tenant,
            kind,
            amount,
            by_admin=True,
            description=description,
            reference_id=reference_id,
        )

    def bill_one(self, tenant: Tenancy) -> tuple[int, str]:
        """One usage event billed by the real worker: (row id, reported `event_id`)."""
        if not self._priced:
            self.world.publish_markup_usd()
            self._priced = True
        row_id = self.world.event(tenant)
        assert self.world.bill().claimed == 1
        row = self.world.row(row_id)
        assert row.wallet_transaction_id is not None
        return row_id, row.event_id

    def set_account_status(self, tenant: Tenancy, status: AccountStatus) -> None:
        self.world.set_account_status(tenant, status)

    def set_billing_mode(self, tenant: Tenancy, mode: BillingMode) -> None:
        with self.factory() as session:
            session.execute(update(Tenant).where(Tenant.id == tenant.id).values(billing_mode=mode))
            session.commit()

    # -- reading --

    def get_wallet(self, headers: dict[str, str], **params):
        return self.client.get(WALLET, params=params, headers=headers)

    def get_page(self, headers: dict[str, str], **params):
        return self.client.get(TRANSACTIONS, params=params, headers=headers)

    def wallet(self, headers: dict[str, str], **params) -> dict:
        response = self.get_wallet(headers, **params)
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert set(data) == WALLET_FIELDS
        return data

    def page(self, headers: dict[str, str], **params) -> dict:
        response = self.get_page(headers, **params)
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert set(data) == PAGE_FIELDS
        assert all(set(item) == ITEM_FIELDS for item in data["items"])
        return data

    def walk(self, headers: dict[str, str], **params) -> list[dict]:
        """Every item from the newest page, following `next_before_sequence`."""
        items: list[dict] = []
        cursor: int | None = None
        while True:
            extra = {} if cursor is None else {"before_sequence": cursor}
            data = self.page(headers, **params, **extra)
            items.extend(data["items"])
            cursor = data["next_before_sequence"]
            if cursor is None:
                return items

    def sequence(self, tenant: Tenancy) -> int:
        with self.factory() as session:
            statement = select(Wallet.version).where(Wallet.tenant_id == tenant.id)
            return int(session.execute(statement).scalar_one())

    def stored(self, tenant: Tenancy) -> dict[int, dict]:
        """What each item should look like, read straight from the ledger table."""
        with self.factory() as session:
            rows = session.execute(
                select(WalletTransaction).where(WalletTransaction.tenant_id == tenant.id)
            ).scalars()
            expected = {}
            for row in rows:
                usage = ReferenceType(row.reference_type) is ReferenceType.USAGE_EVENT
                expected[row.wallet_sequence] = {
                    "id": row.public_id,
                    "wallet_sequence": row.wallet_sequence,
                    "transaction_type": TransactionType(row.transaction_type).value,
                    "amount": money_text(row.amount),
                    "balance_after": money_text(row.balance_after),
                    "event_id": row.reference_id if usage else None,
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


def _portal(factories: Iterator[sessionmaker[Session]], tmp_path) -> Iterator[Portal]:
    factory = next(factories)
    try:
        yield Portal(factory, tmp_path)
    finally:
        # 门户账号挂在租户上（`fk_users_tenant` 是 RESTRICT）：
        # 先删掉它们，MySQL 清场才删得掉租户。
        with factory() as session:
            session.execute(
                delete(User).where(
                    User.tenant_id.is_not(None), User.email.like(f"%{TEST_EMAIL_DOMAIN}")
                )
            )
            session.commit()
        for _ in factories:
            pass


@pytest.fixture
def portal(tmp_path) -> Iterator[Portal]:
    yield from _portal(_sqlite_factory(), tmp_path)


@pytest.fixture(params=["sqlite", "mysql"])
def both(request, monkeypatch, tmp_path) -> Iterator[Portal]:
    """The same cases on SQLite and on a real MySQL (design §7「SQLite + MySQL 参数化」)."""
    factories = _mysql_factory(monkeypatch) if request.param == "mysql" else _sqlite_factory()
    yield from _portal(factories, tmp_path)


@pytest.fixture
def mysql_portal(monkeypatch, tmp_path) -> Iterator[Portal]:
    yield from _portal(_mysql_factory(monkeypatch), tmp_path)


def five_rows(portal: Portal) -> tuple[Tenancy, list[str]]:
    """Sequences 1–5: AI usage, credit adjustment, debit adjustment, system correction, AI usage.

    返回客户与两笔用量事件上报的 `event_id`（按序号 1、5）。
    """
    tenant = portal.tenant()
    _, first_event = portal.bill_one(tenant)
    portal.adjust(tenant, TransactionType.ADJUSTMENT_CREDIT, "100")
    portal.adjust(tenant, TransactionType.ADJUSTMENT_DEBIT, "-2")
    portal.post(
        tenant, TransactionType.SYSTEM_CORRECTION, "-0.25", description="Fictional correction"
    )
    _, second_event = portal.bill_one(tenant)
    assert portal.sequence(tenant) == 5
    return tenant, [first_event, second_event]


def error_of(response) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


# --- 正常路径 ---------------------------------------------------------------------------


def test_the_wallet_of_a_funded_prepaid_customer(both: Portal) -> None:
    """§7「正常路径（钱包）」：PREPAID、余额 12.5、ENABLED。"""
    tenant = both.tenant()
    both.adjust(tenant, TransactionType.ADJUSTMENT_CREDIT, "12.5")
    headers = both.customer(tenant)
    before = both.footprint()

    data = both.wallet(headers)

    assert data == {
        "currency": "MYR",
        "balance": "12.50000000",
        "billing_mode": "PREPAID",
        "billing_status": "ACTIVE",
        "ai_service": "ALLOW_AI",
        "ai_service_reason": None,
    }
    # 只读（INV-2/3/4）：调用前后账本、钱包、租户、审计、outbox 的行数与每个钱包的余额、版本不变。
    assert both.footprint() == before


def test_three_pages_newest_first_match_the_stored_rows(both: Portal) -> None:
    """§7「正常路径（流水）」：page_size=2 连翻三页，序号 5,4 / 3,2 / 1，游标 4、2、null。"""
    tenant, reported = five_rows(both)
    headers = both.customer(tenant)
    stored = both.stored(tenant)
    before = both.footprint()

    first = both.page(headers, page_size=2)
    second = both.page(headers, page_size=2, before_sequence=first["next_before_sequence"])
    third = both.page(headers, page_size=2, before_sequence=second["next_before_sequence"])

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
    # AI 用量行带接入方上报的 `event_id`，其余来源一律 null。
    assert [items[n]["event_id"] for n in (1, 5)] == reported
    assert [items[n]["event_id"] for n in (2, 3, 4)] == [None, None, None]
    assert [items[n]["transaction_type"] for n in (1, 2, 3, 4, 5)] == [
        "AI_USAGE",
        "ADJUSTMENT_CREDIT",
        "ADJUSTMENT_DEBIT",
        "SYSTEM_CORRECTION",
        "AI_USAGE",
    ]
    assert both.footprint() == before


def test_an_empty_ledger(both: Portal) -> None:
    """§7「空账本」：新客户、余额 0、没有账本行。"""
    tenant = both.tenant()
    headers = both.customer(tenant)

    wallet = both.wallet(headers)
    page = both.page(headers)

    assert wallet["balance"] == "0.00000000"
    assert (wallet["billing_status"], wallet["ai_service"], wallet["ai_service_reason"]) == (
        "SUSPENDED",
        "BLOCK_AI",
        "BALANCE_NOT_POSITIVE",
    )
    assert page == {"items": [], "page_size": 20, "next_before_sequence": None}


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
    ],
)
def test_out_of_range_parameters_are_422_without_echoing_the_value(
    portal: Portal, name: str, value: str
) -> None:
    """§7「边界值」：422 VALIDATION_ERROR，文案只列字段名、不回显值。"""
    headers = portal.customer(portal.tenant())

    response = portal.get_page(headers, **{name: value})

    assert error_of(response) == (422, "VALIDATION_ERROR")
    message = response.json()["error"]["message"]
    assert name in message
    assert value not in message


def test_the_largest_page_and_the_edges_of_the_cursor_are_200(portal: Portal) -> None:
    """§7「边界值」：page_size=100、before_sequence=1（空页）与 BIGINT 上界都是 200。"""
    tenant = portal.tenant()
    portal.adjust(tenant, TransactionType.ADJUSTMENT_CREDIT, "10")
    headers = portal.customer(tenant)

    largest = portal.page(headers, page_size=100)
    assert [item["wallet_sequence"] for item in largest["items"]] == [1]
    assert portal.page(headers, before_sequence=1) == {
        "items": [],
        "page_size": 20,
        "next_before_sequence": None,
    }
    newest = portal.page(headers, before_sequence=MAX_BIGINT)
    assert [item["wallet_sequence"] for item in newest["items"]] == [1]


def test_unknown_parameters_and_a_source_filter_are_ignored(portal: Portal) -> None:
    """设计 §2「接口二」：不接受 `reference_type` 或任何筛选；未知参数忽略，不报错。"""
    tenant = portal.tenant()
    portal.adjust(tenant, TransactionType.ADJUSTMENT_CREDIT, "10")
    portal.post(tenant, TransactionType.AI_USAGE, "-1")
    headers = portal.customer(tenant)

    data = portal.page(headers, reference_type="ADMIN_ADJUSTMENT", page=3, total="yes")
    wallet = portal.wallet(headers, page_size=0, reference_type="TOPUP")

    assert [item["wallet_sequence"] for item in data["items"]] == [2, 1]
    assert wallet["balance"] == "9.00000000"


# --- 计费状态与 AI 服务 ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("amount", "balance"),
    [(None, "0.00000000"), ("-3.2", "-3.20000000")],
)
def test_a_zero_or_negative_balance_blocks_ai(
    both: Portal, amount: str | None, balance: str
) -> None:
    """§7「余额恰好为 0 与负余额」：都是 SUSPENDED + BLOCK_AI + BALANCE_NOT_POSITIVE。"""
    tenant = both.tenant()
    if amount is not None:
        both.post(tenant, TransactionType.AI_USAGE, amount)
    headers = both.customer(tenant)

    data = both.wallet(headers)

    assert data == {
        "currency": "MYR",
        "balance": balance,
        "billing_mode": "PREPAID",
        "billing_status": "SUSPENDED",
        "ai_service": "BLOCK_AI",
        "ai_service_reason": "BALANCE_NOT_POSITIVE",
    }


def test_an_admin_disable_is_the_reason_before_the_balance(both: Portal) -> None:
    """§7「原因顺序」：DISABLED 且余额为负 → ACCOUNT_DISABLED（管理员动作排在余额之前）。"""
    tenant = both.tenant()
    both.post(tenant, TransactionType.AI_USAGE, "-1")
    both.set_account_status(tenant, AccountStatus.DISABLED)
    headers = both.customer(tenant)

    data = both.wallet(headers)

    assert data["balance"] == "-1.00000000"
    assert data["billing_status"] == "SUSPENDED"
    assert (data["ai_service"], data["ai_service_reason"]) == ("BLOCK_AI", "ACCOUNT_DISABLED")


def test_an_internal_metered_tenant_is_allowed_at_a_zero_balance(both: Portal) -> None:
    """§7「内部计量租户」：余额 0、SUSPENDED，但 AI 服务照常可用。"""
    tenant = both.tenant()
    both.set_billing_mode(tenant, BillingMode.INTERNAL_METERED_ONLY)
    headers = both.customer(tenant)

    data = both.wallet(headers)

    assert data == {
        "currency": "MYR",
        "balance": "0.00000000",
        "billing_mode": "INTERNAL_METERED_ONLY",
        "billing_status": "SUSPENDED",
        "ai_service": "ALLOW_AI",
        "ai_service_reason": None,
    }


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (AccountStatus.PENDING_ACTIVATION, "ACCOUNT_PENDING_ACTIVATION"),
        (AccountStatus.ENABLED, None),
        (AccountStatus.DISABLED, "ACCOUNT_DISABLED"),
        (AccountStatus.CLOSED, "ACCOUNT_CLOSED"),
    ],
)
def test_the_read_only_contract(both: Portal, status: AccountStatus, reason: str | None) -> None:
    """§7「只读契约」：四种账户状态下两个接口都 200；ENABLED 时按余额（这里为正，放行）。"""
    tenant = both.tenant()
    both.adjust(tenant, TransactionType.ADJUSTMENT_CREDIT, "12.5")
    both.set_account_status(tenant, status)
    headers = both.customer(tenant)

    wallet = both.wallet(headers)
    page = both.page(headers)

    assert wallet["balance"] == "12.50000000"
    assert wallet["ai_service_reason"] == reason
    assert wallet["ai_service"] == ("ALLOW_AI" if reason is None else "BLOCK_AI")
    assert [item["wallet_sequence"] for item in page["items"]] == [1]


# --- 并发、重放、失败 -------------------------------------------------------------------


def test_postings_between_pages_neither_repeat_nor_skip_older_rows(mysql_portal: Portal) -> None:
    """§7「并发（翻页）」（MySQL）：取第一页后另一事务记两笔，第二页与没有新记账时逐字相同。"""
    portal = mysql_portal
    tenant = portal.tenant()
    portal.adjust(tenant, TransactionType.ADJUSTMENT_CREDIT, "100")
    for _ in range(4):
        portal.post(tenant, TransactionType.AI_USAGE, "-1")
    headers = portal.customer(tenant)
    first = portal.page(headers, page_size=2)
    quiet_second = portal.page(headers, page_size=2, before_sequence=first["next_before_sequence"])

    # 另一个会话、另一个事务里记两笔新账并提交。
    portal.post(tenant, TransactionType.AI_USAGE, "-1")
    portal.adjust(tenant, TransactionType.ADJUSTMENT_CREDIT, "5")
    busy_second = portal.page(headers, page_size=2, before_sequence=first["next_before_sequence"])
    refetched_first = portal.page(headers, page_size=2)

    assert [item["wallet_sequence"] for item in first["items"]] == [5, 4]
    assert busy_second == quiet_second
    assert [item["wallet_sequence"] for item in quiet_second["items"]] == [3, 2]
    assert [item["wallet_sequence"] for item in refetched_first["items"]] == [7, 6]


def test_the_same_request_twice_returns_the_same_response(portal: Portal) -> None:
    """§7「幂等重放」：GET 天然可重复；期间没有新记账时两次的 data 相同。"""
    tenant, _ = five_rows(portal)
    headers = portal.customer(tenant)

    for path, params in ((WALLET, {}), (TRANSACTIONS, {"page_size": 2, "before_sequence": 4})):
        first = portal.client.get(path, params=params, headers=headers)
        again = portal.client.get(path, params=params, headers=headers)
        assert (first.status_code, again.status_code) == (200, 200)
        assert first.json()["data"] == again.json()["data"]


def test_a_failing_query_is_a_500_with_a_request_id_and_writes_nothing(
    portal: Portal, monkeypatch
) -> None:
    """§7「事务中途失败」：repository 抛数据库异常 → 500 带 request_id，各表行数不变。"""
    tenant = portal.tenant()
    portal.adjust(tenant, TransactionType.ADJUSTMENT_CREDIT, "10")
    headers = portal.customer(tenant)
    before = portal.footprint()

    def broken(*_args, **_kwargs):
        raise OperationalError("SELECT", {}, Exception("fictional database failure"))

    monkeypatch.setattr(wallet_repository, "get_wallet_summary", broken)
    monkeypatch.setattr(wallet_repository, "list_transactions_for_tenant", broken)
    client = TestClient(portal.app, raise_server_exceptions=False)

    for path in (WALLET, TRANSACTIONS):
        response = client.get(path, headers=headers)
        assert error_of(response) == (500, "INTERNAL_ERROR")
        body = response.json()
        assert body["data"] is None
        assert body["request_id"]
        assert "fictional database failure" not in response.text
    assert portal.footprint() == before


@pytest.mark.parametrize("with_rows", [False, True])
@pytest.mark.parametrize("path", [WALLET, TRANSACTIONS])
def test_a_missing_wallet_is_a_500_on_both_endpoints(
    portal: Portal, path: str, with_rows: bool
) -> None:
    """§7「缺钱包」（SQLite）：有没有账本行，两个接口都 500，消息只有 WALLET_MISSING。

    流水接口不返回 200 空页：缺钱包这一数据不一致不能被一个看似正常的空流水掩盖。
    """
    tenant = portal.tenant()
    if with_rows:
        portal.adjust(tenant, TransactionType.ADJUSTMENT_CREDIT, "7.5")
    stored_ids = [item["id"] for item in portal.stored(tenant).values()]
    headers = portal.customer(tenant)
    with portal.factory() as session:
        session.execute(delete(Wallet).where(Wallet.tenant_id == tenant.id))
        session.commit()

    # 默认的 TestClient 把服务端异常原样抛出来：看得到处理函数抛的是什么、消息是什么。
    with pytest.raises((RuntimeError, ExceptionGroup)) as raised:
        portal.client.get(path, headers=headers)
    response = TestClient(portal.app, raise_server_exceptions=False).get(path, headers=headers)

    error = raised.value
    while isinstance(error, BaseExceptionGroup):
        error = error.exceptions[0]
    assert type(error) is RuntimeError
    assert str(error) == "WALLET_MISSING"
    assert error_of(response) == (500, "INTERNAL_ERROR")
    assert response.json()["data"] is None
    assert response.json()["request_id"]
    for leaked in (tenant.public_id, "7.50000000", "MYR", *stored_ids):
        assert leaked not in response.text


# --- 租户越权 -------------------------------------------------------------------------


def test_a_customer_only_ever_sees_its_own_wallet(both: Portal) -> None:
    """§7「租户越权」（INV-8）：A 带 B 的标识、用 B 的最大序号 + 1 当游标，得到的都只是 A。"""
    mine = both.tenant()
    theirs = both.tenant()
    for _ in range(3):
        both.adjust(mine, TransactionType.ADJUSTMENT_CREDIT, "1")
    for _ in range(5):
        both.adjust(theirs, TransactionType.ADJUSTMENT_CREDIT, "2")
    headers = both.customer(mine)
    both.customer(theirs)
    their_ids = {item["id"] for item in both.stored(theirs).values()}
    my_ids = {item["id"] for item in both.stored(mine).values()}
    forged = {"customer_id": theirs.public_id, "tenant_id": str(theirs.id)}

    wallet = both.get_wallet(headers, **forged)
    page = both.get_page(headers, **forged)
    borrowed = both.page(headers, before_sequence=both.sequence(theirs) + 1, **forged)
    every_page = both.walk(headers, page_size=1, **forged)

    assert wallet.status_code == 200
    assert wallet.json()["data"]["balance"] == "3.00000000"
    assert page.status_code == 200
    assert {item["id"] for item in page.json()["data"]["items"]} == my_ids
    assert [item["wallet_sequence"] for item in borrowed["items"]] == [3, 2, 1]
    assert {item["id"] for item in every_page} == my_ids
    assert {item["amount"] for item in every_page} == {"1.00000000"}
    for response_text in (wallet.text, page.text):
        assert "10.00000000" not in response_text
        assert theirs.public_id not in response_text
        assert not [their_id for their_id in their_ids if their_id in response_text]


# --- 鉴权 ---------------------------------------------------------------------------


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
@pytest.mark.parametrize("path", [WALLET, TRANSACTIONS])
def test_non_customers_are_refused_before_the_wallet_is_read(
    portal: Portal, path: str, caller: str, expected: tuple[int, str]
) -> None:
    """§7「鉴权」：拒绝的同时一条读钱包或账本的语句都没发。"""
    tenant = portal.tenant()
    portal.adjust(tenant, TransactionType.ADJUSTMENT_CREDIT, "10")
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
    statements: list[str] = []

    def record(_conn, _cursor, statement, *_args) -> None:
        statements.append(statement)

    engine = portal.app.state.engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        response = portal.client.get(path, headers=headers)
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert error_of(response) == expected
    assert response.json()["data"] is None
    ledger = re.compile(r"\b(wallets|wallet_transactions)\b", re.IGNORECASE)
    assert not [statement for statement in statements if ledger.search(statement)]


# --- 无成本毛利、无内部说明 ---------------------------------------------------------------


def test_no_cost_margin_internal_note_operator_or_idempotency_key_leaks(portal: Portal) -> None:
    """§7「无成本毛利、无内部说明」（INV-7）：哨兵值写进库里，两个接口的响应文本里都没有。"""
    tenant = portal.tenant()
    portal.adjust(
        tenant,
        TransactionType.ADJUSTMENT_CREDIT,
        "50",
        description=SENTINEL_DESCRIPTION,
        reference_id=SENTINEL_IDEMPOTENCY_KEY,
    )
    usage_row_id, reported = portal.bill_one(tenant)
    portal.post(tenant, TransactionType.SYSTEM_CORRECTION, "-0.5", description=SENTINEL_DESCRIPTION)
    with portal.factory() as session:
        session.execute(
            update(UsageEvent)
            .where(UsageEvent.id == usage_row_id)
            .values(
                provider_source_cost=SENTINEL_SOURCE_COST,
                estimated_provider_cost_myr=SENTINEL_ESTIMATED_MYR,
                fx_rate_applied=SENTINEL_FX_RATE,
                reference_customer_price=SENTINEL_REFERENCE_PRICE,
            )
        )
        session.commit()
        usage_public_id = session.get(UsageEvent, usage_row_id).public_id
    headers = portal.customer(tenant)

    wallet = portal.get_wallet(headers)
    page = portal.get_page(headers, page_size=100)

    assert (wallet.status_code, page.status_code) == (200, 200)
    assert set(wallet.json()["data"]) == WALLET_FIELDS
    items = page.json()["data"]["items"]
    assert len(items) == 3
    assert all(set(item) == ITEM_FIELDS for item in items)
    assert [item["event_id"] for item in items] == [None, reported, None]
    forbidden = (
        money_text(SENTINEL_SOURCE_COST),
        money_text(SENTINEL_ESTIMATED_MYR),
        money_text(SENTINEL_FX_RATE),
        money_text(SENTINEL_REFERENCE_PRICE),
        SENTINEL_DESCRIPTION,
        SENTINEL_IDEMPOTENCY_KEY,
        portal.world.admin.email,
        usage_public_id,
        "description",
        "reference_type",
        "reference_id",
        "balance_before",
        "created_by",
        "metadata",
        "tenant_id",
        "wallet_id",
        "low_balance_threshold",
        "status_version",
        '"version"',
        "cost",
        "margin",
    )
    for response_text in (wallet.text, page.text):
        for value in forbidden:
            assert value not in response_text, value


# --- 金额精度 -------------------------------------------------------------------------


def test_amounts_round_trip_exactly_at_the_extremes(both: Portal) -> None:
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
    rich_headers = both.customer(rich)
    poor_headers = both.customer(poor)

    rich_items = both.page(rich_headers)["items"]
    [poor_item] = both.page(poor_headers)["items"]

    assert [(item["amount"], item["balance_after"]) for item in rich_items] == [
        ("-0.00000001", top_less_one),
        (top, top),
    ]
    assert (poor_item["amount"], poor_item["balance_after"]) == ("-0.00000001", "-0.00000001")
    assert {item["wallet_sequence"]: item for item in rich_items} == both.stored(rich)
    assert both.wallet(rich_headers)["balance"] == top_less_one
    assert both.wallet(poor_headers)["balance"] == "-0.00000001"
