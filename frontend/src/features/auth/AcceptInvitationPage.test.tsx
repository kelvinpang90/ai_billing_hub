/**
 * 「接受邀请」页（设计 AIH-TASK-089 §2、§7、§8，任务 C）。
 *
 * 这里挂的是**完整的路由表**（`AppRoutes`），不是单独这一页：§7「邀请链接匿名可达」要钉住的正是
 * 「这条路由在 `RequireAuth` 外面」。只渲染页面组件的话，路由被挪进守卫里这些用例也照样全绿，
 * 而线上的现象是客户点开邀请链接、看到一张他没有密码可填的登录表单。
 *
 * 其余用例测的是哪种错误该留在表单上、哪种必须把表单收走（理由同 `ResetPasswordPage.test.tsx`）。
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App as AntdApp } from "antd";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import { ApiError } from "../../api/client";
import { AuthProvider } from "../../auth/AuthProvider";
import { resetTokenStore, setAccessToken } from "../../auth/tokenStore";
import { AppRoutes } from "../../routes";

const api = vi.hoisted(() => ({ acceptInvitation: vi.fn() }));
const refresh = vi.hoisted(() => ({ refreshAccessToken: vi.fn() }));

vi.mock("../../api/auth", async () => {
  const actual = await vi.importActual<typeof import("../../api/auth")>("../../api/auth");
  return { ...actual, ...api };
});

// 登录状态由启动时的静默刷新决定：回 null 是匿名，回一张令牌是已登录。
vi.mock("../../auth/refresh", () => refresh);

/** 页面分片是 `lazy()` 引入的，第一次挂载要等它下载完。 */
const LAZY_WAIT = { timeout: 5_000 };

const TOKEN = "invite-token-1";

function signedOut() {
  refresh.refreshAccessToken.mockResolvedValue(null);
}

/** 已登录的管理员（例如管理员在自己的浏览器里点开了邀请链接）。 */
function signedInAsAdmin() {
  refresh.refreshAccessToken.mockImplementation(() => {
    setAccessToken("admin-access-token");
    return Promise.resolve("admin-access-token");
  });
}

/** 按设计 §2 的邀请链接形状，经完整路由表渲染。 */
function renderAt(search = `?token=${TOKEN}`) {
  return render(
    <AntdApp>
      <MemoryRouter initialEntries={[`/accept-invitation${search}`]}>
        <AuthProvider>
          <AppRoutes />
        </AuthProvider>
      </MemoryRouter>
    </AntdApp>,
  );
}

async function submitPassword(
  user: ReturnType<typeof userEvent.setup>,
  password: string,
  confirmation = password,
) {
  await user.click(await screen.findByLabelText("Choose a password", {}, LAZY_WAIT));
  await user.paste(password);
  await user.click(screen.getByLabelText("Confirm your password"));
  await user.paste(confirmation);
  await user.click(screen.getByRole("button", { name: "Activate my account" }));
}

beforeEach(() => {
  vi.clearAllMocks();
  resetTokenStore();
  signedOut();
});

afterEach(() => {
  resetTokenStore();
  vi.restoreAllMocks();
});

describe("AcceptInvitationPage", () => {
  it("shows the form to an anonymous visitor instead of sending them to sign in", async () => {
    renderAt("?token=x");

    // ⚠️ 设计 §7「邀请链接匿名可达」。点邀请链接的客户还没有密码，送去登录页就是死链。
    expect(await screen.findByLabelText("Choose a password", {}, LAZY_WAIT)).toBeInTheDocument();
    expect(screen.getByLabelText("Confirm your password")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Activate my account" })).toBeInTheDocument();
    // 等静默刷新回来（匿名）之后再看一次：守卫是在那之后才会把人踢走的。
    await waitFor(() => {
      expect(refresh.refreshAccessToken).toHaveBeenCalled();
    });
    expect(screen.queryByLabelText("Email")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Choose a password")).toBeInTheDocument();
  });

  it("shows the same form when an administrator is already signed in", async () => {
    signedInAsAdmin();
    renderAt("?token=x");

    expect(await screen.findByLabelText("Choose a password", {}, LAZY_WAIT)).toBeInTheDocument();
    await waitFor(() => {
      expect(refresh.refreshAccessToken).toHaveBeenCalled();
    });
    // 不看登录状态：既不跳登录页，也不被套进管理端外壳。
    expect(screen.queryByLabelText("Email")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Sign out" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Activate my account" })).toBeInTheDocument();
  });

  it("does not show a form at all when the link carries no token", async () => {
    renderAt("");

    expect(
      await screen.findByText("This invitation link is invalid or has expired", {}, LAZY_WAIT),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("Choose a password")).not.toBeInTheDocument();
    expect(api.acceptInvitation).not.toHaveBeenCalled();
  });

  it("refuses to submit when the two passwords differ", async () => {
    const user = userEvent.setup();
    renderAt();

    await submitPassword(user, "a much longer passphrase", "a much longer passphrasf");

    expect(await screen.findByText("The two passwords do not match.")).toBeInTheDocument();
    expect(api.acceptInvitation).not.toHaveBeenCalled();
  });

  it("sends the token from the link and moves on to sign in", async () => {
    const user = userEvent.setup();
    api.acceptInvitation.mockResolvedValue({});
    renderAt();

    await submitPassword(user, "a much longer passphrase");

    await waitFor(() => {
      expect(api.acceptInvitation).toHaveBeenCalledWith(TOKEN, "a much longer passphrase");
    });
    // 转到登录页，并告诉客户现在可以登录了。
    expect(await screen.findByLabelText("Email", {}, LAZY_WAIT)).toBeInTheDocument();
    expect(await screen.findByText("Your account is ready. You can sign in now.")).toBeInTheDocument();
    expect(screen.queryByLabelText("Choose a password")).not.toBeInTheDocument();
  });

  it("takes the form away when the backend rejects the token", async () => {
    const user = userEvent.setup();
    api.acceptInvitation.mockRejectedValue(
      new ApiError("INVALID_INVITATION_TOKEN", "The invitation is no longer valid.", "req-21"),
    );
    renderAt();

    await submitPassword(user, "a much longer passphrase");

    expect(
      await screen.findByText("This invitation link is invalid or has expired"),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("Choose a password")).not.toBeInTheDocument();
    // 提交确认丢失后的重试也是这个码（设计 §5）：告诉客户密码可能已经设好了。
    expect(
      screen.getByText("If you have already set your password with this link, sign in instead."),
    ).toBeInTheDocument();
    expect(screen.getByText("req-21")).toBeInTheDocument();
  });

  it("treats a missing endpoint as an invalid link", async () => {
    const user = userEvent.setup();
    // 页面先于接口上线（设计 §8）：那段时间里接口是 404，封装把它换成这个码。
    api.acceptInvitation.mockRejectedValue(
      new ApiError("INVITATION_ENDPOINT_NOT_FOUND", "Not Found", "req-24"),
    );
    renderAt();

    await submitPassword(user, "a much longer passphrase");

    expect(
      await screen.findByText("This invitation link is invalid or has expired"),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("Choose a password")).not.toBeInTheDocument();
    // 框架那句「Not Found」对客户毫无意义，不显示。
    expect(screen.queryByText("Not Found")).not.toBeInTheDocument();
  });

  it("keeps the form when it is the password that was rejected", async () => {
    const user = userEvent.setup();
    api.acceptInvitation.mockRejectedValue(
      new ApiError("WEAK_PASSWORD", "Password must be at least 12 characters long.", "req-22"),
    );
    renderAt();

    await submitPassword(user, "short");

    // ⚠️ 可以重试：后端在消费令牌之前做强度校验（设计 §4 第 5 步），链接还好好的。
    expect(await screen.findByText("That password is not strong enough.")).toBeInTheDocument();
    expect(screen.getByText("Password must be at least 12 characters long.")).toBeInTheDocument();
    expect(screen.getByLabelText("Choose a password")).toBeInTheDocument();
    expect(
      screen.queryByText("This invitation link is invalid or has expired"),
    ).not.toBeInTheDocument();
  });

  it("takes the form away when the account has been closed", async () => {
    const user = userEvent.setup();
    api.acceptInvitation.mockRejectedValue(
      new ApiError("ACCOUNT_CLOSED", "The account is closed.", "req-23"),
    );
    renderAt();

    await submitPassword(user, "a much longer passphrase");

    expect(await screen.findByText("This account has been closed")).toBeInTheDocument();
    expect(screen.queryByLabelText("Choose a password")).not.toBeInTheDocument();
    expect(screen.getByText("req-23")).toBeInTheDocument();
  });

  it("keeps the token out of storage and the console", async () => {
    const user = userEvent.setup();
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    const consoleCalls = (["log", "info", "warn", "error", "debug"] as const).map((level) =>
      vi.spyOn(console, level),
    );
    api.acceptInvitation.mockResolvedValue({});
    renderAt();

    await submitPassword(user, "a much longer passphrase");
    await screen.findByLabelText("Email", {}, LAZY_WAIT);

    const calls: unknown[][] = [
      ...setItem.mock.calls,
      ...consoleCalls.flatMap((spy) => spy.mock.calls),
    ];
    expect(calls.filter((args) => args.some((arg) => String(arg).includes(TOKEN)))).toEqual([]);
  });

  it("does not repeat the strength rules the backend owns", async () => {
    renderAt();

    await screen.findByLabelText("Choose a password", {}, LAZY_WAIT);
    expect(screen.queryByText(/\b\d+\s+characters?\b/)).not.toBeInTheDocument();
  });
});
