"""The first tax policy: draft and publish it once (design gate #332 v3, AIH-TASK-078).

spec §45.1、§74.9；ADR-0008 第 5 节。本任务只有规则层：没有任何入口调用它，入口是
AIH-TASK-079 的 CLI（`python -m app.cli publish-initial-tax-policy`）。

⚠️ **一个 `session_scope()`**（INV-13）：草稿、`TAX_POLICY_CREATE`、发布、事务内复查与
`TAX_POLICY_PUBLISH` 同一事务，任何一步抛错整体回滚，连草稿一起没有。repository 只 flush。

⚠️ **锁在前**：事务的第一条语句是 `SELECT … FROM tax_policy_locks WHERE id = 1 FOR UPDATE`，
之后的读全是加锁读（版本表 `FOR SHARE`、审批人 `FOR SHARE`、复查 `FOR UPDATE`）；当前时间在
锁后才取。两次并发运行因此串行，后到者看到先到者已提交的行，判 `TaxPolicyAlreadyExists`。

⚠️ **表里有任何一行就拒绝**（设计 §4「幂等键」）：第二次运行是报错的空操作，不是「内容相同
就返回成功」。提交结果不明时原样再调一次即可判定。

⚠️ 审计的前后状态里，版本用 `public_id`，人用登录邮箱，时刻是 ISO 8601 UTC，`rate` 是字符串
或 `null`；不用内部 id（设计 §2「审计」）。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from decimal import Decimal
from typing import Any, Final

from sqlalchemy.orm import Session, sessionmaker

from app.core.database import session_scope
from app.core.errors import AppError
from app.models.auth import AuditAction, UserRole, UserStatus
from app.models.tax_policy import (
    TaxJurisdiction,
    TaxPolicyStatus,
    TaxPolicyType,
    TaxPolicyVersion,
)
from app.repositories import tax_policy as policies
from app.services.auth import RequestContext, record_audit, utc_now

# 审计的 entity_type；entity_id 是版本的 public_id。
ENTITY_TAX_POLICY_VERSION: Final = "tax_policy_version"
# 照 `create_admin` 的 `reason="BOOTSTRAP"`（app/cli.py）。
AUDIT_REASON: Final = "INITIAL_POLICY"

# 取当前时间（不带时区的 UTC）。可注入，测试据此冻结时钟。
type Clock = Callable[[], dt.datetime]

# --- 首条政策的内容（设计 §2「首条政策的内容」） --------------------------------------------

INITIAL_JURISDICTION: Final = TaxJurisdiction.MY
INITIAL_POLICY_TYPE: Final = TaxPolicyType.SST
# 未注册 SST、不适用税率（ADR-0008 第 5 节）。
INITIAL_RATE: Final[Decimal | None] = None
# 说明文字照 ADR-0008 第 5 节逐字；test_tax_policy_service.py 比对 ADR 原文。
INITIAL_TREATMENT: Final[dict[str, Any]] = {
    "schema_version": 1,
    "sst_registered": False,
    "tax_charged": False,
    "document_notice": {
        "zh": "价格为最终价，已包含所有费用，不另收任何税费",
        "en": "Prices are final and include all charges. No tax is charged separately.",
    },
}
# 不带时区的 UTC 2026-10-16T16:00:00 = 吉隆坡 2026-10-17 00:00（固定 +8 小时，马来西亚没有
# 夏令时，不依赖 tzdata）。早于发布时刻：只把「无政策」变成「有政策」（设计 §2）。
INITIAL_EFFECTIVE_FROM: Final = dt.datetime(2026, 10, 16, 16, 0, 0)
# 仓库内路径，不是链接；未聘税务顾问一事照 spec §45.1 写进出处。
INITIAL_SOURCE_REFERENCE: Final = (
    "ADR-0008 §5 (docs/adr/ADR-0008-sst-tax-treatment.md), decided by the decision maker"
    " on 2026-10-17; no tax adviser consulted"
)


class TaxPolicyAlreadyExists(AppError):
    """The table already has a row, whatever its status. Nothing was written."""

    def __init__(self) -> None:
        super().__init__(
            "A tax policy version already exists.", code="TAX_POLICY_EXISTS", http_status=409
        )


class TaxPolicyApproverInvalid(AppError):
    """No account for the email, not an ADMIN, or not ACTIVE: the three are not told apart."""

    def __init__(self) -> None:
        super().__init__(
            "The approver must be an active ADMIN account.",
            code="TAX_POLICY_APPROVER_INVALID",
            http_status=422,
        )


class TaxPolicyRecheckFailed(RuntimeError):
    """The in-transaction recheck failed: a bug, never a user error. Rolled back."""


def _seconds(moment: dt.datetime) -> dt.datetime:
    # MySQL 的 DATETIME 不存小数秒：与 app/services/fx_rates.py 同一理由。
    return moment.replace(microsecond=0)


def _text(moment: dt.datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None


def _rate_text(rate: Decimal | None) -> str | None:
    return format(rate, "f") if rate is not None else None


def _recheck(session: Session, row: TaxPolicyVersion) -> None:
    # 设计 §2 ⑧：加锁重读，(辖区, 政策类型) 的已发布 / 已退役行恰好一条、就是本行、未截断。
    # SQLite 没有触发器，这是那里唯一的结构检查。
    found = policies.period_versions(session, INITIAL_JURISDICTION, INITIAL_POLICY_TYPE)
    if len(found) != 1 or found[0].id != row.id or found[0].effective_to is not None:
        raise TaxPolicyRecheckFailed("the new policy is not the only open published version")


def publish_initial_tax_policy(
    session_factory: sessionmaker[Session],
    *,
    approver_email: str,
    context: RequestContext,
    clock: Clock = utc_now,
) -> str:
    """Draft and publish the first MY / SST policy in one commit; return its `public_id`.

    `approver_email` 由调用方先经 `normalise_email`，这里只精确匹配。`context` 原样写进两条
    审计（CLI 传 `RequestContext(ip_address=None, user_agent="cli")`），服务不自造上下文。
    表里已有任何一行：`TaxPolicyAlreadyExists`；审批人没有账号、不是 ADMIN 或不是 ACTIVE：
    `TaxPolicyApproverInvalid`。两种情形都不写。
    """
    # 事务外：首条政策的内容不合格是代码缺陷，抛错，不连库。
    treatment = policies.parse_treatment(INITIAL_TREATMENT)
    policies.check_rate_consistency(INITIAL_RATE, treatment)
    with session_scope(session_factory) as session:
        # ① ⚠️ 本事务的第一条语句就是这把锁；之后的读全是加锁读。
        policies.lock_tax_policies(session)
        # ② 有任何一行（无论状态）就拒绝。
        if policies.any_version_exists(session):
            raise TaxPolicyAlreadyExists
        # ③
        approver = policies.approver_by_email(session, approver_email)
        if (
            approver is None
            or UserRole(approver.role) is not UserRole.ADMIN
            or UserStatus(approver.status) is not UserStatus.ACTIVE
        ):
            raise TaxPolicyApproverInvalid
        # ④ ⚠️ 拿到锁之后才取时间。
        stamp = _seconds(clock())
        # ⑤
        row = policies.insert_draft(
            session,
            jurisdiction=INITIAL_JURISDICTION,
            policy_type=INITIAL_POLICY_TYPE,
            rate=INITIAL_RATE,
            treatment=treatment,
            source_reference=INITIAL_SOURCE_REFERENCE,
            created_by=approver.id,
            now=stamp,
        )
        # ⑥
        record_audit(
            session,
            action=AuditAction.TAX_POLICY_CREATE,
            context=context,
            now=stamp,
            actor=approver,
            entity_type=ENTITY_TAX_POLICY_VERSION,
            entity_id=row.public_id,
            after_state={
                "jurisdiction": INITIAL_JURISDICTION.value,
                "policy_type": INITIAL_POLICY_TYPE.value,
                "rate": _rate_text(INITIAL_RATE),
                "treatment": policies.treatment_json(treatment),
                "source_reference": INITIAL_SOURCE_REFERENCE,
                "status": TaxPolicyStatus.DRAFT.value,
                "created_by_email": approver.email,
            },
            reason=AUDIT_REASON,
        )
        # ⑦ MySQL：BEFORE UPDATE 触发器在这里查跃迁、尽头为空、税务一致性与区间不重叠。
        policies.mark_published(
            session,
            row,
            effective_from=INITIAL_EFFECTIVE_FROM,
            approved_by=approver.id,
            now=stamp,
        )
        # ⑧
        _recheck(session, row)
        # ⑨
        record_audit(
            session,
            action=AuditAction.TAX_POLICY_PUBLISH,
            context=context,
            now=stamp,
            actor=approver,
            entity_type=ENTITY_TAX_POLICY_VERSION,
            entity_id=row.public_id,
            before_state={"status": TaxPolicyStatus.DRAFT.value},
            after_state={
                "status": TaxPolicyStatus.PUBLISHED.value,
                "effective_from": _text(INITIAL_EFFECTIVE_FROM),
                "effective_to": None,
                "approved_by_email": approver.email,
                "approved_at": _text(stamp),
            },
            reason=AUDIT_REASON,
        )
        public_id = row.public_id
    # ⑩ 已提交。
    return public_id


__all__ = [
    "AUDIT_REASON",
    "ENTITY_TAX_POLICY_VERSION",
    "INITIAL_EFFECTIVE_FROM",
    "INITIAL_JURISDICTION",
    "INITIAL_POLICY_TYPE",
    "INITIAL_RATE",
    "INITIAL_SOURCE_REFERENCE",
    "INITIAL_TREATMENT",
    "TaxPolicyAlreadyExists",
    "TaxPolicyApproverInvalid",
    "TaxPolicyRecheckFailed",
    "publish_initial_tax_policy",
]
