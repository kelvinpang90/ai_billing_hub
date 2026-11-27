"""Retention purge of portal notifications written 731 days ago (设计闸门 #862 v2 §7; AIH-TASK-267).

覆盖设计 §7 的「通知正常路径」「通知标已读竞争」，以及本项的「边界值」「count 与 off」。「通知翻页」
走真实接口，在 tests/backend/test_customer_notifications_api.py；注册表与静态扫描在
tests/backend/test_retention_purge.py。

标已读竞争调用 app/repositories/notifications.py 的 `mark_read`（门户标已读接口用的就是它），
不打桩。

行为用例在 SQLite 与 MySQL 上各跑一遍（`db` 夹具）。MySQL 那一半需要 `BILLING_TEST_DATABASE_URL`
指向一个**可以被清空的**库，没设时 skip —— **skip 不是 pass**，CI 设了它。MySQL 上每个用例前后清空
`notifications`（没有触发器、没有入向外键，普通 DELETE 即可），并删掉本文件邮箱域的租户；
审计表只追加、拒绝 DELETE，所以只看本用例开始之后写入的审计（`id` 大于起点）。

时间一律注入：`NOW` 是固定的整秒时刻，截止时刻 `CUTOFF = NOW − 731 天`。uuid 用固定的占位值。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, delete, event, func, select
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.core.database import create_session_factory
from app.models.auth import AuditAction, AuditLog
from app.models.base import Base
from app.models.notifications import Notification, NotificationType
from app.models.tenancy import Tenant
from app.repositories import notifications as notifications_repository
from app.services.retention_purge import (
    ENTITY_TYPE,
    ITEMS,
    NOTIFICATIONS_RETENTION,
    PurgeResult,
    run_purge,
)

DOMAIN = "@retention-notifications.example.com"
ITEM = "notifications"

NOW = dt.datetime(2026, 11, 25, 3, 0, 0)
CUTOFF = NOW - dt.timedelta(days=731)
LONG_AGO = CUTOFF - dt.timedelta(days=5)
SECOND = dt.timedelta(seconds=1)

DELETE = {ITEM: "delete"}
COUNT = {ITEM: "count"}
NO_CAP = 50_000
ZERO_UUID = "00000000-0000-0000-0000-000000000000"


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


def clean(engine: Engine) -> None:
    """MySQL only: `notifications` entirely, then this file's tenants."""
    with engine.begin() as connection:
        connection.execute(delete(Notification))
        connection.execute(delete(Tenant).where(Tenant.email.like(f"%{DOMAIN}")))


def _audit_start(factory: sessionmaker[Session]) -> int:
    with factory() as session:
        return int(session.execute(select(func.coalesce(func.max(AuditLog.id), 0))).scalar_one())


@pytest.fixture(params=["sqlite", "mysql"])
def db(request, monkeypatch) -> Iterator[Db]:
    if request.param == "sqlite":
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        factory = create_session_factory(engine)
        try:
            yield Db(engine=engine, factory=factory, audit_start=0)
        finally:
            engine.dispose()
        return

    url = _mysql_url()
    # 建表走 alembic：审计表的只追加触发器只有迁移会建。
    from app.core.config import get_settings

    monkeypatch.setenv("BILLING_DATABASE_URL", url)
    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")
    get_settings.cache_clear()
    engine = create_engine(url, pool_size=4, max_overflow=4)
    clean(engine)
    factory = create_session_factory(engine)
    try:
        yield Db(engine=engine, factory=factory, audit_start=_audit_start(factory))
    finally:
        clean(engine)
        engine.dispose()


# --- 造数 -----------------------------------------------------------------------------

_numbers = iter(range(1, 1_000_000))


def make_tenant(db: Db, number: int) -> int:
    with db.factory() as session:
        tenant = Tenant(
            public_id=str(uuid.UUID(int=number)),
            company_name=f"Fictional Notifications {number} Sdn Bhd",
            email=f"tenant{number}{DOMAIN}",
            created_at=LONG_AGO,
            updated_at=LONG_AGO,
        )
        session.add(tenant)
        session.commit()
        return int(tenant.id)


def add_notification(
    db: Db,
    tenant_id: int,
    *,
    created_at: dt.datetime,
    read_at: dt.datetime | None = None,
) -> str:
    """Insert one notification; return its `public_id`."""
    number = next(_numbers)
    public_id = str(uuid.UUID(int=(1 << 64) + number))
    with db.factory() as session:
        session.add(
            Notification(
                public_id=public_id,
                tenant_id=tenant_id,
                notification_type=NotificationType.SUSPENDED,
                source_outbox_id=10_000 + number,
                balance=Decimal("12.5"),
                threshold=None,
                occurred_at=created_at,
                created_at=created_at,
                read_at=read_at,
            )
        )
        session.commit()
    return public_id


# --- 读回 -----------------------------------------------------------------------------


def notification_ids(db: Db) -> set[str]:
    with db.factory() as session:
        return set(session.execute(select(Notification.public_id)).scalars())


def purge_audits(db: Db) -> list[AuditLog]:
    with db.factory() as session:
        rows = list(
            session.execute(
                select(AuditLog)
                .where(
                    AuditLog.id > db.audit_start,
                    AuditLog.action == AuditAction.DATA_RETENTION_PURGE,
                )
                .order_by(AuditLog.id)
            ).scalars()
        )
        for row in rows:
            session.expunge(row)
        return rows


def rows_of(result: PurgeResult) -> dict[str, int]:
    return {outcome.item: outcome.rows for outcome in result.items}


def purge(
    db: Db, modes: dict[str, Any] | None = None, *, max_rows: int = NO_CAP, now: dt.datetime = NOW
) -> PurgeResult:
    return run_purge(
        db.factory,
        modes=DELETE if modes is None else modes,
        max_rows_per_item=max_rows,
        now=now,
    )


# --- 正常路径 -----------------------------------------------------------------------------


def test_only_notifications_written_731_days_ago_go_read_or_unread(db: Db) -> None:
    """设计 §7「通知正常路径」：写入满 731 天的已读、未读都删；不满的（已读、未读）都留着；
    两个租户一样（全局规则，不按租户）。"""
    first = make_tenant(db, 1)
    second = make_tenant(db, 2)
    gone = {
        add_notification(db, first, created_at=LONG_AGO),
        add_notification(db, first, created_at=CUTOFF - SECOND, read_at=CUTOFF),
        add_notification(db, second, created_at=LONG_AGO, read_at=NOW - dt.timedelta(days=1)),
    }
    kept = {
        add_notification(db, first, created_at=CUTOFF + dt.timedelta(days=1)),
        add_notification(db, first, created_at=NOW - dt.timedelta(days=1), read_at=NOW),
        add_notification(db, second, created_at=CUTOFF + SECOND, read_at=CUTOFF + SECOND),
    }

    result = purge(db)

    assert notification_ids(db) == kept
    assert not gone & notification_ids(db)
    assert result.failed_item is None
    assert rows_of(result) == {
        "refresh_tokens": 0,
        "password_reset_tokens": 0,
        "recovery_codes": 0,
        "domain_outbox": 0,
        ITEM: len(gone),
        "wallet_reconciliation_findings": 0,
        "wallet_reconciliation_runs": 0,
    }
    [outcome] = [o for o in result.items if o.item == ITEM]
    assert (outcome.mode, outcome.cutoff) == ("delete", CUTOFF)
    [audit] = purge_audits(db)
    assert audit.actor_user_id is None
    assert audit.actor_role == "SYSTEM"
    assert audit.entity_type == ENTITY_TYPE
    assert audit.entity_id == "2026-11-25"
    assert audit.created_at == NOW
    # 恰好这四个键，与 R1 相同（设计 §1「可观察的完成标准」第 3 条）。
    assert json.loads(audit.after_state or "null") == {
        "item": ITEM,
        "cutoff": "2024-11-24T03:00:00",
        "batch": 1,
        "deleted": len(gone),
    }


# --- 标已读竞争 ---------------------------------------------------------------------------


def test_marking_a_purged_notification_read_is_like_marking_a_missing_one(db: Db) -> None:
    """设计 §7「通知标已读竞争」、§4「并发」：先删后标，`mark_read` 读回 `None`，与标一条从来不存在
    的通知同一结果，也不改别的行；先标后删，删除照常（`read_at` 不在条件里）。"""
    tenant = make_tenant(db, 1)
    purged = add_notification(db, tenant, created_at=LONG_AGO)
    marked_first = add_notification(db, tenant, created_at=LONG_AGO)
    kept = add_notification(db, tenant, created_at=NOW - dt.timedelta(days=1))
    with db.factory() as session:
        row = notifications_repository.mark_read(session, tenant, marked_first, now=NOW)
        session.commit()
    assert row is not None and row.read_at == NOW

    assert rows_of(purge(db))[ITEM] == 2
    assert notification_ids(db) == {kept}

    with db.factory() as session:
        after_purge = notifications_repository.mark_read(session, tenant, purged, now=NOW)
        never_existed = notifications_repository.mark_read(session, tenant, ZERO_UUID, now=NOW)
        session.commit()
    assert after_purge is None
    assert after_purge == never_existed
    with db.factory() as session:
        assert session.execute(select(Notification.read_at)).scalars().all() == [None]
        assert notifications_repository.count_unread(session, tenant) == 1


# --- 边界值 ---------------------------------------------------------------------------


def test_a_notification_written_exactly_at_the_cutoff_stays(db: Db) -> None:
    """设计 §7「边界值」：严格早于截止时刻。`created_at` 恰好等于的不删（已读、未读），早一秒的删；
    `count` 与 `delete` 一致。"""
    tenant = make_tenant(db, 1)
    at = {
        add_notification(db, tenant, created_at=CUTOFF),
        add_notification(db, tenant, created_at=CUTOFF, read_at=NOW),
    }
    add_notification(db, tenant, created_at=CUTOFF - SECOND)
    add_notification(db, tenant, created_at=CUTOFF - SECOND, read_at=NOW)

    counted = purge(db, COUNT)
    result = purge(db)

    assert notification_ids(db) == at
    assert rows_of(counted)[ITEM] == rows_of(result)[ITEM] == 2


# --- count 与 off -----------------------------------------------------------------------


def test_count_mode_deletes_nothing_and_reports_what_delete_would(db: Db) -> None:
    tenant = make_tenant(db, 1)
    add_notification(db, tenant, created_at=LONG_AGO)
    add_notification(db, tenant, created_at=LONG_AGO, read_at=CUTOFF)
    kept = add_notification(db, tenant, created_at=NOW - dt.timedelta(days=1))
    before = notification_ids(db)

    counted = purge(db, COUNT)

    assert notification_ids(db) == before
    assert purge_audits(db) == []
    assert rows_of(counted)[ITEM] == 2
    assert [(o.item, o.mode) for o in counted.items if o.item == ITEM] == [(ITEM, "count")]
    assert counted.deleted == 0

    deleted = purge(db)

    assert rows_of(deleted)[ITEM] == rows_of(counted)[ITEM]
    assert notification_ids(db) == {kept}


def test_off_mode_runs_no_sql_on_the_notifications(db: Db) -> None:
    """`off` 不执行这张表的 SQL：本项 `off`、其余各项 `count` 时没有一句提到 `notifications`；
    全部 `off` 时一句 SQL 都没有。"""
    tenant = make_tenant(db, 1)
    add_notification(db, tenant, created_at=LONG_AGO)
    before = notification_ids(db)
    statements: list[str] = []
    table = re.compile(rf"\b{Notification.__tablename__}\b")

    def capture(conn, cursor, statement, parameters, context, executemany) -> None:
        statements.append(statement)

    event.listen(db.engine, "before_cursor_execute", capture)
    try:
        others = purge(db, {item.name: "count" for item in ITEMS if item.name != ITEM})
        assert statements, "其余各项 count：确有 SQL 发出"
        mentioned = [s for s in statements if table.search(s.replace("`", "").lower())]
        statements.clear()
        purge(db, {ITEM: "off"})
    finally:
        event.remove(db.engine, "before_cursor_execute", capture)

    assert mentioned == []
    assert statements == []
    assert [(o.item, o.mode, o.rows) for o in others.items if o.item == ITEM] == [(ITEM, "off", 0)]
    assert notification_ids(db) == before


def test_the_retention_is_731_days() -> None:
    """设计 §1「我定的」第 2 条：任意连续 24 个日历月最长 731 天。"""
    assert NOTIFICATIONS_RETENTION == dt.timedelta(days=731)
    assert NOW - NOTIFICATIONS_RETENTION == CUTOFF == dt.datetime(2024, 11, 24, 3, 0, 0)
