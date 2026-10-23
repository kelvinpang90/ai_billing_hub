/**
 * 管理端门户账号（契约见 docs/api.md「管理端门户账号」，设计闸门 #368 v12 §2，AIH-TASK-094 实现）。
 *
 * 一个客户至多一个门户账号。六个接口都在 `/api/v1/admin/customers/{customer_id}/portal-account` 下，
 * 都要 ADMIN —— 前端不做角色判断，403 原样交给面板显示。状态跃迁规则全在后端，面板只按当前状态
 * 决定给哪些按钮，能不能做由后端的 409 说了算。
 *
 * ⚠️ 404 `PORTAL_ACCOUNT_NOT_FOUND` 在 GET 上是一个明确的答案（还没开通），{@link getPortalAccount}
 * 把它映射成 `null`，不当作错误。其余 404（`CUSTOMER_NOT_FOUND`）照常抛出。
 */

import { ApiError, apiGet, client, toApiError, unwrapEnvelope, type ApiEnvelope } from "./client";
import { customerDetailQueryKey } from "./adminCustomers";

const CUSTOMERS_URL = "/api/v1/admin/customers";

/** 客户存在，但还没有门户账号。 */
export const PORTAL_ACCOUNT_NOT_FOUND = "PORTAL_ACCOUNT_NOT_FOUND";
/** 开通时该客户已有门户账号。响应里不带邮箱，前端据此重读 GET。 */
export const PORTAL_ACCOUNT_EXISTS = "PORTAL_ACCOUNT_EXISTS";
/** 开通或重发改邮箱时，这个地址已被任何用户（包括 ADMIN）占用。 */
export const EMAIL_IN_USE = "EMAIL_IN_USE";
/** 客户已关户时开通、重发、启用。 */
export const ACCOUNT_CLOSED = "ACCOUNT_CLOSED";
/** 重发时账号不在 `INVITED`。 */
export const PORTAL_ACCOUNT_NOT_INVITED = "PORTAL_ACCOUNT_NOT_INVITED";
/** 停用一个已停用的账号。 */
export const PORTAL_ACCOUNT_ALREADY_DISABLED = "PORTAL_ACCOUNT_ALREADY_DISABLED";
/** 启用一个不在 `DISABLED` 的账号。 */
export const PORTAL_ACCOUNT_NOT_DISABLED = "PORTAL_ACCOUNT_NOT_DISABLED";
/** 重置 2FA 时，账号既没有已确认的 2FA，也没有待确认的注册。 */
export const PORTAL_2FA_NOT_ENABLED = "PORTAL_2FA_NOT_ENABLED";

export type PortalAccountStatus = "INVITED" | "ACTIVE" | "DISABLED";

/** 门户账号对象：恰好七个键。时间都是不带时区的 UTC（docs/api.md「时间」）。 */
export interface PortalAccount {
  /** 登录邮箱，已归一（去首尾空白、转小写）。与客户的联系邮箱是两回事，可以不同。 */
  email: string;
  status: PortalAccountStatus;
  /** 最近一次邀请的签发时刻；从未发过邀请时为 `null`。 */
  invited_at: string | null;
  /** 账号是 `INVITED` 且最近一次邀请未用、未过期时为它的到期时刻；否则 `null`（表示需要重发）。 */
  invitation_expires_at: string | null;
  activated_at: string | null;
  last_login_at: string | null;
  /** 是否有已确认的 2FA；只有待确认的注册时为 `false`。 */
  two_factor_enabled: boolean;
}

export function portalAccountQueryKey(customerId: string) {
  return [...customerDetailQueryKey(customerId), "portal-account"] as const;
}

function portalAccountUrl(customerId: string): string {
  // 与 adminCustomers.ts 的 customerUrl 同一个理由：id 来自地址栏，`a/b` 不转义就是另一条路径。
  return `${CUSTOMERS_URL}/${encodeURIComponent(customerId)}/portal-account`;
}

/** 有请求体就带上；没有时不传第二个参数（停用、启用没有请求体）。 */
async function post(url: string, body?: unknown): Promise<PortalAccount> {
  try {
    const response =
      body === undefined
        ? await client.post<ApiEnvelope<PortalAccount>>(url)
        : await client.post<ApiEnvelope<PortalAccount>>(url, body);
    return unwrapEnvelope<PortalAccount>(response.data);
  } catch (error) {
    throw toApiError(error);
  }
}

/** 这个客户的门户账号；还没开通（404 `PORTAL_ACCOUNT_NOT_FOUND`）时为 `null`。 */
export async function getPortalAccount(
  customerId: string,
  signal?: AbortSignal,
): Promise<PortalAccount | null> {
  try {
    return await apiGet<PortalAccount>(portalAccountUrl(customerId), signal);
  } catch (error) {
    if (error instanceof ApiError && error.code === PORTAL_ACCOUNT_NOT_FOUND) {
      return null;
    }
    throw error;
  }
}

/** 开通并发出邀请。⚠️ **不幂等**：第二次得到 409 `PORTAL_ACCOUNT_EXISTS`。防双击是调用方的责任。 */
export function createPortalAccount(customerId: string, email: string): Promise<PortalAccount> {
  return post(portalAccountUrl(customerId), { email });
}

/**
 * 重发邀请（只在 `INVITED` 时）：旧链接立即失效。不改邮箱时 `email` 不传，请求体是 `{}`；
 * 给了邮箱、归一后与当前相同，后端同样不算改。
 */
export function resendInvitation(customerId: string, email?: string): Promise<PortalAccount> {
  return post(`${portalAccountUrl(customerId)}/resend-invitation`, email === undefined ? {} : { email });
}

/** `INVITED` / `ACTIVE` → `DISABLED`。客户已关户时也允许。 */
export function disablePortalAccount(customerId: string): Promise<PortalAccount> {
  return post(`${portalAccountUrl(customerId)}/disable`);
}

/** `DISABLED` → `INVITED`（从未接受过邀请）或 `ACTIVE`。不自动重发邀请。 */
export function enablePortalAccount(customerId: string): Promise<PortalAccount> {
  return post(`${portalAccountUrl(customerId)}/enable`);
}

/**
 * 删除该账号的 2FA 与恢复码、使全部会话失效。⚠️ 调用前必须在线下核实对方确实是该客户。
 * `reason` 去掉首尾空白后 1–255 个字符，只写业务说明。
 */
export function resetPortalTwoFactor(customerId: string, reason: string): Promise<PortalAccount> {
  return post(`${portalAccountUrl(customerId)}/reset-2fa`, { reason });
}
