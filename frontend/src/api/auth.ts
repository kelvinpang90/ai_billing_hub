/**
 * The auth endpoints (spec §53, §54).
 *
 * 登录是**两步**：`/login` 用密码换一张 `pending_token`，`/login/totp` 用验证码
 * 换会话。ADMIN 的 2FA 是强制的，所以没注册过的管理员会先被送去 `/2fa/enrol`。
 */

import { client, toApiError, unwrapEnvelope, type ApiEnvelope } from "./client";

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
