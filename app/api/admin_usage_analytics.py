"""Read-only administrator usage analytics endpoint.

AIH-TASK-222（设计闸门 #709 v2 §2「接口一」「V1 口径」「事务边界」）：
`group_by` 多 `day` / `month`；`totals` 是覆盖全部分组的同口径合计，
另带按 `paid_at` 归期的手续费块与扣手续费后的毛利。
分组一页、分组总数、合计与手续费块在同一个只读会话里查、不提交。金额全程 `Decimal`。

导出（AIH-TASK-223，设计闸门 #709 v2 §2「接口二」）：参数与接口一相同、没有分页，每个分组一行
GROUP、末尾恒有一行 TOTAL。检查顺序照客户钱包流水导出（app/api/customer.py）：鉴权 → 校验 →
数据库未配置的 503（早于租约）→ 导出租约（与客户导出共用，拿不到 503 `EXPORT_BUSY`）→ 只 hold、
不在这里释放。查询在一个只读会话里，CSV 在会话关闭之后拼。`AppError` 原样抛，其他异常记
`admin_usage_analytics_export_failed` 后在 `except` 块之外抛 `CustomerExportFailed`（通用 500 的
信封）。成功记一条 INFO，不写审计、不落盘；文件含内部成本，只给管理端。
"""

from __future__ import annotations

import datetime as dt
import itertools
import logging
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Request

from app.api.auth import require_admin, require_session_factory
from app.core.csv_export import (
    EXPORT_LEASE_SCOPE_KEY,
    EXPORT_MAX_ROWS,
    CustomerExportFailed,
    ExportBusy,
    ExportCsvResponse,
    ExportTooLarge,
    csv_response,
    try_acquire_export_lease,
    write_csv,
)
from app.core.errors import AppError
from app.core.fx_source import KUALA_LUMPUR_OFFSET
from app.core.logging import current_request_id
from app.repositories import usage_analytics as analytics_repository
from app.repositories.usage_analytics import (
    PaidPaymentTotals,
    UsageAnalyticsRow,
    UsageAnalyticsTotals,
)
from app.schemas.customers import DEFAULT_PAGE_SIZE, MAX_PAGE, MAX_PAGE_SIZE, money_text
from app.schemas.envelope import ApiResponse, success
from app.schemas.usage_analytics import (
    USAGE_ANALYTICS_EXPORT_COLUMNS,
    USAGE_ANALYTICS_EXPORT_TEXT_COLUMNS,
    UsageAnalyticsGroupBy,
    UsageAnalyticsInternalMeteredOnly,
    UsageAnalyticsItem,
    UsageAnalyticsPayments,
    UsageAnalyticsPrepaid,
    UsageAnalyticsTotalsView,
    UsageAnalyticsView,
    parse_analytics_query,
    ratio_text,
    usage_analytics_export_group_cells,
    usage_analytics_export_total_cells,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


@router.get("/usage-analytics", response_model=ApiResponse[UsageAnalyticsView])
def get_usage_analytics(
    request: Request,
    group_by: str | None = None,
    occurred_from: str | None = None,
    occurred_to: str | None = None,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    customer_id: str | None = None,
    project_id: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    feature: str | None = None,
) -> ApiResponse[UsageAnalyticsView]:
    require_admin(request)
    # 参数出现即算（含空串）：手续费不能归属到项目、供应商、模型或功能（设计 §2「接口一」）。
    fees_attributable = all(value is None for value in (project_id, provider, model, feature))
    group_by, start, end, feature = parse_analytics_query(
        group_by=group_by,
        occurred_from=occurred_from,
        occurred_to=occurred_to,
        feature=feature,
        page=page,
        page_size=page_size,
        max_page=MAX_PAGE,
        max_page_size=MAX_PAGE_SIZE,
    )
    filters: dict[str, Any] = {
        "customer_id": customer_id,
        "project_id": project_id,
        "provider": provider,
        "model": model,
        "feature": feature,
        "occurred_from": start,
        "occurred_to": end,
    }
    # 一个只读会话、不提交：MySQL REPEATABLE READ 下四条 SELECT 看同一个快照。
    with require_session_factory(request)() as session:
        rows, total = analytics_repository.list_usage_analytics(
            session,
            group_by=group_by,
            offset=(page - 1) * page_size,
            limit=page_size,
            **filters,
        )
        totals = analytics_repository.usage_analytics_totals(session, **filters)
        fees = (
            analytics_repository.paid_payment_totals(
                session, customer_id=customer_id, paid_from=start, paid_to=end
            )
            if fees_attributable
            else None
        )
    items = [
        UsageAnalyticsItem(group_key=row.group_key, group_name=row.group_name, **_aggregates(row))
        for row in rows
    ]
    return success(
        UsageAnalyticsView(
            items=items,
            page=page,
            page_size=page_size,
            total=total,
            totals=_totals_view(totals, fees),
        ),
        request_id=current_request_id(),
    )


@router.get("/usage-analytics/export", response_class=ExportCsvResponse)
def export_usage_analytics(
    request: Request,
    group_by: str | None = None,
    occurred_from: str | None = None,
    occurred_to: str | None = None,
    customer_id: str | None = None,
    project_id: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    feature: str | None = None,
) -> ExportCsvResponse:
    """Every group as a GROUP row plus one TOTAL row, as a CSV attachment; at most 20 000 groups.

    含 Acuven 内部成本与毛利：只给管理端，不得转发给客户（INV-7）。
    """
    admin = require_admin(request)
    fees_attributable = all(value is None for value in (project_id, provider, model, feature))
    # 同一个校验函数；没有分页参数，分页那两项恒通过。
    group_by, start, end, feature = parse_analytics_query(
        group_by=group_by,
        occurred_from=occurred_from,
        occurred_to=occurred_to,
        feature=feature,
        page=1,
        page_size=1,
        max_page=1,
        max_page_size=1,
    )
    # 数据库没配置时在这里 503，早于获取租约。
    factory = require_session_factory(request)
    token = try_acquire_export_lease()
    if token is None:
        raise ExportBusy
    # 只 hold，不在这里释放：最外层中间件在整个响应发完后释放。
    request.scope[EXPORT_LEASE_SCOPE_KEY].hold(token)
    filters: dict[str, Any] = {
        "customer_id": customer_id,
        "project_id": project_id,
        "provider": provider,
        "model": model,
        "feature": feature,
        "occurred_from": start,
        "occurred_to": end,
    }
    rows = None
    body = None
    response = None
    failed = False
    try:
        # 一个只读会话、不提交：分组、合计与手续费块看同一个快照。
        with factory() as session:
            # 多取一行，只用来判「超了」。
            rows, _total = analytics_repository.list_usage_analytics(
                session, group_by=group_by, offset=0, limit=EXPORT_MAX_ROWS + 1, **filters
            )
            totals = analytics_repository.usage_analytics_totals(session, **filters)
            fees = (
                analytics_repository.paid_payment_totals(
                    session, customer_id=customer_id, paid_from=start, paid_to=end
                )
                if fees_attributable
                else None
            )
        # 不给部分文件；TOTAL 行不计入上限。
        if len(rows) > EXPORT_MAX_ROWS:
            raise ExportTooLarge
        # 会话已关闭：不持连接拼文件。
        row_count = len(rows)
        groups = (
            usage_analytics_export_group_cells(
                UsageAnalyticsItem(
                    group_key=row.group_key, group_name=row.group_name, **_aggregates(row)
                ),
                group_by=group_by,
                occurred_from=start,
                occurred_to=end,
            )
            for row in rows
        )
        total_row = usage_analytics_export_total_cells(
            _totals_view(totals, fees), group_by=group_by, occurred_from=start, occurred_to=end
        )
        body = write_csv(
            USAGE_ANALYTICS_EXPORT_COLUMNS,
            itertools.chain(groups, (total_row,)),
            USAGE_ANALYTICS_EXPORT_TEXT_COLUMNS,
        )
        # 拼好就丢掉结果列表：之后进程里只留响应体这一份。
        rows = None
        response = csv_response(_export_filename(group_by, start, end), body)
        body = None
    except AppError:
        raise
    except Exception:
        # 与 handle_unexpected_error 同一口径：栈进日志，不带参数值与文件内容。
        logger.exception("admin_usage_analytics_export_failed", extra={"path": request.url.path})
        failed = True
    if failed:
        # 在 except 块之外抛：新异常没有 `__context__`，原异常与它引用的数据此时已可回收。
        rows = None
        body = None
        response = None
        raise CustomerExportFailed
    # 只记管理员内部 id、分组维度与 GROUP 行数：不记筛选值、公司名、金额与文件内容。
    logger.info(
        "admin_usage_analytics_exported",
        extra={"user_id": admin.id, "group_by": group_by, "row_count": row_count},
    )
    return response


def _export_filename(group_by: UsageAnalyticsGroupBy, start: dt.datetime, end: dt.datetime) -> str:
    """`usage-analytics_<group_by>_<from>_<to>.csv` in Malaysian time; no client input in it."""

    def local(moment: dt.datetime) -> str:
        return (moment + KUALA_LUMPUR_OFFSET).strftime("%Y%m%dT%H%M%S") + "MYT"

    return f"usage-analytics_{group_by}_{local(start)}_{local(end)}.csv"


def _ratio(numerator: Decimal, denominator: Decimal | int) -> str | None:
    """`Decimal` 除法后 `ratio_text`；分母为 0 时 null。"""
    if denominator == 0:
        return None
    return ratio_text(Decimal(numerator) / Decimal(denominator))


def _aggregates(row: UsageAnalyticsRow | UsageAnalyticsTotals) -> dict[str, Any]:
    """The ten keys a group item and `totals` share, computed the same way from a repository row.

    平均值按这一行的合计重新算；用量毛利 = PREPAID 计费额 − PREPAID 估算成本。
    """
    margin = row.prepaid_billable_cost - row.prepaid_estimated_cost
    return {
        "event_count": row.event_count,
        "token_event_count": row.token_event_count,
        "input_tokens": row.input_tokens,
        "output_tokens": row.output_tokens,
        "cache_creation_input_tokens": row.cache_creation_input_tokens,
        "cache_read_input_tokens": row.cache_read_input_tokens,
        "average_input_tokens": _ratio(Decimal(row.input_tokens), row.token_event_count),
        "average_output_tokens": _ratio(Decimal(row.output_tokens), row.token_event_count),
        "prepaid": UsageAnalyticsPrepaid(
            event_count=row.prepaid_event_count,
            billable_cost=money_text(row.prepaid_billable_cost),
            estimated_provider_cost_myr=money_text(row.prepaid_estimated_cost),
            gross_margin=money_text(margin),
            gross_margin_rate=_ratio(margin, row.prepaid_billable_cost),
        ),
        "internal_metered_only": UsageAnalyticsInternalMeteredOnly(
            event_count=row.internal_event_count,
            estimated_provider_cost_myr=money_text(row.internal_estimated_cost),
            reference_customer_price=money_text(row.internal_reference_price),
        ),
    }


def _totals_view(
    totals: UsageAnalyticsTotals, fees: PaidPaymentTotals | None
) -> UsageAnalyticsTotalsView:
    """毛利（扣手续费后）只在合计层：用量毛利 − 已知手续费；手续费块为 null 时两个毛利都为 null。"""
    payments: UsageAnalyticsPayments | None = None
    gross_profit: str | None = None
    gross_profit_rate: str | None = None
    if fees is not None:
        payments = UsageAnalyticsPayments(
            paid_count=fees.paid_count,
            topup_amount=money_text(fees.topup_amount),
            gateway_fee=money_text(fees.gateway_fee),
            gateway_fee_unknown_count=fees.gateway_fee_unknown_count,
        )
        profit = totals.prepaid_billable_cost - totals.prepaid_estimated_cost - fees.gateway_fee
        gross_profit = money_text(profit)
        gross_profit_rate = _ratio(profit, totals.prepaid_billable_cost)
    return UsageAnalyticsTotalsView(
        **_aggregates(totals),
        payments=payments,
        gross_profit=gross_profit,
        gross_profit_rate=gross_profit_rate,
    )
