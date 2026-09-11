"""The API response envelope (spec §107).

每个响应都是 `{success, data, error, request_id}`。**成功时 `error` 为 null、
失败时 `data` 为 null**，两者不同时有值——客户端只要看 `success` 一个字段就能
分支，不必猜哪个字段才是真的。
"""

from __future__ import annotations

from pydantic import BaseModel


class ApiError(BaseModel):
    """The machine-readable half of a failure.

    `code` 是稳定契约，客户端按它分支；`message` 是给人看的，**随时可改，且
    绝不包含栈、SQL 或内部标识**（§107）。
    """

    code: str
    message: str


class ApiResponse[T](BaseModel):
    success: bool
    data: T | None = None
    error: ApiError | None = None
    request_id: str | None = None


def success[T](data: T, request_id: str | None = None) -> ApiResponse[T]:
    return ApiResponse[T](success=True, data=data, error=None, request_id=request_id)


def failure(code: str, message: str, request_id: str | None = None) -> ApiResponse[None]:
    return ApiResponse[None](
        success=False, data=None, error=ApiError(code=code, message=message), request_id=request_id
    )
