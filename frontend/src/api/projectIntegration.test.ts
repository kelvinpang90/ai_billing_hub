/**
 * 三个接口发到哪、带什么，以及写操作之后让哪些查询过期。
 *
 * ⚠️ 面板用例把这些函数 mock 掉了，路径与字段名写错时它们照样全绿；这里补上。
 * 地址示例一律用保留的虚构域名（`example.invalid`），uuid 是全零。
 */

import { AxiosError, AxiosHeaders, type AxiosResponse } from "axios";
import { afterEach, describe, expect, it, vi } from "vitest";

import { customerProjectsQueryKey, projectListQueryKey } from "./adminCustomers";
import { ApiError, client } from "./client";
import { credentialListQueryKey } from "./integrationAccess";
import {
  getStatusWebhook,
  isProjectListQueryKey,
  setIntegrationStatus,
  setStatusWebhook,
  statusWebhookQueryKey,
} from "./projectIntegration";
import { webhookSecretListQueryKey } from "./webhookSigning";

const CUSTOMER_ID = "00000000-0000-0000-0000-000000000000";
const PROJECT_ID = "00000000-0000-0000-0000-000000000000";
const ADDRESS = "https://hooks.example.invalid/acuven/receive";

const BASE = `/api/v1/admin/customers/${CUSTOMER_ID}/projects/${PROJECT_ID}`;

function project(overrides: Record<string, unknown> = {}) {
  return {
    id: PROJECT_ID,
    name: "Chatbot",
    description: null,
    integration_status: "DISABLED",
    status_webhook_configured: false,
    created_at: "2026-09-20T08:31:00",
    updated_at: "2026-10-12T08:31:00",
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

describe("setIntegrationStatus", () => {
  it("posts exactly the target status and the reason, and returns the project", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope(project()));

    const updated = await setIntegrationStatus(
      CUSTOMER_ID,
      PROJECT_ID,
      "DISABLED",
      "Integration paused at the customer's request",
    );

    expect(post).toHaveBeenCalledWith(`${BASE}/integration-status`, {
      integration_status: "DISABLED",
      reason: "Integration paused at the customer's request",
    });
    const sent = post.mock.calls[0]?.[1] as Record<string, unknown>;
    expect(Object.keys(sent).sort()).toEqual(["integration_status", "reason"]);
    expect(updated.integration_status).toBe("DISABLED");
  });

  it("escapes the ids in the path", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope(project()));

    await setIntegrationStatus("a/b", "c?d", "ENABLED", "Resume");

    expect(post).toHaveBeenCalledWith(
      "/api/v1/admin/customers/a%2Fb/projects/c%3Fd/integration-status",
      { integration_status: "ENABLED", reason: "Resume" },
    );
  });

  it("turns a 409 into an error that carries the code, backend message, request id and status", async () => {
    vi.spyOn(client, "post").mockRejectedValue(
      failure(409, "ACCOUNT_CLOSED", "The customer account is closed."),
    );

    const attempt = setIntegrationStatus(CUSTOMER_ID, PROJECT_ID, "ENABLED", "Resume");

    await expect(attempt).rejects.toBeInstanceOf(ApiError);
    await expect(attempt).rejects.toMatchObject({
      code: "ACCOUNT_CLOSED",
      message: "The customer account is closed.",
      requestId: "req-409",
      status: 409,
    });
  });

  it("reports no status when there was no response at all", async () => {
    vi.spyOn(client, "post").mockRejectedValue(failure(null));

    await expect(
      setIntegrationStatus(CUSTOMER_ID, PROJECT_ID, "ENABLED", "Resume"),
    ).rejects.toMatchObject({ code: "NETWORK_ERROR", status: null });
  });
});

describe("getStatusWebhook", () => {
  it("reads the project's status webhook address", async () => {
    const data = { status_webhook_url: ADDRESS, status_webhook_configured: true };
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(data));

    const address = await getStatusWebhook(CUSTOMER_ID, PROJECT_ID);

    expect(get).toHaveBeenCalledWith(`${BASE}/status-webhook`, {});
    expect(address).toEqual(data);
  });
});

describe("setStatusWebhook", () => {
  it("puts the address exactly as typed", async () => {
    const put = vi
      .spyOn(client, "put")
      .mockResolvedValue(
        envelope({ status_webhook_url: ADDRESS, status_webhook_configured: true }),
      );

    // 规范化（大小写等）只在后端做：前端原样发出。
    const typed = "HTTPS://Hooks.Example.Invalid/acuven/receive";
    const address = await setStatusWebhook(CUSTOMER_ID, PROJECT_ID, typed);

    expect(put).toHaveBeenCalledWith(`${BASE}/status-webhook`, { url: typed });
    const sent = put.mock.calls[0]?.[1] as Record<string, unknown>;
    expect(Object.keys(sent)).toEqual(["url"]);
    expect(address.status_webhook_url).toBe(ADDRESS);
  });

  it("clears the address with an explicit null rather than leaving the field out", async () => {
    const put = vi
      .spyOn(client, "put")
      .mockResolvedValue(envelope({ status_webhook_url: null, status_webhook_configured: false }));

    await setStatusWebhook(CUSTOMER_ID, PROJECT_ID, null);

    expect(put).toHaveBeenCalledWith(`${BASE}/status-webhook`, { url: null });
    const sent = put.mock.calls[0]?.[1] as Record<string, unknown>;
    expect("url" in sent).toBe(true);
  });

  it("turns a 422 into an error with the backend message, request id and status", async () => {
    vi.spyOn(client, "put").mockRejectedValue(
      failure(422, "VALIDATION_ERROR", "The status webhook address is not valid."),
    );

    const attempt = setStatusWebhook(CUSTOMER_ID, PROJECT_ID, "http://hooks.example.invalid/");

    await expect(attempt).rejects.toMatchObject({
      code: "VALIDATION_ERROR",
      message: "The status webhook address is not valid.",
      requestId: "req-422",
      status: 422,
    });
  });
});

describe("isProjectListQueryKey", () => {
  it("matches only this customer's project list pages", () => {
    expect(isProjectListQueryKey(CUSTOMER_ID, projectListQueryKey(CUSTOMER_ID, 1, 20))).toBe(true);
    expect(isProjectListQueryKey(CUSTOMER_ID, projectListQueryKey(CUSTOMER_ID, 3, 50))).toBe(true);

    const other = "another-customer";
    expect(isProjectListQueryKey(CUSTOMER_ID, projectListQueryKey(other, 1, 20))).toBe(false);
    // 同一前缀下的凭据、签名密钥与地址查询都不算。
    expect(isProjectListQueryKey(CUSTOMER_ID, customerProjectsQueryKey(CUSTOMER_ID))).toBe(false);
    expect(
      isProjectListQueryKey(CUSTOMER_ID, credentialListQueryKey(CUSTOMER_ID, PROJECT_ID, 1, 20)),
    ).toBe(false);
    expect(
      isProjectListQueryKey(CUSTOMER_ID, webhookSecretListQueryKey(CUSTOMER_ID, PROJECT_ID, 1, 20)),
    ).toBe(false);
    expect(isProjectListQueryKey(CUSTOMER_ID, statusWebhookQueryKey(CUSTOMER_ID, PROJECT_ID))).toBe(
      false,
    );
  });
});
