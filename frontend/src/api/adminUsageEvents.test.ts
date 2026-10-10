/**
 * 用量事件请求发到哪个地址、带什么查询参数与请求体。
 *
 * ⚠️ 页面用例把整个 `api/adminUsageEvents` 模块的请求函数 mock 掉了，路径与字段名只在这里被看住。
 *
 * uuid 一律是全零占位值。
 */

import { AxiosError, AxiosHeaders, type AxiosResponse } from "axios";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  bulkRequeueUsageEvents,
  getUsageEvent,
  isRequeuableStatus,
  listUsageEvents,
  recentInternalUsage,
  requeueUsageEvent,
} from "./adminUsageEvents";
import { ApiError, client } from "./client";

const ID = "00000000-0000-0000-0000-000000000000";
const EVENTS = "/api/v1/admin/usage-events";

function envelope(data: unknown) {
  return { data: { success: true, data, error: null, request_id: "req-1" } };
}

const EMPTY_PAGE = { items: [], page: 1, page_size: 20, total: 0 };

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

describe("listing", () => {
  it("lists events with page and page_size only by default", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));

    expect(await listUsageEvents({}, 1, 20)).toEqual(EMPTY_PAGE);

    expect(get).toHaveBeenCalledWith(`${EVENTS}?page=1&page_size=20`, {});
  });

  it("adds every filter that is given, in a fixed order", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));

    await listUsageEvents(
      {
        error_code: "NO_PRICE",
        status: "PRICING_ERROR",
        occurred_to: "2026-09-30T00:00:00Z",
        occurred_from: "2026-09-29T00:00:00Z",
        model: "whisper-x",
        provider: "openai",
        request_id: "call-1",
        conversation_id: "conv-1",
        project_id: ID,
        customer_id: ID,
      },
      3,
      100,
    );

    const query = new URLSearchParams({
      page: "3",
      page_size: "100",
      customer_id: ID,
      project_id: ID,
      conversation_id: "conv-1",
      request_id: "call-1",
      provider: "openai",
      model: "whisper-x",
      occurred_from: "2026-09-29T00:00:00Z",
      occurred_to: "2026-09-30T00:00:00Z",
      status: "PRICING_ERROR",
      error_code: "NO_PRICE",
    });
    expect(get).toHaveBeenCalledWith(`${EVENTS}?${query.toString()}`, {});
  });

  it("leaves out empty and blank filters instead of sending them", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));

    await listUsageEvents({ request_id: "", provider: "   ", model: " gpt-x " }, 1, 20);

    expect(get).toHaveBeenCalledWith(`${EVENTS}?page=1&page_size=20&model=gpt-x`, {});
  });

  it("keeps the customer panel's request as it was", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));

    await recentInternalUsage(ID);

    expect(get).toHaveBeenCalledWith(`${EVENTS}?customer_id=${ID}&page=1&page_size=20`, {});
  });
});

describe("detail", () => {
  it("reads one event by its public id, escaped", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope({ id: ID }));

    await getUsageEvent(ID);
    await getUsageEvent("a/../b");

    expect(get).toHaveBeenNthCalledWith(1, `${EVENTS}/${ID}`, {});
    expect(get).toHaveBeenNthCalledWith(2, `${EVENTS}/a%2F..%2Fb`, {});
  });

  it("passes decimal strings through untouched", async () => {
    const detail = {
      id: ID,
      quantity: "12345678901.12345678",
      billable_cost: "0.10000001",
      fx_rate_applied: "4.0830000001",
      gross_margin: "-0.00000001",
    };
    vi.spyOn(client, "get").mockResolvedValue(envelope(detail));

    const result = await getUsageEvent(ID);

    expect(result).toEqual(detail);
    expect(typeof result.quantity).toBe("string");
    expect(typeof result.billable_cost).toBe("string");
    expect(typeof result.fx_rate_applied).toBe("string");
    expect(typeof result.gross_margin).toBe("string");
  });
});

describe("requeue", () => {
  it("requeues one event with the reason as the only field", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope({ id: ID, status: "RECEIVED" }));

    expect(await requeueUsageEvent(ID, "Price published")).toEqual({ id: ID, status: "RECEIVED" });

    expect(post).toHaveBeenCalledWith(`${EVENTS}/${ID}/requeue`, { reason: "Price published" });
  });

  it("requeues in bulk at the fixed path with status and reason only by default", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope({ requeued: 0, skipped: 0, ids: [] }));

    await bulkRequeueUsageEvents({ status: "MODEL_UNKNOWN", reason: "Model added" });

    expect(post).toHaveBeenCalledWith(`${EVENTS}/requeue`, { status: "MODEL_UNKNOWN", reason: "Model added" });
  });

  it("adds only the optional conditions that are given and not blank", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope({ requeued: 2, skipped: 1, ids: [ID, ID] }));

    await bulkRequeueUsageEvents({
      status: "PRICING_ERROR",
      reason: "Price published",
      error_code: "NO_PRICE",
      customer_id: ID,
      provider: " openai ",
      model: "",
      occurred_from: "2026-09-29T00:00:00Z",
      occurred_to: "2026-09-30T00:00:00Z",
    });

    expect(post).toHaveBeenCalledWith(`${EVENTS}/requeue`, {
      status: "PRICING_ERROR",
      error_code: "NO_PRICE",
      customer_id: ID,
      provider: "openai",
      occurred_from: "2026-09-29T00:00:00Z",
      occurred_to: "2026-09-30T00:00:00Z",
      reason: "Price published",
    });
    const sent = post.mock.calls[0]?.[1] as Record<string, unknown>;
    expect(sent).not.toHaveProperty("model");
  });

  it("turns a 409 into an ApiError carrying the code and the request id", async () => {
    vi.spyOn(client, "post").mockRejectedValue(
      rejection(409, "USAGE_EVENT_NOT_REQUEUABLE", "The event cannot be requeued.", "req-409"),
    );

    const failure = requeueUsageEvent(ID, "Retry");

    await expect(failure).rejects.toBeInstanceOf(ApiError);
    await expect(failure).rejects.toMatchObject({
      code: "USAGE_EVENT_NOT_REQUEUABLE",
      message: "The event cannot be requeued.",
      requestId: "req-409",
    });
  });
});

describe("statuses", () => {
  it("treats only the four error statuses as requeuable", () => {
    expect(isRequeuableStatus("MODEL_UNKNOWN")).toBe(true);
    expect(isRequeuableStatus("PRICING_ERROR")).toBe(true);
    expect(isRequeuableStatus("FX_RATE_ERROR")).toBe(true);
    expect(isRequeuableStatus("FAILED_FINAL")).toBe(true);
    expect(isRequeuableStatus("PROCESSED")).toBe(false);
    expect(isRequeuableStatus("FAILED_RETRYABLE")).toBe(false);
    expect(isRequeuableStatus("IDEMPOTENCY_CONFLICT")).toBe(false);
  });
});
