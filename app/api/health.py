"""Liveness and readiness endpoints.

**这两个是不同的问题，不能合成一个。**

- `/healthz`（存活）：进程还在不在。**刻意不依赖任何外部组件** —— MySQL /
  Redis 挂掉时它仍须应答，否则编排器会在数据库恢复期间反复重启 API 容器，
  把一次可恢复的故障变成滚动重启。
- `/readyz`（就绪）：能不能接活。这个**要**查依赖，不通就返回 503，让负载
  均衡把流量挪开。
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.core.database import check_database
from app.core.logging import current_request_id
from app.schemas.envelope import ApiResponse, success

router = APIRouter(tags=["health"])


class HealthStatus(BaseModel):
    status: str


class ReadinessStatus(BaseModel):
    status: str
    database: str


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

    失败时 `check_database()` 抛 `AppError`，由统一处理器渲染成 503 信封 ——
    **不在这里 catch 后自己拼响应**，那样会出现第二种错误形状。
    """
    check_database(getattr(request.app.state, "engine", None))
    return success(ReadinessStatus(status="ok", database="ok"), request_id=current_request_id())
