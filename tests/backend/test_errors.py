"""Unified error responses (spec §107) and the request context that feeds them."""

from __future__ import annotations

import io
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.core.errors import AppError, register_error_handlers
from app.core.logging import REDACTED, JsonFormatter
from app.core.middleware import REQUEST_ID_HEADER, RequestContextMiddleware


class Payload(BaseModel):
    amount: int
    api_secret: str


class WalletNotFound(AppError):
    code = "WALLET_NOT_FOUND"
    http_status = 404


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    register_error_handlers(app)

    @app.get("/boom")
    def boom():
        raise RuntimeError("connection string postgres://user:hunter2@db-01/billing")

    @app.get("/known")
    def known():
        raise WalletNotFound("Wallet does not exist for this tenant.")

    @app.post("/echo")
    def echo(payload: Payload):
        return {"ok": True}

    return TestClient(app, raise_server_exceptions=False)


def test_unexpected_error_log_is_scrubbed_too(client: TestClient) -> None:
    """响应守住了不等于日志守住了 —— 两条路径都要验。

    日志里的凭据比响应里的更危险：它会被永久留存、被转发到集中日志平台，
    而且没人会盯着它看。
    """
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        client.get("/boom")
    finally:
        root.removeHandler(handler)

    logged = stream.getvalue()
    assert "hunter2" not in logged
    assert REDACTED in logged
    # 栈帧仍在，否则脱敏的代价是排不了障。
    assert "Traceback" in logged


def test_unexpected_error_never_leaks_internals(client: TestClient) -> None:
    """把 str(exc) 塞进 message 是最常见的信息泄漏 —— 这条挡的就是它。"""
    response = client.get("/boom")

    assert response.status_code == 500
    body = response.json()
    assert body["success"] is False
    assert body["data"] is None
    assert body["error"] == {"code": "INTERNAL_ERROR", "message": "An internal error occurred."}
    assert "hunter2" not in response.text
    assert "postgres" not in response.text
    assert "Traceback" not in response.text


def test_domain_error_carries_its_own_code_and_status(client: TestClient) -> None:
    response = client.get("/known")

    assert response.status_code == 404
    assert response.json()["error"] == {
        "code": "WALLET_NOT_FOUND",
        "message": "Wallet does not exist for this tenant.",
    }


def test_framework_404_uses_the_same_envelope(client: TestClient) -> None:
    """不统一的话，客户端要为「路由不存在」认第二种响应形状。"""
    body = client.get("/no-such-route").json()

    assert body["success"] is False
    assert body["error"]["code"] == "HTTP_ERROR"
    assert set(body) == {"success", "data", "error", "request_id"}


def test_validation_error_reports_fields_but_not_values(client: TestClient) -> None:
    response = client.post("/echo", json={"amount": "not-a-number", "api_secret": "s3cr3t"})

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert "amount" in body["error"]["message"]
    # 校验失败的请求体里可能有密钥，一个值都不许回显。
    assert "s3cr3t" not in response.text
    assert "not-a-number" not in response.text


def test_error_envelope_carries_the_request_id(client: TestClient) -> None:
    response = client.get("/boom")

    assert response.json()["request_id"] == response.headers[REQUEST_ID_HEADER]


def test_inbound_request_id_is_reused_for_cross_service_correlation(client: TestClient) -> None:
    response = client.get("/known", headers={REQUEST_ID_HEADER: "upstream-abc_1.2"})

    assert response.headers[REQUEST_ID_HEADER] == "upstream-abc_1.2"
    assert response.json()["request_id"] == "upstream-abc_1.2"


@pytest.mark.parametrize(
    "hostile",
    ["with space", "line\nbreak", "x" * 65, "semi;colon", ""],
)
def test_untrusted_request_id_is_rejected(client: TestClient, hostile: str) -> None:
    """入站 id 会进日志也会回响应头：换行能伪造日志行，超长值能撑爆日志。"""
    response = client.get("/known", headers={REQUEST_ID_HEADER: hostile})

    assert response.headers[REQUEST_ID_HEADER] != hostile
    assert len(response.headers[REQUEST_ID_HEADER]) == 36  # 退回自己生成的 uuid4


def test_each_request_starts_from_a_clean_context(client: TestClient) -> None:
    first = client.get("/known").headers[REQUEST_ID_HEADER]
    second = client.get("/known").headers[REQUEST_ID_HEADER]

    assert first != second
