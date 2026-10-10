/**
 * 用量事件列表：三种状态（spec §132 DoD 第 8 条）、403 与 422；数与金额按字符串显示；状态颜色与可勾选的行；
 * 筛选（吉隆坡时间换成带时区的 RFC 3339、改条件回到第 1 页）；勾选与按条件批量重新入队的二次确认、
 * 成功后重读、失败时显示后端码对应的文案与 request_id。
 *
 * 请求路径与字段名由 `api/adminUsageEvents.test.ts` 管，这里只看页面把什么交给了它。uuid 一律全零。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { configure, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import type { Page } from "../../api/adminCustomers";
import type { UsageEventSummary } from "../../api/adminUsageEvents";
import { ApiError } from "../../api/client";
import { UsageEventsPage, toBulkConditions } from "./UsageEventsPage";

const ID = "00000000-0000-0000-0000-000000000000";
/** 同一页里第二行的 id：表格按 id 区分行，两行不能同 key。不是 uuid，只是个占位串。 */
const OTHER = "other-event";

const api = vi.hoisted(() => ({
  listUsageEvents: vi.fn(),
  requeueUsageEvent: vi.fn(),
  bulkRequeueUsageEvents: vi.fn(),
}));

vi.mock("../../api/adminUsageEvents", async () => {
  const actual = await vi.importActual<typeof import("../../api/adminUsageEvents")>(
    "../../api/adminUsageEvents",
  );
  return { ...actual, ...api };
});

function event(overrides: Partial<UsageEventSummary> = {}): UsageEventSummary {
  return {
    id: ID,
    event_id: ID,
    customer_id: ID,
    customer_company_name: "Fictional Sdn Bhd",
    project_id: ID,
    request_id: "call-1",
    conversation_id: null,
    provider: "openai",
    model: "whisper-x",
    usage_type: "AUDIO_SECOND",
    input_tokens: null,
    output_tokens: null,
    cache_creation_input_tokens: null,
    cache_read_input_tokens: null,
    quantity: "2.00000000",
    unit: "SECOND",
    status: "PROCESSED",
    error_code: null,
    occurred_at: "2026-09-29T09:30:00",
    received_at: "2026-09-29T09:30:01",
    processed_at: "2026-09-29T09:30:05",
    billable_cost: "0.12500000",
    estimated_provider_cost_myr: "0.06250000",
    billing_mode_snapshot: "PREPAID",
    reference_customer_price: null,
    ...overrides,
  };
}

const FAILED = event({
  id: ID,
  status: "PRICING_ERROR",
  error_code: "NO_PRICE",
  processed_at: null,
  billable_cost: null,
  estimated_provider_cost_myr: null,
  billing_mode_snapshot: null,
});

function page(items: UsageEventSummary[], total: number, number = 1): Page<UsageEventSummary> {
  return { items, page: number, page_size: 20, total };
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <UsageEventsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** 表头之后的数据行。 */
function dataRows(): HTMLElement[] {
  return screen.getAllByRole("row").slice(1);
}

function row(index: number): HTMLElement {
  const found = dataRows()[index];
  if (found === undefined) {
    throw new Error(`no row ${String(index)}`);
  }
  return found;
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

/**
 * 往输入框里一次性贴入文字。不用 `user.type`：逐字输入每个字都让整张 antd 表单重画一遍，沙箱里多个文件
 * 并行时这份 CPU 会把别的文件的用例拖过超时。
 */
async function fill(label: string, text: string): Promise<void> {
  const user = userEvent.setup();
  await user.click(screen.getByLabelText(label));
  await user.paste(text);
}

async function pickStatus(status: string): Promise<void> {
  const user = userEvent.setup();
  await user.click(screen.getByLabelText("Status"));
  fireEvent.click(await screen.findByTitle(status));
}

beforeEach(() => {
  vi.clearAllMocks();
});

const SLOW = { timeout: 20_000 };
configure({ asyncUtilTimeout: 5_000 });

describe("UsageEventsPage states", SLOW, () => {
  it("shows a loading state while the first page is on its way", () => {
    api.listUsageEvents.mockReturnValue(new Promise(() => undefined));

    renderPage();

    expect(screen.getByText("Loading usage events…")).toBeInTheDocument();
    expect(api.listUsageEvents).toHaveBeenCalledWith({}, 1, 20, expect.anything());
  });

  it("shows the backend message and the request id when the list fails", async () => {
    api.listUsageEvents.mockRejectedValue(new ApiError("INTERNAL_ERROR", "An unexpected error occurred.", "req-500"));

    renderPage();

    expect(await screen.findByText("The usage events could not be loaded.")).toBeInTheDocument();
    expect(screen.getByText(/An unexpected error occurred\./)).toBeInTheDocument();
    expect(screen.getByText("req-500")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("says there is no permission on a 403 instead of a generic failure", async () => {
    api.listUsageEvents.mockRejectedValue(new ApiError("ADMIN_REQUIRED", "Administrator access is required.", "req-403"));

    renderPage();

    expect(await screen.findByText("Only administrators can view usage events.")).toBeInTheDocument();
    expect(screen.getByText("req-403")).toBeInTheDocument();
    expect(screen.queryByText("The usage events could not be loaded.")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
  });

  it("shows the text for a 422 with the backend message, and keeps the filters editable", async () => {
    api.listUsageEvents.mockRejectedValue(
      new ApiError("VALIDATION_ERROR", "Invalid request fields: query.occurred_from", "req-422"),
    );

    renderPage();

    expect(
      await screen.findByText("The request was not accepted. Check the filters or the reason."),
    ).toBeInTheDocument();
    expect(screen.getByText(/Invalid request fields: query\.occurred_from/)).toBeInTheDocument();
    expect(screen.getByText("req-422")).toBeInTheDocument();
    expect(screen.getByLabelText("Request ID")).toBeEnabled();
  });

  it("says so when there are no usage events at all", async () => {
    api.listUsageEvents.mockResolvedValue(page([], 0));

    renderPage();

    expect(await screen.findByText("No usage events yet.")).toBeInTheDocument();
    expect(screen.getByText("Cost and margin are visible to administrators only.")).toBeInTheDocument();
  });

  it("says nothing matches when a filter leaves the list empty", async () => {
    const user = userEvent.setup();
    api.listUsageEvents.mockResolvedValueOnce(page([event()], 1)).mockResolvedValue(page([], 0));

    renderPage();
    await screen.findByText("Fictional Sdn Bhd");
    await fill("Request ID", "call-404");
    await user.click(screen.getByRole("button", { name: "Apply filters" }));

    expect(await screen.findByText("No usage events match these filters.")).toBeInTheDocument();
  });
});

describe("UsageEventsPage table", SLOW, () => {
  it("shows quantities and amounts exactly as the backend sent them, never through a number", async () => {
    api.listUsageEvents.mockResolvedValue(
      page(
        [
          event({
            // 这两个值经过一次浮点数就不是它们了。
            quantity: "12345678901234567.12345678",
            billable_cost: "99999999999.99999999",
            estimated_provider_cost_myr: "0.10000001",
          }),
          event({
            id: OTHER,
            usage_type: "LLM_TOKEN",
            quantity: null,
            unit: "TOKEN",
            input_tokens: 1200,
            output_tokens: 34,
          }),
        ],
        2,
      ),
    );

    renderPage();

    await screen.findAllByText("Fictional Sdn Bhd");
    expect(row(0)).toHaveTextContent("12,345,678,901,234,567.12345678");
    expect(row(0)).toHaveTextContent("MYR 99,999,999,999.99999999");
    expect(row(0)).toHaveTextContent("MYR 0.10000001");
    // UTC 09:30 是吉隆坡 17:30。
    expect(row(0)).toHaveTextContent("2026-09-29 17:30:00");
    expect(row(1)).toHaveTextContent("In 1200 / Out 34 tokens");
  });

  it("colours the error statuses red and lets only their rows be ticked", async () => {
    api.listUsageEvents.mockResolvedValue(page([event({ id: OTHER }), FAILED], 2));

    renderPage();

    await screen.findByText("PRICING_ERROR");
    expect(within(row(0)).getByText("PROCESSED")).not.toHaveClass("ant-tag-red");
    expect(within(row(0)).getByText("PROCESSED")).toHaveClass("ant-tag-green");
    expect(within(row(1)).getByText("PRICING_ERROR")).toHaveClass("ant-tag-red");
    expect(within(row(0)).getByRole("checkbox")).toBeDisabled();
    expect(within(row(1)).getByRole("checkbox")).toBeEnabled();
    expect(row(1)).toHaveTextContent("NO_PRICE");
  });

  it("links each row to its detail page", async () => {
    api.listUsageEvents.mockResolvedValue(page([event()], 1));

    renderPage();

    expect(await screen.findByRole("link", { name: "View" })).toHaveAttribute("href", `/usage-events/${ID}`);
  });
});

describe("UsageEventsPage filters", SLOW, () => {
  it("sends the trimmed filters and the time range as RFC 3339 with a zone", async () => {
    const user = userEvent.setup();
    api.listUsageEvents.mockResolvedValue(page([event()], 1));

    renderPage();
    await screen.findByText("Fictional Sdn Bhd");
    await fill("Customer ID", ` ${ID} `);
    await fill("Provider (as reported)", "openai");
    await fill("Error code", "NO_PRICE");
    // 吉隆坡 9 月 21 日 00:00 是 UTC 9 月 20 日 16:00。
    fireEvent.change(screen.getByLabelText("Occurred from (Kuala Lumpur time)"), {
      target: { value: "2026-09-21T00:00" },
    });
    fireEvent.change(screen.getByLabelText("Occurred before (Kuala Lumpur time)"), {
      target: { value: "2026-09-22T07:59:59" },
    });
    await pickStatus("PRICING_ERROR");
    await user.click(screen.getByRole("button", { name: "Apply filters" }));

    await waitFor(() =>
      expect(api.listUsageEvents).toHaveBeenLastCalledWith(
        {
          customer_id: ID,
          provider: "openai",
          status: "PRICING_ERROR",
          error_code: "NO_PRICE",
          occurred_from: "2026-09-20T16:00:00Z",
          occurred_to: "2026-09-21T23:59:59Z",
        },
        1,
        20,
        expect.anything(),
      ),
    );
  });

  it("goes back to page 1 when a filter changes", async () => {
    const user = userEvent.setup();
    api.listUsageEvents.mockImplementation((_filters: unknown, requested: number) =>
      Promise.resolve(
        page([event({ customer_company_name: `Company on page ${String(requested)}` })], 45, requested),
      ),
    );

    renderPage();
    expect(await screen.findByText("45 usage events")).toBeInTheDocument();
    await user.click(screen.getByTitle("2"));
    expect(await screen.findByText("Company on page 2")).toBeInTheDocument();
    expect(api.listUsageEvents).toHaveBeenLastCalledWith({}, 2, 20, expect.anything());

    await fill("Model (as reported)", "whisper-x");
    await user.click(screen.getByRole("button", { name: "Apply filters" }));

    await waitFor(() =>
      expect(api.listUsageEvents).toHaveBeenLastCalledWith({ model: "whisper-x" }, 1, 20, expect.anything()),
    );
  });

  it("refuses a range whose end is not after its start without asking the backend", async () => {
    const user = userEvent.setup();
    api.listUsageEvents.mockResolvedValue(page([event()], 1));

    renderPage();
    await screen.findByText("Fictional Sdn Bhd");
    fireEvent.change(screen.getByLabelText("Occurred from (Kuala Lumpur time)"), {
      target: { value: "2026-09-21T00:00" },
    });
    fireEvent.change(screen.getByLabelText("Occurred before (Kuala Lumpur time)"), {
      target: { value: "2026-09-21T00:00" },
    });
    await user.click(screen.getByRole("button", { name: "Apply filters" }));

    expect(await screen.findByText("The end must be later than the start.")).toBeInTheDocument();
    expect(api.listUsageEvents).toHaveBeenCalledTimes(1);
  });

  it("clears every filter", async () => {
    const user = userEvent.setup();
    api.listUsageEvents.mockResolvedValue(page([event()], 1));

    renderPage();
    await screen.findByText("Fictional Sdn Bhd");
    await fill("Request ID", "call-1");
    await user.click(screen.getByRole("button", { name: "Apply filters" }));
    await waitFor(() =>
      expect(api.listUsageEvents).toHaveBeenLastCalledWith({ request_id: "call-1" }, 1, 20, expect.anything()),
    );

    await user.click(screen.getByRole("button", { name: "Clear filters" }));

    await waitFor(() => expect(api.listUsageEvents).toHaveBeenLastCalledWith({}, 1, 20, expect.anything()));
    expect(screen.getByLabelText("Request ID")).toHaveValue("");
  });
});

describe("UsageEventsPage requeue selected", SLOW, () => {
  it("requeues the ticked rows with the trimmed reason only after the confirmation, then refreshes", async () => {
    const user = userEvent.setup();
    api.listUsageEvents.mockResolvedValue(page([event({ id: OTHER }), FAILED], 2));
    api.requeueUsageEvent.mockResolvedValue({ id: ID, status: "RECEIVED" });

    renderPage();
    await screen.findByText("PRICING_ERROR");
    expect(screen.getByRole("button", { name: "Requeue selected (0)" })).toBeDisabled();
    await user.click(within(row(1)).getByRole("checkbox"));
    await user.click(screen.getByRole("button", { name: "Requeue selected (1)" }));

    await user.click(buttonWithText("Review", topDialog()));
    expect(await screen.findByText("Enter the reason.")).toBeInTheDocument();
    await user.click(screen.getByLabelText("Reason"));
    await user.paste("  Price published  ");
    await user.click(buttonWithText("Review", topDialog()));

    expect(await screen.findByText("Requeue 1 selected events?")).toBeInTheDocument();
    expect(api.requeueUsageEvent).not.toHaveBeenCalled();
    await user.click(buttonWithText("Confirm and requeue", topDialog()));

    expect(await screen.findByText("1 requeued, 0 skipped.")).toBeInTheDocument();
    expect(api.requeueUsageEvent).toHaveBeenCalledTimes(1);
    expect(api.requeueUsageEvent).toHaveBeenCalledWith(ID, "Price published");
    await waitFor(() => expect(api.listUsageEvents).toHaveBeenCalledTimes(2));
  });

  it("counts an event that can no longer be requeued as skipped", async () => {
    const user = userEvent.setup();
    api.listUsageEvents.mockResolvedValue(page([FAILED], 1));
    api.requeueUsageEvent.mockRejectedValue(new ApiError("USAGE_EVENT_NOT_REQUEUABLE", "Not requeuable.", "req-409"));

    renderPage();
    await screen.findByText("PRICING_ERROR");
    await user.click(within(row(0)).getByRole("checkbox"));
    await user.click(screen.getByRole("button", { name: "Requeue selected (1)" }));
    await user.click(screen.getByLabelText("Reason"));
    await user.paste("Price published");
    await user.click(buttonWithText("Review", topDialog()));
    await user.click(await screen.findByText("Confirm and requeue"));

    expect(await screen.findByText("0 requeued, 1 skipped.")).toBeInTheDocument();
  });

  it("shows the text and the request id when the backend refuses, and stays in the confirmation", async () => {
    const user = userEvent.setup();
    api.listUsageEvents.mockResolvedValue(page([FAILED], 1));
    api.requeueUsageEvent.mockRejectedValue(new ApiError("VALIDATION_ERROR", "Invalid request fields: body.reason", "req-422"));

    renderPage();
    await screen.findByText("PRICING_ERROR");
    await user.click(within(row(0)).getByRole("checkbox"));
    await user.click(screen.getByRole("button", { name: "Requeue selected (1)" }));
    await user.click(screen.getByLabelText("Reason"));
    await user.paste("Price published");
    await user.click(buttonWithText("Review", topDialog()));
    await user.click(await screen.findByText("Confirm and requeue"));

    expect(
      await screen.findByText("The request was not accepted. Check the filters or the reason."),
    ).toBeInTheDocument();
    expect(screen.getByText("req-422")).toBeInTheDocument();
    expect(screen.getByText("Requeue 1 selected events?")).toBeInTheDocument();
    expect(screen.queryByText(/requeued, .* skipped\./)).not.toBeInTheDocument();
  });
});

describe("UsageEventsPage requeue by filter", SLOW, () => {
  it("is not offered until the filters name one error status", async () => {
    api.listUsageEvents.mockResolvedValue(page([FAILED], 1));

    renderPage();
    await screen.findByText("PRICING_ERROR");

    expect(screen.getByRole("button", { name: "Requeue all matching…" })).toBeDisabled();
    expect(
      screen.getByText(
        "To requeue everything that matches, filter by one error status and not by project, conversation or request.",
      ),
    ).toBeInTheDocument();
  });

  it("submits the current filters with the reason after a confirmation that names the count", async () => {
    const user = userEvent.setup();
    api.listUsageEvents.mockResolvedValue(page([FAILED], 3));
    api.bulkRequeueUsageEvents.mockResolvedValue({ requeued: 2, skipped: 1, ids: [ID, ID] });

    renderPage();
    await screen.findByText("PRICING_ERROR");
    await fill("Provider (as reported)", "openai");
    await pickStatus("PRICING_ERROR");
    await user.click(screen.getByRole("button", { name: "Apply filters" }));
    await waitFor(() =>
      expect(api.listUsageEvents).toHaveBeenLastCalledWith(
        { provider: "openai", status: "PRICING_ERROR" },
        1,
        20,
        expect.anything(),
      ),
    );
    const callsBefore = api.listUsageEvents.mock.calls.length;

    await waitFor(() => expect(screen.getByRole("button", { name: "Requeue all matching…" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Requeue all matching…" }));
    await user.click(screen.getByLabelText("Reason"));
    await user.paste("Price published");
    await user.click(buttonWithText("Review", topDialog()));

    const confirm = await waitFor(() => {
      const dialog = topDialog();
      expect(within(dialog).getByText("Requeue all matching events?")).toBeInTheDocument();
      return dialog;
    });
    expect(within(confirm).getByText("Up to 3 events will be requeued (at most 1000 per run).")).toBeInTheDocument();
    expect(api.bulkRequeueUsageEvents).not.toHaveBeenCalled();
    await user.click(buttonWithText("Confirm and requeue", confirm));

    expect(await screen.findByText("2 requeued, 1 skipped.")).toBeInTheDocument();
    expect(api.bulkRequeueUsageEvents).toHaveBeenCalledWith({
      status: "PRICING_ERROR",
      provider: "openai",
      reason: "Price published",
    });
    await waitFor(() => expect(api.listUsageEvents.mock.calls.length).toBeGreaterThan(callsBefore));
  });

  it("keeps both requeue actions closed until the list for the new filters arrives", async () => {
    const user = userEvent.setup();
    let arrive: (value: Page<UsageEventSummary>) => void = () => undefined;
    api.listUsageEvents.mockResolvedValueOnce(page([FAILED], 1)).mockReturnValueOnce(
      new Promise<Page<UsageEventSummary>>((resolve) => {
        arrive = resolve;
      }),
    );

    renderPage();
    await screen.findByText("PRICING_ERROR");
    await pickStatus("PRICING_ERROR");
    await user.click(screen.getByRole("button", { name: "Apply filters" }));
    await waitFor(() =>
      expect(api.listUsageEvents).toHaveBeenLastCalledWith({ status: "PRICING_ERROR" }, 1, 20, expect.anything()),
    );

    // 表里还是上一份：条数与行都属于旧条件。
    expect(screen.getByRole("button", { name: "Requeue all matching…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Requeue selected (0)" })).toBeDisabled();
    expect(within(row(0)).getByRole("checkbox")).toBeDisabled();

    arrive(page([FAILED], 300));
    await waitFor(() => expect(screen.getByRole("button", { name: "Requeue all matching…" })).toBeEnabled());
    expect(within(row(0)).getByRole("checkbox")).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Requeue all matching…" }));
    expect(await screen.findByText("Up to 300 events will be requeued (at most 1000 per run).")).toBeInTheDocument();
    expect(api.bulkRequeueUsageEvents).not.toHaveBeenCalled();
  });

  it("caps the count shown at 1000", async () => {
    const user = userEvent.setup();
    api.listUsageEvents.mockResolvedValue(page([FAILED], 2500));

    renderPage();
    await screen.findByText("PRICING_ERROR");
    await pickStatus("PRICING_ERROR");
    await user.click(screen.getByRole("button", { name: "Apply filters" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Requeue all matching…" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Requeue all matching…" }));

    expect(await screen.findByText("Up to 1000 events will be requeued (at most 1000 per run).")).toBeInTheDocument();
    expect(api.bulkRequeueUsageEvents).not.toHaveBeenCalled();
  });

  it("shows the text for a customer that does not exist", async () => {
    const user = userEvent.setup();
    api.listUsageEvents.mockResolvedValue(page([FAILED], 1));
    api.bulkRequeueUsageEvents.mockRejectedValue(
      new ApiError("CUSTOMER_NOT_FOUND", "The customer does not exist.", "req-404"),
    );

    renderPage();
    await screen.findByText("PRICING_ERROR");
    await fill("Customer ID", ID);
    await pickStatus("PRICING_ERROR");
    await user.click(screen.getByRole("button", { name: "Apply filters" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Requeue all matching…" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Requeue all matching…" }));
    await user.click(screen.getByLabelText("Reason"));
    await user.paste("Price published");
    await user.click(buttonWithText("Review", topDialog()));
    await user.click(await screen.findByText("Confirm and requeue"));

    expect(await screen.findByText("The customer in the filters does not exist.")).toBeInTheDocument();
    expect(screen.getByText("req-404")).toBeInTheDocument();
  });

  it("only builds a request for one error status without project, conversation or request", () => {
    expect(toBulkConditions({})).toBeNull();
    expect(toBulkConditions({ status: "PROCESSED" })).toBeNull();
    expect(toBulkConditions({ status: "FAILED_FINAL", project_id: ID })).toBeNull();
    expect(toBulkConditions({ status: "FAILED_FINAL", conversation_id: "conv-1" })).toBeNull();
    expect(toBulkConditions({ status: "FAILED_FINAL", request_id: "call-1" })).toBeNull();
    expect(
      toBulkConditions({
        status: "FX_RATE_ERROR",
        customer_id: ID,
        model: "gpt-x",
        occurred_from: "2026-09-20T16:00:00Z",
      }),
    ).toEqual({ status: "FX_RATE_ERROR", customer_id: ID, model: "gpt-x", occurred_from: "2026-09-20T16:00:00Z" });
  });
});
