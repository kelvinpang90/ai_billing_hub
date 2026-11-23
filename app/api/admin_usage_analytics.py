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

xlsx（AIH-TASK-237，设计闸门 #753 v1 §2「参数 format」「各接口的改法」）：
查询参数 `format` 在鉴权与其他参数校验之后判（缺省 / `csv` / `xlsx`，
其他 422 `query.format`）；只在同一个 `try` 里「拼文件 → 构造响应」处分支，
查询、上限、租约与日志对两种格式是同一段。

对账后成本（AIH-TASK-248，设计闸门 #756 v2 §2「规则」「推论」「接口与导出」）：
同一个只读会话里再取 `reconciled_allocation_inputs`，对每个分组与合计的每块按
`covered × R ÷ E`（AIH-TASK-244 的 `allocate`，返回 None 按 0）在 `PRICING_CONTEXT`
内累加、`quantize_money` 只舍入一次；合计用 `total_covered` 单独算，不由分组行相加。
只在查询时算，不写任何表、不写审计。
"""

from __future__ import annotations

import datetime as dt
import itertools
import logging
from collections.abc import Callable
from decimal import Decimal, localcontext
from typing import Annotated, Any, Final, NamedTuple

from fastapi import APIRouter, Query, Request

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
from app.core.xlsx_export import XLSX_MEDIA_TYPE, write_xlsx, xlsx_response
from app.models.base import quantize_money
from app.models.tenancy import BillingMode
from app.repositories import usage_analytics as analytics_repository
from app.repositories.usage_analytics import (
    AllocationInputs,
    CoveringRecord,
    PaidPaymentTotals,
    UsageAnalyticsRow,
    UsageAnalyticsTotals,
)
from app.schemas.customers import DEFAULT_PAGE_SIZE, MAX_PAGE, MAX_PAGE_SIZE, money_text
from app.schemas.envelope import ApiResponse, success
from app.schemas.usage_analytics import (
    USAGE_ANALYTICS_EXPORT_COLUMNS,
    USAGE_ANALYTICS_EXPORT_INTEGER_COLUMNS,
    USAGE_ANALYTICS_EXPORT_TEXT_COLUMNS,
    ProviderCostBasis,
    UsageAnalyticsGroupBy,
    UsageAnalyticsInternalMeteredOnly,
    UsageAnalyticsItem,
    UsageAnalyticsPayments,
    UsageAnalyticsPrepaid,
    UsageAnalyticsReconciliationCoverage,
    UsageAnalyticsTotalsView,
    UsageAnalyticsView,
    parse_analytics_query,
    ratio_text,
    usage_analytics_export_group_cells,
    usage_analytics_export_total_cells,
)
from app.schemas.usage_events import InvalidUsageEventQuery
from app.services.pricing_engine import PRICING_CONTEXT
from app.services.provider_cost_reconciliation import allocate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

# OpenAPI 的 200 列出两种媒体类型；响应类仍是 `ExportCsvResponse`。
_EXPORT_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {"content": {"text/csv": {}, XLSX_MEDIA_TYPE: {}}}
}
# xlsx 的工作表名：程序常量，不拼任何输入。
_EXPORT_SHEET = "usage-analytics"


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
    # 一个只读会话、不提交：MySQL REPEATABLE READ 下全部 SELECT（含分摊输入）看同一个快照。
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
        inputs = analytics_repository.reconciled_allocation_inputs(
            session, group_by=group_by, **filters
        )
    items = [_item(row, inputs) for row in rows]
    return success(
        UsageAnalyticsView(
            items=items,
            page=page,
            page_size=page_size,
            total=total,
            totals=_totals_view(totals, fees, inputs, occurred_from=start, occurred_to=end),
        ),
        request_id=current_request_id(),
    )


@router.get(
    "/usage-analytics/export", response_class=ExportCsvResponse, responses=_EXPORT_RESPONSES
)
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
    # 等同 `= Query(default=None, alias="format")`；Annotated 写法避开 B008。
    export_format: Annotated[str | None, Query(alias="format")] = None,
) -> ExportCsvResponse:
    """Every group as a GROUP row plus one TOTAL row, as CSV or xlsx; at most 20 000 groups.

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
    # 在鉴权与其他参数之后判（设计闸门 #753 v1 §2「参数 format」）：
    # 非法值不拿租约、不查询。
    if export_format not in (None, "csv", "xlsx"):
        raise InvalidUsageEventQuery("format")
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
            inputs = analytics_repository.reconciled_allocation_inputs(
                session, group_by=group_by, **filters
            )
        # 不给部分文件；TOTAL 行不计入上限。
        if len(rows) > EXPORT_MAX_ROWS:
            raise ExportTooLarge
        # 会话已关闭：不持连接拼文件。
        row_count = len(rows)
        groups = (
            usage_analytics_export_group_cells(
                _item(row, inputs), group_by=group_by, occurred_from=start, occurred_to=end
            )
            for row in rows
        )
        total_row = usage_analytics_export_total_cells(
            _totals_view(totals, fees, inputs, occurred_from=start, occurred_to=end),
            group_by=group_by,
            occurred_from=start,
            occurred_to=end,
        )
        if export_format == "xlsx":
            body = write_xlsx(
                USAGE_ANALYTICS_EXPORT_COLUMNS,
                itertools.chain(groups, (total_row,)),
                USAGE_ANALYTICS_EXPORT_TEXT_COLUMNS,
                USAGE_ANALYTICS_EXPORT_INTEGER_COLUMNS,
                sheet_title=_EXPORT_SHEET,
            )
            rows = None
            response = xlsx_response(_export_filename(group_by, start, end, "xlsx"), body)
        else:
            body = write_csv(
                USAGE_ANALYTICS_EXPORT_COLUMNS,
                itertools.chain(groups, (total_row,)),
                USAGE_ANALYTICS_EXPORT_TEXT_COLUMNS,
            )
            # 拼好就丢掉结果列表：之后进程里只留响应体这一份。
            rows = None
            response = csv_response(_export_filename(group_by, start, end, "csv"), body)
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


def _export_filename(
    group_by: UsageAnalyticsGroupBy, start: dt.datetime, end: dt.datetime, extension: str
) -> str:
    """`usage-analytics_<group_by>_<from>_<to>.<extension>` in Malaysian time; no client input."""

    def local(moment: dt.datetime) -> str:
        return (moment + KUALA_LUMPUR_OFFSET).strftime("%Y%m%dT%H%M%S") + "MYT"

    return f"usage-analytics_{group_by}_{local(start)}_{local(end)}.{extension}"


def _ratio(numerator: Decimal, denominator: Decimal | int) -> str | None:
    """`Decimal` 除法后 `ratio_text`；分母为 0 时 null。"""
    if denominator == 0:
        return None
    return ratio_text(Decimal(numerator) / Decimal(denominator))


_PREPAID: Final = BillingMode.PREPAID.value
_INTERNAL: Final = BillingMode.INTERNAL_METERED_ONLY.value

# (记录 public_id, 模式) → 这一个分组或合计里该记录覆盖的估算；缺的键按 0。
Covered = Callable[[str, str], Decimal]


class _ProviderCost(NamedTuple):
    covered: Decimal
    reconciled: Decimal
    provider_cost: Decimal
    basis: ProviderCostBasis


def _provider_cost(
    estimated: Decimal, mode: str, records: tuple[CoveringRecord, ...], covered: Covered
) -> _ProviderCost:
    """One block of one group (or of `totals`), 设计闸门 #756 v2 §2「推论」.

    `reconciled = quantize_money(Σ_r allocate(covered(r), R, E))`：
    在 `PRICING_CONTEXT` 内累加、只舍入一次；`allocate` 返回 None（E = 0）按 0。
    供应商成本 = 分摊额 + 未覆盖部分的估算。
    """
    with localcontext(PRICING_CONTEXT):
        covered_total = Decimal(0)
        allocated = Decimal(0)
        for record in records:
            part = covered(record.public_id, mode)
            covered_total += part
            share = allocate(part, record.reconciled_cost_myr, record.scope_estimated_myr)
            if share is not None:
                allocated += share
        reconciled = quantize_money(allocated)
        provider_cost = reconciled + (estimated - covered_total)
    basis: ProviderCostBasis
    if covered_total == 0:
        basis = "estimated"
    elif covered_total == estimated:
        basis = "reconciled"
    else:
        basis = "mixed"
    return _ProviderCost(covered_total, reconciled, provider_cost, basis)


def _item(row: UsageAnalyticsRow, inputs: AllocationInputs) -> UsageAnalyticsItem:
    def covered(record_id: str, mode: str) -> Decimal:
        return inputs.group_covered.get((row.group_key, record_id, mode), Decimal(0))

    return UsageAnalyticsItem(
        group_key=row.group_key,
        group_name=row.group_name,
        **_aggregates(
            row,
            _provider_cost(row.prepaid_estimated_cost, _PREPAID, inputs.records, covered),
            _provider_cost(row.internal_estimated_cost, _INTERNAL, inputs.records, covered),
        ),
    )


def _aggregates(
    row: UsageAnalyticsRow | UsageAnalyticsTotals,
    prepaid_cost: _ProviderCost,
    internal_cost: _ProviderCost,
) -> dict[str, Any]:
    """The ten keys a group item and `totals` share, computed the same way from a repository row.

    平均值按这一行的合计重新算；用量毛利 = PREPAID 计费额 − PREPAID 估算成本；
    基于供应商成本的毛利 = PREPAID 计费额 − PREPAID 供应商成本。
    """
    margin = row.prepaid_billable_cost - row.prepaid_estimated_cost
    margin_on_provider_cost = row.prepaid_billable_cost - prepaid_cost.provider_cost
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
            covered_estimated_cost_myr=money_text(prepaid_cost.covered),
            reconciled_provider_cost_myr=money_text(prepaid_cost.reconciled),
            provider_cost_myr=money_text(prepaid_cost.provider_cost),
            provider_cost_basis=prepaid_cost.basis,
            gross_margin_on_provider_cost=money_text(margin_on_provider_cost),
            gross_margin_on_provider_cost_rate=_ratio(
                margin_on_provider_cost, row.prepaid_billable_cost
            ),
        ),
        "internal_metered_only": UsageAnalyticsInternalMeteredOnly(
            event_count=row.internal_event_count,
            estimated_provider_cost_myr=money_text(row.internal_estimated_cost),
            reference_customer_price=money_text(row.internal_reference_price),
            covered_estimated_cost_myr=money_text(internal_cost.covered),
            reconciled_provider_cost_myr=money_text(internal_cost.reconciled),
            provider_cost_myr=money_text(internal_cost.provider_cost),
            provider_cost_basis=internal_cost.basis,
        ),
    }


def _allocatable(record: CoveringRecord) -> bool:
    """E = 0 而 R > 0 的记录无法分摊；E = 0 且 R = 0 的没有东西要分，算可分摊。"""
    return record.scope_estimated_myr > 0 or record.reconciled_cost_myr == 0


def _totals_view(
    totals: UsageAnalyticsTotals,
    fees: PaidPaymentTotals | None,
    inputs: AllocationInputs,
    *,
    occurred_from: dt.datetime,
    occurred_to: dt.datetime,
) -> UsageAnalyticsTotalsView:
    """毛利（扣手续费后）只在合计层：用量毛利 − 已知手续费；手续费块为 null 时两个毛利都为 null。

    两块的供应商成本用 `total_covered` 单独算（每块只舍入一次），不由分组行相加；
    基于供应商成本的毛利（扣手续费后）同一规则。
    不可分摊记录的 R 只披露，不进任何成本与毛利。
    """

    def covered(record_id: str, mode: str) -> Decimal:
        return inputs.total_covered.get((record_id, mode), Decimal(0))

    prepaid_cost = _provider_cost(totals.prepaid_estimated_cost, _PREPAID, inputs.records, covered)
    internal_cost = _provider_cost(
        totals.internal_estimated_cost, _INTERNAL, inputs.records, covered
    )
    payments: UsageAnalyticsPayments | None = None
    gross_profit: str | None = None
    gross_profit_rate: str | None = None
    profit_on_provider_cost: str | None = None
    profit_on_provider_cost_rate: str | None = None
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
        on_provider_cost = (
            totals.prepaid_billable_cost - prepaid_cost.provider_cost - fees.gateway_fee
        )
        profit_on_provider_cost = money_text(on_provider_cost)
        profit_on_provider_cost_rate = _ratio(on_provider_cost, totals.prepaid_billable_cost)
    coverage = [
        UsageAnalyticsReconciliationCoverage(
            reconciliation_id=record.public_id,
            provider_code=record.provider_code,
            billing_month=record.billing_month,
            period_start=record.period_start,
            period_end=record.period_end,
            fully_in_range=occurred_from <= record.period_start
            and record.period_end <= occurred_to,
            allocatable=_allocatable(record),
        )
        for record in inputs.records
    ]
    unallocated = [record for record in inputs.records if not _allocatable(record)]
    return UsageAnalyticsTotalsView(
        **_aggregates(totals, prepaid_cost, internal_cost),
        payments=payments,
        gross_profit=gross_profit,
        gross_profit_rate=gross_profit_rate,
        gross_profit_on_provider_cost=profit_on_provider_cost,
        gross_profit_on_provider_cost_rate=profit_on_provider_cost_rate,
        reconciliation_coverage=coverage,
        unallocated_reconciled_months=[
            f"{record.provider_code}:{record.billing_month}" for record in unallocated
        ],
        unallocated_reconciled_cost_myr=(
            money_text(sum((record.reconciled_cost_myr for record in unallocated), Decimal(0)))
            if unallocated
            else None
        ),
    )
