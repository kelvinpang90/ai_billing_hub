/**
 * 用量分析页：四种状态（加载中、失败带 request_id、空列表、无权限）；默认最近 30 天、按供应商分组；切换维度与
 * 时间段（吉隆坡时间换成带时区的 RFC 3339）后的请求参数；时间段校验；翻页；金额、平均值与毛利率原样显示。
 *
 * 请求路径与参数名由 `api/adminUsageAnalytics.test.ts` 管，这里只看页面把什么交给了它。uuid 一律全零。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { configure, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import type { UsageAnalyticsItem, UsageAnalyticsReport } from "../../api/adminUsageAnalytics";
import { ApiError } from "../../api/client";
import { UsageAnalyticsPage } from "./UsageAnalyticsPage";

const ID = "00000000-0000-0000-0000-000000000000";

const api = vi.hoisted(() => ({
  getUsageAnalytics: vi.fn(),
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
    },
    internal_metered_only: {
      event_count: 1,
      estimated_provider_cost_myr: "0.03125000",
      reference_customer_price: "0.06000000",
    },
    ...overrides,
  };
}

function report(items: UsageAnalyticsItem[], total: number, number = 1): UsageAnalyticsReport {
  return { cost_basis: "estimated", items, page: number, page_size: 20, total };
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

  it("says the costs are estimates without gateway fees, for administrators only", () => {
    api.getUsageAnalytics.mockReturnValue(new Promise(() => undefined));

    renderPage();

    expect(screen.getByText("Cost and margin are visible to administrators only.")).toBeInTheDocument();
    expect(screen.getByText(/Provider costs are estimates/)).toHaveTextContent("do not include payment gateway fees");
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
            },
            internal_metered_only: {
              event_count: 1,
              estimated_provider_cost_myr: "12345678901234567.12345678",
              reference_customer_price: "0.00000000",
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
    expect(screen.getByRole("columnheader", { name: "Prepaid (MYR)" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Internal metering only (MYR)" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Gross margin" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Reference customer price" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Provider" })).toBeInTheDocument();
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
    expect(screen.getByRole("columnheader", { name: "Customer" })).toBeInTheDocument();
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
