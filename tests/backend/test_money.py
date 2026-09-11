"""The monetary column convention and rounding rule (Invariant 10, spec §80)."""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal

import pytest
from sqlalchemy import Numeric

from app.models.base import MONEY_PRECISION, MONEY_SCALE, Money, quantize_money


def money_column_type() -> Numeric:
    # Money 是 Annotated[Decimal, mapped_column(...)]，第二项就是那个列定义。
    return Money.__metadata__[0].column.type


def test_money_columns_are_decimal_20_8() -> None:
    """§80 写死的是 DECIMAL(20,8)。写散了就会有人写成 Numeric(10, 2)。"""
    column_type = money_column_type()

    assert isinstance(column_type, Numeric)
    assert (column_type.precision, column_type.scale) == (MONEY_PRECISION, MONEY_SCALE)
    assert (MONEY_PRECISION, MONEY_SCALE) == (20, 8)


def test_money_columns_never_become_float() -> None:
    """Invariant 10：二进制浮点存不下十进制小数，而账本是不可变的。"""
    assert money_column_type().asdecimal is True
    assert not isinstance(money_column_type().python_type(), float)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1.000000005", "1.00000001"),  # 正好 .5 —— 必须进位，不是「就近取偶」
        ("1.000000015", "1.00000002"),  # ROUND_HALF_EVEN 在这里会得 1.00000002 之外的结果
        ("2.000000005", "2.00000001"),
        ("-1.000000005", "-1.00000001"),  # 负数同样是「远离零」
        ("1.1", "1.10000000"),
        ("0", "0.00000000"),
    ],
)
def test_quantize_money_rounds_half_up(raw: str, expected: str) -> None:
    assert quantize_money(Decimal(raw)) == Decimal(expected)
    assert quantize_money(Decimal(raw)).as_tuple().exponent == -MONEY_SCALE


def test_half_up_differs_from_the_python_default() -> None:
    """Decimal 的默认是 ROUND_HALF_EVEN，两者在 .5 上结果不同。

    这正是为什么舍入规则只能有一份实现：散在各处的话，迟早有一处漏写
    `rounding=`，而差异只在 .5 那一类值上出现，对账时才发现。
    """
    value = Decimal("2.000000005")
    default_rounded = value.quantize(Decimal(1).scaleb(-MONEY_SCALE), rounding=ROUND_HALF_EVEN)

    assert quantize_money(value) == Decimal("2.00000001")
    assert default_rounded == Decimal("2.00000000")
    assert quantize_money(value) != default_rounded
