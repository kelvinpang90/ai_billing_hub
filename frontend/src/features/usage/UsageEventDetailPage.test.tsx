/**
 * 用量事件详情：三种状态、404 与无权限；快照与版本的链接、成本 / 计费额 / 毛利（estimated）与账本引用按字符串
 * 显示；认领信息与冲突记录；「重新入队」只在可重新入队时出现，二次确认、成功后重读、失败时显示后端码对应的
 * 文案与 request_id、提交中防重复。「Rebill」按钮、当前有效计费与更正历史只在已计费的预付事件（PROCESSED 且 effective_charge
 * 不为 null）上出现，按钮跳到预填客户与事件的新建页，历史的一行跳到 Rebill 详情（AIH-TASK-260）。
 *
 * 请求路径与字段名由 `api/adminUsageEvents.test.ts` 管。uuid 一律全零。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { configure, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import type { UsageEventDetail } from "../../api/adminUsageEvents";
import { ApiError } from "../../api/client";
import { ROUTES } from "../../routes/paths";
import { UsageEventDetailPage } from "./UsageEventDetailPage";

const ID = "00000000-0000-0000-0000-000000000000";
const AT = "2026-09-29T08:30:00";

const api = vi.hoisted(() => ({
  getUsageEvent: vi.fn(),
  requeueUsageEvent: vi.fn(),
}));

vi.mock("../../api/adminUsageEvents", async () => {
  const actual = await vi.importActual<typeof import("../../api/adminUsageEvents")>(
    "../../api/adminUsageEvents",
  );
  return { ...actual, ...api };
});

function detail(overrides: Partial<UsageEventDetail> = {}): UsageEventDetail {
  return {
    id: ID,
    event_id: ID,
    customer_id: ID,
    customer_company_name: "Fictional Sdn Bhd",
    project_id: ID,
    request_id: "call-1",
    conversation_id: "conv-1",
    provider: "openai",
    model: "whisper-x",
    usage_type: "AUDIO_SECOND",
    input_tokens: null,
    output_tokens: null,
    cache_creation_input_tokens: null,
    cache_read_input_tokens: null,
    // 经过一次浮点数就不是它了。
    quantity: "12345678901234567.12345678",
    unit: "SECOND",
    status: "PROCESSED",
    error_code: null,
    occurred_at: "2026-09-29T09:30:00.250",
    received_at: AT,
    processed_at: AT,
    billable_cost: "0.12500001",
    estimated_provider_cost_myr: "0.06250000",
    billing_mode_snapshot: "PREPAID",
    reference_customer_price: null,
    schema_version: "1",
    payload_shape: "QUANTITY",
    quantity_kind: "DECIMAL",
    payload_fingerprint: "fingerprint-1",
    error_message: null,
    created_at: AT,
    provider_ref_id: ID,
    model_ref_id: ID,
    provider_price_version_id: ID,
    pricing_rule_id: ID,
    fx_rate_version_id: ID,
    fx_rate_applied: "4.0830000001",
    provider_source_currency: "USD",
    provider_source_cost: "0.01530000",
    gross_margin: "0.06250001",
    gross_margin_basis: "estimated",
    wallet_transaction: { id: ID, amount: "-0.12500001" },
    attempt_count: 1,
    next_attempt_at: null,
    claim_token: null,
    claimed_at: null,
    lease_expires_at: null,
    conflicts: [],
    effective_charge: "0.12500001",
    rebills: [],
    ...overrides,
  };
}

/** 错误状态的事件没有快照：价格、规则、汇率、成本、计费额、毛利与账本行全为 null。 */
const FAILED = detail({
  status: "PRICING_ERROR",
  error_code: "NO_PRICE",
  error_message: "NoPriceError",
  processed_at: null,
  billable_cost: null,
  estimated_provider_cost_myr: null,
  billing_mode_snapshot: null,
  provider_price_version_id: null,
  pricing_rule_id: null,
  fx_rate_version_id: null,
  fx_rate_applied: null,
  provider_source_currency: null,
  provider_source_cost: null,
  gross_margin: null,
  gross_margin_basis: null,
  wallet_transaction: null,
  attempt_count: 3,
  effective_charge: null,
});

/** 跳转目标的替身：把地址（路径加查询串）显示出来，证明跳到了哪里。 */
function LocationProbe() {
  const location = useLocation();
  return <p data-testid="location">{`${location.pathname}${location.search}`}</p>;
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[`/usage-events/${ID}`]}>
        <Routes>
          <Route path={ROUTES.usageEventDetail} element={<UsageEventDetailPage />} />
          <Route path={ROUTES.rebillCreate} element={<LocationProbe />} />
          <Route path={ROUTES.rebillDetail} element={<LocationProbe />} />
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

function topDialog(): HTMLElement {
  const top = screen.getAllByRole("dialog").at(-1);
  if (top === undefined) {
    throw new Error("no dialog");
  }
  return top;
}

/** 某个字段（Descriptions 的一行）的值格。 */
function field(label: string): HTMLElement {
  const th = screen.getAllByText(label).map((el) => el.closest("th")).find((el) => el !== null);
  const td = th?.nextElementSibling;
  if (!(td instanceof HTMLElement)) {
    throw new Error(`no field "${label}"`);
  }
  return td;
}

async function openRequeue(reason: string): Promise<ReturnType<typeof userEvent.setup>> {
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Requeue" }));
  await user.click(screen.getByLabelText("Reason"));
  await user.paste(reason);
  await user.click(buttonWithText("Review", topDialog()));
  await screen.findByText("Requeue this event?");
  return user;
}

beforeEach(() => {
  vi.clearAllMocks();
});

const SLOW = { timeout: 20_000 };
configure({ asyncUtilTimeout: 5_000 });

describe("UsageEventDetailPage states", SLOW, () => {
  it("shows a loading state", () => {
    api.getUsageEvent.mockReturnValue(new Promise(() => undefined));

    renderPage();

    expect(screen.getByText("Loading the usage event…")).toBeInTheDocument();
    expect(api.getUsageEvent).toHaveBeenCalledWith(ID, expect.anything());
  });

  it("says the event does not exist on a 404", async () => {
    api.getUsageEvent.mockRejectedValue(new ApiError("USAGE_EVENT_NOT_FOUND", "The usage event does not exist.", "req-404"));

    renderPage();

    expect(await screen.findByText("Usage event not found")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to usage events" })).toHaveAttribute("href", "/usage-events");
  });

  it("shows the backend message and the request id when loading fails", async () => {
    api.getUsageEvent.mockRejectedValue(new ApiError("INTERNAL_ERROR", "An unexpected error occurred.", "req-500"));

    renderPage();

    expect(await screen.findByText("The usage event could not be loaded.")).toBeInTheDocument();
    expect(screen.getByText(/An unexpected error occurred\./)).toBeInTheDocument();
    expect(screen.getByText("req-500")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("says there is no permission on a 403", async () => {
    api.getUsageEvent.mockRejectedValue(new ApiError("ADMIN_REQUIRED", "Administrator access is required.", "req-403"));

    renderPage();

    expect(await screen.findByText("Only administrators can view usage events.")).toBeInTheDocument();
    expect(screen.getByText("req-403")).toBeInTheDocument();
  });

  it("says there are no conflicting requests when there are none", async () => {
    api.getUsageEvent.mockResolvedValue(detail());

    renderPage();

    expect(await screen.findByText("No conflicting requests for this event ID.")).toBeInTheDocument();
  });
});

describe("UsageEventDetailPage content", SLOW, () => {
  it("shows the snapshot, amounts and ledger reference exactly as the backend sent them", async () => {
    api.getUsageEvent.mockResolvedValue(detail());

    renderPage();

    expect(await screen.findByText("Cost and margin are visible to administrators only.")).toBeInTheDocument();
    // 数与金额按字符串显示：千分位、去掉末尾的 0，不舍入、不经 number。
    expect(field("Quantity")).toHaveTextContent("12,345,678,901,234,567.12345678");
    expect(field("Exchange rate applied (MYR)")).toHaveTextContent("4.0830000001");
    expect(field("Provider cost (source currency)")).toHaveTextContent("USD 0.0153");
    expect(field("Estimated provider cost")).toHaveTextContent("MYR 0.0625");
    expect(field("Billable amount")).toHaveTextContent("MYR 0.12500001");
    expect(field("Gross margin")).toHaveTextContent("MYR 0.06250001");
    expect(within(field("Gross margin")).getByText("estimated")).toBeInTheDocument();
    expect(field("Ledger entry")).toHaveTextContent(ID);
    expect(field("Ledger entry")).toHaveTextContent("MYR -0.12500001");
    expect(field("Billing mode")).toHaveTextContent("PREPAID");
    // 上报的小数秒照样换成吉隆坡时间。
    expect(field("Occurred at")).toHaveTextContent("2026-09-29 17:30:00");
    expect(field("Conversation ID")).toHaveTextContent("conv-1");
    expect(field("Payload fingerprint")).toHaveTextContent("fingerprint-1");
  });

  it("links the snapshot versions to their detail pages", async () => {
    api.getUsageEvent.mockResolvedValue(detail());

    renderPage();

    await screen.findByText("Cost and margin are visible to administrators only.");
    expect(within(field("Provider price version")).getByRole("link")).toHaveAttribute(
      "href",
      `/pricing/provider-prices/${ID}`,
    );
    expect(within(field("Pricing rule")).getByRole("link")).toHaveAttribute("href", `/pricing/rules/${ID}`);
    expect(within(field("Exchange rate version")).getByRole("link")).toHaveAttribute("href", "/fx-rates");
    expect(within(field("Provider (catalog)")).getByRole("link")).toHaveAttribute("href", `/catalog/providers/${ID}`);
    expect(within(field("Customer")).getByRole("link", { name: "Fictional Sdn Bhd" })).toHaveAttribute(
      "href",
      `/customers/${ID}`,
    );
  });

  it("does not offer requeue on a processed event", async () => {
    api.getUsageEvent.mockResolvedValue(detail());

    renderPage();

    await screen.findByText("Cost and margin are visible to administrators only.");
    expect(screen.queryByRole("button", { name: "Requeue" })).not.toBeInTheDocument();
  });

  it("does not offer requeue on a final failure that already has a ledger entry", async () => {
    api.getUsageEvent.mockResolvedValue(
      detail({ status: "FAILED_FINAL", error_code: "LEDGER_CONFLICT", gross_margin: null, gross_margin_basis: null }),
    );

    renderPage();

    await screen.findAllByText("FAILED_FINAL");
    expect(screen.queryByRole("button", { name: "Requeue" })).not.toBeInTheDocument();
  });

  it("shows an error event without a snapshot, with its attempts, and offers requeue", async () => {
    api.getUsageEvent.mockResolvedValue(FAILED);

    renderPage();

    expect(await screen.findByRole("button", { name: "Requeue" })).toBeInTheDocument();
    expect(field("Error code")).toHaveTextContent("NO_PRICE");
    expect(field("Error type")).toHaveTextContent("NoPriceError");
    expect(field("Attempts")).toHaveTextContent("3");
    expect(field("Billable amount")).toHaveTextContent("—");
    expect(field("Gross margin")).toHaveTextContent("—");
    expect(field("Ledger entry")).toHaveTextContent("—");
    expect(field("Provider price version")).toHaveTextContent("—");
    // 已解析到的目录引用照样给出。
    expect(within(field("Provider (catalog)")).getByRole("link")).toHaveAttribute("href", `/catalog/providers/${ID}`);
    expect(within(field("Status")).getByText("PRICING_ERROR")).toHaveClass("ant-tag-red");
  });

  it("shows the claim of an event being processed and the conflicting requests", async () => {
    api.getUsageEvent.mockResolvedValue(
      detail({
        status: "PROCESSING",
        claim_token: "claim-1",
        claimed_at: "2026-09-29T16:00:00",
        lease_expires_at: "2026-09-29T16:05:00",
        next_attempt_at: "2026-09-29T16:10:00",
        conflicts: [
          { api_key: "ak_test_1", mismatch: "FINGERPRINT", received_at: AT },
          { api_key: "ak_test_1", mismatch: "FINGERPRINT", received_at: AT },
          { api_key: "ak_test_2", mismatch: "BOTH", received_at: "2026-09-29T09:00:00" },
        ],
      }),
    );

    renderPage();

    expect(await screen.findByText("claim-1")).toBeInTheDocument();
    // UTC 16:00 在吉隆坡已经是第二天。
    expect(field("Claimed at")).toHaveTextContent("2026-09-30 00:00:00");
    expect(field("Lease expires at")).toHaveTextContent("2026-09-30 00:05:00");
    expect(field("Next attempt at")).toHaveTextContent("2026-09-30 00:10:00");
    expect(screen.getAllByText("ak_test_1")).toHaveLength(2);
    expect(screen.getByText("BOTH")).toBeInTheDocument();
    expect(screen.getByText("2026-09-29 17:00:00")).toBeInTheDocument();
  });
});

describe("UsageEventDetailPage rebill", SLOW, () => {
  const REBILL_ID = "rebill-1";

  it("offers Rebill on a billed prepaid event and opens the new rebill page with the customer and the event", async () => {
    const user = userEvent.setup();
    api.getUsageEvent.mockResolvedValue(detail({ customer_id: "customer-1", id: "event-1" }));

    renderPage();
    await user.click(await screen.findByRole("button", { name: "Rebill" }));

    expect(await screen.findByTestId("location")).toHaveTextContent(
      "/rebills/new?customer_id=customer-1&usage_event_id=event-1",
    );
  });

  it("shows the effective charge and an empty history when the event has not been rebilled", async () => {
    api.getUsageEvent.mockResolvedValue(detail());

    renderPage();

    expect(await screen.findByText("Rebill history")).toBeInTheDocument();
    expect(field("Effective charge")).toHaveTextContent("MYR 0.12500001");
    expect(screen.getByText("This event has not been rebilled.")).toBeInTheDocument();
  });

  it("lists the rebills of the event as sent and opens a rebill from its row", async () => {
    const user = userEvent.setup();
    api.getUsageEvent.mockResolvedValue(
      detail({
        effective_charge: "0.10000000",
        rebills: [
          {
            rebill_id: REBILL_ID,
            created_at: "2026-11-20T16:00:00",
            previous_charge: "0.12500001",
            corrected_charge: "0.10000000",
            difference: "-0.02500001",
          },
        ],
      }),
    );

    renderPage();

    expect(await screen.findByText(REBILL_ID)).toBeInTheDocument();
    expect(field("Effective charge")).toHaveTextContent("MYR 0.1");
    expect(screen.getByText("-0.02500001")).toBeInTheDocument();
    expect(screen.getByText("0.10000000")).toBeInTheDocument();
    // UTC 16:00 在吉隆坡已经是第二天。
    expect(screen.getByText("2026-11-21 00:00:00")).toBeInTheDocument();
    // 原计费额不变，仍是事件处理时的值。
    expect(field("Billable amount")).toHaveTextContent("MYR 0.12500001");

    const row = screen.getByText(REBILL_ID).closest("tr");
    if (row === null) {
      throw new Error("no row");
    }
    await user.click(row);
    expect(await screen.findByTestId("location")).toHaveTextContent(`/rebills/${REBILL_ID}`);
  });

  it.each([
    ["an internal metering event", { billing_mode_snapshot: "INTERNAL_METERED_ONLY", effective_charge: null }],
    ["an event being processed", { status: "PROCESSING", effective_charge: null }],
    ["a processed event without an effective charge", { effective_charge: null }],
  ])("shows neither the button nor the history on %s", async (_name, overrides: Partial<UsageEventDetail>) => {
    api.getUsageEvent.mockResolvedValue(detail(overrides));

    renderPage();

    await screen.findByText("Cost and margin are visible to administrators only.");
    expect(screen.queryByRole("button", { name: "Rebill" })).not.toBeInTheDocument();
    expect(screen.queryByText("Rebill history")).not.toBeInTheDocument();
    expect(screen.queryByText("Effective charge")).not.toBeInTheDocument();
  });

  it("shows neither the button nor the history on an error event", async () => {
    api.getUsageEvent.mockResolvedValue(FAILED);

    renderPage();

    expect(await screen.findByRole("button", { name: "Requeue" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Rebill" })).not.toBeInTheDocument();
    expect(screen.queryByText("Rebill history")).not.toBeInTheDocument();
  });
});

describe("UsageEventDetailPage requeue", SLOW, () => {
  it("requeues with the trimmed reason only after the confirmation, then refreshes", async () => {
    const user = userEvent.setup();
    api.getUsageEvent.mockResolvedValue(FAILED);
    api.requeueUsageEvent.mockResolvedValue({ id: ID, status: "RECEIVED" });

    renderPage();
    await user.click(await screen.findByRole("button", { name: "Requeue" }));
    await user.click(buttonWithText("Review", topDialog()));
    expect(await screen.findByText("Enter the reason.")).toBeInTheDocument();

    await user.click(screen.getByLabelText("Reason"));
    await user.paste("  Price published  ");
    await user.click(buttonWithText("Review", topDialog()));

    expect(await screen.findByText("Requeue this event?")).toBeInTheDocument();
    expect(within(topDialog()).getByText("Price published")).toBeInTheDocument();
    expect(api.requeueUsageEvent).not.toHaveBeenCalled();
    await user.click(buttonWithText("Confirm and requeue", topDialog()));

    expect(await screen.findByText("Requeued. The event will be billed in the next sweep.")).toBeInTheDocument();
    expect(api.requeueUsageEvent).toHaveBeenCalledWith(ID, "Price published");
    await waitFor(() => expect(api.getUsageEvent).toHaveBeenCalledTimes(2));
  });

  it("goes back to the reason without sending anything", async () => {
    api.getUsageEvent.mockResolvedValue(FAILED);

    renderPage();
    const user = await openRequeue("Price published");
    await user.click(buttonWithText("Back to edit", topDialog()));

    await waitFor(() => expect(screen.queryByText("Requeue this event?")).not.toBeInTheDocument());
    expect(screen.getByLabelText("Reason")).toHaveValue("Price published");
    expect(api.requeueUsageEvent).not.toHaveBeenCalled();
  });

  it("disables the confirmation while the request is on its way and sends it once", async () => {
    api.getUsageEvent.mockResolvedValue(FAILED);
    api.requeueUsageEvent.mockReturnValue(new Promise(() => undefined));

    renderPage();
    const user = await openRequeue("Price published");
    await user.dblClick(buttonWithText("Confirm and requeue", topDialog()));

    await waitFor(() => expect(buttonWithText("Confirm and requeue", topDialog())).toBeDisabled());
    expect(buttonWithText("Back to edit", topDialog())).toBeDisabled();
    expect(api.requeueUsageEvent).toHaveBeenCalledTimes(1);
  });

  it("shows the text for an event that can no longer be requeued, and does not refresh", async () => {
    api.getUsageEvent.mockResolvedValue(FAILED);
    api.requeueUsageEvent.mockRejectedValue(
      new ApiError("USAGE_EVENT_NOT_REQUEUABLE", "The event cannot be requeued.", "req-409"),
    );

    renderPage();
    const user = await openRequeue("Price published");
    await user.click(buttonWithText("Confirm and requeue", topDialog()));

    expect(
      await screen.findByText(
        "This event cannot be requeued: it is not in an error status, or it already has a ledger entry.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("req-409")).toBeInTheDocument();
    expect(screen.getByText("Requeue this event?")).toBeInTheDocument();
    expect(api.getUsageEvent).toHaveBeenCalledTimes(1);
  });

  it("falls back to the backend message for a code it does not know", async () => {
    api.getUsageEvent.mockResolvedValue(FAILED);
    api.requeueUsageEvent.mockRejectedValue(new ApiError("SOMETHING_NEW", "The backend says no.", "req-409"));

    renderPage();
    const user = await openRequeue("Price published");
    await user.click(buttonWithText("Confirm and requeue", topDialog()));

    expect(await screen.findByText("The events could not be requeued.")).toBeInTheDocument();
    expect(screen.getByText(/The backend says no\./)).toBeInTheDocument();
    expect(screen.getByText("req-409")).toBeInTheDocument();
  });
});
