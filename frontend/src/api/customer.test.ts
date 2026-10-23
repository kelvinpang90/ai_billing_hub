/**
 * 客户门户接口发到哪里、带什么，以及两个 `/2fa/self/*` 接口自己处理的令牌过期。
 *
 * ⚠️ 要钉住的是**哪些 401 刷新、哪些不刷新**：`TOKEN_INVALID` 刷新一次并重试一次；`INVALID_CREDENTIALS`、
 * `INVALID_TOTP` 是业务结果，刷新就等于把一个打错的密码变成一次多余的令牌轮换。
 */

import { AxiosError, AxiosHeaders, type AxiosResponse } from "axios";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { getAccessToken, resetTokenStore, setAccessToken } from "../auth/tokenStore";
import { ApiError, client } from "./client";
import {
  CONFIRM_URL,
  ENROL_URL,
  confirmSelfEnrolment,
  getCustomerMe,
  startSelfEnrolment,
  type CustomerMe,
} from "./customer";

const refresh = vi.hoisted(() => ({ refreshAccessToken: vi.fn() }));

vi.mock("../auth/refresh", () => refresh);

const ME: CustomerMe = {
  customer_id: "00000000-0000-0000-0000-000000000000",
  company_name: "Example Sdn Bhd",
  account_status: "ENABLED",
  read_only: false,
};

const ENROLMENT = { secret: "JBSWY3DPEHPK3PXP", otpauth_uri: "otpauth://totp/Acuven:portal?secret=JBSWY3DPEHPK3PXP" };
const CODES = { recovery_codes: ["AAAA-BBBB-CCCC", "DDDD-EEEE-FFFF"] };

function envelope(data: unknown) {
  return { data: { success: true, data, error: null, request_id: "req-1" } };
}

function rejection(status: number, code: string, requestId = "req-err") {
  const response = {
    status,
    data: { success: false, data: null, error: { code, message: code }, request_id: requestId },
    statusText: "",
    headers: {},
    config: { headers: new AxiosHeaders() },
  } satisfies AxiosResponse;
  return new AxiosError("Request failed", "ERR_BAD_REQUEST", undefined, undefined, response);
}

beforeEach(() => {
  vi.clearAllMocks();
  resetTokenStore();
  setAccessToken("access-token-0");
});

afterEach(() => {
  vi.restoreAllMocks();
  resetTokenStore();
});

describe("getCustomerMe", () => {
  it("reads /api/v1/customer/me without any tenant parameter", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(ME));

    await expect(getCustomerMe()).resolves.toEqual(ME);

    expect(get).toHaveBeenCalledWith("/api/v1/customer/me", {});
  });

  it("throws the backend error with its request id", async () => {
    vi.spyOn(client, "get").mockRejectedValue(rejection(403, "CUSTOMER_REQUIRED", "req-403"));

    const error: unknown = await getCustomerMe().catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ code: "CUSTOMER_REQUIRED", requestId: "req-403" });
  });
});

describe("self enrolment", () => {
  it("re-verifies the password on /2fa/self/enrol", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope(ENROLMENT));

    await expect(startSelfEnrolment("a-customer-passphrase")).resolves.toEqual(ENROLMENT);

    expect(post).toHaveBeenCalledTimes(1);
    expect(post).toHaveBeenCalledWith(ENROL_URL, { password: "a-customer-passphrase" });
    expect(refresh.refreshAccessToken).not.toHaveBeenCalled();
  });

  it("submits the code on /2fa/self/confirm", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope(CODES));

    await expect(confirmSelfEnrolment("123456")).resolves.toEqual(CODES);

    expect(post).toHaveBeenCalledTimes(1);
    expect(post).toHaveBeenCalledWith(CONFIRM_URL, { code: "123456" });
  });

  it.each([
    ["enrol", () => startSelfEnrolment("a-customer-passphrase"), ENROLMENT],
    ["confirm", () => confirmSelfEnrolment("123456"), CODES],
  ])("%s: refreshes once on TOKEN_INVALID and retries once", async (_name, call, data) => {
    const post = vi
      .spyOn(client, "post")
      .mockRejectedValueOnce(rejection(401, "TOKEN_INVALID"))
      .mockResolvedValueOnce(envelope(data));
    refresh.refreshAccessToken.mockResolvedValue("access-token-1");

    await expect(call()).resolves.toEqual(data);

    expect(refresh.refreshAccessToken).toHaveBeenCalledTimes(1);
    expect(post).toHaveBeenCalledTimes(2);
    // 重试的是同一个请求、同一个请求体。
    expect(post.mock.calls[1]).toEqual(post.mock.calls[0]);
    expect(getAccessToken()).not.toBeNull();
  });

  it.each([
    ["enrol", () => startSelfEnrolment("a-customer-passphrase")],
    ["confirm", () => confirmSelfEnrolment("123456")],
  ])("%s: ends the session (sign-in page) when the refresh fails", async (_name, call) => {
    const post = vi.spyOn(client, "post").mockRejectedValue(rejection(401, "TOKEN_INVALID", "req-401"));
    refresh.refreshAccessToken.mockResolvedValue(null);

    const error: unknown = await call().catch((caught: unknown) => caught);

    expect(error).toMatchObject({ code: "TOKEN_INVALID", requestId: "req-401" });
    expect(post).toHaveBeenCalledTimes(1);
    // 令牌清空 = AuthProvider 变成 anonymous = RequireAuth 送去登录页。
    expect(getAccessToken()).toBeNull();
  });

  it.each([
    ["enrol", () => startSelfEnrolment("a-customer-passphrase")],
    ["confirm", () => confirmSelfEnrolment("123456")],
  ])("%s: ends the session when the retry is still TOKEN_INVALID, without a second refresh", async (_name, call) => {
    const post = vi.spyOn(client, "post").mockRejectedValue(rejection(401, "TOKEN_INVALID"));
    refresh.refreshAccessToken.mockResolvedValue("access-token-1");

    const error: unknown = await call().catch((caught: unknown) => caught);

    expect(error).toMatchObject({ code: "TOKEN_INVALID" });
    expect(refresh.refreshAccessToken).toHaveBeenCalledTimes(1);
    expect(post).toHaveBeenCalledTimes(2);
    expect(getAccessToken()).toBeNull();
  });

  it.each([
    ["a wrong password", () => startSelfEnrolment("wrong"), 401, "INVALID_CREDENTIALS"],
    ["a wrong code", () => confirmSelfEnrolment("000000"), 401, "INVALID_TOTP"],
    ["an existing enrolment", () => startSelfEnrolment("a-customer-passphrase"), 409, "ALREADY_ENROLLED"],
  ])("does not refresh on %s", async (_name, call, status, code) => {
    const post = vi.spyOn(client, "post").mockRejectedValue(rejection(status, code, "req-biz"));

    const error: unknown = await call().catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ code, requestId: "req-biz" });
    expect(refresh.refreshAccessToken).not.toHaveBeenCalled();
    expect(post).toHaveBeenCalledTimes(1);
    // 业务结果不结束会话。
    expect(getAccessToken()).toBe("access-token-0");
  });
});
