"""Structured logging, correlation context and redaction (spec §94)."""

from __future__ import annotations

import json
import logging
import sys

import pytest

from app.core.logging import (
    REDACTED,
    JsonFormatter,
    bind_log_context,
    clear_log_context,
    current_request_id,
    redact,
    scrub_text,
)


@pytest.fixture(autouse=True)
def _clean_context():
    clear_log_context()
    yield
    clear_log_context()


def format_record(**kwargs) -> dict:
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=kwargs.pop("msg", "hello"),
        args=None,
        exc_info=None,
    )
    for key, value in kwargs.items():
        setattr(record, key, value)
    return json.loads(JsonFormatter().format(record))


def test_output_is_one_json_object_with_the_standard_fields() -> None:
    payload = format_record()

    assert payload["level"] == "INFO"
    assert payload["logger"] == "test"
    assert payload["message"] == "hello"
    assert payload["timestamp"].endswith("+00:00")


def test_correlation_fields_are_attached_from_context() -> None:
    bind_log_context(request_id="r-1")
    bind_log_context(tenant_id="t-9", project_id="p-3")

    payload = format_record()

    assert payload["request_id"] == "r-1"
    assert payload["tenant_id"] == "t-9"
    assert payload["project_id"] == "p-3"
    assert current_request_id() == "r-1"


def test_extra_fields_reach_the_payload() -> None:
    assert format_record(status_code=200)["status_code"] == 200


@pytest.mark.parametrize(
    "key",
    ["password", "totp_secret", "api_key", "authorization", "hmac_signature", "prompt"],
)
def test_sensitive_field_names_are_redacted(key: str) -> None:
    """§94 的「绝不记录」清单按字段名兜底。"""
    assert format_record(**{key: "leaked"})[key] == REDACTED


def test_redaction_reaches_nested_structures() -> None:
    payload = format_record(context={"user": {"password": "leaked"}, "items": [{"token": "t"}]})

    assert payload["context"]["user"]["password"] == REDACTED
    assert payload["context"]["items"][0]["token"] == REDACTED


def test_non_sensitive_values_survive() -> None:
    payload = format_record(context={"user": {"email": "a@example.com"}})

    assert payload["context"]["user"]["email"] == "a@example.com"


def test_redaction_terminates_on_self_referencing_structures() -> None:
    """脱敏里出现无限递归，会把一次误用变成进程挂死。"""
    loop: dict = {"name": "x"}
    loop["self"] = loop

    redact(loop)  # 不抛 RecursionError 即为通过


def test_credential_urls_in_free_text_are_scrubbed() -> None:
    """连接串出现在异常消息里是最常见的一种泄漏。"""
    scrubbed = scrub_text("cannot reach postgres://svc:hunter2@db-01/billing")

    assert "hunter2" not in scrubbed
    assert REDACTED in scrubbed
    # 主机与库名要留着 —— 脱敏不能把排障信息一起吃掉。
    assert "db-01/billing" in scrubbed


@pytest.mark.parametrize(
    ("text", "leaked"),
    [
        ("api_secret=s3cr3t and amount=100", "s3cr3t"),
        ('{"totp_secret": "ABCD"}', "ABCD"),
        ("Authorization: Bearer abc.def", "abc.def"),
    ],
)
def test_sensitive_assignments_in_free_text_are_scrubbed(text: str, leaked: str) -> None:
    assert leaked not in scrub_text(text)


def test_scrub_leaves_ordinary_text_alone() -> None:
    assert scrub_text("wallet balance is 100 MYR") == "wallet balance is 100 MYR"
    assert scrub_text("amount=100") == "amount=100"


def test_exception_tracebacks_are_scrubbed() -> None:
    """漏掉这条，等于结构化字段守住了、异常栈那条路敞着。"""
    try:
        raise RuntimeError("cannot reach postgres://svc:hunter2@db-01/billing")
    except RuntimeError:
        record = logging.LogRecord(
            name="test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="boom",
            args=None,
            exc_info=sys.exc_info(),
        )

    payload = json.loads(JsonFormatter().format(record))

    assert "hunter2" not in payload["exception"]
    assert REDACTED in payload["exception"]
    # 栈帧要留着，否则脱敏的代价是排不了障。
    assert "Traceback" in payload["exception"]


def test_message_itself_is_scrubbed() -> None:
    assert "s3cr3t" not in format_record(msg="using api_secret=s3cr3t")["message"]


def test_context_is_replaced_not_mutated_in_place() -> None:
    """ContextVar 的值在并发任务间是共享引用，原地 mutate 会串到别的请求上。"""
    bind_log_context(request_id="r-1")
    first = format_record()

    clear_log_context()
    bind_log_context(request_id="r-2")

    assert first["request_id"] == "r-1"
    assert format_record()["request_id"] == "r-2"
