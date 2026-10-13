"""Small Anthropic adapter; no business-module database access."""

from __future__ import annotations

from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class Generation:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    stop_reason: str | None = None


class AnthropicProvider:
    def __init__(self, api_key: str, model: str, *, transport: httpx.BaseTransport | None = None) -> None:
        self.api_key = api_key
        self.model = model
        self.transport = transport

    def generate(
        self,
        system: str,
        context: str,
        *,
        output_schema: dict | None = None,
        effort: str | None = None,
        max_tokens: int = 500,
    ) -> Generation:
        if not self.api_key:
            raise RuntimeError("AI provider is not configured")
        body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": context}],
        }
        output_config = {}
        if output_schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": output_schema}
        if effort is not None:
            output_config["effort"] = effort
        if output_config:
            body["output_config"] = output_config
        with httpx.Client(timeout=60, transport=self.transport) as client:
            response = client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=body,
            )
            response.raise_for_status()
            data = response.json()
        usage = data["usage"]
        blocks = data["content"]
        text = "\n".join(block["text"] for block in blocks if block.get("type") == "text")
        # A response with usage must still enter the outbox, including a refusal or empty text.
        if not text:
            text = "模型未生成可显示的文本。"
        return Generation(
            text=text,
            model=data["model"],
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            cache_creation_input_tokens=usage.get("cache_creation_input_tokens") or 0,
            cache_read_input_tokens=usage.get("cache_read_input_tokens") or 0,
            stop_reason=data.get("stop_reason"),
        )
