"""`POST /api/v1/integration/usage-events` (design gate #176 v8 §2「接口」).

只接受集成签名认证：不读 `Authorization`，管理员会话在这里等于没带签名，一律 401。

⚠️ **验签用原始字节**：签名覆盖原始请求体的 SHA-256，所以这里不用 FastAPI 的模型参数
（它会先解析 JSON），而是自己按上限读原始字节，验签之后再由服务层解析。

⚠️ 处理函数是 `async` 只为读请求体；验签、Redis 与数据库都是同步调用，整段丢进线程池，
不阻塞事件循环。

成功（202 / 200）之后，`last_used_at` 由响应的后台任务在独立短事务里节流写入：
响应已经发出，它失败只记日志。
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session, sessionmaker
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from app.api.auth import request_context
from app.core.logging import current_request_id
from app.schemas.envelope import ApiResponse, success
from app.schemas.usage_ingest import UsageEventReceipt
from app.services import usage_ingest
from app.services.auth import RequestContext, utc_now
from app.services.usage_ingest import (
    MAX_BODY_BYTES,
    IngestOutcome,
    PayloadTooLarge,
    ServiceUnavailable,
    SignedRequest,
)

router = APIRouter(prefix="/api/v1/integration", tags=["integration"])


async def _read_body(request: Request, limit: int) -> bytes:
    """The raw body, refused with 413 as soon as it is known to exceed `limit` bytes.

    先看 `Content-Length`，再边读边数：超限就停，不把整个超大请求体读进内存。
    """
    declared = request.headers.get("content-length", "")
    if declared.isascii() and declared.isdigit() and int(declared) > limit:
        raise PayloadTooLarge
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise PayloadTooLarge
        chunks.append(chunk)
    return b"".join(chunks)


def _path_and_query(request: Request) -> str:
    """The path as sent (percent-encoding untouched) and the raw query string."""
    raw_path = request.scope.get("raw_path")
    path = raw_path.decode("latin-1") if raw_path else request.url.path
    query = request.scope.get("query_string", b"").decode("latin-1")
    return f"{path}?{query}" if query else path


def _receive(
    request: Request,
    factory: sessionmaker[Session],
    signed: SignedRequest,
    context: RequestContext,
    now: dt.datetime,
) -> tuple[IngestOutcome, int]:
    state = request.app.state
    credential = usage_ingest.authenticate(
        factory,
        state.settings,
        secret_cache=state.secret_cache,
        nonce_store=state.nonce_store,
        request=signed,
        now=now,
    )
    payload = usage_ingest.parse_json_object(signed.body)
    outcome = usage_ingest.ingest_one(factory, credential, payload, context=context, now=now)
    return outcome, credential.id


@router.post(
    "/usage-events",
    status_code=202,
    response_model=ApiResponse[UsageEventReceipt],
)
async def receive_usage_event(request: Request) -> JSONResponse:
    """One usage event. 202 = the `RECEIVED` row is committed (REQ-INGEST-001); 200 = duplicate.

    错误一律是 §107 信封加顶层 `retryable`（app/core/errors.py）。
    """
    body = await _read_body(request, MAX_BODY_BYTES)
    factory: sessionmaker[Session] | None = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise ServiceUnavailable
    signed = SignedRequest(
        method=request.method,
        path_and_query=_path_and_query(request),
        body=body,
        headers=request.headers,
    )
    now = utc_now()
    outcome, credential_id = await run_in_threadpool(
        _receive, request, factory, signed, request_context(request), now
    )
    envelope = success(outcome.receipt(), request_id=current_request_id())
    return JSONResponse(
        status_code=outcome.http_status,
        content=envelope.model_dump(),
        background=BackgroundTask(usage_ingest.record_use, factory, credential_id, now),
    )
