/**
 * 钱包流水面板：三种状态、发出去的游标与筛选、用量行的链接、金额原样显示。
 *
 * ⚠️ 翻页那几条测的是**发出去的参数**：「Older」带的是不是响应给的 `next_before_sequence`、「Newest」
 * 与换来源是不是真的去掉了游标。写错不会报错 —— 页面照样显示一页，只是哪一页不对。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import { ApiError } from "../../api/client";
import type { WalletTransaction, WalletTransactionPage } from "../../api/walletTransactions";
import { WalletTransactionsPanel } from "./WalletTransactionsPanel";

const CUSTOMER_ID = "00000000-0000-0000-0000-000000000000";
const USAGE_EVENT_ID = "00000000-0000-0000-0000-000000000003";

const api = vi.hoisted(() => ({
  listWalletTransactions: vi.fn(),
}));

vi.mock("../../api/walletTransactions", async () => {
  const actual = await vi.importActual<typeof import("../../api/walletTransactions")>(
    "../../api/walletTransactions",
  );
  return { ...actual, ...api };
});

function row(overrides: Partial<WalletTransaction> = {}): WalletTransaction {
  return {
    id: "00000000-0000-0000-0000-000000000001",
    wallet_sequence: 42,
    transaction_type: "AI_USAGE",
    amount: "-0.04120000",
    balance_before: "12.50000000",
    balance_after: "12.45880000",
    reference_type: "USAGE_EVENT",
    reference_id: "00000000-0000-0000-0000-000000000002",
    usage_event_id: USAGE_EVENT_ID,
    description: null,
    created_by_email: null,
    created_at: "2026-10-18T08:30:00",
    ...overrides,
  };
}

function page(items: WalletTransaction[], next: number | null = null): WalletTransactionPage {
  return { items, page_size: 20, next_before_sequence: next };
}

function renderPanel() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <WalletTransactionsPanel customerId={CUSTOMER_ID} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** 第 n 次请求的查询参数（第二个实参）。 */
function query(call: number): unknown {
  return api.listWalletTransactions.mock.calls[call]?.[1];
}

/** antd 下拉在 mousedown 时打开；选项按 title 找、用 fireEvent 点（同定价规则页的用例）。 */
async function chooseSource(option: string): Promise<void> {
  fireEvent.mouseDown(screen.getByLabelText("Source"));
  const candidates = await screen.findAllByTitle(option);
  const item =
    candidates.find((candidate) => candidate.closest(".ant-select-item") !== null) ?? candidates[0];
  if (item === undefined) {
    throw new Error(`no option "${option}"`);
  }
  fireEvent.click(item);
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("WalletTransactionsPanel", () => {
  it("says it is loading while the first page is on its way", () => {
    api.listWalletTransactions.mockReturnValue(new Promise(() => undefined));

    renderPanel();

    expect(screen.getByText("Loading wallet transactions…")).toBeInTheDocument();
  });

  it("shows a failure with the backend message and request id", async () => {
    api.listWalletTransactions.mockRejectedValue(
      new ApiError("INTERNAL_ERROR", "An unexpected error occurred.", "req-500"),
    );

    renderPanel();

    expect(
      await screen.findByText("The wallet transactions could not be loaded."),
    ).toBeInTheDocument();
    expect(screen.getByText(/An unexpected error occurred\./)).toBeInTheDocument();
    expect(screen.getByText("req-500")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Older" })).toBeDisabled();
  });

  it("says the customer has no transactions yet", async () => {
    api.listWalletTransactions.mockResolvedValue(page([]));

    renderPanel();

    expect(
      await screen.findByText("This customer has no wallet transactions yet."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("asks for the newest page first, without a cursor or a source", async () => {
    api.listWalletTransactions.mockResolvedValue(page([row()]));

    renderPanel();

    await screen.findByText("42");
    expect(api.listWalletTransactions).toHaveBeenCalledTimes(1);
    expect(api.listWalletTransactions).toHaveBeenCalledWith(
      CUSTOMER_ID,
      { pageSize: 20, beforeSequence: null, referenceType: null },
      expect.anything(),
    );
    expect(screen.getByRole("button", { name: "Newest" })).toBeDisabled();
  });

  it("pages older with next_before_sequence and back to the newest page", async () => {
    const user = userEvent.setup();
    api.listWalletTransactions.mockImplementation(
      (_customerId: string, { beforeSequence }: { beforeSequence: number | null }) =>
        Promise.resolve(
          beforeSequence === null
            ? page([row({ wallet_sequence: 42 })], 23)
            : page([row({ id: "00000000-0000-0000-0000-000000000004", wallet_sequence: 22 })]),
        ),
    );
    renderPanel();

    await screen.findByText("42");
    await user.click(screen.getByRole("button", { name: "Older" }));

    expect(await screen.findByText("22")).toBeInTheDocument();
    expect(query(1)).toEqual({ pageSize: 20, beforeSequence: 23, referenceType: null });
    // 最后一页：没有更早的了。
    expect(screen.getByRole("button", { name: "Older" })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "Newest" }));

    expect(await screen.findByText("42")).toBeInTheDocument();
    await waitFor(() => {
      expect(api.listWalletTransactions).toHaveBeenLastCalledWith(
        CUSTOMER_ID,
        { pageSize: 20, beforeSequence: null, referenceType: null },
        expect.anything(),
      );
    });
    expect(screen.getByRole("button", { name: "Newest" })).toBeDisabled();
  });

  it("goes back to the newest page when the source changes", async () => {
    const user = userEvent.setup();
    api.listWalletTransactions.mockResolvedValueOnce(page([row({ wallet_sequence: 42 })], 23));
    api.listWalletTransactions.mockResolvedValueOnce(
      page([row({ id: "00000000-0000-0000-0000-000000000004", wallet_sequence: 22 })], 3),
    );
    api.listWalletTransactions.mockResolvedValue(page([]));
    renderPanel();

    await screen.findByText("42");
    await user.click(screen.getByRole("button", { name: "Older" }));
    await screen.findByText("22");

    await chooseSource("Adjustment");

    expect(
      await screen.findByText("No wallet transactions from this source."),
    ).toBeInTheDocument();
    expect(query(2)).toEqual({
      pageSize: 20,
      beforeSequence: null,
      referenceType: "ADMIN_ADJUSTMENT",
    });

    await chooseSource("All sources");

    await waitFor(() => {
      expect(api.listWalletTransactions).toHaveBeenLastCalledWith(
        CUSTOMER_ID,
        { pageSize: 20, beforeSequence: null, referenceType: null },
        expect.anything(),
      );
    });
  });

  it("offers all sources and the five reference types", async () => {
    api.listWalletTransactions.mockResolvedValue(page([]));
    renderPanel();
    await screen.findByText("This customer has no wallet transactions yet.");

    fireEvent.mouseDown(screen.getByLabelText("Source"));

    for (const option of [
      "All sources",
      "AI usage",
      "Top-up",
      "Adjustment",
      "Rebill correction",
      "System correction",
    ]) {
      expect((await screen.findAllByTitle(option)).length).toBeGreaterThan(0);
    }
  });

  it("links an AI usage row to its usage event, and only when it has one", async () => {
    api.listWalletTransactions.mockResolvedValue(
      page([
        row({ wallet_sequence: 42 }),
        row({ id: "00000000-0000-0000-0000-000000000005", wallet_sequence: 41, usage_event_id: null }),
        row({
          id: "00000000-0000-0000-0000-000000000006",
          wallet_sequence: 40,
          transaction_type: "ADJUSTMENT_CREDIT",
          reference_type: "ADMIN_ADJUSTMENT",
          usage_event_id: null,
          description: "Goodwill credit",
          created_by_email: "admin@example.com",
        }),
      ]),
    );

    renderPanel();

    await screen.findByText("42");
    const links = screen.getAllByRole("link", { name: "View usage event" });
    expect(links).toHaveLength(1);
    expect(links[0]).toHaveAttribute("href", `/usage-events/${USAGE_EVENT_ID}`);
    expect(screen.getAllByText("AI usage").length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText("Goodwill credit")).toBeInTheDocument();
    expect(screen.getByText("admin@example.com")).toBeInTheDocument();
    expect(screen.getByText("ADJUSTMENT_CREDIT")).toBeInTheDocument();
  });

  it("shows amounts and balances exactly as the decimal strings the backend sent", async () => {
    api.listWalletTransactions.mockResolvedValue(
      page([
        row({ amount: "-0.00000001", balance_after: "999999999999.99999999" }),
        row({
          id: "00000000-0000-0000-0000-000000000007",
          wallet_sequence: 41,
          amount: "-0.04120000",
          balance_after: "12.45880000",
        }),
      ]),
    );

    renderPanel();

    await screen.findByText("42");
    // 经过 number 的话：`999999999999.99999999` 会变成 1000000000000，末尾的 0 也会被去掉。
    expect(screen.getByText("-0.00000001")).toBeInTheDocument();
    expect(screen.getByText("999999999999.99999999")).toBeInTheDocument();
    expect(screen.getByText("-0.04120000")).toBeInTheDocument();
    expect(screen.getByText("12.45880000")).toBeInTheDocument();
    expect(screen.queryByText("1000000000000")).not.toBeInTheDocument();
    expect(screen.queryByText("12.4588")).not.toBeInTheDocument();
  });
});
