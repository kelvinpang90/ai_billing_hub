/**
 * 管理端支付详情页（AIH-TASK-185，设计闸门 #585 v3 §2「管理端页面」）。
 *
 * 与列表页的测试同一写法：不 mock `api/adminPayments`，而是在 `client.get` / `client.post` 上回应，钉住**页面真正发出去的
 * 地址与请求体**，并断言没有问 `/api/v1/customer/` 路径。
 *
 * 收据（AIH-TASK-205，设计闸门 #589 v2 §2「管理端页面」、§7「管理端页面」）：有与没有收据、下载成功与失败、下载中不可再点、
 * 失败在下一次点下载或详情重新读取时清除。下载同样在 `client.get` 上回应；jsdom 没有 `URL.createObjectURL` /
 * `revokeObjectURL`，按属性装上替身、用完还原，`<a>` 的 `click` 拦下来只记录文件名。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import type { PaymentDetail } from "../../api/adminPayments";
import { ApiError, client } from "../../api/client";
import { DOWNLOAD_TIMEOUT_MS } from "../../api/download";
import { ROUTES, paymentDetailPath } from "../../routes/paths";
import { PaymentDetailPage } from "./PaymentDetailPage";

const ZERO_UUID = "00000000-0000-0000-0000-000000000000";
const DETAIL_URL = `/api/v1/admin/payments/${ZERO_UUID}`;
const RESOLVE_URL = `${DETAIL_URL}/resolve`;
const RECEIPT_NUMBER = "ACV-AIR-2026-000001";
const DOWNLOAD_URL = `/api/v1/admin/receipts/${ZERO_UUID}/download`;

/** 让请求一直挂着（加载中）。 */
const PENDING = Symbol("pending");

function detail(overrides: Partial<PaymentDetail> = {}): PaymentDetail {
  return {
    id: ZERO_UUID,
    customer_id: ZERO_UUID,
    customer_name: "Acme Sdn Bhd",
    amount: "100.00000000",
    currency: "MYR",
    gateway: "STRIPE",
    gateway_reference: "cs_test_0",
    gateway_fee: "2.90000000",
    status: "PAID",
    status_reason: null,
    resolution: null,
    created_at: "2026-11-08T08:00:00",
    paid_at: "2026-11-08T08:01:00",
    review_required_at: null,
    confirmed_amount: "100.00000000",
    confirmed_currency: "MYR",
    credited_amount: "100.00000000",
    gateway_payment_intent_id: "pi_test_0",
    expires_at: "2026-11-08T09:00:00",
    last_reconciled_at: "2026-11-08T08:02:00",
    resolved_at: null,
    resolved_by: null,
    resolution_reason: null,
    receipt_id: null,
    receipt_number: null,
    receipt_issued_at: null,
    events: [
      {
        gateway_event_id: "evt_test_0",
        event_type: "checkout.session.completed",
        processing_status: "PROCESSED",
        last_error_code: null,
        received_at: "2026-11-08T08:00:30",
        processed_at: "2026-11-08T08:00:31",
        fingerprint_conflict_at: null,
      },
    ],
    ...overrides,
  };
}

/** 一笔等待复核的支付：金额不符，Stripe 报了 90 MYR。 */
function inReview(overrides: Partial<PaymentDetail> = {}): PaymentDetail {
  return detail({
    status: "REVIEW_REQUIRED",
    status_reason: "AMOUNT_MISMATCH",
    paid_at: null,
    review_required_at: "2026-11-08T08:01:00",
    confirmed_amount: "90.00000000",
    confirmed_currency: "MYR",
    credited_amount: null,
    ...overrides,
  });
}

function envelope(data: unknown) {
  return { data: { success: true, data, error: null, request_id: "req-1" } };
}

/** 按路径回应 `client.get`：只认这笔支付的详情，别的路径一律当成失败 —— 页面不该问它们。 */
function serve(reply: () => unknown) {
  return vi.spyOn(client, "get").mockImplementation((url: string) => {
    const answer = url === DETAIL_URL ? reply() : new ApiError("NOT_FOUND", "Unexpected request.", null);
    if (answer === PENDING) {
      return new Promise(() => undefined);
    }
    return answer instanceof Error ? Promise.reject(answer) : Promise.resolve(envelope(answer));
  });
}

function serveResolve(reply: () => unknown) {
  return vi.spyOn(client, "post").mockImplementation(() => {
    const answer = reply();
    return answer instanceof Error ? Promise.reject(answer) : Promise.resolve(envelope(answer));
  });
}

/** 已开具收据的已付支付：收据 id 全零，开具在付款之后几秒。 */
function withReceipt(overrides: Partial<PaymentDetail> = {}): PaymentDetail {
  return detail({
    receipt_id: ZERO_UUID,
    receipt_number: RECEIPT_NUMBER,
    receipt_issued_at: "2026-11-08T08:01:05",
    ...overrides,
  });
}

/** 一份下载成功的响应：PDF blob，`headers` 由用例决定（不给 `Content-Disposition` 时页面用收据号作文件名）。 */
function pdfResponse(headers: Record<string, string> = {}) {
  return { data: new Blob(["%PDF-1.3"], { type: "application/pdf" }), status: 200, statusText: "", headers };
}

/**
 * 详情照 `reply` 回应；收据下载照 `download` 回应（一份 {@link pdfResponse}、一个错误，或 PENDING 一直挂着）。
 * 别的路径一律当成失败。
 */
function serveWithDownload(reply: () => unknown, download: () => unknown) {
  return vi.spyOn(client, "get").mockImplementation((url: string) => {
    if (url === DOWNLOAD_URL) {
      const answer = download();
      if (answer === PENDING) {
        return new Promise(() => undefined);
      }
      return answer instanceof Error ? Promise.reject(answer) : Promise.resolve(answer);
    }
    const answer = url === DETAIL_URL ? reply() : new ApiError("NOT_FOUND", "Unexpected request.", null);
    if (answer === PENDING) {
      return new Promise(() => undefined);
    }
    return answer instanceof Error ? Promise.reject(answer) : Promise.resolve(envelope(answer));
  });
}

function renderPage(): QueryClient {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[paymentDetailPath(ZERO_UUID)]}>
        <Routes>
          <Route path={ROUTES.paymentDetail} element={<PaymentDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return queryClient;
}

async function loaded(): Promise<void> {
  await screen.findByText("Acme Sdn Bhd");
}

/** 详情字段：标签 → 显示的文字。只看页面上那张详情表（确认框里的另算）。 */
function fields(): Record<string, string | null> {
  const descriptions = document.querySelector<HTMLElement>(".ant-card .ant-descriptions");
  if (descriptions === null) {
    throw new Error("no descriptions");
  }
  return descriptionValues(descriptions);
}

function descriptionValues(container: HTMLElement): Record<string, string | null> {
  return Object.fromEntries(
    Array.from(container.querySelectorAll(".ant-descriptions-item-label"), (label) => [
      label.textContent ?? "",
      label.nextElementSibling?.textContent ?? null,
    ]),
  );
}

function eventsTable(): HTMLElement {
  const table = document.querySelector<HTMLElement>(".ant-table");
  if (table === null) {
    throw new Error("no events table");
  }
  return table;
}

function reviewPanel(): HTMLElement | null {
  return screen.queryByRole("region", { name: "Review" });
}

function typeReason(value: string): void {
  fireEvent.change(screen.getByLabelText("Reason"), { target: { value } });
}

function creditButton(): HTMLElement {
  return screen.getByRole("button", { name: "Credit as paid" });
}

function closeButton(): HTMLElement {
  return screen.getByRole("button", { name: "Close without crediting" });
}

async function openConfirm(button: HTMLElement): Promise<HTMLElement> {
  fireEvent.click(button);
  return screen.findByRole("dialog");
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("PaymentDetailPage states", () => {
  it("shows a loading state while the payment is on its way", async () => {
    serve(() => PENDING);

    renderPage();

    expect(await screen.findByText("Loading the payment…")).toBeInTheDocument();
  });

  it("shows the code, backend message and request id when the payment fails to load, with a retry", async () => {
    const get = serve(() => new ApiError("INTERNAL_ERROR", "An unexpected error occurred.", "req-500"));

    renderPage();

    expect(await screen.findByText("The payment could not be loaded.")).toBeInTheDocument();
    expect(screen.getByText("INTERNAL_ERROR")).toBeInTheDocument();
    expect(screen.getByText(/An unexpected error occurred\./)).toBeInTheDocument();
    expect(screen.getByText("req-500")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Try again" }));

    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
  });

  it("says the payment does not exist on a 404", async () => {
    serve(() => new ApiError("PAYMENT_NOT_FOUND", "Payment not found.", "req-404"));

    renderPage();

    expect(await screen.findByText("Payment not found")).toBeInTheDocument();
    expect(screen.getByText("There is no payment with this ID.")).toBeInTheDocument();
    expect(screen.queryByText("The payment could not be loaded.")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to payments" })).toHaveAttribute("href", ROUTES.payments);
  });

  it("says there is no permission on a 403", async () => {
    serve(() => new ApiError("ADMIN_REQUIRED", "Administrator access is required.", "req-403"));

    renderPage();

    expect(await screen.findByText("Only administrators can view payments.")).toBeInTheDocument();
    expect(screen.getByText("req-403")).toBeInTheDocument();
  });
});

describe("PaymentDetailPage fields", () => {
  it("shows every detail field with MYT times and amounts in their currencies", async () => {
    serve(() =>
      detail({
        status_reason: "AMOUNT_MISMATCH",
        resolution: "CREDITED",
        review_required_at: "2026-11-08T08:00:40",
        confirmed_amount: "90.00000000",
        credited_amount: "90.00000000",
        resolved_at: "2026-11-09T02:00:00",
        resolved_by: "admin@acuven.example",
        resolution_reason: "Checked in Stripe",
      }),
    );

    renderPage();
    await loaded();

    expect(fields()).toEqual({
      "Payment ID": ZERO_UUID,
      Customer: "Acme Sdn Bhd",
      "Customer ID": ZERO_UUID,
      Amount: "MYR 100.00",
      Status: "Paid",
      "Status reason": "AMOUNT_MISMATCH",
      Resolution: "CREDITED",
      "Confirmed amount": "MYR 90.00",
      "Credited amount": "MYR 90.00",
      "Gateway fee": "MYR 2.90",
      Gateway: "STRIPE",
      "Gateway reference": "cs_test_0",
      "PaymentIntent ID": "pi_test_0",
      // 08:00 UTC → 16:00 吉隆坡时间。
      "Created at (MYT)": "2026-11-08 16:00:00",
      "Expires at (MYT)": "2026-11-08 17:00:00",
      "Paid at (MYT)": "2026-11-08 16:01:00",
      "Review required at (MYT)": "2026-11-08 16:00:40",
      "Last reconciled at (MYT)": "2026-11-08 16:02:00",
      "Resolved at (MYT)": "2026-11-09 10:00:00",
      "Resolved by": "admin@acuven.example",
      "Resolution reason": "Checked in Stripe",
      "Receipt number": "—",
      "Receipt issued at (MYT)": "—",
    });
    expect(screen.getByRole("link", { name: "Acme Sdn Bhd" })).toHaveAttribute("href", `/customers/${ZERO_UUID}`);
  });

  it("shows dashes for the empty fields and the confirmed amount in its own currency", async () => {
    serve(() =>
      detail({
        status: "PENDING",
        gateway_reference: null,
        gateway_fee: null,
        paid_at: null,
        confirmed_amount: "12.50000000",
        confirmed_currency: "USD",
        credited_amount: null,
        gateway_payment_intent_id: null,
        last_reconciled_at: null,
        events: [],
      }),
    );

    renderPage();
    await loaded();

    const values = fields();
    expect(values["Confirmed amount"]).toBe("USD 12.50");
    for (const label of [
      "Status reason",
      "Resolution",
      "Credited amount",
      "Gateway fee",
      "Gateway reference",
      "PaymentIntent ID",
      "Paid at (MYT)",
      "Review required at (MYT)",
      "Last reconciled at (MYT)",
      "Resolved at (MYT)",
      "Resolved by",
      "Resolution reason",
      "Receipt number",
      "Receipt issued at (MYT)",
    ]) {
      expect(values[label]).toBe("—");
    }
    expect(screen.getByText("No webhook events are linked to this payment.")).toBeInTheDocument();
  });

  it("shows only the number and marks the currency unknown when the confirmed currency is missing", async () => {
    serve(() => detail({ confirmed_amount: "90.00000000", confirmed_currency: null }));

    renderPage();
    await loaded();

    expect(fields()["Confirmed amount"]).toBe("90.00Currency unknown");
  });

  it("lists the webhook events in the order given, with MYT times", async () => {
    serve(() =>
      detail({
        events: [
          {
            gateway_event_id: "evt_test_0",
            event_type: "checkout.session.completed",
            processing_status: "PROCESSED",
            last_error_code: null,
            received_at: "2026-11-08T08:00:30",
            processed_at: "2026-11-08T08:00:31",
            fingerprint_conflict_at: null,
          },
          {
            gateway_event_id: "evt_test_1",
            event_type: "charge.refunded",
            processing_status: "UNMATCHED",
            last_error_code: "PAYMENT_NOT_FOUND",
            received_at: "2026-11-09T01:00:00",
            processed_at: null,
            fingerprint_conflict_at: "2026-11-09T01:05:00",
          },
        ],
      }),
    );

    renderPage();
    await loaded();

    const table = eventsTable();
    expect(within(table).getAllByRole("columnheader").map((th) => th.textContent)).toEqual([
      "Event ID",
      "Event type",
      "Processing status",
      "Last error code",
      "Received at (MYT)",
      "Processed at (MYT)",
      "Fingerprint conflict at (MYT)",
    ]);
    const rows = Array.from(table.querySelectorAll("tbody > tr[data-row-key]"));
    expect(rows.map((tr) => tr.getAttribute("data-row-key"))).toEqual(["evt_test_0", "evt_test_1"]);
    expect(rows.map((tr) => Array.from(tr.querySelectorAll(":scope > td"), (td) => td.textContent))).toEqual([
      [
        "evt_test_0",
        "checkout.session.completed",
        "PROCESSED",
        "—",
        "2026-11-08 16:00:30",
        "2026-11-08 16:00:31",
        "—",
      ],
      [
        "evt_test_1",
        "charge.refunded",
        "UNMATCHED",
        "PAYMENT_NOT_FOUND",
        "2026-11-09 09:00:00",
        "—",
        "2026-11-09 09:05:00",
      ],
    ]);
  });
});

describe("PaymentDetailPage review panel", () => {
  it("shows the review panel for a payment in review", async () => {
    serve(() => inReview());

    renderPage();
    await loaded();

    expect(reviewPanel()).toBeInTheDocument();
    expect(creditButton()).toBeInTheDocument();
    expect(closeButton()).toBeInTheDocument();
  });

  it.each(["PENDING", "PAID", "FAILED", "EXPIRED", "CANCELLED"])("shows no review panel for %s", async (status) => {
    serve(() => detail({ status }));

    renderPage();
    await loaded();

    expect(reviewPanel()).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Credit as paid" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Close without crediting" })).not.toBeInTheDocument();
  });

  it("keeps both buttons disabled while the reason is empty, blank or too long", async () => {
    serve(() => inReview());

    renderPage();
    await loaded();

    expect(creditButton()).toBeDisabled();
    expect(closeButton()).toBeDisabled();

    typeReason("   \n\t ");
    expect(creditButton()).toBeDisabled();
    expect(closeButton()).toBeDisabled();

    typeReason("x");
    expect(creditButton()).toBeEnabled();
    expect(closeButton()).toBeEnabled();

    typeReason(` ${"x".repeat(255)} `);
    expect(creditButton()).toBeEnabled();
    expect(closeButton()).toBeEnabled();

    typeReason("x".repeat(256));
    expect(creditButton()).toBeDisabled();
    expect(closeButton()).toBeDisabled();
    expect(screen.getByText("The reason must be at most 255 characters.")).toBeInTheDocument();
  });

  it("asks for confirmation before crediting, says Stripe's received amount counts, and sends nothing until confirmed", async () => {
    serve(() => inReview());
    const post = serveResolve(() => inReview());

    renderPage();
    await loaded();
    typeReason("  Checked in Stripe  ");
    const dialog = await openConfirm(creditButton());

    expect(within(dialog).getByText("Credit this payment?")).toBeInTheDocument();
    expect(
      within(dialog).getByText(
        "The amount credited is the amount received that Stripe reports when you submit, which may differ from the amount shown here.",
      ),
    ).toBeInTheDocument();
    const values = descriptionValues(dialog);
    expect(values["Current confirmed amount (reference)"]).toBe("MYR 90.00");
    expect(values["Reason"]).toBe("Checked in Stripe");
    expect(post).not.toHaveBeenCalled();
  });

  it("says Stripe reported no amount in the credit confirmation when the confirmed amount is missing", async () => {
    serve(() => inReview({ confirmed_amount: null, confirmed_currency: null }));

    renderPage();
    await loaded();
    typeReason("Checked in Stripe");
    const dialog = await openConfirm(creditButton());

    expect(descriptionValues(dialog)["Current confirmed amount (reference)"]).toBe("Stripe reported no amount");
  });

  it("shows only the number and marks the currency unknown in the credit confirmation", async () => {
    serve(() => inReview({ confirmed_amount: "90.00000000", confirmed_currency: null }));

    renderPage();
    await loaded();
    typeReason("Checked in Stripe");
    const dialog = await openConfirm(creditButton());

    expect(descriptionValues(dialog)["Current confirmed amount (reference)"]).toBe("90.00Currency unknown");
    expect(within(dialog).getByText("Currency unknown")).toBeInTheDocument();
  });

  it("shows a non-MYR confirmed amount in its own currency in the credit confirmation", async () => {
    serve(() => inReview({ status_reason: "CURRENCY_MISMATCH", confirmed_amount: "25.00000000", confirmed_currency: "USD" }));

    renderPage();
    await loaded();
    typeReason("Checked in Stripe");
    const dialog = await openConfirm(creditButton());

    expect(descriptionValues(dialog)["Current confirmed amount (reference)"]).toBe("USD 25.00");
  });

  it("asks for confirmation before closing, without the credit reference", async () => {
    serve(() => inReview());
    const post = serveResolve(() => inReview());

    renderPage();
    await loaded();
    typeReason("Customer asked to cancel");
    const dialog = await openConfirm(closeButton());

    expect(within(dialog).getByText("Close this payment without crediting?")).toBeInTheDocument();
    expect(
      within(dialog).getByText(
        "The payment becomes Failed and nothing is credited. Any refund is made by hand in the Stripe Dashboard.",
      ),
    ).toBeInTheDocument();
    expect(descriptionValues(dialog)).not.toHaveProperty("Current confirmed amount (reference)");
    expect(post).not.toHaveBeenCalled();
  });
});

describe("PaymentDetailPage resolve", () => {
  it("sends CREDIT with the trimmed reason after confirmation", async () => {
    serve(() => inReview());
    const post = serveResolve(() =>
      inReview({ status: "PAID", resolution: "CREDITED", credited_amount: "90.00000000" }),
    );

    renderPage();
    await loaded();
    typeReason("  Checked in Stripe  ");
    const dialog = await openConfirm(creditButton());
    fireEvent.click(within(dialog).getByRole("button", { name: "Confirm" }));

    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    expect(post).toHaveBeenCalledWith(RESOLVE_URL, { action: "CREDIT", reason: "Checked in Stripe" });
  });

  it("sends CLOSE with the trimmed reason after confirmation", async () => {
    serve(() => inReview());
    const post = serveResolve(() => inReview({ status: "FAILED", resolution: "CLOSED" }));

    renderPage();
    await loaded();
    typeReason("\tCustomer asked to cancel ");
    const dialog = await openConfirm(closeButton());
    fireEvent.click(within(dialog).getByRole("button", { name: "Confirm" }));

    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    expect(post).toHaveBeenCalledWith(RESOLVE_URL, { action: "CLOSE", reason: "Customer asked to cancel" });
  });

  it("refreshes the page from the returned detail and folds the panel away on success", async () => {
    const get = serve(() => inReview());
    serveResolve(() =>
      inReview({
        status: "PAID",
        resolution: "CREDITED",
        paid_at: "2026-11-09T02:00:00",
        credited_amount: "90.00000000",
        resolved_at: "2026-11-09T02:00:00",
        resolved_by: "admin@acuven.example",
        resolution_reason: "Checked in Stripe",
      }),
    );

    renderPage();
    await loaded();
    typeReason("Checked in Stripe");
    const dialog = await openConfirm(creditButton());
    fireEvent.click(within(dialog).getByRole("button", { name: "Confirm" }));

    expect(await screen.findByText("The payment was resolved.")).toBeInTheDocument();
    expect(reviewPanel()).not.toBeInTheDocument();
    const values = fields();
    expect(values["Status"]).toBe("Paid");
    expect(values["Resolution"]).toBe("CREDITED");
    expect(values["Credited amount"]).toBe("MYR 90.00");
    expect(values["Paid at (MYT)"]).toBe("2026-11-09 10:00:00");
    expect(values["Resolved by"]).toBe("admin@acuven.example");
    expect(values["Resolution reason"]).toBe("Checked in Stripe");
    // 用返回的详情刷新，不再多问一次。
    expect(get).toHaveBeenCalledTimes(1);
  });

  it("shows the 409 code, message and request id, keeps the reason and stays usable", async () => {
    serve(() => inReview());
    const post = serveResolve(
      () => new ApiError("PAYMENT_NOT_IN_REVIEW", "The payment is not awaiting review.", "req-409"),
    );

    renderPage();
    await loaded();
    typeReason("  Checked in Stripe  ");
    const dialog = await openConfirm(creditButton());
    fireEvent.click(within(dialog).getByRole("button", { name: "Confirm" }));

    expect(await screen.findByText("The payment could not be resolved.")).toBeInTheDocument();
    const panel = reviewPanel();
    if (panel === null) {
      throw new Error("no review panel");
    }
    expect(within(panel).getByText("PAYMENT_NOT_IN_REVIEW")).toBeInTheDocument();
    expect(within(panel).getByText(/The payment is not awaiting review\./)).toBeInTheDocument();
    expect(within(panel).getByText("req-409")).toBeInTheDocument();
    expect(screen.getByLabelText("Reason")).toHaveValue("  Checked in Stripe  ");
    await waitFor(() => expect(creditButton()).toBeEnabled());
    expect(closeButton()).toBeEnabled();
    expect(fields()["Status"]).toBe("Review required");

    // 还能接着用：改用不入账结案再提交一次。
    const again = await openConfirm(closeButton());
    fireEvent.click(within(again).getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    expect(post).toHaveBeenLastCalledWith(RESOLVE_URL, { action: "CLOSE", reason: "Checked in Stripe" });
  });

  it("shows the 503 code, message and request id and keeps the reason", async () => {
    serve(() => inReview());
    serveResolve(
      () => new ApiError("PAYMENT_GATEWAY_UNAVAILABLE", "The payment gateway is unavailable.", "req-503"),
    );

    renderPage();
    await loaded();
    typeReason("Checked in Stripe");
    const dialog = await openConfirm(creditButton());
    fireEvent.click(within(dialog).getByRole("button", { name: "Confirm" }));

    expect(await screen.findByText("The payment could not be resolved.")).toBeInTheDocument();
    const panel = reviewPanel();
    if (panel === null) {
      throw new Error("no review panel");
    }
    expect(within(panel).getByText("PAYMENT_GATEWAY_UNAVAILABLE")).toBeInTheDocument();
    expect(within(panel).getByText(/The payment gateway is unavailable\./)).toBeInTheDocument();
    expect(within(panel).getByText("req-503")).toBeInTheDocument();
    expect(screen.getByLabelText("Reason")).toHaveValue("Checked in Stripe");
    await waitFor(() => expect(creditButton()).toBeEnabled());
    expect(closeButton()).toBeEnabled();
  });
});

describe("PaymentDetailPage requests", () => {
  it("asks only admin endpoints, never a /api/v1/customer/ path", async () => {
    const get = serve(() => inReview());
    const post = serveResolve(() => inReview({ status: "FAILED", resolution: "CLOSED" }));

    renderPage();
    await loaded();
    typeReason("Customer asked to cancel");
    const dialog = await openConfirm(closeButton());
    fireEvent.click(within(dialog).getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));

    const urls = [...get.mock.calls, ...post.mock.calls].map(([url]) => url);
    expect(urls).toEqual([DETAIL_URL, RESOLVE_URL]);
    expect(urls.filter((url) => url.startsWith("/api/v1/customer/"))).toEqual([]);
  });
});

describe("PaymentDetailPage receipt", () => {
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

  /**
   * 下载按钮。按文字找再取外层的 `<button>`：`getByRole` 每次都要把整页（详情表、事件表、错误提示）算一遍可访问性，
   * 在 jsdom 里很慢，放进 `waitFor` 反复调用会拖过用例的超时。
   */
  function downloadButton(): HTMLElement {
    const button = screen.getByText("Download receipt").closest("button");
    if (button === null) {
      throw new Error("no download button");
    }
    return button;
  }

  function downloadCalls(get: ReturnType<typeof serveWithDownload>): unknown[][] {
    return get.mock.calls.filter(([url]) => url === DOWNLOAD_URL);
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

  it("shows the receipt number, the MYT issue time and a download button when there is a receipt", async () => {
    serve(() => withReceipt());

    renderPage();
    await loaded();

    const values = fields();
    expect(values["Receipt number"]).toBe(`${RECEIPT_NUMBER}Download receipt`);
    // 08:01:05 UTC → 16:01:05 吉隆坡时间。
    expect(values["Receipt issued at (MYT)"]).toBe("2026-11-08 16:01:05");
    expect(downloadButton()).toBeEnabled();
  });

  it("shows dashes and no download button when the payment has no receipt", async () => {
    // 已付而暂时没有收据（等补发扫描）。
    serve(() => detail());

    renderPage();
    await loaded();

    const values = fields();
    expect(values["Receipt number"]).toBe("—");
    expect(values["Receipt issued at (MYT)"]).toBe("—");
    expect(screen.queryByText("Download receipt")).not.toBeInTheDocument();
  });

  it("downloads the receipt from the admin path, falling back to <receipt_number>.pdf for the file name", async () => {
    const get = serveWithDownload(
      () => withReceipt(),
      () => pdfResponse(),
    );

    renderPage();
    await loaded();
    fireEvent.click(downloadButton());

    await waitFor(() => expect(saved).toEqual([`${RECEIPT_NUMBER}.pdf`]));
    expect(downloadCalls(get)).toEqual([[DOWNLOAD_URL, { responseType: "blob", timeout: DOWNLOAD_TIMEOUT_MS }]]);
    await waitFor(() => expect(downloadButton()).toBeEnabled());
    expect(screen.queryByText("The receipt could not be downloaded.")).not.toBeInTheDocument();
  });

  it("keeps the button disabled while a download is in flight and sends only one request", async () => {
    const get = serveWithDownload(
      () => withReceipt(),
      () => PENDING,
    );

    renderPage();
    await loaded();
    const button = downloadButton();
    fireEvent.click(button);
    fireEvent.click(button);

    await waitFor(() => expect(downloadButton()).toBeDisabled());
    fireEvent.click(downloadButton());
    expect(downloadCalls(get)).toHaveLength(1);
  });

  it("shows the code, message and request id below the button when the download fails", async () => {
    serveWithDownload(
      () => withReceipt(),
      () => new ApiError("RECEIPT_NOT_FOUND", "Receipt not found.", "req-404"),
    );

    renderPage();
    await loaded();
    fireEvent.click(downloadButton());

    const title = await screen.findByText("The receipt could not be downloaded.");
    expect(screen.getByText("RECEIPT_NOT_FOUND")).toBeInTheDocument();
    expect(screen.getByText(/Receipt not found\./)).toBeInTheDocument();
    expect(screen.getByText("req-404")).toBeInTheDocument();
    // 错误在按钮下方。
    expect(downloadButton().compareDocumentPosition(title) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    await waitFor(() => expect(downloadButton()).toBeEnabled());
    expect(saved).toEqual([]);
  });

  it("clears the failure on the next download click", async () => {
    let failing = true;
    const get = serveWithDownload(
      () => withReceipt(),
      () => (failing ? new ApiError("INTERNAL_ERROR", "An unexpected error occurred.", "req-500") : PENDING),
    );

    renderPage();
    await loaded();
    fireEvent.click(downloadButton());
    expect(await screen.findByText("The receipt could not be downloaded.")).toBeInTheDocument();
    await waitFor(() => expect(downloadButton()).toBeEnabled());

    failing = false;
    fireEvent.click(downloadButton());

    await waitFor(() => expect(screen.queryByText("The receipt could not be downloaded.")).not.toBeInTheDocument());
    expect(screen.queryByText("req-500")).not.toBeInTheDocument();
    expect(downloadCalls(get)).toHaveLength(2);
  });

  it("clears the failure when the detail is read again", async () => {
    const get = serveWithDownload(
      () => withReceipt(),
      () => new ApiError("RECEIPT_NOT_FOUND", "Receipt not found.", "req-404"),
    );

    const queryClient = renderPage();
    await loaded();
    fireEvent.click(downloadButton());
    expect(await screen.findByText("The receipt could not be downloaded.")).toBeInTheDocument();

    // 让重新读到的详情落在另一个时刻（页面按 dataUpdatedAt 判断详情是不是重新读过）。
    await new Promise((resolve) => setTimeout(resolve, 5));
    await queryClient.refetchQueries();

    await waitFor(() => expect(screen.queryByText("The receipt could not be downloaded.")).not.toBeInTheDocument());
    expect(screen.queryByText("req-404")).not.toBeInTheDocument();
    expect(get.mock.calls.filter(([url]) => url === DETAIL_URL)).toHaveLength(2);
    expect(downloadButton()).toBeEnabled();
  });

  it("asks only admin endpoints for the receipt, never a /api/v1/customer/ path", async () => {
    const get = serveWithDownload(
      () => withReceipt(),
      () => pdfResponse({ "content-disposition": `attachment; filename="${RECEIPT_NUMBER}.pdf"` }),
    );

    renderPage();
    await loaded();
    fireEvent.click(downloadButton());
    await waitFor(() => expect(saved).toEqual([`${RECEIPT_NUMBER}.pdf`]));

    const urls = get.mock.calls.map(([url]) => url);
    expect(urls).toEqual([DETAIL_URL, DOWNLOAD_URL]);
    expect(urls.filter((url) => url.startsWith("/api/v1/customer/"))).toEqual([]);
  });
});
