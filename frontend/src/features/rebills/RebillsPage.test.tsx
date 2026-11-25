/**
 * 管理端 Rebill 列表页与详情页（AIH-TASK-257，设计闸门 #830 v12 §2「管理端页面」）。
 *
 * 与供应商成本页的测试同一写法：不 mock `api/adminRebills`，而是在 `client.get` 上按路径回应，钉住**页面真正发出去的地址与
 * 查询参数**（按客户筛的 customer_id、翻页的 cursor：Next 用 next_cursor、Previous 回到上一页的游标、首页不带 cursor），并断言
 * 没有问 `/api/v1/customer/` 路径。金额都是 8 位小数的字符串，页面原样显示。
 *
 * uuid 一律是全零占位值；别的 id 只用来区分行与页，原样进路径，页面不解析它们。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import type { RebillDetail, RebillPage, RebillSummary } from "../../api/adminRebills";
import { ApiError, client } from "../../api/client";
import { ROUTES, rebillDetailPath, usageEventDetailPath } from "../../routes/paths";
import { RebillDetailPage } from "./RebillDetailPage";
import { RebillsPage } from "./RebillsPage";

const ZERO_UUID = "00000000-0000-0000-0000-000000000000";
const BASE = "/api/v1/admin/rebills";
const DASH = "—";

function summary(overrides: Partial<RebillSummary> = {}): RebillSummary {
  return {
    id: "rebill-credit",
    customer_id: ZERO_UUID,
    event_count: 3,
    difference: "-0.10000001",
    ledger_type: "REBILL_CREDIT",
    // 经过一次浮点数就不是它了。
    ledger_amount: "0.10000001",
    reason: "Price version fix",
    created_by_email: "admin@example.com",
    created_at: "2026-11-20T03:00:00",
    ...overrides,
  };
}

function page(items: RebillSummary[], nextCursor: string | null = null): RebillPage {
  return { items, next_cursor: nextCursor };
}

function detail(overrides: Partial<RebillDetail> = {}): RebillDetail {
  return {
    ...summary(),
    previous_total: "1.25000000",
    corrected_total: "1.14999999",
    corrective_pricing_rule_id: "rule-2",
    corrective_provider_price_version_id: null,
    wallet_transaction_id: ZERO_UUID,
    lines: [
      {
        rebill_id: "rebill-credit",
        usage_event_id: "event-1",
        occurred_at: "2026-11-02T01:00:00",
        project: null,
        provider: "anthropic",
        model: "claude-x",
        previous_charge: "1.25000000",
        corrected_charge: "1.14999999",
        difference: "-0.10000001",
        previous_pricing_rule: "rule-1",
        corrected_pricing_rule: "rule-2",
        previous_provider_price_version: "price-1",
        corrected_provider_price_version: "price-1",
        corrected_estimated_provider_cost_myr: "0.50000001",
        original_period: "2026-11",
        original_statement_id: null,
      },
      {
        rebill_id: "rebill-credit",
        usage_event_id: "event-2",
        occurred_at: "2026-10-02T01:00:00",
        project: "project-1",
        provider: "anthropic",
        model: "claude-x",
        previous_charge: "2.00000000",
        corrected_charge: "2.00000001",
        difference: "0.00000001",
        previous_pricing_rule: "rule-1",
        corrected_pricing_rule: "rule-2",
        previous_provider_price_version: "price-1",
        corrected_provider_price_version: "price-1",
        corrected_estimated_provider_cost_myr: "0.70000000",
        original_period: "2026-10",
        original_statement_id: "statement-10",
      },
    ],
    ...overrides,
  };
}

function envelope(data: unknown) {
  return { data: { success: true, data, error: null, request_id: "req-1" } };
}

function respond(reply: unknown) {
  return reply instanceof Error ? Promise.reject(reply) : Promise.resolve(envelope(reply));
}

/**
 * 按路径回应 `client.get`：列表交给 `list`（参数是查询串），某一条的详情交给 `detailOf`（参数是路径里的 id）。别的路径一律当成
 * 失败 —— 页面不该问它们，下面也会断言没有问。
 */
function serveGet(list: (query: URLSearchParams) => unknown, detailOf: (id: string) => unknown = () => detail()) {
  return vi.spyOn(client, "get").mockImplementation((url: string) => {
    const parsed = new URL(url, "https://admin.example");
    if (parsed.pathname === BASE) {
      return respond(list(parsed.searchParams));
    }
    if (parsed.pathname.startsWith(`${BASE}/`)) {
      return respond(detailOf(parsed.pathname.slice(BASE.length + 1)));
    }
    return respond(new ApiError("NOT_FOUND", "Unexpected request.", null));
  });
}

function renderAt(path: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path={ROUTES.rebills} element={<RebillsPage />} />
          <Route path={ROUTES.rebillDetail} element={<RebillDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

type GetSpy = ReturnType<typeof serveGet>;

function urls(spy: GetSpy): string[] {
  return spy.mock.calls.map(([url]) => String(url));
}

/** 列表请求的查询参数，按发出的顺序。 */
function listQueries(spy: GetSpy): Record<string, string>[] {
  return urls(spy)
    .map((url) => new URL(url, "https://admin.example"))
    .filter((parsed) => parsed.pathname === BASE)
    .map((parsed) => Object.fromEntries(parsed.searchParams));
}

function lastListQuery(spy: GetSpy): Record<string, string> | undefined {
  return listQueries(spy).at(-1);
}

function expectAdminOnly(spy: GetSpy): void {
  expect(urls(spy).filter((url) => url.startsWith("/api/v1/customer/"))).toEqual([]);
}

function listTable(): HTMLTableElement {
  const table = screen.getByRole("region", { name: "Rebill list" }).querySelector("table");
  if (table === null) {
    throw new Error("no rebill table");
  }
  return table;
}

function headersOf(table: HTMLTableElement): string[] {
  return Array.from(table.querySelectorAll(":scope > thead > tr > th"), (th) => th.textContent ?? "");
}

function rowOf(table: HTMLTableElement, key: string): HTMLElement {
  const tr = table.querySelector<HTMLElement>(`:scope > tbody > tr[data-row-key="${key}"]`);
  if (tr === null) {
    throw new Error(`no row ${key}`);
  }
  return tr;
}

function rowKeys(table: HTMLTableElement): (string | null)[] {
  return Array.from(table.querySelectorAll(":scope > tbody > tr[data-row-key]"), (tr) =>
    tr.getAttribute("data-row-key"),
  );
}

/** 表里 key 那一行、列头为 header 的那一格的文字。 */
function cell(table: HTMLTableElement, key: string, header: string): string | null {
  const index = headersOf(table).indexOf(header);
  if (index < 0) {
    throw new Error(`no column ${header}`);
  }
  return rowOf(table, key).querySelectorAll(":scope > td")[index]?.textContent ?? null;
}

/** 详情头部（带边框的 Descriptions：label 在 <th>，值在紧跟的 <td>）里 label 那一项的文字。 */
function summaryValue(label: string): string | null {
  const th = Array.from(document.querySelectorAll("th")).find(
    (el) => el.textContent === label && el.nextElementSibling?.tagName === "TD",
  );
  if (th === undefined) {
    throw new Error(`no summary item ${label}`);
  }
  return th.nextElementSibling?.textContent ?? null;
}

function linesTable(): HTMLTableElement {
  const table = Array.from(document.querySelectorAll("table")).find((el) =>
    headersOf(el).includes("Usage event"),
  );
  if (table === undefined) {
    throw new Error("no lines table");
  }
  return table;
}

function button(name: string): HTMLButtonElement {
  return screen.getByRole<HTMLButtonElement>("button", { name });
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("rebill list", () => {
  it("shows the columns and each rebill as returned, with Credit / Debit and the ledger amount string", async () => {
    const get = serveGet(() =>
      page([
        summary(),
        summary({
          id: "rebill-debit",
          customer_id: "customer-2",
          event_count: 1,
          difference: "0.20000001",
          ledger_type: "REBILL_DEBIT",
          ledger_amount: "-0.20000001",
          reason: "Rule fix",
          created_by_email: "other@example.com",
        }),
      ]),
    );

    renderAt(ROUTES.rebills);

    await screen.findByText("Price version fix");
    const table = listTable();
    expect(headersOf(table)).toEqual(["Created", "Customer", "Events", "Ledger", "Amount", "Reason", "By"]);
    expect(rowKeys(table)).toEqual(["rebill-credit", "rebill-debit"]);
    expect(cell(table, "rebill-credit", "Customer")).toBe(ZERO_UUID);
    expect(cell(table, "rebill-credit", "Events")).toBe("3");
    expect(cell(table, "rebill-credit", "Ledger")).toBe("Credit");
    expect(cell(table, "rebill-credit", "Amount")).toBe("0.10000001");
    expect(cell(table, "rebill-credit", "By")).toBe("admin@example.com");
    expect(cell(table, "rebill-debit", "Customer")).toBe("customer-2");
    expect(cell(table, "rebill-debit", "Events")).toBe("1");
    expect(cell(table, "rebill-debit", "Ledger")).toBe("Debit");
    expect(cell(table, "rebill-debit", "Amount")).toBe("-0.20000001");
    expect(cell(table, "rebill-debit", "Reason")).toBe("Rule fix");

    // 首页不带 cursor，也不带客户。
    expect(listQueries(get)).toEqual([{ limit: "50" }]);
    expectAdminOnly(get);
  });

  it("says there are no rebills yet when the first page is empty, with Next disabled", async () => {
    const get = serveGet(() => page([]));

    renderAt(ROUTES.rebills);

    await screen.findByText("No rebills yet.");
    expect(rowKeys(listTable())).toEqual([]);
    expect(button("Next").disabled).toBe(true);
    expect(button("Previous").disabled).toBe(true);
    expectAdminOnly(get);
  });

  it("filters by the customer public_id typed in, starting again from the first page", async () => {
    const get = serveGet((query) =>
      query.get("customer_id") === "customer-2"
        ? page([summary({ id: "rebill-debit", customer_id: "customer-2", ledger_type: "REBILL_DEBIT" })])
        : page([summary()], "rebill-credit"),
    );

    renderAt(ROUTES.rebills);
    await screen.findByText("Price version fix");
    fireEvent.click(button("Next"));
    await waitFor(() => expect(lastListQuery(get)).toEqual({ cursor: "rebill-credit", limit: "50" }));

    fireEvent.change(screen.getByLabelText("Customer ID"), { target: { value: "  customer-2 " } });
    fireEvent.click(button("Apply filter"));

    await waitFor(() => expect(lastListQuery(get)).toEqual({ customer_id: "customer-2", limit: "50" }));
    await waitFor(() => expect(rowKeys(listTable())).toEqual(["rebill-debit"]));
    expect(cell(listTable(), "rebill-debit", "Ledger")).toBe("Debit");
    expect(button("Previous").disabled).toBe(true);
  });

  it("says no rebills for this customer when the filtered page is empty", async () => {
    serveGet((query) => (query.has("customer_id") ? page([]) : page([summary()])));

    renderAt(ROUTES.rebills);
    await screen.findByText("Price version fix");
    fireEvent.change(screen.getByLabelText("Customer ID"), { target: { value: "missing" } });
    fireEvent.click(button("Apply filter"));

    await screen.findByText("No rebills for this customer.");
  });

  it("pages with next_cursor forward and the saved cursor stack backward", async () => {
    const pages: Record<string, RebillPage> = {
      "": page([summary({ id: "r-1", reason: "Page one" })], "r-1"),
      "r-1": page([summary({ id: "r-2", reason: "Page two" })], "r-2"),
      "r-2": page([summary({ id: "r-3", reason: "Page three" })], null),
    };
    const get = serveGet((query) => pages[query.get("cursor") ?? ""]);

    renderAt(ROUTES.rebills);
    await screen.findByText("Page one");
    expect(lastListQuery(get)).toEqual({ limit: "50" });
    expect(button("Previous").disabled).toBe(true);

    fireEvent.click(button("Next"));
    await screen.findByText("Page two");
    expect(lastListQuery(get)).toEqual({ cursor: "r-1", limit: "50" });

    fireEvent.click(button("Next"));
    await screen.findByText("Page three");
    expect(lastListQuery(get)).toEqual({ cursor: "r-2", limit: "50" });
    // 最后一页：next_cursor 为 null。
    await waitFor(() => expect(button("Next").disabled).toBe(true));

    fireEvent.click(button("Previous"));
    await screen.findByText("Page two");
    await waitFor(() => expect(lastListQuery(get)).toEqual({ cursor: "r-1", limit: "50" }));

    fireEvent.click(button("Previous"));
    await screen.findByText("Page one");
    await waitFor(() => expect(lastListQuery(get)).toEqual({ limit: "50" }));
    await waitFor(() => expect(button("Previous").disabled).toBe(true));
    expectAdminOnly(get);
  });

  it("opens the detail page when a row is clicked", async () => {
    const get = serveGet(() => page([summary()]));

    renderAt(ROUTES.rebills);
    await screen.findByText("Price version fix");
    fireEvent.click(rowOf(listTable(), "rebill-credit"));

    await screen.findByText("Rebill details");
    expect(urls(get)).toContain(`${BASE}/rebill-credit`);
  });

  it("shows an error with the request id when the list cannot be loaded", async () => {
    serveGet(() => new ApiError("INTERNAL_ERROR", "Something went wrong.", "req-500"));

    renderAt(ROUTES.rebills);

    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText("The rebills could not be loaded.")).toBeTruthy();
    expect(alert.textContent).toContain("req-500");
  });

  it("says only administrators can view rebills on a 403", async () => {
    serveGet(() => new ApiError("ADMIN_REQUIRED", "Admin required.", "req-403"));

    renderAt(ROUTES.rebills);

    expect(await screen.findByText("Only administrators can view rebills.")).toBeTruthy();
  });
});

describe("rebill detail", () => {
  it("shows the header from the response strings and every line", async () => {
    const get = serveGet(
      () => page([]),
      (id) => (id === "rebill-credit" ? detail() : new ApiError("REBILL_NOT_FOUND", "Not found.", "req-404")),
    );

    renderAt(rebillDetailPath("rebill-credit"));

    await screen.findByText("Rebill details");
    expect(summaryValue("Customer")).toBe(ZERO_UUID);
    expect(summaryValue("Events")).toBe("3");
    expect(summaryValue("Previous total (MYR)")).toBe("1.25000000");
    expect(summaryValue("Corrected total (MYR)")).toBe("1.14999999");
    expect(summaryValue("Difference (MYR)")).toBe("-0.10000001");
    expect(summaryValue("Ledger")).toBe("Credit");
    expect(summaryValue("Ledger amount (MYR)")).toBe("0.10000001");
    expect(summaryValue("Reason")).toBe("Price version fix");
    expect(summaryValue("Confirmed by")).toBe("admin@example.com");
    // 只有价格版本为 null：只那一项显示 Current version。
    expect(summaryValue("Corrective pricing rule")).toBe("rule-2");
    expect(summaryValue("Corrective provider price version")).toBe("Current version");
    expect(screen.getAllByText("Current version")).toHaveLength(1);

    const table = linesTable();
    expect(headersOf(table)).toEqual([
      "Usage event",
      "Occurred",
      "Previous",
      "Corrected",
      "Difference",
      "Est. cost (MYR)",
      "Original period",
      "Statement",
    ]);
    expect(rowKeys(table)).toEqual(["event-1", "event-2"]);
    expect(cell(table, "event-1", "Previous")).toBe("1.25000000");
    expect(cell(table, "event-1", "Corrected")).toBe("1.14999999");
    expect(cell(table, "event-1", "Difference")).toBe("-0.10000001");
    expect(cell(table, "event-1", "Est. cost (MYR)")).toBe("0.50000001");
    expect(cell(table, "event-1", "Original period")).toBe("2026-11");
    expect(cell(table, "event-1", "Statement")).toBe(DASH);
    expect(cell(table, "event-2", "Statement")).toBe("statement-10");
    expect(cell(table, "event-2", "Est. cost (MYR)")).toBe("0.70000000");

    // Usage event 是指向用量事件详情的链接；其余 public_id 是纯文本。
    const link = within(rowOf(table, "event-1")).getByRole("link", { name: "event-1" });
    expect(link.getAttribute("href")).toBe(usageEventDetailPath("event-1"));
    expect(within(rowOf(table, "event-2")).getAllByRole("link")).toHaveLength(1);

    expect(urls(get)).toEqual([`${BASE}/rebill-credit`]);
    expectAdminOnly(get);
  });

  it("shows Current version only for the corrective pricing rule when only that one is null", async () => {
    serveGet(
      () => page([]),
      () =>
        detail({
          ledger_type: "REBILL_DEBIT",
          corrective_pricing_rule_id: null,
          corrective_provider_price_version_id: "price-2",
        }),
    );

    renderAt(rebillDetailPath("rebill-credit"));

    await screen.findByText("Rebill details");
    expect(summaryValue("Corrective pricing rule")).toBe("Current version");
    expect(summaryValue("Corrective provider price version")).toBe("price-2");
    expect(screen.getAllByText("Current version")).toHaveLength(1);
    expect(summaryValue("Ledger")).toBe("Debit");
  });

  it("says the rebill was not found on a 404", async () => {
    serveGet(
      () => page([]),
      () => new ApiError("REBILL_NOT_FOUND", "Not found.", "req-404"),
    );

    renderAt(rebillDetailPath("missing"));

    expect(await screen.findByText("Rebill not found")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Back to rebills" }).getAttribute("href")).toBe(ROUTES.rebills);
  });

  it("shows an error with the request id when the detail cannot be loaded", async () => {
    serveGet(
      () => page([]),
      () => new ApiError("INTERNAL_ERROR", "Something went wrong.", "req-500"),
    );

    renderAt(rebillDetailPath("rebill-credit"));

    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText("The rebill could not be loaded.")).toBeTruthy();
    expect(alert.textContent).toContain("req-500");
  });
});
