"""Payment receipts and their number series (design gate #589 v2, AIH-TASK-196).

spec §43「Generate Receipt」、§45、§45.1（`REQ-TAX-001`）、§106；ADR-0008 §5；
Invariant 3、6、10。`receipt_number_series` 每个马来西亚日历年一行（2026–2099，迁移 0039
预建 74 行），记这一年已分配的最大序号；`payment_receipts` 每笔 `PAID` 支付至多一张收据
（`payment_id` 唯一），编号 `ACV-AIR-<年份>-<6 位序号>` 唯一且按年连续，收据数据冻结在
`snapshot_json` 里。

本任务只有数据库层：两张表只由 AIH-TASK-197 的 `issue_receipt` 写（锁住当年的系列行、
`last_sequence + 1`、插入收据，与入账同一事务），下载、列表、补发与告警属于 AIH-TASK-199..203。
时间一律不带时区的 UTC。

⚠️ **收据行不可改、不可删**由 MySQL 触发器保证（迁移 0039）：
`trg_payment_receipts_before_update` 与 `trg_payment_receipts_before_delete`
无条件 SIGNAL 45000。
SQLite 上没有这些触发器，服务层本来就没有更新与删除路径。

⚠️ 索引名、外键名与条件文本同迁移 0039 一致（空白除外），
tests/backend/test_payment_receipts_migration.py 比对两边。
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Final

from sqlalchemy import (
    CHAR,
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

# 外键指向 payments、tenants 与 tax_policy_versions：import 这几个模块，表才在同一个 metadata 里。
from app.models import payments as _payment_models  # noqa: F401
from app.models import tax_policy as _tax_policy_models  # noqa: F401
from app.models import tenancy as _tenancy_models  # noqa: F401
from app.models.base import Base, Money

# 与 app/models/payments.py 同一个写法（SQLite 只对 INTEGER PRIMARY KEY 自增）。
_PrimaryKey = BigInteger().with_variant(Integer, "sqlite")
_ForeignKeyInt = BigInteger().with_variant(Integer, "sqlite")

_PUBLIC_ID_LENGTH = 36
_CURRENCY_LENGTH = 3
# 设计 §2 写死的列宽：`ACV-AIR-YYYY-NNNNNN`。
RECEIPT_NUMBER_LENGTH: Final = 32

# ⚠️ 条件文本与迁移 0039 一致（空白除外）。
SERIES_YEAR_CHECK: Final = "series_year BETWEEN 2026 AND 2099"
SERIES_SEQUENCE_CHECK: Final = "last_sequence BETWEEN 0 AND 999999"
RECEIPT_SERIES_YEAR_CHECK: Final = "series_year BETWEEN 2026 AND 2099"
RECEIPT_SEQUENCE_CHECK: Final = "sequence BETWEEN 1 AND 999999"
RECEIPT_AMOUNT_CHECK: Final = "amount > 0 AND amount = ROUND(amount, 2)"
RECEIPT_CURRENCY_CHECK: Final = "currency = 'MYR'"


class ReceiptNumberSeries(Base):
    """The receipt number counter of one Malaysian calendar year (设计 §2「数据库」「编号」).

    ⚠️ 不要绕过 `issue_receipt`（AIH-TASK-197）改 `last_sequence`：分配编号要在开具的事务里
    `SELECT … FOR UPDATE` 锁住这一年的行、`+ 1`、再插入收据，回滚时一起回滚、不留空号。把它改小
    会让编号复用。行由迁移 0039 预建（2026–2099），不由应用插入或删除。
    """

    __tablename__ = "receipt_number_series"

    # 马来西亚日历年；不自增（MySQL 上没有 AUTO_INCREMENT，迁移的形状检查核对）。
    series_year: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=False)
    # 这一年已分配的最大序号；预建时为 0。
    last_sequence: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    __table_args__ = (
        CheckConstraint(SERIES_YEAR_CHECK, name="ck_receipt_number_series_year"),
        CheckConstraint(SERIES_SEQUENCE_CHECK, name="ck_receipt_number_series_sequence"),
    )


class PaymentReceipt(Base):
    """The receipt of one `PAID` payment (设计 §2「数据库」「快照」).

    ⚠️ 不要直接 UPDATE 或 DELETE 这张表：MySQL 上的触发器无条件拒绝改删任何一行（SQLSTATE
    45000），收据是不可变的财务快照，删了编号就会复用。唯一的写入方是 AIH-TASK-197 的
    `issue_receipt`，只 INSERT。
    """

    __tablename__ = "payment_receipts"

    id: Mapped[int] = mapped_column(_PrimaryKey, primary_key=True, autoincrement=True)
    # uuid4；对外 id（接口里的 `receipt_id`）。
    public_id: Mapped[str] = mapped_column(CHAR(_PUBLIC_ID_LENGTH), nullable=False)
    # 每笔支付至多一张。
    payment_id: Mapped[int] = mapped_column(_ForeignKeyInt, nullable=False)
    # 等于支付的 `tenant_id`。
    tenant_id: Mapped[int] = mapped_column(_ForeignKeyInt, nullable=False)
    # `ACV-AIR-<series_year:04d>-<sequence:06d>`。
    receipt_number: Mapped[str] = mapped_column(String(RECEIPT_NUMBER_LENGTH), nullable=False)
    # `issued_at` 的马来西亚年份。
    series_year: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    # 这笔支付的 `TOPUP` 账本行金额，两位小数（INV-10）。
    amount: Mapped[Money] = mapped_column(nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(_CURRENCY_LENGTH), nullable=False)
    # 等于支付的 `tax_policy_version_id`；它的 `document_notice` 原文冻结进快照（INV-6）。
    tax_policy_version_id: Mapped[int] = mapped_column(_ForeignKeyInt, nullable=False)
    # 快照（schema_version = 1，键集合固定，设计 §2「快照」）。
    snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    issued_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint("public_id", name="uq_payment_receipts_public_id"),
        # 每笔支付至多一张；也是 payment_id 外键用的索引。
        UniqueConstraint("payment_id", name="uq_payment_receipts_payment_id"),
        UniqueConstraint("receipt_number", name="uq_payment_receipts_receipt_number"),
        # 同一年份内序号不重复。
        UniqueConstraint("series_year", "sequence", name="uq_payment_receipts_series_sequence"),
        # 客户列表；也是 tenant_id 外键用的索引。
        Index("ix_payment_receipts_tenant_issued", "tenant_id", "issued_at", "id"),
        # 显式声明：外键用它，MySQL 不另建隐式索引。
        Index("ix_payment_receipts_tax_policy_version_id", "tax_policy_version_id"),
        # RESTRICT：删支付、租户或税务政策版本不许连带删掉收据。
        ForeignKeyConstraint(
            ["payment_id"],
            ["payments.id"],
            name="fk_payment_receipts_payment_id",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_payment_receipts_tenant_id",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tax_policy_version_id"],
            ["tax_policy_versions.id"],
            name="fk_payment_receipts_tax_policy_version_id",
            ondelete="RESTRICT",
        ),
        CheckConstraint(RECEIPT_SERIES_YEAR_CHECK, name="ck_payment_receipts_series_year"),
        CheckConstraint(RECEIPT_SEQUENCE_CHECK, name="ck_payment_receipts_sequence"),
        CheckConstraint(RECEIPT_AMOUNT_CHECK, name="ck_payment_receipts_amount"),
        CheckConstraint(RECEIPT_CURRENCY_CHECK, name="ck_payment_receipts_currency"),
    )


__all__ = [
    "RECEIPT_AMOUNT_CHECK",
    "RECEIPT_CURRENCY_CHECK",
    "RECEIPT_NUMBER_LENGTH",
    "RECEIPT_SEQUENCE_CHECK",
    "RECEIPT_SERIES_YEAR_CHECK",
    "SERIES_SEQUENCE_CHECK",
    "SERIES_YEAR_CHECK",
    "PaymentReceipt",
    "ReceiptNumberSeries",
]
