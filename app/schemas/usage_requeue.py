"""Request and response shapes for requeueing usage events (design gate #181 v2 §2「重新入队」).

⚠️ 请求体一律 `extra="forbid"`，`reason` 必填（去首尾空白后 1–255，与退役的 `Reason` 同一类型）。
批量的 `status` 只收四个错误状态之一，其余取值 422；时间段是 RFC 3339、**必须带时区**、整秒
（与发布的 `effective_from` 同一个 `Moment`），`occurred_from` 必须早于 `occurred_to`。

⚠️ **响应只含数量与事件的 `public_id`**（设计 §6），不含成本、金额或内部 id。
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator

from app.schemas.provider_prices import Moment, PublicId, Reason

# 四个错误状态（与 app/services/usage_requeue.py 的 REQUEUABLE_STATUSES 一致）。
RequeuableStatus = Literal["MODEL_UNKNOWN", "PRICING_ERROR", "FX_RATE_ERROR", "FAILED_FINAL"]
# 列宽：`error_code` 64；上报的供应商代码 64、模型代码 128。
ErrorCode = Annotated[str, StringConstraints(min_length=1, max_length=64)]
ProviderCode = Annotated[str, StringConstraints(min_length=1, max_length=64)]
ModelCode = Annotated[str, StringConstraints(min_length=1, max_length=128)]


class RequeueUsageEventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Reason


class RequeueUsageEventsRequest(BaseModel):
    """批量：`status` 必填，其余是可选的筛选条件，按内部 id 升序至多 1000 条。"""

    model_config = ConfigDict(extra="forbid")

    status: RequeuableStatus
    error_code: ErrorCode | None = None
    # 客户的 public_id。
    customer_id: PublicId | None = None
    # 上报的原始字符串，逐字节比较。
    provider: ProviderCode | None = None
    model: ModelCode | None = None
    occurred_from: Moment = None
    occurred_to: Moment = None
    reason: Reason

    @model_validator(mode="after")
    def _a_period_is_not_empty(self) -> RequeueUsageEventsRequest:
        if (
            self.occurred_from is not None
            and self.occurred_to is not None
            and self.occurred_from >= self.occurred_to
        ):
            raise ValueError("occurred_from must be before occurred_to")
        return self


class RequeuedUsageEventView(BaseModel):
    """单个：被放回的事件与它的新状态（永远是 `RECEIVED`）。"""

    id: str
    status: str


class RequeuedUsageEventsView(BaseModel):
    """批量：放回与跳过的条数，以及放回了的事件的 `public_id`（按内部 id 升序）。"""

    requeued: int
    skipped: int
    ids: list[str]


__all__ = [
    "RequeuableStatus",
    "RequeueUsageEventRequest",
    "RequeueUsageEventsRequest",
    "RequeuedUsageEventView",
    "RequeuedUsageEventsView",
]
