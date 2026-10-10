/**
 * 试算请求发到哪个地址、请求体是什么 JSON 文本。
 *
 * 请求体是手写的 JSON 文本（token 要发 JSON 整数，而前端的数量不转成 number），所以这里直接比对发出去的
 * 那一串，再 `JSON.parse` 看后端会读到什么。uuid 一律是全零占位值。
 */

import { AxiosError, AxiosHeaders, type AxiosResponse } from "axios";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  INTEGER_QUANTITY_PATTERN,
  QUANTITY_PATTERN,
  canonicalTokenCount,
  previewPricing,
  previewRequestJson,
  type QuantityPreviewRequest,
  type TokenPreviewRequest,
} from "./adminPricingPreview";
import { ApiError, client } from "./client";

const ID = "00000000-0000-0000-0000-000000000000";
const PREVIEW = "/api/v1/admin/pricing-preview";
const JSON_HEADERS = { headers: { "Content-Type": "application/json" } };

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

const TOKENS: TokenPreviewRequest = {
  customer_id: ID,
  provider: "anthropic",
  model: "claude-x",
  usage_type: "LLM_TOKEN",
  input_tokens: "1000000000000",
  output_tokens: "0",
  cache_creation_input_tokens: "007",
  cache_read_input_tokens: "3000",
  occurred_at: "2026-10-01T00:00:00Z",
};

const QUANTITY: QuantityPreviewRequest = {
  customer_id: ID,
  provider: "openai",
  model: "whisper-1",
  usage_type: "AUDIO_SECOND",
  quantity: "12.50000001",
  unit: "SECOND",
  occurred_at: "2026-10-01T08:00:00+08:00",
};

afterEach(() => {
  vi.restoreAllMocks();
});

describe("previewing a token event", () => {
  it("posts the four token counts as JSON integers written from the digits typed", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope({ status: "PRICED" }));

    await previewPricing(TOKENS);

    const expected =
      `{"customer_id":"${ID}","provider":"anthropic","model":"claude-x","usage_type":"LLM_TOKEN",` +
      `"input_tokens":1000000000000,"output_tokens":0,"cache_creation_input_tokens":7,` +
      `"cache_read_input_tokens":3000,"occurred_at":"2026-10-01T00:00:00Z"}`;
    expect(post).toHaveBeenCalledWith(PREVIEW, expected, JSON_HEADERS);
    expect(JSON.parse(expected)).toEqual({
      customer_id: ID,
      provider: "anthropic",
      model: "claude-x",
      usage_type: "LLM_TOKEN",
      input_tokens: 1000000000000,
      output_tokens: 0,
      cache_creation_input_tokens: 7,
      cache_read_input_tokens: 3000,
      occurred_at: "2026-10-01T00:00:00Z",
    });
  });

  it("sends nothing when a token count is not a whole number in range", async () => {
    const post = vi.spyOn(client, "post");

    const failure = previewPricing({ ...TOKENS, output_tokens: "1000000000001" });

    await expect(failure).rejects.toBeInstanceOf(ApiError);
    await expect(failure).rejects.toMatchObject({ code: "VALIDATION_ERROR", requestId: null });
    expect(post).not.toHaveBeenCalled();
  });

  it("never splices arbitrary text into the JSON", () => {
    expect(() => previewRequestJson({ ...TOKENS, input_tokens: '1,"x":2' })).toThrow(ApiError);
    expect(() => previewRequestJson({ ...TOKENS, input_tokens: "1.5" })).toThrow(ApiError);
    expect(() => previewRequestJson({ ...TOKENS, input_tokens: "" })).toThrow(ApiError);
  });
});

describe("previewing a quantity event", () => {
  it("posts quantity and unit as JSON strings, exactly as typed", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope({ status: "PRICED" }));

    await previewPricing(QUANTITY);

    const sent: unknown = post.mock.calls[0]?.[1];
    expect(sent).toBe(
      `{"customer_id":"${ID}","provider":"openai","model":"whisper-1","usage_type":"AUDIO_SECOND",` +
        `"quantity":"12.50000001","unit":"SECOND","occurred_at":"2026-10-01T08:00:00+08:00"}`,
    );
    const parsed = JSON.parse(typeof sent === "string" ? sent : "") as Record<string, unknown>;
    expect(parsed.quantity).toBe("12.50000001");
    expect(Object.keys(parsed)).not.toContain("input_tokens");
  });

  it("escapes string fields instead of letting them break the JSON", () => {
    const text = previewRequestJson({ ...QUANTITY, model: 'a"b' });

    expect((JSON.parse(text) as Record<string, unknown>).model).toBe('a"b');
  });
});

describe("responses", () => {
  it("returns the result with every amount still a string", async () => {
    const result = {
      status: "PRICED",
      error_code: null,
      components: [],
      billable_cost_unrounded: "0.15733333333176",
      billable_cost: "0.15733333",
      tax_inclusive: true,
    };
    vi.spyOn(client, "post").mockResolvedValue(envelope(result));

    const preview = await previewPricing(QUANTITY);

    expect(preview).toEqual(result);
    expect(preview.billable_cost_unrounded).toBe("0.15733333333176");
  });

  it("turns a 404 into an ApiError carrying the code and the request id", async () => {
    vi.spyOn(client, "post").mockRejectedValue(
      rejection(404, "USAGE_METER_TYPE_NOT_FOUND", "The usage meter type does not exist.", "req-404"),
    );

    const failure = previewPricing(QUANTITY);

    await expect(failure).rejects.toMatchObject({
      code: "USAGE_METER_TYPE_NOT_FOUND",
      message: "The usage meter type does not exist.",
      requestId: "req-404",
    });
  });
});

describe("field rules", () => {
  it("accept token counts from 0 to 10^12 only, and write them without leading zeros", () => {
    expect(canonicalTokenCount("0")).toBe("0");
    expect(canonicalTokenCount("000")).toBe("0");
    expect(canonicalTokenCount("0042")).toBe("42");
    expect(canonicalTokenCount("1000000000000")).toBe("1000000000000");
    expect(canonicalTokenCount("1000000000001")).toBeNull();
    expect(canonicalTokenCount("9999999999999")).toBeNull();
    expect(canonicalTokenCount("-1")).toBeNull();
    expect(canonicalTokenCount("1e3")).toBeNull();
    expect(canonicalTokenCount(" 1")).toBeNull();
  });

  it("match the backend's quantity rules", () => {
    expect(QUANTITY_PATTERN.test("999999999999.99999999")).toBe(true);
    expect(QUANTITY_PATTERN.test("1.123456789")).toBe(false);
    expect(INTEGER_QUANTITY_PATTERN.test("3")).toBe(true);
    expect(INTEGER_QUANTITY_PATTERN.test("3.0")).toBe(false);
  });
});
