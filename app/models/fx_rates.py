"""FX rate versions, the FX lock and BNM fetch attempts (design gate #183 v3, AIH-TASK-028).

spec §17.1、§74.2；ADR-0005。三张表（本任务是设计 §11 的 F1，只有数据库层）：

- `fx_rate_locks`：只有一行（`id = 1`）、没有业务字段。发布与退役对它
  `SELECT … FOR UPDATE`，计费对它 `FOR SHARE`（与 `pricing_rule_locks` 同一套推理）；
- `fx_rate_versions`：一个币种对（`base_currency` → MYR）的一版汇率，`rate` 是
  1 单位 `base_currency` 等于多少 MYR。状态 `DRAFT → PUBLISHED → RETIRED`，草稿也可以
  `DISCARDED`。已发布（`PUBLISHED` / `RETIRED`）的版本在 `[effective_from, effective_to)`
  内生效；同一币种对的非空区间互不相交，至多一个未截断的已发布版本（生成列 `open_slot`
  + 唯一索引）；同一报价日的 BNM 版本只有一条（唯一约束）；
- `fx_fetch_attempts`：每次向 BNM 拉取留一条记录，只增。

⚠️ **发布后不可变**由 MySQL 触发器保证（迁移 0015）：锁表恰好一行；
版本只能以草稿插入、草稿只能改成草稿 / 已发布 / 已丢弃、发布跃迁上尽头为空；
已发布的行只许改 `effective_to`、`PUBLISHED → RETIRED` 与 `updated_at`；
已退役、已丢弃的行不可改；锁 `fx_rate_locks` 后检查同一币种对的区间不重叠
（空区间跳过）；版本一律不许删除；拉取记录不许改、不许删。SQLite 上没有
这些触发器，服务层按同样的规则写。

⚠️ 条件文本与生成列表达式同迁移 0015 逐字一致（空白除外），test_migrations.py 比对两边。
"""

from __future__ import annotations

import datetime as dt
import enum
from decimal import Decimal
from typing import Final

from sqlalchemy import (
    CHAR,
    DDL,
    BigInteger,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    Enum,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# 与 app/models/auth.py 同一个写法（SQLite 只对 INTEGER PRIMARY KEY 自增）。
_PrimaryKey = BigInteger().with_variant(Integer, "sqlite")
_ForeignKeyInt = BigInteger().with_variant(Integer, "sqlite")

_PUBLIC_ID_LENGTH = 36

# 设计 §2 写死的列宽。
CURRENCY_LENGTH: Final = 3
SOURCE_LENGTH: Final = 16
SOURCE_REFERENCE_LENGTH: Final = 255
STATUS_LENGTH: Final = 16
# `NO_QUOTE_FOR_DATE` 有 17 个字符，照抄 `status` 的 16 放不下（设计 v3）。
OUTCOME_LENGTH: Final = 32
ERROR_CODE_LENGTH: Final = 64

# `rate` 的精度：DECIMAL(24,10)。BNM 按 `unit` 报价，除以 `unit` 后要求在 10 位小数内精确。
RATE_PRECISION: Final = 24
RATE_SCALE: Final = 10

# 锁表唯一一行的 id。
FX_RATE_LOCK_ID: Final = 1
# 汇率只换成这一种币种（§17.1）。
QUOTE_CURRENCY: Final = "MYR"


class FxRateStatus(enum.StrEnum):
    """设计 §2「状态」。`DISCARDED` 是 §74.2 之外补的：丢弃的草稿留痕、不删除。"""

    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    RETIRED = "RETIRED"
    DISCARDED = "DISCARDED"


class FxRateSourceType(enum.StrEnum):
    """ADR-0005 §1、§2：BNM 自动拉取的草稿，或管理员手工录入。"""

    BNM = "BNM"
    MANUAL = "MANUAL"


class FxFetchOutcome(enum.StrEnum):
    """一次拉取的结果（设计 §2 `fx_fetch_attempts`）。前三个都是**成功**，不计入失败。

    - `NEW_DRAFT`：取到报价，写了新草稿；
    - `NO_NEW_QUOTE`：取到报价，但该报价日已有 BNM 版本（同一天的第二、三次拉取，或并发
      拉取的输家）；BNM 更正了同日价格时 `error_code` 记 `QUOTE_CHANGED`；
    - `NO_QUOTE_FOR_DATE`：BNM 明确答「该日无记录」（周末、公众假期、中午场尚未公布）；
    - `FAILED`：超时、网络、HTTP 错误、响应不合格式或除以 `unit` 不精确，`error_code` 必填。
    """

    NEW_DRAFT = "NEW_DRAFT"
    NO_NEW_QUOTE = "NO_NEW_QUOTE"
    NO_QUOTE_FOR_DATE = "NO_QUOTE_FOR_DATE"
    FAILED = "FAILED"


# 已发布过的两种状态：有区间、参与取汇率。
PERIOD_STATUSES: Final = (FxRateStatus.PUBLISHED, FxRateStatus.RETIRED)

# ⚠️ 条件文本与迁移 0015 逐字一致（空白除外），test_migrations.py 比对两边。
# `base_currency` 的格式 `^[A-Z]{3}$` 只在应用层校验，数据库不做正则 CHECK（设计 v3）。
BASE_CURRENCY_CHECK: Final = "base_currency <> 'MYR'"
QUOTE_CURRENCY_CHECK: Final = "quote_currency = 'MYR'"
RATE_CHECK: Final = "rate > 0"
SOURCE_CHECK: Final = "source IN ('BNM', 'MANUAL')"
VERSION_STATUS_CHECK: Final = "status IN ('DRAFT', 'PUBLISHED', 'RETIRED', 'DISCARDED')"
# 设计 §2 CHECK 1：草稿与丢弃的草稿没有区间、没有发布人。
UNPUBLISHED_CHECK: Final = (
    "status NOT IN ('DRAFT', 'DISCARDED') OR (effective_from IS NULL AND effective_to IS NULL"
    " AND approved_by IS NULL AND approved_at IS NULL)"
)
# CHECK 2：发布过的一定有发布人与发布时刻。
APPROVED_CHECK: Final = (
    "status NOT IN ('PUBLISHED', 'RETIRED')"
    " OR (approved_by IS NOT NULL AND approved_at IS NOT NULL)"
)
# CHECK 3：区间不能倒置；空区间只能出现在 RETIRED 行上（撤销预约、同一秒里发布又退役）。
PERIOD_CHECK: Final = (
    "effective_from IS NULL OR effective_to IS NULL OR effective_from < effective_to"
    " OR (status = 'RETIRED' AND effective_from = effective_to)"
)
# CHECK 4：停用必有尽头。没有尽头的 RETIRED 行会一直被取到，
# 并让之后的发布都撞上区间不重叠。
RETIRED_END_CHECK: Final = "status <> 'RETIRED' OR effective_to IS NOT NULL"
# CHECK 5：BNM 的版本必有报价日（同一报价日唯一约束靠它）。
BNM_QUOTE_DATE_CHECK: Final = "source <> 'BNM' OR source_quote_date IS NOT NULL"
FETCH_SOURCE_CHECK: Final = "source = 'BNM'"
FETCH_OUTCOME_CHECK: Final = (
    "outcome IN ('NEW_DRAFT', 'NO_NEW_QUOTE', 'NO_QUOTE_FOR_DATE', 'FAILED')"
)
# 设计 v3：每种结果恰好带哪些列。
FETCH_OUTCOME_COLUMNS_CHECK: Final = (
    "(outcome = 'NEW_DRAFT' AND quote_date IS NOT NULL AND fx_rate_version_id IS NOT NULL"
    " AND error_code IS NULL)"
    " OR (outcome = 'NO_NEW_QUOTE' AND quote_date IS NOT NULL AND fx_rate_version_id IS NULL"
    " AND (error_code IS NULL OR error_code = 'QUOTE_CHANGED'))"
    " OR (outcome = 'NO_QUOTE_FOR_DATE' AND quote_date IS NULL AND fx_rate_version_id IS NULL"
    " AND error_code IS NULL)"
    " OR (outcome = 'FAILED' AND error_code IS NOT NULL AND quote_date IS NULL"
    " AND fx_rate_version_id IS NULL)"
)

# 只有未截断的已发布版本为 1，其余一律为 NULL（不是 0）；唯一索引允许多个
# NULL，所以「同一币种对至多一个未截断的已发布版本」由数据库保证
# （写法同 `provider_price_versions.open_slot`）。
OPEN_SLOT_EXPRESSION: Final = "CASE WHEN status = 'PUBLISHED' AND effective_to IS NULL THEN 1 END"


def _rate() -> Numeric:
    # DECIMAL(24,10)，读出来是 Decimal（INV-10）：汇率不经过 float。
    return Numeric(RATE_PRECISION, RATE_SCALE, asdecimal=True)


class FxRateLock(Base):
    """The one-row lock that serialises publishing FX rates with billing (设计 §2).

    发布 / 退役：`FOR UPDATE`，是主事务的第一条语句；
    计费：`FOR SHARE`，持有到计费事务提交。区间不重叠触发器同样先对它
    `FOR UPDATE`，绕过服务的直接写入也被串行起来。
    """

    __tablename__ = "fx_rate_locks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)


# `Base.metadata.create_all`（单元测试的 SQLite）建表时同样写入那一行；生产上由迁移 0015 写入，
# 迁移不经过这里。
event.listen(
    FxRateLock.__table__,
    "after_create",
    DDL(f"INSERT INTO fx_rate_locks (id) VALUES ({FX_RATE_LOCK_ID})"),
)


class FxRateVersion(Base):
    """One version of the MYR rate for one base currency.

    `effective_from IS NULL` = 「一直以来」，只可能出现在该币种对时间线为空时的发布；
    `effective_to IS NULL` = 仍生效（或尚未发布）。发布后只有
    `effective_to`（截断、撤销预约时恢复）、`status`（`PUBLISHED → RETIRED`）
    与 `updated_at` 会变；退役后什么都不变。
    """

    __tablename__ = "fx_rate_versions"

    id: Mapped[int] = mapped_column(_PrimaryKey, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(CHAR(_PUBLIC_ID_LENGTH), nullable=False)
    base_currency: Mapped[str] = mapped_column(CHAR(CURRENCY_LENGTH), nullable=False)
    quote_currency: Mapped[str] = mapped_column(CHAR(CURRENCY_LENGTH), nullable=False)
    # 1 单位 `base_currency` = 多少 MYR；精确，不舍入。
    rate: Mapped[Decimal] = mapped_column(_rate(), nullable=False)
    source: Mapped[FxRateSourceType] = mapped_column(
        Enum(FxRateSourceType, native_enum=False, length=SOURCE_LENGTH), nullable=False
    )
    # BNM：`bnm:exchange-rate:<ccy>:<报价日>:session=1200:middle_rate:unit=<n>`；
    # 手工：必填，录入者写出处。
    source_reference: Mapped[str] = mapped_column(String(SOURCE_REFERENCE_LENGTH), nullable=False)
    # BNM 的报价日（吉隆坡日期）；手工录入可空。
    source_quote_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    observed_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    status: Mapped[FxRateStatus] = mapped_column(
        Enum(FxRateStatus, native_enum=False, length=STATUS_LENGTH), nullable=False
    )
    effective_from: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    effective_to: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    # 生成列（STORED）：只读，ORM 从不写它。
    open_slot: Mapped[int | None] = mapped_column(
        Integer, Computed(OPEN_SLOT_EXPRESSION, persisted=True)
    )
    # BNM 拉取的草稿为空（系统）。
    created_by: Mapped[int | None] = mapped_column(_ForeignKeyInt, nullable=True)
    # 发布人与发布时刻；发布时写。
    approved_by: Mapped[int | None] = mapped_column(_ForeignKeyInt, nullable=True)
    approved_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_fx_rate_versions_created_by",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["approved_by"],
            ["users.id"],
            name="fk_fx_rate_versions_approved_by",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("public_id", name="uq_fx_rate_versions_public_id"),
        # 同一报价日的 BNM 版本只有一条（并发拉取由它决出一条草稿）；手工录入的报价日可空，
        # 唯一约束不管 NULL。
        UniqueConstraint(
            "base_currency",
            "quote_currency",
            "source",
            "source_quote_date",
            name="uq_fx_rate_versions_quote_date",
        ),
        # 同一币种对至多一个未截断的已发布版本。
        Index(
            "ux_fx_rate_versions_open_slot",
            "base_currency",
            "quote_currency",
            "open_slot",
            unique=True,
        ),
        CheckConstraint(BASE_CURRENCY_CHECK, name="ck_fx_rate_versions_base_currency"),
        CheckConstraint(QUOTE_CURRENCY_CHECK, name="ck_fx_rate_versions_quote_currency"),
        CheckConstraint(RATE_CHECK, name="ck_fx_rate_versions_rate"),
        CheckConstraint(SOURCE_CHECK, name="ck_fx_rate_versions_source"),
        CheckConstraint(VERSION_STATUS_CHECK, name="ck_fx_rate_versions_status"),
        CheckConstraint(UNPUBLISHED_CHECK, name="ck_fx_rate_versions_unpublished"),
        CheckConstraint(APPROVED_CHECK, name="ck_fx_rate_versions_approved"),
        CheckConstraint(PERIOD_CHECK, name="ck_fx_rate_versions_period"),
        CheckConstraint(RETIRED_END_CHECK, name="ck_fx_rate_versions_retired_end"),
        CheckConstraint(BNM_QUOTE_DATE_CHECK, name="ck_fx_rate_versions_bnm_quote_date"),
    )


class FxFetchAttempt(Base):
    """One fetch from BNM for one currency and one requested quote date. Append-only.

    不存响应体：失败只记错误码（`TIMEOUT` / `HTTP_<status>` / `BAD_PAYLOAD` / `UNIT_NOT_EXACT`
    / `NETWORK`）。
    """

    __tablename__ = "fx_fetch_attempts"

    id: Mapped[int] = mapped_column(_PrimaryKey, primary_key=True, autoincrement=True)
    base_currency: Mapped[str] = mapped_column(CHAR(CURRENCY_LENGTH), nullable=False)
    # 永远是 BNM（CHECK）：手工录入不经过拉取。
    source: Mapped[FxRateSourceType] = mapped_column(
        Enum(FxRateSourceType, native_enum=False, length=SOURCE_LENGTH), nullable=False
    )
    # 向 BNM 要的报价日（吉隆坡当天）。
    requested_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    outcome: Mapped[FxFetchOutcome] = mapped_column(
        Enum(FxFetchOutcome, native_enum=False, length=OUTCOME_LENGTH), nullable=False
    )
    # 取到报价时的报价日。
    quote_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(ERROR_CODE_LENGTH), nullable=True)
    # `NEW_DRAFT` 时指向新草稿。
    fx_rate_version_id: Mapped[int | None] = mapped_column(_ForeignKeyInt, nullable=True)
    attempted_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["fx_rate_version_id"],
            ["fx_rate_versions.id"],
            name="fk_fx_fetch_attempts_version",
            ondelete="RESTRICT",
        ),
        # `fx_fetch` 告警维度按币种查最近 72 小时。
        Index("ix_fx_fetch_attempts_currency_time", "base_currency", "attempted_at"),
        CheckConstraint(FETCH_SOURCE_CHECK, name="ck_fx_fetch_attempts_source"),
        CheckConstraint(FETCH_OUTCOME_CHECK, name="ck_fx_fetch_attempts_outcome"),
        CheckConstraint(FETCH_OUTCOME_COLUMNS_CHECK, name="ck_fx_fetch_attempts_outcome_columns"),
    )


__all__ = [
    "APPROVED_CHECK",
    "BASE_CURRENCY_CHECK",
    "BNM_QUOTE_DATE_CHECK",
    "FETCH_OUTCOME_CHECK",
    "FETCH_OUTCOME_COLUMNS_CHECK",
    "FETCH_SOURCE_CHECK",
    "FX_RATE_LOCK_ID",
    "OPEN_SLOT_EXPRESSION",
    "PERIOD_CHECK",
    "PERIOD_STATUSES",
    "QUOTE_CURRENCY",
    "QUOTE_CURRENCY_CHECK",
    "RATE_CHECK",
    "RATE_PRECISION",
    "RATE_SCALE",
    "RETIRED_END_CHECK",
    "SOURCE_CHECK",
    "UNPUBLISHED_CHECK",
    "VERSION_STATUS_CHECK",
    "FxFetchAttempt",
    "FxFetchOutcome",
    "FxRateLock",
    "FxRateSourceType",
    "FxRateStatus",
    "FxRateVersion",
]
