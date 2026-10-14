"""Request and response shapes for the embedded Anthropic Messages subset."""

from __future__ import annotations

from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, model_validator

FEATURE_PATTERN: Final = r"^[a-z][a-z0-9_]{0,31}(?:\.[a-z][a-z0-9_]{0,31}){1,3}$"
MAX_FEATURE_LENGTH: Final = 64


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    role: str
    content: str = Field(min_length=1)

    @model_validator(mode="after")
    def role_is_supported(self) -> Message:
        if self.role not in {"user", "assistant"}:
            raise ValueError("role must be user or assistant")
        return self


class OutputFormat(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    type: str
    schema_: dict[str, Any] = Field(alias="schema")

    @model_validator(mode="after")
    def is_json_schema(self) -> OutputFormat:
        if self.type != "json_schema":
            raise ValueError("format type must be json_schema")
        return self


class OutputConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    format: OutputFormat | None = None
    effort: str | None = None

    @model_validator(mode="after")
    def effort_is_supported(self) -> OutputConfig:
        if self.effort is not None and self.effort not in {"low", "medium", "high"}:
            raise ValueError("unsupported effort")
        return self


class MessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    feature: str = Field(pattern=FEATURE_PATTERN, max_length=MAX_FEATURE_LENGTH)
    model: str = Field(max_length=128)
    system: str | None = None
    messages: list[Message] = Field(min_length=1, max_length=50)
    max_tokens: int
    output_config: OutputConfig | None = None

    @model_validator(mode="after")
    def first_message_is_user(self) -> MessageRequest:
        if self.messages[0].role != "user":
            raise ValueError("first message must have user role")
        return self

    def validate_max_tokens(self, maximum: int) -> None:
        if not 1 <= self.max_tokens <= maximum:
            raise ValueError("max_tokens is outside the configured range")


class Usage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int


class MessageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    request_id: str
    model: str
    text: str
    stop_reason: str | None
    usage: Usage


def request_body_is_within_limit(body: bytes, maximum: int) -> bool:
    """Whether the original request bytes fit the configured application limit."""
    return len(body) <= maximum


def validate_request_body_size(body: bytes, maximum: int) -> None:
    if not request_body_is_within_limit(body, maximum):
        raise ValueError("request body exceeds configured maximum")


def validate_message_request(request: MessageRequest, maximum_output_tokens: int) -> MessageRequest:
    """Apply the configurable output-token guard after structural validation."""
    request.validate_max_tokens(maximum_output_tokens)
    return request
