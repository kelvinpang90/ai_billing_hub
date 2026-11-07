"""Admin views of monthly statements (AIH-TASK-159).

设计闸门 #565 v6 §2「管理端接口」；契约见 [docs/api.md](../../docs/api.md)「月度对账单（管理端）」。

⚠️ **视图是字段白名单**：列表项恰好 8 个键，详情在其上加 `cutoff_at`、`ledger_high_sequence`、
`generated_by_email` 与 `snapshot`。内部自增 `id`、`tenant_id`、`generated_by`（内部用户 id）与
`tax_policy_version_id` 一律不出这里；对账单与客户对外都只用 `public_id`。

⚠️ `snapshot` 原样是 `snapshot_json`（键集合固定，设计 §2「快照」），作为嵌套对象返回、不展开到
顶层；快照里本来就没有成本、汇率、加价与毛利（INV-7），这里也不往里加任何键。
`company_name` 与 `closing_balance` 取自快照（生成时冻结），不读租户当前的公司名。

⚠️ 路径参数 `statement_id` 只收小写规范 UUID 形状（8-4-4-4-12），形状在鉴权之后判，
不合规与查不到是同一个 404 `STATEMENT_NOT_FOUND`。查询参数 `period` 经
`app/services/statements.py` 的 `statement_period` 校验，不合规是 422 `VALIDATION_ERROR`。
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, Final

from pydantic import BaseModel

from app.core.errors import AppError
from app.models.statements import MonthlyStatement, StatementStatus
from app.services.statements import StatementPeriod, statement_period

_STATEMENT_ID: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class StatementNotFound(AppError):
    """No such statement (absent, or not a lowercase canonical uuid)."""

    def __init__(self) -> None:
        super().__init__("Statement not found.", code="STATEMENT_NOT_FOUND", http_status=404)


class StatementGenerationNotEligible(AppError):
    """The customer gets no statements: not `PREPAID`, or without a wallet."""

    def __init__(self) -> None:
        super().__init__(
            "The customer is not eligible for statements.",
            code="STATEMENT_NOT_ELIGIBLE",
            http_status=409,
        )


class InvalidStatementQuery(AppError):
    """`period` is not a `YYYY-MM` that `statement_period` accepts.

    与 FastAPI 的 422 同一个码、同一种文案：只列字段名，不回显值（app/core/errors.py）。
    """

    def __init__(self, field: str) -> None:
        super().__init__(
            f"Invalid request fields: query.{field}", code="VALIDATION_ERROR", http_status=422
        )


class StatementSummary(BaseModel):
    """One statement in the admin list. `statement_id` / `customer_id` are `public_id`s."""

    statement_id: str
    statement_number: str
    customer_id: str
    # 快照里的公司名（生成时冻结）。
    company_name: str
    # 马来西亚日历月，`YYYY-MM`。
    period: str
    # `DRAFT` / `FINALIZED` / `VOID`；V1 只有 `FINALIZED`。
    status: str
    # 带符号、恰好 8 位小数的字符串，来自快照。
    closing_balance: str
    finalized_at: dt.datetime | None


class StatementDetail(StatementSummary):
    """One statement with its cut-off, high-water mark, generator and the whole snapshot."""

    cutoff_at: dt.datetime
    ledger_high_sequence: int
    # 生成者管理员的 `users.email`；系统生成为 null。
    generated_by_email: str | None
    snapshot: dict[str, Any]


class GeneratedStatement(BaseModel):
    """One statement the manual catch-up generated just now."""

    statement_id: str
    period: str


def is_statement_id(value: str) -> bool:
    """A lowercase canonical uuid (36 characters, 8-4-4-4-12)."""
    return _STATEMENT_ID.fullmatch(value) is not None


def parse_period(value: str | None) -> StatementPeriod | None:
    """`None` passes through; anything `statement_period` refuses is a 422 on `period`."""
    if value is None:
        return None
    try:
        return statement_period(value)
    except ValueError:
        raise InvalidStatementQuery("period") from None


def _summary_fields(statement: MonthlyStatement, customer_id: str) -> dict[str, Any]:
    snapshot = statement.snapshot_json
    return {
        "statement_id": statement.public_id,
        "statement_number": statement.statement_number,
        "customer_id": customer_id,
        "company_name": snapshot["company_name"],
        "period": snapshot["period"],
        "status": StatementStatus(statement.status).value,
        "closing_balance": snapshot["closing_balance"],
        "finalized_at": statement.finalized_at,
    }


def statement_summary(statement: MonthlyStatement, *, customer_id: str) -> StatementSummary:
    return StatementSummary(**_summary_fields(statement, customer_id))


def statement_detail(
    statement: MonthlyStatement, *, customer_id: str, generated_by_email: str | None
) -> StatementDetail:
    return StatementDetail(
        **_summary_fields(statement, customer_id),
        cutoff_at=statement.cutoff_at,
        ledger_high_sequence=statement.ledger_high_sequence,
        generated_by_email=generated_by_email,
        snapshot=statement.snapshot_json,
    )


def generated_statement(statement: MonthlyStatement) -> GeneratedStatement:
    return GeneratedStatement(
        statement_id=statement.public_id, period=statement.snapshot_json["period"]
    )


__all__ = [
    "GeneratedStatement",
    "InvalidStatementQuery",
    "StatementDetail",
    "StatementGenerationNotEligible",
    "StatementNotFound",
    "StatementSummary",
    "generated_statement",
    "is_statement_id",
    "parse_period",
    "statement_detail",
    "statement_summary",
]
