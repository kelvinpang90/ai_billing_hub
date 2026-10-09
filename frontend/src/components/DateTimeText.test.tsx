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
import {
  DateTimeText,
  displayTimeToRfc3339,
  displayTimeToUtc,
  formatDateTime,
  parseUtc,
  utcToDisplayInput,
} from "./DateTimeText";

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

describe("displayTimeToUtc", () => {
  it("shifts a Kuala Lumpur time back to naive UTC with seconds", () => {
    expect(displayTimeToUtc("2026-09-20T16:30")).toBe("2026-09-20T08:30:00");
    expect(displayTimeToUtc("2026-09-20T16:30:05")).toBe("2026-09-20T08:30:05");
  });

  it("rolls back to the previous day when Kuala Lumpur is just past midnight", () => {
    expect(displayTimeToUtc("2026-09-21T00:00")).toBe("2026-09-20T16:00:00");
    expect(displayTimeToUtc("2026-09-21T07:59:59")).toBe("2026-09-20T23:59:59");
    // 跨年、跨月也一样。
    expect(displayTimeToUtc("2027-01-01T04:15:05")).toBe("2026-12-31T20:15:05");
    expect(displayTimeToUtc("2026-03-01T03:00")).toBe("2026-02-28T19:00:00");
  });

  it("stays on the same day from 08:00 Kuala Lumpur time onwards", () => {
    expect(displayTimeToUtc("2026-09-21T08:00")).toBe("2026-09-21T00:00:00");
  });

  it("accepts the milliseconds a normalised datetime-local value carries and drops them", () => {
    expect(displayTimeToUtc("2026-09-22T07:59:59.000")).toBe("2026-09-21T23:59:59");
    expect(displayTimeToUtc("2026-09-20T16:30:05.5")).toBe("2026-09-20T08:30:05");
    expect(displayTimeToUtc("2026-09-20T16:30.000")).toBeNull();
  });

  it("is the exact inverse of formatDateTime", () => {
    for (const utc of ["2026-09-20T08:30:00", "2026-09-20T16:00:00", "2026-12-31T20:15:05"]) {
      const shown = formatDateTime(utc).replace(" ", "T");
      expect(displayTimeToUtc(shown)).toBe(utc);
    }
  });

  it("refuses anything that is not a real wall-clock time instead of rolling it over", () => {
    expect(displayTimeToUtc("2026-02-30T00:00")).toBeNull();
    expect(displayTimeToUtc("2026-09-20T24:00")).toBeNull();
    expect(displayTimeToUtc("2026-09-20T08:60")).toBeNull();
    expect(displayTimeToUtc("2026-09-20")).toBeNull();
    expect(displayTimeToUtc("2026-09-20T08:30:00Z")).toBeNull();
    expect(displayTimeToUtc("2026-09-20 08:30:00")).toBeNull();
    expect(displayTimeToUtc("")).toBeNull();
  });
});

describe("displayTimeToRfc3339", () => {
  it("turns a Kuala Lumpur time into RFC 3339 with a zone, on a whole second", () => {
    expect(displayTimeToRfc3339("2026-10-01T08:00")).toBe("2026-10-01T00:00:00Z");
    expect(displayTimeToRfc3339("2026-10-01T07:59:59.000")).toBe("2026-09-30T23:59:59Z");
    expect(displayTimeToRfc3339("2026-10-01T16:30:05.5")).toBe("2026-10-01T08:30:05Z");
  });

  it("refuses what displayTimeToUtc refuses", () => {
    expect(displayTimeToRfc3339("2026-02-30T00:00")).toBeNull();
    expect(displayTimeToRfc3339("")).toBeNull();
  });
});

describe("utcToDisplayInput", () => {
  it("turns naive UTC into the Kuala Lumpur value of a datetime-local input", () => {
    expect(utcToDisplayInput("2026-09-29T04:00:00")).toBe("2026-09-29T12:00:00");
    expect(utcToDisplayInput("2026-09-29T20:15:05")).toBe("2026-09-30T04:15:05");
  });

  it("round-trips through displayTimeToUtc", () => {
    expect(displayTimeToUtc(utcToDisplayInput("2026-12-31T20:15:05"))).toBe("2026-12-31T20:15:05");
  });

  it("gives an empty value for anything it cannot read", () => {
    expect(utcToDisplayInput("2026-09-29T04:00:00Z")).toBe("");
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
