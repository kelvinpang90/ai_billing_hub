/**
 * 客户支付四个接口发到哪个地址、用什么方法、带什么查询参数与请求体（AIH-TASK-192）。
 *
 * 钉住：充值选项没有查询串；列表是页码分页（`page` 在前、`page_size` 在后，与 `listCustomerStatements` 同一写法）；
 * 详情把 `payment_id` 原样放进路径、没有查询串；建单是 POST、请求体只有字符串 `amount`。四个接口都**不带**任何租户
 * 或客户标识、不碰 `/api/v1/admin/`。
 *
 * 收据下载（AIH-TASK-204）：GET `/api/v1/customer/receipts/{receipt_id}/download`，`receipt_id` 原样放进路径、没有查询串；
 * 经 `downloadFile` 取 blob，文件名先认 `Content-Disposition`，取不到时用 `<receipt_number>.pdf`。jsdom 没有
 * `URL.createObjectURL` / `revokeObjectURL`，按属性装上替身、用完还原；`<a>` 的 `click` 拦下来只记录文件名。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, client } from "./client";
import {
  CUSTOMER_PAYMENTS_QUERY_KEY,
  CUSTOMER_TOPUP_OPTIONS_QUERY_KEY,
  createTopup,
  customerPaymentDetailQueryKey,
  customerPaymentListQueryKey,
  downloadCustomerReceipt,
  getCustomerPayment,
  getTopupOptions,
  listCustomerPayments,
} from "./customerPayments";
import { DOWNLOAD_TIMEOUT_MS } from "./download";

const TOPUP_OPTIONS_URL = "/api/v1/customer/topup-options";
const PAYMENTS_URL = "/api/v1/customer/payments";
const TOPUPS_URL = "/api/v1/customer/topups";
const RECEIPTS_URL = "/api/v1/customer/receipts";
const PAYMENT_ID = "00000000-0000-0000-0000-000000000000";
const RECEIPT_ID = "00000000-0000-0000-0000-000000000000";
const RECEIPT_NUMBER = "ACV-AIR-2026-000001";

const EMPTY_PAGE = { items: [], page: 1, page_size: 20, total: 0 };

const OPTIONS = {
  currency: "MYR",
  available: true,
  unavailable_reason: null,
  min_amount: "10.00",
  max_amount: null,
  preset_amounts: ["50.00", "100.00"],
  recovery_minimum: null,
  effective_minimum: "10.00",
};

const CREATED = {
  payment_id: PAYMENT_ID,
  redirect_url: "https://checkout.example/c/pay/cs_test_0",
  expires_at: "2026-11-10T09:00:00",
};

function envelope(data: unknown) {
  return { data: { success: true, data, error: null, request_id: "req-1" } };
}

function failure(code: string, requestId: string) {
  return { data: { success: false, data: null, error: { code, message: "Failed." }, request_id: requestId } };
}

/** 被调用的地址拆成路径与查询参数（按出现顺序）。 */
function parse(url: string): { path: string; params: [string, string][] } {
  const parsed = new URL(url, "https://portal.example");
  return { path: parsed.pathname, params: [...parsed.searchParams.entries()] };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("getTopupOptions", () => {
  it("GETs /api/v1/customer/topup-options with no query string", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(OPTIONS));

    const options = await getTopupOptions();

    expect(get.mock.calls).toEqual([[TOPUP_OPTIONS_URL, {}]]);
    expect(options).toEqual(OPTIONS);
  });

  it("passes the abort signal through", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(OPTIONS));
    const controller = new AbortController();

    await getTopupOptions(controller.signal);

    expect(get).toHaveBeenCalledWith(TOPUP_OPTIONS_URL, { signal: controller.signal });
  });
});

describe("listCustomerPayments", () => {
  it("sends page and page_size, in that order, to /api/v1/customer/payments", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));

    const page = await listCustomerPayments(3, 20);

    expect(get).toHaveBeenCalledTimes(1);
    const [url] = get.mock.calls[0] ?? [];
    expect(String(url)).toBe(`${PAYMENTS_URL}?page=3&page_size=20`);
    expect(parse(String(url))).toEqual({
      path: PAYMENTS_URL,
      params: [
        ["page", "3"],
        ["page_size", "20"],
      ],
    });
    expect(page).toEqual(EMPTY_PAGE);
  });

  it("passes the abort signal through", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));
    const controller = new AbortController();

    await listCustomerPayments(1, 20, controller.signal);

    expect(get).toHaveBeenCalledWith(`${PAYMENTS_URL}?page=1&page_size=20`, { signal: controller.signal });
  });
});

describe("getCustomerPayment", () => {
  it("puts the payment_id into the path as given, with no query string", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope({ id: PAYMENT_ID }));

    await getCustomerPayment(PAYMENT_ID);

    expect(get.mock.calls[0]).toEqual([`${PAYMENTS_URL}/${PAYMENT_ID}`, {}]);
  });

  it("does not reshape the id: the backend's 404 decides what is not a payment", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope({}));

    await getCustomerPayment("not-a-uuid");

    expect(get.mock.calls[0]?.[0]).toBe(`${PAYMENTS_URL}/not-a-uuid`);
  });

  it("throws the backend's error code and request_id", async () => {
    vi.spyOn(client, "get").mockResolvedValue(failure("PAYMENT_NOT_FOUND", "req-404"));

    await expect(getCustomerPayment(PAYMENT_ID)).rejects.toMatchObject({
      code: "PAYMENT_NOT_FOUND",
      requestId: "req-404",
    });
  });
});

describe("createTopup", () => {
  it("POSTs only the amount, as a string, to /api/v1/customer/topups", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope(CREATED));

    const created = await createTopup({ amount: "100.50" });

    expect(post.mock.calls).toEqual([[TOPUPS_URL, { amount: "100.50" }]]);
    expect(created).toEqual(CREATED);
  });

  it("does not send fields the caller added", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope(CREATED));
    const body = { amount: "50", tenant_id: "x", customer_id: "y" };

    await createTopup(body);

    expect(post.mock.calls[0]?.[1]).toEqual({ amount: "50" });
  });

  it("throws the backend's error code and request_id", async () => {
    vi.spyOn(client, "post").mockResolvedValue(failure("TOPUP_BELOW_RECOVERY_MINIMUM", "req-422"));

    await expect(createTopup({ amount: "10.00" })).rejects.toMatchObject({
      code: "TOPUP_BELOW_RECOVERY_MINIMUM",
      requestId: "req-422",
    });
  });
});

describe("downloadCustomerReceipt", () => {
  const originalCreate = Object.getOwnPropertyDescriptor(URL, "createObjectURL");
  const originalRevoke = Object.getOwnPropertyDescriptor(URL, "revokeObjectURL");
  /** 被点击的链接的 download 属性（即存成的文件名）。 */
  let saved: string[];

  function restore(name: "createObjectURL" | "revokeObjectURL", descriptor: PropertyDescriptor | undefined): void {
    if (descriptor === undefined) {
      Reflect.deleteProperty(URL, name);
    } else {
      Object.defineProperty(URL, name, descriptor);
    }
  }

  function pdfResponse(headers: Record<string, string>) {
    return { data: new Blob(["%PDF-1.3"], { type: "application/pdf" }), status: 200, statusText: "", headers };
  }

  beforeEach(() => {
    Object.defineProperty(URL, "createObjectURL", {
      configurable: true,
      writable: true,
      value: vi.fn(() => "blob:https://portal.example/00000000-0000-0000-0000-000000000000"),
    });
    Object.defineProperty(URL, "revokeObjectURL", { configurable: true, writable: true, value: vi.fn() });
    saved = [];
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      saved.push(this.download);
    });
  });

  afterEach(() => {
    restore("createObjectURL", originalCreate);
    restore("revokeObjectURL", originalRevoke);
  });

  it("GETs the receipt's download path as a blob, with no query string", async () => {
    const get = vi
      .spyOn(client, "get")
      .mockResolvedValue(pdfResponse({ "content-disposition": `attachment; filename="${RECEIPT_NUMBER}.pdf"` }));

    await downloadCustomerReceipt(RECEIPT_ID, RECEIPT_NUMBER);

    expect(get.mock.calls).toEqual([
      [`${RECEIPTS_URL}/${RECEIPT_ID}/download`, { responseType: "blob", timeout: DOWNLOAD_TIMEOUT_MS }],
    ]);
    expect(saved).toEqual([`${RECEIPT_NUMBER}.pdf`]);
  });

  it("takes the file name from Content-Disposition when there is one", async () => {
    vi.spyOn(client, "get").mockResolvedValue(pdfResponse({ "content-disposition": 'attachment; filename="other.pdf"' }));

    await downloadCustomerReceipt(RECEIPT_ID, RECEIPT_NUMBER);

    expect(saved).toEqual(["other.pdf"]);
  });

  it("falls back to <receipt_number>.pdf without a usable file name", async () => {
    vi.spyOn(client, "get").mockResolvedValue(pdfResponse({}));

    await downloadCustomerReceipt(RECEIPT_ID, RECEIPT_NUMBER);

    expect(saved).toEqual([`${RECEIPT_NUMBER}.pdf`]);
  });

  it("does not reshape the id: the backend's 404 decides what is not a receipt", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(pdfResponse({}));

    await downloadCustomerReceipt("not-a-uuid", RECEIPT_NUMBER);

    expect(get.mock.calls[0]?.[0]).toBe(`${RECEIPTS_URL}/not-a-uuid/download`);
  });

  it("throws the backend's error code and request_id", async () => {
    vi.spyOn(client, "get").mockRejectedValue(new ApiError("RECEIPT_NOT_FOUND", "Receipt not found.", "req-404"));

    await expect(downloadCustomerReceipt(RECEIPT_ID, RECEIPT_NUMBER)).rejects.toMatchObject({
      code: "RECEIPT_NOT_FOUND",
      requestId: "req-404",
    });
    expect(saved).toEqual([]);
  });
});

describe("customer payments requests", () => {
  it("never send a tenant or customer identifier and never touch admin paths", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope(CREATED));

    await getTopupOptions();
    await listCustomerPayments(1, 20);
    await getCustomerPayment(PAYMENT_ID);
    await createTopup({ amount: "50.00" });

    expect(get).toHaveBeenCalledTimes(3);
    expect(post).toHaveBeenCalledTimes(1);
    for (const [url] of [...get.mock.calls, ...post.mock.calls]) {
      expect(url).not.toMatch(/tenant|customer_id|\/admin\//);
    }
  });

  it("keeps the options, list pages and details under the customer payments prefix", () => {
    const prefix = [...CUSTOMER_PAYMENTS_QUERY_KEY];
    const options = [...CUSTOMER_TOPUP_OPTIONS_QUERY_KEY];
    const list = [...customerPaymentListQueryKey(1, 20)];
    const detail = [...customerPaymentDetailQueryKey(PAYMENT_ID)];
    for (const key of [options, list, detail]) {
      expect(key.slice(0, prefix.length)).toEqual(prefix);
    }
    expect(list).not.toEqual([...customerPaymentListQueryKey(2, 20)]);
    expect(prefix).not.toEqual(["admin", "payments"]);
  });
});
