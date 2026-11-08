"""HTTP transport (design gate #571 v24 §2「模块与公开接口」transport 一行, §4「并发」).

`Transport` 只发一次请求、不重试（重试只在投递层）；测试注入假实现。默认的
`UrllibTransport` 用 `urllib.request`：

- **不跟随重定向**：3xx 一律抛 `TransportError`，投递层按网络错误处理；
- 响应体最多读 64 KiB，多出的部分丢弃（截断后通常不是合法 JSON，按「没有信封」判）；
- 超时取调用方传入的值（投递层传配置的 `http_timeout_seconds`）；
- 不走代理（与 `app/ai_api/delivery.py` 相同），只接受 http / https 地址；
- 非 2xx 的响应照常返回 `Response`，由投递层按信封判定。

⚠️ `TransportError` 的消息只带异常类型或状态码，不带请求头（含签名与 API key）与
请求体（事件载荷），也不链接原异常（§6「日志」）。
"""

from __future__ import annotations

import http.client
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Protocol
from urllib.parse import urlsplit

MAX_RESPONSE_BYTES: Final = 64 * 1024


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    # 最多 MAX_RESPONSE_BYTES 字节。
    body: bytes


class TransportError(Exception):
    """Network failure, timeout or redirect; the request may or may not have reached the server."""


class Transport(Protocol):
    def send(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes, timeout: float
    ) -> Response: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # 返回 None 后 urllib 把 3xx 当作 HTTPError 抛出，下面统一转成 TransportError。
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UrllibTransport:
    """The default transport: one `urllib.request` call per `send`, no redirects, no proxies."""

    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def send(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes, timeout: float
    ) -> Response:
        if urlsplit(url).scheme.lower() not in ("http", "https"):
            raise TransportError("only http and https URLs are supported")
        request = urllib.request.Request(
            url, data=body or None, headers=dict(headers), method=method.upper()
        )
        try:
            try:
                response = self._opener.open(request, timeout=timeout)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                status = response.status if hasattr(response, "status") else response.code
                if 300 <= status < 400:
                    raise TransportError(f"redirect not followed (HTTP {status})")
                data = response.read(MAX_RESPONSE_BYTES)
        except TransportError:
            raise
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
            raise TransportError(f"request failed: {type(exc).__name__}") from None
        return Response(status=int(status), body=bytes(data[:MAX_RESPONSE_BYTES]))


__all__ = ["MAX_RESPONSE_BYTES", "Response", "Transport", "TransportError", "UrllibTransport"]
