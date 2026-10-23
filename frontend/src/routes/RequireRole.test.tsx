/**
 * 按 `/api/v1/me` 的角色分流（AIH-TASK-096）。
 *
 * 挂的是与 `routes/index.tsx` 同样的嵌套：`RequireAuth` 之内，管理端一组、门户一组。只测守卫本身的话，
 * 「CUSTOMER 被转到 /portal、而 /portal 又把他转走」这种两组之间的来回就看不见。
 *
 * ⚠️ 最要紧的是**失败那一支不默认放行**：`/api/v1/me` 出错时两边的页面都不能画出来。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import "../i18n";
import { ROLE_ADMIN, ROLE_CUSTOMER, type Me } from "../api/auth";
import { ApiError } from "../api/client";
import { AuthProvider } from "../auth/AuthProvider";
import { resetTokenStore } from "../auth/tokenStore";
import { RequireAuth } from "./RequireAuth";
import { RequireRole } from "./RequireRole";
import { ROUTES } from "./paths";

const api = vi.hoisted(() => ({ getMe: vi.fn() }));
const refresh = vi.hoisted(() => ({ refreshAccessToken: vi.fn() }));

vi.mock("../api/auth", async () => {
  const actual = await vi.importActual<typeof import("../api/auth")>("../api/auth");
  return { ...actual, ...api };
});

// 守卫在 `RequireAuth` 之内：启动时那次静默刷新回一张令牌，才走得到这里。
vi.mock("../auth/refresh", () => refresh);

function me(role: string): Me {
  return { role, email: "someone@example.com", two_factor_enabled: true };
}

function LocationProbe() {
  const location = useLocation();
  return <span data-testid="location">{location.pathname}</span>;
}

function renderAt(entry: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[entry]}>
        <AuthProvider>
          <Routes>
            <Route path={ROUTES.login} element={<div data-testid="login-page" />} />
            <Route element={<RequireAuth />}>
              <Route element={<RequireRole role={ROLE_ADMIN} />}>
                <Route path={ROUTES.dashboard} element={<div data-testid="admin-home" />} />
                <Route path={ROUTES.customers} element={<div data-testid="admin-customers" />} />
              </Route>
              <Route element={<RequireRole role={ROLE_CUSTOMER} />}>
                <Route path={ROUTES.portalHome} element={<div data-testid="portal-home" />} />
              </Route>
            </Route>
          </Routes>
          <LocationProbe />
        </AuthProvider>
      </MemoryRouter>
    </QueryClientProvider>,
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

describe("RequireRole", () => {
  it("lets an administrator into the admin pages", async () => {
    api.getMe.mockResolvedValue(me(ROLE_ADMIN));

    renderAt(ROUTES.customers);

    expect(await screen.findByTestId("admin-customers")).toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent(ROUTES.customers);
    expect(screen.queryByTestId("portal-home")).not.toBeInTheDocument();
  });

  it("sends a customer who opens an admin page to the portal home", async () => {
    api.getMe.mockResolvedValue(me(ROLE_CUSTOMER));

    renderAt(ROUTES.customers);

    expect(await screen.findByTestId("portal-home")).toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent(ROUTES.portalHome);
    expect(screen.queryByTestId("admin-customers")).not.toBeInTheDocument();
  });

  it("sends an administrator who opens the portal to the admin home", async () => {
    api.getMe.mockResolvedValue(me(ROLE_ADMIN));

    renderAt(ROUTES.portalHome);

    expect(await screen.findByTestId("admin-home")).toBeInTheDocument();
    expect(screen.getByTestId("location").textContent).toBe(ROUTES.dashboard);
    expect(screen.queryByTestId("portal-home")).not.toBeInTheDocument();
  });

  it("lets a customer into the portal", async () => {
    api.getMe.mockResolvedValue(me(ROLE_CUSTOMER));

    renderAt(ROUTES.portalHome);

    expect(await screen.findByTestId("portal-home")).toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent(ROUTES.portalHome);
  });

  it("shows an error with the request id, and neither page, when /api/v1/me fails", async () => {
    api.getMe.mockRejectedValue(
      new ApiError("INTERNAL_ERROR", "An unexpected error occurred.", "req-me-500"),
    );

    renderAt(ROUTES.customers);

    expect(
      await screen.findByText("We could not confirm which account you are signed in with."),
    ).toBeInTheDocument();
    expect(screen.getByText("req-me-500")).toBeInTheDocument();
    // ⚠️ 不默认放行：哪一边都不画，也不转向。
    expect(screen.queryByTestId("admin-customers")).not.toBeInTheDocument();
    expect(screen.queryByTestId("portal-home")).not.toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent(ROUTES.customers);
  });

  it("does not let a role it does not recognise into either side", async () => {
    api.getMe.mockResolvedValue(me("AUDITOR"));

    renderAt(ROUTES.customers);

    expect(
      await screen.findByText("This account has a role this app does not recognise."),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("admin-customers")).not.toBeInTheDocument();
    expect(screen.queryByTestId("portal-home")).not.toBeInTheDocument();
  });

  it("shows the same spinner as RequireAuth, and neither page, while /api/v1/me is in flight", async () => {
    let answer!: (value: Me) => void;
    api.getMe.mockReturnValue(
      new Promise<Me>((resolve) => {
        answer = resolve;
      }),
    );

    const { container } = renderAt(ROUTES.customers);

    // 先等过 `RequireAuth` 那一段：静默刷新回来之后才轮到这里去问角色。
    await vi.waitFor(() => {
      expect(api.getMe).toHaveBeenCalled();
    });
    expect(container.querySelector(".ant-spin")).not.toBeNull();
    expect(screen.queryByTestId("admin-customers")).not.toBeInTheDocument();
    expect(screen.queryByTestId("portal-home")).not.toBeInTheDocument();
    expect(screen.queryByTestId("login-page")).not.toBeInTheDocument();

    answer(me(ROLE_ADMIN));
    expect(await screen.findByTestId("admin-customers")).toBeInTheDocument();
  });
});
