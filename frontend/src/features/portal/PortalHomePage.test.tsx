/**
 * 门户外壳与首页占位（AIH-TASK-096）。
 *
 * ⚠️ 要钉住的是**外壳里没有管理端菜单**：客户点得到的管理端链接只会被 `RequireRole` 转回门户，后端也一律 403。
 * 把 `AppLayout` 当成门户外壳复用的变异，这里必须变红。
 */

import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import { AuthProvider } from "../../auth/AuthProvider";
import { resetTokenStore } from "../../auth/tokenStore";
import { PortalLayout } from "../../layouts/PortalLayout";
import { ROUTES } from "../../routes/paths";
import { PortalHomePage } from "./PortalHomePage";

const refresh = vi.hoisted(() => ({ refreshAccessToken: vi.fn() }));

vi.mock("../../auth/refresh", () => refresh);

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

function renderPortal() {
  return render(
    <MemoryRouter initialEntries={[ROUTES.portalHome]}>
      <AuthProvider>
        <Routes>
          <Route element={<PortalLayout />}>
            <Route path={ROUTES.portalHome} element={<PortalHomePage />} />
          </Route>
        </Routes>
      </AuthProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  resetTokenStore();
  refresh.refreshAccessToken.mockResolvedValue("access-token-1");
});

afterEach(() => {
  resetTokenStore();
});

describe("PortalHomePage in the portal shell", () => {
  it("shows the welcome placeholder and a way to sign out", async () => {
    renderPortal();

    expect(await screen.findByText("Welcome")).toBeInTheDocument();
    expect(screen.getByText("Customer portal")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign out" })).toBeInTheDocument();
  });

  it("has none of the admin menu items", async () => {
    renderPortal();
    await screen.findByText("Welcome");

    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(screen.queryByRole("menuitem")).not.toBeInTheDocument();
    for (const label of ADMIN_LABELS) {
      expect(screen.queryByText(label)).not.toBeInTheDocument();
    }
    const hrefs = screen.getAllByRole("link").map((link) => link.getAttribute("href"));
    // 唯一的链接是门户自己的首页。
    expect(hrefs).toEqual([ROUTES.portalHome]);
    for (const path of ADMIN_PATHS) {
      expect(hrefs).not.toContain(path);
    }
  });
});
