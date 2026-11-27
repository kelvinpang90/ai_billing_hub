"""The daily retention purge task (设计闸门 #833 v7 §2「任务与配置」§7; AIH-TASK-264).

覆盖设计 §7 的「off 模式」（三项都 `off` 时捕获语句，断言一条 SQL 都没有）、`count` 与 `delete`
模式把配置原样交给 `run_purge`、「并发：两个实例」（MySQL：另一个连接先拿住命名锁，任务记一行
INFO 后返回 0、不删任何行；锁释放后再跑照常删），以及拿不到锁时那一行 INFO 的内容。Beat 一行在
tests/backend/test_celery.py，配置校验在 tests/backend/test_config.py；清理规则本身在
tests/backend/test_retention_purge.py。

行为用例在 SQLite 与 MySQL 上各跑一遍（`db` 夹具）。MySQL 那一半需要 `BILLING_TEST_DATABASE_URL`
指向一个**可以被清空的**库，没设时 skip —— **skip 不是 pass**，CI 设了它。MySQL 上每个用例前后
清空三张令牌表并删掉本文件邮箱域的用户；审计表只追加、拒绝 DELETE，只看本用例开始之后写入的审计。

时间一律注入：`NOW` 是固定的整秒时刻（另有一个带微秒的 `NOW_WITH_MICROSECONDS` 验截秒）。
"""

from __future__ import annotations

import datetime as dt
import itertools
import logging
import os
import pathlib
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, delete, event, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.core.config import Settings
from app.core.database import create_session_factory
from app.models.auth import (
    AuditAction,
    AuditLog,
    PasswordResetToken,
    RecoveryCode,
    RefreshToken,
    TokenPurpose,
    User,
    UserRole,
    UserStatus,
)
from app.models.base import Base
from app.services.retention_purge import ITEMS, ItemOutcome, PurgeResult
from app.tasks import retention as task

DOMAIN = "@retention-task.example.com"
LOGGER = "app.tasks.retention"

NOW = dt.datetime(2026, 11, 25, 20, 17, 0)
NOW_WITH_MICROSECONDS = NOW.replace(microsecond=654_321)
CUTOFF = NOW - dt.timedelta(days=30)
LONG_AGO = CUTOFF - dt.timedelta(days=5)

_LOCK_SQL = "SELECT GET_LOCK(CONCAT('retention_purge:', DATABASE()), 0)"
_UNLOCK_SQL = "SELECT RELEASE_LOCK(CONCAT('retention_purge:', DATABASE()))"
_IS_FREE_SQL = "SELECT IS_FREE_LOCK(CONCAT('retention_purge:', DATABASE()))"

_hashes = itertools.count(1)


# --- 库 -------------------------------------------------------------------------------


@dataclass
class Db:
    engine: Engine
    factory: sessionmaker[Session]
    # 本用例开始之前审计表里最大的 id：MySQL 上的审计不能删，只看它之后的。
    audit_start: int


def _mysql_url() -> str:
    # 环境变量在夹具里读：xdist 换库名（conftest 的 pytest_configure）之后才有最终值。
    url = os.environ.get("BILLING_TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("BILLING_TEST_DATABASE_URL is not set; the MySQL half needs a real MySQL")
    return url


def _clean(engine: Engine) -> None:
    """MySQL only: the three token tables entirely, then this file's users."""
    with engine.begin() as connection:
        connection.execute(delete(RefreshToken))
        connection.execute(delete(PasswordResetToken))
        connection.execute(delete(RecoveryCode))
        connection.execute(delete(User).where(User.email.like(f"%{DOMAIN}")))


def _audit_start(factory: sessionmaker[Session]) -> int:
    with factory() as session:
        return int(session.execute(select(func.coalesce(func.max(AuditLog.id), 0))).scalar_one())


def _sqlite_db() -> Iterator[Db]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield Db(engine=engine, factory=create_session_factory(engine), audit_start=0)
    finally:
        engine.dispose()


def _mysql_db(monkeypatch: pytest.MonkeyPatch) -> Iterator[Db]:
    url = _mysql_url()
    # 建表走 alembic：审计表的只追加触发器只有迁移会建。
    from app.core.config import get_settings

    monkeypatch.setenv("BILLING_DATABASE_URL", url)
    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")
    get_settings.cache_clear()
    engine = create_engine(url, pool_size=4, max_overflow=4)
    _clean(engine)
    factory = create_session_factory(engine)
    try:
        yield Db(engine=engine, factory=factory, audit_start=_audit_start(factory))
    finally:
        _clean(engine)
        engine.dispose()


@pytest.fixture(params=["sqlite", "mysql"])
def db(request, monkeypatch) -> Iterator[Db]:
    if request.param == "mysql":
        yield from _mysql_db(monkeypatch)
        return
    yield from _sqlite_db()


@pytest.fixture
def mysql_db(monkeypatch) -> Iterator[Db]:
    yield from _mysql_db(monkeypatch)


def wire(monkeypatch: pytest.MonkeyPatch, db: Db | None, **settings: Any) -> None:
    """Point the task at this database and these settings, as a worker would see them."""
    if db is not None:
        monkeypatch.setattr(task, "_session_factory", lambda: db.factory)
    monkeypatch.setattr(task, "get_settings", lambda: Settings(**settings))


@pytest.fixture
def records() -> Iterator[list[logging.LogRecord]]:
    """What the task logs (INFO and up), collected on its own logger."""
    found: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            found.append(record)

    handler = _Collect(level=logging.INFO)
    logger = logging.getLogger(LOGGER)
    previous = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        yield found
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


@pytest.fixture
def statements(db: Db) -> Iterator[list[str]]:
    """Every statement sent on this database's engine while the test runs."""
    seen: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany) -> None:
        seen.append(statement)

    event.listen(db.engine, "before_cursor_execute", _capture)
    try:
        yield seen
    finally:
        event.remove(db.engine, "before_cursor_execute", _capture)


# 设计闸门 #862 v2 §7「R1 用例不受新项干扰」：固定只开 R1 三项，之后注册的项不在这里（缺省 `off`）。
ALL_MODES = {
    "retention_refresh_tokens_mode": "delete",
    "retention_password_reset_tokens_mode": "delete",
    "retention_recovery_codes_mode": "delete",
}


# --- 造数 -----------------------------------------------------------------------------


def _hash() -> str:
    return f"{next(_hashes):064x}"


def seed(db: Db) -> dict[str, int]:
    """One deletable row per item, and one live row per item. Returns the live rows' ids."""
    with db.factory() as session:
        user = User(
            email=f"user{DOMAIN}",
            password_hash="not-a-real-hash",
            role=UserRole.CUSTOMER,
            status=UserStatus.ACTIVE,
            created_at=LONG_AGO,
            updated_at=LONG_AGO,
        )
        session.add(user)
        session.flush()
        dead_family = RefreshToken(
            user_id=user.id,
            family_id=uuid.UUID(int=1).hex,
            token_hash=_hash(),
            issued_at=LONG_AGO - dt.timedelta(hours=12),
            expires_at=LONG_AGO,
            used_at=LONG_AGO,
            password_version=0,
        )
        live_family = RefreshToken(
            user_id=user.id,
            family_id=uuid.UUID(int=2).hex,
            token_hash=_hash(),
            issued_at=NOW - dt.timedelta(hours=1),
            expires_at=NOW + dt.timedelta(hours=11),
            password_version=0,
        )
        old_reset = PasswordResetToken(
            user_id=user.id,
            token_hash=_hash(),
            expires_at=LONG_AGO,
            created_at=LONG_AGO - dt.timedelta(minutes=30),
            purpose=TokenPurpose.PASSWORD_RESET,
        )
        new_reset = PasswordResetToken(
            user_id=user.id,
            token_hash=_hash(),
            expires_at=NOW + dt.timedelta(minutes=30),
            created_at=NOW,
            purpose=TokenPurpose.PASSWORD_RESET,
        )
        used_code = RecoveryCode(
            user_id=user.id,
            code_hash="not-a-real-hash",
            used_at=LONG_AGO,
            created_at=LONG_AGO - dt.timedelta(days=30),
        )
        unused_code = RecoveryCode(
            user_id=user.id,
            code_hash="not-a-real-hash",
            created_at=LONG_AGO - dt.timedelta(days=30),
        )
        session.add_all([dead_family, live_family, old_reset, new_reset, used_code, unused_code])
        session.commit()
        return {
            "refresh_tokens": int(live_family.id),
            "password_reset_tokens": int(new_reset.id),
            "recovery_codes": int(unused_code.id),
        }


def counts(db: Db) -> dict[str, int]:
    with db.factory() as session:
        return {
            name: int(session.execute(select(func.count()).select_from(model)).scalar_one())
            for name, model in (
                ("refresh_tokens", RefreshToken),
                ("password_reset_tokens", PasswordResetToken),
                ("recovery_codes", RecoveryCode),
            )
        }


def purge_audit_count(db: Db) -> int:
    with db.factory() as session:
        return int(
            session.execute(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.id > db.audit_start,
                    AuditLog.action == AuditAction.DATA_RETENTION_PURGE,
                )
            ).scalar_one()
        )


# --- off 模式 -------------------------------------------------------------------------


def test_all_off_returns_without_any_sql(
    db: Db, statements: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """设计 §7「off 模式」：三项都 `off`（缺省）时直接返回，不执行任何 SQL（捕获语句断言）。"""
    seed(db)
    before = counts(db)
    wire(monkeypatch, db)
    statements.clear()

    assert task.purge(now=NOW) == 0

    assert statements == []
    assert counts(db) == before


def test_all_off_does_not_even_build_the_session_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    """三项都 `off` 时不建连接池、不取命名锁、不调 `run_purge`。"""

    def _must_not_be_called(*args: object, **kwargs: object) -> None:
        raise AssertionError("nothing should run while every item is off")

    wire(monkeypatch, None)
    monkeypatch.setattr(task, "_session_factory", _must_not_be_called)
    monkeypatch.setattr(task, "run_purge", _must_not_be_called)

    assert task.purge(now=NOW) == 0


# --- 配置原样交给 run_purge -------------------------------------------------------------


@dataclass
class Call:
    factory: Any
    modes: dict[str, str]
    max_rows_per_item: int
    now: dt.datetime


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[Call]:
    """Replace `run_purge` with a recorder that reports 3 + 4 rows deleted and 5 counted."""
    seen: list[Call] = []

    def _record(factory, *, modes, max_rows_per_item, now) -> PurgeResult:
        seen.append(Call(factory, dict(modes), max_rows_per_item, now))
        return PurgeResult(
            items=(
                ItemOutcome(item="refresh_tokens", mode="delete", cutoff=CUTOFF, rows=3),
                ItemOutcome(item="password_reset_tokens", mode="count", cutoff=CUTOFF, rows=5),
                ItemOutcome(item="recovery_codes", mode="delete", cutoff=CUTOFF, rows=4),
            )
        )

    monkeypatch.setattr(task, "run_purge", _record)
    return seen


@pytest.mark.parametrize(
    ("refresh", "reset", "codes"),
    [
        ("count", "off", "off"),
        ("off", "delete", "off"),
        ("off", "off", "count"),
        ("delete", "count", "delete"),
        ("delete", "delete", "delete"),
        ("count", "count", "count"),
    ],
)
def test_the_modes_and_the_cap_are_passed_to_run_purge(
    db: Db,
    calls: list[Call],
    monkeypatch: pytest.MonkeyPatch,
    refresh: str,
    reset: str,
    codes: str,
) -> None:
    """`count` 与 `delete` 模式：每项的配置按项名交给 `run_purge`，连同每轮上限与截到整秒的 now。

    返回值是删除总数（`PurgeResult.deleted`，只算 `delete` 项），`count` 项的行数不计。
    """
    wire(
        monkeypatch,
        db,
        retention_refresh_tokens_mode=refresh,
        retention_password_reset_tokens_mode=reset,
        retention_recovery_codes_mode=codes,
        retention_domain_outbox_mode="off",
        retention_max_rows_per_item=2_000,
    )

    assert task.purge(now=NOW_WITH_MICROSECONDS) == 7

    assert calls == [
        Call(
            factory=db.factory,
            modes={
                "refresh_tokens": refresh,
                "password_reset_tokens": reset,
                "recovery_codes": codes,
                "domain_outbox": "off",
            },
            max_rows_per_item=2_000,
            now=NOW,
        )
    ]


def test_without_an_injected_now_the_task_uses_utc_now_to_the_second(
    db: Db, calls: list[Call], monkeypatch: pytest.MonkeyPatch
) -> None:
    wire(monkeypatch, db, retention_recovery_codes_mode="count")
    monkeypatch.setattr(task, "utc_now", lambda: NOW_WITH_MICROSECONDS)

    task.purge()

    assert [call.now for call in calls] == [NOW]
    assert calls[0].max_rows_per_item == 50_000


def test_the_mode_keys_follow_the_registered_item_names() -> None:
    """配置键是 `retention_<项名>_mode`：注册表里的每一项在 `Settings` 里都有它的开关。"""
    settings = Settings()

    assert {item.name: getattr(settings, f"retention_{item.name}_mode") for item in ITEMS} == {
        "refresh_tokens": "off",
        "password_reset_tokens": "off",
        "recovery_codes": "off",
        "domain_outbox": "off",
    }


# --- 真跑一轮 ---------------------------------------------------------------------------


def test_count_mode_deletes_nothing_and_returns_zero(
    db: Db, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(db)
    before = counts(db)
    wire(monkeypatch, db, **{name: "count" for name in ALL_MODES})

    assert task.purge(now=NOW) == 0

    assert counts(db) == before
    assert purge_audit_count(db) == 0


def test_delete_mode_deletes_the_due_rows_and_returns_the_total(
    db: Db, statements: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    live = seed(db)
    wire(monkeypatch, db, **ALL_MODES)

    assert task.purge(now=NOW) == 3

    assert counts(db) == {name: 1 for name in live}
    with db.factory() as session:
        assert session.get(RefreshToken, live["refresh_tokens"]) is not None
        assert session.get(PasswordResetToken, live["password_reset_tokens"]) is not None
        assert session.get(RecoveryCode, live["recovery_codes"]) is not None
    assert purge_audit_count(db) == 3
    locking = [statement for statement in statements if "retention_purge:" in statement]
    if db.engine.dialect.name == "sqlite":
        # SQLite 跳过命名锁。
        assert locking == []
    else:
        assert locking == [_LOCK_SQL, _UNLOCK_SQL]


# --- 并发：两个实例（MySQL） --------------------------------------------------------------


def test_a_second_instance_returns_while_the_named_lock_is_held(
    mysql_db: Db, records: list[logging.LogRecord], monkeypatch: pytest.MonkeyPatch
) -> None:
    """设计 §7「并发：两个实例」：另一个连接拿住 `retention_purge:<库名>` 时，任务记一行 INFO
    后返回 0、不删任何行；锁释放后再跑照常删，跑完锁已释放。"""
    db = mysql_db
    seed(db)
    before = counts(db)
    wire(monkeypatch, db, **ALL_MODES)

    with db.engine.connect() as holder:
        assert holder.scalar(text(_LOCK_SQL)) == 1
        try:
            assert task.purge(now=NOW) == 0
            assert counts(db) == before
            assert purge_audit_count(db) == 0
        finally:
            holder.execute(text(_UNLOCK_SQL))

    ours = [record for record in records if record.name == LOGGER]
    assert [(record.levelno, record.getMessage()) for record in ours] == [
        (logging.INFO, "retention_purge_already_running")
    ]

    assert task.purge(now=NOW) == 3
    assert counts(db) == {"refresh_tokens": 1, "password_reset_tokens": 1, "recovery_codes": 1}
    with db.engine.connect() as probe:
        assert probe.scalar(text(_IS_FREE_SQL)) == 1


def test_the_lock_is_released_when_run_purge_raises(
    mysql_db: Db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`finally` 里释放命名锁：`run_purge` 抛出时锁也不会留在连接上。"""
    db = mysql_db
    wire(monkeypatch, db, **ALL_MODES)

    def _boom(*args: object, **kwargs: object) -> PurgeResult:
        raise RuntimeError("boom")

    monkeypatch.setattr(task, "run_purge", _boom)

    with pytest.raises(RuntimeError):
        task.purge(now=NOW)

    with db.engine.connect() as probe:
        assert probe.scalar(text(_IS_FREE_SQL)) == 1


def test_the_lock_line_carries_no_row_contents(
    mysql_db: Db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """拿不到命名锁那一行 INFO：固定事件名，不带任何附加字段（设计 §6：日志不含行内容）。"""
    db = mysql_db
    wire(monkeypatch, db, retention_password_reset_tokens_mode="count")
    standard = set(vars(logging.makeLogRecord({})))
    # 在任务自己的 logger 上、record 刚产生时拍下它的字段：之后 root 上的 handler 可能往里添东西。
    snapshots: list[dict[str, Any]] = []

    class _Snapshot(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            snapshots.append(dict(vars(record)))

    handler = _Snapshot(level=logging.DEBUG)
    logger = logging.getLogger(LOGGER)
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        with db.engine.connect() as holder:
            assert holder.scalar(text(_LOCK_SQL)) == 1
            try:
                assert task.purge(now=NOW) == 0
            finally:
                holder.execute(text(_UNLOCK_SQL))
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)

    [line] = snapshots
    assert line["levelno"] == logging.INFO
    assert line["msg"] == "retention_purge_already_running"
    assert line["args"] == ()
    assert set(line) - standard == set()


# --- 安全 -----------------------------------------------------------------------------

MODULE = pathlib.Path(__file__).resolve().parents[2] / "app" / "tasks" / "retention.py"
# 拼出来而不是写成字面量：本文件自己也不出现这几个词。
FORBIDDEN = ("TRUN" + "CATE", "DR" + "OP", "FOREIGN_KEY" + "_CHECKS")


def test_the_task_module_never_empties_or_removes_a_table() -> None:
    """设计 §7「安全」：静态扫描 app/tasks/retention.py。"""
    source = MODULE.read_text(encoding="utf-8").upper()
    assert [word for word in FORBIDDEN if word in source] == []
