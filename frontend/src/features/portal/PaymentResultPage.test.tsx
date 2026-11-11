/**
 * 门户付款结果页（AIH-TASK-193，设计闸门 #587 v3 §2「门户页面」、§7「支付历史与付款结果页」）。
 *
 * 与其他门户页的测试同一写法：不 mock `api/customerPayments`，而是在 `client.get` 上按路径回应，钉住**页面真正发出去的
 * 请求** —— 只问 `/api/v1/customer/payments/{payment_id}`，任何时候都不请求 `/api/v1/admin/`。
 *
 * 计时用假时钟：只假 `setTimeout` / `clearTimeout`（页面的刷新只用它们），用 `vi.advanceTimersByTimeAsync` 推进。
 * ⚠️ 不用 `findBy*` / `waitFor`：Testing Library 在它们收尾时用 `setTimeout(0)` 排空微任务，假时钟下那一下永远不来。这里
 * 一律在 `act` 里推进时钟、排空微任务，再同步断言。
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
    ...overrides,
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
 * 按路径回应 `client.get`：详情的第 n 次读取（从 1 起）回 `handler(n)`。别的路径一律当成失败 —— 页面不该问它们，下面也会
 * 断言没有问。
 */
function serve(handler: (read: number) => unknown) {
  let reads = 0;
  return vi.spyOn(client, "get").mockImplementation((url: string) => {
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

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
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
