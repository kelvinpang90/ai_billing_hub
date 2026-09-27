/**
 * 金额展示：千分位、小数末尾去 0 但至少 2 位，不舍入、不截断（INV-10）。
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { MoneyText, formatDecimal } from "./MoneyText";

describe("MoneyText", () => {
  it("shows a zero balance as 0.00", () => {
    render(<MoneyText amount="0.00000000" currency="MYR" />);

    expect(screen.getByText("MYR 0.00")).toBeInTheDocument();
  });

  it("keeps the sign of a negative balance", () => {
    render(<MoneyText amount="-5.00000000" currency="MYR" />);

    expect(screen.getByText("MYR -5.00")).toBeInTheDocument();
  });

  it("groups thousands and keeps every significant decimal", () => {
    render(<MoneyText amount="1234567.12345678" currency="MYR" />);

    expect(screen.getByText("MYR 1,234,567.12345678")).toBeInTheDocument();
  });
});

describe("formatDecimal", () => {
  it.each([
    ["12.50000000", "12.50"],
    ["0.01000000", "0.01"],
    ["0.00000001", "0.00000001"],
    ["999.00000000", "999.00"],
    ["1000.00000000", "1,000.00"],
    ["-1234.56780000", "-1,234.5678"],
    ["-0.00000000", "0.00"],
  ])("formats %s as %s", (amount, shown) => {
    expect(formatDecimal(amount)).toBe(shown);
  });

  it("does not lose digits that a float would", () => {
    // 19 位有效数字：过一遍双精度会变成 …901.12345679 之类。
    expect(formatDecimal("12345678901.12345678")).toBe("12,345,678,901.12345678");
  });

  it("leaves anything that is not a plain decimal untouched", () => {
    expect(formatDecimal("1e3")).toBe("1e3");
    expect(formatDecimal("")).toBe("");
  });
});
