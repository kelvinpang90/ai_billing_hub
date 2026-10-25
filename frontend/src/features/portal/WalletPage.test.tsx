/**
 * 门户钱包页（AIH-TASK-100）：三种状态、发出去的游标、金额的显示精度、余额卡的文案。
 *
 * 这里不 mock `api/customerWallet`，而是在 `client.get` 上按路径回应：要钉住的正是**页面真正发出去的地址**——
 * 「Older」带的是不是响应给的 `next_before_sequence`、「Newest」是不是真的去掉了游标、有没有请求任何
 * `/api/v1/admin/` 路径。写错不会报错，页面照样显示一页，只是哪一页不对、或者问了不该问的接口。
 *
 * ⚠️ 金额那几条测的是设计 §2「金额（前端显示）」：余额与 `balance_after` 2 位，`AI_USAGE` 行金额最多 6 位，
 * 其余行金额 2 位，舍入后为零时不带负号。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import { ApiError, client } from "../../api/client";
import type { CustomerTransaction, CustomerTransactionPage, CustomerWallet } from "../../api/customerWallet";
import { ROUTES } from "../../routes/paths";
import { WalletPage } from "./WalletPage";

const WALLET_URL = "/api/v1/customer/wallet";
const TRANSACTIONS_URL = "/api/v1/customer/wallet/transactions";
const EVENT_ID = "00000000-0000-0000-0000-000000000000";

/** 让请求一直挂着（加载中）。 */
const PENDING = Symbol("pending");

function wallet(overrides: Partial<CustomerWallet> = {}): CustomerWallet {
  return {
    currency: "MYR",
    balance: "12.45880000",
    billing_mode: "PREPAID",
    billing_status: "ACTIVE",
    ai_service: "ALLOW_AI",
    ai_service_reason: null,
    ...overrides,
  };
}

function row(overrides: Partial<CustomerTransaction> = {}): CustomerTransaction {
  return {
    id: "00000000-0000-0000-0000-000000000001",
    wallet_sequence: 42,
    transaction_type: "AI_USAGE",
    amount: "-0.04120000",
    balance_after: "12.45880000",
    event_id: EVENT_ID,
    created_at: "2026-10-18T08:30:00",
    ...overrides,
  };
}

function page(items: CustomerTransaction[], next: number | null = null): CustomerTransactionPage {
  return { items, page_size: 20, next_before_sequence: next };
}

function envelope(data: unknown) {
  return { data: { success: true, data, error: null, request_id: "req-1" } };
}

/**
 * 按路径回应 `client.get`：钱包接口回 `walletReply`，流水接口按查询串交给 `transactions`。别的路径一律当成失败 ——
 * 页面不该问它们，下面也会逐条断言没有问。
 */
function serve(walletReply: unknown, transactions: (query: URLSearchParams) => unknown) {
  return vi.spyOn(client, "get").mockImplementation((url: string) => {
    const parsed = new URL(url, "https://portal.example");
    let reply: unknown;
    if (parsed.pathname === WALLET_URL) {
      reply = walletReply;
    } else if (parsed.pathname === TRANSACTIONS_URL) {
      reply = transactions(parsed.searchParams);
    } else {
      reply = new ApiError("NOT_FOUND", "Unexpected request.", null);
    }
    if (reply === PENDING) {
      return new Promise(() => undefined);
    }
    return reply instanceof Error ? Promise.reject(reply) : Promise.resolve(envelope(reply));
  });
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[ROUTES.portalWallet]}>
        <WalletPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** 余额卡（带边框的 Descriptions）：label 在 <th>，值在紧跟的 <td>。 */
function valueOf(label: string): string | null {
  const th = screen.getByText(label).closest("th");
  return th?.nextElementSibling?.textContent ?? null;
}

/** 流水表里某一行（`data-row-key` 是行的 id）各列的文字：时间、类型、金额、余额（后）、event_id。 */
function cellsOf(container: HTMLElement, id: string): string[] {
  const tr = container.querySelector(`tr[data-row-key="${id}"]`);
  if (tr === null) {
    throw new Error(`no row ${id}`);
  }
  return Array.from(tr.querySelectorAll(":scope > td"), (td) => td.textContent ?? "");
}

const AMOUNT = 2;
const BALANCE_AFTER = 3;
const EVENT = 4;
const TYPE = 1;

afterEach(() => {
  vi.restoreAllMocks();
});

describe("WalletPage states", () => {
  it("says it is loading while the wallet and the first page are on their way", async () => {
    serve(PENDING, () => PENDING);

    renderPage();

    expect(await screen.findByText("Loading your wallet…")).toBeInTheDocument();
    expect(screen.getByText("Loading your transactions…")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Older" })).not.toBeInTheDocument();
  });

  it("shows each failure with its request id", async () => {
    serve(
      new ApiError("INTERNAL_ERROR", "An unexpected error occurred.", "req-wallet-500"),
      () => new ApiError("INTERNAL_ERROR", "An unexpected error occurred.", "req-transactions-500"),
    );

    renderPage();

    expect(await screen.findByText("Your wallet could not be loaded.")).toBeInTheDocument();
    expect(await screen.findByText("Your transactions could not be loaded.")).toBeInTheDocument();
    expect(screen.getByText("req-wallet-500")).toBeInTheDocument();
    expect(screen.getByText("req-transactions-500")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Try again" })).toHaveLength(2);
    expect(screen.queryByRole("button", { name: "Older" })).not.toBeInTheDocument();
  });

  it("says there are no transactions yet, without a table or an Older button", async () => {
    serve(wallet({ balance: "0.00000000", billing_status: "SUSPENDED" }), () => page([]));

    renderPage();

    expect(await screen.findByText("You have no transactions yet.")).toBeInTheDocument();
    // 余额卡的 Descriptions 本身也画成 <table>：只看流水这一块。
    const section = screen.getByRole("region", { name: "Transactions" });
    expect(within(section).queryByRole("table")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Older" })).not.toBeInTheDocument();
  });
});

describe("WalletPage paging", () => {
  it("asks for the newest page first, without a cursor", async () => {
    const get = serve(wallet(), () => page([row()], 23));

    renderPage();

    expect(await screen.findByText("AI usage")).toBeInTheDocument();
    const urls = get.mock.calls.map(([url]) => url);
    expect(urls).toContain(WALLET_URL);
    expect(urls).toContain(`${TRANSACTIONS_URL}?page_size=20`);
    expect(screen.getByRole("button", { name: "Newest" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Older" })).toBeEnabled();
  });

  it("pages older with next_before_sequence, has no Older on the last page, and goes back to the newest page", async () => {
    const user = userEvent.setup();
    const get = serve(wallet(), (query) =>
      query.has("before_sequence")
        ? page([row({ id: "00000000-0000-0000-0000-000000000004", wallet_sequence: 22, transaction_type: "ADJUSTMENT_CREDIT", amount: "5.00000000", event_id: null })])
        : page([row()], 23),
    );
    renderPage();

    await screen.findByText("AI usage");
    await user.click(screen.getByRole("button", { name: "Older" }));

    expect(await screen.findByText("Credit adjustment")).toBeInTheDocument();
    expect(get).toHaveBeenLastCalledWith(`${TRANSACTIONS_URL}?page_size=20&before_sequence=23`, expect.anything());
    // 最后一页：没有更早的了，按钮不出现。
    await waitFor(() => {
      expect(screen.queryByRole("button", { name: "Older" })).not.toBeInTheDocument();
    });
    expect(screen.getByRole("button", { name: "Newest" })).toBeEnabled();

    await user.click(screen.getByRole("button", { name: "Newest" }));

    expect(await screen.findByText("AI usage")).toBeInTheDocument();
    await waitFor(() => {
      expect(get).toHaveBeenLastCalledWith(`${TRANSACTIONS_URL}?page_size=20`, expect.anything());
    });
    expect(screen.getByRole("button", { name: "Newest" })).toBeDisabled();
    expect(await screen.findByRole("button", { name: "Older" })).toBeEnabled();
  });

  it("says there are no older transactions when an older page comes back empty", async () => {
    const user = userEvent.setup();
    serve(wallet(), (query) => (query.has("before_sequence") ? page([]) : page([row()], 1)));
    renderPage();

    await screen.findByText("AI usage");
    await user.click(screen.getByRole("button", { name: "Older" }));

    expect(await screen.findByText("There are no older transactions.")).toBeInTheDocument();
  });

  it("never asks an admin endpoint, only the two customer wallet endpoints", async () => {
    const user = userEvent.setup();
    const get = serve(wallet(), (query) => (query.has("before_sequence") ? page([]) : page([row()], 23)));
    renderPage();

    await screen.findByText("AI usage");
    await user.click(screen.getByRole("button", { name: "Older" }));
    await screen.findByText("There are no older transactions.");

    const urls = get.mock.calls.map(([url]) => url);
    expect(urls.length).toBeGreaterThan(0);
    for (const url of urls) {
      expect(url).not.toContain("/api/v1/admin/");
      expect(url === WALLET_URL || url.startsWith(TRANSACTIONS_URL)).toBe(true);
      expect(url).not.toMatch(/tenant|customer_id/);
    }
  });
});

describe("WalletPage amounts", () => {
  it("shows the balance with the currency first, rounded to 2 places", async () => {
    serve(wallet({ balance: "12.45880000" }), () => page([]));

    renderPage();

    await screen.findByText("You have no transactions yet.");
    expect(await screen.findByText("MYR 12.46")).toBeInTheDocument();
    expect(valueOf("Balance")).toBe("MYR 12.46");
  });

  it.each([
    ["-3.20000000", "MYR -3.20"],
    ["-0.00400000", "MYR 0.00"],
    ["999999999999.99999999", "MYR 1,000,000,000,000.00"],
  ])("shows a balance of %s as %s", async (balance, shown) => {
    serve(wallet({ balance, billing_status: "SUSPENDED" }), () => page([]));

    renderPage();

    expect(await screen.findByText(shown)).toBeInTheDocument();
    expect(valueOf("Balance")).toBe(shown);
  });

  it("shows AI usage amounts with up to 6 places and everything else with 2", async () => {
    serve(wallet(), () =>
      page([
        row({ id: "00000000-0000-0000-0000-000000000001", amount: "-0.04123457", balance_after: "12.45880000" }),
        row({
          id: "00000000-0000-0000-0000-000000000002",
          wallet_sequence: 41,
          transaction_type: "ADJUSTMENT_CREDIT",
          amount: "5.00500000",
          balance_after: "12.50003457",
          event_id: null,
        }),
        row({
          id: "00000000-0000-0000-0000-000000000003",
          wallet_sequence: 40,
          amount: "-0.00000049",
          balance_after: "7.49503457",
        }),
        row({
          id: "00000000-0000-0000-0000-000000000005",
          wallet_sequence: 39,
          transaction_type: "ADJUSTMENT_DEBIT",
          amount: "-0.00400000",
          balance_after: "-0.00400000",
          event_id: null,
        }),
      ]),
    );

    const { container } = renderPage();
    await screen.findByText("Credit adjustment");

    const usage = cellsOf(container, "00000000-0000-0000-0000-000000000001");
    expect(usage[AMOUNT]).toBe("-0.041235");
    expect(usage[BALANCE_AFTER]).toBe("12.46");

    const credit = cellsOf(container, "00000000-0000-0000-0000-000000000002");
    expect(credit[AMOUNT]).toBe("5.01");
    expect(credit[BALANCE_AFTER]).toBe("12.50");

    // 舍入后为零：显示 0.00，不带负号。
    const tiny = cellsOf(container, "00000000-0000-0000-0000-000000000003");
    expect(tiny[AMOUNT]).toBe("0.00");
    expect(tiny[BALANCE_AFTER]).toBe("7.50");

    const debit = cellsOf(container, "00000000-0000-0000-0000-000000000005");
    expect(debit[AMOUNT]).toBe("0.00");
    expect(debit[BALANCE_AFTER]).toBe("0.00");

    // 原样的 8 位字符串不出现：显示的是舍入后的值。
    expect(screen.queryByText("-0.04123457")).not.toBeInTheDocument();
    expect(screen.queryByText("12.45880000")).not.toBeInTheDocument();
  });
});

describe("WalletPage wallet summary", () => {
  it("shows the billing status of a prepaid customer", async () => {
    serve(wallet({ balance: "0.00000000", billing_status: "SUSPENDED", ai_service: "BLOCK_AI", ai_service_reason: "BALANCE_NOT_POSITIVE" }), () => page([]));

    renderPage();

    await screen.findByText("MYR 0.00");
    expect(valueOf("Billing status")).toBe("Suspended");
    expect(valueOf("AI services")).toBe("Unavailable");
  });

  it("shows an active prepaid customer as active with AI available and no reason", async () => {
    serve(wallet(), () => page([]));

    renderPage();

    await screen.findByText("MYR 12.46");
    expect(valueOf("Billing status")).toBe("Active");
    expect(valueOf("AI services")).toBe("Available");
    expect(screen.queryByText("Reason")).not.toBeInTheDocument();
  });

  it("does not say anything is suspended or paused for an internally metered customer", async () => {
    serve(
      wallet({
        balance: "0.00000000",
        billing_mode: "INTERNAL_METERED_ONLY",
        billing_status: "SUSPENDED",
        ai_service: "ALLOW_AI",
        ai_service_reason: null,
      }),
      () => page([]),
    );

    const { container } = renderPage();
    await screen.findByText("MYR 0.00");

    expect(screen.queryByText("Billing status")).not.toBeInTheDocument();
    expect(valueOf("AI services")).toBe("Available");
    expect(container.textContent ?? "").not.toMatch(/suspend|pause|unavailable/i);
  });

  it.each([
    ["ACCOUNT_CLOSED", "Your account is closed."],
    ["ACCOUNT_DISABLED", "Your account is disabled. Contact Acuven if you have questions."],
    ["ACCOUNT_PENDING_ACTIVATION", "Your account has not been activated yet."],
    ["BALANCE_NOT_POSITIVE", "Your balance is not above zero, so AI services are paused."],
  ])("explains why AI services are blocked: %s", async (reason, text) => {
    serve(wallet({ ai_service: "BLOCK_AI", ai_service_reason: reason }), () => page([]));

    renderPage();

    await screen.findByText("MYR 12.46");
    expect(valueOf("AI services")).toBe("Unavailable");
    expect(valueOf("Reason")).toBe(text);
  });

  it("shows values it does not recognise as they are", async () => {
    serve(
      wallet({ billing_status: "FROZEN", ai_service: "THROTTLE_AI", ai_service_reason: "PROJECT_INTEGRATION_DISABLED" }),
      () => page([]),
    );

    renderPage();

    await screen.findByText("MYR 12.46");
    expect(valueOf("Billing status")).toBe("FROZEN");
    expect(valueOf("AI services")).toBe("THROTTLE_AI");
    expect(valueOf("Reason")).toBe("PROJECT_INTEGRATION_DISABLED");
  });
});

describe("WalletPage transaction rows", () => {
  it("has a fixed label for each of the nine transaction types and shows an unknown type as it is", async () => {
    const types: [string, string][] = [
      ["TOPUP", "Top-up"],
      ["AI_USAGE", "AI usage"],
      ["ADJUSTMENT_CREDIT", "Credit adjustment"],
      ["ADJUSTMENT_DEBIT", "Debit adjustment"],
      ["REBILL_CREDIT", "Rebill credit"],
      ["REBILL_DEBIT", "Rebill debit"],
      ["REFUND_ADJUSTMENT", "Refund adjustment"],
      ["BONUS", "Bonus"],
      ["SYSTEM_CORRECTION", "System correction"],
      ["CHARGEBACK", "CHARGEBACK"],
    ];
    const ids = types.map((_, index) => `00000000-0000-0000-0000-${String(index + 1).padStart(12, "0")}`);
    serve(wallet(), () =>
      page(
        types.map(([type], index) =>
          row({
            id: ids[index] ?? "",
            wallet_sequence: 50 - index,
            transaction_type: type,
            event_id: type === "AI_USAGE" ? EVENT_ID : null,
          }),
        ),
      ),
    );

    const { container } = renderPage();
    await screen.findByText("System correction");

    types.forEach(([, label], index) => {
      expect(cellsOf(container, ids[index] ?? "")[TYPE]).toBe(label);
    });
  });

  it("shows the event id of an AI usage row and leaves it empty when there is none", async () => {
    serve(wallet(), () =>
      page([
        row({ id: "00000000-0000-0000-0000-000000000001" }),
        row({
          id: "00000000-0000-0000-0000-000000000002",
          wallet_sequence: 41,
          transaction_type: "BONUS",
          amount: "1.00000000",
          event_id: null,
        }),
      ]),
    );

    const { container } = renderPage();
    await screen.findByText("Bonus");

    expect(cellsOf(container, "00000000-0000-0000-0000-000000000001")[EVENT]).toBe(EVENT_ID);
    expect(cellsOf(container, "00000000-0000-0000-0000-000000000002")[EVENT]).toBe("");
  });

  it("links the event id to the usage page with only event_id in the query, and has no link without one", async () => {
    serve(wallet(), () =>
      page([
        row({ id: "00000000-0000-0000-0000-000000000001" }),
        row({
          id: "00000000-0000-0000-0000-000000000002",
          wallet_sequence: 41,
          transaction_type: "BONUS",
          amount: "1.00000000",
          event_id: null,
        }),
      ]),
    );

    const { container } = renderPage();
    await screen.findByText("Bonus");

    const usageRow = container.querySelector<HTMLElement>('tr[data-row-key="00000000-0000-0000-0000-000000000001"]');
    const bonusRow = container.querySelector<HTMLElement>('tr[data-row-key="00000000-0000-0000-0000-000000000002"]');
    if (usageRow === null || bonusRow === null) {
      throw new Error("rows missing");
    }
    const link = within(usageRow).getByRole("link", { name: EVENT_ID });
    const href = new URL(link.getAttribute("href") ?? "", "https://portal.example");
    expect(href.pathname).toBe(ROUTES.portalUsage);
    expect(Array.from(href.searchParams.entries())).toEqual([["event_id", EVENT_ID]]);
    expect(link).toHaveAttribute("href", `${ROUTES.portalUsage}?event_id=${EVENT_ID}`);
    expect(within(bonusRow).queryByRole("link")).not.toBeInTheDocument();
  });

  it("shows the columns a customer needs and no internal one", async () => {
    serve(wallet(), () => page([row()]));

    const { container } = renderPage();
    await screen.findByText("AI usage");

    const heads = Array.from(container.querySelectorAll("thead th"), (th) => th.textContent);
    expect(heads).toEqual(["Time", "Type", "Amount", "Balance after", "Event ID"]);
    expect(container.textContent ?? "").not.toMatch(/cost|margin|description|operator|sequence|tenant/i);
  });
});
