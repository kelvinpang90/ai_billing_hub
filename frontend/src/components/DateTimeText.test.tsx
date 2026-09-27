/**
 * 后端的不带时区 UTC → 吉隆坡时间（UTC+8，无夏令时）。
 *
 * ⚠️ 这些断言与跑测试那台机器的时区无关：没有显式补 `Z` 的实现会把串当成本地
 * 时间解析，只在时区不是 UTC 的机器上才错 —— 在 UTC 的机器上恰好看不出来。
 * 所以这里专门有一条跨日的用例：本地解析的话日期也会跟着错。
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import "../i18n";
import { DateTimeText, formatDateTime, parseUtc } from "./DateTimeText";

describe("formatDateTime", () => {
  it("shifts a UTC timestamp to Kuala Lumpur time", () => {
    expect(formatDateTime("2026-09-20T08:30:00")).toBe("2026-09-20 16:30:00");
  });

  it("rolls over to the next day when Kuala Lumpur is already past midnight", () => {
    expect(formatDateTime("2026-09-20T16:00:00")).toBe("2026-09-21 00:00:00");
    expect(formatDateTime("2026-12-31T20:15:05")).toBe("2027-01-01 04:15:05");
  });

  it("accepts fractional seconds", () => {
    expect(formatDateTime("2026-09-20T08:30:00.500")).toBe("2026-09-20 16:30:00");
  });

  it("does not guess at anything that is not a naive ISO timestamp", () => {
    expect(formatDateTime("yesterday")).toBe("yesterday");
    // 已经带时区的串不是后端的契约形状，不替它再补一个 Z。
    expect(parseUtc("2026-09-20T08:30:00Z")).toBeNull();
  });
});

describe("DateTimeText", () => {
  it("renders the local time and keeps the exact instant machine-readable", () => {
    render(<DateTimeText value="2026-09-20T08:30:00" />);

    const time = screen.getByText("2026-09-20 16:30:00");
    expect(time).toHaveAttribute("datetime", "2026-09-20T08:30:00.000Z");
    expect(time).toHaveAttribute("title", "Kuala Lumpur time (UTC+08:00)");
  });
});
