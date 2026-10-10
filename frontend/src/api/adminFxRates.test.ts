/**
 * 汇率请求发到哪个地址、带什么查询参数与请求体。
 *
 * ⚠️ 页面用例把整个 `api/adminFxRates` 模块的请求函数 mock 掉了，路径与字段名只在这里被看住。
 *
 * uuid 一律是全零占位值。
 */

import { AxiosError, AxiosHeaders, type AxiosResponse } from "axios";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  FX_RATE_PATTERN,
  createFxRate,
  discardFxRate,
  getFxRate,
  listFxFetchAttempts,
  listFxRates,
  publishFxRate,
  retireFxRate,
  updateFxRate,
} from "./adminFxRates";
import { ApiError, client } from "./client";

const ID = "00000000-0000-0000-0000-000000000000";
const RATES = "/api/v1/admin/fx-rates";

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
  it("lists versions with page and page_size only by default", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));

    expect(await listFxRates({}, 1, 20)).toEqual(EMPTY_PAGE);

    expect(get).toHaveBeenCalledWith(`${RATES}?page=1&page_size=20`, {});
  });

  it("adds base_currency, status and source when they are chosen", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));

    await listFxRates({ base_currency: "USD", status: "DRAFT", source: "BNM" }, 3, 100);

    expect(get).toHaveBeenCalledWith(
      `${RATES}?page=3&page_size=100&base_currency=USD&status=DRAFT&source=BNM`,
      {},
    );
  });

  it("lists fetch attempts at their own path, filters only when given", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));

    await listFxFetchAttempts({}, 1, 20);
    await listFxFetchAttempts({ base_currency: "SGD", outcome: "FAILED" }, 2, 20);

    expect(get).toHaveBeenNthCalledWith(1, `${RATES}/fetch-attempts?page=1&page_size=20`, {});
    expect(get).toHaveBeenNthCalledWith(
      2,
      `${RATES}/fetch-attempts?page=2&page_size=20&base_currency=SGD&outcome=FAILED`,
      {},
    );
  });
});

describe("manual drafts", () => {
  it("creates a draft with the rate as the exact string typed", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope({ id: ID }));
    const body = {
      base_currency: "USD",
      // 10 位小数：走一遍浮点数就不是它了。
      rate: "12345678901234.1234567891",
      observed_at: "2026-09-29T04:00:00Z",
      source_reference: "Fictional bank notice, viewed 2026-09-29",
    };

    await createFxRate(body);

    expect(post).toHaveBeenCalledWith(RATES, body);
    const sent = post.mock.calls[0]?.[1] as typeof body;
    expect(typeof sent.rate).toBe("string");
    expect(sent.rate).toBe("12345678901234.1234567891");
  });

  it("reads one version and patches only the fields given", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope({ id: ID }));
    const patch = vi.spyOn(client, "patch").mockResolvedValue(envelope({ id: ID }));

    await getFxRate(ID);
    await updateFxRate(ID, { rate: "4.083" });

    expect(get).toHaveBeenCalledWith(`${RATES}/${ID}`, {});
    expect(patch).toHaveBeenCalledWith(`${RATES}/${ID}`, { rate: "4.083" });
  });

  it("discards with an empty object as the body", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope({ id: ID }));

    await discardFxRate(ID);

    expect(post).toHaveBeenCalledWith(`${RATES}/${ID}/discard`, {});
  });

  it("escapes an id instead of letting it change the path", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope({ id: ID }));

    await getFxRate("a/../b");

    expect(get).toHaveBeenCalledWith(`${RATES}/a%2F..%2Fb`, {});
  });
});

describe("publishing and retiring", () => {
  it("publishes with an empty body when no time is given, and with the time when it is", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope({ id: ID }));

    await publishFxRate(ID);
    await publishFxRate(ID, "2026-10-01T00:00:00Z");

    expect(post).toHaveBeenNthCalledWith(1, `${RATES}/${ID}/publish`, {});
    expect(post).toHaveBeenNthCalledWith(2, `${RATES}/${ID}/publish`, {
      effective_from: "2026-10-01T00:00:00Z",
    });
  });

  it("retires with the reason", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope({ id: ID }));

    await retireFxRate(ID, "Wrong rate entered");

    expect(post).toHaveBeenCalledWith(`${RATES}/${ID}/retire`, { reason: "Wrong rate entered" });
  });

  it("turns a 409 into an ApiError carrying the code and the request id", async () => {
    vi.spyOn(client, "patch").mockRejectedValue(
      rejection(409, "FX_RATE_NOT_EDITABLE", "BNM drafts cannot be edited.", "req-409"),
    );

    const failure = updateFxRate(ID, { rate: "4.1" });

    await expect(failure).rejects.toBeInstanceOf(ApiError);
    await expect(failure).rejects.toMatchObject({
      code: "FX_RATE_NOT_EDITABLE",
      message: "BNM drafts cannot be edited.",
      requestId: "req-409",
    });
  });
});

describe("field rules", () => {
  it("allow up to 10 decimal places and nothing a float or a sign would bring", () => {
    expect(FX_RATE_PATTERN.test("4.083")).toBe(true);
    expect(FX_RATE_PATTERN.test("1.1234567891")).toBe(true);
    expect(FX_RATE_PATTERN.test("1.12345678912")).toBe(false);
    expect(FX_RATE_PATTERN.test("123456789012345")).toBe(false);
    expect(FX_RATE_PATTERN.test("+4.1")).toBe(false);
    expect(FX_RATE_PATTERN.test("4.1e0")).toBe(false);
    expect(FX_RATE_PATTERN.test(" 4.1")).toBe(false);
  });
});
