"""Smoke tests: the app boots and answers liveness with no datastore attached."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


def test_healthz_returns_ok() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_create_app_uses_injected_settings() -> None:
    """工厂必须能被注入配置，否则后续任务只能靠改环境变量测不同分支。"""
    assert create_app(Settings(debug=True)).debug is True
    assert create_app(Settings(debug=False)).debug is False
