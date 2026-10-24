/**
 * 门户首页（AIH-TASK-097）与门户外壳（AIH-TASK-096）。
 *
 * ⚠️ 要钉住的有两件：
 * - **外壳里没有管理端菜单**：客户点得到的管理端链接只会被 `RequireRole` 转回门户，后端也一律 403。把 `AppLayout`
 *   当成门户外壳复用的变异，这里必须变红。
 * - **页面上没有内部字段**：`customer_id` 与任何成本、毛利、计费模式都不出现（INV-7）。
 *
 * 余额卡与最近流水（AIH-TASK-101）：钱包两个接口不 mock `api/customerWallet`，而是在 `client.get` 上按路径回应
 * （同 `WalletPage.test.tsx`）—— 要钉住的是页面真正发出去的地址：最近流水带 `page_size=5`、没有请求任何
 * `/api/v1/admin/` 路径。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import type { Me } from "../../api/auth";
import { ApiError, client } from "../../api/client";
import type { CustomerMe } from "../../api/customer";
import type { CustomerTransaction, CustomerTransactionPage, CustomerWallet } from "../../api/customerWallet";
import { AuthProvider } from "../../auth/AuthProvider";
import { resetTokenStore } from "../../auth/tokenStore";
import { PortalLayout } from "../../layouts/PortalLayout";
import { ROUTES } from "../../routes/paths";
import { PortalHomePage } from "./PortalHomePage";

const refresh = vi.hoisted(() => ({ refreshAccessToken: vi.fn() }));
const customerApi = vi.hoisted(() => ({ getCustomerMe: vi.fn() }));
const authApi = vi.hoisted(() => ({ getMe: vi.fn() }));

vi.mock("../../auth/refresh", () => refresh);
vi.mock("../../api/customer", async () => {
  const actual = await vi.importActual<typeof import("../../api/customer")>("../../api/customer");
  return { ...actual, ...customerApi };
});
vi.mock("../../api/auth", async () => {
  const actual = await vi.importActual<typeof import("../../api/auth")>("../../api/auth");
  return { ...actual, ...authApi };
});

/** 管理端顶栏的每一项（`AppLayout`）。门户里一项都不许出现。 */
const ADMIN_PATHS = [
  ROUTES.dashboard,
  ROUTES.customers,
  ROUTES.audit,
  ROUTES.catalogProviders,
  ROUTES.catalogMeterTypes,
  ROUTES.providerPrices,
  ROUTES.fxRates,
  ROUTES.pricingRules,
  ROUTES.pricingPreview,
  ROUTES.usageEvents,
  ROUTES.usageAnalytics,
];

const ADMIN_LABELS = [
  "Dashboard",
  "Customers",
  "Audit log",
  "AI catalog",
  "Provider prices",
  "Exchange rates",
  "Pricing rules",
  "Pricing preview",
  "Usage events",
  "Usage analytics",
];

const CUSTOMER_ID = "00000000-0000-0000-0000-000000000000";

function account(overrides: Partial<CustomerMe> = {}): CustomerMe {
  return {
    customer_id: CUSTOMER_ID,
    company_name: "Example Sdn Bhd",
    account_status: "ENABLED",
    read_only: false,
    ...overrides,
  };
}

function me(twoFactorEnabled: boolean): Me {
  return { role: "CUSTOMER", email: "portal@example.com", two_factor_enabled: twoFactorEnabled };
}

const WALLET_URL = "/api/v1/customer/wallet";
const TRANSACTIONS_URL = "/api/v1/customer/wallet/transactions";
const ZERO_UUID = "00000000-0000-0000-0000-000000000000";

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
    id: ZERO_UUID,
    wallet_sequence: 42,
    transaction_type: "AI_USAGE",
    amount: "-0.04120000",
    balance_after: "12.45880000",
    event_id: ZERO_UUID,
    created_at: "2026-10-18T08:30:00",
    ...overrides,
  };
}

function page(items: CustomerTransaction[], next: number | null = null): CustomerTransactionPage {
  return { items, page_size: 5, next_before_sequence: next };
}

function envelope(data: unknown) {
  return { data: { success: true, data, error: null, request_id: "req-1" } };
}

/**
 * 按路径回应 `client.get`：钱包接口回 `walletReply`，流水接口回 `transactionsReply`。别的路径一律当成失败 ——
 * 首页不该问它们（`/customer/me` 与 `/me` 在上面按模块 mock 掉了，不经这里）。
 */
function serve(walletReply: unknown, transactionsReply: unknown) {
  return vi.spyOn(client, "get").mockImplementation((url: string) => {
    const parsed = new URL(url, "https://portal.example");
    let reply: unknown;
    if (parsed.pathname === WALLET_URL) {
      reply = walletReply;
    } else if (parsed.pathname === TRANSACTIONS_URL) {
      reply = transactionsReply;
    } else {
      reply = new ApiError("NOT_FOUND", "Unexpected request.", null);
    }
    if (reply === PENDING) {
      return new Promise(() => undefined);
    }
    return reply instanceof Error ? Promise.reject(reply) : Promise.resolve(envelope(reply));
  });
}

function renderPortal() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[ROUTES.portalHome]}>
        <AuthProvider>
          <Routes>
            <Route element={<PortalLayout />}>
              <Route path={ROUTES.portalHome} element={<PortalHomePage />} />
            </Route>
          </Routes>
        </AuthProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** antd 带边框的 Descriptions：label 在 <th>，值在紧跟的 <td>。 */
function valueOf(label: string): string | null {
  const th = screen.getByText(label).closest("th");
  return th?.nextElementSibling?.textContent ?? null;
}

beforeEach(() => {
  vi.clearAllMocks();
  resetTokenStore();
  refresh.refreshAccessToken.mockResolvedValue("access-token-1");
  authApi.getMe.mockResolvedValue(me(true));
  // 默认：钱包读得到、还没有流水。各用例需要别的回应时自己再 serve 一次（后一次覆盖前一次）。
  serve(wallet(), page([]));
});

afterEach(() => {
  vi.restoreAllMocks();
  resetTokenStore();
});

describe("PortalHomePage", () => {
  it("shows the company name and account status", async () => {
    customerApi.getCustomerMe.mockResolvedValue(account());

    renderPortal();

    expect(await screen.findByText("Example Sdn Bhd")).toBeInTheDocument();
    expect(valueOf("Company name")).toBe("Example Sdn Bhd");
    expect(valueOf("Account status")).toBe("Enabled");
    expect(screen.queryByText("Your account is read-only")).not.toBeInTheDocument();
  });

  it("explains what still works when the account is read-only", async () => {
    customerApi.getCustomerMe.mockResolvedValue(account({ account_status: "DISABLED", read_only: true }));

    renderPortal();

    expect(await screen.findByText("Your account is read-only")).toBeInTheDocument();
    expect(screen.getByText(/You can still view your bills and receipts\. New actions are paused/)).toBeInTheDocument();
    expect(valueOf("Account status")).toBe("Disabled");
  });

  it("shows a closed account as read-only too", async () => {
    customerApi.getCustomerMe.mockResolvedValue(account({ account_status: "CLOSED", read_only: true }));

    renderPortal();

    expect(await screen.findByText("Your account is read-only")).toBeInTheDocument();
    expect(valueOf("Account status")).toBe("Closed");
  });

  it("shows an account status it does not recognise as it is", async () => {
    customerApi.getCustomerMe.mockResolvedValue(account({ account_status: "SUSPENDED_FOR_REVIEW" }));

    renderPortal();

    await screen.findByText("Example Sdn Bhd");
    expect(valueOf("Account status")).toBe("SUSPENDED_FOR_REVIEW");
  });

  it("shows a loading state while the account is being read", async () => {
    customerApi.getCustomerMe.mockReturnValue(new Promise<CustomerMe>(() => undefined));

    renderPortal();

    expect(await screen.findByText("Loading your account…")).toBeInTheDocument();
    expect(screen.queryByText("Company name")).not.toBeInTheDocument();
  });

  it("shows the failure with its request id", async () => {
    customerApi.getCustomerMe.mockRejectedValue(
      new ApiError("INTERNAL_ERROR", "An unexpected error occurred.", "req-customer-me-500"),
    );

    renderPortal();

    expect(await screen.findByText("Your account details could not be loaded.")).toBeInTheDocument();
    expect(screen.getByText("req-customer-me-500")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
    expect(screen.queryByText("Company name")).not.toBeInTheDocument();
  });

  it("shows no internal field", async () => {
    customerApi.getCustomerMe.mockResolvedValue(account());

    const { container } = renderPortal();
    await screen.findByText("Example Sdn Bhd");

    const text = container.textContent ?? "";
    expect(text).not.toContain(CUSTOMER_ID);
    expect(text).not.toMatch(/cost|margin|billing mode|billing_mode|customer id|customer_id|tenant/i);
    // 账户信息那张表只有这两项：公司名与账户状态（余额卡与最近流水各有自己的表头，不在这里数）。
    const accountTable = screen.getByText("Company name").closest("table");
    expect(Array.from(accountTable?.querySelectorAll("th") ?? [], (th) => th.textContent)).toEqual([
      "Company name",
      "Account status",
    ]);
  });

  it("offers to turn on two-factor authentication only while it is off", async () => {
    customerApi.getCustomerMe.mockResolvedValue(account());
    authApi.getMe.mockResolvedValue(me(false));

    renderPortal();

    expect(await screen.findByText("Two-factor authentication")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Set up two-factor authentication" })).toBeInTheDocument();
  });

  it("still offers two-factor authentication when the account summary fails", async () => {
    customerApi.getCustomerMe.mockRejectedValue(new ApiError("INTERNAL_ERROR", "An unexpected error occurred.", null));
    authApi.getMe.mockResolvedValue(me(false));

    renderPortal();

    expect(await screen.findByText("Your account details could not be loaded.")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Set up two-factor authentication" })).toBeInTheDocument();
  });
});

describe("PortalHomePage wallet summary and recent transactions", () => {
  /** 最近流水那一块（role=region，aria-label 为「Recent transactions」）。 */
  function recent(): HTMLElement {
    return screen.getByRole("region", { name: "Recent transactions" });
  }

  it("shows the balance card and the most recent transactions", async () => {
    customerApi.getCustomerMe.mockResolvedValue(account());
    serve(wallet(), page([row()]));

    renderPortal();

    expect(await screen.findByText("MYR 12.46")).toBeInTheDocument();
    expect(valueOf("Balance")).toBe("MYR 12.46");
    expect(valueOf("AI services")).toBe("Available");
    expect(await within(recent()).findByText("AI usage")).toBeInTheDocument();
    // 金额的显示精度在复用的表里：AI 用量行最多 6 位，余额（后）2 位。
    expect(within(recent()).getByText("-0.0412")).toBeInTheDocument();
    expect(within(recent()).getByText("12.46")).toBeInTheDocument();
    expect(screen.getByText("Example Sdn Bhd")).toBeInTheDocument();
  });

  it("asks for the five most recent transactions, and no admin endpoint", async () => {
    customerApi.getCustomerMe.mockResolvedValue(account());
    const get = serve(wallet(), page([row()]));

    renderPortal();
    await within(recent()).findByText("AI usage");
    await screen.findByText("MYR 12.46");

    const urls = get.mock.calls.map(([url]) => url);
    expect(urls).toContain(WALLET_URL);
    expect(urls).toContain(`${TRANSACTIONS_URL}?page_size=5`);
    for (const url of urls) {
      expect(url).not.toContain("/api/v1/admin/");
      expect(url === WALLET_URL || url === `${TRANSACTIONS_URL}?page_size=5`).toBe(true);
    }
  });

  it("shows at most the five rows it was given and does not page", async () => {
    customerApi.getCustomerMe.mockResolvedValue(account());
    const ids = [1, 2, 3, 4, 5].map((n) => `00000000-0000-0000-0000-00000000000${n}`);
    serve(
      wallet(),
      page(
        ids.map((id, index) => row({ id, wallet_sequence: 42 - index, transaction_type: "BONUS", event_id: null })),
        37,
      ),
    );

    const { container } = renderPortal();
    await within(recent()).findAllByText("Bonus");

    expect(container.querySelectorAll("tr[data-row-key]")).toHaveLength(5);
    expect(screen.queryByRole("button", { name: "Older" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Newest" })).not.toBeInTheDocument();
  });

  it("links to the wallet page", async () => {
    customerApi.getCustomerMe.mockResolvedValue(account());

    renderPortal();

    const link = await screen.findByRole("link", { name: "View all transactions" });
    expect(link).toHaveAttribute("href", ROUTES.portalWallet);
  });

  it("says it is loading while the wallet and the recent transactions are on their way", async () => {
    customerApi.getCustomerMe.mockResolvedValue(account());
    serve(PENDING, PENDING);

    renderPortal();

    expect(await screen.findByText("Loading your wallet…")).toBeInTheDocument();
    expect(screen.getByText("Loading your transactions…")).toBeInTheDocument();
    expect(within(recent()).queryByRole("table")).not.toBeInTheDocument();
    expect(await screen.findByText("Example Sdn Bhd")).toBeInTheDocument();
  });

  it("shows each failure with its request id", async () => {
    customerApi.getCustomerMe.mockResolvedValue(account());
    serve(
      new ApiError("INTERNAL_ERROR", "An unexpected error occurred.", "req-wallet-500"),
      new ApiError("INTERNAL_ERROR", "An unexpected error occurred.", "req-transactions-500"),
    );

    renderPortal();

    expect(await screen.findByText("Your wallet could not be loaded.")).toBeInTheDocument();
    expect(await screen.findByText("Your transactions could not be loaded.")).toBeInTheDocument();
    expect(screen.getByText("req-wallet-500")).toBeInTheDocument();
    expect(screen.getByText("req-transactions-500")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Try again" })).toHaveLength(2);
    // 失败时到钱包页的链接照样在。
    expect(screen.getByRole("link", { name: "View all transactions" })).toHaveAttribute("href", ROUTES.portalWallet);
  });

  it("says there are no transactions yet, without a table", async () => {
    customerApi.getCustomerMe.mockResolvedValue(account());
    serve(wallet({ balance: "0.00000000", billing_status: "SUSPENDED" }), page([]));

    renderPortal();

    expect(await screen.findByText("You have no transactions yet.")).toBeInTheDocument();
    expect(within(recent()).queryByRole("table")).not.toBeInTheDocument();
    expect(await screen.findByText("MYR 0.00")).toBeInTheDocument();
  });

  it("still shows the account and offers two-factor authentication when the wallet fails", async () => {
    customerApi.getCustomerMe.mockResolvedValue(account());
    authApi.getMe.mockResolvedValue(me(false));
    serve(new ApiError("INTERNAL_ERROR", "An unexpected error occurred.", "req-wallet-500"), page([row()]));

    renderPortal();

    expect(await screen.findByText("Your wallet could not be loaded.")).toBeInTheDocument();
    expect(await screen.findByText("Example Sdn Bhd")).toBeInTheDocument();
    expect(valueOf("Company name")).toBe("Example Sdn Bhd");
    expect(valueOf("Account status")).toBe("Enabled");
    expect(await screen.findByRole("button", { name: "Set up two-factor authentication" })).toBeInTheDocument();
    // 余额读失败也不挡最近流水。
    expect(await within(recent()).findByText("AI usage")).toBeInTheDocument();
  });

  it("still shows the balance when the recent transactions fail", async () => {
    customerApi.getCustomerMe.mockResolvedValue(account());
    serve(wallet(), new ApiError("INTERNAL_ERROR", "An unexpected error occurred.", "req-transactions-500"));

    renderPortal();

    expect(await screen.findByText("Your transactions could not be loaded.")).toBeInTheDocument();
    expect(await screen.findByText("MYR 12.46")).toBeInTheDocument();
    expect(screen.getByText("Example Sdn Bhd")).toBeInTheDocument();
  });

  it("shows no internal field in the wallet blocks either", async () => {
    customerApi.getCustomerMe.mockResolvedValue(account());
    serve(wallet(), page([row({ transaction_type: "ADJUSTMENT_CREDIT", amount: "5.00000000", event_id: null })]));

    const { container } = renderPortal();
    await within(recent()).findByText("Credit adjustment");
    await screen.findByText("MYR 12.46");

    const text = container.textContent ?? "";
    expect(text).not.toContain(CUSTOMER_ID);
    expect(text).not.toMatch(/cost|margin|billing mode|billing_mode|customer id|customer_id|tenant|description|sequence/i);
  });
});

describe("PortalHomePage in the portal shell", () => {
  it("has a way to sign out", async () => {
    customerApi.getCustomerMe.mockResolvedValue(account());

    renderPortal();

    expect(await screen.findByText("Example Sdn Bhd")).toBeInTheDocument();
    expect(screen.getByText("Customer portal")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign out" })).toBeInTheDocument();
  });

  it("has none of the admin menu items", async () => {
    customerApi.getCustomerMe.mockResolvedValue(account());
    authApi.getMe.mockResolvedValue(me(false));

    renderPortal();
    await screen.findByText("Example Sdn Bhd");
    await screen.findByText("Two-factor authentication");

    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(screen.queryByRole("menuitem")).not.toBeInTheDocument();
    for (const label of ADMIN_LABELS) {
      expect(screen.queryByText(label)).not.toBeInTheDocument();
    }
    const hrefs = screen.getAllByRole("link").map((link) => link.getAttribute("href"));
    // 每个链接都在门户自己的路径之下（首页、钱包，以及之后任务加的门户页）。
    expect(hrefs.length).toBeGreaterThan(0);
    for (const href of hrefs) {
      expect(href?.startsWith(ROUTES.portalHome)).toBe(true);
    }
    for (const path of ADMIN_PATHS) {
      expect(hrefs).not.toContain(path);
    }
  });
});
