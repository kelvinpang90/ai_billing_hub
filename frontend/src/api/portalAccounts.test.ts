/**
 * 门户账号请求发到哪个地址、带什么请求体。
 *
 * 面板用例把整个 `api/portalAccounts` 模块 mock 掉了，路径与字段名写错时它们照样全绿 ——
 * 与 `adminCustomers.test.ts` 记的是同一个盲区。
 */

import { AxiosError, AxiosHeaders, type AxiosResponse } from "axios";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, client } from "./client";
import {
  createPortalAccount,
  disablePortalAccount,
  enablePortalAccount,
  getPortalAccount,
  resendInvitation,
  resetPortalTwoFactor,
  type PortalAccount,
} from "./portalAccounts";

const CUSTOMER_ID = "00000000-0000-0000-0000-000000000000";
const BASE = `/api/v1/admin/customers/${CUSTOMER_ID}/portal-account`;

const ACCOUNT: PortalAccount = {
  email: "portal@example.com",
  status: "INVITED",
  invited_at: "2026-10-23T08:00:00",
  invitation_expires_at: "2026-10-26T08:00:00",
  activated_at: null,
  last_login_at: null,
  two_factor_enabled: false,
};

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

afterEach(() => {
  vi.restoreAllMocks();
});

describe("portal account requests", () => {
  it("reads the portal account of the customer in the path", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(ACCOUNT));

    const account = await getPortalAccount(CUSTOMER_ID);

    expect(get).toHaveBeenCalledWith(BASE, {});
    expect(account).toEqual(ACCOUNT);
  });

  it("reads 404 PORTAL_ACCOUNT_NOT_FOUND as 'not provisioned', not as an error", async () => {
    vi.spyOn(client, "get").mockRejectedValue(rejection(404, "PORTAL_ACCOUNT_NOT_FOUND"));

    await expect(getPortalAccount(CUSTOMER_ID)).resolves.toBeNull();
  });

  it("still throws when the customer itself does not exist", async () => {
    vi.spyOn(client, "get").mockRejectedValue(rejection(404, "CUSTOMER_NOT_FOUND", "req-404"));

    const error: unknown = await getPortalAccount(CUSTOMER_ID).catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ code: "CUSTOMER_NOT_FOUND", requestId: "req-404" });
  });

  it("creates the account with just the email", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope(ACCOUNT));

    await createPortalAccount(CUSTOMER_ID, "portal@example.com");

    expect(post).toHaveBeenCalledWith(BASE, { email: "portal@example.com" });
  });

  it("resends with an empty body when the email is not changed", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope(ACCOUNT));

    await resendInvitation(CUSTOMER_ID);

    expect(post).toHaveBeenCalledWith(`${BASE}/resend-invitation`, {});
  });

  it("resends with the corrected email", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope(ACCOUNT));

    await resendInvitation(CUSTOMER_ID, "corrected@example.com");

    expect(post).toHaveBeenCalledWith(`${BASE}/resend-invitation`, {
      email: "corrected@example.com",
    });
  });

  it("disables and enables without a request body", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope(ACCOUNT));

    await disablePortalAccount(CUSTOMER_ID);
    await enablePortalAccount(CUSTOMER_ID);

    expect(post).toHaveBeenNthCalledWith(1, `${BASE}/disable`);
    expect(post).toHaveBeenNthCalledWith(2, `${BASE}/enable`);
  });

  it("resets 2FA with the reason", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope(ACCOUNT));

    await resetPortalTwoFactor(CUSTOMER_ID, "Lost phone; identity confirmed by call-back.");

    expect(post).toHaveBeenCalledWith(`${BASE}/reset-2fa`, {
      reason: "Lost phone; identity confirmed by call-back.",
    });
  });

  it("escapes an id from the address bar in every path", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(ACCOUNT));
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope(ACCOUNT));
    const escaped = "/api/v1/admin/customers/a%2F..%2Fb/portal-account";

    await getPortalAccount("a/../b");
    await createPortalAccount("a/../b", "portal@example.com");
    await resendInvitation("a/../b");
    await disablePortalAccount("a/../b");
    await enablePortalAccount("a/../b");
    await resetPortalTwoFactor("a/../b", "Reason");

    expect(get).toHaveBeenCalledWith(escaped, {});
    expect(post.mock.calls.map((call) => call[0])).toEqual([
      escaped,
      `${escaped}/resend-invitation`,
      `${escaped}/disable`,
      `${escaped}/enable`,
      `${escaped}/reset-2fa`,
    ]);
  });

  it("turns a 409 into an ApiError carrying the code and request id", async () => {
    vi.spyOn(client, "post").mockRejectedValue(rejection(409, "EMAIL_IN_USE", "req-409"));

    const error: unknown = await createPortalAccount(CUSTOMER_ID, "taken@example.com").catch(
      (caught: unknown) => caught,
    );

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ code: "EMAIL_IN_USE", requestId: "req-409" });
  });
});
