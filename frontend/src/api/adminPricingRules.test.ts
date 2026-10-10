/**
 * 定价规则请求发到哪个地址、带什么查询参数与请求体。
 *
 * ⚠️ 页面用例把整个 `api/adminPricingRules` 模块的请求函数 mock 掉了（它们测的是界面），所以路径与
 * 字段名写错时它们照样全绿 —— 与 `adminProviderPrices.test.ts` 记的是同一个盲区。
 *
 * uuid 一律是全零占位值。
 */

import { AxiosError, AxiosHeaders, type AxiosResponse } from "axios";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  PRIORITY_SCOPES,
  SCOPE_FIELDS,
  createPricingRule,
  discardPricingRule,
  getPricingRule,
  listPricingRules,
  publishPricingRule,
  retirePricingRule,
  updatePricingRule,
  type CreatePricingRuleBody,
} from "./adminPricingRules";
import { ApiError, client } from "./client";

const ID = "00000000-0000-0000-0000-000000000000";
const RULES = "/api/v1/admin/pricing-rules";

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

describe("listing pricing rules", () => {
  it("sends only page and page_size when no filter is chosen", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));

    const page = await listPricingRules({}, 2, 50);

    expect(get).toHaveBeenCalledWith(`${RULES}?page=2&page_size=50`, {});
    expect(page).toEqual(EMPTY_PAGE);
  });

  it("adds every chosen filter: scope, customer, provider, model and status", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));

    await listPricingRules(
      {
        priority_scope: "CUSTOMER_PROVIDER_MODEL",
        customer_id: ID,
        provider_id: ID,
        model_id: ID,
        status: "PUBLISHED",
      },
      1,
      20,
    );

    expect(get).toHaveBeenCalledWith(
      `${RULES}?page=1&page_size=20&priority_scope=CUSTOMER_PROVIDER_MODEL&customer_id=${ID}` +
        `&provider_id=${ID}&model_id=${ID}&status=PUBLISHED`,
      {},
    );
  });

  it("leaves out an empty filter value instead of sending it", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));

    await listPricingRules({ customer_id: "" }, 1, 20);

    expect(get).toHaveBeenCalledWith(`${RULES}?page=1&page_size=20`, {});
  });

  it("passes the abort signal through", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));
    const controller = new AbortController();

    await listPricingRules({}, 1, 20, controller.signal);

    expect(get).toHaveBeenCalledWith(`${RULES}?page=1&page_size=20`, { signal: controller.signal });
  });
});

describe("drafts", () => {
  it("creates a MARKUP draft with the multiplier as the exact string typed and no other fields", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope({ id: ID }));
    const body: CreatePricingRuleBody = {
      priority_scope: "GLOBAL",
      strategy: "MARKUP",
      // 走一遍浮点数的话，这个值在第 8 位上已经不是它自己了。
      markup_multiplier: "1.10000001",
    };

    await createPricingRule(body);

    expect(post).toHaveBeenCalledWith(RULES, body);
    const sent = post.mock.calls[0]?.[1] as Record<string, unknown>;
    expect(Object.keys(sent).sort()).toEqual(["markup_multiplier", "priority_scope", "strategy"]);
    expect(sent.markup_multiplier).toBe("1.10000001");
  });

  it("creates a FIXED_RATE draft with the scope fields and the rates as strings", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope({ id: ID }));
    const body: CreatePricingRuleBody = {
      priority_scope: "CUSTOMER_PROVIDER_MODEL",
      customer_id: ID,
      provider_id: ID,
      model_id: ID,
      strategy: "FIXED_RATE",
      components: [
        { component_code: "OCR_PAGE", unit_quantity: "1000", rate_amount: "999999999999.99999999" },
      ],
    };

    await createPricingRule(body);

    expect(post).toHaveBeenCalledWith(RULES, body);
    const sent = post.mock.calls[0]?.[1] as CreatePricingRuleBody;
    expect(typeof sent.components?.[0]?.rate_amount).toBe("string");
    expect(sent.components?.[0]?.rate_amount).toBe("999999999999.99999999");
  });

  it("reads one rule and patches only the fields given", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope({ id: ID }));
    const patch = vi.spyOn(client, "patch").mockResolvedValue(envelope({ id: ID }));

    await getPricingRule(ID);
    await updatePricingRule(ID, { markup_multiplier: "2.5" });

    expect(get).toHaveBeenCalledWith(`${RULES}/${ID}`, {});
    expect(patch).toHaveBeenCalledWith(`${RULES}/${ID}`, { markup_multiplier: "2.5" });
  });

  it("escapes an id from the address bar instead of letting it change the path", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope({ id: ID }));

    await getPricingRule("a/../b");

    expect(get).toHaveBeenCalledWith(`${RULES}/a%2F..%2Fb`, {});
  });

  it("discards with an empty object as the body", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope({ id: ID }));

    await discardPricingRule(ID);

    expect(post).toHaveBeenCalledWith(`${RULES}/${ID}/discard`, {});
  });
});

describe("publishing and disabling", () => {
  it("publishes with an empty body when no time is given", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope({ id: ID }));

    await publishPricingRule(ID);

    // 不是 `{ effective_from: null }`：省略的可选参数不出现在请求体里。
    expect(post).toHaveBeenCalledWith(`${RULES}/${ID}/publish`, {});
  });

  it("publishes with the scheduled time exactly as given", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope({ id: ID }));

    await publishPricingRule(ID, "2026-10-01T00:00:00Z");

    expect(post).toHaveBeenCalledWith(`${RULES}/${ID}/publish`, { effective_from: "2026-10-01T00:00:00Z" });
  });

  it("disables with the reason", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope({ id: ID }));

    await retirePricingRule(ID, "Customer moved to a fixed price");

    expect(post).toHaveBeenCalledWith(`${RULES}/${ID}/retire`, { reason: "Customer moved to a fixed price" });
  });

  it("turns a 409 into an ApiError carrying the code and the request id", async () => {
    vi.spyOn(client, "post").mockRejectedValue(
      rejection(409, "PRICING_RULE_INCOMPLETE", "Missing components: LLM_CACHE_READ_TOKEN", "req-409"),
    );

    const failure = publishPricingRule(ID);

    await expect(failure).rejects.toBeInstanceOf(ApiError);
    await expect(failure).rejects.toMatchObject({
      code: "PRICING_RULE_INCOMPLETE",
      message: "Missing components: LLM_CACHE_READ_TOKEN",
      requestId: "req-409",
    });
  });
});

describe("scopes", () => {
  it("are the five levels of spec §16, highest first, with the backend's field combinations", () => {
    expect(PRIORITY_SCOPES).toEqual([
      "CUSTOMER_PROVIDER_MODEL",
      "CUSTOMER_PROVIDER",
      "CUSTOMER",
      "GLOBAL_PROVIDER_MODEL",
      "GLOBAL",
    ]);
    expect(SCOPE_FIELDS.CUSTOMER_PROVIDER_MODEL).toEqual({ customer: true, provider: true, model: true });
    expect(SCOPE_FIELDS.CUSTOMER_PROVIDER).toEqual({ customer: true, provider: true, model: false });
    expect(SCOPE_FIELDS.CUSTOMER).toEqual({ customer: true, provider: false, model: false });
    expect(SCOPE_FIELDS.GLOBAL_PROVIDER_MODEL).toEqual({ customer: false, provider: true, model: true });
    expect(SCOPE_FIELDS.GLOBAL).toEqual({ customer: false, provider: false, model: false });
  });
});
