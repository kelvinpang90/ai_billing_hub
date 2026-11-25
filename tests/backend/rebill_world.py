"""The rebill world shared by the rebill tests (design gate #830 v12 §7, AIH-TASK-254).

不以 `test_` 开头，pytest 不会把它当用例收集。夹具在 conftest.py 注册。
库地址在夹具里读（`provider_cost_world.mysql_url`），不在模块顶层读：
xdist 换库名（conftest 的 `pytest_configure`）之后才有最终值。

**世界**（全部时刻是不带时区的 UTC，服务的时钟固定在 `SETUP_AT`）：

- 供应商 `anthropic`，模型 `claude-test`（`M1`）与 `claude-other`（`M2`）；USD→MYR 汇率 4.5，
  从 2026-11-01 起；
- 客户：`A`（预付，项目 `A1`、`A2`）、`B`（预付，项目 `B1`）、`I`（内部计量，项目 `I1`）；
  `A`、`B` 有钱包；
- 价格版本（`VERSIONS`）与定价规则（`RULES`）经服务建、发布、停用、丢弃，覆盖
  PUBLISHED / RETIRED（生效区间为空）/ DRAFT / DISCARDED、别的模型、别的币种、缺分量、
  别的客户、别的项目、FIXED_RATE 缺分量与溢出；
- 事件由 `add_event` 一次写成终态：已计费的计费额由 `reprice_from_snapshot` 按快照（`V_BASE`、
  汇率、`R_GLOBAL`，或指定的规则）算出，大于 0 时先经 `post_transaction` 写 `AI_USAGE` 行；
  计费额为 0 的没有账本行；内部计量的计费额 0、参考价是按快照算出的计费额；
- `add_rebill` 直接写一次 Rebill（`REBILL_*` 账本行经 `post_transaction`）与它的明细，
  `previous_*` 取写入时的当前有效状态；`add_statement` 写一份已定稿的对账单。

复用 tests/backend/provider_cost_world.py 的辅助函数（建库、种子、清场），不改它。

MySQL 那部分需要 `BILLING_TEST_DATABASE_URL` 指向一个**可以被清空的**库，没设时 skip ——
**不要把 skipped 读成 passed**，CI 设了它并把 skipped 判成失败。uuid 是全零占位。
"""

from __future__ import annotations

import datetime as dt
import itertools
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Final

import pytest
from alembic.config import Config
from provider_cost_world import (
    CONTEXT,
    SETUP_AT,
    TEST_EMAIL_DOMAIN,
    clean_wallets,
    mysql_url,
    seed_meter_types,
    zero_uuid,
)
from provider_cost_world import clean as clean_provider_cost
from sqlalchemy import Engine, create_engine, func, insert, select, text
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.core.database import create_session_factory
from app.models.ai_catalog import AiModel, AiProvider, UsageMeterType
from app.models.auth import User, UserRole, UserStatus
from app.models.base import Base
from app.models.fx_rates import FxRateVersion
from app.models.integration import CredentialStatus, IntegrationCredential
from app.models.pricing_rules import PricingRule, PricingScope, PricingStrategy
from app.models.provider_prices import ProviderPriceVersion
from app.models.statements import MonthlyStatement, StatementStatus
from app.models.tax_policy import (
    TaxJurisdiction,
    TaxPolicyStatus,
    TaxPolicyType,
    TaxPolicyVersion,
)
from app.models.tenancy import BillingMode, Project, Tenant
from app.models.usage import UsageEvent, UsageEventStatus
from app.models.usage_rebills import UsageRebill, UsageRebillLine
from app.models.wallet import ReferenceType, TransactionType
from app.repositories.wallet import create_wallet, post_transaction
from app.schemas.pricing_rules import RuleComponentInput
from app.schemas.provider_prices import ComponentInput
from app.services import ai_catalog, fx_rates, pricing_rules, provider_prices
from app.services.pricing_engine import ChargeResult, ChargeSnapshot, reprice_from_snapshot
from app.services.statements import period_containing, statement_period

PROVIDER_CODE: Final = "anthropic"
MODEL_CODE: Final = "claude-test"
OTHER_MODEL_CODE: Final = "claude-other"
METER_CODE: Final = "LLM_TOKEN"
FX_RATE: Final = Decimal("4.5")
FX_FROM: Final = dt.datetime(2026, 11, 1, 0, 0, 0)

A: Final = "A"
B: Final = "B"
INTERNAL: Final = "I"
A1: Final = "A1"
A2: Final = "A2"
B1: Final = "B1"
I1: Final = "I1"
_TENANT_MODES: Final = {
    A: BillingMode.PREPAID,
    B: BillingMode.PREPAID,
    INTERNAL: BillingMode.INTERNAL_METERED_ONLY,
}
_PROJECTS: Final = {A1: A, A2: A, B1: B, I1: INTERNAL}

# 默认数量：按 V_BASE 是 0.5 USD，汇率 4.5 是 2.25 MYR，R_GLOBAL ×2 算出计费额 4.50000000。
# ⚠️ SQLite 把 Numeric 存成二进制浮点，`ck_wallet_transactions_arithmetic` 在 REAL 上算：
# 写进账本的金额与余额都必须是二进制精确的（分母是 2 的小次幂），所以这里的数量、单价、
# R_LOW 的倍数与用例里写给 `add_rebill` 的计费额都照这个选。
DEFAULT_TOKENS: Final = (1000, 1000, 1000, 8000)
ZERO_TOKENS: Final = (0, 0, 0, 0)
_TOKEN_NAMES: Final = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)
_LLM_CODES: Final = (
    "LLM_INPUT_TOKEN",
    "LLM_OUTPUT_TOKEN",
    "LLM_CACHE_WRITE_TOKEN",
    "LLM_CACHE_READ_TOKEN",
)

# --- 价格版本 -------------------------------------------------------------------------
V_BASE: Final = "V_BASE"
V_HIGH: Final = "V_HIGH"
V_MYR: Final = "V_MYR"
V_EMBED: Final = "V_EMBED"
V_RETIRED: Final = "V_RETIRED"
V_DRAFT: Final = "V_DRAFT"
V_DISCARDED: Final = "V_DISCARDED"
V_OTHER_MODEL: Final = "V_OTHER_MODEL"


@dataclass(frozen=True)
class VersionSpec:
    name: str
    model: str
    currency: str
    # component_code → (unit_quantity, rate_amount)，都是十进制字符串。
    components: dict[str, tuple[str, str]]
    # "publish"：发布（`effective_from` 为空 = 时间线的第一个）；"retire"：预约后撤销，
    # 留下空区间的 RETIRED；"draft"；"discard"。
    action: str
    effective_from: dt.datetime | None = None


def _llm(rates: Sequence[str], unit: str = "1000") -> dict[str, tuple[str, str]]:
    return {code: (unit, rate) for code, rate in zip(_LLM_CODES, rates, strict=True)}


_BASE_RATES: Final = ("0.0625", "0.25", "0.125", "0.0078125")
# 按这个顺序建（同一模型的发布起点递增）。
VERSIONS: Final = (
    VersionSpec(V_BASE, MODEL_CODE, "USD", _llm(_BASE_RATES), "publish"),
    # 单价翻倍；生效区间从 2027-03-01 起，不覆盖任何事件时刻。
    VersionSpec(
        V_HIGH,
        MODEL_CODE,
        "USD",
        _llm(("0.125", "0.5", "0.25", "0.015625")),
        "publish",
        dt.datetime(2027, 3, 1),
    ),
    VersionSpec(V_MYR, MODEL_CODE, "MYR", _llm(_BASE_RATES), "publish", dt.datetime(2027, 4, 1)),
    # 只定了 EMBEDDING_TOKEN 的价：对 LLM_TOKEN 事件缺分量。
    VersionSpec(
        V_EMBED,
        MODEL_CODE,
        "USD",
        {"EMBEDDING_TOKEN": ("1000", "0.01")},
        "publish",
        dt.datetime(2027, 5, 1),
    ),
    # 预约在 2027-09-01、生效前撤销：RETIRED，区间 [F, F) 为空。
    VersionSpec(
        V_RETIRED,
        MODEL_CODE,
        "USD",
        _llm(("0.1", "0.4", "0.2", "0.01")),
        "retire",
        dt.datetime(2027, 9, 1),
    ),
    VersionSpec(V_DRAFT, MODEL_CODE, "USD", _llm(_BASE_RATES), "draft"),
    VersionSpec(V_DISCARDED, MODEL_CODE, "USD", _llm(_BASE_RATES), "discard"),
    VersionSpec(V_OTHER_MODEL, OTHER_MODEL_CODE, "USD", _llm(_BASE_RATES), "publish"),
)

# --- 定价规则 -------------------------------------------------------------------------
R_GLOBAL: Final = "R_GLOBAL"
R_LOW: Final = "R_LOW"
R_FIXED: Final = "R_FIXED"
R_HUGE: Final = "R_HUGE"
R_OTHER_CUSTOMER: Final = "R_OTHER_CUSTOMER"
R_OTHER_PROJECT: Final = "R_OTHER_PROJECT"
R_OTHER_MODEL: Final = "R_OTHER_MODEL"
R_FIXED_MISSING: Final = "R_FIXED_MISSING"
R_RETIRED: Final = "R_RETIRED"
R_DRAFT: Final = "R_DRAFT"
R_DISCARDED: Final = "R_DISCARDED"


@dataclass(frozen=True)
class RuleSpec:
    name: str
    scope: PricingScope
    customer: str | None
    project: str | None
    with_provider: bool
    model: str | None
    strategy: PricingStrategy
    multiplier: str | None
    components: dict[str, tuple[str, str]] | None
    # 同 `VersionSpec.action`。
    action: str
    effective_from: dt.datetime | None = None


_MARKUP = PricingStrategy.MARKUP
_FIXED = PricingStrategy.FIXED_RATE
RULES: Final = (
    # 事件快照的规则：全局 ×2。
    RuleSpec(R_GLOBAL, PricingScope.GLOBAL, None, None, False, None, _MARKUP, "2", None, "publish"),
    RuleSpec(
        R_LOW,
        PricingScope.GLOBAL_PROVIDER_MODEL,
        None,
        None,
        True,
        MODEL_CODE,
        _MARKUP,
        "1.5",
        None,
        "publish",
    ),
    # 客户 A 的 FIXED_RATE：1 个输入 token 恰好 0.000000005（第 9 位小数的 5，进位成 0.00000001）；
    # 1 个缓存读 token 0.000000004（舍成 0）。
    RuleSpec(
        R_FIXED,
        PricingScope.CUSTOMER_PROVIDER_MODEL,
        A,
        None,
        True,
        MODEL_CODE,
        _FIXED,
        None,
        _llm(("0.000005", "0.00001", "0.00001", "0.000004")),
        "publish",
    ),
    # 1 个 token 就超出 DECIMAL(20,8)。
    RuleSpec(
        R_HUGE,
        PricingScope.CUSTOMER,
        A,
        None,
        False,
        None,
        _FIXED,
        None,
        _llm(("999999999999",) * 4, unit="0.00000001"),
        "publish",
    ),
    RuleSpec(
        R_OTHER_CUSTOMER, PricingScope.CUSTOMER, B, None, False, None, _MARKUP, "3", None, "publish"
    ),
    RuleSpec(
        R_OTHER_PROJECT, PricingScope.PROJECT, A, A2, False, None, _MARKUP, "3", None, "publish"
    ),
    RuleSpec(
        R_OTHER_MODEL,
        PricingScope.GLOBAL_PROVIDER_MODEL,
        None,
        None,
        True,
        OTHER_MODEL_CODE,
        _MARKUP,
        "3",
        None,
        "publish",
    ),
    # FIXED_RATE，只定了 EMBEDDING_TOKEN 的价。
    RuleSpec(
        R_FIXED_MISSING,
        PricingScope.CUSTOMER_PROVIDER,
        A,
        None,
        True,
        None,
        _FIXED,
        None,
        {"EMBEDDING_TOKEN": ("1000", "0.01")},
        "publish",
    ),
    # 客户 A 项目 A1 的规则，预约在 2027-09-01、生效前撤销：RETIRED，区间为空。
    RuleSpec(
        R_RETIRED,
        PricingScope.PROJECT_PROVIDER,
        A,
        A1,
        True,
        None,
        _MARKUP,
        "2.5",
        None,
        "retire",
        dt.datetime(2027, 9, 1),
    ),
    RuleSpec(
        R_DRAFT,
        PricingScope.PROJECT_PROVIDER_MODEL,
        A,
        A1,
        True,
        MODEL_CODE,
        _MARKUP,
        "1.5",
        None,
        "draft",
    ),
    RuleSpec(
        R_DISCARDED,
        PricingScope.PROJECT_PROVIDER_MODEL,
        A,
        A1,
        True,
        MODEL_CODE,
        _MARKUP,
        "1.5",
        None,
        "discard",
    ),
)


# --- 世界 -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Customer:
    name: str
    tenant_id: int
    public_id: str
    billing_mode: BillingMode


@dataclass(frozen=True)
class ProjectRef:
    name: str
    customer: str
    tenant_id: int
    project_id: int
    public_id: str
    credential_id: int


@dataclass(frozen=True)
class EventRef:
    id: int
    public_id: str
    tenant_id: int
    occurred_at: dt.datetime
    billable_cost: Decimal | None
    wallet_transaction_id: int | None


@dataclass(frozen=True)
class LineSpec:
    """One line of `add_rebill`: the new charge and the versions it used (`None` = keep refs)."""

    event: EventRef
    corrected_charge: str
    rule: str | None = None
    version: str | None = None
    original_statement_id: int | None = None


@dataclass(frozen=True)
class RebillRef:
    id: int
    public_id: str
    idempotency_key: str
    wallet_transaction_id: int
    # 明细的内部 id，按写入顺序。
    line_ids: tuple[int, ...]


@dataclass
class RebillWorld:
    factory: sessionmaker[Session]
    admin: User
    provider_id: int
    model_id: int
    other_model_id: int
    meter: UsageMeterType
    fx_version_id: int
    customers: dict[str, Customer]
    projects: dict[str, ProjectRef]
    # 名字 → 内部 id。
    versions: dict[str, int]
    rules: dict[str, int]
    tax_version_id: int | None = None
    _numbers: Iterator[int] = field(default_factory=lambda: itertools.count(1))

    @property
    def admin_id(self) -> int:
        return int(self.admin.id)

    def number(self) -> int:
        return next(self._numbers)

    # --- 事件 ---

    def _event_values(
        self,
        session: Session,
        *,
        project: str,
        occurred_at: dt.datetime,
        status: UsageEventStatus,
        tokens: tuple[int, int, int, int],
        rule: str,
        charge: ChargeResult | None = None,
    ) -> dict[str, Any]:
        ref = self.projects[project]
        mode = self.customers[ref.customer].billing_mode
        number = self.number()
        whole = occurred_at.replace(microsecond=0)
        values: dict[str, Any] = {
            "public_id": zero_uuid(number),
            "event_id": f"{number:026d}",
            "schema_version": "1.0",
            "tenant_id": ref.tenant_id,
            "project_id": ref.project_id,
            "integration_credential_id": ref.credential_id,
            "request_id": f"req-{number}",
            "conversation_id": None,
            "provider_code_raw": PROVIDER_CODE,
            "model_code_raw": MODEL_CODE,
            "usage_meter_type_id": self.meter.id,
            "payload_shape": self.meter.payload_shape,
            "quantity_kind": self.meter.quantity_kind,
            "unit": self.meter.unit,
            **dict(zip(_TOKEN_NAMES, tokens, strict=True)),
            "quantity": None,
            "payload_fingerprint": "0" * 64,
            "status": status,
            "feature": None,
            "occurred_at": occurred_at,
            "received_at": whole,
            "created_at": whole,
        }
        if status is not UsageEventStatus.PROCESSED:
            return values
        snapshot = ChargeSnapshot(
            usage_meter_type_id=self.meter.id,
            provider_price_version_id=self.versions[V_BASE],
            fx_rate_version_id=self.fx_version_id,
            pricing_rule_id=self.rules[rule],
        )
        if charge is None:
            charge = reprice_from_snapshot(
                session, snapshot, dict(zip(_TOKEN_NAMES, tokens, strict=True))
            )
        internal = mode is BillingMode.INTERNAL_METERED_ONLY
        values.update(
            provider_id=self.provider_id,
            model_id=self.model_id,
            provider_price_version_id=snapshot.provider_price_version_id,
            pricing_rule_id=snapshot.pricing_rule_id,
            fx_rate_version_id=snapshot.fx_rate_version_id,
            provider_source_currency=charge.provider_source_currency,
            provider_source_cost=charge.provider_source_cost,
            fx_rate_applied=charge.fx_rate_applied,
            estimated_provider_cost_myr=charge.estimated_provider_cost_myr,
            billable_cost=Decimal(0) if internal else charge.billable_cost,
            billing_mode_snapshot=mode.value,
            reference_customer_price=charge.billable_cost if internal else None,
            wallet_transaction_id=None,
            processed_at=whole,
        )
        return values

    def add_event(
        self,
        *,
        project: str,
        occurred_at: dt.datetime,
        status: UsageEventStatus = UsageEventStatus.PROCESSED,
        tokens: tuple[int, int, int, int] = DEFAULT_TOKENS,
        rule: str = R_GLOBAL,
        ledger_at: dt.datetime | None = None,
    ) -> EventRef:
        """One usage event in its final state; a billed one gets its `AI_USAGE` row first.

        账本行的 `created_at` 默认是 `occurred_at` 截到整秒（准时入账）；`ledger_at` 给晚到的。
        """
        with self.factory() as session:
            values = self._event_values(
                session,
                project=project,
                occurred_at=occurred_at,
                status=status,
                tokens=tokens,
                rule=rule,
            )
            billable = values.get("billable_cost")
            if billable is not None and billable > 0:
                posted = post_transaction(
                    session,
                    tenant_id=values["tenant_id"],
                    transaction_type=TransactionType.AI_USAGE,
                    amount=-billable,
                    reference_type=ReferenceType.USAGE_EVENT,
                    reference_id=values["event_id"],
                    now=ledger_at or occurred_at.replace(microsecond=0),
                )
                values["wallet_transaction_id"] = int(posted.transaction.id)
            row = UsageEvent(**values)
            session.add(row)
            session.commit()
            return EventRef(
                int(row.id),
                row.public_id,
                row.tenant_id,
                row.occurred_at,
                row.billable_cost,
                row.wallet_transaction_id,
            )

    def add_zero_events(self, *, project: str, moments: Sequence[dt.datetime]) -> None:
        """Many processed prepaid events with zero tokens (计费额 0、没有账本行), one INSERT."""
        with self.factory() as session:
            snapshot = ChargeSnapshot(
                self.meter.id, self.versions[V_BASE], self.fx_version_id, self.rules[R_GLOBAL]
            )
            # 数量全是 0：每个事件的计价结果相同，只算一次。
            charge = reprice_from_snapshot(
                session, snapshot, dict(zip(_TOKEN_NAMES, ZERO_TOKENS, strict=True))
            )
            rows = [
                self._event_values(
                    session,
                    project=project,
                    occurred_at=moment,
                    status=UsageEventStatus.PROCESSED,
                    tokens=ZERO_TOKENS,
                    rule=R_GLOBAL,
                    charge=charge,
                )
                for moment in moments
            ]
            session.execute(insert(UsageEvent), rows)
            session.commit()

    # --- Rebill 与对账单 ---

    def add_rebill(
        self,
        customer: str,
        lines: Sequence[LineSpec],
        *,
        created_at: dt.datetime = dt.datetime(2026, 12, 20, 0, 0, 0),
        corrective_rule: str | None = None,
        corrective_version: str | None = None,
    ) -> RebillRef:
        """One rebill written directly: its `REBILL_*` row (through `post_transaction`) and lines.

        `previous_*` 是写入时的当前有效状态（最近一条明细，没有就是事件快照）。差额不许为 0。
        """
        number = self.number()
        public_id = zero_uuid(number)
        idempotency_key = zero_uuid(self.number())
        tenant_id = self.customers[customer].tenant_id
        with self.factory() as session:
            prepared: list[dict[str, Any]] = []
            for spec in lines:
                event = session.get(UsageEvent, spec.event.id)
                assert event is not None and event.tenant_id == tenant_id
                latest = session.execute(
                    select(UsageRebillLine)
                    .where(UsageRebillLine.usage_event_id == event.id)
                    .order_by(UsageRebillLine.id.desc())
                    .limit(1)
                ).scalar_one_or_none()
                if latest is None:
                    assert event.billable_cost is not None
                    assert event.pricing_rule_id is not None
                    assert event.provider_price_version_id is not None
                    previous = event.billable_cost
                    rule_id = event.pricing_rule_id
                    version_id = event.provider_price_version_id
                else:
                    previous = latest.corrected_charge
                    rule_id = latest.corrected_pricing_rule_id
                    version_id = latest.corrected_provider_price_version_id
                corrected = Decimal(spec.corrected_charge)
                assert corrected != previous
                prepared.append(
                    {
                        "usage_event_id": event.id,
                        "original_wallet_transaction_id": event.wallet_transaction_id,
                        "previous_charge": previous,
                        "corrected_charge": corrected,
                        "difference": corrected - previous,
                        "previous_pricing_rule_id": rule_id,
                        "previous_provider_price_version_id": version_id,
                        "corrected_pricing_rule_id": (
                            self.rules[spec.rule] if spec.rule is not None else rule_id
                        ),
                        "corrected_provider_price_version_id": (
                            self.versions[spec.version] if spec.version is not None else version_id
                        ),
                        "corrected_estimated_provider_cost_myr": Decimal(0),
                        "original_period": period_containing(event.occurred_at).label,
                        "original_statement_id": spec.original_statement_id,
                        "created_at": created_at,
                    }
                )
            previous_total = sum((item["previous_charge"] for item in prepared), Decimal(0))
            corrected_total = sum((item["corrected_charge"] for item in prepared), Decimal(0))
            difference = corrected_total - previous_total
            assert difference != 0
            posted = post_transaction(
                session,
                tenant_id=tenant_id,
                transaction_type=(
                    TransactionType.REBILL_DEBIT
                    if difference > 0
                    else TransactionType.REBILL_CREDIT
                ),
                amount=-difference,
                reference_type=ReferenceType.REBILL,
                reference_id=public_id,
                now=created_at,
                created_by=self.admin_id,
                description=f"Rebill correction ({len(prepared)} usage events)",
                metadata={"rebill": public_id, "events": len(prepared)},
            )
            rebill = UsageRebill(
                public_id=public_id,
                tenant_id=tenant_id,
                idempotency_key=idempotency_key,
                request_fingerprint="0" * 64,
                corrective_pricing_rule_id=(
                    self.rules[corrective_rule] if corrective_rule is not None else None
                ),
                corrective_provider_price_version_id=(
                    self.versions[corrective_version] if corrective_version is not None else None
                ),
                event_count=len(prepared),
                previous_total=previous_total,
                corrected_total=corrected_total,
                difference=difference,
                wallet_transaction_id=posted.transaction.id,
                reason="Fictional rebill for a test",
                created_by=self.admin_id,
                created_at=created_at,
            )
            session.add(rebill)
            session.flush()
            rows = [UsageRebillLine(rebill_id=rebill.id, **item) for item in prepared]
            for row in rows:
                session.add(row)
                session.flush()
            session.commit()
            return RebillRef(
                int(rebill.id),
                public_id,
                idempotency_key,
                int(posted.transaction.id),
                tuple(int(row.id) for row in rows),
            )

    def add_statement(self, customer: str, label: str) -> int:
        """A FINALIZED statement of the customer for `YYYY-MM` (written directly, never updated)."""
        period = statement_period(label)
        with self.factory() as session:
            if self.tax_version_id is None:
                version = TaxPolicyVersion(
                    public_id=zero_uuid(self.number()),
                    jurisdiction=TaxJurisdiction.MY,
                    policy_type=TaxPolicyType.SST,
                    rate=None,
                    treatment_json={
                        "schema_version": 1,
                        "sst_registered": False,
                        "tax_charged": False,
                    },
                    status=TaxPolicyStatus.DRAFT,
                    effective_from=None,
                    effective_to=None,
                    source_reference="Fictional policy for a rebill test",
                    created_by=self.admin_id,
                    approved_by=None,
                    approved_at=None,
                    created_at=SETUP_AT,
                    updated_at=SETUP_AT,
                )
                session.add(version)
                session.flush()
                self.tax_version_id = int(version.id)
            number = self.number()
            row = MonthlyStatement(
                public_id=zero_uuid(number),
                tenant_id=self.customers[customer].tenant_id,
                statement_number=f"ST-{label.replace('-', '')}-{number:08d}",
                period_start=period.start,
                period_end=period.end,
                cutoff_at=period.cutoff,
                status=StatementStatus.FINALIZED,
                ledger_high_sequence=0,
                snapshot_json={"schema_version": 1, "period": label},
                generated_by=None,
                tax_policy_version_id=self.tax_version_id,
                finalized_at=period.cutoff,
                created_at=period.cutoff,
            )
            session.add(row)
            session.commit()
            return int(row.id)

    # --- 读 ---

    def event(self, session: Session, ref: EventRef) -> UsageEvent:
        row = session.get(UsageEvent, ref.id)
        assert row is not None
        return row

    def version(self, session: Session, name: str) -> ProviderPriceVersion:
        row = session.get(ProviderPriceVersion, self.versions[name])
        assert row is not None
        return row

    def rule(self, session: Session, name: str) -> PricingRule:
        row = session.get(PricingRule, self.rules[name])
        assert row is not None
        return row

    def public_version(self, name: str) -> str:
        with self.factory() as session:
            return self.version(session, name).public_id

    def public_rule(self, name: str) -> str:
        with self.factory() as session:
            return self.rule(session, name).public_id


# --- 建世界 ---------------------------------------------------------------------------


def _clock() -> dt.datetime:
    return SETUP_AT


def _row_id(session: Session, model: Any, public_id: str) -> int:
    return int(session.execute(select(model.id).where(model.public_id == public_id)).scalar_one())


def _tenancy(
    factory: sessionmaker[Session], numbers: Iterator[int]
) -> tuple[dict[str, Customer], dict[str, ProjectRef]]:
    customers: dict[str, Customer] = {}
    projects: dict[str, ProjectRef] = {}
    with factory() as session:
        for name, mode in _TENANT_MODES.items():
            tenant = Tenant(
                public_id=zero_uuid(next(numbers)),
                company_name=f"Fictional {name} Sdn Bhd",
                email=f"rebill-{name.lower()}{TEST_EMAIL_DOMAIN}",
                billing_mode=mode,
                created_at=SETUP_AT,
                updated_at=SETUP_AT,
            )
            session.add(tenant)
            session.flush()
            if mode is BillingMode.PREPAID:
                create_wallet(session, tenant_id=tenant.id, now=SETUP_AT)
            customers[name] = Customer(name, int(tenant.id), tenant.public_id, mode)
        for name, owner in _PROJECTS.items():
            number = next(numbers)
            tenant_id = customers[owner].tenant_id
            project = Project(
                public_id=zero_uuid(number),
                tenant_id=tenant_id,
                name=name,
                created_at=SETUP_AT,
                updated_at=SETUP_AT,
            )
            session.add(project)
            session.flush()
            credential = IntegrationCredential(
                tenant_id=tenant_id,
                project_id=project.id,
                public_api_key=f"ak_rb{number:030d}",
                key_version=1,
                encrypted_secret="not-a-real-ciphertext",
                encryption_key_version=1,
                status=CredentialStatus.ACTIVE,
                valid_from=SETUP_AT,
                created_at=SETUP_AT,
            )
            session.add(credential)
            session.flush()
            projects[name] = ProjectRef(
                name, owner, tenant_id, int(project.id), project.public_id, int(credential.id)
            )
        session.commit()
    return customers, projects


def build_world(factory: sessionmaker[Session]) -> RebillWorld:
    """The rebill world of this module's docstring."""
    numbers = itertools.count(1)
    with factory() as session:
        admin = User(
            email=f"rebill-admin{TEST_EMAIL_DOMAIN}",
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
    models = {
        code: ai_catalog.create_model(
            factory, provider_id=provider.id, code=code, display_name=f"Fictional {code}", **common
        )
        for code in (MODEL_CODE, OTHER_MODEL_CODE)
    }
    fx_draft = fx_rates.create_draft(
        factory,
        base_currency="USD",
        rate=FX_RATE,
        observed_at=SETUP_AT,
        source_reference="Fictional quote",
        **common,
    )
    fx_rates.publish(factory, fx_rate_id=fx_draft.id, effective_from=FX_FROM, **common)
    customers, projects = _tenancy(factory, numbers)

    version_ids: dict[str, str] = {}
    for spec in VERSIONS:
        draft = provider_prices.create_draft(
            factory,
            provider_id=provider.id,
            model_id=models[spec.model].id,
            source_currency=spec.currency,
            source_reference="Fictional price page",
            components=[
                ComponentInput.model_validate(
                    {"component_code": code, "unit_quantity": unit, "rate_amount": rate}
                )
                for code, (unit, rate) in spec.components.items()
            ],
            **common,
        )
        version_ids[spec.name] = draft.id
        if spec.action in ("publish", "retire"):
            provider_prices.publish(
                factory, price_version_id=draft.id, effective_from=spec.effective_from, **common
            )
        if spec.action == "retire":
            provider_prices.retire(
                factory, price_version_id=draft.id, reason="Fictional withdrawal", **common
            )
        if spec.action == "discard":
            provider_prices.discard_draft(factory, price_version_id=draft.id, **common)

    rule_ids: dict[str, str] = {}
    for rule in RULES:
        created = pricing_rules.create_draft(
            factory,
            priority_scope=rule.scope,
            customer_id=customers[rule.customer].public_id if rule.customer else None,
            project_id=projects[rule.project].public_id if rule.project else None,
            provider_id=provider.id if rule.with_provider else None,
            model_id=models[rule.model].id if rule.model else None,
            strategy=rule.strategy,
            markup_multiplier=Decimal(rule.multiplier) if rule.multiplier else None,
            components=(
                [
                    RuleComponentInput.model_validate(
                        {"component_code": code, "unit_quantity": unit, "rate_amount": rate}
                    )
                    for code, (unit, rate) in rule.components.items()
                ]
                if rule.components is not None
                else None
            ),
            **common,
        )
        rule_ids[rule.name] = created.id
        if rule.action in ("publish", "retire"):
            pricing_rules.publish(
                factory, rule_id=created.id, effective_from=rule.effective_from, **common
            )
        if rule.action == "retire":
            pricing_rules.retire(
                factory, rule_id=created.id, reason="Fictional withdrawal", **common
            )
        if rule.action == "discard":
            pricing_rules.discard_draft(factory, rule_id=created.id, **common)

    with factory() as session:
        meter = session.execute(
            select(UsageMeterType).where(UsageMeterType.code == METER_CODE)
        ).scalar_one()
        session.expunge(meter)
        return RebillWorld(
            factory=factory,
            admin=admin,
            provider_id=_row_id(session, AiProvider, provider.id),
            model_id=_row_id(session, AiModel, models[MODEL_CODE].id),
            other_model_id=_row_id(session, AiModel, models[OTHER_MODEL_CODE].id),
            meter=meter,
            fx_version_id=_row_id(session, FxRateVersion, fx_draft.id),
            customers=customers,
            projects=projects,
            versions={
                name: _row_id(session, ProviderPriceVersion, public_id)
                for name, public_id in version_ids.items()
            },
            rules={
                name: _row_id(session, PricingRule, public_id)
                for name, public_id in rule_ids.items()
            },
            _numbers=numbers,
        )


def count_rows(factory: sessionmaker[Session], model: Any) -> int:
    with factory() as session:
        return int(session.execute(select(func.count()).select_from(model)).scalar_one())


# --- 库 -------------------------------------------------------------------------------


def clean(engine: Engine) -> None:
    """MySQL only. ⚠️ 库是共享的，每个用例前后都清场。

    两张 Rebill 表、对账单与税务政策版本拒绝 DELETE（触发器），关掉外键检查后 TRUNCATE；
    账本与钱包交给 `clean_wallets`，其余交给 provider_cost_world 的 `clean`（同一个邮箱域）。
    """
    with engine.begin() as connection:
        connection.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        try:
            for name in (
                "usage_rebill_lines",
                "usage_rebills",
                "monthly_statements",
                "tax_policy_versions",
            ):
                connection.execute(text(f"TRUNCATE TABLE {name}"))
        finally:
            connection.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
    clean_wallets(engine)
    clean_provider_cost(engine)


# SQLite 上代替迁移 0006 的 AFTER INSERT 触发器（同 test_admin_usage_events_api.py）：
# 账本行推进钱包，同一客户的第二行账本才拿得到下一个序号。
_SQLITE_LEDGER_TRIGGER: Final = """
CREATE TRIGGER test_wallet_follows_ledger
AFTER INSERT ON wallet_transactions
FOR EACH ROW
BEGIN
    UPDATE wallets
       SET balance = NEW.balance_after, version = NEW.wallet_sequence,
           updated_at = NEW.created_at
     WHERE id = NEW.wallet_id;
END
"""


def sqlite_factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql(_SQLITE_LEDGER_TRIGGER)
    factory = create_session_factory(engine)
    seed_meter_types(factory)
    try:
        yield factory
    finally:
        engine.dispose()


def mysql_factory(monkeypatch: pytest.MonkeyPatch) -> Iterator[sessionmaker[Session]]:
    url = mysql_url()
    # ⚠️ 建表必须走 alembic：版本、账本与 Rebill 表的触发器只有迁移会建。
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


# --- 夹具（conftest.py 注册） -----------------------------------------------------------


@pytest.fixture(params=["sqlite", "mysql"])
def rebill_db(request, monkeypatch) -> Iterator[sessionmaker[Session]]:
    """The empty database (meter types seeded) on SQLite and on a real MySQL."""
    if request.param == "mysql":
        yield from mysql_factory(monkeypatch)
        return
    yield from sqlite_factory()


@pytest.fixture
def rebill_world(rebill_db) -> RebillWorld:
    """The rebill world on SQLite and on a real MySQL."""
    return build_world(rebill_db)
