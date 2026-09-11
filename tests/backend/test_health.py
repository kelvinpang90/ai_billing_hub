"""Smoke tests: the app boots and answers liveness with no datastore attached."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import create_engine

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


def test_app_starts_without_a_database() -> None:
    """配置笔误不该让容器进重启循环 —— 存活探针必须仍能应答。"""
    app = create_app(Settings(database_url=""))

    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200


def test_readyz_reports_not_configured_when_there_is_no_database() -> None:
    with TestClient(create_app(Settings(database_url=""))) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "DATABASE_NOT_CONFIGURED"
    # 就绪失败也要走同一个信封，否则编排器与客户端要认两种形状。
    assert set(body) == {"success", "data", "error", "request_id"}


def test_readyz_passes_when_the_database_answers() -> None:
    app = create_app(Settings(database_url=""))
    app.state.engine = create_engine("sqlite+pysqlite:///:memory:")

    with TestClient(app) as client:
        response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json()["data"] == {"status": "ok", "database": "ok"}


def test_readyz_reports_unavailable_without_leaking_the_target() -> None:
    app = create_app(Settings(database_url=""))
    app.state.engine = create_engine("sqlite+pysqlite:////nonexistent-dir-for-tests/x.db")

    with TestClient(app) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DATABASE_UNAVAILABLE"
    assert "nonexistent-dir-for-tests" not in response.text


def test_liveness_stays_up_when_the_database_is_down() -> None:
    """MySQL 挂掉时存活探针仍须应答，否则编排器会在恢复期间反复重启容器。"""
    app = create_app(Settings(database_url=""))
    app.state.engine = create_engine("sqlite+pysqlite:////nonexistent-dir-for-tests/x.db")

    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 503
