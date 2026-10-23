/**
 * 客户自己开启 2FA（AIH-TASK-097）。
 *
 * ⚠️ 要钉住的有三件：
 * - 只在 `/api/v1/me` 说还没开启时出现；
 * - 密码错、验证码错各有一句固定文案，并且**不把人退回别的步骤**（验证码错时密钥还在，可以再输）；
 * - 恢复码**只展示一次**：点「已保存」之后不在页面上，也没有任何办法再叫出来；密钥与恢复码不进存储、不进控制台。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import { ME_QUERY_KEY, type Me } from "../../api/auth";
import { ApiError } from "../../api/client";
import { EnableTwoFactor } from "./EnableTwoFactor";

const authApi = vi.hoisted(() => ({ getMe: vi.fn() }));
const customerApi = vi.hoisted(() => ({ startSelfEnrolment: vi.fn(), confirmSelfEnrolment: vi.fn() }));

vi.mock("../../api/auth", async () => {
  const actual = await vi.importActual<typeof import("../../api/auth")>("../../api/auth");
  return { ...actual, ...authApi };
});
vi.mock("../../api/customer", async () => {
  const actual = await vi.importActual<typeof import("../../api/customer")>("../../api/customer");
  return { ...actual, ...customerApi };
});
// 二维码本身由 `QrCode` 的实现负责；这里只看交给它的是不是那条 otpauth URI。
// 包一层自己的元素：表单里直接放一个文本节点的话，`getByText` 找不到只含它的那个元素。
vi.mock("../auth/QrCode", async () => {
  const { createElement } = await vi.importActual<typeof import("react")>("react");
  return { QrCode: ({ value }: { value: string }) => createElement("span", null, `qr:${value}`) };
});

const qrText = (value: string) => `qr:${value}`;
const anyQr = (_content: string, element: Element | null) =>
  element !== null && element.childElementCount === 0 && (element.textContent ?? "").startsWith("qr:");

const SECRET = "JBSWY3DPEHPK3PXP";
const OTPAUTH_URI = `otpauth://totp/Acuven:portal%40example.com?secret=${SECRET}&issuer=Acuven`;
const RECOVERY_CODES = ["AAAA-BBBB-CCCC", "DDDD-EEEE-FFFF", "GGGG-HHHH-JJJJ"];
const PASSWORD = "a-customer-perfectly-fine-passphrase";

function me(twoFactorEnabled: boolean): Me {
  return { role: "CUSTOMER", email: "portal@example.com", two_factor_enabled: twoFactorEnabled };
}

function renderEnable() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={queryClient}>
      <EnableTwoFactor />
    </QueryClientProvider>,
  );
  return { ...view, queryClient };
}

async function submitPassword(user: ReturnType<typeof userEvent.setup>, password = PASSWORD) {
  await user.click(await screen.findByLabelText("Current password"));
  await user.paste(password);
  await user.click(screen.getByRole("button", { name: "Set up two-factor authentication" }));
}

async function submitCode(user: ReturnType<typeof userEvent.setup>, code: string) {
  const input = await screen.findByLabelText("Enter the code your app shows");
  await user.clear(input);
  await user.click(input);
  await user.paste(code);
  await user.click(screen.getByRole("button", { name: "Turn on two-factor authentication" }));
}

beforeEach(() => {
  vi.clearAllMocks();
  authApi.getMe.mockResolvedValue(me(false));
  customerApi.startSelfEnrolment.mockResolvedValue({ secret: SECRET, otpauth_uri: OTPAUTH_URI });
  customerApi.confirmSelfEnrolment.mockResolvedValue({ recovery_codes: RECOVERY_CODES });
});

afterEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
  sessionStorage.clear();
});

describe("EnableTwoFactor", () => {
  it("re-verifies the password, shows the key and QR code, confirms the code and shows the recovery codes", async () => {
    const user = userEvent.setup();
    renderEnable();

    expect(await screen.findByText("Two-factor authentication")).toBeInTheDocument();
    await submitPassword(user);

    await waitFor(() => {
      expect(customerApi.startSelfEnrolment).toHaveBeenCalledWith(PASSWORD);
    });
    expect(await screen.findByText(qrText(OTPAUTH_URI))).toBeInTheDocument();
    expect(screen.getByText(SECRET)).toBeInTheDocument();
    // 密码这一步收起来了。
    expect(screen.queryByLabelText("Current password")).not.toBeInTheDocument();

    await submitCode(user, "123456");

    await waitFor(() => {
      expect(customerApi.confirmSelfEnrolment).toHaveBeenCalledWith("123456");
    });
    expect(await screen.findByText("These codes are shown once and cannot be retrieved later.")).toBeInTheDocument();
    expect(screen.getByText(/If you lose both your authenticator and these codes, contact Acuven\./)).toBeInTheDocument();
    for (const code of RECOVERY_CODES) {
      expect(screen.getByText(code)).toBeInTheDocument();
    }
    // 密钥用完了，不再显示。
    expect(screen.queryByText(SECRET)).not.toBeInTheDocument();
    expect(screen.queryByText(anyQr)).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "I have saved them" }));

    expect(
      await screen.findByText("Two-factor authentication is on. You will be asked for a code the next time you sign in."),
    ).toBeInTheDocument();
  });

  it("does not appear when two-factor authentication is already on", async () => {
    authApi.getMe.mockResolvedValue(me(true));

    const { container, queryClient } = renderEnable();

    await waitFor(() => {
      expect(queryClient.getQueryState(ME_QUERY_KEY)?.status).toBe("success");
    });
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByText("Two-factor authentication")).not.toBeInTheDocument();
    expect(customerApi.startSelfEnrolment).not.toHaveBeenCalled();
  });

  it("says the password is wrong and stays on the password step", async () => {
    const user = userEvent.setup();
    customerApi.startSelfEnrolment.mockRejectedValue(
      new ApiError("INVALID_CREDENTIALS", "Invalid credentials.", "req-enrol-401"),
    );
    renderEnable();

    await submitPassword(user, "wrong");

    expect(await screen.findByText("That password is not correct.")).toBeInTheDocument();
    expect(screen.getByText("req-enrol-401")).toBeInTheDocument();
    expect(screen.getByLabelText("Current password")).toBeInTheDocument();
    expect(screen.queryByText(anyQr)).not.toBeInTheDocument();
    expect(customerApi.confirmSelfEnrolment).not.toHaveBeenCalled();
  });

  it("says the code is wrong, keeps the key, and accepts a second try", async () => {
    const user = userEvent.setup();
    customerApi.confirmSelfEnrolment
      .mockRejectedValueOnce(new ApiError("INVALID_TOTP", "Invalid code.", "req-confirm-401"))
      .mockResolvedValueOnce({ recovery_codes: RECOVERY_CODES });
    renderEnable();

    await submitPassword(user);
    await submitCode(user, "000000");

    expect(
      await screen.findByText("That code is not correct. Enter the code your authenticator app shows now."),
    ).toBeInTheDocument();
    expect(screen.getByText("req-confirm-401")).toBeInTheDocument();
    // 还在扫码这一步：不用重新输密码。
    expect(screen.getByText(SECRET)).toBeInTheDocument();
    expect(customerApi.startSelfEnrolment).toHaveBeenCalledTimes(1);

    await submitCode(user, "123456");

    expect(await screen.findByText(RECOVERY_CODES[0] ?? "")).toBeInTheDocument();
    expect(screen.queryByText("That code is not correct. Enter the code your authenticator app shows now.")).not.toBeInTheDocument();
  });

  it("explains a 409 when it is already enrolled", async () => {
    const user = userEvent.setup();
    customerApi.startSelfEnrolment.mockRejectedValue(
      new ApiError("ALREADY_ENROLLED", "Already enrolled.", "req-enrol-409"),
    );
    renderEnable();

    await submitPassword(user);

    expect(await screen.findByText("Two-factor authentication is already on for this account.")).toBeInTheDocument();
    expect(screen.getByText("req-enrol-409")).toBeInTheDocument();
    expect(screen.queryByLabelText("Current password")).not.toBeInTheDocument();
  });

  it("shows the recovery codes only once, and keeps them and the key out of storage and the console", async () => {
    const user = userEvent.setup();
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    const consoleSpies = (["log", "info", "warn", "error", "debug"] as const).map((method) =>
      vi.spyOn(console, method),
    );
    const { queryClient } = renderEnable();

    await submitPassword(user);
    await submitCode(user, "123456");
    expect(await screen.findByText(RECOVERY_CODES[0] ?? "")).toBeInTheDocument();

    // 恢复码那一屏上 `/api/v1/me` 被重读成已开启：恢复码不能因此被收走。
    authApi.getMe.mockResolvedValue(me(true));
    await act(() => queryClient.invalidateQueries({ queryKey: ME_QUERY_KEY }));
    expect(queryClient.getQueryData<Me>(ME_QUERY_KEY)?.two_factor_enabled).toBe(true);
    expect(screen.getByText(RECOVERY_CODES[0] ?? "")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "I have saved them" }));

    await screen.findByText("Two-factor authentication is on. You will be asked for a code the next time you sign in.");
    for (const code of RECOVERY_CODES) {
      expect(screen.queryByText(code)).not.toBeInTheDocument();
    }
    // 没有任何办法再叫出来：没有重新显示的按钮，接口也只调过一次。
    expect(screen.queryByRole("button", { name: "I have saved them" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Set up two-factor authentication" })).not.toBeInTheDocument();
    expect(customerApi.confirmSelfEnrolment).toHaveBeenCalledTimes(1);

    expect(setItem).not.toHaveBeenCalled();
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
    const logged = consoleSpies.flatMap((spy) => spy.mock.calls.flat().map((arg) => String(arg))).join("\n");
    expect(logged).not.toContain(SECRET);
    for (const code of RECOVERY_CODES) {
      expect(logged).not.toContain(code);
    }
  });
});
