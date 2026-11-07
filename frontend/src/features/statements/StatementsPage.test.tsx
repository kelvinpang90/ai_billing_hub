/**
 * 管理端月度对账单页（AIH-TASK-161，设计闸门 #565 v6 §2「前端」）。
 *
 * 与钱包核对页的测试同一写法：不 mock `api/adminStatements`，而是在 `client.get` / `client.post` 上按路径回应，钉住
 * **页面真正发出去的地址**（列表的分页与筛选查询串、点一行后问的是哪一份、补生成发给了哪个客户），并断言没有问
 * `/api/v1/customer/` 路径。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import type { CustomerSummary, Page } from "../../api/adminCustomers";
import type { StatementDetail, StatementSummary } from "../../api/adminStatements";
import { ApiError, client } from "../../api/client";
import { ROUTES } from "../../routes/paths";
import { StatementsPage } from "./StatementsPage";

const ZERO_UUID = "00000000-0000-0000-0000-000000000000";
/** 第二份对账单的 id：只用来区分两行，原样进路径，页面不解析它。 */
const SECOND_ID = "second-statement";
const STATEMENTS_URL = "/api/v1/admin/statements";
const CUSTOMERS_URL = "/api/v1/admin/customers";
const GENERATE_URL = `${CUSTOMERS_URL}/${ZERO_UUID}/statements/generate`;
const LIST_FIRST_PAGE = `${STATEMENTS_URL}?page=1&page_size=20`;
const LIST_FOR_CUSTOMER = `${LIST_FIRST_PAGE}&customer_id=${ZERO_UUID}`;

/** 快照里的税务说明原文：首尾空白与换行都要原样留着。 */
const TAX_NOTICE_EN = "  Prices include SST at 8%.\nSST registration No. W10-0000-00000000.  ";

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

function summary(overrides: Partial<StatementSummary> = {}): StatementSummary {
  return {
    statement_id: ZERO_UUID,
    statement_number: "ST-202612-0A1B2C3D",
    customer_id: ZERO_UUID,
    company_name: "Acme Sdn Bhd",
    period: "2026-12",
    status: "FINALIZED",
    closing_balance: "92.00000000",
    finalized_at: "2027-01-01T17:00:00",
    ...overrides,
  };
}

function detail(overrides: Partial<StatementSummary> = {}): StatementDetail {
  const base = summary(overrides);
  return {
    ...base,
    cutoff_at: "2027-01-01T16:00:00",
    ledger_high_sequence: 42,
    generated_by_email: null,
    snapshot: {
      schema_version: 1,
      period: base.period,
      currency: "MYR",
      company_name: base.company_name,
      tax_notice: { zh: "价格含 8% 销售与服务税。", en: TAX_NOTICE_EN },
      opening_balance: "100.00000000",
      total_topup: "50.00000000",
      total_ai_usage: "-60.25000000",
      total_adjustments: "5.00000000",
      prior_period: {
        usage: [
          { usage_period: "2026-10", request_count: 3, amount: "-1.50000000" },
          { usage_period: "2026-11", request_count: 2, amount: "-0.75000000" },
        ],
        rebill: "-0.50000000",
        total: "-2.75000000",
      },
      closing_balance: "92.00000000",
      request_count: 12,
      conversation_count: 4,
      input_tokens: 1500,
      output_tokens: 700,
      total_customer_charges: "60.25000000",
      by_provider: [
        { provider_code: "openai", request_count: 12, input_tokens: 1500, output_tokens: 700, charges: "60.25000000" },
      ],
      by_model: [
        {
          provider_code: "openai",
          model_code: "gpt-4o",
          request_count: 12,
          input_tokens: 1500,
          output_tokens: 700,
          charges: "60.25000000",
        },
      ],
      by_project: [
        {
          project_public_id: ZERO_UUID,
          project_name: "Support bot",
          request_count: 12,
          input_tokens: 1500,
          output_tokens: 700,
          charges: "60.25000000",
        },
      ],
    },
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
 * 按路径回应 `client.get`：客户列表交给 `customers`，对账单列表交给 `list`，某一份的详情交给 `detailOf`（参数是路径
 * 里的 id）。别的路径一律当成失败 —— 页面不该问它们，下面也会断言没有问。
 */
function serve(
  list: Reply,
  detailOf: (statementId: string) => unknown = () => detail(),
  customers: Reply = () => page([customer()], 1),
) {
  return vi.spyOn(client, "get").mockImplementation((url: string) => {
    const parsed = new URL(url, "https://admin.example");
    let reply: unknown;
    if (parsed.pathname === CUSTOMERS_URL) {
      reply = customers(parsed.searchParams);
    } else if (parsed.pathname === STATEMENTS_URL) {
      reply = list(parsed.searchParams);
    } else if (parsed.pathname.startsWith(`${STATEMENTS_URL}/`)) {
      reply = detailOf(parsed.pathname.slice(STATEMENTS_URL.length + 1));
    } else {
      reply = new ApiError("NOT_FOUND", "Unexpected request.", null);
    }
    if (reply === PENDING) {
      return new Promise(() => undefined);
    }
    return reply instanceof Error ? Promise.reject(reply) : Promise.resolve(envelope(reply));
  });
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[ROUTES.statements]}>
        <StatementsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function listRegion(): HTMLElement {
  return screen.getByRole("region", { name: "Statements" });
}

function detailRegion(): Promise<HTMLElement> {
  return screen.findByRole("region", { name: "Statement details" });
}

function queryDetailRegion(): HTMLElement | null {
  return screen.queryByRole("region", { name: "Statement details" });
}

/** 一张表的表头文字，按显示顺序。 */
function headersOf(region: HTMLElement): string[] {
  return within(region)
    .getAllByRole("columnheader")
    .map((th) => th.textContent ?? "");
}

/** 一张表里 `data-row-key` 为 key 的那一行。 */
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

/** 带边框的 Descriptions：label 在 <th>，值在紧跟的 <td>。 */
function valueOf(container: HTMLElement, label: string): string | null {
  const th = Array.from(container.querySelectorAll("th")).find(
    (candidate) => candidate.textContent === label && candidate.nextElementSibling?.tagName === "TD",
  );
  if (th === undefined) {
    throw new Error(`no item ${label}`);
  }
  return th.nextElementSibling?.textContent ?? null;
}

function urls(get: ReturnType<typeof serve>): string[] {
  return get.mock.calls.map(([url]) => url);
}

function listUrls(get: ReturnType<typeof serve>): string[] {
  return urls(get).filter((url) => url.startsWith(`${STATEMENTS_URL}?`));
}

/** antd 下拉在 mousedown 时打开；选项按 title 找、用 fireEvent 点（同定价规则页的用例）。 */
async function choose(field: HTMLElement, option: string): Promise<void> {
  fireEvent.mouseDown(field);
  const candidates = await screen.findAllByTitle(option);
  const item = candidates.find((candidate) => candidate.closest(".ant-select-item") !== null) ?? candidates[0];
  if (item === undefined) {
    throw new Error(`no option "${option}"`);
  }
  fireEvent.click(item);
}

async function chooseCustomer(): Promise<void> {
  await choose(screen.getByLabelText("Customer"), "Acme Sdn Bhd");
}

function typePeriod(value: string): void {
  fireEvent.change(screen.getByLabelText("Period"), { target: { value } });
}

async function openRow(key = ZERO_UUID): Promise<HTMLElement> {
  await screen.findByText("ST-202612-0A1B2C3D");
  fireEvent.click(rowOf(listRegion(), key));
  return detailRegion();
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("StatementsPage list", () => {
  it("shows a loading state while the statements are on their way", async () => {
    serve(() => PENDING);

    renderPage();

    expect(await screen.findByText("Loading statements…")).toBeInTheDocument();
    expect(within(listRegion()).queryByRole("table")).not.toBeInTheDocument();
  });

  it("shows the backend message and the request id when the statements fail, with a retry", async () => {
    serve(() => new ApiError("INTERNAL_ERROR", "An unexpected error occurred.", "req-500"));

    renderPage();

    expect(await screen.findByText("The statements could not be loaded.")).toBeInTheDocument();
    expect(screen.getByText(/An unexpected error occurred\./)).toBeInTheDocument();
    expect(screen.getByText("req-500")).toBeInTheDocument();
    expect(within(listRegion()).getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("says there is no permission on a 403", async () => {
    serve(() => new ApiError("ADMIN_REQUIRED", "Administrator access is required.", "req-403"));

    renderPage();

    expect(await screen.findByText("Only administrators can view statements.")).toBeInTheDocument();
    expect(screen.getByText("req-403")).toBeInTheDocument();
    expect(screen.queryByText("The statements could not be loaded.")).not.toBeInTheDocument();
  });

  it("says so when there are no statements yet", async () => {
    serve(() => page([], 0));

    renderPage();

    expect(await screen.findByText("No statements yet.")).toBeInTheDocument();
    expect(within(listRegion()).queryByRole("table")).not.toBeInTheDocument();
  });

  it("lists the statements with their columns, Kuala Lumpur times and MYR amounts", async () => {
    const get = serve(() => page([summary()], 1));

    renderPage();
    await screen.findByText("ST-202612-0A1B2C3D");

    const region = listRegion();
    expect(headersOf(region)).toEqual([
      "Statement number",
      "Customer",
      "Period",
      "Status",
      "Closing balance",
      "Finalized at (MYT)",
    ]);
    expect(rowKeys(region)).toEqual([ZERO_UUID]);
    expect(cellTexts(region, ZERO_UUID)).toEqual([
      "ST-202612-0A1B2C3D",
      "Acme Sdn Bhd",
      "2026-12",
      "Finalized",
      "MYR 92.00",
      // 17:00 UTC → 次日 01:00 吉隆坡时间。
      "2027-01-02 01:00:00",
    ]);
    expect(listUrls(get)).toEqual([LIST_FIRST_PAGE]);
    // 客户下拉的选项逐页取全，用的是客户列表接口。
    expect(urls(get)).toContain(`${CUSTOMERS_URL}?page=1&page_size=100`);
  });

  it("says the closing balance follows T+1 attribution and is not the month-end wallet balance", async () => {
    serve(() => page([summary()], 1));

    renderPage();
    await screen.findByText("ST-202612-0A1B2C3D");

    expect(
      screen.getByText(
        "The closing balance is worked out with T+1 attribution. It is not the wallet balance at the moment the month ended.",
      ),
    ).toBeInTheDocument();
  });

  it("asks for the next page with Next, enables the buttons by total, and has no page size choice", async () => {
    const get = serve((query) => page([summary()], 25, Number(query.get("page"))));

    renderPage();
    await screen.findByText("ST-202612-0A1B2C3D");

    const previous = within(listRegion()).getByRole("button", { name: "Previous" });
    const next = within(listRegion()).getByRole("button", { name: "Next" });
    expect(previous).toBeDisabled();
    expect(next).toBeEnabled();
    expect(screen.queryByRole("combobox", { name: /page/i })).not.toBeInTheDocument();

    fireEvent.click(next);

    await waitFor(() => expect(listUrls(get)).toContain(`${STATEMENTS_URL}?page=2&page_size=20`));
    // 25 条、每页 20 条：第 2 页是末页。
    await waitFor(() => expect(within(listRegion()).getByRole("button", { name: "Next" })).toBeDisabled());
    expect(within(listRegion()).getByRole("button", { name: "Previous" })).toBeEnabled();
  });

  it("disables both buttons when everything fits on one page", async () => {
    serve(() => page([summary()], 1));

    renderPage();
    await screen.findByText("ST-202612-0A1B2C3D");

    expect(within(listRegion()).getByRole("button", { name: "Previous" })).toBeDisabled();
    expect(within(listRegion()).getByRole("button", { name: "Next" })).toBeDisabled();
  });
});

describe("StatementsPage filters", () => {
  it("adds customer_id to the query when a customer is chosen", async () => {
    const get = serve(() => page([summary()], 1));

    renderPage();
    await screen.findByText("ST-202612-0A1B2C3D");
    await chooseCustomer();

    await waitFor(() => expect(listUrls(get)).toContain(LIST_FOR_CUSTOMER));
  });

  it("adds period to the query when a valid period is typed", async () => {
    const get = serve(() => page([summary()], 1));

    renderPage();
    await screen.findByText("ST-202612-0A1B2C3D");
    typePeriod("2026-12");

    await waitFor(() => expect(listUrls(get)).toContain(`${LIST_FIRST_PAGE}&period=2026-12`));
  });

  it("combines both filters", async () => {
    const get = serve(() => page([summary()], 1));

    renderPage();
    await screen.findByText("ST-202612-0A1B2C3D");
    await chooseCustomer();
    typePeriod("2026-12");

    await waitFor(() => expect(listUrls(get)).toContain(`${LIST_FOR_CUSTOMER}&period=2026-12`));
  });

  it("sends no request for a period that is not YYYY-MM and says how to write it", async () => {
    const get = serve(() => page([summary()], 1));

    renderPage();
    await screen.findByText("ST-202612-0A1B2C3D");
    for (const value of ["2026-13", "2026-1", "202612", "1999-12"]) {
      typePeriod(value);
      expect(await screen.findByText("Enter the period as YYYY-MM, for example 2026-12.")).toBeInTheDocument();
    }

    expect(listUrls(get)).toEqual([LIST_FIRST_PAGE]);
    expect(within(listRegion()).queryByRole("table")).not.toBeInTheDocument();
  });

  it("says no statements match when a filter finds none", async () => {
    serve((query) => (query.has("period") ? page([], 0) : page([summary()], 1)));

    renderPage();
    await screen.findByText("ST-202612-0A1B2C3D");
    typePeriod("2027-05");

    expect(await screen.findByText("No statements match these filters.")).toBeInTheDocument();
  });

  it("goes back to the first page when a filter changes", async () => {
    const get = serve((query) => page([summary()], 45, Number(query.get("page"))));

    renderPage();
    await screen.findByText("ST-202612-0A1B2C3D");
    fireEvent.click(within(listRegion()).getByRole("button", { name: "Next" }));
    await waitFor(() => expect(listUrls(get)).toContain(`${STATEMENTS_URL}?page=2&page_size=20`));

    typePeriod("2026-12");

    await waitFor(() => expect(listUrls(get)).toContain(`${LIST_FIRST_PAGE}&period=2026-12`));
    expect(listUrls(get).some((url) => url.includes("page=2") && url.includes("period="))).toBe(false);
  });
});

describe("StatementsPage detail", () => {
  it("asks for nothing until a row is selected", async () => {
    const get = serve(() => page([summary()], 1));

    renderPage();
    await screen.findByText("ST-202612-0A1B2C3D");

    expect(screen.getByText("Select a statement to see its details.")).toBeInTheDocument();
    expect(urls(get).some((url) => url.startsWith(`${STATEMENTS_URL}/`))).toBe(false);
    expect(queryDetailRegion()).toBeNull();
  });

  it("shows every field of the selected statement below the list", async () => {
    const get = serve(() => page([summary()], 1));

    renderPage();
    const region = await openRow();
    await within(region).findByText("Statement number");

    expect(urls(get)).toContain(`${STATEMENTS_URL}/${ZERO_UUID}`);
    expect(valueOf(region, "Statement number")).toBe("ST-202612-0A1B2C3D");
    expect(valueOf(region, "Customer")).toBe("Acme Sdn Bhd");
    expect(valueOf(region, "Period")).toBe("2026-12");
    expect(valueOf(region, "Status")).toBe("Finalized");
    expect(valueOf(region, "Finalized at (MYT)")).toBe("2027-01-02 01:00:00");
    expect(valueOf(region, "T+1 cut-off (MYT)")).toBe("2027-01-02 00:00:00");
    expect(valueOf(region, "Generated by")).toBe("Generated automatically");
    expect(valueOf(region, "Ledger high sequence")).toBe("42");

    const balance = within(region).getByRole("region", { name: "Balance" });
    expect(valueOf(balance, "Opening balance")).toBe("MYR 100.00");
    expect(valueOf(balance, "Top-ups")).toBe("MYR 50.00");
    expect(valueOf(balance, "AI usage charges this period")).toBe("MYR -60.25");
    expect(valueOf(balance, "Adjustments")).toBe("MYR 5.00");
    expect(valueOf(region, "Closing balance")).toBe("MYR 92.00");

    const usage = within(region).getByRole("region", { name: "Usage this period" });
    expect(valueOf(usage, "Requests")).toBe("12");
    expect(valueOf(usage, "Conversations")).toBe("4");
    expect(valueOf(usage, "Input tokens")).toBe("1500");
    expect(valueOf(usage, "Output tokens")).toBe("700");
    expect(valueOf(usage, "Customer charges")).toBe("MYR 60.25");
  });

  it("shows the generating administrator's email when there is one", async () => {
    serve(
      () => page([summary()], 1),
      () => ({ ...detail(), generated_by_email: "admin@example.com" }),
    );

    renderPage();
    const region = await openRow();
    await within(region).findByText("Statement number");

    expect(valueOf(region, "Generated by")).toBe("admin@example.com");
  });

  it("lists the prior-period adjustments one row per usage period, then rebill and the subtotal", async () => {
    serve(() => page([summary()], 1));

    renderPage();
    const region = await openRow();
    const prior = await within(region).findByRole("region", { name: "Prior-period adjustments" });

    expect(headersOf(prior)).toEqual(["Usage period", "Requests", "Amount"]);
    expect(rowKeys(prior)).toEqual(["usage-2026-10", "usage-2026-11", "rebill", "total"]);
    expect(cellTexts(prior, "usage-2026-10")).toEqual(["2026-10", "3", "MYR -1.50"]);
    expect(cellTexts(prior, "usage-2026-11")).toEqual(["2026-11", "2", "MYR -0.75"]);
    expect(cellTexts(prior, "rebill")).toEqual(["Rebill", "—", "MYR -0.50"]);
    expect(cellTexts(prior, "total")).toEqual(["Subtotal", "—", "MYR -2.75"]);
  });

  it("shows the provider, model and project breakdowns", async () => {
    serve(() => page([summary()], 1));

    renderPage();
    const region = await openRow();

    const byProvider = await within(region).findByRole("region", { name: "By provider" });
    expect(headersOf(byProvider)).toEqual(["Provider", "Requests", "Input tokens", "Output tokens", "Charges"]);
    expect(cellTexts(byProvider, "openai")).toEqual(["openai", "12", "1500", "700", "MYR 60.25"]);

    const byModel = within(region).getByRole("region", { name: "By model" });
    expect(headersOf(byModel)).toEqual(["Provider", "Model", "Requests", "Input tokens", "Output tokens", "Charges"]);
    expect(cellTexts(byModel, "openai/gpt-4o")).toEqual(["openai", "gpt-4o", "12", "1500", "700", "MYR 60.25"]);

    const byProject = within(region).getByRole("region", { name: "By project" });
    expect(headersOf(byProject)).toEqual(["Project", "Requests", "Input tokens", "Output tokens", "Charges"]);
    expect(cellTexts(byProject, ZERO_UUID)).toEqual(["Support bot", "12", "1500", "700", "MYR 60.25"]);
  });

  it("says there is no usage when a breakdown is empty", async () => {
    const empty = detail();
    empty.snapshot.by_provider = [];
    serve(
      () => page([summary()], 1),
      () => empty,
    );

    renderPage();
    const region = await openRow();
    const byProvider = await within(region).findByRole("region", { name: "By provider" });

    expect(within(byProvider).getByText("No usage in this period.")).toBeInTheDocument();
  });

  it("shows the snapshot's English tax notice exactly as given", async () => {
    serve(() => page([summary()], 1));

    renderPage();
    const region = await openRow();
    const tax = await within(region).findByRole("region", { name: "Tax notice" });

    const paragraphs = Array.from(tax.querySelectorAll("div, p")).filter(
      (element) => element.textContent === TAX_NOTICE_EN,
    );
    expect(paragraphs.length).toBeGreaterThan(0);
    // 只显示英文那一份，不拼上中文。
    expect(within(region).queryByText(/销售与服务税/)).not.toBeInTheDocument();
  });

  it("collapses the detail when the same row is clicked again", async () => {
    serve(() => page([summary()], 1));

    renderPage();
    const region = await openRow();
    await within(region).findByText("Statement number");

    fireEvent.click(rowOf(listRegion(), ZERO_UUID));

    await waitFor(() => expect(queryDetailRegion()).toBeNull());
  });

  it("switches to another statement when another row is clicked", async () => {
    const get = serve(
      () =>
        page(
          [summary(), summary({ statement_id: SECOND_ID, statement_number: "ST-202611-ZZZZZZZZ", period: "2026-11" })],
          2,
        ),
      (statementId) =>
        statementId === SECOND_ID
          ? detail({ statement_id: SECOND_ID, statement_number: "ST-202611-ZZZZZZZZ", period: "2026-11" })
          : detail(),
    );

    renderPage();
    const first = await openRow();
    await within(first).findByText("Statement number");
    expect(valueOf(first, "Statement number")).toBe("ST-202612-0A1B2C3D");

    fireEvent.click(rowOf(listRegion(), SECOND_ID));

    const second = await detailRegion();
    await within(second).findByText("Statement number");
    expect(valueOf(second, "Statement number")).toBe("ST-202611-ZZZZZZZZ");
    expect(screen.getAllByRole("region", { name: "Statement details" })).toHaveLength(1);
    expect(urls(get)).toContain(`${STATEMENTS_URL}/${SECOND_ID}`);
  });

  it("clears the selected detail when a filter changes", async () => {
    serve(() => page([summary()], 1));

    renderPage();
    const region = await openRow();
    await within(region).findByText("Statement number");

    typePeriod("2026-12");

    await waitFor(() => expect(queryDetailRegion()).toBeNull());
  });

  it("clears the selected detail when the page changes", async () => {
    serve((query) => page([summary()], 45, Number(query.get("page"))));

    renderPage();
    const region = await openRow();
    await within(region).findByText("Statement number");

    fireEvent.click(within(listRegion()).getByRole("button", { name: "Next" }));

    await waitFor(() => expect(queryDetailRegion()).toBeNull());
  });

  it("shows a loading state while the detail is on its way", async () => {
    serve(
      () => page([summary()], 1),
      () => PENDING,
    );

    renderPage();
    const region = await openRow();

    expect(within(region).getByText("Loading the statement…")).toBeInTheDocument();
  });

  it("shows the error and request id when the detail fails, and keeps the list usable", async () => {
    serve(
      () =>
        page(
          [summary(), summary({ statement_id: SECOND_ID, statement_number: "ST-202611-ZZZZZZZZ", period: "2026-11" })],
          2,
        ),
      (statementId) =>
        statementId === SECOND_ID
          ? detail({ statement_id: SECOND_ID, statement_number: "ST-202611-ZZZZZZZZ", period: "2026-11" })
          : new ApiError("INTERNAL_ERROR", "An unexpected error occurred.", "req-500"),
    );

    renderPage();
    const region = await openRow();

    expect(await within(region).findByText("The statement could not be loaded.")).toBeInTheDocument();
    expect(within(region).getByText("req-500")).toBeInTheDocument();
    expect(within(listRegion()).getByRole("table")).toBeInTheDocument();

    fireEvent.click(rowOf(listRegion(), SECOND_ID));

    const second = await detailRegion();
    await within(second).findByText("Statement number");
    expect(valueOf(second, "Statement number")).toBe("ST-202611-ZZZZZZZZ");
  });

  it("says the statement was not found on a 404", async () => {
    serve(
      () => page([summary()], 1),
      () => new ApiError("STATEMENT_NOT_FOUND", "Statement not found.", "req-404"),
    );

    renderPage();
    const region = await openRow();

    expect(await within(region).findByText("This statement was not found.")).toBeInTheDocument();
    expect(within(region).getByText("req-404")).toBeInTheDocument();
  });
});

describe("StatementsPage generate missing statements", () => {
  it("offers the button only when the list is filtered by a customer", async () => {
    serve(() => page([summary()], 1));

    renderPage();
    await screen.findByText("ST-202612-0A1B2C3D");
    expect(screen.queryByRole("button", { name: "Generate missing statements" })).not.toBeInTheDocument();

    typePeriod("2026-12");
    await screen.findByText("ST-202612-0A1B2C3D");
    expect(screen.queryByRole("button", { name: "Generate missing statements" })).not.toBeInTheDocument();

    await chooseCustomer();

    expect(await screen.findByRole("button", { name: "Generate missing statements" })).toBeInTheDocument();
  });

  it("says how many statements were generated and refreshes the list", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(
      envelope([
        { statement_id: ZERO_UUID, period: "2026-12" },
        { statement_id: ZERO_UUID, period: "2027-01" },
      ]),
    );
    const get = serve(() => page([summary()], 1));

    renderPage();
    await screen.findByText("ST-202612-0A1B2C3D");
    await chooseCustomer();
    await waitFor(() => expect(listUrls(get)).toContain(LIST_FOR_CUSTOMER));
    const before = listUrls(get).filter((url) => url === LIST_FOR_CUSTOMER).length;

    fireEvent.click(await screen.findByRole("button", { name: "Generate missing statements" }));

    expect(await screen.findByText("Generated 2 new statements.")).toBeInTheDocument();
    expect(post).toHaveBeenCalledTimes(1);
    expect(post).toHaveBeenCalledWith(GENERATE_URL);
    await waitFor(() =>
      expect(listUrls(get).filter((url) => url === LIST_FOR_CUSTOMER).length).toBeGreaterThan(before),
    );
  });

  it("says nothing was missing when no statement was generated", async () => {
    vi.spyOn(client, "post").mockResolvedValue(envelope([]));
    serve(() => page([summary()], 1));

    renderPage();
    await screen.findByText("ST-202612-0A1B2C3D");
    await chooseCustomer();
    fireEvent.click(await screen.findByRole("button", { name: "Generate missing statements" }));

    expect(await screen.findByText("No statements were missing for this customer.")).toBeInTheDocument();
  });

  it("shows the error and request id on failure and still refreshes the list", async () => {
    vi.spyOn(client, "post").mockRejectedValue(
      new ApiError("INTERNAL_ERROR", "An unexpected error occurred.", "req-500"),
    );
    const get = serve(() => page([summary()], 1));

    renderPage();
    await screen.findByText("ST-202612-0A1B2C3D");
    await chooseCustomer();
    await waitFor(() => expect(listUrls(get)).toContain(LIST_FOR_CUSTOMER));
    const before = listUrls(get).filter((url) => url === LIST_FOR_CUSTOMER).length;

    fireEvent.click(await screen.findByRole("button", { name: "Generate missing statements" }));

    expect(
      await screen.findByText(
        "The missing statements could not all be generated. Statements generated before the failure are kept; the list has been refreshed.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("req-500")).toBeInTheDocument();
    await waitFor(() =>
      expect(listUrls(get).filter((url) => url === LIST_FOR_CUSTOMER).length).toBeGreaterThan(before),
    );
  });

  it("says statements are only for prepaid customers on a 409", async () => {
    vi.spyOn(client, "post").mockRejectedValue(
      new ApiError("STATEMENT_NOT_ELIGIBLE", "Customer is not eligible.", "req-409"),
    );
    serve(() => page([summary()], 1));

    renderPage();
    await screen.findByText("ST-202612-0A1B2C3D");
    await chooseCustomer();
    fireEvent.click(await screen.findByRole("button", { name: "Generate missing statements" }));

    expect(
      await screen.findByText("Statements are only generated for prepaid customers that have a wallet."),
    ).toBeInTheDocument();
    expect(screen.getByText("req-409")).toBeInTheDocument();
  });
});

describe("StatementsPage requests", () => {
  it("asks only admin endpoints, never a /api/v1/customer/ path", async () => {
    vi.spyOn(client, "post").mockResolvedValue(envelope([]));
    const get = serve(() => page([summary()], 1));

    renderPage();
    const region = await openRow();
    await within(region).findByText("Statement number");
    await chooseCustomer();
    fireEvent.click(await screen.findByRole("button", { name: "Generate missing statements" }));
    await screen.findByText("No statements were missing for this customer.");

    expect(urls(get).length).toBeGreaterThan(0);
    expect(urls(get).filter((url) => url.startsWith("/api/v1/customer/"))).toEqual([]);
    expect(urls(get).every((url) => url.startsWith("/api/v1/admin/"))).toBe(true);
  });
});
