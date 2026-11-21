/**
 * 用量分析的合计块（设计闸门 #709 v2 §7「前端合计块」「前端不经 number」）：各值原样显示后端字符串、null 是「—」；
 * 手续费未知时的提示带数量，手续费块为 null 时说明不显示毛利；两条提示都不是 role=alert；合计块不是数据表格
 * （Descriptions 内部的 table 元素允许，但没有 thead、没有带 data-row-key 的行 —— 验收脚本按这两样识别数据表格）。
 * AIH-TASK-249（设计闸门 #756 v2 §7「前端分析页」）：对账后成本的 8 项各自一对 th / td、td 里只有那一个值（口径是显示
 * 文案，null 是「—」）；覆盖说明的两行逐字固定，覆盖为空时只有 none 那一行。uuid 一律全零。
 */

import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import "../../i18n";
import type {
  UsageAnalyticsPayments,
  UsageAnalyticsReconciliationCoverage,
  UsageAnalyticsReportTotals,
} from "../../api/adminUsageAnalytics";
import { UsageAnalyticsTotals } from "./UsageAnalyticsTotals";

const ID = "00000000-0000-0000-0000-000000000000";

const UNKNOWN_NOTE = /paid payment\(s\) have no recorded gateway fee; gross profit does not deduct them\./;
const NOT_ATTRIBUTABLE_NOTE =
  "Gateway fees cannot be attributed to project, provider, model or feature filters, so gross profit is not shown.";

function payments(overrides: Partial<UsageAnalyticsPayments> = {}): UsageAnalyticsPayments {
  return {
    period_basis: "paid_at",
    paid_count: 3,
    topup_amount: "300.00000000",
    gateway_fee: "3.00000000",
    gateway_fee_unknown_count: 0,
    ...overrides,
  };
}

function totals(overrides: Partial<UsageAnalyticsReportTotals> = {}): UsageAnalyticsReportTotals {
  return {
    event_count: 120,
    token_event_count: 118,
    input_tokens: 1180,
    output_tokens: 590,
    cache_creation_input_tokens: 0,
    cache_read_input_tokens: 0,
    average_input_tokens: "10",
    average_output_tokens: "5",
    prepaid: {
      event_count: 100,
      billable_cost: "10.00000000",
      estimated_provider_cost_myr: "6.00000000",
      gross_margin: "4.00000000",
      gross_margin_rate: "0.4",
      covered_estimated_cost_myr: "5.00000000",
      reconciled_provider_cost_myr: "5.50000000",
      provider_cost_myr: "6.50000000",
      provider_cost_basis: "mixed",
      gross_margin_on_provider_cost: "3.50000000",
      gross_margin_on_provider_cost_rate: "0.35",
    },
    internal_metered_only: {
      event_count: 20,
      estimated_provider_cost_myr: "1.00000000",
      reference_customer_price: "2.00000000",
      covered_estimated_cost_myr: "1.00000000",
      reconciled_provider_cost_myr: "1.10000000",
      provider_cost_myr: "1.10000000",
      provider_cost_basis: "reconciled",
    },
    payments: payments(),
    gross_profit: "1.00000000",
    gross_profit_rate: "0.1",
    gross_profit_on_provider_cost: "0.50000000",
    gross_profit_on_provider_cost_rate: "0.05",
    reconciliation_coverage: [],
    unallocated_reconciled_months: [],
    unallocated_reconciled_cost_myr: null,
    ...overrides,
  };
}

function coverage(
  billingMonth: string,
  periodStart: string,
  periodEnd: string,
  fullyInRange: boolean,
  allocatable: boolean,
): UsageAnalyticsReconciliationCoverage {
  return {
    reconciliation_id: ID,
    provider_code: "anthropic",
    billing_month: billingMonth,
    period_start: periodStart,
    period_end: periodEnd,
    fully_in_range: fullyInRange,
    allocatable,
  };
}

/**
 * 设计 §7「前端分析页」的覆盖：四条记录对应同一个连续区间 [2026-12-01, 2027-03-15)，只有末月 2027-03 不完整；1、2 月
 * 不可分摊。
 */
const COVERAGE = [
  coverage("2026-12", "2026-12-01T00:00:00", "2027-01-01T00:00:00", true, true),
  coverage("2027-01", "2027-01-01T00:00:00", "2027-02-01T00:00:00", true, false),
  coverage("2027-02", "2027-02-01T00:00:00", "2027-03-01T00:00:00", true, false),
  coverage("2027-03", "2027-03-01T00:00:00", "2027-04-01T00:00:00", false, true),
];

const RECONCILED_LINE = "Reconciled months (UTC): anthropic 2026-12, anthropic 2027-03 (partial)";
const NOT_ALLOCATED_LINE =
  "Not allocated (no estimated cost in that month, UTC): anthropic 2027-01, anthropic 2027-02 — reconciled cost not included in provider cost: 7.50000000 MYR";
const NONE_LINE = "Reconciled months (UTC): none — provider costs are estimated";
const PROVIDER_COST_NOTE =
  "Gross margin / Gross profit on provider cost are calculated from the provider cost of the prepaid block and have the same basis as Provider cost basis; gateway fees are counted by payment confirmation time (paid_at).";

function region(): HTMLElement {
  return screen.getByRole("region", { name: "Totals" });
}

/** 合计块里标签为 label 的那一项的值（带边框的 Descriptions：标签在 th，值在紧跟的 td）。 */
function valueOf(label: string): string {
  const cells: Element[] = [];
  for (const th of Array.from(region().querySelectorAll<HTMLTableCellElement>("th"))) {
    const td = th.nextElementSibling;
    if (th.textContent?.trim() === label && td !== null && td.tagName === "TD") {
      cells.push(td);
    }
  }
  expect(cells).toHaveLength(1);
  return cells[0]?.textContent?.trim() ?? "";
}

describe("UsageAnalyticsTotals", () => {
  it("shows every total exactly as the backend sent it when all gateway fees are known", () => {
    render(<UsageAnalyticsTotals totals={totals()} />);

    expect(valueOf("Usage revenue")).toBe("10.00000000");
    expect(valueOf("Estimated provider cost")).toBe("6.00000000");
    expect(valueOf("Usage gross margin")).toBe("4.00000000");
    expect(valueOf("Gateway fees")).toBe("3.00000000");
    expect(valueOf("Gross profit")).toBe("1.00000000");
    expect(valueOf("Gross margin")).toBe("0.1");
    expect(valueOf("Top-ups received (cash, not revenue)")).toBe("300.00000000");
    expect(valueOf("Paid payments")).toBe("3");
    expect(valueOf("Internal metering events")).toBe("20");
    expect(valueOf("Internal metering estimated provider cost")).toBe("1.00000000");
    expect(valueOf("Reference customer price")).toBe("2.00000000");
    expect(screen.queryByText(UNKNOWN_NOTE)).not.toBeInTheDocument();
    expect(screen.queryByText(NOT_ATTRIBUTABLE_NOTE)).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("says how many paid payments have no recorded gateway fee, as plain text", () => {
    render(<UsageAnalyticsTotals totals={totals({ payments: payments({ gateway_fee_unknown_count: 2 }) })} />);

    const note = within(region()).getByText(UNKNOWN_NOTE);
    expect(note).toHaveTextContent(
      "2 paid payment(s) have no recorded gateway fee; gross profit does not deduct them.",
    );
    expect(valueOf("Gross profit")).toBe("1.00000000");
    expect(screen.queryByText(NOT_ATTRIBUTABLE_NOTE)).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows dashes and explains why when the gateway fees cannot be attributed", () => {
    render(<UsageAnalyticsTotals totals={totals({ payments: null, gross_profit: null, gross_profit_rate: null })} />);

    expect(within(region()).getByText(NOT_ATTRIBUTABLE_NOTE)).toBeInTheDocument();
    expect(valueOf("Gateway fees")).toBe("—");
    expect(valueOf("Gross profit")).toBe("—");
    expect(valueOf("Gross margin")).toBe("—");
    expect(valueOf("Top-ups received (cash, not revenue)")).toBe("—");
    expect(valueOf("Paid payments")).toBe("—");
    // 用量合计照常。
    expect(valueOf("Usage revenue")).toBe("10.00000000");
    expect(valueOf("Usage gross margin")).toBe("4.00000000");
    expect(screen.queryByText(UNKNOWN_NOTE)).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows a dash for the gross margin ratio when there is no usage revenue", () => {
    render(
      <UsageAnalyticsTotals
        totals={totals({
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
          gross_profit: "-3.00000000",
          gross_profit_rate: null,
          gross_profit_on_provider_cost: "-3.00000000",
          gross_profit_on_provider_cost_rate: null,
        })}
      />,
    );

    expect(valueOf("Usage revenue")).toBe("0.00000000");
    expect(valueOf("Gross profit")).toBe("-3.00000000");
    expect(valueOf("Gross margin")).toBe("—");
    expect(valueOf("Gross margin on provider cost (ratio)")).toBe("—");
    expect(valueOf("Gross profit on provider cost (ratio)")).toBe("—");
    expect(valueOf("Provider cost basis")).toBe("Estimated");
  });

  it("is not a data table: no table header and no rows with a row key", () => {
    render(<UsageAnalyticsTotals totals={totals({ payments: payments({ gateway_fee_unknown_count: 2 }) })} />);

    expect(region().tagName).toBe("SECTION");
    expect(region().querySelector("thead")).toBeNull();
    expect(region().querySelector("[data-row-key]")).toBeNull();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("never passes amounts or ratios through a number", () => {
    render(
      <UsageAnalyticsTotals
        totals={totals({
          prepaid: {
            event_count: 3,
            billable_cost: "0.10000001",
            estimated_provider_cost_myr: "0.00000001",
            gross_margin: "0.10000000",
            gross_margin_rate: "0.9999999000000009999999",
            covered_estimated_cost_myr: "0.00000001",
            reconciled_provider_cost_myr: "0.00000001",
            provider_cost_myr: "0.00000001",
            provider_cost_basis: "reconciled",
            gross_margin_on_provider_cost: "0.10000000",
            gross_margin_on_provider_cost_rate: "0.9999999000000009999999",
          },
          payments: payments({ gateway_fee: "0.03333333" }),
          gross_profit: "0.06666667",
          gross_profit_rate: "0.333333333333333333333333333",
          gross_profit_on_provider_cost: "0.06666667",
          gross_profit_on_provider_cost_rate: "0.333333333333333333333333333",
        })}
      />,
    );

    expect(valueOf("Usage revenue")).toBe("0.10000001");
    expect(valueOf("Gross margin")).toBe("0.333333333333333333333333333");
    expect(valueOf("Gateway fees")).toBe("0.03333333");
    expect(valueOf("Gross profit")).toBe("0.06666667");
    expect(valueOf("Provider cost (reconciled where available)")).toBe("0.00000001");
    expect(valueOf("Gross margin on provider cost (ratio)")).toBe("0.9999999000000009999999");
    expect(valueOf("Gross profit on provider cost")).toBe("0.06666667");
    expect(valueOf("Gross profit on provider cost (ratio)")).toBe("0.333333333333333333333333333");
  });
});

describe("UsageAnalyticsTotals provider cost (AIH-TASK-249)", () => {
  it("shows the eight provider cost items, each td holding only its own value", () => {
    render(
      <UsageAnalyticsTotals
        totals={totals({
          reconciliation_coverage: COVERAGE,
          unallocated_reconciled_months: ["anthropic:2027-01", "anthropic:2027-02"],
          unallocated_reconciled_cost_myr: "7.50000000",
          gross_profit_on_provider_cost: null,
          gross_profit_on_provider_cost_rate: null,
        })}
      />,
    );

    expect(valueOf("Provider cost (reconciled where available)")).toBe("6.50000000");
    expect(valueOf("Provider cost basis")).toBe("Mixed (partly reconciled)");
    expect(valueOf("Internal provider cost (reconciled where available)")).toBe("1.10000000");
    expect(valueOf("Internal provider cost basis")).toBe("Reconciled");
    expect(valueOf("Gross margin on provider cost")).toBe("3.50000000");
    expect(valueOf("Gross margin on provider cost (ratio)")).toBe("0.35");
    expect(valueOf("Gross profit on provider cost")).toBe("—");
    expect(valueOf("Gross profit on provider cost (ratio)")).toBe("—");
    // 页面不显示后端原词。
    expect(within(region()).queryByText("mixed")).not.toBeInTheDocument();
    expect(within(region()).queryByText("reconciled")).not.toBeInTheDocument();
    // 原有各项照旧。
    expect(valueOf("Estimated provider cost")).toBe("6.00000000");
    expect(valueOf("Gross profit")).toBe("1.00000000");
  });

  it("lists the reconciled and the not allocated months on two lines, as plain text", () => {
    render(
      <UsageAnalyticsTotals
        totals={totals({
          reconciliation_coverage: COVERAGE,
          unallocated_reconciled_months: ["anthropic:2027-01", "anthropic:2027-02"],
          unallocated_reconciled_cost_myr: "7.50000000",
        })}
      />,
    );

    const reconciled = within(region()).getByText(RECONCILED_LINE);
    const notAllocated = within(region()).getByText(NOT_ALLOCATED_LINE);
    expect(reconciled.textContent).toBe(RECONCILED_LINE);
    expect(notAllocated.textContent).toBe(NOT_ALLOCATED_LINE);
    // 两行都在合计块下方，不在任何 td 里。
    expect(reconciled.closest("td")).toBeNull();
    expect(notAllocated.closest("td")).toBeNull();
    expect(within(region()).getByText(PROVIDER_COST_NOTE).closest("td")).toBeNull();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("says provider costs are estimated and has no second line when nothing is reconciled", () => {
    render(<UsageAnalyticsTotals totals={totals()} />);

    expect(within(region()).getByText(NONE_LINE).textContent).toBe(NONE_LINE);
    expect(within(region()).queryByText(/^Not allocated/)).not.toBeInTheDocument();
    expect(within(region()).queryByText(/^Reconciled months \(UTC\): anthropic/)).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("stays a description list, not a data table, with the new items", () => {
    render(
      <UsageAnalyticsTotals
        totals={totals({
          reconciliation_coverage: COVERAGE,
          unallocated_reconciled_months: ["anthropic:2027-01", "anthropic:2027-02"],
          unallocated_reconciled_cost_myr: "7.50000000",
        })}
      />,
    );

    expect(region().querySelector("thead")).toBeNull();
    expect(region().querySelector("[data-row-key]")).toBeNull();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
