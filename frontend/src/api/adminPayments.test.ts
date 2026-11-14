/**
 * 管理端支付的三个接口发到哪个地址、用什么方法、带什么查询参数与请求体。
 *
 * ⚠️ 参数名写错（`pageSize`、`createdFrom`）后端不报错（未知参数忽略），结果是筛选悄悄失效、永远停在第 1 页。
 *
 * 收据下载（AIH-TASK-205）：GET `/api/v1/admin/receipts/{receipt_id}/download`，`receipt_id` 原样放进路径、没有查询串；
 * 经 `downloadFile` 取 blob，文件名先认 `Content-Disposition`，取不到时用 `<receipt_number>.pdf`。jsdom 没有
 * `URL.createObjectURL` / `revokeObjectURL`，按属性装上替身、用完还原；`<a>` 的 `click` 拦下来只记录文件名。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, client } from "./client";
import {
  downloadAdminReceipt,
  getPayment,
  listPayments,
  resolvePayment,
  type ResolvePaymentBody,
} from "./adminPayments";
import { DOWNLOAD_TIMEOUT_MS } from "./download";

const ZERO_UUID = "00000000-0000-0000-0000-000000000000";
const PAYMENTS_URL = "/api/v1/admin/payments";
const RECEIPTS_URL = "/api/v1/admin/receipts";
const RECEIPT_NUMBER = "ACV-AIR-2026-000001";

function envelope(data: unknown) {
  return { data: { success: true, data, error: null, request_id: "req-1" } };
}

const EMPTY_PAGE = { items: [], page: 1, page_size: 20, total: 0 };

afterEach(() => {
  vi.restoreAllMocks();
});

describe("admin payment list", () => {
  it("lists payments with only page and page_size when there is no filter", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope({ ...EMPTY_PAGE, page: 2, page_size: 50 }));

    const page = await listPayments({}, 2, 50);

    expect(get).toHaveBeenCalledTimes(1);
    expect(get).toHaveBeenCalledWith(`${PAYMENTS_URL}?page=2&page_size=50`, {});
    expect(page).toEqual({ items: [], page: 2, page_size: 50, total: 0 });
  });

  it("adds status when a status is given", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));

    await listPayments({ status: "REVIEW_REQUIRED" }, 1, 20);

    expect(get).toHaveBeenCalledWith(`${PAYMENTS_URL}?page=1&page_size=20&status=REVIEW_REQUIRED`, {});
  });

  it("adds customer_id when a customer is given", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));

    await listPayments({ customer_id: ZERO_UUID }, 1, 20);

    expect(get).toHaveBeenCalledWith(`${PAYMENTS_URL}?page=1&page_size=20&customer_id=${ZERO_UUID}`, {});
  });

  it("adds created_from and created_to as the calendar dates given", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));

    await listPayments({ created_from: "2026-11-01" }, 1, 20);
    await listPayments({ created_to: "2026-11-30" }, 1, 20);

    expect(get).toHaveBeenNthCalledWith(1, `${PAYMENTS_URL}?page=1&page_size=20&created_from=2026-11-01`, {});
    expect(get).toHaveBeenNthCalledWith(2, `${PAYMENTS_URL}?page=1&page_size=20&created_to=2026-11-30`, {});
  });

  it("adds every filter together", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));

    await listPayments(
      { status: "PAID", customer_id: ZERO_UUID, created_from: "2026-11-01", created_to: "2026-11-08" },
      3,
      20,
    );

    expect(get).toHaveBeenCalledWith(
      `${PAYMENTS_URL}?page=3&page_size=20&status=PAID&customer_id=${ZERO_UUID}&created_from=2026-11-01&created_to=2026-11-08`,
      {},
    );
  });
});

describe("admin payment detail", () => {
  it("asks for the payment under its id", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope({ id: ZERO_UUID }));

    await getPayment(ZERO_UUID);

    expect(get).toHaveBeenCalledWith(`${PAYMENTS_URL}/${ZERO_UUID}`, {});
  });

  it("puts the payment id into the path exactly as given, without checking its shape", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope({}));

    await getPayment("Not-A-Payment");

    // 形状由后端判（404 PAYMENT_NOT_FOUND），这一层不拦、不改大小写。
    expect(get).toHaveBeenCalledWith(`${PAYMENTS_URL}/Not-A-Payment`, {});
  });

  it("turns a failure envelope into an ApiError with the request id", async () => {
    vi.spyOn(client, "get").mockResolvedValue({
      data: {
        success: false,
        data: null,
        error: { code: "PAYMENT_NOT_FOUND", message: "Payment not found." },
        request_id: "req-404",
      },
    });

    const failure = await getPayment(ZERO_UUID).catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(ApiError);
    expect(failure).toMatchObject({ code: "PAYMENT_NOT_FOUND", requestId: "req-404" });
  });
});

describe("admin payment resolve", () => {
  it("POSTs action and reason to the payment's resolve path and returns the detail", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope({ id: ZERO_UUID, status: "FAILED" }));
    const get = vi.spyOn(client, "get");

    const result = await resolvePayment(ZERO_UUID, { action: "CLOSE", reason: "Confirmed with the customer" });

    expect(post).toHaveBeenCalledTimes(1);
    expect(post).toHaveBeenCalledWith(`${PAYMENTS_URL}/${ZERO_UUID}/resolve`, {
      action: "CLOSE",
      reason: "Confirmed with the customer",
    });
    expect(get).not.toHaveBeenCalled();
    expect(result).toEqual({ id: ZERO_UUID, status: "FAILED" });
  });

  it("sends only action and reason, even when the caller passes more", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope({}));
    const body = { action: "CREDIT", reason: "Paid by card", amount: "1.00" } as ResolvePaymentBody;

    await resolvePayment(ZERO_UUID, body);

    // toEqual 不认多出来的键：请求体里有 amount 就不相等。
    expect(post.mock.calls[0]?.[1]).toEqual({ action: "CREDIT", reason: "Paid by card" });
  });

  it("puts the payment id into the path exactly as given", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope({}));

    await resolvePayment("Not-A-Payment", { action: "CLOSE", reason: "x" });

    expect(post).toHaveBeenCalledWith(`${PAYMENTS_URL}/Not-A-Payment/resolve`, { action: "CLOSE", reason: "x" });
  });

  it("turns a failure into an ApiError with the request id", async () => {
    vi.spyOn(client, "post").mockResolvedValue({
      data: {
        success: false,
        data: null,
        error: { code: "PAYMENT_NOT_IN_REVIEW", message: "Payment is not in review." },
        request_id: "req-409",
      },
    });

    const failure = await resolvePayment(ZERO_UUID, { action: "CLOSE", reason: "x" }).catch(
      (error: unknown) => error,
    );

    expect(failure).toBeInstanceOf(ApiError);
    expect(failure).toMatchObject({ code: "PAYMENT_NOT_IN_REVIEW", requestId: "req-409" });
  });
});

describe("admin receipt download", () => {
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
      value: vi.fn(() => "blob:https://admin.example/00000000-0000-0000-0000-000000000000"),
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

  it("GETs the admin receipt download path as a blob, with no query string", async () => {
    const get = vi
      .spyOn(client, "get")
      .mockResolvedValue(pdfResponse({ "content-disposition": `attachment; filename="${RECEIPT_NUMBER}.pdf"` }));

    await downloadAdminReceipt(ZERO_UUID, RECEIPT_NUMBER);

    expect(get.mock.calls).toEqual([
      [`${RECEIPTS_URL}/${ZERO_UUID}/download`, { responseType: "blob", timeout: DOWNLOAD_TIMEOUT_MS }],
    ]);
    expect(saved).toEqual([`${RECEIPT_NUMBER}.pdf`]);
  });

  it("never asks a customer path", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(pdfResponse({}));

    await downloadAdminReceipt(ZERO_UUID, RECEIPT_NUMBER);

    expect(String(get.mock.calls[0]?.[0])).not.toContain("/api/v1/customer/");
  });

  it("takes the file name from Content-Disposition when there is one", async () => {
    vi.spyOn(client, "get").mockResolvedValue(pdfResponse({ "content-disposition": 'attachment; filename="other.pdf"' }));

    await downloadAdminReceipt(ZERO_UUID, RECEIPT_NUMBER);

    expect(saved).toEqual(["other.pdf"]);
  });

  it("falls back to <receipt_number>.pdf without a usable file name", async () => {
    vi.spyOn(client, "get").mockResolvedValue(pdfResponse({}));

    await downloadAdminReceipt(ZERO_UUID, RECEIPT_NUMBER);

    expect(saved).toEqual([`${RECEIPT_NUMBER}.pdf`]);
  });

  it("falls back to <receipt_number>.pdf when Content-Disposition has an empty file name", async () => {
    vi.spyOn(client, "get").mockResolvedValue(pdfResponse({ "content-disposition": 'attachment; filename=""' }));

    await downloadAdminReceipt(ZERO_UUID, RECEIPT_NUMBER);

    expect(saved).toEqual([`${RECEIPT_NUMBER}.pdf`]);
  });

  it("puts the receipt id into the path exactly as given", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(pdfResponse({}));

    await downloadAdminReceipt("Not-A-Receipt", RECEIPT_NUMBER);

    expect(get.mock.calls[0]?.[0]).toBe(`${RECEIPTS_URL}/Not-A-Receipt/download`);
  });

  it("throws the backend's error code and request_id", async () => {
    vi.spyOn(client, "get").mockRejectedValue(new ApiError("RECEIPT_NOT_FOUND", "Receipt not found.", "req-404"));

    await expect(downloadAdminReceipt(ZERO_UUID, RECEIPT_NUMBER)).rejects.toMatchObject({
      code: "RECEIPT_NOT_FOUND",
      requestId: "req-404",
    });
    expect(saved).toEqual([]);
  });
});
