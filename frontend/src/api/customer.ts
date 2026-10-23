/**
 * 客户门户自己的接口（契约见 docs/api.md「客户门户认证」，设计闸门 #368 v12 §2，AIH-TASK-097）。
 *
 * - `GET /api/v1/customer/me`：客户自己的摘要，恰好四个键。不带任何租户参数 —— 客户是谁只取自访问令牌（spec §90）。
 * - `POST /api/v1/auth/2fa/self/enrol`、`/2fa/self/confirm`：客户自己开启 2FA，收访问令牌（不是 pending 令牌）。
 *
 * ⚠️ 后两个在 `/api/v1/auth/` 下，`client.ts` 的拦截器对这个前缀的 401 **不刷新令牌**（那里的 401 多半是密码错、
 * 验证码错）。可这两个接口收的是访问令牌，用户在扫码、抄恢复码的时候它完全可能过期。所以由这里自己处理
 * `TOKEN_INVALID`：刷新一次、重试一次；刷新失败或重试仍是 `TOKEN_INVALID`，就是会话结束了，清掉令牌，
 * `RequireAuth` 随即把人送去登录页。`INVALID_CREDENTIALS`、`INVALID_TOTP`、409 是业务结果，原样抛给页面，不刷新。
 */

import { refreshAccessToken } from "../auth/refresh";
import { setAccessToken } from "../auth/tokenStore";
import type { Enrolment, RecoveryCodes } from "./auth";
import { ApiError, apiGet, client, toApiError, unwrapEnvelope, type ApiEnvelope } from "./client";

/** 访问令牌无效或过期（`app/core/tokens.py` 的 `InvalidToken`）。 */
export const TOKEN_INVALID = "TOKEN_INVALID";
/** 重验密码不对（401）。不计入登录锁定。 */
export const INVALID_CREDENTIALS = "INVALID_CREDENTIALS";
/** 验证码不对（401）。 */
export const INVALID_TOTP = "INVALID_TOTP";
/** 已经开启过 2FA（409），不重发密钥。 */
export const ALREADY_ENROLLED = "ALREADY_ENROLLED";
/** 确认时没有待确认的注册（409）。 */
export const NOT_ENROLLED = "NOT_ENROLLED";

export const ENROL_URL = "/api/v1/auth/2fa/self/enrol";
export const CONFIRM_URL = "/api/v1/auth/2fa/self/confirm";

/**
 * `GET /api/v1/customer/me` 的响应：恰好这四个键，没有金额、成本、毛利、内部 id、`billing_mode`（INV-7）。
 *
 * `account_status` 写成 `string`：后端将来多一种状态时，页面原样显示它，而不是假装只有这四种。
 */
export interface CustomerMe {
  /** 客户的 `public_id`。页面不显示它。 */
  customer_id: string;
  company_name: string;
  /** `PENDING_ACTIVATION` / `ENABLED` / `DISABLED` / `CLOSED`。 */
  account_status: string;
  /** `account_status` 为 `DISABLED` 或 `CLOSED` 时为 `true`：能读，客户写接口一律 403 `ACCOUNT_READ_ONLY`。 */
  read_only: boolean;
}

export const CUSTOMER_ME_QUERY_KEY = ["customer", "me"] as const;

export function getCustomerMe(signal?: AbortSignal): Promise<CustomerMe> {
  return apiGet<CustomerMe>("/api/v1/customer/me", signal);
}

async function post<T>(url: string, body: unknown): Promise<T> {
  try {
    const response = await client.post<ApiEnvelope<T>>(url, body);
    return unwrapEnvelope<T>(response.data);
  } catch (error) {
    throw toApiError(error);
  }
}

const isTokenInvalid = (error: unknown): error is ApiError =>
  error instanceof ApiError && error.code === TOKEN_INVALID;

/**
 * 发一个收访问令牌的 `/api/v1/auth/` 请求；`TOKEN_INVALID` 时刷新一次、重试一次。
 *
 * ⚠️ 至多重试一次，理由同拦截器的 `authRetried`：新令牌也不被接受，说明问题不在过期（被停用、改了密码、被重置
 * 2FA），再刷再试只会打转。重试那一次的令牌由请求拦截器现取，所以拿到的是刚刷新的那张。
 */
async function postWithSession<T>(url: string, body: unknown): Promise<T> {
  try {
    return await post<T>(url, body);
  } catch (error) {
    if (!isTokenInvalid(error)) {
      throw error;
    }
    const token = await refreshAccessToken(client);
    if (!token) {
      // 会话结束了。清掉令牌，`AuthProvider` 跟着变成 anonymous，`RequireAuth` 送去登录页。
      setAccessToken(null);
      throw error;
    }
  }
  try {
    return await post<T>(url, body);
  } catch (error) {
    if (isTokenInvalid(error)) {
      setAccessToken(null);
    }
    throw error;
  }
}

/**
 * 第一步：重验密码，拿回密钥与 `otpauth://` URI。
 *
 * ⚠️ 两样都**只返回这一次**，而且都是第二因子本身：调用方只把它们放在组件状态里，不进日志、不进任何存储。
 */
export function startSelfEnrolment(password: string): Promise<Enrolment> {
  return postWithSession<Enrolment>(ENROL_URL, { password });
}

/** 第二步：提交验证器上的六位数，拿回恢复码（同样只返回这一次）。 */
export function confirmSelfEnrolment(code: string): Promise<RecoveryCodes> {
  return postWithSession<RecoveryCodes>(CONFIRM_URL, { code });
}
