"""Liveness and readiness endpoints.

**这两个是不同的问题，不能合成一个。**

- `/healthz`（存活）：进程还在不在。**刻意不依赖任何外部组件** —— MySQL /
  Redis 挂掉时它仍须应答，否则编排器会在数据库恢复期间反复重启 API 容器，
  把一次可恢复的故障变成滚动重启。
- `/readyz`（就绪）：能不能接活。这个**要**查依赖，不通就返回 503，让负载
  均衡把流量挪开。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.core.broker import BrokerStatus, check_broker
from app.core.database import check_database
from app.core.logging import current_request_id
from app.schemas.envelope import ApiResponse, success

logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])


class HealthStatus(BaseModel):
    status: str


class ReadinessStatus(BaseModel):
    status: str
    database: str
    redis: str


@router.get("/healthz", response_model=ApiResponse[HealthStatus])
def healthz() -> ApiResponse[HealthStatus]:
    """Return a constant payload; the presence of a response is the signal.

    仍然走 §107 的信封：编排器只看状态码，而**给健康检查开一个形状例外，就得
    在每个客户端里维护「这个端点不一样」**。一致的代价在这里是零。
    """
    return success(HealthStatus(status="ok"), request_id=current_request_id())


@router.get("/readyz", response_model=ApiResponse[ReadinessStatus])
def readyz(request: Request) -> ApiResponse[ReadinessStatus]:
    """Check every dependency this process needs to serve traffic.

    **数据库不通 = 503；Redis 不通 = 200 但 `degraded`。**这个不对称是刻意的：

    spec §74.6 规定 Redis/Celery 只承载投递触发、数据库 Outbox 才是事实来源，
    REQ-AVAIL-001 又要求 Redis 不可用不得成为终端 AI 请求路径上的同步依赖。
    Redis 挂了，API 仍能收用量事件、落库、返回 202，投递触发等 Redis 回来由
    周期恢复补上。**这时把实例摘出轮转，就是自己造出 Invariant 1 要防的中断。**

    数据库不通则不同：落不了库就等于事件丢了，那必须停止接流量。

    失败时 `check_database()` 抛 `AppError`，由统一处理器渲染成 503 信封 ——
    **不在这里 catch 后自己拼响应**，那样会出现第二种错误形状。
    """
    check_database(getattr(request.app.state, "engine", None))
    broker = check_broker(request.app.state.settings)
    if broker is not BrokerStatus.OK:
        # **这条日志是这个故障唯一的发现途径。**因为返回的是 200，负载均衡
        # 不会替我们发现它；告警只能挂在这条上。message 与 component 是稳定
        # 契约，改它们等于把告警条件改没了 —— 见 docs/runbook.md。
        logger.warning(
            "Readiness degraded", extra={"component": "redis", "component_status": broker.value}
        )
    return success(
        ReadinessStatus(
            status="ok" if broker is BrokerStatus.OK else "degraded",
            database="ok",
            redis=broker.value,
        ),
        request_id=current_request_id(),
    )
