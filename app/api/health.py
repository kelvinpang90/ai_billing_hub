"""Liveness endpoint.

刻意不依赖任何外部组件：MySQL / Redis 挂掉时这个端点仍须应答，否则编排器会
在数据库恢复期间反复重启 API 容器。带依赖的就绪检查是另一件事，等 T0.4 /
T0.5 把数据存储接进来之后单独加。
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/healthz")
def healthz() -> dict[str, str]:
    """Return a constant payload; the presence of a response is the signal."""
    return {"status": "ok"}
