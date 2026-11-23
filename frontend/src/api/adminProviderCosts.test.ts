/**
 * 供应商成本对账请求发到哪个地址、用什么方法、带什么请求体（设计闸门 #755 v1 §7「前端 api」）。
 *
 * ⚠️ 金额全程是字符串：`source_total` 与期望来源成本原样发出，不经 number；预览的请求体恰好七个字段，不带 `expected_*`。
 *
 * uuid 一律是全零占位值。
 */

import { AxiosError, AxiosHeaders, type AxiosResponse } from "axios";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  SOURCE_TOTAL_PATTERN,
  confirmProviderCost,
  getProviderCostDetail,
  getProviderCostMonths,
  previewProviderCost,
  type ConfirmBody,
  type PreviewBody,
} from "./adminProviderCosts";
import { ApiError, client } from "./client";

const ID = "00000000-0000-0000-0000-000000000000";
const BASE = "/api/v1/admin/provider-cost-reconciliations";

function envelope(data: unknown) {
  return { data: { success: true, data, error: null, request_id: "req-1" } };
}

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

const PREVIEW: PreviewBody = {
  provider_code: "anthropic",
  billing_month: "2026-12",
  source_currency: "USD",
  // 8 位小数：走一遍浮点数就不是它了。
  source_total: "123456789.10000001",
  source_document_reference: "Console cost, platform workspace, 2026-12",
  supersedes_id: null,
  reason: null,
};

afterEach(() => {
  vi.restoreAllMocks();
});

describe("months overview", () => {
  it("asks for the months of one provider with provider_code in the query", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope({ items: [] }));

    expect(await getProviderCostMonths("anthropic")).toEqual({ items: [] });

    expect(get).toHaveBeenCalledWith(`${BASE}/months?provider_code=anthropic`, {});
  });

  it("passes the abort signal through", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope({ items: [] }));
    const controller = new AbortController();

    await getProviderCostMonths("anthropic", controller.signal);

    expect(get).toHaveBeenCalledWith(`${BASE}/months?provider_code=anthropic`, { signal: controller.signal });
  });

  it("keeps the HTTP error code and request id of a 404 AI_PROVIDER_NOT_FOUND", async () => {
    vi.spyOn(client, "get").mockRejectedValue(
      rejection(404, "AI_PROVIDER_NOT_FOUND", "AI provider not found.", "req-404"),
    );

    const failure = getProviderCostMonths("anthropic");

    await expect(failure).rejects.toBeInstanceOf(ApiError);
    await expect(failure).rejects.toMatchObject({
      code: "AI_PROVIDER_NOT_FOUND",
      message: "AI provider not found.",
      requestId: "req-404",
    });
  });
});

describe("preview", () => {
  it("posts exactly the seven fields, with the amount as the exact string typed", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope({ reconciliation_id: null }));

    await previewProviderCost(PREVIEW);

    expect(post).toHaveBeenCalledWith(`${BASE}/preview`, PREVIEW);
    const sent = post.mock.calls[0]?.[1] as Record<string, unknown>;
    expect(Object.keys(sent).sort()).toEqual(
      [
        "billing_month",
        "provider_code",
        "reason",
        "source_currency",
        "source_document_reference",
        "source_total",
        "supersedes_id",
      ].sort(),
    );
    expect(typeof sent.source_total).toBe("string");
    expect(sent.source_total).toBe("123456789.10000001");
  });

  it("never sends expected_* even when the caller's object carries them", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope({ reconciliation_id: null }));
    const withExpected: ConfirmBody = {
      ...PREVIEW,
      expected_event_count: 2,
      expected_estimated_source_cost: "10.50000000",
    };

    await previewProviderCost(withExpected);

    const sent = post.mock.calls[0]?.[1] as Record<string, unknown>;
    expect(sent).not.toHaveProperty("expected_event_count");
    expect(sent).not.toHaveProperty("expected_estimated_source_cost");
  });

  it("sends a correction with supersedes_id and reason", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope({ reconciliation_id: null }));
    const correction = { ...PREVIEW, supersedes_id: ID, reason: "Invoice credit applied" };

    await previewProviderCost(correction);

    expect(post).toHaveBeenCalledWith(`${BASE}/preview`, correction);
  });
});

describe("confirm", () => {
  it("posts the seven fields plus the two expected values from the preview", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope({ reconciliation_id: ID }));
    const body: ConfirmBody = {
      ...PREVIEW,
      expected_event_count: 2,
      expected_estimated_source_cost: "10.50000000",
    };

    await confirmProviderCost(body);

    expect(post).toHaveBeenCalledWith(BASE, body);
    const sent = post.mock.calls[0]?.[1] as Record<string, unknown>;
    expect(Object.keys(sent)).toHaveLength(9);
    expect(typeof sent.source_total).toBe("string");
    expect(typeof sent.expected_estimated_source_cost).toBe("string");
    expect(sent.expected_event_count).toBe(2);
  });

  it("turns a 409 into an ApiError carrying the code and the request id", async () => {
    vi.spyOn(client, "post").mockRejectedValue(
      rejection(409, "RECONCILIATION_ESTIMATE_CHANGED", "The estimate changed.", "req-409"),
    );

    const failure = confirmProviderCost({
      ...PREVIEW,
      expected_event_count: 0,
      expected_estimated_source_cost: "0.00000000",
    });

    await expect(failure).rejects.toBeInstanceOf(ApiError);
    await expect(failure).rejects.toMatchObject({
      code: "RECONCILIATION_ESTIMATE_CHANGED",
      message: "The estimate changed.",
      requestId: "req-409",
    });
  });
});

describe("detail", () => {
  it("reads one record with its id placed in the path as given", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope({ chain: [] }));

    await getProviderCostDetail(ID);

    expect(get).toHaveBeenCalledWith(`${BASE}/${ID}`, {});
  });
});

describe("field rules", () => {
  it("allow up to 9 integer digits and 8 decimal places and nothing a float or a sign would bring", () => {
    expect(SOURCE_TOTAL_PATTERN.test("0")).toBe(true);
    expect(SOURCE_TOTAL_PATTERN.test("11.70")).toBe(true);
    expect(SOURCE_TOTAL_PATTERN.test("0.10000001")).toBe(true);
    expect(SOURCE_TOTAL_PATTERN.test("123456789.12345678")).toBe(true);
    expect(SOURCE_TOTAL_PATTERN.test("1234567890")).toBe(false);
    expect(SOURCE_TOTAL_PATTERN.test("1.123456789")).toBe(false);
    expect(SOURCE_TOTAL_PATTERN.test("-1")).toBe(false);
    expect(SOURCE_TOTAL_PATTERN.test("1e3")).toBe(false);
    expect(SOURCE_TOTAL_PATTERN.test(" 1")).toBe(false);
  });
});
