/**
 * 钱包流水请求发到哪个地址、带什么查询参数。
 *
 * 面板用例把这个模块 mock 掉了，所以路径与参数名写错时它们照样全绿：`before_sequence` 打成
 * `beforeSequence` 在浏览器里不报错，只是永远停在最新一页。
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { client } from "./client";
import {
  listWalletTransactions,
  walletTransactionListQueryKey,
  walletTransactionsQueryKey,
} from "./walletTransactions";

const CUSTOMER_ID = "00000000-0000-0000-0000-000000000000";
const TRANSACTIONS_URL = `/api/v1/admin/customers/${CUSTOMER_ID}/wallet/transactions`;

function envelope(data: unknown) {
  return { data: { success: true, data, error: null, request_id: "req-1" } };
}

const EMPTY_PAGE = { items: [], page_size: 20, next_before_sequence: null };

afterEach(() => {
  vi.restoreAllMocks();
});

describe("wallet transaction requests", () => {
  it("leaves every omitted parameter out of the query string", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));

    const page = await listWalletTransactions(CUSTOMER_ID);

    expect(get).toHaveBeenCalledWith(TRANSACTIONS_URL, {});
    expect(page).toEqual(EMPTY_PAGE);
  });

  it("treats null like omitted", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));

    await listWalletTransactions(CUSTOMER_ID, { pageSize: 20, beforeSequence: null, referenceType: null });

    expect(get).toHaveBeenCalledWith(`${TRANSACTIONS_URL}?page_size=20`, {});
  });

  it("sends page_size, before_sequence and reference_type under the names the backend reads", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));

    await listWalletTransactions(CUSTOMER_ID, {
      pageSize: 50,
      beforeSequence: 23,
      referenceType: "ADMIN_ADJUSTMENT",
    });

    expect(get).toHaveBeenCalledWith(
      `${TRANSACTIONS_URL}?page_size=50&before_sequence=23&reference_type=ADMIN_ADJUSTMENT`,
      {},
    );
  });

  it("sends only the cursor when that is all it is given", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));

    await listWalletTransactions(CUSTOMER_ID, { beforeSequence: 1 });

    expect(get).toHaveBeenCalledWith(`${TRANSACTIONS_URL}?before_sequence=1`, {});
  });

  it("escapes an id from the address bar instead of letting it change the path", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));

    await listWalletTransactions("a/../b");

    expect(get).toHaveBeenCalledWith("/api/v1/admin/customers/a%2F..%2Fb/wallet/transactions", {});
  });

  it("passes the abort signal through", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));
    const controller = new AbortController();

    await listWalletTransactions(CUSTOMER_ID, {}, controller.signal);

    expect(get).toHaveBeenCalledWith(TRANSACTIONS_URL, { signal: controller.signal });
  });

  it("starts every page and filter key with the customer's prefix", () => {
    const prefix = walletTransactionsQueryKey(CUSTOMER_ID);
    for (const key of [
      walletTransactionListQueryKey(CUSTOMER_ID, null, null, 20),
      walletTransactionListQueryKey(CUSTOMER_ID, "USAGE_EVENT", 23, 20),
    ]) {
      expect(key.slice(0, prefix.length)).toEqual([...prefix]);
    }
    expect(walletTransactionListQueryKey(CUSTOMER_ID, null, null, 20)).not.toEqual(
      walletTransactionListQueryKey(CUSTOMER_ID, "PAYMENT", null, 20),
    );
  });
});
