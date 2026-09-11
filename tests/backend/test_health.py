"""Smoke tests: the app boots and answers liveness with no datastore attached."""

from __future__ import annotations

import os

import pytest
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


def test_readyz_is_degraded_but_serving_when_redis_is_missing() -> None:
    """⚠️ Redis 不通**不能**让实例被摘出轮转。

    spec §74.6：Redis/Celery 只承载投递触发，数据库 Outbox 才是事实来源。
    REQ-AVAIL-001：Redis 不可用不得成为终端 AI 请求路径上的同步依赖。
    把 Redis 做成就绪阻断项，等于 Redis 一挂就自己造出 Invariant 1 要防的中断。
    """
    app = create_app(Settings(database_url="", redis_url=""))
    app.state.engine = create_engine("sqlite+pysqlite:///:memory:")

    with TestClient(app) as client:
        response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json()["data"] == {
        "status": "degraded",
        "database": "ok",
        "redis": "not_configured",
    }


def test_readyz_is_degraded_when_redis_is_unreachable() -> None:
    app = create_app(Settings(database_url="", redis_url="redis://127.0.0.1:1/0"))
    app.state.engine = create_engine("sqlite+pysqlite:///:memory:")

    with TestClient(app) as client:
        response = client.get("/readyz")

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["status"] == "degraded"
    assert body["redis"] == "unavailable"
    # 连接错误里带着主机名与端口（§94）—— 一个字都不许进响应。
    assert "127.0.0.1" not in response.text


@pytest.mark.skipif(
    not os.environ.get("BILLING_TEST_REDIS_URL"),
    reason="BILLING_TEST_REDIS_URL is not set; the healthy-readiness path needs a real Redis",
)
def test_readyz_is_ok_when_both_dependencies_answer() -> None:
    app = create_app(Settings(database_url="", redis_url=os.environ["BILLING_TEST_REDIS_URL"]))
    app.state.engine = create_engine("sqlite+pysqlite:///:memory:")

    with TestClient(app) as client:
        response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json()["data"] == {"status": "ok", "database": "ok", "redis": "ok"}


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
