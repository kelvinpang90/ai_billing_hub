/**
 * 路由守卫的三态。
 *
 * ⚠️ 最要紧的是**第一态**：页面刚加载时访问令牌还在静默换取中（它只在内存里，
 * F5 必然丢）。这一刻把用户当成未登录，现象就是「每按一次 F5 都要重新登录」——
 * 功能全对、测试全绿，只有人用起来才发现。所以这条单独测。
 *
 * 守卫本身**不是访问控制**（spec §51 要求后端独立鉴权），它只决定画哪一页。
 */

import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AuthProvider } from "../auth/AuthProvider";
import { resetTokenStore, setAccessToken } from "../auth/tokenStore";
import { RequireAuth } from "./RequireAuth";
import { ROUTES } from "./paths";

const refresh = vi.hoisted(() => ({ refreshAccessToken: vi.fn() }));

// 守卫要等的就是这一次静默刷新，所以它必须可控 —— 否则「加载中」那一态
// 短得没法断言。
vi.mock("../auth/refresh", () => refresh);

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

function renderGuarded() {
  return render(
    <MemoryRouter initialEntries={[ROUTES.dashboard]}>
      <AuthProvider>
        <Routes>
          <Route path={ROUTES.login} element={<div data-testid="login-page" />} />
          <Route element={<RequireAuth />}>
            <Route path={ROUTES.dashboard} element={<div data-testid="protected-page" />} />
          </Route>
        </Routes>
      </AuthProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  resetTokenStore();
});

afterEach(() => {
  resetTokenStore();
});

describe("RequireAuth", () => {
  it("shows neither page while the silent refresh is still in flight", async () => {
    const gate = deferred<string | null>();
    refresh.refreshAccessToken.mockReturnValue(gate.promise);

    const { container } = renderGuarded();

    // ⚠️ 这一刻**不能**跳登录页：还不知道 cookie 里那张刷新令牌算不算数。
    expect(screen.queryByTestId("login-page")).not.toBeInTheDocument();
    expect(screen.queryByTestId("protected-page")).not.toBeInTheDocument();
    expect(container.querySelector(".ant-spin")).not.toBeNull();

    gate.resolve("token-1");
    await screen.findByTestId("protected-page");
  });

  it("renders the protected page once the refresh returns a token", async () => {
    refresh.refreshAccessToken.mockResolvedValue("token-1");

    renderGuarded();

    expect(await screen.findByTestId("protected-page")).toBeInTheDocument();
    expect(screen.queryByTestId("login-page")).not.toBeInTheDocument();
  });

  it("redirects to the login page when the refresh comes back empty", async () => {
    refresh.refreshAccessToken.mockResolvedValue(null);

    renderGuarded();

    expect(await screen.findByTestId("login-page")).toBeInTheDocument();
    expect(screen.queryByTestId("protected-page")).not.toBeInTheDocument();
  });

  it("falls back to the login page when the token is cleared later", async () => {
    refresh.refreshAccessToken.mockResolvedValue("token-1");

    renderGuarded();
    await screen.findByTestId("protected-page");

    // 拦截器在刷新失败时会清掉令牌（会话被吊销、cookie 过期）。界面必须跟着
    // 走回登录页，不能继续停在一个再也发不出请求的后台页上。
    setAccessToken(null);

    await waitFor(() => {
      expect(screen.getByTestId("login-page")).toBeInTheDocument();
    });
  });
});
