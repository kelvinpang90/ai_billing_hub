"""The pricing engine: what an event costs and what the customer is billed (design gate #179 v1).

spec §14、§15、§15.1、§16、§17、§17.1、§59、§80；ADR-0005（同一事件重算得出相同
MYR）、ADR-0008（含税）。实现登记为 AIH-TASK-031。四个入口：

- `price_event`：**纯函数**，不碰数据库。输入事件的数量、价格版本的分量、
  汇率（MYR 原币时为空）与规则，输出 §14 的快照值、计费额与逐分量明细；
- `resolve_charge_inputs`：在**调用方的事务里**按 025 / 026 / 027 / 028 的契约
  依次取模型、价格版本、汇率与规则，返回齐全的输入，或第一个遇到的错误状态与细分码；
- `reprice_from_snapshot`：取快照引用的版本与规则重新调用 `price_event`（只读）；
- `preview_charge`：管理端试算（只读事务，`locking=False`），与计费同一套代码。

⚠️ **精度（§80、INV-10）**：一切计算都在 `PRICING_CONTEXT`（50 位有效数字、
`ROUND_HALF_UP`、陷阱全关）里，用 `localcontext` 包住，不依赖进程或线程的默认上下文。
中间值不舍入；**每个存储值都直接由未舍入的中间值舍入一次**，舍入只经
`quantize_money`。MYR 估算成本与计费额都从未舍入的值算起，所以「存储的估算成本
× 倍数」与「存储的计费额」可以差最后一位 —— 这是 §80 要求的结果。本模块不出现
任何浮点转换（静态检查用例扫描源码）。

⚠️ **结果超出 DECIMAL(20,8)** 抛 `ChargeOverflow`（T-H 把事件标 `FAILED_FINAL`、
`AMOUNT_OUT_OF_RANGE`）。计费额可以是 0：T-H 照样保存快照，但**不写账本**
（设计 §2「对下游任务的契约」）。

⚠️ 不写库、不写审计、不写日志；异常只带错误码，不带金额（设计 §6）。
"""

from __future__ import annotations

import datetime as dt
import enum
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Context, Decimal, localcontext
from typing import Final

from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import AppError
from app.models.ai_catalog import (
    QUANTITY_FIELD,
    TOKEN_FIELDS,
    PayloadShape,
    QuantityKind,
    UsageMeterType,
)
from app.models.base import MONEY_PRECISION, MONEY_SCALE, quantize_money
from app.models.fx_rates import QUOTE_CURRENCY, FxRateVersion
from app.models.pricing_rules import PricingRule, PricingScope, PricingStrategy
from app.models.provider_prices import ProviderPriceVersion
from app.repositories import ai_catalog as catalog
from app.repositories import fx_rates as rates
from app.repositories import pricing_rules as rules
from app.repositories import provider_prices as prices
from app.repositories import tenancy
from app.repositories import usage_events as usage
from app.repositories.fx_rates import ResolvedFxRate
from app.repositories.pricing_rules import ResolvedPricingRule
from app.repositories.provider_prices import ResolvedProviderPrice
from app.schemas.customers import money_text
from app.schemas.pricing_preview import (
    PreviewComponent,
    PreviewFxRate,
    PreviewModel,
    PreviewPriceVersion,
    PreviewPricingRule,
    PricingPreviewRequest,
    PricingPreviewView,
)
from app.services.ai_catalog import MeterTypeNotFound
from app.services.customers import CustomerNotFound

# 设计 §2「算法」：50 位有效数字远超 DECIMAL(20,8) 与 DECIMAL(24,10) 的量级，
# 除法的截断误差在第 40 位以后，不影响任何存储值。`traps=[]`：一概不抛。
PRICING_CONTEXT: Final = Context(prec=50, rounding=ROUND_HALF_UP, traps=[], flags=[])

# DECIMAL(20,8) 放得下的上界（不含）：整数部分最多 12 位。
_MONEY_LIMIT: Final = Decimal(10) ** (MONEY_PRECISION - MONEY_SCALE)


class ChargeStatus(enum.StrEnum):
    """结果状态。`PRICED` = 输入齐全（`resolve_charge_inputs`）/ 已算出（试算）。"""

    PRICED = "PRICED"
    MODEL_UNKNOWN = "MODEL_UNKNOWN"
    PRICING_ERROR = "PRICING_ERROR"
    FX_RATE_ERROR = "FX_RATE_ERROR"


class PricingErrorCode(enum.StrEnum):
    """`PRICING_ERROR` 的细分码（设计 §2），T-H 原样写进事件。"""

    NO_PROVIDER_PRICE = "NO_PROVIDER_PRICE"
    MISSING_PROVIDER_COMPONENT = "MISSING_PROVIDER_COMPONENT"
    NO_PRICING_RULE = "NO_PRICING_RULE"
    MISSING_RULE_COMPONENT = "MISSING_RULE_COMPONENT"


class MatchedVia(enum.StrEnum):
    CODE = "code"
    ALIAS = "alias"


class ChargeOverflow(ArithmeticError):
    """A stored amount does not fit DECIMAL(20,8). Carries only the error code, never amounts."""

    code = "AMOUNT_OUT_OF_RANGE"

    def __init__(self) -> None:
        super().__init__(self.code)


# --- 纯函数 -----------------------------------------------------------------------


@dataclass(frozen=True)
class UnitPrice:
    """One component's price: `rate_amount` per `unit_quantity` units of `quantity_field`."""

    component_code: str
    quantity_field: str
    unit_quantity: Decimal
    rate_amount: Decimal


@dataclass(frozen=True)
class RuleTerms:
    """What `price_event` needs of a rule: a multiplier (MARKUP) or MYR prices (FIXED_RATE)."""

    strategy: PricingStrategy
    markup_multiplier: Decimal | None
    components: tuple[UnitPrice, ...]


@dataclass(frozen=True)
class ComponentCharge:
    """One component of the event, unrounded. Not stored by T-H (design §2): recomputable."""

    component_code: str
    quantity: Decimal
    # pcᵢ = qᵢ × price.rate_amountᵢ / price.unit_quantityᵢ，原币。
    provider_cost_unrounded: Decimal
    # bᵢ = qᵢ × rule.rate_amountᵢ / rule.unit_quantityᵢ，MYR 含税；MARKUP 为空。
    customer_price_unrounded: Decimal | None


@dataclass(frozen=True)
class ChargeResult:
    """The §14 snapshot values, the billed amount and the per-component detail.

    `provider_source_cost`、`estimated_provider_cost_myr`、`billable_cost` 是存储值
    （8 位小数，各自由未舍入值舍入一次）；`fx_rate_applied` 是汇率原值，MYR 原币时为空。
    """

    components: tuple[ComponentCharge, ...]
    provider_source_currency: str
    fx_rate_applied: Decimal | None
    provider_source_cost_unrounded: Decimal
    provider_source_cost: Decimal
    estimated_provider_cost_myr_unrounded: Decimal
    estimated_provider_cost_myr: Decimal
    billable_cost_unrounded: Decimal
    billable_cost: Decimal


def _quantity(quantities: Mapping[str, int | Decimal], field: str) -> Decimal:
    if field not in quantities:
        raise ValueError(f"the event has no quantity for {field}")
    value = quantities[field]
    if isinstance(value, bool) or not isinstance(value, int | Decimal):
        raise ValueError(f"the quantity for {field} is not an integer or a Decimal")
    # 整数转 Decimal 是精确的，与上下文无关。
    quantity = Decimal(value)
    if not quantity.is_finite() or quantity < 0:
        raise ValueError(f"the quantity for {field} is not a finite non-negative number")
    return quantity


def _stored(value: Decimal) -> Decimal:
    """The one rounding of a stored amount (§80): `quantize_money`, inside DECIMAL(20,8).

    先判未舍入值（太大的值在 50 位上下文里连量化都做不了），再判舍入后的值
    （.5 进位可能恰好进到上界）。
    """
    if not value.is_finite() or value >= _MONEY_LIMIT:
        raise ChargeOverflow
    rounded = quantize_money(value)
    if rounded >= _MONEY_LIMIT:
        raise ChargeOverflow
    return rounded


def price_event(
    *,
    quantities: Mapping[str, int | Decimal],
    source_currency: str,
    provider_components: Sequence[UnitPrice],
    fx_rate: Decimal | None,
    rule: RuleTerms,
) -> ChargeResult:
    """Price one event (design §2「算法」). Pure: the same inputs give bit-identical results.

    - `quantities`：按 025 的 `quantity_field` 取数（四个 token 字段或 `quantity`）；
    - `provider_components`：该计量类型在 025 里的**全部**分量的供应商价（原币）；
    - `fx_rate`：1 单位原币 = 多少 MYR；**`source_currency` 为 MYR 时必须为空**
      （不查汇率，`EC = PC`），否则必须给；
    - `rule`：MARKUP 带倍数；FIXED_RATE 带上述全部分量的 MYR 含税价。

    输入不完整是调用方的错误（`ValueError`）：缺分量要在 `resolve_charge_inputs`
    里判成 `PRICING_ERROR`，绝不按 0 算（§15.1）。结果超出 DECIMAL(20,8)：
    `ChargeOverflow`。
    """
    if (source_currency == QUOTE_CURRENCY) != (fx_rate is None):
        raise ValueError("an FX rate is applied exactly when the source currency is not MYR")
    if not provider_components:
        raise ValueError("an event has at least one priced component")
    if rule.strategy is PricingStrategy.MARKUP and rule.markup_multiplier is None:
        raise ValueError("a MARKUP rule needs its multiplier")
    customer_prices = {item.component_code: item for item in rule.components}

    with localcontext(PRICING_CONTEXT):
        lines: list[ComponentCharge] = []
        for price in sorted(provider_components, key=lambda item: item.component_code):
            quantity = _quantity(quantities, price.quantity_field)
            cost = quantity * price.rate_amount / price.unit_quantity
            customer: Decimal | None = None
            if rule.strategy is PricingStrategy.FIXED_RATE:
                fixed = customer_prices.get(price.component_code)
                if fixed is None:
                    raise ValueError("the FIXED_RATE rule lacks a component of the event")
                customer = quantity * fixed.rate_amount / fixed.unit_quantity
            lines.append(ComponentCharge(price.component_code, quantity, cost, customer))

        # 各分量先求和（未舍入），再各自舍入一次（§80）。
        source_cost = sum((line.provider_cost_unrounded for line in lines), Decimal(0))
        estimated = source_cost if fx_rate is None else source_cost * fx_rate
        if rule.strategy is PricingStrategy.MARKUP:
            assert rule.markup_multiplier is not None
            # 从未舍入的 MYR 估算成本算起，只乘一次（§15）。
            billable = estimated * rule.markup_multiplier
        else:
            # 与供应商成本无关（§15）。
            fixed_prices = [line.customer_price_unrounded or Decimal(0) for line in lines]
            billable = sum(fixed_prices, Decimal(0))

        return ChargeResult(
            components=tuple(lines),
            provider_source_currency=source_currency,
            fx_rate_applied=fx_rate,
            provider_source_cost_unrounded=source_cost,
            provider_source_cost=_stored(source_cost),
            estimated_provider_cost_myr_unrounded=estimated,
            estimated_provider_cost_myr=_stored(estimated),
            billable_cost_unrounded=billable,
            billable_cost=_stored(billable),
        )


# --- 解析（给 T-H 与试算） ------------------------------------------------------------


@dataclass(frozen=True)
class MeterComponent:
    """One component the event needs: every component of its meter type in 025 (design §2)."""

    component_code: str
    quantity_field: str


@dataclass(frozen=True)
class ResolvedModelRef:
    """The model `resolve_model` found. `model_id` / `provider_id` are internal ids."""

    model_id: int
    provider_id: int
    provider_code: str
    model_code: str
    matched_via: MatchedVia


@dataclass(frozen=True)
class ChargeResolution:
    """What `resolve_charge_inputs` found, in the order it looks.

    `status` 为 `PRICED`：输入齐全，`provider_components` 与 `rule_terms` 可直接
    交给 `price_event`（见 `price_resolved`）。否则是第一个遇到的错误：`error_code`
    是 `PRICING_ERROR` 的细分码（其余状态为空），已解析到的部分照样给出（试算要让
    管理员看到缺什么）。内部 id（`provider_price.version_id`、`fx_rate.version_id`、
    `pricing_rule.rule_id`）是 T-H 写进快照的值，不对外。
    """

    status: ChargeStatus
    error_code: PricingErrorCode | None
    meter_components: tuple[MeterComponent, ...]
    model: ResolvedModelRef | None = None
    provider_price: ResolvedProviderPrice | None = None
    fx_rate: ResolvedFxRate | None = None
    pricing_rule: ResolvedPricingRule | None = None
    provider_components: tuple[UnitPrice, ...] = ()
    rule_terms: RuleTerms | None = None


def _meter_components(session: Session, meter_type_id: int) -> tuple[MeterComponent, ...]:
    """Every component of the meter type, `component_code` ascending (rows never change)."""
    found = catalog.components_for(session, [meter_type_id])[meter_type_id]
    if not found:
        # 每个计量类型至少一个分量（025：建类型与建分量同一事务）。
        raise ValueError("the meter type has no components")
    return tuple(MeterComponent(row.component_code, row.quantity_field) for row in found)


def _covering(
    amounts: Mapping[str, tuple[Decimal, Decimal]], needed: Sequence[MeterComponent]
) -> tuple[UnitPrice, ...] | None:
    """The prices of exactly the needed components, or `None` when any one is missing.

    `amounts`：`component_code` → (`unit_quantity`, `rate_amount`)。版本或规则
    可以还定了别的计量类型的价，那些不参与这个事件。
    """
    covered = []
    for item in needed:
        if item.component_code not in amounts:
            return None
        unit_quantity, rate_amount = amounts[item.component_code]
        covered.append(
            UnitPrice(item.component_code, item.quantity_field, unit_quantity, rate_amount)
        )
    return tuple(covered)


def _rule_terms(
    strategy: PricingStrategy,
    markup_multiplier: Decimal | None,
    amounts: Mapping[str, tuple[Decimal, Decimal]],
    needed: Sequence[MeterComponent],
) -> RuleTerms | None:
    """MARKUP: the multiplier. FIXED_RATE: its prices for the needed components, or `None`."""
    if strategy is PricingStrategy.MARKUP:
        return RuleTerms(PricingStrategy.MARKUP, markup_multiplier, ())
    covered = _covering(amounts, needed)
    if covered is None:
        return None
    return RuleTerms(PricingStrategy.FIXED_RATE, None, covered)


def resolve_charge_inputs(
    session: Session,
    *,
    tenant_id: int,
    provider_code: str,
    model_code: str,
    meter_type_id: int,
    occurred_at: dt.datetime,
    locking: bool = True,
) -> ChargeResolution:
    """Model, provider price, FX rate and pricing rule for one event (design §2「解析函数与锁」).

    在调用方的事务里运行，不开事务、不提交。`tenant_id`、`meter_type_id` 是事件的
    内部 id；`provider_code` / `model_code` 是上报的原始字符串；`occurred_at` 是不带
    时区的 UTC。

    错误按下面的顺序判定，**先遇到哪个报哪个**：

    1. `resolve_model`（025）没有 → `MODEL_UNKNOWN`；
    2. `resolve_provider_price`（026）没有 → `PRICING_ERROR` / `NO_PROVIDER_PRICE`；
       版本不含该计量类型的全部分量 → `MISSING_PROVIDER_COMPONENT`；
    3. 原币不是 MYR：`resolve_fx_rate`（028）没有 → `FX_RATE_ERROR`；MYR 不查汇率；
    4. `resolve_pricing_rule`（027）没有 → `PRICING_ERROR` / `NO_PRICING_RULE`；
       命中的 FIXED_RATE 规则不含该计量类型的全部分量 → `MISSING_RULE_COMPONENT`，
       **不下落**到更低一级。

    `locking=True`（T-H 的计费事务）：取锁顺序是 **供应商（S，`resolve_model` 里）
    → `fx_rate_locks`（S）→ `pricing_rule_locks`（S）**，之后价格、汇率、规则都是
    加锁读；锁由调用方的事务持有到提交。本函数**不**判断 `occurred_at` 是否晚于当前
    时间 —— 那是 T-H 持锁后做的（025 契约）。

    `locking=False`（试算）：不取两张单行锁表的共享锁，发布不必等试算。025 / 026 /
    027 / 028 的解析函数自身的读按它们的契约不变（MySQL 上是 `FOR SHARE`），随试算
    的只读事务结束释放。
    """
    model = catalog.resolve_model(session, provider_code, model_code, occurred_at)
    if model is None:
        return ChargeResolution(ChargeStatus.MODEL_UNKNOWN, None, ())
    if locking:
        rates.lock_fx_rates_shared(session)
        rules.lock_pricing_rules_shared(session)

    needed = _meter_components(session, meter_type_id)
    found = ResolvedModelRef(
        model_id=model.id,
        provider_id=model.provider_id,
        provider_code=provider_code,
        model_code=model.code,
        # 同一供应商下模型代码与别名不会撞（025），代码不等就是经别名命中的。
        matched_via=MatchedVia.CODE if model.code == model_code else MatchedVia.ALIAS,
    )

    price = prices.resolve_provider_price(session, model.provider_id, model.id, occurred_at)
    if price is None:
        return ChargeResolution(
            ChargeStatus.PRICING_ERROR, PricingErrorCode.NO_PROVIDER_PRICE, needed, model=found
        )
    provider_components = _covering(
        {item.component_code: (item.unit_quantity, item.rate_amount) for item in price.components},
        needed,
    )
    if provider_components is None:
        return ChargeResolution(
            ChargeStatus.PRICING_ERROR,
            PricingErrorCode.MISSING_PROVIDER_COMPONENT,
            needed,
            model=found,
            provider_price=price,
        )

    fx: ResolvedFxRate | None = None
    if price.source_currency != QUOTE_CURRENCY:
        fx = rates.resolve_fx_rate(session, price.source_currency, occurred_at)
        if fx is None:
            return ChargeResolution(
                ChargeStatus.FX_RATE_ERROR, None, needed, model=found, provider_price=price
            )

    rule = rules.resolve_pricing_rule(session, tenant_id, model.provider_id, model.id, occurred_at)
    if rule is None:
        return ChargeResolution(
            ChargeStatus.PRICING_ERROR,
            PricingErrorCode.NO_PRICING_RULE,
            needed,
            model=found,
            provider_price=price,
            fx_rate=fx,
        )
    terms = _rule_terms(
        rule.strategy,
        rule.markup_multiplier,
        {item.component_code: (item.unit_quantity, item.rate_amount) for item in rule.components},
        needed,
    )
    if terms is None:
        return ChargeResolution(
            ChargeStatus.PRICING_ERROR,
            PricingErrorCode.MISSING_RULE_COMPONENT,
            needed,
            model=found,
            provider_price=price,
            fx_rate=fx,
            pricing_rule=rule,
        )
    return ChargeResolution(
        ChargeStatus.PRICED,
        None,
        needed,
        model=found,
        provider_price=price,
        fx_rate=fx,
        pricing_rule=rule,
        provider_components=provider_components,
        rule_terms=terms,
    )


def price_resolved(
    resolution: ChargeResolution, quantities: Mapping[str, int | Decimal]
) -> ChargeResult:
    """`price_event` on what `resolve_charge_inputs` found. Only for a `PRICED` resolution."""
    if resolution.status is not ChargeStatus.PRICED:
        raise ValueError("only a resolution with every input can be priced")
    assert resolution.provider_price is not None and resolution.rule_terms is not None
    return price_event(
        quantities=quantities,
        source_currency=resolution.provider_price.source_currency,
        provider_components=resolution.provider_components,
        fx_rate=resolution.fx_rate.rate if resolution.fx_rate is not None else None,
        rule=resolution.rule_terms,
    )


# --- 重算（ADR-0005） -------------------------------------------------------------


@dataclass(frozen=True)
class ChargeSnapshot:
    """The version references T-H stores on an event (internal ids; `fx_rate_version_id` is empty
    for an MYR source)."""

    usage_meter_type_id: int
    provider_price_version_id: int
    fx_rate_version_id: int | None
    pricing_rule_id: int


def reprice_from_snapshot(
    session: Session, snapshot: ChargeSnapshot, quantities: Mapping[str, int | Decimal]
) -> ChargeResult:
    """Price the event again from the versions its snapshot references. Read-only.

    版本、汇率与规则发布后不可变（026 / 027 / 028 的触发器），所以结果与快照一致（ADR-0005）。
    快照引用的行缺失或不再覆盖事件的分量是数据损坏：`ValueError`。
    """
    needed = _meter_components(session, snapshot.usage_meter_type_id)
    version = session.get(ProviderPriceVersion, snapshot.provider_price_version_id)
    rule = session.get(PricingRule, snapshot.pricing_rule_id)
    if version is None or rule is None:
        raise ValueError("the snapshot references a missing price version or rule")
    fx_rate: Decimal | None = None
    if snapshot.fx_rate_version_id is not None:
        fx = session.get(FxRateVersion, snapshot.fx_rate_version_id)
        if fx is None:
            raise ValueError("the snapshot references a missing FX rate version")
        fx_rate = fx.rate

    priced = prices.components_for(session, [version.id])[version.id]
    provider_components = _covering(
        {
            item.component.component_code: (item.row.unit_quantity, item.row.rate_amount)
            for item in priced
        },
        needed,
    )
    stored = rules.components_for(session, [rule.id])[rule.id]
    terms = _rule_terms(
        PricingStrategy(rule.strategy),
        rule.markup_multiplier,
        {
            item.component.component_code: (item.row.unit_quantity, item.row.rate_amount)
            for item in stored
        },
        needed,
    )
    if provider_components is None or terms is None:
        raise ValueError("the snapshot's versions do not price the event")
    return price_event(
        quantities=quantities,
        source_currency=version.source_currency,
        provider_components=provider_components,
        fx_rate=fx_rate,
        rule=terms,
    )


# --- 试算（§59） ------------------------------------------------------------------


class PreviewInvalid(AppError):
    """The usage fields do not fit the meter type's shape. Names the fields, never the values."""

    def __init__(self, *fields: str) -> None:
        names = ", ".join(sorted(set(fields)))
        super().__init__(
            f"Invalid request fields: {names}", code="VALIDATION_ERROR", http_status=422
        )


class PreviewOutOfRange(AppError):
    """A result does not fit DECIMAL(20,8) (`ChargeOverflow`): billing would fail the event."""

    def __init__(self) -> None:
        super().__init__(
            "A calculated amount is out of range.",
            code=ChargeOverflow.code,
            http_status=422,
        )


def _preview_quantities(
    request: PricingPreviewRequest, meter: UsageMeterType
) -> dict[str, int | Decimal]:
    """The usage fields by the meter type's shape, as ingestion takes them (AIH-TASK-029 ⑦)."""
    present = request.model_fields_set
    bad: list[str] = []
    if meter.payload_shape is PayloadShape.LLM_TOKEN_FIELDS:
        tokens: dict[str, int | Decimal] = {}
        for name in TOKEN_FIELDS:
            value = getattr(request, name)
            if value is None:
                bad.append(f"body.{name}")
            else:
                tokens[name] = value
        bad.extend(f"body.{name}" for name in ("quantity", "unit") if name in present)
        if bad:
            raise PreviewInvalid(*bad)
        return tokens

    bad.extend(f"body.{name}" for name in TOKEN_FIELDS if name in present)
    if request.quantity is None:
        bad.append("body.quantity")
    elif meter.quantity_kind is QuantityKind.INTEGER and "." in request.quantity:
        # 整数类型不许有小数部分，`"3.0"` 也拒绝（与摄取一致）。
        bad.append("body.quantity")
    if request.unit != meter.unit:
        bad.append("body.unit")
    if bad:
        raise PreviewInvalid(*bad)
    assert request.quantity is not None
    # 正则已保证是 ≤ 12 位整数、≤ 8 位小数的十进制串：精确解析。
    return {QUANTITY_FIELD: Decimal(request.quantity)}


def _occurred_at(value: str) -> dt.datetime:
    """RFC 3339 with an offset → naive UTC. The pattern already held; the calendar may not."""
    try:
        moment = dt.datetime.fromisoformat(value)
        return moment.astimezone(dt.UTC).replace(tzinfo=None)
    except (ValueError, OverflowError):
        raise PreviewInvalid("body.occurred_at") from None


def _text(value: Decimal) -> str:
    """An exact decimal string, never in exponent notation."""
    return format(value, "f")


def _optional_text(value: Decimal | None) -> str | None:
    return _text(value) if value is not None else None


def preview_charge(
    session_factory: sessionmaker[Session], request: PricingPreviewRequest
) -> PricingPreviewView:
    """§59 preview calculation: one read-only transaction, `locking=False`, nothing written.

    与计费同一套解析（`resolve_charge_inputs`）与计算（`price_event`）。
    客户不存在 404 `CUSTOMER_NOT_FOUND`；`usage_type` 不是计量类型 404
    `USAGE_METER_TYPE_NOT_FOUND`；用量字段与形态不符 422 `VALIDATION_ERROR`；
    结果超出 DECIMAL(20,8) 422 `AMOUNT_OUT_OF_RANGE`。其余（模型未知、缺价格 /
    汇率 / 规则）是 200 带状态与已解析到的部分。只用已发布的版本与规则。
    """
    with session_factory() as session:
        tenant = tenancy.get_tenant_by_public_id(session, request.customer_id)
        if tenant is None:
            raise CustomerNotFound
        meter = usage.find_meter_type(session, request.usage_type)
        if meter is None:
            raise MeterTypeNotFound
        occurred_at = _occurred_at(request.occurred_at)
        quantities = _preview_quantities(request, meter)
        resolution = resolve_charge_inputs(
            session,
            tenant_id=tenant.id,
            provider_code=request.provider,
            model_code=request.model,
            meter_type_id=meter.id,
            occurred_at=occurred_at,
            locking=False,
        )
        charge: ChargeResult | None = None
        if resolution.status is ChargeStatus.PRICED:
            try:
                charge = price_resolved(resolution, quantities)
            except ChargeOverflow:
                raise PreviewOutOfRange from None
        observed_at: dt.datetime | None = None
        if resolution.fx_rate is not None:
            version = rates.get_version(session, resolution.fx_rate.public_id)
            assert version is not None
            observed_at = version.observed_at
        return _preview_view(resolution, charge, observed_at)


def _model_view(model: ResolvedModelRef | None) -> PreviewModel | None:
    if model is None:
        return None
    return PreviewModel(
        provider=model.provider_code, model=model.model_code, matched_via=model.matched_via.value
    )


def _price_view(price: ResolvedProviderPrice | None) -> PreviewPriceVersion | None:
    if price is None:
        return None
    return PreviewPriceVersion(
        id=price.public_id,
        source_currency=price.source_currency,
        effective_from=price.effective_from,
        effective_to=price.effective_to,
    )


def _fx_view(fx: ResolvedFxRate | None, observed_at: dt.datetime | None) -> PreviewFxRate | None:
    if fx is None or observed_at is None:
        return None
    # 汇率原值（DECIMAL(24,10)），不舍入。
    return PreviewFxRate(id=fx.public_id, rate=_text(fx.rate), observed_at=observed_at)


def _rule_view(rule: ResolvedPricingRule | None) -> PreviewPricingRule | None:
    if rule is None:
        return None
    multiplier = rule.markup_multiplier
    return PreviewPricingRule(
        id=rule.public_id,
        priority_scope=PricingScope(rule.priority_scope).value,
        strategy=PricingStrategy(rule.strategy).value,
        markup_multiplier=money_text(multiplier) if multiplier is not None else None,
    )


def _amounts(charge: ChargeResult | None) -> dict[str, str | None]:
    """The six amounts as decimal strings; all `None` when nothing was priced."""
    names = (
        "provider_source_cost_unrounded",
        "provider_source_cost",
        "estimated_provider_cost_myr_unrounded",
        "estimated_provider_cost_myr",
        "billable_cost_unrounded",
        "billable_cost",
    )
    return {name: None if charge is None else _text(getattr(charge, name)) for name in names}


def _preview_view(
    resolution: ChargeResolution, charge: ChargeResult | None, observed_at: dt.datetime | None
) -> PricingPreviewView:
    components: list[PreviewComponent] = []
    if charge is not None:
        components = [
            PreviewComponent(
                component_code=line.component_code,
                quantity=_text(line.quantity),
                provider_cost_unrounded=_text(line.provider_cost_unrounded),
                customer_price_unrounded=_optional_text(line.customer_price_unrounded),
            )
            for line in charge.components
        ]
    error_code = resolution.error_code
    return PricingPreviewView(
        status=resolution.status.value,
        error_code=error_code.value if error_code is not None else None,
        model=_model_view(resolution.model),
        provider_price_version=_price_view(resolution.provider_price),
        fx_rate_version=_fx_view(resolution.fx_rate, observed_at),
        pricing_rule=_rule_view(resolution.pricing_rule),
        components=components,
        # ADR-0008：计费额是含税金额。
        tax_inclusive=True,
        **_amounts(charge),
    )


__all__ = [
    "PRICING_CONTEXT",
    "ChargeOverflow",
    "ChargeResolution",
    "ChargeResult",
    "ChargeSnapshot",
    "ChargeStatus",
    "ComponentCharge",
    "MatchedVia",
    "MeterComponent",
    "PreviewInvalid",
    "PreviewOutOfRange",
    "PricingErrorCode",
    "ResolvedModelRef",
    "RuleTerms",
    "UnitPrice",
    "preview_charge",
    "price_event",
    "price_resolved",
    "reprice_from_snapshot",
    "resolve_charge_inputs",
]
