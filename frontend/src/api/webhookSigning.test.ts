/**
 * 四个签名密钥接口发到哪、带什么，以及「结果未知」的判定。
 *
 * ⚠️ 面板用例把这些函数 mock 掉了，路径与字段名写错时它们照样全绿；这里补上。
 * 示例值一律是 docs/api.md 的全零占位值（`whs_` 加 64 个 0），uuid 也是全零，不是真实密钥。
 */

import { AxiosError, AxiosHeaders, type AxiosResponse } from "axios";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, client } from "./client";
import {
  WebhookSigningError,
  activateWebhookSecret,
  isOutcomeUnknown,
  issueWebhookSecret,
  listWebhookSecrets,
  retireWebhookSecret,
} from "./webhookSigning";

const CUSTOMER_ID = "00000000-0000-0000-0000-000000000000";
const PROJECT_ID = "00000000-0000-0000-0000-000000000000";
const SECRET = `whs_${"0".repeat(64)}`;

const BASE = `/api/v1/admin/customers/${CUSTOMER_ID}/projects/${PROJECT_ID}/webhook-secrets`;

function version(overrides: Record<string, unknown> = {}) {
  return {
    key_version: 1,
    status: "PENDING",
    created_at: "2026-09-28T08:30:00",
    activated_at: null,
    retired_at: null,
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

describe("listWebhookSecrets", () => {
  it("asks for the page of the project's signing versions", async () => {
    const get = vi
      .spyOn(client, "get")
      .mockResolvedValue(envelope({ items: [version()], page: 2, page_size: 10, total: 11 }));

    const page = await listWebhookSecrets(CUSTOMER_ID, PROJECT_ID, 2, 10);

    expect(get).toHaveBeenCalledWith(`${BASE}?page=2&page_size=10`, {});
    expect(page.items).toEqual([version()]);
  });
});

describe("issueWebhookSecret", () => {
  it("posts an empty object and hands the one-time secret back", async () => {
    const post = vi
      .spyOn(client, "post")
      .mockResolvedValue(envelope({ ...version(), secret: SECRET }, 201));

    const issued = await issueWebhookSecret(CUSTOMER_ID, PROJECT_ID);

    expect(post).toHaveBeenCalledWith(BASE, {});
    expect(issued.secret).toBe(SECRET);
    expect(issued.status).toBe("PENDING");
  });

  it("turns a 409 into an error that carries the code, backend message, request id and status", async () => {
    vi.spyOn(client, "post").mockRejectedValue(
      failure(409, "WEBHOOK_SECRET_PENDING_EXISTS", "A pending version already exists."),
    );

    const attempt = issueWebhookSecret(CUSTOMER_ID, PROJECT_ID);

    await expect(attempt).rejects.toBeInstanceOf(ApiError);
    await expect(attempt).rejects.toMatchObject({
      code: "WEBHOOK_SECRET_PENDING_EXISTS",
      message: "A pending version already exists.",
      requestId: "req-409",
      status: 409,
    });
  });

  it("reports no status when there was no response at all", async () => {
    vi.spyOn(client, "post").mockRejectedValue(failure(null));

    await expect(issueWebhookSecret(CUSTOMER_ID, PROJECT_ID)).rejects.toMatchObject({
      code: "NETWORK_ERROR",
      status: null,
    });
  });
});

describe("activateWebhookSecret", () => {
  it("posts an empty object to the version's activate path and returns every version", async () => {
    const all = [
      version({ status: "RETIRED", activated_at: "2026-09-28T09:00:00", retired_at: "2026-09-29T09:00:00" }),
      version({ key_version: 2, status: "ACTIVE", activated_at: "2026-09-29T09:00:00" }),
    ];
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope(all));

    const versions = await activateWebhookSecret(CUSTOMER_ID, PROJECT_ID, 2);

    expect(post).toHaveBeenCalledWith(`${BASE}/2/activate`, {});
    expect(versions).toEqual(all);
  });

  it.each([0, -1, 1.5, Number.NaN])("refuses to send a key version of %s", async (keyVersion) => {
    const post = vi.spyOn(client, "post");

    await expect(activateWebhookSecret(CUSTOMER_ID, PROJECT_ID, keyVersion)).rejects.toThrow(
      RangeError,
    );
    expect(post).not.toHaveBeenCalled();
  });
});

describe("retireWebhookSecret", () => {
  it("posts only the reason to the version's retire path", async () => {
    const post = vi
      .spyOn(client, "post")
      .mockResolvedValue(envelope(version({ key_version: 3, status: "RETIRED", retired_at: "2026-09-29T09:00:00" })));

    const retired = await retireWebhookSecret(CUSTOMER_ID, PROJECT_ID, 3, "Leaked in a support ticket");

    expect(post).toHaveBeenCalledWith(`${BASE}/3/retire`, { reason: "Leaked in a support ticket" });
    const sent = post.mock.calls[0]?.[1] as Record<string, unknown>;
    expect(Object.keys(sent)).toEqual(["reason"]);
    expect(retired.status).toBe("RETIRED");
  });

  it("refuses to send a key version that is not a positive integer", async () => {
    const post = vi.spyOn(client, "post");

    await expect(retireWebhookSecret(CUSTOMER_ID, PROJECT_ID, 0, "Unused")).rejects.toThrow(
      RangeError,
    );
    expect(post).not.toHaveBeenCalled();
  });
});

describe("isOutcomeUnknown", () => {
  it.each([400, 401, 403, 404, 409, 422])("is known after a %i: nothing was written", (status) => {
    expect(isOutcomeUnknown(new WebhookSigningError("X", "x", null, status))).toBe(false);
  });

  it.each(["ENCRYPTION_NOT_CONFIGURED", "DATABASE_NOT_CONFIGURED"])(
    "is known after a 503 %s",
    (code) => {
      expect(isOutcomeUnknown(new WebhookSigningError(code, "x", null, 503))).toBe(false);
    },
  );

  it.each([500, 502, 503, 504, 408, 429, 201])("is unknown after a %i", (status) => {
    expect(isOutcomeUnknown(new WebhookSigningError("X", "x", null, status))).toBe(true);
  });

  it("is unknown after a network error, or for an error that carries no status", () => {
    expect(isOutcomeUnknown(new WebhookSigningError("NETWORK_ERROR", "x", null, null))).toBe(true);
    expect(isOutcomeUnknown(new ApiError("WEBHOOK_SECRET_CONFLICT", "x", null))).toBe(true);
    expect(isOutcomeUnknown(new TypeError("boom"))).toBe(true);
  });
});
