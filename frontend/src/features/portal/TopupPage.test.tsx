/**
 * 门户充值页（AIH-TASK-192，设计闸门 #587 v3 §2「门户页面」、§7「充值页」）。
 *
 * 与其他门户页的测试同一写法：不 mock `api/customerPayments`，而是在 `client.get` / `client.post` 上按路径回应，钉住
 * **页面真正发出去的请求** —— 读选项只问 `/api/v1/customer/topup-options`，建单只在点「Go to payment」时 POST
 * `/api/v1/customer/topups`、请求体只有去空白后的 `amount` 字符串，任何时候都不请求 `/api/v1/admin/`。
 *
 * 要钉住的几件（§7「充值页」一行）：
 * - 五种不可充值原因各有说明，且不显示表单。
 * - 推荐金额按钮：低于 `effective_minimum` 或高于 `max_amount` 的不可点；点了只把金额填进输入框，不提交。
 * - 自定义金额：去空白后按正则即时提示；低于 `effective_minimum`（即使不低于 `min_amount`）时去付款按钮不可点。
 * - 有 `recovery_minimum` 时的挂起说明；请求中不可重复点；成功跳到 `redirect_url`；失败显示错误码、信息与 request_id；
 *   `TOPUP_BELOW_RECOVERY_MINIMUM` 后重新读一次选项。
 * - 金额全部来自响应：各用例用不同的金额与币种，页面上不出现写死的数。
 *
 * jsdom 的 `window.location.assign` 不能替换：跳转经页面导出的 `topupRedirect` 发出，这里替换它、只记录地址。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";

import "../../i18n";
import { ApiError, client } from "../../api/client";
import type { TopupOptions } from "../../api/customerPayments";
import { ROUTES } from "../../routes/paths";
import { TopupPage, topupRedirect } from "./TopupPage";

const TOPUP_OPTIONS_URL = "/api/v1/customer/topup-options";
const TOPUPS_URL = "/api/v1/customer/topups";
const PAYMENT_ID = "00000000-0000-0000-0000-000000000000";
const REDIRECT_URL = "https://checkout.example/c/pay/cs_test_0?x=1#frag";

const SUBMIT = "Go to payment";
const AMOUNT_LABEL = "Amount (MYR)";
const FORMAT_HINT = "Enter an amount with up to 6 digits and up to 2 decimal places, for example 100 or 100.50.";
const BELOW_HINT = "The amount must be at least";
const ABOVE_HINT = "The amount must be at most";
const RECOVERY_BODY = "To restore service, top up at least";

/** 让请求一直挂着（加载中）。 */
const PENDING = Symbol("pending");

function options(overrides: Partial<TopupOptions> = {}): TopupOptions {
  return {
    currency: "MYR",
    available: true,
    unavailable_reason: null,
    min_amount: "10.00",
    max_amount: null,
    preset_amounts: ["50.00", "100.00", "300.00"],
    recovery_minimum: null,
    effective_minimum: "10.00",
    ...overrides,
  };
}

function envelope(data: unknown) {
  return { data: { success: true, data, error: null, request_id: "req-1" } };
}

function reply(value: unknown) {
  if (value === PENDING) {
    return new Promise(() => undefined);
  }
  return value instanceof Error ? Promise.reject(value) : Promise.resolve(envelope(value));
}

/**
 * 按路径回应 `client.get`：充值选项依次回 `replies` 的各项（用完后一直回最后一项）。别的路径一律当成失败 —— 页面不该问
 * 它们，下面也会断言没有问。
 */
function serve(...replies: unknown[]) {
  let reads = 0;
  return vi.spyOn(client, "get").mockImplementation((url: string) => {
    if (url !== TOPUP_OPTIONS_URL) {
      return reply(new ApiError("NOT_FOUND", "Unexpected request.", null));
    }
    const value = replies[Math.min(reads, replies.length - 1)];
    reads += 1;
    return reply(value);
  });
}

/** `client.post` 依次回 `replies` 的各项（用完后一直回最后一项）。 */
function servePost(...replies: unknown[]) {
  let posts = 0;
  return vi.spyOn(client, "post").mockImplementation(() => {
    const value = replies[Math.min(posts, replies.length - 1)];
    posts += 1;
    return reply(value);
  });
}

function optionReads(get: ReturnType<typeof serve>): number {
  return get.mock.calls.filter(([url]) => url === TOPUP_OPTIONS_URL).length;
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[ROUTES.portalTopup]}>
        <TopupPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** 文字恰好为 text 的按钮（加载中的图标没有文字，不影响）；没有时 null。 */
function buttonWithText(text: string): HTMLButtonElement | null {
  return (
    Array.from(document.querySelectorAll<HTMLButtonElement>("button")).find((button) => (button.textContent ?? "").trim() === text) ?? null
  );
}

function submitButton(): HTMLButtonElement {
  const button = buttonWithText(SUBMIT);
  if (button === null) {
    throw new Error("no submit button");
  }
  return button;
}

/** 推荐金额一组里的按钮，按显示顺序。 */
function presetButtons(): HTMLButtonElement[] {
  const group = document.querySelector('[role="group"][aria-label="Suggested amounts"]');
  return group === null ? [] : Array.from(group.querySelectorAll<HTMLButtonElement>("button"));
}

function amountInput(): HTMLInputElement {
  return screen.getByLabelText<HTMLInputElement>(AMOUNT_LABEL);
}

async function formShown(): Promise<void> {
  await screen.findByLabelText(AMOUNT_LABEL);
}

function typeAmount(value: string): void {
  fireEvent.change(amountInput(), { target: { value } });
}

let redirect: MockInstance<(url: string) => void>;

beforeEach(() => {
  redirect = vi.spyOn(topupRedirect, "assign").mockImplementation(() => undefined);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("TopupPage loading and failure", () => {
  it("shows the loading state while the options are read", async () => {
    serve(PENDING);

    renderPage();

    expect(await screen.findByText("Loading top-up options…")).toBeInTheDocument();
    expect(buttonWithText(SUBMIT)).toBeNull();
  });

  it("shows the failure with its request_id and no form", async () => {
    serve(new ApiError("INTERNAL_ERROR", "An unexpected error occurred.", "req-500"));

    renderPage();

    expect(await screen.findByText("Top-up options could not be loaded.")).toBeInTheDocument();
    expect(screen.getByText("req-500")).toBeInTheDocument();
    expect(buttonWithText(SUBMIT)).toBeNull();
    expect(screen.queryByLabelText(AMOUNT_LABEL)).toBeNull();
  });
});

describe("TopupPage when top-ups are not available", () => {
  const cases: [string, string][] = [
    ["PAYMENTS_DISABLED", "Online top-ups are not available at the moment. Please try again later."],
    [
      "ACCOUNT_READ_ONLY",
      "Your account is read-only, so it cannot be topped up. If you have questions, please contact support.",
    ],
    ["TOPUPS_NOT_OPEN", "Online top-ups are not open for your account yet."],
    ["TOPUP_NOT_ALLOWED", "Your account is not on prepaid billing, so it cannot be topped up online."],
    [
      "RECOVERY_EXCEEDS_MAXIMUM",
      "The amount needed to restore your service is more than the largest single top-up allowed. Please contact support.",
    ],
  ];

  it.each(cases)("explains %s and shows no form", async (reason, text) => {
    const get = serve(
      options({
        available: false,
        unavailable_reason: reason,
        recovery_minimum: "2000.00",
        effective_minimum: "2000.00",
        max_amount: "1000.00",
      }),
    );
    const post = servePost(new ApiError("NOT_FOUND", "Unexpected request.", null));

    renderPage();

    expect(await screen.findByText(text)).toBeInTheDocument();
    expect(screen.getByText("Top-ups are not available")).toBeInTheDocument();
    expect(buttonWithText(SUBMIT)).toBeNull();
    expect(screen.queryByLabelText(AMOUNT_LABEL)).toBeNull();
    expect(presetButtons()).toEqual([]);
    // 不可充值时连挂起说明也不显示：只有原因。
    expect(screen.queryByText(RECOVERY_BODY)).toBeNull();
    expect(post).not.toHaveBeenCalled();
    expect(optionReads(get)).toBe(1);
  });

  it("each reason has its own explanation", () => {
    expect(new Set(cases.map(([, text]) => text)).size).toBe(cases.length);
  });
});

describe("TopupPage suggested amounts", () => {
  it("shows one button per preset, in order, with the response's currency", async () => {
    serve(options({ currency: "MYR", preset_amounts: ["20.00", "75.50", "1234.00"] }));

    renderPage();
    await formShown();

    expect(presetButtons().map((button) => button.textContent)).toEqual(["MYR 20.00", "MYR 75.50", "MYR 1,234.00"]);
  });

  it("disables presets below effective_minimum or above max_amount", async () => {
    serve(
      options({
        min_amount: "10.00",
        recovery_minimum: "60.00",
        effective_minimum: "60.00",
        max_amount: "500.00",
        preset_amounts: ["50.00", "60.00", "100.00", "500.00", "1000.00"],
      }),
    );

    renderPage();
    await formShown();

    // 50 不低于 min_amount 但低于 effective_minimum：不可点；60 与 500 恰在边界上：可点；1000 高于上限：不可点。
    expect(presetButtons().map((button) => button.disabled)).toEqual([true, false, false, false, true]);
  });

  it("fills the amount box when a preset is clicked, without submitting", async () => {
    serve(options({ preset_amounts: ["50.00", "100.00"] }));
    const post = servePost(new ApiError("NOT_FOUND", "Unexpected request.", null));

    renderPage();
    await formShown();
    const [, hundred] = presetButtons();
    fireEvent.click(hundred as HTMLButtonElement);

    expect(amountInput().value).toBe("100.00");
    expect(submitButton()).toBeEnabled();
    expect(post).not.toHaveBeenCalled();
    expect(redirect).not.toHaveBeenCalled();
  });

  it("does nothing when a disabled preset is clicked", async () => {
    serve(options({ effective_minimum: "80.00", recovery_minimum: "80.00", preset_amounts: ["50.00", "100.00"] }));

    renderPage();
    await formShown();
    const [fifty] = presetButtons();
    fireEvent.click(fifty as HTMLButtonElement);

    expect(amountInput().value).toBe("");
    expect(submitButton()).toBeDisabled();
  });
});

describe("TopupPage custom amount", () => {
  it("keeps Go to payment disabled while the box is empty, without a hint", async () => {
    serve(options());

    renderPage();
    await formShown();

    expect(submitButton()).toBeDisabled();
    expect(screen.queryByText(FORMAT_HINT)).toBeNull();
    expect(screen.queryByText(BELOW_HINT)).toBeNull();
  });

  it.each(["abc", "12.345", "1234567", ".5", "1e3", "-10", "10.", "1,000"])(
    "hints at the format and disables Go to payment for %j",
    async (value) => {
      serve(options());

      renderPage();
      await formShown();
      typeAmount(value);

      expect(screen.getByText(FORMAT_HINT)).toBeInTheDocument();
      expect(submitButton()).toBeDisabled();
    },
  );

  it("disables Go to payment below effective_minimum even when the amount is above min_amount", async () => {
    serve(options({ min_amount: "10.00", recovery_minimum: "22.35", effective_minimum: "22.35" }));
    const post = servePost(new ApiError("NOT_FOUND", "Unexpected request.", null));

    renderPage();
    await formShown();
    typeAmount("22.34");

    expect(screen.getByText(BELOW_HINT).textContent).toBe(`${BELOW_HINT} MYR 22.35`);
    expect(screen.queryByText(FORMAT_HINT)).toBeNull();
    expect(submitButton()).toBeDisabled();
    fireEvent.click(submitButton());
    expect(post).not.toHaveBeenCalled();

    typeAmount("22.35");
    expect(screen.queryByText(BELOW_HINT)).toBeNull();
    expect(submitButton()).toBeEnabled();
  });

  it("disables Go to payment above max_amount", async () => {
    serve(options({ max_amount: "500.00" }));

    renderPage();
    await formShown();
    typeAmount("500.01");

    expect(screen.getByText(ABOVE_HINT).textContent).toBe(`${ABOVE_HINT} MYR 500.00`);
    expect(submitButton()).toBeDisabled();

    typeAmount("500");
    expect(screen.queryByText(ABOVE_HINT)).toBeNull();
    expect(submitButton()).toBeEnabled();
  });

  it("sets no upper limit when max_amount is null", async () => {
    serve(options({ max_amount: null }));

    renderPage();
    await formShown();
    typeAmount("999999.99");

    expect(screen.queryByText(ABOVE_HINT)).toBeNull();
    expect(screen.queryByText("Maximum", { exact: false })).toBeNull();
    expect(submitButton()).toBeEnabled();
  });

  it("checks and sends the amount with surrounding whitespace removed", async () => {
    serve(options());
    const post = servePost({ payment_id: PAYMENT_ID, redirect_url: REDIRECT_URL, expires_at: "2026-11-10T09:00:00" });

    renderPage();
    await formShown();
    typeAmount("  100.5 ");

    expect(screen.queryByText(FORMAT_HINT)).toBeNull();
    expect(submitButton()).toBeEnabled();
    fireEvent.click(submitButton());

    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    expect(post.mock.calls[0]).toEqual([TOPUPS_URL, { amount: "100.5" }]);
  });

  it("shows the limits from the response", async () => {
    serve(options({ effective_minimum: "15.00", min_amount: "15.00", max_amount: "2500.00" }));

    renderPage();
    await formShown();

    expect(screen.getByText("Minimum", { exact: false }).textContent).toBe("Minimum: MYR 15.00 · Maximum: MYR 2,500.00");
  });
});

describe("TopupPage suspended account", () => {
  it("explains above the form that at least effective_minimum restores service", async () => {
    serve(options({ min_amount: "10.00", recovery_minimum: "60.00", effective_minimum: "60.00" }));

    renderPage();
    await formShown();

    expect(screen.getByText("Your account is suspended")).toBeInTheDocument();
    const body = screen.getByText(RECOVERY_BODY);
    expect(body.textContent).toBe(`${RECOVERY_BODY} MYR 60.00`);
    // 在表单上方。
    expect(body.compareDocumentPosition(amountInput()) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("uses effective_minimum, not recovery_minimum, when min_amount is larger", async () => {
    serve(options({ min_amount: "30.00", recovery_minimum: "12.00", effective_minimum: "30.00" }));

    renderPage();
    await formShown();

    expect(screen.getByText(RECOVERY_BODY).textContent).toBe(`${RECOVERY_BODY} MYR 30.00`);
  });

  it("shows no suspension note when recovery_minimum is null", async () => {
    serve(options({ recovery_minimum: null }));

    renderPage();
    await formShown();

    expect(screen.queryByText("Your account is suspended")).toBeNull();
    expect(screen.queryByText(RECOVERY_BODY)).toBeNull();
  });
});

describe("TopupPage submitting", () => {
  it("cannot be clicked again while the request is in flight", async () => {
    serve(options());
    const post = servePost(PENDING);

    renderPage();
    await formShown();
    typeAmount("100");
    fireEvent.click(submitButton());

    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(submitButton()).toBeDisabled());
    fireEvent.click(submitButton());
    // 回车提交表单同样挡住。
    const form = amountInput().closest("form");
    if (form !== null) {
      fireEvent.submit(form);
    }

    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(post).toHaveBeenCalledTimes(1);
    expect(redirect).not.toHaveBeenCalled();
  });

  it("goes to redirect_url exactly as returned on success", async () => {
    serve(options());
    const post = servePost({ payment_id: PAYMENT_ID, redirect_url: REDIRECT_URL, expires_at: "2026-11-10T09:00:00" });

    renderPage();
    await formShown();
    typeAmount("50");
    fireEvent.click(submitButton());

    await waitFor(() => expect(redirect).toHaveBeenCalledTimes(1));
    expect(redirect).toHaveBeenCalledWith(REDIRECT_URL);
    expect(post.mock.calls).toEqual([[TOPUPS_URL, { amount: "50" }]]);
    // 页面正在跳走：不再可点。
    expect(submitButton()).toBeDisabled();
  });

  it("shows the error code, message and request_id on failure", async () => {
    const get = serve(options());
    servePost(new ApiError("TOPUP_AMOUNT_INVALID", "The amount is not valid.", "req-422"));

    renderPage();
    await formShown();
    typeAmount("100");
    fireEvent.click(submitButton());

    expect(await screen.findByText("The payment could not be started.")).toBeInTheDocument();
    expect(screen.getByText("TOPUP_AMOUNT_INVALID")).toBeInTheDocument();
    expect(screen.getByText(/The amount is not valid\./)).toBeInTheDocument();
    expect(screen.getByText("req-422")).toBeInTheDocument();
    expect(redirect).not.toHaveBeenCalled();
    // 别的失败不重读选项；按钮恢复可点，客户可以再试。
    expect(optionReads(get)).toBe(1);
    expect(submitButton()).toBeEnabled();
  });

  it("reads the options again after TOPUP_BELOW_RECOVERY_MINIMUM and shows the new amounts", async () => {
    const get = serve(
      options({ recovery_minimum: null, effective_minimum: "10.00" }),
      options({ recovery_minimum: "22.35", effective_minimum: "22.35" }),
    );
    servePost(new ApiError("TOPUP_BELOW_RECOVERY_MINIMUM", "The amount is below the minimum.", "req-recovery"));

    renderPage();
    await formShown();
    expect(screen.queryByText(RECOVERY_BODY)).toBeNull();
    typeAmount("15");
    fireEvent.click(submitButton());

    expect(await screen.findByText("TOPUP_BELOW_RECOVERY_MINIMUM")).toBeInTheDocument();
    expect(screen.getByText("req-recovery")).toBeInTheDocument();
    await waitFor(() => expect(optionReads(get)).toBe(2));
    expect((await screen.findByText(RECOVERY_BODY)).textContent).toBe(`${RECOVERY_BODY} MYR 22.35`);
    // 刷新后的下限生效：15 低于新的 effective_minimum，去付款不可点。
    expect(amountInput().value).toBe("15");
    expect(submitButton()).toBeDisabled();
    expect(redirect).not.toHaveBeenCalled();
  });

  it("never requests an admin path", async () => {
    const get = serve(options());
    const post = servePost({ payment_id: PAYMENT_ID, redirect_url: REDIRECT_URL, expires_at: "2026-11-10T09:00:00" });

    renderPage();
    await formShown();
    typeAmount("300");
    fireEvent.click(submitButton());
    await waitFor(() => expect(redirect).toHaveBeenCalledTimes(1));

    for (const [url] of [...get.mock.calls, ...post.mock.calls]) {
      expect(String(url)).not.toMatch(/\/api\/v1\/admin\//);
    }
  });
});
