"""Request and response shapes for FX rates (design gate #183 v3 §2「接口」, AIH-TASK-041).

⚠️ **`rate` 只收 JSON 字符串**，按十进制正则精确解析、不经过 `float`
（INV-10）：JSON 数字、指数写法、符号、0 一律 422；超过 10 位小数也是 422，
**不舍入**。`rate` 是 1 单位 `base_currency` 等于多少 MYR。

⚠️ 手工草稿的 `observed_at` 与发布的 `effective_from` 同一规则（设计 v3）：
RFC 3339、**必须带时区**，换算成 UTC 后必须是整秒，否则 422；存 UTC
（用的就是 app/schemas/provider_prices.py 的 `Moment`）。

⚠️ `base_currency` 是任何 `^[A-Z]{3}$` 且不是 MYR 的代码，**不限于**
`BILLING_FX_CURRENCIES`：那是 BNM 自动拉取的范围，手工录入正是为 BNM
不覆盖的情形准备的。

⚠️ 请求体一律 `extra="forbid"`：状态、区间、来源、报价日、发布人与任何 id
都只由服务端写。PATCH 不收 `base_currency`：币种建后不可改。请求体由
FastAPI 在处理函数之前校验，所以**请求校验先于状态判断**（设计 v3）：
格式不对的 `effective_from` 无论版本处于什么状态都是 422。

⚠️ **响应模型是字段白名单**：内部自增 id、`created_by` / `approved_by`
的内部值与生成列 `open_slot` 都不会出现在响应里。对外的 id 一律是
`public_id`，人一律用登录邮箱表示。
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal
from typing import Annotated, Final

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    StringConstraints,
    WithJsonSchema,
    field_validator,
    model_validator,
)

from app.models.fx_rates import QUOTE_CURRENCY
from app.schemas.provider_prices import Moment, Reason, SourceReference
from app.services.fx_rates import FxFetchAttemptView, FxRateVersionView, rate_text

# DECIMAL(24,10)：整数部分最多 14 位，小数最多 10 位，不带符号。⚠️ 用 [0-9] 而不是 \d：
# \d 还认全角与其他文字的数字。
_RATE_PATTERN: Final = re.compile(r"[0-9]{1,14}(\.[0-9]{1,10})?")


def _parse_rate(value: object) -> Decimal:
    """A positive decimal string → `Decimal`, never through float. Anything else is refused."""
    # 422 只列字段名，不回显值（app/core/errors.py），所以这里的文案里也不放值。
    if not isinstance(value, str):
        raise ValueError("must be a JSON string")
    if _RATE_PATTERN.fullmatch(value) is None:
        raise ValueError("is not a decimal with at most 14 integer and 10 fraction digits")
    rate = Decimal(value)
    if rate.is_zero():
        raise ValueError("must be positive")
    return rate


def _not_myr(value: str) -> str:
    if value == QUOTE_CURRENCY:
        raise ValueError("must not be MYR")
    return value


Rate = Annotated[
    Decimal,
    BeforeValidator(_parse_rate),
    WithJsonSchema({"type": "string", "pattern": _RATE_PATTERN.pattern}),
]
# ISO 4217 大写三字母（大小写敏感：`usd` 是 422）。
Currency = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]


class CreateFxRateRequest(BaseModel):
    """手工建草稿：四个字段都必填。来源固定为 `MANUAL`，报价日为空。"""

    model_config = ConfigDict(extra="forbid")

    base_currency: Currency
    rate: Rate
    # 录入者给出的观测时刻；不许 `null`（下面的校验）。
    observed_at: Moment
    source_reference: SourceReference

    @field_validator("base_currency")
    @classmethod
    def _base_is_not_myr(cls, value: str) -> str:
        return _not_myr(value)

    @field_validator("observed_at", mode="before")
    @classmethod
    def _observed_at_not_null(cls, value: object) -> object:
        if value is None:
            raise ValueError("must not be null")
        return value


class UpdateFxRateRequest(BaseModel):
    """PATCH 手工草稿：只改请求体里出现的字段。

    三个字段都不许显式传 `null`；一个字段都不带是 422（与价格的 PATCH 一致）。
    """

    model_config = ConfigDict(extra="forbid")

    rate: Rate | None = None
    observed_at: Moment = None
    source_reference: SourceReference | None = None

    @field_validator("rate", "observed_at", "source_reference", mode="before")
    @classmethod
    def _not_null(cls, value: object) -> object:
        # 只对显式传入的值运行（默认值不校验），所以「没带」与「带了 null」分得开。
        if value is None:
            raise ValueError("must not be null")
        return value

    @model_validator(mode="after")
    def _at_least_one_field(self) -> UpdateFxRateRequest:
        if not self.model_fields_set:
            raise ValueError("no fields to update")
        return self


class PublishFxRateRequest(BaseModel):
    """`effective_from` 可选：不带 = 按设计 §2「发布」的 P1–P4 定起点。"""

    model_config = ConfigDict(extra="forbid")

    effective_from: Moment = None


class RetireFxRateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Reason


class DiscardFxRateRequest(BaseModel):
    """请求体就是 `{}`：要丢弃的草稿只来自路径。"""

    model_config = ConfigDict(extra="forbid")


# --- 响应 -------------------------------------------------------------------------


class FxRateView(BaseModel):
    """一版汇率：1 单位 `base_currency` 等于 `rate` MYR。

    `effective_from` 为 `null`：草稿，或该币种时间线为空时发布、未指定时刻 =「一直以来」；
    `effective_to` 为 `null`：草稿，或仍生效。
    """

    id: str
    base_currency: str
    quote_currency: str
    # 精确的十进制字符串（去掉末尾的 0），永远不是浮点数。
    rate: str
    source: str
    source_reference: str
    source_quote_date: dt.date | None
    observed_at: dt.datetime
    status: str
    effective_from: dt.datetime | None
    effective_to: dt.datetime | None
    created_by_email: str | None
    approved_by_email: str | None
    approved_at: dt.datetime | None
    created_at: dt.datetime
    updated_at: dt.datetime


class FxFetchAttemptItem(BaseModel):
    """一次 BNM 拉取。没有 id：拉取记录只读、不可单条引用（设计 v3）。"""

    base_currency: str
    source: str
    requested_date: dt.date
    outcome: str
    quote_date: dt.date | None
    error_code: str | None
    fx_rate_id: str | None
    attempted_at: dt.datetime


def fx_rate_view(version: FxRateVersionView) -> FxRateView:
    return FxRateView(
        id=version.id,
        base_currency=version.base_currency,
        quote_currency=version.quote_currency,
        rate=rate_text(version.rate),
        source=version.source.value,
        source_reference=version.source_reference,
        source_quote_date=version.source_quote_date,
        observed_at=version.observed_at,
        status=version.status.value,
        effective_from=version.effective_from,
        effective_to=version.effective_to,
        created_by_email=version.created_by_email,
        approved_by_email=version.approved_by_email,
        approved_at=version.approved_at,
        created_at=version.created_at,
        updated_at=version.updated_at,
    )


def fetch_attempt_item(attempt: FxFetchAttemptView) -> FxFetchAttemptItem:
    return FxFetchAttemptItem(
        base_currency=attempt.base_currency,
        source=attempt.source.value,
        requested_date=attempt.requested_date,
        outcome=attempt.outcome.value,
        quote_date=attempt.quote_date,
        error_code=attempt.error_code,
        fx_rate_id=attempt.fx_rate_id,
        attempted_at=attempt.attempted_at,
    )
