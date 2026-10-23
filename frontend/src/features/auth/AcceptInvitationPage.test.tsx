/**
 * 「接受邀请」页（设计 AIH-TASK-089 §2、§7、§8，AIH-TASK-092）。
 *
 * 这里挂的是**完整路由表**（`AppRoutes` + `AuthProvider`），不是单独的页面组件：§7「邀请链接匿名可达」
 * 要钉住的正是「这条路由在 `RequireAuth` 外面」。只渲染页面组件的话，把路由挪进守卫的变异照样全绿，
 * 而线上的表现是邀请链接被送去登录页 —— 一个 `INVITED` 账号根本没有密码可填。
 *
 * 其余用例测**哪种错误该留在表单上、哪种必须把表单收走**，理由同 ResetPasswordPage.test.tsx。
 */

import { act, configure, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App as AntdApp } from "antd";
import { MemoryRouter, useLocation } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import { ApiError } from "../../api/client";
import { AuthProvider } from "../../auth/AuthProvider";
import { resetTokenStore } from "../../auth/tokenStore";
import { AppRoutes } from "../../routes";
import { ROUTES } from "../../routes/paths";

const api = vi.hoisted(() => ({ acceptInvitation: vi.fn() }));
const refresh = vi.hoisted(() => ({ refreshAccessToken: vi.fn() }));

vi.mock("../../api/auth", async () => {
  const actual = await vi.importActual<typeof import("../../api/auth")>("../../api/auth");
  return { ...actual, ...api };
});

// 守卫的状态由启动时这一次静默刷新决定：回 null 是匿名，回一张令牌是已登录。
vi.mock("../../auth/refresh", () => refresh);

const TOKEN = "invitation-token-7f3a";
const PASSWORD = "a much longer passphrase";

/** 把当前路径画出来，好断言「没有被送去登录页」与「成功后转到了登录页」。 */
function LocationProbe() {
  const location = useLocation();
  return <span data-testid="location">{location.pathname}</span>;
}

/** 按后端邀请邮件拼出来的那种链接，挂完整路由表。session 为 null 是匿名，否则是已登录的管理员。 */
function renderApp(entry: string, session: string | null = null) {
  refresh.refreshAccessToken.mockResolvedValue(session);
  return render(
    <AntdApp>
      <MemoryRouter initialEntries={[entry]}>
        <AuthProvider>
          <AppRoutes />
          <LocationProbe />
        </AuthProvider>
      </MemoryRouter>
    </AntdApp>,
  );
}

function invitationLink(token = TOKEN) {
  return `${ROUTES.acceptInvitation}?token=${encodeURIComponent(token)}`;
}

/** 某个浏览器存储里的全部键与值。 */
function storedValues(storage: Storage): string[] {
  return Array.from({ length: storage.length }, (_, index) => {
    const key = storage.key(index) ?? "";
    return `${key}=${storage.getItem(key) ?? ""}`;
  });
}

/** 等守卫拿到静默刷新的结果。之后还停在原地，才说明这条路由不受守卫管。 */
async function settleAuth() {
  await waitFor(() => {
    expect(refresh.refreshAccessToken).toHaveBeenCalled();
  });
  await act(async () => {
    await Promise.resolve();
  });
}

async function submitPassword(
  user: ReturnType<typeof userEvent.setup>,
  password: string,
  confirmation = password,
) {
  await user.click(await screen.findByLabelText("Choose a password"));
  await user.paste(password);
  await user.click(screen.getByLabelText("Confirm your password"));
  await user.paste(confirmation);
  await user.click(screen.getByRole("button", { name: "Activate my account" }));
}

// 页面分片走 `lazy()`，首次动态引入（尤其是登录页）在 CI 上可能超过默认的 1 秒。
configure({ asyncUtilTimeout: 5_000 });

beforeEach(() => {
  vi.clearAllMocks();
  resetTokenStore();
  localStorage.clear();
  sessionStorage.clear();
});

afterEach(() => {
  resetTokenStore();
});

describe("AcceptInvitationPage routing", () => {
  it("shows the form to an anonymous visitor instead of sending them to sign in", async () => {
    renderApp(invitationLink());

    expect(await screen.findByLabelText("Choose a password")).toBeInTheDocument();
    await settleAuth();

    // ⚠️ 点邀请链接的人按定义没有会话、也没有密码。被守卫送去登录页就是死链（设计 §8）。
    expect(screen.getByLabelText("Choose a password")).toBeInTheDocument();
    expect(screen.getByLabelText("Confirm your password")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Activate my account" })).toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent(ROUTES.acceptInvitation);
    expect(screen.queryByLabelText("Email")).not.toBeInTheDocument();
  });

  it("shows the same form to a signed-in administrator", async () => {
    // 管理员在自己的浏览器里点开客户的邀请链接（例如帮忙核对）也不该被挡或被转走。
    renderApp(invitationLink(), "admin-access-token");

    expect(await screen.findByLabelText("Choose a password")).toBeInTheDocument();
    await settleAuth();

    expect(screen.getByRole("button", { name: "Activate my account" })).toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent(ROUTES.acceptInvitation);
  });

  it("still sends anonymous visitors of guarded pages to sign in", async () => {
    // 对照组：同一套挂法下守卫确实会转走匿名访问 —— 否则上面那条可能只是「守卫没生效」。
    renderApp(ROUTES.dashboard);

    expect(await screen.findByLabelText("Email")).toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent(ROUTES.login);
  });
});

describe("AcceptInvitationPage", () => {
  it("sends the token from the link with the password and then goes to sign in", async () => {
    const user = userEvent.setup();
    api.acceptInvitation.mockResolvedValue({});
    renderApp(invitationLink());

    await submitPassword(user, PASSWORD);

    await waitFor(() => {
      expect(api.acceptInvitation).toHaveBeenCalledWith(TOKEN, PASSWORD);
    });
    expect(await screen.findByLabelText("Email")).toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent(ROUTES.login);
    expect(
      await screen.findByText("Your account is ready. You can now sign in with your new password."),
    ).toBeInTheDocument();
    // 令牌只进请求体，不落进浏览器存储。
    expect([...storedValues(localStorage), ...storedValues(sessionStorage)].join("\n")).not.toContain(
      TOKEN,
    );
  });

  it("does not show a form at all when the link carries no token", async () => {
    renderApp(ROUTES.acceptInvitation);

    expect(
      await screen.findByText("This invitation link is invalid or has expired"),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("Choose a password")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Go to sign in" })).toBeInTheDocument();
  });

  it("refuses to submit when the two passwords differ", async () => {
    const user = userEvent.setup();
    renderApp(invitationLink());

    await submitPassword(user, PASSWORD, "a much longer passphrasf");

    expect(await screen.findByText("The two passwords do not match.")).toBeInTheDocument();
    expect(api.acceptInvitation).not.toHaveBeenCalled();
  });

  it("takes the form away when the backend rejects the token", async () => {
    const user = userEvent.setup();
    api.acceptInvitation.mockRejectedValue(
      new ApiError("INVALID_INVITATION_TOKEN", "The invitation is no longer valid.", "req-21"),
    );
    renderApp(invitationLink());

    await submitPassword(user, PASSWORD);

    // 令牌用过了 / 过期了 —— 换个密码再试一百遍也是这个结果。
    expect(
      await screen.findByText("This invitation link is invalid or has expired"),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("Choose a password")).not.toBeInTheDocument();
    expect(screen.getByText("req-21")).toBeInTheDocument();
  });

  it("treats a missing endpoint as an invalid link", async () => {
    const user = userEvent.setup();
    // 这一页先于接口上线（设计 §8）：那段窗口里提交得到 404，`api/auth` 把它换成这个码。
    api.acceptInvitation.mockRejectedValue(
      new ApiError("INVITATION_ENDPOINT_NOT_FOUND", "Not Found", "req-22"),
    );
    renderApp(invitationLink());

    await submitPassword(user, PASSWORD);

    expect(
      await screen.findByText("This invitation link is invalid or has expired"),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("Choose a password")).not.toBeInTheDocument();
    // 框架那句「Not Found」不是给客户看的。
    expect(screen.queryByText("Not Found")).not.toBeInTheDocument();
  });

  it("keeps the form when it is the password that was rejected", async () => {
    const user = userEvent.setup();
    api.acceptInvitation.mockRejectedValue(
      new ApiError("WEAK_PASSWORD", "Password must be at least 12 characters long.", "req-23"),
    );
    renderApp(invitationLink());

    await submitPassword(user, "short");

    // ⚠️ 这个可以重试：链接还好好的，换个密码就能过。
    expect(await screen.findByText("This password is not strong enough.")).toBeInTheDocument();
    expect(screen.getByText("Password must be at least 12 characters long.")).toBeInTheDocument();
    expect(screen.getByLabelText("Choose a password")).toBeInTheDocument();
    expect(
      screen.queryByText("This invitation link is invalid or has expired"),
    ).not.toBeInTheDocument();
  });

  it("takes the form away when the account has been closed", async () => {
    const user = userEvent.setup();
    api.acceptInvitation.mockRejectedValue(
      new ApiError("ACCOUNT_CLOSED", "The customer account is closed.", "req-24"),
    );
    renderApp(invitationLink());

    await submitPassword(user, PASSWORD);

    expect(await screen.findByText("This account has been closed")).toBeInTheDocument();
    expect(screen.queryByLabelText("Choose a password")).not.toBeInTheDocument();
    expect(screen.getByText("req-24")).toBeInTheDocument();
  });

  it("does not repeat the strength rules the backend owns", async () => {
    renderApp(invitationLink());

    await screen.findByLabelText("Choose a password");
    // 最短长度只有 `validate_password_strength` 一处说了算（同 ResetPasswordPage）。
    expect(screen.queryByText(/\b\d+\s+characters?\b/)).not.toBeInTheDocument();
  });
});
