/**
 * 后端的不带时区 UTC → Asia/Kuala_Lumpur（UTC+8，无夏令时）。
 *
 * ⚠️ 这些用例在任何时区的机器上都必须给出同一个结果。「当成本地时间解析」的
 * 写法在 UTC 的 CI 上恰好全对，所以这里的期望值都是**跨了日期**或**跨了年**的
 * 那种 —— 解析错了必然有一条红。
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { DateTimeText, formatUtcDateTime } from "./DateTimeText";

describe("formatUtcDateTime", () => {
  it("adds eight hours for Kuala Lumpur", () => {
    expect(formatUtcDateTime("2026-09-20T08:30:00")).toBe("2026-09-20 16:30:00");
  });

  it("rolls over into the next day", () => {
    expect(formatUtcDateTime("2026-09-20T20:15:05")).toBe("2026-09-21 04:15:05");
  });

  it("rolls over into the next year", () => {
    expect(formatUtcDateTime("2026-12-31T16:00:00")).toBe("2027-01-01 00:00:00");
  });

  it("writes midnight as 00, not 24", () => {
    expect(formatUtcDateTime("2026-09-20T16:00:00")).toBe("2026-09-21 00:00:00");
  });

  it("ignores fractional seconds", () => {
    expect(formatUtcDateTime("2026-09-20T08:30:00.999999")).toBe("2026-09-20 16:30:00");
  });

  it("rejects values outside the contract instead of guessing a zone", () => {
    expect(formatUtcDateTime("2026-09-20T08:30:00+08:00")).toBeNull();
    expect(formatUtcDateTime("2026-02-30T08:30:00")).toBeNull();
    expect(formatUtcDateTime("yesterday")).toBeNull();
  });
});

describe("DateTimeText", () => {
  it("renders the Kuala Lumpur time and keeps the UTC instant machine-readable", () => {
    render(<DateTimeText value="2026-09-20T08:30:00" />);

    const shown = screen.getByText("2026-09-20 16:30:00");
    expect(shown).toHaveAttribute("datetime","2026-09-20T08:30:00Z");
  });

  it("shows a malformed value as it came", () => {
    render(<DateTimeText value="not a time" />);

    expect(screen.getByText("not a time")).toBeInTheDocument();
  });
});
