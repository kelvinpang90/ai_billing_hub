/**
 * 低余额阈值面板：显示（2 位、未设置）、修改后重读、清除发 null、客户端校验拦下不合格的值、失败带 request_id。
 *
 * 校验那几条测的是**没有发出去的请求**：前端放过一个 `"1.001"`，管理员看到的是一句 422；
 * 放过一个 `"0"` 更糟 —— 后端同样 422，但管理员以为 0 是「关掉提醒」。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import { ApiError } from "../../api/client";
import { LowBalanceThresholdPanel, thresholdProblem } from "./LowBalanceThresholdPanel";

const CUSTOMER_ID = "00000000-0000-0000-0000-000000000000";

const api = vi.hoisted(() => ({
  getLowBalanceThreshold: vi.fn(),
  setLowBalanceThreshold: vi.fn(),
}));

vi.mock("../../api/adminCustomers", async () => {
  const actual =
    await vi.importActual<typeof import("../../api/adminCustomers")>("../../api/adminCustomers");
  return { ...actual, ...api };
});

const MESSAGES = {
  required: "Enter a threshold.",
  invalid:
    "Enter an amount with up to 12 digits before and 2 after the decimal point, without signs or spaces.",
  zero: "The threshold must be greater than 0. To turn the warning off, clear the threshold instead.",
};

function renderPanel() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <LowBalanceThresholdPanel customerId={CUSTOMER_ID} />
    </QueryClientProvider>,
  );
}

/** 面板里带边框的 Descriptions：label 在 <th>，值在紧跟的 <td>。 */
async function thresholdCell(): Promise<HTMLElement> {
  const label = await screen.findByText("Threshold");
  const cell = label.closest("th")?.nextElementSibling;
  if (!(cell instanceof HTMLElement)) {
    throw new Error("the threshold has no value cell");
  }
  return cell;
}

/** 打开修改表单，原样贴进 value，点保存。 */
async function submitThreshold(user: ReturnType<typeof userEvent.setup>, value: string) {
  await user.click(await screen.findByRole("button", { name: "Change" }));
  await user.click(screen.getByLabelText("New threshold"));
  await user.paste(value);
  await user.click(screen.getByRole("button", { name: "Save threshold" }));
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("LowBalanceThresholdPanel", () => {
  it("says it is loading while the threshold is being read", async () => {
    api.getLowBalanceThreshold.mockReturnValue(new Promise(() => undefined));

    renderPanel();

    expect(await screen.findByText("Loading the low balance threshold…")).toBeInTheDocument();
    expect(api.getLowBalanceThreshold).toHaveBeenCalledWith(CUSTOMER_ID, expect.anything());
    expect(screen.queryByRole("button", { name: "Change" })).not.toBeInTheDocument();
  });

  it.each([
    { stored: "50.00000000", shown: "50.00" },
    { stored: "7.50000000", shown: "7.50" },
    { stored: "0.01000000", shown: "0.01" },
    { stored: "999999999999.99000000", shown: "999,999,999,999.99" },
    // 设计 §2「金额」：入参最多 2 位，但显示仍按 2 位 ROUND_HALF_UP 舍入，不截断。
    { stored: "50.00500000", shown: "50.01" },
  ])("shows the stored threshold $stored with 2 decimals as $shown", async ({ stored, shown }) => {
    api.getLowBalanceThreshold.mockResolvedValue({ low_balance_threshold: stored });

    renderPanel();

    // 逐字相同：只有舍入后的金额，没有币种、成本或别的字段。
    expect((await thresholdCell()).textContent).toBe(shown);
    expect(screen.queryByText("Not set")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Clear" })).toBeEnabled();
  });

  it("says the threshold is not set when it is null and offers nothing to clear", async () => {
    api.getLowBalanceThreshold.mockResolvedValue({ low_balance_threshold: null });

    renderPanel();

    expect(await thresholdCell()).toHaveTextContent("Not set");
    expect(screen.getByRole("button", { name: "Change" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Clear" })).toBeDisabled();
  });

  it("shows a load failure with its request id and reads again on retry", async () => {
    const user = userEvent.setup();
    api.getLowBalanceThreshold
      .mockRejectedValueOnce(
        new ApiError("DATABASE_NOT_CONFIGURED", "The database is not configured.", "req-503"),
      )
      .mockResolvedValue({ low_balance_threshold: null });

    renderPanel();

    expect(
      await screen.findByText("The low balance threshold could not be loaded."),
    ).toBeInTheDocument();
    expect(screen.getByText("req-503")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await thresholdCell()).toHaveTextContent("Not set");
    expect(api.getLowBalanceThreshold).toHaveBeenCalledTimes(2);
  });

  it("sends the typed string as is and reads the threshold again after saving", async () => {
    const user = userEvent.setup();
    api.getLowBalanceThreshold
      .mockResolvedValueOnce({ low_balance_threshold: null })
      .mockResolvedValue({ low_balance_threshold: "75.50000000" });
    api.setLowBalanceThreshold.mockResolvedValue({ low_balance_threshold: "75.50000000" });
    renderPanel();

    await submitThreshold(user, "75.5");

    expect(await screen.findByText("Low balance threshold saved.")).toBeInTheDocument();
    expect(api.setLowBalanceThreshold).toHaveBeenCalledTimes(1);
    // 字符串，原样：不补零、不转成 number。
    expect(api.setLowBalanceThreshold).toHaveBeenCalledWith(CUSTOMER_ID, "75.5");
    await waitFor(() => {
      expect(api.getLowBalanceThreshold).toHaveBeenCalledTimes(2);
    });
    expect(await screen.findByText("75.50")).toBeInTheDocument();
    expect(screen.queryByLabelText("New threshold")).not.toBeInTheDocument();
  });

  it("clears the threshold by sending null after confirmation and reads it again", async () => {
    const user = userEvent.setup();
    api.getLowBalanceThreshold
      .mockResolvedValueOnce({ low_balance_threshold: "50.00000000" })
      .mockResolvedValue({ low_balance_threshold: null });
    api.setLowBalanceThreshold.mockResolvedValue({ low_balance_threshold: null });
    renderPanel();

    await user.click(await screen.findByRole("button", { name: "Clear" }));
    const dialog = await screen.findByRole("dialog");
    expect(api.setLowBalanceThreshold).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole("button", { name: "Clear threshold" }));

    expect(await screen.findByText("Low balance threshold cleared.")).toBeInTheDocument();
    expect(api.setLowBalanceThreshold).toHaveBeenCalledTimes(1);
    expect(api.setLowBalanceThreshold).toHaveBeenCalledWith(CUSTOMER_ID, null);
    await waitFor(() => {
      expect(api.getLowBalanceThreshold).toHaveBeenCalledTimes(2);
    });
    expect(await thresholdCell()).toHaveTextContent("Not set");
  });

  it("does not clear when the confirmation is cancelled", async () => {
    const user = userEvent.setup();
    api.getLowBalanceThreshold.mockResolvedValue({ low_balance_threshold: "50.00000000" });
    renderPanel();

    await user.click(await screen.findByRole("button", { name: "Clear" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));

    expect(api.setLowBalanceThreshold).not.toHaveBeenCalled();
  });

  it.each([
    { value: "0", message: MESSAGES.zero },
    { value: "0.00", message: MESSAGES.zero },
    { value: "-1", message: MESSAGES.invalid },
    { value: "-50.00", message: MESSAGES.invalid },
    { value: "1.001", message: MESSAGES.invalid },
    { value: "abc", message: MESSAGES.invalid },
    { value: "5O", message: MESSAGES.invalid },
    { value: "1e3", message: MESSAGES.invalid },
  ])("rejects $value on the client and sends nothing", async ({ value, message }) => {
    const user = userEvent.setup();
    api.getLowBalanceThreshold.mockResolvedValue({ low_balance_threshold: null });
    renderPanel();

    await submitThreshold(user, value);

    expect(await screen.findByText(message)).toBeInTheDocument();
    expect(api.setLowBalanceThreshold).not.toHaveBeenCalled();
  });

  it("asks for a value when the field is left empty and sends nothing", async () => {
    const user = userEvent.setup();
    api.getLowBalanceThreshold.mockResolvedValue({ low_balance_threshold: null });
    renderPanel();

    await user.click(await screen.findByRole("button", { name: "Change" }));
    await user.click(screen.getByRole("button", { name: "Save threshold" }));

    expect(await screen.findByText(MESSAGES.required)).toBeInTheDocument();
    expect(api.setLowBalanceThreshold).not.toHaveBeenCalled();
  });

  it("shows a rejected save with its request id and keeps the form", async () => {
    const user = userEvent.setup();
    api.getLowBalanceThreshold.mockResolvedValue({ low_balance_threshold: null });
    api.setLowBalanceThreshold.mockRejectedValue(
      new ApiError("VALIDATION_ERROR", "Invalid field: body.low_balance_threshold", "req-422"),
    );
    renderPanel();

    await submitThreshold(user, "50");

    expect(
      await screen.findByText("The low balance threshold was not saved."),
    ).toBeInTheDocument();
    expect(screen.getByText("req-422")).toBeInTheDocument();
    expect(screen.getByLabelText("New threshold")).toHaveValue("50");
    expect(screen.queryByText("Low balance threshold saved.")).not.toBeInTheDocument();
    // 失败不重读。
    expect(api.getLowBalanceThreshold).toHaveBeenCalledTimes(1);
  });

  it("shows a rejected clear with its request id inside the confirmation", async () => {
    const user = userEvent.setup();
    api.getLowBalanceThreshold.mockResolvedValue({ low_balance_threshold: "50.00000000" });
    api.setLowBalanceThreshold.mockRejectedValue(
      new ApiError("INTERNAL_ERROR", "Something went wrong.", "req-500"),
    );
    renderPanel();

    await user.click(await screen.findByRole("button", { name: "Clear" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Clear threshold" }));

    expect(
      await within(dialog).findByText("The low balance threshold was not saved."),
    ).toBeInTheDocument();
    expect(within(dialog).getByText("req-500")).toBeInTheDocument();
    expect(await thresholdCell()).toHaveTextContent("50.00");
  });
});

describe("thresholdProblem", () => {
  it.each([
    { value: "50", problem: null },
    { value: "50.0", problem: null },
    { value: "0.01", problem: null },
    { value: "999999999999.99", problem: null },
    { value: "", problem: "required" },
    { value: undefined, problem: "required" },
    { value: "0", problem: "zero" },
    { value: "000.00", problem: "zero" },
    { value: "1000000000000", problem: "invalid" },
    { value: "5.", problem: "invalid" },
    { value: ".5", problem: "invalid" },
    { value: " 5", problem: "invalid" },
    { value: "5 ", problem: "invalid" },
    { value: "+5", problem: "invalid" },
    { value: "1,000", problem: "invalid" },
    // 全角数字：后端用 [0-9] 拒收，这里同样不认。
    { value: "１", problem: "invalid" },
  ])("judges $value as $problem, like the backend", ({ value, problem }) => {
    expect(thresholdProblem(value)).toBe(problem);
  });
});
