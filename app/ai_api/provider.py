"""Async Anthropic adapter with deliberately small request and response surfaces."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2

from app.ai_api.client_rules import MAX_TOKENS, MODEL_PATTERN
from app.ai_api.messages import MessageRequest

_ERROR_TYPE_PATTERN = re.compile(r"^[a-z_]{1,64}$")
_MODEL_PATTERN = re.compile(MODEL_PATTERN)


class ProviderRejectedRequest(Exception):
    def __init__(self, error_type: str) -> None:
        super().__init__(error_type)
        self.error_type = error_type


class ProviderUnavailable(Exception):
    pass


@dataclass(frozen=True)
class Generation:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    stop_reason: str | None


def read_anthropic_key(path: str | Path) -> str | None:
    try:
        value = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    value = value.strip()
    return value or None


read_anthropic_key_file = read_anthropic_key


def _rejection_type(response: httpx2.Response) -> str:
    try:
        value = response.json().get("error", {}).get("type")
    except (ValueError, AttributeError, TypeError):
        return "unknown"
    return value if isinstance(value, str) and _ERROR_TYPE_PATTERN.fullmatch(value) else "unknown"


def _usage(data: dict[str, Any]) -> tuple[int, int, int, int] | None:
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return None
    values: list[int] = []
    for name, required in (
        ("input_tokens", True),
        ("output_tokens", True),
        ("cache_creation_input_tokens", False),
        ("cache_read_input_tokens", False),
    ):
        value = usage.get(name)
        if value is None and not required:
            values.append(0)
        elif type(value) is int and 0 <= value <= MAX_TOKENS:
            values.append(value)
        else:
            return None
    return tuple(values)  # type: ignore[return-value]


class AnthropicProvider:
    def __init__(self, api_key: str, *, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        self.api_key = api_key
        self.transport = transport

    async def generate(
        self,
        request: MessageRequest,
        *,
        timeout: float,
        wall_clock_budget: float,
    ) -> Generation:
        if not self.api_key:
            raise ProviderUnavailable
        body: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "messages": [message.model_dump() for message in request.messages],
        }
        if request.system is not None:
            body["system"] = request.system
        if request.output_config is not None:
            body["output_config"] = request.output_config.model_dump(
                by_alias=True, exclude_none=True
            )
        try:
            async with asyncio.timeout(wall_clock_budget):
                async with httpx2.AsyncClient(timeout=timeout, transport=self.transport) as client:
                    response = await client.post(
                        "https://api.anthropic.com/v1/messages",
                        headers={
                            "x-api-key": self.api_key,
                            "anthropic-version": "2023-06-01",
                            "content-type": "application/json",
                        },
                        json=body,
                    )
        except (TimeoutError, httpx2.RequestError):
            raise ProviderUnavailable from None
        if response.status_code in {400, 404, 413, 422}:
            raise ProviderRejectedRequest(_rejection_type(response))
        if not 200 <= response.status_code < 300:
            raise ProviderUnavailable
        try:
            data = response.json()
        except ValueError:
            raise ProviderUnavailable from None
        if not isinstance(data, dict):
            raise ProviderUnavailable
        model = data.get("model")
        usage = _usage(data)
        if not isinstance(model, str) or _MODEL_PATTERN.fullmatch(model) is None or usage is None:
            raise ProviderUnavailable
        content = data.get("content")
        text = (
            "\n".join(
                block["text"]
                for block in content
                if isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            )
            if isinstance(content, list)
            else ""
        )
        stop_reason = data.get("stop_reason")
        return Generation(
            text, model, *usage, stop_reason if isinstance(stop_reason, str) else None
        )
