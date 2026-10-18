"""Tax policy version data access and policy resolution (design gate #332 v3, AIH-TASK-078).

⚠️ **只 flush，不 commit。**草稿、发布与两条审计同一事务，事务边界归调用方的
`session_scope()`（app/services/tax_policy.py）。`resolve_tax_policy` 不开事务：它在调用方
（Phase 4 的支付、收据、对账单）生成凭证的那个事务里加锁读（设计 §2「事务边界」）。

⚠️ **锁后的读一律是加锁读**（`FOR UPDATE` / `FOR SHARE`）：MySQL 默认的 REPEATABLE READ 下，
普通 SELECT 读的是本事务第一次一致性读时的快照 —— 锁拿到了、数据却可能是等锁之前的。加锁读
总是读最新提交的版本。`populate_existing`：会话里已有的对象按锁内读到的值刷新。SQLite 忽略
这两种锁。

⚠️ `treatment_json` 的形状只在应用层校验（设计 §2「`treatment_json` 的形状」、§9）：写入前
（服务层）与读出时（`resolve_tax_policy`）各走一次 `parse_treatment` 与
`check_rate_consistency`；读出时不合格就抛错，不返回半个结果（fail closed）。

本任务**不**提供 `lock_tax_policies_shared`：没有调用方，由第一个消费方在自己的闸门里加
（设计 §2「对下游任务的契约」、§10 未决 3）。
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import Select, or_, select
from sqlalchemy.orm import Session

from app.models.auth import User
from app.models.tax_policy import (
    PERIOD_STATUSES,
    TAX_POLICY_LOCK_ID,
    TaxJurisdiction,
    TaxPolicyLock,
    TaxPolicyStatus,
    TaxPolicyType,
    TaxPolicyVersion,
)

# `treatment_json` 的 schema 版本；形状一变就加一（设计 §2）。
TREATMENT_SCHEMA_VERSION: Final = 1
_TREATMENT_KEYS: Final = frozenset(
    {"schema_version", "sst_registered", "tax_charged", "document_notice"}
)
_NOTICE_KEYS: Final = frozenset({"zh", "en"})
# 说明文字的长度上限（设计 §2 规则 4）。
NOTICE_MAX_LENGTH: Final = 255


@dataclass(frozen=True)
class DocumentNotice:
    """The sentence every receipt and statement prints, in Chinese and English (ADR-0008 §5)."""

    zh: str
    en: str


@dataclass(frozen=True)
class TaxTreatment:
    """`treatment_json`, checked by `parse_treatment`."""

    schema_version: int
    sst_registered: bool
    tax_charged: bool
    document_notice: DocumentNotice


@dataclass(frozen=True)
class ResolvedTaxPolicy:
    """The tax policy version in effect at `at`.

    `version_id` 是内部 id：消费方写进 `payments.tax_policy_version_id`，不对外。
    `treatment` 读出时经 `parse_treatment` 与 `check_rate_consistency` 再校验一次。
    """

    version_id: int
    public_id: str
    jurisdiction: str
    policy_type: str
    rate: Decimal | None
    treatment: TaxTreatment
    effective_from: dt.datetime
    effective_to: dt.datetime | None


def _new_public_id() -> str:
    return str(uuid.uuid4())


def _shared[T](statement: Select[T]) -> Select[T]:
    """Locking read in share mode (`FOR SHARE`); refresh objects already in the session."""
    return statement.with_for_update(read=True).execution_options(populate_existing=True)


# --- treatment_json（设计 §2「`treatment_json` 的形状」） ------------------------------------


def _require_not_charged_unless_registered(sst_registered: bool, tax_charged: bool) -> None:
    # 未注册不得收税。`parse_treatment` 与 `check_rate_consistency` 共用这一份。
    if not sst_registered and tax_charged:
        raise ValueError("tax cannot be charged while not SST registered")


def _require_bool(raw: dict[str, Any], key: str) -> bool:
    value = raw[key]
    # `type(x) is bool`：字符串 "false"、0 都拒绝。
    if type(value) is not bool:
        raise ValueError(f"{key} must be a boolean")
    return value


def _require_notice_text(value: object, key: str) -> str:
    if type(value) is not str:
        raise ValueError(f"document_notice.{key} must be a string")
    if not value:
        raise ValueError(f"document_notice.{key} must not be empty")
    if value.strip() != value:
        raise ValueError(f"document_notice.{key} must not have leading or trailing whitespace")
    if "\n" in value or "\r" in value:
        raise ValueError(f"document_notice.{key} must not contain a line break")
    if len(value) > NOTICE_MAX_LENGTH:
        raise ValueError(f"document_notice.{key} must be at most {NOTICE_MAX_LENGTH} characters")
    return value


def parse_treatment(raw: object) -> TaxTreatment:
    """Check `treatment_json` explicitly (no Pydantic coercion) or raise `ValueError`.

    设计 §2 的五条规则：① 顶层是对象、键恰好四个；② `schema_version` 是 `int`（不是 `bool`）
    且为 1；③ 两个标志是 `bool`；④ `document_notice` 键恰好 `zh`、`en`，值是非空、无首尾空白、
    无换行、≤ 255 字符的字符串；⑤ 未注册不得收税。
    """
    if not isinstance(raw, dict):
        raise ValueError("treatment must be an object")
    if set(raw) != _TREATMENT_KEYS:
        raise ValueError(
            "treatment must have exactly the keys " + ", ".join(sorted(_TREATMENT_KEYS))
        )
    schema_version = raw["schema_version"]
    if type(schema_version) is not int or schema_version != TREATMENT_SCHEMA_VERSION:
        raise ValueError(f"schema_version must be the integer {TREATMENT_SCHEMA_VERSION}")
    sst_registered = _require_bool(raw, "sst_registered")
    tax_charged = _require_bool(raw, "tax_charged")
    notice = raw["document_notice"]
    if not isinstance(notice, dict):
        raise ValueError("document_notice must be an object")
    if set(notice) != _NOTICE_KEYS:
        raise ValueError("document_notice must have exactly the keys en, zh")
    _require_not_charged_unless_registered(sst_registered, tax_charged)
    return TaxTreatment(
        schema_version=schema_version,
        sst_registered=sst_registered,
        tax_charged=tax_charged,
        document_notice=DocumentNotice(
            zh=_require_notice_text(notice["zh"], "zh"),
            en=_require_notice_text(notice["en"], "en"),
        ),
    )


def treatment_json(treatment: TaxTreatment) -> dict[str, Any]:
    """The JSON object stored in `treatment_json` (and shown in the audit)."""
    return {
        "schema_version": treatment.schema_version,
        "sst_registered": treatment.sst_registered,
        "tax_charged": treatment.tax_charged,
        "document_notice": {
            "zh": treatment.document_notice.zh,
            "en": treatment.document_notice.en,
        },
    }


def check_rate_consistency(rate: Decimal | None, treatment: TaxTreatment) -> None:
    """Raise `ValueError` unless the `rate` column agrees with the flags (设计 §2, v2).

    不收税 ⇔ `rate` 为空；未注册不得收税。写入前（服务层）与读出时（`resolve_tax_policy`）
    都调这里；MySQL 发布跃迁上的触发器规则 3b 是同一条规则的数据库兜底。
    """
    _require_not_charged_unless_registered(treatment.sst_registered, treatment.tax_charged)
    if treatment.tax_charged != (rate is not None):
        raise ValueError("a rate is required exactly when tax is charged")


# --- 锁 ---------------------------------------------------------------------------


def lock_tax_policies(session: Session) -> TaxPolicyLock:
    """`SELECT … FROM tax_policy_locks WHERE id = 1 FOR UPDATE` (MySQL; SQLite ignores it).

    发布事务的第一条语句；版本表的区间不重叠触发器也先拿这把锁。之后的读全是加锁读，
    当前时间在它返回**之后**才取。
    """
    statement = (
        select(TaxPolicyLock)
        .where(TaxPolicyLock.id == TAX_POLICY_LOCK_ID)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return session.execute(statement).scalar_one()


# --- 读 ---------------------------------------------------------------------------


def any_version_exists(session: Session) -> bool:
    """`SELECT 1 … LIMIT 1 FOR SHARE`: any row at all, whatever its status."""
    statement = select(TaxPolicyVersion.id).limit(1)
    return session.execute(_shared(statement)).first() is not None


def approver_by_email(session: Session, email: str) -> User | None:
    """The user with exactly this (already normalised) email, `FOR SHARE`. Role and status are
    the service's to judge."""
    statement = select(User).where(User.email == email)
    return session.execute(_shared(statement)).scalar_one_or_none()


def period_versions(
    session: Session, jurisdiction: TaxJurisdiction, policy_type: TaxPolicyType
) -> list[TaxPolicyVersion]:
    """Every PUBLISHED / RETIRED version of one (jurisdiction, policy type), `FOR UPDATE`."""
    statement = (
        select(TaxPolicyVersion)
        .where(
            TaxPolicyVersion.jurisdiction == jurisdiction,
            TaxPolicyVersion.policy_type == policy_type,
            TaxPolicyVersion.status.in_(PERIOD_STATUSES),
        )
        .order_by(TaxPolicyVersion.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return list(session.execute(statement).scalars().all())


# --- 写 ---------------------------------------------------------------------------


def insert_draft(
    session: Session,
    *,
    jurisdiction: TaxJurisdiction,
    policy_type: TaxPolicyType,
    rate: Decimal | None,
    treatment: TaxTreatment,
    source_reference: str,
    created_by: int,
    now: dt.datetime,
) -> TaxPolicyVersion:
    """Insert and flush a draft: no period, no approver (版本只能以草稿插入)."""
    row = TaxPolicyVersion(
        public_id=_new_public_id(),
        jurisdiction=jurisdiction,
        policy_type=policy_type,
        rate=rate,
        treatment_json=treatment_json(treatment),
        status=TaxPolicyStatus.DRAFT,
        effective_from=None,
        effective_to=None,
        source_reference=source_reference,
        created_by=created_by,
        approved_by=None,
        approved_at=None,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    return row


def mark_published(
    session: Session,
    row: TaxPolicyVersion,
    *,
    effective_from: dt.datetime,
    approved_by: int,
    now: dt.datetime,
) -> None:
    """DRAFT → PUBLISHED with `[effective_from, NULL)`; the approver is the publisher."""
    if row.status is not TaxPolicyStatus.DRAFT:
        raise ValueError("only a draft is published")
    row.status = TaxPolicyStatus.PUBLISHED
    row.effective_from = effective_from
    row.effective_to = None
    row.approved_by = approved_by
    row.approved_at = now
    row.updated_at = now
    session.flush()


# --- 按时刻解析（Phase 4 的消费方调用；本任务没有调用方） ------------------------------------


def resolve_tax_policy(
    session: Session, jurisdiction: str, policy_type: str, at: dt.datetime
) -> ResolvedTaxPolicy | None:
    """The policy of (`jurisdiction`, `policy_type`) in effect at `at`, or `None`.

    取 `PUBLISHED` / `RETIRED` 且 `effective_from ≤ at` 且（`effective_to` 为空或 `> at`）的
    版本，加锁读（`FOR SHARE`）。`at` 是不带时区的 UTC。空区间永不匹配。多于一个是数据损坏，
    `scalar_one_or_none` 抛错，不静默挑一个。读出时 `parse_treatment` 或
    `check_rate_consistency` 失败就抛 `ValueError`：消费方据此拒绝出凭证，不默认「无税」。
    """
    not_ended = or_(
        TaxPolicyVersion.effective_to.is_(None),
        TaxPolicyVersion.effective_to > at,
    )
    in_effect = select(TaxPolicyVersion).where(
        TaxPolicyVersion.jurisdiction == jurisdiction,
        TaxPolicyVersion.policy_type == policy_type,
        TaxPolicyVersion.status.in_(PERIOD_STATUSES),
        TaxPolicyVersion.effective_from <= at,
        not_ended,
    )
    version = session.execute(_shared(in_effect)).scalar_one_or_none()
    if version is None:
        return None
    treatment = parse_treatment(version.treatment_json)
    check_rate_consistency(version.rate, treatment)
    if version.effective_from is None:
        # PUBLISHED_START_CHECK 保证不会发生；不发生也不返回半个结果。
        raise ValueError("a published tax policy version has no start")
    return ResolvedTaxPolicy(
        version_id=version.id,
        public_id=version.public_id,
        jurisdiction=str(version.jurisdiction),
        policy_type=str(version.policy_type),
        rate=version.rate,
        treatment=treatment,
        effective_from=version.effective_from,
        effective_to=version.effective_to,
    )


__all__ = [
    "NOTICE_MAX_LENGTH",
    "TREATMENT_SCHEMA_VERSION",
    "DocumentNotice",
    "ResolvedTaxPolicy",
    "TaxTreatment",
    "any_version_exists",
    "approver_by_email",
    "check_rate_consistency",
    "insert_draft",
    "lock_tax_policies",
    "mark_published",
    "parse_treatment",
    "period_versions",
    "resolve_tax_policy",
    "treatment_json",
]
