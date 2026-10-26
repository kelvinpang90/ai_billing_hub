/**
 * 门户钱包页（AIH-TASK-100）：三种状态、发出去的游标、金额的显示精度、余额卡的文案。
 *
 * 这里不 mock `api/customerWallet`，而是在 `client.get` 上按路径回应：要钉住的正是**页面真正发出去的地址**——
 * 「Older」带的是不是响应给的 `next_before_sequence`、「Newest」是不是真的去掉了游标、有没有请求任何
 * `/api/v1/admin/` 路径。写错不会报错，页面照样显示一页，只是哪一页不对、或者问了不该问的接口。
 *
 * ⚠️ 金额那几条测的是设计 §2「金额（前端显示）」：余额与 `balance_after` 2 位，`AI_USAGE` 行金额最多 6 位，
 * 其余行金额 2 位，舍入后为零时不带负号。
 *
 * 导出区（AIH-TASK-118）同样在 `client.get` 上按路径回应：成功回一个 blob 文件，失败回一个响应体是 blob 的
 * `AxiosError`（与浏览器里 `responseType: "blob"` 的失败同形）。jsdom 没有对象 URL，也不会真下载：装上替身、拦下
 * 链接的点击，只记录下载的文件名。「现在」用 `vi.useFakeTimers({ toFake: ["Date"] })` 固定：只假 `Date`，定时器照常走。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AxiosError, AxiosHeaders, type AxiosResponse } from "axios";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import { ApiError, client } from "../../api/client";
import type { CustomerTransaction, CustomerTransactionPage, CustomerWallet } from "../../api/customerWallet";
import { ROUTES } from "../../routes/paths";
import { WalletPage } from "./WalletPage";

const WALLET_URL = "/api/v1/customer/wallet";
const TRANSACTIONS_URL = "/api/v1/customer/wallet/transactions";
const EXPORT_URL = "/api/v1/customer/wallet/transactions/export";
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
    low_balance_threshold: null,
    low_balance: false,
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
 * 按路径回应 `client.get`：钱包接口回 `walletReply`，流水接口按查询串交给 `transactions`，导出接口回 `exported()` 的
 * 结果（文件响应原样、不包信封；默认当成失败 —— 不点「Export CSV」的用例里页面不该问它）。别的路径一律当成失败 ——
 * 页面不该问它们，下面也会逐条断言没有问。
 */
function serve(
  walletReply: unknown,
  transactions: (query: URLSearchParams) => unknown,
  exported: () => unknown = () => new ApiError("NOT_FOUND", "Unexpected request.", null),
) {
  return vi.spyOn(client, "get").mockImplementation((url: string) => {
    const parsed = new URL(url, "https://portal.example");
    if (parsed.pathname === EXPORT_URL) {
      const file = exported();
      if (file === PENDING) {
        return new Promise(() => undefined);
      }
      return file instanceof Error ? Promise.reject(file) : Promise.resolve(file);
    }
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

describe("WalletPage low balance warning", () => {
  /** 余额卡（role=region，aria-label 为「Your wallet」）。 */
  function card(): HTMLElement {
    return screen.getByRole("region", { name: "Your wallet" });
  }

  it.each([
    ["50.00000000", "MYR 50.00"],
    ["1234.50500000", "MYR 1,234.51"],
  ])("warns inside the balance card when the balance is low, with a threshold of %s shown as %s", async (threshold, shown) => {
    serve(wallet({ balance: "12.45880000", low_balance_threshold: threshold, low_balance: true }), () => page([]));

    renderPage();

    await screen.findByText("MYR 12.46");
    const alert = within(card()).getByRole("alert");
    expect(within(alert).getByText("Low balance")).toBeInTheDocument();
    expect(alert.textContent ?? "").toContain(
      `Your balance is at or below your low balance alert of ${shown}. Please top up soon.`,
    );
    expect(alert.textContent ?? "").not.toContain(threshold);
    // 只说请充值，没有充值按钮或链接。
    expect(within(alert).queryByRole("button")).not.toBeInTheDocument();
    expect(within(alert).queryByRole("link")).not.toBeInTheDocument();
    // 卡的其余字段照常。
    expect(valueOf("Balance")).toBe("MYR 12.46");
    expect(valueOf("Billing status")).toBe("Active");
    expect(valueOf("AI services")).toBe("Available");
  });

  it("goes by low_balance alone, not by comparing the balance with the threshold", async () => {
    // 余额高于阈值，但后端说是低余额：照样提示（规则只在后端算）。
    serve(wallet({ balance: "80.00000000", low_balance_threshold: "50.00000000", low_balance: true }), () => page([]));

    renderPage();

    await screen.findByText("MYR 80.00");
    expect(within(card()).getByRole("alert")).toHaveTextContent("MYR 50.00");
  });

  it.each([
    ["no threshold is set", wallet({ low_balance_threshold: null, low_balance: false })],
    [
      "the threshold is set but the balance is above it",
      wallet({ balance: "80.00000000", low_balance_threshold: "50.00000000", low_balance: false }),
    ],
    [
      "the customer is internally metered",
      wallet({
        balance: "0.00000000",
        billing_mode: "INTERNAL_METERED_ONLY",
        billing_status: "SUSPENDED",
        low_balance_threshold: "50.00000000",
        low_balance: false,
      }),
    ],
  ])("shows no warning when %s", async (_, reply) => {
    serve(reply, () => page([]));

    const { container } = renderPage();
    await screen.findByText("You have no transactions yet.");
    await within(card()).findByText("Balance");

    expect(within(card()).queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByText("Low balance")).not.toBeInTheDocument();
    expect(container.textContent ?? "").not.toMatch(/low balance alert|top up|50\.00/i);
  });

  it("still asks only the two customer wallet endpoints when the balance is low", async () => {
    const get = serve(wallet({ low_balance_threshold: "50.00000000", low_balance: true }), () => page([]));

    renderPage();
    await screen.findByText("You have no transactions yet.");
    await within(card()).findByRole("alert");

    const urls = get.mock.calls.map(([url]) => url);
    for (const url of urls) {
      expect(url).not.toContain("/api/v1/admin/");
      expect(url === WALLET_URL || url.startsWith(TRANSACTIONS_URL)).toBe(true);
    }
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

describe("WalletPage export", () => {
  const EXPORT_NAME = /Export CSV/;
  const FROM = "From (Malaysia time)";
  const TO = "To (Malaysia time)";
  /** 固定的「现在」：UTC 2026-10-26 04:00，马来西亚时间 2026-10-26 12:00。 */
  const NOW = new Date("2026-10-26T04:00:00.000Z");
  const FILE_NAME = "wallet-transactions_2026-10-01_2026-10-26.csv";
  const originalCreate = Object.getOwnPropertyDescriptor(URL, "createObjectURL");
  const originalRevoke = Object.getOwnPropertyDescriptor(URL, "revokeObjectURL");
  /** 被点击的下载链接的文件名，按点击顺序。 */
  let downloads: string[];

  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(NOW);
    downloads = [];
    Object.defineProperty(URL, "createObjectURL", {
      configurable: true,
      writable: true,
      value: () => "blob:https://portal.example/00000000-0000-0000-0000-000000000000",
    });
    Object.defineProperty(URL, "revokeObjectURL", { configurable: true, writable: true, value: () => undefined });
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      downloads.push(this.download);
    });
  });

  afterEach(() => {
    vi.useRealTimers();
    for (const [name, descriptor] of [
      ["createObjectURL", originalCreate],
      ["revokeObjectURL", originalRevoke],
    ] as const) {
      if (descriptor === undefined) {
        Reflect.deleteProperty(URL, name);
      } else {
        Object.defineProperty(URL, name, descriptor);
      }
    }
  });

  function file(name: string = FILE_NAME) {
    return {
      data: new Blob(["﻿id,wallet_sequence\r\n"], { type: "text/csv" }),
      status: 200,
      statusText: "",
      headers: { "content-disposition": `attachment; filename="${name}"` },
    };
  }

  /** 后端的失败：响应体是装着 JSON 信封的 blob。 */
  function failure(status: number, code: string, requestId: string) {
    const body = JSON.stringify({ success: false, data: null, error: { code, message: "Safe message." }, request_id: requestId });
    const response = {
      data: new Blob([body], { type: "application/json" }),
      status,
      statusText: "",
      headers: {},
      config: { headers: new AxiosHeaders() },
    } satisfies AxiosResponse;
    return new AxiosError("Request failed", "ERR_BAD_REQUEST", undefined, undefined, response);
  }

  /** 发给导出接口的全部地址，按发出顺序。 */
  function exportUrls(get: ReturnType<typeof serve>): string[] {
    return get.mock.calls.map(([url]) => url).filter((url) => url.startsWith(EXPORT_URL));
  }

  /** 发给流水列表接口的全部地址，按发出顺序（不含导出）。 */
  function transactionUrls(get: ReturnType<typeof serve>): string[] {
    return get.mock.calls
      .map(([url]) => url)
      .filter((url) => url === TRANSACTIONS_URL || url.startsWith(`${TRANSACTIONS_URL}?`));
  }

  function setDate(label: string, value: string): void {
    fireEvent.change(screen.getByLabelText(label), { target: { value } });
  }

  it.each<[string, string, string]>([
    ["2026-10-26T04:00:00.000Z", "2026-10-01", "2026-10-26"],
    // UTC 还是 10 月 25 日，马来西亚时间已是 10 月 26 日。
    ["2026-10-25T16:30:00.000Z", "2026-10-01", "2026-10-26"],
    // UTC 16:00 之前：马来西亚时间还是 10 月 31 日。
    ["2026-10-31T15:59:59.999Z", "2026-10-01", "2026-10-31"],
    // UTC 16:00 起：马来西亚时间已是 11 月 1 日，本月只有这一天。
    ["2026-10-31T16:00:00.000Z", "2026-11-01", "2026-11-01"],
    // 跨年。
    ["2026-12-31T16:00:00.000Z", "2027-01-01", "2027-01-01"],
  ])("at %s defaults to %s through %s and exports that range", async (now, from, to) => {
    vi.setSystemTime(new Date(now));
    const user = userEvent.setup();
    const name = `wallet-transactions_${from}_${to}.csv`;
    const get = serve(wallet(), () => page([row()]), () => file(name));
    renderPage();
    await screen.findByText("AI usage");

    expect(screen.getByLabelText(FROM)).toHaveValue(from);
    expect(screen.getByLabelText(TO)).toHaveValue(to);
    expect(screen.getByRole("button", { name: EXPORT_NAME })).toBeEnabled();

    await user.click(screen.getByRole("button", { name: EXPORT_NAME }));

    await waitFor(() => {
      expect(downloads).toEqual([name]);
    });
    expect(exportUrls(get)).toEqual([`${EXPORT_URL}?from_date=${from}&to_date=${to}`]);
  });

  it("puts the export area above the transactions, as its own region", async () => {
    serve(wallet(), () => page([row()]));
    renderPage();
    await screen.findByText("AI usage");

    const area = screen.getByRole("region", { name: "Export transactions" });
    const transactions = screen.getByRole("region", { name: "Transactions" });
    expect(within(area).getByRole("button", { name: EXPORT_NAME })).toBeEnabled();
    expect(within(transactions).queryByRole("button", { name: EXPORT_NAME })).not.toBeInTheDocument();
    expect(area.compareDocumentPosition(transactions) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("limits the date pickers to 2020-01-01 through 2099-12-31", async () => {
    serve(wallet(), () => page([]));
    renderPage();
    await screen.findByText("You have no transactions yet.");

    for (const label of [FROM, TO]) {
      expect(screen.getByLabelText(label)).toHaveAttribute("min", "2020-01-01");
      expect(screen.getByLabelText(label)).toHaveAttribute("max", "2099-12-31");
    }
  });

  it("exports a chosen range of exactly 92 days, with only the two dates", async () => {
    const user = userEvent.setup();
    const get = serve(wallet(), () => page([row()]), () => file("wallet-transactions_2026-07-27_2026-10-26.csv"));
    renderPage();
    await screen.findByText("AI usage");

    setDate(FROM, "2026-07-27");

    expect(screen.getByRole("button", { name: EXPORT_NAME })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: EXPORT_NAME }));

    await waitFor(() => {
      expect(downloads).toEqual(["wallet-transactions_2026-07-27_2026-10-26.csv"]);
    });
    expect(exportUrls(get)).toEqual([`${EXPORT_URL}?from_date=2026-07-27&to_date=2026-10-26`]);
    const [, config] = get.mock.calls.find(([url]) => url.startsWith(EXPORT_URL)) ?? [];
    expect(config).toMatchObject({ responseType: "blob" });
  });

  it.each<[string, string, string, string]>([
    ["reversed", "2026-10-20", "2026-10-10", "The end date cannot be before the start date."],
    ["longer than 92 days", "2026-07-26", "2026-10-26", "Choose a date range of 92 days or less."],
    ["incomplete", "", "2026-10-10", "Choose both a start date and an end date."],
    ["before 2020-01-01", "2019-12-31", "2020-01-31", "Choose dates between 2020-01-01 and 2099-12-31."],
  ])("cannot export when the range is %s", async (_, from, to, text) => {
    const get = serve(wallet(), () => page([row()]), () => file());
    renderPage();
    await screen.findByText("AI usage");
    expect(screen.getByRole("button", { name: EXPORT_NAME })).toBeEnabled();

    setDate(FROM, from);
    setDate(TO, to);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: EXPORT_NAME })).toBeDisabled();
    });
    expect(screen.getByText(text)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: EXPORT_NAME }));
    expect(exportUrls(get)).toEqual([]);
    expect(downloads).toEqual([]);
    // 流水照常。
    expect(screen.getByText("AI usage")).toBeInTheDocument();
  });

  it("shows the button loading while exporting, does not export twice, and leaves paging alone", async () => {
    const user = userEvent.setup();
    const get = serve(
      wallet(),
      (query) =>
        query.has("before_sequence")
          ? page([row({ id: EVENT_ID, wallet_sequence: 22, transaction_type: "BONUS", amount: "1.00000000", event_id: null })])
          : page([row()], 23),
      () => PENDING,
    );
    renderPage();
    await screen.findByText("AI usage");

    await user.click(screen.getByRole("button", { name: EXPORT_NAME }));

    await waitFor(() => {
      expect(screen.getByRole("button", { name: EXPORT_NAME })).toBeDisabled();
    });
    const button = screen.getByRole("button", { name: EXPORT_NAME });
    expect(within(button).getByRole("img", { name: "loading" })).toBeInTheDocument();
    fireEvent.click(button);
    fireEvent.click(button);
    expect(exportUrls(get)).toHaveLength(1);
    expect(downloads).toEqual([]);

    // 导出中照常翻页。
    await user.click(screen.getByRole("button", { name: "Older" }));
    expect(await screen.findByText("Bonus")).toBeInTheDocument();
    expect(transactionUrls(get).at(-1)).toBe(`${TRANSACTIONS_URL}?page_size=20&before_sequence=23`);
    expect(exportUrls(get)).toHaveLength(1);
  });

  it("does not ask for the transactions again when exporting", async () => {
    const user = userEvent.setup();
    const get = serve(wallet(), () => page([row()], 23), () => file());
    renderPage();
    await screen.findByText("AI usage");
    const listed = transactionUrls(get).length;

    await user.click(screen.getByRole("button", { name: EXPORT_NAME }));

    await waitFor(() => {
      expect(downloads).toEqual([FILE_NAME]);
    });
    expect(transactionUrls(get)).toHaveLength(listed);
    expect(screen.getByText("AI usage")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Newest" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Older" })).toBeEnabled();
    expect(screen.getByRole("button", { name: EXPORT_NAME })).toBeEnabled();
    for (const [url] of get.mock.calls) {
      expect(url).not.toContain("/api/v1/admin/");
      expect(url).not.toMatch(/tenant|customer_id/);
      expect(url === WALLET_URL || url.startsWith(TRANSACTIONS_URL)).toBe(true);
    }
  });

  it.each<[number, string, string]>([
    [422, "EXPORT_TOO_LARGE", "This export has more than 20,000 rows. Choose a shorter date range, then try again."],
    [
      422,
      "QUERY_TOO_BROAD",
      "This export covers too many transactions to finish in time. Choose a shorter date range, then try again.",
    ],
    [503, "EXPORT_BUSY", "Another export is in progress. Wait a few seconds, then try again."],
  ])("explains a %s %s in its own words", async (status, code, text) => {
    const user = userEvent.setup();
    serve(wallet(), () => page([row()]), () => failure(status, code, "req-export-specific"));
    renderPage();
    await screen.findByText("AI usage");

    await user.click(screen.getByRole("button", { name: EXPORT_NAME }));

    expect(await screen.findByText(text)).toBeInTheDocument();
    expect(screen.queryByText("Your transactions could not be exported.")).not.toBeInTheDocument();
    expect(screen.queryByText("req-export-specific")).not.toBeInTheDocument();
    // 流水不受影响，按钮可以再点。
    expect(screen.getByText("AI usage")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: EXPORT_NAME })).toBeEnabled();
    expect(downloads).toEqual([]);
  });

  it("shows any other failure with its request id", async () => {
    const user = userEvent.setup();
    serve(wallet(), () => page([row()]), () => failure(500, "INTERNAL_ERROR", "req-export-500"));
    renderPage();
    await screen.findByText("AI usage");

    await user.click(screen.getByRole("button", { name: EXPORT_NAME }));

    expect(await screen.findByText("Your transactions could not be exported.")).toBeInTheDocument();
    expect(screen.getByText("req-export-500")).toBeInTheDocument();
    expect(screen.queryByText("Your transactions could not be loaded.")).not.toBeInTheDocument();
    expect(downloads).toEqual([]);
  });

  it("clears an old failure when the range changes", async () => {
    const user = userEvent.setup();
    serve(wallet(), () => page([row()]), () => failure(500, "INTERNAL_ERROR", "req-export-500"));
    renderPage();
    await screen.findByText("AI usage");
    await user.click(screen.getByRole("button", { name: EXPORT_NAME }));
    await screen.findByText("Your transactions could not be exported.");

    setDate(FROM, "2026-10-02");

    await waitFor(() => {
      expect(screen.queryByText("Your transactions could not be exported.")).not.toBeInTheDocument();
    });
    expect(screen.getByRole("button", { name: EXPORT_NAME })).toBeEnabled();
  });
});
