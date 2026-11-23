"""The provider cost world shared by the reconciliation tests (design gate #755 v1 §7).

不以 `test_` 开头，pytest 不会把它当用例收集。夹具在 conftest.py 注册。
库地址在夹具里读（`mysql_url`），不在模块顶层读：
xdist 换库名（conftest 的 `pytest_configure`）之后才有最终值。

**标准世界**逐条照设计 §7（全部时刻是不带时区的 UTC）：

- 供应商 `anthropic`、模型 `claude-test`，一个 USD 价格版本与一条全局 MARKUP 规则，
  只被事件快照引用，数值不参与断言；
- `T_int`（`INTERNAL_METERED_ONLY`）的项目 `P_crm`；`T_pre`（`PREPAID`）的项目
  `P_pre` 与 `P_chat`（集成方项目）；
- USD→MYR 汇率 `V45` `[2026-11-01 00:00, 2026-12-31 12:00)` 4.5，
  `V44` 从 2026-12-31 12:00 起、无上界 4.4；
  每个事件快照的汇率版本就是按它的 `occurred_at` 取到的那一版；
- 事件 E1–E6 与 outbox 行见 `STANDARD_EVENTS`、`EXTRA_OUTBOX`。

**计费额一律 0**：不建钱包与账本行（`PROCESSED_LEDGER_CHECK` 只在计费额 > 0 时要账本行）。
`T_int` 的事件 `reference_customer_price` = 估算 MYR × 2，`T_pre` 的为 NULL。

目录、价格、规则与汇率经服务建（时钟固定在 `SETUP_AT`）；租户、项目、凭据、事件与 outbox
行直接写表。⚠️ outbox 行按表名插入、不经 `app.ai_api`；已处理事件一次写成终态，
之后不再改（MySQL 触发器拒绝改 `PROCESSED` 行）。

**变体世界**：`build_fx_switch_world`、`build_myr_three_world`、`build_unallocatable_world`。
单条根记录经 `add_record` 写（`UNALLOCATABLE_RECORDS` 的 12 月或 `MYR_THREE_DECEMBER`）。

MySQL 那部分需要 `BILLING_TEST_DATABASE_URL` 指向一个**可以被清空的**库，没设时 skip ——
**不要把 skipped 读成 passed**，CI 设了它并把 skipped 判成失败。uuid 是全零占位。
"""

from __future__ import annotations

import datetime as dt
import itertools
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Final

import pytest
from alembic.config import Config
from sqlalchemy import DateTime, Engine, column, create_engine, delete, insert, select, table, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

# ⚠️ 只为让 SQLite 的 `create_all` 建出 `ai_usage_outbox`；本模块不用它的类，
# outbox 行一律按表名写（下面的 `_OUTBOX`）。
import app.ai_api.outbox  # noqa: F401
from alembic import command
from app.core.database import create_session_factory
from app.models.ai_catalog import (
    SEED_METER_TYPES,
    AiModel,
    AiModelAlias,
    AiProvider,
    UsageMeterComponent,
    UsageMeterType,
)
from app.models.auth import DomainOutbox, User, UserRole, UserStatus
from app.models.base import Base, quantize_money
from app.models.fx_rates import FxRateVersion
from app.models.integration import CredentialStatus, IntegrationCredential
from app.models.pricing_rules import PricingRule, PricingScope, PricingStrategy
from app.models.provider_prices import ProviderPriceVersion
from app.models.tenancy import BillingMode, Project, Tenant
from app.models.usage import UsageEvent, UsageEventStatus
from app.models.wallet import TransactionType, Wallet
from app.repositories import provider_cost_reconciliations as reconciliations
from app.repositories.wallet import create_wallet
from app.schemas.provider_prices import ComponentInput
from app.services import ai_catalog, fx_rates, pricing_rules, provider_prices
from app.services.auth import RequestContext
from app.services.wallet_adjustments import post_adjustment

TEST_EMAIL_DOMAIN = "@provider-cost-test.example.com"
CONTEXT = RequestContext(ip_address="203.0.113.75", user_agent="provider-cost-world")

# 服务的固定时钟：早于两个汇率版本的起点，发布时的预约都合法。
SETUP_AT = dt.datetime(2026, 10, 1, 0, 0, 0)

PROVIDER_CODE: Final = "anthropic"
MODEL_CODE: Final = "claude-test"
SCOPE: Final = "AI_MODULE"
COVERAGE: Final = "PERIOD_AGGREGATE"
FEATURE: Final = "provider_cost_test"
METER_CODE: Final = "LLM_TOKEN"

# 设计 §7 写死的 26 字符 event_id：E1–E6 同时是事件与 outbox 行的；另两行 outbox 无事件。
E1: Final = "00000000000000000000000E01"
E2: Final = "00000000000000000000000E02"
E3: Final = "00000000000000000000000E03"
E4: Final = "00000000000000000000000E04"
E5: Final = "00000000000000000000000E05"
E6: Final = "00000000000000000000000E06"
O_PENDING: Final = "00000000000000000000000P01"
O_DISCARDED: Final = "00000000000000000000000D01"

T_INT: Final = "T_int"
T_PRE: Final = "T_pre"
P_CRM: Final = "P_crm"
P_PRE: Final = "P_pre"
P_CHAT: Final = "P_chat"
Q1: Final = "Q1"
Q2: Final = "Q2"
Q3: Final = "Q3"

V45: Final = "V45"
V44: Final = "V44"


def period(label: str) -> tuple[dt.datetime, dt.datetime]:
    """`YYYY-MM` → `[月首 00:00, 下月首 00:00)`, naive UTC."""
    year, month = (int(part) for part in label.split("-"))
    start = dt.datetime(year, month, 1)
    end = dt.datetime(year + month // 12, month % 12 + 1, 1)
    return start, end


# Fictional USD provider prices per 1 000 tokens (只被引用，数值不参与断言).
_PRICE_RATES = {
    "LLM_INPUT_TOKEN": "0.0625",
    "LLM_OUTPUT_TOKEN": "0.25",
    "LLM_CACHE_WRITE_TOKEN": "0.125",
    "LLM_CACHE_READ_TOKEN": "0.0078125",
}
_ZERO_TOKENS = {
    "input_tokens": 0,
    "output_tokens": 0,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0,
}


@dataclass(frozen=True)
class FxSpec:
    name: str
    rate: str
    effective_from: dt.datetime


# 按发布顺序：后一个的起点截断前一个（经 `fx_rates.publish`，与生产同一条路径）。
STANDARD_FX: Final = (
    FxSpec(V45, "4.5", dt.datetime(2026, 11, 1, 0, 0, 0)),
    FxSpec(V44, "4.4", dt.datetime(2026, 12, 31, 12, 0, 0)),
)
# 设计 §7 `fx_switch`：V45 到 2027-01-01 为止、V44 从那一刻起。
FX_SWITCH_FX: Final = (
    FxSpec(V45, "4.5", dt.datetime(2026, 11, 1, 0, 0, 0)),
    FxSpec(V44, "4.4", dt.datetime(2027, 1, 1, 0, 0, 0)),
)


@dataclass(frozen=True)
class EventSpec:
    event_id: str
    project: str
    occurred_at: dt.datetime
    status: UsageEventStatus
    # USD；`None` = 没有快照（未处理）。
    source_cost: str | None
    # 有一行 `DELIVERED` 的 outbox（经 AI 模块发出）。
    outbox: bool


# 设计 §7 的事件表。汇率版本与估算 MYR 由 `occurred_at` 取到的版本算出：
# E1 V45 27.00、E2 V44 19.80（`fx_switch` 为 V45 20.25）、E3 V45 4.50、E4 V44 8.80、
# E5 V45 450.00。
STANDARD_EVENTS: Final = (
    EventSpec(
        E1, P_CRM, dt.datetime(2026, 12, 5, 3, 0, 0), UsageEventStatus.PROCESSED, "6.00", True
    ),
    # 马来西亚已是 2027-01-01 04:00，UTC 仍在 12 月。
    EventSpec(
        E2, P_PRE, dt.datetime(2026, 12, 31, 20, 0, 0), UsageEventStatus.PROCESSED, "4.50", True
    ),
    # 马来西亚已是 2026-12-01，UTC 仍在 11 月。
    EventSpec(
        E3,
        P_CRM,
        dt.datetime(2026, 11, 30, 23, 59, 59, 999999),
        UsageEventStatus.PROCESSED,
        "1.00",
        True,
    ),
    EventSpec(
        E4, P_CRM, dt.datetime(2027, 1, 1, 0, 0, 0), UsageEventStatus.PROCESSED, "2.00", True
    ),
    # 集成方用自己的供应商密钥调用后上报：没有 outbox 行。
    EventSpec(
        E5, P_CHAT, dt.datetime(2026, 12, 10, 0, 0, 0), UsageEventStatus.PROCESSED, "100.00", False
    ),
    EventSpec(
        E6, P_CRM, dt.datetime(2026, 12, 15, 0, 0, 0), UsageEventStatus.PRICING_ERROR, None, True
    ),
)


@dataclass(frozen=True)
class OutboxSpec:
    event_id: str
    status: str
    created_at: dt.datetime


# 没有对应事件的两行 outbox（租户与项目用 T_int / P_crm）。
EXTRA_OUTBOX: Final = (
    OutboxSpec(O_PENDING, "PENDING", dt.datetime(2026, 12, 20, 0, 0, 0)),
    OutboxSpec(O_DISCARDED, "DISCARDED", dt.datetime(2026, 12, 21, 0, 0, 0)),
)

# 项目 → 所属租户；租户 → 计费模式。
_STANDARD_PROJECTS: Final = {P_CRM: T_INT, P_PRE: T_PRE, P_CHAT: T_PRE}
_MYR_THREE_PROJECTS: Final = {Q1: T_INT, Q2: T_INT, Q3: T_INT}
_TENANT_MODES: Final = {
    T_INT: BillingMode.INTERNAL_METERED_ONLY,
    T_PRE: BillingMode.PREPAID,
}

# `myr_three`：三个 AI 模块事件，来源币种 MYR、成本 = 估算 = 1.00。
MYR_THREE_EVENTS: Final = (
    (Q1, "00000000000000000000000Q01", dt.datetime(2026, 12, 2, 12, 0, 0)),
    (Q2, "00000000000000000000000Q02", dt.datetime(2026, 12, 3, 12, 0, 0)),
    (Q3, "00000000000000000000000Q03", dt.datetime(2026, 12, 4, 12, 0, 0)),
)


@dataclass(frozen=True)
class RecordSpec:
    """One root reconciliation of `unallocatable`, its amounts worked out by hand (设计 §7)."""

    public_id: str
    billing_month: str
    source_currency: str
    source_total: str
    # 汇率版本名；MYR 录入为 None。
    fx: str | None
    reconciled_cost_myr: str
    estimated_event_count: int
    estimated_source_cost: str
    estimated_cost_myr: str
    unprocessed_event_count: int
    outbox_unsettled_count: int
    outbox_discarded_count: int


# 2026-12：11.70 × 4.4 = 51.48（V44，取值时刻 12-31 23:59:59.999999）；
# 估算与诊断快照照标准世界的 12 月，另两条全为 0。
UNALLOCATABLE_RECORDS: Final = (
    RecordSpec(
        "00000000-0000-0000-0000-000000000101",
        "2026-12",
        "USD",
        "11.70",
        V44,
        "51.48",
        2,
        "10.50",
        "46.80",
        1,
        1,
        1,
    ),
    RecordSpec(
        "00000000-0000-0000-0000-000000000102",
        "2027-02",
        "MYR",
        "5.00",
        None,
        "5.00",
        0,
        "0",
        "0",
        0,
        0,
        0,
    ),
    RecordSpec(
        "00000000-0000-0000-0000-000000000103",
        "2027-03",
        "MYR",
        "0.00",
        None,
        "0.00",
        0,
        "0",
        "0",
        0,
        0,
        0,
    ),
)

# ⚠️ 按表名写 outbox（不经 `app.ai_api`）。时间列带上 DateTime 类型：SQLite 上与仓储比较时
# 绑定的值同一种文本格式。
_OUTBOX = table(
    "ai_usage_outbox",
    column("event_id"),
    column("request_id"),
    column("tenant_id"),
    column("project_id"),
    column("feature"),
    column("payload_json"),
    column("status"),
    column("attempts"),
    column("next_attempt_at", DateTime),
    column("created_at", DateTime),
)


def zero_uuid(number: int) -> str:
    """A zero placeholder uuid; `number` keeps the ones of one table apart."""
    return f"00000000-0000-0000-0000-{number:012d}"


# --- 世界 -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Tenancy:
    tenant: str
    tenant_id: int
    project_id: int
    credential_id: int
    billing_mode: BillingMode


@dataclass(frozen=True)
class FxVersion:
    id: int
    rate: Decimal
    effective_from: dt.datetime
    effective_to: dt.datetime | None


@dataclass
class World:
    factory: sessionmaker[Session]
    admin: User
    provider_id: int
    model_id: int
    price_version_id: int
    pricing_rule_id: int
    meter: UsageMeterType
    fx: dict[str, FxVersion]
    projects: dict[str, Tenancy]
    # event_id → usage_events.id
    events: dict[str, int] = field(default_factory=dict)
    # 记录的 public_id（`unallocatable`）。
    records: list[str] = field(default_factory=list)
    _numbers: Iterator[int] = field(default_factory=lambda: itertools.count(1))

    @property
    def admin_id(self) -> int:
        return int(self.admin.id)

    def fx_at(self, moment: dt.datetime) -> tuple[str, FxVersion]:
        """The published version in effect at `moment` (what `resolve_fx_rate` would give)."""
        for name, version in self.fx.items():
            if version.effective_from <= moment and (
                version.effective_to is None or moment < version.effective_to
            ):
                return name, version
        raise AssertionError(f"no FX version at {moment}")

    def add_event(
        self,
        *,
        event_id: str,
        project: str,
        occurred_at: dt.datetime,
        status: UsageEventStatus = UsageEventStatus.PROCESSED,
        source_cost: str | None = None,
        source_currency: str = "USD",
    ) -> int:
        """One usage event, written once in its final state (not through the billing worker).

        已处理：来源币种非 MYR 时汇率版本按 `occurred_at` 取，
        估算 = 来源成本 × 汇率；MYR 时两个汇率列为空、估算 = 来源成本。
        计费额 0；内部计量租户的参考价 = 估算 × 2，预付租户的为空。
        """
        tenancy = self.projects[project]
        number = next(self._numbers)
        whole = occurred_at.replace(microsecond=0)
        values: dict[str, Any] = {
            "public_id": zero_uuid(number),
            "event_id": event_id,
            "schema_version": "1.0",
            "tenant_id": tenancy.tenant_id,
            "project_id": tenancy.project_id,
            "integration_credential_id": tenancy.credential_id,
            "request_id": event_id,
            "conversation_id": None,
            "provider_code_raw": PROVIDER_CODE,
            "model_code_raw": MODEL_CODE,
            "usage_meter_type_id": self.meter.id,
            "payload_shape": self.meter.payload_shape,
            "quantity_kind": self.meter.quantity_kind,
            "unit": self.meter.unit,
            **_ZERO_TOKENS,
            "quantity": None,
            "payload_fingerprint": "0" * 64,
            "status": status,
            "feature": None,
            "occurred_at": occurred_at,
            "received_at": whole,
            "created_at": whole,
        }
        if status is UsageEventStatus.PROCESSED:
            assert source_cost is not None
            cost = Decimal(source_cost)
            if source_currency == "MYR":
                fx_id, rate, estimated = None, None, cost
            else:
                _, version = self.fx_at(occurred_at)
                fx_id, rate = version.id, version.rate
                estimated = quantize_money(cost * version.rate)
            internal = tenancy.billing_mode is BillingMode.INTERNAL_METERED_ONLY
            values.update(
                provider_id=self.provider_id,
                model_id=self.model_id,
                provider_price_version_id=self.price_version_id,
                pricing_rule_id=self.pricing_rule_id,
                fx_rate_version_id=fx_id,
                provider_source_currency=source_currency,
                provider_source_cost=cost,
                fx_rate_applied=rate,
                estimated_provider_cost_myr=estimated,
                billable_cost=Decimal(0),
                billing_mode_snapshot=tenancy.billing_mode.value,
                reference_customer_price=estimated * 2 if internal else None,
                wallet_transaction_id=None,
                processed_at=whole,
            )
        with self.factory() as session:
            row = UsageEvent(**values)
            session.add(row)
            session.commit()
            self.events[event_id] = int(row.id)
            return int(row.id)

    def add_outbox(
        self, *, event_id: str, project: str, status: str, created_at: dt.datetime
    ) -> None:
        """One `ai_usage_outbox` row with the placeholders of design §7, by table name."""
        tenancy = self.projects[project]
        with self.factory() as session:
            session.execute(
                insert(_OUTBOX).values(
                    event_id=event_id,
                    request_id=event_id,
                    tenant_id=tenancy.tenant_id,
                    project_id=tenancy.project_id,
                    feature=FEATURE,
                    payload_json="{}",
                    status=status,
                    attempts=0,
                    next_attempt_at=created_at,
                    created_at=created_at,
                )
            )
            session.commit()


def reconciliation_fields(
    world: World,
    *,
    public_id: str,
    billing_month: str,
    source_currency: str,
    source_total: str,
    fx: str | None,
    reconciled_cost_myr: str,
    estimated_event_count: int = 0,
    estimated_source_cost: str = "0",
    estimated_cost_myr: str = "0",
    unprocessed_event_count: int = 0,
    outbox_unsettled_count: int = 0,
    outbox_discarded_count: int = 0,
    supersedes_id: int | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """The columns of one `AI_MODULE` record of `anthropic` for `insert_record`.

    `created_at = period_end + 1 天`，`source_document_reference = test-statement-<YYYY-MM>`。
    """
    start, end = period(billing_month)
    version = world.fx[fx] if fx is not None else None
    return {
        "public_id": public_id,
        "provider_id": world.provider_id,
        "scope": SCOPE,
        "billing_month": billing_month,
        "period_start": start,
        "period_end": end,
        "source_document_reference": f"test-statement-{billing_month}",
        "source_currency": source_currency,
        "source_total": Decimal(source_total),
        "fx_rate_version_id": version.id if version is not None else None,
        "fx_rate_applied": version.rate if version is not None else None,
        "reconciled_cost_myr": Decimal(reconciled_cost_myr),
        "coverage_method": COVERAGE,
        "estimated_event_count": estimated_event_count,
        "unprocessed_event_count": unprocessed_event_count,
        "outbox_unsettled_count": outbox_unsettled_count,
        "outbox_discarded_count": outbox_discarded_count,
        "estimated_source_cost": Decimal(estimated_source_cost),
        "estimated_cost_myr": Decimal(estimated_cost_myr),
        "supersedes_id": supersedes_id,
        "reason": reason,
        "created_by": world.admin_id,
        "created_at": end + dt.timedelta(days=1),
    }


# --- 建世界 ---------------------------------------------------------------------------


def _clock() -> dt.datetime:
    return SETUP_AT


def _row_id(session: Session, model: Any, public_id: str) -> int:
    return int(session.execute(select(model.id).where(model.public_id == public_id)).scalar_one())


def _catalog(
    factory: sessionmaker[Session], *, price_currency: str, fx_specs: tuple[FxSpec, ...]
) -> World:
    """Admin, provider, model, one published price, a global MARKUP rule and the FX versions."""
    with factory() as session:
        admin = User(
            email=f"{uuid.uuid4().hex}{TEST_EMAIL_DOMAIN}",
            password_hash="not-a-real-hash",
            role=UserRole.ADMIN,
            status=UserStatus.ACTIVE,
            created_at=SETUP_AT,
            updated_at=SETUP_AT,
        )
        session.add(admin)
        session.commit()
    common: dict[str, Any] = {"actor": admin, "context": CONTEXT, "clock": _clock}
    provider = ai_catalog.create_provider(
        factory, code=PROVIDER_CODE, display_name="Fictional provider", **common
    )
    model = ai_catalog.create_model(
        factory, provider_id=provider.id, code=MODEL_CODE, display_name="Fictional model", **common
    )
    price = provider_prices.create_draft(
        factory,
        provider_id=provider.id,
        model_id=model.id,
        source_currency=price_currency,
        source_reference="Fictional price page",
        components=[
            ComponentInput.model_validate(
                {"component_code": code, "unit_quantity": "1000", "rate_amount": rate}
            )
            for code, rate in _PRICE_RATES.items()
        ],
        **common,
    )
    provider_prices.publish(factory, price_version_id=price.id, effective_from=None, **common)
    rule = pricing_rules.create_draft(
        factory,
        priority_scope=PricingScope.GLOBAL,
        customer_id=None,
        provider_id=None,
        model_id=None,
        strategy=PricingStrategy.MARKUP,
        markup_multiplier=Decimal("2"),
        components=None,
        **common,
    )
    pricing_rules.publish(factory, rule_id=rule.id, effective_from=None, **common)
    fx_public_ids: dict[str, str] = {}
    for spec in fx_specs:
        draft = fx_rates.create_draft(
            factory,
            base_currency="USD",
            rate=Decimal(spec.rate),
            observed_at=SETUP_AT,
            source_reference="Fictional quote",
            **common,
        )
        fx_rates.publish(factory, fx_rate_id=draft.id, effective_from=spec.effective_from, **common)
        fx_public_ids[spec.name] = draft.id

    with factory() as session:
        meter = session.execute(
            select(UsageMeterType).where(UsageMeterType.code == METER_CODE)
        ).scalar_one()
        session.expunge(meter)
        fx: dict[str, FxVersion] = {}
        for name, public_id in fx_public_ids.items():
            row = session.execute(
                select(FxRateVersion).where(FxRateVersion.public_id == public_id)
            ).scalar_one()
            assert row.effective_from is not None
            fx[name] = FxVersion(int(row.id), row.rate, row.effective_from, row.effective_to)
        return World(
            factory=factory,
            admin=admin,
            provider_id=_row_id(session, AiProvider, provider.id),
            model_id=_row_id(session, AiModel, model.id),
            price_version_id=_row_id(session, ProviderPriceVersion, price.id),
            pricing_rule_id=_row_id(session, PricingRule, rule.id),
            meter=meter,
            fx=fx,
            projects={},
        )


def _tenancies(world: World, projects: dict[str, str]) -> None:
    """The tenants (no wallet), their projects and one credential per project, by table."""
    tenant_ids: dict[str, int] = {}
    with world.factory() as session:
        for number, tenant in enumerate(dict.fromkeys(projects.values()), start=1):
            row = Tenant(
                public_id=zero_uuid(number),
                company_name=f"Fictional {tenant} Sdn Bhd",
                email=f"{tenant.lower()}{TEST_EMAIL_DOMAIN}",
                billing_mode=_TENANT_MODES[tenant],
                created_at=SETUP_AT,
                updated_at=SETUP_AT,
            )
            session.add(row)
            session.flush()
            tenant_ids[tenant] = int(row.id)
        for number, (project, tenant) in enumerate(projects.items(), start=1):
            project_row = Project(
                public_id=zero_uuid(number),
                tenant_id=tenant_ids[tenant],
                name=project,
                created_at=SETUP_AT,
                updated_at=SETUP_AT,
            )
            session.add(project_row)
            session.flush()
            credential = IntegrationCredential(
                tenant_id=tenant_ids[tenant],
                project_id=project_row.id,
                public_api_key=f"ak_fc{number:030d}",
                key_version=1,
                encrypted_secret="not-a-real-ciphertext",
                encryption_key_version=1,
                status=CredentialStatus.ACTIVE,
                valid_from=SETUP_AT,
                created_at=SETUP_AT,
            )
            session.add(credential)
            session.flush()
            world.projects[project] = Tenancy(
                tenant,
                tenant_ids[tenant],
                int(project_row.id),
                int(credential.id),
                _TENANT_MODES[tenant],
            )
        session.commit()


def build_standard_world(
    factory: sessionmaker[Session], *, fx_specs: tuple[FxSpec, ...] = STANDARD_FX
) -> World:
    """The standard world of design §7 (`fx_specs` only differs for `fx_switch`)."""
    world = _catalog(factory, price_currency="USD", fx_specs=fx_specs)
    _tenancies(world, _STANDARD_PROJECTS)
    for spec in STANDARD_EVENTS:
        world.add_event(
            event_id=spec.event_id,
            project=spec.project,
            occurred_at=spec.occurred_at,
            status=spec.status,
            source_cost=spec.source_cost,
        )
        if spec.outbox:
            world.add_outbox(
                event_id=spec.event_id,
                project=spec.project,
                status="DELIVERED",
                created_at=spec.occurred_at,
            )
    for extra in EXTRA_OUTBOX:
        world.add_outbox(
            event_id=extra.event_id, project=P_CRM, status=extra.status, created_at=extra.created_at
        )
    return world


def build_fx_switch_world(factory: sessionmaker[Session]) -> World:
    """`fx_switch`: the standard world written from scratch with V45 up to 2027-01-01.

    ⚠️ 一开始就按变体的值写：V45、V44 的区间，
    E2 的汇率快照（V45）与估算（20.25）。
    不在标准世界上改已发布的汇率版本与已处理事件（MySQL 触发器不允许）；
    V45 的尽头是发布 V44 时 `fx_rates.publish` 照常截断出来的。
    """
    return build_standard_world(factory, fx_specs=FX_SWITCH_FX)


def build_myr_three_world(factory: sessionmaker[Session]) -> World:
    """`myr_three`: on its own, T_int's AI-module projects Q1–Q3, one MYR event each.

    来源币种 MYR、成本 = 估算 = 1.00（两个汇率列为空）、参考价 2.00、计费额 0；
    outbox 各一行 `DELIVERED`。价格版本以 MYR 计价，没有汇率版本。
    """
    world = _catalog(factory, price_currency="MYR", fx_specs=())
    _tenancies(world, _MYR_THREE_PROJECTS)
    for project, event_id, occurred_at in MYR_THREE_EVENTS:
        world.add_event(
            event_id=event_id,
            project=project,
            occurred_at=occurred_at,
            source_cost="1.00",
            source_currency="MYR",
        )
        world.add_outbox(
            event_id=event_id, project=project, status="DELIVERED", created_at=occurred_at
        )
    return world


def build_unallocatable_world(factory: sessionmaker[Session]) -> World:
    """`unallocatable`: the standard world and three root records written by `insert_record`.

    不经 `confirm`：金额与汇率列按设计「金额与精度」算好写死（`UNALLOCATABLE_RECORDS`）。
    """
    world = build_standard_world(factory)
    with factory() as session:
        for spec in UNALLOCATABLE_RECORDS:
            reconciliations.insert_record(
                session,
                **reconciliation_fields(
                    world,
                    public_id=spec.public_id,
                    billing_month=spec.billing_month,
                    source_currency=spec.source_currency,
                    source_total=spec.source_total,
                    fx=spec.fx,
                    reconciled_cost_myr=spec.reconciled_cost_myr,
                    estimated_event_count=spec.estimated_event_count,
                    estimated_source_cost=spec.estimated_source_cost,
                    estimated_cost_myr=spec.estimated_cost_myr,
                    unprocessed_event_count=spec.unprocessed_event_count,
                    outbox_unsettled_count=spec.outbox_unsettled_count,
                    outbox_discarded_count=spec.outbox_discarded_count,
                ),
            )
            world.records.append(spec.public_id)
        session.commit()
    return world


# --- 库 -------------------------------------------------------------------------------


def seed_meter_types(factory: sessionmaker[Session]) -> None:
    """What migration 0012 seeds, for the SQLite tables `create_all` builds empty."""
    with factory() as session:
        for number, seed in enumerate(SEED_METER_TYPES, start=1):
            kind = UsageMeterType(
                public_id=zero_uuid(number),
                code=seed.code,
                display_name=seed.display_name,
                payload_shape=seed.payload_shape,
                unit=seed.unit,
                quantity_kind=seed.quantity_kind,
                created_at=SETUP_AT,
                updated_at=SETUP_AT,
            )
            session.add(kind)
            session.flush()
            for part in seed.components:
                session.add(
                    UsageMeterComponent(
                        meter_type_id=kind.id,
                        payload_shape=seed.payload_shape,
                        component_code=part.component_code,
                        quantity_field=part.quantity_field,
                        created_at=SETUP_AT,
                    )
                )
        session.commit()


def sqlite_factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    seed_meter_types(factory)
    try:
        yield factory
    finally:
        engine.dispose()


def sqlite_shared_factory() -> Iterator[sessionmaker[Session]]:
    """Like `sqlite_factory`, with one connection shared by every thread (AIH-TASK-245).

    TestClient 在线程池里跑同步端点：内存库必须所有连接共用一个（StaticPool，与
    test_admin_customers_api.py 的 app 夹具同理），否则端点看到的是另一个空库。
    """
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    seed_meter_types(factory)
    try:
        yield factory
    finally:
        engine.dispose()


def mysql_url() -> str:
    # 环境变量在夹具里读：xdist 换库名（conftest 的 pytest_configure）之后才有最终值。
    url = os.environ.get("BILLING_TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("BILLING_TEST_DATABASE_URL is not set; the MySQL half needs a real MySQL")
    return url


def mysql_factory(monkeypatch: pytest.MonkeyPatch) -> Iterator[sessionmaker[Session]]:
    url = mysql_url()
    # ⚠️ 建表必须走 alembic：版本表与对账表的触发器只有迁移会建。
    from app.core.config import get_settings

    monkeypatch.setenv("BILLING_DATABASE_URL", url)
    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")
    get_settings.cache_clear()

    engine = create_engine(url, pool_size=10, max_overflow=4)
    clean(engine)
    try:
        yield create_session_factory(engine)
    finally:
        clean(engine)
        engine.dispose()


# 本世界写过的、带 BEFORE DELETE 触发器（拒绝 DELETE）的表。锁表 `fx_rate_locks`、
# `pricing_rule_locks` 的种子行是取锁的前提，本世界不写它们，不清。
TRUNCATED_TABLES: Final = (
    "provider_cost_reconciliations",
    "audit_logs",
    "fx_rate_versions",
    "provider_price_versions",
    "provider_price_components",
    "pricing_rules",
    "pricing_rule_components",
)


def clean(engine: Engine) -> None:
    """MySQL only. ⚠️ 库是共享的，每个用例前后都清场。

    带 BEFORE DELETE 触发器的表关掉外键检查后 TRUNCATE（TRUNCATE 不经触发器），再恢复
    （写法同 payment_world.py 的 `clean_receipts`）；其余按外键从子到父 DELETE 本世界的行。
    """
    with engine.begin() as connection:
        connection.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        try:
            for name in TRUNCATED_TABLES:
                connection.execute(text(f"TRUNCATE TABLE {name}"))
        finally:
            connection.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
    outbox = table("ai_usage_outbox", column("tenant_id"))
    with engine.begin() as connection:
        ours = select(Tenant.id).where(Tenant.email.like(f"%{TEST_EMAIL_DOMAIN}"))
        connection.execute(delete(outbox).where(outbox.c.tenant_id.in_(ours)))
        connection.execute(delete(UsageEvent).where(UsageEvent.tenant_id.in_(ours)))
        connection.execute(
            delete(IntegrationCredential).where(IntegrationCredential.tenant_id.in_(ours))
        )
        connection.execute(delete(Project).where(Project.tenant_id.in_(ours)))
        connection.execute(delete(Tenant).where(Tenant.email.like(f"%{TEST_EMAIL_DOMAIN}")))
        connection.execute(delete(AiModelAlias))
        connection.execute(delete(AiModel))
        connection.execute(delete(AiProvider))
        connection.execute(delete(User).where(User.email.like(f"%{TEST_EMAIL_DOMAIN}")))


# --- 单条记录（AIH-TASK-248，设计闸门 #756 v2 §7） ---

# 设计 #756 §7「分摊舍入」：`myr_three` 的 12 月记录 MYR 1.00，估算快照是三个事件各 1.00。
MYR_THREE_DECEMBER: Final = RecordSpec(
    "00000000-0000-0000-0000-000000000201",
    "2026-12",
    "MYR",
    "1.00",
    None,
    "1.00",
    3,
    "3.00",
    "3.00",
    0,
    0,
    0,
)


def add_record(world: World, spec: RecordSpec) -> str:
    """One root record of `spec` written by `insert_record` (not through `confirm`)."""
    with world.factory() as session:
        reconciliations.insert_record(
            session,
            **reconciliation_fields(
                world,
                public_id=spec.public_id,
                billing_month=spec.billing_month,
                source_currency=spec.source_currency,
                source_total=spec.source_total,
                fx=spec.fx,
                reconciled_cost_myr=spec.reconciled_cost_myr,
                estimated_event_count=spec.estimated_event_count,
                estimated_source_cost=spec.estimated_source_cost,
                estimated_cost_myr=spec.estimated_cost_myr,
                unprocessed_event_count=spec.unprocessed_event_count,
                outbox_unsettled_count=spec.outbox_unsettled_count,
                outbox_discarded_count=spec.outbox_discarded_count,
            ),
        )
        session.commit()
    world.records.append(spec.public_id)
    return spec.public_id


# --- 钱包（AIH-TASK-244「快照不变」，只在用到的用例里建） ---------------------------------------


def add_wallet_credit(world: World, *, project: str, amount: str) -> None:
    """A wallet for the project's tenant and one manual credit through `post_adjustment`.

    标准世界没有钱包与账本；「快照不变」用例要一个非空的余额与账本行数作比较对象。
    经现有的手工调账服务记账（与生产同一条路径），不直接写账本。
    MySQL 上用完要在世界清场之前调 `clean_wallets`（有钱包的租户删不掉）。
    """
    tenancy = world.projects[project]
    with world.factory() as session:
        create_wallet(session, tenant_id=tenancy.tenant_id, now=SETUP_AT)
        customer_id = session.execute(
            select(Tenant.public_id).where(Tenant.id == tenancy.tenant_id)
        ).scalar_one()
        session.commit()
    post_adjustment(
        world.factory,
        actor=world.admin,
        customer_id=customer_id,
        transaction_type=TransactionType.ADJUSTMENT_CREDIT,
        amount=Decimal(amount),
        reason="Fictional opening credit",
        idempotency_key=zero_uuid(next(world._numbers)),
        context=CONTEXT,
        now=SETUP_AT,
    )


def clean_wallets(engine: Engine) -> None:
    """MySQL only: the ledger, wallets and tenant outbox rows `add_wallet_credit` left behind.

    账本拒绝 DELETE（触发器），关掉外键检查后 TRUNCATE（写法同 test_wallet_adjustment_service.py）；
    审计由 `clean` 清。要在 `clean` 删租户之前调用。
    """
    with engine.begin() as connection:
        connection.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        try:
            connection.execute(text("TRUNCATE TABLE wallet_transactions"))
        finally:
            connection.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
    with engine.begin() as connection:
        ours = select(Tenant.id).where(Tenant.email.like(f"%{TEST_EMAIL_DOMAIN}"))
        our_public_ids = select(Tenant.public_id).where(Tenant.email.like(f"%{TEST_EMAIL_DOMAIN}"))
        connection.execute(delete(Wallet).where(Wallet.tenant_id.in_(ours)))
        connection.execute(
            delete(DomainOutbox).where(
                DomainOutbox.aggregate_type == "tenant",
                DomainOutbox.aggregate_id.in_(our_public_ids),
            )
        )


# --- 夹具（conftest.py 注册） -----------------------------------------------------------


@pytest.fixture
def provider_cost_sqlite() -> Iterator[sessionmaker[Session]]:
    """An empty SQLite database (meter types seeded) for a world builder of this module."""
    yield from sqlite_factory()


@pytest.fixture
def provider_cost_mysql(monkeypatch) -> Iterator[sessionmaker[Session]]:
    """A clean real MySQL at head for a world builder of this module."""
    yield from mysql_factory(monkeypatch)


@pytest.fixture(params=["sqlite", "mysql"])
def provider_cost_db(request, monkeypatch) -> Iterator[sessionmaker[Session]]:
    """The empty database on SQLite and on a real MySQL."""
    if request.param == "mysql":
        yield from mysql_factory(monkeypatch)
        return
    yield from sqlite_factory()


@pytest.fixture
def provider_cost_world(provider_cost_db) -> World:
    """The standard world on SQLite and on a real MySQL."""
    return build_standard_world(provider_cost_db)


@pytest.fixture
def provider_cost_mysql_world(provider_cost_mysql) -> World:
    """The standard world on a real MySQL only."""
    return build_standard_world(provider_cost_mysql)
