/**
 * 用量分析页：四种状态（加载中、失败带 request_id、空列表、无权限）；默认最近 30 天、按供应商分组；切换维度与
 * 时间段（吉隆坡时间换成带时区的 RFC 3339）后的请求参数；时间段校验；翻页；金额、平均值与毛利率原样显示。
 * AIH-TASK-224（设计闸门 #709 v2 §7「前端维度与导出」）：按日 / 按月的请求参数与分组格；「Export CSV」用已应用的条件、
 * 下载中不可点、失败显示码、信息与 request_id 并在再点时收起。合计块本身的内容由 `UsageAnalyticsTotals.test.tsx` 管。
 * AIH-TASK-240（设计闸门 #753 v1 §7「前端按钮」）：「Export Excel」在「Export CSV」右边、点它用已应用的条件与
 * `format: "xlsx"`、下载中两个都不可点且加载状态在被点的那个上、失败文案与 CSV 相同并按原规则收起。
 *
 * ⚠️ 合计块是带边框的 Descriptions，它也渲染成 table 元素，标签在 th 里（有的与列头同名）：列头断言只在带 thead 的
 * 数据表格里找（`dataTable()`）。
 *
 * 请求路径与参数名由 `api/adminUsageAnalytics.test.ts` 管，这里只看页面把什么交给了它。uuid 一律全零。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { configure, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import {
  usageAnalyticsExportUrl,
  type UsageAnalyticsItem,
  type UsageAnalyticsQuery,
  type UsageAnalyticsReport,
  type UsageAnalyticsReportTotals,
} from "../../api/adminUsageAnalytics";
import { ApiError } from "../../api/client";
import { UsageAnalyticsPage } from "./UsageAnalyticsPage";

const ID = "00000000-0000-0000-0000-000000000000";

const api = vi.hoisted(() => ({
  getUsageAnalytics: vi.fn(),
  exportUsageAnalytics: vi.fn(),
}));

vi.mock("../../api/adminUsageAnalytics", async () => {
  const actual = await vi.importActual<typeof import("../../api/adminUsageAnalytics")>(
    "../../api/adminUsageAnalytics",
  );
  return { ...actual, ...api };
});

function item(overrides: Partial<UsageAnalyticsItem> = {}): UsageAnalyticsItem {
  return {
    group_key: "anthropic",
    group_name: null,
    event_count: 3,
    token_event_count: 3,
    input_tokens: 1200,
    output_tokens: 34,
    cache_creation_input_tokens: 5,
    cache_read_input_tokens: 7,
    average_input_tokens: "400",
    average_output_tokens: "11.33333333333333333333333333",
    prepaid: {
      event_count: 2,
      billable_cost: "0.12500000",
      estimated_provider_cost_myr: "0.06250000",
      gross_margin: "0.06250000",
      gross_margin_rate: "0.5",
      covered_estimated_cost_myr: "0.03125000",
      reconciled_provider_cost_myr: "0.03750000",
      provider_cost_myr: "0.06875000",
      provider_cost_basis: "mixed",
      gross_margin_on_provider_cost: "0.05625000",
      gross_margin_on_provider_cost_rate: "0.45",
    },
    internal_metered_only: {
      event_count: 1,
      estimated_provider_cost_myr: "0.03125000",
      reference_customer_price: "0.06000000",
      covered_estimated_cost_myr: "0.03125000",
      reconciled_provider_cost_myr: "0.03437500",
      provider_cost_myr: "0.03437500",
      provider_cost_basis: "reconciled",
    },
    ...overrides,
  };
}

function totals(): UsageAnalyticsReportTotals {
  return {
    event_count: 0,
    token_event_count: 0,
    input_tokens: 0,
    output_tokens: 0,
    cache_creation_input_tokens: 0,
    cache_read_input_tokens: 0,
    average_input_tokens: null,
    average_output_tokens: null,
    prepaid: {
      event_count: 0,
      billable_cost: "0.00000000",
      estimated_provider_cost_myr: "0.00000000",
      gross_margin: "0.00000000",
      gross_margin_rate: null,
      covered_estimated_cost_myr: "0.00000000",
      reconciled_provider_cost_myr: "0.00000000",
      provider_cost_myr: "0.00000000",
      provider_cost_basis: "estimated",
      gross_margin_on_provider_cost: "0.00000000",
      gross_margin_on_provider_cost_rate: null,
    },
    internal_metered_only: {
      event_count: 0,
      estimated_provider_cost_myr: "0.00000000",
      reference_customer_price: "0.00000000",
      covered_estimated_cost_myr: "0.00000000",
      reconciled_provider_cost_myr: "0.00000000",
      provider_cost_myr: "0.00000000",
      provider_cost_basis: "estimated",
    },
    payments: {
      period_basis: "paid_at",
      paid_count: 0,
      topup_amount: "0.00000000",
      gateway_fee: "0.00000000",
      gateway_fee_unknown_count: 0,
    },
    gross_profit: "0.00000000",
    gross_profit_rate: null,
    gross_profit_on_provider_cost: "0.00000000",
    gross_profit_on_provider_cost_rate: null,
    reconciliation_coverage: [],
    unallocated_reconciled_months: [],
    unallocated_reconciled_cost_myr: null,
  };
}

function report(items: UsageAnalyticsItem[], total: number, number = 1): UsageAnalyticsReport {
  return {
    cost_basis: "estimated",
    usage_period_basis: "occurred_at",
    provider_cost_allocation: "pro_rata_estimated_cost",
    reconciliation_period_basis: "utc_calendar_month",
    items,
    page: number,
    page_size: 20,
    total,
    totals: totals(),
  };
}

/** 带 thead 的那张数据表格（合计块的 Descriptions 也是 table 元素，但没有 thead）。 */
function dataTable(): HTMLElement {
  const found = screen.getAllByRole("table").find((table) => table.querySelector("thead") !== null);
  if (found === undefined) {
    throw new Error("no data table");
  }
  return found;
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <UsageAnalyticsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** 表格里的数据行（带 data-row-key，不含表头与空状态行）。 */
function dataRows(): HTMLElement[] {
  return Array.from(document.querySelectorAll<HTMLElement>("tbody tr[data-row-key]"));
}

function row(index: number): HTMLElement {
  const found = dataRows()[index];
  if (found === undefined) {
    throw new Error(`no row ${String(index)}`);
  }
  return found;
}

async function pickGroupBy(label: string): Promise<void> {
  const user = userEvent.setup();
  await user.click(screen.getByLabelText("Group by"));
  fireEvent.click(await screen.findByTitle(label));
}

function setTime(label: string, value: string): void {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}

const FROM_LABEL = "Occurred from (Kuala Lumpur time)";
const TO_LABEL = "Occurred before (Kuala Lumpur time)";

beforeEach(() => {
  vi.clearAllMocks();
});

afterEach(() => {
  vi.useRealTimers();
});

const SLOW = { timeout: 20_000 };
configure({ asyncUtilTimeout: 5_000 });

describe("UsageAnalyticsPage states", SLOW, () => {
  it("asks for the last 30 days by provider while showing a loading state", () => {
    // 只冻住 Date：定时器照常走，antd 与 Testing Library 不受影响。
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-10-18T04:00:00.400Z"));
    api.getUsageAnalytics.mockReturnValue(new Promise(() => undefined));

    renderPage();

    expect(screen.getByText("Loading usage analytics…")).toBeInTheDocument();
    expect(api.getUsageAnalytics).toHaveBeenCalledWith(
      { group_by: "provider", occurred_from: "2026-09-18T04:00:00Z", occurred_to: "2026-10-18T04:00:00Z" },
      1,
      20,
      expect.anything(),
    );
    // 表单里是同一对时刻的吉隆坡墙上时间（UTC+8）。jsdom 会把为零的秒规范化掉，两种写法都认。
    expect(screen.getByLabelText<HTMLInputElement>(FROM_LABEL).value).toMatch(/^2026-09-18T12:00(:00)?$/);
    expect(screen.getByLabelText<HTMLInputElement>(TO_LABEL).value).toMatch(/^2026-10-18T12:00(:00)?$/);
  });

  it("says the costs are estimates and which period each figure follows, for administrators only", () => {
    api.getUsageAnalytics.mockReturnValue(new Promise(() => undefined));

    renderPage();

    expect(screen.getByText("Cost and margin are visible to administrators only.")).toBeInTheDocument();
    const note = screen.getByText(/Provider costs are estimates/);
    expect(note).toHaveTextContent(
      "Usage revenue, provider cost and usage gross margin are counted by call time (occurred_at",
    );
    expect(note).toHaveTextContent("gateway fees and top-ups are counted by payment confirmation time (paid_at");
    expect(note).toHaveTextContent("Gross profit = usage gross margin − gateway fees.");
    expect(note).not.toHaveTextContent("do not include payment gateway fees");
    // 说明文字不是错误提示：验收脚本把任何 role=alert 都当成失败。
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows the backend message and the request id when loading fails", async () => {
    api.getUsageAnalytics.mockRejectedValue(new ApiError("INTERNAL_ERROR", "An unexpected error occurred.", "req-500"));

    renderPage();

    expect(await screen.findByText("The usage analytics could not be loaded.")).toBeInTheDocument();
    expect(screen.getByText(/An unexpected error occurred\./)).toBeInTheDocument();
    expect(screen.getByText("req-500")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("says there is no permission on a 403 instead of a generic failure", async () => {
    api.getUsageAnalytics.mockRejectedValue(
      new ApiError("ADMIN_REQUIRED", "Administrator access is required.", "req-403"),
    );

    renderPage();

    expect(await screen.findByText("Only administrators can view usage analytics.")).toBeInTheDocument();
    expect(screen.getByText("req-403")).toBeInTheDocument();
    expect(screen.queryByText("The usage analytics could not be loaded.")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
  });

  it("says so when nothing was processed in the period", async () => {
    api.getUsageAnalytics.mockResolvedValue(report([], 0));

    renderPage();

    expect(await screen.findByText("No processed usage matches these conditions.")).toBeInTheDocument();
    expect(dataRows()).toHaveLength(0);
  });

  it("still shows the totals, with the zero values from the backend, when there are no groups", async () => {
    api.getUsageAnalytics.mockResolvedValue(report([], 0));

    renderPage();

    const totalsBlock = await screen.findByRole("region", { name: "Totals" });
    expect(screen.getByText("No processed usage matches these conditions.")).toBeInTheDocument();
    expect(within(totalsBlock).getAllByText("0.00000000").length).toBeGreaterThan(0);
    expect(totalsBlock.querySelector("thead")).toBeNull();
    expect(dataRows()).toHaveLength(0);
  });
});

describe("UsageAnalyticsPage table", SLOW, () => {
  it("shows amounts, averages and margin ratios exactly as the backend sent them, never through a number", async () => {
    api.getUsageAnalytics.mockResolvedValue(
      report(
        [
          item({
            // 这些值经过一次浮点数就不是它们了。
            average_input_tokens: "3.333333333333333333333333333",
            prepaid: {
              event_count: 2,
              billable_cost: "99999999999.99999999",
              estimated_provider_cost_myr: "0.10000001",
              gross_margin: "-0.00000001",
              gross_margin_rate: "-0.0000000000000001000000000001",
              covered_estimated_cost_myr: "0.10000001",
              reconciled_provider_cost_myr: "0.11000002",
              provider_cost_myr: "0.11000002",
              provider_cost_basis: "reconciled",
              gross_margin_on_provider_cost: "99999999999.88999997",
              gross_margin_on_provider_cost_rate: "0.9999999999999999999989000000",
            },
            internal_metered_only: {
              event_count: 1,
              estimated_provider_cost_myr: "12345678901234567.12345678",
              reference_customer_price: "0.00000000",
              covered_estimated_cost_myr: "12345678901234567.12345678",
              reconciled_provider_cost_myr: "12345678901234567.98765432",
              provider_cost_myr: "12345678901234567.98765432",
              provider_cost_basis: "reconciled",
            },
          }),
          item({
            group_key: null,
            token_event_count: 0,
            average_input_tokens: null,
            average_output_tokens: null,
            prepaid: {
              event_count: 0,
              billable_cost: "0.00000000",
              estimated_provider_cost_myr: "0.00000000",
              gross_margin: "0.00000000",
              gross_margin_rate: null,
              covered_estimated_cost_myr: "0.00000000",
              reconciled_provider_cost_myr: "0.00000000",
              provider_cost_myr: "0.00000000",
              provider_cost_basis: "estimated",
              gross_margin_on_provider_cost: "0.00000000",
              gross_margin_on_provider_cost_rate: null,
            },
          }),
        ],
        2,
      ),
    );

    renderPage();

    await screen.findByText("anthropic");
    expect(dataRows()).toHaveLength(2);
    const first = within(row(0));
    expect(first.getByText("3.333333333333333333333333333")).toBeInTheDocument();
    expect(first.getByText("99999999999.99999999")).toBeInTheDocument();
    expect(first.getByText("0.10000001")).toBeInTheDocument();
    expect(first.getByText("-0.00000001")).toBeInTheDocument();
    expect(first.getByText("-0.0000000000000001000000000001")).toBeInTheDocument();
    expect(first.getByText("12345678901234567.12345678")).toBeInTheDocument();
    expect(first.getByText("0.00000000")).toBeInTheDocument();
    expect(row(0)).toHaveTextContent("1200");
    // 未解析的 null 组单独说明；没有平均值与毛利率时是「—」。
    expect(within(row(1)).getByText("Unresolved or not set")).toBeInTheDocument();
    expect(within(row(1)).getAllByText("—")).toHaveLength(3);
    // 预付与内部计量是两组列。
    const table = within(dataTable());
    expect(table.getByRole("columnheader", { name: "Prepaid (MYR)" })).toBeInTheDocument();
    expect(table.getByRole("columnheader", { name: "Internal metering only (MYR)" })).toBeInTheDocument();
    expect(table.getByRole("columnheader", { name: "Gross margin" })).toBeInTheDocument();
    expect(table.getByRole("columnheader", { name: "Reference customer price" })).toBeInTheDocument();
    expect(table.getByRole("columnheader", { name: "Provider" })).toBeInTheDocument();
    // AIH-TASK-249：Prepaid 组的「Provider cost」「Margin on provider cost」与 Internal 组的「Provider cost」。
    expect(table.getAllByRole("columnheader", { name: "Provider cost" })).toHaveLength(2);
    expect(table.getByRole("columnheader", { name: "Margin on provider cost" })).toBeInTheDocument();
    expect(first.getByText("0.11000002")).toBeInTheDocument();
    expect(first.getByText("99999999999.88999997")).toBeInTheDocument();
    expect(first.getByText("12345678901234567.98765432")).toBeInTheDocument();
  });
});

/** 表头里最后一行（列组的子列）的列头文字，按顺序；跳过没有文字的占位格。 */
function leafHeaders(): string[] {
  const headerRows = dataTable().querySelectorAll("thead tr");
  const last = headerRows[headerRows.length - 1];
  return Array.from(last?.querySelectorAll("th") ?? [])
    .map((th) => th.textContent?.trim() ?? "")
    .filter((text) => text !== "");
}

/** 第 index 行里第 column 格（0 起，按叶子列的顺序）。 */
function cell(index: number, column: number): HTMLElement {
  const found = row(index).querySelectorAll<HTMLElement>("td")[column];
  if (found === undefined) {
    throw new Error(`no cell ${String(column)}`);
  }
  return found;
}

// 叶子列的顺序：分组、事件数、四种 token、两个平均值（8 列），Prepaid 组 7 列，Internal 组 4 列。
const PREPAID_PROVIDER_COST = 13;
const PREPAID_MARGIN_ON_PROVIDER_COST = 14;
const INTERNAL_PROVIDER_COST = 18;

describe("UsageAnalyticsPage provider cost (AIH-TASK-249)", SLOW, () => {
  it("adds two columns to the Prepaid group and one to the Internal group, with the basis in small text", async () => {
    api.getUsageAnalytics.mockResolvedValue(
      report(
        [
          item(),
          item({
            group_key: "openai",
            prepaid: {
              ...item().prepaid,
              provider_cost_myr: "0.06250000",
              provider_cost_basis: "estimated",
              gross_margin_on_provider_cost: "0.333333333333333333333333333",
            },
            internal_metered_only: {
              ...item().internal_metered_only,
              provider_cost_myr: "0.03500001",
              provider_cost_basis: "mixed",
            },
          }),
        ],
        2,
      ),
    );

    renderPage();

    await screen.findByText("anthropic");
    // 其他列不变，新列只追加在各自列组的末尾。
    expect(leafHeaders()).toEqual([
      "Events",
      "Billable amount",
      "Estimated provider cost",
      "Gross margin",
      "Gross margin ratio",
      "Provider cost",
      "Margin on provider cost",
      "Events",
      "Estimated provider cost",
      "Reference customer price",
      "Provider cost",
    ]);
    const table = within(dataTable());
    expect(table.getByRole("columnheader", { name: "Prepaid (MYR)" })).toBeInTheDocument();
    expect(table.getByRole("columnheader", { name: "Internal metering only (MYR)" })).toBeInTheDocument();

    // 值原样，下一行小字是口径的显示文案（不是后端原词）。
    expect(within(cell(0, PREPAID_PROVIDER_COST)).getByText("0.06875000")).toBeInTheDocument();
    expect(within(cell(0, PREPAID_PROVIDER_COST)).getByText("Mixed (partly reconciled)")).toBeInTheDocument();
    expect(cell(0, PREPAID_MARGIN_ON_PROVIDER_COST).textContent).toBe("0.05625000");
    expect(within(cell(0, INTERNAL_PROVIDER_COST)).getByText("0.03437500")).toBeInTheDocument();
    expect(within(cell(0, INTERNAL_PROVIDER_COST)).getByText("Reconciled")).toBeInTheDocument();
    expect(within(cell(1, PREPAID_PROVIDER_COST)).getByText("0.06250000")).toBeInTheDocument();
    expect(within(cell(1, PREPAID_PROVIDER_COST)).getByText("Estimated")).toBeInTheDocument();
    expect(cell(1, PREPAID_MARGIN_ON_PROVIDER_COST).textContent).toBe("0.333333333333333333333333333");
    expect(within(cell(1, INTERNAL_PROVIDER_COST)).getByText("0.03500001")).toBeInTheDocument();
    expect(within(cell(1, INTERNAL_PROVIDER_COST)).getByText("Mixed (partly reconciled)")).toBeInTheDocument();
    for (const index of [0, 1]) {
      expect(within(row(index)).queryByText("mixed")).not.toBeInTheDocument();
      expect(within(row(index)).queryByText("estimated")).not.toBeInTheDocument();
      expect(within(row(index)).queryByText("reconciled")).not.toBeInTheDocument();
    }
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("explains how reconciled cost is allocated, the UTC months and the three bases", () => {
    api.getUsageAnalytics.mockReturnValue(new Promise(() => undefined));

    renderPage();

    const note = screen.getByText(/allocated to groups in proportion to their estimated cost/);
    expect(note).toHaveTextContent("it is not the actual cost of each request");
    expect(note).toHaveTextContent("Reconciled months are UTC calendar months.");
    expect(note).toHaveTextContent("Estimated: no reconciled month covers this cost, so it is the estimate");
    expect(note).toHaveTextContent("Reconciled: all of the estimated cost falls in reconciled months");
    expect(note).toHaveTextContent("Mixed (partly reconciled): part is reconciled and the rest is the estimate.");
    expect(note).toHaveTextContent(
      "Gross margin / Gross profit on provider cost use the prepaid provider cost with the same basis; gateway fees are counted by paid_at.",
    );
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows the provider cost totals and the coverage line with the rest of the totals", async () => {
    api.getUsageAnalytics.mockResolvedValue(report([item()], 1));

    renderPage();

    const totalsBlock = await screen.findByRole("region", { name: "Totals" });
    expect(within(totalsBlock).getByText("Reconciled months (UTC): none — provider costs are estimated")).toBeInTheDocument();
    expect(totalsBlock.querySelector("thead")).toBeNull();
    expect(dataRows()).toHaveLength(1);
  });
});

describe("UsageAnalyticsPage conditions", SLOW, () => {
  it("sends the new dimension, the period as RFC 3339 with a zone, and only the filters that are filled in", async () => {
    const user = userEvent.setup();
    api.getUsageAnalytics.mockResolvedValue(report([item()], 1));

    renderPage();
    await screen.findByText("anthropic");
    await pickGroupBy("Customer");
    // 吉隆坡 9 月 21 日 00:00 是 UTC 9 月 20 日 16:00。
    setTime(FROM_LABEL, "2026-09-21T00:00");
    setTime(TO_LABEL, "2026-09-22T07:59:59");
    await user.click(screen.getByLabelText("Provider (as reported)"));
    await user.paste(" anthropic ");
    await user.click(screen.getByLabelText("Model (as reported)"));
    await user.paste("   ");
    api.getUsageAnalytics.mockResolvedValue(
      report([item({ group_key: ID, group_name: "Fictional Sdn Bhd" })], 1),
    );
    await user.click(screen.getByRole("button", { name: "Apply filters" }));

    await waitFor(() =>
      expect(api.getUsageAnalytics).toHaveBeenLastCalledWith(
        {
          group_by: "customer",
          occurred_from: "2026-09-20T16:00:00Z",
          occurred_to: "2026-09-21T23:59:59Z",
          provider: "anthropic",
        },
        1,
        20,
        expect.anything(),
      ),
    );
    // 表头跟着新的维度走，客户显示名称与 public id。
    expect(await screen.findByText("Fictional Sdn Bhd")).toBeInTheDocument();
    expect(within(dataTable()).getByRole("columnheader", { name: "Customer" })).toBeInTheDocument();
    expect(row(0)).toHaveTextContent(ID);
  });

  it("goes back to page 1 when the conditions change", async () => {
    const user = userEvent.setup();
    api.getUsageAnalytics.mockImplementation((_query: unknown, requested: number) =>
      Promise.resolve(report([item({ group_key: `model-on-page-${String(requested)}` })], 45, requested)),
    );

    renderPage();
    expect(await screen.findByText("45 groups")).toBeInTheDocument();
    await user.click(screen.getByTitle("2"));
    expect(await screen.findByText("model-on-page-2")).toBeInTheDocument();
    expect(api.getUsageAnalytics).toHaveBeenLastCalledWith(
      expect.objectContaining({ group_by: "provider" }),
      2,
      20,
      expect.anything(),
    );

    await pickGroupBy("Model");
    await user.click(screen.getByRole("button", { name: "Apply filters" }));

    await waitFor(() =>
      expect(api.getUsageAnalytics).toHaveBeenLastCalledWith(
        expect.objectContaining({ group_by: "model" }),
        1,
        20,
        expect.anything(),
      ),
    );
  });

  it("refuses a reversed, too long or empty period without asking the backend", async () => {
    const user = userEvent.setup();
    api.getUsageAnalytics.mockResolvedValue(report([item()], 1));

    renderPage();
    await screen.findByText("anthropic");
    const apply = screen.getByRole("button", { name: "Apply filters" });

    setTime(FROM_LABEL, "2026-09-22T00:00");
    setTime(TO_LABEL, "2026-09-21T00:00");
    await user.click(apply);
    expect(await screen.findByText("The end must be later than the start.")).toBeInTheDocument();

    setTime(FROM_LABEL, "2025-01-01T00:00");
    setTime(TO_LABEL, "2026-01-03T00:00");
    await user.click(apply);
    expect(await screen.findByText("The period can be at most 366 days.")).toBeInTheDocument();

    setTime(FROM_LABEL, "");
    await user.click(apply);
    expect(await screen.findByText("This field is required.")).toBeInTheDocument();

    expect(api.getUsageAnalytics).toHaveBeenCalledTimes(1);
  });
});

/** 第一行分组格（第一列）的文字。 */
function groupCellText(index: number): string {
  return row(index).querySelector("td")?.textContent ?? "";
}

describe("UsageAnalyticsPage days and months", SLOW, () => {
  it("asks for days and shows only the date key in the group cell", async () => {
    const user = userEvent.setup();
    api.getUsageAnalytics.mockResolvedValue(report([item()], 1));

    renderPage();
    await screen.findByText("anthropic");
    api.getUsageAnalytics.mockResolvedValue(
      report([item({ group_key: "2026-10-01" }), item({ group_key: "2026-10-02" })], 2),
    );
    await pickGroupBy("Day");
    await user.click(screen.getByRole("button", { name: "Apply filters" }));

    await waitFor(() =>
      expect(api.getUsageAnalytics).toHaveBeenLastCalledWith(
        expect.objectContaining({ group_by: "day" }),
        1,
        20,
        expect.anything(),
      ),
    );
    expect(await screen.findByText("2026-10-01")).toBeInTheDocument();
    expect(within(dataTable()).getByRole("columnheader", { name: "Day" })).toBeInTheDocument();
    expect(groupCellText(0)).toBe("2026-10-01");
    expect(groupCellText(1)).toBe("2026-10-02");
  });

  it("asks for months and shows only the month key in the group cell", async () => {
    const user = userEvent.setup();
    api.getUsageAnalytics.mockResolvedValue(report([item()], 1));

    renderPage();
    await screen.findByText("anthropic");
    api.getUsageAnalytics.mockResolvedValue(report([item({ group_key: "2026-10" })], 1));
    await pickGroupBy("Month");
    await user.click(screen.getByRole("button", { name: "Apply filters" }));

    await waitFor(() =>
      expect(api.getUsageAnalytics).toHaveBeenLastCalledWith(
        expect.objectContaining({ group_by: "month" }),
        1,
        20,
        expect.anything(),
      ),
    );
    expect(await screen.findByText("2026-10")).toBeInTheDocument();
    expect(within(dataTable()).getByRole("columnheader", { name: "Month" })).toBeInTheDocument();
    expect(groupCellText(0)).toBe("2026-10");
  });
});

describe("UsageAnalyticsPage export", SLOW, () => {
  const EXPORT_NAME = /Export CSV/;

  it("exports the applied conditions, not the form values that were never applied", async () => {
    const user = userEvent.setup();
    api.getUsageAnalytics.mockResolvedValue(report([item()], 1));
    api.exportUsageAnalytics.mockResolvedValue(undefined);

    renderPage();
    await screen.findByText("anthropic");
    const applied: unknown = api.getUsageAnalytics.mock.calls[0]?.[0];
    expect(applied).toEqual(expect.objectContaining({ group_by: "provider" }));

    // 改了表单却不提交：导出仍用表格的那一组条件。
    await pickGroupBy("Month");
    await user.click(screen.getByLabelText("Provider (as reported)"));
    await user.paste("anthropic");
    await user.click(screen.getByRole("button", { name: EXPORT_NAME }));

    await waitFor(() => expect(api.exportUsageAnalytics).toHaveBeenCalledTimes(1));
    expect(api.exportUsageAnalytics).toHaveBeenCalledWith(applied);
    // 导出不让表格与合计重新请求。
    expect(api.getUsageAnalytics).toHaveBeenCalledTimes(1);

    // 提交之后，导出跟着新的已应用条件走。
    await user.click(screen.getByRole("button", { name: "Apply filters" }));
    await waitFor(() => expect(api.getUsageAnalytics).toHaveBeenCalledTimes(2));
    await user.click(screen.getByRole("button", { name: EXPORT_NAME }));
    await waitFor(() => expect(api.exportUsageAnalytics).toHaveBeenCalledTimes(2));
    expect(api.exportUsageAnalytics).toHaveBeenLastCalledWith(
      expect.objectContaining({ group_by: "month", provider: "anthropic" }),
    );
  });

  it("cannot be clicked again while the download is running", async () => {
    const user = userEvent.setup();
    api.getUsageAnalytics.mockResolvedValue(report([item()], 1));
    api.exportUsageAnalytics.mockReturnValue(new Promise(() => undefined));

    renderPage();
    await screen.findByText("anthropic");
    await user.click(screen.getByRole("button", { name: EXPORT_NAME }));

    await waitFor(() => {
      expect(screen.getByRole("button", { name: EXPORT_NAME })).toBeDisabled();
    });
    fireEvent.click(screen.getByRole("button", { name: EXPORT_NAME }));
    expect(api.exportUsageAnalytics).toHaveBeenCalledTimes(1);
    // 表格照常显示。
    expect(dataRows()).toHaveLength(1);
  });

  it("shows the code, the message and the request id of a failure, and clears them on the next click", async () => {
    const user = userEvent.setup();
    api.getUsageAnalytics.mockResolvedValue(report([item()], 1));
    api.exportUsageAnalytics.mockRejectedValueOnce(
      new ApiError("EXPORT_TOO_LARGE", "The export has more than 20000 rows.", "req-422"),
    );

    renderPage();
    await screen.findByText("anthropic");
    await user.click(screen.getByRole("button", { name: EXPORT_NAME }));

    expect(
      await screen.findByText("There are too many groups to export. Shorten the period or add filters."),
    ).toBeInTheDocument();
    expect(screen.getByText("EXPORT_TOO_LARGE")).toBeInTheDocument();
    expect(screen.getByText(/The export has more than 20000 rows\./)).toBeInTheDocument();
    expect(screen.getByText("req-422")).toBeInTheDocument();
    // 表格不受影响。
    expect(dataRows()).toHaveLength(1);

    api.exportUsageAnalytics.mockReturnValue(new Promise(() => undefined));
    await user.click(screen.getByRole("button", { name: EXPORT_NAME }));

    await waitFor(() => expect(screen.queryByText("req-422")).not.toBeInTheDocument());
    expect(screen.queryByText("EXPORT_TOO_LARGE")).not.toBeInTheDocument();
  });

  it("clears a failure when the applied conditions change", async () => {
    const user = userEvent.setup();
    api.getUsageAnalytics.mockResolvedValue(report([item()], 1));
    api.exportUsageAnalytics.mockRejectedValueOnce(
      new ApiError("EXPORT_BUSY", "Another export is in progress.", "req-503"),
    );

    renderPage();
    await screen.findByText("anthropic");
    await user.click(screen.getByRole("button", { name: EXPORT_NAME }));

    expect(await screen.findByText("Another export is in progress. Try again in a few seconds.")).toBeInTheDocument();
    expect(screen.getByText("EXPORT_BUSY")).toBeInTheDocument();
    expect(screen.getByText("req-503")).toBeInTheDocument();

    await pickGroupBy("Day");
    await user.click(screen.getByRole("button", { name: "Apply filters" }));

    await waitFor(() => expect(screen.queryByText("req-503")).not.toBeInTheDocument());
    expect(screen.queryByText("EXPORT_BUSY")).not.toBeInTheDocument();
  });
});

describe("UsageAnalyticsPage Excel export (AIH-TASK-240)", SLOW, () => {
  const EXPORT_NAME = /Export CSV/;
  const XLSX_NAME = /Export Excel/;

  it("exports the applied conditions with format xlsx, not the form values that were never applied", async () => {
    const user = userEvent.setup();
    api.getUsageAnalytics.mockResolvedValue(report([item()], 1));
    api.exportUsageAnalytics.mockResolvedValue(undefined);

    renderPage();
    await screen.findByText("anthropic");
    const applied = api.getUsageAnalytics.mock.calls[0]?.[0] as UsageAnalyticsQuery;

    // 改了表单却不提交：导出仍用表格的那一组条件。
    await pickGroupBy("Month");
    await user.click(screen.getByLabelText("Customer ID"));
    await user.paste(ID);
    await user.click(screen.getByRole("button", { name: XLSX_NAME }));

    await waitFor(() => expect(api.exportUsageAnalytics).toHaveBeenCalledTimes(1));
    expect(api.exportUsageAnalytics).toHaveBeenCalledWith({ ...applied, format: "xlsx" });
    // 导出不让表格与合计重新请求。
    expect(api.getUsageAnalytics).toHaveBeenCalledTimes(1);
    // 交给 api 的条件落到管理端的导出地址上，不碰门户路径。
    const url = usageAnalyticsExportUrl({ ...applied, format: "xlsx" });
    expect(url.startsWith("/api/v1/admin/usage-analytics/export?")).toBe(true);
    expect(url.endsWith("&format=xlsx")).toBe(true);
    expect(url).not.toContain("/api/v1/customer/");
    expect(url).not.toContain(ID);
  });

  it("puts Export Excel right of Export CSV, disables both while exporting and shows loading only on Export Excel when that is the one clicked", async () => {
    const user = userEvent.setup();
    api.getUsageAnalytics.mockResolvedValue(report([item()], 1));
    api.exportUsageAnalytics.mockReturnValue(new Promise(() => undefined));

    renderPage();
    await screen.findByText("anthropic");

    expect(screen.getAllByRole("button", { name: EXPORT_NAME })).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: XLSX_NAME })).toHaveLength(1);
    const csvBefore = screen.getByRole("button", { name: EXPORT_NAME });
    const xlsxBefore = screen.getByRole("button", { name: XLSX_NAME });
    expect(csvBefore).toBeEnabled();
    expect(xlsxBefore).toBeEnabled();
    expect(csvBefore.compareDocumentPosition(xlsxBefore) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    await user.click(xlsxBefore);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: XLSX_NAME })).toBeDisabled();
    });
    const csv = screen.getByRole("button", { name: EXPORT_NAME });
    const xlsx = screen.getByRole("button", { name: XLSX_NAME });
    expect(csv).toBeDisabled();
    expect(within(xlsx).getByRole("img", { name: "loading" })).toBeInTheDocument();
    expect(within(csv).queryByRole("img", { name: "loading" })).not.toBeInTheDocument();
    fireEvent.click(csv);
    fireEvent.click(xlsx);
    expect(api.exportUsageAnalytics).toHaveBeenCalledTimes(1);
    // 表格照常显示。
    expect(dataRows()).toHaveLength(1);
  });

  it("shows the code and the request id of an Excel failure, clears them on the next click of either button, and then shows loading only on Export CSV", async () => {
    const user = userEvent.setup();
    api.getUsageAnalytics.mockResolvedValue(report([item()], 1));
    api.exportUsageAnalytics.mockRejectedValueOnce(
      new ApiError("EXPORT_TOO_LARGE", "The export has more than 20000 rows.", "req-422"),
    );

    renderPage();
    await screen.findByText("anthropic");
    await user.click(screen.getByRole("button", { name: XLSX_NAME }));

    // 文案与 CSV 相同，不提格式。
    expect(
      await screen.findByText("There are too many groups to export. Shorten the period or add filters."),
    ).toBeInTheDocument();
    expect(screen.getByText("EXPORT_TOO_LARGE")).toBeInTheDocument();
    expect(screen.getByText(/The export has more than 20000 rows\./)).toBeInTheDocument();
    expect(screen.getByText("req-422")).toBeInTheDocument();
    expect(dataRows()).toHaveLength(1);

    api.exportUsageAnalytics.mockReturnValue(new Promise(() => undefined));
    await user.click(screen.getByRole("button", { name: EXPORT_NAME }));

    await waitFor(() => expect(screen.queryByText("req-422")).not.toBeInTheDocument());
    expect(screen.queryByText("EXPORT_TOO_LARGE")).not.toBeInTheDocument();

    // 这次点的是 CSV：两个都不可点，加载状态只在 CSV 上。
    await waitFor(() => {
      expect(screen.getByRole("button", { name: XLSX_NAME })).toBeDisabled();
    });
    const csv = screen.getByRole("button", { name: EXPORT_NAME });
    const xlsx = screen.getByRole("button", { name: XLSX_NAME });
    expect(csv).toBeDisabled();
    expect(within(csv).getByRole("img", { name: "loading" })).toBeInTheDocument();
    expect(within(xlsx).queryByRole("img", { name: "loading" })).not.toBeInTheDocument();
  });

  it("clears an Excel failure when the applied conditions change", async () => {
    const user = userEvent.setup();
    api.getUsageAnalytics.mockResolvedValue(report([item()], 1));
    api.exportUsageAnalytics.mockRejectedValueOnce(
      new ApiError("EXPORT_BUSY", "Another export is in progress.", "req-503"),
    );

    renderPage();
    await screen.findByText("anthropic");
    await user.click(screen.getByRole("button", { name: XLSX_NAME }));

    expect(await screen.findByText("Another export is in progress. Try again in a few seconds.")).toBeInTheDocument();
    expect(screen.getByText("EXPORT_BUSY")).toBeInTheDocument();
    expect(screen.getByText("req-503")).toBeInTheDocument();

    await pickGroupBy("Day");
    await user.click(screen.getByRole("button", { name: "Apply filters" }));

    await waitFor(() => expect(screen.queryByText("req-503")).not.toBeInTheDocument());
    expect(screen.queryByText("EXPORT_BUSY")).not.toBeInTheDocument();
  });
});
