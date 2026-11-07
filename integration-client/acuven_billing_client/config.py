"""Client configuration (design gate #571 v24 §2「模块与公开接口」, §6).

⚠️ 密钥（`secret` 与 webhook 密钥）不进 `repr` / `str`，也不进任何异常消息：
校验失败时消息只点名字段或环境变量，不带值（§6「密钥存储与读取」）。

直接构造 `BillingClientConfig` 与 `from_env` 走同一套校验；
`from_env` 另按环境变量名报错。
"""

from __future__ import annotations

import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final
from urllib.parse import urlsplit

DEFAULT_ENV_PREFIX: Final = "ACUVEN_BILLING_"
# 已送达行至少保留 1 天（§2 可调参数）。
MIN_DELIVERED_RETENTION_SECONDS: Final = 86_400
# 单条事件上限 16384 字节加批量包装 `{"events":[…]}`：比它小，合法的大事件永远领不出去。
MIN_BATCH_MAX_BYTES: Final = 17_000
# 只认 ASCII 数字，不收空白、正负号、下划线（`int()` 都收）。
_VERSION_PATTERN: Final = re.compile(r"[1-9][0-9]{0,18}")

_COUNT_FIELDS: Final = (
    "batch_max_events",
    "batch_max_bytes",
    "held_max_events",
    "pending_incidents_max",
)
_SECONDS_FIELDS: Final = (
    "delivery_interval_seconds",
    "lease_seconds",
    "http_timeout_seconds",
    "reconcile_interval_seconds",
    "reconcile_retry_seconds",
    "delivered_retention_seconds",
)
# 字段名 → 环境变量名的后缀（除 WEBHOOK_SECRETS 外都必填）。
_REQUIRED_ENV: Final = (
    ("base_url", "BASE_URL"),
    ("api_key", "API_KEY"),
    ("key_version", "KEY_VERSION"),
    ("secret", "API_SECRET"),
    ("tenant_id", "TENANT_ID"),
    ("project_id", "PROJECT_ID"),
    ("outbox_path", "OUTBOX_PATH"),
)


def _check_required_string(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _check_base_url(value: object, name: str) -> None:
    _check_required_string(value, name)
    try:
        parts = urlsplit(value)  # type: ignore[arg-type]
    except ValueError:
        raise ValueError(f"{name} must be an http or https URL") from None
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        raise ValueError(f"{name} must be an http or https URL")


def _check_key_version(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _has_whitespace(value: str) -> bool:
    return any(character.isspace() for character in value)


def _check_webhook_secrets(value: object, name: str) -> None:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping of key version to secret")
    for version, secret in value.items():
        if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
            raise ValueError(f"{name} keys must be positive integers")
        if not isinstance(secret, str) or not secret or _has_whitespace(secret):
            raise ValueError(f"{name} values must be non-empty strings without whitespace")


def _parse_webhook_secrets(raw: str | None, name: str) -> dict[int, str]:
    """`<版本>:<密钥>` 用逗号分隔。出错时消息只给变量名与第几项，不给任何值。"""
    if raw is None or not raw.strip():
        return {}
    parsed: dict[int, str] = {}
    for position, item in enumerate(raw.split(","), start=1):
        version, separator, secret = item.strip().partition(":")
        if (
            not separator
            or _VERSION_PATTERN.fullmatch(version) is None
            or not secret
            or _has_whitespace(secret)
            or int(version) in parsed
        ):
            raise ValueError(f"{name} is malformed (item {position})")
        parsed[int(version)] = secret
    return parsed


@dataclass(frozen=True, repr=False)
class BillingClientConfig:
    """Immutable settings of one client: one tenant × project, one outbox file."""

    base_url: str
    api_key: str
    key_version: int
    secret: str
    # 对外 id（public_id），只用于校验状态应答与 webhook；事件载荷里不写（§2「事件」）。
    tenant_id: str
    project_id: str
    outbox_path: str
    # 空字典表示不接收状态 webhook。
    webhook_secrets: dict[int, str] = field(default_factory=dict)
    delivery_interval_seconds: float = 10
    batch_max_events: int = 100
    batch_max_bytes: int = 1_000_000
    lease_seconds: float = 60
    http_timeout_seconds: float = 10
    reconcile_interval_seconds: float = 240
    reconcile_retry_seconds: float = 30
    delivered_retention_seconds: float = 7 * 86_400
    held_max_events: int = 1000
    pending_incidents_max: int = 10_000

    def __post_init__(self) -> None:
        if isinstance(self.outbox_path, os.PathLike):
            object.__setattr__(self, "outbox_path", os.fspath(self.outbox_path))
        _check_base_url(self.base_url, "base_url")
        for name in ("api_key", "secret", "tenant_id", "project_id", "outbox_path"):
            _check_required_string(getattr(self, name), name)
        _check_key_version(self.key_version, "key_version")
        _check_webhook_secrets(self.webhook_secrets, "webhook_secrets")
        # 拷一份：调用方之后改自己的字典不影响配置。
        object.__setattr__(self, "webhook_secrets", dict(self.webhook_secrets))
        for name in _COUNT_FIELDS:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        for name in _SECONDS_FIELDS:
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} must be a positive finite number")
        if self.batch_max_bytes < MIN_BATCH_MAX_BYTES:
            raise ValueError(f"batch_max_bytes must be at least {MIN_BATCH_MAX_BYTES}")
        if self.delivered_retention_seconds < MIN_DELIVERED_RETENTION_SECONDS:
            raise ValueError(
                f"delivered_retention_seconds must be at least {MIN_DELIVERED_RETENTION_SECONDS}"
            )

    def __repr__(self) -> str:
        # 不含 `secret` 与 webhook 密钥；webhook 只给出配了哪几个版本。
        return (
            f"BillingClientConfig(base_url={self.base_url!r}, api_key={self.api_key!r}, "
            f"key_version={self.key_version!r}, tenant_id={self.tenant_id!r}, "
            f"project_id={self.project_id!r}, outbox_path={self.outbox_path!r}, "
            f"webhook_key_versions={sorted(self.webhook_secrets)!r})"
        )

    __str__ = __repr__

    @classmethod
    def from_env(cls, prefix: str = DEFAULT_ENV_PREFIX) -> BillingClientConfig:
        """Read the eight `<prefix>…` variables; errors name the variable, never its value."""
        values: dict[str, str] = {}
        missing: list[str] = []
        for field_name, suffix in _REQUIRED_ENV:
            value = os.environ.get(prefix + suffix, "").strip()
            if value:
                values[field_name] = value
            else:
                missing.append(prefix + suffix)
        if missing:
            raise ValueError("missing required environment variables: " + ", ".join(missing))

        _check_base_url(values["base_url"], prefix + "BASE_URL")
        if _VERSION_PATTERN.fullmatch(values["key_version"]) is None:
            raise ValueError(f"{prefix}KEY_VERSION must be a positive integer")
        webhook_secrets = _parse_webhook_secrets(
            os.environ.get(prefix + "WEBHOOK_SECRETS"), prefix + "WEBHOOK_SECRETS"
        )
        return cls(
            base_url=values["base_url"],
            api_key=values["api_key"],
            key_version=int(values["key_version"]),
            secret=values["secret"],
            tenant_id=values["tenant_id"],
            project_id=values["project_id"],
            outbox_path=values["outbox_path"],
            webhook_secrets=webhook_secrets,
        )


__all__ = ["BillingClientConfig"]
