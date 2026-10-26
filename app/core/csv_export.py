"""Customer CSV exports: format, row cap, the process-wide export lease (design gate #435 v10 §2).

⚠️ **格式**（§2「CSV 格式」）：UTF-8 带 BOM、RFC 4180
（`csv.QUOTE_MINIMAL`、行尾 `\\r\\n`）、空值写成空字段。`write_csv` 不做类型转换：
单元格只能是 `str` 或 `None`，时间、整数、金额变成字符串与列的改名都由调用方
按白名单视图的 JSON 值完成，这里遇到别的类型抛 `TypeError`。
BOM 只在编码成字节时加（`utf-8-sig`），不写进 `StringIO`：U+FEFF 不是 Latin-1
字符，写进去会让整段文本按双字节存储，内存上界随之翻倍。

⚠️ **公式注入**：只对调用方声明的文本列，值以 `=`、`+`、`-`、`@`、制表符或回车
开头时前加一个单引号。数值列与时间列不加：负金额 `-0.04120000` 必须保持是数。

⚠️ **导出租约**（§2「上限与资源边界」，INV-1）：进程里同一时刻至多一个导出在生成
或在发送。获取不阻塞，被占用就返回 `None`（处理函数回 503 `EXPORT_BUSY`）；
**没有按时间的过期与接管**，持有超过 120 秒只在获取失败时记一条
`customer_export_lease_held_long`（每个持有者一次）。
唯一的释放点是 `ExportLeaseMiddleware`：`app/main.py` 里最后一个
`add_middleware`，是最外层，在整个请求结束时（正常、出错、断开、取消都一样）
释放槽里的 token。处理函数拿到 token 后只 `hold`，之后不在任何地方释放。

⚠️ **分段发送**：`ExportCsvResponse` 把拼好的字节按 64 KiB 用 `memoryview` 切片
逐条发送。uvicorn 每发一段之前按流控等排空，最后一段返回时传输缓冲里
至多约 128 KiB，租约这之后才释放。

写 CSV 与构造响应的函数是纯函数，不碰数据库、不打日志。
"""

from __future__ import annotations

import csv
import io
import logging
import re
import threading
import time
from collections.abc import Collection, Iterable, Sequence
from typing import Final

from fastapi import status
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.errors import AppError
from app.core.logging import current_request_id

logger = logging.getLogger(__name__)

# 两个导出接口共用。查询取 `EXPORT_MAX_ROWS + 1` 行，多出来的那一行只用来判「超了」。
EXPORT_MAX_ROWS: Final = 20_000
# 进程级：改成多进程部署时总并发 = 进程数 × 1（docs/deployment.md §3.3）。
EXPORT_MAX_CONCURRENT: Final = 1
# 只用于告警，不用于接管。
EXPORT_LEASE_WARN_SECONDS: Final = 120
EXPORT_SEND_CHUNK_BYTES: Final = 65_536
# `ExportLeaseMiddleware` 放进 `scope` 的租约槽。
EXPORT_LEASE_SCOPE_KEY: Final = "acuven.export_lease"

EXPORT_MEDIA_TYPE: Final = "text/csv; charset=utf-8"
EXPORT_RETRY_AFTER_SECONDS: Final = 5

_FORMULA_PREFIXES: Final = ("=", "+", "-", "@", "\t", "\r")
_FILENAME: Final = re.compile(r"[A-Za-z0-9._-]+")

# 测试 monkeypatch 这一个名字推时钟，不动全局的 `time.monotonic`。
_clock = time.monotonic


# --- 错误 -------------------------------------------------------------------------------


class ExportTooLarge(AppError):
    """More rows than `EXPORT_MAX_ROWS`. Fixed text: no parameter values, no actual row count."""

    def __init__(self) -> None:
        super().__init__(
            "The export has more than 20,000 rows. Narrow the date range or add filters.",
            code="EXPORT_TOO_LARGE",
            http_status=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )


class ExportBusy(AppError):
    """Another export holds the process-wide lease; retry in a few seconds."""

    def __init__(self) -> None:
        super().__init__(
            "Another export is in progress. Retry in a few seconds.",
            code="EXPORT_BUSY",
            http_status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
        self.headers = {"Retry-After": str(EXPORT_RETRY_AFTER_SECONDS)}


class CustomerExportFailed(AppError):
    """An unexpected failure while exporting, sent inside the lease by the inner handler.

    信封与通用 500（`handle_unexpected_error`）逐字相同：同一个码、同一句固定文案。
    """

    def __init__(self) -> None:
        super().__init__(
            "An internal error occurred.",
            code="INTERNAL_ERROR",
            http_status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


# --- 导出租约 ---------------------------------------------------------------------------


class ExportLeaseToken:
    """One holder of the export lease. Compared by identity only."""

    __slots__ = ()


class _ExportLease:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._token: ExportLeaseToken | None = None
        self._acquired_at = 0.0
        self._warned = False

    def try_acquire(self) -> ExportLeaseToken | None:
        warn = False
        with self._lock:
            if self._token is None:
                token = ExportLeaseToken()
                self._token = token
                self._acquired_at = _clock()
                self._warned = False
                return token
            if not self._warned and _clock() - self._acquired_at > EXPORT_LEASE_WARN_SECONDS:
                self._warned = True
                warn = True
        if warn:
            logger.warning(
                "customer_export_lease_held_long", extra={"request_id": current_request_id()}
            )
        return None

    def release(self, token: ExportLeaseToken) -> None:
        with self._lock:
            if self._token is token:
                self._token = None

    def held(self) -> bool:
        with self._lock:
            return self._token is not None


_LEASE: Final = _ExportLease()


def try_acquire_export_lease() -> ExportLeaseToken | None:
    """A new token when no export is running, else `None`. Never blocks, never takes over."""
    return _LEASE.try_acquire()


def release_export_lease(token: ExportLeaseToken) -> None:
    """Free the lease if `token` still holds it; any other token is a no-op."""
    _LEASE.release(token)


def export_lease_held() -> bool:
    return _LEASE.held()


class ExportLeaseSlot:
    """A request's slot in `scope`: the handler holds its token, the middleware releases it."""

    __slots__ = ("_token",)

    def __init__(self) -> None:
        self._token: ExportLeaseToken | None = None

    def hold(self, token: ExportLeaseToken) -> None:
        self._token = token

    def release(self) -> None:
        token, self._token = self._token, None
        if token is not None:
            release_export_lease(token)


class ExportLeaseMiddleware:
    """Pure ASGI, outermost user middleware: releases the request's export lease when it ends.

    ⚠️ 必须是纯 ASGI 且在最外层：`RequestContextMiddleware` 是
    `BaseHTTPMiddleware`，内层 `send` 返回只说明消息交给了它。只有这里的
    `await self.app(...)` 结束，响应体才真正交给了服务器。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        slot = ExportLeaseSlot()
        scope[EXPORT_LEASE_SCOPE_KEY] = slot
        try:
            await self.app(scope, receive, send)
        finally:
            slot.release()


# --- CSV --------------------------------------------------------------------------------


def write_csv(
    columns: Sequence[str],
    rows: Iterable[Sequence[str | None]],
    text_columns: Collection[str],
) -> bytes:
    """BOM, header and rows in `columns` order; formula prefixes guarded on `text_columns` only."""
    guarded = [name in text_columns for name in columns]
    # 默认的 `newline="\n"` 写入时不做换行转换，`\r\n` 原样留下。
    buffer = io.StringIO()
    writer = csv.writer(buffer, quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    writer.writerow(columns)
    for row in rows:
        cells: list[str] = []
        for value, guard in zip(row, guarded, strict=True):
            if value is None:
                cells.append("")
            elif type(value) is not str:
                raise TypeError("CSV cells must be str or None")
            elif guard and value.startswith(_FORMULA_PREFIXES):
                cells.append("'" + value)
            else:
                cells.append(value)
        writer.writerow(cells)
    text = buffer.getvalue()
    buffer.close()
    # BOM 在编码时加，不进 StringIO（见模块说明）。
    return text.encode("utf-8-sig")


class ExportCsvResponse(Response):
    """An attachment sent as `EXPORT_SEND_CHUNK_BYTES` slices of one `memoryview`, never copied."""

    # Starlette 给 `text/*` 补上 `; charset=utf-8`，得到 `EXPORT_MEDIA_TYPE`。
    media_type = "text/csv"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": self.status_code,
                "headers": self.raw_headers,
            }
        )
        view = memoryview(self.body)
        total = len(view)
        offset = 0
        while True:
            end = offset + EXPORT_SEND_CHUNK_BYTES
            last = end >= total
            await send(
                {"type": "http.response.body", "body": view[offset:end], "more_body": not last}
            )
            if last:
                return
            offset = end


def csv_response(filename: str, body: bytes) -> ExportCsvResponse:
    """200 `text/csv` attachment. `filename` is built from validated dates by the caller."""
    if _FILENAME.fullmatch(filename) is None:
        raise ValueError("export filename must be ASCII letters, digits, '-', '_' or '.'")
    return ExportCsvResponse(
        content=body,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )


__all__ = [
    "EXPORT_LEASE_SCOPE_KEY",
    "EXPORT_LEASE_WARN_SECONDS",
    "EXPORT_MAX_CONCURRENT",
    "EXPORT_MAX_ROWS",
    "EXPORT_MEDIA_TYPE",
    "EXPORT_RETRY_AFTER_SECONDS",
    "EXPORT_SEND_CHUNK_BYTES",
    "CustomerExportFailed",
    "ExportBusy",
    "ExportCsvResponse",
    "ExportLeaseMiddleware",
    "ExportLeaseSlot",
    "ExportLeaseToken",
    "ExportTooLarge",
    "csv_response",
    "export_lease_held",
    "release_export_lease",
    "try_acquire_export_lease",
    "write_csv",
]
