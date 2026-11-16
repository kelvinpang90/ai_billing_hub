/**
 * 关户弹窗（设计闸门 #707 §7「前端」一行）：打开即读关户检查、有 blockers 时确认不可点、Refresh 重读；
 * 原因 0 / 1 / 255 / 256 码点；客户名不符、大小写不同、首尾空白；成功后详情与列表过期；404 / 409 / 422 显示码与
 * request_id 并重读检查；5xx 与网络错误显示「Try again」并原样重发。
 *
 * 输入一律 click + paste：逐字 `user.type` 每按一键 antd 就重渲染一次，全量并行时会超时。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import {
  CloseAccountError,
  CUSTOMER_LISTS_QUERY_KEY,
  customerDetailQueryKey,
  type ClosureCheck,
  type CustomerDetail,
} from "../../api/adminCustomers";
import { CloseAccountModal } from "./CloseAccountModal";

const CUSTOMER_ID = "00000000-0000-0000-0000-000000000000";
const COMPANY = "Acme Sdn Bhd";

const api = vi.hoisted(() => ({
  getClosureCheck: vi.fn(),
  postCloseAccount: vi.fn(),
}));

vi.mock("../../api/adminCustomers", async () => {
  const actual =
    await vi.importActual<typeof import("../../api/adminCustomers")>("../../api/adminCustomers");
  return { ...actual, ...api };
});

const CONFIRM = "Close this account";
const LOADING = "Checking whether the account can be closed…";
const LOAD_FAILED = "The closure check could not be loaded.";
const NO_BLOCKERS = "Nothing blocks the closure right now. The backend checks again when you confirm.";
const BLOCKERS_TITLE = "The account cannot be closed yet.";
const REASON_TOO_LONG = "The reason can be at most 255 characters.";
const REJECTED = "The account was not closed.";
const UNKNOWN_TITLE = "We could not confirm whether the account was closed.";
const NAME_LABEL = "Type the company name to confirm";

function customer(overrides: Partial<CustomerDetail> = {}): CustomerDetail {
  return {
    id: CUSTOMER_ID,
    company_name: COMPANY,
    contact_name: null,
    email: "ops@example.com",
    phone: null,
    billing_status: "SUSPENDED",
    account_status: "DISABLED",
    billing_mode: "PREPAID",
    ai_service_enabled: true,
    status_version: 4,
    created_at: "2026-09-20T08:30:00",
    updated_at: "2026-09-21T01:02:03",
    wallet: { currency: "MYR", balance: "0.00000000", version: 9 },
    ...overrides,
  };
}

function closureCheck(overrides: Partial<ClosureCheck> = {}): ClosureCheck {
  return {
    account_status: "DISABLED",
    balance: "0.00000000",
    unresolved_usage_events: 0,
    unresolved_ai_usage: 0,
    unresolved_payments: 0,
    unresolved_payment_events: 0,
    payment_events_truncated: false,
    blockers: [],
    ...overrides,
  };
}

function renderModal() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidate = vi.spyOn(queryClient, "invalidateQueries");
  const onClose = vi.fn();
  render(
    <QueryClientProvider client={queryClient}>
      <CloseAccountModal customer={customer()} onClose={onClose} />
    </QueryClientProvider>,
  );
  return { invalidate, onClose };
}

type User = ReturnType<typeof userEvent.setup>;

/** 按按钮上的文字找按钮（`getByRole` 每次都要算整棵 antd 弹窗的可访问性树，慢）。 */
function buttonWithText(text: string): HTMLElement {
  const button = screen.getByText(text).closest("button");
  if (button === null) {
    throw new Error(`"${text}" is not inside a button`);
  }
  return button;
}

async function typeInto(user: User, label: string, value: string) {
  await user.click(screen.getByLabelText(label));
  if (value !== "") {
    await user.paste(value);
  }
}

/** 填原因与客户名（默认都合格）。 */
async function fill(user: User, { reason = "Closure ticket CL-0001", name = COMPANY } = {}) {
  await typeInto(user, "Reason", reason);
  await typeInto(user, NAME_LABEL, name);
}

/** 等检查读完且没有 blockers。 */
async function checkPassed() {
  expect(await screen.findByText(NO_BLOCKERS)).toBeInTheDocument();
}

function rejection(status: number, code: string, requestId: string) {
  return new CloseAccountError(code, `Rejected with ${code}.`, requestId, status);
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("CloseAccountModal", () => {
  it("reads the closure check on open, showing a loading state with confirm disabled until it arrives", async () => {
    const user = userEvent.setup();
    let resolve: (value: ClosureCheck) => void = () => undefined;
    api.getClosureCheck.mockReturnValue(
      new Promise<ClosureCheck>((settle) => {
        resolve = settle;
      }),
    );
    renderModal();

    expect(await screen.findByText(LOADING)).toBeInTheDocument();
    expect(api.getClosureCheck).toHaveBeenCalledTimes(1);
    expect(api.getClosureCheck).toHaveBeenCalledWith(CUSTOMER_ID, expect.anything());
    // 输入都合格，但检查还没回来：确认不可点。
    await fill(user);
    expect(buttonWithText(CONFIRM)).toBeDisabled();

    resolve(closureCheck());

    await checkPassed();
    expect(screen.queryByText(LOADING)).not.toBeInTheDocument();
    expect(screen.getByText("MYR 0.00")).toBeInTheDocument();
    await waitFor(() => {
      expect(buttonWithText(CONFIRM)).toBeEnabled();
    });
    expect(api.postCloseAccount).not.toHaveBeenCalled();
  });

  it("shows the balance and the four counts of the check", async () => {
    api.getClosureCheck.mockResolvedValue(
      closureCheck({
        balance: "12.50000000",
        unresolved_usage_events: 3,
        unresolved_ai_usage: 5,
        unresolved_payments: 7,
        unresolved_payment_events: 11,
        payment_events_truncated: true,
        blockers: [
          "ACCOUNT_CLOSURE_BALANCE_NOT_ZERO",
          "ACCOUNT_CLOSURE_USAGE_PENDING",
          "ACCOUNT_CLOSURE_AI_USAGE_PENDING",
          "ACCOUNT_CLOSURE_PAYMENTS_PENDING",
          "ACCOUNT_CLOSURE_PAYMENT_EVENTS_PENDING",
        ],
      }),
    );
    renderModal();

    expect(await screen.findByText("MYR 12.50")).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.getByText("5")).toBeInTheDocument();
    expect(screen.getByText("7")).toBeInTheDocument();
    expect(screen.getByText("11 (scan limit reached; there may be more)")).toBeInTheDocument();
  });

  it("explains every blocker, keeps confirm disabled, and reads the check again on Refresh", async () => {
    const user = userEvent.setup();
    api.getClosureCheck
      .mockResolvedValueOnce(
        closureCheck({
          account_status: "ENABLED",
          unresolved_payments: 1,
          blockers: ["ACCOUNT_STATUS_TRANSITION_INVALID", "ACCOUNT_CLOSURE_PAYMENTS_PENDING"],
        }),
      )
      .mockResolvedValueOnce(closureCheck());
    renderModal();

    expect(await screen.findByText(BLOCKERS_TITLE)).toBeInTheDocument();
    expect(screen.getByText(/The account is not disabled\. Disable it first\./)).toBeInTheDocument();
    expect(
      screen.getByText(/Some payments are pending, under review, or expired less than 7 days ago\./),
    ).toBeInTheDocument();
    expect(screen.getByText("ACCOUNT_STATUS_TRANSITION_INVALID")).toBeInTheDocument();
    expect(screen.getByText("ACCOUNT_CLOSURE_PAYMENTS_PENDING")).toBeInTheDocument();

    // 原因与客户名都合格也不行。
    await fill(user);
    expect(buttonWithText(CONFIRM)).toBeDisabled();

    await user.click(buttonWithText("Refresh"));

    await checkPassed();
    expect(api.getClosureCheck).toHaveBeenCalledTimes(2);
    expect(screen.queryByText(BLOCKERS_TITLE)).not.toBeInTheDocument();
    await waitFor(() => {
      expect(buttonWithText(CONFIRM)).toBeEnabled();
    });
    expect(api.postCloseAccount).not.toHaveBeenCalled();
  });

  it("shows the error code and request id of a failed check, keeps confirm disabled, and offers Refresh", async () => {
    const user = userEvent.setup();
    api.getClosureCheck
      .mockRejectedValueOnce(
        new CloseAccountError("INTERNAL_ERROR", "An unexpected error occurred.", "req-500", 500),
      )
      .mockResolvedValueOnce(closureCheck());
    renderModal();

    expect(await screen.findByText(LOAD_FAILED)).toBeInTheDocument();
    expect(screen.getByText("Error code: INTERNAL_ERROR")).toBeInTheDocument();
    expect(screen.getByText("req-500")).toBeInTheDocument();
    await fill(user);
    expect(buttonWithText(CONFIRM)).toBeDisabled();
    // 不自动重试。
    expect(api.getClosureCheck).toHaveBeenCalledTimes(1);

    await user.click(buttonWithText("Refresh"));

    await checkPassed();
    expect(api.getClosureCheck).toHaveBeenCalledTimes(2);
    await waitFor(() => {
      expect(buttonWithText(CONFIRM)).toBeEnabled();
    });
  });

  it.each([
    { name: "a 404", error: rejection(404, "CUSTOMER_NOT_FOUND", "req-404"), shown: "Error code: CUSTOMER_NOT_FOUND" },
    {
      name: "a network failure",
      error: new CloseAccountError("NETWORK_ERROR", "Could not reach the billing platform.", null, null),
      shown: "Error code: NETWORK_ERROR",
    },
    {
      name: "a response without an error code",
      error: new CloseAccountError("MALFORMED_RESPONSE", "The server returned an unexpected response.", null, 502),
      shown: "Error code: HTTP 502",
    },
  ])("keeps confirm disabled after $name of the check", async ({ error, shown }) => {
    const user = userEvent.setup();
    api.getClosureCheck.mockRejectedValue(error);
    renderModal();

    expect(await screen.findByText(LOAD_FAILED)).toBeInTheDocument();
    expect(screen.getByText(shown)).toBeInTheDocument();
    await fill(user);
    expect(buttonWithText(CONFIRM)).toBeDisabled();
    expect(buttonWithText("Refresh")).toBeEnabled();
  });

  it("keeps confirm disabled when the latest check failed, even after an earlier one passed", async () => {
    const user = userEvent.setup();
    api.getClosureCheck
      .mockResolvedValueOnce(closureCheck({ blockers: ["ACCOUNT_CLOSURE_USAGE_PENDING"], unresolved_usage_events: 2 }))
      .mockRejectedValueOnce(rejection(404, "CUSTOMER_NOT_FOUND", "req-404"));
    renderModal();

    expect(await screen.findByText(BLOCKERS_TITLE)).toBeInTheDocument();
    await user.click(buttonWithText("Refresh"));

    expect(await screen.findByText(LOAD_FAILED)).toBeInTheDocument();
    await fill(user);
    expect(buttonWithText(CONFIRM)).toBeDisabled();
  });

  it.each([
    { label: "0 code points", reason: "", enabled: false, tooLong: false },
    { label: "only whitespace", reason: "   \n  ", enabled: false, tooLong: false },
    { label: "1 code point", reason: "a", enabled: true, tooLong: false },
    { label: "255 code points", reason: "a".repeat(255), enabled: true, tooLong: false },
    // emoji 是 1 个码点、2 个 UTF-16 单元：按码点数才是 255。
    { label: "255 emoji", reason: "😀".repeat(255), enabled: true, tooLong: false },
    { label: "256 code points", reason: "a".repeat(256), enabled: false, tooLong: true },
  ])("accepts a reason of $label only when it is 1–255 after trimming", async ({ reason, enabled, tooLong }) => {
    const user = userEvent.setup();
    api.getClosureCheck.mockResolvedValue(closureCheck());
    renderModal();
    await checkPassed();

    await fill(user, { reason });

    if (enabled) {
      await waitFor(() => {
        expect(buttonWithText(CONFIRM)).toBeEnabled();
      });
    } else {
      expect(buttonWithText(CONFIRM)).toBeDisabled();
    }
    if (tooLong) {
      expect(screen.getByText(REASON_TOO_LONG)).toBeInTheDocument();
    } else {
      expect(screen.queryByText(REASON_TOO_LONG)).not.toBeInTheDocument();
    }
  });

  it("sends 255 code points surrounded by spaces as the trimmed reason", async () => {
    const user = userEvent.setup();
    api.getClosureCheck.mockResolvedValue(closureCheck());
    api.postCloseAccount.mockResolvedValue(customer({ account_status: "CLOSED" }));
    renderModal();
    await checkPassed();

    await fill(user, { reason: ` ${"a".repeat(255)} ` });
    await user.click(buttonWithText(CONFIRM));

    await waitFor(() => {
      expect(api.postCloseAccount).toHaveBeenCalledWith(CUSTOMER_ID, { reason: "a".repeat(255) });
    });
  });

  it.each([
    { label: "nothing", name: "", enabled: false },
    { label: "a different name", name: "Acme", enabled: false },
    { label: "a different case", name: "acme sdn bhd", enabled: false },
    { label: "the name with surrounding whitespace", name: `  ${COMPANY}  `, enabled: true },
    { label: "the exact name", name: COMPANY, enabled: true },
  ])("enables confirm for $label as the company name only when it matches exactly", async ({ name, enabled }) => {
    const user = userEvent.setup();
    api.getClosureCheck.mockResolvedValue(closureCheck());
    renderModal();
    await checkPassed();

    await fill(user, { name });

    if (enabled) {
      await waitFor(() => {
        expect(buttonWithText(CONFIRM)).toBeEnabled();
      });
    } else {
      expect(buttonWithText(CONFIRM)).toBeDisabled();
    }
  });

  it("closes and makes this customer's detail and the customer lists stale on success", async () => {
    const user = userEvent.setup();
    api.getClosureCheck.mockResolvedValue(closureCheck());
    api.postCloseAccount.mockResolvedValue(customer({ account_status: "CLOSED", status_version: 5 }));
    const { invalidate, onClose } = renderModal();
    await checkPassed();

    await fill(user, { reason: "  Closure ticket CL-0001  " });
    await user.click(buttonWithText(CONFIRM));

    await waitFor(() => {
      expect(onClose).toHaveBeenCalledTimes(1);
    });
    expect(api.postCloseAccount).toHaveBeenCalledTimes(1);
    expect(api.postCloseAccount).toHaveBeenCalledWith(CUSTOMER_ID, { reason: "Closure ticket CL-0001" });
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: customerDetailQueryKey(CUSTOMER_ID),
      exact: true,
    });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: CUSTOMER_LISTS_QUERY_KEY });
  });

  it.each([
    { status: 404, code: "CUSTOMER_NOT_FOUND", requestId: "req-404" },
    { status: 409, code: "ACCOUNT_CLOSURE_BALANCE_NOT_ZERO", requestId: "req-409" },
    { status: 422, code: "VALIDATION_ERROR", requestId: "req-422" },
  ])(
    "shows the error code, message and request id of a $status and reads the check again",
    async ({ status, code, requestId }) => {
      const user = userEvent.setup();
      api.getClosureCheck.mockResolvedValue(closureCheck());
      api.postCloseAccount.mockRejectedValue(rejection(status, code, requestId));
      const { invalidate, onClose } = renderModal();
      await checkPassed();

      await fill(user);
      await user.click(buttonWithText(CONFIRM));

      expect(await screen.findByText(REJECTED)).toBeInTheDocument();
      expect(screen.getByText(`Error code: ${code}`)).toBeInTheDocument();
      expect(screen.getByText(new RegExp(`Rejected with ${code}\\.`))).toBeInTheDocument();
      expect(screen.getByText(requestId)).toBeInTheDocument();
      expect(screen.queryByText(UNKNOWN_TITLE)).not.toBeInTheDocument();
      await waitFor(() => {
        expect(api.getClosureCheck).toHaveBeenCalledTimes(2);
      });
      // 不自动重试。
      expect(api.postCloseAccount).toHaveBeenCalledTimes(1);
      expect(screen.queryByText("Try again")).not.toBeInTheDocument();
      expect(onClose).not.toHaveBeenCalled();
      expect(invalidate).not.toHaveBeenCalled();
    },
  );

  it("keeps confirm disabled when the check read after a 409 reports a blocker", async () => {
    const user = userEvent.setup();
    api.getClosureCheck
      .mockResolvedValueOnce(closureCheck())
      .mockResolvedValueOnce(
        closureCheck({ unresolved_usage_events: 1, blockers: ["ACCOUNT_CLOSURE_USAGE_PENDING"] }),
      );
    api.postCloseAccount.mockRejectedValue(rejection(409, "ACCOUNT_CLOSURE_USAGE_PENDING", "req-409"));
    renderModal();
    await checkPassed();

    await fill(user);
    await user.click(buttonWithText(CONFIRM));

    expect(await screen.findByText(BLOCKERS_TITLE)).toBeInTheDocument();
    expect(screen.getByText("req-409")).toBeInTheDocument();
    expect(buttonWithText(CONFIRM)).toBeDisabled();
  });

  it("offers to retry a 5xx as it is, keeping the inputs, and sends the same body again", async () => {
    const user = userEvent.setup();
    api.getClosureCheck.mockResolvedValue(closureCheck());
    api.postCloseAccount
      .mockRejectedValueOnce(
        new CloseAccountError("INTERNAL_ERROR", "An unexpected error occurred.", "req-500", 500),
      )
      .mockResolvedValueOnce(customer({ account_status: "CLOSED", status_version: 5 }));
    const { onClose } = renderModal();
    await checkPassed();

    await fill(user);
    await user.click(buttonWithText(CONFIRM));

    expect(await screen.findByText(UNKNOWN_TITLE)).toBeInTheDocument();
    expect(screen.getByText("req-500")).toBeInTheDocument();
    expect(screen.queryByText(CONFIRM)).not.toBeInTheDocument();
    expect(screen.getByLabelText("Reason")).toHaveValue("Closure ticket CL-0001");
    expect(screen.getByLabelText("Reason")).toBeDisabled();
    expect(screen.getByLabelText(NAME_LABEL)).toBeDisabled();
    // 不自动重试；5xx 不重读检查。
    expect(api.postCloseAccount).toHaveBeenCalledTimes(1);
    expect(api.getClosureCheck).toHaveBeenCalledTimes(1);

    await user.click(buttonWithText("Try again"));

    await waitFor(() => {
      expect(onClose).toHaveBeenCalledTimes(1);
    });
    expect(api.postCloseAccount).toHaveBeenCalledTimes(2);
    expect(api.postCloseAccount.mock.calls[1]).toEqual(api.postCloseAccount.mock.calls[0]);
  });

  it("offers to retry a network failure as it is", async () => {
    const user = userEvent.setup();
    api.getClosureCheck.mockResolvedValue(closureCheck());
    api.postCloseAccount.mockRejectedValue(
      new CloseAccountError("NETWORK_ERROR", "Could not reach the billing platform.", null, null),
    );
    renderModal();
    await checkPassed();

    await fill(user);
    await user.click(buttonWithText(CONFIRM));

    expect(await screen.findByText(UNKNOWN_TITLE)).toBeInTheDocument();
    expect(screen.getByText(/Could not reach the billing platform\./)).toBeInTheDocument();
    expect(api.postCloseAccount).toHaveBeenCalledTimes(1);

    await user.click(buttonWithText("Try again"));

    await waitFor(() => {
      expect(api.postCloseAccount).toHaveBeenCalledTimes(2);
    });
    expect(api.postCloseAccount).toHaveBeenLastCalledWith(CUSTOMER_ID, { reason: "Closure ticket CL-0001" });
  });
});
