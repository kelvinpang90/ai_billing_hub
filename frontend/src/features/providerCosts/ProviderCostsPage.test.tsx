/**
 * 管理端供应商成本页（AIH-TASK-246，设计闸门 #755 v1 §7「前端页面」）。
 *
 * 与对账单页的测试同一写法：不 mock `api/adminProviderCosts`，而是在 `client.get` / `client.post` 上按路径回应，钉住**页面真正
 * 发出去的地址与请求体**（总览的 provider_code、预览不带 expected_*、确认带预览返回的两个期望值、详情问的是哪一条），并断言
 * 没有问 `/api/v1/customer/` 路径。`App.useApp` 换成替身，只为看住 `message.success` 被调用。
 *
 * 全程断言没有 `role="alert"`：这一页的错误是普通文字，成功是 `message.success`。
 *
 * uuid 一律是全零占位值；更正链里被取代的那一版的 id 只用来区分两行，原样进路径，页面不解析它。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { App } from "antd";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from "vitest";

import "../../i18n";
import type { MonthItem, ReconciliationDetail, ReconciliationView } from "../../api/adminProviderCosts";
import { ApiError, client } from "../../api/client";
import { ROUTES } from "../../routes/paths";
import { ProviderCostsPage } from "./ProviderCostsPage";

const ZERO_UUID = "00000000-0000-0000-0000-000000000000";
const SUPERSEDED_ID = "superseded-version";
const BASE = "/api/v1/admin/provider-cost-reconciliations";
const MONTHS_PATH = `${BASE}/months`;
const PREVIEW_PATH = `${BASE}/preview`;
const DASH = "—";

/** 让请求一直挂着（加载中）。 */
const PENDING = Symbol("pending");

function view(overrides: Partial<ReconciliationView> = {}): ReconciliationView {
  return {
    reconciliation_id: ZERO_UUID,
    provider_code: "anthropic",
    scope: "AI_MODULE",
    billing_month: "2026-12",
    period_basis: "utc_calendar_month",
    period_start: "2026-12-01T00:00:00",
    period_end: "2027-01-01T00:00:00",
    source_document_reference: "Cost page, platform workspace, 2026-12",
    source_currency: "USD",
    source_total: "11.70000000",
    fx_rate_version_id: ZERO_UUID,
    fx_rate_applied: "4.4",
    fx_rate_moment: "2026-12-31T23:59:59.999999",
    reconciled_cost_myr: "51.48000000",
    estimated_event_count: 2,
    estimated_source_cost: "10.50000000",
    estimated_cost_myr: "46.80000000",
    source_variance: "1.20000000",
    source_variance_rate: "0.1142857142857142857142857143",
    myr_variance: "4.68000000",
    myr_variance_rate: "0.1",
    exceeds_threshold: true,
    variance_threshold: "0.05",
    unprocessed_event_count: 1,
    outbox_unsettled_count: 3,
    outbox_discarded_count: 4,
    coverage_method: "PERIOD_AGGREGATE",
    supersedes_id: null,
    reason: null,
    is_current: true,
    created_at: "2027-01-05T02:00:00",
    created_by_email: "admin@example.com",
    ...overrides,
  };
}

/** 预览的结果：没有 id、确认人与确认时刻。 */
function previewView(overrides: Partial<ReconciliationView> = {}): ReconciliationView {
  return view({ reconciliation_id: null, is_current: null, created_at: null, created_by_email: null, ...overrides });
}

function period(month: string): Pick<MonthItem, "period_start" | "period_end"> {
  const [year, number] = month.split("-").map(Number);
  const nextYear = number === 12 ? (year ?? 0) + 1 : (year ?? 0);
  const nextMonth = number === 12 ? 1 : (number ?? 0) + 1;
  return {
    period_start: `${month}-01T00:00:00`,
    period_end: `${nextYear}-${String(nextMonth).padStart(2, "0")}-01T00:00:00`,
  };
}

function reconciled(month: string, current: ReconciliationView = view({ billing_month: month })): MonthItem {
  return { billing_month: month, ...period(month), status: "RECONCILED", current };
}

function notReconciled(month: string): MonthItem {
  return { billing_month: month, ...period(month), status: "NOT_RECONCILED", current: null };
}

function detail(): ReconciliationDetail {
  const first = view({
    reconciliation_id: SUPERSEDED_ID,
    source_total: "10.00000000",
    reconciled_cost_myr: "44.00000000",
    is_current: false,
    created_by_email: "first@example.com",
  });
  const second = view({
    supersedes_id: SUPERSEDED_ID,
    reason: "Invoice credit applied",
    created_at: "2027-01-06T03:00:00",
    created_by_email: "second@example.com",
  });
  return {
    reconciliation: second,
    chain: [first, second],
    live_estimate: {
      estimated_event_count: 3,
      estimated_source_cost: "12.00000000",
      estimated_cost_myr: "52.80000000",
      currency_mismatch_count: 0,
      unprocessed_event_count: 0,
    },
    estimate_changed_since_confirmation: true,
  };
}

function envelope(data: unknown) {
  return { data: { success: true, data, error: null, request_id: "req-1" } };
}

function respond(reply: unknown) {
  if (reply === PENDING) {
    return new Promise(() => undefined);
  }
  return reply instanceof Error ? Promise.reject(reply) : Promise.resolve(envelope(reply));
}

/**
 * 按路径回应 `client.get`：月份总览交给 `months`（每次请求现算，可以前后不同），某一条的详情交给 `detailOf`（参数是路径里的
 * id）。别的路径一律当成失败 —— 页面不该问它们，下面也会断言没有问。
 */
function serveGet(months: () => unknown, detailOf: (id: string) => unknown = () => detail()) {
  return vi.spyOn(client, "get").mockImplementation((url: string) => {
    const parsed = new URL(url, "https://admin.example");
    if (parsed.pathname === MONTHS_PATH) {
      return respond(months());
    }
    if (parsed.pathname.startsWith(`${BASE}/`)) {
      return respond(detailOf(parsed.pathname.slice(BASE.length + 1)));
    }
    return respond(new ApiError("NOT_FOUND", "Unexpected request.", null));
  });
}

/** 按路径回应 `client.post`：预览交给 `preview`，确认交给 `confirm`，参数都是请求体。 */
function servePost(
  preview: (body: Record<string, unknown>) => unknown = (body) =>
    previewView({ source_total: typeof body.source_total === "string" ? body.source_total : "" }),
  confirm: (body: Record<string, unknown>) => unknown = () => view(),
) {
  return vi.spyOn(client, "post").mockImplementation((url: string, body?: unknown) => {
    const sent = body as Record<string, unknown>;
    if (url === PREVIEW_PATH) {
      return respond(preview(sent));
    }
    if (url === BASE) {
      return respond(confirm(sent));
    }
    return respond(new ApiError("NOT_FOUND", "Unexpected request.", null));
  });
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[ROUTES.providerCosts]}>
        <ProviderCostsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function monthsRegion(): HTMLElement {
  return screen.getByRole("region", { name: "Months" });
}

function monthsTable(): HTMLTableElement {
  const table = monthsRegion().querySelector("table");
  if (table === null) {
    throw new Error("no months table");
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

/** `scope` 里文字恰好为 `name` 的按钮（加载中的图标没有文字，不影响）。 */
function buttonNamed(scope: ParentNode, name: string): HTMLButtonElement {
  const button = Array.from(scope.querySelectorAll("button")).find((el) => el.textContent?.trim() === name);
  if (button === undefined) {
    throw new Error(`no ${name} button`);
  }
  return button;
}

function dialog(): HTMLElement {
  return screen.getByRole("dialog");
}

async function openForm(month: string, action: "Reconcile" | "Correct"): Promise<HTMLElement> {
  await screen.findByText(month);
  fireEvent.click(buttonNamed(rowOf(monthsTable(), month), action));
  return screen.findByRole("dialog");
}

function type(label: string, value: string): void {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}

/**
 * 下拉当前选中的那一项的文字；没选时 `null`。antd v6 的单选没有 `.ant-select-selection-item`：选中值画在
 * `.ant-select-content` 里（同定价规则页的用例）。
 */
function selectedText(field: HTMLElement): string | null {
  const select = field.closest(".ant-select");
  const item = select?.querySelector(".ant-select-selection-item, .ant-select-content-value");
  if (item) {
    return item.textContent;
  }
  const content = select?.querySelector(".ant-select-content, .ant-select-selector");
  if (!content || content.querySelector(".ant-select-placeholder, .ant-select-selection-placeholder")) {
    return null;
  }
  const raw: string | null = content.textContent;
  const text = raw?.trim() ?? "";
  return text === "" ? null : text;
}

/** 打开着的下拉里全部选项的 title，按显示顺序。 */
function openOptionTitles(): string[] {
  return Array.from(
    document.querySelectorAll(".ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option"),
    (option) => option.getAttribute("title") ?? "",
  );
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

function urls(spy: { mock: { calls: unknown[][] } }): string[] {
  return spy.mock.calls.map(([url]) => String(url));
}

function monthsUrls(get: ReturnType<typeof serveGet>): string[] {
  return urls(get).filter((url) => url.startsWith(`${MONTHS_PATH}?`));
}

function postedTo(post: ReturnType<typeof servePost>, path: string): Record<string, unknown>[] {
  return post.mock.calls.filter(([url]) => url === path).map(([, body]) => body as Record<string, unknown>);
}

function expectNoAlert(): void {
  expect(document.querySelector('[role="alert"]')).toBeNull();
}

function expectAdminOnly(...spies: { mock: { calls: unknown[][] } }[]): void {
  for (const spy of spies) {
    expect(urls(spy).filter((url) => url.startsWith("/api/v1/customer/"))).toEqual([]);
  }
}

/** 填一次 Reconcile 表单并预览，等预览结果出来。 */
async function previewNewMonth(month: string, amount = "11.70"): Promise<HTMLElement> {
  const form = await openForm(month, "Reconcile");
  type("Actual amount", amount);
  type("Source reference", "Console cost, platform workspace");
  fireEvent.click(buttonNamed(form, "Preview"));
  await within(form).findByRole("region", { name: "Preview" });
  return form;
}

let success: Mock;

beforeEach(() => {
  success = vi.fn();
  vi.spyOn(App, "useApp").mockReturnValue({ message: { success } } as unknown as ReturnType<typeof App.useApp>);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ProviderCostsPage months overview", () => {
  it("asks for the anthropic months, shows the provider as the only choice and says exactly that there is nothing to reconcile", async () => {
    const get = serveGet(() => ({ items: [] }));

    renderPage();

    const empty = await screen.findByText("No completed months to reconcile yet.");
    expect(monthsRegion()).toContainElement(empty);
    expect(rowKeys(monthsTable())).toEqual([]);
    expect(monthsUrls(get)).toEqual([`${MONTHS_PATH}?provider_code=anthropic`]);
    expect(selectedText(screen.getByLabelText("Provider"))).toBe("anthropic");
    fireEvent.mouseDown(screen.getByLabelText("Provider"));
    await screen.findAllByTitle("anthropic");
    expect(openOptionTitles()).toEqual(["anthropic"]);
    expectNoAlert();
    expectAdminOnly(get);
  });

  it("shows a loading state while the months are on their way", async () => {
    serveGet(() => PENDING);

    renderPage();

    expect(await screen.findByText("Loading months…")).toBeInTheDocument();
    expect(monthsRegion().querySelector("table")).toBeNull();
  });

  it("shows one reconciled and one unreconciled month with their status text and the backend strings as they are", async () => {
    const get = serveGet(() => ({
      items: [
        notReconciled("2027-01"),
        reconciled("2026-12", view({ source_total: "0.10000001", reconciled_cost_myr: "0.44000004" })),
      ],
    }));

    renderPage();

    await screen.findByText("2027-01");
    const table = monthsTable();
    expect(headersOf(table)).toEqual(
      expect.arrayContaining([
        "Month (UTC)",
        "Status",
        "Actual (source)",
        "Actual (MYR)",
        "Estimated (MYR)",
        "Variance rate (source)",
        "Variance rate (MYR)",
      ]),
    );
    // 顺序由后端决定，最近的在前。
    expect(rowKeys(table)).toEqual(["2027-01", "2026-12"]);
    // pagination={false}：antd 默认哪怕只有一页也画分页条。
    expect(monthsRegion().querySelector(".ant-pagination")).toBeNull();

    expect(cell(table, "2027-01", "Month (UTC)")).toBe("2027-01");
    expect(cell(table, "2027-01", "Status")).toBe("Not reconciled");
    for (const header of [
      "Actual (source)",
      "Actual (MYR)",
      "Estimated (MYR)",
      "Variance rate (source)",
      "Variance rate (MYR)",
    ]) {
      expect(cell(table, "2027-01", header)).toBe(DASH);
    }
    expect(buttonNamed(rowOf(table, "2027-01"), "Reconcile")).toBeEnabled();

    expect(cell(table, "2026-12", "Month (UTC)")).toBe("2026-12");
    expect(cell(table, "2026-12", "Status")).toBe("Reconciled");
    expect(cell(table, "2026-12", "Actual (source)")).toBe("0.10000001 USD");
    expect(cell(table, "2026-12", "Actual (MYR)")).toBe("0.44000004");
    expect(cell(table, "2026-12", "Estimated (MYR)")).toBe("46.80000000");
    expect(cell(table, "2026-12", "Variance rate (MYR)")).toBe("0.1");
    expect(buttonNamed(rowOf(table, "2026-12"), "Correct")).toBeEnabled();

    expect(screen.getByText(/UTC calendar month/)).toBeInTheDocument();
    expect(screen.getByText(/through the AI module/)).toBeInTheDocument();
    expect(screen.getByText(/published exchange rate in effect at the last moment of the month/)).toBeInTheDocument();
    expect(screen.getByText(/never changes any billed amount/)).toBeInTheDocument();
    expectNoAlert();
    expectAdminOnly(get);
  });

  it("shows the code, message and request id as plain text when the months fail, without the empty state", async () => {
    serveGet(() => new ApiError("AI_PROVIDER_NOT_FOUND", "AI provider not found.", "req-404"));

    renderPage();

    expect(await screen.findByText("AI_PROVIDER_NOT_FOUND")).toBeInTheDocument();
    expect(screen.getByText(/AI provider not found\./)).toBeInTheDocument();
    expect(screen.getByText("req-404")).toBeInTheDocument();
    expect(screen.queryByText("No completed months to reconcile yet.")).not.toBeInTheDocument();
    expect(monthsRegion().querySelector("table")).toBeNull();
    expectNoAlert();
  });

  it("shows any other failure the same way", async () => {
    serveGet(() => new ApiError("INTERNAL_ERROR", "An unexpected error occurred.", "req-500"));

    renderPage();

    expect(await screen.findByText("The months could not be loaded.")).toBeInTheDocument();
    expect(screen.getByText("INTERNAL_ERROR")).toBeInTheDocument();
    expect(screen.getByText("req-500")).toBeInTheDocument();
    expectNoAlert();
  });

  it("marks Above 5% only next to the source variance rate, and shows — for null rates", async () => {
    serveGet(() => ({
      items: [
        // 超阈值：只在原币差额率旁边标记。
        reconciled("2027-03"),
        // 估算为 0、实际大于 0：原币差额率为 null 但仍超阈值。
        reconciled(
          "2027-02",
          view({ billing_month: "2027-02", source_variance_rate: null, myr_variance_rate: null, exceeds_threshold: true }),
        ),
        // 不超阈值：没有标记。
        reconciled(
          "2027-01",
          view({ billing_month: "2027-01", source_variance_rate: "0.01", myr_variance_rate: "0.02", exceeds_threshold: false }),
        ),
      ],
    }));

    renderPage();

    await screen.findByText("2027-03");
    const table = monthsTable();
    expect(cell(table, "2027-03", "Variance rate (source)")).toBe("0.1142857142857142857142857143 Above 5%");
    expect(cell(table, "2027-03", "Variance rate (MYR)")).toBe("0.1");
    expect(cell(table, "2027-02", "Variance rate (source)")).toBe(`${DASH} Above 5%`);
    expect(cell(table, "2027-02", "Variance rate (MYR)")).toBe(DASH);
    expect(cell(table, "2027-01", "Variance rate (source)")).toBe("0.01");
    expect(cell(table, "2027-01", "Variance rate (MYR)")).toBe("0.02");
    expect(screen.getAllByText("Above 5%")).toHaveLength(2);
  });
});

describe("ProviderCostsPage reconcile form", () => {
  it("opens an empty form in USD for an unreconciled month, with only USD and MYR to choose from", async () => {
    serveGet(() => ({ items: [notReconciled("2027-01")] }));

    renderPage();
    const form = await openForm("2027-01", "Reconcile");

    expect(within(form).getByText("Reconcile 2027-01")).toBeInTheDocument();
    expect(selectedText(screen.getByLabelText("Currency"))).toBe("USD");
    expect(screen.getByLabelText("Actual amount")).toHaveValue("");
    expect(screen.getByLabelText("Source reference")).toHaveValue("");
    expect(screen.queryByLabelText("Reason for correction")).not.toBeInTheDocument();

    fireEvent.mouseDown(screen.getByLabelText("Currency"));
    await screen.findAllByTitle("MYR");
    expect(openOptionTitles()).toEqual(["USD", "MYR"]);
    expectNoAlert();
  });

  it("lets Confirm be clicked only after a successful preview, and confirms with the expected values from the preview", async () => {
    const get = serveGet(() => ({ items: [notReconciled("2027-01")] }));
    const post = servePost(() =>
      previewView({
        billing_month: "2027-01",
        source_total: "0.10000001",
        estimated_event_count: 7,
        estimated_source_cost: "0.09000000",
      }),
    );

    renderPage();
    const form = await openForm("2027-01", "Reconcile");
    expect(buttonNamed(form, "Confirm")).toBeDisabled();
    // 没填金额与来源说明时连预览都不能点。
    expect(buttonNamed(form, "Preview")).toBeDisabled();

    type("Actual amount", "0.10000001");
    type("Source reference", "  Console cost, platform workspace  ");
    expect(buttonNamed(form, "Confirm")).toBeDisabled();
    fireEvent.click(buttonNamed(form, "Preview"));

    const result = await within(form).findByRole("region", { name: "Preview" });
    expect(within(result).getByText("0.10000001", { exact: false })).toBeInTheDocument();
    expect(within(result).getByText("Unprocessed AI module events")).toBeInTheDocument();
    expect(within(result).getByText("Undelivered AI module outbox rows")).toBeInTheDocument();
    expect(within(result).getByText("Discarded AI module outbox rows")).toBeInTheDocument();
    expect(within(result).getByText(/pushes the variance up/)).toBeInTheDocument();
    await waitFor(() => expect(buttonNamed(form, "Confirm")).toBeEnabled());

    expect(postedTo(post, PREVIEW_PATH)).toEqual([
      {
        provider_code: "anthropic",
        billing_month: "2027-01",
        source_currency: "USD",
        source_total: "0.10000001",
        source_document_reference: "Console cost, platform workspace",
        supersedes_id: null,
        reason: null,
      },
    ]);

    fireEvent.click(buttonNamed(form, "Confirm"));

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(postedTo(post, BASE)).toEqual([
      {
        provider_code: "anthropic",
        billing_month: "2027-01",
        source_currency: "USD",
        source_total: "0.10000001",
        source_document_reference: "Console cost, platform workspace",
        supersedes_id: null,
        reason: null,
        expected_event_count: 7,
        expected_estimated_source_cost: "0.09000000",
      },
    ]);
    // 201 之后：关表单、月份总览再请求一次、message.success。
    await waitFor(() => expect(monthsUrls(get)).toHaveLength(2));
    expect(success).toHaveBeenCalledWith("The reconciliation was confirmed.");
    expectNoAlert();
    expectAdminOnly(get, post);
  });

  it("disables Confirm again when the form is changed after the preview", async () => {
    serveGet(() => ({ items: [notReconciled("2027-01")] }));
    const post = servePost();

    renderPage();
    const form = await previewNewMonth("2027-01");
    await waitFor(() => expect(buttonNamed(form, "Confirm")).toBeEnabled());

    type("Actual amount", "11.71");

    expect(buttonNamed(form, "Confirm")).toBeDisabled();
    expect(within(form).queryByRole("region", { name: "Preview" })).not.toBeInTheDocument();

    // 换币种同样算改动。
    fireEvent.click(buttonNamed(form, "Preview"));
    await within(form).findByRole("region", { name: "Preview" });
    await waitFor(() => expect(buttonNamed(form, "Confirm")).toBeEnabled());
    await choose(screen.getByLabelText("Currency"), "MYR");
    expect(buttonNamed(form, "Confirm")).toBeDisabled();
    expect(postedTo(post, BASE)).toEqual([]);
  });

  it("asks to preview again on 409 RECONCILIATION_ESTIMATE_CHANGED and keeps the form open", async () => {
    serveGet(() => ({ items: [notReconciled("2027-01")] }));
    servePost(undefined, () => new ApiError("RECONCILIATION_ESTIMATE_CHANGED", "The estimate changed.", "req-409"));

    renderPage();
    const form = await previewNewMonth("2027-01");
    await waitFor(() => expect(buttonNamed(form, "Confirm")).toBeEnabled());
    fireEvent.click(buttonNamed(form, "Confirm"));

    expect(await within(form).findByText(/Preview again, then confirm\./)).toBeInTheDocument();
    expect(dialog()).toBeInTheDocument();
    expect(buttonNamed(form, "Confirm")).toBeDisabled();
    expect(success).not.toHaveBeenCalled();
    expectNoAlert();
  });

  it("closes the form, reloads the months and says a newer version exists on 409 RECONCILIATION_EXISTS", async () => {
    let calls = 0;
    const get = serveGet(() => {
      calls += 1;
      return { items: [calls === 1 ? notReconciled("2027-01") : reconciled("2027-01")] };
    });
    servePost(undefined, () => new ApiError("RECONCILIATION_EXISTS", "Already reconciled.", "req-409"));

    renderPage();
    const form = await previewNewMonth("2027-01");
    await waitFor(() => expect(buttonNamed(form, "Confirm")).toBeEnabled());
    fireEvent.click(buttonNamed(form, "Confirm"));

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(await screen.findByText(/A newer version of this month's reconciliation already exists\./)).toBeInTheDocument();
    await waitFor(() => expect(monthsUrls(get)).toHaveLength(2));
    await waitFor(() => expect(cell(monthsTable(), "2027-01", "Status")).toBe("Reconciled"));
    expect(success).not.toHaveBeenCalled();
    expectNoAlert();
  });

  it("does the same when the preview itself answers 409 RECONCILIATION_NOT_CURRENT", async () => {
    const get = serveGet(() => ({ items: [reconciled("2026-12")] }));
    servePost(() => new ApiError("RECONCILIATION_NOT_CURRENT", "Already superseded.", "req-409"));

    renderPage();
    const form = await openForm("2026-12", "Correct");
    type("Reason for correction", "Invoice credit applied");
    fireEvent.click(buttonNamed(form, "Preview"));

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(await screen.findByText(/A newer version of this month's reconciliation already exists\./)).toBeInTheDocument();
    await waitFor(() => expect(monthsUrls(get)).toHaveLength(2));
  });

  it("shows the code, message and request id in the form for other errors", async () => {
    serveGet(() => ({ items: [notReconciled("2027-01")] }));
    servePost(
      () => new ApiError("RECONCILIATION_AMOUNT_OUT_OF_RANGE", "The converted amount is too large.", "req-422"),
    );

    renderPage();
    const form = await openForm("2027-01", "Reconcile");
    type("Actual amount", "999999999.99999999");
    type("Source reference", "Console cost");
    fireEvent.click(buttonNamed(form, "Preview"));

    expect(await within(form).findByText("RECONCILIATION_AMOUNT_OUT_OF_RANGE")).toBeInTheDocument();
    expect(within(form).getByText(/The converted amount is too large\./)).toBeInTheDocument();
    expect(within(form).getByText("req-422")).toBeInTheDocument();
    expect(buttonNamed(form, "Confirm")).toBeDisabled();
    expectNoAlert();
  });
});

describe("ProviderCostsPage correction", () => {
  it("prefills the current currency, amount and source reference, leaves the reason empty and requires it", async () => {
    const current = view({
      source_currency: "MYR",
      source_total: "0.10000001",
      source_document_reference: "Invoice INV-0001, 2026-12",
      fx_rate_version_id: null,
      fx_rate_applied: null,
      fx_rate_moment: null,
      reconciled_cost_myr: "0.10000001",
    });
    serveGet(() => ({ items: [reconciled("2026-12", current)] }));
    const post = servePost();

    renderPage();
    const form = await openForm("2026-12", "Correct");

    expect(within(form).getByText("Correct 2026-12")).toBeInTheDocument();
    expect(selectedText(screen.getByLabelText("Currency"))).toBe("MYR");
    expect(screen.getByLabelText("Actual amount")).toHaveValue("0.10000001");
    expect(screen.getByLabelText("Source reference")).toHaveValue("Invoice INV-0001, 2026-12");
    expect(screen.getByLabelText("Reason for correction")).toHaveValue("");
    // 理由必填：空着不能预览。
    expect(buttonNamed(form, "Preview")).toBeDisabled();
    type("Reason for correction", "   ");
    expect(buttonNamed(form, "Preview")).toBeDisabled();

    type("Reason for correction", "Invoice credit applied");
    expect(buttonNamed(form, "Preview")).toBeEnabled();
    fireEvent.click(buttonNamed(form, "Preview"));
    await within(form).findByRole("region", { name: "Preview" });

    expect(postedTo(post, PREVIEW_PATH)).toEqual([
      {
        provider_code: "anthropic",
        billing_month: "2026-12",
        source_currency: "MYR",
        source_total: "0.10000001",
        source_document_reference: "Invoice INV-0001, 2026-12",
        supersedes_id: ZERO_UUID,
        reason: "Invoice credit applied",
      },
    ]);
    expectNoAlert();
  });
});

describe("ProviderCostsPage detail", () => {
  it("expands a reconciled month into its correction chain and the estimate-changed notice", async () => {
    const detailOf = vi.fn<(id: string) => unknown>(() => detail());
    const get = serveGet(() => ({ items: [notReconciled("2027-01"), reconciled("2026-12")] }), detailOf);

    renderPage();
    await screen.findByText("2026-12");
    fireEvent.click(rowOf(monthsTable(), "2026-12"));

    const region = await screen.findByRole("region", { name: "Correction history" });
    await within(region).findByText("second@example.com");
    expect(detailOf).toHaveBeenCalledWith(ZERO_UUID);
    expect(urls(get)).toContain(`${BASE}/${ZERO_UUID}`);

    const chain = region.querySelector("table");
    if (chain === null) {
      throw new Error("no chain table");
    }
    expect(rowKeys(chain)).toEqual([SUPERSEDED_ID, ZERO_UUID]);
    expect(cell(chain, SUPERSEDED_ID, "Version")).toBe("Superseded");
    expect(cell(chain, SUPERSEDED_ID, "Reason")).toBe(DASH);
    expect(cell(chain, SUPERSEDED_ID, "Confirmed by")).toBe("first@example.com");
    expect(cell(chain, SUPERSEDED_ID, "Actual (source)")).toBe("10.00000000 USD");
    expect(cell(chain, SUPERSEDED_ID, "Actual (MYR)")).toBe("44.00000000");
    expect(cell(chain, ZERO_UUID, "Version")).toBe("Current");
    expect(cell(chain, ZERO_UUID, "Reason")).toBe("Invoice credit applied");
    expect(cell(chain, ZERO_UUID, "Confirmed at (MYT)")).toBe("2027-01-06 11:00:00");
    expect(within(region).getByText(/The estimate has changed since this reconciliation was confirmed\./)).toBeInTheDocument();

    // 未对账的行不展开、不去取详情。
    fireEvent.click(rowOf(monthsTable(), "2027-01"));
    expect(detailOf).toHaveBeenCalledTimes(1);
    expectNoAlert();
    expectAdminOnly(get);
  });
});
