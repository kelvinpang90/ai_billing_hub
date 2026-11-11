/**
 * 管理端支付设置（契约见 docs/api.md「支付设置（管理端）」，AIH-TASK-191；设计闸门 #587 v3 §2「管理端接口」「管理端页面」）。
 *
 * 两个接口：读取（`GET`）与整体替换（`PUT`）。支付设置是恰好一行的全局设置；要 ADMIN，角色以后端数据库为准 —— 前端不做
 * 任何角色判断，403 原样交给页面显示。页面在 features/payments/PaymentSettingsPage.tsx。
 *
 * ⚠️ 金额是**恰好两位小数的字符串**（不是 8 位），这一层不解析、不转浮点。`PUT` 是整体替换：请求体恰好是 6 个业务字段加
 * 读到的 `version`（乐观锁），多一个或少一个字段后端都是 422 `VALIDATION_ERROR`；版本对不上是 409。
 */

import { apiGet, client, toApiError, unwrapEnvelope, type ApiEnvelope } from "./client";

const PAYMENT_SETTINGS_URL = "/api/v1/admin/payment-settings";

// --- 错误码（docs/api.md「支付设置（管理端）」的「错误」） ----------------------------------

/** `PUT`：`version` 不是当前版本，设置已被别人修改（409）。重新读、在最新的值上再改。 */
export const PAYMENT_SETTINGS_VERSION_CONFLICT = "PAYMENT_SETTINGS_VERSION_CONFLICT";
/** `PUT`：某个字段不合规则（422）；`message` 点名第一个不合规的字段。 */
export const PAYMENT_SETTINGS_INVALID = "PAYMENT_SETTINGS_INVALID";

// --- 对象 -----------------------------------------------------------------------

/** 设置对象：恰好这 9 个键。时刻是不带时区的 UTC，精确到秒（docs/api.md「时间」）。 */
export interface PaymentSettings {
  /** 最低充值额，两位小数的字符串。 */
  min_amount: string;
  /** 最高充值额；`null` 表示不设（单笔上限仍是 `999999.99`）。 */
  max_amount: string | null;
  /** 最小恢复额的余额缓冲。 */
  recovery_buffer: string;
  /** 充值页的推荐金额，1–8 个、严格升序。 */
  preset_amounts: string[];
  /** 同额复用窗口（秒），0–300。 */
  reuse_window_seconds: number;
  /** 付款页有效期（秒），2400–86400。 */
  pending_timeout_seconds: number;
  /** 乐观锁版本，种子行为 1，每次修改 +1。 */
  version: number;
  updated_at: string;
  /** 最后一次修改的管理员邮箱；种子行（从未改过）为 `null`。 */
  updated_by: string | null;
}

/** `PUT` 的请求体：设置对象的前 7 个键（6 个业务字段加读到的 `version`）。 */
export type PaymentSettingsUpdate = Pick<
  PaymentSettings,
  | "min_amount"
  | "max_amount"
  | "recovery_buffer"
  | "preset_amounts"
  | "reuse_window_seconds"
  | "pending_timeout_seconds"
  | "version"
>;

// --- 查询键 ---------------------------------------------------------------------

export const PAYMENT_SETTINGS_QUERY_KEY = ["admin", "payment-settings"] as const;

// --- 接口 -----------------------------------------------------------------------

export function getPaymentSettings(signal?: AbortSignal): Promise<PaymentSettings> {
  return apiGet<PaymentSettings>(PAYMENT_SETTINGS_URL, signal);
}

/**
 * 整体替换。成功返回替换后的设置对象（与 {@link getPaymentSettings} 同样的 9 个键）。请求体只带这 7 个字段，调用方
 * 多给的字段不会被发出去（多余字段后端 422）。
 *
 * ⚠️ 这里**不自动重试**：409 要管理员刷新后在最新的值上再改。
 */
export async function updatePaymentSettings(body: PaymentSettingsUpdate): Promise<PaymentSettings> {
  try {
    const response = await client.put<ApiEnvelope<PaymentSettings>>(PAYMENT_SETTINGS_URL, {
      min_amount: body.min_amount,
      max_amount: body.max_amount,
      recovery_buffer: body.recovery_buffer,
      preset_amounts: body.preset_amounts,
      reuse_window_seconds: body.reuse_window_seconds,
      pending_timeout_seconds: body.pending_timeout_seconds,
      version: body.version,
    });
    return unwrapEnvelope<PaymentSettings>(response.data);
  } catch (error) {
    throw toApiError(error);
  }
}
