/**
 * 金额展示：只按字符串处理，不舍入、不截断（INV-10）。
 *
 * 每条断言都是「显示值去掉币种与逗号之后，与后端字符串在数值上相等」的具体例子。
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { MoneyText, formatAmount, formatMoney } from "./MoneyText";

describe("formatAmount", () => {
  it("shows a zero balance with two decimals", () => {
    expect(formatAmount("0.00000000")).toBe("0.00");
  });

  it("keeps the sign of a negative balance", () => {
    expect(formatAmount("-5.00000000")).toBe("-5.00");
  });

  it("groups thousands and keeps every significant decimal", () => {
    // ⚠️ 走一遍浮点的话，这个值在第 8 位上已经不是它自己了。
    expect(formatAmount("1234567.12345678")).toBe("1,234,567.12345678");
  });

  it("drops only the trailing zeros beyond two decimals", () => {
    expect(formatAmount("12.50000000")).toBe("12.50");
    expect(formatAmount("12.34500000")).toBe("12.345");
    expect(formatAmount("0.00000001")).toBe("0.00000001");
  });

  it("does not round values that a float cannot hold exactly", () => {
    expect(formatAmount("999999999999.99999999")).toBe("999,999,999,999.99999999");
    expect(formatAmount("0.10000001")).toBe("0.10000001");
  });

  it("does not print a negative zero", () => {
    expect(formatAmount("-0.00000000")).toBe("0.00");
  });

  it("leaves anything that is not a decimal string untouched", () => {
    expect(formatAmount("1e3")).toBe("1e3");
    expect(formatAmount("")).toBe("");
  });
});

describe("MoneyText", () => {
  it("puts the currency in front of the amount", () => {
    expect(formatMoney("-5.00000000", "MYR")).toBe("MYR -5.00");

    render(<MoneyText amount="1234567.12345678" currency="MYR" />);

    expect(screen.getByText("MYR 1,234,567.12345678")).toBeInTheDocument();
  });
});
