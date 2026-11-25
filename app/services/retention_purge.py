"""Retention purge: the framework and the three authentication-token items (AIH-TASK-263).

设计闸门 #833 v7（docs/design/AIH-TASK-263-retention-purge.md）§2「清理框架」「三项清理的条件」
「审计动作与查询」「事务边界」「时间语义」。这里只有框架与三项清理；Celery 任务、Beat、配置与
MySQL 命名锁属于 AIH-TASK-264（app/tasks/retention.py），这个模块不知道它们。

- 注册表 `ITEMS` 只在代码里：新增一项必须过设计闸门（设计 §3 INV-5），本任务恰好三项。
- 保留期是代码常量（设计 §1「我定的」第 5 条），改它要改 ADR-0017，不是配置。
- 每一项的规则只依赖行本身的列与同表的其他行，部分完成也一致，下一轮按同样条件继续（INV-14）。
- 只用 `DELETE … WHERE id IN (…) AND <同样的条件>`；不清空整表、不删表、不关外键检查与触发器。

⚠️ **设计 §4「业务路径不会读写待删的行」并不完全准确**（v5 审查的不阻断更正）：
- 重置密码成功时，把同一用户所有 `used_at` 为空的兄弟令牌置为已用，不带 `expires_at` 条件
  （app/services/password_reset.py `reset_password` 的最后一条 UPDATE）——它会写到过期满 30 天、
  正要被清理的行；
- 出示已死家族里用过的刷新令牌时，`_revoke_family`（app/services/auth.py）吊销整个家族，
  同样会写到正要被清理的行。
这两条可能与某个清理批次短暂等锁（清理批次只锁它删的行、事务很短）。若真等到锁等待超时或死锁，
按设计 §5 处理：抛错的一方回滚；清理这边本轮停止、下一轮重做。
tests/backend/test_retention_purge_mysql.py 让这两条业务路径各与一个停在提交前的清理批次并发一次。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal, get_args

from sqlalchemy import ColumnElement, Select, and_, delete, func, or_, select, tuple_
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import session_scope
from app.models.auth import (
    AuditAction,
    AuditLog,
    PasswordResetToken,
    RecoveryCode,
    RefreshToken,
)
from app.repositories.wallet import SYSTEM_ACTOR_ROLE

logger = logging.getLogger(__name__)

Mode = Literal["off", "count", "delete"]

# 每批最多删这么多行，一批一个短事务（设计 §2「清理框架」）。
BATCH_SIZE: Final = 1000
# K7–K9（ADR-0017）：三项都是 30 天。
RETENTION: Final = dt.timedelta(days=30)
# 审计的实体类型；`entity_id` 是本轮 now 的 UTC 日期（app/services/audit_query.py 归入对外 id）。
ENTITY_TYPE: Final = "data_retention_run"


@dataclass(frozen=True)
class PurgeItem:
    """One registered purge (设计 §2「清理框架」的表)."""

    # 稳定名：审计 `after_state.item` 与配置键 `retention_<name>_mode`（AIH-TASK-264）的后缀。
    name: str
    retention: dt.timedelta
    # 最多 `limit` 个可删的主键，按主键升序。
    select_ids: Callable[[Session, dt.datetime, int], list[int]]
    # `DELETE … WHERE id IN (ids) AND <同样的条件>`，返回删除行数。
    delete_ids: Callable[[Session, dt.datetime, Sequence[int]], int]
    # 与 `select_ids` 同条件的行数。
    count: Callable[[Session, dt.datetime], int]


@dataclass(frozen=True)
class ItemOutcome:
    """What one item did this round. `rows` is counted (count) or deleted and committed (delete)."""

    item: str
    mode: Mode
    cutoff: dt.datetime
    rows: int


@dataclass(frozen=True)
class PurgeResult:
    """Every item handled this round, in registry order, and the one that failed (if any).

    出错的那一项也在 `items` 里，`rows` 是它出错前已提交的行数；它后面的项不处理、不出现。
    """

    items: tuple[ItemOutcome, ...]
    failed_item: str | None = None
    error_type: str | None = None

    @property
    def deleted(self) -> int:
        return sum(outcome.rows for outcome in self.items if outcome.mode == "delete")


# --- refresh_tokens：按家族 ---------------------------------------------------------
#
# 设计 §1「我定的」第 1 条：一个 `(user_id, family_id)` 的所有行都 `expires_at < cutoff` 才删这个
# 家族的全部行。重放检测靠旧行还在；这样的家族不可能再被续期（续期要出示未过期的令牌），所以活着
# 的家族的检测能力不变。


def _family_key() -> ColumnElement[Any]:
    return tuple_(RefreshToken.user_id, RefreshToken.family_id)


def _dead_families(cutoff: dt.datetime) -> Select[tuple[int, str]]:
    # ⚠️ 分组前**不加** `WHERE expires_at < :cutoff`（设计 v5）：先过滤会让 HAVING 恒为真，活着但
    # 有旧行的家族也被选成候选，每批删 0 行。`correlate(None)`：作为子查询嵌进同表的语句时不要被
    # 自动关联掉 FROM。
    return (
        select(RefreshToken.user_id, RefreshToken.family_id)
        .group_by(RefreshToken.user_id, RefreshToken.family_id)
        .having(func.max(RefreshToken.expires_at) < cutoff)
        .correlate(None)
    )


def _select_refresh_tokens(session: Session, cutoff: dt.datetime, limit: int) -> list[int]:
    # ① 候选家族：每个家族至少一行，取 `limit` 个家族足够凑满 `limit` 行。
    families = session.execute(
        _dead_families(cutoff).order_by(func.min(RefreshToken.id)).limit(limit)
    ).all()
    if not families:
        return []
    # ② 这些家族的行（走 `ix_refresh_tokens_family`）。一个家族的行数超过余量时这一批只取它的前
    # `limit` 行，余下的下一批再删——条件仍成立。
    ids = session.execute(
        select(RefreshToken.id)
        .where(_family_key().in_([tuple(family) for family in families]))
        .order_by(RefreshToken.id)
        .limit(limit)
    ).scalars()
    return [int(row_id) for row_id in ids]


def _delete_refresh_tokens(session: Session, cutoff: dt.datetime, ids: Sequence[int]) -> int:
    # 本批的家族：同一事务里的一条普通读（READ COMMITTED 下是一致读，不加锁）。
    families = session.execute(
        select(RefreshToken.user_id, RefreshToken.family_id)
        .where(RefreshToken.id.in_(ids))
        .distinct()
    ).all()
    if not families:
        return 0
    # 派生表只按本批候选家族经 `(user_id, family_id)` 索引探查，不做全表聚合（设计 v4）。双层子查询
    # 绕开 MySQL「DELETE 的子查询不能直接引用目标表」的限制：派生表先物化。
    live = (
        select(RefreshToken.user_id, RefreshToken.family_id)
        .where(
            _family_key().in_([tuple(family) for family in families]),
            RefreshToken.expires_at >= cutoff,
        )
        .correlate(None)
        .subquery("live")
    )
    result = session.execute(
        delete(RefreshToken)
        .where(
            RefreshToken.id.in_(ids),
            RefreshToken.expires_at < cutoff,
            _family_key().not_in(select(live.c.user_id, live.c.family_id)),
        )
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount)


def _count_refresh_tokens(session: Session, cutoff: dt.datetime) -> int:
    return int(
        session.execute(
            select(func.count())
            .select_from(RefreshToken)
            .where(_family_key().in_(_dead_families(cutoff)))
        ).scalar_one()
    )


# --- password_reset_tokens：两种 purpose 一样 -----------------------------------------
#
# 设计 §1「我定的」第 2 条：邀请照 K8 一律删，不保留最新一条；门户账号视图的 `invited_at` 随之为空。


def _reset_token_expired(cutoff: dt.datetime) -> ColumnElement[bool]:
    return PasswordResetToken.expires_at < cutoff


def _select_reset_tokens(session: Session, cutoff: dt.datetime, limit: int) -> list[int]:
    ids = session.execute(
        select(PasswordResetToken.id)
        .where(_reset_token_expired(cutoff))
        .order_by(PasswordResetToken.id)
        .limit(limit)
    ).scalars()
    return [int(row_id) for row_id in ids]


def _delete_reset_tokens(session: Session, cutoff: dt.datetime, ids: Sequence[int]) -> int:
    result = session.execute(
        delete(PasswordResetToken)
        .where(PasswordResetToken.id.in_(ids), _reset_token_expired(cutoff))
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount)


def _count_reset_tokens(session: Session, cutoff: dt.datetime) -> int:
    return int(
        session.execute(
            select(func.count()).select_from(PasswordResetToken).where(_reset_token_expired(cutoff))
        ).scalar_one()
    )


# --- recovery_codes：用掉或吊销 ---------------------------------------------------------


def _recovery_code_finished(cutoff: dt.datetime) -> ColumnElement[bool]:
    return or_(
        and_(RecoveryCode.used_at.is_not(None), RecoveryCode.used_at < cutoff),
        and_(RecoveryCode.revoked_at.is_not(None), RecoveryCode.revoked_at < cutoff),
    )


def _select_recovery_codes(session: Session, cutoff: dt.datetime, limit: int) -> list[int]:
    ids = session.execute(
        select(RecoveryCode.id)
        .where(_recovery_code_finished(cutoff))
        .order_by(RecoveryCode.id)
        .limit(limit)
    ).scalars()
    return [int(row_id) for row_id in ids]


def _delete_recovery_codes(session: Session, cutoff: dt.datetime, ids: Sequence[int]) -> int:
    result = session.execute(
        delete(RecoveryCode)
        .where(RecoveryCode.id.in_(ids), _recovery_code_finished(cutoff))
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount)


def _count_recovery_codes(session: Session, cutoff: dt.datetime) -> int:
    return int(
        session.execute(
            select(func.count()).select_from(RecoveryCode).where(_recovery_code_finished(cutoff))
        ).scalar_one()
    )


# --- 注册表 ---------------------------------------------------------------------------

ITEMS: Final[tuple[PurgeItem, ...]] = (
    PurgeItem(
        name="refresh_tokens",
        retention=RETENTION,
        select_ids=_select_refresh_tokens,
        delete_ids=_delete_refresh_tokens,
        count=_count_refresh_tokens,
    ),
    PurgeItem(
        name="password_reset_tokens",
        retention=RETENTION,
        select_ids=_select_reset_tokens,
        delete_ids=_delete_reset_tokens,
        count=_count_reset_tokens,
    ),
    PurgeItem(
        name="recovery_codes",
        retention=RETENTION,
        select_ids=_select_recovery_codes,
        delete_ids=_delete_recovery_codes,
        count=_count_recovery_codes,
    ),
)


# --- 执行 -----------------------------------------------------------------------------


def _read_committed(session: Session) -> None:
    """The first statement of every delete batch (设计 §2「事务边界」).

    ⚠️ 写法照 app/services/password_reset.py 的 `_start_reset`：必须是本会话的第一句，只在 `mysql`
    方言上设（SQLite 不接受，也没有行锁与快照问题）；会话关闭、连接归还时隔离级别还原为引擎默认值。
    REPEATABLE READ 下，`DELETE` 里读同表的子查询与按范围的查找会加 next-key 锁，可能挡住登录的插入
    与刷新的条件更新；READ COMMITTED 下查找不加间隙锁、子查询是一致读，删除只锁它删的那些行。
    """
    if session.get_bind().dialect.name == "mysql":
        session.connection(execution_options={"isolation_level": "READ COMMITTED"})


def _delete_batch(
    session_factory: sessionmaker[Session],
    item: PurgeItem,
    *,
    cutoff: dt.datetime,
    limit: int,
    batch: int,
    now: dt.datetime,
) -> tuple[int, int]:
    """One short transaction: select, delete, and the batch's audit. Returns (selected, deleted).

    删除与审计一起提交或一起回滚：删了就一定有审计，没有审计就一定没删（设计 v2）。删除 0 行不写。
    """
    with session_scope(session_factory) as session:
        _read_committed(session)
        ids = item.select_ids(session, cutoff, limit)
        if not ids:
            return 0, 0
        deleted = item.delete_ids(session, cutoff, ids)
        if deleted > 0:
            session.add(
                AuditLog(
                    actor_user_id=None,
                    actor_role=SYSTEM_ACTOR_ROLE,
                    action=AuditAction.DATA_RETENTION_PURGE,
                    entity_type=ENTITY_TYPE,
                    entity_id=now.date().isoformat(),
                    # 恰好这四个键：只有项名、截止时刻、批次号与行数，不含行内容（设计 §6）。
                    after_state=json.dumps(
                        {
                            "item": item.name,
                            "cutoff": cutoff.isoformat(),
                            "batch": batch,
                            "deleted": deleted,
                        },
                        sort_keys=True,
                    ),
                    created_at=now,
                )
            )
        return len(ids), deleted


def _purge_item(
    session_factory: sessionmaker[Session],
    item: PurgeItem,
    *,
    cutoff: dt.datetime,
    max_rows: int,
    now: dt.datetime,
    progress: list[int],
) -> None:
    """Delete in batches until nothing is selected, the cap is reached, or a batch deletes nothing.

    `progress[0]` 是已提交的删除行数：出错时调用方据此报告这一项出错前删了多少。
    """
    batch = 0
    while progress[0] < max_rows:
        selected, deleted = _delete_batch(
            session_factory,
            item,
            cutoff=cutoff,
            limit=min(BATCH_SIZE, max_rows - progress[0]),
            batch=batch + 1,
            now=now,
        )
        if selected == 0:
            return
        if deleted == 0:
            # 无进展即停（设计 v5）：条件在选择与删除之间不一致，正常不会发生。这一项本轮结束，
            # 不让本轮空转、占住命名锁。
            logger.warning(
                "retention_purge_no_progress", extra={"item": item.name, "selected": selected}
            )
            return
        batch += 1
        progress[0] += deleted


def run_purge(
    session_factory: sessionmaker[Session],
    *,
    modes: Mapping[str, Mode],
    max_rows_per_item: int,
    now: dt.datetime,
) -> PurgeResult:
    """Run every registered item once, in registry order (设计 §2「清理框架」).

    `now` 由调用方给出：不带时区的 UTC、整秒，整轮共用一个截止时刻 `now − retention`，跨批不变。
    没在 `modes` 里的项按 `off`。任何一项抛错：当前事务回滚、记一行 ERROR、本轮停止，
    已提交的批保留。
    日志只有项名、模式、截止时刻、行数、耗时与异常类型，不含行内容。
    """
    # ⚠️ 认不出的模式值不能落到「删除」那一支：配置校验（AIH-TASK-264）之外再挡一道。
    unknown = {name for name, mode in modes.items() if mode not in get_args(Mode)}
    if unknown or not set(modes) <= {item.name for item in ITEMS}:
        raise ValueError("Unknown retention purge item or mode")
    now = now.replace(microsecond=0)
    outcomes: list[ItemOutcome] = []
    for item in ITEMS:
        mode = modes.get(item.name, "off")
        cutoff = now - item.retention
        if mode == "off":
            outcomes.append(ItemOutcome(item=item.name, mode=mode, cutoff=cutoff, rows=0))
            continue
        started = time.monotonic()
        progress = [0]
        try:
            if mode == "count":
                # 一个只读事务一条 COUNT；会话关闭即回滚，不需要 READ COMMITTED。
                with session_factory() as session:
                    progress[0] = item.count(session, cutoff)
            else:
                _purge_item(
                    session_factory,
                    item,
                    cutoff=cutoff,
                    max_rows=max_rows_per_item,
                    now=now,
                    progress=progress,
                )
        except Exception as exc:
            # 只记异常类型：异常消息里可能带着 SQL 参数（主键、时间）。
            logger.error(
                "retention_purge_failed",
                extra={"item": item.name, "error_type": type(exc).__name__},
            )
            outcomes.append(ItemOutcome(item=item.name, mode=mode, cutoff=cutoff, rows=progress[0]))
            return PurgeResult(
                items=tuple(outcomes), failed_item=item.name, error_type=type(exc).__name__
            )
        outcomes.append(ItemOutcome(item=item.name, mode=mode, cutoff=cutoff, rows=progress[0]))
        logger.info(
            "retention_purge_item",
            extra={
                "item": item.name,
                "mode": mode,
                "cutoff": cutoff.isoformat(),
                "rows": progress[0],
                "duration_ms": int((time.monotonic() - started) * 1000),
            },
        )
    return PurgeResult(items=tuple(outcomes))


__all__ = [
    "BATCH_SIZE",
    "ENTITY_TYPE",
    "ITEMS",
    "RETENTION",
    "ItemOutcome",
    "Mode",
    "PurgeItem",
    "PurgeResult",
    "run_purge",
]
