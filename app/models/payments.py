"""Payments and payment gateway events (design gate #580 v3, AIH-TASK-172).

spec §43、§63、§74.7、§118；ADR-0004、ADR-0006、ADR-0007、ADR-0008、ADR-0015；Invariant 3。
`payments` 一笔充值一行（Stripe Checkout Session）；`payment_gateway_events` 一个验签通过的
Stripe 事件一行。

本任务只有数据库层：适配器属于 AIH-TASK-173，`confirm_payment` 与入账属于 174，事件落库与处理
任务属于 175，建单属于 176，路由属于 177。`payments` 行由 176 的建单插入、174 的 `confirm_payment`
改状态、175 的认回会话写 `gateway_payment_id`；事件行由 175 写。时间一律不带时区的 UTC。

⚠️ **幂等的三层**（设计 §2，INV-3）里属于这两张表的两层：
1. `uq_payment_gateway_events_event (gateway, gateway_event_id)`：同一事件重复投递只落一行；
   它只去重投递，**不足以挡重复入账**（Stripe 同一笔支付会发多个不同 id 的事件）。
2. `uq_payments_gateway_payment_id`、`uq_payments_gateway_payment_intent_id`：一个 Stripe 付款
   对象只能对应一笔内部支付。
第三层是账本的 `uq_wallet_transactions_reference`（`(PAYMENT, public_id)` 至多一条 `TOPUP`）。

⚠️ 索引名、外键名与条件文本同迁移 0036 一致（空白除外），
tests/backend/test_payments_migration.py 比对两边。
"""

from __future__ import annotations

import datetime as dt
import enum
from typing import Any, Final

from sqlalchemy import (
    CHAR,
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

# 外键指向 tenants 与 tax_policy_versions：import 这两个模块，表才在同一个 metadata 里。
from app.models import tax_policy as _tax_policy_models  # noqa: F401
from app.models import tenancy as _tenancy_models  # noqa: F401
from app.models.base import Base, Money

# 与 app/models/wallet.py 同一个写法（SQLite 只对 INTEGER PRIMARY KEY 自增）。
_PrimaryKey = BigInteger().with_variant(Integer, "sqlite")
_ForeignKeyInt = BigInteger().with_variant(Integer, "sqlite")

_PUBLIC_ID_LENGTH = 36
_CURRENCY_LENGTH = 3
# 设计 §2 写死的列宽。
GATEWAY_LENGTH: Final = 16
PAYMENT_STATUS_LENGTH: Final = 20
STATUS_REASON_LENGTH: Final = 64
GATEWAY_REFERENCE_LENGTH: Final = 255
EVENT_TYPE_LENGTH: Final = 64
EVENT_STATUS_LENGTH: Final = 16
PAYLOAD_FINGERPRINT_LENGTH: Final = 64
ERROR_CODE_LENGTH: Final = 64


class PaymentGateway(enum.StrEnum):
    """本批只实现 Stripe（ADR-0006）。"""

    STRIPE = "STRIPE"


class PaymentStatus(enum.StrEnum):
    """设计 §4 的六种状态。「终结」只指 `PAID` 与 `REVIEW_REQUIRED`；`CANCELLED` 本批没有产生
    路径（预留给以后客户主动取消）；`EXPIRED` 不是终态，过期后到账照样入账（ADR-0007）。"""

    PENDING = "PENDING"
    PAID = "PAID"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class PaymentEventVerificationStatus(enum.StrEnum):
    """只保存验签通过的事件（设计 §6），所以只有一个值。"""

    VERIFIED = "VERIFIED"


class PaymentEventProcessingStatus(enum.StrEnum):
    """设计 §2「事件处理任务」。`REJECTED`：Stripe 拒绝了权威查询（例如会话不存在）或认回会话
    撞了唯一约束。"""

    RECEIVED = "RECEIVED"
    PROCESSING = "PROCESSING"
    RETRY = "RETRY"
    PROCESSED = "PROCESSED"
    IGNORED = "IGNORED"
    UNMATCHED = "UNMATCHED"
    REJECTED = "REJECTED"


# ⚠️ 条件文本与迁移 0036 一致（空白除外）。
PAYMENT_GATEWAY_CHECK: Final = "gateway = 'STRIPE'"
EXPECTED_AMOUNT_CHECK: Final = "expected_amount > 0 AND expected_amount = ROUND(expected_amount, 2)"
EXPECTED_CURRENCY_CHECK: Final = "expected_currency = 'MYR'"
WALLET_CREDIT_AMOUNT_CHECK: Final = "wallet_credit_amount = expected_amount"
TAX_AMOUNT_CHECK: Final = "tax_amount IS NULL"
PAYMENT_STATUS_CHECK: Final = (
    "status IN ('PENDING', 'PAID', 'FAILED', 'EXPIRED', 'CANCELLED', 'REVIEW_REQUIRED')"
)
PAID_FIELDS_CHECK: Final = (
    "status <> 'PAID' OR (paid_at IS NOT NULL AND confirmed_amount IS NOT NULL"
    " AND confirmed_currency IS NOT NULL)"
)
REVIEW_AT_CHECK: Final = "status <> 'REVIEW_REQUIRED' OR review_required_at IS NOT NULL"
EVENT_GATEWAY_CHECK: Final = "gateway = 'STRIPE'"
VERIFICATION_STATUS_CHECK: Final = "verification_status = 'VERIFIED'"
PROCESSING_STATUS_CHECK: Final = (
    "processing_status IN ('RECEIVED', 'PROCESSING', 'RETRY', 'PROCESSED', 'IGNORED',"
    " 'UNMATCHED', 'REJECTED')"
)


class Payment(Base):
    """One top-up (设计 §2「数据库」).

    ⚠️ 不要绕过 `confirm_payment`（AIH-TASK-174）改 `status`：`PAID` 必须与 `TOPUP` 账本行、
    `PAYMENT_STATUS_CHANGE` 审计同一事务提交（INV-13），而且只能在锁住本行之后判定（INV-3）。
    `PAID` 与 `REVIEW_REQUIRED` 不会被自动改变。
    """

    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(_PrimaryKey, primary_key=True, autoincrement=True)
    # uuid4；对外 id，也是账本引用（`reference_id`）与 Stripe `client_reference_id`。
    public_id: Mapped[str] = mapped_column(CHAR(_PUBLIC_ID_LENGTH), nullable=False)
    tenant_id: Mapped[int] = mapped_column(_ForeignKeyInt, nullable=False)
    gateway: Mapped[PaymentGateway] = mapped_column(
        Enum(PaymentGateway, native_enum=False, length=GATEWAY_LENGTH), nullable=False
    )
    # 客户要付的含税毛额，两位小数。
    expected_amount: Mapped[Money] = mapped_column(nullable=False)
    expected_currency: Mapped[str] = mapped_column(CHAR(_CURRENCY_LENGTH), nullable=False)
    # ADR-0008：等于 `expected_amount`，手续费不影响入账额。
    wallet_credit_amount: Mapped[Money] = mapped_column(nullable=False)
    # 建单时生效的税务政策版本（INV-6）。
    tax_policy_version_id: Mapped[int] = mapped_column(_ForeignKeyInt, nullable=False)
    # ADR-0008：未注册 SST，恒为空。
    tax_amount: Mapped[Money | None] = mapped_column(nullable=True)
    # 可信确认时 Stripe 报的金额与币种（进入 `REVIEW_REQUIRED` 时也照样记下）。
    confirmed_amount: Mapped[Money | None] = mapped_column(nullable=True)
    confirmed_currency: Mapped[str | None] = mapped_column(CHAR(_CURRENCY_LENGTH), nullable=True)
    # Checkout Session id（`cs_…`）；建单成功或认回会话后写入，之后不变。
    gateway_payment_id: Mapped[str | None] = mapped_column(
        String(GATEWAY_REFERENCE_LENGTH), nullable=True
    )
    # 付款后才产生的 PaymentIntent id（`pi_…`）。
    gateway_payment_intent_id: Mapped[str | None] = mapped_column(
        String(GATEWAY_REFERENCE_LENGTH), nullable=True
    )
    # Stripe 余额流水里的逐笔实际手续费（MYR）；拿不到时为空。
    gateway_fee: Mapped[Money | None] = mapped_column(nullable=True)
    status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus, native_enum=False, length=PAYMENT_STATUS_LENGTH), nullable=False
    )
    # 设计 §2「状态原因」的取值。
    status_reason: Mapped[str | None] = mapped_column(String(STATUS_REASON_LENGTH), nullable=True)
    # 进入 `REVIEW_REQUIRED` 的时刻（告警的 24 小时窗口用它）。
    review_required_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    # 建单时刻 + 待付超时。
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    # 本系统做出可信确认的时刻，不是 Stripe 的付款时刻。
    paid_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    # 每次向 Stripe 查询后更新。
    last_reconciled_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint("public_id", name="uq_payments_public_id"),
        # 幂等的第二层：一个 Stripe 付款对象只能对应一笔内部支付（spec §118）。
        UniqueConstraint("gateway_payment_id", name="uq_payments_gateway_payment_id"),
        UniqueConstraint("gateway_payment_intent_id", name="uq_payments_gateway_payment_intent_id"),
        # 按租户列；也是 tenant_id 外键用的索引。
        Index("ix_payments_tenant_created", "tenant_id", "created_at"),
        Index("ix_payments_status_created", "status", "created_at"),
        # 60 秒同额复用的查找（ADR-0007）。
        Index("ix_payments_reuse", "tenant_id", "status", "expected_amount", "created_at"),
        # 显式声明：外键用它，MySQL 不另建隐式索引。
        Index("ix_payments_tax_policy_version_id", "tax_policy_version_id"),
        Index("ix_payments_review_required_at", "review_required_at"),
        # RESTRICT：删租户或税务政策版本不许连带删掉支付记录。
        ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_payments_tenant_id", ondelete="RESTRICT"
        ),
        ForeignKeyConstraint(
            ["tax_policy_version_id"],
            ["tax_policy_versions.id"],
            name="fk_payments_tax_policy_version_id",
            ondelete="RESTRICT",
        ),
        CheckConstraint(PAYMENT_GATEWAY_CHECK, name="ck_payments_gateway"),
        CheckConstraint(EXPECTED_AMOUNT_CHECK, name="ck_payments_expected_amount"),
        CheckConstraint(EXPECTED_CURRENCY_CHECK, name="ck_payments_expected_currency"),
        CheckConstraint(WALLET_CREDIT_AMOUNT_CHECK, name="ck_payments_wallet_credit_amount"),
        CheckConstraint(TAX_AMOUNT_CHECK, name="ck_payments_tax_amount"),
        CheckConstraint(PAYMENT_STATUS_CHECK, name="ck_payments_status"),
        CheckConstraint(PAID_FIELDS_CHECK, name="ck_payments_paid_fields"),
        CheckConstraint(REVIEW_AT_CHECK, name="ck_payments_review_at"),
    )


class PaymentGatewayEvent(Base):
    """One verified gateway webhook event (设计 §2「数据库」).

    只保存验签通过的事件；`metadata_json` 只存设计 §6 的白名单字段，**不存原始请求体**（Stripe
    的会话里有客户邮箱、姓名等个人数据）。同一 `(gateway, gateway_event_id)` 只落一行，内容不同的
    重复投递只记 `fingerprint_conflict_at`、不覆盖原行。
    """

    __tablename__ = "payment_gateway_events"

    id: Mapped[int] = mapped_column(_PrimaryKey, primary_key=True, autoincrement=True)
    gateway: Mapped[PaymentGateway] = mapped_column(
        Enum(PaymentGateway, native_enum=False, length=GATEWAY_LENGTH), nullable=False
    )
    # Stripe 事件 `evt_…`。
    gateway_event_id: Mapped[str] = mapped_column(String(GATEWAY_REFERENCE_LENGTH), nullable=False)
    event_type: Mapped[str] = mapped_column(String(EVENT_TYPE_LENGTH), nullable=False)
    # 匹配到支付后写入。
    payment_id: Mapped[int | None] = mapped_column(_ForeignKeyInt, nullable=True)
    # 原始请求体的 SHA-256，小写十六进制。
    payload_fingerprint: Mapped[str] = mapped_column(
        CHAR(PAYLOAD_FINGERPRINT_LENGTH), nullable=False
    )
    verification_status: Mapped[PaymentEventVerificationStatus] = mapped_column(
        Enum(PaymentEventVerificationStatus, native_enum=False, length=EVENT_STATUS_LENGTH),
        nullable=False,
    )
    processing_status: Mapped[PaymentEventProcessingStatus] = mapped_column(
        Enum(PaymentEventProcessingStatus, native_enum=False, length=EVENT_STATUS_LENGTH),
        nullable=False,
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    # 落库时等于 `received_at`（`IGNORED` 也一样）。
    next_attempt_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    lease_until: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    # `LIVEMODE_MISMATCH`、Stripe 的错误码等；进入 `PROCESSED` 时清空。
    last_error_code: Mapped[str | None] = mapped_column(String(ERROR_CODE_LENGTH), nullable=True)
    # 同一事件 id 后来收到内容不同的请求体时记下。
    fingerprint_conflict_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    received_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    # 进入 `PROCESSED`、`IGNORED`、`UNMATCHED`、`REJECTED` 时写入。
    processed_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    __table_args__ = (
        # 幂等的第一层：同一事件重复投递只落一行（ADR-0004）。
        UniqueConstraint("gateway", "gateway_event_id", name="uq_payment_gateway_events_event"),
        # 领取到期的 RECEIVED / RETRY 事件。
        Index("ix_payment_gateway_events_due", "processing_status", "next_attempt_at"),
        # 显式声明：外键用它。
        Index("ix_payment_gateway_events_payment_id", "payment_id"),
        # 告警查最早的未处理事件。
        Index("ix_payment_gateway_events_received", "received_at"),
        ForeignKeyConstraint(
            ["payment_id"],
            ["payments.id"],
            name="fk_payment_gateway_events_payment_id",
            ondelete="RESTRICT",
        ),
        CheckConstraint(EVENT_GATEWAY_CHECK, name="ck_payment_gateway_events_gateway"),
        CheckConstraint(
            VERIFICATION_STATUS_CHECK, name="ck_payment_gateway_events_verification_status"
        ),
        CheckConstraint(
            PROCESSING_STATUS_CHECK, name="ck_payment_gateway_events_processing_status"
        ),
    )


__all__ = [
    "EVENT_GATEWAY_CHECK",
    "EXPECTED_AMOUNT_CHECK",
    "EXPECTED_CURRENCY_CHECK",
    "PAID_FIELDS_CHECK",
    "PAYMENT_GATEWAY_CHECK",
    "PAYMENT_STATUS_CHECK",
    "PROCESSING_STATUS_CHECK",
    "REVIEW_AT_CHECK",
    "TAX_AMOUNT_CHECK",
    "VERIFICATION_STATUS_CHECK",
    "WALLET_CREDIT_AMOUNT_CHECK",
    "Payment",
    "PaymentEventProcessingStatus",
    "PaymentEventVerificationStatus",
    "PaymentGateway",
    "PaymentGatewayEvent",
    "PaymentStatus",
]
