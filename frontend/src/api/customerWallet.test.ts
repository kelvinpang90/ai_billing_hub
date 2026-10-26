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
  customerTransactionExportFilename,
  customerTransactionExportUrl,
  customerTransactionListQueryKey,
  exportCustomerTransactions,
  getCustomerWallet,
  listCustomerTransactions,
} from "./customerWallet";

const WALLET_URL = "/api/v1/customer/wallet";
const TRANSACTIONS_URL = "/api/v1/customer/wallet/transactions";
const EXPORT_URL = "/api/v1/customer/wallet/transactions/export";

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

describe("exportCustomerTransactions", () => {
  const originalCreate = Object.getOwnPropertyDescriptor(URL, "createObjectURL");
  const originalRevoke = Object.getOwnPropertyDescriptor(URL, "revokeObjectURL");
  /** 被点击的下载链接的文件名，按点击顺序。 */
  let downloads: string[];

  /** jsdom 没有对象 URL，也不会真的下载：装上替身、拦下链接的点击，只记录文件名。 */
  function stubDownload(): void {
    downloads = [];
    Object.defineProperty(URL, "createObjectURL", {
      configurable: true,
      writable: true,
      value: () => "blob:https://portal.example/00000000-0000-0000-0000-000000000000",
    });
    Object.defineProperty(URL, "revokeObjectURL", { configurable: true, writable: true, value: () => undefined });
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      downloads.push(this.download);
    });
  }

  afterEach(() => {
    for (const [name, descriptor] of [
      ["createObjectURL", originalCreate],
      ["revokeObjectURL", originalRevoke],
    ] as const) {
      if (descriptor === undefined) {
        Reflect.deleteProperty(URL, name);
      } else {
        Object.defineProperty(URL, name, descriptor);
      }
    }
  });

  function file(headers: Record<string, string> = {}) {
    return { data: new Blob(["﻿id\r\n"], { type: "text/csv" }), status: 200, statusText: "", headers };
  }

  it("downloads from /api/v1/customer/wallet/transactions/export as a blob with only the dates", async () => {
    stubDownload();
    const get = vi
      .spyOn(client, "get")
      .mockResolvedValue(file({ "content-disposition": 'attachment; filename="wallet-transactions_2026-10-01_2026-10-26.csv"' }));

    await exportCustomerTransactions({ fromDate: "2026-10-01", toDate: "2026-10-26" });

    expect(get).toHaveBeenCalledTimes(1);
    const [url, config] = get.mock.calls[0] ?? [];
    expect(url).toBe(`${EXPORT_URL}?from_date=2026-10-01&to_date=2026-10-26`);
    expect(config).toMatchObject({ responseType: "blob" });
    expect(downloads).toEqual(["wallet-transactions_2026-10-01_2026-10-26.csv"]);
  });

  it("falls back to the backend's file name rule when the response has none", async () => {
    stubDownload();
    vi.spyOn(client, "get").mockResolvedValue(file());

    await exportCustomerTransactions({ fromDate: "2026-09-01", toDate: "2026-09-30" });

    expect(downloads).toEqual(["wallet-transactions_2026-09-01_2026-09-30.csv"]);
    expect(customerTransactionExportFilename({ fromDate: "2026-09-01", toDate: "2026-09-30" })).toBe(
      "wallet-transactions_2026-09-01_2026-09-30.csv",
    );
  });

  it("sends from_date and to_date under the names the backend reads, and nothing else", () => {
    const parsed = new URL(customerTransactionExportUrl({ fromDate: "2026-10-01", toDate: "2026-10-26" }), "https://portal.example");

    expect(parsed.pathname).toBe(EXPORT_URL);
    expect(Array.from(parsed.searchParams.entries())).toEqual([
      ["from_date", "2026-10-01"],
      ["to_date", "2026-10-26"],
    ]);
  });

  it("has no paging and never a tenant or customer identifier", () => {
    const url = customerTransactionExportUrl({ fromDate: "2026-10-01", toDate: "2026-10-26" });

    expect(url).not.toMatch(/page_size|before_sequence|reference_type|tenant|customer_id|\/admin\//);
  });

  it("passes the abort signal through", async () => {
    stubDownload();
    const get = vi.spyOn(client, "get").mockResolvedValue(file());
    const controller = new AbortController();

    await exportCustomerTransactions({ fromDate: "2026-10-01", toDate: "2026-10-26" }, controller.signal);

    const [, config] = get.mock.calls[0] ?? [];
    expect(config).toMatchObject({ signal: controller.signal });
  });
});
