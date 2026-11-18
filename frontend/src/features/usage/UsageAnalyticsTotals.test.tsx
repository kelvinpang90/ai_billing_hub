/**
 * 用量分析的合计块（设计闸门 #709 v2 §7「前端合计块」「前端不经 number」）：各值原样显示后端字符串、null 是「—」；
 * 手续费未知时的提示带数量，手续费块为 null 时说明不显示毛利；两条提示都不是 role=alert；合计块不是数据表格
 * （Descriptions 内部的 table 元素允许，但没有 thead、没有带 data-row-key 的行 —— 验收脚本按这两样识别数据表格）。
 */

import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import "../../i18n";
import type { UsageAnalyticsPayments, UsageAnalyticsReportTotals } from "../../api/adminUsageAnalytics";
import { UsageAnalyticsTotals } from "./UsageAnalyticsTotals";

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
    },
    internal_metered_only: {
      event_count: 20,
      estimated_provider_cost_myr: "1.00000000",
      reference_customer_price: "2.00000000",
    },
    payments: payments(),
    gross_profit: "1.00000000",
    gross_profit_rate: "0.1",
    ...overrides,
  };
}

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
          },
          gross_profit: "-3.00000000",
          gross_profit_rate: null,
        })}
      />,
    );

    expect(valueOf("Usage revenue")).toBe("0.00000000");
    expect(valueOf("Gross profit")).toBe("-3.00000000");
    expect(valueOf("Gross margin")).toBe("—");
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
          },
          payments: payments({ gateway_fee: "0.03333333" }),
          gross_profit: "0.06666667",
          gross_profit_rate: "0.333333333333333333333333333",
        })}
      />,
    );

    expect(valueOf("Usage revenue")).toBe("0.10000001");
    expect(valueOf("Gross margin")).toBe("0.333333333333333333333333333");
    expect(valueOf("Gateway fees")).toBe("0.03333333");
    expect(valueOf("Gross profit")).toBe("0.06666667");
  });
});
