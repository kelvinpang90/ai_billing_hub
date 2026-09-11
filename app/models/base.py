"""Declarative base and the monetary column convention (Invariant 10, spec §80).

**金额一律 `DECIMAL(20,8)`，永远不用 `FLOAT` / `DOUBLE`。**这不是风格问题：
二进制浮点存不下十进制小数，钱包余额会在加减之间漂移，而账本是不可变的——
错了改不回来，只能再记一笔补偿交易。

用 `Money` 这个注解类型声明列，不要每张表各写一遍 `Numeric(...)`：写散了就会
有人写成 `Numeric(10, 2)`，而那时候没有任何东西会报错。
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated

from sqlalchemy import Numeric
from sqlalchemy.orm import DeclarativeBase, mapped_column

# spec §80：持久化精度 DECIMAL(20,8)，钱包同精度。
MONEY_PRECISION = 20
MONEY_SCALE = 8

# `asdecimal=True` 是默认值，这里写出来是为了挡住「顺手改成 float 更快」的念头。
Money = Annotated[Decimal, mapped_column(Numeric(MONEY_PRECISION, MONEY_SCALE, asdecimal=True))]

_MONEY_QUANTUM = Decimal(1).scaleb(-MONEY_SCALE)


class Base(DeclarativeBase):
    """All ORM models inherit from here so Alembic can autogenerate against them."""


def quantize_money(value: Decimal) -> Decimal:
    """Round to persisted precision with ROUND_HALF_UP (spec §80).

    §80 要求的是「先把一个用量事件的所有价格分量加总，**再做恰好一次**
    `ROUND_HALF_UP` 到 8 位小数」。逐个分量早舍入会累积误差。

    这个函数只负责「那一次」舍入，放在这里是为了**只有一份实现**——四舍五入
    规则散在各处，迟早会有一处写成默认的 `ROUND_HALF_EVEN`，而两者在 .5 上
    结果不同，对账时才会发现。
    """
    return value.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP)
