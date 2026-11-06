"""Catch-up and the hourly task of monthly statements (AIH-TASK-157, gate #565 v6).

Rows of design §7 marked 157 covered here:

- **catch-up** — a run after two months down generates both periods; a tenant whose middle
  period fails a self-check stops before it while the others catch up, the count returned
  is right and no log line carries an amount or a company name; a statement number
  collision warns, stops that tenant and is not an error;
- **永久停出（已知限制）** — a tenant failing self-check 2 in `M`, run again on two more
  hours, `now` crossing `M+1`'s due time: `M` fails every time with its check number in
  the log, `M+1` is never generated, the other tenant gets both;
- **并发** (MySQL only) — another session inserts and commits the same statement while the
  catch-up's INSERT waits on the unique key: the `IntegrityError` counts as generated and
  the catch-up goes on with the next period;
- **合格性** — only tenants `PREPAID` now are caught up;
- the `before_commit` hook: called exactly once per new statement inside its transaction;
  any exception from it (an `IntegrityError` too) is a failure and the statement is not
  written; `raise_errors` decides whether it is raised;
- before the first period is due every run is empty; the Celery task passes the current
  UTC time and logs the count.

Ledger rows and events are written as test_statement_generate.py does (its `Statements`).
The MySQL half needs `BILLING_TEST_DATABASE_URL`; it skips without it — **a skip is not a
pass**, CI sets it. On MySQL, `monthly_statements` and `tax_policy_versions` are truncated
with foreign key checks off before and after every case (their triggers refuse DELETE).
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from collections.abc import Iterator
from decimal import Decimal

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from test_statement_generate import (
    DAY,
    DECEMBER,
    FEBRUARY,
    HOUR,
    JANUARY,
    SECOND,
    Statements,
    due,
)
from test_usage_billing import TEST_DATABASE_URL, Tenancy, _sqlite_factory, clean

from alembic import command
from app.core.database import create_session_factory
from app.models.statements import MonthlyStatement
from app.models.tenancy import BillingMode, Tenant
from app.services import statements
from app.services.statements import PreviousStatementMissing, generate_statement

# Distinctive amounts: no log line may carry them.
AMOUNT = "-0.31415926"
WRONG_CHARGE = "0.27182818"

_RESERVED_LOG_ATTRS = frozenset(
    set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime", "taskName"}
)

FAILED = "Statement generation failed"
COLLIDED = "Statement number collided; the next run retries"
TENANT_FAILED = "Statement catch-up failed for a tenant"
INTEGRITY = "Statement integrity check failed"

# Design §7「并发」: the catch-up's INSERT really waits on the unique key.
_WAITING_ON_STATEMENTS = text(
    "SELECT COUNT(*) FROM performance_schema.data_lock_waits AS w"
    " JOIN performance_schema.data_locks AS l"
    " ON l.ENGINE = w.ENGINE AND l.ENGINE_LOCK_ID = w.REQUESTING_ENGINE_LOCK_ID"
    " WHERE l.OBJECT_SCHEMA = DATABASE() AND l.OBJECT_NAME = 'monthly_statements'"
)
_WAIT_LIMIT_SECONDS = 10.0
_POLL_SECONDS = 0.05


# --- fixtures ---------------------------------------------------------------------------


def _wipe(engine: Engine) -> None:
    """Statements and tax policy versions refuse DELETE (triggers) and reference the tenants and
    users `clean` deletes: TRUNCATE them first, with foreign key checks off."""
    with engine.begin() as connection:
        connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 0")
        try:
            connection.exec_driver_sql("TRUNCATE TABLE monthly_statements")
            connection.exec_driver_sql("TRUNCATE TABLE tax_policy_versions")
        finally:
            connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 1")
    clean(engine)


def _mysql_factory(monkeypatch: pytest.MonkeyPatch) -> Iterator[sessionmaker[Session]]:
    if not TEST_DATABASE_URL:
        pytest.skip("BILLING_TEST_DATABASE_URL is not set; the MySQL half needs a real MySQL")
    from app.core.config import get_settings

    monkeypatch.setenv("BILLING_DATABASE_URL", TEST_DATABASE_URL)
    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")
    get_settings.cache_clear()
    engine = create_engine(TEST_DATABASE_URL)
    _wipe(engine)
    try:
        yield create_session_factory(engine)
    finally:
        _wipe(engine)
        engine.dispose()


@pytest.fixture(params=["sqlite", "mysql"])
def factory(request, monkeypatch) -> Iterator[sessionmaker[Session]]:
    if request.param == "mysql":
        yield from _mysql_factory(monkeypatch)
        return
    yield from _sqlite_factory()


@pytest.fixture
def mysql_factory(monkeypatch) -> Iterator[sessionmaker[Session]]:
    yield from _mysql_factory(monkeypatch)


@pytest.fixture
def books(factory) -> Statements:
    """SQLite and a real MySQL, with a tax policy in effect from before every period."""
    return Statements(factory)


@pytest.fixture
def mysql_books(mysql_factory) -> Statements:
    return Statements(mysql_factory)


# --- helpers ----------------------------------------------------------------------------


def _records(caplog, level: int | None = None) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name == statements.__name__ and (level is None or r.levelno == level)
    ]


def _extras(record: logging.LogRecord) -> dict[str, object]:
    return {k: v for k, v in vars(record).items() if k not in _RESERVED_LOG_ATTRS}


def _lines(caplog, message: str) -> list[dict[str, object]]:
    return [_extras(r) for r in _records(caplog) if r.getMessage() == message]


def _assert_no_amount_or_company(caplog) -> None:
    """Design §6: logs carry public ids, periods, exception type names and counts only."""
    forbidden = [AMOUNT.lstrip("-"), WRONG_CHARGE, "Sdn Bhd"]
    for record in _records(caplog):
        line = f"{record.getMessage()} {_extras(record)!r}"
        for value in forbidden:
            assert value not in line, (value, line)
        assert record.exc_info is None


def _periods(books: Statements, tenant: Tenancy) -> list[dt.datetime]:
    return [s.period_start for s in books.statements(tenant)]


def _defect(books: Statements, tenant: Tenancy, at: dt.datetime) -> None:
    """A processed event whose `billable_cost` differs from its ledger row: self-check 2 fails."""
    books.charge(tenant, at, at, AMOUNT, billable=WRONG_CHARGE)


# --- catch-up (§7「catch-up」) -------------------------------------------------------------


def test_a_run_after_two_months_down_generates_both_periods(books) -> None:
    ours, theirs = books.tenant("a"), books.tenant("b", opening=Decimal("500"))
    for tenant in (ours, theirs):
        books.charge(tenant, DECEMBER.start + DAY, DECEMBER.start + DAY, AMOUNT)
        books.charge(tenant, JANUARY.start + DAY, JANUARY.start + DAY, AMOUNT)

    assert statements.generate_due(books.factory, due(JANUARY)) == 4

    for tenant in (ours, theirs):
        december, january = books.statements(tenant)
        assert (december.period_start, january.period_start) == (DECEMBER.start, JANUARY.start)
        assert january.snapshot_json["opening_balance"] == december.snapshot_json["closing_balance"]
        assert december.generated_by is None and january.generated_by is None
        assert december.created_at == january.created_at == due(JANUARY)
    # The next hour finds nothing missing.
    assert statements.generate_due(books.factory, due(JANUARY) + HOUR) == 0
    assert len(books.statements()) == 4


def test_a_failing_middle_period_stops_only_that_tenant(books, caplog) -> None:
    good, bad = books.tenant("good"), books.tenant("bad")
    for tenant in (good, bad):
        for period in (DECEMBER, JANUARY, FEBRUARY):
            books.charge(tenant, period.start + DAY, period.start + DAY, AMOUNT)
    _defect(books, bad, JANUARY.start + 2 * DAY)

    with caplog.at_level(logging.INFO, logger=statements.__name__):
        generated = statements.generate_due(books.factory, due(FEBRUARY))

    assert generated == 4
    assert _periods(books, good) == [DECEMBER.start, JANUARY.start, FEBRUARY.start]
    assert _periods(books, bad) == [DECEMBER.start]
    assert _lines(caplog, INTEGRITY) == [
        {"customer_id": bad.public_id, "period": "2027-01", "check": "2"}
    ]
    assert _lines(caplog, FAILED) == [
        {
            "customer_id": bad.public_id,
            "period": "2027-01",
            "error_type": "StatementIntegrityError",
        }
    ]
    assert _records(caplog, logging.WARNING) == []
    _assert_no_amount_or_company(caplog)


def test_a_failure_outside_the_periods_skips_only_that_tenant(books, caplog, monkeypatch) -> None:
    broken, fine = books.tenant("a"), books.tenant("b")
    real = statements.catch_up_tenant

    def flaky(factory, tenant_id, now, **kwargs):
        if tenant_id == broken.id:
            raise RuntimeError("fictional failure")
        return real(factory, tenant_id, now, **kwargs)

    monkeypatch.setattr(statements, "catch_up_tenant", flaky)

    with caplog.at_level(logging.INFO, logger=statements.__name__):
        assert statements.generate_due(books.factory, due(DECEMBER)) == 1

    assert _periods(books, broken) == []
    assert _periods(books, fine) == [DECEMBER.start]
    assert _lines(caplog, TENANT_FAILED) == [
        {"customer_id": broken.public_id, "error_type": "RuntimeError"}
    ]
    _assert_no_amount_or_company(caplog)


def test_a_statement_number_collision_warns_and_stops_the_tenant(
    books, caplog, monkeypatch
) -> None:
    first, second = books.tenant("a"), books.tenant("b")
    taken = books.generate(first, DECEMBER).statement.statement_number
    real = statements.new_statement_number

    def colliding(period: statements.StatementPeriod) -> str:
        # January's random part always lands on December's number of the first tenant.
        return taken if period.label == JANUARY.label else real(period)

    monkeypatch.setattr(statements, "new_statement_number", colliding)

    with caplog.at_level(logging.INFO, logger=statements.__name__):
        generated = statements.generate_due(books.factory, due(FEBRUARY))

    # Only the second tenant's December; February is never tried (it would have been
    # `PreviousStatementMissing`, an error).
    assert generated == 1
    assert _periods(books, first) == [DECEMBER.start]
    assert _periods(books, second) == [DECEMBER.start]
    assert [(r.getMessage(), _extras(r)) for r in _records(caplog, logging.WARNING)] == [
        (COLLIDED, {"customer_id": first.public_id, "period": "2027-01"}),
        (COLLIDED, {"customer_id": second.public_id, "period": "2027-01"}),
    ]
    assert _records(caplog, logging.ERROR) == []

    # Not an error for the admin path either.
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=statements.__name__):
        assert (
            statements.catch_up_tenant(books.factory, second.id, due(FEBRUARY), raise_errors=True)
            == []
        )
    assert len(_records(caplog, logging.WARNING)) == 1
    assert _records(caplog, logging.ERROR) == []
    _assert_no_amount_or_company(caplog)

    # The next run draws a fresh random part.
    monkeypatch.setattr(statements, "new_statement_number", real)
    assert statements.generate_due(books.factory, due(FEBRUARY) + HOUR) == 4
    for tenant in (first, second):
        assert _periods(books, tenant) == [DECEMBER.start, JANUARY.start, FEBRUARY.start]


# --- the known limitation (§7「永久停出」) ----------------------------------------------------


def test_a_data_defect_stops_the_tenant_for_good_while_others_go_on(books, caplog) -> None:
    good, bad = books.tenant("good"), books.tenant("bad")
    books.charge(good, DECEMBER.start + DAY, DECEMBER.start + DAY, AMOUNT)
    _defect(books, bad, DECEMBER.start + DAY)
    runs = [due(DECEMBER), due(DECEMBER) + HOUR, due(JANUARY)]

    generated = []
    for now in runs:
        caplog.clear()
        with caplog.at_level(logging.INFO, logger=statements.__name__):
            generated.append(statements.generate_due(books.factory, now))
        assert _lines(caplog, INTEGRITY) == [
            {"customer_id": bad.public_id, "period": "2026-12", "check": "2"}
        ]
        assert _lines(caplog, FAILED) == [
            {
                "customer_id": bad.public_id,
                "period": "2026-12",
                "error_type": "StatementIntegrityError",
            }
        ]
        _assert_no_amount_or_company(caplog)

    assert generated == [1, 0, 1]
    assert _periods(books, good) == [DECEMBER.start, JANUARY.start]
    assert _periods(books, bad) == []
    with pytest.raises(PreviousStatementMissing):
        books.generate(bad, JANUARY)


# --- eligibility, nothing due, an unknown tenant -------------------------------------------


def test_before_the_first_period_is_due_every_run_is_empty(books, caplog) -> None:
    tenant = books.tenant()
    books.charge(tenant, DECEMBER.start + DAY, DECEMBER.start + DAY, AMOUNT)
    # 2027-01-02 01:00 in Malaysia.
    assert due(DECEMBER) == dt.datetime(2027, 1, 1, 17)

    with caplog.at_level(logging.INFO, logger=statements.__name__):
        for now in (dt.datetime(2026, 11, 6, 3), DECEMBER.cutoff, due(DECEMBER) - SECOND):
            assert statements.generate_due(books.factory, now) == 0
            assert statements.catch_up_tenant(books.factory, tenant.id, now) == []

    assert books.statements() == []
    assert _records(caplog) == []
    assert statements.generate_due(books.factory, due(DECEMBER)) == 1


def test_only_tenants_prepaid_now_are_caught_up(books) -> None:
    prepaid, internal = books.tenant("a"), books.tenant("b")
    with books.factory() as session:
        session.execute(
            update(Tenant)
            .where(Tenant.id == internal.id)
            .values(billing_mode=BillingMode.INTERNAL_METERED_ONLY)
        )
        session.commit()

    assert statements.generate_due(books.factory, due(DECEMBER)) == 1

    assert _periods(books, prepaid) == [DECEMBER.start]
    assert _periods(books, internal) == []
    # Directly, the ineligible tenant's periods are skipped, not errors.
    assert statements.catch_up_tenant(books.factory, internal.id, due(DECEMBER)) == []


def test_an_unknown_tenant_has_nothing_to_catch_up(books) -> None:
    books.tenant()

    assert statements.catch_up_tenant(books.factory, 10**9, due(JANUARY), raise_errors=True) == []
    assert books.statements() == []


# --- the before_commit hook ---------------------------------------------------------------


def test_the_hook_runs_once_per_new_statement_inside_its_transaction(books) -> None:
    tenant = books.tenant()
    # Already there: no hook call for it.
    books.generate(tenant, DECEMBER)
    mysql = books.factory.kw["bind"].dialect.name == "mysql"
    calls: list[str] = []

    def hook(session: Session, statement: MonthlyStatement) -> None:
        assert statement in session
        assert session.in_transaction()
        # Flushed in this transaction, so this session reads it back.
        found = session.execute(
            select(MonthlyStatement.id).where(MonthlyStatement.public_id == statement.public_id)
        ).scalar_one()
        assert found == statement.id
        if mysql:
            # Not committed yet: another connection does not see it.
            with books.factory() as outside:
                assert (
                    outside.execute(
                        select(func.count())
                        .select_from(MonthlyStatement)
                        .where(MonthlyStatement.public_id == statement.public_id)
                    ).scalar_one()
                    == 0
                )
        calls.append(statement.public_id)

    generated = statements.catch_up_tenant(
        books.factory,
        tenant.id,
        due(FEBRUARY),
        generated_by=books.world.admin.id,
        before_commit=hook,
    )

    assert [s.period_start for s in generated] == [JANUARY.start, FEBRUARY.start]
    assert calls == [s.public_id for s in generated]
    stored = books.statements(tenant)
    assert [s.period_start for s in stored] == [DECEMBER.start, JANUARY.start, FEBRUARY.start]
    assert [s.generated_by for s in stored] == [None, books.world.admin.id, books.world.admin.id]
    # Nothing new: the hook is not called again.
    assert (
        statements.catch_up_tenant(books.factory, tenant.id, due(FEBRUARY), before_commit=hook)
        == []
    )
    assert len(calls) == 2


@pytest.mark.parametrize("raise_errors", [True, False])
def test_a_failing_hook_writes_no_statement(books, caplog, raise_errors: bool) -> None:
    tenant = books.tenant()
    calls: list[str] = []

    def hook(session: Session, statement: MonthlyStatement) -> None:
        calls.append(statement.public_id)
        if statement.period_start == JANUARY.start:
            raise RuntimeError("fictional audit failure")

    with caplog.at_level(logging.INFO, logger=statements.__name__):
        if raise_errors:
            with pytest.raises(RuntimeError):
                statements.catch_up_tenant(
                    books.factory, tenant.id, due(FEBRUARY), before_commit=hook, raise_errors=True
                )
        else:
            generated = statements.catch_up_tenant(
                books.factory, tenant.id, due(FEBRUARY), before_commit=hook
            )
            assert [s.period_start for s in generated] == [DECEMBER.start]

    # December committed; January rolled back with its hook; February never tried.
    assert len(calls) == 2
    assert _periods(books, tenant) == [DECEMBER.start]
    assert _lines(caplog, FAILED) == [
        {"customer_id": tenant.public_id, "period": "2027-01", "error_type": "RuntimeError"}
    ]


@pytest.mark.parametrize("raise_errors", [True, False])
def test_an_integrity_error_from_the_hook_is_a_failure(books, caplog, raise_errors: bool) -> None:
    tenant = books.tenant()

    def hook(session: Session, statement: MonthlyStatement) -> None:
        if statement.period_start == JANUARY.start:
            raise IntegrityError("INSERT INTO fictional", {}, Exception("fictional duplicate"))

    with caplog.at_level(logging.INFO, logger=statements.__name__):
        if raise_errors:
            with pytest.raises(IntegrityError):
                statements.catch_up_tenant(
                    books.factory, tenant.id, due(JANUARY), before_commit=hook, raise_errors=True
                )
        else:
            generated = statements.catch_up_tenant(
                books.factory, tenant.id, due(JANUARY), before_commit=hook
            )
            assert [s.period_start for s in generated] == [DECEMBER.start]

    assert _periods(books, tenant) == [DECEMBER.start]
    assert _lines(caplog, FAILED) == [
        {"customer_id": tenant.public_id, "period": "2027-01", "error_type": "IntegrityError"}
    ]
    # A failure, not a concurrent duplicate or a number collision.
    assert _records(caplog, logging.WARNING) == []


# --- the Celery task ----------------------------------------------------------------------


def test_the_task_runs_on_the_current_utc_time_and_logs_the_count(
    books, caplog, monkeypatch
) -> None:
    from app.tasks import statements as task

    tenant = books.tenant()
    monkeypatch.setattr(task, "_session_factory", lambda: books.factory)
    monkeypatch.setattr(task, "utc_now", lambda: due(DECEMBER) + dt.timedelta(microseconds=500))

    with caplog.at_level(logging.INFO, logger=task.__name__):
        assert task.generate_due() == 1

    [record] = [r for r in caplog.records if r.name == task.__name__]
    assert record.levelno == logging.INFO
    assert record.getMessage() == "Generated due monthly statements"
    assert _extras(record) == {"count": 1}
    [stored] = books.statements(tenant)
    assert stored.created_at == stored.finalized_at == due(DECEMBER)


# --- MySQL only: a concurrent duplicate (§7「并发」) ------------------------------------------


def test_a_concurrent_insert_of_the_same_statement_counts_as_generated(mysql_books, caplog) -> None:
    books = mysql_books
    tenant = books.tenant()
    books.charge(tenant, DECEMBER.start + DAY, DECEMBER.start + DAY, AMOUNT)
    returned: list[list[MonthlyStatement]] = []
    errors: list[BaseException] = []

    def catch_up() -> None:
        try:
            returned.append(
                statements.catch_up_tenant(
                    books.factory, tenant.id, due(JANUARY), raise_errors=True
                )
            )
        except BaseException as error:
            # Handed to the main thread.
            errors.append(error)

    other = books.factory()
    worker = threading.Thread(target=catch_up, daemon=True)
    try:
        with caplog.at_level(logging.INFO, logger=statements.__name__):
            # The other generator inserts December first and holds it uncommitted ...
            assert generate_statement(other, tenant.id, DECEMBER, now=due(DECEMBER)).created
            worker.start()
            # ... until the catch-up's INSERT waits on the unique key, then commits.
            deadline = time.monotonic() + _WAIT_LIMIT_SECONDS
            while True:
                with books.factory() as probe:
                    waiting = probe.execute(_WAITING_ON_STATEMENTS).scalar_one()
                if waiting or errors or not worker.is_alive():
                    break
                assert time.monotonic() < deadline, "the catch-up INSERT never waited"
                time.sleep(_POLL_SECONDS)
            assert waiting, errors
            other.commit()
            worker.join(_WAIT_LIMIT_SECONDS * 3)
    finally:
        other.close()
        worker.join(_WAIT_LIMIT_SECONDS * 3)

    assert not worker.is_alive()
    assert errors == []
    # December counts as generated (by the other one, so not returned); January follows.
    [generated] = returned
    assert [s.period_start for s in generated] == [JANUARY.start]
    assert _periods(books, tenant) == [DECEMBER.start, JANUARY.start]
    assert _records(caplog, logging.WARNING) == []
    assert _records(caplog, logging.ERROR) == []
