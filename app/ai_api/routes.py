"""The public, deliberately small Anthropic Messages-compatible endpoint."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.ai_api.access import (
    AuthenticationFailed,
    ModelNotAllowed,
    ServiceBlocked,
    resolve_access,
)
from app.ai_api.client_rules import new_ulid, usage_payload
from app.ai_api.database import AiDatabaseExecutor
from app.ai_api.held import HeldBuffer, HeldUsage
from app.ai_api.messages import MessageRequest, MessageResponse, Usage, validate_message_request
from app.ai_api.outbox import persist_usage
from app.ai_api.provider import (
    AnthropicProvider,
    ProviderRejectedRequest,
    ProviderUnavailable,
    read_anthropic_key,
)
from app.core.errors import AppError

router = APIRouter(tags=["ai"])
_MISSING = object()


class AiRequestError(AppError):
    def __init__(self, code: str, status: int, message: str) -> None:
        super().__init__(message, code=code, http_status=status)


@dataclass
class AiRuntime:
    engine: Engine
    session_factory: sessionmaker[Session]
    executor: AiDatabaseExecutor
    held_buffer: HeldBuffer
    key_file: str
    _key: str | None | object = _MISSING

    def key(self) -> str | None:
        if self._key is _MISSING:
            value = read_anthropic_key(Path(self.key_file))
            if value is not None:
                self._key = value
            return value
        return self._key if isinstance(self._key, str) else None


def _runtime(request: Request) -> AiRuntime:
    runtime = getattr(request.app.state, "ai_runtime", None)
    if runtime is None:
        raise AiRequestError("AI_STORE_UNAVAILABLE", 503, "AI storage is unavailable.")
    return runtime


def _read_limits(settings: Any) -> tuple[float, float]:
    read = (
        settings.ai_db_pool_timeout_seconds
        + settings.ai_db_connect_timeout_seconds
        + settings.ai_db_rw_timeout_seconds
    )
    write = (
        2 * (settings.ai_db_pool_timeout_seconds + settings.ai_db_connect_timeout_seconds)
        + 3 * settings.ai_db_rw_timeout_seconds
    )
    return read, write


def _validate(body: bytes, maximum: int, output_maximum: int) -> MessageRequest:
    if len(body) > maximum:
        raise AiRequestError("REQUEST_TOO_LARGE", 422, "Request body is too large.")
    try:
        request = MessageRequest.model_validate_json(body)
        return validate_message_request(request, output_maximum)
    except (ValidationError, ValueError):
        raise AiRequestError("VALIDATION_ERROR", 422, "Invalid request.") from None


@router.post("/v1/messages", response_model=MessageResponse)
async def messages(request: Request) -> JSONResponse:
    async with asyncio.timeout(request.app.state.settings.ai_request_deadline_seconds):
        return await _messages(request)


async def _messages(request: Request) -> JSONResponse:
    settings = request.app.state.settings
    body = await request.body()
    message = _validate(body, settings.ai_max_request_bytes, settings.ai_max_output_tokens)
    runtime = _runtime(request)
    auth_limit, write_limit = _read_limits(settings)
    started = time.monotonic()
    try:
        async with asyncio.timeout(auth_limit):
            decision = await runtime.executor.run(
                resolve_access,
                runtime.session_factory,
                client_id=request.headers.get("X-Acuven-Client-Id"),
                authorization=request.headers.get("Authorization"),
                model=message.model,
            )
    except AuthenticationFailed:
        raise AiRequestError("AI_AUTH_FAILED", 401, "AI authentication failed.") from None
    except ServiceBlocked:
        raise AiRequestError("AI_SERVICE_BLOCKED", 403, "AI service is blocked.") from None
    except ModelNotAllowed:
        raise AiRequestError("MODEL_NOT_ALLOWED", 422, "Model is not allowed.") from None
    except Exception as error:
        if isinstance(error, AiRequestError):
            raise
        raise AiRequestError("AI_STORE_UNAVAILABLE", 503, "AI storage is unavailable.") from None

    key = runtime.key()
    if key is None:
        raise AiRequestError("AI_PROVIDER_NOT_CONFIGURED", 503, "AI provider is not configured.")
    provider_budget = min(
        settings.ai_provider_timeout_seconds,
        settings.ai_request_deadline_seconds - (time.monotonic() - started) - write_limit,
    )
    if provider_budget <= 0:
        raise AiRequestError("AI_PROVIDER_UNAVAILABLE", 502, "AI provider is unavailable.")
    resolved = message.model_copy(update={"model": decision.model_code})
    try:
        async with asyncio.timeout(provider_budget):
            generation = await AnthropicProvider(key).generate(
                resolved,
                timeout=provider_budget,
                wall_clock_budget=provider_budget,
            )
    except (TimeoutError, ProviderUnavailable):
        raise AiRequestError(
            "AI_PROVIDER_UNAVAILABLE", 502, "AI provider is unavailable."
        ) from None
    except ProviderRejectedRequest as error:
        raise AiRequestError(
            "PROVIDER_REJECTED_REQUEST", 422, f"Provider rejected request: {error.error_type}."
        ) from None

    request_id = new_ulid()
    payload = usage_payload(
        tenant_id=decision.tenant_id,
        project_id=decision.project_id,
        feature=message.feature,
        request_id=request_id,
        model=generation.model,
        input_tokens=generation.input_tokens,
        output_tokens=generation.output_tokens,
        cache_creation_input_tokens=generation.cache_creation_input_tokens,
        cache_read_input_tokens=generation.cache_read_input_tokens,
    )
    held = HeldUsage(
        payload, decision.internal_tenant_id, decision.internal_project_id, request_id, "unknown"
    )
    try:
        outcome = await persist_usage(
            runtime.executor,
            runtime.session_factory,
            runtime.held_buffer,
            held,
            timeout_seconds=write_limit,
        )
    except asyncio.CancelledError:
        raise
    if outcome not in {"written", "written_after_error"}:
        raise AiRequestError("AI_USAGE_NOT_PERSISTED", 503, "AI usage was not persisted.")
    response = MessageResponse(
        request_id=request_id,
        model=generation.model,
        text=generation.text,
        stop_reason=generation.stop_reason,
        usage=Usage(
            input_tokens=generation.input_tokens,
            output_tokens=generation.output_tokens,
            cache_creation_input_tokens=generation.cache_creation_input_tokens,
            cache_read_input_tokens=generation.cache_read_input_tokens,
        ),
    )
    return JSONResponse(status_code=200, content=response.model_dump())
