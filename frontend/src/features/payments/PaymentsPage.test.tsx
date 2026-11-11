/**
 * 管理端支付列表页（AIH-TASK-184，设计闸门 #585 v3 §2「管理端页面」）。
 *
 * 与对账单页的测试同一写法：不 mock `api/adminPayments`，而是在 `client.get` 上按路径回应，钉住**页面真正发出去的
 * 地址**（分页与各筛选的查询串），并断言没有问 `/api/v1/customer/` 路径。点一行之后看地址栏落到了哪条路径。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import type { CustomerSummary, Page } from "../../api/adminCustomers";
import type { PaymentSummary } from "../../api/adminPayments";
import { ApiError, client } from "../../api/client";
import { ROUTES } from "../../routes/paths";
import { PaymentsPage } from "./PaymentsPage";

const ZERO_UUID = "00000000-0000-0000-0000-000000000000";
/** 第二笔支付的 id：只用来区分两行，原样进路径，页面不解析它。 */
const SECOND_ID = "second-payment";
const PAYMENTS_URL = "/api/v1/admin/payments";
const CUSTOMERS_URL = "/api/v1/admin/customers";
const LIST_FIRST_PAGE = `${PAYMENTS_URL}?page=1&page_size=20`;

/** 让请求一直挂着（加载中）。 */
const PENDING = Symbol("pending");

function customer(): CustomerSummary {
  return {
    id: ZERO_UUID,
    company_name: "Acme Sdn Bhd",
    contact_name: null,
    email: "billing@acme.example",
    phone: null,
    billing_status: "ACTIVE",
    account_status: "ENABLED",
    billing_mode: "PREPAID",
    ai_service_enabled: true,
    status_version: 1,
    created_at: "2026-11-01T00:00:00",
    updated_at: "2026-11-01T00:00:00",
  };
}

function payment(overrides: Partial<PaymentSummary> = {}): PaymentSummary {
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
    ...overrides,
  };
}

function page<T>(items: T[], total: number, number = 1, pageSize = 20): Page<T> {
  return { items, page: number, page_size: pageSize, total };
}

function envelope(data: unknown) {
  return { data: { success: true, data, error: null, request_id: "req-1" } };
}

type Reply = (query: URLSearchParams) => unknown;

/**
 * 按路径回应 `client.get`：客户列表交给 `customers`，支付列表交给 `list`。别的路径一律当成失败 —— 页面不该问它们，
 * 下面也会断言没有问。
 */
function serve(list: Reply, customers: Reply = () => page([customer()], 1)) {
  return vi.spyOn(client, "get").mockImplementation((url: string) => {
    const parsed = new URL(url, "https://admin.example");
    let reply: unknown;
    if (parsed.pathname === CUSTOMERS_URL) {
      reply = customers(parsed.searchParams);
    } else if (parsed.pathname === PAYMENTS_URL) {
      reply = list(parsed.searchParams);
    } else {
      reply = new ApiError("NOT_FOUND", "Unexpected request.", null);
    }
    if (reply === PENDING) {
      return new Promise(() => undefined);
    }
    return reply instanceof Error ? Promise.reject(reply) : Promise.resolve(envelope(reply));
  });
}

/** 详情页的路由由 AIH-TASK-185 挂上；这里只放一个显示地址栏的占位，看点一行落到了哪条路径。 */
function WhereAmI() {
  const location = useLocation();
  return <output data-testid="location">{location.pathname}</output>;
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[ROUTES.payments]}>
        <Routes>
          <Route path={ROUTES.payments} element={<PaymentsPage />} />
          <Route path="*" element={<WhereAmI />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function listRegion(): HTMLElement {
  return screen.getByRole("region", { name: "Payment list" });
}

function headersOf(region: HTMLElement): string[] {
  return within(region)
    .getAllByRole("columnheader")
    .map((th) => th.textContent ?? "");
}

function rowOf(region: HTMLElement, key: string): HTMLElement {
  const tr = region.querySelector<HTMLElement>(`tr[data-row-key="${key}"]`);
  if (tr === null) {
    throw new Error(`no row ${key}`);
  }
  return tr;
}

function cellTexts(region: HTMLElement, key: string): (string | null)[] {
  return Array.from(rowOf(region, key).querySelectorAll(":scope > td"), (td) => td.textContent);
}

function rowKeys(region: HTMLElement): (string | null)[] {
  return Array.from(region.querySelectorAll("tbody > tr[data-row-key]"), (tr) => tr.getAttribute("data-row-key"));
}

function urls(get: ReturnType<typeof serve>): string[] {
  return get.mock.calls.map(([url]) => url);
}

function listUrls(get: ReturnType<typeof serve>): string[] {
  return urls(get).filter((url) => url.startsWith(`${PAYMENTS_URL}?`));
}

/** antd 下拉在 mousedown 时打开；选项按 title 找、用 fireEvent 点（同对账单页的用例）。 */
async function choose(field: HTMLElement, option: string): Promise<void> {
  fireEvent.mouseDown(field);
  const candidates = await screen.findAllByTitle(option);
  const item = candidates.find((candidate) => candidate.closest(".ant-select-item") !== null) ?? candidates[0];
  if (item === undefined) {
    throw new Error(`no option "${option}"`);
  }
  fireEvent.click(item);
}

function typeDate(label: "Created from (MYT)" | "Created to (MYT)", value: string): void {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("PaymentsPage states", () => {
  it("shows a loading state while the payments are on their way", async () => {
    serve(() => PENDING);

    renderPage();

    expect(await screen.findByText("Loading payments…")).toBeInTheDocument();
    expect(within(listRegion()).queryByRole("table")).not.toBeInTheDocument();
  });

  it("shows the backend message and the request id when the payments fail, with a retry", async () => {
    serve(() => new ApiError("INTERNAL_ERROR", "An unexpected error occurred.", "req-500"));

    renderPage();

    expect(await screen.findByText("The payments could not be loaded.")).toBeInTheDocument();
    expect(screen.getByText(/An unexpected error occurred\./)).toBeInTheDocument();
    expect(screen.getByText("req-500")).toBeInTheDocument();
    expect(within(listRegion()).getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("says there is no permission on a 403", async () => {
    serve(() => new ApiError("ADMIN_REQUIRED", "Administrator access is required.", "req-403"));

    renderPage();

    expect(await screen.findByText("Only administrators can view payments.")).toBeInTheDocument();
    expect(screen.getByText("req-403")).toBeInTheDocument();
    expect(screen.queryByText("The payments could not be loaded.")).not.toBeInTheDocument();
  });

  it("says so when there are no payments yet", async () => {
    serve(() => page([], 0));

    renderPage();

    expect(await screen.findByText("No payments yet.")).toBeInTheDocument();
    expect(within(listRegion()).queryByRole("table")).not.toBeInTheDocument();
  });

  it("says no payments match when a filter finds none", async () => {
    serve((query) => (query.has("created_from") ? page([], 0) : page([payment()], 1)));

    renderPage();
    await screen.findByText(ZERO_UUID);
    typeDate("Created from (MYT)", "2027-01-01");

    expect(await screen.findByText("No payments match these filters.")).toBeInTheDocument();
  });
});

describe("PaymentsPage columns", () => {
  it("lists the payments with their columns, MYT times and amounts in the returned currency", async () => {
    const get = serve(() => page([payment()], 1));

    renderPage();
    await screen.findByText(ZERO_UUID);

    const region = listRegion();
    expect(headersOf(region)).toEqual([
      "Payment ID",
      "Customer",
      "Amount",
      "Gateway fee",
      "Status",
      "Status reason",
      "Resolution",
      "Created at (MYT)",
      "Paid at (MYT)",
    ]);
    expect(rowKeys(region)).toEqual([ZERO_UUID]);
    expect(cellTexts(region, ZERO_UUID)).toEqual([
      ZERO_UUID,
      "Acme Sdn Bhd",
      "MYR 100.00",
      "MYR 2.90",
      "Paid",
      "—",
      "—",
      // 08:00 UTC → 16:00 吉隆坡时间。
      "2026-11-08 16:00:00",
      "2026-11-08 16:01:00",
    ]);
    expect(listUrls(get)).toEqual([LIST_FIRST_PAGE]);
    // 客户下拉的选项逐页取全，用的是客户列表接口。
    expect(urls(get)).toContain(`${CUSTOMERS_URL}?page=1&page_size=100`);
  });

  it("shows dashes for a missing fee and paid time, and the reason and resolution codes as given", async () => {
    serve(() =>
      page(
        [
          payment({
            currency: "USD",
            amount: "12.50000000",
            gateway_fee: null,
            status: "REVIEW_REQUIRED",
            status_reason: "AMOUNT_MISMATCH",
            paid_at: null,
          }),
          payment({ id: SECOND_ID, status: "FAILED", status_reason: "NO_GATEWAY_SESSION", resolution: "CLOSED" }),
        ],
        2,
      ),
    );

    renderPage();
    await screen.findByText(ZERO_UUID);

    const region = listRegion();
    expect(cellTexts(region, ZERO_UUID)).toEqual([
      ZERO_UUID,
      "Acme Sdn Bhd",
      // 币种取返回的 currency，不写死 MYR。
      "USD 12.50",
      "—",
      "Review required",
      "AMOUNT_MISMATCH",
      "—",
      "2026-11-08 16:00:00",
      "—",
    ]);
    expect(cellTexts(region, SECOND_ID).slice(4, 7)).toEqual(["Failed", "NO_GATEWAY_SESSION", "CLOSED"]);
  });

  it("names every one of the six statuses", async () => {
    const statuses = ["PENDING", "PAID", "FAILED", "EXPIRED", "CANCELLED", "REVIEW_REQUIRED"];
    serve(() =>
      page(
        statuses.map((status) => payment({ id: status, status })),
        statuses.length,
      ),
    );

    renderPage();
    await screen.findByText("PENDING");

    const region = listRegion();
    expect(statuses.map((status) => cellTexts(region, status)[4])).toEqual([
      "Pending",
      "Paid",
      "Failed",
      "Expired",
      "Cancelled",
      "Review required",
    ]);
  });
});

describe("PaymentsPage filters", () => {
  it("adds status to the query when a status is chosen, and drops it for all statuses", async () => {
    const get = serve(() => page([payment()], 1));

    renderPage();
    await screen.findByText(ZERO_UUID);
    await choose(screen.getByLabelText("Status"), "Review required");

    await waitFor(() => expect(listUrls(get)).toContain(`${LIST_FIRST_PAGE}&status=REVIEW_REQUIRED`));

    await choose(screen.getByLabelText("Status"), "All statuses");

    await waitFor(() => expect(listUrls(get).filter((url) => url === LIST_FIRST_PAGE).length).toBeGreaterThan(1));
    expect(listUrls(get).every((url) => !url.includes("status=ALL"))).toBe(true);
  });

  it("offers all statuses and the six statuses in the status filter", async () => {
    serve(() => page([payment()], 1));

    renderPage();
    await screen.findByText(ZERO_UUID);
    fireEvent.mouseDown(screen.getByLabelText("Status"));

    for (const label of ["All statuses", "Pending", "Paid", "Failed", "Expired", "Cancelled", "Review required"]) {
      expect((await screen.findAllByTitle(label)).length).toBeGreaterThan(0);
    }
  });

  it("adds customer_id to the query when a customer is chosen", async () => {
    const get = serve(() => page([payment()], 1));

    renderPage();
    await screen.findByText(ZERO_UUID);
    await choose(screen.getByLabelText("Customer"), "Acme Sdn Bhd");

    await waitFor(() => expect(listUrls(get)).toContain(`${LIST_FIRST_PAGE}&customer_id=${ZERO_UUID}`));
  });

  it("passes the created dates as the Malaysian calendar dates typed", async () => {
    const get = serve(() => page([payment()], 1));

    renderPage();
    await screen.findByText(ZERO_UUID);
    typeDate("Created from (MYT)", "2026-11-01");

    await waitFor(() => expect(listUrls(get)).toContain(`${LIST_FIRST_PAGE}&created_from=2026-11-01`));

    typeDate("Created to (MYT)", "2026-11-08");

    await waitFor(() =>
      expect(listUrls(get)).toContain(`${LIST_FIRST_PAGE}&created_from=2026-11-01&created_to=2026-11-08`),
    );
  });

  it("accepts the same day at both ends", async () => {
    const get = serve(() => page([payment()], 1));

    renderPage();
    await screen.findByText(ZERO_UUID);
    typeDate("Created from (MYT)", "2026-11-08");
    typeDate("Created to (MYT)", "2026-11-08");

    await waitFor(() =>
      expect(listUrls(get)).toContain(`${LIST_FIRST_PAGE}&created_from=2026-11-08&created_to=2026-11-08`),
    );
  });

  it("combines every filter", async () => {
    const get = serve(() => page([payment()], 1));

    renderPage();
    await screen.findByText(ZERO_UUID);
    await choose(screen.getByLabelText("Status"), "Paid");
    await choose(screen.getByLabelText("Customer"), "Acme Sdn Bhd");
    typeDate("Created from (MYT)", "2026-11-01");
    typeDate("Created to (MYT)", "2026-11-30");

    await waitFor(() =>
      expect(listUrls(get)).toContain(
        `${LIST_FIRST_PAGE}&status=PAID&customer_id=${ZERO_UUID}&created_from=2026-11-01&created_to=2026-11-30`,
      ),
    );
  });

  it("sends no request when the start date is later than the end date, and says so", async () => {
    const get = serve(() => page([payment()], 1));

    renderPage();
    await screen.findByText(ZERO_UUID);
    typeDate("Created to (MYT)", "2026-11-01");
    await waitFor(() => expect(listUrls(get)).toContain(`${LIST_FIRST_PAGE}&created_to=2026-11-01`));
    const before = listUrls(get).length;

    typeDate("Created from (MYT)", "2026-11-02");

    expect(await screen.findByText("The end date must not be earlier than the start date.")).toBeInTheDocument();
    expect(listUrls(get)).toHaveLength(before);
    expect(listUrls(get).some((url) => url.includes("created_from="))).toBe(false);
    expect(within(listRegion()).queryByRole("table")).not.toBeInTheDocument();
  });

  it("sends no request for a date that is not YYYY-MM-DD and says how to write it", async () => {
    const get = serve(() => page([payment()], 1));

    renderPage();
    await screen.findByText(ZERO_UUID);
    for (const value of ["2026-1-08", "20261108", "2026-11-08T00:00"]) {
      typeDate("Created from (MYT)", value);
      expect(await screen.findByText("Enter the date as YYYY-MM-DD, for example 2026-11-08.")).toBeInTheDocument();
    }

    expect(listUrls(get)).toEqual([LIST_FIRST_PAGE]);
  });

  it("goes back to the first page when a filter changes", async () => {
    const get = serve((query) => page([payment()], 45, Number(query.get("page"))));

    renderPage();
    await screen.findByText(ZERO_UUID);
    fireEvent.click(within(listRegion()).getByRole("button", { name: "Next" }));
    await waitFor(() => expect(listUrls(get)).toContain(`${PAYMENTS_URL}?page=2&page_size=20`));

    await choose(screen.getByLabelText("Status"), "Pending");

    await waitFor(() => expect(listUrls(get)).toContain(`${LIST_FIRST_PAGE}&status=PENDING`));
    expect(listUrls(get).some((url) => url.includes("page=2") && url.includes("status="))).toBe(false);
  });
});

describe("PaymentsPage paging", () => {
  it("asks for the next page with Next and enables the buttons by total", async () => {
    const get = serve((query) => page([payment()], 25, Number(query.get("page"))));

    renderPage();
    await screen.findByText(ZERO_UUID);

    const previous = within(listRegion()).getByRole("button", { name: "Previous" });
    const next = within(listRegion()).getByRole("button", { name: "Next" });
    expect(previous).toBeDisabled();
    expect(next).toBeEnabled();

    fireEvent.click(next);

    await waitFor(() => expect(listUrls(get)).toContain(`${PAYMENTS_URL}?page=2&page_size=20`));
    // 25 条、每页 20 条：第 2 页是末页。
    await waitFor(() => expect(within(listRegion()).getByRole("button", { name: "Next" })).toBeDisabled());
    expect(within(listRegion()).getByRole("button", { name: "Previous" })).toBeEnabled();

    fireEvent.click(within(listRegion()).getByRole("button", { name: "Previous" }));

    await waitFor(() => expect(within(listRegion()).getByRole("button", { name: "Previous" })).toBeDisabled());
    expect(listUrls(get).filter((url) => url === LIST_FIRST_PAGE).length).toBeGreaterThan(0);
  });

  it("disables both buttons when everything fits on one page", async () => {
    serve(() => page([payment()], 20));

    renderPage();
    await screen.findByText(ZERO_UUID);

    expect(within(listRegion()).getByRole("button", { name: "Previous" })).toBeDisabled();
    expect(within(listRegion()).getByRole("button", { name: "Next" })).toBeDisabled();
  });
});

describe("PaymentsPage row click", () => {
  it("goes to the payment's detail path when a row is clicked", async () => {
    serve(() => page([payment(), payment({ id: SECOND_ID })], 2));

    renderPage();
    await screen.findByText(ZERO_UUID);
    fireEvent.click(rowOf(listRegion(), SECOND_ID));

    expect(await screen.findByTestId("location")).toHaveTextContent(`/payments/${SECOND_ID}`);
  });

  it("goes to the detail path with Enter on a focused row", async () => {
    serve(() => page([payment()], 1));

    renderPage();
    await screen.findByText(ZERO_UUID);
    fireEvent.keyDown(rowOf(listRegion(), ZERO_UUID), { key: "Enter" });

    expect(await screen.findByTestId("location")).toHaveTextContent(`/payments/${ZERO_UUID}`);
  });
});

describe("PaymentsPage requests", () => {
  it("asks only admin endpoints, never a /api/v1/customer/ path", async () => {
    const get = serve(() => page([payment()], 45));

    renderPage();
    await screen.findByText(ZERO_UUID);
    await choose(screen.getByLabelText("Customer"), "Acme Sdn Bhd");
    await choose(screen.getByLabelText("Status"), "Paid");
    typeDate("Created from (MYT)", "2026-11-01");
    await waitFor(() => expect(listUrls(get).some((url) => url.includes("created_from="))).toBe(true));
    fireEvent.click(within(listRegion()).getByRole("button", { name: "Next" }));
    await waitFor(() => expect(listUrls(get).some((url) => url.includes("page=2"))).toBe(true));

    expect(urls(get).length).toBeGreaterThan(0);
    expect(urls(get).filter((url) => url.startsWith("/api/v1/customer/"))).toEqual([]);
    expect(urls(get).every((url) => url.startsWith("/api/v1/admin/"))).toBe(true);
  });
});
