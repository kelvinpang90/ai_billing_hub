"""Smoke tests: the app boots and answers liveness with no datastore attached."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.middleware import REQUEST_ID_HEADER
from app.main import create_app


def test_healthz_returns_ok_envelope() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"] == {"status": "ok"}
    assert body["error"] is None
    # request_id 必须同时出现在信封与响应头里，且是同一个值 —— 它是把客户端
    # 报的问题和服务端日志对上的唯一钥匙。
    assert body["request_id"] == response.headers[REQUEST_ID_HEADER]


def test_create_app_uses_injected_settings() -> None:
    """工厂必须能被注入配置，否则后续任务只能靠改环境变量测不同分支。"""
    assert create_app(Settings(debug=True)).debug is True
    assert create_app(Settings(debug=False)).debug is False
