"""The first tax policy through the service, and resolving it (design gate #332 v3 §2, §7).

Groups:

- **publishing** (`publish_initial_tax_policy`): the normal path, the two audit rows, replaying
  it, a row that should not be there, the approver checks, a write failing half way, a commit
  whose acknowledgement is lost, and the lock being the first statement. SQLite and MySQL once
  each (`factory` fixture); the concurrency and lock-ordering cases need two connections and run
  on MySQL only (`mysql_factory`);
- **treatment_json** and **rate consistency** (unit), the notice not drifting from ADR-0008 and
  the start converted from Kuala Lumpur time;
- **resolve_tax_policy**: the boundaries on both dialects; other states, read-time checks and
  corrupted overlapping periods on SQLite, where no trigger stops the ORM from writing them;
- the `status` CHECK on SQLite (on MySQL a trigger refuses `DISCARDED` first).

The MySQL half needs `BILLING_TEST_DATABASE_URL` pointing at a database that may be wiped; it
skips without it — **a skip is not a pass**, CI sets it and treats a skip as a failure. Emails
are on the reserved `example.com` domain; placeholder uuids are fresh `uuid4()` values.
"""

from __future__ import annotations

import copy
import dataclasses
import datetime as dt
import itertools
import json
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, delete, event, select, text
from sqlalchemy.exc import IntegrityError, MultipleResultsFound, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.core.database import create_session_factory
from app.core.emails import normalise_email
from app.models.auth import AuditAction, AuditLog, User, UserRole, UserStatus
from app.models.base import Base
from app.models.tax_policy import (
    TaxJurisdiction,
    TaxPolicyLock,
    TaxPolicyStatus,
    TaxPolicyType,
    TaxPolicyVersion,
)
from app.repositories import tax_policy as policies
from app.repositories.tax_policy import (
    DocumentNotice,
    TaxTreatment,
    check_rate_consistency,
    parse_treatment,
    resolve_tax_policy,
)
from app.services import tax_policy
from app.services.auth import RequestContext
from app.services.tax_policy import (
    TaxPolicyAlreadyExists,
    TaxPolicyApproverInvalid,
    TaxPolicyRecheckFailed,
    publish_initial_tax_policy,
)

TEST_EMAIL_DOMAIN = "@tax-policy-test.example.com"
CONTEXT = RequestContext(ip_address=None, user_agent="cli")
ADR_0008 = Path(__file__).resolve().parents[2] / "docs" / "adr" / "ADR-0008-sst-tax-treatment.md"

# Design §7: the frozen clock; the row and audits carry it cut to a whole second.
FROZEN = dt.datetime(2026, 10, 18, 3, 4, 5, 678_000)
STAMP = dt.datetime(2026, 10, 18, 3, 4, 5)
START = dt.datetime(2026, 10, 16, 16, 0, 0)
SECOND = dt.timedelta(seconds=1)

# Design §2: written out here, not taken from the service, so a drift in the constants fails.
EXPECTED_TREATMENT: dict[str, Any] = {
    "schema_version": 1,
    "sst_registered": False,
    "tax_charged": False,
    "document_notice": {
        "zh": "价格为最终价，已包含所有费用，不另收任何税费",
        "en": "Prices are final and include all charges. No tax is charged separately.",
    },
}
EXPECTED_SOURCE_REFERENCE = (
    "ADR-0008 §5 (docs/adr/ADR-0008-sst-tax-treatment.md), decided by the decision maker"
    " on 2026-10-17; no tax adviser consulted"
)

_EMAIL_NUMBERS = itertools.count(1)
# Connection housekeeping a driver may run; not statements of the service.
_HOUSEKEEPING = ("SET", "SHOW", "SELECT @@")


def frozen_clock() -> dt.datetime:
    return FROZEN


def treatment(**changes: object) -> dict[str, Any]:
    """A deep copy of the design's object with top-level keys changed."""
    value = copy.deepcopy(EXPECTED_TREATMENT)
    value.update(changes)
    return value


# --- helpers ----------------------------------------------------------------------


def make_user(
    factory: sessionmaker[Session],
    *,
    role: UserRole = UserRole.ADMIN,
    status: UserStatus = UserStatus.ACTIVE,
) -> User:
    """A committed user, detached with its columns loaded."""
    with factory() as session:
        user = User(
            email=f"admin-{next(_EMAIL_NUMBERS)}{TEST_EMAIL_DOMAIN}",
            password_hash="not-a-real-hash",
            role=role,
            status=status,
            created_at=STAMP,
            updated_at=STAMP,
        )
        session.add(user)
        session.commit()
        return user


def publish(
    factory: sessionmaker[Session],
    email: str,
    clock: Callable[[], dt.datetime] = frozen_clock,
) -> str:
    return publish_initial_tax_policy(factory, approver_email=email, context=CONTEXT, clock=clock)


_COLUMNS = (
    "id",
    "public_id",
    "jurisdiction",
    "policy_type",
    "rate",
    "treatment_json",
    "status",
    "effective_from",
    "effective_to",
    "source_reference",
    "created_by",
    "approved_by",
    "approved_at",
    "created_at",
    "updated_at",
)


def versions(factory: sessionmaker[Session]) -> list[TaxPolicyVersion]:
    with factory() as session:
        statement = select(TaxPolicyVersion).order_by(TaxPolicyVersion.id)
        return list(session.execute(statement).scalars().all())


def audits(factory: sessionmaker[Session]) -> list[AuditLog]:
    with factory() as session:
        return list(session.execute(select(AuditLog).order_by(AuditLog.id)).scalars().all())


def snapshot(factory: sessionmaker[Session]) -> tuple[list[tuple[object, ...]], list[object]]:
    """Every version row column by column, and every audit row."""
    rows = [tuple(getattr(row, column) for column in _COLUMNS) for row in versions(factory)]
    logs = [
        (row.action, row.entity_type, row.entity_id, row.before_state, row.after_state)
        for row in audits(factory)
    ]
    return rows, logs


def add_version(
    factory: sessionmaker[Session],
    creator: User,
    *,
    status: TaxPolicyStatus,
    effective_from: dt.datetime | None = None,
    effective_to: dt.datetime | None = None,
    raw_treatment: dict[str, Any] | None = None,
    rate: Decimal | None = None,
) -> int:
    """A version written straight through the ORM, around the service (no triggers on SQLite)."""
    published = status is not TaxPolicyStatus.DRAFT
    with factory() as session:
        row = TaxPolicyVersion(
            public_id=str(uuid.uuid4()),
            jurisdiction=TaxJurisdiction.MY,
            policy_type=TaxPolicyType.SST,
            rate=rate,
            treatment_json=raw_treatment if raw_treatment is not None else treatment(),
            status=status,
            effective_from=effective_from,
            effective_to=effective_to,
            source_reference="fictional policy for a service test",
            created_by=creator.id,
            approved_by=creator.id if published else None,
            approved_at=STAMP if published else None,
            created_at=STAMP,
            updated_at=STAMP,
        )
        session.add(row)
        session.commit()
        return row.id


def resolve(
    factory: sessionmaker[Session],
    moment: dt.datetime,
    jurisdiction: str = "MY",
    policy_type: str = "SST",
) -> policies.ResolvedTaxPolicy | None:
    with factory() as session:
        return resolve_tax_policy(session, jurisdiction, policy_type, moment)


def lock_clause(engine: Engine, *, read: bool) -> str:
    """The locking clause this dialect renders after the table (empty on SQLite).

    Compiled from the dialect itself, so the assertions carry no dialect literal (CLAUDE.md
    「写后端测试的约定」).
    """
    compiled = str(
        select(TaxPolicyLock.id).with_for_update(read=read).compile(dialect=engine.dialect)
    )
    return compiled[compiled.rindex("tax_policy_locks") + len("tax_policy_locks") :].strip()


# --- fixtures ---------------------------------------------------------------------


@pytest.fixture
def sqlite_factory() -> Iterator[sessionmaker[Session]]:
    yield from _sqlite_factory()


@pytest.fixture
def mysql_factory(monkeypatch) -> Iterator[sessionmaker[Session]]:
    yield from _mysql_factory(monkeypatch)


@pytest.fixture(params=["sqlite", "mysql"])
def factory(request, monkeypatch) -> Iterator[sessionmaker[Session]]:
    if request.param == "mysql":
        yield from _mysql_factory(monkeypatch)
        return
    yield from _sqlite_factory()


def _sqlite_factory() -> Iterator[sessionmaker[Session]]:
    # `create_all` also writes the one row of `tax_policy_locks` (app/models/tax_policy.py).
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


def _mysql_factory(monkeypatch) -> Iterator[sessionmaker[Session]]:
    url = os.environ.get("BILLING_TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("BILLING_TEST_DATABASE_URL is not set; the MySQL half needs a real MySQL")
    # Tables through alembic: triggers, the generated column, the unique index, checks and the
    # lock row are the ones migration 0022 builds.
    from app.core.config import get_settings

    monkeypatch.setenv("BILLING_DATABASE_URL", url)
    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")
    get_settings.cache_clear()

    engine = create_engine(url, pool_size=6, max_overflow=2)
    clean(engine)
    try:
        yield create_session_factory(engine)
    finally:
        clean(engine)
        engine.dispose()


def clean(engine: Engine) -> None:
    """⚠️ The database is shared: wipe before and after every case.

    The version triggers refuse DELETE, so TRUNCATE (DDL, no triggers) with foreign key checks
    off. `tax_policy_locks` is never touched: its one row belongs to the migration. The audit
    table is append-only and truncated the same way.
    """
    with engine.begin() as connection:
        connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 0")
        try:
            connection.exec_driver_sql("TRUNCATE TABLE tax_policy_versions")
        finally:
            connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 1")
    with engine.begin() as connection:
        connection.exec_driver_sql("TRUNCATE TABLE audit_logs")
    with engine.begin() as connection:
        connection.execute(delete(User).where(User.email.like(f"%{TEST_EMAIL_DOMAIN}")))


# --- publishing: the normal path and the audit ---------------------------------------


def test_publish_writes_one_published_policy(factory) -> None:
    admin = make_user(factory)

    public_id = publish(factory, admin.email)

    assert str(uuid.UUID(public_id)) == public_id
    (row,) = versions(factory)
    assert row.public_id == public_id
    assert row.jurisdiction == "MY"
    assert row.policy_type == "SST"
    assert row.rate is None
    assert row.treatment_json == EXPECTED_TREATMENT
    assert row.status == "PUBLISHED"
    assert (row.effective_from, row.effective_to) == (START, None)
    assert row.source_reference == EXPECTED_SOURCE_REFERENCE
    assert row.created_by == row.approved_by == admin.id
    assert row.created_at == row.updated_at == row.approved_at == STAMP
    if factory.kw["bind"].dialect.name == "mysql":
        assert row.open_slot == 1


def test_publish_writes_the_two_audit_rows(factory) -> None:
    admin = make_user(factory)

    public_id = publish(factory, admin.email)

    create, publish_row = audits(factory)
    assert [create.action, publish_row.action] == [
        AuditAction.TAX_POLICY_CREATE,
        AuditAction.TAX_POLICY_PUBLISH,
    ]
    for row in (create, publish_row):
        assert row.entity_type == "tax_policy_version"
        assert row.entity_id == public_id
        assert row.actor_user_id == admin.id
        assert row.actor_role == "ADMIN"
        assert row.ip_address is None
        assert row.user_agent == "cli"
        assert row.reason == "INITIAL_POLICY"
        assert row.created_at == STAMP
    assert create.before_state is None
    assert json.loads(create.after_state or "") == {
        "jurisdiction": "MY",
        "policy_type": "SST",
        "rate": None,
        "treatment": EXPECTED_TREATMENT,
        "source_reference": EXPECTED_SOURCE_REFERENCE,
        "status": "DRAFT",
        "created_by_email": admin.email,
    }
    assert json.loads(publish_row.before_state or "") == {"status": "DRAFT"}
    assert json.loads(publish_row.after_state or "") == {
        "status": "PUBLISHED",
        "effective_from": "2026-10-16T16:00:00",
        "effective_to": None,
        "approved_by_email": admin.email,
        "approved_at": "2026-10-18T03:04:05",
    }


# --- publishing: refusals -------------------------------------------------------------


def test_publishing_again_is_refused_and_writes_nothing(factory) -> None:
    """Design §4: a second run is an error no-op, the same approver or another one."""
    admin = make_user(factory)
    other = make_user(factory)
    publish(factory, admin.email)
    before = snapshot(factory)

    for email in (admin.email, other.email):
        with pytest.raises(TaxPolicyAlreadyExists):
            publish(factory, email, clock=lambda: FROZEN + dt.timedelta(days=1))

    assert snapshot(factory) == before
    assert len(before[0]) == 1
    assert len(before[1]) == 2


def test_an_unexpected_draft_already_there_is_refused(factory) -> None:
    admin = make_user(factory)
    draft_id = add_version(factory, admin, status=TaxPolicyStatus.DRAFT)

    with pytest.raises(TaxPolicyAlreadyExists):
        publish(factory, admin.email)

    (row,) = versions(factory)
    assert (row.id, row.status) == (draft_id, "DRAFT")
    assert audits(factory) == []


@pytest.mark.parametrize("approver", ["no-account", "customer", "disabled-admin"])
def test_an_invalid_approver_is_refused_and_writes_nothing(factory, approver: str) -> None:
    if approver == "no-account":
        email = f"nobody{TEST_EMAIL_DOMAIN}"
    elif approver == "customer":
        email = make_user(factory, role=UserRole.CUSTOMER).email
    else:
        email = make_user(factory, status=UserStatus.DISABLED).email

    with pytest.raises(TaxPolicyApproverInvalid):
        publish(factory, email)

    assert versions(factory) == []
    assert audits(factory) == []


def test_a_normalised_email_with_capitals_and_spaces_finds_the_admin(factory) -> None:
    admin = make_user(factory)

    public_id = publish(factory, normalise_email(f"  {admin.email.upper()} "))

    (row,) = versions(factory)
    assert row.public_id == public_id
    assert row.approved_by == admin.id


def test_a_broken_initial_treatment_fails_before_connecting(monkeypatch) -> None:
    """Design §5: a code defect in the constant fails outside the transaction, no database."""

    def no_database() -> Session:
        raise AssertionError("the service connected to the database")

    monkeypatch.setattr(tax_policy, "INITIAL_TREATMENT", treatment(tax_charged=True))

    with pytest.raises(ValueError):
        publish_initial_tax_policy(
            no_database,  # type: ignore[arg-type]
            approver_email=f"admin{TEST_EMAIL_DOMAIN}",
            context=CONTEXT,
            clock=frozen_clock,
        )


# --- publishing: a write failing half way (INV-13) ------------------------------------


class InjectedFailure(Exception):
    pass


def test_a_failing_publish_audit_rolls_everything_back(factory, monkeypatch) -> None:
    admin = make_user(factory)
    armed = [True]
    real = tax_policy.record_audit

    def flaky(session: Session, **options: Any) -> None:
        if armed[0] and options["action"] is AuditAction.TAX_POLICY_PUBLISH:
            raise InjectedFailure("the publish audit failed")
        real(session, **options)

    monkeypatch.setattr(tax_policy, "record_audit", flaky)

    with pytest.raises(InjectedFailure):
        publish(factory, admin.email)
    assert versions(factory) == []
    assert audits(factory) == []

    armed[0] = False
    publish(factory, admin.email)
    assert len(versions(factory)) == 1
    assert len(audits(factory)) == 2


def test_a_failing_recheck_rolls_everything_back(factory, monkeypatch) -> None:
    admin = make_user(factory)
    armed = [True]
    real = policies.period_versions

    def flaky(*args: Any, **options: Any) -> list[TaxPolicyVersion]:
        found = real(*args, **options)
        return [] if armed[0] else found

    monkeypatch.setattr(policies, "period_versions", flaky)

    with pytest.raises(TaxPolicyRecheckFailed):
        publish(factory, admin.email)
    assert versions(factory) == []
    assert audits(factory) == []

    armed[0] = False
    publish(factory, admin.email)
    assert len(versions(factory)) == 1
    assert len(audits(factory)) == 2


def test_a_lost_commit_acknowledgement_is_settled_by_running_again(factory, monkeypatch) -> None:
    """Design §5 (v2): COMMIT went through but the reply was lost; the rerun reports it."""
    admin = make_user(factory)
    real = Session.commit
    armed = [True]

    def commit_then_lose_the_reply(self: Session) -> None:
        real(self)
        if armed[0]:
            armed[0] = False
            raise OperationalError("COMMIT", {}, Exception("connection lost after commit"))

    monkeypatch.setattr(Session, "commit", commit_then_lose_the_reply)

    with pytest.raises(OperationalError):
        publish(factory, admin.email)
    after_first = snapshot(factory)
    assert len(after_first[0]) == 1
    assert len(after_first[1]) == 2

    with pytest.raises(TaxPolicyAlreadyExists):
        publish(factory, admin.email)
    assert snapshot(factory) == after_first


# --- publishing: the lock comes first -------------------------------------------------


def test_the_lock_is_the_first_statement_and_every_later_read_is_locked(factory) -> None:
    """Design §2 ①–④: lock first, then locking reads only; the clock is read after them."""
    admin = make_user(factory)
    engine = factory.kw["bind"]
    update_clause = lock_clause(engine, read=False)
    share_clause = lock_clause(engine, read=True)
    events: list[str] = []

    def clock() -> dt.datetime:
        events.append("clock")
        return FROZEN

    def capture(_connection, _cursor, statement: str, *_args: object) -> None:
        if not statement.lstrip().upper().startswith(_HOUSEKEEPING):
            events.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        publish(factory, admin.email, clock=clock)
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert "tax_policy_locks" in events[0]
    assert update_clause in events[0]
    assert events.count("clock") == 1
    reads = [
        statement
        for statement in events
        if statement.lstrip().upper().startswith("SELECT")
        and ("tax_policy" in statement or "users" in statement)
    ]
    assert len(reads) >= 4  # lock, any row, approver, recheck
    for statement in reads[1:]:
        assert share_clause in statement or update_clause in statement
    clock_at = events.index("clock")
    assert any("tax_policy_versions" in statement for statement in events[1:clock_at])
    assert any("users" in statement for statement in events[1:clock_at])
    assert all(
        not statement.lstrip().upper().startswith(("INSERT", "UPDATE"))
        for statement in events[:clock_at]
    )


def test_concurrent_publishes_let_exactly_one_win(mysql_factory) -> None:
    """Design §7「并发」: two threads, two connections, released together."""
    admin = make_user(mysql_factory)
    barrier = threading.Barrier(2)
    results: list[object] = []
    guard = threading.Lock()

    def run() -> None:
        barrier.wait()
        try:
            outcome: object = publish(mysql_factory, admin.email)
        except Exception as error:  # collected and asserted below
            outcome = error
        with guard:
            results.append(outcome)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert len(results) == 2
    winners = [result for result in results if isinstance(result, str)]
    losers = [result for result in results if isinstance(result, TaxPolicyAlreadyExists)]
    assert len(winners) == 1
    assert len(losers) == 1
    (row,) = versions(mysql_factory)
    assert row.public_id == winners[0]
    assert len(audits(mysql_factory)) == 2


def test_the_read_after_the_lock_sees_what_the_holder_committed(mysql_factory) -> None:
    """Design §7「锁在前」: B waits for A's lock, then its locking read sees A's draft."""
    admin = make_user(mysql_factory)
    results: list[object] = []

    def run() -> None:
        try:
            outcome: object = publish(mysql_factory, admin.email)
        except Exception as error:  # asserted below
            outcome = error
        results.append(outcome)

    waiter = threading.Thread(target=run)
    holder = mysql_factory()
    try:
        policies.lock_tax_policies(holder)
        policies.insert_draft(
            holder,
            jurisdiction=TaxJurisdiction.MY,
            policy_type=TaxPolicyType.SST,
            rate=None,
            treatment=parse_treatment(treatment()),
            source_reference="fictional draft held under the lock",
            created_by=admin.id,
            now=STAMP,
        )
        waiter.start()
        time.sleep(1)
        assert waiter.is_alive(), "B did not wait for the lock"
        holder.commit()
    finally:
        holder.close()
        if waiter.is_alive():
            waiter.join(timeout=60)

    assert len(results) == 1
    assert isinstance(results[0], TaxPolicyAlreadyExists)
    (row,) = versions(mysql_factory)
    assert row.status == "DRAFT"
    assert audits(mysql_factory) == []


# --- treatment_json (unit) ----------------------------------------------------------


def test_the_design_object_parses() -> None:
    parsed = parse_treatment(treatment())

    assert parsed == TaxTreatment(
        schema_version=1,
        sst_registered=False,
        tax_charged=False,
        document_notice=DocumentNotice(
            zh=EXPECTED_TREATMENT["document_notice"]["zh"],
            en=EXPECTED_TREATMENT["document_notice"]["en"],
        ),
    )
    assert policies.treatment_json(parsed) == EXPECTED_TREATMENT


def _notice(**changes: object) -> dict[str, Any]:
    value = treatment()
    value["document_notice"].update(changes)
    return value


def _without(key: str) -> dict[str, Any]:
    value = treatment()
    del value[key]
    return value


def _notice_without(key: str) -> dict[str, Any]:
    value = treatment()
    del value["document_notice"][key]
    return value


ZH = EXPECTED_TREATMENT["document_notice"]["zh"]
INVALID_TREATMENTS: dict[str, object] = {
    "not-an-object": [treatment()],
    "missing-tax_charged": _without("tax_charged"),
    "extra-key": treatment(extra=True),
    "schema_version-2": treatment(schema_version=2),
    "schema_version-true": treatment(schema_version=True),
    "schema_version-string": treatment(schema_version="1"),
    "sst_registered-string": treatment(sst_registered="false"),
    "sst_registered-zero": treatment(sst_registered=0),
    "notice-not-an-object": treatment(document_notice=ZH),
    "notice-missing-en": _notice_without("en"),
    "notice-extra-ms": _notice(ms="Harga adalah muktamad."),
    "zh-empty": _notice(zh=""),
    "zh-padded": _notice(zh=f" {ZH}"),
    "zh-newline": _notice(zh=f"{ZH}\n{ZH}"),
    "zh-256-characters": _notice(zh="价" * 256),
    "zh-not-a-string": _notice(zh=1),
    "unregistered-but-charging": treatment(tax_charged=True),
}


@pytest.mark.parametrize("case", sorted(INVALID_TREATMENTS))
def test_an_invalid_treatment_is_rejected(case: str) -> None:
    with pytest.raises(ValueError):
        parse_treatment(INVALID_TREATMENTS[case])


def test_a_notice_of_255_characters_is_accepted() -> None:
    parsed = parse_treatment(_notice(zh="价" * 255))

    assert len(parsed.document_notice.zh) == 255


# --- rate and tax_charged (unit) -----------------------------------------------------


def _flags(*, registered: bool, charged: bool) -> TaxTreatment:
    return parse_treatment(treatment(sst_registered=registered, tax_charged=charged))


def test_no_tax_and_no_rate_are_consistent() -> None:
    check_rate_consistency(None, _flags(registered=False, charged=False))
    check_rate_consistency(Decimal("0.08"), _flags(registered=True, charged=True))


@pytest.mark.parametrize(
    ("rate", "registered", "charged"),
    [(Decimal("0.08"), False, False), (None, True, True)],
)
def test_a_rate_that_disagrees_with_tax_charged_is_rejected(
    rate: Decimal | None, registered: bool, charged: bool
) -> None:
    with pytest.raises(ValueError):
        check_rate_consistency(rate, _flags(registered=registered, charged=charged))


def test_charging_while_unregistered_is_rejected_by_the_rate_check_too() -> None:
    # Built around `parse_treatment`, which would refuse it already.
    contradictory = TaxTreatment(
        schema_version=1,
        sst_registered=False,
        tax_charged=True,
        document_notice=DocumentNotice(zh=ZH, en="Fictional."),
    )

    with pytest.raises(ValueError):
        check_rate_consistency(Decimal("0.08"), contradictory)


# --- the constants (unit) -------------------------------------------------------------


def test_the_notice_is_word_for_word_in_adr_0008() -> None:
    adr = ADR_0008.read_text(encoding="utf-8")
    notice = tax_policy.INITIAL_TREATMENT["document_notice"]

    assert notice["zh"] in adr
    assert notice["en"] in adr


def test_the_start_is_midnight_in_kuala_lumpur() -> None:
    kuala_lumpur = dt.timezone(dt.timedelta(hours=8))
    midnight = dt.datetime(2026, 10, 17, tzinfo=kuala_lumpur)

    assert midnight.astimezone(dt.UTC).replace(tzinfo=None) == tax_policy.INITIAL_EFFECTIVE_FROM
    assert tax_policy.INITIAL_EFFECTIVE_FROM == START


def test_the_initial_policy_is_consistent() -> None:
    parsed = parse_treatment(tax_policy.INITIAL_TREATMENT)

    check_rate_consistency(tax_policy.INITIAL_RATE, parsed)
    assert tax_policy.INITIAL_TREATMENT == EXPECTED_TREATMENT
    assert tax_policy.INITIAL_SOURCE_REFERENCE == EXPECTED_SOURCE_REFERENCE


# --- resolve_tax_policy ---------------------------------------------------------------


def test_resolve_at_the_boundaries(factory) -> None:
    admin = make_user(factory)
    public_id = publish(factory, admin.email)
    (row,) = versions(factory)

    assert resolve(factory, START - SECOND) is None
    resolved = resolve(factory, START)
    assert resolve(factory, dt.datetime(2099, 1, 1)) == resolved
    assert resolve(factory, START, "SG", "SST") is None
    assert resolve(factory, START, "MY", "GST") is None

    assert resolved is not None
    assert resolved.version_id == row.id
    assert resolved.public_id == public_id
    assert (resolved.jurisdiction, resolved.policy_type) == ("MY", "SST")
    assert resolved.rate is None
    assert resolved.treatment == parse_treatment(EXPECTED_TREATMENT)
    assert (resolved.effective_from, resolved.effective_to) == (START, None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(resolved, "rate", Decimal("0.08"))  # noqa: B010 - the point is the assignment
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(resolved.treatment.document_notice, "zh", "changed")  # noqa: B010


def test_resolve_ignores_a_draft(factory) -> None:
    admin = make_user(factory)
    add_version(factory, admin, status=TaxPolicyStatus.DRAFT)

    assert resolve(factory, START) is None
    assert resolve(factory, dt.datetime(2099, 1, 1)) is None


def test_resolve_a_retired_version_and_an_empty_period(sqlite_factory) -> None:
    admin = make_user(sqlite_factory)
    d1, d2 = START, START + dt.timedelta(days=30)
    retired = add_version(
        sqlite_factory,
        admin,
        status=TaxPolicyStatus.RETIRED,
        effective_from=d1,
        effective_to=d2,
    )
    empty_at = START + dt.timedelta(days=60)
    add_version(
        sqlite_factory,
        admin,
        status=TaxPolicyStatus.RETIRED,
        effective_from=empty_at,
        effective_to=empty_at,
    )

    first = resolve(sqlite_factory, d1)
    last = resolve(sqlite_factory, d2 - SECOND)
    assert first is not None and first.version_id == retired
    assert last is not None and last.version_id == retired
    assert first.effective_to == d2
    assert resolve(sqlite_factory, d2) is None
    assert resolve(sqlite_factory, empty_at) is None


def test_resolve_refuses_a_published_row_with_a_broken_treatment(sqlite_factory) -> None:
    admin = make_user(sqlite_factory)
    add_version(
        sqlite_factory,
        admin,
        status=TaxPolicyStatus.PUBLISHED,
        effective_from=START,
        raw_treatment=_without("document_notice"),
    )

    with pytest.raises(ValueError):
        resolve(sqlite_factory, START)


INCONSISTENT_ROWS: dict[str, tuple[Decimal | None, bool, bool]] = {
    "charged-without-rate": (None, True, True),
    "rate-without-charging": (Decimal("0.08"), False, False),
    "unregistered-charging-with-rate": (Decimal("0.08"), False, True),
}


@pytest.mark.parametrize("case", sorted(INCONSISTENT_ROWS))
def test_resolve_refuses_an_inconsistent_published_row(sqlite_factory, case: str) -> None:
    rate, registered, charged = INCONSISTENT_ROWS[case]
    admin = make_user(sqlite_factory)
    add_version(
        sqlite_factory,
        admin,
        status=TaxPolicyStatus.PUBLISHED,
        effective_from=START,
        rate=rate,
        raw_treatment=treatment(sst_registered=registered, tax_charged=charged),
    )

    with pytest.raises(ValueError):
        resolve(sqlite_factory, START)


def test_resolve_refuses_overlapping_periods(sqlite_factory) -> None:
    """Corrupted data: a truncated and an open version both cover one moment."""
    admin = make_user(sqlite_factory)
    add_version(
        sqlite_factory,
        admin,
        status=TaxPolicyStatus.PUBLISHED,
        effective_from=START,
        effective_to=START + dt.timedelta(days=30),
    )
    add_version(
        sqlite_factory,
        admin,
        status=TaxPolicyStatus.PUBLISHED,
        effective_from=START + dt.timedelta(days=10),
    )

    with pytest.raises(MultipleResultsFound):
        resolve(sqlite_factory, START + dt.timedelta(days=20))


# --- the status CHECK (SQLite) --------------------------------------------------------


_RAW_INSERT = text(
    "INSERT INTO tax_policy_versions"
    " (public_id, jurisdiction, policy_type, treatment_json, status, source_reference,"
    " created_by, created_at, updated_at)"
    " VALUES (:public_id, 'MY', 'SST', :treatment, :status, 'fictional', :created_by,"
    " :stamp, :stamp)"
)


def test_the_status_check_refuses_discarded(sqlite_factory) -> None:
    """Design §7 (v3): around the enum, with text SQL; a DRAFT through the same SQL passes."""
    admin = make_user(sqlite_factory)

    def insert(status: str) -> None:
        with sqlite_factory() as session:
            session.execute(
                _RAW_INSERT,
                {
                    "public_id": str(uuid.uuid4()),
                    "treatment": json.dumps(EXPECTED_TREATMENT),
                    "status": status,
                    "created_by": admin.id,
                    "stamp": STAMP.isoformat(sep=" "),
                },
            )
            session.commit()

    with pytest.raises(IntegrityError):
        insert("DISCARDED")
    insert("DRAFT")

    assert [row.status for row in versions(sqlite_factory)] == ["DRAFT"]
