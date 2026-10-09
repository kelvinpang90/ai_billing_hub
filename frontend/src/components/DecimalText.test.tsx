/**
 * 不带币种的十进制数：只按字符串处理，不舍入、不截断（INV-10）。
 *
 * 每条断言都是「显示值去掉逗号之后，与后端字符串在数值上相等」的具体例子；挑的值都是走一遍
 * 浮点数就会变样的。
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { DecimalText, formatDecimal, isZeroDecimal, normalizeDecimal } from "./DecimalText";

describe("normalizeDecimal", () => {
  it("drops leading and trailing zeros and a bare decimal point", () => {
    expect(normalizeDecimal("1000000.00000000")).toBe("1000000");
    expect(normalizeDecimal("007.50")).toBe("7.5");
    expect(normalizeDecimal("0.5")).toBe("0.5");
    expect(normalizeDecimal("000")).toBe("0");
  });

  it("gives equal values the same spelling, without going through a float", () => {
    expect(normalizeDecimal("2.5")).toBe(normalizeDecimal("2.5000000000"));
    expect(normalizeDecimal("1000000")).toBe(normalizeDecimal("1000000.0"));
    // 这两个在双精度浮点里是同一个数；作为十进制它们不相等。
    expect(normalizeDecimal("0.10000000000000001")).not.toBe(normalizeDecimal("0.1"));
  });

  it("does not keep the sign of a zero", () => {
    expect(normalizeDecimal("-0.000")).toBe("0");
    expect(normalizeDecimal("-1.10")).toBe("-1.1");
  });

  it("refuses anything that is not a decimal string", () => {
    expect(normalizeDecimal("1e3")).toBeNull();
    expect(normalizeDecimal("+1")).toBeNull();
    expect(normalizeDecimal("1,000")).toBeNull();
    expect(normalizeDecimal(" 1")).toBeNull();
    expect(normalizeDecimal("")).toBeNull();
  });
});

describe("isZeroDecimal", () => {
  it("is true only for a decimal string whose value is zero", () => {
    expect(isZeroDecimal("0")).toBe(true);
    expect(isZeroDecimal("0.00000000")).toBe(true);
    expect(isZeroDecimal("0.00000001")).toBe(false);
    expect(isZeroDecimal("abc")).toBe(false);
  });
});

describe("formatDecimal", () => {
  it("groups thousands and shows no needless zeros", () => {
    expect(formatDecimal("1000000.00000000")).toBe("1,000,000");
    expect(formatDecimal("1.11111111")).toBe("1.11111111");
    expect(formatDecimal("4.083")).toBe("4.083");
  });

  it("keeps every significant decimal that a float would lose", () => {
    expect(formatDecimal("999999999999.99999999")).toBe("999,999,999,999.99999999");
    expect(formatDecimal("12345678901234.1234567891")).toBe("12,345,678,901,234.1234567891");
    expect(formatDecimal("0.1000000001")).toBe("0.1000000001");
  });

  it("leaves anything that is not a decimal string untouched", () => {
    expect(formatDecimal("1e3")).toBe("1e3");
    expect(formatDecimal("")).toBe("");
  });

  it("renders the formatted string as it is", () => {
    render(<DecimalText value="1234567.12345678" />);

    expect(screen.getByText("1,234,567.12345678")).toBeInTheDocument();
  });
});
