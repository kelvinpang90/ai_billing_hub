"""`wallet_negative_balance` merged with the reconciliation runs (AIH-TASK-133, design gate #474 v13
§2「告警」, §7).

§7 里属于告警的行都在这里，运行行直接用模型插入，不经核对任务：告警；告警的新旧并存；
未完成运行里的不一致；运行始终完不成；不一致修复后恢复；时钟与插入顺序不一致；
时钟偏快期间跑完的运行；完成时刻在未来的运行；首次运行在凌晨（告警部分）；
未来日期的运行（告警部分）。另有：只有时钟异常的 `COMPLETED` 行视为从未完成、
10 分钟与 30 小时的边界、只发 SELECT。

两库参数化。SQLite 用 test_usage_billing 的工厂（`create_all` 建表）；MySQL 那一半需要
`BILLING_TEST_DATABASE_URL`，没设时 skip —— **不要把 skipped 读成 passed**，CI 里不许 skip。
MySQL 库是共用的：核对表前后清空，但别的测试文件留下的钱包可能有负余额，所以期望的负余额
部分按库里的实际个数拼（负余额本身的判级在 test_usage_alerts.py）；需要自己造负余额钱包的
用例只在 SQLite 上跑（MySQL 的触发器挡住直接写钱包）。

⚠️ SQL 文本只用来判断语句种类，不写死方言字面量。
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterator
from decimal import Decimal

import pytest
from sqlalchemy import Engine, event, func, select, update
from sqlalchemy.orm import Session, sessionmaker
from test_usage_billing import _sqlite_factory
from test_wallet_reconciliation_start import _mysql, myt, put_run

from app.core.fx_source import kuala_lumpur_date
from app.models.wallet import Wallet
from app.models.wallet_reconciliation import ReconciliationRunStatus, WalletReconciliationRun
from app.services import alerts
from app.services.alerts import AlertContext, AlertLevel, AlertResult

Run = WalletReconciliationRun
RUNNING = ReconciliationRunStatus.RUNNING
COMPLETED = ReconciliationRunStatus.COMPLETED
ABANDONED = ReconciliationRunStatus.ABANDONED

OK = AlertLevel.OK
P1 = AlertLevel.P1
P2 = AlertLevel.P2

DAY = dt.timedelta(days=1)
HOUR = dt.timedelta(hours=1)
MINUTE = dt.timedelta(minutes=1)
SECOND = dt.timedelta(seconds=1)

TODAY = dt.date(2026, 10, 29)
YESTERDAY = TODAY - DAY
TOMORROW = TODAY + DAY
# 马来西亚今天 12:00。
NOW = myt(TODAY, 12)

# 摘要只有计数与日期（INV-7）：小写字母、数字、空格、分号与日期里的连字符。
SUMMARY_CHARACTERS = re.compile(r"^[a-z0-9 ;\-]+$")


# --- fixtures --------------------------------------------------------------------------


@pytest.fixture(params=["sqlite", "mysql"])
def both(request, monkeypatch) -> Iterator[sessionmaker[Session]]:
    """The same cases on SQLite and on a real MySQL."""
    if request.param == "mysql":
        yield from _mysql(monkeypatch)
        return
    yield from _sqlite_factory()


@pytest.fixture
def sqlite_factory() -> Iterator[sessionmaker[Session]]:
    yield from _sqlite_factory()


# --- helpers ---------------------------------------------------------------------------


def evaluate(factory: sessionmaker[Session], now: dt.datetime = NOW) -> AlertResult:
    with factory() as session:
        return alerts.wallet_negative_balance(session, AlertContext(now=now, currencies=("USD",)))


def below_zero(factory: sessionmaker[Session]) -> int:
    with factory() as session:
        statement = select(func.count()).select_from(Wallet).where(Wallet.balance < 0)
        return int(session.execute(statement).scalar_one())


def negative_text(count: int) -> str:
    if not count:
        return "no wallet below zero"
    return "1 wallet below zero" if count == 1 else f"{count} wallets below zero"


def expect(
    factory: sessionmaker[Session],
    level: AlertLevel,
    reconciliation: str,
    now: dt.datetime = NOW,
) -> None:
    """The whole line: the negative part as the database has it, then the reconciliation part."""
    negatives = below_zero(factory)
    if negatives and level is OK:
        level = P2
    result = evaluate(factory, now)
    assert result == AlertResult(level, f"{negative_text(negatives)}; {reconciliation}")
    assert SUMMARY_CHARACTERS.match(result.summary)


def completed(
    factory: sessionmaker[Session],
    run_date: dt.date,
    finished_at: dt.datetime,
    *,
    inconsistent: int = 0,
    checked: int = 3,
) -> int:
    return put_run(
        factory,
        run_date,
        COMPLETED,
        started_at=finished_at - HOUR,
        finished_at=finished_at,
        cursor_tenant_id=checked,
        wallets_checked=checked,
        wallets_inconsistent=inconsistent,
    )


def abandoned(
    factory: sessionmaker[Session],
    run_date: dt.date,
    *,
    inconsistent: int = 0,
    finished_at: dt.datetime | None = None,
) -> int:
    return put_run(
        factory,
        run_date,
        ABANDONED,
        finished_at=finished_at or myt(run_date + DAY, 0, 5),
        cursor_tenant_id=inconsistent,
        wallets_checked=inconsistent,
        wallets_inconsistent=inconsistent,
    )


def running(factory: sessionmaker[Session], run_date: dt.date, *, inconsistent: int = 0) -> int:
    return put_run(
        factory,
        run_date,
        RUNNING,
        cursor_tenant_id=inconsistent,
        wallets_checked=inconsistent,
        wallets_inconsistent=inconsistent,
    )


def add_negative_wallet(factory: sessionmaker[Session], tenant_id: int) -> None:
    """SQLite only: the balance is fictional and written directly (no trigger there)."""
    with factory() as session:
        session.add(
            Wallet(
                tenant_id=tenant_id,
                currency="MYR",
                balance=Decimal("-7.75"),
                version=1,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()


# --- 告警 ------------------------------------------------------------------------------


def test_without_any_run_it_is_p2_never_completed(both) -> None:
    expect(both, P2, "reconciliation never completed")


def test_a_consistent_recent_completed_run_is_ok(both) -> None:
    completed(both, TODAY, myt(TODAY, 3, 30))

    expect(both, OK, "reconciliation 2026-10-29 checked 3 inconsistent 0")


@pytest.mark.parametrize(
    ("age", "level", "stale"),
    [
        (29 * HOUR, OK, ""),
        (30 * HOUR, OK, ""),
        (30 * HOUR + SECOND, P2, " stale"),
        (31 * HOUR, P2, " stale"),
    ],
    ids=["29-hours", "exactly-30-hours", "30-hours-and-a-second", "31-hours"],
)
def test_a_completed_run_older_than_30_hours_is_stale(
    both, age: dt.timedelta, level: AlertLevel, stale: str
) -> None:
    finished = NOW - age
    day = kuala_lumpur_date(finished)
    completed(both, day, finished)

    expect(both, level, f"reconciliation {day.isoformat()} checked 3 inconsistent 0{stale}")


def test_an_inconsistent_completed_run_is_p1(both) -> None:
    completed(both, TODAY, myt(TODAY, 3, 30), inconsistent=1)

    expect(both, P1, "reconciliation 2026-10-29 checked 3 inconsistent 1")


def test_an_inconsistency_outranks_negative_wallets(sqlite_factory) -> None:
    completed(sqlite_factory, TODAY, myt(TODAY, 3, 30), inconsistent=1)
    add_negative_wallet(sqlite_factory, 1)
    add_negative_wallet(sqlite_factory, 2)

    result = evaluate(sqlite_factory)

    assert result == AlertResult(
        P1, "2 wallets below zero; reconciliation 2026-10-29 checked 3 inconsistent 1"
    )
    assert "7.75" not in result.summary


def test_negative_wallets_with_a_stale_run_are_p2(sqlite_factory) -> None:
    completed(sqlite_factory, dt.date(2026, 10, 26), myt(dt.date(2026, 10, 26), 4), inconsistent=1)
    completed(sqlite_factory, dt.date(2026, 10, 27), myt(dt.date(2026, 10, 27), 4))
    add_negative_wallet(sqlite_factory, 1)
    add_negative_wallet(sqlite_factory, 2)

    assert evaluate(sqlite_factory) == AlertResult(
        P2, "2 wallets below zero; reconciliation 2026-10-27 checked 3 inconsistent 0 stale"
    )


# --- 待判运行集 ------------------------------------------------------------------------


def test_an_inconsistent_completed_run_stays_p1_while_a_new_run_is_running(both) -> None:
    """告警的新旧并存。"""
    completed(both, YESTERDAY, myt(YESTERDAY, 4), inconsistent=1)
    running(both, TODAY)

    # 今天 03:10，新运行刚开始。
    expect(both, P1, "reconciliation 2026-10-28 checked 3 inconsistent 1", now=myt(TODAY, 3, 10))


def test_an_inconsistency_in_an_unfinished_run_is_p1_at_once(both) -> None:
    """未完成运行里的不一致。"""
    running(both, TODAY, inconsistent=1)

    expect(both, P1, "reconciliation never completed; later inconsistent 1")


def test_runs_that_never_complete_keep_the_largest_later_inconsistency(both) -> None:
    """运行始终完不成：最近完成是 3 天前且一致，之后三条的不一致数 1、2、0。"""
    three_days_ago = TODAY - 3 * DAY
    completed(both, three_days_ago, myt(three_days_ago, 4))
    abandoned(both, TODAY - 2 * DAY, inconsistent=1)
    abandoned(both, YESTERDAY, inconsistent=2)
    running(both, TODAY)

    expect(
        both, P1, "reconciliation 2026-10-26 checked 3 inconsistent 0 stale; later inconsistent 2"
    )


def test_a_consistent_completed_run_clears_an_earlier_inconsistency(both) -> None:
    """不一致修复后恢复：`ABANDONED` 那条已不在待判集里。"""
    completed(both, TODAY - 2 * DAY, myt(TODAY - 2 * DAY, 4))
    abandoned(both, YESTERDAY, inconsistent=1)
    completed(both, TODAY, myt(TODAY, 4))

    expect(both, OK, "reconciliation 2026-10-29 checked 3 inconsistent 0")


def test_the_latest_completed_run_is_the_largest_id_not_the_latest_clock(both) -> None:
    """时钟与插入顺序不一致：两条都是正常行。"""
    earlier = completed(both, TODAY, myt(TODAY, 4))
    later = completed(both, YESTERDAY, myt(YESTERDAY, 20), inconsistent=1)
    assert later > earlier

    expect(both, P1, "reconciliation 2026-10-28 checked 3 inconsistent 1")


# --- 时钟异常的行 ------------------------------------------------------------------------


def test_a_run_finished_while_the_clock_ran_fast(both) -> None:
    """时钟偏快期间跑完的运行：D 当天它是异常行；D+1 当天它是正常行与最近完成。"""
    first = completed(both, TODAY, myt(TODAY, 4), inconsistent=1)
    fast = completed(both, TOMORROW, myt(TOMORROW, 3, 30))
    assert fast > first

    expect(both, P1, "reconciliation 2026-10-29 checked 3 inconsistent 1; clock anomaly 1")
    expect(
        both,
        OK,
        "reconciliation 2026-10-30 checked 3 inconsistent 0",
        now=myt(TOMORROW, 12),
    )


def test_a_run_finished_in_the_future_still_raises_its_inconsistency(both) -> None:
    """完成时刻在未来的运行：最近完成仍是三天前那条。"""
    three_days_ago = TODAY - 3 * DAY
    completed(both, three_days_ago, myt(three_days_ago, 4))
    completed(both, YESTERDAY, NOW + 2 * HOUR, inconsistent=1)

    expect(
        both,
        P1,
        "reconciliation 2026-10-26 checked 3 inconsistent 0 stale; later inconsistent 1;"
        " clock anomaly 1",
    )


@pytest.mark.parametrize(
    ("ahead", "expected"),
    [
        (10 * MINUTE, AlertResult(OK, "reconciliation 2026-10-29 checked 3 inconsistent 0")),
        (
            10 * MINUTE + SECOND,
            AlertResult(
                P2,
                "reconciliation 2026-10-28 checked 3 inconsistent 0; clock anomaly 1",
            ),
        ),
    ],
    ids=["exactly-10-minutes-ahead", "10-minutes-and-a-second-ahead"],
)
def test_finished_at_up_to_10_minutes_ahead_is_still_normal(
    both, ahead: dt.timedelta, expected: AlertResult
) -> None:
    # 24 小时前：不过期。
    completed(both, YESTERDAY, myt(YESTERDAY, 12))
    completed(both, TODAY, NOW + ahead)

    expect(both, expected.level, expected.summary)


def test_future_dated_runs_are_a_clock_anomaly(both) -> None:
    """未来日期的运行（告警部分）：明天的 `COMPLETED`、被收尾的后天的运行与今天完成的运行。"""
    completed(both, TOMORROW, myt(TOMORROW, 4))
    # 第 0 步收尾：`finished_at` 是今天，`run_date` 仍是后天。
    abandoned(both, TOMORROW + DAY, finished_at=myt(TODAY, 3, 0))
    today = completed(both, TODAY, myt(TODAY, 3, 30))

    expect(both, P2, "reconciliation 2026-10-29 checked 3 inconsistent 0; clock anomaly 2")

    # 今天那条的 `finished_at` 拨到 31 小时前：异常行不掩盖 `stale`。
    with both() as session:
        session.execute(update(Run).where(Run.id == today).values(finished_at=NOW - 31 * HOUR))
        session.commit()

    expect(both, P2, "reconciliation 2026-10-29 checked 3 inconsistent 0 stale; clock anomaly 2")


@pytest.mark.parametrize(
    ("inconsistent", "level", "later"),
    [(0, P2, ""), (1, P1, "; later inconsistent 1")],
    ids=["consistent", "inconsistent"],
)
def test_only_a_clock_anomaly_completed_run_counts_as_never_completed(
    both, inconsistent: int, level: AlertLevel, later: str
) -> None:
    completed(both, TOMORROW, myt(TOMORROW, 4), inconsistent=inconsistent)

    expect(both, level, f"reconciliation never completed{later}; clock anomaly 1")


# --- 首次运行在凌晨 ----------------------------------------------------------------------


def test_a_first_run_just_after_midnight_is_not_stale_before_the_next_one(both) -> None:
    """首次运行在凌晨（告警部分）：表为空时马来西亚 00:00:30 开始并完成。"""
    put_run(
        both,
        TODAY,
        COMPLETED,
        started_at=myt(TODAY, 0, 0, 30),
        finished_at=myt(TODAY, 0, 1, 30),
        cursor_tenant_id=3,
        wallets_checked=3,
        wallets_inconsistent=0,
    )
    summary = "reconciliation 2026-10-29 checked 3 inconsistent 0"

    # 次日 02:59，下一次运行开始之前。
    expect(both, OK, summary, now=myt(TOMORROW, 2, 59))
    expect(both, OK, summary, now=myt(TOMORROW, 3, 5))
    # 30 小时之后仍没有下一次完成。
    expect(both, P2, f"{summary} stale", now=myt(TOMORROW, 6, 1, 31))


# --- 只读 ------------------------------------------------------------------------------


def test_the_dimension_only_reads(both) -> None:
    completed(both, TODAY, myt(TODAY, 4), inconsistent=1)
    running(both, TOMORROW, inconsistent=1)
    engine = both.kw["bind"]
    assert isinstance(engine, Engine)
    seen: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        result = evaluate(both)
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert result.level is P1
    assert seen
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in seen)
