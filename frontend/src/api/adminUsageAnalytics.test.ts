/**
 * 用量分析请求发到哪个地址、带什么查询参数。
 *
 * ⚠️ 页面用例把 `getUsageAnalytics` mock 掉了，路径与参数名只在这里被看住。
 *
 * uuid 一律是全零占位值。
 */

import { AxiosError, AxiosHeaders, type AxiosResponse } from "axios";
import { afterEach, describe, expect, it, vi } from "vitest";

import { getUsageAnalytics } from "./adminUsageAnalytics";
import { ApiError, client } from "./client";

const ID = "00000000-0000-0000-0000-000000000000";
const ANALYTICS = "/api/v1/admin/usage-analytics";
const FROM = "2026-09-01T00:00:00Z";
const TO = "2026-10-01T00:00:00Z";

function envelope(data: unknown) {
  return { data: { success: true, data, error: null, request_id: "req-1" } };
}

const EMPTY_REPORT = { cost_basis: "estimated", items: [], page: 1, page_size: 20, total: 0 };

function rejection(status: number, code: string, message: string, requestId: string) {
  const response = {
    data: { success: false, data: null, error: { code, message }, request_id: requestId },
    status,
    statusText: "",
    headers: {},
    config: { headers: new AxiosHeaders() },
  } satisfies AxiosResponse;
  return new AxiosError("Request failed", "ERR_BAD_REQUEST", undefined, undefined, response);
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("usage analytics", () => {
  it("sends the required parameters and paging only by default", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_REPORT));

    expect(await getUsageAnalytics({ group_by: "provider", occurred_from: FROM, occurred_to: TO }, 1, 20)).toEqual(
      EMPTY_REPORT,
    );

    const query = new URLSearchParams({
      group_by: "provider",
      occurred_from: FROM,
      occurred_to: TO,
      page: "1",
      page_size: "20",
    });
    expect(get).toHaveBeenCalledWith(`${ANALYTICS}?${query.toString()}`, {});
  });

  it("adds every optional filter that is given, in a fixed order", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_REPORT));

    await getUsageAnalytics(
      {
        feature: "crm.contact_summary",
        model: "claude-x",
        provider: "anthropic",
        project_id: ID,
        customer_id: ID,
        occurred_to: TO,
        occurred_from: FROM,
        group_by: "model",
      },
      3,
      50,
    );

    const query = new URLSearchParams({
      group_by: "model",
      occurred_from: FROM,
      occurred_to: TO,
      page: "3",
      page_size: "50",
      customer_id: ID,
      project_id: ID,
      provider: "anthropic",
      model: "claude-x",
      feature: "crm.contact_summary",
    });
    expect(get).toHaveBeenCalledWith(`${ANALYTICS}?${query.toString()}`, {});
  });

  it("leaves out empty and blank optional filters instead of sending them", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_REPORT));

    await getUsageAnalytics(
      { group_by: "customer", occurred_from: FROM, occurred_to: TO, customer_id: "", provider: "   ", model: " m " },
      1,
      20,
    );

    const sent = String(get.mock.calls[0]?.[0]);
    const params = new URLSearchParams(sent.slice(sent.indexOf("?") + 1));
    expect(sent.startsWith(`${ANALYTICS}?`)).toBe(true);
    expect(params.has("customer_id")).toBe(false);
    expect(params.has("provider")).toBe(false);
    expect(params.has("project_id")).toBe(false);
    expect(params.has("feature")).toBe(false);
    expect(params.get("model")).toBe("m");
  });

  it("passes decimal strings through untouched", async () => {
    const report = {
      ...EMPTY_REPORT,
      items: [
        {
          group_key: "anthropic",
          group_name: null,
          event_count: 3,
          token_event_count: 3,
          input_tokens: 10,
          output_tokens: 5,
          cache_creation_input_tokens: 0,
          cache_read_input_tokens: 0,
          average_input_tokens: "3.333333333333333333333333333",
          average_output_tokens: "1.666666666666666666666666667",
          prepaid: {
            event_count: 3,
            billable_cost: "0.10000001",
            estimated_provider_cost_myr: "0.00000001",
            gross_margin: "0.10000000",
            gross_margin_rate: "0.9999999000000009999999",
          },
          internal_metered_only: {
            event_count: 0,
            estimated_provider_cost_myr: "0.00000000",
            reference_customer_price: "0.00000000",
          },
        },
      ],
      total: 1,
    };
    vi.spyOn(client, "get").mockResolvedValue(envelope(report));

    const result = await getUsageAnalytics({ group_by: "provider", occurred_from: FROM, occurred_to: TO }, 1, 20);

    expect(result).toEqual(report);
    expect(typeof result.items[0]?.prepaid.billable_cost).toBe("string");
    expect(typeof result.items[0]?.prepaid.gross_margin_rate).toBe("string");
    expect(typeof result.items[0]?.average_input_tokens).toBe("string");
  });

  it("turns a 403 into an ApiError carrying the code and the request id", async () => {
    vi.spyOn(client, "get").mockRejectedValue(
      rejection(403, "ADMIN_REQUIRED", "Administrator access is required.", "req-403"),
    );

    const failure = getUsageAnalytics({ group_by: "provider", occurred_from: FROM, occurred_to: TO }, 1, 20);

    await expect(failure).rejects.toBeInstanceOf(ApiError);
    await expect(failure).rejects.toMatchObject({ code: "ADMIN_REQUIRED", requestId: "req-403" });
  });
});
