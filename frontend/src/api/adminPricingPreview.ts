/**
 * 管理端试算预览（契约见 docs/api.md「管理端试算预览」，设计闸门 #179 v1，后端为 AIH-TASK-031）。
 *
 * 只有一个接口：`POST /api/v1/admin/pricing-preview`。**只读**：不写库、不写审计，只用已发布的版本与规则。
 * 响应里有供应商成本、汇率与倍数，只在管理端（INV-7）—— 是**估算成本，客户不可见**。
 *
 * **错误状态也是 200**（`MODEL_UNKNOWN` / `PRICING_ERROR` / `FX_RATE_ERROR`）：给出已解析到的部分，其余为
 * `null`。只有请求本身不合法才是 4xx。
 *
 * ⚠️ 金额、未舍入值、汇率、倍数与数量全是**十进制字符串**，这一层不解析（INV-10）。
 *
 * ⚠️ 四个 token 字段后端只收 **JSON 整数**（严格类型：`"3"` 是 422），而前端的数量一律是字符串、不转成
 * `number`。所以请求体在这里**手写成 JSON 文本**：字符串字段照常 `JSON.stringify`，token 先规范成不带前导 0
 * 的十进制整数串（{@link canonicalTokenCount}，0–10¹²），再原样拼进去当 JSON 数字。不合规范的值根本不拼。
 */

import { VALIDATION_ERROR } from "./adminCatalog";
import { ApiError, client, toApiError, unwrapEnvelope, type ApiEnvelope } from "./client";

const PREVIEW_URL = "/api/v1/admin/pricing-preview";

// --- 错误码（docs/api.md 本节「本节专有的错误码」） ---------------------------------
// `CUSTOMER_NOT_FOUND` 在 adminCustomers 里，`USAGE_METER_TYPE_NOT_FOUND` 在 adminCatalog 里。

/** 某个存储值超出 DECIMAL(20,8)（计费侧会把这样的事件标 `FAILED_FINAL`）。 */
export const AMOUNT_OUT_OF_RANGE = "AMOUNT_OUT_OF_RANGE";

// --- 字段规则 -------------------------------------------------------------------
//
// 与后端 `app/schemas/usage_ingest.py` 是同一组规则（试算与摄取同一套字段写法）。
// 前端校验只是提前告诉用户；后端的 422 才是准绳。

/** `QUANTITY` 形态的 `quantity`：十进制字符串，整数最多 12 位、小数最多 8 位，不带符号。 */
export const QUANTITY_PATTERN = /^[0-9]{1,12}(\.[0-9]{1,8})?$/;
/** 整数类型（`quantity_kind = INTEGER`）的 `quantity`：不许有小数部分，`"3.0"` 也拒绝。 */
export const INTEGER_QUANTITY_PATTERN = /^[0-9]{1,12}$/;
/** token 个数的上限 10¹²，按字符串写。 */
export const MAX_TOKENS = "1000000000000";

/**
 * token 个数 → 规范写法（去掉前导 0），0–10¹²；不是这个范围里的整数（空串、小数、符号、空白、超上限）
 * 返回 `null`。只做字符串操作（`"007"` 不是合法的 JSON 数字，所以要先规范）。
 */
export function canonicalTokenCount(value: string): string | null {
  if (!/^[0-9]{1,13}$/.test(value)) {
    return null;
  }
  const canonical = value.replace(/^0+(?=[0-9])/, "");
  return canonical.length < MAX_TOKENS.length || canonical === MAX_TOKENS ? canonical : null;
}

// --- 请求 -----------------------------------------------------------------------

interface PreviewRequestBase {
  /** 客户（租户）的 `public_id`。 */
  customer_id: string;
  /** 供应商代码。 */
  provider: string;
  /** 模型代码或别名，按 `occurred_at` 解析。 */
  model: string;
  /** 计量类型 `code`。 */
  usage_type: string;
  /** RFC 3339，**必须带时区**；可以是将来。 */
  occurred_at: string;
}

/** `LLM_TOKEN_FIELDS` 形态（`LLM_TOKEN`）：四个都必填。值是十进制整数**字符串**，发出去时变成 JSON 整数。 */
export interface TokenPreviewRequest extends PreviewRequestBase {
  input_tokens: string;
  output_tokens: string;
  cache_creation_input_tokens: string;
  cache_read_input_tokens: string;
}

/** `QUANTITY` 形态：`quantity` 是 JSON 字符串，`unit` 必须等于该类型的单位。 */
export interface QuantityPreviewRequest extends PreviewRequestBase {
  quantity: string;
  unit: string;
}

export type PricingPreviewRequest = TokenPreviewRequest | QuantityPreviewRequest;

const TOKEN_FIELDS = [
  "input_tokens",
  "output_tokens",
  "cache_creation_input_tokens",
  "cache_read_input_tokens",
] as const;

function isTokenRequest(request: PricingPreviewRequest): request is TokenPreviewRequest {
  return "input_tokens" in request;
}

/**
 * 请求 → JSON 文本。字段顺序固定；字符串一律 `JSON.stringify`，token 是规范后的整数串（JSON 数字）。
 * 有一个 token 不合规范就抛 `VALIDATION_ERROR`，什么都不发 —— 不把任意文本拼进 JSON。
 */
export function previewRequestJson(request: PricingPreviewRequest): string {
  const fields: [string, string][] = [
    ["customer_id", JSON.stringify(request.customer_id)],
    ["provider", JSON.stringify(request.provider)],
    ["model", JSON.stringify(request.model)],
    ["usage_type", JSON.stringify(request.usage_type)],
  ];
  if (isTokenRequest(request)) {
    for (const name of TOKEN_FIELDS) {
      const count = canonicalTokenCount(request[name]);
      if (count === null) {
        throw new ApiError(VALIDATION_ERROR, `Invalid request fields: ${name}`, null);
      }
      fields.push([name, count]);
    }
  } else {
    fields.push(["quantity", JSON.stringify(request.quantity)], ["unit", JSON.stringify(request.unit)]);
  }
  fields.push(["occurred_at", JSON.stringify(request.occurred_at)]);
  return `{${fields.map(([name, value]) => `${JSON.stringify(name)}:${value}`).join(",")}}`;
}

// --- 响应 -----------------------------------------------------------------------
// 时间都是不带时区的 UTC，精确到秒（docs/api.md「时间」）。

export interface PreviewModel {
  provider: string;
  /** 模型自己的代码（用别名试算时也是）。 */
  model: string;
  /** `code` 或 `alias`。 */
  matched_via: string;
}

export interface PreviewPriceVersion {
  id: string;
  source_currency: string;
  /** `null` =「一直以来」。 */
  effective_from: string | null;
  /** `null` =「仍生效」。 */
  effective_to: string | null;
}

export interface PreviewFxRate {
  id: string;
  /** 1 单位原币 = 多少 MYR，原值（不舍入）。 */
  rate: string;
  observed_at: string;
}

export interface PreviewPricingRule {
  id: string;
  priority_scope: string;
  strategy: string;
  /** 只有 MARKUP 有。 */
  markup_multiplier: string | null;
}

export interface PreviewComponent {
  component_code: string;
  quantity: string;
  /** 未舍入的供应商原币成本。 */
  provider_cost_unrounded: string;
  /** 未舍入的客户分量价（MYR 含税），只有 FIXED_RATE 有。 */
  customer_price_unrounded: string | null;
}

export interface PricingPreview {
  /** `PRICED` / `MODEL_UNKNOWN` / `PRICING_ERROR` / `FX_RATE_ERROR`；认不出的原样显示。 */
  status: string;
  /** `PRICING_ERROR` 的细分码；其余状态为 `null`。 */
  error_code: string | null;
  model: PreviewModel | null;
  provider_price_version: PreviewPriceVersion | null;
  /** 原币是 MYR 时为 `null`（不查汇率）；没解析到时也是 `null`。 */
  fx_rate_version: PreviewFxRate | null;
  pricing_rule: PreviewPricingRule | null;
  /** 该计量类型的全部分量，`component_code` 升序；错误状态为空。 */
  components: PreviewComponent[];
  provider_source_cost_unrounded: string | null;
  /** 存储值：恰好 8 位小数。 */
  provider_source_cost: string | null;
  estimated_provider_cost_myr_unrounded: string | null;
  estimated_provider_cost_myr: string | null;
  billable_cost_unrounded: string | null;
  /** 含税金额（ADR-0008）。 */
  billable_cost: string | null;
  /** 恒为 `true`。 */
  tax_inclusive: boolean;
}

// --- 接口 -----------------------------------------------------------------------

/** 试算一次。只读，可以重发。 */
export async function previewPricing(request: PricingPreviewRequest): Promise<PricingPreview> {
  try {
    const body = previewRequestJson(request);
    // 已经是 JSON 文本：axios 见到 JSON 的 Content-Type 与字符串就原样发出，不再序列化一遍。
    const response = await client.post<ApiEnvelope<PricingPreview>>(PREVIEW_URL, body, {
      headers: { "Content-Type": "application/json" },
    });
    return unwrapEnvelope<PricingPreview>(response.data);
  } catch (error) {
    throw toApiError(error);
  }
}
