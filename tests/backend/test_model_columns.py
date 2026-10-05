"""Column-shape guards that SQLite cannot enforce for us.

⚠️ **这个文件存在的理由是一次真事故。**T0.8b 往 `AuditAction` 里加了
`RECOVERY_CODES_REGENERATED`（26 字符），而 `action` 列是 0002 按当时最长的
`LOGIN_FAILED`（12 字符）建的。结果：

- 208 条单元测试**全绿** —— SQLite 不强制 VARCHAR 长度
- 真 MySQL 在插入时报 `Data too long for column 'action'`

也就是说，**跑在 SQLite 上的用例对这一类缺陷是结构性失明的**。所以这里不测
行为，直接测**列的形状**：宽度够不够装下所有枚举值。
"""

from __future__ import annotations

import enum

import pytest
from sqlalchemy import Enum as SAEnum
from sqlalchemy.types import Numeric

from app.models.ai_catalog import (
    AiModel,
    AiProvider,
    CatalogStatus,
    PayloadShape,
    QuantityKind,
    UsageMeterComponent,
    UsageMeterType,
)
from app.models.auth import (
    _ENUM_LENGTH,
    AuditAction,
    AuditLog,
    DomainOutbox,
    OutboxStatus,
    User,
    UserRole,
    UserStatus,
)
from app.models.base import MONEY_PRECISION, MONEY_SCALE
from app.models.fx_rates import (
    RATE_PRECISION,
    RATE_SCALE,
    FxFetchAttempt,
    FxFetchOutcome,
    FxRateSourceType,
    FxRateStatus,
    FxRateVersion,
)
from app.models.pricing_rules import (
    PricingRule,
    PricingRuleComponent,
    PricingRuleStatus,
    PricingScope,
    PricingStrategy,
)
from app.models.provider_prices import (
    PriceSourceType,
    PriceVersionStatus,
    ProviderPriceComponent,
    ProviderPriceVersion,
)
from app.models.tenancy import BillingStatus, Tenant
from app.models.usage import (
    QUANTITY_PRECISION,
    QUANTITY_SCALE,
    ConflictMismatch,
    UsageEvent,
    UsageEventConflict,
    UsageEventStatus,
)
from app.models.wallet import ReferenceType, TransactionType, Wallet, WalletTransaction

# AI 目录（AIH-TASK-025）的四张表各有枚举列：状态、上报形态、数量类型。
_CATALOG_MODELS = (UsageMeterType, UsageMeterComponent, AiProvider, AiModel)
# 供应商价格版本（AIH-TASK-026）的两个枚举列：状态、来源类型。
_PRICE_MODELS = (ProviderPriceVersion,)
# 定价规则（AIH-TASK-027）的枚举列：规则的范围、策略、状态，分量的策略。
_RULE_MODELS = (PricingRule, PricingRuleComponent)
# FX 汇率（AIH-TASK-028）的枚举列：版本的来源、状态，拉取记录的来源、结果。
_FX_MODELS = (FxRateVersion, FxFetchAttempt)
# 用量事件（AIH-TASK-029）的枚举列：事件的形态、数量类型、状态，冲突行的不符类型。
_USAGE_MODELS = (UsageEvent, UsageEventConflict)


def enum_columns():
    """Every VARCHAR-backed enum column in the auth, tenant, ledger, catalog, price, pricing
    rule, FX and usage event tables."""
    tables = (User, AuditLog, DomainOutbox, Tenant, WalletTransaction)
    models = (*_CATALOG_MODELS, *_PRICE_MODELS, *_RULE_MODELS, *_FX_MODELS, *_USAGE_MODELS)
    for model in (*tables, *models):
        for column in model.__table__.columns:
            if isinstance(column.type, SAEnum):
                yield f"{model.__tablename__}.{column.name}", column


_ENUM_COLUMNS = list(enum_columns())


def test_there_are_enum_columns_to_check() -> None:
    """⚠️ 没有这一条，上面那个收集函数一旦失效，下面的参数化用例会在空集合上
    「全部通过」—— 测试全绿而校验什么也没做。"""
    assert len(_ENUM_COLUMNS) >= 3


def test_the_catalog_enum_columns_are_collected() -> None:
    """设计 §2 写死的列宽：状态 16、上报形态 32、数量类型 16（与迁移 0012 一致）。"""
    tables = {model.__tablename__ for model in _CATALOG_MODELS}
    widths = {
        label: column.type.length
        for label, column in _ENUM_COLUMNS
        if label.split(".")[0] in tables
    }

    assert widths == {
        "usage_meter_types.payload_shape": 32,
        "usage_meter_types.quantity_kind": 16,
        "usage_meter_types.status": 16,
        "usage_meter_components.payload_shape": 32,
        "ai_providers.status": 16,
        "ai_models.status": 16,
    }


def test_the_price_enum_columns_are_collected() -> None:
    """设计 §2 写死的列宽：状态 16、来源类型 16（与迁移 0013 一致）。"""
    tables = {model.__tablename__ for model in _PRICE_MODELS}
    widths = {
        label: column.type.length
        for label, column in _ENUM_COLUMNS
        if label.split(".")[0] in tables
    }

    assert widths == {
        "provider_price_versions.source_type": 16,
        "provider_price_versions.status": 16,
    }


def test_the_pricing_rule_enum_columns_are_collected() -> None:
    """设计 §2 写死的列宽：范围 32、策略 16、状态 16（与迁移 0014 一致）。"""
    tables = {model.__tablename__ for model in _RULE_MODELS}
    widths = {
        label: column.type.length
        for label, column in _ENUM_COLUMNS
        if label.split(".")[0] in tables
    }

    assert widths == {
        "pricing_rules.priority_scope": 32,
        "pricing_rules.strategy": 16,
        "pricing_rules.status": 16,
        "pricing_rule_components.strategy": 16,
    }


def test_the_fx_enum_columns_are_collected() -> None:
    """设计 §2 写死的列宽：来源 16、状态 16、结果 32（v3：`NO_QUOTE_FOR_DATE` 有 17 个字符，
    照抄 16 放不下），与迁移 0015 一致。"""
    tables = {model.__tablename__ for model in _FX_MODELS}
    widths = {
        label: column.type.length
        for label, column in _ENUM_COLUMNS
        if label.split(".")[0] in tables
    }

    assert widths == {
        "fx_rate_versions.source": 16,
        "fx_rate_versions.status": 16,
        "fx_fetch_attempts.source": 16,
        "fx_fetch_attempts.outcome": 32,
    }


def test_the_usage_event_enum_columns_are_collected() -> None:
    """设计闸门 #176 v8 §2 写死的列宽：形态 32、数量类型 16（与 `usage_meter_types` 的同名列
    一致，复合外键要求）、状态 32、不符类型 32，与迁移 0017 一致。"""
    tables = {model.__tablename__ for model in _USAGE_MODELS}
    widths = {
        label: column.type.length
        for label, column in _ENUM_COLUMNS
        if label.split(".")[0] in tables
    }

    assert widths == {
        "usage_events.payload_shape": 32,
        "usage_events.quantity_kind": 16,
        "usage_events.status": 32,
        "usage_event_conflicts.mismatch": 32,
    }


def test_the_usage_quantity_is_decimal_20_8() -> None:
    """`quantity` 是用量不是金额，但同样是 DECIMAL(20,8)、读出来是 Decimal（设计 §2）。"""
    column = UsageEvent.__table__.c.quantity

    assert isinstance(column.type, Numeric)
    assert (column.type.precision, column.type.scale) == (QUANTITY_PRECISION, QUANTITY_SCALE)
    assert (QUANTITY_PRECISION, QUANTITY_SCALE) == (20, 8)
    assert column.type.asdecimal is True


@pytest.mark.parametrize(
    "column",
    [FxRateVersion.__table__.c.rate, UsageEvent.__table__.c.fx_rate_applied],
    ids=["fx_rate_versions.rate", "usage_events.fx_rate_applied"],
)
def test_the_fx_rate_is_decimal_24_10(column) -> None:
    """INV-10：汇率是 DECIMAL(24,10)（设计 §2，不是金额的 20,8），读出来是 Decimal。用量事件
    快照里的 `fx_rate_applied` 与它同精度（AIH-TASK-032）：原值写入，不舍入。"""
    assert isinstance(column.type, Numeric)
    assert (column.type.precision, column.type.scale) == (RATE_PRECISION, RATE_SCALE) == (24, 10)
    assert column.type.asdecimal is True


@pytest.mark.parametrize("label,column", _ENUM_COLUMNS, ids=[label for label, _ in _ENUM_COLUMNS])
def test_every_enum_value_fits_its_column(label: str, column) -> None:
    """⚠️ 宽度必须装得下**所有**成员，包括以后加的。

    不够宽的话，真 MySQL 在插入那一刻才报错 —— 而且只在用到那个新值的路径上
    报，很可能是上线之后。
    """
    longest = max(len(member.value) for member in column.type.enum_class)
    assert column.type.length is not None, f"{label}: 枚举列必须写死宽度，不能让它自己推"
    assert column.type.length >= longest, (
        f"{label}: 列宽 {column.type.length} 装不下最长的成员（{longest} 字符）"
    )


@pytest.mark.parametrize(
    "enum_class",
    [
        AuditAction,
        UserRole,
        UserStatus,
        OutboxStatus,
        BillingStatus,
        TransactionType,
        ReferenceType,
        CatalogStatus,
        PayloadShape,
        QuantityKind,
        PriceVersionStatus,
        PriceSourceType,
        PricingScope,
        PricingStrategy,
        PricingRuleStatus,
        FxRateStatus,
        FxRateSourceType,
        FxFetchOutcome,
        UsageEventStatus,
        ConflictMismatch,
    ],
    ids=lambda cls: cls.__name__,
)
def test_enum_values_stay_within_the_pinned_width(enum_class: type[enum.StrEnum]) -> None:
    """反向的那一半：加枚举值时，这条会先于数据库告诉你超了。"""
    too_long = [member.value for member in enum_class if len(member.value) > _ENUM_LENGTH]
    assert too_long == [], f"这些值超过了 _ENUM_LENGTH={_ENUM_LENGTH}：{too_long}"


_MONEY_COLUMNS = [
    Wallet.__table__.c.balance,
    WalletTransaction.__table__.c.amount,
    WalletTransaction.__table__.c.balance_before,
    WalletTransaction.__table__.c.balance_after,
    Tenant.__table__.c.low_balance_threshold,
    # 供应商价格分量（AIH-TASK-026）：原币种单价与「多少个单位」，同一精度、都不经过 float。
    ProviderPriceComponent.__table__.c.unit_quantity,
    ProviderPriceComponent.__table__.c.rate_amount,
    # 定价规则（AIH-TASK-027）：MYR 含税单价、「多少个单位」与 MARKUP 的倍数。
    PricingRule.__table__.c.markup_multiplier,
    PricingRuleComponent.__table__.c.unit_quantity,
    PricingRuleComponent.__table__.c.rate_amount,
    # 用量事件的计费快照（AIH-TASK-032）：原币成本、MYR 估算成本与计费额。
    UsageEvent.__table__.c.provider_source_cost,
    UsageEvent.__table__.c.estimated_provider_cost_myr,
    UsageEvent.__table__.c.billable_cost,
]


@pytest.mark.parametrize(
    "column",
    _MONEY_COLUMNS,
    ids=[f"{column.table.name}.{column.name}" for column in _MONEY_COLUMNS],
)
def test_every_amount_column_is_decimal_20_8(column) -> None:
    """INV-10：金额列一律 DECIMAL(20,8) 且读出来是 Decimal，没有一列写散成别的精度。"""
    assert isinstance(column.type, Numeric)
    assert (column.type.precision, column.type.scale) == (MONEY_PRECISION, MONEY_SCALE)
    assert column.type.asdecimal is True
