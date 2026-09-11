"""Liveness endpoint.

刻意不依赖任何外部组件：MySQL / Redis 挂掉时这个端点仍须应答，否则编排器会
在数据库恢复期间反复重启 API 容器。带依赖的就绪检查是另一件事，等 T0.4 /
T0.5 把数据存储接进来之后单独加。
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.core.logging import current_request_id
from app.schemas.envelope import ApiResponse, success

router = APIRouter(tags=["health"])


class HealthStatus(BaseModel):
    status: str


@router.get("/healthz", response_model=ApiResponse[HealthStatus])
def healthz() -> ApiResponse[HealthStatus]:
    """Return a constant payload; the presence of a response is the signal.

    仍然走 §107 的信封：编排器只看状态码，而**给健康检查开一个形状例外，就得
    在每个客户端里维护「这个端点不一样」**。一致的代价在这里是零。
    """
    return success(HealthStatus(status="ok"), request_id=current_request_id())
