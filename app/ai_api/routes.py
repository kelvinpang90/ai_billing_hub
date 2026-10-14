"""The public embedded Anthropic Messages subset."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.ai_api.access import (
    AuthenticationFailed,
    ModelNotAllowed,
    ServiceBlocked,
    authorize_request,
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
from app.core.config import Settings
from app.core.errors import AppError

router = APIRouter(tags=["ai"])


@dataclass
class AiRuntime:
    engine: Engine
    session_factory: sessionmaker[Session]
    executor: AiDatabaseExecutor
    held_buffer: HeldBuffer
    key: str | None = None
    key_loaded: bool = False


class AiError(AppError):
    pass


def _error(code: str, status: int, message: str) -> AiError:
    return AiError(message, code=code, http_status=status)


def _bearer(request: Request) -> str | None:
    value = request.headers.get("authorization", "")
    scheme, separator, token = value.partition(" ")
    return token if separator and scheme == "Bearer" and token else None


async def _body(request: Request, maximum: int) -> bytes:
    declared = request.headers.get("content-length", "")
    if declared.isascii() and declared.isdigit() and int(declared) > maximum:
        raise _error("REQUEST_TOO_LARGE", 422, "Request body exceeds the configured limit.")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > maximum:
            raise _error("REQUEST_TOO_LARGE", 422, "Request body exceeds the configured limit.")
        chunks.append(chunk)
    return b"".join(chunks)


def _parse(body: bytes, settings: Settings) -> MessageRequest:
    try:
        value = json.loads(body)
        parsed = MessageRequest.model_validate(value)
        return validate_message_request(parsed, settings.ai_max_output_tokens)
    except (ValueError, TypeError, ValidationError) as error:
        raise _error("VALIDATION_ERROR", 422, "Invalid request.") from error


def _load_key(runtime: AiRuntime, settings: Settings) -> str | None:
    if not runtime.key_loaded:
        value = read_anthropic_key(Path(settings.ai_anthropic_key_file))
        if value is not None:
            runtime.key = value
            runtime.key_loaded = True
    return runtime.key


@router.post("/v1/messages")
async def create_message(request: Request) -> JSONResponse:
    settings: Settings = request.app.state.settings
    runtime: AiRuntime | None = getattr(request.app.state, "ai_runtime", None)
    if runtime is None:
        raise _error("AI_STORE_UNAVAILABLE", 503, "AI storage is unavailable.")
    body = await _body(request, settings.ai_max_request_bytes)
    message = _parse(body, settings)
    auth_limit = (
        settings.ai_db_pool_timeout_seconds
        + settings.ai_db_connect_timeout_seconds
        + settings.ai_db_rw_timeout_seconds
    )
    started = time.monotonic()
    try:
        async with asyncio.timeout(auth_limit):
            authorized = await runtime.executor.run(
                authorize_request,
                runtime.session_factory,
                client_id=request.headers.get("x-acuven-client-id"),
                token=_bearer(request),
                model=message.model,
            )
    except AuthenticationFailed:
        raise _error("AI_AUTH_FAILED", 401, "AI authentication failed.") from None
    except ServiceBlocked:
        raise _error("AI_SERVICE_BLOCKED", 403, "AI service is blocked.") from None
    except ModelNotAllowed:
        raise _error("MODEL_NOT_ALLOWED", 422, "Model is not allowed.") from None
    except Exception as error:
        if isinstance(error, AiError):
            raise
        raise _error("AI_STORE_UNAVAILABLE", 503, "AI storage is unavailable.") from None
    key = _load_key(runtime, settings)
    if key is None:
        raise _error("AI_PROVIDER_NOT_CONFIGURED", 503, "AI provider is not configured.")
    provider_budget = min(
        settings.ai_provider_timeout_seconds,
        settings.ai_request_deadline_seconds
        - (time.monotonic() - started)
        - _write_limit(settings),
    )
    if provider_budget <= 0:
        raise _error("AI_PROVIDER_UNAVAILABLE", 502, "AI provider is unavailable.")
    forwarded = message.model_copy(update={"model": authorized.model_code})
    try:
        async with asyncio.timeout(provider_budget):
            generation = await AnthropicProvider(key).generate(
                forwarded,
                timeout=provider_budget,
                wall_clock_budget=provider_budget,
            )
    except ProviderRejectedRequest as error:
        raise _error("PROVIDER_REJECTED_REQUEST", 422, error.error_type) from None
    except (ProviderUnavailable, TimeoutError):
        raise _error("AI_PROVIDER_UNAVAILABLE", 502, "AI provider is unavailable.") from None
    request_id = new_ulid()
    payload = usage_payload(
        tenant_id=authorized.tenant_public_id,
        project_id=authorized.project_public_id,
        feature=message.feature,
        request_id=request_id,
        model=generation.model,
        input_tokens=generation.input_tokens,
        output_tokens=generation.output_tokens,
        cache_creation_input_tokens=generation.cache_creation_input_tokens,
        cache_read_input_tokens=generation.cache_read_input_tokens,
    )
    held = HeldUsage(
        payload,
        authorized.internal_tenant_id,
        authorized.internal_project_id,
        request_id,
        "unknown",
    )
    outcome = await persist_usage(
        runtime.executor,
        runtime.session_factory,
        runtime.held_buffer,
        held,
        timeout_seconds=_write_limit(settings),
    )
    if outcome not in {"written", "written_after_error"}:
        raise _error("AI_USAGE_NOT_PERSISTED", 503, "AI usage was not persisted.")
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
    return JSONResponse(status_code=200, content=response.model_dump(mode="json"))


def _write_limit(settings: Settings) -> float:
    return (
        2 * (settings.ai_db_pool_timeout_seconds + settings.ai_db_connect_timeout_seconds)
        + 3 * settings.ai_db_rw_timeout_seconds
    )
