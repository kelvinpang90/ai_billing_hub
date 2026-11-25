"""Usage rebill: preview and confirm (design gate #830 v12 §2「服务」, AIH-TASK-255).

spec §19「Rebill / Reprocess」、§66 审计动作 `REBILL`；Invariant 2、4、5、11、12、13。管理员对一个
客户的一组已计费预付事件按更正版本重算：先预览（旧计费、新计费、差额与预览指纹），再确认（原因与
幂等键），合计差额以一行 `REBILL_CREDIT` / `REBILL_DEBIT` 入账，逐事件明细写进
`usage_rebill_lines`。

口径（`eff(e)`、`refs(e)`、`new(e)`、`d(e)`、原账期、原对账单）、适用性与预览指纹都在
app/services/rebill_pricing.py 与 app/repositories/usage_rebills.py，预览与确认共用同一个
`_compute`：选事件 → 当前有效状态 → 校验更正版本 → 重算 → 指纹，只有差额不为 0 的事件进明细，
合计与 `event_count` 只按它们算（设计 v9）。

⚠️ **预览**：一个只读事务，不加锁、不写库、不写审计。没有可入账的差额时照常返回，
`ledger_type` / `ledger_amount` 为 `None`（设计 v7）。

⚠️ **确认是两个事务，先后进行**（设计「读视图与锁的约定」）：
0. 校验事务（只读，结束后才开确认事务）：客户存在、是预付，取内部 id；
1. 确认事务：MySQL 上本会话第一句设 `READ COMMITTED`（写法照 app/services/password_reset.py
   的 `_start_reset`），第一条 SQL 是按客户内部 id 锁钱包的 `SELECT … FOR UPDATE`；之后本模块与
   仓储发出的读**全部是普通读**，没有 `FOR SHARE`（`post_transaction` 内部对钱包与租户的
   `FOR UPDATE` 是既有行为）。同一客户的 Rebill 由钱包锁串行，锁后的每次读都看得到之前已提交的
   Rebill 与明细；时间也在锁后取、截到整秒；
2. 锁后读租户复核 `billing_mode`；
3. 按幂等键查重——**先于**第 4 步的指纹比对：同键重发时状态早已被第一次改变；
4. 锁内重新选事件、重算、算指纹，与 `preview_token` 比对；
5. `post_transaction` 写一行账本（停复机、低余额事件在它里面）；
6. 写 `usage_rebills` 与明细；
7. 审计 `REBILL`；
8. 提交。撞 `idempotency_key` 唯一约束时回滚、开新事务按第 3 步重查一次；其他 `IntegrityError`
   照常抛出。锁等待超时与死锁（`OperationalError`）不重试，同键重发即可。

⚠️ 等式「difference = corrected − previous」数据库不建 CHECK（设计 v9），只在这里算一次并写入。
⚠️ 不写日志；错误消息不带金额（只有事件 `public_id` 与匹配数）。全程 `Decimal`。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import session_scope
from app.core.errors import AppError
from app.models.ai_catalog import AiModel, AiProvider
from app.models.auth import AuditAction, User
from app.models.pricing_rules import PricingRule
from app.models.provider_prices import ProviderPriceVersion
from app.models.statements import MonthlyStatement
from app.models.tenancy import BillingMode, Project, Tenant
from app.models.usage import UsageEvent, UsageEventStatus
from app.models.usage_rebills import MAX_EVENT_COUNT, REASON_LENGTH, UsageRebill, UsageRebillLine
from app.models.wallet import ReferenceType, TransactionType, Wallet
from app.repositories import pricing_rules as rule_rows
from app.repositories import provider_prices as price_rows
from app.repositories import tenancy
from app.repositories import usage_rebills as rebills
from app.repositories.wallet import (
    InvalidAmount,
    TenantClosed,
    fits_money,
    get_wallet_for_tenant,
    post_transaction,
)
from app.schemas.customers import money_text
from app.services.auth import RequestContext, record_audit
from app.services.customers import CustomerNotFound
from app.services.integration_access import ProjectNotFound
from app.services.pricing_rules import PricingRuleNotFound
from app.services.project_integration import AccountClosed
from app.services.provider_prices import PriceVersionNotFound
from app.services.rebill_pricing import (
    SELECTION_EVENT_IDS,
    RebillAmountOutOfRange,
    RepricingInputs,
    TokenEvent,
    canonical_json,
    check_corrections,
    corrected_charge,
    effective_state,
    normalize_selection,
    preview_token,
)
from app.services.usage_requeue import UsageEventNotFound

# 审计的 entity_type；entity_id 是 Rebill 的 public_id。
ENTITY_USAGE_REBILL: Final = "usage_rebill"

# `post_transaction` 的 `InvalidAmount` 问题码里，映射为 422 `AMOUNT_OUT_OF_RANGE` 的两个。
_OUT_OF_RANGE_PROBLEMS: Final = frozenset({"BALANCE_OUT_OF_RANGE", "AMOUNT_OUT_OF_RANGE"})

# 规范化之后的条件时间（rebill_pricing.normalize_selection 的输出形式）。
_UTC_TEXT_FORMAT: Final = "%Y-%m-%dT%H:%M:%S.%fZ"
_PREVIEW_TOKEN: Final = re.compile(r"[0-9a-f]{64}")
_IDEMPOTENCY_KEY: Final = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
# `idempotency_key` 唯一冲突的识别：MySQL 的消息带约束名，SQLite 的带「表.列」。
_IDEMPOTENCY_CONFLICT_MARKERS: Final = (
    "uq_usage_rebills_idempotency_key",
    "usage_rebills.idempotency_key",
)

# 取当前时间（不带时区的 UTC）。可注入，测试据此冻结时钟。
type Clock = Callable[[], dt.datetime]


# --- 错误（设计「管理端接口」的错误码汇总；消息不带金额） -----------------------------------


class RebillRequestInvalid(AppError):
    """A request field only the service can judge. Names the fields, never the values."""

    def __init__(self, *fields: str) -> None:
        names = ", ".join(sorted(set(fields)))
        super().__init__(
            f"Invalid request fields: {names}", code="VALIDATION_ERROR", http_status=422
        )


class RebillNotPrepaid(AppError):
    def __init__(self) -> None:
        super().__init__(
            "Only a prepaid customer can be rebilled.", code="REBILL_NOT_PREPAID", http_status=409
        )


class UsageEventNotRebillable(AppError):
    """Another customer's, not PROCESSED, or not prepaid. Names the event only."""

    def __init__(self, usage_event_id: str) -> None:
        super().__init__(
            f"Usage event {usage_event_id} cannot be rebilled.",
            code="USAGE_EVENT_NOT_REBILLABLE",
            http_status=409,
        )
        self.usage_event_id = usage_event_id


class RebillSelectionTooLarge(AppError):
    """More matching events than one rebill takes; the count is in the message (设计 §2)."""

    def __init__(self, match_count: int) -> None:
        super().__init__(
            f"Selection matches {match_count} usage events; at most {MAX_EVENT_COUNT}.",
            code="REBILL_SELECTION_TOO_LARGE",
            http_status=422,
        )
        self.match_count = match_count


class NothingToRebill(AppError):
    def __init__(self) -> None:
        super().__init__(
            "No usage event in the selection changes its charge.",
            code="NOTHING_TO_REBILL",
            http_status=409,
        )


class RebillNetZero(AppError):
    def __init__(self) -> None:
        super().__init__(
            "The corrections cancel out; there is nothing to post.",
            code="REBILL_NET_ZERO",
            http_status=409,
        )


class RebillPreviewStale(AppError):
    def __init__(self) -> None:
        super().__init__(
            "The data changed since the preview; preview again.",
            code="REBILL_PREVIEW_STALE",
            http_status=409,
        )


class RebillConflict(AppError):
    def __init__(self) -> None:
        super().__init__(
            "This idempotency key was already used for a different rebill.",
            code="REBILL_CONFLICT",
            http_status=409,
        )


# --- 请求与结果 ------------------------------------------------------------------------------


@dataclass(frozen=True)
class RebillRequest:
    """What preview and confirm share. References are public ids; `selection` as the API sends."""

    customer_id: str
    selection: Mapping[str, object]
    corrective_pricing_rule_id: str | None = None
    corrective_provider_price_version_id: str | None = None


@dataclass(frozen=True)
class RebillLineView:
    """One line of a preview or of a rebill. References are public ids; provider / model codes."""

    usage_event_id: str
    occurred_at: dt.datetime
    # 项目的 public_id；事件没有项目时为空。
    project: str | None
    provider: str
    model: str
    previous_charge: Decimal
    corrected_charge: Decimal
    difference: Decimal
    previous_pricing_rule: str
    corrected_pricing_rule: str
    previous_provider_price_version: str
    corrected_provider_price_version: str
    corrected_estimated_provider_cost_myr: Decimal
    original_period: str
    original_statement_id: str | None


@dataclass(frozen=True)
class RebillPreview:
    customer_id: str
    # 只含差额不为 0 的事件，按事件 `id` 升序。
    lines: tuple[RebillLineView, ...]
    unchanged_count: int
    # 下面四个只按差额不为 0 的事件算，与 `usage_rebills` 的同名列一致（设计 v9）。
    event_count: int
    previous_total: Decimal
    corrected_total: Decimal
    difference: Decimal
    # 没有可入账的差额（明细为空或合计为 0）时都为 `None`（设计 v7）。
    ledger_type: TransactionType | None
    ledger_amount: Decimal | None
    balance: Decimal
    # 当前余额 − 合计差额，只作提示。
    balance_after: Decimal
    preview_token: str


@dataclass(frozen=True)
class RebillResult:
    """The rebill detail of the admin API (设计「管理端接口」), plus `replayed`."""

    id: str
    customer_id: str
    event_count: int
    previous_total: Decimal
    corrected_total: Decimal
    difference: Decimal
    ledger_type: TransactionType
    ledger_amount: Decimal
    reason: str
    created_by_email: str
    created_at: dt.datetime
    corrective_pricing_rule_id: str | None
    corrective_provider_price_version_id: str | None
    wallet_transaction_id: str
    # 明细 `id` 升序。
    lines: tuple[RebillLineView, ...]
    replayed: bool


# --- 共用的计算 ------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Line:
    """One event whose charge changes: what its line row and its view need (internal ids)."""

    event: UsageEvent
    previous_charge: Decimal
    corrected_charge: Decimal
    difference: Decimal
    previous_pricing_rule_id: int
    previous_provider_price_version_id: int
    corrected_pricing_rule_id: int
    corrected_provider_price_version_id: int
    corrected_estimated_provider_cost_myr: Decimal
    original_period: str
    original_statement_id: int | None


@dataclass(frozen=True)
class _Computation:
    selection: dict[str, object]
    rule: PricingRule | None
    price_version: ProviderPriceVersion | None
    lines: tuple[_Line, ...]
    unchanged_count: int
    previous_total: Decimal
    corrected_total: Decimal
    difference: Decimal
    token: str

    @property
    def ledger_type(self) -> TransactionType | None:
        # 账本金额 = −D：D < 0（多扣了）补退，D > 0（少扣了）补扣。
        if self.difference < 0:
            return TransactionType.REBILL_CREDIT
        if self.difference > 0:
            return TransactionType.REBILL_DEBIT
        return None


def _normalized(selection: Mapping[str, object]) -> dict[str, object]:
    """The canonical selection, or 422 `VALIDATION_ERROR` (设计「选事件」「规范化的 selection」)."""
    try:
        normalized = normalize_selection(selection)
    except ValueError:
        raise RebillRequestInvalid("selection") from None
    if SELECTION_EVENT_IDS in normalized:
        ids = normalized[SELECTION_EVENT_IDS]
        assert isinstance(ids, list)
        # 1 到 1000 个不重复的事件 public_id。
        if not 1 <= len(ids) <= MAX_EVENT_COUNT or len(set(ids)) != len(ids):
            raise RebillRequestInvalid(f"selection.{SELECTION_EVENT_IDS}")
        return normalized
    if _utc(normalized["occurred_from"]) >= _utc(normalized["occurred_to"]):
        raise RebillRequestInvalid("selection.occurred_from", "selection.occurred_to")
    return normalized


def _utc(value: object) -> dt.datetime:
    assert isinstance(value, str)
    return dt.datetime.strptime(value, _UTC_TEXT_FORMAT)


def _correctives(
    session: Session, request: RebillRequest
) -> tuple[PricingRule | None, ProviderPriceVersion | None]:
    rule: PricingRule | None = None
    if request.corrective_pricing_rule_id is not None:
        rule = rule_rows.get_rule(session, request.corrective_pricing_rule_id)
        if rule is None:
            raise PricingRuleNotFound
    version: ProviderPriceVersion | None = None
    if request.corrective_provider_price_version_id is not None:
        version = price_rows.get_version(session, request.corrective_provider_price_version_id)
        if version is None:
            raise PriceVersionNotFound
    return rule, version


def _rebillable(event: UsageEvent) -> bool:
    return (
        event.status == UsageEventStatus.PROCESSED
        and event.billing_mode_snapshot == BillingMode.PREPAID.value
    )


def _explicit_events(session: Session, tenant_id: int, public_ids: list[str]) -> list[UsageEvent]:
    found = rebills.find_explicit_events(session, tenant_id, public_ids)
    if found.missing:
        raise UsageEventNotFound
    own = {event.public_id for event in found.events}
    foreign = [item for item in public_ids if item not in own]
    if foreign:
        # 别的客户的事件：409，不是 404（INV-8：存在与否不靠这个区分，它对管理员可见）。
        raise UsageEventNotRebillable(foreign[0])
    for event in found.events:
        if not _rebillable(event):
            raise UsageEventNotRebillable(event.public_id)
    return found.events


def _criteria_events(
    session: Session, tenant_id: int, selection: Mapping[str, object]
) -> list[UsageEvent]:
    project_id: int | None = None
    project_public_id = selection["project_id"]
    if project_public_id is not None:
        assert isinstance(project_public_id, str)
        project = tenancy.get_project_for_tenant(session, tenant_id, project_public_id)
        if project is None:
            raise ProjectNotFound
        project_id = project.id
    provider = selection["provider"]
    model = selection["model"]
    found = rebills.select_events(
        session,
        tenant_id,
        occurred_from=_utc(selection["occurred_from"]),
        occurred_to=_utc(selection["occurred_to"]),
        project_id=project_id,
        provider_code=provider if isinstance(provider, str) else None,
        model_code=model if isinstance(model, str) else None,
    )
    if found.match_count > MAX_EVENT_COUNT:
        raise RebillSelectionTooLarge(found.match_count)
    if found.match_count == 0:
        raise NothingToRebill
    return found.events


def _select(session: Session, tenant_id: int, selection: Mapping[str, object]) -> list[UsageEvent]:
    """The selected events, `id` ascending."""
    if SELECTION_EVENT_IDS in selection:
        ids = selection[SELECTION_EVENT_IDS]
        assert isinstance(ids, list)
        return _explicit_events(session, tenant_id, ids)
    return _criteria_events(session, tenant_id, selection)


def _check_range(*amounts: Decimal) -> None:
    # 合计与账本金额（−D，绝对值同 D）都要放得进 DECIMAL(20,8)；写库之前判。
    if not all(fits_money(amount) for amount in amounts):
        raise RebillAmountOutOfRange


def _compute(
    session: Session, tenant: Tenant, request: RebillRequest, selection: dict[str, object]
) -> _Computation:
    """Select, take the effective state, check the correctives, reprice, fingerprint.

    全部是普通读（仓储与 rebill_pricing 都不加锁）。任一不适用的事件让整个请求失败。
    """
    rule, version = _correctives(session, request)
    events = _select(session, tenant.id, selection)
    inputs = RepricingInputs(session)
    states = effective_state(session, events)
    check_corrections(session, events, version, rule, inputs=inputs)

    token_events: list[TokenEvent] = []
    changed: list[tuple[UsageEvent, Decimal, Decimal, int, int, int, int, Decimal]] = []
    for event in events:
        state = states[event.id]
        charge = corrected_charge(session, event, state, version, rule, inputs=inputs)
        new = charge.billable_cost
        token_events.append(
            TokenEvent(event.id, event.public_id, state.eff, new, state.latest_line_id)
        )
        if new == state.eff:
            continue
        changed.append(
            (
                event,
                state.eff,
                new,
                state.pricing_rule_id,
                state.provider_price_version_id,
                rule.id if rule is not None else state.pricing_rule_id,
                version.id if version is not None else state.provider_price_version_id,
                charge.estimated_provider_cost_myr,
            )
        )

    changed_events = [item[0] for item in changed]
    periods = rebills.original_periods(session, changed_events)
    statements = rebills.statement_ids(session, tenant.id, periods.values())
    lines = tuple(
        _Line(
            event=event,
            previous_charge=previous,
            corrected_charge=new,
            # 等式只在这里算一次（数据库不建等式 CHECK，设计 v9）。
            difference=new - previous,
            previous_pricing_rule_id=previous_rule,
            previous_provider_price_version_id=previous_version,
            corrected_pricing_rule_id=corrected_rule,
            corrected_provider_price_version_id=corrected_version,
            corrected_estimated_provider_cost_myr=estimated,
            original_period=periods[event.id],
            original_statement_id=statements.get(periods[event.id]),
        )
        for (
            event,
            previous,
            new,
            previous_rule,
            previous_version,
            corrected_rule,
            corrected_version,
            estimated,
        ) in changed
    )
    previous_total = sum((line.previous_charge for line in lines), Decimal(0))
    corrected_total = sum((line.corrected_charge for line in lines), Decimal(0))
    difference = corrected_total - previous_total
    _check_range(previous_total, corrected_total, difference)

    token = preview_token(
        customer_id=tenant.public_id,
        corrective_pricing_rule_id=rule.public_id if rule is not None else None,
        corrective_provider_price_version_id=version.public_id if version is not None else None,
        selection=selection,
        events=token_events,
    )
    return _Computation(
        selection=selection,
        rule=rule,
        price_version=version,
        lines=lines,
        unchanged_count=len(events) - len(lines),
        previous_total=previous_total,
        corrected_total=corrected_total,
        difference=difference,
        token=token,
    )


def _public_ids(session: Session, model: Any, ids: Iterable[int | None]) -> dict[int, str]:
    wanted = sorted({item for item in ids if item is not None})
    if not wanted:
        return {}
    rows = session.execute(select(model.id, model.public_id).where(model.id.in_(wanted))).all()
    return {int(row_id): public_id for row_id, public_id in rows}


def _codes(session: Session, model: Any, ids: Iterable[int | None]) -> dict[int, str]:
    wanted = sorted({item for item in ids if item is not None})
    if not wanted:
        return {}
    rows = session.execute(select(model.id, model.code).where(model.id.in_(wanted))).all()
    return {int(row_id): code for row_id, code in rows}


def _line_views(session: Session, lines: Sequence[_Line]) -> tuple[RebillLineView, ...]:
    """Preview lines with references as public ids / catalog codes."""
    rule_ids = _public_ids(
        session,
        PricingRule,
        [line.previous_pricing_rule_id for line in lines]
        + [line.corrected_pricing_rule_id for line in lines],
    )
    version_ids = _public_ids(
        session,
        ProviderPriceVersion,
        [line.previous_provider_price_version_id for line in lines]
        + [line.corrected_provider_price_version_id for line in lines],
    )
    projects = _public_ids(session, Project, [line.event.project_id for line in lines])
    statements = _public_ids(
        session, MonthlyStatement, [line.original_statement_id for line in lines]
    )
    providers = _codes(session, AiProvider, [line.event.provider_id for line in lines])
    models = _codes(session, AiModel, [line.event.model_id for line in lines])
    views: list[RebillLineView] = []
    for line in lines:
        event = line.event
        assert event.provider_id is not None and event.model_id is not None
        views.append(
            RebillLineView(
                usage_event_id=event.public_id,
                occurred_at=event.occurred_at,
                project=projects.get(event.project_id),
                provider=providers[event.provider_id],
                model=models[event.model_id],
                previous_charge=line.previous_charge,
                corrected_charge=line.corrected_charge,
                difference=line.difference,
                previous_pricing_rule=rule_ids[line.previous_pricing_rule_id],
                corrected_pricing_rule=rule_ids[line.corrected_pricing_rule_id],
                previous_provider_price_version=version_ids[
                    line.previous_provider_price_version_id
                ],
                corrected_provider_price_version=version_ids[
                    line.corrected_provider_price_version_id
                ],
                corrected_estimated_provider_cost_myr=line.corrected_estimated_provider_cost_myr,
                original_period=line.original_period,
                original_statement_id=(
                    statements[line.original_statement_id]
                    if line.original_statement_id is not None
                    else None
                ),
            )
        )
    return tuple(views)


def _prepaid_tenant(session: Session, customer_id: str) -> Tenant:
    tenant = tenancy.get_tenant_by_public_id(session, customer_id)
    if tenant is None:
        raise CustomerNotFound
    if tenant.billing_mode is not BillingMode.PREPAID:
        raise RebillNotPrepaid
    return tenant


# --- 预览 ------------------------------------------------------------------------------------


def preview_rebill(
    session_factory: sessionmaker[Session], *, request: RebillRequest
) -> RebillPreview:
    """One read-only transaction, no locks, no writes, no audit (设计「服务」).

    明细为空或合计差额为 0 时照常返回，方向与金额为 `None`；条件匹配为 0 时 409
    `NOTHING_TO_REBILL`（没有可展示的事件）。
    """
    selection = _normalized(request.selection)
    with session_factory() as session:
        tenant = _prepaid_tenant(session, request.customer_id)
        computed = _compute(session, tenant, request, selection)
        wallet = get_wallet_for_tenant(session, tenant.id)
        balance = wallet.balance if wallet is not None else Decimal(0)
        kind = computed.ledger_type
        preview = RebillPreview(
            customer_id=tenant.public_id,
            lines=_line_views(session, computed.lines),
            unchanged_count=computed.unchanged_count,
            event_count=len(computed.lines),
            previous_total=computed.previous_total,
            corrected_total=computed.corrected_total,
            difference=computed.difference,
            ledger_type=kind,
            ledger_amount=-computed.difference if kind is not None else None,
            balance=balance,
            balance_after=balance - computed.difference,
            preview_token=computed.token,
        )
        # 只读：不提交，关闭会话时回滚。
        session.rollback()
    return preview


# --- 确认 ------------------------------------------------------------------------------------


def request_fingerprint(
    request: RebillRequest, selection: Mapping[str, object], preview_token: str, reason: str
) -> str:
    """sha256 hex of the canonical request (设计「规范化的 selection」).

    输入：客户 public_id、两个更正版本的 public_id、规范化的 selection、`preview_token`、去首尾空白
    后的原因。
    """
    canonical = canonical_json(
        {
            "customer_id": request.customer_id,
            "corrective_pricing_rule_id": request.corrective_pricing_rule_id,
            "corrective_provider_price_version_id": request.corrective_provider_price_version_id,
            "selection": dict(selection),
            "preview_token": preview_token,
            "reason": reason,
        }
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def confirm_rebill(
    session_factory: sessionmaker[Session],
    *,
    actor: User,
    request: RebillRequest,
    preview_token: str,
    reason: str,
    idempotency_key: str,
    context: RequestContext,
    clock: Clock,
) -> RebillResult:
    """Post the previewed rebill once, or return the one posted under this key (`replayed`).

    第 0 步的校验事务结束后才开确认事务；见模块说明的第 0 至 8 步。
    """
    trimmed = reason.strip() if isinstance(reason, str) else ""
    bad: list[str] = []
    if not trimmed or len(trimmed) > REASON_LENGTH:
        bad.append("reason")
    if not isinstance(preview_token, str) or _PREVIEW_TOKEN.fullmatch(preview_token) is None:
        bad.append("preview_token")
    if not isinstance(idempotency_key, str) or _IDEMPOTENCY_KEY.fullmatch(idempotency_key) is None:
        bad.append("idempotency_key")
    if bad:
        raise RebillRequestInvalid(*bad)
    selection = _normalized(request.selection)
    fingerprint = request_fingerprint(request, selection, preview_token, trimmed)

    # 第 0 步：单独的只读事务。
    with session_factory() as session:
        tenant_id = _prepaid_tenant(session, request.customer_id).id
        session.rollback()

    try:
        return _confirm_once(
            session_factory,
            actor=actor,
            request=request,
            selection=selection,
            tenant_id=tenant_id,
            preview_token=preview_token,
            reason=trimmed,
            idempotency_key=idempotency_key,
            fingerprint=fingerprint,
            context=context,
            clock=clock,
        )
    except IntegrityError as error:
        if not _is_idempotency_conflict(error):
            raise
        # 第 8 步：另一个事务先提交了同一个键。确认事务已整个回滚；开新事务按第 3 步重查一次。
        with session_factory() as session:
            existing = rebills.get_by_idempotency_key(session, idempotency_key)
            if existing is None:
                raise
            result = _judge_existing(session, existing, fingerprint)
            session.rollback()
        return result


def _is_idempotency_conflict(error: IntegrityError) -> bool:
    message = str(error.orig)
    return any(marker in message for marker in _IDEMPOTENCY_CONFLICT_MARKERS)


def _judge_existing(session: Session, existing: UsageRebill, fingerprint: str) -> RebillResult:
    """第 3 步：同一个请求 → 原结果（`replayed`）；不同 → 409 `REBILL_CONFLICT`。"""
    if existing.request_fingerprint != fingerprint:
        raise RebillConflict
    return _result(session, existing.public_id, replayed=True)


def _lock_wallet(session: Session, tenant_id: int) -> Wallet | None:
    # 与 `post_transaction` 内部锁的是同一行，同一事务再锁不阻塞。`populate_existing`：
    # 锁到的是最新已提交的值，不是会话里的旧对象。
    statement = (
        select(Wallet)
        .where(Wallet.tenant_id == tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return session.execute(statement).scalar_one_or_none()


def _confirm_once(
    session_factory: sessionmaker[Session],
    *,
    actor: User,
    request: RebillRequest,
    selection: dict[str, object],
    tenant_id: int,
    preview_token: str,
    reason: str,
    idempotency_key: str,
    fingerprint: str,
    context: RequestContext,
    clock: Clock,
) -> RebillResult:
    with session_scope(session_factory) as session:
        # 第 1 步。⚠️ 必须是本会话的第一句：`execution_options` 只在为本事务取连接时生效一次。
        # 只在 mysql 方言上设（SQLite 方言不接受，也没有行锁与快照问题）；会话结束归还连接时
        # 隔离级别还原为引擎默认值，不泄漏到连接池。
        if session.get_bind().dialect.name == "mysql":
            session.connection(execution_options={"isolation_level": "READ COMMITTED"})
        # 第一条 SQL：锁钱包。同一客户的所有 Rebill 由此串行。
        if _lock_wallet(session, tenant_id) is None:
            # 没有钱包的客户不是预付客户（内部计量客户不建钱包）。
            raise RebillNotPrepaid
        # 锁之后取时间、截到整秒：Rebill 行的 created_at 不早于它看到的最新状态。
        now = clock().replace(microsecond=0)

        # 第 2 步：锁后的普通读。
        tenant = session.get(Tenant, tenant_id, populate_existing=True)
        if tenant is None or tenant.billing_mode is not BillingMode.PREPAID:
            raise RebillNotPrepaid

        # 第 3 步：幂等判定先于指纹比对。
        existing = rebills.get_by_idempotency_key(session, idempotency_key)
        if existing is not None:
            return _judge_existing(session, existing, fingerprint)

        # 第 4 步。
        computed = _compute(session, tenant, request, selection)
        if computed.token != preview_token:
            raise RebillPreviewStale
        if not computed.lines:
            raise NothingToRebill
        kind = computed.ledger_type
        if kind is None:
            raise RebillNetZero
        count = len(computed.lines)

        # 第 5 步。
        public_id = str(uuid.uuid4())
        try:
            posted = post_transaction(
                session,
                tenant_id=tenant_id,
                transaction_type=kind,
                amount=-computed.difference,
                reference_type=ReferenceType.REBILL,
                reference_id=public_id,
                now=now,
                created_by=actor.id,
                description=f"Rebill correction ({count} usage events)",
                # 只有 Rebill id 与事件数：没有任何成本键（INV-7）。
                metadata={"rebill": public_id, "events": count},
                actor_role=actor.role.value,
                ip_address=context.ip_address,
                user_agent=context.user_agent,
            )
        except TenantClosed:
            raise AccountClosed from None
        except InvalidAmount as error:
            if str(error) in _OUT_OF_RANGE_PROBLEMS:
                raise RebillAmountOutOfRange from None
            raise
        transaction = posted.transaction

        # 第 6 步。
        rebill = UsageRebill(
            public_id=public_id,
            tenant_id=tenant_id,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            corrective_pricing_rule_id=computed.rule.id if computed.rule is not None else None,
            corrective_provider_price_version_id=(
                computed.price_version.id if computed.price_version is not None else None
            ),
            event_count=count,
            previous_total=computed.previous_total,
            corrected_total=computed.corrected_total,
            difference=computed.difference,
            wallet_transaction_id=transaction.id,
            reason=reason,
            created_by=actor.id,
            created_at=now,
        )
        session.add(rebill)
        session.flush()
        _write_lines(session, rebill, computed.lines, now)

        # 第 7 步：金额是客户计费额（定点字符串），没有估算成本。
        record_audit(
            session,
            action=AuditAction.REBILL,
            context=context,
            now=now,
            actor=actor,
            entity_type=ENTITY_USAGE_REBILL,
            entity_id=public_id,
            before_state={
                "previous_total": money_text(computed.previous_total),
                "event_count": count,
            },
            after_state={
                "corrected_total": money_text(computed.corrected_total),
                "difference": money_text(computed.difference),
                "wallet_transaction": transaction.public_id,
                "corrective_pricing_rule": (
                    computed.rule.public_id if computed.rule is not None else None
                ),
                "corrective_provider_price_version": (
                    computed.price_version.public_id if computed.price_version is not None else None
                ),
                "selection": computed.selection,
            },
            reason=reason,
        )
        session.flush()
        result = _result(session, public_id, replayed=False)
    # 第 8 步：`session_scope` 在这里提交。
    return result


def _write_lines(
    session: Session, rebill: UsageRebill, lines: Sequence[_Line], now: dt.datetime
) -> None:
    for line in lines:
        session.add(
            UsageRebillLine(
                rebill_id=rebill.id,
                usage_event_id=line.event.id,
                original_wallet_transaction_id=line.event.wallet_transaction_id,
                previous_charge=line.previous_charge,
                corrected_charge=line.corrected_charge,
                difference=line.difference,
                previous_pricing_rule_id=line.previous_pricing_rule_id,
                previous_provider_price_version_id=line.previous_provider_price_version_id,
                corrected_pricing_rule_id=line.corrected_pricing_rule_id,
                corrected_provider_price_version_id=line.corrected_provider_price_version_id,
                corrected_estimated_provider_cost_myr=line.corrected_estimated_provider_cost_myr,
                original_period=line.original_period,
                original_statement_id=line.original_statement_id,
                created_at=now,
            )
        )
    session.flush()


def _result(session: Session, public_id: str, *, replayed: bool) -> RebillResult:
    detail = rebills.get_rebill(session, public_id)
    if detail is None:
        raise ValueError("the rebill was just found or written")
    summary = detail.summary
    rebill = summary.rebill
    return RebillResult(
        id=rebill.public_id,
        customer_id=summary.customer_id,
        event_count=rebill.event_count,
        previous_total=rebill.previous_total,
        corrected_total=rebill.corrected_total,
        difference=rebill.difference,
        ledger_type=summary.ledger_type,
        ledger_amount=summary.ledger_amount,
        reason=rebill.reason,
        created_by_email=summary.created_by_email,
        created_at=rebill.created_at,
        corrective_pricing_rule_id=detail.corrective_pricing_rule_id,
        corrective_provider_price_version_id=detail.corrective_provider_price_version_id,
        wallet_transaction_id=summary.wallet_transaction_id,
        lines=tuple(
            RebillLineView(
                usage_event_id=item.usage_event_id,
                occurred_at=item.occurred_at,
                project=item.project_id,
                provider=item.provider,
                model=item.model,
                previous_charge=item.line.previous_charge,
                corrected_charge=item.line.corrected_charge,
                difference=item.line.difference,
                previous_pricing_rule=item.previous_pricing_rule_id,
                corrected_pricing_rule=item.corrected_pricing_rule_id,
                previous_provider_price_version=item.previous_provider_price_version_id,
                corrected_provider_price_version=item.corrected_provider_price_version_id,
                corrected_estimated_provider_cost_myr=(
                    item.line.corrected_estimated_provider_cost_myr
                ),
                original_period=item.line.original_period,
                original_statement_id=item.original_statement_id,
            )
            for item in detail.lines
        ),
        replayed=replayed,
    )


__all__ = [
    "ENTITY_USAGE_REBILL",
    "NothingToRebill",
    "RebillConflict",
    "RebillLineView",
    "RebillNetZero",
    "RebillNotPrepaid",
    "RebillPreview",
    "RebillPreviewStale",
    "RebillRequest",
    "RebillRequestInvalid",
    "RebillResult",
    "RebillSelectionTooLarge",
    "UsageEventNotRebillable",
    "confirm_rebill",
    "preview_rebill",
    "request_fingerprint",
]
