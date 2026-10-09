/**
 * 供应商价格请求发到哪个地址、带什么查询参数与请求体。
 *
 * ⚠️ 页面用例把整个 `api/adminProviderPrices` 模块的请求函数 mock 掉了（它们测的是界面），所以路径与
 * 字段名写错时它们照样全绿 —— 与 `adminCatalog.test.ts` 记的是同一个盲区。
 *
 * uuid 一律是全零占位值。
 */

import { AxiosError, AxiosHeaders, type AxiosResponse } from "axios";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  CURRENCY_PATTERN,
  PRICE_DECIMAL_PATTERN,
  createProviderPrice,
  discardProviderPrice,
  getProviderPrice,
  listProviderPrices,
  publishProviderPrice,
  retireProviderPrice,
  updateProviderPrice,
} from "./adminProviderPrices";
import { ApiError, client } from "./client";

const ID = "00000000-0000-0000-0000-000000000000";
const PRICES = "/api/v1/admin/provider-prices";

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

describe("listing provider prices", () => {
  it("sends only page and page_size when no filter is chosen", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));

    const page = await listProviderPrices({}, 2, 50);

    expect(get).toHaveBeenCalledWith(`${PRICES}?page=2&page_size=50`, {});
    expect(page).toEqual(EMPTY_PAGE);
  });

  it("adds provider_id, model_id and status when they are chosen", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));

    await listProviderPrices({ provider_id: ID, model_id: ID, status: "PUBLISHED" }, 1, 20);

    expect(get).toHaveBeenCalledWith(
      `${PRICES}?page=1&page_size=20&provider_id=${ID}&model_id=${ID}&status=PUBLISHED`,
      {},
    );
  });

  it("leaves out an empty filter value instead of sending it", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));

    await listProviderPrices({ provider_id: "" }, 1, 20);

    expect(get).toHaveBeenCalledWith(`${PRICES}?page=1&page_size=20`, {});
  });

  it("passes the abort signal through", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));
    const controller = new AbortController();

    await listProviderPrices({}, 1, 20, controller.signal);

    expect(get).toHaveBeenCalledWith(`${PRICES}?page=1&page_size=20`, { signal: controller.signal });
  });
});

describe("drafts", () => {
  it("creates a draft with the amounts as the exact strings typed", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope({ id: ID }));
    const body = {
      provider_id: ID,
      model_id: ID,
      source_currency: "USD",
      source_reference: "Fictional price sheet, viewed 2026-09-29",
      components: [
        // 走一遍浮点数的话，这两个值在第 8 位上已经不是它们自己了。
        { component_code: "LLM_INPUT_TOKEN", unit_quantity: "1000000", rate_amount: "0.10000001" },
        {
          component_code: "LLM_OUTPUT_TOKEN",
          unit_quantity: "1000000",
          rate_amount: "999999999999.99999999",
          metadata: { tier: "standard" },
        },
      ],
    };

    await createProviderPrice(body);

    expect(post).toHaveBeenCalledWith(PRICES, body);
    const sent = post.mock.calls[0]?.[1] as typeof body;
    expect(typeof sent.components[0]?.rate_amount).toBe("string");
    expect(sent.components[0]?.rate_amount).toBe("0.10000001");
    expect(sent.components[1]?.rate_amount).toBe("999999999999.99999999");
  });

  it("reads one version and patches only the fields given", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope({ id: ID }));
    const patch = vi.spyOn(client, "patch").mockResolvedValue(envelope({ id: ID }));

    await getProviderPrice(ID);
    await updateProviderPrice(ID, { source_reference: "Updated sheet" });

    expect(get).toHaveBeenCalledWith(`${PRICES}/${ID}`, {});
    expect(patch).toHaveBeenCalledWith(`${PRICES}/${ID}`, { source_reference: "Updated sheet" });
  });

  it("escapes an id from the address bar instead of letting it change the path", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope({ id: ID }));

    await getProviderPrice("a/../b");

    expect(get).toHaveBeenCalledWith(`${PRICES}/a%2F..%2Fb`, {});
  });

  it("discards with an empty object as the body", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope({ id: ID }));

    await discardProviderPrice(ID);

    expect(post).toHaveBeenCalledWith(`${PRICES}/${ID}/discard`, {});
  });
});

describe("publishing and retiring", () => {
  it("publishes with an empty body when no time is given", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope({ id: ID }));

    await publishProviderPrice(ID);

    // 不是 `{ effective_from: null }`：省略的可选参数不出现在请求体里。
    expect(post).toHaveBeenCalledWith(`${PRICES}/${ID}/publish`, {});
  });

  it("publishes with the scheduled time exactly as given", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope({ id: ID }));

    await publishProviderPrice(ID, "2026-10-01T00:00:00Z");

    expect(post).toHaveBeenCalledWith(`${PRICES}/${ID}/publish`, {
      effective_from: "2026-10-01T00:00:00Z",
    });
  });

  it("retires with the reason", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope({ id: ID }));

    await retireProviderPrice(ID, "Provider changed its prices");

    expect(post).toHaveBeenCalledWith(`${PRICES}/${ID}/retire`, {
      reason: "Provider changed its prices",
    });
  });

  it("turns a 409 into an ApiError carrying the code and the request id", async () => {
    vi.spyOn(client, "post").mockRejectedValue(
      rejection(409, "PRICE_VERSION_INCOMPLETE", "Missing components: LLM_CACHE_READ_TOKEN", "req-409"),
    );

    const failure = publishProviderPrice(ID);

    await expect(failure).rejects.toBeInstanceOf(ApiError);
    await expect(failure).rejects.toMatchObject({
      code: "PRICE_VERSION_INCOMPLETE",
      message: "Missing components: LLM_CACHE_READ_TOKEN",
      requestId: "req-409",
    });
  });
});

describe("field rules", () => {
  it("match the backend's decimal and currency rules", () => {
    expect(PRICE_DECIMAL_PATTERN.test("1000000")).toBe(true);
    expect(PRICE_DECIMAL_PATTERN.test("999999999999.99999999")).toBe(true);
    expect(PRICE_DECIMAL_PATTERN.test("1.123456789")).toBe(false);
    expect(PRICE_DECIMAL_PATTERN.test("1000000000000")).toBe(false);
    expect(PRICE_DECIMAL_PATTERN.test("-1")).toBe(false);
    expect(PRICE_DECIMAL_PATTERN.test("1e3")).toBe(false);
    expect(PRICE_DECIMAL_PATTERN.test("1,000")).toBe(false);
    expect(CURRENCY_PATTERN.test("USD")).toBe(true);
    expect(CURRENCY_PATTERN.test("usd")).toBe(false);
  });
});
