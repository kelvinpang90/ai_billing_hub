/**
 * 客户钱包的两个请求发到哪个地址、带什么查询参数。
 *
 * 钱包页的用例把这个模块 mock 掉了，所以路径与参数名写错时它们照样全绿：`before_sequence` 打成
 * `beforeSequence` 在浏览器里不报错，只是「Older」永远停在最新一页。另外钉住：两个请求都**不带**任何租户或客户标识。
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { client } from "./client";
import {
  CUSTOMER_TRANSACTIONS_QUERY_KEY,
  customerTransactionListQueryKey,
  getCustomerWallet,
  listCustomerTransactions,
} from "./customerWallet";

const WALLET_URL = "/api/v1/customer/wallet";
const TRANSACTIONS_URL = "/api/v1/customer/wallet/transactions";

const WALLET = {
  currency: "MYR",
  balance: "12.45880000",
  billing_mode: "PREPAID",
  billing_status: "ACTIVE",
  ai_service: "ALLOW_AI",
  ai_service_reason: null,
  low_balance_threshold: "50.00000000",
  low_balance: true,
};

const EMPTY_PAGE = { items: [], page_size: 20, next_before_sequence: null };

function envelope(data: unknown) {
  return { data: { success: true, data, error: null, request_id: "req-1" } };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("getCustomerWallet", () => {
  it("reads /api/v1/customer/wallet without any query string", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(WALLET));

    await expect(getCustomerWallet()).resolves.toEqual(WALLET);

    expect(get).toHaveBeenCalledWith(WALLET_URL, {});
  });

  it("passes the abort signal through", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(WALLET));
    const controller = new AbortController();

    await getCustomerWallet(controller.signal);

    expect(get).toHaveBeenCalledWith(WALLET_URL, { signal: controller.signal });
  });
});

describe("listCustomerTransactions", () => {
  it("leaves every omitted parameter out of the query string", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));

    const page = await listCustomerTransactions();

    expect(get).toHaveBeenCalledWith(TRANSACTIONS_URL, {});
    expect(page).toEqual(EMPTY_PAGE);
  });

  it("treats a null cursor like an omitted one", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));

    await listCustomerTransactions({ pageSize: 20, beforeSequence: null });

    expect(get).toHaveBeenCalledWith(`${TRANSACTIONS_URL}?page_size=20`, {});
  });

  it("sends page_size and before_sequence under the names the backend reads", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));

    await listCustomerTransactions({ pageSize: 50, beforeSequence: 23 });

    expect(get).toHaveBeenCalledWith(`${TRANSACTIONS_URL}?page_size=50&before_sequence=23`, {});
  });

  it("sends only the cursor when that is all it is given", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));

    await listCustomerTransactions({ beforeSequence: 1 });

    expect(get).toHaveBeenCalledWith(`${TRANSACTIONS_URL}?before_sequence=1`, {});
  });

  it("never sends a tenant or customer identifier", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));

    await listCustomerTransactions({ pageSize: 20, beforeSequence: 23 });
    await getCustomerWallet();

    for (const [url] of get.mock.calls) {
      expect(url).not.toMatch(/tenant|customer_id|reference_type|\/admin\//);
    }
  });

  it("passes the abort signal through", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));
    const controller = new AbortController();

    await listCustomerTransactions({}, controller.signal);

    expect(get).toHaveBeenCalledWith(TRANSACTIONS_URL, { signal: controller.signal });
  });

  it("starts every page key with the same prefix and tells pages apart", () => {
    const prefix = [...CUSTOMER_TRANSACTIONS_QUERY_KEY];
    for (const key of [customerTransactionListQueryKey(null, 20), customerTransactionListQueryKey(23, 20)]) {
      expect(key.slice(0, prefix.length)).toEqual(prefix);
    }
    expect(customerTransactionListQueryKey(null, 20)).not.toEqual(customerTransactionListQueryKey(23, 20));
  });
});
