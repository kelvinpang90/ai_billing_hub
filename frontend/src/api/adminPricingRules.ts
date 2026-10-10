/**
 * 管理端定价规则（契约见 docs/api.md「管理端定价规则」，设计闸门 #178 v4，后端为 AIH-TASK-027）。
 *
 * 收录那一节的全部 7 个接口。每个都要 ADMIN，角色以后端数据库为准 —— 前端不做任何角色判断，
 * 403 原样交给页面显示。路径里的 id 一律是 `public_id`；客户用租户的 `public_id`（`customer_id`）。
 *
 * **价格一律是 MYR 含税价（ADR-0008）**：FIXED_RATE 的 `rate_amount` 是含税单价；倍数乘 MYR 估算成本，
 * 得到的就是含税计费额。倍数（即 markup）只在管理端可见（INV-7）。
 *
 * ⚠️ `markup_multiplier`、`unit_quantity` 与 `rate_amount` 是**字符串**（INV-10），收发都不在这一层解析：
 * 后端只收 JSON 字符串（JSON 数字是 422），回来的是恰好 8 位小数的字符串。
 */

import type { Page } from "./adminCustomers";
import { apiGet, client, toApiError, unwrapEnvelope, type ApiEnvelope } from "./client";

const RULES_URL = "/api/v1/admin/pricing-rules";

// --- 错误码（docs/api.md 本节「本节专有的错误码」） ---------------------------------
// 与价格共用的几个（`AI_PROVIDER_NOT_FOUND`、`AI_MODEL_NOT_FOUND`、`USAGE_METER_COMPONENT_NOT_FOUND`、
// `CATALOG_ITEM_RETIRED`、`EFFECTIVE_FROM_CONFLICT`、`EFFECTIVE_FROM_IN_PAST`）沿用 adminCatalog /
// adminProviderPrices 里的常量，不另起一份。

export const PRICING_RULE_NOT_FOUND = "PRICING_RULE_NOT_FOUND";
export const PRICING_RULE_NOT_DRAFT = "PRICING_RULE_NOT_DRAFT";
/** 发布 FIXED_RATE 规则时完整性不满足；`message` 列出缺的分量代码。 */
export const PRICING_RULE_INCOMPLETE = "PRICING_RULE_INCOMPLETE";
/** 停用一条已被后继截断的历史规则，或停用草稿。 */
export const PRICING_RULE_NOT_RETIRABLE = "PRICING_RULE_NOT_RETIRABLE";
/** 对已停用 / 已丢弃的规则做任何写操作。 */
export const PRICING_RULE_FINAL = "PRICING_RULE_FINAL";

// --- 范围与策略 -------------------------------------------------------------------

/** spec §16 的五级，从高到低（docs/api.md 本节「范围」）。 */
export const PRIORITY_SCOPES = [
  "CUSTOMER_PROVIDER_MODEL",
  "CUSTOMER_PROVIDER",
  "CUSTOMER",
  "GLOBAL_PROVIDER_MODEL",
  "GLOBAL",
] as const;
export type PriorityScope = (typeof PRIORITY_SCOPES)[number];

/**
 * 每一级必填哪几个范围字段；其余的**不许给**（多给或少给都是 422，`null` 等同不给）。
 * 与后端 `app/schemas/pricing_rules.py` 的 `required_scope_fields` 是同一张表。
 */
export interface ScopeFields {
  customer: boolean;
  provider: boolean;
  model: boolean;
}

export const SCOPE_FIELDS: Readonly<Record<PriorityScope, ScopeFields>> = {
  CUSTOMER_PROVIDER_MODEL: { customer: true, provider: true, model: true },
  CUSTOMER_PROVIDER: { customer: true, provider: true, model: false },
  CUSTOMER: { customer: true, provider: false, model: false },
  GLOBAL_PROVIDER_MODEL: { customer: false, provider: true, model: true },
  GLOBAL: { customer: false, provider: false, model: false },
};

export const PRICING_STRATEGIES = ["MARKUP", "FIXED_RATE"] as const;
export type PricingStrategy = (typeof PRICING_STRATEGIES)[number];

export const PRICING_RULE_STATUSES = ["DRAFT", "PUBLISHED", "RETIRED", "DISCARDED"] as const;
export type PricingRuleStatus = (typeof PRICING_RULE_STATUSES)[number];

/** 规则的币种固定为 MYR，请求里不收 `currency`。 */
export const RULE_CURRENCY = "MYR";

export function isPriorityScope(value: unknown): value is PriorityScope {
  return typeof value === "string" && (PRIORITY_SCOPES as readonly string[]).includes(value);
}

export function isPricingStrategy(value: unknown): value is PricingStrategy {
  return typeof value === "string" && (PRICING_STRATEGIES as readonly string[]).includes(value);
}

export function isPricingRuleStatus(value: unknown): value is PricingRuleStatus {
  return typeof value === "string" && (PRICING_RULE_STATUSES as readonly string[]).includes(value);
}

// --- 对象 -----------------------------------------------------------------------
// 时间都是不带时区的 UTC，精确到秒（docs/api.md「时间」）。

export interface PricingRuleComponent {
  component_code: string;
  /** 分量所属的计量类型与它的单位：`unit_quantity` 个这种单位对应一个 `rate_amount`。 */
  meter_type_code: string;
  unit: string;
  /** 恰好 8 位小数的十进制字符串。 */
  unit_quantity: string;
  /** MYR 含税单价，恰好 8 位小数的十进制字符串。 */
  rate_amount: string;
  /** 永远是 MYR。 */
  currency: string;
  created_at: string;
}

export interface PricingRule {
  id: string;
  /** 后端的取值；认不出的原样显示，所以类型放宽成 string。 */
  priority_scope: string;
  /** 按 `priority_scope` 有值，其余为 `null`（`provider_code` / `model_code` 同理）。 */
  customer_id: string | null;
  provider_id: string | null;
  provider_code: string | null;
  model_id: string | null;
  model_code: string | null;
  strategy: string;
  /** MARKUP 的倍数，恰好 8 位小数的十进制字符串；FIXED_RATE 为 `null`。 */
  markup_multiplier: string | null;
  status: string;
  /** 草稿为 `null`；已发布时 `null` = `GLOBAL` 范围的第一条「一直以来」。 */
  effective_from: string | null;
  /** `null` = 仍生效（或尚未发布）。与 `effective_from` 相等是空区间，永不命中。 */
  effective_to: string | null;
  /** FIXED_RATE 的分量，`component_code` 升序；MARKUP 为空列表。 */
  components: PricingRuleComponent[];
  created_by_email: string | null;
  approved_by_email: string | null;
  created_at: string;
  updated_at: string;
  approved_at: string | null;
}

// --- 请求体 ---------------------------------------------------------------------
// 所有请求体都拒绝多余字段（422），这里的类型也不给别的字段留位置。

export interface RuleComponentBody {
  component_code: string;
  unit_quantity: string;
  rate_amount: string;
}

/**
 * 建草稿。范围字段按 `priority_scope` 给，不要的**不出现**在请求体里；MARKUP 只带 `markup_multiplier`，
 * FIXED_RATE 只带 `components`（混着带是 422）。
 */
export interface CreatePricingRuleBody {
  priority_scope: PriorityScope;
  customer_id?: string;
  provider_id?: string;
  model_id?: string;
  strategy: PricingStrategy;
  markup_multiplier?: string;
  components?: RuleComponentBody[];
}

/** 只带要改的字段，至少一个，都不许是 `null`；`components` 是整体替换。范围字段不可改。 */
export interface PricingRulePatch {
  strategy?: PricingStrategy;
  markup_multiplier?: string;
  components?: RuleComponentBody[];
}

// --- 查询条件 -------------------------------------------------------------------

export interface PricingRuleFilter {
  priority_scope?: PriorityScope;
  customer_id?: string;
  provider_id?: string;
  model_id?: string;
  status?: PricingRuleStatus;
}

// --- 查询键 ---------------------------------------------------------------------

export const PRICING_RULES_QUERY_KEY = ["admin", "pricing-rules"] as const;
/** 所有列表页：任何写操作之后让它们整体过期。 */
export const PRICING_RULE_LISTS_QUERY_KEY = [...PRICING_RULES_QUERY_KEY, "list"] as const;

export function pricingRuleListQueryKey(filter: PricingRuleFilter, page: number, pageSize: number) {
  return [...PRICING_RULE_LISTS_QUERY_KEY, filter, page, pageSize] as const;
}

export function pricingRuleDetailQueryKey(ruleId: string) {
  return [...PRICING_RULES_QUERY_KEY, "detail", ruleId] as const;
}

// --- 地址 -----------------------------------------------------------------------

// id 来自地址栏，不能信它只含 uuid 的字符：不转义的话 `a/b` 会变成另一条路径。
function ruleUrl(ruleId: string): string {
  return `${RULES_URL}/${encodeURIComponent(ruleId)}`;
}

/** 分页参数加上筛选条件。⚠️ 没给的条件**不出现在查询串里**（`status=` 这种空值后端回 422）。 */
function listQuery(page: number, pageSize: number, extra: Record<string, string | undefined>): string {
  const query = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
  for (const [name, value] of Object.entries(extra)) {
    if (value !== undefined && value !== "") {
      query.set(name, value);
    }
  }
  return query.toString();
}

async function send<T>(method: "post" | "patch", url: string, body: unknown): Promise<T> {
  try {
    const response =
      method === "post"
        ? await client.post<ApiEnvelope<T>>(url, body)
        : await client.patch<ApiEnvelope<T>>(url, body);
    return unwrapEnvelope<T>(response.data);
  } catch (error) {
    throw toApiError(error);
  }
}

// --- 接口 -----------------------------------------------------------------------

/** spec §59「查看价格历史」：按范围（§16 的顺序）、`effective_from`（`null` 在前）排序，每项带分量。 */
export function listPricingRules(
  filter: PricingRuleFilter,
  page: number,
  pageSize: number,
  signal?: AbortSignal,
): Promise<Page<PricingRule>> {
  const query = listQuery(page, pageSize, {
    priority_scope: filter.priority_scope,
    customer_id: filter.customer_id,
    provider_id: filter.provider_id,
    model_id: filter.model_id,
    status: filter.status,
  });
  return apiGet<Page<PricingRule>>(`${RULES_URL}?${query}`, signal);
}

/** ⚠️ **不幂等**：重发会建出两个草稿（草稿不影响计费，丢弃多余的即可）。防双击是调用方的责任。 */
export function createPricingRule(body: CreatePricingRuleBody): Promise<PricingRule> {
  return send<PricingRule>("post", RULES_URL, body);
}

export function getPricingRule(ruleId: string, signal?: AbortSignal): Promise<PricingRule> {
  return apiGet<PricingRule>(ruleUrl(ruleId), signal);
}

export function updatePricingRule(ruleId: string, patch: PricingRulePatch): Promise<PricingRule> {
  return send<PricingRule>("patch", ruleUrl(ruleId), patch);
}

/**
 * 发布。`effectiveFrom` 是带时区的 RFC 3339、整秒（见 `displayTimeToRfc3339`）；不给就是「不指定」，
 * 请求体是 `{}` —— 不发 `effective_from: null`。已发布再发布：200，什么都不写。
 */
export function publishPricingRule(ruleId: string, effectiveFrom?: string): Promise<PricingRule> {
  const body = effectiveFrom === undefined ? {} : { effective_from: effectiveFrom };
  return send<PricingRule>("post", `${ruleUrl(ruleId)}/publish`, body);
}

/** 停用当前规则（spec §59「disable rule」），或撤销一个尚未开始的预约。`reason` 记在审计上。 */
export function retirePricingRule(ruleId: string, reason: string): Promise<PricingRule> {
  return send<PricingRule>("post", `${ruleUrl(ruleId)}/retire`, { reason });
}

/** 草稿 → `DISCARDED`，行留着。请求体是 `{}`。 */
export function discardPricingRule(ruleId: string): Promise<PricingRule> {
  return send<PricingRule>("post", `${ruleUrl(ruleId)}/discard`, {});
}
