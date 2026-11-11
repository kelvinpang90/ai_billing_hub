/**
 * 管理端支付设置页（AIH-TASK-194，设计闸门 #587 v3 §2「管理端页面」）。
 *
 * 与支付列表、详情页的测试同一写法：不 mock `api/adminPaymentSettings`，而是在 `client.get` / `client.put` 上回应，钉住
 * **页面真正发出去的地址与请求体**，并断言没有问 `/api/v1/customer/` 路径。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import type { PaymentSettings } from "../../api/adminPaymentSettings";
import { ApiError, client } from "../../api/client";
import { ROUTES } from "../../routes/paths";
import { PaymentSettingsPage } from "./PaymentSettingsPage";

const ZERO_UUID = "00000000-0000-0000-0000-000000000000";
const SETTINGS_URL = "/api/v1/admin/payment-settings";

/** 让请求一直挂着（加载中 / 保存中）。 */
const PENDING = Symbol("pending");

/** 迁移写入的种子行：从未改过，`updated_by` 为 null。 */
function seed(overrides: Partial<PaymentSettings> = {}): PaymentSettings {
  return {
    min_amount: "10.00",
    max_amount: null,
    recovery_buffer: "10.00",
    preset_amounts: ["50.00", "100.00", "300.00", "500.00", "1000.00"],
    reuse_window_seconds: 60,
    pending_timeout_seconds: 3600,
    version: 1,
    updated_at: "2026-11-08T00:00:00",
    updated_by: null,
    ...overrides,
  };
}

function envelope(data: unknown) {
  return { data: { success: true, data, error: null, request_id: "req-1" } };
}

/** 按路径回应 `client.get`：只认设置接口，别的路径一律当成失败 —— 页面不该问它们。 */
function serve(...replies: (() => unknown)[]) {
  let call = 0;
  return vi.spyOn(client, "get").mockImplementation((url: string) => {
    let answer: unknown;
    if (url === SETTINGS_URL) {
      const reply = replies[Math.min(call, replies.length - 1)];
      call += 1;
      answer = reply === undefined ? new ApiError("NOT_FOUND", "Unexpected request.", null) : reply();
    } else {
      answer = new ApiError("NOT_FOUND", "Unexpected request.", null);
    }
    if (answer === PENDING) {
      return new Promise(() => undefined);
    }
    return answer instanceof Error ? Promise.reject(answer) : Promise.resolve(envelope(answer));
  });
}

function serveSave(reply: () => unknown) {
  return vi.spyOn(client, "put").mockImplementation(() => {
    const answer = reply();
    if (answer === PENDING) {
      return new Promise(() => undefined);
    }
    return answer instanceof Error ? Promise.reject(answer) : Promise.resolve(envelope(answer));
  });
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[ROUTES.paymentSettings]}>
        <Routes>
          <Route path={ROUTES.paymentSettings} element={<PaymentSettingsPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const LABEL = {
  min: "Minimum top-up (MYR)",
  max: "Maximum top-up (MYR)",
  buffer: "Recovery buffer (MYR)",
  presets: "Suggested amounts (MYR)",
  reuse: "Reuse window (seconds)",
  timeout: "Payment page timeout (seconds)",
} as const;

function input(label: string): HTMLInputElement {
  return screen.getByLabelText<HTMLInputElement>(label);
}

function type(label: string, value: string): void {
  fireEvent.change(input(label), { target: { value } });
}

/** 保存中 antd 在按钮里加一个 aria-label 为「loading」的图标，按钮的可读名称随之变长，所以按包含匹配。 */
function saveButton(): HTMLElement {
  return screen.getByRole("button", { name: /Save settings/ });
}

async function loaded(): Promise<void> {
  await screen.findByLabelText(LABEL.min);
}

/** 版本、修改人、修改时间那张表：标签 → 显示的文字。 */
function meta(): Record<string, string | null> {
  const descriptions = document.querySelector<HTMLElement>(".ant-descriptions");
  if (descriptions === null) {
    throw new Error("no descriptions");
  }
  return Object.fromEntries(
    Array.from(descriptions.querySelectorAll(".ant-descriptions-item-label"), (label) => [
      label.textContent ?? "",
      label.nextElementSibling?.textContent ?? null,
    ]),
  );
}

function askedCustomerPaths(get: ReturnType<typeof serve>): boolean {
  return get.mock.calls.some(([url]) => String(url).startsWith("/api/v1/customer/"));
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("PaymentSettingsPage — reading", () => {
  it("shows a loading state while the settings are read", async () => {
    serve(() => PENDING);

    renderPage();

    expect(await screen.findByText("Loading payment settings…")).toBeInTheDocument();
    expect(screen.queryByLabelText(LABEL.min)).not.toBeInTheDocument();
  });

  it("shows the failure with its code and request id, and can try again", async () => {
    const get = serve(
      () => new ApiError("INTERNAL_ERROR", "Something went wrong.", "req-500"),
      () => seed(),
    );

    renderPage();

    expect(await screen.findByText("The payment settings could not be loaded.")).toBeInTheDocument();
    expect(screen.getByText("INTERNAL_ERROR")).toBeInTheDocument();
    expect(screen.getByText("req-500")).toBeInTheDocument();
    expect(screen.queryByLabelText(LABEL.min)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Try again" }));

    await loaded();
    expect(get).toHaveBeenCalledTimes(2);
  });

  it("fills the form from the settings and shows the version, editor and time", async () => {
    const get = serve(() => seed({ max_amount: "5000.00" }));

    renderPage();
    await loaded();

    expect(get).toHaveBeenCalledWith(SETTINGS_URL, expect.anything());
    expect(askedCustomerPaths(get)).toBe(false);
    expect(input(LABEL.min).value).toBe("10.00");
    expect(input(LABEL.max).value).toBe("5000.00");
    expect(input(LABEL.buffer).value).toBe("10.00");
    expect(input(LABEL.presets).value).toBe("50.00, 100.00, 300.00, 500.00, 1000.00");
    expect(input(LABEL.reuse).value).toBe("60");
    expect(input(LABEL.timeout).value).toBe("3600");
    expect(meta()).toEqual({
      Version: "1",
      // 种子行从未改过：修改人显示短横。
      "Last changed by": "—",
      // 不带时区的 UTC 按吉隆坡时间（+8）显示。
      "Last changed at (MYT)": "2026-11-08 08:00:00",
    });
  });

  it("leaves the maximum empty when it is not set, and shows the editor's email once changed", async () => {
    serve(() => seed({ version: 7, updated_by: "admin@example.com", updated_at: "2026-11-09T16:30:00" }));

    renderPage();
    await loaded();

    expect(input(LABEL.max).value).toBe("");
    expect(meta()).toEqual({
      Version: "7",
      "Last changed by": "admin@example.com",
      "Last changed at (MYT)": "2026-11-10 00:30:00",
    });
  });
});

describe("PaymentSettingsPage — saving", () => {
  it("sends every field with the version it read, amounts trimmed but otherwise as typed", async () => {
    const get = serve(() => seed({ version: 4 }));
    const put = serveSave(() => seed({ version: 5 }));

    renderPage();
    await loaded();
    type(LABEL.min, "  20.5 ");
    type(LABEL.max, " 8000 ");
    type(LABEL.buffer, "\t1\t");
    type(LABEL.reuse, " 0 ");
    type(LABEL.timeout, "2400");
    fireEvent.click(saveButton());

    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));
    expect(put).toHaveBeenCalledWith(SETTINGS_URL, {
      min_amount: "20.5",
      max_amount: "8000",
      recovery_buffer: "1",
      preset_amounts: ["50.00", "100.00", "300.00", "500.00", "1000.00"],
      reuse_window_seconds: 0,
      pending_timeout_seconds: 2400,
      version: 4,
    });
    expect(askedCustomerPaths(get)).toBe(false);
  });

  it("splits the suggested amounts on commas, trims each one and drops empty parts", async () => {
    serve(() => seed());
    const put = serveSave(() => seed({ version: 2 }));

    renderPage();
    await loaded();
    type(LABEL.presets, "50, 100,,300, ");
    fireEvent.click(saveButton());

    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));
    expect(put.mock.calls[0]?.[1]).toMatchObject({ preset_amounts: ["50", "100", "300"] });
  });

  it("sends null for a maximum that is empty or only spaces", async () => {
    serve(() => seed({ max_amount: "5000.00" }));
    const put = serveSave(() => seed({ version: 2 }));

    renderPage();
    await loaded();
    type(LABEL.max, "");
    fireEvent.click(saveButton());

    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));
    const first = put.mock.calls[0]?.[1] as Record<string, unknown>;
    expect(Object.hasOwn(first, "max_amount")).toBe(true);
    expect(first).toMatchObject({ max_amount: null });

    // 保存成功后表单换成返回的值（最高额为空）；再填只有空白的最高额。
    await screen.findByText("The payment settings were saved.");
    type(LABEL.max, "   ");
    fireEvent.click(saveButton());

    await waitFor(() => expect(put).toHaveBeenCalledTimes(2));
    expect(put.mock.calls[1]?.[1]).toMatchObject({ max_amount: null, version: 2 });
  });

  it.each([
    ["empty", ""],
    ["only spaces", "   "],
    ["a decimal", "1.5"],
    ["negative", "-1"],
    ["with letters", "6O"],
    ["non-ASCII digits", "٦٠"],
  ])("does not send anything when a seconds box is %s", async (_case, value) => {
    serve(() => seed());
    const put = serveSave(() => seed({ version: 2 }));

    renderPage();
    await loaded();
    type(LABEL.reuse, value);
    fireEvent.click(saveButton());

    expect(await screen.findByText("Enter a whole number of seconds using digits only, for example 60.")).toBeInTheDocument();
    expect(put).not.toHaveBeenCalled();

    // 另一个秒数框同一规则。
    type(LABEL.reuse, "60");
    type(LABEL.timeout, value);
    fireEvent.click(saveButton());

    expect(await screen.findByText("Enter a whole number of seconds using digits only, for example 60.")).toBeInTheDocument();
    expect(put).not.toHaveBeenCalled();
  });

  it("disables the save button while saving and sends only once", async () => {
    serve(() => seed());
    const put = serveSave(() => PENDING);

    renderPage();
    await loaded();
    fireEvent.click(saveButton());
    fireEvent.click(saveButton());

    await waitFor(() => expect(saveButton()).toBeDisabled());
    fireEvent.click(saveButton());
    expect(put).toHaveBeenCalledTimes(1);
  });

  it("refreshes the form from the saved settings", async () => {
    serve(() => seed());
    const saved = seed({
      min_amount: "20.00",
      max_amount: "8000.00",
      recovery_buffer: "5.00",
      preset_amounts: ["50.00", "100.00"],
      reuse_window_seconds: 120,
      pending_timeout_seconds: 7200,
      version: 2,
      updated_at: "2026-11-10T02:00:00",
      updated_by: "admin@example.com",
    });
    serveSave(() => saved);

    renderPage();
    await loaded();
    type(LABEL.min, "20");
    fireEvent.click(saveButton());

    expect(await screen.findByText("The payment settings were saved.")).toBeInTheDocument();
    // 后端把金额写回两位小数：表单显示的是返回值，不是输入的「20」。
    expect(input(LABEL.min).value).toBe("20.00");
    expect(input(LABEL.max).value).toBe("8000.00");
    expect(input(LABEL.buffer).value).toBe("5.00");
    expect(input(LABEL.presets).value).toBe("50.00, 100.00");
    expect(input(LABEL.reuse).value).toBe("120");
    expect(input(LABEL.timeout).value).toBe("7200");
    expect(meta()).toEqual({
      Version: "2",
      "Last changed by": "admin@example.com",
      "Last changed at (MYT)": "2026-11-10 10:00:00",
    });
    expect(saveButton()).toBeEnabled();
  });

  it("saves the next change with the version it got back", async () => {
    serve(() => seed());
    let version = 1;
    const put = serveSave(() => {
      version += 1;
      return seed({ version });
    });

    renderPage();
    await loaded();
    fireEvent.click(saveButton());
    await screen.findByText("The payment settings were saved.");
    await waitFor(() => expect(meta().Version).toBe("2"));
    fireEvent.click(saveButton());

    await waitFor(() => expect(put).toHaveBeenCalledTimes(2));
    expect(put.mock.calls[0]?.[1]).toMatchObject({ version: 1 });
    expect(put.mock.calls[1]?.[1]).toMatchObject({ version: 2 });
  });

  it("on a version conflict says someone else changed the settings and refreshes on request", async () => {
    const get = serve(
      () => seed(),
      () => seed({ min_amount: "30.00", version: 3, updated_by: "other@example.com" }),
    );
    serveSave(
      () =>
        new ApiError(
          "PAYMENT_SETTINGS_VERSION_CONFLICT",
          "Payment settings were changed by someone else.",
          "req-409",
        ),
    );

    renderPage();
    await loaded();
    type(LABEL.min, "25");
    fireEvent.click(saveButton());

    expect(
      await screen.findByText(
        "These settings were changed by someone else. Refresh, then make your changes again on the latest values.",
      ),
    ).toBeInTheDocument();
    // 没刷新之前保留正在改的值。
    expect(input(LABEL.min).value).toBe("25");

    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));

    await waitFor(() => expect(input(LABEL.min).value).toBe("30.00"));
    expect(get).toHaveBeenCalledTimes(2);
    expect(meta()).toMatchObject({ Version: "3", "Last changed by": "other@example.com" });
    expect(
      screen.queryByText(
        "These settings were changed by someone else. Refresh, then make your changes again on the latest values.",
      ),
    ).not.toBeInTheDocument();
  });

  it("shows the backend code and message for a rejected value", async () => {
    serve(() => seed());
    serveSave(
      () => new ApiError("PAYMENT_SETTINGS_INVALID", "Invalid payment settings: recovery_buffer.", "req-422"),
    );

    renderPage();
    await loaded();
    type(LABEL.buffer, "0");
    fireEvent.click(saveButton());

    expect(await screen.findByText("The payment settings could not be saved.")).toBeInTheDocument();
    expect(screen.getByText("PAYMENT_SETTINGS_INVALID")).toBeInTheDocument();
    expect(screen.getByText(/Invalid payment settings: recovery_buffer\./)).toBeInTheDocument();
    expect(screen.getByText("req-422")).toBeInTheDocument();
    // 输入保留，可以改了再存。
    expect(input(LABEL.buffer).value).toBe("0");
    expect(saveButton()).toBeEnabled();
  });

  it("shows a request validation error the same way", async () => {
    serve(() => seed());
    serveSave(() => new ApiError("VALIDATION_ERROR", "Invalid request: preset_amounts.", ZERO_UUID));

    renderPage();
    await loaded();
    type(LABEL.presets, " , ");
    fireEvent.click(saveButton());

    expect(await screen.findByText("VALIDATION_ERROR")).toBeInTheDocument();
    expect(screen.getByText(/Invalid request: preset_amounts\./)).toBeInTheDocument();
    expect(screen.getByText(ZERO_UUID)).toBeInTheDocument();
  });
});
