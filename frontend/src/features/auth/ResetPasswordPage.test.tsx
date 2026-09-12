/**
 * 「重置密码」页（spec §53）。
 *
 * 这里测的是**哪种错误该留在表单上、哪种错误必须把表单收走**。两者搞反不会
 * 报错，只会表现成用户对着一张永远提交不成功的表单反复改密码 —— 一条指不回
 * 原因的现象（T0.8c 在 pending 令牌上踩过同一个坑）。
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import { ApiError } from "../../api/client";
import { ResetPasswordPage } from "./ResetPasswordPage";

const api = vi.hoisted(() => ({ resetPassword: vi.fn() }));

vi.mock("../../api/auth", async () => {
  const actual = await vi.importActual<typeof import("../../api/auth")>("../../api/auth");
  return { ...actual, ...api };
});

/** 按后端 `_render_password_reset` 拼出来的那种链接渲染这一页。 */
function renderPage(search = "?token=reset-token-1") {
  return render(
    <MemoryRouter initialEntries={[`/reset-password${search}`]}>
      <ResetPasswordPage />
    </MemoryRouter>,
  );
}

async function submitPassword(
  user: ReturnType<typeof userEvent.setup>,
  password: string,
  confirmation = password,
) {
  await user.type(screen.getByLabelText("New password"), password);
  await user.type(screen.getByLabelText("Confirm new password"), confirmation);
  await user.click(screen.getByRole("button", { name: "Change my password" }));
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("ResetPasswordPage", () => {
  it("sends the token from the link together with the new password", async () => {
    const user = userEvent.setup();
    api.resetPassword.mockResolvedValue({});
    renderPage();

    await submitPassword(user, "a much longer passphrase");

    await waitFor(() => {
      // ⚠️ 令牌必须是**链接里那一张**。这里传错（空串、写死的值）后端一律回
      // 「令牌无效」，于是现象指向链接，原因却在这一行。
      expect(api.resetPassword).toHaveBeenCalledWith("reset-token-1", "a much longer passphrase");
    });
    expect(await screen.findByText("Your password has been changed")).toBeInTheDocument();
  });

  it("reads a url-escaped token back exactly as the backend wrote it", async () => {
    const user = userEvent.setup();
    api.resetPassword.mockResolvedValue({});
    // 后端拼链接时对令牌做了 percent-encoding，这里要拿回原值而不是转义后的串。
    renderPage("?token=abc%2Bdef%3D");

    await submitPassword(user, "a much longer passphrase");

    await waitFor(() => {
      expect(api.resetPassword).toHaveBeenCalledWith("abc+def=", "a much longer passphrase");
    });
  });

  it("does not show a form at all when the link carries no token", () => {
    renderPage("");

    // ⚠️ 没有令牌时那张表单**再也不可能提交成功**。摆出来等于请用户做一件
    // 注定失败的事，还会让他以为问题出在密码上。
    expect(screen.queryByLabelText("New password")).not.toBeInTheDocument();
    expect(screen.getByText("This link cannot be used")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Send me a new link" })).toBeInTheDocument();
  });

  it("takes the form away when the backend rejects the token", async () => {
    const user = userEvent.setup();
    api.resetPassword.mockRejectedValue(
      new ApiError("INVALID_RESET_TOKEN", "The reset link is no longer valid.", "req-11"),
    );
    renderPage();

    await submitPassword(user, "a much longer passphrase");

    // 令牌用过了 / 过期了 —— 换个密码再试一百遍也是这个结果。
    expect(await screen.findByText("This link cannot be used")).toBeInTheDocument();
    expect(screen.queryByLabelText("New password")).not.toBeInTheDocument();
    // 后端的说法比我们的泛泛而谈准确，而 request_id 是报给支持的唯一钥匙。
    expect(screen.getByText("The reset link is no longer valid.")).toBeInTheDocument();
    expect(screen.getByText("req-11")).toBeInTheDocument();
  });

  it("keeps the form when it is the password that was rejected", async () => {
    const user = userEvent.setup();
    api.resetPassword.mockRejectedValue(
      new ApiError("WEAK_PASSWORD", "Password must be at least 12 characters long.", "req-12"),
    );
    renderPage();

    await submitPassword(user, "short");

    // ⚠️ 这个是**可以重试**的：链接还好好的，换个密码就能过。把表单收走等于
    // 让用户为了一个打字问题重新去要一封邮件。
    expect(
      await screen.findByText("Password must be at least 12 characters long."),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("New password")).toBeInTheDocument();
    expect(screen.queryByText("This link cannot be used")).not.toBeInTheDocument();
  });

  it("refuses to submit when the two passwords differ", async () => {
    const user = userEvent.setup();
    renderPage();

    await submitPassword(user, "a much longer passphrase", "a much longer passphrasf");

    expect(await screen.findByText("The two passwords do not match.")).toBeInTheDocument();
    // 打错一个字母就把密码改成自己不知道的东西，下次登录才发现 —— 挡在这里。
    expect(api.resetPassword).not.toHaveBeenCalled();
  });

  it("does not repeat the strength rules the backend owns", () => {
    renderPage();

    // ⚠️ 最短长度只有 `validate_password_strength` 一处说了算。前端再写一遍的话，
    // 后端一改，这里就变成一句**安静地说错**的提示。这条用例钉住「不许写死数字」。
    expect(screen.queryByText(/\b\d+\s+characters?\b/)).not.toBeInTheDocument();
  });
});
