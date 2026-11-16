"""Downloading a monthly statement PDF, shared by the customer and admin routes (gate #708 v3).

设计 §2「下载：statement_pdf_response」「客户接口」「管理端接口」「事务边界」、§5、§6；契约见
[docs/api.md](../../docs/api.md)「月度对账单（客户）」「月度对账单（管理端）」。AIH-TASK-217。
写法照 app/api/receipt_download.py。

⚠️ **租户过滤由调用方显式给出**：客户传令牌主体的 `tenant_id`（INV-8，别的租户的对账单与不存在的
不可区分，同是 404 `STATEMENT_NOT_FOUND`）；只有管理端（`require_admin` 之后）传 `None`。参数没有
缺省值，漏传是类型错误而不是「不过滤」。不是小写规范 uuid、非 `FINALIZED` 也是同一个 404。

⚠️ **只读，渲染在会话关闭之后**：会话里只读出四列（不加锁、不提交、不写审计），关闭之后才调用
`render_statement_pdf`，不持连接渲染。PDF 只来自快照与两个不可变列（spec §106、INV-6、INV-12）。

⚠️ **渲染失败**记一条只带对账单 `public_id`（兜底的那种另带异常类名）的 ERROR，转成通用 500
`INTERNAL_ERROR` 信封（固定文案）；不记快照内容、公司名、项目名、金额与异常原文，所以不用
`logger.exception`、也不交给 `handle_unexpected_error`（设计 §6）。成功下载不记日志。

⚠️ **文件名只来自对账单号**（`ST-YYYYMM-` 加 8 位 Crockford base32，渲染时已校验），不拼任何客户
输入。
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Final

from fastapi import status
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from starlette.responses import Response

from app.core.errors import AppError
from app.models.statements import MonthlyStatement, StatementStatus
from app.schemas.statements import StatementNotFound, is_statement_id
from app.services.statement_pdf import (
    StatementFontUnavailable,
    StatementSnapshotInvalid,
    render_statement_pdf,
)

logger = logging.getLogger(__name__)

STATEMENT_MEDIA_TYPE: Final = "application/pdf"


class StatementRenderFailed(AppError):
    """The statement cannot be rendered. Same envelope as the generic 500."""

    def __init__(self) -> None:
        super().__init__(
            "An internal error occurred.",
            code="INTERNAL_ERROR",
            http_status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


class StatementPdfResponse(Response):
    """A 200 `application/pdf` attachment."""

    media_type = STATEMENT_MEDIA_TYPE


def _read(
    factory: sessionmaker[Session], statement_id: str, tenant_id: int | None
) -> tuple[str, str, dt.datetime, Any]:
    """`(public_id, statement_number, finalized_at, snapshot)`; `StatementNotFound` otherwise."""
    query = select(
        MonthlyStatement.public_id,
        MonthlyStatement.statement_number,
        MonthlyStatement.finalized_at,
        MonthlyStatement.snapshot_json,
    ).where(
        MonthlyStatement.public_id == statement_id,
        MonthlyStatement.status == StatementStatus.FINALIZED,
    )
    if tenant_id is not None:
        query = query.where(MonthlyStatement.tenant_id == tenant_id)
    with factory() as session:
        found = session.execute(query).one_or_none()
    if found is None:
        raise StatementNotFound
    public_id, statement_number, finalized_at, snapshot = found
    return str(public_id), str(statement_number), finalized_at, snapshot


def statement_pdf_response(
    factory: sessionmaker[Session], statement_id: str, *, tenant_id: int | None
) -> StatementPdfResponse:
    """The statement's PDF as an attachment named `<statement_number>.pdf`.

    `tenant_id`：客户传令牌主体的租户；只有管理端传 `None`（不按租户过滤）。
    """
    if not is_statement_id(statement_id):
        raise StatementNotFound
    public_id, statement_number, finalized_at, snapshot = _read(factory, statement_id, tenant_id)
    # 会话已关闭。日志只带对账单 public_id：不带快照内容、公司名、金额与异常原文（设计 §6）。
    try:
        body = render_statement_pdf(
            snapshot, statement_number=statement_number, finalized_at=finalized_at
        )
    except StatementSnapshotInvalid:
        logger.error(
            "Statement snapshot invalid; download refused", extra={"statement_id": public_id}
        )
        raise StatementRenderFailed from None
    except StatementFontUnavailable:
        logger.error(
            "Statement font unavailable; download refused", extra={"statement_id": public_id}
        )
        raise StatementRenderFailed from None
    except Exception as exc:
        logger.error(
            "Statement PDF rendering failed",
            extra={"statement_id": public_id, "error_type": type(exc).__name__},
        )
        raise StatementRenderFailed from None
    return StatementPdfResponse(
        content=body,
        headers={
            "Content-Disposition": f'attachment; filename="{statement_number}.pdf"',
            "Cache-Control": "no-store",
        },
    )


__all__ = [
    "STATEMENT_MEDIA_TYPE",
    "StatementPdfResponse",
    "StatementRenderFailed",
    "statement_pdf_response",
]
