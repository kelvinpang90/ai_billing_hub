"""Rebill pricing: effective state, applicability, recomputation, preview token (AIH-TASK-254).

设计闸门 #830 v12 §2「口径」「更正版本的适用性」「预览指纹」「金额与精度」；Invariant 6、8、10。
预览与确认（AIH-TASK-255）共用：

- `effective_state`：每个事件的当前有效计费 `eff(e)`、当前有效版本 `refs(e)` 与最近明细 id。
  `eff(e)` = 最近一条明细（明细 `id` 最大）的 `corrected_charge`，没有明细时是
  `billable_cost`；所以恒有 `eff(e) = billable_cost + Σ 该事件明细的 difference`；
- `check_corrections`：请求里的更正价格版本与更正规则对每个事件是否适用，任一不适用整个请求
  422（错误带第一个不适用事件的 `public_id`，不带金额）。**不看生效区间**：生效区间为空的
  `RETIRED` 版本（发布后在生效前就停用的）同样可用，这是有意的；`DRAFT` 与 `DISCARDED` 不可用；
- `corrected_charge`：新计费 `new(e)` —— `price_event(数量 = _quantities(e), 价格分量 = 更正
  价格版本, 汇率 = 快照汇率, 规则 = 更正规则)`，空的那一项取 `refs(e)`；两项都空就是「按当前
  有效版本重算」，对同一组引用与 `reprice_from_snapshot` 逐位相同；
- `preview_token` / `normalize_selection` / `canonical_json`：预览指纹与规范化的 `selection`
  （AIH-TASK-255 也用它们算 `request_fingerprint`）。

数量直接用 app/services/usage_billing.py 的 `_quantities`，计算直接用 app/services/pricing_engine.py
的 `price_event`、`covering`、`rule_terms`、`meter_components`：与计费同一套代码，在
`PRICING_CONTEXT` 里舍入一次（8 位小数，`ROUND_HALF_UP`）。差额与合计由服务按 8 位小数精确加减。

⚠️ 只读、不加锁、不提交；不写日志、不写审计。全程 `Decimal`，不出现浮点（静态检查用例扫描源码）。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models.fx_rates import FxRateVersion
from app.models.pricing_rules import PricingRule, PricingRuleStatus, PricingStrategy
from app.models.provider_prices import PriceVersionStatus, ProviderPriceVersion
from app.models.usage import UsageEvent
from app.repositories import pricing_rules as rules
from app.repositories import provider_prices as prices
from app.repositories import usage_rebills as rebills
from app.schemas.customers import money_text
from app.services.pricing_engine import (
    ChargeOverflow,
    ChargeResult,
    MeterComponent,
    covering,
    meter_components,
    price_event,
    rule_terms,
)
from app.services.usage_billing import _quantities

# 发布过（经审批）的两种状态：更正版本只能是它们（设计 §1 第 2 条）。
_USABLE_PRICE_STATUSES: Final = frozenset(
    {PriceVersionStatus.PUBLISHED, PriceVersionStatus.RETIRED}
)
_USABLE_RULE_STATUSES: Final = frozenset({PricingRuleStatus.PUBLISHED, PricingRuleStatus.RETIRED})

# 规范化的 `selection`（设计 v7）。
SELECTION_EVENT_IDS: Final = "usage_event_ids"
SELECTION_CRITERIA: Final = ("occurred_from", "occurred_to", "project_id", "provider", "model")
_REQUIRED_CRITERIA: Final = ("occurred_from", "occurred_to")


class RebillPriceNotApplicable(AppError):
    """The corrective provider price version does not apply to an event. Names the event only."""

    def __init__(self, usage_event_id: str) -> None:
        super().__init__(
            "The corrective provider price version does not apply to usage event "
            f"{usage_event_id}.",
            code="REBILL_PRICE_NOT_APPLICABLE",
            http_status=422,
        )
        self.usage_event_id = usage_event_id


class RebillRuleNotApplicable(AppError):
    """The corrective pricing rule does not apply to an event. Names the event only."""

    def __init__(self, usage_event_id: str) -> None:
        super().__init__(
            f"The corrective pricing rule does not apply to usage event {usage_event_id}.",
            code="REBILL_RULE_NOT_APPLICABLE",
            http_status=422,
        )
        self.usage_event_id = usage_event_id


class RebillAmountOutOfRange(AppError):
    """A recomputed amount does not fit DECIMAL(20,8) (`ChargeOverflow`). No amounts."""

    def __init__(self) -> None:
        super().__init__(
            "A calculated amount is out of range.",
            code=ChargeOverflow.code,
            http_status=422,
        )


# --- 当前有效状态 -----------------------------------------------------------------------


@dataclass(frozen=True)
class EffectiveState:
    """`eff(e)`, `refs(e)` and the latest line id of one event (设计 §2「口径」)."""

    eff: Decimal
    pricing_rule_id: int
    provider_price_version_id: int
    # 没有明细时为空；指纹里记作 0。
    latest_line_id: int | None


def effective_state(session: Session, events: Sequence[UsageEvent]) -> dict[int, EffectiveState]:
    """Event id → its effective state. One plain read of the latest lines; no locks.

    汇率版本不在这里：始终是事件快照的 `fx_rate_version_id`。
    """
    latest = rebills.latest_lines(session, [event.id for event in events])
    states: dict[int, EffectiveState] = {}
    for event in events:
        line = latest.get(event.id)
        if line is not None:
            states[event.id] = EffectiveState(
                eff=line.corrected_charge,
                pricing_rule_id=line.corrected_pricing_rule_id,
                provider_price_version_id=line.corrected_provider_price_version_id,
                latest_line_id=line.id,
            )
            continue
        if (
            event.billable_cost is None
            or event.pricing_rule_id is None
            or event.provider_price_version_id is None
        ):
            raise ValueError("only a priced usage event has an effective state")
        states[event.id] = EffectiveState(
            eff=event.billable_cost,
            pricing_rule_id=event.pricing_rule_id,
            provider_price_version_id=event.provider_price_version_id,
            latest_line_id=None,
        )
    return states


# --- 重算的输入 ------------------------------------------------------------------------


_Amounts = Mapping[str, tuple[Decimal, Decimal]]


class RepricingInputs:
    """What recomputation reads, read once per request (plain reads, no locks).

    计量类型的分量、已发布版本与规则的分量都不可变（025 / 026 / 027 的触发器），所以同一请求里
    记住它们不会读到过期值；一次最多 1000 个事件也不必逐个再查。
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._needed: dict[int, tuple[MeterComponent, ...]] = {}
        self._version_amounts: dict[int, _Amounts] = {}
        self._rule_amounts: dict[int, _Amounts] = {}
        self._fx_rates: dict[int, Decimal] = {}

    def needed(self, meter_type_id: int) -> tuple[MeterComponent, ...]:
        if meter_type_id not in self._needed:
            self._needed[meter_type_id] = meter_components(self._session, meter_type_id)
        return self._needed[meter_type_id]

    def version(self, version_id: int) -> ProviderPriceVersion:
        row = self._session.get(ProviderPriceVersion, version_id)
        if row is None:
            raise ValueError("a missing provider price version")
        return row

    def rule(self, rule_id: int) -> PricingRule:
        row = self._session.get(PricingRule, rule_id)
        if row is None:
            raise ValueError("a missing pricing rule")
        return row

    def version_amounts(self, version_id: int) -> _Amounts:
        if version_id not in self._version_amounts:
            priced = prices.components_for(self._session, [version_id])[version_id]
            self._version_amounts[version_id] = {
                item.component.component_code: (item.row.unit_quantity, item.row.rate_amount)
                for item in priced
            }
        return self._version_amounts[version_id]

    def rule_amounts(self, rule_id: int) -> _Amounts:
        if rule_id not in self._rule_amounts:
            stored = rules.components_for(self._session, [rule_id])[rule_id]
            self._rule_amounts[rule_id] = {
                item.component.component_code: (item.row.unit_quantity, item.row.rate_amount)
                for item in stored
            }
        return self._rule_amounts[rule_id]

    def fx_rate(self, fx_rate_version_id: int) -> Decimal:
        if fx_rate_version_id not in self._fx_rates:
            row = self._session.get(FxRateVersion, fx_rate_version_id)
            if row is None:
                raise ValueError("the snapshot references a missing FX rate version")
            self._fx_rates[fx_rate_version_id] = row.rate
        return self._fx_rates[fx_rate_version_id]


# --- 适用性 ----------------------------------------------------------------------------


def _price_applies(
    version: ProviderPriceVersion,
    amounts: _Amounts,
    event: UsageEvent,
    needed: Sequence[MeterComponent],
) -> bool:
    return (
        PriceVersionStatus(version.status) in _USABLE_PRICE_STATUSES
        and version.provider_id == event.provider_id
        and version.model_id == event.model_id
        # 汇率沿用快照：换币种就没有可用的汇率（设计 §1「我定的」第 4 条）。
        and version.source_currency == event.provider_source_currency
        and covering(amounts, needed) is not None
    )


def _rule_applies(
    rule: PricingRule, amounts: _Amounts, event: UsageEvent, needed: Sequence[MeterComponent]
) -> bool:
    if PricingRuleStatus(rule.status) not in _USABLE_RULE_STATUSES:
        return False
    # 每个范围列为空或等于事件的对应值：正是该规则的范围覆盖这个事件的条件（SCOPE_COLUMNS_CHECK
    # 保证八级各自的列取值）。别的客户的规则在这里被拒（INV-8）。
    scope = (
        (rule.tenant_id, event.tenant_id),
        (rule.project_id, event.project_id),
        (rule.provider_id, event.provider_id),
        (rule.model_id, event.model_id),
    )
    if any(column is not None and column != value for column, value in scope):
        return False
    terms = rule_terms(PricingStrategy(rule.strategy), rule.markup_multiplier, amounts, needed)
    return terms is not None


def check_corrections(
    session: Session,
    events: Sequence[UsageEvent],
    price_version: ProviderPriceVersion | None,
    rule: PricingRule | None,
    *,
    inputs: RepricingInputs | None = None,
) -> None:
    """Whether the corrective version and rule apply to every event (设计 §2「适用性」).

    按给定顺序逐个事件判断，每个事件先看价格版本再看规则；第一个不适用的事件让整个请求失败：
    `RebillPriceNotApplicable` / `RebillRuleNotApplicable`（422，消息带它的 `public_id`）。
    没给的那一项不判断（取 `refs(e)`，那是事件已经用过的版本）。不看生效区间。
    """
    if price_version is None and rule is None:
        return
    found = inputs or RepricingInputs(session)
    version_amounts = found.version_amounts(price_version.id) if price_version else None
    rule_amounts = found.rule_amounts(rule.id) if rule else None
    for event in events:
        needed = found.needed(event.usage_meter_type_id)
        if price_version is not None and version_amounts is not None:
            if not _price_applies(price_version, version_amounts, event, needed):
                raise RebillPriceNotApplicable(event.public_id)
        if rule is not None and rule_amounts is not None:
            if not _rule_applies(rule, rule_amounts, event, needed):
                raise RebillRuleNotApplicable(event.public_id)


# --- 重算 ------------------------------------------------------------------------------


def corrected_charge(
    session: Session,
    event: UsageEvent,
    state: EffectiveState,
    price_version: ProviderPriceVersion | None,
    rule: PricingRule | None,
    *,
    inputs: RepricingInputs | None = None,
) -> ChargeResult:
    """`new(e)` and its snapshot values: the corrective versions, or `refs(e)` where empty.

    汇率始终是事件快照的那一版（MYR 原币时没有）。更正版本须先经 `check_corrections`；版本不覆盖
    事件的分量是调用方的错误（`ValueError`）。结果超出 DECIMAL(20,8)：`RebillAmountOutOfRange`。
    """
    found = inputs or RepricingInputs(session)
    version_id = price_version.id if price_version is not None else state.provider_price_version_id
    rule_id = rule.id if rule is not None else state.pricing_rule_id
    version = found.version(version_id)
    rule_row = found.rule(rule_id)
    needed = found.needed(event.usage_meter_type_id)
    provider_components = covering(found.version_amounts(version_id), needed)
    terms = rule_terms(
        PricingStrategy(rule_row.strategy),
        rule_row.markup_multiplier,
        found.rule_amounts(rule_id),
        needed,
    )
    if provider_components is None or terms is None:
        raise ValueError("the versions do not price the event")
    fx_rate: Decimal | None = None
    if event.fx_rate_version_id is not None:
        fx_rate = found.fx_rate(event.fx_rate_version_id)
    try:
        return price_event(
            quantities=_quantities(event),
            source_currency=version.source_currency,
            provider_components=provider_components,
            fx_rate=fx_rate,
            rule=terms,
        )
    except ChargeOverflow:
        raise RebillAmountOutOfRange from None


# --- 预览指纹 ---------------------------------------------------------------------------


def canonical_json(value: object) -> str:
    """Keys sorted, no whitespace. Amounts must already be fixed-point strings."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _utc_text(value: object, name: str) -> str:
    """RFC 3339 (or a datetime) → UTC `YYYY-MM-DDTHH:MM:SS.ffffffZ`; naive means UTC already."""
    if isinstance(value, str):
        try:
            moment = dt.datetime.fromisoformat(value)
        except ValueError:
            raise ValueError(f"{name} is not an RFC 3339 time") from None
    elif isinstance(value, dt.datetime):
        moment = value
    else:
        raise ValueError(f"{name} is not a time")
    if moment.tzinfo is not None:
        moment = moment.astimezone(dt.UTC).replace(tzinfo=None)
    return f"{moment:%Y-%m-%dT%H:%M:%S.%f}Z"


def _optional_text(value: object, name: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{name} is not a string")
    return value


def normalize_selection(selection: Mapping[str, object]) -> dict[str, object]:
    """The canonical `selection` (设计 v7 §2「规范化的 selection」).

    显式事件：`{"usage_event_ids": [按字符串升序]}`。条件：五个键都在，时间换成 UTC 的
    `YYYY-MM-DDTHH:MM:SS.ffffffZ`，没给的可选项为 `None`。两种形状混用或有别的键：`ValueError`。
    已规范化的输入原样得到同一结果。
    """
    keys = set(selection)
    if SELECTION_EVENT_IDS in keys:
        if keys != {SELECTION_EVENT_IDS}:
            raise ValueError("a selection is either usage_event_ids or criteria")
        ids = selection[SELECTION_EVENT_IDS]
        if not isinstance(ids, list | tuple) or not all(isinstance(item, str) for item in ids):
            raise ValueError("usage_event_ids is a list of public ids")
        return {SELECTION_EVENT_IDS: sorted(ids)}
    unknown = keys - set(SELECTION_CRITERIA)
    if unknown:
        raise ValueError("unknown selection keys: " + ", ".join(sorted(unknown)))
    for name in _REQUIRED_CRITERIA:
        if selection.get(name) is None:
            raise ValueError(f"{name} is required")
    return {
        "occurred_from": _utc_text(selection["occurred_from"], "occurred_from"),
        "occurred_to": _utc_text(selection["occurred_to"], "occurred_to"),
        "project_id": _optional_text(selection.get("project_id"), "project_id"),
        "provider": _optional_text(selection.get("provider"), "provider"),
        "model": _optional_text(selection.get("model"), "model"),
    }


@dataclass(frozen=True)
class TokenEvent:
    """One event's part of the preview token. `event_id` (internal) only orders the list."""

    event_id: int
    public_id: str
    eff: Decimal
    new: Decimal
    latest_line_id: int | None


def preview_token(
    *,
    customer_id: str,
    corrective_pricing_rule_id: str | None,
    corrective_provider_price_version_id: str | None,
    selection: Mapping[str, object],
    events: Iterable[TokenEvent],
) -> str:
    """sha256 hex (64) of the canonical request and every selected event (设计 §2「预览指纹」).

    输入：客户 `public_id`、两个更正版本的 `public_id`（空为 `null`）、规范化后的 `selection`、
    按事件 `id` 升序的 `[事件 public_id, eff(e), new(e), 最近明细 id 或 0]`（包括差额为 0 的事件）；
    金额是 8 位小数的定点字符串。不是秘密，只用来比较。
    """
    ordered = sorted(events, key=lambda item: item.event_id)
    return _sha256(
        {
            "customer_id": customer_id,
            "corrective_pricing_rule_id": corrective_pricing_rule_id,
            "corrective_provider_price_version_id": corrective_provider_price_version_id,
            "selection": normalize_selection(selection),
            "events": [
                [
                    item.public_id,
                    money_text(item.eff),
                    money_text(item.new),
                    item.latest_line_id or 0,
                ]
                for item in ordered
            ],
        }
    )


__all__ = [
    "SELECTION_CRITERIA",
    "SELECTION_EVENT_IDS",
    "EffectiveState",
    "RebillAmountOutOfRange",
    "RebillPriceNotApplicable",
    "RebillRuleNotApplicable",
    "RepricingInputs",
    "TokenEvent",
    "canonical_json",
    "check_corrections",
    "corrected_charge",
    "effective_state",
    "normalize_selection",
    "preview_token",
]
