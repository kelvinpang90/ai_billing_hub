/**
 * 五个凭据接口发到哪、带什么，以及「结果未知」的判定。
 *
 * ⚠️ 面板用例把这些函数 mock 掉了，路径与字段名写错时它们照样全绿；这里补上。
 * 示例值一律是 docs/api.md 的全零占位值，不是真实凭据。
 */

import { AxiosError, AxiosHeaders, type AxiosResponse } from "axios";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, client } from "./client";
import {
  CredentialError,
  createCredential,
  isOutcomeUnknown,
  listCredentials,
  revokeCredential,
  revokeCredentialVersion,
  rotateCredential,
} from "./integrationAccess";

const CUSTOMER_ID = "00000000-0000-0000-0000-000000000000";
const PROJECT_ID = "00000000-0000-0000-0000-000000000000";
const API_KEY = `ak_${"0".repeat(32)}`;
const SECRET = `sk_${"0".repeat(64)}`;

const BASE = `/api/v1/admin/customers/${CUSTOMER_ID}/projects/${PROJECT_ID}/credentials`;

function version(overrides: Record<string, unknown> = {}) {
  return {
    api_key: API_KEY,
    key_version: 1,
    status: "ACTIVE",
    valid_from: "2026-09-25T08:30:00",
    valid_until: null,
    last_used_at: null,
    created_at: "2026-09-25T08:30:00",
    revoked_at: null,
    verifiable: true,
    ...overrides,
  };
}

function envelope(data: unknown, status = 200) {
  return { data: { success: true, data, error: null, request_id: "req-1" }, status };
}

function failure(status: number | null, code = "SOME_ERROR", message = "It failed.") {
  if (status === null) {
    return new AxiosError("timeout of 15000ms exceeded", "ECONNABORTED");
  }
  const response = {
    data: { success: false, data: null, error: { code, message }, request_id: `req-${status}` },
    status,
    statusText: "",
    headers: {},
    config: { headers: new AxiosHeaders() },
  } satisfies AxiosResponse;
  return new AxiosError("Request failed", "ERR_BAD_RESPONSE", undefined, undefined, response);
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("listCredentials", () => {
  it("asks for the page of the project's credentials", async () => {
    const get = vi
      .spyOn(client, "get")
      .mockResolvedValue(envelope({ items: [version()], page: 2, page_size: 10, total: 11 }));

    const page = await listCredentials(CUSTOMER_ID, PROJECT_ID, 2, 10);

    expect(get).toHaveBeenCalledWith(`${BASE}?page=2&page_size=10`, {});
    expect(page.items).toEqual([version()]);
  });

  it("escapes the customer and project ids instead of letting them change the path", async () => {
    const get = vi
      .spyOn(client, "get")
      .mockResolvedValue(envelope({ items: [], page: 1, page_size: 20, total: 0 }));

    await listCredentials("a/../b", "c/d", 1, 20);

    expect(get).toHaveBeenCalledWith(
      "/api/v1/admin/customers/a%2F..%2Fb/projects/c%2Fd/credentials?page=1&page_size=20",
      {},
    );
  });
});

describe("createCredential", () => {
  it("posts an empty object and hands the one-time secret back", async () => {
    const post = vi
      .spyOn(client, "post")
      .mockResolvedValue(envelope({ ...version(), secret: SECRET }, 201));

    const issued = await createCredential(CUSTOMER_ID, PROJECT_ID);

    expect(post).toHaveBeenCalledWith(BASE, {});
    expect(issued.secret).toBe(SECRET);
  });
});

describe("rotateCredential", () => {
  it("sends the current key version as a JSON integer", async () => {
    const post = vi
      .spyOn(client, "post")
      .mockResolvedValue(envelope({ ...version({ key_version: 3 }), secret: SECRET }, 201));

    await rotateCredential(CUSTOMER_ID, PROJECT_ID, API_KEY, 2);

    expect(post).toHaveBeenCalledWith(`${BASE}/${API_KEY}/rotate`, { current_key_version: 2 });
    const sent = post.mock.calls[0]?.[1] as { current_key_version: unknown };
    expect(typeof sent.current_key_version).toBe("number");
    expect(Number.isInteger(sent.current_key_version)).toBe(true);
  });

  it("turns a 409 into an ApiError that carries the backend message, request id and status", async () => {
    vi.spyOn(client, "post").mockRejectedValue(
      failure(409, "CREDENTIAL_VERSION_CONFLICT", "The key was rotated by someone else."),
    );

    const attempt = rotateCredential(CUSTOMER_ID, PROJECT_ID, API_KEY, 1);

    await expect(attempt).rejects.toBeInstanceOf(ApiError);
    await expect(attempt).rejects.toMatchObject({
      code: "CREDENTIAL_VERSION_CONFLICT",
      message: "The key was rotated by someone else.",
      requestId: "req-409",
      status: 409,
    });
  });

  it("reports no status when there was no response at all", async () => {
    vi.spyOn(client, "post").mockRejectedValue(failure(null));

    await expect(rotateCredential(CUSTOMER_ID, PROJECT_ID, API_KEY, 1)).rejects.toMatchObject({
      code: "NETWORK_ERROR",
      status: null,
    });
  });
});

describe("revokeCredentialVersion", () => {
  it("posts the reason to the version's revoke path", async () => {
    const post = vi
      .spyOn(client, "post")
      .mockResolvedValue(envelope(version({ key_version: 2, status: "REVOKED" })));

    const revoked = await revokeCredentialVersion(
      CUSTOMER_ID,
      PROJECT_ID,
      API_KEY,
      2,
      "Leaked in a support ticket",
    );

    expect(post).toHaveBeenCalledWith(`${BASE}/${API_KEY}/versions/2/revoke`, {
      reason: "Leaked in a support ticket",
    });
    expect(revoked.status).toBe("REVOKED");
  });
});

describe("revokeCredential", () => {
  it("posts the reason to the key's revoke path and returns every version", async () => {
    const all = [
      version({ status: "REVOKED" }),
      version({ key_version: 2, status: "REVOKED" }),
    ];
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope(all));

    const revoked = await revokeCredential(CUSTOMER_ID, PROJECT_ID, API_KEY, "Project retired");

    expect(post).toHaveBeenCalledWith(`${BASE}/${API_KEY}/revoke`, { reason: "Project retired" });
    expect(revoked).toEqual(all);
  });

  it("escapes the api key instead of letting it change the path", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope([]));

    await revokeCredential(CUSTOMER_ID, PROJECT_ID, "../x", "Project retired");

    expect(post).toHaveBeenCalledWith(`${BASE}/..%2Fx/revoke`, { reason: "Project retired" });
  });
});

describe("isOutcomeUnknown", () => {
  it.each([400, 401, 403, 404, 409, 422])("is known after a %i: nothing was written", (status) => {
    expect(isOutcomeUnknown(new CredentialError("X", "x", null, status))).toBe(false);
  });

  it.each(["ENCRYPTION_NOT_CONFIGURED", "DATABASE_NOT_CONFIGURED"])(
    "is known after a 503 %s",
    (code) => {
      expect(isOutcomeUnknown(new CredentialError(code, "x", null, 503))).toBe(false);
    },
  );

  it.each([500, 502, 503, 504, 408, 429, 201])("is unknown after a %i", (status) => {
    expect(isOutcomeUnknown(new CredentialError("X", "x", null, status))).toBe(true);
  });

  it("is unknown after a network error, or for an error that carries no status", () => {
    expect(isOutcomeUnknown(new CredentialError("NETWORK_ERROR", "x", null, null))).toBe(true);
    expect(isOutcomeUnknown(new ApiError("CREDENTIAL_REVOKED", "x", null))).toBe(true);
    expect(isOutcomeUnknown(new TypeError("boom"))).toBe(true);
  });
});
