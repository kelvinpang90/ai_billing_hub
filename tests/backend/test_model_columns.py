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
from app.models.provider_prices import (
    PriceSourceType,
    PriceVersionStatus,
    ProviderPriceComponent,
    ProviderPriceVersion,
)
from app.models.tenancy import BillingStatus, Tenant
from app.models.wallet import ReferenceType, TransactionType, Wallet, WalletTransaction

# AI 目录（AIH-TASK-025）的四张表各有枚举列：状态、上报形态、数量类型。
_CATALOG_MODELS = (UsageMeterType, UsageMeterComponent, AiProvider, AiModel)
# 供应商价格版本（AIH-TASK-026）的两个枚举列：状态、来源类型。
_PRICE_MODELS = (ProviderPriceVersion,)


def enum_columns():
    """Every VARCHAR-backed enum column in the auth, tenant, ledger, catalog and price tables."""
    tables = (User, AuditLog, DomainOutbox, Tenant, WalletTransaction)
    for model in (*tables, *_CATALOG_MODELS, *_PRICE_MODELS):
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
