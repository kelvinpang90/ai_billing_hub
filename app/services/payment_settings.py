"""Payment settings: read, replace with optimistic locking and audit, the recovery minimum.

设计闸门 #587 v3 §2「支付设置服务」「最小恢复额」「事务边界」「金额与精度」（AIH-TASK-189）。

- `get_payment_settings`：普通读（不加锁）`payment_settings` 那一行；
  不存在 → `PaymentSettingsMissing`（503 `PAYMENTS_MISCONFIGURED`，只会在迁移没跑时出现）。
- `update_payment_settings`：整体替换。先按设计的字段顺序逐条校验规则（类型由请求层严格校验），
  再在一个事务里普通读那一行、`WHERE id = 1 AND version = expected_version` 条件更新并写
  `PAYMENT_SETTINGS_UPDATE` 审计（INV-6、INV-13）。影响 0 行 → 回滚、409。新值与旧值相同也照常
  `version + 1` 并写审计：管理员的确认动作本身要留痕。
- `recovery_minimum` / `effective_minimum` / `topup_cap` / `recovery_exceeds_cap`：建单（A 的
  `create_topup`）与充值选项（AIH-TASK-190）共用的金额规则。

⚠️ 金额一律是两位小数的 `Decimal`，比较全部在 `Decimal` 上做，不经 `float`（INV-10）；推荐金额在
JSON 里存定点字符串（例如 `"50.00"`），读出时转回 `Decimal`。

⚠️ 日志只记管理员用户 id 与新版本号，不记请求体（设计 §6）。
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from typing import Final

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import session_scope
from app.core.errors import AppError
from app.models.auth import AuditAction, User
from app.models.payment_settings import PAYMENT_SETTINGS_ID
from app.models.payment_settings import PaymentSettings as PaymentSettingsRow
from app.models.tenancy import BillingMode
from app.services.auth import RequestContext, record_audit

logger = logging.getLogger(__name__)

# 审计的 `entity_type`；`entity_id` 恒为单行表的主键 "1"（见 app/services/audit_query.py）。
ENTITY_PAYMENT_SETTINGS: Final = "payment_settings"

# ADR-0015：MYR 单笔最低 RM2。
MIN_AMOUNT_FLOOR: Final = Decimal("2.00")
# 缓冲至少 0.01：按最小恢复额充值后余额严格大于 0（设计 §2「最小恢复额」）。
RECOVERY_BUFFER_FLOOR: Final = Decimal("0.01")
# 单笔上限：`max_amount` 为空时为 999999.99（A 的金额正则决定，Stripe 单笔上限）。
TOPUP_CEILING: Final = Decimal("999999.99")
PRESET_AMOUNTS_MAX: Final = 8
REUSE_WINDOW_RANGE: Final = (0, 300)
PENDING_TIMEOUT_RANGE: Final = (2400, 86_400)

# 与 A 的建单同一个正则（app/services/payments.py）：至多六位整数、两位小数。
# `[0-9]` 而不是 `\d`：全角数字等 Unicode 数字不算。
_AMOUNT_PATTERN: Final = re.compile(r"[0-9]{1,6}(\.[0-9]{1,2})?")
_CENT: Final = Decimal("0.01")


class PaymentSettingsMissing(AppError):
    """The `payment_settings` row does not exist (migration 0038 has not run)."""

    code = "PAYMENTS_MISCONFIGURED"
    http_status = 503

    def __init__(self) -> None:
        super().__init__("Payments are not configured.")


class PaymentSettingsInvalid(AppError):
    """A rule failed; the message names the first failing field in the design's order."""

    code = "PAYMENT_SETTINGS_INVALID"
    http_status = 422

    def __init__(self, field: str) -> None:
        self.field = field
        super().__init__(f"Invalid payment settings: {field}.")


class PaymentSettingsVersionConflict(AppError):
    """`expected_version` is not the current version: someone else changed the settings."""

    code = "PAYMENT_SETTINGS_VERSION_CONFLICT"
    http_status = 409

    def __init__(self) -> None:
        super().__init__("The payment settings were changed by someone else.")


@dataclass(frozen=True)
class PaymentSettings:
    """The one row, amounts as two-decimal `Decimal`s (设计 §2「数据库」)."""

    min_amount: Decimal
    max_amount: Decimal | None
    recovery_buffer: Decimal
    preset_amounts: tuple[Decimal, ...]
    reuse_window_seconds: int
    pending_timeout_seconds: int
    version: int
    updated_at: dt.datetime
    updated_by_user_id: int | None


@dataclass(frozen=True)
class PaymentSettingsValues:
    """All business fields of a replacement, as the request layer passed them (金额是字符串)."""

    min_amount: str
    max_amount: str | None
    recovery_buffer: str
    preset_amounts: Sequence[str]
    reuse_window_seconds: int
    pending_timeout_seconds: int


@dataclass(frozen=True)
class _Rules:
    """`PaymentSettingsValues` after validation."""

    min_amount: Decimal
    max_amount: Decimal | None
    recovery_buffer: Decimal
    preset_amounts: tuple[Decimal, ...]
    reuse_window_seconds: int
    pending_timeout_seconds: int


def get_payment_settings(session: Session) -> PaymentSettings:
    """The one row, read without a lock. `PaymentSettingsMissing` when there is none."""
    row = session.execute(
        select(PaymentSettingsRow).where(PaymentSettingsRow.id == PAYMENT_SETTINGS_ID)
    ).scalar_one_or_none()
    if row is None:
        raise PaymentSettingsMissing
    return PaymentSettings(
        min_amount=_cents(row.min_amount),
        max_amount=None if row.max_amount is None else _cents(row.max_amount),
        recovery_buffer=_cents(row.recovery_buffer),
        preset_amounts=tuple(_cents(Decimal(text)) for text in row.preset_amounts),
        reuse_window_seconds=row.reuse_window_seconds,
        pending_timeout_seconds=row.pending_timeout_seconds,
        version=row.version,
        updated_at=row.updated_at,
        updated_by_user_id=row.updated_by_user_id,
    )


def validate_payment_settings(values: PaymentSettingsValues) -> _Rules:
    """Check the rules field by field, in the design's order; the first failure is raised."""
    minimum = _amount("min_amount", values.min_amount)
    if minimum < MIN_AMOUNT_FLOOR:
        raise PaymentSettingsInvalid("min_amount")
    maximum = None
    if values.max_amount is not None:
        maximum = _amount("max_amount", values.max_amount)
        if maximum < minimum:
            raise PaymentSettingsInvalid("max_amount")
    buffer = _amount("recovery_buffer", values.recovery_buffer)
    if buffer < RECOVERY_BUFFER_FLOOR:
        raise PaymentSettingsInvalid("recovery_buffer")
    presets = _preset_amounts(values.preset_amounts, minimum, maximum)
    window = _seconds("reuse_window_seconds", values.reuse_window_seconds, REUSE_WINDOW_RANGE)
    timeout = _seconds(
        "pending_timeout_seconds", values.pending_timeout_seconds, PENDING_TIMEOUT_RANGE
    )
    return _Rules(minimum, maximum, buffer, presets, window, timeout)


def update_payment_settings(
    session_factory: sessionmaker[Session],
    *,
    actor: User,
    values: PaymentSettingsValues,
    expected_version: int,
    context: RequestContext,
    now: dt.datetime,
) -> PaymentSettings:
    """Replace every business field when `expected_version` is current (设计 §2 三步).

    规则不过：`PaymentSettingsInvalid`，不连库。行缺失：`PaymentSettingsMissing`（不当作版本冲突）。
    版本不符：`PaymentSettingsVersionConflict`，整个事务回滚、数据不变。`now` 是不带时区的 UTC，
    截到整秒后写进 `updated_at` 与审计。
    """
    rules = validate_payment_settings(values)
    stamp = now.replace(microsecond=0)
    with session_scope(session_factory) as session:
        before = get_payment_settings(session)
        written = session.execute(
            update(PaymentSettingsRow)
            .where(
                PaymentSettingsRow.id == PAYMENT_SETTINGS_ID,
                PaymentSettingsRow.version == expected_version,
            )
            .values(
                min_amount=rules.min_amount,
                max_amount=rules.max_amount,
                recovery_buffer=rules.recovery_buffer,
                preset_amounts=[_money_text(amount) for amount in rules.preset_amounts],
                reuse_window_seconds=rules.reuse_window_seconds,
                pending_timeout_seconds=rules.pending_timeout_seconds,
                version=PaymentSettingsRow.version + 1,
                updated_at=stamp,
                updated_by_user_id=actor.id,
            )
            .execution_options(synchronize_session=False)
        ).rowcount
        if written != 1:
            # 抛出即回滚（`session_scope`）：别人的修改原样保留。
            raise PaymentSettingsVersionConflict
        after = PaymentSettings(
            min_amount=rules.min_amount,
            max_amount=rules.max_amount,
            recovery_buffer=rules.recovery_buffer,
            preset_amounts=rules.preset_amounts,
            reuse_window_seconds=rules.reuse_window_seconds,
            pending_timeout_seconds=rules.pending_timeout_seconds,
            version=expected_version + 1,
            updated_at=stamp,
            updated_by_user_id=actor.id,
        )
        record_audit(
            session,
            action=AuditAction.PAYMENT_SETTINGS_UPDATE,
            context=context,
            now=stamp,
            actor=actor,
            entity_type=ENTITY_PAYMENT_SETTINGS,
            entity_id=str(PAYMENT_SETTINGS_ID),
            before_state=_audit_state(before),
            after_state=_audit_state(after),
        )
    logger.info("Payment settings updated", extra={"user_id": actor.id, "version": after.version})
    return after


# --- 最小恢复额（设计 §2「最小恢复额」） ------------------------------------------------------


def recovery_minimum(
    balance: Decimal, billing_mode: BillingMode, settings: PaymentSettings
) -> Decimal | None:
    """`ceil(−balance + recovery_buffer)` to the cent for a `PREPAID` tenant at or below 0.

    其他情形为 `None`。`balance` 是 DECIMAL(20,8)，欠款可能有两位以上小数：向上取保证充值后余额
    不小于缓冲（至少 RM0.01，严格大于 0）。
    """
    if billing_mode is not BillingMode.PREPAID or balance > 0:
        return None
    return (settings.recovery_buffer - balance).quantize(_CENT, rounding=ROUND_CEILING)


def effective_minimum(
    balance: Decimal, billing_mode: BillingMode, settings: PaymentSettings
) -> Decimal:
    """`max(min_amount, recovery_minimum)`; `min_amount` when there is no recovery minimum."""
    recovery = recovery_minimum(balance, billing_mode, settings)
    if recovery is None:
        return settings.min_amount
    return max(settings.min_amount, recovery)


def topup_cap(settings: PaymentSettings) -> Decimal:
    """The single top-up ceiling: `max_amount`, or 999999.99 when it is not set."""
    return TOPUP_CEILING if settings.max_amount is None else settings.max_amount


def recovery_exceeds_cap(minimum: Decimal, settings: PaymentSettings) -> bool:
    """No amount can be accepted: the effective minimum is above the ceiling.

    充值选项据此给出 `RECOVERY_EXCEEDS_MAXIMUM`（AIH-TASK-190）。
    """
    return minimum > topup_cap(settings)


# --- 校验与写法 -----------------------------------------------------------------------------


def _amount(field: str, value: object) -> Decimal:
    if type(value) is not str or not _AMOUNT_PATTERN.fullmatch(value):
        raise PaymentSettingsInvalid(field)
    return Decimal(value).quantize(_CENT)


def _preset_amounts(
    values: object, minimum: Decimal, maximum: Decimal | None
) -> tuple[Decimal, ...]:
    """1–8 amounts, each in the amount format, strictly ascending (so distinct), within range."""
    field = "preset_amounts"
    if not isinstance(values, list | tuple) or not 1 <= len(values) <= PRESET_AMOUNTS_MAX:
        raise PaymentSettingsInvalid(field)
    amounts: list[Decimal] = []
    for value in values:
        amount = _amount(field, value)
        if amounts and amount <= amounts[-1]:
            raise PaymentSettingsInvalid(field)
        if amount < minimum or (maximum is not None and amount > maximum):
            raise PaymentSettingsInvalid(field)
        amounts.append(amount)
    return tuple(amounts)


def _seconds(field: str, value: object, bounds: tuple[int, int]) -> int:
    # `bool` 是 `int` 的子类：`type(...) is int` 才挡得住 True / False。
    if type(value) is not int or not bounds[0] <= value <= bounds[1]:
        raise PaymentSettingsInvalid(field)
    return value


def _cents(value: Decimal) -> Decimal:
    # DECIMAL(20,8) 读回来是八位小数；CHECK 保证它本来就是整分。
    return value.quantize(_CENT)


def _money_text(value: Decimal | None) -> str | None:
    # 与 app/repositories/wallet.py 的 `_money_text` 同一写法：定点，不出现 "0E-8"。
    return None if value is None else format(value, "f")


def _audit_state(settings: PaymentSettings) -> dict[str, object]:
    """Every business field and `version` (设计 §2 第 2 步)."""
    return {
        "min_amount": _money_text(settings.min_amount),
        "max_amount": _money_text(settings.max_amount),
        "recovery_buffer": _money_text(settings.recovery_buffer),
        "preset_amounts": [_money_text(amount) for amount in settings.preset_amounts],
        "reuse_window_seconds": settings.reuse_window_seconds,
        "pending_timeout_seconds": settings.pending_timeout_seconds,
        "version": settings.version,
    }


__all__ = [
    "ENTITY_PAYMENT_SETTINGS",
    "MIN_AMOUNT_FLOOR",
    "RECOVERY_BUFFER_FLOOR",
    "TOPUP_CEILING",
    "PaymentSettings",
    "PaymentSettingsInvalid",
    "PaymentSettingsMissing",
    "PaymentSettingsValues",
    "PaymentSettingsVersionConflict",
    "effective_minimum",
    "get_payment_settings",
    "recovery_exceeds_cap",
    "recovery_minimum",
    "topup_cap",
    "update_payment_settings",
    "validate_payment_settings",
]
