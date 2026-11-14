/**
 * 门户付款结果页（AIH-TASK-193，设计闸门 #587 v3 §2「门户页面」、§7「支付历史与付款结果页」）。
 *
 * 与其他门户页的测试同一写法：不 mock `api/customerPayments`，而是在 `client.get` 上按路径回应，钉住**页面真正发出去的
 * 请求** —— 只问 `/api/v1/customer/payments/{payment_id}`，任何时候都不请求 `/api/v1/admin/`。
 *
 * 计时用假时钟：只假 `setTimeout` / `clearTimeout`（页面的刷新只用它们），用 `vi.advanceTimersByTimeAsync` 推进。
 * ⚠️ 不用 `findBy*` / `waitFor`：Testing Library 在它们收尾时用 `setTimeout(0)` 排空微任务，假时钟下那一下永远不来。这里
 * 一律在 `act` 里推进时钟、排空微任务，再同步断言。
 *
 * 收据（AIH-TASK-204，设计闸门 #589 v2 §2「门户页面」、§7「门户页面」）：`PAID` 有收据时收据号与下载按钮（下载中不可再点；
 * 失败在按钮下方显示错误码、信息与 request_id，下一次点下载时清除）；`PAID` 无收据时提示正在准备；其他状态没有收据区域。
 * jsdom 没有 `URL.createObjectURL` / `revokeObjectURL`：按属性装上替身、用完还原；`<a>` 的 `click` 拦下来只记录文件名。
 */

import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import { ApiError, client } from "../../api/client";
import type { CustomerPayment } from "../../api/customerPayments";
import { ROUTES } from "../../routes/paths";
import { MAX_REFRESHES, POLL_INTERVAL_MS, PaymentResultPage } from "./PaymentResultPage";

const PAYMENT_ID = "00000000-0000-0000-0000-000000000000";
const DETAIL_URL = `/api/v1/customer/payments/${PAYMENT_ID}`;
const RESULT_PATH = `${ROUTES.portalPayments}/${PAYMENT_ID}`;

const STILL_CONFIRMING =
  "The payment result is still being confirmed. You can check it later in your payment history.";
const LOAD_FAILED = "The payment could not be loaded.";
const NOT_FOUND = "This payment was not found.";
const CANCELLED_NOTICE = "You cancelled the payment.";
const AWAITING = "Awaiting payment";

const RECEIPT_ID = "00000000-0000-0000-0000-000000000000";
const RECEIPT_NUMBER = "ACV-AIR-2026-000001";
const DOWNLOAD_URL = `/api/v1/customer/receipts/${RECEIPT_ID}/download`;
const RECEIPT_PREPARING = "Your receipt is being prepared. You can download it later from your payment history.";
const DOWNLOAD_FAILED = "The receipt could not be downloaded.";

function payment(overrides: Partial<CustomerPayment> = {}): CustomerPayment {
  return {
    id: PAYMENT_ID,
    amount: "100.00",
    currency: "MYR",
    status: "PENDING",
    credited_amount: null,
    created_at: "2026-11-08T08:00:00",
    paid_at: null,
    expires_at: "2026-11-08T09:00:00",
    receipt_id: null,
    receipt_number: null,
    ...overrides,
  };
}

/** 已付、已入账的支付；`receipt` 为真时带收据。 */
function paid(receipt: boolean): CustomerPayment {
  return payment({
    status: "PAID",
    credited_amount: "100.00",
    paid_at: "2026-11-08T08:01:00",
    ...(receipt ? { receipt_id: RECEIPT_ID, receipt_number: RECEIPT_NUMBER } : {}),
  });
}

function pdf(filename: string) {
  return {
    data: new Blob(["%PDF-1.3"], { type: "application/pdf" }),
    status: 200,
    statusText: "",
    headers: { "content-disposition": `attachment; filename="${filename}"` },
  };
}

function failure(requestId: string): ApiError {
  return new ApiError("INTERNAL_ERROR", "Something went wrong.", requestId);
}

function notFound(): ApiError {
  return new ApiError("PAYMENT_NOT_FOUND", "Payment not found.", "req-404");
}

function envelope(data: unknown) {
  return { data: { success: true, data, error: null, request_id: "req-1" } };
}

/**
 * 按路径回应 `client.get`：详情的第 n 次读取（从 1 起）回 `handler(n)`；收据下载的第 n 次回 `download(n)`（它回的就是
 * `client.get` 的结果）。别的路径一律当成失败 —— 页面不该问它们，下面也会断言没有问。
 */
function serve(handler: (read: number) => unknown, download?: (attempt: number) => Promise<unknown>) {
  let reads = 0;
  let downloads = 0;
  return vi.spyOn(client, "get").mockImplementation((url: string) => {
    if (url === DOWNLOAD_URL && download !== undefined) {
      downloads += 1;
      return download(downloads);
    }
    if (url !== DETAIL_URL) {
      return Promise.reject(new ApiError("NOT_FOUND", "Unexpected request.", null));
    }
    reads += 1;
    const value = handler(reads);
    return value instanceof Error ? Promise.reject(value) : Promise.resolve(envelope(value));
  });
}

function reads(get: ReturnType<typeof serve>): number {
  return get.mock.calls.filter(([url]) => url === DETAIL_URL).length;
}

function noAdminRequests(get: ReturnType<typeof serve>): void {
  expect(get.mock.calls.filter(([url]) => url.includes("/api/v1/admin/"))).toEqual([]);
}

/** 推进假时钟，并排空随后的微任务（请求的回应、页面的状态更新）。 */
async function advance(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
    for (let i = 0; i < 20; i += 1) {
      await Promise.resolve();
    }
  });
}

/** 刷新 count 次（每次一个间隔）。 */
async function refresh(count: number): Promise<void> {
  for (let i = 0; i < count; i += 1) {
    await advance(POLL_INTERVAL_MS);
  }
}

function renderPage({ search = "", state }: { search?: string; state?: unknown } = {}) {
  return render(
    <MemoryRouter initialEntries={[{ pathname: RESULT_PATH, search, state }]}>
      <Routes>
        <Route path={ROUTES.portalPaymentResult} element={<PaymentResultPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

function retryButton(): HTMLElement | null {
  return screen.queryByRole("button", { name: /Try again/ });
}

function downloadButton(): HTMLButtonElement | null {
  return screen.queryByRole<HTMLButtonElement>("button", { name: "Download" });
}

function downloads(get: ReturnType<typeof serve>): number {
  return get.mock.calls.filter(([url]) => url === DOWNLOAD_URL).length;
}

const originalCreate = Object.getOwnPropertyDescriptor(URL, "createObjectURL");
const originalRevoke = Object.getOwnPropertyDescriptor(URL, "revokeObjectURL");

function restore(name: "createObjectURL" | "revokeObjectURL", descriptor: PropertyDescriptor | undefined): void {
  if (descriptor === undefined) {
    Reflect.deleteProperty(URL, name);
  } else {
    Object.defineProperty(URL, name, descriptor);
  }
}

/** 存下的文件名（被点击的链接的 download 属性），按顺序。 */
let saved: string[];

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  Object.defineProperty(URL, "createObjectURL", {
    configurable: true,
    writable: true,
    value: vi.fn(() => "blob:https://portal.test/00000000-0000-0000-0000-000000000000"),
  });
  Object.defineProperty(URL, "revokeObjectURL", { configurable: true, writable: true, value: vi.fn() });
  saved = [];
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
    saved.push(this.download);
  });
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  restore("createObjectURL", originalCreate);
  restore("revokeObjectURL", originalRevoke);
});

describe("PaymentResultPage", () => {
  it("reads the payment again every 3 seconds while it is PENDING", async () => {
    const get = serve(() => payment());

    renderPage();
    await advance(0);

    expect(reads(get)).toBe(1);
    expect(screen.getByText(AWAITING)).toBeInTheDocument();
    expect(screen.getByText(PAYMENT_ID)).toBeInTheDocument();

    await advance(POLL_INTERVAL_MS - 1);
    expect(reads(get)).toBe(1);
    await advance(1);
    expect(reads(get)).toBe(2);
    await advance(POLL_INTERVAL_MS);
    expect(reads(get)).toBe(3);
    noAdminRequests(get);
  });

  it("stops after 40 refreshes and says the result is still being confirmed", async () => {
    const get = serve(() => payment());

    renderPage();
    await advance(0);
    await refresh(MAX_REFRESHES - 1);

    expect(reads(get)).toBe(MAX_REFRESHES);
    expect(screen.queryByText(STILL_CONFIRMING)).not.toBeInTheDocument();

    await refresh(1);
    expect(reads(get)).toBe(1 + MAX_REFRESHES);
    expect(screen.getByText(STILL_CONFIRMING)).toBeInTheDocument();
    expect(screen.getByText(AWAITING)).toBeInTheDocument();
    // 最后一次成功：不给错误、不给重试。
    expect(screen.queryByText(LOAD_FAILED)).not.toBeInTheDocument();
    expect(retryButton()).toBeNull();

    await advance(POLL_INTERVAL_MS * 10);
    expect(reads(get)).toBe(1 + MAX_REFRESHES);
  });

  it("stops refreshing once the payment reaches a final status", async () => {
    const get = serve((read) => (read === 1 ? payment() : payment({ status: "PAID", credited_amount: "100.00", paid_at: "2026-11-08T08:01:00" })));

    renderPage();
    await advance(0);
    await refresh(1);

    expect(reads(get)).toBe(2);
    expect(screen.getByText("Credited")).toBeInTheDocument();
    expect(screen.queryByText(AWAITING)).not.toBeInTheDocument();

    await advance(POLL_INTERVAL_MS * 5);
    expect(reads(get)).toBe(2);
    expect(screen.queryByText(STILL_CONFIRMING)).not.toBeInTheDocument();
  });

  it("does not read a final payment again", async () => {
    const get = serve(() => payment({ status: "EXPIRED" }));

    renderPage();
    await advance(0);
    await advance(POLL_INTERVAL_MS * 5);

    expect(reads(get)).toBe(1);
    expect(screen.getByText("Expired")).toBeInTheDocument();
  });

  it("stops the timer when the page is left", async () => {
    const get = serve(() => payment());

    const { unmount } = renderPage();
    await advance(0);
    expect(reads(get)).toBe(1);

    unmount();
    await advance(POLL_INTERVAL_MS * 5);
    expect(reads(get)).toBe(1);
  });

  it("says the payment was cancelled when result=cancelled, with the status still from the API", async () => {
    serve(() => payment());

    renderPage({ search: "?result=cancelled" });
    await advance(0);

    expect(screen.getByText(CANCELLED_NOTICE)).toBeInTheDocument();
    expect(screen.getByText(AWAITING)).toBeInTheDocument();
  });

  it("shows nothing extra for result=success", async () => {
    const get = serve(() => payment());

    renderPage({ search: "?result=success" });
    await advance(0);

    expect(screen.queryByText(CANCELLED_NOTICE)).not.toBeInTheDocument();
    // 跳回不代表已付：状态与刷新照接口来。
    expect(screen.getByText(AWAITING)).toBeInTheDocument();
    await refresh(1);
    expect(reads(get)).toBe(2);
  });

  it("says the payment was not found on a 404", async () => {
    const get = serve(() => notFound());

    renderPage();
    await advance(0);

    expect(screen.getByText(NOT_FOUND)).toBeInTheDocument();
    expect(retryButton()).toBeNull();
    await advance(POLL_INTERVAL_MS * 5);
    expect(reads(get)).toBe(1);
  });

  it("shows the first failure with its request_id and a retry, and counts 40 again after a PENDING retry", async () => {
    const get = serve((read) => (read === 1 ? failure("req-first") : payment()));

    renderPage();
    await advance(0);

    expect(screen.getByText(LOAD_FAILED)).toBeInTheDocument();
    expect(screen.getByText("req-first")).toBeInTheDocument();
    expect(retryButton()).not.toBeNull();
    // 第一次失败不自动刷新。
    await advance(POLL_INTERVAL_MS * 5);
    expect(reads(get)).toBe(1);

    fireEvent.click(retryButton()!);
    await advance(0);
    expect(reads(get)).toBe(2);
    expect(screen.queryByText(LOAD_FAILED)).not.toBeInTheDocument();
    expect(screen.getByText(AWAITING)).toBeInTheDocument();

    await refresh(MAX_REFRESHES - 1);
    expect(reads(get)).toBe(1 + MAX_REFRESHES);
    expect(screen.queryByText(STILL_CONFIRMING)).not.toBeInTheDocument();
    await refresh(1);
    expect(reads(get)).toBe(2 + MAX_REFRESHES);
    expect(screen.getByText(STILL_CONFIRMING)).toBeInTheDocument();
    await advance(POLL_INTERVAL_MS * 5);
    expect(reads(get)).toBe(2 + MAX_REFRESHES);
  });

  it("keeps the last content and keeps refreshing when a refresh fails", async () => {
    const get = serve((read) => (read === 2 ? failure("req-mid") : payment()));

    renderPage();
    await advance(0);
    await refresh(1);

    expect(reads(get)).toBe(2);
    expect(screen.getByText(AWAITING)).toBeInTheDocument();
    expect(screen.getByText(PAYMENT_ID)).toBeInTheDocument();
    // 中途的失败不打扰。
    expect(screen.queryByText(LOAD_FAILED)).not.toBeInTheDocument();
    expect(retryButton()).toBeNull();

    await refresh(1);
    expect(reads(get)).toBe(3);
  });

  it("counts failed refreshes toward the 40", async () => {
    const get = serve((read) => (read % 2 === 0 ? failure(`req-${read}`) : payment()));

    renderPage();
    await advance(0);
    await refresh(MAX_REFRESHES);

    expect(reads(get)).toBe(1 + MAX_REFRESHES);
    await advance(POLL_INTERVAL_MS * 5);
    expect(reads(get)).toBe(1 + MAX_REFRESHES);
    expect(screen.getByText(STILL_CONFIRMING)).toBeInTheDocument();
  });

  it("shows the still-confirming notice, the last error and a retry together when the 40th refresh fails", async () => {
    const lastRead = 1 + MAX_REFRESHES;
    const get = serve((read) => (read === lastRead ? failure("req-last") : payment()));

    renderPage();
    await advance(0);
    await refresh(MAX_REFRESHES);

    expect(reads(get)).toBe(lastRead);
    expect(screen.getByText(STILL_CONFIRMING)).toBeInTheDocument();
    expect(screen.getByText(LOAD_FAILED)).toBeInTheDocument();
    expect(screen.getByText("req-last")).toBeInTheDocument();
    expect(retryButton()).not.toBeNull();
    // 上一次成功的内容照留。
    expect(screen.getByText(AWAITING)).toBeInTheDocument();

    await advance(POLL_INTERVAL_MS * 5);
    expect(reads(get)).toBe(lastRead);

    // 重试成功且仍是 PENDING：从 0 重新计 40 次。
    fireEvent.click(retryButton()!);
    await advance(0);
    expect(reads(get)).toBe(lastRead + 1);
    expect(screen.queryByText(STILL_CONFIRMING)).not.toBeInTheDocument();
    expect(screen.queryByText(LOAD_FAILED)).not.toBeInTheDocument();

    await refresh(MAX_REFRESHES);
    expect(reads(get)).toBe(lastRead + 1 + MAX_REFRESHES);
    expect(screen.getByText(STILL_CONFIRMING)).toBeInTheDocument();
    await advance(POLL_INTERVAL_MS * 5);
    expect(reads(get)).toBe(lastRead + 1 + MAX_REFRESHES);
  });

  it("stops and says the payment was not found when a refresh gets a 404", async () => {
    const get = serve((read) => (read === 3 ? notFound() : payment()));

    renderPage();
    await advance(0);
    await refresh(2);

    expect(reads(get)).toBe(3);
    expect(screen.getByText(NOT_FOUND)).toBeInTheDocument();
    expect(screen.queryByText(AWAITING)).not.toBeInTheDocument();

    await advance(POLL_INTERVAL_MS * 5);
    expect(reads(get)).toBe(3);
  });

  it("links back to the history page from the route state, and to top up again", async () => {
    serve(() => payment({ status: "PAID", credited_amount: "100.00", paid_at: "2026-11-08T08:01:00" }));

    renderPage({ state: { fromPage: 3 } });
    await advance(0);

    expect(screen.getByRole("link", { name: "Back to payment history" })).toHaveAttribute(
      "href",
      `${ROUTES.portalPayments}?page=3`,
    );
    expect(screen.getByRole("link", { name: "Top up again" })).toHaveAttribute("href", ROUTES.portalTopup);
  });

  it("links back to page 1 of the history without route state, as when returning from Stripe", async () => {
    serve(() => payment({ status: "PAID", credited_amount: "100.00", paid_at: "2026-11-08T08:01:00" }));

    renderPage({ search: "?result=success" });
    await advance(0);

    expect(screen.getByRole("link", { name: "Back to payment history" })).toHaveAttribute(
      "href",
      `${ROUTES.portalPayments}?page=1`,
    );
  });
});

describe("PaymentResultPage receipt", () => {
  it("shows the receipt number and downloads it, not clickable while downloading", async () => {
    let finish!: (value: unknown) => void;
    const get = serve(
      () => paid(true),
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );

    renderPage();
    await advance(0);

    expect(screen.getByText(`Receipt: ${RECEIPT_NUMBER}`)).toBeInTheDocument();
    expect(screen.queryByText(RECEIPT_PREPARING)).not.toBeInTheDocument();
    expect(downloadButton()).toBeEnabled();

    fireEvent.click(downloadButton()!);
    await advance(0);
    expect(downloads(get)).toBe(1);
    expect(downloadButton()).toBeDisabled();

    // 下载中再点：不发第二个请求。
    fireEvent.click(downloadButton()!);
    await advance(0);
    expect(downloads(get)).toBe(1);

    finish(pdf(`${RECEIPT_NUMBER}.pdf`));
    await advance(0);
    expect(saved).toEqual([`${RECEIPT_NUMBER}.pdf`]);
    expect(downloadButton()).toBeEnabled();
    expect(screen.queryByText(DOWNLOAD_FAILED)).not.toBeInTheDocument();
    // 已付：下载不改变刷新规则，不再读详情。
    await advance(POLL_INTERVAL_MS * 5);
    expect(reads(get)).toBe(1);
    noAdminRequests(get);
  });

  it("shows a failed download below the button with its code, message and request_id, cleared by the next click", async () => {
    const get = serve(
      () => paid(true),
      (attempt) =>
        attempt === 1
          ? Promise.reject(new ApiError("INTERNAL_ERROR", "Something went wrong.", "req-500"))
          : new Promise(() => undefined),
    );

    renderPage();
    await advance(0);

    fireEvent.click(downloadButton()!);
    await advance(0);

    const failed = screen.getByText(DOWNLOAD_FAILED);
    expect(screen.getByText("INTERNAL_ERROR")).toBeInTheDocument();
    expect(screen.getByText(/Something went wrong\./)).toBeInTheDocument();
    expect(screen.getByText("req-500")).toBeInTheDocument();
    expect(downloadButton()!.compareDocumentPosition(failed) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(downloadButton()).toBeEnabled();
    expect(saved).toEqual([]);

    fireEvent.click(downloadButton()!);
    await advance(0);
    expect(downloads(get)).toBe(2);
    expect(screen.queryByText(DOWNLOAD_FAILED)).not.toBeInTheDocument();
    expect(screen.queryByText("req-500")).not.toBeInTheDocument();
  });

  it("says the receipt is being prepared when PAID without a receipt, without reading again", async () => {
    const get = serve(() => paid(false));

    renderPage();
    await advance(0);

    expect(screen.getByText(RECEIPT_PREPARING)).toBeInTheDocument();
    expect(downloadButton()).toBeNull();
    await advance(POLL_INTERVAL_MS * 5);
    expect(reads(get)).toBe(1);
  });

  it.each(["PENDING", "IN_REVIEW", "NOT_CREDITED", "FAILED", "EXPIRED", "CANCELLED"])(
    "shows no receipt area for %s",
    async (status) => {
      serve(() => payment({ status }));

      renderPage();
      await advance(0);

      expect(screen.getByText(PAYMENT_ID)).toBeInTheDocument();
      expect(screen.queryByText(RECEIPT_PREPARING)).not.toBeInTheDocument();
      expect(screen.queryByText(/^Receipt/)).not.toBeInTheDocument();
      expect(downloadButton()).toBeNull();
    },
  );

  it("shows the receipt once a PENDING payment turns PAID with one", async () => {
    const get = serve((read) => (read === 1 ? payment() : paid(true)));

    renderPage();
    await advance(0);
    expect(downloadButton()).toBeNull();

    await refresh(1);
    expect(reads(get)).toBe(2);
    expect(screen.getByText(`Receipt: ${RECEIPT_NUMBER}`)).toBeInTheDocument();
    expect(downloadButton()).toBeEnabled();
  });
});
