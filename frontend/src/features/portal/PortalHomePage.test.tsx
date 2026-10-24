/**
 * 门户首页（AIH-TASK-097）与门户外壳（AIH-TASK-096）。
 *
 * ⚠️ 要钉住的有两件：
 * - **外壳里没有管理端菜单**：客户点得到的管理端链接只会被 `RequireRole` 转回门户，后端也一律 403。把 `AppLayout`
 *   当成门户外壳复用的变异，这里必须变红。
 * - **页面上没有内部字段**：`customer_id` 与任何成本、毛利、计费模式都不出现（INV-7）。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import type { Me } from "../../api/auth";
import { ApiError } from "../../api/client";
import type { CustomerMe } from "../../api/customer";
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
});

afterEach(() => {
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
    // 只有这两项：公司名与账户状态。
    expect(Array.from(container.querySelectorAll("th"), (th) => th.textContent)).toEqual([
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
