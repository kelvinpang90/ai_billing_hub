/**
 * 手工调账表单：发出去的请求体、确认步骤、幂等键与「结果未知」的锁，以及各种结果的展示。
 *
 * ⚠️ 这里最要紧的是**发出去的东西**：金额带没带对符号、是不是字符串、重试是不是同一个键
 * 同一份内容。这几处写错都不会报错 —— 界面照样说「已记账」，账上却是方向相反的一笔，
 * 或者重复的一笔。
 *
 * 「每次打开换新键」要经详情页的开关才测得到，放在 CustomerDetailPage.test.tsx。
 * 输入一律 click + paste：逐字 `user.type` 每按一键 antd 表单就重渲染一次，全量并行时会超时。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import type { CustomerDetail } from "../../api/adminCustomers";
import {
  AdjustmentError,
  type Adjustment,
  type AdjustmentType,
} from "../../api/walletAdjustments";
import { AdjustmentModal } from "./AdjustmentModal";

const CUSTOMER_ID = "00000000-0000-4000-8000-000000000000";
const KEY = "00000000-0000-4000-8000-000000000000";
const ROW_ID = "00000000-0000-4000-8000-000000000001";

const api = vi.hoisted(() => ({
  postAdjustment: vi.fn(),
}));

vi.mock("../../api/walletAdjustments", async () => {
  const actual = await vi.importActual<typeof import("../../api/walletAdjustments")>(
    "../../api/walletAdjustments",
  );
  return { ...actual, ...api };
});

const TYPE_LABEL: Record<AdjustmentType, string> = {
  ADJUSTMENT_CREDIT: "Adjustment credit (adds to the balance)",
  BONUS: "Bonus (adds to the balance)",
  ADJUSTMENT_DEBIT: "Adjustment debit (takes from the balance)",
  REFUND_ADJUSTMENT: "Refund adjustment (takes from the balance)",
};

const AMOUNT_INVALID =
  "Enter digits only: up to 12 before the decimal point and up to 8 after it, with no sign or exponent.";
const UNKNOWN_TITLE = "We could not confirm whether the adjustment was posted.";
const REPLAYED = "This adjustment had already been posted earlier. It was not posted again.";

function customer(): CustomerDetail {
  return {
    id: CUSTOMER_ID,
    company_name: "Acme Sdn Bhd",
    contact_name: null,
    email: "ops@example.com",
    phone: null,
    billing_status: "ACTIVE",
    status_version: 3,
    created_at: "2026-09-20T08:30:00",
    updated_at: "2026-09-21T01:02:03",
    wallet: { currency: "MYR", balance: "100.00000000", version: 3 },
  };
}

function adjustment(overrides: Partial<Adjustment> = {}): Adjustment {
  return {
    id: ROW_ID,
    customer_id: CUSTOMER_ID,
    transaction_type: "ADJUSTMENT_DEBIT",
    amount: "-20.50000000",
    balance_before: "100.00000000",
    balance_after: "79.50000000",
    wallet_sequence: 4,
    reason: "Refund for outage",
    idempotency_key: KEY,
    created_at: "2026-09-23T08:30:00",
    replayed: false,
    billing_status: "ACTIVE",
    status_version: 3,
    ...overrides,
  };
}

function renderModal() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidate = vi.spyOn(queryClient, "invalidateQueries");
  const onClose = vi.fn();
  render(
    <QueryClientProvider client={queryClient}>
      <AdjustmentModal customer={customer()} onClose={onClose} />
    </QueryClientProvider>,
  );
  return { invalidate, onClose };
}

type User = ReturnType<typeof userEvent.setup>;

/**
 * 按按钮上的文字找按钮。填表流程里不用 `getByRole`：它每次都要算整棵 antd 弹窗的可访问性树，
 * 每个用例要调好几次，冷启动的第一个用例因此超过 5 秒。
 */
function buttonWithText(text: string): HTMLElement {
  const button = screen.getByText(text).closest("button");
  if (button === null) {
    throw new Error(`"${text}" is not inside a button`);
  }
  return button;
}

async function fillAndReview(
  user: User,
  {
    type = "ADJUSTMENT_DEBIT",
    amount = "20.5",
    reason = "Refund for outage",
  }: { type?: AdjustmentType; amount?: string; reason?: string } = {},
) {
  await user.click(screen.getByLabelText(TYPE_LABEL[type]));
  await user.click(screen.getByLabelText("Amount"));
  await user.paste(amount);
  if (reason !== "") {
    await user.click(screen.getByLabelText("Reason"));
    await user.paste(reason);
  }
  await user.click(buttonWithText("Review"));
}

async function confirmPosting(user: User) {
  await screen.findByText("Confirm and post");
  await user.click(buttonWithText("Confirm and post"));
}

function expectLocked() {
  expect(screen.getByLabelText("Amount")).toBeDisabled();
  expect(screen.getByLabelText("Reason")).toBeDisabled();
  for (const radio of screen.getAllByRole("radio")) {
    expect(radio).toBeDisabled();
  }
  expect(screen.queryByRole("button", { name: "Back to edit" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Confirm and post" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Review" })).not.toBeInTheDocument();
}

async function waitForRetry() {
  // 发送中按钮带 loading 图标，名字不是这一句；等它回到可点的状态。
  await waitFor(() => {
    expect(screen.getByRole("button", { name: "Retry with the same key" })).toBeEnabled();
  });
  return screen.getByRole("button", { name: "Retry with the same key" });
}

function spyOnRandomUUID() {
  return vi.spyOn(crypto, "randomUUID").mockReturnValue(KEY);
}

let randomUUID: ReturnType<typeof spyOnRandomUUID>;

/** 贷方原样，借方加 `-`。 */
const SIGNED: [AdjustmentType, string][] = [
  ["ADJUSTMENT_CREDIT", "20.5"],
  ["BONUS", "20.5"],
  ["ADJUSTMENT_DEBIT", "-20.5"],
  ["REFUND_ADJUSTMENT", "-20.5"],
];

beforeEach(() => {
  vi.clearAllMocks();
  randomUUID = spyOnRandomUUID();
});

afterEach(() => {
  randomUUID.mockRestore();
});

// 每个用例都要走完一整遍填表 → 确认 → 发送；文件里第一个用例还要替 antd 生成全部组件的样式，
// 全量并行时 5 秒不够。放宽的只是时间，断言一条没动。
describe("AdjustmentModal", { timeout: 15_000 }, () => {
  it.each(SIGNED)("sends %s with the amount as the string %j", async (type, expected) => {
    const user = userEvent.setup();
    api.postAdjustment.mockResolvedValue(adjustment());
    renderModal();

    await fillAndReview(user, { type, amount: "20.5" });
    await confirmPosting(user);

    await waitFor(() => {
      expect(api.postAdjustment).toHaveBeenCalledTimes(1);
    });
    expect(api.postAdjustment).toHaveBeenCalledWith(CUSTOMER_ID, {
      transaction_type: type,
      amount: expected,
      reason: "Refund for outage",
      idempotency_key: KEY,
    });
    const body = api.postAdjustment.mock.calls[0]?.[1] as { amount: unknown };
    expect(typeof body.amount).toBe("string");
  });

  it("keeps every digit of the amount: no rounding, no float", async () => {
    const user = userEvent.setup();
    api.postAdjustment.mockResolvedValue(adjustment());
    renderModal();

    await fillAndReview(user, { type: "BONUS", amount: "123456789012.10000001" });
    await confirmPosting(user);

    await waitFor(() => {
      expect(api.postAdjustment).toHaveBeenCalledWith(
        CUSTOMER_ID,
        expect.objectContaining({ amount: "123456789012.10000001" }),
      );
    });
  });

  it("shows the customer, type, signed amount and reason before sending anything", async () => {
    const user = userEvent.setup();
    renderModal();

    await fillAndReview(user, { reason: "  Refund for outage  " });

    expect(await screen.findByText("Check before posting")).toBeInTheDocument();
    expect(screen.getByText("Acme Sdn Bhd")).toBeInTheDocument();
    expect(screen.getByText(CUSTOMER_ID)).toBeInTheDocument();
    expect(screen.getAllByText(TYPE_LABEL.ADJUSTMENT_DEBIT).length).toBeGreaterThan(1);
    expect(screen.getByText("MYR -20.50")).toBeInTheDocument();
    // 只读的原因输入框里也有这段文字；要找的是确认摘要里去了空白的那一份。
    expect(
      screen.getByText("Refund for outage", { ignore: "script, style, textarea" }),
    ).toBeInTheDocument();
    expect(api.postAdjustment).not.toHaveBeenCalled();
  });

  it("sends the reason without its surrounding whitespace", async () => {
    const user = userEvent.setup();
    api.postAdjustment.mockResolvedValue(adjustment());
    renderModal();

    await fillAndReview(user, { reason: "  Refund for outage  " });
    await confirmPosting(user);

    await waitFor(() => {
      expect(api.postAdjustment).toHaveBeenCalledWith(
        CUSTOMER_ID,
        expect.objectContaining({ reason: "Refund for outage" }),
      );
    });
  });

  it.each([
    ["0", "The amount cannot be zero."],
    ["1234567890123", AMOUNT_INVALID],
    ["1.123456789", AMOUNT_INVALID],
    ["1e2", AMOUNT_INVALID],
    ["+1", AMOUNT_INVALID],
  ])("stops %j before it is sent", async (amount, message) => {
    const user = userEvent.setup();
    renderModal();

    await fillAndReview(user, { amount });

    expect(await screen.findByText(message)).toBeInTheDocument();
    expect(screen.queryByText("Check before posting")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Confirm and post" })).not.toBeInTheDocument();
    expect(api.postAdjustment).not.toHaveBeenCalled();
  });

  it("requires a reason and says not to put personal data in it", async () => {
    const user = userEvent.setup();
    renderModal();

    expect(
      screen.getByText(
        "Write only the business reason. Do not include personal data such as names, phone numbers or email addresses. The reason is kept permanently and can never be changed.",
      ),
    ).toBeInTheDocument();

    await fillAndReview(user, { reason: "" });

    expect(await screen.findByText("Enter the reason.")).toBeInTheDocument();
    expect(api.postAdjustment).not.toHaveBeenCalled();
  });

  it("requires a type", async () => {
    const user = userEvent.setup();
    renderModal();

    await user.click(screen.getByLabelText("Amount"));
    await user.paste("5");
    await user.click(screen.getByLabelText("Reason"));
    await user.paste("Goodwill credit");
    await user.click(screen.getByRole("button", { name: "Review" }));

    expect(await screen.findByText("Choose the type of adjustment.")).toBeInTheDocument();
    expect(api.postAdjustment).not.toHaveBeenCalled();
  });

  it("sends once when Confirm is double-clicked", async () => {
    const user = userEvent.setup();
    api.postAdjustment.mockReturnValue(new Promise(() => undefined));
    renderModal();

    await fillAndReview(user);
    await screen.findByText("Confirm and post");
    await user.dblClick(buttonWithText("Confirm and post"));

    expect(api.postAdjustment).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["a network error or timeout", new AdjustmentError("NETWORK_ERROR", "Could not reach the billing platform.", null, null)],
    ["a 500", new AdjustmentError("INTERNAL_ERROR", "Internal error.", "req-500", 500)],
    ["a 503", new AdjustmentError("DATABASE_NOT_CONFIGURED", "Not configured.", "req-503", 503)],
  ])("locks the type, amount and reason after %s", async (_name, error) => {
    const user = userEvent.setup();
    api.postAdjustment.mockRejectedValue(error);
    renderModal();

    await fillAndReview(user);
    await confirmPosting(user);

    expect(await screen.findByText(UNKNOWN_TITLE)).toBeInTheDocument();
    expectLocked();
    expect(screen.getByRole("button", { name: "Retry with the same key" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Close this form" })).toBeEnabled();
  });

  it("retries with the same key and the same body, and never makes a new key", async () => {
    const user = userEvent.setup();
    api.postAdjustment
      .mockRejectedValueOnce(
        new AdjustmentError("INTERNAL_ERROR", "Internal error.", "req-500", 500),
      )
      .mockRejectedValueOnce(
        new AdjustmentError("NETWORK_ERROR", "Could not reach the billing platform.", null, null),
      )
      .mockResolvedValueOnce(adjustment({ replayed: true }));
    renderModal();

    await fillAndReview(user);
    await confirmPosting(user);
    await user.click(await waitForRetry());
    // 第二次也没结果：还是锁着。
    await waitFor(() => {
      expect(api.postAdjustment).toHaveBeenCalledTimes(2);
    });
    const retry = await waitForRetry();
    expect(screen.getByText(UNKNOWN_TITLE)).toBeInTheDocument();
    expectLocked();
    await user.click(retry);

    expect(await screen.findByText(REPLAYED)).toBeInTheDocument();
    expect(api.postAdjustment).toHaveBeenCalledTimes(3);
    const [first, second, third] = api.postAdjustment.mock.calls;
    expect(second).toEqual(first);
    expect(third).toEqual(first);
    expect(first?.[1] as unknown).toMatchObject({ amount: "-20.5", idempotency_key: KEY });
    expect(randomUUID).toHaveBeenCalledTimes(1);
  });

  it("shows the request id of a failure whose outcome is unknown", async () => {
    const user = userEvent.setup();
    api.postAdjustment.mockRejectedValue(
      new AdjustmentError("INTERNAL_ERROR", "An unexpected error occurred.", "req-500", 500),
    );
    renderModal();

    await fillAndReview(user);
    await confirmPosting(user);

    expect(await screen.findByText(/An unexpected error occurred\./)).toBeInTheDocument();
    expect(screen.getByText("req-500")).toBeInTheDocument();
  });

  it("shows the posted adjustment, the balance after it and the billing status", async () => {
    const user = userEvent.setup();
    api.postAdjustment.mockResolvedValue(adjustment());
    const { invalidate, onClose } = renderModal();

    await fillAndReview(user);
    await confirmPosting(user);

    expect(await screen.findByText("Adjustment posted.")).toBeInTheDocument();
    expect(screen.queryByText(REPLAYED)).not.toBeInTheDocument();
    expect(screen.getByText("MYR -20.50")).toBeInTheDocument();
    expect(screen.getByText("MYR 79.50")).toBeInTheDocument();
    expect(screen.getByText("Active")).toBeInTheDocument();
    // 余额与计费状态以后端为准：让详情（只是详情本身）与列表过期重读。
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: ["admin", "customers", "detail", CUSTOMER_ID],
      exact: true,
    });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["admin", "customers", "list"] });

    await user.click(screen.getByRole("button", { name: "Done" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("says plainly that a replay was not posted a second time", async () => {
    const user = userEvent.setup();
    api.postAdjustment.mockResolvedValue(
      adjustment({ replayed: true, balance_after: "79.50000000", billing_status: "SUSPENDED" }),
    );
    const { invalidate } = renderModal();

    await fillAndReview(user);
    await confirmPosting(user);

    expect(await screen.findByText(REPLAYED)).toBeInTheDocument();
    expect(screen.queryByText("Adjustment posted.")).not.toBeInTheDocument();
    expect(screen.getByText("MYR 79.50")).toBeInTheDocument();
    expect(screen.getByText("Suspended")).toBeInTheDocument();
    // 重放也刷新：中间可能有别的记账。
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: ["admin", "customers", "detail", CUSTOMER_ID],
      exact: true,
    });
  });

  it("shows a 409 with the backend message and request id and only lets the form be closed", async () => {
    const user = userEvent.setup();
    api.postAdjustment.mockRejectedValue(
      new AdjustmentError(
        "ADJUSTMENT_CONFLICT",
        "The idempotency key was already used for a different adjustment.",
        "req-409",
        409,
      ),
    );
    const { onClose } = renderModal();

    await fillAndReview(user);
    await confirmPosting(user);

    expect(
      await screen.findByText(
        "This adjustment conflicts with one already posted under the same key.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/The idempotency key was already used for a different adjustment\./),
    ).toBeInTheDocument();
    expect(screen.getByText("req-409")).toBeInTheDocument();
    expectLocked();
    expect(
      screen.queryByRole("button", { name: "Retry with the same key" }),
    ).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Close this form" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it.each([
    [
      "422 BALANCE_OUT_OF_RANGE",
      new AdjustmentError(
        "BALANCE_OUT_OF_RANGE",
        "The balance would be out of range.",
        "req-422",
        422,
      ),
      /The balance would be out of range\./,
      "req-422",
    ],
    [
      "422 VALIDATION_ERROR",
      new AdjustmentError("VALIDATION_ERROR", "Invalid field: body.amount", "req-422v", 422),
      /Invalid field: body\.amount/,
      "req-422v",
    ],
    [
      "404 CUSTOMER_NOT_FOUND",
      new AdjustmentError("CUSTOMER_NOT_FOUND", "The customer does not exist.", "req-404", 404),
      /The customer does not exist\./,
      "req-404",
    ],
  ])("shows a %s with its message and request id and does not lock", async (_name, error, message, requestId) => {
    const user = userEvent.setup();
    api.postAdjustment.mockRejectedValue(error);
    renderModal();

    await fillAndReview(user);
    await confirmPosting(user);

    expect(await screen.findByText("The adjustment was not posted.")).toBeInTheDocument();
    expect(screen.getByText(message)).toBeInTheDocument();
    expect(screen.getByText(requestId)).toBeInTheDocument();
    expect(screen.queryByText(UNKNOWN_TITLE)).not.toBeInTheDocument();

    // 后端明确拒绝、什么都没写：可以回去改。
    await user.click(screen.getByRole("button", { name: "Back to edit" }));
    expect(screen.getByLabelText("Amount")).toBeEnabled();
    expect(screen.getByLabelText("Reason")).toBeEnabled();
  });

  it("does not let the form be closed while the request is on its way", async () => {
    const user = userEvent.setup();
    api.postAdjustment.mockReturnValue(new Promise(() => undefined));
    const { onClose } = renderModal();

    await fillAndReview(user);
    await confirmPosting(user);
    await user.keyboard("{Escape}");

    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Back to edit" })).toBeDisabled();
  });
});
