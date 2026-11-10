"""Payment routes (design gate #580 v3 §2「支付 Webhook」「建单」, §6; AIH-TASK-177).

- `POST /api/v1/webhooks/payments/{gateway}`：Stripe 的回调；
- `POST /api/v1/customer/topups`：客户建单。

⚠️ **只在 `payments_enabled` 为真时注册**（app/main.py）：开关关闭时两个路径都是框架的 404。

回调：

- `gateway` 不是 `stripe` → 404 `NOT_FOUND`；
- **验签用原始字节**：照 app/api/integration_usage.py 的写法自己按上限读原始字节，不让框架先解析
  JSON；超过 64 KiB → 413 `PAYLOAD_TOO_LARGE`，不落库；
- 凭据读不到（`read_stripe_credentials` 或 `get_payment_gateway` 为 `None`）→ 503
  `PAYMENTS_MISCONFIGURED`，Stripe 会重试；
- 验签失败 → 400 `WEBHOOK_SIGNATURE_INVALID`；解析失败 → 400 `WEBHOOK_PAYLOAD_INVALID`。都不落库，
  各记一条 WARNING，**不带请求体、请求头原文与密钥**（设计 §6）；
- `record_event` 一个事务插入事件后提交（`livemode` 取 `read_stripe_credentials` 的结果）；新事件、
  重复投递与指纹冲突都回 200；数据库不可用 → 503 `SERVICE_UNAVAILABLE`，Stripe 会重试；
- **不在请求里做确认**：事件由后台任务（app/tasks/payment_events.py）领取，向 Stripe 查权威状态。

建单：

- 第一条语句是 `require_customer_writable`（鉴权、`DISABLED` / `CLOSED` 403 `ACCOUNT_READ_ONLY`）。
  ⚠️ 请求体因此**不用 FastAPI 的模型参数**（它会在鉴权之前校验）：鉴权之后自己读、按
  `TopupRequest` 校验，多余字段 422 `VALIDATION_ERROR`，先于 `create_topup` 的全部有序校验；
- 租户只取自 `CustomerPrincipal.tenant_id`（INV-8）；
- `create_topup` 在自己的写事务里用 `lock_tenant_for_customer_write` 锁租户行重查；它的错误都是
  `AppError`，码与状态码就是设计 §2「建单」的那几个，原样交给错误信封；
- 成功 201，`data` 恰好 `payment_id`、`redirect_url`、`expires_at`（INV-7）。

⚠️ 处理函数是 `async` 只为读请求体；鉴权、读密钥文件、Stripe 与数据库都是同步调用，丢进线程池，
不阻塞事件循环。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Final

from fastapi import APIRouter, Request
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy import exc as sa_exc
from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool

from app.api.auth import require_customer_writable, require_session_factory
from app.core.config import read_stripe_credentials
from app.core.logging import current_request_id
from app.payments import WebhookPayloadError, WebhookSignatureError, get_payment_gateway
from app.schemas.envelope import ApiResponse, success
from app.schemas.payments import (
    PaymentGatewayNotFound,
    TopupCreated,
    TopupRequest,
    WebhookPayloadInvalid,
    WebhookReceipt,
    WebhookSignatureInvalid,
)
from app.services.auth import utc_now
from app.services.payment_events import record_event
from app.services.payments import PaymentsMisconfigured, TopupResult, create_topup
from app.services.usage_ingest import PayloadTooLarge, ServiceUnavailable

logger = logging.getLogger(__name__)

router = APIRouter(tags=["payments"])

# 本批只接 Stripe（设计 §2「Stripe 适配器」）。
STRIPE: Final = "stripe"
# 回调请求体上限（设计 §2「支付 Webhook」）。
WEBHOOK_MAX_BODY_BYTES: Final = 64 * 1024


# --- 回调 -------------------------------------------------------------------------------


@router.post("/api/v1/webhooks/payments/{gateway}", response_model=ApiResponse[WebhookReceipt])
async def receive_payment_webhook(request: Request, gateway: str) -> ApiResponse[WebhookReceipt]:
    """Verify and store one gateway event; 200 for a new event, a redelivery or a conflict."""
    if gateway != STRIPE:
        raise PaymentGatewayNotFound
    body = await _read_body(request, WEBHOOK_MAX_BODY_BYTES)
    await run_in_threadpool(_record, request, body, request.headers)
    return success(WebhookReceipt(), request_id=current_request_id())


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


def _record(request: Request, raw_body: bytes, headers: Mapping[str, str]) -> None:
    settings = request.app.state.settings
    credentials = read_stripe_credentials(settings)
    if credentials is None:
        # 读不到的原因 `read_stripe_credentials` 已记 ERROR（只带路径），告警见设计 §2「告警」。
        raise PaymentsMisconfigured
    gateway = get_payment_gateway(settings)
    if gateway is None:
        raise PaymentsMisconfigured
    factory: sessionmaker[Session] | None = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise ServiceUnavailable
    # MySQL 的 DATETIME 不存小数秒。
    now = utc_now().replace(microsecond=0)
    try:
        gateway.verify_webhook(raw_body, headers, now=now)
    except WebhookSignatureError:
        logger.warning(
            "Payment webhook refused: invalid signature",
            extra={"gateway": STRIPE, "error_code": "WEBHOOK_SIGNATURE_INVALID"},
        )
        raise WebhookSignatureInvalid from None
    try:
        parsed = gateway.parse_webhook(raw_body)
    except WebhookPayloadError:
        logger.warning(
            "Payment webhook refused: unusable payload",
            extra={"gateway": STRIPE, "error_code": "WEBHOOK_PAYLOAD_INVALID"},
        )
        raise WebhookPayloadInvalid from None
    try:
        record_event(factory, parsed, raw_body, now=now, livemode=credentials.livemode)
    except (sa_exc.OperationalError, sa_exc.InterfaceError, sa_exc.TimeoutError):
        logger.exception(
            "Database unavailable while recording a payment webhook event",
            extra={"gateway_event_id": parsed.gateway_event_id, "event_type": parsed.event_type},
        )
        raise ServiceUnavailable from None


# --- 建单 -------------------------------------------------------------------------------


@router.post(
    "/api/v1/customer/topups",
    status_code=201,
    response_model=ApiResponse[TopupCreated],
)
async def create_customer_topup(request: Request) -> ApiResponse[TopupCreated]:
    """Create (or reuse within 60 seconds) a `PENDING` top-up and its Stripe payment page."""
    principal = await run_in_threadpool(require_customer_writable, request)
    payload = _topup_request(await request.body())
    result = await run_in_threadpool(_create, request, principal.tenant_id, payload.amount)
    created = TopupCreated(
        payment_id=result.payment_public_id,
        redirect_url=result.redirect_url,
        expires_at=result.expires_at,
    )
    return success(created, request_id=current_request_id())


def _topup_request(raw_body: bytes) -> TopupRequest:
    """Validate the body after authentication. 422 names the fields, never echoes their values."""
    try:
        return TopupRequest.model_validate_json(raw_body)
    except ValidationError as error:
        raise RequestValidationError(
            [
                {"type": item["type"], "loc": ("body", *item["loc"]), "msg": item["msg"]}
                for item in error.errors()
            ]
        ) from None


def _create(request: Request, tenant_id: int, amount: object) -> TopupResult:
    return create_topup(
        require_session_factory(request),
        get_payment_gateway,
        tenant_id,
        amount,
        now=utc_now().replace(microsecond=0),
        settings=request.app.state.settings,
    )


__all__ = ["STRIPE", "WEBHOOK_MAX_BODY_BYTES", "router"]
