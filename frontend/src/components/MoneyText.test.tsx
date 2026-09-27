/**
 * 金额展示（INV-10）。
 *
 * 这里钉住的是「展示值与后端字符串在数值上完全相等」：去掉的只有末尾多余的 0，
 * 不舍入、不截断，也不经过浮点数。
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { MoneyText, formatMoney } from "./MoneyText";

describe("formatMoney", () => {
  it("shows a zero balance with two decimals", () => {
    expect(formatMoney("0.00000000")).toBe("0.00");
  });

  it("keeps the sign of a negative amount", () => {
    expect(formatMoney("-5.00000000")).toBe("-5.00");
  });

  it("groups thousands and keeps every significant decimal", () => {
    // 转成浮点数的话这里已经丢精度了：1234567.12345678 不能精确表示。
    expect(formatMoney("1234567.12345678")).toBe("1,234,567.12345678");
  });

  it("trims only the trailing zeros", () => {
    expect(formatMoney("12.50000000")).toBe("12.50");
    expect(formatMoney("0.10000001")).toBe("0.10000001");
    expect(formatMoney("1000.00000000")).toBe("1,000.00");
  });

  it("does not round digits beyond what a float could hold", () => {
    expect(formatMoney("999999999999.99999999")).toBe("999,999,999,999.99999999");
  });

  it("never shows a negative zero", () => {
    expect(formatMoney("-0.00000000")).toBe("0.00");
  });

  it("refuses to guess at something that is not a decimal string", () => {
    expect(formatMoney("1e5")).toBeNull();
    expect(formatMoney("")).toBeNull();
    expect(formatMoney("NaN")).toBeNull();
  });
});

describe("MoneyText", () => {
  it.each([
    ["0.00000000", "MYR 0.00"],
    ["-5.00000000", "MYR -5.00"],
    ["1234567.12345678", "MYR 1,234,567.12345678"],
  ])("renders %s as %s", (amount, shown) => {
    render(<MoneyText amount={amount} currency="MYR" />);

    expect(screen.getByText(shown)).toBeInTheDocument();
  });

  it("shows the raw text rather than a guessed number when the amount is malformed", () => {
    render(<MoneyText amount="abc" currency="MYR" />);

    expect(screen.getByText("MYR abc")).toBeInTheDocument();
  });
});
