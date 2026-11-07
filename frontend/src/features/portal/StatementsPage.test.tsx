/**
 * 门户对账单页（AIH-TASK-162，设计闸门 #565 v6 §2「前端」）。
 *
 * 与其他门户页的测试同一写法：不 mock `api/customerStatements`，而是在 `client.get` 上按路径回应，钉住**页面真正发出去的
 * 地址** —— 列表带 `page` 与 `page_size=12`、详情把 `statement_id` 放进路径，任何时候都不请求 `/api/v1/admin/`。
 *
 * 要钉住的几件：
 * - 列表四列（编号、账期、期末余额 `MYR x.xx`、定稿时间按 MYT），翻页按 `total` 判断按钮能不能点，翻页清掉已选的详情。
 * - 点一行在列表下方显示详情；再点同一行收起，点另一行换成那一行；详情失败只影响详情，列表照常可用。
 * - 详情的各项金额都是 2 位小数，上期调整逐行列出原用量期间、再加 rebill 与小计，`tax_notice.en` 原样显示。
 *
 * DOM 查询多于 role 查询：antd 注入的样式让 jsdom 里每次 role 查询都要算很多 getComputedStyle，一条用例里查多了会超时。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import type { Page } from "../../api/adminCustomers";
import { ApiError, client } from "../../api/client";
import type {
  CustomerStatementDetail,
  CustomerStatementSummary,
  StatementSnapshot,
} from "../../api/customerStatements";
import { StatementsPage } from "./StatementsPage";

const STATEMENTS_URL = "/api/v1/customer/statements";

const ID_0 = "00000000-0000-0000-0000-000000000000";
const ID_1 = "00000000-0000-0000-0000-000000000001";
const PROJECT_ID = "00000000-0000-0000-0000-000000000000";

const CLOSING_NOTE =
  "The closing balance is worked out with T+1 attribution. It is not the wallet balance at the moment the month ended.";
const EMPTY_TEXT =
  "You have no statements yet. Each month's statement is created on the 2nd day after that month ends.";

/** 原文带换行与标点：页面必须逐字显示，不改写、不拼接。 */
const TAX_NOTICE_EN = "Amounts exclude SST.\nSST registration: not registered (as of 2026-12-01).";

/** 让请求一直挂着（加载中）。 */
const PENDING = Symbol("pending");

function summary(overrides: Partial<CustomerStatementSummary> = {}): CustomerStatementSummary {
  return {
    statement_id: ID_0,
    statement_number: "ST-202612-0A1B2C3D",
    period: "2026-12",
    closing_balance: "87.50000000",
    finalized_at: "2027-01-01T17:00:00",
    ...overrides,
  };
}

function snapshot(overrides: Partial<StatementSnapshot> = {}): StatementSnapshot {
  return {
    schema_version: 1,
    period: "2026-12",
    currency: "MYR",
    company_name: "Example Sdn Bhd",
    tax_notice: { zh: "金额不含 SST。", en: TAX_NOTICE_EN },
    opening_balance: "100.00500000",
    total_topup: "50.00000000",
    total_ai_usage: "-61.23456789",
    total_adjustments: "-0.00400000",
    prior_period: {
      usage: [
        { usage_period: "2026-10", request_count: 3, amount: "-1.11500000" },
        { usage_period: "2026-11", request_count: 7, amount: "-2.22000000" },
      ],
      rebill: "-0.50000000",
      total: "-3.83500000",
    },
    closing_balance: "87.50000000",
    request_count: 1234,
    conversation_count: 56,
    input_tokens: 78901,
    output_tokens: 23456,
    total_customer_charges: "61.23456789",
    by_provider: [
      { provider_code: "anthropic", request_count: 1234, input_tokens: 78901, output_tokens: 23456, charges: "-61.23456789" },
    ],
    by_model: [
      {
        provider_code: "anthropic",
        model_code: "claude-sonnet-5-5",
        request_count: 1000,
        input_tokens: 70000,
        output_tokens: 20000,
        charges: "-50.00000000",
      },
      {
        provider_code: "anthropic",
        model_code: "claude-haiku-4-5",
        request_count: 234,
        input_tokens: 8901,
        output_tokens: 3456,
        charges: "-11.23456789",
      },
    ],
    by_project: [
      {
        project_public_id: PROJECT_ID,
        project_name: "Support bot",
        request_count: 1234,
        input_tokens: 78901,
        output_tokens: 23456,
        charges: "-61.23456789",
      },
    ],
    ...overrides,
  };
}

function detail(overrides: Partial<CustomerStatementDetail> = {}): CustomerStatementDetail {
  return { ...summary(), snapshot: snapshot(), ...overrides };
}

function page(items: CustomerStatementSummary[], total = items.length, pageNumber = 1): Page<CustomerStatementSummary> {
  return { items, page: pageNumber, page_size: 12, total };
}

function envelope(data: unknown) {
  return { data: { success: true, data, error: null, request_id: "req-1" } };
}

function reply(value: unknown) {
  if (value === PENDING) {
    return new Promise(() => undefined);
  }
  return value instanceof Error ? Promise.reject(value) : Promise.resolve(envelope(value));
}

/**
 * 按地址回应 `client.get`：列表（`list(query)`）与详情（`one(statementId)`）。别的路径一律当成失败 —— 页面不该问它们，
 * 下面也会断言没有问。
 */
function serve(list: (query: URLSearchParams) => unknown, one: (statementId: string) => unknown = () => detail()) {
  return vi.spyOn(client, "get").mockImplementation((url: string) => {
    const parsed = new URL(url, "https://portal.example");
    if (parsed.pathname === STATEMENTS_URL) {
      return reply(list(parsed.searchParams));
    }
    if (parsed.pathname.startsWith(`${STATEMENTS_URL}/`) && parsed.search === "") {
      return reply(one(parsed.pathname.slice(STATEMENTS_URL.length + 1)));
    }
    return reply(new ApiError("NOT_FOUND", "Unexpected request.", null));
  });
}

function requestedUrls(get: ReturnType<typeof serve>): string[] {
  return get.mock.calls.map(([url]) => url);
}

function listUrls(get: ReturnType<typeof serve>): string[] {
  return requestedUrls(get).filter((url) => url.startsWith(`${STATEMENTS_URL}?`));
}

function detailUrls(get: ReturnType<typeof serve>): string[] {
  return requestedUrls(get).filter((url) => url.startsWith(`${STATEMENTS_URL}/`));
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <StatementsPage />
    </QueryClientProvider>,
  );
}

/** role=region、aria-label 恰好为 `name` 的那一块；没有时为 null。 */
function findRegion(name: string): HTMLElement | null {
  return (
    Array.from(document.querySelectorAll<HTMLElement>('[role="region"]')).find(
      (el) => el.getAttribute("aria-label") === name,
    ) ?? null
  );
}

function region(name: string): HTMLElement {
  const found = findRegion(name);
  if (found === null) {
    throw new Error(`no region ${name}`);
  }
  return found;
}

function listRegion(): HTMLElement {
  return region("Your statements");
}

function detailRegion(): HTMLElement {
  return region("Statement details");
}

/** `scope` 里第一张表格带 data-row-key 的行的 key，按显示顺序。 */
function rowKeys(scope: HTMLElement): string[] {
  const table = scope.querySelector("table");
  if (table === null) {
    return [];
  }
  return Array.from(table.querySelectorAll(":scope > tbody > tr[data-row-key]"), (tr) => tr.getAttribute("data-row-key") ?? "");
}

function rowOf(scope: HTMLElement, key: string): HTMLElement {
  const tr = scope.querySelector<HTMLElement>(`tr[data-row-key="${key}"]`);
  if (tr === null) {
    throw new Error(`no row ${key}`);
  }
  return tr;
}

function headsOf(scope: HTMLElement): string[] {
  const table = scope.querySelector("table");
  if (table === null) {
    throw new Error("no table");
  }
  return Array.from(table.querySelectorAll("thead th"), (th) => th.textContent ?? "");
}

/** `scope` 里第一张表格某一行各列的文字，按列头取。 */
function cellsOf(scope: HTMLElement, key: string): Record<string, string> {
  const heads = headsOf(scope);
  const cells = Array.from(rowOf(scope, key).querySelectorAll(":scope > td"), (td) => td.textContent ?? "");
  return Object.fromEntries(heads.map((head, index) => [head, cells[index] ?? ""]));
}

/** 带边框的 Descriptions 里 label 为 `label` 的那一项的值（label 在 <th>，值在紧跟的 <td>）。 */
function valueOf(scope: HTMLElement, label: string): string {
  const th = Array.from(scope.querySelectorAll("th")).find((el) => el.textContent === label);
  const td = th?.nextElementSibling;
  if (td === null || td === undefined) {
    throw new Error(`no item ${label}`);
  }
  return td.textContent ?? "";
}

function buttonNamed(scope: ParentNode, name: string): HTMLButtonElement {
  const button = Array.from(scope.querySelectorAll("button")).find((el) => el.textContent?.trim() === name);
  if (button === undefined) {
    throw new Error(`no ${name} button`);
  }
  return button;
}

async function listRowsShown(): Promise<void> {
  await waitFor(() => expect(findRegion("Your statements")?.querySelector("tr[data-row-key]")).toBeTruthy());
}

async function detailShown(): Promise<void> {
  await waitFor(() => expect(findRegion("Statement details")?.querySelector("table")).toBeTruthy());
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("StatementsPage states", () => {
  it("says it is loading while the first page is on its way", async () => {
    serve(() => PENDING);

    renderPage();

    expect(await screen.findByText("Loading your statements…")).toBeInTheDocument();
    expect(listRegion().querySelector('[aria-busy="true"]')).not.toBeNull();
  });

  it("shows the failure with its request_id", async () => {
    serve(() => new ApiError("INTERNAL_ERROR", "An unexpected error occurred.", "req-list-500"));

    renderPage();

    expect(await screen.findByText("Your statements could not be loaded.")).toBeInTheDocument();
    expect(screen.getByText("req-list-500")).toBeInTheDocument();
    expect(buttonNamed(listRegion(), "Try again")).toBeEnabled();
  });

  it("explains when statements are created when there are none yet", async () => {
    serve(() => page([], 0));

    renderPage();

    expect(await screen.findByText(EMPTY_TEXT)).toBeInTheDocument();
    expect(listRegion().querySelector("tr[data-row-key]")).toBeNull();
    expect(screen.queryByText("Previous")).not.toBeInTheDocument();
  });

  it("says the closing balance is not the wallet balance at the end of the month", async () => {
    serve(() => page([], 0));

    renderPage();

    expect(await screen.findByText(CLOSING_NOTE)).toBeInTheDocument();
    // 说明不是错误提示：不用 role=alert。
    expect(document.querySelector('[role="alert"]')).toBeNull();
  });
});

describe("StatementsPage list", () => {
  it("shows number, period, closing balance in MYR to 2 places and the finalized time in MYT", async () => {
    const get = serve(() =>
      page([
        summary(),
        summary({
          statement_id: ID_1,
          statement_number: "ST-202611-ZZZZ0000",
          period: "2026-11",
          closing_balance: "-1234.56500000",
          finalized_at: "2026-12-01T17:00:00",
        }),
      ]),
    );

    renderPage();
    await listRowsShown();

    expect(headsOf(listRegion())).toEqual(["Statement number", "Period", "Closing balance", "Finalized at (MYT)"]);
    // 行序就是接口给的顺序（账期倒序）。
    expect(rowKeys(listRegion())).toEqual([ID_0, ID_1]);
    expect(cellsOf(listRegion(), ID_0)).toEqual({
      "Statement number": "ST-202612-0A1B2C3D",
      Period: "2026-12",
      "Closing balance": "MYR 87.50",
      "Finalized at (MYT)": "2027-01-02 01:00:00",
    });
    expect(cellsOf(listRegion(), ID_1)).toEqual({
      "Statement number": "ST-202611-ZZZZ0000",
      Period: "2026-11",
      "Closing balance": "MYR -1,234.57",
      "Finalized at (MYT)": "2026-12-02 01:00:00",
    });
    expect(listUrls(get)).toEqual([`${STATEMENTS_URL}?page=1&page_size=12`]);
    expect(detailUrls(get)).toEqual([]);
  });

  it("pages with page and page_size=12, enabling the buttons from total and clearing the selected statement", async () => {
    const get = serve((query) => {
      const pageNumber = Number(query.get("page"));
      return page([summary({ statement_id: `00000000-0000-0000-0000-00000000000${pageNumber}` })], 30, pageNumber);
    });

    renderPage();
    await listRowsShown();
    expect(buttonNamed(listRegion(), "Previous")).toBeDisabled();
    expect(buttonNamed(listRegion(), "Next")).toBeEnabled();

    // 先选一行，翻页之后详情应当收起。
    fireEvent.click(rowOf(listRegion(), "00000000-0000-0000-0000-000000000001"));
    await detailShown();

    fireEvent.click(buttonNamed(listRegion(), "Next"));
    await waitFor(() => expect(rowKeys(listRegion())).toEqual(["00000000-0000-0000-0000-000000000002"]));
    expect(findRegion("Statement details")).toBeNull();
    expect(buttonNamed(listRegion(), "Previous")).toBeEnabled();
    expect(buttonNamed(listRegion(), "Next")).toBeEnabled();

    fireEvent.click(buttonNamed(listRegion(), "Next"));
    await waitFor(() => expect(rowKeys(listRegion())).toEqual(["00000000-0000-0000-0000-000000000003"]));
    // 3 × 12 ≥ 30：没有下一页。
    expect(buttonNamed(listRegion(), "Next")).toBeDisabled();
    expect(buttonNamed(listRegion(), "Previous")).toBeEnabled();

    fireEvent.click(buttonNamed(listRegion(), "Previous"));
    await waitFor(() => expect(rowKeys(listRegion())).toEqual(["00000000-0000-0000-0000-000000000002"]));

    expect(listUrls(get)).toEqual([
      `${STATEMENTS_URL}?page=1&page_size=12`,
      `${STATEMENTS_URL}?page=2&page_size=12`,
      `${STATEMENTS_URL}?page=3&page_size=12`,
      `${STATEMENTS_URL}?page=2&page_size=12`,
    ]);
  });

  it("disables both buttons when everything fits on one page", async () => {
    serve(() => page([summary()], 12));

    renderPage();
    await listRowsShown();

    expect(buttonNamed(listRegion(), "Previous")).toBeDisabled();
    expect(buttonNamed(listRegion(), "Next")).toBeDisabled();
  });
});

describe("StatementsPage details", () => {
  it("shows every field of the selected statement below the list, amounts to 2 places", async () => {
    const get = serve(() => page([summary()]));

    const { container } = renderPage();
    await listRowsShown();
    expect(findRegion("Statement details")).toBeNull();

    fireEvent.click(rowOf(listRegion(), ID_0));
    await detailShown();

    expect(detailUrls(get)).toEqual([`${STATEMENTS_URL}/${ID_0}`]);
    // 详情在列表下方，不是弹窗。
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(listRegion().compareDocumentPosition(detailRegion()) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    const card = detailRegion();
    expect(valueOf(card, "Statement number")).toBe("ST-202612-0A1B2C3D");
    expect(valueOf(card, "Company name")).toBe("Example Sdn Bhd");
    expect(valueOf(card, "Period")).toBe("2026-12");
    expect(valueOf(card, "Finalized at (MYT)")).toBe("2027-01-02 01:00:00");

    const balance = region("Balance");
    expect(valueOf(balance, "Opening balance")).toBe("MYR 100.01");
    expect(valueOf(balance, "Top-ups")).toBe("MYR 50.00");
    expect(valueOf(balance, "AI usage charges this period")).toBe("MYR -61.23");
    // -0.004 舍入成 0.00：不显示成让人以为欠了钱的「-0.00」。
    expect(valueOf(balance, "Adjustments")).toBe("MYR 0.00");
    expect(valueOf(card, "Closing balance")).toBe("MYR 87.50");

    const usage = region("Usage this period");
    expect(valueOf(usage, "Requests")).toBe("1234");
    expect(valueOf(usage, "Conversations")).toBe("56");
    expect(valueOf(usage, "Input tokens")).toBe("78901");
    expect(valueOf(usage, "Output tokens")).toBe("23456");
    expect(valueOf(usage, "Customer charges")).toBe("MYR 61.23");

    // 不显示内部 id 或任何成本、毛利字段。
    expect(container.textContent ?? "").not.toContain(PROJECT_ID);
    expect(container.textContent ?? "").not.toMatch(/cost|margin|markup|cut-off \(MYT\)|ledger/i);
  });

  it("lists the prior-period adjustments by usage period, then the rebill and the subtotal", async () => {
    serve(() => page([summary()]));

    renderPage();
    await listRowsShown();
    fireEvent.click(rowOf(listRegion(), ID_0));
    await detailShown();

    const prior = region("Prior-period adjustments");
    expect(rowKeys(prior)).toEqual(["usage-2026-10", "usage-2026-11", "rebill", "total"]);
    expect(headsOf(prior)).toEqual(["Usage period", "Requests", "Amount"]);
    expect(cellsOf(prior, "usage-2026-10")).toEqual({ "Usage period": "2026-10", Requests: "3", Amount: "MYR -1.12" });
    expect(cellsOf(prior, "usage-2026-11")).toEqual({ "Usage period": "2026-11", Requests: "7", Amount: "MYR -2.22" });
    expect(cellsOf(prior, "rebill")).toEqual({ "Usage period": "Rebill", Requests: "—", Amount: "MYR -0.50" });
    expect(cellsOf(prior, "total")).toEqual({ "Usage period": "Subtotal", Requests: "—", Amount: "MYR -3.84" });
  });

  it("shows only the rebill and the subtotal when there is no late usage", async () => {
    serve(
      () => page([summary()]),
      () => detail({ snapshot: snapshot({ prior_period: { usage: [], rebill: "0.00000000", total: "0.00000000" } }) }),
    );

    renderPage();
    await listRowsShown();
    fireEvent.click(rowOf(listRegion(), ID_0));
    await detailShown();

    const prior = region("Prior-period adjustments");
    expect(rowKeys(prior)).toEqual(["rebill", "total"]);
    expect(cellsOf(prior, "total").Amount).toBe("MYR 0.00");
  });

  it("shows the three breakdown tables in the order the backend gave", async () => {
    serve(() => page([summary()]));

    renderPage();
    await listRowsShown();
    fireEvent.click(rowOf(listRegion(), ID_0));
    await detailShown();

    const byProvider = region("By provider");
    expect(headsOf(byProvider)).toEqual(["Provider", "Requests", "Input tokens", "Output tokens", "Charges"]);
    expect(cellsOf(byProvider, "anthropic")).toEqual({
      Provider: "anthropic",
      Requests: "1234",
      "Input tokens": "78901",
      "Output tokens": "23456",
      Charges: "MYR -61.23",
    });

    const byModel = region("By model");
    expect(headsOf(byModel)).toEqual(["Provider", "Model", "Requests", "Input tokens", "Output tokens", "Charges"]);
    expect(rowKeys(byModel)).toEqual(["anthropic/claude-sonnet-5-5", "anthropic/claude-haiku-4-5"]);
    expect(cellsOf(byModel, "anthropic/claude-haiku-4-5")).toEqual({
      Provider: "anthropic",
      Model: "claude-haiku-4-5",
      Requests: "234",
      "Input tokens": "8901",
      "Output tokens": "3456",
      Charges: "MYR -11.23",
    });

    const byProject = region("By project");
    expect(headsOf(byProject)).toEqual(["Project", "Requests", "Input tokens", "Output tokens", "Charges"]);
    expect(cellsOf(byProject, PROJECT_ID)).toEqual({
      Project: "Support bot",
      Requests: "1234",
      "Input tokens": "78901",
      "Output tokens": "23456",
      Charges: "MYR -61.23",
    });
  });

  it("shows the tax notice's English text exactly as the snapshot has it", async () => {
    serve(() => page([summary()]));

    renderPage();
    await listRowsShown();
    fireEvent.click(rowOf(listRegion(), ID_0));
    await detailShown();

    const notice = region("Tax notice");
    // 有一个元素的文字恰好是原文（换行照留），不多不少。
    const texts = Array.from(notice.querySelectorAll("*"), (el) => el.textContent ?? "");
    expect(texts).toContain(TAX_NOTICE_EN);
    // 只显示英文那一段，不拼上中文。
    expect(notice.textContent ?? "").not.toContain("金额不含 SST。");
  });

  it("closes the details when the same row is selected again and switches to another row", async () => {
    const get = serve(
      () => page([summary(), summary({ statement_id: ID_1, statement_number: "ST-202611-ZZZZ0000", period: "2026-11" })]),
      (id) =>
        id === ID_1
          ? detail({ statement_id: ID_1, statement_number: "ST-202611-ZZZZ0000", period: "2026-11" })
          : detail(),
    );

    renderPage();
    await listRowsShown();

    fireEvent.click(rowOf(listRegion(), ID_0));
    await detailShown();
    expect(valueOf(detailRegion(), "Statement number")).toBe("ST-202612-0A1B2C3D");

    fireEvent.click(rowOf(listRegion(), ID_1));
    await waitFor(() => expect(valueOf(detailRegion(), "Statement number")).toBe("ST-202611-ZZZZ0000"));
    expect(valueOf(detailRegion(), "Period")).toBe("2026-11");

    fireEvent.click(rowOf(listRegion(), ID_1));
    await waitFor(() => expect(findRegion("Statement details")).toBeNull());

    expect(detailUrls(get)).toEqual([`${STATEMENTS_URL}/${ID_0}`, `${STATEMENTS_URL}/${ID_1}`]);
  });

  it("says the details are loading while they are on their way", async () => {
    serve(
      () => page([summary()]),
      () => PENDING,
    );

    renderPage();
    await listRowsShown();
    fireEvent.click(rowOf(listRegion(), ID_0));

    expect(await screen.findByText("Loading the statement…")).toBeInTheDocument();
    expect(detailRegion().querySelector('[aria-busy="true"]')).not.toBeNull();
  });

  it("shows a failed detail with its request_id and keeps the list usable", async () => {
    const get = serve(
      (query) =>
        page([summary({ statement_id: `00000000-0000-0000-0000-00000000000${query.get("page") ?? ""}` })], 13),
      () => new ApiError("INTERNAL_ERROR", "An unexpected error occurred.", "req-detail-500"),
    );

    renderPage();
    await listRowsShown();
    fireEvent.click(rowOf(listRegion(), "00000000-0000-0000-0000-000000000001"));

    expect(await screen.findByText("The statement could not be loaded.")).toBeInTheDocument();
    expect(screen.getByText("req-detail-500")).toBeInTheDocument();
    // 列表不受影响：行还在、翻页照常。
    expect(rowKeys(listRegion())).toEqual(["00000000-0000-0000-0000-000000000001"]);
    expect(screen.queryByText("Your statements could not be loaded.")).not.toBeInTheDocument();

    fireEvent.click(buttonNamed(listRegion(), "Next"));
    await waitFor(() => expect(rowKeys(listRegion())).toEqual(["00000000-0000-0000-0000-000000000002"]));
    expect(findRegion("Statement details")).toBeNull();
    expect(listUrls(get)).toEqual([`${STATEMENTS_URL}?page=1&page_size=12`, `${STATEMENTS_URL}?page=2&page_size=12`]);
  });

  it("says when the statement was not found", async () => {
    serve(
      () => page([summary()]),
      () => new ApiError("STATEMENT_NOT_FOUND", "Statement not found.", "req-detail-404"),
    );

    renderPage();
    await listRowsShown();
    fireEvent.click(rowOf(listRegion(), ID_0));

    expect(await screen.findByText("This statement was not found.")).toBeInTheDocument();
    expect(screen.getByText("req-detail-404")).toBeInTheDocument();
  });
});

describe("StatementsPage requests", () => {
  it("only ever asks the customer statements endpoints, never an admin path", async () => {
    const get = serve(
      () => page([summary(), summary({ statement_id: ID_1 })], 20),
      (id) => detail({ statement_id: id }),
    );

    renderPage();
    await listRowsShown();
    fireEvent.click(rowOf(listRegion(), ID_0));
    await detailShown();
    fireEvent.click(rowOf(listRegion(), ID_1));
    await waitFor(() => expect(detailUrls(get)).toHaveLength(2));
    fireEvent.click(buttonNamed(listRegion(), "Next"));
    await waitFor(() => expect(listUrls(get)).toHaveLength(2));

    const urls = requestedUrls(get);
    expect(urls.length).toBeGreaterThan(0);
    for (const url of urls) {
      expect(url.startsWith(STATEMENTS_URL)).toBe(true);
      expect(url).not.toContain("/api/v1/admin/");
      expect(url).not.toMatch(/tenant|customer_id/);
    }
  });
});
