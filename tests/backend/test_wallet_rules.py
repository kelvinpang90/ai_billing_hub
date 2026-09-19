"""Wallet ledger rules that need no database (design gate #88 v6 §7).

⚠️ 这里**只测纯函数**：金额校验、类型↔符号↔来源、原因与操作者、metadata 禁用键、
计费状态跃迁、低余额跨越、核对规则。凡是调用 `post_transaction` / `create_wallet` /
`verify_wallet` 的场景都在 `test_wallet_repository.py`，对着真 MySQL 跑 —— 钱包由
触发器推进，SQLite 上没有那些触发器。
"""

from __future__ import annotations

import inspect
from decimal import Decimal

import pytest

from app.models.tenancy import BillingStatus
from app.models.wallet import (
    ADJUSTMENT_REFERENCE_TYPES,
    CREDIT_TYPES,
    DEBIT_TYPES,
    NONZERO_TYPES,
    REFERENCE_TYPE_FOR,
    ReferenceType,
    TransactionType,
)
from app.repositories import wallet as wallet_repository
from app.repositories.wallet import (
    BALANCE_NOT_LAST_BALANCE_AFTER,
    BALANCE_NOT_LEDGER_SUM,
    BILLING_STATUS_MISMATCH,
    CHAIN_BROKEN,
    LEDGER_MISSING,
    ROW_ARITHMETIC_BROKEN,
    SEQUENCE_GAP,
    VERSION_NOT_LAST_SEQUENCE,
    InvalidAmount,
    InvalidTransaction,
    LedgerConflict,
    LedgerError,
    LedgerLine,
    WalletNotFound,
    billing_status_for,
    billing_transition,
    check_metadata,
    check_reason_and_actor,
    check_reference_id,
    check_transaction_shape,
    coerce_reference_type,
    coerce_transaction_type,
    crosses_low_balance,
    ledger_problems,
    validate_amount,
)

# --- 金额（INV-10） -------------------------------------------------------------


@pytest.mark.parametrize(
    "amount",
    [
        Decimal("0.00000001"),
        Decimal("-0.00000001"),
        Decimal("100"),
        Decimal("-49.87654322"),
        # 存进 DECIMAL(20,8) 不改变值：可以。判据是值，不是写法。
        Decimal("1.500000000"),
        Decimal("999999999999.99999999"),
        Decimal("-999999999999.99999999"),
    ],
    ids=str,
)
def test_postable_amounts_come_back_unchanged(amount: Decimal) -> None:
    assert validate_amount(amount) is amount


@pytest.mark.parametrize(
    "amount",
    [
        # 第 9 位小数：要舍入才存得下。⚠️ 账本层不舍入（spec §80 只舍入一次）。
        Decimal("0.000000001"),
        Decimal("0.123456789"),
        Decimal("0"),
        Decimal("-0"),
        Decimal("0E-8"),
        Decimal("NaN"),
        Decimal("sNaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
        # 超出 DECIMAL(20,8)：整数部分最多 12 位。
        Decimal("1000000000000"),
        Decimal("-1000000000000"),
        0.1,
        1,
        True,
        "1.00",
        None,
    ],
    ids=repr,
)
def test_everything_else_is_refused(amount: object) -> None:
    with pytest.raises(InvalidAmount):
        validate_amount(amount)


def test_a_float_is_refused_even_when_it_looks_exact() -> None:
    """`0.5` 在二进制里是精确的 —— 拒绝它不是因为这一个值，是因为 float 这条路。"""
    with pytest.raises(InvalidAmount):
        validate_amount(0.5)


# --- 类型 ↔ 符号 ↔ 来源 ----------------------------------------------------------


def test_every_type_has_exactly_one_sign_rule_and_one_source() -> None:
    """三组符号规则互不相交且覆盖全部九种；每种类型恰好对应一个来源类型。

    ⚠️ 数据库 CHECK 是由这几组映射生成的，漏掉一种类型，CHECK 会拒绝它的每一行。
    """
    groups = [CREDIT_TYPES, DEBIT_TYPES, NONZERO_TYPES]

    assert sum(len(group) for group in groups) == len(TransactionType)
    assert frozenset().union(*groups) == frozenset(TransactionType)
    assert set(REFERENCE_TYPE_FOR) == set(TransactionType)
    assert set(REFERENCE_TYPE_FOR.values()) == set(ReferenceType)


def test_the_mapping_matches_the_design() -> None:
    """设计 §2 的类型↔来源表，逐条钉住。"""
    assert REFERENCE_TYPE_FOR[TransactionType.AI_USAGE] is ReferenceType.USAGE_EVENT
    assert REFERENCE_TYPE_FOR[TransactionType.TOPUP] is ReferenceType.PAYMENT
    for kind in (
        TransactionType.ADJUSTMENT_CREDIT,
        TransactionType.ADJUSTMENT_DEBIT,
        TransactionType.BONUS,
        TransactionType.REFUND_ADJUSTMENT,
    ):
        assert REFERENCE_TYPE_FOR[kind] is ReferenceType.ADMIN_ADJUSTMENT
    assert REFERENCE_TYPE_FOR[TransactionType.REBILL_CREDIT] is ReferenceType.REBILL
    assert REFERENCE_TYPE_FOR[TransactionType.REBILL_DEBIT] is ReferenceType.REBILL
    assert REFERENCE_TYPE_FOR[TransactionType.SYSTEM_CORRECTION] is ReferenceType.SYSTEM
    # 系统外人工退款之后的扣回：借方（spec §8）。
    assert TransactionType.REFUND_ADJUSTMENT in DEBIT_TYPES


@pytest.mark.parametrize(
    ("kind", "amount"),
    [
        (TransactionType.TOPUP, "100"),
        (TransactionType.AI_USAGE, "-0.12345678"),
        (TransactionType.ADJUSTMENT_CREDIT, "5"),
        (TransactionType.ADJUSTMENT_DEBIT, "-5"),
        (TransactionType.REBILL_CREDIT, "1"),
        (TransactionType.REBILL_DEBIT, "-1"),
        (TransactionType.REFUND_ADJUSTMENT, "-20"),
        (TransactionType.BONUS, "10"),
        (TransactionType.SYSTEM_CORRECTION, "3"),
        (TransactionType.SYSTEM_CORRECTION, "-3"),
    ],
    ids=lambda value: str(value),
)
def test_well_formed_rows_pass(kind: TransactionType, amount: str) -> None:
    check_transaction_shape(kind, Decimal(amount), REFERENCE_TYPE_FOR[kind])


@pytest.mark.parametrize(
    ("kind", "amount", "source"),
    [
        (TransactionType.TOPUP, "-10", ReferenceType.PAYMENT),
        (TransactionType.AI_USAGE, "10", ReferenceType.USAGE_EVENT),
        (TransactionType.AI_USAGE, "-10", ReferenceType.PAYMENT),
        (TransactionType.REFUND_ADJUSTMENT, "10", ReferenceType.ADMIN_ADJUSTMENT),
        (TransactionType.BONUS, "-10", ReferenceType.ADMIN_ADJUSTMENT),
        (TransactionType.TOPUP, "10", ReferenceType.ADMIN_ADJUSTMENT),
        (TransactionType.SYSTEM_CORRECTION, "10", ReferenceType.ADMIN_ADJUSTMENT),
        (TransactionType.ADJUSTMENT_CREDIT, "10", ReferenceType.SYSTEM),
    ],
    ids=lambda value: str(value),
)
def test_mismatched_type_sign_or_source_is_refused(
    kind: TransactionType, amount: str, source: ReferenceType
) -> None:
    with pytest.raises(InvalidTransaction):
        check_transaction_shape(kind, Decimal(amount), source)


def test_unknown_type_and_source_names_are_refused() -> None:
    assert coerce_transaction_type("TOPUP") is TransactionType.TOPUP
    assert coerce_reference_type("PAYMENT") is ReferenceType.PAYMENT
    with pytest.raises(InvalidTransaction):
        coerce_transaction_type("REFUND")
    with pytest.raises(InvalidTransaction):
        coerce_reference_type("GIFT")


@pytest.mark.parametrize("reference_id", ["", "   ", "x" * 65, None, 42], ids=repr)
def test_a_reference_id_must_be_a_short_non_blank_string(reference_id: object) -> None:
    with pytest.raises(InvalidTransaction):
        check_reference_id(reference_id)


def test_a_reference_id_of_64_characters_fits() -> None:
    check_reference_id("x" * 64)


# --- 原因与操作者（spec §8、§60） ------------------------------------------------


def test_adjustments_need_a_reason_and_an_actor() -> None:
    admin = ReferenceType.ADMIN_ADJUSTMENT
    check_reason_and_actor(admin, "Refund paid by bank transfer", 7)

    with pytest.raises(InvalidTransaction):
        check_reason_and_actor(admin, None, 7)
    with pytest.raises(InvalidTransaction):
        check_reason_and_actor(admin, " \t\n ", 7)
    with pytest.raises(InvalidTransaction):
        check_reason_and_actor(admin, "Refund paid by bank transfer", None)


def test_a_system_correction_needs_a_reason_but_no_user() -> None:
    check_reason_and_actor(ReferenceType.SYSTEM, "Reconciliation fix", None)

    with pytest.raises(InvalidTransaction):
        check_reason_and_actor(ReferenceType.SYSTEM, None, None)
    with pytest.raises(InvalidTransaction):
        check_reason_and_actor(ReferenceType.SYSTEM, "   ", None)


@pytest.mark.parametrize(
    "source",
    [ReferenceType.USAGE_EVENT, ReferenceType.PAYMENT, ReferenceType.REBILL],
    ids=str,
)
def test_other_sources_need_neither(source: ReferenceType) -> None:
    assert source not in ADJUSTMENT_REFERENCE_TYPES
    check_reason_and_actor(source, None, None)


def test_reason_and_actor_role_must_fit_their_columns() -> None:
    with pytest.raises(InvalidTransaction):
        check_reason_and_actor(ReferenceType.SYSTEM, "x" * 256, None)
    with pytest.raises(InvalidTransaction):
        check_reason_and_actor(ReferenceType.SYSTEM, "fix", None, "R" * 33)
    check_reason_and_actor(ReferenceType.SYSTEM, "x" * 255, None, "R" * 32)


# --- metadata（INV-7、INV-9） ----------------------------------------------------


@pytest.mark.parametrize(
    "metadata",
    [
        {"cost": "1.00"},
        {"provider_cost": "1.00"},
        {"margin": "0.30"},
        {"estimated_provider_cost_myr": "1.00"},
        {"prompt": "hello"},
        {"response": "hi"},
        {"messages": []},
        {"content": "hello"},
        # 不分大小写、任意嵌套层级都要挡住：塞进子对象不等于没塞。
        {"Prompt": "hello"},
        {"usage": {"model": "x", "cost": "1.00"}},
        {"events": [{"ok": 1}, {"response": "hi"}]},
    ],
    ids=repr,
)
def test_cost_margin_and_conversation_keys_are_refused(metadata: dict[str, object]) -> None:
    with pytest.raises(InvalidTransaction):
        check_metadata(metadata)


def test_ordinary_billing_context_is_accepted() -> None:
    check_metadata(None)
    check_metadata({})
    check_metadata({"model": "claude-sonnet-5", "input_tokens": 1200, "project": "abc"})


@pytest.mark.parametrize(
    "metadata",
    [["not", "an", "object"], "text", {"amount": Decimal("1")}, {"x": float("nan")}],
    ids=repr,
)
def test_metadata_must_be_a_json_object(metadata: object) -> None:
    """存不进 JSON 列的值要在写入之前被发现，而不是在 flush 时炸。"""
    with pytest.raises(InvalidTransaction):
        check_metadata(metadata)


# --- 计费状态跃迁（spec §7 第 8–11 条） -------------------------------------------


@pytest.mark.parametrize(
    ("current", "balance", "expected"),
    [
        (BillingStatus.ACTIVE, "0.00000001", None),
        (BillingStatus.ACTIVE, "0", BillingStatus.SUSPENDED),
        (BillingStatus.ACTIVE, "-5", BillingStatus.SUSPENDED),
        # 正好为 0 仍是暂停（spec §7 第 10 条）。
        (BillingStatus.SUSPENDED, "0", None),
        (BillingStatus.SUSPENDED, "-2", None),
        (BillingStatus.SUSPENDED, "0.00000001", BillingStatus.ACTIVE),
    ],
    ids=lambda value: str(value),
)
def test_the_transition_table(
    current: BillingStatus, balance: str, expected: BillingStatus | None
) -> None:
    assert billing_transition(current, Decimal(balance)) == expected


def test_status_follows_the_sign_of_the_balance() -> None:
    assert billing_status_for(Decimal("0.00000001")) is BillingStatus.ACTIVE
    assert billing_status_for(Decimal("0")) is BillingStatus.SUSPENDED
    assert billing_status_for(Decimal("-0.00000001")) is BillingStatus.SUSPENDED


def test_low_balance_fires_only_when_crossing_down() -> None:
    """设计 §7：阈值 20，50 → 25 → 15 → 10 → 30 → 18，恰好在 15 与 18 两笔发。"""
    threshold = Decimal("20")
    balances = [Decimal(b) for b in ("50", "25", "15", "10", "30", "18")]

    fired = [
        after
        for before, after in zip(balances, balances[1:], strict=False)
        if crosses_low_balance(threshold, before, after)
    ]

    assert fired == [Decimal("15"), Decimal("18")]


def test_landing_exactly_on_the_threshold_counts_as_crossing() -> None:
    assert crosses_low_balance(Decimal("20"), Decimal("25"), Decimal("20"))
    assert not crosses_low_balance(Decimal("20"), Decimal("20"), Decimal("10"))


def test_no_threshold_means_no_low_balance_event() -> None:
    assert not crosses_low_balance(None, Decimal("50"), Decimal("-50"))


# --- 核对规则（篡改检测） ----------------------------------------------------------
#
# ⚠️ 在 MySQL 上触发器会拒绝这些篡改，造不出来；所以核对规则本身在这里用构造的
# 数据测，`verify_wallet` 读库的那一半在 test_wallet_repository.py。


def _clean_chain() -> list[LedgerLine]:
    amounts = [Decimal("100"), Decimal("-0.12345678"), Decimal("-50")]
    lines = []
    balance = Decimal("0")
    for sequence, amount in enumerate(amounts, start=1):
        lines.append(LedgerLine(sequence, amount, balance, balance + amount))
        balance += amount
    return lines


def _problems(lines: list[LedgerLine], **overrides: object) -> list[str]:
    arguments: dict[str, object] = {
        "balance": Decimal("49.87654322"),
        "version": 3,
        "billing_status": BillingStatus.ACTIVE,
        "lines": lines,
    }
    arguments.update(overrides)
    return ledger_problems(**arguments)


def test_a_clean_chain_has_no_problems() -> None:
    assert _problems(_clean_chain()) == []


def test_an_empty_wallet_with_no_ledger_is_consistent() -> None:
    problems = ledger_problems(
        balance=Decimal("0"),
        version=0,
        billing_status=BillingStatus.SUSPENDED,
        lines=[],
    )
    assert problems == []


def test_a_balance_edited_in_place_is_reported() -> None:
    problems = _problems(_clean_chain(), balance=Decimal("1049.87654322"))

    assert BALANCE_NOT_LEDGER_SUM in problems
    assert BALANCE_NOT_LAST_BALANCE_AFTER in problems


def test_an_amount_edited_in_place_is_reported() -> None:
    lines = _clean_chain()
    edited = lines[1]
    lines[1] = LedgerLine(2, Decimal("-1"), edited.balance_before, edited.balance_after)

    problems = _problems(lines)

    assert BALANCE_NOT_LEDGER_SUM in problems
    assert ROW_ARITHMETIC_BROKEN in problems


def test_a_deleted_row_is_reported() -> None:
    lines = _clean_chain()
    del lines[1]

    problems = _problems(lines)

    assert SEQUENCE_GAP in problems
    assert CHAIN_BROKEN in problems
    assert BALANCE_NOT_LEDGER_SUM in problems


def test_a_version_that_does_not_match_the_last_row_is_reported() -> None:
    assert VERSION_NOT_LAST_SEQUENCE in _problems(_clean_chain(), version=4)


def test_a_truncated_ledger_is_reported() -> None:
    """`TRUNCATE` 不经触发器（设计 §10 的残余风险）：版本 > 0 而账本为空。"""
    problems = _problems([])

    assert LEDGER_MISSING in problems
    assert BALANCE_NOT_LEDGER_SUM in problems
    assert VERSION_NOT_LAST_SEQUENCE in problems


def test_a_billing_status_that_disagrees_with_the_balance_is_reported() -> None:
    problems = _problems(_clean_chain(), billing_status=BillingStatus.SUSPENDED)

    assert problems == [BILLING_STATUS_MISMATCH]


# --- 不可变（INV-5）与错误契约 ------------------------------------------------------


def test_the_repository_has_no_way_to_change_or_remove_a_ledger_row() -> None:
    """INV-5 的应用层一半：公开函数里没有任何更新或删除，模块也没导入那两个构造。"""
    public = [
        name
        for name, member in inspect.getmembers(wallet_repository, inspect.isfunction)
        if member.__module__ == wallet_repository.__name__ and not name.startswith("_")
    ]

    assert "post_transaction" in public, "反射本身得先找得到函数，否则下面的断言是空的"
    for word in ("update", "delete", "remove", "amend", "void", "reverse"):
        assert [name for name in public if word in name] == [], word
    assert not hasattr(wallet_repository, "update")
    assert not hasattr(wallet_repository, "delete")


def test_every_ledger_error_shares_one_base() -> None:
    for error in (InvalidAmount, InvalidTransaction, WalletNotFound, LedgerConflict):
        assert issubclass(error, LedgerError)
