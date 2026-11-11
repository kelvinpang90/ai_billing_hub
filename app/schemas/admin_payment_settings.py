"""Admin payment settings: the view and the replacement request (AIH-TASK-191).

设计闸门 #587 v3 §2「管理端接口」「支付设置服务」；契约见 [docs/api.md](../../docs/api.md)
「支付设置（管理端）」。

⚠️ **视图是字段白名单**：恰好 9 个键，按设计 JSON 示例的顺序。`updated_by` 是管理员的邮箱，
种子行为 null；内部的 `updated_by_user_id` 不出这里。

⚠️ 金额是**两位小数**的定点字符串（例如 `"50.00"`），不经 float（INV-10）；时刻是不带时区的 UTC、
精确到秒。

请求体是视图的前 7 个键（全部业务字段加 `version`），`extra="forbid"`、`strict=True`：金额必须是
JSON 字符串、推荐金额必须是字符串数组、两个秒数与 `version` 必须是 JSON 整数；布尔值、字符串形式
的数字、数字形式的金额都是 422 `VALIDATION_ERROR`。**规则**（格式、范围、顺序）不在这里校验，
由 `update_payment_settings` 按设计的字段顺序给出 422 `PAYMENT_SETTINGS_INVALID`。
"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, ConfigDict

from app.schemas.customer_payments import cents_text
from app.services.payment_settings import PaymentSettings, PaymentSettingsValues


class PaymentSettingsUpdateRequest(BaseModel):
    """`PUT /api/v1/admin/payment-settings`: a whole replacement, with the version read."""

    model_config = ConfigDict(extra="forbid", strict=True)

    min_amount: str
    # null 表示不设最高额；字段本身必须给。
    max_amount: str | None
    recovery_buffer: str
    preset_amounts: list[str]
    reuse_window_seconds: int
    pending_timeout_seconds: int
    # 读到的 `version`（乐观锁）；不是当前版本 → 409。
    version: int

    def to_values(self) -> PaymentSettingsValues:
        return PaymentSettingsValues(
            min_amount=self.min_amount,
            max_amount=self.max_amount,
            recovery_buffer=self.recovery_buffer,
            preset_amounts=self.preset_amounts,
            reuse_window_seconds=self.reuse_window_seconds,
            pending_timeout_seconds=self.pending_timeout_seconds,
        )


class AdminPaymentSettingsView(BaseModel):
    """`GET` / `PUT /api/v1/admin/payment-settings`."""

    min_amount: str
    max_amount: str | None
    recovery_buffer: str
    preset_amounts: list[str]
    reuse_window_seconds: int
    pending_timeout_seconds: int
    version: int
    updated_at: dt.datetime
    # 最后修改的管理员邮箱；种子行为 null。
    updated_by: str | None


def admin_payment_settings_view(
    settings: PaymentSettings, updated_by: str | None
) -> AdminPaymentSettingsView:
    return AdminPaymentSettingsView(
        min_amount=cents_text(settings.min_amount),
        max_amount=None if settings.max_amount is None else cents_text(settings.max_amount),
        recovery_buffer=cents_text(settings.recovery_buffer),
        preset_amounts=[cents_text(amount) for amount in settings.preset_amounts],
        reuse_window_seconds=settings.reuse_window_seconds,
        pending_timeout_seconds=settings.pending_timeout_seconds,
        version=settings.version,
        updated_at=settings.updated_at.replace(microsecond=0),
        updated_by=updated_by,
    )


__all__ = [
    "AdminPaymentSettingsView",
    "PaymentSettingsUpdateRequest",
    "admin_payment_settings_view",
]
