"""Usage-event validation and serialization (design gate #571 v24 §2「事件」).

纯函数：不读时钟、不写库。`now`（缺省的 `occurred_at`）与 `new_id`
（生成 `event_id`）由调用方传入。

校验规则逐条照服务端 `app/schemas/usage_ingest.py`：正则与范围照抄，
`schema_version` 固定 `"1.0"`。正则一律用显式字符类并整串匹配（`fullmatch`）：
`\\d` 还认全角数字，`$` 还放过末尾的换行。

载荷里**不写** `tenant_id` / `project_id`：归属由签名凭据决定，配置一旦写错，
已入库的字节就永远与凭据不符（§2「事件」）。没给的 `conversation_id`、`feature`
不出现（不是 `null`）；LLM 载荷没有 `quantity` / `unit`，QUANTITY 载荷没有
token 字段 —— 服务端对形态不符的字段即使为 `null` 也拒绝。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final

SCHEMA_VERSION: Final = "1.0"
MAX_EVENT_BYTES: Final = 16 * 1024
MAX_TOKENS: Final = 10**12
MAX_FEATURE_LENGTH: Final = 64
MAX_UNIT_LENGTH: Final = 64

_UUID7: Final = r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
_ULID: Final = r"[0-7][0-9A-HJKMNP-TV-Z]{25}"
_EVENT_ID: Final = re.compile(rf"(?:{_UUID7}|{_ULID})")
_REQUEST_ID: Final = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_PROVIDER: Final = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_MODEL: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}")
_FEATURE: Final = re.compile(r"[a-z][a-z0-9_]{0,31}(?:\.[a-z][a-z0-9_]{0,31}){1,3}")
_QUANTITY: Final = re.compile(r"[0-9]{1,12}(?:\.[0-9]{1,8})?")
# 字符串形式的 quantity：ASCII 数字、可选的小数点加 ASCII 数字；没有空白、正负号、下划线、指数。
_DECIMAL_STRING: Final = re.compile(r"[0-9]+(?:\.[0-9]+)?")
_TOKEN_FIELDS: Final = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def _check_pattern(value: object, pattern: re.Pattern[str], name: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"{name} is invalid")
    return value


def _check_aware(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value


def _format_occurred_at(value: datetime) -> str:
    """RFC 3339 in UTC: `Z` suffix, six fractional digits only when there are microseconds."""
    try:
        moment = value.astimezone(UTC).replace(tzinfo=None)
    except (OverflowError, ValueError):
        raise ValueError("occurred_at is out of range") from None
    return moment.isoformat() + "Z"


def _check_tokens(value: object, name: str) -> int:
    # bool 是 int 的子类，但不算整数（服务端 strict）。
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_TOKENS:
        raise ValueError(f"{name} must be an integer from 0 to {MAX_TOKENS}")
    return value


def normalize_quantity(value: object) -> str:
    """`Decimal` or decimal string → `^[0-9]{1,12}(\\.[0-9]{1,8})?$`, never through float.

    先去掉小数部分的尾零、再判位数：`0.000000010` → `0.00000001` 合法；`1E+3` → `1000`。
    """
    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, str):
        if _DECIMAL_STRING.fullmatch(value) is None:
            raise ValueError("quantity must be a decimal string or Decimal")
        number = Decimal(value)
    else:
        # int、float 与其他类型一律不收（INV-10）。
        raise ValueError("quantity must be a decimal string or Decimal")
    if not number.is_finite():
        raise ValueError("quantity must be finite")
    if number.is_zero():
        # 含负零。
        return "0"
    if number < 0:
        raise ValueError("quantity must not be negative")
    _, digits, exponent = number.as_tuple()
    # 先按「整数部分超过 12 位」挡掉，免得下面把 1E+100000 这类值展开成很长的串。
    if number.adjusted() >= 12:
        raise ValueError("quantity has more than 12 integer digits")
    if exponent < 0:
        text = "".join(map(str, digits))
        removable = min(len(text) - len(text.rstrip("0")), -exponent)
        digits = digits[: len(digits) - removable]
        exponent += removable
    if exponent < -8:
        raise ValueError("quantity has more than 8 decimal places")
    text = format(Decimal((0, digits, exponent)), "f")
    if _QUANTITY.fullmatch(text) is None:
        raise ValueError("quantity is out of range")
    return text


def _check_unit(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > MAX_UNIT_LENGTH
    ):
        raise ValueError(f"unit must be a non-empty trimmed string of at most {MAX_UNIT_LENGTH}")
    return value


def _common_fields(
    *,
    provider: object,
    model: object,
    usage_type: object,
    request_id: object,
    now: object,
    new_id: Callable[[], str],
    occurred_at: object,
    conversation_id: object,
    feature: object,
    event_id: object,
) -> dict[str, Any]:
    if event_id is not None and occurred_at is None:
        # 缺省的 occurred_at 取调用时刻：同一个 event_id 第二次调用的载荷必然不同。
        raise ValueError("occurred_at is required when event_id is given")
    current = _check_aware(now, "now")
    moment = current if occurred_at is None else _check_aware(occurred_at, "occurred_at")
    if not isinstance(usage_type, str) or not usage_type:
        # 是否在服务端的计量类型目录里，客户端不知道，不在本地判。
        raise ValueError("usage_type must be a non-empty string")
    fields: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "event_id": _check_pattern(
            new_id() if event_id is None else event_id, _EVENT_ID, "event_id"
        ),
        "request_id": _check_pattern(request_id, _REQUEST_ID, "request_id"),
        "provider": _check_pattern(provider, _PROVIDER, "provider"),
        "model": _check_pattern(model, _MODEL, "model"),
        "usage_type": usage_type,
        "occurred_at": _format_occurred_at(moment),
    }
    if conversation_id is not None:
        fields["conversation_id"] = _check_pattern(conversation_id, _REQUEST_ID, "conversation_id")
    if feature is not None:
        _check_pattern(feature, _FEATURE, "feature")
        if len(feature) > MAX_FEATURE_LENGTH:  # type: ignore[arg-type]
            raise ValueError("feature is invalid")
        fields["feature"] = feature
    return fields


def _serialize(fields: dict[str, Any]) -> tuple[str, bytes]:
    payload = json.dumps(fields, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode(
        "utf-8"
    )
    if len(payload) > MAX_EVENT_BYTES:
        raise ValueError(f"event payload exceeds {MAX_EVENT_BYTES} bytes")
    return fields["event_id"], payload


def build_llm_event(
    *,
    provider: str,
    model: str,
    usage_type: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation_input_tokens: int,
    cache_read_input_tokens: int,
    request_id: str,
    now: datetime,
    new_id: Callable[[], str],
    occurred_at: datetime | None = None,
    conversation_id: str | None = None,
    feature: str | None = None,
    event_id: str | None = None,
) -> tuple[str, bytes]:
    """Validate and serialize one LLM-token event. Returns `(event_id, payload bytes)`."""
    fields = _common_fields(
        provider=provider,
        model=model,
        usage_type=usage_type,
        request_id=request_id,
        now=now,
        new_id=new_id,
        occurred_at=occurred_at,
        conversation_id=conversation_id,
        feature=feature,
        event_id=event_id,
    )
    tokens = (
        input_tokens,
        output_tokens,
        cache_creation_input_tokens,
        cache_read_input_tokens,
    )
    for name, value in zip(_TOKEN_FIELDS, tokens, strict=True):
        fields[name] = _check_tokens(value, name)
    return _serialize(fields)


def build_quantity_event(
    *,
    provider: str,
    model: str,
    usage_type: str,
    quantity: Decimal | str,
    unit: str,
    request_id: str,
    now: datetime,
    new_id: Callable[[], str],
    occurred_at: datetime | None = None,
    conversation_id: str | None = None,
    feature: str | None = None,
    event_id: str | None = None,
) -> tuple[str, bytes]:
    """Validate and serialize one QUANTITY event. Returns `(event_id, payload bytes)`."""
    fields = _common_fields(
        provider=provider,
        model=model,
        usage_type=usage_type,
        request_id=request_id,
        now=now,
        new_id=new_id,
        occurred_at=occurred_at,
        conversation_id=conversation_id,
        feature=feature,
        event_id=event_id,
    )
    fields["quantity"] = normalize_quantity(quantity)
    fields["unit"] = _check_unit(unit)
    return _serialize(fields)


__all__ = ["build_llm_event", "build_quantity_event", "normalize_quantity"]
