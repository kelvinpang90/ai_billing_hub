"""Usage rebills (design gate #830 v12, AIH-TASK-253).

spec §19「Rebill / Reprocess」、§16（更正保留新旧引用）、§131；Invariant 5、6、10。
`usage_rebills` 一行是一次 Rebill：管理员对一个客户的一组已计费预付事件按更正版本重算，合计差额以
一行 `REBILL_CREDIT` / `REBILL_DEBIT` 账本入账（`wallet_transaction_id`）。`usage_rebill_lines`
一行是这次 Rebill 里一个事件的明细：原计费、新计费、差额、新旧规则与价格版本、原账本行、原账期与
当时已定稿的原对账单。只有差额不为 0 的事件写明细。

口径（设计 §2「口径」）：
- 当前有效计费 `eff(e)`：事件最近一条明细（明细 `id` 最大）的 `corrected_charge`；没有明细时是
  `usage_events.billable_cost`。恒有 `eff(e) = billable_cost + Σ 该事件明细的 difference`。
- 当前有效版本 `refs(e)`：最近一条明细的 `corrected_pricing_rule_id`、
  `corrected_provider_price_version_id`；没有明细时是事件快照的那两项。
- `previous_charge = eff(e)`、`corrected_charge = new(e)`、`difference = new(e) − eff(e)`；
  Rebill 行的三个合计是明细之和。等式由 AIH-TASK-255 的服务计算并写入，**不建等式 CHECK**
  （SQLite 把 DECIMAL 当浮点比较，合法行会被拒）。

本任务只有数据库层：唯一的写入方是 AIH-TASK-255 的确认（只 INSERT），仓储、服务与接口属于
AIH-TASK-254 起的任务。时间一律不带时区的 UTC。

⚠️ **行不可改、不可删**由 MySQL 触发器保证（迁移 0042）：
`trg_usage_rebills_before_update`、`trg_usage_rebills_before_delete`、
`trg_usage_rebill_lines_before_update`、`trg_usage_rebill_lines_before_delete` 无条件 SIGNAL 45000。
SQLite 上没有这些触发器，服务层本来就没有更新与删除路径。

⚠️ 索引名、外键名与条件文本同迁移 0042 一致（空白除外），
tests/backend/test_usage_rebills_migration.py 比对两边。
"""

from __future__ import annotations

import datetime as dt
from typing import Final

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

# 外键指向这些表：import 这几个模块，表才在同一个 metadata 里。
from app.models import auth as _auth_models  # noqa: F401
from app.models import pricing_rules as _pricing_rule_models  # noqa: F401
from app.models import provider_prices as _provider_price_models  # noqa: F401
from app.models import statements as _statement_models  # noqa: F401
from app.models import tenancy as _tenancy_models  # noqa: F401
from app.models import usage as _usage_models  # noqa: F401
from app.models import wallet as _wallet_models  # noqa: F401
from app.models.base import Base, Money

# 与 app/models/provider_cost_reconciliations.py 同一个写法
# （SQLite 只对 INTEGER PRIMARY KEY 自增）。
_PrimaryKey = BigInteger().with_variant(Integer, "sqlite")
_ForeignKeyInt = BigInteger().with_variant(Integer, "sqlite")

_PUBLIC_ID_LENGTH = 36
# 设计 §2 写死的列宽。
IDEMPOTENCY_KEY_LENGTH: Final = 36
REQUEST_FINGERPRINT_LENGTH: Final = 64
REASON_LENGTH: Final = 255
ORIGINAL_PERIOD_LENGTH: Final = 7
# 一次 Rebill 的事件数上限（设计 §1「我定的」第 2 条：超过就拒绝，不截断）。
MAX_EVENT_COUNT: Final = 1000

_REBILLS = "usage_rebills"
_LINES = "usage_rebill_lines"

# ⚠️ 条件文本与迁移 0042 一致（空白除外）。原因把 ASCII 空白控制字符（制表、换行、垂直制表、换页、
# 回车）替换为空格后 TRIM 不为空串；不换行空格等非 ASCII 空白由确认接口的请求校验拒绝（设计 §2）。
REASON_CHECK: Final = (
    "TRIM(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(reason, CHAR(9), ' '), CHAR(10), ' '),"
    " CHAR(11), ' '), CHAR(12), ' '), CHAR(13), ' ')) <> ''"
)
EVENT_COUNT_CHECK: Final = f"event_count BETWEEN 1 AND {MAX_EVENT_COUNT}"
DIFFERENCE_CHECK: Final = "difference <> 0"


def _restrict(table: str, column: str, referred: str) -> ForeignKeyConstraint:
    """`fk_<表>_<列>` → `<引用表>.id`，RESTRICT：删父行不许连带删掉财务记录。"""
    return ForeignKeyConstraint(
        [column], [f"{referred}.id"], name=f"fk_{table}_{column}", ondelete="RESTRICT"
    )


class UsageRebill(Base):
    """One rebill: one customer, one ledger row, its lines (设计 §2「数据库」).

    ⚠️ 不要直接 UPDATE 或 DELETE 这张表：MySQL 上的触发器无条件拒绝改删任何一行（SQLSTATE
    45000），Rebill 是 spec §19 要求的审计链。更正写错 → 再做一次 Rebill，不删行。唯一的写入方是
    AIH-TASK-255 的确认，只 INSERT。
    """

    __tablename__ = _REBILLS

    id: Mapped[int] = mapped_column(_PrimaryKey, primary_key=True, autoincrement=True)
    # uuid4；对外 id，也是账本行的 `reference_id`（`reference_type = REBILL`）。
    public_id: Mapped[str] = mapped_column(CHAR(_PUBLIC_ID_LENGTH), nullable=False)
    tenant_id: Mapped[int] = mapped_column(_ForeignKeyInt, nullable=False)
    # 客户端生成的小写 UUID；同键同请求重放，同键异请求 409。
    idempotency_key: Mapped[str] = mapped_column(CHAR(IDEMPOTENCY_KEY_LENGTH), nullable=False)
    # 规范请求（客户、更正版本、selection、preview_token、原因）的 sha256 十六进制。
    request_fingerprint: Mapped[str] = mapped_column(
        CHAR(REQUEST_FINGERPRINT_LENGTH), nullable=False
    )
    # 请求里指定的更正版本；空 = 取每个事件的当前有效版本。
    corrective_pricing_rule_id: Mapped[int | None] = mapped_column(_ForeignKeyInt, nullable=True)
    corrective_provider_price_version_id: Mapped[int | None] = mapped_column(
        _ForeignKeyInt, nullable=True
    )
    # 明细行数，1..1000（CHECK）。
    event_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # Σ eff(e)、Σ new(e)、两者之差（≠ 0）；账本金额 = −difference。
    previous_total: Mapped[Money] = mapped_column(nullable=False)
    corrected_total: Mapped[Money] = mapped_column(nullable=False)
    difference: Mapped[Money] = mapped_column(nullable=False)
    # 这次 Rebill 写的那一行 REBILL_CREDIT / REBILL_DEBIT。
    wallet_transaction_id: Mapped[int] = mapped_column(_ForeignKeyInt, nullable=False)
    # 去首尾空白后的原因；只由 ASCII 空白组成的由 CHECK 拒绝。
    reason: Mapped[str] = mapped_column(String(REASON_LENGTH), nullable=False)
    created_by: Mapped[int] = mapped_column(_ForeignKeyInt, nullable=False)
    # 整秒 UTC，钱包锁之后取值；对账单按它的马来西亚月归属。
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint("public_id", name="uq_usage_rebills_public_id"),
        UniqueConstraint("idempotency_key", name="uq_usage_rebills_idempotency_key"),
        # 一次 Rebill 恰有一行账本；也是 wallet_transaction_id 外键用的索引。
        UniqueConstraint("wallet_transaction_id", name="uq_usage_rebills_wallet_transaction_id"),
        # 按客户列 Rebill；也是 tenant_id 外键用的索引。
        Index("ix_usage_rebills_tenant", "tenant_id", "id"),
        # 显式声明：外键用它们，MySQL 不另建隐式索引。
        Index("ix_usage_rebills_corrective_pricing_rule_id", "corrective_pricing_rule_id"),
        Index(
            "ix_usage_rebills_corrective_provider_price_version_id",
            "corrective_provider_price_version_id",
        ),
        Index("ix_usage_rebills_created_by", "created_by"),
        _restrict(_REBILLS, "tenant_id", "tenants"),
        _restrict(_REBILLS, "corrective_pricing_rule_id", "pricing_rules"),
        _restrict(_REBILLS, "corrective_provider_price_version_id", "provider_price_versions"),
        _restrict(_REBILLS, "wallet_transaction_id", "wallet_transactions"),
        _restrict(_REBILLS, "created_by", "users"),
        CheckConstraint(EVENT_COUNT_CHECK, name="ck_usage_rebills_event_count"),
        CheckConstraint("previous_total >= 0", name="ck_usage_rebills_previous_total"),
        CheckConstraint("corrected_total >= 0", name="ck_usage_rebills_corrected_total"),
        CheckConstraint(DIFFERENCE_CHECK, name="ck_usage_rebills_difference_nonzero"),
        CheckConstraint(REASON_CHECK, name="ck_usage_rebills_reason"),
    )


class UsageRebillLine(Base):
    """One usage event's line in one rebill (设计 §2「数据库」).

    ⚠️ 不要直接 UPDATE 或 DELETE 这张表：MySQL 上的触发器无条件拒绝改删任何一行（SQLSTATE
    45000）。事件的当前有效计费由最近一条明细推导，改删明细等于改写计费历史。
    """

    __tablename__ = _LINES

    id: Mapped[int] = mapped_column(_PrimaryKey, primary_key=True, autoincrement=True)
    rebill_id: Mapped[int] = mapped_column(_ForeignKeyInt, nullable=False)
    usage_event_id: Mapped[int] = mapped_column(_ForeignKeyInt, nullable=False)
    # 事件原来的 AI_USAGE 账本行；计费额为 0 的事件没有，为空。
    original_wallet_transaction_id: Mapped[int | None] = mapped_column(
        _ForeignKeyInt, nullable=True
    )
    # eff(e)、new(e) 与差额（≠ 0）。
    previous_charge: Mapped[Money] = mapped_column(nullable=False)
    corrected_charge: Mapped[Money] = mapped_column(nullable=False)
    difference: Mapped[Money] = mapped_column(nullable=False)
    # refs(e)：重算之前的当前有效规则与价格版本。
    previous_pricing_rule_id: Mapped[int] = mapped_column(_ForeignKeyInt, nullable=False)
    previous_provider_price_version_id: Mapped[int] = mapped_column(_ForeignKeyInt, nullable=False)
    # 这次重算用的规则与价格版本：之后这个事件的 refs(e)。
    corrected_pricing_rule_id: Mapped[int] = mapped_column(_ForeignKeyInt, nullable=False)
    corrected_provider_price_version_id: Mapped[int] = mapped_column(_ForeignKeyInt, nullable=False)
    # 按更正价格版本算出的估算成本，只给管理端看；用量分析的成本仍以事件快照为准。
    corrected_estimated_provider_cost_myr: Mapped[Money] = mapped_column(nullable=False)
    # `YYYY-MM`：原账本行按对账单「账本归属规则」的归属月；没有原账本行时取 `occurred_at` 所在
    # 马来西亚月。格式由服务层保证（与 `provider_cost_reconciliations.billing_month` 同一做法）。
    original_period: Mapped[str] = mapped_column(CHAR(ORIGINAL_PERIOD_LENGTH), nullable=False)
    # 确认时这个客户、这个原账期已存在（即已定稿）的对账单；没有为空。
    original_statement_id: Mapped[int | None] = mapped_column(_ForeignKeyInt, nullable=True)
    # 与所属 Rebill 相同。
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        # 一次 Rebill 里一个事件至多一行；也是 rebill_id 外键用的索引。
        UniqueConstraint("rebill_id", "usage_event_id", name="uq_usage_rebill_lines_rebill_event"),
        # 取一个事件的最近明细；也是 usage_event_id 外键用的索引。
        Index("ix_usage_rebill_lines_usage_event", "usage_event_id", "id"),
        # 显式声明：外键用它们，MySQL 不另建隐式索引。
        Index(
            "ix_usage_rebill_lines_original_wallet_transaction_id",
            "original_wallet_transaction_id",
        ),
        Index("ix_usage_rebill_lines_previous_pricing_rule_id", "previous_pricing_rule_id"),
        Index(
            "ix_usage_rebill_lines_previous_provider_price_version_id",
            "previous_provider_price_version_id",
        ),
        Index("ix_usage_rebill_lines_corrected_pricing_rule_id", "corrected_pricing_rule_id"),
        Index(
            "ix_usage_rebill_lines_corrected_provider_price_version_id",
            "corrected_provider_price_version_id",
        ),
        Index("ix_usage_rebill_lines_original_statement_id", "original_statement_id"),
        _restrict(_LINES, "rebill_id", _REBILLS),
        _restrict(_LINES, "usage_event_id", "usage_events"),
        _restrict(_LINES, "original_wallet_transaction_id", "wallet_transactions"),
        _restrict(_LINES, "previous_pricing_rule_id", "pricing_rules"),
        _restrict(_LINES, "previous_provider_price_version_id", "provider_price_versions"),
        _restrict(_LINES, "corrected_pricing_rule_id", "pricing_rules"),
        _restrict(_LINES, "corrected_provider_price_version_id", "provider_price_versions"),
        _restrict(_LINES, "original_statement_id", "monthly_statements"),
        CheckConstraint("previous_charge >= 0", name="ck_usage_rebill_lines_previous_charge"),
        CheckConstraint("corrected_charge >= 0", name="ck_usage_rebill_lines_corrected_charge"),
        CheckConstraint(
            "corrected_estimated_provider_cost_myr >= 0",
            name="ck_usage_rebill_lines_estimated_cost",
        ),
        CheckConstraint(DIFFERENCE_CHECK, name="ck_usage_rebill_lines_difference_nonzero"),
    )


__all__ = [
    "DIFFERENCE_CHECK",
    "EVENT_COUNT_CHECK",
    "IDEMPOTENCY_KEY_LENGTH",
    "MAX_EVENT_COUNT",
    "ORIGINAL_PERIOD_LENGTH",
    "REASON_CHECK",
    "REASON_LENGTH",
    "REQUEST_FINGERPRINT_LENGTH",
    "UsageRebill",
    "UsageRebillLine",
]
