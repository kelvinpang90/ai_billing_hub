"""`lock_tenant_for_customer_write` (design gate #368 v10 §2「依赖」, §7「写权限并发」).

「测试专用写操作」= 在一个事务里先调辅助函数、再往 `customer_write_lock_probe` 插一行。这张表
只在本文件里定义，建表与删表都由本文件的夹具负责，不写任何产品表（设计 §7：本设计没有客户写
接口，用测试专用的写操作验证）。

两类用例：

- **返回与抛错**：四种 `account_status` 与租户不存在，夹具 `factory` 的两个参数在 SQLite 与真
  MySQL 上各跑一次；
- **并发**：两个连接、`threading.Barrier` 控制先后，只在真 MySQL 上（SQLite 忽略 `FOR UPDATE`）。
  写先拿到锁 / 停用先提交两种先后各跑一次；另有一条对照：只做一次不加锁的状态读取而不调辅助
  函数时，停用在两者之间提交，写入照样成功。

MySQL 那部分需要 `BILLING_TEST_DATABASE_URL`，没设时 skip —— **不要把 skipped 读成 passed**，
CI 设了它。用一个独立的库（`<库名>_write_lock`），不和迁移用例共用：那边靠 `alembic_version`
记状态，这里 `create_all` / `drop_all` 会把它的表删掉（test_two_factor.py 同一做法）。
"""

from __future__ import annotations

import datetime as dt
import os
import threading
import time
from collections.abc import Callable, Iterator

import pytest
from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Engine,
    Integer,
    MetaData,
    Table,
    create_engine,
    func,
    insert,
    select,
)
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import create_session_factory
from app.models.base import Base
from app.models.tenancy import AccountStatus, Tenant
from app.repositories import tenancy
from app.services.account_status import (
    READ_ONLY_STATUSES,
    AccountReadOnly,
    lock_tenant_for_customer_write,
)
from app.services.customers import CustomerNotFound

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")
NOW = dt.datetime(2026, 10, 21, 8, 0, 0)
COMPANY = "Write Lock Test Sdn Bhd"

PENDING = AccountStatus.PENDING_ACTIVATION
ENABLED = AccountStatus.ENABLED
DISABLED = AccountStatus.DISABLED
CLOSED = AccountStatus.CLOSED

# 持锁的一方在屏障之后多等这么久再继续，给另一方时间撞上行锁。等待本身不是断言的依据：
# 断言看的是事件的先后，这段时间只让「没加锁」的实现有机会抢到前面、从而让用例变红。
HOLD_SECONDS = 0.5
BARRIER_TIMEOUT = 15
JOIN_TIMEOUT = 30

# 测试专用表：独立的 MetaData，不进 `Base.metadata`，产品的建表与迁移都看不见它。
_PROBE_METADATA = MetaData()
PROBE = Table(
    "customer_write_lock_probe",
    _PROBE_METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("tenant_id", BigInteger, nullable=False),
    Column("created_at", DateTime, nullable=False),
)


# --- 夹具 -----------------------------------------------------------------------


@pytest.fixture(params=["sqlite", "mysql"])
def factory(request) -> Iterator[sessionmaker[Session]]:
    engines = _mysql_engine() if request.param == "mysql" else _sqlite_engine()
    for engine in engines:
        yield create_session_factory(engine)


@pytest.fixture
def mysql_factory() -> Iterator[sessionmaker[Session]]:
    for engine in _mysql_engine():
        yield create_session_factory(engine)


def _sqlite_engine() -> Iterator[Engine]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    PROBE.create(engine)
    try:
        yield engine
    finally:
        PROBE.drop(engine, checkfirst=True)
        engine.dispose()


def _mysql_engine() -> Iterator[Engine]:
    if not TEST_DATABASE_URL:
        pytest.skip("BILLING_TEST_DATABASE_URL is not set; the MySQL half needs a real MySQL")
    base_url = make_url(TEST_DATABASE_URL)
    admin = create_engine(base_url.set(database=""), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.exec_driver_sql(f"CREATE DATABASE IF NOT EXISTS `{base_url.database}_write_lock`")
    admin.dispose()

    engine = create_engine(base_url.set(database=f"{base_url.database}_write_lock"), pool_size=5)
    PROBE.drop(engine, checkfirst=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    PROBE.create(engine)
    try:
        yield engine
    finally:
        PROBE.drop(engine, checkfirst=True)
        Base.metadata.drop_all(engine)
        engine.dispose()


# --- 帮手 -----------------------------------------------------------------------


def make_tenant(factory, status: AccountStatus) -> int:
    """A committed tenant whose account status is written directly."""
    with factory() as session:
        tenant = tenancy.create_tenant(
            session, company_name=COMPANY, email="ops@example.com", now=NOW
        )
        tenant.account_status = status
        tenant_id = int(tenant.id)
        session.commit()
    return tenant_id


def guarded_write(factory, tenant_id: int) -> None:
    """The test-only customer write: lock and re-check the tenant, then insert one row."""
    with factory() as session:
        lock_tenant_for_customer_write(session, tenant_id)
        session.execute(insert(PROBE).values(tenant_id=tenant_id, created_at=NOW))
        session.commit()


def probe_rows(factory, tenant_id: int) -> int:
    with factory() as session:
        statement = select(func.count()).select_from(PROBE).where(PROBE.c.tenant_id == tenant_id)
        return int(session.execute(statement).scalar_one())


def account_status_of(factory, tenant_id: int) -> AccountStatus:
    with factory() as session:
        statement = select(Tenant.account_status).where(Tenant.id == tenant_id)
        return session.execute(statement).scalar_one()


def lock_tenant_row(session: Session, tenant_id: int) -> Tenant:
    """The row lock `change_account_status` takes before it changes the status.

    产品代码按 `public_id` 锁（`get_tenant_by_public_id(..., for_update=True)`），这里按主键锁
    同一行，效果相同。停用方直接写列，不走服务：服务另写审计与 outbox，与这里要验的锁无关。
    """
    return session.execute(
        select(Tenant).where(Tenant.id == tenant_id).with_for_update()
    ).scalar_one()


def disable_tenant(factory, tenant_id: int) -> None:
    with factory() as session:
        lock_tenant_row(session, tenant_id).account_status = DISABLED
        session.commit()


def run_pair(**work: Callable[[], None]) -> dict[str, Exception | None]:
    """Run two callables on two threads (so two connections); return what each raised."""
    outcomes: dict[str, Exception | None] = {}

    def runner(name: str, fn: Callable[[], None]) -> None:
        try:
            fn()
            outcomes[name] = None
        except Exception as exc:  # noqa: BLE001 - 由调用方逐个断言
            outcomes[name] = exc

    threads = [threading.Thread(target=runner, args=item) for item in work.items()]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=JOIN_TIMEOUT)
    assert not any(thread.is_alive() for thread in threads), "a side never finished"
    return outcomes


# --- 返回与抛错（SQLite + MySQL） -----------------------------------------------


@pytest.mark.parametrize("status", [PENDING, ENABLED])
def test_a_writable_tenant_is_returned_and_the_write_goes_through(
    factory, status: AccountStatus
) -> None:
    tenant_id = make_tenant(factory, status)
    with factory() as session:
        tenant = lock_tenant_for_customer_write(session, tenant_id)
        assert (tenant.id, tenant.account_status) == (tenant_id, status)
        session.execute(insert(PROBE).values(tenant_id=tenant_id, created_at=NOW))
        session.commit()
    assert probe_rows(factory, tenant_id) == 1


@pytest.mark.parametrize("status", [DISABLED, CLOSED])
def test_a_read_only_tenant_refuses_the_write(factory, status: AccountStatus) -> None:
    tenant_id = make_tenant(factory, status)
    with pytest.raises(AccountReadOnly) as caught:
        guarded_write(factory, tenant_id)
    assert (caught.value.code, caught.value.http_status) == ("ACCOUNT_READ_ONLY", 403)
    assert probe_rows(factory, tenant_id) == 0


def test_the_read_only_statuses_are_exactly_disabled_and_closed() -> None:
    assert frozenset(READ_ONLY_STATUSES) == {DISABLED, CLOSED}


def test_a_missing_tenant_is_customer_not_found(factory) -> None:
    tenant_id = make_tenant(factory, ENABLED)
    with factory() as session, pytest.raises(CustomerNotFound):
        lock_tenant_for_customer_write(session, tenant_id + 1_000)


# --- 并发（真 MySQL） -----------------------------------------------------------


def test_a_plain_read_earlier_in_the_transaction_does_not_hide_a_disable(mysql_factory) -> None:
    """同一事务里先普通读过租户（建立了快照、identity map 里有旧对象），之后别的连接停用并
    提交：辅助函数的锁读仍读到 `DISABLED` 并拒绝。"""
    tenant_id = make_tenant(mysql_factory, ENABLED)
    with mysql_factory() as session:
        stale = session.get(Tenant, tenant_id)
        assert stale is not None
        assert stale.account_status is ENABLED

        disable_tenant(mysql_factory, tenant_id)

        with pytest.raises(AccountReadOnly):
            lock_tenant_for_customer_write(session, tenant_id)
    assert probe_rows(mysql_factory, tenant_id) == 0


def test_a_write_that_takes_the_lock_first_completes_before_the_disable(mysql_factory) -> None:
    """写先拿到锁：写入成功，停用排在它后面、随后生效。"""
    tenant_id = make_tenant(mysql_factory, ENABLED)
    barrier = threading.Barrier(2, timeout=BARRIER_TIMEOUT)
    events: list[str] = []

    def write() -> None:
        with mysql_factory() as session:
            lock_tenant_for_customer_write(session, tenant_id)
            barrier.wait()  # 写方已持有租户锁
            time.sleep(HOLD_SECONDS)
            session.execute(insert(PROBE).values(tenant_id=tenant_id, created_at=NOW))
            events.append("write inserted")
            session.commit()

    def disable() -> None:
        barrier.wait()
        with mysql_factory() as session:
            tenant = lock_tenant_row(session, tenant_id)
            events.append("disable locked")
            tenant.account_status = DISABLED
            session.commit()

    outcomes = run_pair(write=write, disable=disable)

    assert outcomes == {"write": None, "disable": None}
    # 停用方在写方提交之后才拿到锁。辅助函数不加锁的话，停用方在写方等待期间就拿到了锁。
    assert events == ["write inserted", "disable locked"]
    assert probe_rows(mysql_factory, tenant_id) == 1
    assert account_status_of(mysql_factory, tenant_id) is DISABLED


def test_a_disable_that_commits_first_refuses_the_write(mysql_factory) -> None:
    """停用先提交：写入方拿到锁后读到 `DISABLED`，抛 `ACCOUNT_READ_ONLY`，测试表没有新行。"""
    tenant_id = make_tenant(mysql_factory, ENABLED)
    barrier = threading.Barrier(2, timeout=BARRIER_TIMEOUT)
    events: list[str] = []

    def disable() -> None:
        with mysql_factory() as session:
            lock_tenant_row(session, tenant_id).account_status = DISABLED
            session.flush()
            barrier.wait()  # 停用方已持锁、尚未提交
            time.sleep(HOLD_SECONDS)
            events.append("disable committing")
            session.commit()

    def write() -> None:
        barrier.wait()
        try:
            guarded_write(mysql_factory, tenant_id)
        except AccountReadOnly:
            events.append("write refused")
            raise

    outcomes = run_pair(disable=disable, write=write)

    assert outcomes["disable"] is None
    assert isinstance(outcomes["write"], AccountReadOnly)
    # 不加锁的实现在停用提交之前就读到 ENABLED 并写入成功，这里就不会有 "write refused"。
    assert events == ["disable committing", "write refused"]
    assert probe_rows(mysql_factory, tenant_id) == 0
    assert account_status_of(mysql_factory, tenant_id) is DISABLED


def test_an_unlocked_status_check_alone_does_not_stop_a_write_after_a_disable(
    mysql_factory,
) -> None:
    """对照（刻意的）：只做一次不加锁的状态读取、读完就结束事务（与 `require_customer_writable`
    等价；本任务还没有这个依赖，用等价读取代替），而写事务不调辅助函数时，停用在两者之间提交，
    写入照样成功。这证明提前拒绝本身不是保证，每个客户写操作都必须在自己的写事务里调辅助函数。
    """
    tenant_id = make_tenant(mysql_factory, ENABLED)
    barrier = threading.Barrier(2, timeout=BARRIER_TIMEOUT)

    def write() -> None:
        with mysql_factory() as session:
            status = session.execute(
                select(Tenant.account_status).where(Tenant.id == tenant_id)
            ).scalar_one()
        assert status not in READ_ONLY_STATUSES
        barrier.wait()  # 提前检查已通过
        barrier.wait()  # 停用已提交
        with mysql_factory() as session:
            session.execute(insert(PROBE).values(tenant_id=tenant_id, created_at=NOW))
            session.commit()

    def disable() -> None:
        barrier.wait()
        disable_tenant(mysql_factory, tenant_id)
        barrier.wait()

    outcomes = run_pair(write=write, disable=disable)

    assert outcomes == {"write": None, "disable": None}
    assert account_status_of(mysql_factory, tenant_id) is DISABLED
    assert probe_rows(mysql_factory, tenant_id) == 1, "停用之后仍写进了一行：这正是对照要证明的"
