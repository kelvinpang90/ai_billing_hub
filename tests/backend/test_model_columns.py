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


def enum_columns():
    """Every VARCHAR-backed enum column in the auth tables."""
    for model in (User, AuditLog, DomainOutbox):
        for column in model.__table__.columns:
            if isinstance(column.type, SAEnum):
                yield f"{model.__tablename__}.{column.name}", column


_ENUM_COLUMNS = list(enum_columns())


def test_there_are_enum_columns_to_check() -> None:
    """⚠️ 没有这一条，上面那个收集函数一旦失效，下面的参数化用例会在空集合上
    「全部通过」—— 测试全绿而校验什么也没做。"""
    assert len(_ENUM_COLUMNS) >= 3


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
    [AuditAction, UserRole, UserStatus, OutboxStatus],
    ids=lambda cls: cls.__name__,
)
def test_enum_values_stay_within_the_pinned_width(enum_class: type[enum.StrEnum]) -> None:
    """反向的那一半：加枚举值时，这条会先于数据库告诉你超了。"""
    too_long = [member.value for member in enum_class if len(member.value) > _ENUM_LENGTH]
    assert too_long == [], f"这些值超过了 _ENUM_LENGTH={_ENUM_LENGTH}：{too_long}"
