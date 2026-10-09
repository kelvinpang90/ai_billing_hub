/**
 * 汇率页：版本列表与拉取记录各自的三种状态、无权限；「中间价、吉隆坡中午场、从发布时刻起生效」；
 * BNM 草稿没有编辑按钮；手工草稿的新建与编辑（只发改了的字段）、发布、退役、丢弃的二次确认、成功后重读、
 * 失败时显示后端码对应的文案与 request_id、确认中防重复提交；汇率原样是字符串。
 *
 * 请求路径与字段名由 `api/adminFxRates.test.ts` 管。uuid 一律全零，所以每张表只放一行（行键是 id）。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { configure, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import type { Page } from "../../api/adminCustomers";
import type { FetchAttempt, FxRate } from "../../api/adminFxRates";
import { ApiError } from "../../api/client";
import { FxRatesPage } from "./FxRatesPage";

const ID = "00000000-0000-0000-0000-000000000000";
const AT = "2026-09-29T08:30:00";

const api = vi.hoisted(() => ({
  listFxRates: vi.fn(),
  listFxFetchAttempts: vi.fn(),
  createFxRate: vi.fn(),
  updateFxRate: vi.fn(),
  publishFxRate: vi.fn(),
  retireFxRate: vi.fn(),
  discardFxRate: vi.fn(),
}));

vi.mock("../../api/adminFxRates", async () => {
  const actual = await vi.importActual<typeof import("../../api/adminFxRates")>("../../api/adminFxRates");
  return { ...actual, ...api };
});

function page<T>(items: T[], total = items.length): Page<T> {
  return { items, page: 1, page_size: 20, total };
}

function rate(overrides: Partial<FxRate> = {}): FxRate {
  return {
    id: ID,
    base_currency: "USD",
    quote_currency: "MYR",
    rate: "4.083",
    source: "MANUAL",
    source_reference: "Fictional bank notice, viewed 2026-09-29",
    source_quote_date: null,
    observed_at: "2026-09-29T04:00:00",
    status: "DRAFT",
    effective_from: null,
    effective_to: null,
    created_by_email: "admin@example.com",
    approved_by_email: null,
    approved_at: null,
    created_at: AT,
    updated_at: AT,
    ...overrides,
  };
}

const BNM_DRAFT = rate({
  source: "BNM",
  source_reference: "bnm:exchange-rate:USD:2026-09-29:session=1200:middle_rate:unit=1",
  source_quote_date: "2026-09-29",
  created_by_email: null,
});

const PUBLISHED = rate({
  status: "PUBLISHED",
  effective_from: "2026-09-29T08:30:01",
  approved_by_email: "publisher@example.com",
  approved_at: AT,
});

const ATTEMPT: FetchAttempt = {
  base_currency: "USD",
  source: "BNM",
  requested_date: "2026-09-29",
  outcome: "FAILED",
  quote_date: null,
  error_code: "TIMEOUT",
  fx_rate_id: null,
  attempted_at: "2026-09-29T04:30:00",
};

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <FxRatesPage />
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

/** 版本表里带这个操作按钮的那一行。 */
function actionRow(text: string): HTMLElement {
  const row = buttonWithText(text).closest("tr");
  if (row === null) {
    throw new Error(`"${text}" is not inside a table row`);
  }
  return row;
}

function topDialog(): HTMLElement {
  const top = screen.getAllByRole("dialog").at(-1);
  if (top === undefined) {
    throw new Error("no dialog");
  }
  return top;
}

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

type User = ReturnType<typeof userEvent.setup>;

/** 填手工草稿表单并点「Review」。观测时刻按吉隆坡时间。 */
async function fillManualDraft(user: User, currency: string, value: string): Promise<void> {
  await user.click(await screen.findByRole("button", { name: "New manual draft" }));
  const form = topDialog();
  await user.click(within(form).getByLabelText("Currency"));
  await user.paste(currency);
  await user.click(within(form).getByLabelText("Rate (MYR)"));
  await user.paste(value);
  fireEvent.change(within(form).getByLabelText("Observed at (Kuala Lumpur time)"), {
    target: { value: "2026-09-29T12:00" },
  });
  await user.click(within(form).getByLabelText("Source reference"));
  await user.paste(" Fictional bank notice, viewed 2026-09-29 ");
  await user.click(buttonWithText("Review", form));
}

beforeEach(() => {
  vi.clearAllMocks();
  api.listFxFetchAttempts.mockResolvedValue(page([]));
});

const SLOW = { timeout: 20_000 };
configure({ asyncUtilTimeout: 5_000 });

describe("FxRatesPage states", SLOW, () => {
  it("shows a loading state for both lists", () => {
    api.listFxRates.mockReturnValue(new Promise(() => undefined));
    api.listFxFetchAttempts.mockReturnValue(new Promise(() => undefined));

    renderPage();

    expect(screen.getByText("Loading exchange rates…")).toBeInTheDocument();
    expect(screen.getByText("Loading fetch attempts…")).toBeInTheDocument();
  });

  it("shows the backend message and the request id when the versions fail to load", async () => {
    api.listFxRates.mockRejectedValue(new ApiError("INTERNAL_ERROR", "An unexpected error occurred.", "req-500"));

    renderPage();

    expect(await screen.findByText("The exchange rates could not be loaded.")).toBeInTheDocument();
    expect(screen.getByText(/An unexpected error occurred\./)).toBeInTheDocument();
    expect(screen.getByText("req-500")).toBeInTheDocument();
    // 拉取记录一节照常显示。
    expect(await screen.findByText("No fetch attempts yet.")).toBeInTheDocument();
  });

  it("shows the request id when the fetch attempts fail to load", async () => {
    api.listFxRates.mockResolvedValue(page([]));
    api.listFxFetchAttempts.mockRejectedValue(new ApiError("INTERNAL_ERROR", "Boom.", "req-501"));

    renderPage();

    expect(await screen.findByText("The fetch attempts could not be loaded.")).toBeInTheDocument();
    expect(screen.getByText("req-501")).toBeInTheDocument();
  });

  it("says there is no permission on a 403", async () => {
    api.listFxRates.mockRejectedValue(new ApiError("ADMIN_REQUIRED", "Administrator access is required.", "req-403"));

    renderPage();

    expect(await screen.findByText("Only administrators can manage prices and exchange rates.")).toBeInTheDocument();
    expect(screen.queryByText("The exchange rates could not be loaded.")).not.toBeInTheDocument();
  });

  it("says so when both lists are empty, and says how rates take effect", async () => {
    api.listFxRates.mockResolvedValue(page([]));

    renderPage();

    expect(await screen.findByText("No exchange rates yet.")).toBeInTheDocument();
    expect(await screen.findByText("No fetch attempts yet.")).toBeInTheDocument();
    expect(screen.getByText("BNM fetch attempts")).toBeInTheDocument();
    expect(
      screen.getByText("Middle rate, Kuala Lumpur noon session, effective from the moment it is published."),
    ).toBeInTheDocument();
    expect(api.listFxRates).toHaveBeenCalledWith({}, 1, 20, expect.anything());
    expect(api.listFxFetchAttempts).toHaveBeenCalledWith({}, 1, 20, expect.anything());
  });
});

describe("FxRatesPage tables", SLOW, () => {
  it("shows the rate exactly as the backend sent it", async () => {
    api.listFxRates.mockResolvedValue(page([rate({ rate: "12345678901234.1234567891" })]));

    renderPage();

    // 10 位小数：走一遍浮点数就不是它了。
    expect(await screen.findByText("12,345,678,901,234.1234567891")).toBeInTheDocument();
  });

  it("offers no edit on a BNM draft, but publish and discard", async () => {
    api.listFxRates.mockResolvedValue(page([BNM_DRAFT]));

    renderPage();

    // 按文字找按钮再取所在行：在整页 antd 表格上按 role 查（要算可访问名）在 jsdom 里慢到超时。
    await screen.findByText("Publish");
    const row = actionRow("Publish");
    expect(within(row).getByText("Discard").closest("button")).not.toBeNull();
    expect(within(row).queryByText("Edit")).not.toBeInTheDocument();
    expect(row).toHaveTextContent("BNM");
    expect(row).toHaveTextContent("2026-09-29");
  });

  it("offers edit, publish and discard on a manual draft", async () => {
    api.listFxRates.mockResolvedValue(page([rate()]));

    renderPage();

    await screen.findByText("Edit");
    const row = actionRow("Edit");
    expect(within(row).getByText("Publish").closest("button")).not.toBeNull();
    expect(within(row).getByText("Discard").closest("button")).not.toBeNull();
    expect(within(row).queryByText("Retire")).not.toBeInTheDocument();
  });

  it("offers only retire on the current published rate", async () => {
    api.listFxRates.mockResolvedValue(page([PUBLISHED]));

    renderPage();

    await screen.findByText("Retire");
    const row = actionRow("Retire");
    expect(within(row).queryByText("Edit")).not.toBeInTheDocument();
    expect(within(row).queryByText("Publish")).not.toBeInTheDocument();
    expect(row).toHaveTextContent("publisher@example.com");
    expect(row).toHaveTextContent("2026-09-29 16:30:01");
    expect(row).toHaveTextContent("No end");
  });

  it("lists fetch attempts with their outcome and error code", async () => {
    api.listFxRates.mockResolvedValue(page([]));
    api.listFxFetchAttempts.mockResolvedValue(page([ATTEMPT]));

    renderPage();

    expect(await screen.findByText("TIMEOUT")).toBeInTheDocument();
    expect(screen.getByText("Failed")).toBeInTheDocument();
    expect(screen.getByText("2026-09-29 12:30:00")).toBeInTheDocument();
  });
});

describe("FxRatesPage manual drafts", SLOW, () => {
  it("creates only after the confirmation, with the rate as typed and the time with a zone", async () => {
    const user = userEvent.setup();
    api.listFxRates.mockResolvedValue(page([]));
    api.createFxRate.mockResolvedValue(rate());

    renderPage();
    await fillManualDraft(user, "USD", "4.0830000001");

    expect(await screen.findByText("Create this manual draft?")).toBeInTheDocument();
    expect(api.createFxRate).not.toHaveBeenCalled();
    expect(within(topDialog()).getByText("4.0830000001")).toBeInTheDocument();
    await user.click(buttonWithText("Confirm and create", topDialog()));

    expect(await screen.findByText("Draft created.")).toBeInTheDocument();
    expect(api.createFxRate).toHaveBeenCalledWith({
      base_currency: "USD",
      rate: "4.0830000001",
      // 12:00 吉隆坡 = 04:00 UTC，带时区、整秒。
      observed_at: "2026-09-29T04:00:00Z",
      source_reference: "Fictional bank notice, viewed 2026-09-29",
    });
    await waitFor(() => expect(api.listFxRates).toHaveBeenCalledTimes(2));
  });

  it("shows the text and the request id when the backend refuses the new draft, and does not refresh", async () => {
    const user = userEvent.setup();
    api.listFxRates.mockResolvedValue(page([]));
    api.createFxRate.mockRejectedValue(new ApiError("VALIDATION_ERROR", "rate: invalid.", "req-422"));

    renderPage();
    await fillManualDraft(user, "USD", "4.083");
    await user.click(await screen.findByText("Confirm and create"));

    expect(await screen.findByText("Some fields were not accepted.")).toBeInTheDocument();
    expect(screen.getByText("req-422")).toBeInTheDocument();
    expect(screen.queryByText("Draft created.")).not.toBeInTheDocument();
    expect(api.listFxRates).toHaveBeenCalledTimes(1);
  });

  it("refuses a rate with more than 10 decimal places instead of rounding it", async () => {
    const user = userEvent.setup();
    api.listFxRates.mockResolvedValue(page([]));

    renderPage();
    await fillManualDraft(user, "USD", "4.08300000001");

    expect(
      await screen.findByText(
        "Enter a positive number with up to 14 digits before the decimal point and up to 10 after it, with no sign or exponent.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText("Create this manual draft?")).not.toBeInTheDocument();
  });

  it("refuses MYR as the currency", async () => {
    const user = userEvent.setup();
    api.listFxRates.mockResolvedValue(page([]));

    renderPage();
    await fillManualDraft(user, "MYR", "1");

    expect(await screen.findByText("Enter three capital letters other than MYR.")).toBeInTheDocument();
    expect(screen.queryByText("Create this manual draft?")).not.toBeInTheDocument();
  });

  it("sends once however often the confirm button is clicked while it is running", async () => {
    const user = userEvent.setup();
    const pending = deferred<FxRate>();
    api.listFxRates.mockResolvedValue(page([]));
    api.createFxRate.mockReturnValue(pending.promise);

    renderPage();
    await fillManualDraft(user, "SGD", "3.1");
    await user.dblClick(buttonWithText("Confirm and create", topDialog()));

    await waitFor(() => expect(buttonWithText("Confirm and create", topDialog())).toBeDisabled());
    expect(api.createFxRate).toHaveBeenCalledTimes(1);

    pending.resolve(rate());
    expect(await screen.findByText("Draft created.")).toBeInTheDocument();
  });

  it("edits only the rate that changed, compared as a decimal", async () => {
    const user = userEvent.setup();
    api.listFxRates.mockResolvedValue(page([rate()]));
    api.updateFxRate.mockResolvedValue(rate({ rate: "4.1" }));

    renderPage();
    await user.click(await screen.findByRole("button", { name: "Edit" }));
    const form = topDialog();
    // 编辑时观测时刻预填成吉隆坡时间（jsdom 会把整分的秒省掉）；币种不可改，不在表单里。
    const observed = within(form).getByLabelText("Observed at (Kuala Lumpur time)");
    expect((observed as HTMLInputElement).value).toMatch(/^2026-09-29T12:00/);
    expect(within(form).queryByLabelText("Currency")).not.toBeInTheDocument();
    await user.clear(within(form).getByLabelText("Rate (MYR)"));
    await user.paste("4.1000000000");
    await user.click(buttonWithText("Review", form));

    expect(await screen.findByText("Save the changes to this draft?")).toBeInTheDocument();
    await user.click(buttonWithText("Confirm and save", topDialog()));

    expect(await screen.findByText("Draft saved.")).toBeInTheDocument();
    expect(api.updateFxRate).toHaveBeenCalledWith(ID, { rate: "4.1000000000" });
    await waitFor(() => expect(api.listFxRates).toHaveBeenCalledTimes(2));
  });

  it("does not offer to save when the rate only gained trailing zeros", async () => {
    const user = userEvent.setup();
    api.listFxRates.mockResolvedValue(page([rate()]));

    renderPage();
    await user.click(await screen.findByRole("button", { name: "Edit" }));
    const form = topDialog();
    await user.clear(within(form).getByLabelText("Rate (MYR)"));
    await user.paste("4.0830000000");
    await user.click(buttonWithText("Review", form));

    expect(await screen.findByText("Nothing has been changed.")).toBeInTheDocument();
    expect(api.updateFxRate).not.toHaveBeenCalled();
  });

  it("shows the text when the backend refuses the edit as a BNM draft", async () => {
    const user = userEvent.setup();
    api.listFxRates.mockResolvedValue(page([rate()]));
    api.updateFxRate.mockRejectedValue(new ApiError("FX_RATE_NOT_EDITABLE", "BNM drafts cannot be edited.", "req-409"));

    renderPage();
    await user.click(await screen.findByRole("button", { name: "Edit" }));
    const form = topDialog();
    await user.clear(within(form).getByLabelText("Rate (MYR)"));
    await user.paste("4.2");
    await user.click(buttonWithText("Review", form));
    await user.click(await screen.findByText("Confirm and save"));

    expect(
      await screen.findByText("BNM drafts cannot be edited. Discard it and enter the rate by hand instead."),
    ).toBeInTheDocument();
    expect(screen.getByText("req-409")).toBeInTheDocument();
  });
});

describe("FxRatesPage publish, retire and discard", SLOW, () => {
  it("publishes a BNM draft now only after the confirmation, then refreshes", async () => {
    const user = userEvent.setup();
    api.listFxRates.mockResolvedValue(page([BNM_DRAFT]));
    api.publishFxRate.mockResolvedValue(PUBLISHED);

    renderPage();
    await user.click(await screen.findByRole("button", { name: "Publish" }));
    await user.click(buttonWithText("Review", topDialog()));

    expect(await screen.findByText("Publish this version?")).toBeInTheDocument();
    expect(api.publishFxRate).not.toHaveBeenCalled();
    await user.click(buttonWithText("Confirm and publish", topDialog()));

    expect(await screen.findByText("Published.")).toBeInTheDocument();
    expect(api.publishFxRate).toHaveBeenCalledWith(ID, undefined);
    await waitFor(() => expect(api.listFxRates).toHaveBeenCalledTimes(2));
  });

  it("shows the text for a conflicting schedule with the request id", async () => {
    const user = userEvent.setup();
    api.listFxRates.mockResolvedValue(page([BNM_DRAFT]));
    api.publishFxRate.mockRejectedValue(new ApiError("EFFECTIVE_FROM_CONFLICT", "Conflict.", "req-409"));

    renderPage();
    await user.click(await screen.findByRole("button", { name: "Publish" }));
    await user.click(buttonWithText("Review", topDialog()));
    await user.click(await screen.findByText("Confirm and publish"));

    expect(
      await screen.findByText(
        "The time does not fit the existing versions. If a scheduled version has not started yet, choose a later time or retire that version first.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("req-409")).toBeInTheDocument();
    expect(api.listFxRates).toHaveBeenCalledTimes(1);
  });

  it("retires with the reason only after the confirmation", async () => {
    const user = userEvent.setup();
    api.listFxRates.mockResolvedValue(page([PUBLISHED]));
    api.retireFxRate.mockResolvedValue({ ...PUBLISHED, status: "RETIRED" });

    renderPage();
    await user.click(await screen.findByRole("button", { name: "Retire" }));
    await user.click(screen.getByLabelText("Reason"));
    await user.paste("Wrong rate entered");
    await user.click(buttonWithText("Review", topDialog()));

    expect(await screen.findByText("Retire this version?")).toBeInTheDocument();
    expect(api.retireFxRate).not.toHaveBeenCalled();
    await user.click(buttonWithText("Confirm and retire", topDialog()));

    expect(await screen.findByText("Retired.")).toBeInTheDocument();
    expect(api.retireFxRate).toHaveBeenCalledWith(ID, "Wrong rate entered");
    await waitFor(() => expect(api.listFxRates).toHaveBeenCalledTimes(2));
  });

  it("shows the text and the request id for a rate that can no longer be retired, and does not refresh", async () => {
    const user = userEvent.setup();
    api.listFxRates.mockResolvedValue(page([PUBLISHED]));
    api.retireFxRate.mockRejectedValue(new ApiError("FX_RATE_NOT_RETIRABLE", "Cannot retire.", "req-409"));

    renderPage();
    await user.click(await screen.findByRole("button", { name: "Retire" }));
    await user.click(screen.getByLabelText("Reason"));
    await user.paste("Wrong rate entered");
    await user.click(buttonWithText("Review", topDialog()));
    await user.click(await screen.findByText("Confirm and retire"));

    expect(
      await screen.findByText("Only the latest published exchange rate can be retired. Drafts are discarded instead."),
    ).toBeInTheDocument();
    expect(screen.getByText("req-409")).toBeInTheDocument();
    expect(screen.queryByText("Retired.")).not.toBeInTheDocument();
    expect(api.listFxRates).toHaveBeenCalledTimes(1);
  });

  it("discards only after the confirmation, and shows the text when it was already published", async () => {
    const user = userEvent.setup();
    api.listFxRates.mockResolvedValue(page([BNM_DRAFT]));
    api.discardFxRate.mockRejectedValue(new ApiError("FX_RATE_NOT_DRAFT", "Not a draft.", "req-409"));

    renderPage();
    await user.click(await screen.findByRole("button", { name: "Discard" }));

    expect(await screen.findByText("Discard this draft?")).toBeInTheDocument();
    expect(api.discardFxRate).not.toHaveBeenCalled();
    await user.click(buttonWithText("Confirm and discard", topDialog()));

    expect(
      await screen.findByText(
        "This exchange rate has already been published, so it can no longer be edited or discarded.",
      ),
    ).toBeInTheDocument();
    expect(api.discardFxRate).toHaveBeenCalledWith(ID);
    expect(screen.getByText("req-409")).toBeInTheDocument();
  });

  it("discards and refreshes on success", async () => {
    const user = userEvent.setup();
    api.listFxRates.mockResolvedValue(page([BNM_DRAFT]));
    api.discardFxRate.mockResolvedValue({ ...BNM_DRAFT, status: "DISCARDED" });

    renderPage();
    await user.click(await screen.findByRole("button", { name: "Discard" }));
    await user.click(buttonWithText("Confirm and discard", topDialog()));

    expect(await screen.findByText("Draft discarded.")).toBeInTheDocument();
    await waitFor(() => expect(api.listFxRates).toHaveBeenCalledTimes(2));
  });
});
