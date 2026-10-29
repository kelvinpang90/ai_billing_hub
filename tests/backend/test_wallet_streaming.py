"""Streaming wallet judgement (AIH-TASK-130, design gate #474 v13 §2「钱包判定」, §7).

§7 的这几行在这里：

- **判定等价**（unit）：标准库 `random`、固定种子、2000 组随机账本，改写前的 `ledger_problems`
  （下面保留一份原样副本）与 `ledger_problems_in_order`（先排好序）逐项相同，含顺序；
- **流式读取**：`chunk_rows = 3`。需要直接写坏数据的情形只在 SQLite 上（模型建表，没有 0006 的
  触发器；但有 `ck_wallet_transactions_arithmetic`，所以每种篡改都保持
  `balance_after = balance_before + amount`）；`ROW_ARITHMETIC_BROKEN` 在库里写不进去，只在
  判定等价里覆盖。MySQL 上只验一致的 10 行钱包与 `TRUNCATE` 账本后的 `LEDGER_MISSING`；
- **分块与提示**：捕获执行语句，块数、`MAX_EXECUTION_TIME` 提示、`on_chunk` 的次数与时机；
- **一致性快照**、**不加锁**、**金额精度（判定）**：只在 MySQL 上。

SQLite 上用 test_usage_billing 的工厂：`create_all` 建表，再装一条代替 0006 AFTER INSERT 触发器
推进钱包的测试触发器，所以 `post_transaction` 照常记账。SQLite 把 DECIMAL 存成二进制浮点，这里
的金额都是整数，两库上都精确。MySQL 那一半需要 `BILLING_TEST_DATABASE_URL`，没设时 skip ——
**不要把 skipped 读成 passed**，CI 里不许 skip。

⚠️ SQL 文本断言不写死方言字面量；只有 MySQL 才有的断言放在方言分支里。
"""

from __future__ import annotations

import dataclasses
import itertools
import math
import random
import re
import time
from collections.abc import Iterator, Sequence
from decimal import Decimal

import pytest
from sqlalchemy import Engine, delete, event, select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from test_usage_billing import T0, TEST_EMAIL_DOMAIN, _mysql_factory, _sqlite_factory

from app.models.tenancy import BillingStatus, Tenant
from app.models.wallet import ReferenceType, TransactionType, Wallet, WalletTransaction
from app.repositories.tenancy import create_tenant
from app.repositories.wallet import (
    BALANCE_NOT_LAST_BALANCE_AFTER,
    BALANCE_NOT_LEDGER_SUM,
    BILLING_STATUS_MISMATCH,
    CHAIN_BROKEN,
    LEDGER_CHUNK_TIMEOUT_MS,
    LEDGER_MISSING,
    ROW_ARITHMETIC_BROKEN,
    SEQUENCE_GAP,
    VERSION_NOT_LAST_SEQUENCE,
    WALLET_MISSING,
    LedgerLine,
    WalletCheck,
    billing_status_for,
    check_wallet_snapshot,
    create_wallet,
    ledger_problems,
    ledger_problems_in_order,
    post_transaction,
    verify_wallet,
)

CHUNK_ROWS = 3
SEED = 130
CASES = 2000
LEDGER_TABLE = re.compile(r"\bwallet_transactions\b", re.IGNORECASE)
LOWER_BOUND = re.compile(r"\bwallet_sequence\s*>", re.IGNORECASE)
HINT = "MAX_EXECUTION_TIME(60000)"

_NUMBERS = itertools.count(1)


# --- 改写前的 ledger_problems（原样副本，只改了名字；判定等价的对照） --------------------


def ledger_problems_before_130(
    *,
    balance: Decimal,
    version: int,
    billing_status: BillingStatus,
    lines: Sequence[LedgerLine],
) -> list[str]:
    """Compare a wallet with its ledger. An empty list means they agree."""
    ordered = sorted(lines, key=lambda line: line.wallet_sequence)
    problems = []
    if version > 0 and not ordered:
        # `TRUNCATE` 不经触发器（设计 §10 的残余风险），会表现成这样。
        problems.append(LEDGER_MISSING)
    if balance != sum((line.amount for line in ordered), Decimal(0)):
        problems.append(BALANCE_NOT_LEDGER_SUM)
    if ordered and balance != ordered[-1].balance_after:
        problems.append(BALANCE_NOT_LAST_BALANCE_AFTER)
    if version != (ordered[-1].wallet_sequence if ordered else 0):
        problems.append(VERSION_NOT_LAST_SEQUENCE)
    if [line.wallet_sequence for line in ordered] != list(range(1, len(ordered) + 1)):
        problems.append(SEQUENCE_GAP)
    # 钱包从空开始，所以第一行的前余额必须是 0。
    previous = Decimal(0)
    chained = arithmetic = True
    for line in ordered:
        chained = chained and line.balance_before == previous
        arithmetic = arithmetic and line.balance_after == line.balance_before + line.amount
        previous = line.balance_after
    if not chained:
        problems.append(CHAIN_BROKEN)
    if not arithmetic:
        problems.append(ROW_ARITHMETIC_BROKEN)
    if billing_status != billing_status_for(balance):
        problems.append(BILLING_STATUS_MISMATCH)
    return problems


# --- 判定等价（unit） -----------------------------------------------------------------


def _money(rng: random.Random) -> Decimal:
    """A DECIMAL(20,8) value: mostly small, sometimes near the 12-digit limit, sometimes 0."""
    roll = rng.random()
    if roll < 0.05:
        return Decimal(0).scaleb(-8)
    if roll < 0.15:
        return Decimal(rng.randint(-(10**20) + 1, 10**20 - 1)).scaleb(-8)
    return Decimal(rng.randint(-(10**10), 10**10)).scaleb(-8)


@dataclasses.dataclass(frozen=True)
class Case:
    balance: Decimal
    version: int
    billing_status: BillingStatus
    lines: list[LedgerLine]


def _random_case(rng: random.Random) -> Case:
    """A random wallet and ledger: a consistent chain, then some of the §7 kinds of damage."""
    count = rng.randint(0, 30)
    lines: list[LedgerLine] = []
    running = Decimal(0)
    for sequence in range(1, count + 1):
        amount = _money(rng)
        lines.append(LedgerLine(sequence, amount, running, running + amount))
        running += amount
    balance = running
    version = count
    damaged = rng.random() < 0.8
    if damaged and lines and rng.random() < 0.25:
        # 断档：删掉一行。
        del lines[rng.randrange(len(lines))]
    if damaged and lines and rng.random() < 0.15:
        # 重复序号：一行改成另一行的序号。
        index, other = rng.randrange(len(lines)), rng.randrange(len(lines))
        lines[index] = dataclasses.replace(
            lines[index], wallet_sequence=lines[other].wallet_sequence
        )
    if damaged and lines and rng.random() < 0.1:
        # 序号整体平移（不从 1 开始）。
        shift = rng.choice([-1, 1, 2])
        lines = [
            dataclasses.replace(line, wallet_sequence=line.wallet_sequence + shift)
            for line in lines
        ]
    if damaged and lines and rng.random() < 0.2:
        index = rng.randrange(len(lines))
        lines[index] = dataclasses.replace(lines[index], balance_before=_money(rng))
    if damaged and lines and rng.random() < 0.2:
        index = rng.randrange(len(lines))
        lines[index] = dataclasses.replace(lines[index], balance_after=_money(rng))
    if damaged and lines and rng.random() < 0.15:
        # 前后余额同移、金额不变：链断而行内算术仍成立。
        index, delta = rng.randrange(len(lines)), _money(rng)
        line = lines[index]
        lines[index] = dataclasses.replace(
            line,
            balance_before=line.balance_before + delta,
            balance_after=line.balance_after + delta,
        )
    if damaged and lines and rng.random() < 0.15:
        index = rng.randrange(len(lines))
        lines[index] = dataclasses.replace(lines[index], amount=_money(rng))
    if damaged and rng.random() < 0.15:
        balance = _money(rng)
    if damaged and rng.random() < 0.15:
        version = rng.randint(0, 35)
    status = billing_status_for(balance)
    if damaged and rng.random() < 0.15:
        status = rng.choice(list(BillingStatus))
    if damaged and lines and rng.random() < 0.4:
        # 乱序：两种实现都应与输入顺序无关。
        rng.shuffle(lines)
    return Case(balance, version, status, lines)


def test_streaming_judgement_equals_the_original_on_random_ledgers() -> None:
    rng = random.Random(SEED)
    seen: set[str] = set()
    consistent = 0
    for _ in range(CASES):
        case = _random_case(rng)
        expected = ledger_problems_before_130(
            balance=case.balance,
            version=case.version,
            billing_status=case.billing_status,
            lines=case.lines,
        )
        ordered = sorted(case.lines, key=lambda line: line.wallet_sequence)
        streamed = ledger_problems_in_order(
            balance=case.balance,
            version=case.version,
            billing_status=case.billing_status,
            # 一次性的迭代器：流式实现只能逐行消费、不能回头。
            lines=iter(ordered),
        )
        delegated = ledger_problems(
            balance=case.balance,
            version=case.version,
            billing_status=case.billing_status,
            lines=case.lines,
        )

        assert streamed == expected, case
        assert delegated == expected, case
        seen.update(expected)
        consistent += not expected

    # 区分力：每个问题码与「一致」都真的出现过。
    assert seen == {
        LEDGER_MISSING,
        BALANCE_NOT_LEDGER_SUM,
        BALANCE_NOT_LAST_BALANCE_AFTER,
        VERSION_NOT_LAST_SEQUENCE,
        SEQUENCE_GAP,
        CHAIN_BROKEN,
        ROW_ARITHMETIC_BROKEN,
        BILLING_STATUS_MISMATCH,
    }
    assert consistent > 0


def test_streaming_judgement_on_an_empty_ledger() -> None:
    # 行数 0 时序号视为 `[] == []`：新钱包一致。
    streamed = ledger_problems_in_order(
        balance=Decimal(0), version=0, billing_status=BillingStatus.SUSPENDED, lines=iter(())
    )
    original = ledger_problems_before_130(
        balance=Decimal(0), version=0, billing_status=BillingStatus.SUSPENDED, lines=[]
    )

    assert streamed == original == []


# --- 工厂与帮手 -------------------------------------------------------------------------


@pytest.fixture
def sqlite_factory() -> Iterator[sessionmaker[Session]]:
    yield from _sqlite_factory()


@pytest.fixture
def mysql_factory(monkeypatch) -> Iterator[sessionmaker[Session]]:
    yield from _mysql_factory(monkeypatch)


@pytest.fixture(params=["sqlite", "mysql"])
def both(request, monkeypatch) -> Iterator[sessionmaker[Session]]:
    """The same cases on SQLite and on a real MySQL."""
    if request.param == "mysql":
        yield from _mysql_factory(monkeypatch)
        return
    yield from _sqlite_factory()


def engine_of(factory: sessionmaker[Session]) -> Engine:
    engine = factory.kw["bind"]
    assert isinstance(engine, Engine)
    return engine


def post_in(session: Session, tenant_id: int, amount: Decimal) -> None:
    """One top-up through the real repository, in the caller's transaction."""
    post_transaction(
        session,
        tenant_id=tenant_id,
        transaction_type=TransactionType.TOPUP,
        amount=amount,
        reference_type=ReferenceType.PAYMENT,
        reference_id=f"streaming-{next(_NUMBERS)}",
        now=T0,
    )


def make_tenant(factory: sessionmaker[Session], *, wallet: bool = True) -> int:
    with factory() as session:
        tenant = create_tenant(
            session,
            company_name="Fictional Streaming Sdn Bhd",
            email=f"streaming-{next(_NUMBERS)}{TEST_EMAIL_DOMAIN}",
            now=T0,
        )
        if wallet:
            create_wallet(session, tenant_id=tenant.id, now=T0)
        session.commit()
        return int(tenant.id)


def make_wallet(factory: sessionmaker[Session], rows: int) -> int:
    """A consistent wallet of `rows` top-ups 1, 2, …, `rows`, each in its own transaction."""
    tenant_id = make_tenant(factory)
    for amount in range(1, rows + 1):
        with factory() as session:
            post_in(session, tenant_id, Decimal(amount))
            session.commit()
    return tenant_id


def truncate_ledger(factory: sessionmaker[Session]) -> None:
    """Empty the ledger without the triggers: TRUNCATE on MySQL, DELETE on SQLite."""
    engine = engine_of(factory)
    with engine.begin() as connection:
        if engine.dialect.name == "mysql":
            # 0018 起 usage_events 有外键引用账本：被引用的表要关掉外键检查才能 TRUNCATE。
            connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 0")
            try:
                connection.exec_driver_sql("TRUNCATE TABLE wallet_transactions")
            finally:
                connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 1")
        else:
            connection.execute(delete(WalletTransaction))


def stored_lines(session: Session, tenant_id: int) -> list[LedgerLine]:
    """Every ledger row of the tenant in one plain SELECT, for `ledger_problems`."""
    statement = (
        select(
            WalletTransaction.wallet_sequence,
            WalletTransaction.amount,
            WalletTransaction.balance_before,
            WalletTransaction.balance_after,
        )
        .where(WalletTransaction.tenant_id == tenant_id)
        .order_by(WalletTransaction.id)
    )
    return [LedgerLine(*row) for row in session.execute(statement)]


def checked(factory: sessionmaker[Session], tenant_id: int) -> tuple[WalletCheck, list[str]]:
    """`check_wallet_snapshot` (3-row chunks) next to `verify_wallet` and `ledger_problems`.

    三者在同一个事务里读，看到的是同一份数据；返回检查结果与对同一批行调 `ledger_problems`
    的结果。
    """
    with factory() as session:
        check = check_wallet_snapshot(session, tenant_id, chunk_rows=CHUNK_ROWS)
        verified = verify_wallet(session, tenant_id)
        lines = stored_lines(session, tenant_id)
        billing_status = session.execute(
            select(Tenant.billing_status).where(Tenant.id == tenant_id)
        ).scalar_one()
        session.rollback()
    assert isinstance(check, WalletCheck)
    assert check.version is not None
    assert check.balance is not None
    judged = ledger_problems(
        balance=check.balance,
        version=check.version,
        billing_status=billing_status,
        lines=lines,
    )
    assert check.problem_codes == verified == judged
    assert check.ledger_rows == len(lines)
    return check, judged


def record_statements(engine: Engine) -> tuple[list[str], object]:
    statements: list[str] = []

    def record(_conn, _cursor, statement, *_args) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    return statements, record


# --- 流式读取 ---------------------------------------------------------------------------


def test_wallet_check_has_the_four_fields_task_131_reads() -> None:
    assert WalletCheck._fields == ("problem_codes", "balance", "version", "ledger_rows")
    assert LEDGER_CHUNK_TIMEOUT_MS == 60000


def test_a_consistent_ten_row_wallet_reports_nothing(both) -> None:
    tenant_id = make_wallet(both, 10)

    check, _ = checked(both, tenant_id)

    assert check == WalletCheck([], Decimal(55), 10, 10)


def test_an_emptied_ledger_is_ledger_missing_with_zero_rows(both) -> None:
    tenant_id = make_wallet(both, 10)
    truncate_ledger(both)

    check, _ = checked(both, tenant_id)

    assert LEDGER_MISSING in check.problem_codes
    assert check.ledger_rows == 0
    assert (check.balance, check.version) == (Decimal(55), 10)


def test_a_missing_wallet_is_wallet_missing(both) -> None:
    tenant_id = make_tenant(both, wallet=False)

    with both() as session:
        check = check_wallet_snapshot(session, tenant_id, chunk_rows=CHUNK_ROWS)
        assert verify_wallet(session, tenant_id) == [WALLET_MISSING]

    assert check == WalletCheck([WALLET_MISSING], None, None, 0)


def _wallet_balance_plus_one(session: Session, tenant_id: int) -> None:
    session.execute(
        update(Wallet).where(Wallet.tenant_id == tenant_id).values(balance=Wallet.balance + 1)
    )


def _last_row_amount_and_after_plus_one(session: Session, tenant_id: int) -> None:
    session.execute(
        update(WalletTransaction)
        .where(WalletTransaction.tenant_id == tenant_id, WalletTransaction.wallet_sequence == 10)
        .values(
            amount=WalletTransaction.amount + 1,
            balance_after=WalletTransaction.balance_after + 1,
        )
    )


def _wallet_version_plus_one(session: Session, tenant_id: int) -> None:
    session.execute(
        update(Wallet).where(Wallet.tenant_id == tenant_id).values(version=Wallet.version + 1)
    )


def _middle_row_deleted(session: Session, tenant_id: int) -> None:
    session.execute(
        delete(WalletTransaction).where(
            WalletTransaction.tenant_id == tenant_id, WalletTransaction.wallet_sequence == 5
        )
    )


def _middle_row_before_and_after_plus_one(session: Session, tenant_id: int) -> None:
    session.execute(
        update(WalletTransaction)
        .where(WalletTransaction.tenant_id == tenant_id, WalletTransaction.wallet_sequence == 5)
        .values(
            balance_before=WalletTransaction.balance_before + 1,
            balance_after=WalletTransaction.balance_after + 1,
        )
    )


def _billing_status_suspended(session: Session, tenant_id: int) -> None:
    # 余额 55 > 0 应为 ACTIVE。
    session.execute(
        update(Tenant).where(Tenant.id == tenant_id).values(billing_status=BillingStatus.SUSPENDED)
    )


@pytest.mark.parametrize(
    ("damage", "target", "rows"),
    [
        (_wallet_balance_plus_one, BALANCE_NOT_LEDGER_SUM, 10),
        (_last_row_amount_and_after_plus_one, BALANCE_NOT_LAST_BALANCE_AFTER, 10),
        (_wallet_version_plus_one, VERSION_NOT_LAST_SEQUENCE, 10),
        (_middle_row_deleted, SEQUENCE_GAP, 9),
        (_middle_row_before_and_after_plus_one, CHAIN_BROKEN, 10),
        (_billing_status_suspended, BILLING_STATUS_MISMATCH, 10),
    ],
    ids=[
        "balance_not_ledger_sum",
        "balance_not_last_balance_after",
        "version_not_last_sequence",
        "sequence_gap",
        "chain_broken",
        "billing_status_mismatch",
    ],
)
def test_each_kind_of_damage_is_found_in_chunks(sqlite_factory, damage, target, rows) -> None:
    # 只在 SQLite 上：MySQL 的触发器挡住直接改钱包与账本（设计 §7「流式读取」）。
    tenant_id = make_wallet(sqlite_factory, 10)
    with sqlite_factory() as session:
        damage(session, tenant_id)
        session.commit()

    check, judged = checked(sqlite_factory, tenant_id)

    # 一种篡改可能同时带出几个码：比整份列表（在 `checked` 里），再看目标码在不在。
    assert target in check.problem_codes, judged
    assert check.ledger_rows == rows


def test_ledger_missing_on_sqlite_with_a_version_and_no_rows(sqlite_factory) -> None:
    tenant_id = make_tenant(sqlite_factory)
    with sqlite_factory() as session:
        session.execute(update(Wallet).where(Wallet.tenant_id == tenant_id).values(version=3))
        session.commit()

    check, _ = checked(sqlite_factory, tenant_id)

    assert LEDGER_MISSING in check.problem_codes
    assert check.ledger_rows == 0


# --- 分块与提示 -------------------------------------------------------------------------


@pytest.mark.parametrize("rows", [0, 9, 10])
def test_chunks_hints_and_on_chunk(both, rows) -> None:
    tenant_id = make_wallet(both, rows)
    engine = engine_of(both)
    commits: list[object] = []

    def count_commit(_conn) -> None:
        commits.append(_conn)

    seen_at_chunk: list[int] = []
    statements, record = record_statements(engine)
    event.listen(engine, "commit", count_commit)
    try:
        with both() as session:

            def on_chunk() -> None:
                seen_at_chunk.append(len([s for s in statements if LEDGER_TABLE.search(s)]))

            check = check_wallet_snapshot(
                session, tenant_id, chunk_rows=CHUNK_ROWS, on_chunk=on_chunk
            )
            # 不提交、不结束调用方的事务。
            assert session.in_transaction()
            session.rollback()
    finally:
        event.remove(engine, "before_cursor_execute", record)
        event.remove(engine, "commit", count_commit)

    assert check.ledger_rows == rows
    assert check.problem_codes == []
    assert commits == []
    # 只读、不加锁（INV-4、INV-5、§2「不加锁」）。
    for statement in statements:
        assert statement.lstrip().upper().startswith("SELECT"), statement
        assert "FOR UPDATE" not in statement.upper(), statement
        assert "SHARE" not in statement.upper(), statement

    chunks = [statement for statement in statements if LEDGER_TABLE.search(statement)]
    full = math.ceil(rows / CHUNK_ROWS)
    # 每个非空块一次，再加一次取空。
    assert len(chunks) == full + 1
    # 第 k 次 `on_chunk` 发生在第 k 块读完之后、第 k + 1 块之前。
    assert seen_at_chunk == list(range(1, full + 1))
    # 第一块不设序号下界；之后每块都带上一块末尾的序号。
    assert not LOWER_BOUND.search(chunks[0])
    for chunk in chunks[1:]:
        assert LOWER_BOUND.search(chunk), chunk
    for chunk in chunks:
        if engine.dialect.name == "mysql":
            assert chunk.count("MAX_EXECUTION_TIME") == 1, chunk
            # 在最外层：紧跟唯一的那个 SELECT 关键字。
            assert re.match(r"\s*SELECT\s+/\*\+\s*MAX_EXECUTION_TIME\(60000\)\s*\*/", chunk), chunk
            assert len(re.findall(r"\bSELECT\b", chunk, re.IGNORECASE)) == 1, chunk
            assert HINT in chunk
        else:
            assert "MAX_EXECUTION_TIME" not in chunk.upper(), chunk


def test_a_chunk_error_reaches_the_caller_unchanged(sqlite_factory, monkeypatch) -> None:
    # MySQL 的执行超时（3024）不被转换成别的异常：调用方（AIH-TASK-132）按 `advance` 处理。
    tenant_id = make_wallet(sqlite_factory, 10)
    timeout = OperationalError(
        "SELECT wallet_transactions",
        {},
        Exception(3024, "Query execution was interrupted, maximum statement execution time"),
    )
    chunks = itertools.count(1)
    called: list[None] = []

    with sqlite_factory() as session:
        execute = session.execute

        def failing_execute(statement, *args, **kwargs):
            if LEDGER_TABLE.search(str(statement)) and next(chunks) == 2:
                raise timeout
            return execute(statement, *args, **kwargs)

        monkeypatch.setattr(session, "execute", failing_execute)
        with pytest.raises(OperationalError) as raised:
            check_wallet_snapshot(
                session, tenant_id, chunk_rows=CHUNK_ROWS, on_chunk=lambda: called.append(None)
            )

    assert raised.value is timeout
    assert len(called) == 1


# --- 一致性快照与不加锁（MySQL） -------------------------------------------------------


def _check_while_posting(
    factory: sessionmaker[Session], tenant_id: int
) -> tuple[WalletCheck, float]:
    """Check with 3-row chunks; after the first chunk, another session posts 11 and commits.

    另一个会话把锁等待上限设成 1 秒：核对若持有钱包或租户上的任何锁，记账会等不到而报错。
    """
    waited: list[float] = []

    def post_from_another_session() -> None:
        if waited:
            return
        with factory() as other:
            other.execute(text("SET SESSION innodb_lock_wait_timeout = 1"))
            started = time.monotonic()
            post_in(other, tenant_id, Decimal(11))
            other.commit()
            waited.append(time.monotonic() - started)

    with factory() as reader:
        check = check_wallet_snapshot(
            reader, tenant_id, chunk_rows=CHUNK_ROWS, on_chunk=post_from_another_session
        )
        assert reader.in_transaction()
        reader.rollback()
    assert len(waited) == 1
    return check, waited[0]


def test_the_check_reads_one_consistent_snapshot(mysql_factory) -> None:
    with mysql_factory() as session:
        isolation = session.execute(text("SELECT @@transaction_isolation")).scalar_one()
    # 与生产一致（设计 §1）：快照靠的是 REPEATABLE READ。
    assert isolation == "REPEATABLE-READ"
    tenant_id = make_wallet(mysql_factory, 10)

    check, _ = _check_while_posting(mysql_factory, tenant_id)

    # 看到的是记账前的快照：钱包与账本都还是 10 行时的状态，不误报。
    assert check == WalletCheck([], Decimal(55), 10, 10)
    with mysql_factory() as session:
        again = check_wallet_snapshot(session, tenant_id, chunk_rows=CHUNK_ROWS)
    assert again == WalletCheck([], Decimal(66), 11, 11)


def test_the_check_does_not_block_posting(mysql_factory) -> None:
    tenant_id = make_wallet(mysql_factory, 10)

    _check, waited = _check_while_posting(mysql_factory, tenant_id)

    # 记账不等待：锁等待上限 1 秒，等过就会报错；这里远小于它。
    assert waited < 1
    with mysql_factory() as session:
        assert verify_wallet(session, tenant_id) == []
        version = session.execute(
            select(Wallet.version).where(Wallet.tenant_id == tenant_id)
        ).scalar_one()
    assert version == 11


# --- 金额精度（判定，MySQL） -----------------------------------------------------------


def test_the_largest_balance_is_judged_and_returned_exactly(mysql_factory) -> None:
    exact = Decimal("999999999999.99999999")
    # 用例有区分力：这个值经 float 往返会变。
    assert Decimal(float(exact)) != exact
    assert Decimal(repr(float(exact))) != exact
    tenant_id = make_tenant(mysql_factory)
    for amount in ("999999999999.99999998", "0.00000001"):
        with mysql_factory() as session:
            post_in(session, tenant_id, Decimal(amount))
            session.commit()

    with mysql_factory() as session:
        check = check_wallet_snapshot(session, tenant_id, chunk_rows=CHUNK_ROWS)

    assert check.problem_codes == []
    assert isinstance(check.balance, Decimal)
    assert check.balance == exact
    assert format(check.balance, "f") == "999999999999.99999999"
    assert (check.version, check.ledger_rows) == (2, 2)
