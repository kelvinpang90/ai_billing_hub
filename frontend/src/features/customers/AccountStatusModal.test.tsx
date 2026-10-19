/**
 * 启用 / 停用账户的弹窗：原因的校验、停用的二次确认、发出去的请求体、成功后的失效重读，
 * 以及 404 / 409 / 422 / 5xx / 网络错误各自的展示。
 *
 * 输入一律 click + paste：逐字 `user.type` 每按一键 antd 表单就重渲染一次，全量并行时会超时。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import {
  AccountStatusError,
  CUSTOMER_LISTS_QUERY_KEY,
  customerDetailQueryKey,
  type AccountStatusTarget,
  type CustomerDetail,
} from "../../api/adminCustomers";
import { AccountStatusModal, accountStatusTarget } from "./AccountStatusModal";

const CUSTOMER_ID = "00000000-0000-0000-0000-000000000000";

const api = vi.hoisted(() => ({
  postAccountStatus: vi.fn(),
}));

vi.mock("../../api/adminCustomers", async () => {
  const actual =
    await vi.importActual<typeof import("../../api/adminCustomers")>("../../api/adminCustomers");
  return { ...actual, ...api };
});

const REASON_REQUIRED = "Enter a reason.";
const REASON_TOO_LONG = "The reason can be at most 255 characters.";
const REJECTED = "The account status was not changed.";
const UNKNOWN_TITLE = "We could not confirm whether the account status was changed.";

function customer(overrides: Partial<CustomerDetail> = {}): CustomerDetail {
  return {
    id: CUSTOMER_ID,
    company_name: "Acme Sdn Bhd",
    contact_name: null,
    email: "ops@example.com",
    phone: null,
    billing_status: "ACTIVE",
    account_status: "ENABLED",
    billing_mode: "PREPAID",
    ai_service_enabled: true,
    status_version: 3,
    created_at: "2026-09-20T08:30:00",
    updated_at: "2026-09-21T01:02:03",
    wallet: { currency: "MYR", balance: "100.00000000", version: 3 },
    ...overrides,
  };
}

function renderModal(target: AccountStatusTarget) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidate = vi.spyOn(queryClient, "invalidateQueries");
  const onClose = vi.fn();
  render(
    <QueryClientProvider client={queryClient}>
      <AccountStatusModal customer={customer()} target={target} onClose={onClose} />
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

async function typeReason(user: User, reason: string) {
  await user.click(screen.getByLabelText("Reason"));
  await user.paste(reason);
}

/** 停用：填原因 → Continue → Confirm and disable。 */
async function disableWith(user: User, reason = "Contract paused") {
  await typeReason(user, reason);
  await user.click(buttonWithText("Continue"));
  await user.click(await screen.findByText("Confirm and disable"));
}

function rejection(status: number, code: string, requestId: string) {
  return new AccountStatusError(code, `Rejected with ${code}.`, requestId, status);
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("accountStatusTarget", () => {
  it("offers disable for ENABLED, enable for DISABLED and nothing otherwise", () => {
    expect(accountStatusTarget("ENABLED")).toBe("DISABLED");
    expect(accountStatusTarget("DISABLED")).toBe("ENABLED");
    expect(accountStatusTarget("PENDING_ACTIVATION")).toBeNull();
    expect(accountStatusTarget("CLOSED")).toBeNull();
    expect(accountStatusTarget("FROZEN")).toBeNull();
    expect(accountStatusTarget(undefined)).toBeNull();
  });
});

describe("AccountStatusModal", () => {
  it("says what disabling does and that the reason must not hold personal data", () => {
    renderModal("DISABLED");

    expect(screen.getByText(/AI service requests are refused \(ACCOUNT_DISABLED\)/)).toBeInTheDocument();
    expect(
      screen.getByText(/Do not include contact names, phone numbers, email addresses/),
    ).toBeInTheDocument();
  });

  it("does not submit an empty reason", async () => {
    const user = userEvent.setup();
    renderModal("ENABLED");

    await user.click(buttonWithText("Confirm and enable"));

    expect(await screen.findByText(REASON_REQUIRED)).toBeInTheDocument();
    expect(api.postAccountStatus).not.toHaveBeenCalled();
  });

  it("does not submit a reason that is only whitespace", async () => {
    const user = userEvent.setup();
    renderModal("ENABLED");

    await typeReason(user, "   \n  ");
    await user.click(buttonWithText("Confirm and enable"));

    expect(await screen.findByText(REASON_REQUIRED)).toBeInTheDocument();
    expect(api.postAccountStatus).not.toHaveBeenCalled();
  });

  it("does not submit a reason longer than 255 characters after trimming", async () => {
    const user = userEvent.setup();
    renderModal("DISABLED");

    await typeReason(user, "a".repeat(256));
    await user.click(buttonWithText("Continue"));

    expect(await screen.findByText(REASON_TOO_LONG)).toBeInTheDocument();
    expect(screen.queryByText("Confirm and disable")).not.toBeInTheDocument();
    expect(api.postAccountStatus).not.toHaveBeenCalled();
  });

  it("accepts 255 characters surrounded by spaces and sends the trimmed reason", async () => {
    const user = userEvent.setup();
    api.postAccountStatus.mockResolvedValue(customer({ account_status: "ENABLED" }));
    renderModal("ENABLED");

    await typeReason(user, ` ${"a".repeat(255)} `);
    await user.click(buttonWithText("Confirm and enable"));

    await waitFor(() => {
      expect(api.postAccountStatus).toHaveBeenCalledWith(CUSTOMER_ID, {
        account_status: "ENABLED",
        reason: "a".repeat(255),
      });
    });
  });

  it("asks for confirmation before disabling and sends nothing until confirmed", async () => {
    const user = userEvent.setup();
    api.postAccountStatus.mockResolvedValue(customer({ account_status: "DISABLED" }));
    renderModal("DISABLED");

    await typeReason(user, "  Contract paused  ");
    await user.click(buttonWithText("Continue"));

    expect(await screen.findByText("Disable the account of Acme Sdn Bhd?")).toBeInTheDocument();
    expect(screen.getByText(/every AI service request from this customer is refused/)).toBeInTheDocument();
    expect(api.postAccountStatus).not.toHaveBeenCalled();

    await user.click(buttonWithText("Confirm and disable"));

    await waitFor(() => {
      expect(api.postAccountStatus).toHaveBeenCalledTimes(1);
    });
    expect(api.postAccountStatus).toHaveBeenCalledWith(CUSTOMER_ID, {
      account_status: "DISABLED",
      reason: "Contract paused",
    });
  });

  it("goes back from the confirmation without sending anything", async () => {
    const user = userEvent.setup();
    renderModal("DISABLED");

    await typeReason(user, "Contract paused");
    await user.click(buttonWithText("Continue"));
    await user.click(await screen.findByText("Back"));

    expect(screen.queryByText("Confirm and disable")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Reason")).toHaveValue("Contract paused");
    expect(screen.getByLabelText("Reason")).toBeEnabled();
    expect(api.postAccountStatus).not.toHaveBeenCalled();
  });

  it("enables in one step", async () => {
    const user = userEvent.setup();
    api.postAccountStatus.mockResolvedValue(customer({ account_status: "ENABLED" }));
    renderModal("ENABLED");

    await typeReason(user, "Paid up");
    await user.click(buttonWithText("Confirm and enable"));

    await waitFor(() => {
      expect(api.postAccountStatus).toHaveBeenCalledWith(CUSTOMER_ID, {
        account_status: "ENABLED",
        reason: "Paid up",
      });
    });
  });

  it("closes and makes this customer's detail and the customer lists stale on success", async () => {
    const user = userEvent.setup();
    api.postAccountStatus.mockResolvedValue(customer({ account_status: "DISABLED" }));
    const { invalidate, onClose } = renderModal("DISABLED");

    await disableWith(user);

    await waitFor(() => {
      expect(onClose).toHaveBeenCalledTimes(1);
    });
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: customerDetailQueryKey(CUSTOMER_ID),
      exact: true,
    });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: CUSTOMER_LISTS_QUERY_KEY });
  });

  it("disables the button while the request is on its way and sends it once", async () => {
    const user = userEvent.setup();
    let resolve: (value: CustomerDetail) => void = () => undefined;
    api.postAccountStatus.mockReturnValue(
      new Promise<CustomerDetail>((settle) => {
        resolve = settle;
      }),
    );
    const { onClose } = renderModal("DISABLED");

    await disableWith(user);
    const confirm = buttonWithText("Confirm and disable");
    await waitFor(() => {
      expect(confirm).toBeDisabled();
    });
    expect(buttonWithText("Back")).toBeDisabled();
    expect(api.postAccountStatus).toHaveBeenCalledTimes(1);
    expect(onClose).not.toHaveBeenCalled();

    resolve(customer({ account_status: "DISABLED" }));
    await waitFor(() => {
      expect(onClose).toHaveBeenCalledTimes(1);
    });
  });

  it.each([
    { status: 404, code: "CUSTOMER_NOT_FOUND", requestId: "req-404" },
    { status: 409, code: "ACCOUNT_STATUS_TRANSITION_INVALID", requestId: "req-409" },
    { status: 422, code: "VALIDATION_ERROR", requestId: "req-422" },
  ])("shows the error code and request id of a $status", async ({ status, code, requestId }) => {
    const user = userEvent.setup();
    api.postAccountStatus.mockRejectedValue(rejection(status, code, requestId));
    const { invalidate, onClose } = renderModal("DISABLED");

    await disableWith(user);

    expect(await screen.findByText(REJECTED)).toBeInTheDocument();
    expect(screen.getByText(`Error code: ${code}`)).toBeInTheDocument();
    expect(screen.getByText(requestId)).toBeInTheDocument();
    expect(screen.queryByText(UNKNOWN_TITLE)).not.toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
    expect(invalidate).not.toHaveBeenCalled();
  });

  it("offers to retry a 5xx as it is, keeping the reason, and sends the same body again", async () => {
    const user = userEvent.setup();
    api.postAccountStatus
      .mockRejectedValueOnce(
        new AccountStatusError("INTERNAL_ERROR", "An unexpected error occurred.", "req-500", 500),
      )
      .mockResolvedValueOnce(customer({ account_status: "DISABLED" }));
    const { onClose } = renderModal("DISABLED");

    await disableWith(user);

    expect(await screen.findByText(UNKNOWN_TITLE)).toBeInTheDocument();
    expect(screen.getByText("req-500")).toBeInTheDocument();
    expect(screen.getByLabelText("Reason")).toHaveValue("Contract paused");
    // 不自动重试。
    expect(api.postAccountStatus).toHaveBeenCalledTimes(1);

    await user.click(buttonWithText("Try again"));

    await waitFor(() => {
      expect(onClose).toHaveBeenCalledTimes(1);
    });
    expect(api.postAccountStatus).toHaveBeenCalledTimes(2);
    expect(api.postAccountStatus.mock.calls[1]).toEqual(api.postAccountStatus.mock.calls[0]);
  });

  it("offers to retry a network failure as it is, keeping the reason", async () => {
    const user = userEvent.setup();
    api.postAccountStatus.mockRejectedValue(
      new AccountStatusError("NETWORK_ERROR", "Could not reach the billing platform.", null, null),
    );
    renderModal("ENABLED");

    await typeReason(user, "Paid up");
    await user.click(buttonWithText("Confirm and enable"));

    expect(await screen.findByText(UNKNOWN_TITLE)).toBeInTheDocument();
    expect(screen.getByText(/Could not reach the billing platform\./)).toBeInTheDocument();
    expect(screen.getByLabelText("Reason")).toHaveValue("Paid up");
    expect(api.postAccountStatus).toHaveBeenCalledTimes(1);

    await user.click(buttonWithText("Try again"));

    await waitFor(() => {
      expect(api.postAccountStatus).toHaveBeenCalledTimes(2);
    });
    expect(api.postAccountStatus).toHaveBeenLastCalledWith(CUSTOMER_ID, {
      account_status: "ENABLED",
      reason: "Paid up",
    });
  });
});
