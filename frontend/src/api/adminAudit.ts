/**
 * 管理端审计日志（契约见 docs/api.md「管理端审计日志」，AIH-TASK-022）。
 *
 * 只有一个只读接口。要 ADMIN，角色以后端数据库为准 —— 前端不做任何角色判断，
 * 403 原样交给页面显示。查看审计本身不写审计。
 *
 * 响应里没有审计行的 `id`，也没有任何内部自增 id：操作者是 `actor_email`，以用户为
 * 对象的审计 `entity_id` 为 `null`、改填 `entity_user_email`。
 */

import type { Page } from "./adminCustomers";
import { apiGet } from "./client";

const AUDIT_LOGS_URL = "/api/v1/admin/audit-logs";

/** 422 的错误码：查询参数不合法（时间格式、区间、用户类型带 `entity_id` 等）。 */
export const VALIDATION_ERROR = "VALIDATION_ERROR";

/**
 * 后端已知的动作值，顺序同 `app/models/auth.py` 的 `AuditAction`。
 *
 * ⚠️ 后端对 `action` 是枚举校验，不认识的值 422。那边加了新动作而这里没跟上，
 * 结果只是下拉里少一项（表格照样显示后端给的任何值）；反过来这里多写一个后端
 * 没有的值，选它就是一句 422。所以只抄那边已有的，不预先补 spec §66 里还没有写入方的。
 *
 * 这份清单不会再悄悄漂移：adminAudit.test.ts 直接读 `app/models/auth.py`、解析
 * `AuditAction` 的全部取值并与这里逐项比对（缺一个、多一个、顺序不同都失败）。
 * 后端加了动作，照声明顺序抄到这里即可。
 */
export const AUDIT_ACTIONS = [
  "LOGIN",
  "LOGIN_FAILED",
  "LOGOUT",
  "TOKEN_REUSED",
  "USER_CREATED",
  "TWO_FACTOR_ENABLED",
  "RECOVERY_CODES_REGENERATED",
  "PASSWORD_RESET_REQUESTED",
  "PASSWORD_RESET",
  "WALLET_ADJUSTMENT_POSTED",
  "TENANT_BILLING_STATUS_CHANGED",
  "CUSTOMER_CREATE",
  "PROJECT_CREATE",
  "CUSTOMER_UPDATE",
  "INTERNAL_BILLING_MODE_SET",
  "API_KEY_CREATE",
  "API_KEY_ROTATE",
  "API_KEY_REVOKE",
  "WEBHOOK_SECRET_ISSUE",
  "WEBHOOK_SECRET_ACTIVATE",
  "WEBHOOK_SECRET_RETIRE",
  "TENANT_ACCOUNT_STATUS_CHANGED",
  "USAGE_METER_TYPE_CREATE",
  "USAGE_METER_TYPE_UPDATE",
  "AI_PROVIDER_CREATE",
  "AI_PROVIDER_UPDATE",
  "AI_MODEL_CREATE",
  "AI_MODEL_UPDATE",
  "AI_MODEL_ALIAS_MAP",
  "AI_MODEL_ALIAS_RETIRE",
  "PROVIDER_PRICE_CREATE",
  "PROVIDER_PRICE_UPDATE",
  "PROVIDER_PRICE_DISCARD",
  "PROVIDER_PRICE_PUBLISH",
  "PROVIDER_PRICE_RETIRE",
  "PRICING_CREATE",
  "PRICING_UPDATE",
  "PRICING_PUBLISH",
  "PRICING_RETIRE",
  "PRICING_DISCARD",
  "FX_RATE_CREATE",
  "FX_RATE_UPDATE",
  "FX_RATE_DISCARD",
  "FX_RATE_PUBLISH",
  "FX_RATE_RETIRE",
  "USAGE_EVENT_SCOPE_MISMATCH",
  "USAGE_EVENT_IDEMPOTENCY_CONFLICT",
  "USAGE_EVENT_REQUEUE",
  "PROJECT_INTEGRATION_STATUS_CHANGED",
  "PROJECT_STATUS_WEBHOOK_SET",
  "AI_CLIENT_KEY_CREATE",
  "AI_CLIENT_KEY_REVOKE",
  "AI_USAGE_REQUEUE",
  "AI_USAGE_DEAD_ACK",
  "AI_USAGE_HELD_RELEASE",
  "AI_USAGE_HELD_DISCARD",
  "AI_USAGE_HELD_IMPORT",
  "AI_USAGE_REVIEW_ACK",
  "TAX_POLICY_CREATE",
  "TAX_POLICY_PUBLISH",
] as const;

/** 系统动作（计费状态跃迁）的 `actor_role`。 */
export const SYSTEM_ACTOR_ROLE = "SYSTEM";

/** 一条审计。时间是不带时区的 UTC，精确到秒（docs/api.md「时间」）。 */
export interface AuditLogEntry {
  created_at: string;
  action: string;
  /** 写入时操作者的角色；系统动作是 `SYSTEM`；不知道是谁时 `null`。 */
  actor_role: string | null;
  /** 没有操作者（登录失败时邮箱不存在、系统动作）或用户已不存在时 `null`。 */
  actor_email: string | null;
  entity_type: string | null;
  /** 对外 id；以用户为对象的审计上一律 `null`。 */
  entity_id: string | null;
  /** 只在以用户为对象的审计上有值。 */
  entity_user_email: string | null;
  ip_address: string | null;
  user_agent: string | null;
  reason: string | null;
  /** 已经解析好的对象（不是字符串）；没有时 `null`。 */
  before_state: Record<string, unknown> | null;
  after_state: Record<string, unknown> | null;
}

/** 查询参数名，顺序即查询串里的顺序。 */
const FILTER_PARAMS = [
  "action",
  "entity_type",
  "entity_id",
  "actor_email",
  "created_from",
  "created_to",
] as const;

/**
 * 筛选条件，全部可省略、同时给的按 AND 组合。
 *
 * `created_from`（含）/ `created_to`（不含）必须是**不带时区的 UTC**、精确到秒
 * （`2026-09-20T08:30:00`）—— 从吉隆坡时间换算见 components/DateTimeText.tsx 的
 * `displayTimeToUtc`。这一层不换算、不校验，后端 422 才是准绳。
 */
export type AuditLogFilters = Partial<Record<(typeof FILTER_PARAMS)[number], string>>;

export const AUDIT_LOGS_QUERY_KEY = ["admin", "audit-logs"] as const;

export function auditLogListQueryKey(filters: AuditLogFilters, page: number, pageSize: number) {
  return [...AUDIT_LOGS_QUERY_KEY, filters, page, pageSize] as const;
}

/**
 * 审计分页，最新在前（按审计行的自增 id 倒序，由后端决定，前端不重排）。
 *
 * ⚠️ 省略的、以及去掉首尾空白后为空的条件**不出现在查询串里**：后端对
 * `entity_type=` 这种空值是按空串精确匹配，结果是一张莫名其妙的空列表。
 */
export function listAuditLogs(
  filters: AuditLogFilters,
  page: number,
  pageSize: number,
  signal?: AbortSignal,
): Promise<Page<AuditLogEntry>> {
  const query = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
  for (const name of FILTER_PARAMS) {
    const value = filters[name]?.trim();
    if (value !== undefined && value !== "") {
      query.set(name, value);
    }
  }
  return apiGet<Page<AuditLogEntry>>(`${AUDIT_LOGS_URL}?${query.toString()}`, signal);
}
