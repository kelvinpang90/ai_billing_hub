"""Downloading a payment receipt PDF, shared by the customer and admin routes (gate #589 v2).

设计 §2「客户接口」「管理端接口」「事务边界」、§5 下载的两行、§6；契约见
[docs/api.md](../../docs/api.md)「支付（客户）」。客户的下载接口属于 AIH-TASK-202，管理端的
（AIH-TASK-203）复用这里的 `receipt_pdf_response`。

⚠️ **租户过滤由调用方显式给出**：客户传令牌主体的 `tenant_id`（INV-8，别的租户的收据与不存在的
不可区分，同是 404 `RECEIPT_NOT_FOUND`）；只有管理端（`require_admin` 之后）传 `None`。参数没有
缺省值，漏传是类型错误而不是「不过滤」。`receipt_id` 不做格式校验：不像 uuid 的值只是查不到。

⚠️ **只读，渲染在会话关闭之后**：会话里只读出收据号与快照（不加锁、不提交、不写审计），关闭之后
才调用 `render_receipt_pdf`，不持连接渲染。PDF 只来自快照（spec §106、INV-6）。

⚠️ **快照不合规**（`ReceiptSnapshotInvalid`）记一条只带收据 `public_id` 的 ERROR，转成通用 500
`INTERNAL_ERROR` 信封（固定文案，与 `handle_unexpected_error` 相同）；不记快照内容、公司名与金额
（设计 §6）。

⚠️ **文件名只来自收据号**（`ACV-AIR-YYYY-NNNNNN`，ASCII），不拼任何客户输入；列里的收据号不是
这个格式时按快照不合规处理。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Final

from fastapi import status
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from starlette.responses import Response

from app.core.errors import AppError
from app.models.receipts import PaymentReceipt
from app.services.receipt_pdf import ReceiptSnapshotInvalid, render_receipt_pdf

logger = logging.getLogger(__name__)

RECEIPT_NOT_FOUND: Final = "RECEIPT_NOT_FOUND"
RECEIPT_MEDIA_TYPE: Final = "application/pdf"

_RECEIPT_NUMBER: Final = re.compile(r"ACV-AIR-[0-9]{4}-[0-9]{6}")


class ReceiptNotFound(AppError):
    """No receipt has this `public_id` (or, for a customer, it is another tenant's)."""

    code = RECEIPT_NOT_FOUND
    http_status = status.HTTP_404_NOT_FOUND

    def __init__(self) -> None:
        super().__init__("Receipt not found.")


class ReceiptRenderFailed(AppError):
    """The stored snapshot cannot be rendered. Same envelope as the generic 500."""

    def __init__(self) -> None:
        super().__init__(
            "An internal error occurred.",
            code="INTERNAL_ERROR",
            http_status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


class ReceiptPdfResponse(Response):
    """A 200 `application/pdf` attachment."""

    media_type = RECEIPT_MEDIA_TYPE


def _read(
    factory: sessionmaker[Session], receipt_id: str, tenant_id: int | None
) -> tuple[str, str, Any]:
    """`(public_id, receipt_number, snapshot)` of the receipt; `ReceiptNotFound` otherwise."""
    statement = select(
        PaymentReceipt.public_id, PaymentReceipt.receipt_number, PaymentReceipt.snapshot_json
    ).where(PaymentReceipt.public_id == receipt_id)
    if tenant_id is not None:
        statement = statement.where(PaymentReceipt.tenant_id == tenant_id)
    with factory() as session:
        found = session.execute(statement).one_or_none()
    if found is None:
        raise ReceiptNotFound
    public_id, receipt_number, snapshot = found
    return str(public_id), str(receipt_number), snapshot


def receipt_pdf_response(
    factory: sessionmaker[Session], receipt_id: str, *, tenant_id: int | None
) -> ReceiptPdfResponse:
    """The receipt's PDF as an attachment named `<receipt_number>.pdf`.

    `tenant_id`：客户传令牌主体的租户；只有管理端传 `None`（不按租户过滤）。
    """
    public_id, receipt_number, snapshot = _read(factory, receipt_id, tenant_id)
    try:
        if _RECEIPT_NUMBER.fullmatch(receipt_number) is None:
            raise ReceiptSnapshotInvalid("receipt number column has an unexpected format")
        body = render_receipt_pdf(snapshot)
    except ReceiptSnapshotInvalid:
        # 只带收据 public_id：不带快照内容与异常原文（设计 §6）。
        logger.error("Receipt snapshot invalid; download refused", extra={"receipt_id": public_id})
        raise ReceiptRenderFailed from None
    return ReceiptPdfResponse(
        content=body,
        headers={
            "Content-Disposition": f'attachment; filename="{receipt_number}.pdf"',
            "Cache-Control": "no-store",
        },
    )


__all__ = [
    "RECEIPT_MEDIA_TYPE",
    "RECEIPT_NOT_FOUND",
    "ReceiptNotFound",
    "ReceiptPdfResponse",
    "ReceiptRenderFailed",
    "receipt_pdf_response",
]
