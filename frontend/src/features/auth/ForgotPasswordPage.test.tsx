/**
 * 「忘记密码」页（spec §53）。
 *
 * 这里测的主要不是「表单能不能提交」，而是**这一页有没有把后端刻意抹平的差异
 * 漏回界面上**。后端为了不让这个免鉴权端点变成用户枚举工具，对任何输入都回同一个
 * 200、同一个空 body，连耗时都补齐了 —— 界面上一句「该邮箱未注册」就能让那一整套
 * 白费。所以下面有两条用例专门盯着「两种邮箱看到的东西必须一模一样」。
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import { ApiError } from "../../api/client";
import { ForgotPasswordPage } from "./ForgotPasswordPage";

const api = vi.hoisted(() => ({ requestPasswordReset: vi.fn() }));

vi.mock("../../api/auth", async () => {
  const actual = await vi.importActual<typeof import("../../api/auth")>("../../api/auth");
  return { ...actual, ...api };
});

function renderPage() {
  return render(
    <MemoryRouter>
      <ForgotPasswordPage />
    </MemoryRouter>,
  );
}

async function submitEmail(user: ReturnType<typeof userEvent.setup>, email: string) {
  await user.type(screen.getByLabelText("Email"), email);
  await user.click(screen.getByRole("button", { name: "Send the reset link" }));
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("ForgotPasswordPage", () => {
  it("sends the address to the backend and confirms in conditional wording", async () => {
    const user = userEvent.setup();
    api.requestPasswordReset.mockResolvedValue({});
    renderPage();

    await submitEmail(user, "admin@example.com");

    expect(await screen.findByText("Check your inbox")).toBeInTheDocument();
    expect(api.requestPasswordReset).toHaveBeenCalledWith("admin@example.com");
  });

  it("never echoes the address back, which would confirm the account exists", async () => {
    const user = userEvent.setup();
    api.requestPasswordReset.mockResolvedValue({});
    renderPage();

    await submitEmail(user, "admin@example.com");
    await screen.findByText("Check your inbox");

    // ⚠️ 「信已发往 admin@example.com」是最顺手的写法，也正是那句话把后端抹平的
    // 差异漏了回来：能看到这句，就等于确认了该账号存在。
    expect(screen.queryByText(/admin@example\.com/)).not.toBeInTheDocument();
  });

  it("shows the same thing for an address that has no account", async () => {
    const user = userEvent.setup();
    // 后端对两种邮箱返回的东西完全一样，界面也就必须一样。
    api.requestPasswordReset.mockResolvedValue({});
    renderPage();

    await submitEmail(user, "nobody@example.com");

    expect(await screen.findByText("Check your inbox")).toBeInTheDocument();
    expect(
      screen.getByText(
        "If that address has an account, a reset link is on its way. The link works once and expires shortly.",
      ),
    ).toBeInTheDocument();
  });

  it("keeps the form and shows the reason when the request itself fails", async () => {
    const user = userEvent.setup();
    // 限流是按**来源**的，和邮箱存不存在无关，所以显示它不泄漏任何东西。
    api.requestPasswordReset.mockRejectedValue(
      new ApiError("RATE_LIMITED", "Too many requests. Try again later.", "req-9"),
    );
    renderPage();

    await submitEmail(user, "admin@example.com");

    expect(await screen.findByText("Too many requests. Try again later.")).toBeInTheDocument();
    // 没有 request_id，用户能说的只有「收不到信」。
    expect(screen.getByText("req-9")).toBeInTheDocument();
    // 失败了就得还能再试一次 —— 把表单换掉等于把人卡死在这一页。
    expect(screen.getByLabelText("Email")).toBeInTheDocument();
  });

  it("does not call the backend at all when the address is blank", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByRole("button", { name: "Send the reset link" }));

    await waitFor(() => {
      expect(screen.queryByText("Check your inbox")).not.toBeInTheDocument();
    });
    expect(api.requestPasswordReset).not.toHaveBeenCalled();
  });
});
