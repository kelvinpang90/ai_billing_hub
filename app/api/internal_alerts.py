"""`GET /internal/alerts`: the alert lines `deploy/monitor.sh` reads (AIH-TASK-042).

设计闸门 #183 v3 §2「内部告警接口」、§5 令牌一行、§6。
契约见 [docs/api.md](../../docs/api.md)「内部告警接口」。

⚠️ **机器接口，不在 `/api/` 下、不套 §107 信封**：monitor.sh 是 bash，
宿主机未必有 `jq`。200、401、404 都是 `text/plain`，这里直接返回纯文本响应，
**不抛 `AppError`**（全局错误处理会把它套成信封）。

两层访问控制，这里是第二层。第一层是 nginx 的网段白名单，归 AIH-TASK-043；
在那之前这个路径经 nginx 落到前端、不可达。第二层是
`Authorization: Bearer <令牌>`，令牌从 `BILLING_MONITOR_TOKEN_FILE` 所指的文件读。
令牌文件的每种状态（设计 §2 的表）：

- 未配置（空串）→ 404，不记日志；
- 读不出（不存在、是目录、无权限）→ 404，每次请求一条 ERROR；
- 去首尾空白后少于 32 个字符 → 404，同上；
- 可用 → 缺头、不是 Bearer、令牌不对都是 401，不说明原因；对 → 200。

ERROR 日志不含路径与内容。⚠️ **每次请求重新读文件**（轮换令牌不需要重启）；
**令牌判定先于任何数据库查询**。
"""

from __future__ import annotations

import hmac
import logging
from pathlib import Path
from typing import Final

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from app.core.config import Settings
from app.services import alerts

logger = logging.getLogger(__name__)
router = APIRouter(tags=["internal"])

ALERTS_PATH: Final = "/internal/alerts"
# `openssl rand -hex 32` 生成 64 个字符；少于 32 个字符的当作没配好。
MIN_TOKEN_LENGTH: Final = 32
_BEARER: Final = "bearer"


def _text(body: str, status_code: int, headers: dict[str, str] | None = None) -> PlainTextResponse:
    return PlainTextResponse(body, status_code=status_code, headers=headers)


def _not_found() -> PlainTextResponse:
    return _text("not found\n", 404)


def _unauthorized() -> PlainTextResponse:
    return _text("unauthorized\n", 401, {"WWW-Authenticate": "Bearer"})


def read_monitor_token(settings: Settings) -> str | None:
    """The configured token, or `None` when the endpoint is off (unconfigured or unusable).

    ⚠️ 日志里不写路径、不写内容，也不带 `exc_info`：
    `FileNotFoundError` 的消息里就有路径。
    """
    path = settings.monitor_token_file.strip()
    if not path:
        return None
    try:
        token = Path(path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as error:
        logger.error(
            "The monitor token file cannot be read", extra={"error_type": type(error).__name__}
        )
        return None
    if len(token) < MIN_TOKEN_LENGTH:
        logger.error("The monitor token file holds fewer than 32 characters")
        return None
    return token


def _presented_token(request: Request) -> str | None:
    scheme, _, credentials = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != _BEARER:
        return None
    return credentials.strip() or None


def _same_token(presented: str, expected: str) -> bool:
    # 常量时间比较。按字节比：非 ASCII 的令牌也不会让 compare_digest 抛错。
    return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


@router.get(ALERTS_PATH, response_class=PlainTextResponse, include_in_schema=False)
def internal_alerts(request: Request) -> PlainTextResponse:
    """One line per dimension: `<dimension> <OK|P1|P2> <summary>`."""
    settings: Settings = request.app.state.settings
    expected = read_monitor_token(settings)
    if expected is None:
        return _not_found()
    presented = _presented_token(request)
    if presented is None or not _same_token(presented, expected):
        return _unauthorized()

    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        return _text("database not configured\n", 503)
    try:
        body = alerts.alert_lines(factory, settings.fx_currency_codes)
    except Exception:
        # 查不出来就是「接口不通」：monitor.sh 不推这些维度，
        # Healthchecks 宽限期后报「未上报」。
        logger.exception("Evaluating the alert dimensions failed")
        return _text("unavailable\n", 503)
    return _text(body, 200)
