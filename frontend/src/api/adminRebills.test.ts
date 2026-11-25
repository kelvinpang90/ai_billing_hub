/**
 * Rebill 请求发到哪个地址、用什么方法、带什么请求体（docs/api.md「管理端 Rebill」）。
 *
 * ⚠️ 金额全程是字符串：回来的 8 位小数原样交给调用方，不经 number；预览与确认的请求体按字段逐个拼出，多带的字段不发。
 *
 * uuid 一律是全零占位值。
 */

import { AxiosError, AxiosHeaders, type AxiosResponse } from "axios";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  confirmRebill,
  getRebill,
  listRebills,
  previewRebill,
  type RebillConfirmRequest,
  type RebillRequest,
} from "./adminRebills";
import { ApiError, client } from "./client";

const ID = "00000000-0000-0000-0000-000000000000";
const BASE = "/api/v1/admin/rebills";
const TOKEN = "a".repeat(64);
const KEY = "00000000-0000-4000-8000-000000000000";

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

const REQUEST: RebillRequest = {
  customer_id: ID,
  selection: { usage_event_ids: [ID] },
  corrective_pricing_rule_id: null,
  corrective_provider_price_version_id: ID,
};

afterEach(() => {
  vi.restoreAllMocks();
});

describe("list", () => {
  it("asks for the first page without a cursor when nothing is given", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope({ items: [], next_cursor: null }));

    expect(await listRebills({})).toEqual({ items: [], next_cursor: null });

    expect(get).toHaveBeenCalledWith(BASE, {});
  });

  it("sends customer_id, cursor and limit in the query and passes the abort signal", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope({ items: [], next_cursor: null }));
    const controller = new AbortController();

    await listRebills({ customer_id: ID, cursor: "rebill-2", limit: 50 }, controller.signal);

    const [url, config] = get.mock.calls[0] ?? [];
    const parsed = new URL(String(url), "https://admin.example");
    expect(parsed.pathname).toBe(BASE);
    expect(Object.fromEntries(parsed.searchParams)).toEqual({ customer_id: ID, cursor: "rebill-2", limit: "50" });
    expect(config).toEqual({ signal: controller.signal });
  });

  it("hands the amounts back as the exact strings the server sent", async () => {
    const item = {
      id: ID,
      customer_id: ID,
      event_count: 2,
      difference: "-0.10000001",
      ledger_type: "REBILL_CREDIT",
      ledger_amount: "0.10000001",
      reason: "Price fix",
      created_by_email: "admin@example.com",
      created_at: "2026-11-20T03:00:00",
    };
    vi.spyOn(client, "get").mockResolvedValue(envelope({ items: [item], next_cursor: ID }));

    const page = await listRebills({});

    expect(page.next_cursor).toBe(ID);
    expect(page.items[0]?.ledger_amount).toBe("0.10000001");
    expect(typeof page.items[0]?.difference).toBe("string");
  });

  it("keeps the HTTP error code and request id of a 422", async () => {
    vi.spyOn(client, "get").mockRejectedValue(rejection(422, "VALIDATION_ERROR", "cursor", "req-422"));

    const failure = listRebills({ cursor: "unknown" });

    await expect(failure).rejects.toBeInstanceOf(ApiError);
    await expect(failure).rejects.toMatchObject({ code: "VALIDATION_ERROR", requestId: "req-422" });
  });
});

describe("detail", () => {
  it("reads one rebill with its id placed in the path as given", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope({ id: ID, lines: [] }));

    await getRebill(ID);

    expect(get).toHaveBeenCalledWith(`${BASE}/${ID}`, {});
  });

  it("turns a 404 into an ApiError carrying REBILL_NOT_FOUND", async () => {
    vi.spyOn(client, "get").mockRejectedValue(rejection(404, "REBILL_NOT_FOUND", "Rebill not found.", "req-404"));

    await expect(getRebill(ID)).rejects.toMatchObject({ code: "REBILL_NOT_FOUND", requestId: "req-404" });
  });
});

describe("preview", () => {
  it("posts exactly the four fields to the preview path", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope({ lines: [] }));

    await previewRebill(REQUEST);

    expect(post).toHaveBeenCalledWith(`${BASE}/preview`, REQUEST);
    const sent = post.mock.calls[0]?.[1] as Record<string, unknown>;
    expect(Object.keys(sent).sort()).toEqual(
      ["corrective_pricing_rule_id", "corrective_provider_price_version_id", "customer_id", "selection"].sort(),
    );
  });

  it("sends omitted corrective versions as null and a criteria selection with only the given keys", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope({ lines: [] }));

    await previewRebill({
      customer_id: ID,
      selection: { occurred_from: "2026-11-01T00:00:00+08:00", occurred_to: "2026-11-10T00:00:00+08:00", model: "m-1" },
    });

    expect(post).toHaveBeenCalledWith(`${BASE}/preview`, {
      customer_id: ID,
      selection: { occurred_from: "2026-11-01T00:00:00+08:00", occurred_to: "2026-11-10T00:00:00+08:00", model: "m-1" },
      corrective_pricing_rule_id: null,
      corrective_provider_price_version_id: null,
    });
  });

  it("never forwards confirm-only fields or anything else the caller's object carries", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope({ lines: [] }));
    const withExtra = { ...REQUEST, preview_token: TOKEN, reason: "x", idempotency_key: KEY, amount: "1.00000000" };

    await previewRebill(withExtra);

    const sent = post.mock.calls[0]?.[1] as Record<string, unknown>;
    expect(sent).not.toHaveProperty("preview_token");
    expect(sent).not.toHaveProperty("reason");
    expect(sent).not.toHaveProperty("idempotency_key");
    expect(sent).not.toHaveProperty("amount");
  });

  it("hands the totals back as strings", async () => {
    vi.spyOn(client, "post").mockResolvedValue(
      envelope({
        lines: [],
        unchanged_count: 0,
        event_count: 1,
        previous_total: "1.00000000",
        corrected_total: "1.20000001",
        difference: "0.20000001",
        ledger_type: "REBILL_DEBIT",
        ledger_amount: "-0.20000001",
        balance: "10.00000000",
        balance_after: "9.79999999",
        preview_token: TOKEN,
      }),
    );

    const preview = await previewRebill(REQUEST);

    expect(preview.ledger_amount).toBe("-0.20000001");
    expect(preview.balance_after).toBe("9.79999999");
  });
});

describe("confirm", () => {
  const body: RebillConfirmRequest = { ...REQUEST, preview_token: TOKEN, reason: "Price fix", idempotency_key: KEY };

  it("posts the four fields plus preview_token, reason and idempotency_key to the collection", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope({ id: ID, replayed: false }));

    await confirmRebill(body);

    expect(post).toHaveBeenCalledWith(BASE, body);
    const sent = post.mock.calls[0]?.[1] as Record<string, unknown>;
    expect(Object.keys(sent)).toHaveLength(7);
  });

  it("turns a 409 REBILL_PREVIEW_STALE into an ApiError carrying the code and the request id", async () => {
    vi.spyOn(client, "post").mockRejectedValue(rejection(409, "REBILL_PREVIEW_STALE", "Preview again.", "req-409"));

    const failure = confirmRebill(body);

    await expect(failure).rejects.toBeInstanceOf(ApiError);
    await expect(failure).rejects.toMatchObject({ code: "REBILL_PREVIEW_STALE", requestId: "req-409" });
  });
});
