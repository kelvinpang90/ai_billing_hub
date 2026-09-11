"""Structured logging with correlation IDs and redaction (spec §94).

三件事：

1. **JSON 行日志**，不是人读的字符串拼接——日志要能被查询。
2. **关联 ID 走 ContextVar**，不靠层层传参。`request_id` 由中间件写入，
   `tenant_id` / `project_id` / `event_id` 由后续 Phase 在拿到它们的地方
   `bind_log_context()` 补上。
3. **脱敏是过滤器，不是调用点的自觉。**

脱敏覆盖**三条**进入日志的路径，缺一条就等于没做：

- `extra=` 传进来的结构化字段 —— 按字段名递归脱敏
- 日志 message 本身 —— 按文本模式脱敏（`key=value`、带凭据的 URL）
- **异常栈** —— 同样按文本模式脱敏。这条最容易漏：`logger.exception()` 会把
  异常消息原样写进日志，而异常消息最常见的写法就是把触发它的那个值带上
  （连接串、密钥、请求体片段）。栈帧本身（文件、行号、函数、源码行）来自我们
  自己的代码，不含运行时数据，照记。

⚠️ §94 的「绝不记录」清单（密码、TOTP 密钥、API 明文密钥、支付密钥、客户的
AI prompt / response）**首要防线是根本不把它们交给 logger**。这里的脱敏是
**兜底，不是防线**：按字段名兜不住换了名字的字段，按文本模式兜不住没有
`key=value` 形状的自由文本。两条硬规矩：

1. **不要把请求体、响应体、原始 payload 写进日志。**
2. **不要把密钥、token、prompt 拼进异常消息** —— 异常消息会进日志。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
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

# 自由文本（日志 message、异常栈）里的脱敏模式。结构化字段那条路径按字段名走，
# 这条只能按形状认。
#
# 1) 带凭据的 URL：postgres://user:hunter2@host/db —— 连接串出现在异常消息里
#    是最常见的一种泄漏。
_URL_CREDENTIALS = re.compile(r"(?i)([a-z][a-z0-9+.\-]*://[^\s:/@]+:)([^\s@/]+)(@)")
# 2) key=value / key: value / "key": "value" —— f-string 调试消息与 dict repr
#    落进异常消息时的形状。
#    值那一组里 `Bearer xxx` 这个分支不能省：`Authorization: Bearer abc.def`
#    如果按「取到空格为止」，redact 掉的是 `Bearer`，token 原样留在后面。
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)((?:" + "|".join(_SENSITIVE_KEY_PARTS) + r")[\w.\-]*[\"']?\s*[=:]\s*)"
    r"(\"[^\"]*\"|'[^']*'|(?:bearer|basic|token|digest)\s+\S+|[^\s,;)\]}]+)"
)

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


def scrub_text(text: str) -> str:
    """Redact secret-shaped fragments in free text (log messages, tracebacks).

    只能按形状认，认不出没有 `key=value` 形状的自由文本 —— 见模块顶部那两条
    硬规矩。它的作用是把最常见的几种泄漏形状挡掉，不是许可把敏感值往消息里放。

    用 lambda 而不是 `\\1` 模板串：反向引用模板经过任何一层转义就会**静默失效**，
    变成「脱敏跑了但什么都没换掉」。lambda 没有这个失效模式。
    """
    text = _URL_CREDENTIALS.sub(lambda m: f"{m[1]}{REDACTED}{m[3]}", text)
    return _SENSITIVE_ASSIGNMENT.sub(lambda m: f"{m[1]}{REDACTED}", text)


class JsonFormatter(logging.Formatter):
    """Render one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": dt.datetime.fromtimestamp(record.created, dt.UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": scrub_text(record.getMessage()),
        }
        payload.update(redact(get_log_context()))
        extras = {
            key: value for key, value in vars(record).items() if key not in _RESERVED_RECORD_ATTRS
        }
        payload.update(redact(extras))
        if record.exc_info:
            # 栈只进日志，绝不进 HTTP 响应（§107）—— 但**进日志也要过脱敏**。
            # 异常消息是自由文本里最容易夹带密钥的一处：抛错的人往往把触发它的
            # 那个值一起写进消息。漏掉这条，等于结构化字段守住了、栈这条路敞着。
            payload["exception"] = scrub_text(self.formatException(record.exc_info))
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
