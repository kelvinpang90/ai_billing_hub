"""Structured logging with correlation IDs and redaction (spec §94).

三件事：

1. **JSON 行日志**，不是人读的字符串拼接——日志要能被查询。
2. **关联 ID 走 ContextVar**，不靠层层传参。`request_id` 由中间件写入，
   `tenant_id` / `project_id` / `event_id` 由后续 Phase 在拿到它们的地方
   `bind_log_context()` 补上。
3. **脱敏是过滤器，不是调用点的自觉。**

⚠️ §94 的「绝不记录」清单（密码、TOTP 密钥、API 明文密钥、支付密钥、客户的
AI prompt / response）**首要防线是根本不把它们交给 logger**。下面的过滤器按
字段名兜底，兜不住换了名字的字段，更兜不住塞进 message 里的自由文本。
**不要把请求体、响应体、异常里的原始 payload 直接写进日志。**
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sys
from contextvars import ContextVar
from typing import Any

# 关联字段。整段替换而不是原地改字典——ContextVar 的值在并发任务之间是共享
# 引用，原地 mutate 会串到别的请求上。默认值必须是 None 而不是 {}：可变默认值
# 是同一个对象，一旦有人原地改它，所有还没 set 过的上下文都会跟着变。
_log_context: ContextVar[dict[str, Any] | None] = ContextVar("log_context", default=None)

# 结构化字段里按名字兜底脱敏的部分匹配串。小写比较。
_SENSITIVE_KEY_PARTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "authorization",
    "api_key",
    "apikey",
    "totp",
    "signature",
    "prompt",
    "completion",
)

REDACTED = "***REDACTED***"

# LogRecord 自带的属性。要挑出调用方通过 extra= 传进来的字段，只能靠排除法。
_RESERVED_RECORD_ATTRS = frozenset(
    set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime", "taskName"}
)


def bind_log_context(**fields: Any) -> None:
    """Add correlation fields to the current context (spec §94)."""
    _log_context.set({**get_log_context(), **fields})


def get_log_context() -> dict[str, Any]:
    return dict(_log_context.get() or {})


def clear_log_context() -> None:
    _log_context.set({})


def current_request_id() -> str | None:
    """The id the middleware bound for this request, if there is one.

    响应信封要带它（§107），日志也要带它（§94）——两边取的必须是同一个值，
    所以只有这一个读取入口。
    """
    value = get_log_context().get("request_id")
    return str(value) if value is not None else None


def is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def redact(value: Any, _depth: int = 0) -> Any:
    """Recursively replace values held under sensitive-looking keys.

    深度设上限是为了挡住自引用结构——日志格式化里出现无限递归，会把一次误用
    变成进程挂死。
    """
    if _depth >= 6:
        return value
    if isinstance(value, dict):
        return {
            key: REDACTED if is_sensitive_key(str(key)) else redact(item, _depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item, _depth + 1) for item in value]
    return value


class JsonFormatter(logging.Formatter):
    """Render one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": dt.datetime.fromtimestamp(record.created, dt.UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(redact(get_log_context()))
        extras = {
            key: value for key, value in vars(record).items() if key not in _RESERVED_RECORD_ATTRS
        }
        payload.update(redact(extras))
        if record.exc_info:
            # 栈只进日志，绝不进 HTTP 响应（§107）。
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str) -> None:
    """Install the JSON handler on the root logger. Idempotent."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())

    # uvicorn 自己在这几个 logger 上装 handler 且 propagate=False，不接管的话
    # 生产日志会一半 JSON 一半纯文本 —— 那等于没有结构化日志。
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
