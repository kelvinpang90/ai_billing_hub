"""Customer projects and billed usage end to end (AIH-TASK-104, design gate #398 v4 §7).

`GET /api/v1/customer/projects` 与 `GET /api/v1/customer/usage-events`：只读、租户只取自会话。
§7 后端表格里「索引存在且可用」「迁移可重跑」「迁移往返」属于 AIH-TASK-103
（test_usage_customer_index_migration.py），「路由守卫」在 test_customer_api.py，不在这里。

用量事件直接按库里的约束写成各种状态（`UsageEventPortal.put`）：`PROCESSED` 的快照引用取自一组
真实发布的价格、汇率与规则，计费额大于 0 时先用真实的 `post_transaction` 记那一行 `AI_USAGE`
账本（MySQL 的 CHECK 要求两者对应）。「与钱包对得上」那一条由真实的计费 worker 计费。
- SQLite：`create_all` 建表，装上 test_usage_billing 那条代替触发器推进钱包的测试触发器；
- MySQL：表与触发器都走迁移，需要 `BILLING_TEST_DATABASE_URL`，没设时 MySQL 那一半 skip ——
  **不要把 skipped 读成 passed**，CI 里不许 skip。

客户照 AIH-TASK-093（test_customer_api.py）的写法经 HTTP 登录拿访问令牌（test_customer_wallet_api 的
`Portal`）。

⚠️ 鉴权一行与设计 #398 §2「错误」的字面不同（任务契约的勘误）：`require_customer` 先调用的
`require_current_user` 对非 `ACTIVE` 用户抛 `InvalidToken`，所以门户账号被停用后的旧令牌是 401
`TOKEN_INVALID`；403 `CUSTOMER_REQUIRED` 只用于 ADMIN 令牌与没有租户的 CUSTOMER。

⚠️ SQL 文本断言不写死方言字面量；只有 MySQL 才有的断言放在方言分支里。示例 uuid 用全零占位值。
"""

from __future__ import annotations

import base64
import datetime as dt
import functools
import io
import json
import logging
import re
import uuid
from collections.abc import Iterator
from decimal import Decimal
from typing import NamedTuple

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, func, insert, select, update
from sqlalchemy.dialects import mysql, sqlite
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from test_customer_wallet_api import CONTEXT, Portal, _sqlite_factory, error_of
from test_usage_billing import _NUMBERS as BILLING_NUMBERS
from test_usage_billing import (
    T0,
    TEST_EMAIL_DOMAIN,
    TOKENS,
    Tenancy,
    _mysql_factory,
)

from app.core.logging import JsonFormatter
from app.models.ai_catalog import UsageMeterType
from app.models.auth import (
    AuditLog,
    DomainOutbox,
    PasswordResetToken,
    RecoveryCode,
    RefreshToken,
    TwoFactorSetting,
    User,
    UserRole,
)
from app.models.fx_rates import FxRateVersion
from app.models.integration import CredentialStatus, IntegrationCredential
from app.models.pricing_rules import PricingRule
from app.models.provider_prices import ProviderPriceVersion
from app.models.tenancy import AccountStatus, BillingMode, Project
from app.models.usage import ConflictMismatch, UsageEvent, UsageEventStatus
from app.models.wallet import ReferenceType, TransactionType, Wallet, WalletTransaction
from app.repositories import tenancy as tenancy_repository
from app.repositories import usage_events as usage_repository
from app.repositories import wallet as wallet_repository
from app.repositories.usage_events import (
    CUSTOMER_USAGE_QUERY_TIMEOUT_MS,
    QueryTooBroad,
    customer_usage_statement,
    list_processed_events_for_customer,
)
from app.schemas.customer_usage import encode_cursor
from app.schemas.customers import money_text
from app.services import customers
from app.services.portal_accounts import disable_portal_account

PROJECTS = "/api/v1/customer/projects"
USAGE = "/api/v1/customer/usage-events"

# 设计 §2：项目每项恰好 2 个键；用量 `data` 恰好 3 个键，每项恰好 18 个键。多一个就红。
PROJECT_FIELDS = {"id", "name"}
PAGE_FIELDS = {"items", "page_size", "next_cursor"}
ITEM_FIELDS = {
    "id",
    "event_id",
    "request_id",
    "conversation_id",
    "feature",
    "project_id",
    "project_name",
    "provider",
    "model",
    "usage_type",
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "quantity",
    "unit",
    "charged_amount",
    "occurred_at",
}

# 一个马来西亚日历日：UTC [2026-09-30 16:00, 2026-10-01 16:00)。
DAY = "2026-10-01"
ON_DAY = dt.datetime(2026, 10, 1, 2, 0, 0, 123456)
MINUTE = dt.timedelta(minutes=1)
ZERO_UUID = "00000000-0000-0000-0000-000000000000"

# 成本列的默认值：精确的二进制小数（SQLite 把 NUMERIC 存成浮点）。
DEFAULT_COSTS = {
    "provider_source_cost": Decimal("0.03125"),
    "fx_rate_applied": Decimal("4.5"),
    "estimated_provider_cost_myr": Decimal("0.140625"),
}
# INV-7 的哨兵：写进事件与项目，断言它们不出现在原始响应文本里。
SENTINEL_SOURCE_COST = Decimal("0.13579246")
SENTINEL_ESTIMATED_MYR = Decimal("0.24681357")
SENTINEL_FX_RATE = Decimal("0.97531864")
SENTINEL_REFERENCE_PRICE = Decimal("0.86420975")
SENTINEL_DESCRIPTION = "Sentinel project note 0000"
SENTINEL_WEBHOOK = "https://hooks.example.com/sentinel-webhook-token-0000"
SENTINEL_API_KEY = "ak_sentinel_conflict_key_00000000"

UNBILLED = (
    UsageEventStatus.RECEIVED,
    UsageEventStatus.PROCESSING,
    UsageEventStatus.PRICING_ERROR,
    UsageEventStatus.MODEL_UNKNOWN,
    UsageEventStatus.FAILED_RETRYABLE,
    UsageEventStatus.FAILED_FINAL,
)


class ProjectRef(NamedTuple):
    public_id: str
    id: int
    credential_id: int


class Stored(NamedTuple):
    row_id: int
    public_id: str
    event_id: str


class UsageEventPortal(Portal):
    """The wallet test's portal, plus projects and usage events in any state."""

    def __init__(self, factory: sessionmaker[Session], tmp_path) -> None:
        super().__init__(factory, tmp_path)
        self._snapshot: dict[str, object] | None = None

    # -- the catalog snapshot every PROCESSED row points at --

    def snapshot(self) -> dict[str, object]:
        """Publish one USD price, rate and MARKUP rule once; their ids for PROCESSED rows."""
        if self._snapshot is None:
            price = self.world.price()
            rate = self.world.fx()
            rule = self.world.rule()
            # `Portal.bill_one` 不再重复发布。
            self._priced = True
            provider_id = self.world.provider_row_id()
            model_id = self.world.model_row_id()
            with self.factory() as session:
                self._snapshot = {
                    "provider_id": provider_id,
                    "model_id": model_id,
                    "provider_price_version_id": session.execute(
                        select(ProviderPriceVersion.id).where(
                            ProviderPriceVersion.public_id == price
                        )
                    ).scalar_one(),
                    "pricing_rule_id": session.execute(
                        select(PricingRule.id).where(PricingRule.public_id == rule)
                    ).scalar_one(),
                    "fx_rate_version_id": session.execute(
                        select(FxRateVersion.id).where(FxRateVersion.public_id == rate)
                    ).scalar_one(),
                    "provider_source_currency": "USD",
                }
        return self._snapshot

    # -- projects --

    def project(
        self,
        tenant: Tenancy,
        name: str,
        *,
        description: str | None = None,
        webhook: str | None = None,
    ) -> ProjectRef:
        """Another project of `tenant` with an integration credential of its own."""
        view = customers.create_project(
            self.factory,
            actor=self.world.admin,
            customer_id=tenant.public_id,
            name=name,
            description=description,
            context=CONTEXT,
            now=T0,
        )
        number = next(BILLING_NUMBERS)
        with self.factory() as session:
            project_id = session.execute(
                select(Project.id).where(Project.public_id == view.id)
            ).scalar_one()
            if webhook is not None:
                session.execute(
                    update(Project)
                    .where(Project.id == project_id)
                    .values(status_webhook_url=webhook)
                )
            credential = IntegrationCredential(
                tenant_id=tenant.id,
                project_id=project_id,
                public_api_key=f"ak_{number:032d}",
                key_version=1,
                encrypted_secret="not-a-real-ciphertext",
                encryption_key_version=1,
                status=CredentialStatus.ACTIVE,
                valid_from=T0,
                created_at=T0,
            )
            session.add(credential)
            session.commit()
            return ProjectRef(view.id, project_id, credential.id)

    def main_project(self, tenant: Tenancy) -> ProjectRef:
        """The project `World.tenant` made (named `Chatbot`)."""
        with self.factory() as session:
            public_id = session.execute(
                select(Project.public_id).where(Project.id == tenant.project_id)
            ).scalar_one()
        return ProjectRef(public_id, tenant.project_id, tenant.credential_id)

    # -- usage events --

    def _row_values(
        self,
        session: Session,
        tenant: Tenancy,
        where: ProjectRef,
        *,
        status: UsageEventStatus,
        occurred_at: dt.datetime,
        provider: str,
        model: str,
        conversation_id: str | None,
        request_id: str | None,
        feature: str | None,
        tokens: dict[str, int] | None,
        seconds: str | None,
    ) -> dict[str, object]:
        number = next(BILLING_NUMBERS)
        meter = usage_repository.find_meter_type(
            session, "LLM_TOKEN" if seconds is None else "AUDIO_SECOND"
        )
        assert meter is not None
        token_fields = (
            dict.fromkeys(TOKENS)
            if seconds is not None
            else dict(tokens if tokens is not None else TOKENS)
        )
        values: dict[str, object] = {
            "public_id": str(uuid.uuid4()),
            "event_id": f"00000000-0000-7000-8000-{number:012d}",
            "schema_version": "1.0",
            "tenant_id": tenant.id,
            "project_id": where.id,
            "integration_credential_id": where.credential_id,
            "request_id": request_id or f"call-{number}",
            "conversation_id": conversation_id,
            "provider_code_raw": provider,
            "model_code_raw": model,
            "usage_meter_type_id": meter.id,
            "payload_shape": meter.payload_shape,
            "quantity_kind": meter.quantity_kind,
            "unit": meter.unit,
            **token_fields,
            "quantity": None if seconds is None else Decimal(seconds),
            "payload_fingerprint": "0" * 64,
            "status": status,
            "feature": feature,
            "occurred_at": occurred_at,
            "received_at": T0,
            "created_at": T0,
        }
        if status is UsageEventStatus.PROCESSING:
            values.update(
                claim_token=str(uuid.uuid4()),
                claimed_at=T0,
                lease_expires_at=T0 + dt.timedelta(seconds=120),
            )
        elif status is not UsageEventStatus.RECEIVED and status is not UsageEventStatus.PROCESSED:
            values["error_code"] = "FICTIONAL_ERROR"
        return values

    def _billed(
        self,
        *,
        billable: Decimal,
        mode: BillingMode,
        reference: Decimal | None,
        costs: dict[str, Decimal] | None,
        ledger_id: int | None,
    ) -> dict[str, object]:
        return {
            **self.snapshot(),
            **DEFAULT_COSTS,
            **(costs or {}),
            "billable_cost": billable,
            "billing_mode_snapshot": mode.value,
            "reference_customer_price": reference,
            "processed_at": T0,
            "wallet_transaction_id": ledger_id,
        }

    def put(
        self,
        tenant: Tenancy,
        *,
        project: ProjectRef | None = None,
        status: UsageEventStatus = UsageEventStatus.PROCESSED,
        occurred_at: dt.datetime = ON_DAY,
        provider: str = "anthropic",
        model: str = "claude-x",
        conversation_id: str | None = None,
        request_id: str | None = None,
        feature: str | None = None,
        tokens: dict[str, int] | None = None,
        seconds: str | None = None,
        billable: str = "0.0625",
        mode: BillingMode = BillingMode.PREPAID,
        reference: Decimal | None = None,
        costs: dict[str, Decimal] | None = None,
    ) -> Stored:
        """One usage event in `status`; LLM_TOKEN unless `seconds` is given (AUDIO_SECOND)."""
        where = project or self.main_project(tenant)
        # 先发布（自己的会话、自己提交），再开写事件的会话：SQLite 的 StaticPool 只有一个连接。
        self.snapshot()
        with self.factory() as session:
            values = self._row_values(
                session,
                tenant,
                where,
                status=status,
                occurred_at=occurred_at,
                provider=provider,
                model=model,
                conversation_id=conversation_id,
                request_id=request_id,
                feature=feature,
                tokens=tokens,
                seconds=seconds,
            )
            if status is UsageEventStatus.PROCESSED:
                amount = Decimal(billable)
                ledger_id = None
                if amount > 0:
                    posted = wallet_repository.post_transaction(
                        session,
                        tenant_id=tenant.id,
                        transaction_type=TransactionType.AI_USAGE,
                        amount=-amount,
                        reference_type=ReferenceType.USAGE_EVENT,
                        reference_id=str(values["event_id"]),
                        now=T0,
                    )
                    ledger_id = posted.transaction.id
                values.update(
                    self._billed(
                        billable=amount,
                        mode=mode,
                        reference=reference,
                        costs=costs,
                        ledger_id=ledger_id,
                    )
                )
            row = UsageEvent(**values)
            session.add(row)
            session.commit()
            return Stored(row.id, row.public_id, row.event_id)

    def mark_processed(self, stored: Stored) -> None:
        """Bill a RECEIVED event late, at 0 (no ledger row), in a transaction of its own."""
        values = self._billed(
            billable=Decimal("0"),
            mode=BillingMode.PREPAID,
            reference=None,
            costs=None,
            ledger_id=None,
        )
        with self.factory() as session:
            session.execute(
                update(UsageEvent)
                .where(UsageEvent.id == stored.row_id)
                .values(status=UsageEventStatus.PROCESSED, **values)
            )
            session.commit()

    def bulk(self, tenant: Tenancy, count: int, start: dt.datetime) -> None:
        """`count` PROCESSED events at 0, one second apart from `start`, in batches."""
        billed = self._billed(
            billable=Decimal("0"),
            mode=BillingMode.PREPAID,
            reference=None,
            costs=None,
            ledger_id=None,
        )
        where = self.main_project(tenant)
        batch: list[dict[str, object]] = []
        with self.factory() as session:
            template = self._row_values(
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
                        "occurred_at": start + dt.timedelta(seconds=offset),
                    }
                )
                if len(batch) == 1000:
                    session.execute(insert(UsageEvent), batch)
                    batch = []
            if batch:
                session.execute(insert(UsageEvent), batch)
            session.commit()

    def conflict(self, tenant: Tenancy, stored: Stored, api_key: str) -> None:
        with self.factory() as session:
            usage_repository.insert_conflict(
                session,
                usage_event_id=stored.row_id,
                event_id=stored.event_id,
                integration_credential_id=tenant.credential_id,
                api_key=api_key,
                payload_fingerprint="1" * 64,
                mismatch=ConflictMismatch.FINGERPRINT,
                now=T0,
            )
            session.commit()

    def bill_for_real(self, tenant: Tenancy) -> Stored:
        """One event billed by the real worker (MARKUP 2 on a USD price)."""
        self.snapshot()
        row_id = self.world.event(tenant)
        assert self.world.bill().claimed == 1
        row = self.world.row(row_id)
        assert row.status is UsageEventStatus.PROCESSED
        return Stored(row.id, row.public_id, row.event_id)

    # -- reading --

    def get_usage(self, headers: dict[str, str], **params):
        return self.client.get(USAGE, params=params, headers=headers)

    def get_projects(self, headers: dict[str, str], **params):
        return self.client.get(PROJECTS, params=params, headers=headers)

    def usage(self, headers: dict[str, str], **params) -> dict:
        response = self.get_usage(headers, **params)
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert set(data) == PAGE_FIELDS
        assert all(set(item) == ITEM_FIELDS for item in data["items"])
        return data

    def ids(self, headers: dict[str, str], **params) -> list[str]:
        return [item["id"] for item in self.usage(headers, **params)["items"]]

    def walk(self, headers: dict[str, str], **params) -> list[dict]:
        """Every item from the newest page, following `next_cursor`."""
        items: list[dict] = []
        cursor: str | None = None
        while True:
            extra = {} if cursor is None else {"cursor": cursor}
            data = self.usage(headers, **params, **extra)
            items.extend(data["items"])
            cursor = data["next_cursor"]
            if cursor is None:
                return items

    def expected(self, stored: Stored) -> dict:
        """What the item should look like, read straight from the tables."""
        with self.factory() as session:
            row = session.get(UsageEvent, stored.row_id)
            assert row is not None
            project = session.get(Project, row.project_id)
            meter = session.get(UsageMeterType, row.usage_meter_type_id)
            assert project is not None and meter is not None and row.billable_cost is not None
            return {
                "id": row.public_id,
                "event_id": row.event_id,
                "request_id": row.request_id,
                "conversation_id": row.conversation_id,
                "feature": row.feature,
                "project_id": project.public_id,
                "project_name": project.name,
                "provider": row.provider_code_raw,
                "model": row.model_code_raw,
                "usage_type": meter.code,
                "input_tokens": row.input_tokens,
                "output_tokens": row.output_tokens,
                "cache_creation_input_tokens": row.cache_creation_input_tokens,
                "cache_read_input_tokens": row.cache_read_input_tokens,
                "quantity": None if row.quantity is None else money_text(row.quantity),
                "unit": row.unit,
                "charged_amount": money_text(row.billable_cost),
                "occurred_at": row.occurred_at.isoformat(),
            }

    def tables(self) -> tuple[dict[str, int], list[tuple], list[tuple]]:
        """Row counts of every table a write would touch, each event's status, every wallet."""
        with self.factory() as session:
            counts = {
                model.__tablename__: int(
                    session.execute(select(func.count()).select_from(model)).scalar_one()
                )
                for model in (UsageEvent, WalletTransaction, Wallet, AuditLog, DomainOutbox)
            }
            statuses = [
                tuple(row)
                for row in session.execute(
                    select(UsageEvent.id, UsageEvent.status).order_by(UsageEvent.id)
                ).all()
            ]
            wallets = [
                tuple(row)
                for row in session.execute(
                    select(Wallet.tenant_id, Wallet.balance, Wallet.version).order_by(Wallet.id)
                ).all()
            ]
            return counts, statuses, wallets


def _usage_portal(
    factories: Iterator[sessionmaker[Session]], tmp_path
) -> Iterator[UsageEventPortal]:
    # 与 test_customer_wallet_api._portal 同一清场：先删门户账号与指向它们的行，再交给 MySQL 清场。
    factory = next(factories)
    try:
        yield UsageEventPortal(factory, tmp_path)
    finally:
        with factory() as session:
            portal_users = select(User.id).where(
                User.role == UserRole.CUSTOMER, User.email.like(f"%{TEST_EMAIL_DOMAIN}")
            )
            ids = list(session.execute(portal_users).scalars())
            if ids:
                for model in (RefreshToken, RecoveryCode, TwoFactorSetting, PasswordResetToken):
                    session.execute(delete(model).where(model.user_id.in_(ids)))
                session.execute(delete(User).where(User.id.in_(ids)))
            session.commit()
        for _ in factories:
            pass


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


def record_statements(engine) -> tuple[list[str], object]:
    statements: list[str] = []

    def record(_conn, _cursor, statement, *_args) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    return statements, record


def usage_statements(statements: list[str]) -> list[str]:
    table = re.compile(r"\busage_events\b", re.IGNORECASE)
    return [statement for statement in statements if table.search(statement)]


def raw_cursor(body: str) -> str:
    return base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")


# --- 正常路径 ---------------------------------------------------------------------------


def test_the_project_list_is_exactly_this_customers_projects(both: UsageEventPortal) -> None:
    """§7「正常路径（项目列表）」：本客户 3 项、按创建先后、每项恰好 `id`、`name`。"""
    mine = both.tenant()
    theirs = both.tenant()
    first = both.main_project(mine)
    second = both.project(mine, "Support desk")
    third = both.project(mine, "Sales assistant")
    their_main = both.main_project(theirs)
    their_other = both.project(theirs, "Theirs secret project")
    headers = both.customer(mine)
    before = both.tables()

    response = both.get_projects(headers)

    assert response.status_code == 200
    data = response.json()["data"]
    assert set(data) == {"items"}
    assert all(set(item) == PROJECT_FIELDS for item in data["items"])
    assert data["items"] == [
        {"id": first.public_id, "name": "Chatbot"},
        {"id": second.public_id, "name": "Support desk"},
        {"id": third.public_id, "name": "Sales assistant"},
    ]
    for leaked in (their_main.public_id, their_other.public_id, "Theirs secret project"):
        assert leaked not in response.text
    assert both.tables() == before


def test_three_pages_newest_first_match_the_stored_rows(both: UsageEventPortal) -> None:
    """§7「正常路径（用量）」：5 条、两个项目、两种形态，page_size=2 连翻三页 2 / 2 / 1。"""
    tenant = both.tenant()
    other = both.project(tenant, "Support desk")
    stored = [
        both.put(tenant, occurred_at=ON_DAY, conversation_id="conv-1", feature="crm.summary"),
        both.put(tenant, project=other, occurred_at=ON_DAY + MINUTE, seconds="12.5"),
        both.put(tenant, occurred_at=ON_DAY + 2 * MINUTE, tokens=dict.fromkeys(TOKENS, 7)),
        both.put(tenant, project=other, occurred_at=ON_DAY + 3 * MINUTE, billable="0.125"),
        both.put(tenant, occurred_at=ON_DAY + 4 * MINUTE, seconds="3", billable="0.5"),
    ]
    headers = both.customer(tenant)
    before = both.tables()

    first = both.usage(headers, from_date=DAY, to_date=DAY, page_size=2)
    second = both.usage(
        headers, from_date=DAY, to_date=DAY, page_size=2, cursor=first["next_cursor"]
    )
    third = both.usage(
        headers, from_date=DAY, to_date=DAY, page_size=2, cursor=second["next_cursor"]
    )

    pages = (first, second, third)
    newest_first = [both.expected(row) for row in reversed(stored)]
    assert [len(page["items"]) for page in pages] == [2, 2, 1]
    assert [item for page in pages for item in page["items"]] == newest_first
    assert first["next_cursor"] and second["next_cursor"]
    assert third["next_cursor"] is None
    assert {page["page_size"] for page in pages} == {2}
    items = {item["id"]: item for page in pages for item in page["items"]}
    assert items[stored[1].public_id]["project_name"] == "Support desk"
    assert items[stored[1].public_id]["usage_type"] == "AUDIO_SECOND"
    assert items[stored[1].public_id]["quantity"] == "12.50000000"
    assert items[stored[1].public_id]["input_tokens"] is None
    assert items[stored[0].public_id]["usage_type"] == "LLM_TOKEN"
    assert items[stored[0].public_id]["quantity"] is None
    assert items[stored[0].public_id]["input_tokens"] == TOKENS["input_tokens"]
    assert items[stored[0].public_id]["project_name"] == "Chatbot"
    assert items[stored[0].public_id]["conversation_id"] == "conv-1"
    assert items[stored[0].public_id]["feature"] == "crm.summary"
    assert items[stored[0].public_id]["occurred_at"] == "2026-10-01T02:00:00.123456"
    assert both.tables() == before


def test_only_billed_events_are_listed(both: UsageEventPortal) -> None:
    """§7「只看已计费」：同一区间里六种未计费 / 错误状态各一条，一条都不出现。"""
    tenant = both.tenant()
    billed = both.put(tenant)
    for number, status in enumerate(UNBILLED, start=1):
        both.put(tenant, status=status, occurred_at=ON_DAY + number * MINUTE)
    headers = both.customer(tenant)

    assert both.ids(headers, from_date=DAY, to_date=DAY, page_size=100) == [billed.public_id]


def test_events_at_the_same_instant_page_by_id_without_repeats(both: UsageEventPortal) -> None:
    """§7「同一时刻的平局」：3 条微秒相同，page_size=1 翻页，按 `id` 倒序逐条出现。"""
    tenant = both.tenant()
    stored = [both.put(tenant, occurred_at=ON_DAY) for _ in range(3)]
    headers = both.customer(tenant)

    walked = both.walk(headers, from_date=DAY, to_date=DAY, page_size=1)

    assert [item["id"] for item in walked] == sorted(
        (row.public_id for row in stored), reverse=True
    )


def test_a_malaysian_day_is_utc_16_00_to_16_00(both: UsageEventPortal) -> None:
    """§7「日期换算」：2026-10-01（MYT）= UTC [09-30 16:00, 10-01 16:00)，只返回中间两条。"""
    tenant = both.tenant()
    moments = (
        dt.datetime(2026, 9, 30, 15, 59, 59, 999999),
        dt.datetime(2026, 9, 30, 16, 0, 0),
        dt.datetime(2026, 10, 1, 15, 59, 59, 999999),
        dt.datetime(2026, 10, 1, 16, 0, 0),
    )
    stored = [both.put(tenant, occurred_at=moment) for moment in moments]
    headers = both.customer(tenant)

    assert both.ids(headers, from_date=DAY, to_date=DAY) == [
        stored[2].public_id,
        stored[1].public_id,
    ]


# --- 日期规则与极值 ---------------------------------------------------------------------


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
        ({"from_date": "20261001", "to_date": DAY}, "query.from_date"),
    ],
)
def test_date_rules_are_422_without_echoing_the_value(
    portal: UsageEventPortal, params: dict, fields: str
) -> None:
    """§7「日期规则」：缺、倒置、93 天、格式错 → 422，文案只列字段名。"""
    headers = portal.customer(portal.tenant())

    response = portal.get_usage(headers, **params)

    assert error_of(response) == (422, "VALIDATION_ERROR")
    assert response.json()["error"]["message"] == f"Invalid request fields: {fields}"
    for value in params.values():
        assert value not in response.json()["error"]["message"]


def test_a_92_day_period_is_200(portal: UsageEventPortal) -> None:
    """§7「日期规则」：`to − from = 91`（92 天，含两端）可以；首尾两天的事件都在。"""
    tenant = portal.tenant()
    first = portal.put(tenant, occurred_at=dt.datetime(2026, 6, 30, 16, 0, 0))
    last = portal.put(tenant, occurred_at=dt.datetime(2026, 9, 30, 15, 59, 59, 999999))
    headers = portal.customer(tenant)

    ids = portal.ids(headers, from_date="2026-07-01", to_date="2026-09-30")

    assert ids == [last.public_id, first.public_id]


@pytest.mark.parametrize(
    ("from_date", "to_date", "field"),
    [
        ("0001-01-01", "0001-01-02", "from_date"),
        ("9999-12-30", "9999-12-31", "from_date"),
        ("2019-12-31", "2020-01-01", "from_date"),
        ("2099-12-31", "2100-01-01", "to_date"),
        ("2020-01-01", "9999-12-31", "to_date"),
        ("0001-01-01", "2020-01-01", "from_date"),
    ],
)
def test_extreme_dates_are_422_not_500(
    portal: UsageEventPortal, from_date: str, to_date: str, field: str
) -> None:
    """§7「日期极值」：范围在任何日期运算之前判；只带 `event_id` 时附带这些日期同样 422。"""
    tenant = portal.tenant()
    stored = portal.put(tenant)
    headers = portal.customer(tenant)
    client = TestClient(portal.app, raise_server_exceptions=False)
    lone = from_date if field == "from_date" else to_date

    ranged = client.get(USAGE, params={"from_date": from_date, "to_date": to_date}, headers=headers)
    point = client.get(USAGE, params={"event_id": stored.event_id, field: lone}, headers=headers)

    for response in (ranged, point):
        assert error_of(response) == (422, "VALIDATION_ERROR")
        assert response.json()["error"]["message"] == f"Invalid request fields: query.{field}"
        assert lone not in response.text


@pytest.mark.parametrize(
    ("from_date", "to_date"), [("2020-01-01", "2020-03-01"), ("2099-11-01", "2099-12-31")]
)
def test_the_edges_of_the_date_range_are_200(
    portal: UsageEventPortal, from_date: str, to_date: str
) -> None:
    """§7「日期极值」：`2020-01-01` 起与 `2099-12-31` 止的区间 → 200（空页）。"""
    headers = portal.customer(portal.tenant())

    data = portal.usage(headers, from_date=from_date, to_date=to_date)

    assert data == {"items": [], "page_size": 20, "next_cursor": None}


# --- 筛选 -----------------------------------------------------------------------------


def test_each_filter_and_their_combination(both: UsageEventPortal) -> None:
    """§7「筛选」：按项目、`provider`、`model`、`conversation_id` 各筛一次，及组合。"""
    tenant = both.tenant()
    other = both.project(tenant, "Support desk")
    a = both.put(tenant, occurred_at=ON_DAY, conversation_id="conv-a")
    b = both.put(tenant, project=other, occurred_at=ON_DAY + MINUTE, provider="openai")
    c = both.put(
        tenant,
        project=other,
        occurred_at=ON_DAY + 2 * MINUTE,
        model="gpt-x",
        conversation_id="conv-a",
    )
    d = both.put(tenant, occurred_at=ON_DAY + 3 * MINUTE, provider="openai", model="gpt-x")
    headers = both.customer(tenant)
    day = {"from_date": DAY, "to_date": DAY}

    assert both.ids(headers, **day) == [d.public_id, c.public_id, b.public_id, a.public_id]
    assert both.ids(headers, **day, project_id=other.public_id) == [c.public_id, b.public_id]
    assert both.ids(headers, **day, provider="openai") == [d.public_id, b.public_id]
    assert both.ids(headers, **day, model="gpt-x") == [d.public_id, c.public_id]
    assert both.ids(headers, **day, conversation_id="conv-a") == [c.public_id, a.public_id]
    assert both.ids(headers, **day, provider="openai", model="gpt-x") == [d.public_id]
    assert both.ids(headers, **day, project_id=other.public_id, conversation_id="conv-a") == [
        c.public_id
    ]
    assert both.ids(headers, **day, project_id=other.public_id, provider="anthropic") == [
        c.public_id
    ]
    # 逐字节比较：大小写不同就不匹配。
    assert both.ids(headers, **day, provider="openai", model="GPT-X") == []
    assert both.ids(headers, **day, project_id=ZERO_UUID) == []


def test_an_event_id_alone_finds_exactly_that_event(both: UsageEventPortal) -> None:
    """§7「`event_id` 点查」：只带 `event_id`；别的客户的、未计费的、区间外的 → 空页。"""
    mine = both.tenant()
    theirs = both.tenant()
    target = both.put(mine, occurred_at=ON_DAY)
    both.put(mine, occurred_at=ON_DAY + MINUTE)
    unbilled = both.put(mine, status=UsageEventStatus.RECEIVED, occurred_at=ON_DAY)
    their_event = both.put(theirs, occurred_at=ON_DAY)
    headers = both.customer(mine)

    found = both.usage(headers, event_id=target.event_id)

    assert [item["id"] for item in found["items"]] == [target.public_id]
    assert found["items"][0] == both.expected(target)
    assert found["next_cursor"] is None
    assert both.ids(headers, event_id=their_event.event_id) == []
    assert both.ids(headers, event_id=unbilled.event_id) == []
    assert both.ids(headers, event_id=target.event_id, from_date=DAY, to_date=DAY) == [
        target.public_id
    ]
    assert (
        both.ids(headers, event_id=target.event_id, from_date="2026-10-02", to_date="2026-10-02")
        == []
    )


def test_a_project_of_another_customer_is_an_empty_page_without_a_usage_query(
    portal: UsageEventPortal,
) -> None:
    """§7「项目先查」：项目不属于本客户 → 空页，且不发用量查询。"""
    mine = portal.tenant()
    theirs = portal.tenant()
    portal.put(mine)
    portal.put(theirs)
    their_project = portal.main_project(theirs)
    headers = portal.customer(mine)
    engine = portal.app.state.engine
    statements, record = record_statements(engine)
    try:
        foreign = portal.get_usage(
            headers, from_date=DAY, to_date=DAY, project_id=their_project.public_id
        )
        unknown = portal.get_usage(headers, from_date=DAY, to_date=DAY, project_id=ZERO_UUID)
    finally:
        event.remove(engine, "before_cursor_execute", record)

    for response in (foreign, unknown):
        assert response.status_code == 200
        assert response.json()["data"] == {"items": [], "page_size": 20, "next_cursor": None}
    assert statements, "监听落空"
    assert usage_statements(statements) == []


# --- 游标与边界值 ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "cursor",
    [
        "not base64!",
        "A",
        "x" * 300,
        raw_cursor("garbage"),
        raw_cursor(f"2026-10-01T02:00:00.123456|{ZERO_UUID}|extra"),
        raw_cursor(f"2026-10-01T02:00:00|{ZERO_UUID}"),
        raw_cursor(f"2026-13-45T00:00:00.000000|{ZERO_UUID}"),
        raw_cursor("2026-10-01T02:00:00.123456|not-a-uuid"),
        raw_cursor("2026-10-01T02:00:00.123456|00000000-0000-4000-8000-00000000000A"),
        raw_cursor("2026-10-01T02:00:00.123456"),
        base64.urlsafe_b64encode(b"\xff\xfe\xfd").decode(),
    ],
)
def test_a_tampered_cursor_is_422_without_echoing_it(portal: UsageEventPortal, cursor: str) -> None:
    """§7「游标」：非 base64、缺字段、非法时间、非法 uuid → 422，文案不回显。"""
    headers = portal.customer(portal.tenant())

    response = portal.get_usage(headers, from_date=DAY, to_date=DAY, cursor=cursor)

    assert error_of(response) == (422, "VALIDATION_ERROR")
    assert response.json()["error"]["message"] == "Invalid request fields: query.cursor"
    # 太短的串（`A`、`__79`）随便就是别的文本的子串，只对够长的断言不回显。
    if len(cursor) > 8:
        assert cursor not in response.text


def test_a_valid_cursor_only_narrows_to_strictly_older_rows(portal: UsageEventPortal) -> None:
    """游标是 `(occurred_at, id)` 的严格小于：同一时刻、`id` 更小的行还在后面。"""
    tenant = portal.tenant()
    older = portal.put(tenant, occurred_at=ON_DAY - MINUTE)
    tied = sorted((portal.put(tenant, occurred_at=ON_DAY) for _ in range(2)), key=lambda s: s[1])
    headers = portal.customer(tenant)

    after_top = portal.ids(
        headers, from_date=DAY, to_date=DAY, cursor=encode_cursor(ON_DAY, tied[1].public_id)
    )

    assert after_top == [tied[0].public_id, older.public_id]


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("page_size", "0"),
        ("page_size", "101"),
        ("page_size", "1.5"),
        ("page_size", "twenty"),
        ("provider", "Bad Provider"),
        ("provider", "-anthropic"),
        ("provider", "a" * 65),
        ("model", "-claude"),
        ("model", "claude x"),
        ("model", "m" * 129),
        ("conversation_id", "has space"),
        ("conversation_id", "c" * 129),
        ("event_id", "not-an-event-id"),
        ("event_id", "00000000-0000-7000-8000-00000000000A"),
        ("event_id", "00000000-0000-4000-8000-000000000000"),
    ],
)
def test_out_of_range_parameters_are_422_without_echoing_the_value(
    portal: UsageEventPortal, name: str, value: str
) -> None:
    """§7「边界值」：类型、范围与格式由框架在鉴权之前判，文案只列字段名。"""
    headers = portal.customer(portal.tenant())

    response = portal.get_usage(headers, from_date=DAY, to_date=DAY, **{name: value})
    anonymous = portal.get_usage({}, from_date=DAY, to_date=DAY, **{name: value})

    for refused in (response, anonymous):
        assert error_of(refused) == (422, "VALIDATION_ERROR")
        assert refused.json()["error"]["message"] == f"Invalid request fields: query.{name}"
        assert value not in refused.json()["error"]["message"]


def test_the_largest_page_and_unknown_parameters_are_200(portal: UsageEventPortal) -> None:
    """§7「边界值」：`page_size=100` 为 200；未知参数与排序、`total` 之类忽略。"""
    tenant = portal.tenant()
    stored = [portal.put(tenant, occurred_at=ON_DAY + n * MINUTE) for n in range(3)]
    headers = portal.customer(tenant)

    data = portal.usage(
        headers, from_date=DAY, to_date=DAY, page_size=100, sort="asc", total="yes", page=3
    )

    assert data["page_size"] == 100
    assert [item["id"] for item in data["items"]] == [row.public_id for row in reversed(stored)]
    assert data["next_cursor"] is None


# --- 并发、重放、失败 -------------------------------------------------------------------


def test_late_billing_between_pages_never_repeats_a_row(mysql_portal: UsageEventPortal) -> None:
    """§7「并发」（MySQL）：取第一页后另一事务把一条更旧的事件计费、再插一条更新的事件。

    翻页全程没有任何行出现两次；更旧的那条出现在后面的页里，更新的那条只在最新那一端。
    """
    portal = mysql_portal
    tenant = portal.tenant()
    late = portal.put(tenant, status=UsageEventStatus.RECEIVED, occurred_at=ON_DAY)
    billed = [portal.put(tenant, occurred_at=ON_DAY + n * MINUTE) for n in range(1, 6)]
    headers = portal.customer(tenant)
    params = {"from_date": DAY, "to_date": DAY, "page_size": 2}

    first = portal.usage(headers, **params)
    # 另一个会话、另一个事务里提交。
    portal.mark_processed(late)
    newer = portal.put(tenant, occurred_at=ON_DAY + 10 * MINUTE)
    seen = [item["id"] for item in first["items"]]
    cursor = first["next_cursor"]
    while cursor is not None:
        data = portal.usage(headers, **params, cursor=cursor)
        seen.extend(item["id"] for item in data["items"])
        cursor = data["next_cursor"]

    assert len(seen) == len(set(seen))
    assert seen == [row.public_id for row in reversed(billed)] + [late.public_id]
    assert newer.public_id not in seen
    assert portal.ids(headers, **params)[0] == newer.public_id


def test_the_same_request_twice_returns_the_same_response(portal: UsageEventPortal) -> None:
    """§7「幂等重放」：期间没有新计费的事件时两次的 data 相同。"""
    tenant = portal.tenant()
    for n in range(3):
        portal.put(tenant, occurred_at=ON_DAY + n * MINUTE)
    headers = portal.customer(tenant)
    cursor = portal.usage(headers, from_date=DAY, to_date=DAY, page_size=1)["next_cursor"]

    for path, params in (
        (PROJECTS, {}),
        (USAGE, {"from_date": DAY, "to_date": DAY, "page_size": 1, "cursor": cursor}),
    ):
        first = portal.client.get(path, params=params, headers=headers)
        again = portal.client.get(path, params=params, headers=headers)
        assert (first.status_code, again.status_code) == (200, 200)
        assert first.json()["data"] == again.json()["data"]


def test_a_failing_query_is_a_500_with_a_request_id_and_writes_nothing(
    portal: UsageEventPortal, monkeypatch
) -> None:
    """§7「事务中途失败」：repository 抛数据库异常 → 500 带 request_id，各表不变。"""
    tenant = portal.tenant()
    portal.put(tenant)
    headers = portal.customer(tenant)
    before = portal.tables()

    def broken(*_args, **_kwargs):
        raise OperationalError("SELECT", {}, Exception("fictional database failure"))

    monkeypatch.setattr(usage_repository, "list_processed_events_for_customer", broken)
    monkeypatch.setattr(tenancy_repository, "list_projects_for_tenant", broken)
    client = TestClient(portal.app, raise_server_exceptions=False)

    for path, params in ((USAGE, {"from_date": DAY, "to_date": DAY}), (PROJECTS, {})):
        response = client.get(path, params=params, headers=headers)
        assert error_of(response) == (500, "INTERNAL_ERROR")
        body = response.json()
        assert body["data"] is None
        assert body["request_id"]
        assert "fictional database failure" not in response.text
    assert portal.tables() == before


# --- 扫描上限与超时映射 -----------------------------------------------------------------


def test_the_execution_time_hint_is_rendered_on_mysql_only() -> None:
    """§7「扫描上限」的编译断言：MySQL 上带 `MAX_EXECUTION_TIME` 提示，SQLite 上不带。"""
    default = customer_usage_statement(1, limit=21, provider="no-such-provider")
    short = customer_usage_statement(1, limit=21, timeout_ms=1)

    def compiled(statement, dialect) -> str:
        return " ".join(str(statement.compile(dialect=dialect)).split())

    assert CUSTOMER_USAGE_QUERY_TIMEOUT_MS == 2000
    assert "MAX_EXECUTION_TIME" not in compiled(default, sqlite.dialect())
    assert "MAX_EXECUTION_TIME" not in compiled(short, sqlite.dialect())
    # 只有 MySQL 才有的断言：提示紧跟 SELECT 关键字（优化器提示只在那里生效）。
    assert compiled(default, mysql.dialect()).startswith("SELECT /*+ MAX_EXECUTION_TIME(2000) */ ")
    assert compiled(short, mysql.dialect()).startswith("SELECT /*+ MAX_EXECUTION_TIME(1) */ ")


def test_the_executed_query_carries_the_hint_on_mysql_only(both: UsageEventPortal) -> None:
    """真正发出的那条用量查询：MySQL 上带 2 秒提示，SQLite 上不带。"""
    tenant = both.tenant()
    both.put(tenant)
    headers = both.customer(tenant)
    engine = both.app.state.engine
    statements, record = record_statements(engine)
    try:
        assert len(both.ids(headers, from_date=DAY, to_date=DAY)) == 1
    finally:
        event.remove(engine, "before_cursor_execute", record)

    [query] = usage_statements(statements)
    if engine.dialect.name == "mysql":
        assert "MAX_EXECUTION_TIME(2000)" in query
    else:
        assert "MAX_EXECUTION_TIME" not in query


def test_a_scan_past_the_budget_is_query_too_broad(
    mysql_portal: UsageEventPortal, monkeypatch
) -> None:
    """§7「扫描上限」（MySQL）：30 000 条区间内的事件，按不存在的 `provider` 筛。

    ① 测试专用 1 毫秒超时：repository 抛 `QueryTooBroad`，接口层 422 `QUERY_TOO_BROAD`；
    ② 默认 2 秒：空页。
    """
    portal = mysql_portal
    tenant = portal.tenant()
    portal.bulk(tenant, 30_000, dt.datetime(2026, 9, 30, 16, 0, 0))
    headers = portal.customer(tenant)
    period = {
        "occurred_from": dt.datetime(2026, 9, 30, 16, 0, 0),
        "occurred_to": dt.datetime(2026, 10, 1, 16, 0, 0),
    }

    with portal.factory() as session, pytest.raises(QueryTooBroad):
        list_processed_events_for_customer(
            session, tenant.id, limit=21, provider="no-such-provider", timeout_ms=1, **period
        )
    with portal.factory() as session:
        assert (
            list_processed_events_for_customer(
                session, tenant.id, limit=21, provider="no-such-provider", **period
            )
            == []
        )
    assert portal.ids(headers, from_date=DAY, to_date=DAY, provider="no-such-provider") == []

    monkeypatch.setattr(
        usage_repository,
        "list_processed_events_for_customer",
        functools.partial(list_processed_events_for_customer, timeout_ms=1),
    )
    response = portal.get_usage(headers, from_date=DAY, to_date=DAY, provider="no-such-provider")

    assert error_of(response) == (422, "QUERY_TOO_BROAD")
    assert response.json()["data"] is None
    assert response.json()["request_id"]


def test_query_too_broad_is_a_422_with_one_warning_and_no_parameter_values(
    portal: UsageEventPortal, monkeypatch
) -> None:
    """§7「超时映射」：repository 抛 `QueryTooBroad` → 422 `QUERY_TOO_BROAD`，带 `request_id`；

    一条 `customer_usage_query_timeout` 日志，带 `request_id` 与客户 `public_id`，响应与日志里都
    没有任何查询参数的值。
    """
    tenant = portal.tenant()
    stored = portal.put(tenant, conversation_id="conv-secret-0000")
    headers = portal.customer(tenant)
    before = portal.tables()

    def too_broad(*_args, **_kwargs):
        raise QueryTooBroad

    monkeypatch.setattr(usage_repository, "list_processed_events_for_customer", too_broad)
    params = {
        "from_date": "2026-09-17",
        "to_date": "2026-10-03",
        "provider": "rare-provider",
        "model": "rare-model",
        "conversation_id": "conv-secret-0000",
        "event_id": stored.event_id,
        "project_id": portal.main_project(tenant).public_id,
    }
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        response = portal.get_usage(headers, **params)
    finally:
        root.removeHandler(handler)

    assert error_of(response) == (422, "QUERY_TOO_BROAD")
    body = response.json()
    assert body["data"] is None
    assert body["request_id"]
    # 只看服务端（`app.*`）的日志：测试客户端 httpx 自己会把完整 URL 记在 INFO 上。
    lines = [
        line
        for line in (json.loads(raw) for raw in stream.getvalue().splitlines() if raw.strip())
        if str(line["logger"]).startswith("app.")
    ]
    server_log = json.dumps(lines)
    timeouts = [line for line in lines if line["message"] == "customer_usage_query_timeout"]
    assert len(timeouts) == 1
    [timeout] = timeouts
    assert timeout["level"] == "WARNING"
    assert timeout["request_id"] == body["request_id"]
    assert timeout["customer_id"] == tenant.public_id
    for value in params.values():
        assert value not in response.text
        assert value not in server_log
    assert portal.tables() == before


# --- 租户越权 -------------------------------------------------------------------------


def test_a_customer_only_ever_sees_its_own_usage(both: UsageEventPortal) -> None:
    """§7「租户越权」（INV-8）：A、B 共用同一个 `conversation_id` 与 `request_id` 值。

    A 用 B 的项目 id、`event_id`、`conversation_id`、游标，并带 B 的标识，只得到 A 的行或空页。
    """
    mine = both.tenant()
    theirs = both.tenant()
    their_project = both.project(theirs, "Theirs secret project")
    shared = {"conversation_id": "conv-shared", "request_id": "req-shared"}
    my_rows = [both.put(mine, occurred_at=ON_DAY + n * MINUTE, **shared) for n in range(3)]
    their_rows = [
        both.put(theirs, project=their_project, occurred_at=ON_DAY + n * MINUTE + MINUTE / 2)
        for n in range(3)
    ]
    their_rows.append(both.put(theirs, occurred_at=ON_DAY + 5 * MINUTE, **shared))
    headers = both.customer(mine)
    their_headers = both.customer(theirs)
    forged = {"customer_id": theirs.public_id, "tenant_id": str(theirs.id)}
    day = {"from_date": DAY, "to_date": DAY}
    their_cursor = both.usage(their_headers, **day, page_size=1)["next_cursor"]
    my_ids = [row.public_id for row in reversed(my_rows)]

    responses = [
        both.get_usage(headers, **day, **forged),
        both.get_usage(headers, **day, project_id=their_project.public_id, **forged),
        both.get_usage(headers, event_id=their_rows[0].event_id, **forged),
        both.get_usage(headers, **day, conversation_id="conv-shared", **forged),
        both.get_usage(headers, **day, cursor=their_cursor, **forged),
        both.get_projects(headers, **forged),
    ]

    for response in responses:
        assert response.status_code == 200
    data = [response.json()["data"] for response in responses]
    assert [item["id"] for item in data[0]["items"]] == my_ids
    assert data[1]["items"] == []
    assert data[2]["items"] == []
    assert [item["id"] for item in data[3]["items"]] == my_ids
    # B 的游标只用于 `<` 比较：A 得到的仍是 A 自己更旧的行（这里是 A 的全部三行）。
    assert [item["id"] for item in data[4]["items"]] == my_ids
    assert [item["id"] for item in data[5]["items"]] == [both.main_project(mine).public_id]
    for response in responses:
        for leaked in (
            theirs.public_id,
            their_project.public_id,
            "Theirs secret project",
            *(row.public_id for row in their_rows),
            *(row.event_id for row in their_rows),
        ):
            assert leaked not in response.text


# --- 鉴权与只读契约 ---------------------------------------------------------------------


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
@pytest.mark.parametrize("path", [PROJECTS, USAGE])
def test_non_customers_are_refused_before_any_usage_or_project_is_read(
    portal: UsageEventPortal, path: str, caller: str, expected: tuple[int, str]
) -> None:
    """§7「鉴权」：拒绝的同时一条读用量或项目的语句都没发。"""
    tenant = portal.tenant()
    portal.put(tenant)
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
        response = portal.client.get(
            path, params={"from_date": DAY, "to_date": DAY}, headers=headers
        )
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert error_of(response) == expected
    assert response.json()["data"] is None
    touched = re.compile(r"\b(usage_events|projects)\b", re.IGNORECASE)
    assert not [statement for statement in statements if touched.search(statement)]


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
    """§7「只读契约」：四种账户状态下两个接口都 200。"""
    tenant = both.tenant()
    stored = both.put(tenant)
    both.set_account_status(tenant, status)
    headers = both.customer(tenant)

    projects = both.get_projects(headers)
    usage = both.usage(headers, from_date=DAY, to_date=DAY)

    assert projects.status_code == 200
    assert [item["name"] for item in projects.json()["data"]["items"]] == ["Chatbot"]
    assert [item["id"] for item in usage["items"]] == [stored.public_id]


# --- 无成本毛利、内部计量、与钱包对得上 ---------------------------------------------------


def test_no_cost_rate_reference_price_or_project_secret_leaks(both: UsageEventPortal) -> None:
    """§7「无成本毛利」「内部计量租户」（INV-7）：哨兵值写进库里，原始响应文本里都没有。

    `ck_usage_events_prepaid_reference` 不许 PREPAID 事件带参考价：参考价的哨兵写在一个内部计量
    客户的事件上，它的 `charged_amount` 是 `"0.00000000"`。
    """
    sentinels = {
        "provider_source_cost": SENTINEL_SOURCE_COST,
        "estimated_provider_cost_myr": SENTINEL_ESTIMATED_MYR,
        "fx_rate_applied": SENTINEL_FX_RATE,
    }
    tenant = both.tenant()
    secret = both.project(
        tenant, "Support desk", description=SENTINEL_DESCRIPTION, webhook=SENTINEL_WEBHOOK
    )
    prepaid = both.put(tenant, project=secret, costs=sentinels)
    both.conflict(tenant, prepaid, SENTINEL_API_KEY)
    internal = both.tenant()
    both.set_billing_mode(internal, BillingMode.INTERNAL_METERED_ONLY)
    metered = both.put(
        internal,
        billable="0",
        mode=BillingMode.INTERNAL_METERED_ONLY,
        reference=SENTINEL_REFERENCE_PRICE,
        costs=sentinels,
    )
    headers = both.customer(tenant)
    internal_headers = both.customer(internal)

    responses = [
        both.get_usage(headers, from_date=DAY, to_date=DAY),
        both.get_usage(headers, event_id=prepaid.event_id),
        both.get_projects(headers),
        both.get_usage(internal_headers, from_date=DAY, to_date=DAY),
        both.get_projects(internal_headers),
    ]

    for response in responses:
        assert response.status_code == 200
    [prepaid_item] = responses[0].json()["data"]["items"]
    assert prepaid_item == both.expected(prepaid)
    assert prepaid_item["charged_amount"] == "0.06250000"
    [metered_item] = responses[3].json()["data"]["items"]
    assert metered_item["id"] == metered.public_id
    assert metered_item["charged_amount"] == "0.00000000"
    assert set(metered_item) == ITEM_FIELDS
    forbidden = (
        "0.13579246",
        "0.24681357",
        "0.97531864",
        "0.86420975",
        SENTINEL_DESCRIPTION,
        SENTINEL_WEBHOOK,
        "sentinel-webhook-token",
        SENTINEL_API_KEY,
        '"status"',
        "error_code",
        "error_message",
        "received_at",
        "processed_at",
        "schema_version",
        "fingerprint",
        "provider_source",
        "estimated_provider_cost",
        "fx_rate",
        "reference_customer_price",
        "billing_mode",
        "wallet_transaction",
        "integration_status",
        "status_webhook_url",
        "description",
        "api_key",
        "conflicts",
        "tenant_id",
        "margin",
    )
    for response in responses:
        for value in forbidden:
            assert value not in response.text, value


def test_the_charged_amount_matches_the_wallet_ledger(both: UsageEventPortal) -> None:
    """§7「与钱包对得上」（INV-5）：真实 worker 计费的 PREPAID 事件，`charged_amount` 等于
    同一 `event_id` 账本行 `amount` 的绝对值。"""
    tenant = both.tenant()
    billed = both.bill_for_real(tenant)
    headers = both.customer(tenant)
    with both.factory() as session:
        ledger = session.execute(
            select(WalletTransaction.amount).where(
                WalletTransaction.reference_type == ReferenceType.USAGE_EVENT,
                WalletTransaction.reference_id == billed.event_id,
            )
        ).scalar_one()

    [item] = both.usage(headers, event_id=billed.event_id)["items"]

    assert item["id"] == billed.public_id
    assert item["charged_amount"] == money_text(-ledger)
    assert Decimal(item["charged_amount"]) > 0


def test_amounts_and_quantities_round_trip_exactly_at_the_extremes(
    both: UsageEventPortal,
) -> None:
    """§7「金额精度」（INV-10）：`0.00000001`、`DECIMAL(20,8)` 上界与 `quantity` 最小值逐字相等。

    ⚠️ 上界只在 MySQL 上断言：SQLite 把 NUMERIC 存成浮点，20 位有效数字的上界在库里就已经变了，
    测不到接口。SQLite 上换一个它能精确存下的大数，照样逐字比对。每个金额一个客户：两笔加起来
    会超出余额列的上界。
    """
    if both.factory.kw["bind"].dialect.name == "mysql":
        top = "999999999999.99999999"
    else:
        top = "1234567.12345678"
    smallest = both.tenant()
    largest = both.tenant()
    tiny = both.put(smallest, billable="0.00000001")
    tiny_quantity = both.put(
        smallest, seconds="0.00000001", billable="0", occurred_at=ON_DAY - MINUTE
    )
    huge = both.put(largest, billable=top)

    small_items = both.usage(both.customer(smallest), from_date=DAY, to_date=DAY)["items"]
    [huge_item] = both.usage(both.customer(largest), from_date=DAY, to_date=DAY)["items"]

    assert [(item["id"], item["charged_amount"], item["quantity"]) for item in small_items] == [
        (tiny.public_id, "0.00000001", None),
        (tiny_quantity.public_id, "0.00000000", "0.00000001"),
    ]
    assert (huge_item["id"], huge_item["charged_amount"]) == (huge.public_id, top)
    assert small_items == [both.expected(tiny), both.expected(tiny_quantity)]
