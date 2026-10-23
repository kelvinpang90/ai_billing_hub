/**
 * The auth endpoints (spec §53, §54).
 *
 * 登录是**两步**：`/login` 用密码换一张 `pending_token`，`/login/totp` 用验证码
 * 换会话。ADMIN 的 2FA 是强制的，所以没注册过的管理员会先被送去 `/2fa/enrol`。
 */

import { AxiosError } from "axios";

import { ApiError, client, toApiError, unwrapEnvelope, type ApiEnvelope } from "./client";

export const STAGE_TOTP_REQUIRED = "TOTP_REQUIRED";
export const STAGE_ENROL_2FA = "ENROL_2FA";

export interface LoginResult {
  access_token: string | null;
  token_type: string | null;
  expires_in: number | null;
  stage: string | null;
  pending_token: string | null;
  recovery_codes_remaining: number | null;
}

export interface Enrolment {
  secret: string;
  otpauth_uri: string;
}

export interface RecoveryCodes {
  recovery_codes: string[];
}

async function post<T>(url: string, body?: unknown): Promise<T> {
  try {
    const response = await client.post<ApiEnvelope<T>>(url, body);
    return unwrapEnvelope<T>(response.data);
  } catch (error) {
    throw toApiError(error);
  }
}

export function login(email: string, password: string): Promise<LoginResult> {
  return post<LoginResult>("/api/v1/auth/login", { email, password });
}

export function submitSecondFactor(pendingToken: string, code: string): Promise<LoginResult> {
  return post<LoginResult>("/api/v1/auth/login/totp", {
    pending_token: pendingToken,
    code,
  });
}

export function startEnrolment(pendingToken: string): Promise<Enrolment> {
  return post<Enrolment>("/api/v1/auth/2fa/enrol", { pending_token: pendingToken });
}

export function confirmEnrolment(pendingToken: string, code: string): Promise<RecoveryCodes> {
  return post<RecoveryCodes>("/api/v1/auth/2fa/confirm", {
    pending_token: pendingToken,
    code,
  });
}

export function logout(): Promise<unknown> {
  return post<unknown>("/api/v1/auth/logout");
}

/**
 * 申请一封重置邮件。
 *
 * ⚠️ **无论邮箱存不存在，后端都回同一个 200 空响应**（还刻意补齐了耗时），
 * 这样这个不需要凭据的端点才不会变成用户枚举工具。调用方**不许**试图从结果
 * 里区分两种情况 —— 没有可区分的东西，任何「看起来能区分」的写法都是错觉。
 */
export function requestPasswordReset(email: string): Promise<unknown> {
  return post<unknown>("/api/v1/auth/password/forgot", { email });
}

/** 用邮件里那张令牌换一个新密码。这一个**会**明说令牌不对（见后端注释）。 */
export function resetPassword(token: string, newPassword: string): Promise<unknown> {
  return post<unknown>("/api/v1/auth/password/reset", {
    token,
    new_password: newPassword,
  });
}

/** 后端 `InvalidResetToken` 的错误码（`app/services/password_reset.py`）。 */
export const INVALID_RESET_TOKEN = "INVALID_RESET_TOKEN";

/** 接受邀请的错误码（设计 AIH-TASK-089 §2）：令牌不存在、已用、过期、用途不对，或账号不是 `INVITED`。 */
export const INVALID_INVITATION_TOKEN = "INVALID_INVITATION_TOKEN";
/** 接受邀请时租户已关户（409）。 */
export const ACCOUNT_CLOSED = "ACCOUNT_CLOSED";
/** 密码强度不合格（400），与重置密码同一个错误。 */
export const WEAK_PASSWORD = "WEAK_PASSWORD";
/**
 * **前端自己的**错误码：接受邀请接口回了 404。
 *
 * ⚠️ 这一页先于接口上线（设计 §8）：那段窗口里后端没有这条路由，框架的 404 走的是通用信封
 * `HTTP_ERROR`，与 405 等别的框架错误同码，`ApiError` 上又不带 HTTP 状态。所以由这一层按状态码
 * 换成一个能认出来的码，页面按「链接无效」处理，而不是把一句「Not Found」摆给客户。
 */
export const INVITATION_ENDPOINT_NOT_FOUND = "INVITATION_ENDPOINT_NOT_FOUND";

const HTTP_NOT_FOUND = 404;

/** 用邀请邮件里那张令牌给门户账号设密码（设计 §2）。成功后账号变为 `ACTIVE`，用现有登录即可。 */
export async function acceptInvitation(token: string, password: string): Promise<unknown> {
  try {
    const response = await client.post<ApiEnvelope<unknown>>("/api/v1/auth/invitation/accept", {
      token,
      password,
    });
    return unwrapEnvelope<unknown>(response.data);
  } catch (error) {
    const apiError = toApiError(error);
    if (error instanceof AxiosError && error.response?.status === HTTP_NOT_FOUND) {
      throw new ApiError(INVITATION_ENDPOINT_NOT_FOUND, apiError.message, apiError.requestId);
    }
    throw apiError;
  }
}
