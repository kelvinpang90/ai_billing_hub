"""Request and response shapes of `POST …/integration/usage-events` (design gate #176 v8 §2).

这里只做校验顺序的第 ④ 步：JSON 顶层结构、字段名（`extra="forbid"`）、各字段的类型与格式。
**不含**按形态的字段组校验 —— 那要先查到 `usage_type` 的形态（第 ⑥、⑦ 步，
app/services/usage_ingest.py），否则未知类型会先被判成 `VALIDATION_ERROR`。

⚠️ **严格类型**（`strict=True`）：JSON 的 `true` 不是整数，`1.0` 不是整数，数字不是字符串。
`quantity` 只收字符串，按十进制解析、绝不经过 float（INV-10 的同一做法）。

⚠️ 没有客户计算的费用、成本字段：出现即 422（§11「Do NOT send calculated customer cost」）；
也没有任何 prompt / 回复字段（INV-9、REQ-PRIV-001）。

正则一律用显式字符类（`[0-9]` 而不是 `\\d`）：`\\d` 还认全角与其他文字的数字。
"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel, ConfigDict, Field

# 只接受的上报版本（第 ⑤ 步单独判，不在这里卡：别的字符串报 `UNSUPPORTED_SCHEMA_VERSION`）。
SCHEMA_VERSION: Final = "1.0"

# UUIDv7：小写、带连字符，版本位 7、变体位 10。ULID：26 个大写 Crockford base32 字符，
# 首字符 ≤ 7 保证不溢出 128 位。只收规范写法：同一个 ID 只有一种字节表示（设计 §2）。
UUID7_PATTERN: Final = r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
ULID_PATTERN: Final = r"[0-7][0-9A-HJKMNP-TV-Z]{25}"
EVENT_ID_PATTERN: Final = rf"^(?:{UUID7_PATTERN}|{ULID_PATTERN})$"
# `request_id`、`conversation_id`，以及请求头 `X-Acuven-Request-Id`。
REQUEST_ID_PATTERN: Final = r"^[A-Za-z0-9._:-]{1,128}$"
# AIH-TASK-025 的供应商代码与模型代码 / 别名写法。
PROVIDER_PATTERN: Final = r"^[a-z0-9][a-z0-9_-]{0,63}$"
MODEL_PATTERN: Final = r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}$"
# 最多 12 位整数、8 位小数，没有正负号（所以总是 ≥ 0）。
QUANTITY_PATTERN: Final = r"^[0-9]{1,12}(?:\.[0-9]{1,8})?$"
# RFC 3339，必须带时区，最多 6 位小数秒。日历上是否存在由服务层换算时判。
OCCURRED_AT_PATTERN: Final = (
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?"
    r"(?:Z|[+-][0-9]{2}:[0-9]{2})$"
)
MAX_TOKENS: Final = 10**12
# 诊断用的 `tenant_id` / `project_id` 是 public_id（36 个字符）；格式不卡，只卡长度，
# 好让比对不符的请求照样走 403 与审计，又不让一个 16 KiB 的串进审计行。
MAX_SCOPE_ID_LENGTH: Final = 64


class UsageEventPayload(BaseModel):
    """One usage event as the integrated backend reports it (spec §11, design §2 字段规则)."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: str
    event_id: str = Field(pattern=EVENT_ID_PATTERN)
    # AI 调用的 id。
    request_id: str = Field(pattern=REQUEST_ID_PATTERN)
    conversation_id: str | None = Field(default=None, pattern=REQUEST_ID_PATTERN)
    # 只作诊断：给出时必须等于凭据所属租户 / 项目的 public_id，否则 403。
    tenant_id: str | None = Field(default=None, max_length=MAX_SCOPE_ID_LENGTH)
    project_id: str | None = Field(default=None, max_length=MAX_SCOPE_ID_LENGTH)
    provider: str = Field(pattern=PROVIDER_PATTERN)
    model: str = Field(pattern=MODEL_PATTERN)
    # 必须是 `usage_meter_types.code` 之一：查表（第 ⑥ 步），不在这里写死清单。
    usage_type: str
    # 按形态（第 ⑦ 步）：`LLM_TOKEN_FIELDS` 四个都必填，`QUANTITY` 一个都不许出现。
    input_tokens: int | None = Field(default=None, ge=0, le=MAX_TOKENS)
    output_tokens: int | None = Field(default=None, ge=0, le=MAX_TOKENS)
    cache_creation_input_tokens: int | None = Field(default=None, ge=0, le=MAX_TOKENS)
    cache_read_input_tokens: int | None = Field(default=None, ge=0, le=MAX_TOKENS)
    # 按形态：`QUANTITY` 必填，`LLM_TOKEN_FIELDS` 不许出现。
    quantity: str | None = Field(default=None, pattern=QUANTITY_PATTERN)
    unit: str | None = None
    occurred_at: str = Field(pattern=OCCURRED_AT_PATTERN)


class UsageEventReceipt(BaseModel):
    """`data` of a 202 / 200. Only the event id and the processing status (design §6)."""

    event_id: str
    # `accepted` / `already_received` / `already_processed`
    status: str
    processing_status: str


__all__ = [
    "EVENT_ID_PATTERN",
    "MAX_SCOPE_ID_LENGTH",
    "MAX_TOKENS",
    "MODEL_PATTERN",
    "OCCURRED_AT_PATTERN",
    "PROVIDER_PATTERN",
    "QUANTITY_PATTERN",
    "REQUEST_ID_PATTERN",
    "SCHEMA_VERSION",
    "ULID_PATTERN",
    "UUID7_PATTERN",
    "UsageEventPayload",
    "UsageEventReceipt",
]
