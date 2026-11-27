"""Retention purge: the framework and the three token items (设计闸门 #833 v7 §7; AIH-TASK-263).

覆盖设计 §7 的「正常路径」「刷新令牌家族」「活家族排在前面」「无进展即停」「重放检测不变」
「邀请与重置令牌」「限频」「恢复码」「边界值」「批与上限」「大家族跨批」「count 模式」「off 模式」
「幂等重放」「事务中途失败」「审计与删除一致」「安全」「注册」各行。只在真 MySQL 上才有意义的
并发、锁范围与隔离级别在 tests/backend/test_retention_purge_mysql.py。

行为用例在 SQLite 与 MySQL 上各跑一遍（`db` 夹具）。MySQL 那一半需要 `BILLING_TEST_DATABASE_URL`
指向一个**可以被清空的**库，没设时 skip —— **skip 不是 pass**，CI 设了它。MySQL 上每个用例前后
清空三张令牌表（它们没有触发器、没有入向外键，普通 DELETE 即可）并删掉本文件邮箱域的用户与租户；
审计表只追加、拒绝 DELETE，所以只看本用例开始之后写入的审计（`id` 大于起点）。

时间一律注入：`NOW` 是固定的整秒时刻，截止时刻 `CUTOFF = NOW − 30 天`。uuid 用全零占位值。

设计闸门 #862 v2 §7「R1 用例不受新项干扰」（AIH-TASK-266 起）：这里开 `delete` / `count` 的模式表
只开 R1 三项，新项（`domain_outbox` 等）一律 `off`，MySQL 测试库里别的用例留下的行不会被新项删掉；
新项的用例在各自的文件里（`domain_outbox`：tests/backend/test_retention_outbox.py；`notifications`：
tests/backend/test_retention_notifications.py；钱包核对的发现与运行：
tests/backend/test_retention_reconciliation.py）。
"""

from __future__ import annotations

import base64
import dataclasses
import datetime as dt
import itertools
import json
import logging
import os
import pathlib
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pyotp
import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, delete, event, func, insert, select
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.core.config import Settings
from app.core.database import create_session_factory
from app.core.tokens import hash_refresh_token
from app.models.auth import (
    AuditAction,
    AuditLog,
    DomainOutbox,
    PasswordResetToken,
    RecoveryCode,
    RefreshToken,
    TokenPurpose,
    User,
    UserRole,
    UserStatus,
)
from app.models.base import Base
from app.models.tenancy import Tenant
from app.services import retention_purge
from app.services.auth import RequestContext, TokenReused, refresh_session, utc_now
from app.services.password_reset import request_reset
from app.services.portal_accounts import get_portal_account, resend_invitation
from app.services.retention_purge import (
    BATCH_SIZE,
    DOMAIN_OUTBOX_RETENTION,
    ENTITY_TYPE,
    ITEMS,
    NOTIFICATIONS_RETENTION,
    RETENTION,
    WALLET_RECONCILIATION_RETENTION,
    PurgeResult,
    run_purge,
)
from app.services.two_factor import confirm_enrolment, start_enrolment, verify_second_factor

DOMAIN = "@retention-purge.example.com"
CONTEXT = RequestContext(ip_address="10.0.0.1", user_agent="pytest")
LOGGER = "app.services.retention_purge"

NOW = dt.datetime(2026, 11, 25, 3, 0, 0)
CUTOFF = NOW - dt.timedelta(days=30)
LONG_AGO = CUTOFF - dt.timedelta(days=5)
SECOND = dt.timedelta(seconds=1)

R1 = ("refresh_tokens", "password_reset_tokens", "recovery_codes")
# 设计闸门 #862 v2 §7「R1 用例不受新项干扰」：只开 R1 三项，新项 `off`。
ALL_DELETE = {name: "delete" for name in R1}
ALL_COUNT = {name: "count" for name in R1}
ALL_OFF = {item.name: "off" for item in ITEMS}
# R1 三项之后注册的项在这些用例里都是 `off`：结果里它们 0 行。
NEW_ITEMS_OFF = {
    "domain_outbox": 0,
    "notifications": 0,
    "wallet_reconciliation_findings": 0,
    "wallet_reconciliation_runs": 0,
}
OUTBOX_CUTOFF = NOW - DOMAIN_OUTBOX_RETENTION
NOTIFICATIONS_CUTOFF = NOW - NOTIFICATIONS_RETENTION
RECONCILIATION_CUTOFF = NOW - WALLET_RECONCILIATION_RETENTION
NO_CAP = 50_000

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


def clean(engine: Engine) -> None:
    """MySQL only: the three token tables entirely, then this file's users, outbox and tenants."""
    with engine.begin() as connection:
        connection.execute(delete(RefreshToken))
        connection.execute(delete(PasswordResetToken))
        connection.execute(delete(RecoveryCode))
        ours = [
            str(user_id)
            for user_id in connection.execute(
                select(User.id).where(User.email.like(f"%{DOMAIN}"))
            ).scalars()
        ]
        if ours:
            connection.execute(
                delete(DomainOutbox).where(
                    DomainOutbox.aggregate_type == "users", DomainOutbox.aggregate_id.in_(ours)
                )
            )
        # 两步 2FA 设置随用户外键级联删除。
        connection.execute(delete(User).where(User.email.like(f"%{DOMAIN}")))
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


@pytest.fixture
def records() -> Iterator[list[logging.LogRecord]]:
    """What `run_purge` logs (INFO and up), collected on its own logger."""
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


def logged(records: list[logging.LogRecord], message: str) -> list[logging.LogRecord]:
    return [record for record in records if record.name == LOGGER and record.msg == message]


# --- 造数 -----------------------------------------------------------------------------


def _hash() -> str:
    return f"{next(_hashes):064x}"


def make_user(
    db: Db,
    name: str,
    *,
    role: UserRole = UserRole.CUSTOMER,
    status: UserStatus = UserStatus.ACTIVE,
    tenant_id: int | None = None,
) -> int:
    with db.factory() as session:
        user = User(
            email=f"{name}{DOMAIN}",
            password_hash="not-a-real-hash",
            role=role,
            status=status,
            tenant_id=tenant_id,
            created_at=LONG_AGO,
            updated_at=LONG_AGO,
        )
        session.add(user)
        session.commit()
        return int(user.id)


def make_tenant(db: Db, number: int) -> tuple[int, str]:
    public_id = str(uuid.UUID(int=number))
    with db.factory() as session:
        tenant = Tenant(
            public_id=public_id,
            company_name=f"Fictional Retention {number} Sdn Bhd",
            email=f"tenant{number}{DOMAIN}",
            created_at=LONG_AGO,
            updated_at=LONG_AGO,
        )
        session.add(tenant)
        session.commit()
        return int(tenant.id), public_id


def add_refresh(
    db: Db,
    user_id: int,
    family: str,
    *,
    expires_at: dt.datetime,
    used_at: dt.datetime | None = None,
    raw: str | None = None,
) -> int:
    with db.factory() as session:
        row = RefreshToken(
            user_id=user_id,
            family_id=family,
            token_hash=hash_refresh_token(raw) if raw is not None else _hash(),
            issued_at=expires_at - dt.timedelta(hours=12),
            expires_at=expires_at,
            used_at=used_at,
            password_version=0,
        )
        session.add(row)
        session.commit()
        return int(row.id)


def add_reset(
    db: Db,
    user_id: int,
    *,
    expires_at: dt.datetime,
    purpose: TokenPurpose = TokenPurpose.PASSWORD_RESET,
    created_at: dt.datetime | None = None,
) -> int:
    with db.factory() as session:
        row = PasswordResetToken(
            user_id=user_id,
            token_hash=_hash(),
            expires_at=expires_at,
            created_at=created_at or expires_at - dt.timedelta(minutes=30),
            purpose=purpose,
        )
        session.add(row)
        session.commit()
        return int(row.id)


def add_code(
    db: Db,
    user_id: int,
    *,
    used_at: dt.datetime | None = None,
    revoked_at: dt.datetime | None = None,
) -> int:
    with db.factory() as session:
        row = RecoveryCode(
            user_id=user_id,
            code_hash="not-a-real-hash",
            used_at=used_at,
            revoked_at=revoked_at,
            created_at=LONG_AGO - dt.timedelta(days=30),
        )
        session.add(row)
        session.commit()
        return int(row.id)


def add_many_resets(db: Db, user_id: int, count: int, *, expires_at: dt.datetime) -> None:
    with db.factory() as session:
        session.execute(
            insert(PasswordResetToken),
            [
                {
                    "user_id": user_id,
                    "token_hash": _hash(),
                    "expires_at": expires_at,
                    "created_at": expires_at - dt.timedelta(minutes=30),
                    "purpose": TokenPurpose.PASSWORD_RESET,
                }
                for _ in range(count)
            ],
        )
        session.commit()


def add_big_family(db: Db, user_id: int, family: str, count: int) -> None:
    with db.factory() as session:
        session.execute(
            insert(RefreshToken),
            [
                {
                    "user_id": user_id,
                    "family_id": family,
                    "token_hash": _hash(),
                    "issued_at": LONG_AGO - dt.timedelta(hours=12),
                    "expires_at": LONG_AGO + dt.timedelta(seconds=n),
                    "used_at": LONG_AGO,
                    "password_version": 0,
                }
                for n in range(count)
            ],
        )
        session.commit()


def family_id(n: int) -> str:
    return uuid.UUID(int=n).hex


# --- 读回 -----------------------------------------------------------------------------


def ids(db: Db, model: type[Any]) -> set[int]:
    with db.factory() as session:
        return set(session.execute(select(model.id)).scalars())


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


def states(db: Db) -> list[dict[str, Any]]:
    return [json.loads(row.after_state or "null") for row in purge_audits(db)]


def batches(db: Db, item: str) -> list[tuple[int, int]]:
    """(batch, deleted) of one item's audits, in write order."""
    return [(s["batch"], s["deleted"]) for s in states(db) if s["item"] == item]


def rows_of(result: PurgeResult) -> dict[str, int]:
    return {outcome.item: outcome.rows for outcome in result.items}


def purge(
    db: Db, modes: dict[str, Any] | None = None, *, max_rows: int = NO_CAP, now: dt.datetime = NOW
) -> PurgeResult:
    return run_purge(
        db.factory,
        modes=ALL_DELETE if modes is None else modes,
        max_rows_per_item=max_rows,
        now=now,
    )


@dataclass
class Seeded:
    """The normal-path data: what must go and what must stay, per table."""

    gone: dict[str, set[int]]
    kept: dict[str, set[int]]


TABLES = {
    "refresh_tokens": RefreshToken,
    "password_reset_tokens": PasswordResetToken,
    "recovery_codes": RecoveryCode,
}


def seed_normal(db: Db) -> Seeded:
    user = make_user(db, "normal")
    dead = family_id(1)
    live = family_id(2)
    refresh_gone = {
        add_refresh(db, user, dead, expires_at=LONG_AGO, used_at=LONG_AGO),
        add_refresh(db, user, dead, expires_at=CUTOFF - SECOND),
    }
    refresh_kept = {
        add_refresh(db, user, live, expires_at=NOW - dt.timedelta(days=1), used_at=NOW),
        add_refresh(db, user, live, expires_at=NOW + dt.timedelta(hours=1)),
    }
    reset_gone = {add_reset(db, user, expires_at=CUTOFF - SECOND)}
    reset_kept = {
        add_reset(db, user, expires_at=NOW - dt.timedelta(days=1)),
        add_reset(db, user, expires_at=NOW + dt.timedelta(minutes=10)),
    }
    codes_gone = {
        add_code(db, user, used_at=CUTOFF - SECOND),
        add_code(db, user, revoked_at=LONG_AGO),
    }
    codes_kept = {
        add_code(db, user),
        add_code(db, user, used_at=NOW - dt.timedelta(days=1)),
        add_code(db, user, revoked_at=NOW - dt.timedelta(days=29)),
    }
    return Seeded(
        gone={
            "refresh_tokens": refresh_gone,
            "password_reset_tokens": reset_gone,
            "recovery_codes": codes_gone,
        },
        kept={
            "refresh_tokens": refresh_kept,
            "password_reset_tokens": reset_kept,
            "recovery_codes": codes_kept,
        },
    )


def assert_audits_match_deletions(db: Db, before: dict[str, int]) -> None:
    """「审计与删除一致」: per item, the audits' `deleted` sum to how many rows went; none is 0."""
    found = states(db)
    assert all(state["deleted"] > 0 for state in found), found
    for name, model in TABLES.items():
        audited = sum(state["deleted"] for state in found if state["item"] == name)
        assert audited == before[name] - len(ids(db, model)), name


def counts(db: Db) -> dict[str, int]:
    return {name: len(ids(db, model)) for name, model in TABLES.items()}


# --- 正常路径、审计与日志 ---------------------------------------------------------------


def test_only_expired_terminal_rows_go_and_each_item_writes_one_audit(
    db: Db, records: list[logging.LogRecord]
) -> None:
    seeded = seed_normal(db)
    before = counts(db)

    result = purge(db)

    for name, model in TABLES.items():
        assert ids(db, model) == seeded.kept[name], name
    assert result.failed_item is None and result.error_type is None
    assert rows_of(result) == {
        **{name: len(rows) for name, rows in seeded.gone.items()},
        **NEW_ITEMS_OFF,
    }
    assert [(o.item, o.mode, o.cutoff) for o in result.items] == [
        *[(name, "delete", CUTOFF) for name in TABLES],
        ("domain_outbox", "off", OUTBOX_CUTOFF),
        ("notifications", "off", NOTIFICATIONS_CUTOFF),
        ("wallet_reconciliation_findings", "off", RECONCILIATION_CUTOFF),
        ("wallet_reconciliation_runs", "off", RECONCILIATION_CUTOFF),
    ]
    assert result.deleted == sum(len(rows) for rows in seeded.gone.values())

    audits = purge_audits(db)
    assert len(audits) == 3
    for audit, name in zip(audits, TABLES, strict=True):
        assert audit.actor_user_id is None
        assert audit.actor_role == "SYSTEM"
        assert audit.entity_type == ENTITY_TYPE == "data_retention_run"
        assert audit.entity_id == "2026-11-25"
        assert audit.before_state is None
        assert audit.reason is None and audit.ip_address is None and audit.user_agent is None
        assert audit.created_at == NOW
        # 恰好这四个键：没有行内容（INV-8、设计 §6）。
        assert json.loads(audit.after_state or "null") == {
            "item": name,
            "cutoff": "2026-10-26T03:00:00",
            "batch": 1,
            "deleted": len(seeded.gone[name]),
        }
    assert_audits_match_deletions(db, before)

    infos = logged(records, "retention_purge_item")
    assert [(r.levelno, r.item, r.mode, r.cutoff, r.rows) for r in infos] == [
        (logging.INFO, name, "delete", "2026-10-26T03:00:00", len(seeded.gone[name]))
        for name in TABLES
    ]
    assert all(isinstance(record.duration_ms, int) for record in infos)
    assert logged(records, "retention_purge_no_progress") == []
    assert logged(records, "retention_purge_failed") == []


def test_an_unknown_mode_or_item_is_refused_before_any_work(db: Db) -> None:
    seed_normal(db)
    before = counts(db)

    with pytest.raises(ValueError):
        purge(db, {"refresh_tokens": "remove"})
    with pytest.raises(ValueError):
        # R3 不在设计闸门 #862 v2 里（§1「R3 推迟」）：`ai_usage_outbox` 不是注册项。
        purge(db, {"ai_usage_outbox": "delete"})
    with pytest.raises(ValueError):
        purge(db, {"domain_outbox": "remove"})

    assert counts(db) == before
    assert purge_audits(db) == []


# --- 刷新令牌家族 -------------------------------------------------------------------------


def test_a_refresh_token_family_goes_only_when_every_row_is_past_the_cutoff(db: Db) -> None:
    """A 全过期满 30 天；B 有旧行、最新一行未过期；C 最新一行恰好在截止时刻。"""
    user = make_user(db, "families")
    a = family_id(1)
    b = family_id(2)
    c = family_id(3)
    for when in (LONG_AGO, LONG_AGO + SECOND, CUTOFF - SECOND):
        add_refresh(db, user, a, expires_at=when, used_at=when)
    b_rows = {
        add_refresh(db, user, b, expires_at=LONG_AGO, used_at=LONG_AGO),
        add_refresh(db, user, b, expires_at=NOW + dt.timedelta(hours=1)),
    }
    c_rows = {
        add_refresh(db, user, c, expires_at=LONG_AGO, used_at=LONG_AGO),
        add_refresh(db, user, c, expires_at=CUTOFF),
    }

    result = purge(db, {"refresh_tokens": "delete"})

    assert ids(db, RefreshToken) == b_rows | c_rows
    assert rows_of(result)["refresh_tokens"] == 3
    assert batches(db, "refresh_tokens") == [(1, 3)]


def test_a_live_family_with_a_smaller_id_does_not_stall_the_item(
    db: Db, records: list[logging.LogRecord]
) -> None:
    """活着的家族 B 有过期满 30 天的旧行，`MIN(id)` 比可删家族 A 小。

    B 不会被选成候选（分组前不过滤），所以本轮正常结束：A 全删、B 不动、没有 0 行的审计、
    没有 `retention_purge_no_progress`。
    """
    user = make_user(db, "ahead")
    b = family_id(2)
    a = family_id(1)
    b_rows = {
        add_refresh(db, user, b, expires_at=LONG_AGO, used_at=LONG_AGO),
        add_refresh(db, user, b, expires_at=LONG_AGO + SECOND, used_at=LONG_AGO),
    }
    a_rows = {add_refresh(db, user, a, expires_at=LONG_AGO) for _ in range(2)}
    b_rows.add(add_refresh(db, user, b, expires_at=NOW + dt.timedelta(hours=1)))
    assert min(b_rows) < min(a_rows)

    result = purge(db, {"refresh_tokens": "delete"})

    assert ids(db, RefreshToken) == b_rows
    assert rows_of(result)["refresh_tokens"] == 2
    assert batches(db, "refresh_tokens") == [(1, 2)]
    assert logged(records, "retention_purge_no_progress") == []


def test_a_batch_that_selects_rows_but_deletes_none_ends_that_item_only(
    db: Db, records: list[logging.LogRecord], monkeypatch: pytest.MonkeyPatch
) -> None:
    """无进展即停：打桩让刷新令牌的 `delete_ids` 返回 0；后面的项照常。"""
    user = make_user(db, "stall")
    family = family_id(1)
    refresh_rows = {add_refresh(db, user, family, expires_at=LONG_AGO) for _ in range(3)}
    add_reset(db, user, expires_at=LONG_AGO)
    selects: list[int] = []
    original = ITEMS[0]
    assert original.name == "refresh_tokens"

    def select_ids(session: Session, cutoff: dt.datetime, limit: int) -> list[int]:
        found = original.select_ids(session, cutoff, limit)
        selects.append(len(found))
        return found

    stalled = dataclasses.replace(
        original, select_ids=select_ids, delete_ids=lambda session, cutoff, ids: 0
    )
    monkeypatch.setattr(retention_purge, "ITEMS", (stalled, *ITEMS[1:]))

    result = purge(db)

    assert selects == [3], "选到了却删 0 行：这一项本轮只走一批"
    assert ids(db, RefreshToken) == refresh_rows
    assert result.failed_item is None
    assert rows_of(result) == {
        "refresh_tokens": 0,
        "password_reset_tokens": 1,
        "recovery_codes": 0,
        **NEW_ITEMS_OFF,
    }
    [warning] = logged(records, "retention_purge_no_progress")
    assert warning.levelno == logging.WARNING
    assert (warning.item, warning.selected) == ("refresh_tokens", 3)
    assert batches(db, "refresh_tokens") == []
    assert batches(db, "password_reset_tokens") == [(1, 1)]
    assert ids(db, PasswordResetToken) == set()


def test_replaying_an_old_token_of_a_live_family_still_revokes_the_family(db: Db) -> None:
    """重放检测不变：活家族 B 里过期满 30 天的旧行清理后仍在；出示它照旧吊销整个家族。"""
    user = make_user(db, "replay")
    family = family_id(7)
    old_raw = "an-old-refresh-token-of-a-live-family"
    old = add_refresh(db, user, family, expires_at=LONG_AGO, used_at=LONG_AGO, raw=old_raw)
    newest = add_refresh(db, user, family, expires_at=NOW + dt.timedelta(hours=1))

    purge(db, {"refresh_tokens": "delete"})
    assert ids(db, RefreshToken) == {old, newest}

    with pytest.raises(TokenReused):
        refresh_session(db.factory, Settings(), raw_token=old_raw, context=CONTEXT, now=NOW)

    with db.factory() as session:
        revoked = list(session.execute(select(RefreshToken.revoked_at)).scalars())
        reused = list(
            session.execute(
                select(AuditLog).where(
                    AuditLog.id > db.audit_start, AuditLog.action == AuditAction.TOKEN_REUSED
                )
            ).scalars()
        )
    assert revoked == [NOW, NOW], "整个家族吊销"
    assert [(row.entity_type, row.entity_id) for row in reused] == [("refresh_tokens", family)]


# --- 重置与邀请令牌 -----------------------------------------------------------------------


def test_invitations_and_reset_tokens_go_alike_and_the_invited_at_view_follows(db: Db) -> None:
    """A 三条过期满 30 天的邀请；B 一条旧邀请、一条未过期的新邀请；另有两条旧重置令牌。"""
    settings = Settings()
    admin = make_user(db, "admin", role=UserRole.ADMIN)
    tenant_a, public_a = make_tenant(db, 1)
    tenant_b, public_b = make_tenant(db, 2)
    user_a = make_user(db, "invited-a", status=UserStatus.INVITED, tenant_id=tenant_a)
    user_b = make_user(db, "invited-b", status=UserStatus.INVITED, tenant_id=tenant_b)
    other = make_user(db, "resetter")
    for days in (12, 11, 10):
        add_reset(
            db,
            user_a,
            purpose=TokenPurpose.INVITATION,
            created_at=CUTOFF - dt.timedelta(days=days),
            expires_at=CUTOFF - dt.timedelta(days=days - 3),
        )
    add_reset(
        db,
        user_b,
        purpose=TokenPurpose.INVITATION,
        created_at=CUTOFF - dt.timedelta(days=5),
        expires_at=CUTOFF - dt.timedelta(days=2),
    )
    new_created = NOW - dt.timedelta(hours=1)
    new_expires = NOW + dt.timedelta(days=2)
    b_new = add_reset(
        db,
        user_b,
        purpose=TokenPurpose.INVITATION,
        created_at=new_created,
        expires_at=new_expires,
    )
    add_reset(db, other, expires_at=CUTOFF - dt.timedelta(hours=1))
    add_reset(db, other, expires_at=CUTOFF - dt.timedelta(hours=2))

    result = purge(db, {"password_reset_tokens": "delete"})

    assert ids(db, PasswordResetToken) == {b_new}
    assert rows_of(result)["password_reset_tokens"] == 6
    view_a = get_portal_account(db.factory, customer_id=public_a, now=NOW)
    assert view_a.status == UserStatus.INVITED
    assert view_a.invited_at is None
    assert view_a.invitation_expires_at is None
    view_b = get_portal_account(db.factory, customer_id=public_b, now=NOW)
    assert view_b.invited_at == new_created
    assert view_b.invitation_expires_at == new_expires

    # `INVITED` 的用户照常重发邀请（设计 §3「邀请时间显示」）。
    with db.factory() as session:
        actor = session.get(User, admin)
        assert actor is not None
        session.expunge(actor)
    issued = resend_invitation(
        db.factory, settings, actor=actor, customer_id=public_a, context=CONTEXT, now=NOW
    )
    assert issued.account.status == UserStatus.INVITED
    assert issued.account.invited_at == NOW
    assert issued.account.invitation_expires_at == NOW + dt.timedelta(
        seconds=settings.customer_invitation_ttl_seconds
    )


def test_a_reset_token_inside_the_throttle_window_stays_and_still_throttles(db: Db) -> None:
    settings = Settings(password_reset_ttl_seconds=1_800, password_reset_min_interval_seconds=600)
    make_user(db, "throttled")
    email = f"throttled{DOMAIN}"
    assert request_reset(
        db.factory, settings, email=email, context=CONTEXT, now=NOW - dt.timedelta(seconds=60)
    )
    issued = ids(db, PasswordResetToken)
    assert len(issued) == 1

    purge(db, {"password_reset_tokens": "delete"})
    assert ids(db, PasswordResetToken) == issued

    assert request_reset(db.factory, settings, email=email, context=CONTEXT, now=NOW) is None
    assert ids(db, PasswordResetToken) == issued, "仍在窗口内：不再签发"


# --- 恢复码 ---------------------------------------------------------------------------


def test_only_long_used_or_revoked_recovery_codes_go_and_2fa_still_verifies(
    db: Db, tmp_path: pathlib.Path
) -> None:
    master = tmp_path / "master.key"
    master.write_text(f"1:{base64.b64encode(os.urandom(32)).decode()}\n", encoding="utf-8")
    settings = Settings(master_key_file=str(master))
    user = make_user(db, "two-factor", role=UserRole.ADMIN)
    enrolment = start_enrolment(db.factory, settings, user_id=user, password_version=0)
    codes = confirm_enrolment(
        db.factory,
        settings,
        user_id=user,
        password_version=0,
        code=pyotp.TOTP(enrolment.secret).now(),
        context=CONTEXT,
    )
    live = ids(db, RecoveryCode)
    assert len(live) == 10
    gone = {
        add_code(db, user, used_at=CUTOFF - SECOND),
        add_code(db, user, revoked_at=CUTOFF - dt.timedelta(days=3)),
    }
    recent = {
        add_code(db, user, used_at=NOW - dt.timedelta(days=29)),
        add_code(db, user, revoked_at=NOW - dt.timedelta(days=1)),
    }

    result = purge(db, {"recovery_codes": "delete"})

    assert ids(db, RecoveryCode) == live | recent
    assert rows_of(result)["recovery_codes"] == len(gone)
    with db.factory() as session:
        verified = verify_second_factor(
            session, settings, user_id=user, code=codes[0], now=utc_now()
        )
        session.commit()
    assert verified.used_recovery_code
    assert verified.remaining_recovery_codes == 9


# --- 边界值 ---------------------------------------------------------------------------


def test_a_row_exactly_at_the_cutoff_stays(db: Db) -> None:
    """严格早于截止时刻：恰好等于的不删，早一秒的删。"""
    user = make_user(db, "boundary")
    at = family_id(1)
    before = family_id(2)
    refresh_at = add_refresh(db, user, at, expires_at=CUTOFF)
    add_refresh(db, user, before, expires_at=CUTOFF - SECOND)
    reset_at = add_reset(db, user, expires_at=CUTOFF)
    add_reset(db, user, expires_at=CUTOFF - SECOND)
    used_at = add_code(db, user, used_at=CUTOFF)
    revoked_at = add_code(db, user, revoked_at=CUTOFF)
    add_code(db, user, used_at=CUTOFF - SECOND)
    add_code(db, user, revoked_at=CUTOFF - SECOND)

    counted = purge(db, ALL_COUNT)
    result = purge(db)

    assert ids(db, RefreshToken) == {refresh_at}
    assert ids(db, PasswordResetToken) == {reset_at}
    assert ids(db, RecoveryCode) == {used_at, revoked_at}
    expected = {
        "refresh_tokens": 1,
        "password_reset_tokens": 1,
        "recovery_codes": 2,
        **NEW_ITEMS_OFF,
    }
    assert rows_of(counted) == rows_of(result) == expected


# --- 批与上限 -------------------------------------------------------------------------


def test_batches_stop_at_the_cap_and_the_next_round_finishes(db: Db) -> None:
    """2 500 行可删、上限 2 000：两批各 1 000 停在上限；下一轮删余下 500。"""
    user = make_user(db, "batches")
    add_many_resets(db, user, 2_500, expires_at=LONG_AGO)
    before = counts(db)

    first = purge(db, {"password_reset_tokens": "delete"}, max_rows=2_000)

    assert rows_of(first)["password_reset_tokens"] == 2_000
    assert len(ids(db, PasswordResetToken)) == 500
    assert batches(db, "password_reset_tokens") == [(1, BATCH_SIZE), (2, BATCH_SIZE)]

    second = purge(db, {"password_reset_tokens": "delete"}, max_rows=2_000)

    assert rows_of(second)["password_reset_tokens"] == 500
    assert ids(db, PasswordResetToken) == set()
    assert batches(db, "password_reset_tokens") == [(1, 1_000), (2, 1_000), (1, 500)]
    assert_audits_match_deletions(db, before)


def test_a_cap_below_a_batch_takes_only_what_is_left(db: Db) -> None:
    """余量不足一批时只取余量。"""
    user = make_user(db, "remainder")
    add_many_resets(db, user, 1_200, expires_at=LONG_AGO)

    result = purge(db, {"password_reset_tokens": "delete"}, max_rows=1_100)

    assert rows_of(result)["password_reset_tokens"] == 1_100
    assert batches(db, "password_reset_tokens") == [(1, 1_000), (2, 100)]
    assert len(ids(db, PasswordResetToken)) == 100


def test_a_large_family_is_deleted_over_two_batches(db: Db) -> None:
    user = make_user(db, "large-family")
    add_big_family(db, user, family_id(1), 1_500)

    result = purge(db, {"refresh_tokens": "delete"})

    assert rows_of(result)["refresh_tokens"] == 1_500
    assert ids(db, RefreshToken) == set()
    assert batches(db, "refresh_tokens") == [(1, 1_000), (2, 500)]


def test_a_partly_deleted_large_family_is_still_deletable(db: Db) -> None:
    """大家族跨批的中间状态：删掉前 1 000 行之后，余下 500 行的家族条件仍成立。"""
    user = make_user(db, "half-family")
    add_big_family(db, user, family_id(1), 1_500)
    live = add_refresh(db, user, family_id(2), expires_at=NOW + dt.timedelta(hours=1))

    first = purge(db, {"refresh_tokens": "delete"}, max_rows=1_000)

    assert rows_of(first)["refresh_tokens"] == 1_000
    left = ids(db, RefreshToken) - {live}
    assert len(left) == 500
    assert rows_of(purge(db, {"refresh_tokens": "count"}))["refresh_tokens"] == 500

    second = purge(db, {"refresh_tokens": "delete"})

    assert rows_of(second)["refresh_tokens"] == 500
    assert ids(db, RefreshToken) == {live}
    assert batches(db, "refresh_tokens") == [(1, 1_000), (1, 500)]


# --- count 与 off -----------------------------------------------------------------------


def test_count_mode_deletes_nothing_and_reports_what_delete_would(
    db: Db, records: list[logging.LogRecord]
) -> None:
    seeded = seed_normal(db)
    before = counts(db)

    counted = purge(db, ALL_COUNT)

    assert counts(db) == before
    assert purge_audits(db) == []
    gone = {name: len(rows) for name, rows in seeded.gone.items()}
    expected = {**gone, **NEW_ITEMS_OFF}
    assert rows_of(counted) == expected
    assert [o.mode for o in counted.items] == ["count"] * 3 + ["off"] * 4
    assert counted.deleted == 0
    assert [(r.item, r.mode, r.rows) for r in logged(records, "retention_purge_item")] == [
        (name, "count", rows) for name, rows in gone.items()
    ]

    assert rows_of(purge(db)) == expected


def test_off_mode_runs_no_sql(db: Db, records: list[logging.LogRecord]) -> None:
    seed_normal(db)
    before = counts(db)
    statements: list[str] = []

    def capture(conn, cursor, statement, parameters, context, executemany) -> None:
        statements.append(statement)

    event.listen(db.engine, "before_cursor_execute", capture)
    try:
        result = purge(db, ALL_OFF)
        empty = purge(db, {})
    finally:
        event.remove(db.engine, "before_cursor_execute", capture)

    assert statements == []
    assert [(o.item, o.mode, o.rows) for o in result.items] == [
        (name, "off", 0)
        for name in (
            *TABLES,
            "domain_outbox",
            "notifications",
            "wallet_reconciliation_findings",
            "wallet_reconciliation_runs",
        )
    ]
    assert rows_of(empty) == rows_of(result)
    assert logged(records, "retention_purge_item") == []
    assert counts(db) == before


# --- 幂等与中途失败 ---------------------------------------------------------------------


def test_running_twice_with_the_same_now_deletes_nothing_the_second_time(db: Db) -> None:
    seed_normal(db)
    purge(db)
    audits = len(purge_audits(db))
    after_first = counts(db)

    again = purge(db)

    assert rows_of(again) == {**{name: 0 for name in TABLES}, **NEW_ITEMS_OFF}
    assert len(purge_audits(db)) == audits
    assert counts(db) == after_first


class InjectedFailure(Exception):
    """Stands in for a database error in the middle of a round."""


def _sql(statement: str) -> str:
    return " ".join(statement.replace("`", "").lower().split())


@pytest.mark.parametrize("where", ["delete", "audit"])
def test_a_failure_in_the_second_batch_keeps_the_first_and_stops_the_round(
    db: Db, records: list[logging.LogRecord], where: str
) -> None:
    """① 第二批删除时、② 第二批写审计时注入异常：第一批与它的审计都在，本轮停止，下一轮补完。"""
    user = make_user(db, "failure")
    refresh = add_refresh(db, user, family_id(1), expires_at=LONG_AGO)
    add_many_resets(db, user, 1_500, expires_at=LONG_AGO)
    code = add_code(db, user, used_at=LONG_AGO)
    before = counts(db)
    seen: list[str] = []

    def fail_second(conn, cursor, statement, parameters, context, executemany) -> None:
        sql = _sql(statement)
        if where == "delete":
            hit = sql.startswith("delete from password_reset_tokens")
        else:
            hit = sql.startswith("insert into audit_logs") and "password_reset_tokens" in str(
                parameters
            )
        if hit:
            seen.append(sql)
            if len(seen) == 2:
                raise InjectedFailure

    event.listen(db.engine, "before_cursor_execute", fail_second)
    try:
        result = purge(db)
    finally:
        event.remove(db.engine, "before_cursor_execute", fail_second)

    assert len(seen) == 2
    assert result.failed_item == "password_reset_tokens"
    assert result.error_type == "InjectedFailure"
    assert rows_of(result) == {"refresh_tokens": 1, "password_reset_tokens": 1_000}
    assert refresh not in ids(db, RefreshToken)
    assert len(ids(db, PasswordResetToken)) == 500, "第二批回滚"
    assert ids(db, RecoveryCode) == {code}, "出错之后的项本轮不处理"
    assert batches(db, "refresh_tokens") == [(1, 1)]
    assert batches(db, "password_reset_tokens") == [(1, 1_000)]
    [failure] = logged(records, "retention_purge_failed")
    assert failure.levelno == logging.ERROR
    assert (failure.item, failure.error_type) == ("password_reset_tokens", "InjectedFailure")
    assert [r.item for r in logged(records, "retention_purge_item")] == ["refresh_tokens"]

    retried = purge(db)

    assert retried.failed_item is None
    assert rows_of(retried) == {
        "refresh_tokens": 0,
        "password_reset_tokens": 500,
        "recovery_codes": 1,
        **NEW_ITEMS_OFF,
    }
    assert counts(db) == {name: 0 for name in TABLES}
    assert batches(db, "password_reset_tokens") == [(1, 1_000), (1, 500)]
    assert_audits_match_deletions(db, before)


# --- 安全与注册 -------------------------------------------------------------------------

MODULE = pathlib.Path(__file__).resolve().parents[2] / "app" / "services" / "retention_purge.py"
# 拼出来而不是写成字面量：本文件自己也不出现这几个词。
FORBIDDEN = ("TRUN" + "CATE", "DR" + "OP", "FOREIGN_KEY" + "_CHECKS")


def test_the_purge_module_never_empties_or_removes_a_table() -> None:
    """设计 §6：不清空整表、不删表、不关外键检查；靠代码约定与这条静态扫描。

    app/tasks/retention.py 由 AIH-TASK-264 新建，它的扫描随那个任务加。
    """
    text = MODULE.read_text(encoding="utf-8").upper()
    assert [word for word in FORBIDDEN if word in text] == []


def test_the_purge_module_does_not_import_the_ai_module() -> None:
    """设计闸门 #862 v2 §3「供应商成本对账与分摊」：清理不碰 `ai_usage_outbox`。"""
    text = MODULE.read_text(encoding="utf-8")
    assert "app.ai_api" not in text
    assert "ai_usage_outbox" not in text


def test_exactly_seven_items_are_registered_in_order_with_their_retention() -> None:
    """设计闸门 #862 v2 §7「保留期常量」：AIH-TASK-268 之后恰好七项（R1 三项、`domain_outbox`、
    `notifications`、核对发现、核对运行），顺序固定、发现在运行之前；R1 三项 30 天，领域 outbox
    90 天，通知与核对两项 731 天；项名与配置键一一对应；没有 `ai_usage_outbox`。
    """
    assert [(item.name, item.retention) for item in ITEMS] == [
        ("refresh_tokens", dt.timedelta(days=30)),
        ("password_reset_tokens", dt.timedelta(days=30)),
        ("recovery_codes", dt.timedelta(days=30)),
        ("domain_outbox", dt.timedelta(days=90)),
        ("notifications", dt.timedelta(days=731)),
        ("wallet_reconciliation_findings", dt.timedelta(days=731)),
        ("wallet_reconciliation_runs", dt.timedelta(days=731)),
    ]
    # 配置键（AIH-TASK-264）是 `retention_<name>_mode`，与项名一一对应。
    assert [f"retention_{item.name}_mode" for item in ITEMS] == [
        "retention_refresh_tokens_mode",
        "retention_password_reset_tokens_mode",
        "retention_recovery_codes_mode",
        "retention_domain_outbox_mode",
        "retention_notifications_mode",
        "retention_wallet_reconciliation_findings_mode",
        "retention_wallet_reconciliation_runs_mode",
    ]
    settings = Settings()
    assert all(getattr(settings, f"retention_{item.name}_mode") == "off" for item in ITEMS)
    assert RETENTION == dt.timedelta(days=30)
    assert DOMAIN_OUTBOX_RETENTION == dt.timedelta(days=90)
    assert NOTIFICATIONS_RETENTION == dt.timedelta(days=731)
    assert WALLET_RECONCILIATION_RETENTION == dt.timedelta(days=731)
    assert all(item.retention == RETENTION for item in ITEMS if item.name in TABLES)
    assert BATCH_SIZE == 1_000
    assert {item.name for item in ITEMS} == set(TABLES) | {
        "domain_outbox",
        "notifications",
        "wallet_reconciliation_findings",
        "wallet_reconciliation_runs",
    }
    assert "ai_usage_outbox" not in {item.name for item in ITEMS}
