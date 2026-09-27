/**
 * 后端的 naive UTC → 吉隆坡时间（UTC+8，无夏令时）。
 *
 * ⚠️ 这些断言与跑测试那台机器的时区无关：换算不经过 `new Date(string)` 的本地
 * 时区解析。CI 在 UTC 上跑，要是实现偷用了本地时区，这里的 +8 小时就对不上。
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { DateTimeText, formatDateTime } from "./DateTimeText";

describe("DateTimeText", () => {
  it("shows backend UTC as Kuala Lumpur time", () => {
    render(<DateTimeText value="2026-09-20T08:30:00" />);

    const shown = screen.getByText("2026-09-20 16:30:00");
    // 机器可读的那一份仍是 UTC，并且显式带上了 Z。
    expect(shown).toHaveAttribute("datetime", "2026-09-20T08:30:00Z");
  });
});

describe("formatDateTime", () => {
  it("rolls over into the next day, month and year", () => {
    expect(formatDateTime("2026-12-31T16:00:00")).toBe("2027-01-01 00:00:00");
    expect(formatDateTime("2026-12-31T23:59:59")).toBe("2027-01-01 07:59:59");
  });

  it("keeps the day when the shift stays inside it", () => {
    expect(formatDateTime("2026-01-01T00:00:00")).toBe("2026-01-01 08:00:00");
  });

  it("ignores fractional seconds instead of rounding them", () => {
    expect(formatDateTime("2026-09-20T08:30:00.999")).toBe("2026-09-20 16:30:00");
  });

  it("leaves anything that is not a naive UTC timestamp untouched", () => {
    // 带时区的串不再换算一次，免得错上加错。
    expect(formatDateTime("2026-09-20T08:30:00Z")).toBe("2026-09-20T08:30:00Z");
    expect(formatDateTime("not a time")).toBe("not a time");
  });
});
