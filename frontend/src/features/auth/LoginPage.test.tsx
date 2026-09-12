/**
 * 登录页的三段式流程（spec §53、§54）。
 *
 * 这里测的是**状态机**：密码这一步返回什么，界面就该走到哪一段，以及下一步
 * 带出去的 `pending_token` 是不是刚拿到的那一张。这三处接错了不会报错，只会
 * 表现成「输了验证码说验证码错」——一条指不回原因的现象。
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import { ApiError } from "../../api/client";
import { LoginPage } from "./LoginPage";

const navigate = vi.fn();
const signedIn = vi.fn();

vi.mock("react-router", async () => {
  const actual = await vi.importActual<typeof import("react-router")>("react-router");
  return { ...actual, useNavigate: () => navigate };
});

vi.mock("../../auth/AuthProvider", () => ({
  useAuth: () => ({ status: "anonymous", signedIn, signOut: vi.fn() }),
}));

const api = vi.hoisted(() => ({
  login: vi.fn(),
  submitSecondFactor: vi.fn(),
  startEnrolment: vi.fn(),
  confirmEnrolment: vi.fn(),
}));

vi.mock("../../api/auth", async () => {
  const actual = await vi.importActual<typeof import("../../api/auth")>("../../api/auth");
  return { ...actual, ...api };
});

/** 后端 `LoginResult` 的空壳，用例只覆盖它关心的那几个字段。 */
function loginResult(overrides: Record<string, unknown>) {
  return {
    access_token: null,
    token_type: null,
    expires_in: null,
    stage: null,
    pending_token: null,
    recovery_codes_remaining: null,
    ...overrides,
  };
}

function renderLogin() {
  return render(
    <MemoryRouter>
      <LoginPage />
    </MemoryRouter>,
  );
}

async function fillCredentials(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText("Email"), "admin@example.com");
  await user.type(screen.getByLabelText("Password"), "correct horse battery");
  await user.click(screen.getByRole("button", { name: "Continue" }));
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("LoginPage", () => {
  it("signs in directly when the backend returns a session", async () => {
    const user = userEvent.setup();
    api.login.mockResolvedValue(loginResult({ access_token: "token-1" }));
    renderLogin();

    await fillCredentials(user);

    await waitFor(() => {
      expect(signedIn).toHaveBeenCalledWith("token-1");
    });
    expect(api.login).toHaveBeenCalledWith("admin@example.com", "correct horse battery");
    expect(navigate).toHaveBeenCalledWith("/", { replace: true });
  });

  it("asks for the second factor and carries the pending token into it", async () => {
    const user = userEvent.setup();
    api.login.mockResolvedValue(
      loginResult({ stage: "TOTP_REQUIRED", pending_token: "pending-1" }),
    );
    api.submitSecondFactor.mockResolvedValue(loginResult({ access_token: "token-2" }));
    renderLogin();

    await fillCredentials(user);

    const code = await screen.findByLabelText("Verification code");
    await user.type(code, "123456");
    await user.click(screen.getByRole("button", { name: "Verify" }));

    await waitFor(() => {
      // ⚠️ 第二步必须带**刚拿到的**那张 pending token。这里传错（空串、旧值）
      // 后端一律回「验证码无效」，于是现象指向验证码，原因却在上一步。
      expect(api.submitSecondFactor).toHaveBeenCalledWith("pending-1", "123456");
    });
    expect(signedIn).toHaveBeenCalledWith("token-2");
  });

  it("never issues a session straight from the credentials step when 2FA is pending", async () => {
    const user = userEvent.setup();
    api.login.mockResolvedValue(
      loginResult({ stage: "TOTP_REQUIRED", pending_token: "pending-1" }),
    );
    renderLogin();

    await fillCredentials(user);

    await screen.findByLabelText("Verification code");
    // 只有 pending token 的那一步**不是**登录成功。这条断言看着显然，但
    // 「先 signedIn 再让他补验证码」写起来同样顺手，而那等于 2FA 形同虚设。
    expect(signedIn).not.toHaveBeenCalled();
    expect(navigate).not.toHaveBeenCalled();
  });

  it("walks a new admin through enrolment and shows the recovery codes once", async () => {
    const user = userEvent.setup();
    api.login.mockResolvedValue(loginResult({ stage: "ENROL_2FA", pending_token: "pending-2" }));
    api.startEnrolment.mockResolvedValue({
      secret: "JBSWY3DPEHPK3PXP",
      otpauth_uri: "otpauth://totp/Acuven:admin@example.com?secret=JBSWY3DPEHPK3PXP&issuer=Acuven",
    });
    api.confirmEnrolment.mockResolvedValue({ recovery_codes: ["aaaa-bbbb", "cccc-dddd"] });
    renderLogin();

    await fillCredentials(user);

    // 扫不了码时要能手输密钥，所以密钥本身必须显示出来。
    expect(await screen.findByText("JBSWY3DPEHPK3PXP")).toBeInTheDocument();
    expect(api.startEnrolment).toHaveBeenCalledWith("pending-2");

    await user.type(screen.getByLabelText("Enter the code your app shows"), "654321");
    await user.click(screen.getByRole("button", { name: "Turn on two-factor authentication" }));

    expect(await screen.findByText("aaaa-bbbb")).toBeInTheDocument();
    expect(screen.getByText("cccc-dddd")).toBeInTheDocument();
    expect(api.confirmEnrolment).toHaveBeenCalledWith("pending-2", "654321");

    // ⚠️ 注册成功**不等于**登录成功：后端那一步只启用了 2FA，没有发会话。
    expect(signedIn).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "I have saved them" }));

    // ⚠️ 回到的是**密码那一步**，不是验证码那一步。手上那张 pending 令牌是
    // 登录时签的、只活 120 秒，扫码加抄码之后几乎必然已经过期；跳去验证码
    // 只会让用户拿到一句指向验证码的错误，而原因在两步之前。
    expect(await screen.findByLabelText("Password")).toBeInTheDocument();
    expect(
      screen.getByText("Two-factor authentication is on. Sign in again to finish."),
    ).toBeInTheDocument();
    // 恢复码只显示这一次，离开就没了 —— 界面上不许留着。
    expect(screen.queryByText("aaaa-bbbb")).not.toBeInTheDocument();
  });

  it("sends the user back to the first step when the pending token has expired", async () => {
    const user = userEvent.setup();
    api.login.mockResolvedValue(
      loginResult({ stage: "TOTP_REQUIRED", pending_token: "pending-1" }),
    );
    api.submitSecondFactor.mockRejectedValue(
      new ApiError("TOKEN_INVALID", "The token is invalid or has expired.", "req-7"),
    );
    renderLogin();

    await fillCredentials(user);
    await user.type(await screen.findByLabelText("Verification code"), "123456");
    await user.click(screen.getByRole("button", { name: "Verify" }));

    // ⚠️ 令牌一过期，那张验证码表单**再也不可能提交成功**。留在原地等于让用户
    // 对着「令牌无效」反复重输验证码 —— 现象指向验证码，原因却在 pending 令牌。
    expect(await screen.findByLabelText("Password")).toBeInTheDocument();
    expect(screen.getByText("The token is invalid or has expired.")).toBeInTheDocument();
    expect(screen.queryByLabelText("Verification code")).not.toBeInTheDocument();
  });

  it("stays on the code form when the code itself is wrong", async () => {
    const user = userEvent.setup();
    api.login.mockResolvedValue(
      loginResult({ stage: "TOTP_REQUIRED", pending_token: "pending-1" }),
    );
    api.submitSecondFactor.mockRejectedValue(
      new ApiError("INVALID_CREDENTIALS", "Invalid verification code.", "req-8"),
    );
    renderLogin();

    await fillCredentials(user);
    await user.type(await screen.findByLabelText("Verification code"), "000000");
    await user.click(screen.getByRole("button", { name: "Verify" }));

    // 输错验证码是**可以重试**的，别把人踢回去重输密码。
    expect(await screen.findByText("Invalid verification code.")).toBeInTheDocument();
    expect(screen.getByLabelText("Verification code")).toBeInTheDocument();
  });

  it("shows the backend message and the request id when login fails", async () => {
    const user = userEvent.setup();
    api.login.mockRejectedValue(
      new ApiError("INVALID_CREDENTIALS", "Invalid email or password.", "req-42"),
    );
    renderLogin();

    await fillCredentials(user);

    expect(await screen.findByText("Invalid email or password.")).toBeInTheDocument();
    // 没有 request_id 的话，用户能说的只有「登不进去」。
    expect(screen.getByText("req-42")).toBeInTheDocument();
    expect(screen.getByLabelText("Password")).toBeInTheDocument();
  });
});
