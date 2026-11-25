/**
 * 新建 Rebill 页（AIH-TASK-260，设计闸门 #830 v12 §2「管理端页面」、§5「提交时或提交后连接中断」）。
 *
 * mock `api/adminRebills` 的预览与确认，钉住**页面交给它们的请求体**（两种 selection、吉隆坡时间换成带时区的 RFC 3339、更正版本
 * 空为 null、确认带预览指纹、去首尾空白的原因与小写幂等键）。地址与字段的拼法由 `api/adminRebills.test.ts` 管。
 *
 * 确认的几条线：第一次点 Confirm 才生成幂等键，之后表单（含 Reason）只读；结果不明（四个码）只给 Retry（原样重发同一请求体）
 * 与 Start over；409 REBILL_PREVIEW_STALE 与其他明确拒绝清空预览与幂等键、解锁表单；成功跳详情。确认之前改 Reason 不作废预览，
 * 改其他字段作废。停复机提示的边界（预估恰为 0）用字符串定点比较。
 *
 * 页面只经 `api/adminRebills`（这里已 mock）发请求，不问任何 `/api/v1/customer/` 路径。uuid 一律是占位值；别的 id 只用来区分，
 * 页面不解析它们。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { configure, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useParams } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";

import "../../i18n";
import type { RebillConfirmation, RebillPreview } from "../../api/adminRebills";
import { ApiError } from "../../api/client";
import { ROUTES, newRebillPath } from "../../routes/paths";
import { NewRebillPage, balanceTransition, parseEventIds, pickerToRfc3339, toFixedPoint } from "./NewRebillPage";

const CUSTOMER = "00000000-0000-0000-0000-000000000001";
const EVENT = "00000000-0000-0000-0000-000000000002";
const OTHER_EVENT = "00000000-0000-0000-0000-000000000003";
const TOKEN = "a".repeat(64);
/** `randomUUID` 本来就回小写；这里故意给大写，钉住页面再转一次小写。 */
const KEY = "0A1B2C3D-0000-4000-8000-00000000000A";
const SECOND_KEY = "0A1B2C3D-0000-4000-8000-00000000000B";

const api = vi.hoisted(() => ({
  previewRebill: vi.fn(),
  confirmRebill: vi.fn(),
}));

vi.mock("../../api/adminRebills", async () => {
  const actual = await vi.importActual<typeof import("../../api/adminRebills")>("../../api/adminRebills");
  return { ...actual, ...api };
});

function previewResult(overrides: Partial<RebillPreview> = {}): RebillPreview {
  return {
    lines: [
      {
        usage_event_id: EVENT,
        occurred_at: "2026-11-02T01:00:00",
        project: null,
        provider: "anthropic",
        model: "claude-x",
        previous_charge: "1.25000000",
        // 经过一次浮点数就不是它了。
        corrected_charge: "1.35000001",
        difference: "0.10000001",
        previous_pricing_rule: "rule-1",
        corrected_pricing_rule: "rule-2",
        previous_provider_price_version: "price-1",
        corrected_provider_price_version: "price-1",
        corrected_estimated_provider_cost_myr: "0.50000000",
        original_period: "2026-11",
        original_statement_id: "statement-11",
      },
    ],
    unchanged_count: 7,
    event_count: 1,
    previous_total: "1.25000000",
    corrected_total: "1.35000001",
    difference: "0.10000001",
    ledger_type: "REBILL_DEBIT",
    ledger_amount: "-0.10000001",
    balance: "10.00000000",
    balance_after: "9.89999999",
    preview_token: TOKEN,
    ...overrides,
  };
}

function confirmation(overrides: Partial<RebillConfirmation> = {}): RebillConfirmation {
  return {
    id: "rebill-1",
    customer_id: CUSTOMER,
    event_count: 1,
    difference: "0.10000001",
    ledger_type: "REBILL_DEBIT",
    ledger_amount: "-0.10000001",
    reason: "Price fix",
    created_by_email: "admin@example.com",
    created_at: "2026-11-20T03:00:00",
    previous_total: "1.25000000",
    corrected_total: "1.35000001",
    corrective_pricing_rule_id: null,
    corrective_provider_price_version_id: null,
    wallet_transaction_id: "ledger-1",
    lines: [],
    replayed: false,
    ...overrides,
  };
}

/** 详情页的替身：只把路径里的 id 显示出来，证明跳到了哪一条。 */
function DetailProbe() {
  const { rebillId = "" } = useParams();
  return <p data-testid="rebill-detail">{rebillId}</p>;
}

function renderPage(entry: string = ROUTES.rebillCreate) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[entry]}>
        <Routes>
          <Route path={ROUTES.rebillCreate} element={<NewRebillPage />} />
          <Route path={ROUTES.rebillDetail} element={<DetailProbe />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function buttonWithText(text: string, container: HTMLElement = document.body): HTMLElement {
  const button = within(container).getByText(text).closest("button");
  if (button === null) {
    throw new Error(`"${text}" is not inside a button`);
  }
  return button;
}

function fill(label: string, value: string): void {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}

/** antd DatePicker（showTime）：在输入框里键入完整的时刻再回车提交。 */
function pick(label: string, value: string): void {
  const input = screen.getByLabelText(label);
  fireEvent.mouseDown(input);
  fireEvent.focus(input);
  fireEvent.change(input, { target: { value } });
  fireEvent.keyDown(input, { key: "Enter", code: "Enter", keyCode: 13 });
  fireEvent.blur(input);
}

/** 某个字段（Descriptions 的一行）的值格。 */
function field(label: string): HTMLElement {
  const th = screen
    .getAllByText(label)
    .map((el) => el.closest("th"))
    .find((el) => el !== null);
  const td = th?.nextElementSibling;
  if (!(td instanceof HTMLElement)) {
    throw new Error(`no field "${label}"`);
  }
  return td;
}

const previewRegion = () => screen.queryByRole("region", { name: "Rebill preview" });

/** 经查询参数预填客户与一个事件，点 Preview，等预览出来。 */
async function previewPrefilled(result: RebillPreview = previewResult()): Promise<void> {
  api.previewRebill.mockResolvedValue(result);
  renderPage(newRebillPath(CUSTOMER, EVENT));
  fireEvent.click(buttonWithText("Preview"));
  await screen.findByRole("region", { name: "Rebill preview" });
}

/** 预览已出来时填原因并点 Confirm。 */
function confirmWith(reason: string): void {
  fill("Reason", reason);
  fireEvent.click(buttonWithText("Confirm"));
}

const EXPECTED_BODY = {
  customer_id: CUSTOMER,
  selection: { usage_event_ids: [EVENT] },
  corrective_pricing_rule_id: null,
  corrective_provider_price_version_id: null,
  preview_token: TOKEN,
  reason: "Price fix",
  idempotency_key: KEY.toLowerCase(),
};

let randomUUID: MockInstance<typeof crypto.randomUUID>;

beforeEach(() => {
  vi.clearAllMocks();
  randomUUID = vi
    .spyOn(crypto, "randomUUID")
    .mockReturnValueOnce(KEY)
    .mockReturnValueOnce(SECOND_KEY);
});

afterEach(() => {
  randomUUID.mockRestore();
});

const SLOW = { timeout: 20_000 };
configure({ asyncUtilTimeout: 5_000 });

describe("rebill helpers", () => {
  it("compares balances as fixed-point strings, with exactly zero counting as suspended", () => {
    expect(toFixedPoint("0.10000001")).toBe(10_000_001n);
    expect(toFixedPoint("-12.5")).toBe(-1_250_000_000n);
    expect(toFixedPoint("1.123456789")).toBeNull();
    expect(toFixedPoint("1e3")).toBeNull();

    expect(balanceTransition("0.50000000", "0.00000000")).toBe("suspend");
    expect(balanceTransition("0.00000001", "-0.00000001")).toBe("suspend");
    expect(balanceTransition("0.50000000", "0.00000001")).toBeNull();
    expect(balanceTransition("0.00000000", "0.00000001")).toBe("resume");
    expect(balanceTransition("-1.00000000", "0.00000000")).toBeNull();
    expect(balanceTransition("0.00000000", "0.00000000")).toBeNull();
    expect(balanceTransition("10.00000000", "9.89999999")).toBeNull();
  });

  it("turns Kuala Lumpur wall time into an RFC 3339 instant with a time zone", () => {
    const wall = (text: string) => ({ format: () => text }) as unknown as Parameters<typeof pickerToRfc3339>[0];
    expect(pickerToRfc3339(wall("2026-11-01T08:00:00"))).toBe("2026-11-01T00:00:00Z");
    // 吉隆坡的凌晨在 UTC 还是前一天。
    expect(pickerToRfc3339(wall("2026-11-01T00:30:15"))).toBe("2026-10-31T16:30:15Z");
    expect(pickerToRfc3339(null)).toBeNull();
  });

  it("reads one event ID per line, dropping blank lines and duplicates", () => {
    expect(parseEventIds(` ${EVENT} \n\n${OTHER_EVENT}\r\n${EVENT}\n`)).toEqual([EVENT, OTHER_EVENT]);
  });
});

describe("NewRebillPage form", SLOW, () => {
  it("prefills the customer and the usage event from the query parameters", () => {
    renderPage(newRebillPath(CUSTOMER, EVENT));

    expect(screen.getByLabelText("Customer")).toHaveValue(CUSTOMER);
    expect(screen.getByLabelText("Usage events")).toBeChecked();
    expect(screen.getByLabelText("Usage event IDs")).toHaveValue(EVENT);
    expect(buttonWithText("Preview")).toBeEnabled();
  });

  it("keeps Preview disabled until the customer and the selection are complete", () => {
    renderPage();

    expect(buttonWithText("Preview")).toBeDisabled();
    fill("Customer", CUSTOMER);
    expect(buttonWithText("Preview")).toBeDisabled();
    fill("Usage event IDs", "  \n ");
    expect(buttonWithText("Preview")).toBeDisabled();
    fill("Usage event IDs", EVENT);
    expect(buttonWithText("Preview")).toBeEnabled();

    fireEvent.click(screen.getByLabelText("Date range"));
    expect(buttonWithText("Preview")).toBeDisabled();
  });

  it("says it recalculates with the current versions only while both corrective versions are empty", () => {
    renderPage();
    const hint =
      "Recalculate with the current versions: each event is repriced with the pricing rule and provider price version it is currently billed with.";

    expect(screen.getByText(hint)).toBeInTheDocument();
    fill("Corrective pricing rule", "rule-2");
    expect(screen.queryByText(hint)).not.toBeInTheDocument();
    fill("Corrective pricing rule", " ");
    fill("Corrective provider price version", "price-2");
    expect(screen.queryByText(hint)).not.toBeInTheDocument();
  });

  it("previews an explicit list of usage events with the corrective versions", async () => {
    api.previewRebill.mockResolvedValue(previewResult());
    renderPage();

    fill("Customer", `  ${CUSTOMER} `);
    fill("Usage event IDs", `${EVENT}\n\n ${OTHER_EVENT} \n${EVENT}`);
    fill("Corrective pricing rule", " rule-2 ");
    fireEvent.click(buttonWithText("Preview"));

    await screen.findByRole("region", { name: "Rebill preview" });
    expect(api.previewRebill).toHaveBeenCalledTimes(1);
    expect(api.previewRebill).toHaveBeenCalledWith({
      customer_id: CUSTOMER,
      selection: { usage_event_ids: [EVENT, OTHER_EVENT] },
      corrective_pricing_rule_id: "rule-2",
      corrective_provider_price_version_id: null,
    });
  });

  it("previews a date range entered in Kuala Lumpur time as RFC 3339 instants, [From, To)", async () => {
    api.previewRebill.mockResolvedValue(previewResult());
    renderPage();

    fill("Customer", CUSTOMER);
    fireEvent.click(screen.getByLabelText("Date range"));
    expect(
      screen.getByText("Not included: events that occurred at exactly this moment are not selected."),
    ).toBeInTheDocument();
    expect(screen.getByText(/already past the T\+1 cut-off/)).toBeInTheDocument();
    pick("From (Kuala Lumpur time)", "2026-11-01 00:30:15");
    pick("To (Kuala Lumpur time)", "2026-11-15 08:00:00");
    fill("Provider (optional)", " anthropic ");

    await waitFor(() => expect(buttonWithText("Preview")).toBeEnabled());
    fireEvent.click(buttonWithText("Preview"));

    await screen.findByRole("region", { name: "Rebill preview" });
    expect(api.previewRebill).toHaveBeenCalledWith({
      customer_id: CUSTOMER,
      selection: {
        occurred_from: "2026-10-31T16:30:15Z",
        occurred_to: "2026-11-15T00:00:00Z",
        provider: "anthropic",
      },
      corrective_pricing_rule_id: null,
      corrective_provider_price_version_id: null,
    });
  });

  it("shows the preview lines, totals, ledger direction and balances exactly as sent", async () => {
    await previewPrefilled();

    const region = previewRegion();
    expect(region).not.toBeNull();
    expect(field("Events to correct")).toHaveTextContent("1");
    expect(field("Unchanged events")).toHaveTextContent("7");
    expect(field("Previous total (MYR)")).toHaveTextContent("1.25000000");
    expect(field("Corrected total (MYR)")).toHaveTextContent("1.35000001");
    expect(field("Difference (MYR)")).toHaveTextContent("0.10000001");
    expect(field("Ledger")).toHaveTextContent("Debit");
    expect(field("Ledger amount (MYR)")).toHaveTextContent("-0.10000001");
    expect(field("Current balance (MYR)")).toHaveTextContent("10.00000000");
    expect(field("Estimated balance after (MYR)")).toHaveTextContent("9.89999999");
    expect(screen.getByRole("link", { name: EVENT })).toHaveAttribute("href", `/usage-events/${EVENT}`);
    expect(screen.getByText("statement-11")).toBeInTheDocument();
    expect(screen.getByText("2026-11")).toBeInTheDocument();
    // 吉隆坡时间。
    expect(screen.getByText("2026-11-02 09:00:00")).toBeInTheDocument();
    expect(screen.queryByText(/the customer will be suspended/)).not.toBeInTheDocument();
    expect(screen.queryByText(/the customer will be resumed/)).not.toBeInTheDocument();
  });

  it("warns that the customer will be suspended when the estimated balance is exactly zero", async () => {
    await previewPrefilled(previewResult({ balance: "0.10000001", balance_after: "0.00000000" }));

    expect(
      screen.getByText("After confirming, the balance will be zero or below: the customer will be suspended."),
    ).toBeInTheDocument();
  });

  it("says the customer will be resumed when the balance goes above zero", async () => {
    await previewPrefilled(
      previewResult({
        difference: "-0.10000001",
        ledger_type: "REBILL_CREDIT",
        ledger_amount: "0.10000001",
        balance: "0.00000000",
        balance_after: "0.10000001",
      }),
    );

    expect(
      screen.getByText("After confirming, the balance will be above zero: the customer will be resumed."),
    ).toBeInTheDocument();
    expect(field("Ledger")).toHaveTextContent("Credit");
  });

  it("does not say resumed when a suspended customer only reaches exactly zero", async () => {
    await previewPrefilled(
      previewResult({ ledger_type: "REBILL_CREDIT", balance: "-1.00000000", balance_after: "0.00000000" }),
    );

    expect(screen.queryByText(/the customer will be resumed/)).not.toBeInTheDocument();
    expect(screen.queryByText(/the customer will be suspended/)).not.toBeInTheDocument();
  });

  it("shows the code-specific text when the preview is refused", async () => {
    api.previewRebill.mockRejectedValue(
      new ApiError("REBILL_PRICE_NOT_APPLICABLE", `Not applicable to usage event ${EVENT}.`, "req-422"),
    );
    renderPage(newRebillPath(CUSTOMER, EVENT));
    fireEvent.click(buttonWithText("Preview"));

    expect(
      await screen.findByText("The corrective provider price version does not apply to a selected usage event."),
    ).toBeInTheDocument();
    expect(screen.getByText(new RegExp(`Not applicable to usage event ${EVENT}`))).toBeInTheDocument();
    expect(screen.getByText("req-422")).toBeInTheDocument();
    expect(previewRegion()).toBeNull();
  });
});

describe("NewRebillPage before confirming", SLOW, () => {
  it("keeps Confirm disabled while the reason is empty", async () => {
    await previewPrefilled();

    expect(buttonWithText("Confirm")).toBeDisabled();
    fill("Reason", "   ");
    expect(buttonWithText("Confirm")).toBeDisabled();
    fill("Reason", "Price fix");
    expect(buttonWithText("Confirm")).toBeEnabled();
  });

  it("disables Confirm when the preview has nothing to post", async () => {
    await previewPrefilled(
      previewResult({ lines: [], event_count: 0, ledger_type: null, ledger_amount: null, balance_after: "10.00000000" }),
    );

    expect(screen.getByText("Nothing to post: this preview has no difference to put in the ledger.")).toBeInTheDocument();
    fill("Reason", "Price fix");
    expect(buttonWithText("Confirm")).toBeDisabled();
  });

  it("keeps the preview when only the reason changes", async () => {
    await previewPrefilled();

    fill("Reason", "Price fix");
    fill("Reason", "Price fix, take two");

    expect(previewRegion()).not.toBeNull();
    expect(buttonWithText("Confirm")).toBeEnabled();
  });

  it.each([
    ["Customer", "00000000-0000-0000-0000-000000000009"],
    ["Usage event IDs", OTHER_EVENT],
    ["Corrective pricing rule", "rule-2"],
    ["Corrective provider price version", "price-2"],
  ])("drops the preview when %s changes", async (label, value) => {
    await previewPrefilled();
    fill("Reason", "Price fix");

    fill(label, value);

    expect(previewRegion()).toBeNull();
    expect(screen.queryByText("Confirm")).not.toBeInTheDocument();
    expect(api.confirmRebill).not.toHaveBeenCalled();
  });

  it("drops the preview when the selection kind changes", async () => {
    await previewPrefilled();

    fireEvent.click(screen.getByLabelText("Date range"));

    expect(previewRegion()).toBeNull();
  });
});

describe("NewRebillPage confirming", SLOW, () => {
  it("generates the idempotency key on the first Confirm and locks the whole form", async () => {
    api.confirmRebill.mockReturnValue(new Promise(() => undefined));
    await previewPrefilled();
    expect(randomUUID).not.toHaveBeenCalled();

    confirmWith("  Price fix  ");

    await waitFor(() => expect(api.confirmRebill).toHaveBeenCalledTimes(1));
    expect(api.confirmRebill).toHaveBeenCalledWith(EXPECTED_BODY);
    expect(randomUUID).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText("Customer")).toBeDisabled();
    expect(screen.getByLabelText("Usage event IDs")).toBeDisabled();
    expect(screen.getByLabelText("Date range")).toBeDisabled();
    expect(screen.getByLabelText("Corrective pricing rule")).toBeDisabled();
    expect(screen.getByLabelText("Corrective provider price version")).toBeDisabled();
    expect(screen.getByLabelText("Reason")).toBeDisabled();
    expect(buttonWithText("Preview")).toBeDisabled();
    expect(buttonWithText("Confirm")).toBeDisabled();

    // 双击也只发一次。
    fireEvent.click(buttonWithText("Confirm"));
    expect(api.confirmRebill).toHaveBeenCalledTimes(1);
  });

  it("goes to the rebill detail page after 201", async () => {
    api.confirmRebill.mockResolvedValue(confirmation({ id: "rebill-201" }));
    await previewPrefilled();

    confirmWith("Price fix");

    expect(await screen.findByTestId("rebill-detail")).toHaveTextContent("rebill-201");
  });

  it("goes to the rebill detail page after a 200 replay", async () => {
    api.confirmRebill.mockResolvedValue(confirmation({ id: "rebill-200", replayed: true }));
    await previewPrefilled();

    confirmWith("Price fix");

    expect(await screen.findByTestId("rebill-detail")).toHaveTextContent("rebill-200");
  });

  it.each(["NETWORK_ERROR", "MALFORMED_RESPONSE", "INTERNAL_ERROR", "DATABASE_UNAVAILABLE"])(
    "offers only Retry and Start over after %s, with the form still read-only",
    async (code) => {
      api.confirmRebill.mockRejectedValue(new ApiError(code, "Something went wrong.", "req-500"));
      await previewPrefilled();

      confirmWith("Price fix");

      expect(await screen.findByText(/The result is unknown/)).toBeInTheDocument();
      expect(screen.getByText("req-500")).toBeInTheDocument();
      expect(buttonWithText("Retry")).toBeEnabled();
      expect(buttonWithText("Start over")).toBeEnabled();
      expect(buttonWithText("Confirm")).toBeDisabled();
      expect(buttonWithText("Preview")).toBeDisabled();
      expect(screen.getByLabelText("Reason")).toBeDisabled();
      expect(screen.getByLabelText("Customer")).toBeDisabled();
    },
  );

  it("retries with exactly the same request body and the same idempotency key", async () => {
    api.confirmRebill
      .mockRejectedValueOnce(new ApiError("NETWORK_ERROR", "Could not reach the billing platform.", null))
      .mockResolvedValueOnce(confirmation({ id: "rebill-replayed", replayed: true }));
    await previewPrefilled();
    confirmWith("Price fix");
    await screen.findByText(/The result is unknown/);

    // 只读：想改原因也改不了。
    fill("Reason", "Something else");
    fireEvent.click(buttonWithText("Retry"));

    expect(await screen.findByTestId("rebill-detail")).toHaveTextContent("rebill-replayed");
    expect(api.confirmRebill).toHaveBeenCalledTimes(2);
    expect(api.confirmRebill).toHaveBeenNthCalledWith(1, EXPECTED_BODY);
    expect(api.confirmRebill).toHaveBeenNthCalledWith(2, EXPECTED_BODY);
    expect(randomUUID).toHaveBeenCalledTimes(1);
  });

  it("starts over: clears the preview and the key, and requires a new preview before a new key", async () => {
    api.confirmRebill
      .mockRejectedValueOnce(new ApiError("INTERNAL_ERROR", "An unexpected error occurred.", "req-500"))
      .mockReturnValueOnce(new Promise(() => undefined));
    await previewPrefilled();
    confirmWith("Price fix");
    await screen.findByText(/The result is unknown/);

    fireEvent.click(buttonWithText("Start over"));

    expect(previewRegion()).toBeNull();
    expect(screen.getByText(/The unfinished confirmation was abandoned\. Preview again before confirming/)).toBeInTheDocument();
    expect(screen.getByLabelText("Customer")).toBeEnabled();
    expect(buttonWithText("Preview")).toBeEnabled();

    fireEvent.click(buttonWithText("Preview"));
    await screen.findByRole("region", { name: "Rebill preview" });
    confirmWith("Price fix");

    await waitFor(() => expect(api.confirmRebill).toHaveBeenCalledTimes(2));
    expect(api.confirmRebill).toHaveBeenNthCalledWith(2, { ...EXPECTED_BODY, idempotency_key: SECOND_KEY.toLowerCase() });
    expect(randomUUID).toHaveBeenCalledTimes(2);
    expect(api.previewRebill).toHaveBeenCalledTimes(2);
  });

  it("clears the preview and the key on REBILL_PREVIEW_STALE and asks for a new preview", async () => {
    api.confirmRebill
      .mockRejectedValueOnce(new ApiError("REBILL_PREVIEW_STALE", "The preview is stale.", "req-409"))
      .mockReturnValueOnce(new Promise(() => undefined));
    await previewPrefilled();
    confirmWith("Price fix");

    expect(
      await screen.findByText("The data changed after the preview. Preview again before confirming."),
    ).toBeInTheDocument();
    expect(previewRegion()).toBeNull();
    expect(screen.queryByText("Retry")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Customer")).toBeEnabled();

    fireEvent.click(buttonWithText("Preview"));
    await screen.findByRole("region", { name: "Rebill preview" });
    // 原因还在，重新预览之后可以直接确认；这一次是新的幂等键。
    expect(screen.getByLabelText("Reason")).toHaveValue("Price fix");
    fireEvent.click(buttonWithText("Confirm"));

    await waitFor(() => expect(api.confirmRebill).toHaveBeenCalledTimes(2));
    expect(api.confirmRebill).toHaveBeenNthCalledWith(2, { ...EXPECTED_BODY, idempotency_key: SECOND_KEY.toLowerCase() });
  });

  it.each([
    ["REBILL_NET_ZERO", "The credits and debits cancel out to zero. Split the events into two rebills if needed."],
    ["ACCOUNT_CLOSED", "The customer's account is closed. Nothing was written."],
    ["NOTHING_TO_REBILL", "Nothing to rebill: no usage event matches, or no event has a difference."],
    ["REBILL_CONFLICT", "This idempotency key was already used for a different request. Preview again."],
  ])("unlocks the form and shows the text for %s", async (code, text) => {
    api.confirmRebill.mockRejectedValue(new ApiError(code, "Refused.", "req-409"));
    await previewPrefilled();
    confirmWith("Price fix");

    expect(await screen.findByText(text)).toBeInTheDocument();
    expect(screen.getByText(code)).toBeInTheDocument();
    expect(screen.getByText("req-409")).toBeInTheDocument();
    expect(previewRegion()).toBeNull();
    expect(screen.queryByText("Retry")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Customer")).toBeEnabled();
    expect(buttonWithText("Preview")).toBeEnabled();
  });
});
