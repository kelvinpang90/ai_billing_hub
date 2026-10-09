/**
 * 价格分量按计量类型成组：选一个计量类型就列出它的全部分量、只能整组移除、缺一个不许提交（与后端
 * 发布时的完整性规则一致）；旧草稿缺的分量列出来等人补，备注原样带回；数与单价原样是字符串。
 *
 * uuid 一律全零。输入一律 click + paste（逐字 `user.type` 在全量并行时太慢）。
 */

import { configure, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import "../../i18n";
import type { MeterType } from "../../api/adminCatalog";
import type { PriceComponent } from "../../api/adminProviderPrices";
import {
  ComponentRatesInput,
  ratesProblem,
  rowsForMeterType,
  rowsFromVersion,
  toComponentBodies,
  type RateRow,
} from "./ComponentRatesInput";

const ID = "00000000-0000-0000-0000-000000000000";
const AT = "2026-09-29T08:30:00";

function meterType(code: string, components: string[], overrides: Partial<MeterType> = {}): MeterType {
  return {
    id: ID,
    code,
    display_name: code,
    payload_shape: components.length > 1 ? "LLM_TOKEN_FIELDS" : "QUANTITY",
    unit: code === "LLM_TOKEN" ? "TOKEN" : "PAGE",
    quantity_kind: "INTEGER",
    status: "ACTIVE",
    components: components.map((component) => ({
      component_code: component,
      quantity_field: "quantity",
      created_at: AT,
    })),
    created_at: AT,
    updated_at: AT,
    ...overrides,
  };
}

const LLM_TOKEN = meterType("LLM_TOKEN", [
  "LLM_CACHE_READ_TOKEN",
  "LLM_CACHE_WRITE_TOKEN",
  "LLM_INPUT_TOKEN",
  "LLM_OUTPUT_TOKEN",
]);
const OCR_PAGE = meterType("OCR_PAGE", ["OCR_PAGE"]);
const OLD_TYPE = meterType("OLD_TYPE", ["OLD_TYPE"], { status: "RETIRED" });
const CATALOG = [LLM_TOKEN, OCR_PAGE, OLD_TYPE];

function priced(code: string, meter: string, rate: string, metadata: Record<string, unknown> | null = null): PriceComponent {
  return {
    component_code: code,
    meter_type_code: meter,
    unit: "TOKEN",
    unit_quantity: "1000000.00000000",
    rate_amount: rate,
    metadata,
    created_at: AT,
  };
}

function row(code: string, meter: string, quantity: string, rate: string): RateRow {
  return {
    component_code: code,
    meter_type_code: meter,
    unit: "TOKEN",
    unit_quantity: quantity,
    rate_amount: rate,
    metadata: null,
  };
}

describe("rows", () => {
  it("lists every component of a meter type, empty", () => {
    expect(rowsForMeterType(LLM_TOKEN).map((r) => [r.component_code, r.unit_quantity, r.rate_amount])).toEqual([
      ["LLM_CACHE_READ_TOKEN", "", ""],
      ["LLM_CACHE_WRITE_TOKEN", "", ""],
      ["LLM_INPUT_TOKEN", "", ""],
      ["LLM_OUTPUT_TOKEN", "", ""],
    ]);
  });

  it("fills a draft's components in and lists the ones it is missing", () => {
    const rows = rowsFromVersion(
      [
        priced("LLM_INPUT_TOKEN", "LLM_TOKEN", "1.11111111", { tier: "standard" }),
        priced("LLM_OUTPUT_TOKEN", "LLM_TOKEN", "2.22222222"),
      ],
      CATALOG,
    );

    expect(rows.map((r) => [r.component_code, r.rate_amount])).toEqual([
      ["LLM_CACHE_READ_TOKEN", ""],
      ["LLM_CACHE_WRITE_TOKEN", ""],
      ["LLM_INPUT_TOKEN", "1.11111111"],
      ["LLM_OUTPUT_TOKEN", "2.22222222"],
    ]);
    // 备注原样留着，提交时带回去。
    expect(rows[2]?.metadata).toEqual({ tier: "standard" });
  });

  it("keeps a draft's components as they are when their meter type is not in the catalog", () => {
    const rows = rowsFromVersion([priced("GONE", "GONE_TYPE", "3.00000000")], CATALOG);

    expect(rows.map((r) => r.component_code)).toEqual(["GONE"]);
  });
});

describe("ratesProblem", () => {
  const complete = rowsForMeterType(LLM_TOKEN).map((r) => ({ ...r, unit_quantity: "1000000", rate_amount: "0.10000001" }));

  it("accepts a complete group with valid amounts", () => {
    expect(ratesProblem(complete, CATALOG)).toBeNull();
  });

  it("refuses an empty list", () => {
    expect(ratesProblem([], CATALOG)).toEqual({ kind: "empty" });
  });

  it("names the components a chosen meter type is missing, like the backend's publish check", () => {
    expect(ratesProblem(complete.slice(1), CATALOG)).toEqual({ kind: "missing", codes: ["LLM_CACHE_READ_TOKEN"] });
  });

  it("refuses an amount the backend would refuse, without turning it into a number", () => {
    for (const bad of ["", "0", "0.00000000", "-1", "1e3", "1.123456789", "1,000"]) {
      const rows = complete.map((r, index) => (index === 0 ? { ...r, rate_amount: bad } : r));
      expect(ratesProblem(rows, CATALOG)).toEqual({ kind: "invalid" });
    }
  });

  it("refuses more than 64 components", () => {
    const many = Array.from({ length: 65 }, (_, index) => row(`C_${String(index)}`, "GONE_TYPE", "1", "1"));
    expect(ratesProblem(many, CATALOG)).toEqual({ kind: "tooMany" });
  });
});

describe("toComponentBodies", () => {
  it("sends the strings as typed and adds metadata only when there is some", () => {
    const bodies = toComponentBodies([
      row("LLM_INPUT_TOKEN", "LLM_TOKEN", "1000000", "999999999999.99999999"),
      { ...row("LLM_OUTPUT_TOKEN", "LLM_TOKEN", "1000000", "0.10000001"), metadata: { tier: "x" } },
    ]);

    expect(bodies).toEqual([
      { component_code: "LLM_INPUT_TOKEN", unit_quantity: "1000000", rate_amount: "999999999999.99999999" },
      { component_code: "LLM_OUTPUT_TOKEN", unit_quantity: "1000000", rate_amount: "0.10000001", metadata: { tier: "x" } },
    ]);
    expect(bodies.every((body) => typeof body.rate_amount === "string")).toBe(true);
  });
});

// antd 的下拉与表格在 jsdom 里渲染慢，全量并行时默认的时限不够（同目录页的用例）。
const SLOW = { timeout: 15_000 };
configure({ asyncUtilTimeout: 5_000 });

function Harness({ initial, onRows }: { initial: RateRow[]; onRows: (rows: RateRow[]) => void }) {
  const [rows, setRows] = useState(initial);
  return (
    <ComponentRatesInput
      value={rows}
      meterTypes={CATALOG}
      onChange={(next) => {
        setRows(next);
        onRows(next);
      }}
    />
  );
}

describe("ComponentRatesInput", SLOW, () => {
  it("lists all four LLM_TOKEN components as soon as the meter type is chosen", async () => {
    const onRows = vi.fn();
    render(<Harness initial={[]} onRows={onRows} />);

    expect(screen.getByText("No meter types chosen yet.")).toBeInTheDocument();
    fireEvent.mouseDown(screen.getByLabelText("Add a meter type"));
    fireEvent.click(await screen.findByTitle("LLM_TOKEN"));

    expect(await screen.findByLabelText("Rate for LLM_CACHE_READ_TOKEN")).toBeInTheDocument();
    for (const code of ["LLM_CACHE_WRITE_TOKEN", "LLM_INPUT_TOKEN", "LLM_OUTPUT_TOKEN"]) {
      expect(screen.getByLabelText(`Per quantity for ${code}`)).toBeInTheDocument();
      expect(screen.getByLabelText(`Rate for ${code}`)).toBeInTheDocument();
    }
    expect(onRows).toHaveBeenLastCalledWith(rowsForMeterType(LLM_TOKEN));
  });

  it("does not offer retired meter types or ones already chosen", async () => {
    render(<Harness initial={rowsForMeterType(OCR_PAGE)} onRows={vi.fn()} />);

    fireEvent.mouseDown(screen.getByLabelText("Add a meter type"));

    expect(await screen.findByTitle("LLM_TOKEN")).toBeInTheDocument();
    expect(screen.queryByTitle("OLD_TYPE")).not.toBeInTheDocument();
    expect(screen.queryByTitle("OCR_PAGE")).not.toBeInTheDocument();
  });

  it("keeps what is typed as the exact string", async () => {
    const user = userEvent.setup();
    const onRows = vi.fn();
    render(<Harness initial={rowsForMeterType(OCR_PAGE)} onRows={onRows} />);

    await user.click(screen.getByLabelText("Rate for OCR_PAGE"));
    await user.paste("0.10000001");

    expect(screen.getByLabelText("Rate for OCR_PAGE")).toHaveValue("0.10000001");
    const last = onRows.mock.lastCall?.[0] as RateRow[];
    expect(last[0]?.rate_amount).toBe("0.10000001");
  });

  it("removes a whole meter type at once, never a single component", async () => {
    const user = userEvent.setup();
    const onRows = vi.fn();
    render(<Harness initial={[...rowsForMeterType(LLM_TOKEN), ...rowsForMeterType(OCR_PAGE)]} onRows={onRows} />);

    await user.click(screen.getByRole("button", { name: "Remove LLM_TOKEN" }));

    await waitFor(() => expect(screen.queryByLabelText("Rate for LLM_INPUT_TOKEN")).not.toBeInTheDocument());
    expect(screen.getByLabelText("Rate for OCR_PAGE")).toBeInTheDocument();
    expect(onRows).toHaveBeenLastCalledWith(rowsForMeterType(OCR_PAGE));
  });
});
