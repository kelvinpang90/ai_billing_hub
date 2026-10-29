/**
 * 管理端钱包核对页（AIH-TASK-135，设计闸门 #474 v13 §2「前端」）。
 *
 * 与门户页的测试同一写法：不 mock `api/adminWalletReconciliation`，而是在 `client.get` 上按路径回应，钉住**页面真正
 * 发出去的地址**（两张表的分页查询串、点一行后问的是不是那次运行），并逐条断言没有问 `/api/v1/customer/` 路径、
 * 没有发任何写请求。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import type { Page } from "../../api/adminCustomers";
import {
  PROBLEM_CODES,
  type ReconciliationFinding,
  type ReconciliationRun,
} from "../../api/adminWalletReconciliation";
import { ApiError, client } from "../../api/client";
import { ROUTES } from "../../routes/paths";
import { WalletReconciliationPage } from "./WalletReconciliationPage";

const ZERO_UUID = "00000000-0000-0000-0000-000000000000";
const RUNS_URL = "/api/v1/admin/wallet-reconciliation/runs";
const FINDINGS_URL = `${RUNS_URL}/${ZERO_UUID}/findings`;

/** 让请求一直挂着（加载中）。 */
const PENDING = Symbol("pending");

/** 九个问题码各自的说明（en.json 的 walletReconciliation.problem.*），按判定顺序。 */
const DESCRIPTIONS: Record<(typeof PROBLEM_CODES)[number], string> = {
  WALLET_MISSING: "This customer has no wallet.",
  LEDGER_MISSING: "The wallet version is above 0, but the ledger has no rows.",
  BALANCE_NOT_LEDGER_SUM: "The wallet balance does not equal the sum of all ledger amounts.",
  BALANCE_NOT_LAST_BALANCE_AFTER: "The wallet balance does not equal the balance after the last ledger row.",
  VERSION_NOT_LAST_SEQUENCE:
    "The wallet version does not equal the sequence number of the last ledger row (it should be 0 when there are no rows).",
  SEQUENCE_GAP: "The ledger sequence numbers do not run 1, 2, 3… without gaps or repeats.",
  CHAIN_BROKEN:
    "A ledger row's balance before does not equal the previous row's balance after (the first row should start from 0).",
  ROW_ARITHMETIC_BROKEN: "A ledger row's balance after does not equal its balance before plus its amount.",
  BILLING_STATUS_MISMATCH: "The customer's billing status does not match the status its wallet balance calls for.",
};

function run(overrides: Partial<ReconciliationRun> = {}): ReconciliationRun {
  return {
    run_id: ZERO_UUID,
    run_date: "2026-10-29",
    status: "COMPLETED",
    started_at: "2026-10-28T19:05:00",
    finished_at: "2026-10-28T19:12:00",
    wallets_checked: 3,
    wallets_inconsistent: 1,
    ...overrides,
  };
}

function finding(overrides: Partial<ReconciliationFinding> = {}): ReconciliationFinding {
  return {
    customer_id: ZERO_UUID,
    company_name: "Acme Sdn Bhd",
    problem_codes: ["LEDGER_MISSING", "BALANCE_NOT_LEDGER_SUM", "VERSION_NOT_LAST_SEQUENCE"],
    balance: "1234.50000000",
    wallet_version: 4,
    ledger_rows: 0,
    checked_at: "2026-10-28T19:06:00",
    ...overrides,
  };
}

function page<T>(items: T[], total: number, number = 1, pageSize = 20): Page<T> {
  return { items, page: number, page_size: pageSize, total };
}

function envelope(data: unknown) {
  return { data: { success: true, data, error: null, request_id: "req-1" } };
}

type Reply = (query: URLSearchParams) => unknown;

/**
 * 按路径回应 `client.get`：运行列表交给 `runs`，这次运行（全零 run_id）的不一致记录交给 `findings`。别的路径一律
 * 当成失败 —— 页面不该问它们，下面也会断言没有问。
 */
function serve(runs: Reply, findings: Reply = () => page([], 0)) {
  return vi.spyOn(client, "get").mockImplementation((url: string) => {
    const parsed = new URL(url, "https://admin.example");
    let reply: unknown;
    if (parsed.pathname === RUNS_URL) {
      reply = runs(parsed.searchParams);
    } else if (parsed.pathname === FINDINGS_URL) {
      reply = findings(parsed.searchParams);
    } else {
      reply = new ApiError("NOT_FOUND", "Unexpected request.", null);
    }
    if (reply === PENDING) {
      return new Promise(() => undefined);
    }
    return reply instanceof Error ? Promise.reject(reply) : Promise.resolve(envelope(reply));
  });
}

/** 写请求一个都不该发：装上替身，只用来断言没有被调用。 */
function spyWrites() {
  return [
    vi.spyOn(client, "post"),
    vi.spyOn(client, "put"),
    vi.spyOn(client, "patch"),
    vi.spyOn(client, "delete"),
  ];
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[ROUTES.walletReconciliation]}>
        <WalletReconciliationPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function runsRegion(): HTMLElement {
  return screen.getByRole("region", { name: "Reconciliation runs" });
}

function findingsRegion(date = "2026-10-29"): Promise<HTMLElement> {
  return screen.findByRole("region", { name: `Inconsistent wallets in the run of ${date}` });
}

/** 一张表的表头文字，按显示顺序。 */
function headersOf(region: HTMLElement): string[] {
  return within(region)
    .getAllByRole("columnheader")
    .map((th) => th.textContent ?? "");
}

/** 一张表里 `data-row-key` 为 key 的那一行的各个 <td>。 */
function cellsOf(region: HTMLElement, key: string): HTMLElement[] {
  const tr = region.querySelector(`tr[data-row-key="${key}"]`);
  if (tr === null) {
    throw new Error(`no row ${key}`);
  }
  return Array.from(tr.querySelectorAll<HTMLElement>(":scope > td"));
}

function dataRowCount(region: HTMLElement): number {
  return region.querySelectorAll("tbody > tr[data-row-key]").length;
}

function urls(get: ReturnType<typeof serve>): string[] {
  return get.mock.calls.map(([url]) => url);
}

async function openRun(key = ZERO_UUID): Promise<HTMLElement> {
  await screen.findByText("2026-10-29");
  const tr = runsRegion().querySelector(`tr[data-row-key="${key}"]`);
  if (tr === null) {
    throw new Error(`no run ${key}`);
  }
  fireEvent.click(tr);
  return findingsRegion();
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("WalletReconciliationPage runs", () => {
  it("shows a loading state while the runs are on their way", async () => {
    serve(() => PENDING);

    renderPage();

    expect(await screen.findByText("Loading reconciliation runs…")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("shows the backend message and the request id when the runs fail, with a retry", async () => {
    serve(() => new ApiError("INTERNAL_ERROR", "An unexpected error occurred.", "req-500"));

    renderPage();

    expect(await screen.findByText("The reconciliation runs could not be loaded.")).toBeInTheDocument();
    expect(screen.getByText(/An unexpected error occurred\./)).toBeInTheDocument();
    expect(screen.getByText("req-500")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("says there is no permission on a 403", async () => {
    serve(() => new ApiError("ADMIN_REQUIRED", "Administrator access is required.", "req-403"));

    renderPage();

    expect(await screen.findByText("Only administrators can view wallet reconciliation.")).toBeInTheDocument();
    expect(screen.getByText("req-403")).toBeInTheDocument();
    expect(screen.queryByText("The reconciliation runs could not be loaded.")).not.toBeInTheDocument();
  });

  it("says so when there are no reconciliation runs yet", async () => {
    serve(() => page([], 0));

    renderPage();

    expect(await screen.findByText("No reconciliation runs yet.")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("lists the runs with Kuala Lumpur times, one row per item, in the backend's order", async () => {
    serve(() =>
      page(
        [
          run({ status: "RUNNING", finished_at: null, wallets_checked: 2, wallets_inconsistent: 0 }),
        ],
        1,
      ),
    );

    renderPage();
    await screen.findByText("2026-10-29");

    const region = runsRegion();
    expect(headersOf(region)).toEqual([
      "Run date",
      "Status",
      "Started (MYT)",
      "Finished (MYT)",
      "Wallets checked",
      "Inconsistent wallets",
    ]);
    expect(dataRowCount(region)).toBe(1);
    expect(cellsOf(region, ZERO_UUID).map((td) => td.textContent)).toEqual([
      "2026-10-29",
      "Running",
      // 19:05 UTC → 次日 03:05 吉隆坡时间。
      "2026-10-29 03:05:00",
      "—",
      "2",
      "0",
    ]);
  });

  it("shows the finish time of a completed run and makes a non-zero inconsistent count stand out", async () => {
    serve(() => page([run({ wallets_inconsistent: 1 })], 1));

    renderPage();
    await screen.findByText("2026-10-29");

    const cells = cellsOf(runsRegion(), ZERO_UUID);
    expect(cells[1]?.textContent).toBe("Completed");
    expect(cells[3]?.textContent).toBe("2026-10-29 03:12:00");
    expect(cells[4]?.textContent).toBe("3");
    expect(cells[5]?.querySelector("strong")?.textContent).toBe("1");
  });

  it("does not make a zero inconsistent count stand out", async () => {
    serve(() => page([run({ status: "ABANDONED", wallets_inconsistent: 0 })], 1));

    renderPage();
    await screen.findByText("2026-10-29");

    const cells = cellsOf(runsRegion(), ZERO_UUID);
    expect(cells[1]?.textContent).toBe("Abandoned");
    expect(cells[5]?.textContent).toBe("0");
    expect(cells[5]?.querySelector("strong")).toBeNull();
  });

  it("asks for the runs with page and page_size, and for the next page when it is chosen", async () => {
    const get = serve((query) => page([run()], 45, Number(query.get("page"))));

    renderPage();
    await screen.findByText("2026-10-29");
    expect(urls(get)).toEqual([`${RUNS_URL}?page=1&page_size=20`]);

    fireEvent.click(within(runsRegion()).getByTitle("2"));

    await waitFor(() => expect(urls(get)).toContain(`${RUNS_URL}?page=2&page_size=20`));
  });

  it("asks for no findings until a run is selected", async () => {
    const get = serve(() => page([run()], 1));

    renderPage();
    await screen.findByText("2026-10-29");

    expect(screen.getByText("Select a run to see its inconsistent wallets.")).toBeInTheDocument();
    expect(urls(get).some((url) => url.includes("/findings"))).toBe(false);
  });
});

describe("WalletReconciliationPage findings", () => {
  it("shows the inconsistent wallets of the selected run with a link to the customer", async () => {
    const get = serve(
      () => page([run()], 1),
      () => page([finding()], 1),
    );

    renderPage();
    const region = await openRun();
    await within(region).findByText("Acme Sdn Bhd");

    expect(urls(get)).toContain(`${FINDINGS_URL}?page=1&page_size=20`);
    expect(headersOf(region)).toEqual([
      "Customer",
      "Problems",
      "Balance",
      "Wallet version",
      "Ledger rows",
      "Checked at (MYT)",
    ]);
    expect(within(region).getByRole("link", { name: "Acme Sdn Bhd" })).toHaveAttribute(
      "href",
      `/customers/${ZERO_UUID}`,
    );
    const cells = cellsOf(region, ZERO_UUID);
    // 与客户详情页同一个金额组件：币种在前、千分位、去掉多余的 0。
    expect(cells[2]?.textContent).toBe("MYR 1,234.50");
    expect(cells[3]?.textContent).toBe("4");
    expect(cells[4]?.textContent).toBe("0");
    expect(cells[5]?.textContent).toBe("2026-10-29 03:06:00");
  });

  it("shows every problem code as its own tag with its description, in the backend's order", async () => {
    serve(
      () => page([run()], 1),
      () => page([finding({ problem_codes: [...PROBLEM_CODES] })], 1),
    );

    renderPage();
    const region = await openRun();
    await within(region).findByText("Acme Sdn Bhd");

    const items = within(cellsOf(region, ZERO_UUID)[1] as HTMLElement).getAllByRole("listitem");
    expect(items).toHaveLength(PROBLEM_CODES.length);
    PROBLEM_CODES.forEach((code, index) => {
      const item = items[index] as HTMLElement;
      expect(within(item).getByText(code)).toBeInTheDocument();
      expect(within(item).getByText(DESCRIPTIONS[code])).toBeInTheDocument();
    });
  });

  it("shows an unknown problem code as it is, without a description", async () => {
    serve(
      () => page([run()], 1),
      () => page([finding({ problem_codes: ["SOMETHING_NEW"] })], 1),
    );

    renderPage();
    const region = await openRun();
    await within(region).findByText("Acme Sdn Bhd");

    const items = within(cellsOf(region, ZERO_UUID)[1] as HTMLElement).getAllByRole("listitem");
    expect(items.map((item) => item.textContent)).toEqual(["SOMETHING_NEW"]);
  });

  it("says there is no wallet when the balance and version are null", async () => {
    serve(
      () => page([run()], 1),
      () =>
        page(
          [finding({ problem_codes: ["WALLET_MISSING"], balance: null, wallet_version: null, ledger_rows: 0 })],
          1,
        ),
    );

    renderPage();
    const region = await openRun();
    await within(region).findByText("Acme Sdn Bhd");

    const cells = cellsOf(region, ZERO_UUID);
    expect(cells[2]?.textContent).toBe("No wallet");
    expect(cells[2]?.textContent).not.toContain("MYR");
    expect(cells[3]?.textContent).toBe("—");
    expect(cells[4]?.textContent).toBe("0");
  });

  it("asks for the next page of findings of the same run when it is chosen", async () => {
    const get = serve(
      () => page([run()], 1),
      (query) => page([finding()], 45, Number(query.get("page"))),
    );

    renderPage();
    const region = await openRun();
    await within(region).findByText("Acme Sdn Bhd");

    fireEvent.click(within(region).getByTitle("2"));

    await waitFor(() => expect(urls(get)).toContain(`${FINDINGS_URL}?page=2&page_size=20`));
    // 运行表没有跟着翻页。
    expect(urls(get).filter((url) => url.startsWith(`${RUNS_URL}?`))).toEqual([`${RUNS_URL}?page=1&page_size=20`]);
  });

  it("shows a loading state while the findings are on their way", async () => {
    serve(
      () => page([run()], 1),
      () => PENDING,
    );

    renderPage();
    const region = await openRun();

    expect(within(region).getByText("Loading inconsistent wallets…")).toBeInTheDocument();
  });

  it("shows the backend message and the request id when the findings fail", async () => {
    serve(
      () => page([run()], 1),
      () => new ApiError("INTERNAL_ERROR", "An unexpected error occurred.", "req-500"),
    );

    renderPage();
    const region = await openRun();

    expect(await within(region).findByText("The inconsistent wallets could not be loaded.")).toBeInTheDocument();
    expect(within(region).getByText("req-500")).toBeInTheDocument();
    expect(within(region).getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("says the run was not found on a 404", async () => {
    serve(
      () => page([run()], 1),
      () => new ApiError("RECONCILIATION_RUN_NOT_FOUND", "Reconciliation run not found.", "req-404"),
    );

    renderPage();
    const region = await openRun();

    expect(await within(region).findByText("This reconciliation run was not found.")).toBeInTheDocument();
    expect(within(region).getByText("req-404")).toBeInTheDocument();
  });

  it("says every wallet was consistent when a completed run has no findings", async () => {
    serve(
      () => page([run({ wallets_inconsistent: 0 })], 1),
      () => page([], 0),
    );

    renderPage();
    const region = await openRun();

    expect(await within(region).findByText("Every wallet checked in this run is consistent.")).toBeInTheDocument();
    expect(within(region).queryByRole("table")).not.toBeInTheDocument();
  });

  it("only speaks of the wallets checked so far when an unfinished run has no findings", async () => {
    serve(
      () => page([run({ status: "RUNNING", finished_at: null, wallets_inconsistent: 0 })], 1),
      () => page([], 0),
    );

    renderPage();
    const region = await openRun();

    expect(
      await within(region).findByText("Every wallet this run has checked so far is consistent."),
    ).toBeInTheDocument();
  });
});

describe("WalletReconciliationPage is read-only", () => {
  it("offers no repair or write action and sends no write request", async () => {
    const writes = spyWrites();
    serve(
      () => page([run()], 1),
      () => page([finding({ problem_codes: [...PROBLEM_CODES] })], 1),
    );

    renderPage();
    const region = await openRun();
    await within(region).findByText("Acme Sdn Bhd");

    // 分页的「上一页 / 下一页」是 <li title> 里的按钮；除此之外页面上没有任何按钮。
    const buttons = screen.queryAllByRole("button").filter((button) => button.closest("li[title]") === null);
    expect(buttons).toEqual([]);
    // 唯一的链接是客户详情。
    expect(screen.getAllByRole("link").map((link) => link.getAttribute("href"))).toEqual([
      `/customers/${ZERO_UUID}`,
    ]);
    for (const write of writes) {
      expect(write).not.toHaveBeenCalled();
    }
  });

  it("asks only the admin wallet reconciliation endpoints, never a /api/v1/customer/ path", async () => {
    const get = serve(
      () => page([run()], 1),
      () => page([finding()], 1),
    );

    renderPage();
    const region = await openRun();
    await within(region).findByText("Acme Sdn Bhd");

    expect(urls(get).length).toBeGreaterThan(0);
    expect(urls(get).filter((url) => url.startsWith("/api/v1/customer/"))).toEqual([]);
    expect(urls(get).every((url) => url.startsWith(`${RUNS_URL}?`) || url.startsWith(`${FINDINGS_URL}?`))).toBe(
      true,
    );
  });
});
