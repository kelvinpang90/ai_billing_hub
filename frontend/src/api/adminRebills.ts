/**
 * 管理端 Rebill（契约见 docs/api.md「管理端 Rebill」，设计闸门 #830 v12 §2「管理端接口」，后端为 AIH-TASK-256）。
 *
 * 收录那一节的全部 4 个接口：列表、详情、预览与确认。列表与详情给 AIH-TASK-257 的两页用，预览与确认给 AIH-TASK-260 的新建页用。
 * 每个都要 ADMIN，角色以后端数据库为准 —— 前端不做任何角色判断，403 原样交给页面显示。路径里的 id 是 Rebill 的 `public_id`，
 * 只来自后端，原样放进路径。
 *
 * ⚠️ 金额全是**恰好 8 位小数的字符串**（INV-10），收发都不在这一层解析，页面也不做金额运算：账本金额、合计与差额都取后端给的值。
 *
 * ⚠️ 预览与确认的请求体 `extra` 一律拒绝（多带任何字段都是 422），所以都按字段逐个拼出来，不把调用方的对象整个转发。
 */

import { apiGet, client, toApiError, unwrapEnvelope, type ApiEnvelope } from "./client";

const REBILLS_URL = "/api/v1/admin/rebills";
/** 与后端一样先于 `{rebill_id}` 写出：这一段不是记录 id。 */
const PREVIEW_URL = `${REBILLS_URL}/preview`;

// --- 错误码（docs/api.md 本节「错误」） -------------------------------------------------

/** 详情的 `rebill_id` 查不到（含不是 UUID 的值）。 */
export const REBILL_NOT_FOUND = "REBILL_NOT_FOUND";
/** 仅确认：预览之后数据或参数变了。重新预览后再确认。 */
export const REBILL_PREVIEW_STALE = "REBILL_PREVIEW_STALE";

// --- 取值 -----------------------------------------------------------------------

/** 账本行类型：`difference < 0` 退回（Credit），`difference > 0` 补扣（Debit）。 */
export const REBILL_CREDIT = "REBILL_CREDIT";
export const REBILL_DEBIT = "REBILL_DEBIT";

/** 列表一页的条数：后端的默认值（1–100）。 */
export const REBILL_PAGE_LIMIT = 50;

// --- 对象 -----------------------------------------------------------------------
// 时间都是不带时区的 UTC，没有 `Z`（docs/api.md 本节开头）。

/** 列表的一项。 */
export interface RebillSummary {
  id: string;
  /** 客户的 `public_id`。接口不返回公司名。 */
  customer_id: string;
  /** 明细行数（只算差额不为 0 的事件）。 */
  event_count: number;
  difference: string;
  /** `REBILL_CREDIT` 或 `REBILL_DEBIT`。 */
  ledger_type: string;
  /** `= −difference`。 */
  ledger_amount: string;
  reason: string;
  created_by_email: string;
  /** 确认时刻，整秒 UTC。 */
  created_at: string;
}

export interface RebillPage {
  items: RebillSummary[];
  /** 还有下一页时是本页最后一条的 `public_id`，否则 `null`。 */
  next_cursor: string | null;
}

/** 预览的一行明细：一个差额不为 0 的事件。 */
export interface RebillLine {
  usage_event_id: string;
  occurred_at: string;
  /** 事件所属项目的 `public_id`；没有项目为 `null`。 */
  project: string | null;
  /** 目录的 `code`。 */
  provider: string;
  model: string;
  /** `eff(e)`、`new(e)` 与 `d(e)`。 */
  previous_charge: string;
  corrected_charge: string;
  difference: string;
  previous_pricing_rule: string;
  corrected_pricing_rule: string;
  previous_provider_price_version: string;
  corrected_provider_price_version: string;
  /** Acuven 内部的估算成本（INV-7：只给管理端看）。 */
  corrected_estimated_provider_cost_myr: string;
  /** `YYYY-MM`，原 `AI_USAGE` 账本行的归属月。 */
  original_period: string;
  /** 这个原账期已存在的月度对账单的 `public_id`；没有为 `null`。 */
  original_statement_id: string | null;
}

/** 详情的一行明细：预览的字段，另加所属 Rebill 的 `public_id`。 */
export interface RebillDetailLine extends RebillLine {
  rebill_id: string;
}

export interface RebillDetail extends RebillSummary {
  previous_total: string;
  corrected_total: string;
  /** 请求里的更正规则与价格版本；没给为 `null`（即按当前有效版本重算）。 */
  corrective_pricing_rule_id: string | null;
  corrective_provider_price_version_id: string | null;
  /** 那一行 `REBILL_*` 账本的 `public_id`。 */
  wallet_transaction_id: string;
  /** 全部明细，按明细 id 升序。 */
  lines: RebillDetailLine[];
}

/** 确认的响应：详情加 `replayed`（同一幂等键、同一请求重发时为 `true`）。 */
export interface RebillConfirmation extends RebillDetail {
  replayed: boolean;
}

export interface RebillPreview {
  lines: RebillLine[];
  /** 选中但差额为 0 的事件数。 */
  unchanged_count: number;
  event_count: number;
  previous_total: string;
  corrected_total: string;
  difference: string;
  /** 没有可入账的差额时两者都为 `null`，页面据此禁用确认。 */
  ledger_type: string | null;
  ledger_amount: string | null;
  balance: string;
  /** `balance − difference`，只作提示。 */
  balance_after: string;
  /** 预览指纹，确认时原样带回。 */
  preview_token: string;
}

// --- 请求体 ---------------------------------------------------------------------

/** 显式事件：1 到 1000 个不重复的事件 `public_id`。 */
export interface RebillEventSelection {
  usage_event_ids: string[];
}

/** 条件：`[occurred_from, occurred_to)`，两个时间都是带时区的 RFC 3339；其余可选。 */
export interface RebillCriteriaSelection {
  occurred_from: string;
  occurred_to: string;
  project_id?: string | null;
  provider?: string | null;
  model?: string | null;
}

/** 两种形状二选一，不能混用。 */
export type RebillSelection = RebillEventSelection | RebillCriteriaSelection;

/** 预览的四个字段。两个更正版本省略或 `null` = 取当前有效版本。 */
export interface RebillRequest {
  customer_id: string;
  selection: RebillSelection;
  corrective_pricing_rule_id?: string | null;
  corrective_provider_price_version_id?: string | null;
}

/** 确认：预览的四个字段，加预览返回的指纹、原因与幂等键（小写 UUID，进入确认步骤时生成一次，重试沿用）。 */
export interface RebillConfirmRequest extends RebillRequest {
  preview_token: string;
  reason: string;
  idempotency_key: string;
}

export interface RebillListParams {
  customer_id?: string | undefined;
  cursor?: string | undefined;
  limit?: number | undefined;
}

// --- 查询键 ---------------------------------------------------------------------

export const REBILLS_QUERY_KEY = ["admin", "rebills"] as const;

export function rebillListQueryKey(params: RebillListParams) {
  return [
    ...REBILLS_QUERY_KEY,
    "list",
    params.customer_id ?? null,
    params.cursor ?? null,
    params.limit ?? null,
  ] as const;
}

export function rebillDetailQueryKey(rebillId: string) {
  return [...REBILLS_QUERY_KEY, "detail", rebillId] as const;
}

// --- 地址与请求体 -----------------------------------------------------------------

function selectionFields(selection: RebillSelection): RebillSelection {
  if ("usage_event_ids" in selection) {
    return { usage_event_ids: [...selection.usage_event_ids] };
  }
  const criteria: RebillCriteriaSelection = {
    occurred_from: selection.occurred_from,
    occurred_to: selection.occurred_to,
  };
  for (const name of ["project_id", "provider", "model"] as const) {
    const value = selection[name];
    if (value !== undefined) {
      criteria[name] = value;
    }
  }
  return criteria;
}

function requestFields(body: RebillRequest): Required<RebillRequest> {
  return {
    customer_id: body.customer_id,
    selection: selectionFields(body.selection),
    corrective_pricing_rule_id: body.corrective_pricing_rule_id ?? null,
    corrective_provider_price_version_id: body.corrective_provider_price_version_id ?? null,
  };
}

async function post<T>(url: string, body: unknown): Promise<T> {
  try {
    const response = await client.post<ApiEnvelope<T>>(url, body);
    return unwrapEnvelope<T>(response.data);
  } catch (error) {
    throw toApiError(error);
  }
}

// --- 接口 -----------------------------------------------------------------------

/** 按 Rebill id 倒序（最新确认的在前），游标分页、不给总数。客户不存在时是空页，不报 404。 */
export function listRebills(params: RebillListParams, signal?: AbortSignal): Promise<RebillPage> {
  const query = new URLSearchParams();
  if (params.customer_id !== undefined) {
    query.set("customer_id", params.customer_id);
  }
  if (params.cursor !== undefined) {
    query.set("cursor", params.cursor);
  }
  if (params.limit !== undefined) {
    query.set("limit", String(params.limit));
  }
  const text = query.toString();
  return apiGet<RebillPage>(text === "" ? REBILLS_URL : `${REBILLS_URL}?${text}`, signal);
}

/** 一次 Rebill 的合计、更正版本与全部明细。只读。 */
export function getRebill(rebillId: string, signal?: AbortSignal): Promise<RebillDetail> {
  return apiGet<RebillDetail>(`${REBILLS_URL}/${rebillId}`, signal);
}

/** 不写库、不写审计。请求体恰好四个字段。 */
export function previewRebill(body: RebillRequest): Promise<RebillPreview> {
  return post<RebillPreview>(PREVIEW_URL, requestFields(body));
}

/** 201 首次 / 200 重放：写恰好一行 `REBILL_*` 账本、明细与一条审计。预览之后数据变了是 409 `REBILL_PREVIEW_STALE`。 */
export function confirmRebill(body: RebillConfirmRequest): Promise<RebillConfirmation> {
  return post<RebillConfirmation>(REBILLS_URL, {
    ...requestFields(body),
    preview_token: body.preview_token,
    reason: body.reason,
    idempotency_key: body.idempotency_key,
  });
}
