"""`python -m app.cli publish-initial-tax-policy` (design gate #332 v3 §2「接口：CLI」, §5, §7).

Every case runs through `cli.main([...])` with `capsys`, against a SQLite file database built
by `create_all` and against MySQL built by `alembic upgrade head` (`factory` fixture). The CLI
builds its own engine from `BILLING_DATABASE_URL`, so the fixture points that variable at the
same database and clears the settings cache around each case.

Groups: the normal path, running it a second time, a commit whose acknowledgement is lost, the
refusals (a malformed email never reaches the database), and the command never prompting.

The MySQL half needs `BILLING_TEST_DATABASE_URL` pointing at a database that may be wiped; it
skips without it — **a skip is not a pass**, CI sets it and treats a skip as a failure. Emails
are on the reserved `example.com` domain.
"""

from __future__ import annotations

import builtins
import datetime as dt
import getpass
import io
import itertools
import os
import sys
import uuid
from collections.abc import Iterator

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, delete, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app import cli
from app.core.config import get_settings
from app.core.database import create_session_factory
from app.models.auth import AuditAction, AuditLog, User, UserRole, UserStatus
from app.models.base import Base
from app.models.tax_policy import TaxPolicyVersion

TEST_EMAIL_DOMAIN = "@tax-policy-test.example.com"
COMMAND = "publish-initial-tax-policy"
STAMP = dt.datetime(2026, 10, 18, 3, 4, 5)

# Design §2: the fixed sentences, written out here so a drift in the CLI fails.
SUCCESS = "Published tax policy MY/SST effective from 2026-10-16T16:00:00 UTC.\n"
ALREADY_EXISTS = "A tax policy version already exists; nothing was written.\n"
COMMIT_UNKNOWN = (
    "If the error happened at commit, the policy may already be published; "
    "run the same command again to find out."
)

_EMAIL_NUMBERS = itertools.count(1)


# --- helpers ----------------------------------------------------------------------


def make_user(
    factory: sessionmaker[Session],
    *,
    email: str | None = None,
    role: UserRole = UserRole.ADMIN,
    status: UserStatus = UserStatus.ACTIVE,
) -> User:
    """A committed user, detached with its columns loaded."""
    with factory() as session:
        user = User(
            email=email or f"admin-{next(_EMAIL_NUMBERS)}{TEST_EMAIL_DOMAIN}",
            password_hash="not-a-real-hash",
            role=role,
            status=status,
            created_at=STAMP,
            updated_at=STAMP,
        )
        session.add(user)
        session.commit()
        return user


def run(email: str) -> int:
    return cli.main([COMMAND, "--approved-by-email", email])


def versions(factory: sessionmaker[Session]) -> list[TaxPolicyVersion]:
    with factory() as session:
        statement = select(TaxPolicyVersion).order_by(TaxPolicyVersion.id)
        return list(session.execute(statement).scalars().all())


def audits(factory: sessionmaker[Session]) -> list[AuditLog]:
    with factory() as session:
        return list(session.execute(select(AuditLog).order_by(AuditLog.id)).scalars().all())


def snapshot(factory: sessionmaker[Session]) -> tuple[list[tuple[object, ...]], list[object]]:
    rows = [
        (row.id, row.public_id, row.status, row.approved_by, row.approved_at, row.updated_at)
        for row in versions(factory)
    ]
    logs = [(row.id, row.action, row.entity_id) for row in audits(factory)]
    return rows, logs


def assert_nothing_written(factory: sessionmaker[Session]) -> None:
    assert versions(factory) == []
    assert audits(factory) == []


# --- fixtures ---------------------------------------------------------------------


@pytest.fixture(params=["sqlite", "mysql"])
def factory(request, monkeypatch, tmp_path) -> Iterator[sessionmaker[Session]]:
    try:
        if request.param == "mysql":
            yield from _mysql_factory(monkeypatch)
        else:
            yield from _sqlite_factory(monkeypatch, tmp_path)
    finally:
        get_settings.cache_clear()


def _point_the_cli_at(monkeypatch, url: str) -> None:
    monkeypatch.setenv("BILLING_DATABASE_URL", url)
    get_settings.cache_clear()


def _sqlite_factory(monkeypatch, tmp_path) -> Iterator[sessionmaker[Session]]:
    # A file, not `:memory:`: the CLI opens its own engine and must see the same database.
    # `create_all` also writes the one row of `tax_policy_locks` (app/models/tax_policy.py).
    url = f"sqlite+pysqlite:///{(tmp_path / 'tax-policy-cli.db').as_posix()}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    _point_the_cli_at(monkeypatch, url)
    try:
        yield create_session_factory(engine)
    finally:
        engine.dispose()


def _mysql_factory(monkeypatch) -> Iterator[sessionmaker[Session]]:
    url = os.environ.get("BILLING_TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("BILLING_TEST_DATABASE_URL is not set; the MySQL half needs a real MySQL")
    # Tables through alembic: triggers, checks and the lock row are the ones 0022 builds.
    _point_the_cli_at(monkeypatch, url)
    command.upgrade(Config("alembic.ini"), "head")
    get_settings.cache_clear()

    engine = create_engine(url)
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


class _UnreadableStdin(io.StringIO):
    """An empty standard input that fails the case if anything reads it."""

    def read(self, *_args: object) -> str:
        raise AssertionError("the CLI read standard input")

    def readline(self, *_args: object) -> str:
        raise AssertionError("the CLI read standard input")


@pytest.fixture
def never_prompts(monkeypatch) -> None:
    def prompted(*_args: object, **_options: object) -> str:
        raise AssertionError("the CLI prompted for input")

    monkeypatch.setattr(builtins, "input", prompted)
    monkeypatch.setattr(getpass, "getpass", prompted)
    monkeypatch.setattr(sys, "stdin", _UnreadableStdin(""))


# --- the normal path and a second run -------------------------------------------------


def test_publishing_prints_only_the_public_id(factory, capsys) -> None:
    """Design §7「CLI 正常路径」: capitals and spaces in the email are normalised first."""
    admin = make_user(factory, email=f"admin{TEST_EMAIL_DOMAIN}")

    assert run(" Admin@Tax-Policy-Test.example.com ") == 0

    (row,) = versions(factory)
    captured = capsys.readouterr()
    assert captured.out == f"{row.public_id}\n"
    assert str(uuid.UUID(row.public_id)) == row.public_id
    assert captured.err == SUCCESS
    assert row.status == "PUBLISHED"
    assert row.created_by == row.approved_by == admin.id
    create, publish = audits(factory)
    assert [create.action, publish.action] == [
        AuditAction.TAX_POLICY_CREATE,
        AuditAction.TAX_POLICY_PUBLISH,
    ]
    assert {log.user_agent for log in (create, publish)} == {"cli"}
    assert {log.ip_address for log in (create, publish)} == {None}
    assert {log.entity_id for log in (create, publish)} == {row.public_id}


def test_running_it_again_is_refused_and_writes_nothing(factory, capsys) -> None:
    admin = make_user(factory)
    assert run(admin.email) == 0
    capsys.readouterr()
    before = snapshot(factory)

    assert run(admin.email) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ALREADY_EXISTS
    assert snapshot(factory) == before
    assert (len(before[0]), len(before[1])) == (1, 2)


def test_another_approver_on_a_second_run_is_refused_too(factory, capsys) -> None:
    """Design §4: changing the approver is not a way to republish."""
    assert run(make_user(factory).email) == 0
    capsys.readouterr()
    before = snapshot(factory)

    assert run(make_user(factory).email) == 1

    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", ALREADY_EXISTS)
    assert snapshot(factory) == before


# --- a commit whose acknowledgement is lost (design §5, v2) ------------------------------


def test_a_lost_commit_acknowledgement_is_settled_by_running_again(
    factory, capsys, monkeypatch
) -> None:
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
        run(admin.email)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert COMMIT_UNKNOWN in captured.err
    after_first = snapshot(factory)
    assert (len(after_first[0]), len(after_first[1])) == (1, 2)

    assert run(admin.email) == 1
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", ALREADY_EXISTS)
    assert snapshot(factory) == after_first


# --- refusals ---------------------------------------------------------------------------


def test_a_malformed_email_exits_two_without_connecting(factory, capsys, monkeypatch) -> None:
    def no_database(*_args: object, **_options: object) -> None:
        raise AssertionError("the CLI built a database engine")

    monkeypatch.setattr(cli, "create_database_engine", no_database)
    monkeypatch.setattr(cli, "create_session_factory", no_database)

    assert run("not-an-email") == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() != ""
    assert_nothing_written(factory)


@pytest.mark.parametrize("approver", ["no-account", "customer", "disabled-admin"])
def test_an_invalid_approver_exits_one_and_writes_nothing(factory, capsys, approver: str) -> None:
    if approver == "no-account":
        email = f"nobody{TEST_EMAIL_DOMAIN}"
    elif approver == "customer":
        email = make_user(factory, role=UserRole.CUSTOMER).email
    else:
        email = make_user(factory, status=UserStatus.DISABLED).email

    assert run(email) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"No active ADMIN account for {email}; nothing was written.\n"
    assert_nothing_written(factory)


def test_a_missing_email_is_an_argparse_error(factory, capsys) -> None:
    make_user(factory)

    with pytest.raises(SystemExit) as exited:
        cli.main([COMMAND])

    assert exited.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--approved-by-email" in captured.err
    assert_nothing_written(factory)


# --- never prompting -------------------------------------------------------------------


def test_it_never_prompts_on_any_path(factory, capsys, never_prompts) -> None:
    """Design §2: no `input()`, no `getpass`, no read of standard input (`exec -T` works)."""
    assert run("not-an-email") == 2
    assert run(f"nobody{TEST_EMAIL_DOMAIN}") == 1
    assert run(make_user(factory, role=UserRole.CUSTOMER).email) == 1
    assert_nothing_written(factory)

    admin = make_user(factory)
    assert run(admin.email) == 0
    assert run(admin.email) == 1

    (row,) = versions(factory)
    assert capsys.readouterr().out == f"{row.public_id}\n"
