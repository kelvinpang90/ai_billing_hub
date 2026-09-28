/**
 * 客户详情与编辑。
 *
 * 编辑那几条测的是**发出去的 PATCH**：只带改了的字段、清空发 null、没改动不发。
 * 这几处写错不会报错 —— 多带一个没改的 email 看上去无害，但它会让审计的
 * `changed_fields` 失真；空 PATCH 则是一句用户看不懂的 422。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import type { CustomerDetail } from "../../api/adminCustomers";
import { ApiError } from "../../api/client";
import { AdjustmentError, type Adjustment } from "../../api/walletAdjustments";
import { ROUTES, customerDetailPath } from "../../routes/paths";
import { CustomerDetailPage } from "./CustomerDetailPage";

const CUSTOMER_ID = "00000000-0000-4000-8000-000000000000";

const api = vi.hoisted(() => ({
  getCustomer: vi.fn(),
  updateCustomer: vi.fn(),
  // 项目区块（AIH-TASK-016）挂在详情页上；它自己的用例在 ProjectsPanel.test.tsx。
  listProjects: vi.fn(),
  createProject: vi.fn(),
}));

vi.mock("../../api/adminCustomers", async () => {
  const actual =
    await vi.importActual<typeof import("../../api/adminCustomers")>("../../api/adminCustomers");
  return { ...actual, ...api };
});

// 手工调账（AIH-TASK-017）从钱包卡片打开；表单自己的用例在 wallet/AdjustmentModal.test.tsx。
const wallet = vi.hoisted(() => ({
  postAdjustment: vi.fn(),
}));

vi.mock("../../api/walletAdjustments", async () => {
  const actual = await vi.importActual<typeof import("../../api/walletAdjustments")>(
    "../../api/walletAdjustments",
  );
  return { ...actual, ...wallet };
});

const FIRST_KEY = "00000000-0000-4000-8000-000000000001";
const SECOND_KEY = "00000000-0000-4000-8000-000000000002";

function adjustment(overrides: Partial<Adjustment> = {}): Adjustment {
  return {
    id: "00000000-0000-4000-8000-000000000003",
    customer_id: CUSTOMER_ID,
    transaction_type: "ADJUSTMENT_DEBIT",
    amount: "-20.50000000",
    balance_before: "1234567.12345678",
    balance_after: "1234546.62345678",
    wallet_sequence: 8,
    reason: "Refund for outage",
    idempotency_key: FIRST_KEY,
    created_at: "2026-09-23T08:30:00",
    replayed: false,
    billing_status: "ACTIVE",
    status_version: 3,
    ...overrides,
  };
}

/** 打开调账表单，填一笔借方 20.5，确认并发送。 */
async function postDebit(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: "Adjust balance" }));
  await user.click(
    await screen.findByRole("radio", { name: "Adjustment debit (takes from the balance)" }),
  );
  await user.click(screen.getByLabelText("Amount"));
  await user.paste("20.5");
  await user.click(screen.getByLabelText("Reason"));
  await user.paste("Refund for outage");
  await user.click(screen.getByRole("button", { name: "Review" }));
  await user.click(await screen.findByRole("button", { name: "Confirm and post" }));
}

function customer(overrides: Partial<CustomerDetail> = {}): CustomerDetail {
  return {
    id: CUSTOMER_ID,
    company_name: "Acme Sdn Bhd",
    contact_name: "Contact Person",
    email: "ops@example.com",
    phone: "+60 3-0000 0000",
    billing_status: "ACTIVE",
    status_version: 3,
    created_at: "2026-09-20T08:30:00",
    updated_at: "2026-09-21T01:02:03",
    wallet: { currency: "MYR", balance: "1234567.12345678", version: 7 },
    ...overrides,
  };
}

function renderDetail() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[customerDetailPath(CUSTOMER_ID)]}>
        <Routes>
          <Route path={ROUTES.customerDetail} element={<CustomerDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function openEditor(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: "Edit" }));
  return screen.getByRole("button", { name: /Save changes/ });
}

beforeEach(() => {
  vi.clearAllMocks();
  api.listProjects.mockResolvedValue({ items: [], page: 1, page_size: 20, total: 0 });
});

afterEach(() => {
  // 只还原 crypto.randomUUID 上的 spy；模块 mock 由 beforeEach 清空。
  vi.restoreAllMocks();
});

describe("CustomerDetailPage", () => {
  it("shows every field of the customer and its wallet", async () => {
    api.getCustomer.mockResolvedValue(customer());

    renderDetail();

    expect(await screen.findByText("Contact Person")).toBeInTheDocument();
    expect(api.getCustomer).toHaveBeenCalledWith(CUSTOMER_ID, expect.anything());
    expect(screen.getByText(CUSTOMER_ID)).toBeInTheDocument();
    expect(screen.getAllByText("Acme Sdn Bhd").length).toBeGreaterThan(0);
    expect(screen.getByText("ops@example.com")).toBeInTheDocument();
    expect(screen.getByText("+60 3-0000 0000")).toBeInTheDocument();
    expect(screen.getByText("Active")).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.getByText("2026-09-20 16:30:00")).toBeInTheDocument();
    expect(screen.getByText("2026-09-21 09:02:03")).toBeInTheDocument();
    // 钱包：币种、余额（逐位不丢）、版本。
    expect(screen.getByText("MYR")).toBeInTheDocument();
    expect(screen.getByText("MYR 1,234,567.12345678")).toBeInTheDocument();
    expect(screen.getByText("7")).toBeInTheDocument();
  });

  it("says the customer does not exist on a 404, not that something went wrong", async () => {
    api.getCustomer.mockRejectedValue(
      new ApiError("CUSTOMER_NOT_FOUND", "The customer does not exist.", "req-404"),
    );

    renderDetail();

    expect(await screen.findByText("Customer not found")).toBeInTheDocument();
    expect(screen.queryByText("The customer could not be loaded.")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to customers" })).toHaveAttribute(
      "href",
      "/customers",
    );
  });

  it("shows the backend message and request id for any other failure", async () => {
    api.getCustomer.mockRejectedValue(
      new ApiError("DATABASE_NOT_CONFIGURED", "The database is not configured.", "req-503"),
    );

    renderDetail();

    expect(await screen.findByText("The customer could not be loaded.")).toBeInTheDocument();
    expect(screen.getByText(/The database is not configured\./)).toBeInTheDocument();
    expect(screen.getByText("req-503")).toBeInTheDocument();
  });

  it("says there is no permission on a 403", async () => {
    api.getCustomer.mockRejectedValue(
      new ApiError("ADMIN_REQUIRED", "Administrator access is required.", "req-403"),
    );

    renderDetail();

    expect(
      await screen.findByText("Only administrators can manage customers."),
    ).toBeInTheDocument();
  });

  it("sends only the field that changed and shows the saved customer from the response", async () => {
    const user = userEvent.setup();
    api.getCustomer.mockResolvedValue(customer());
    api.updateCustomer.mockResolvedValue(customer({ company_name: "Acme Renamed" }));
    renderDetail();

    const save = await openEditor(user);
    const name = screen.getByLabelText("Company name");
    await user.clear(name);
    await user.click(name);
    await user.paste("Acme Renamed");
    await user.click(save);

    expect(await screen.findByText("Changes saved.")).toBeInTheDocument();
    expect(api.updateCustomer).toHaveBeenCalledTimes(1);
    expect(api.updateCustomer).toHaveBeenCalledWith(CUSTOMER_ID, { company_name: "Acme Renamed" });
    expect(screen.getAllByText("Acme Renamed").length).toBeGreaterThan(0);
    // 用的是 PATCH 的响应，没有再读一次。
    expect(api.getCustomer).toHaveBeenCalledTimes(1);
  });

  it("clears contact name and phone by sending null", async () => {
    const user = userEvent.setup();
    api.getCustomer.mockResolvedValue(customer());
    api.updateCustomer.mockResolvedValue(customer({ contact_name: null, phone: null }));
    renderDetail();

    const save = await openEditor(user);
    await user.clear(screen.getByLabelText("Contact name"));
    await user.clear(screen.getByLabelText("Phone"));
    await user.click(save);

    await waitFor(() => {
      expect(api.updateCustomer).toHaveBeenCalledWith(CUSTOMER_ID, {
        contact_name: null,
        phone: null,
      });
    });
  });

  it("does not send a request when nothing was changed", async () => {
    const user = userEvent.setup();
    api.getCustomer.mockResolvedValue(customer());
    renderDetail();

    const save = await openEditor(user);
    await user.click(save);

    expect(
      await screen.findByText("Nothing was changed, so nothing was saved."),
    ).toBeInTheDocument();
    expect(api.updateCustomer).not.toHaveBeenCalled();
  });

  it("keeps the form open with the backend message when the edit is rejected", async () => {
    const user = userEvent.setup();
    api.getCustomer.mockResolvedValue(customer());
    api.updateCustomer.mockRejectedValue(
      new ApiError("VALIDATION_ERROR", "Invalid field: body.email", "req-422"),
    );
    renderDetail();

    const save = await openEditor(user);
    const email = screen.getByLabelText("Email");
    await user.clear(email);
    await user.click(email);
    await user.paste("billing@example.com");
    await user.click(save);

    expect(await screen.findByText(/Invalid field: body\.email/)).toBeInTheDocument();
    expect(screen.getByText("req-422")).toBeInTheDocument();
    expect(screen.getByLabelText("Email")).toHaveValue("billing@example.com");
  });

  it("shows the customer's projects below the wallet", async () => {
    api.getCustomer.mockResolvedValue(customer());
    api.listProjects.mockResolvedValue({
      items: [
        {
          id: "00000000-0000-4000-8000-000000000001",
          name: "Chatbot",
          description: null,
          created_at: "2026-09-20T08:31:00",
          updated_at: "2026-09-20T08:31:00",
        },
      ],
      page: 1,
      page_size: 20,
      total: 1,
    });

    renderDetail();

    expect(await screen.findByText("Chatbot")).toBeInTheDocument();
    expect(screen.getByText("Projects")).toBeInTheDocument();
    expect(api.listProjects).toHaveBeenCalledWith(CUSTOMER_ID, 1, 20, expect.anything());
  });

  it("does not ask for projects of a customer that does not exist", async () => {
    api.getCustomer.mockRejectedValue(
      new ApiError("CUSTOMER_NOT_FOUND", "The customer does not exist.", "req-404"),
    );

    renderDetail();

    expect(await screen.findByText("Customer not found")).toBeInTheDocument();
    expect(api.listProjects).not.toHaveBeenCalled();
  });

  it("refreshes the balance from the backend after an adjustment is posted", async () => {
    const user = userEvent.setup();
    vi.spyOn(crypto, "randomUUID").mockReturnValue(FIRST_KEY);
    api.getCustomer
      .mockResolvedValueOnce(customer())
      .mockResolvedValue(
        customer({ wallet: { currency: "MYR", balance: "1234546.62345678", version: 8 } }),
      );
    wallet.postAdjustment.mockResolvedValue(adjustment());
    renderDetail();

    await postDebit(user);

    expect(await screen.findByText("Adjustment posted.")).toBeInTheDocument();
    expect(wallet.postAdjustment).toHaveBeenCalledWith(CUSTOMER_ID, {
      transaction_type: "ADJUSTMENT_DEBIT",
      amount: "-20.5",
      reason: "Refund for outage",
      idempotency_key: FIRST_KEY,
    });
    // 详情重读了一次，钱包卡片换成后端给的新余额。
    await waitFor(() => {
      expect(api.getCustomer).toHaveBeenCalledTimes(2);
    });
    await user.click(screen.getByRole("button", { name: "Done" }));
    expect(await screen.findByText("MYR 1,234,546.62345678")).toBeInTheDocument();
    expect(screen.queryByText("MYR 1,234,567.12345678")).not.toBeInTheDocument();
    expect(screen.queryByText("Manual wallet adjustment")).not.toBeInTheDocument();
  });

  it("uses a new idempotency key only when the form is closed and opened again", async () => {
    const user = userEvent.setup();
    const randomUUID = vi
      .spyOn(crypto, "randomUUID")
      .mockReturnValueOnce(FIRST_KEY)
      .mockReturnValueOnce(SECOND_KEY);
    api.getCustomer.mockResolvedValue(customer());
    wallet.postAdjustment
      .mockRejectedValueOnce(
        new AdjustmentError("NETWORK_ERROR", "Could not reach the billing platform.", null, null),
      )
      .mockResolvedValueOnce(adjustment({ idempotency_key: SECOND_KEY }));
    renderDetail();

    await postDebit(user);
    expect(
      await screen.findByText("We could not confirm whether the adjustment was posted."),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Close this form" }));
    expect(screen.queryByText("Manual wallet adjustment")).not.toBeInTheDocument();

    // 再打开：表单是新的、可编辑，发出去的是新键。
    await postDebit(user);

    expect(await screen.findByText("Adjustment posted.")).toBeInTheDocument();
    expect(randomUUID).toHaveBeenCalledTimes(2);
    expect(wallet.postAdjustment).toHaveBeenCalledTimes(2);
    expect(wallet.postAdjustment.mock.calls[0]?.[1] as unknown).toMatchObject({
      idempotency_key: FIRST_KEY,
    });
    expect(wallet.postAdjustment.mock.calls[1]?.[1] as unknown).toMatchObject({
      idempotency_key: SECOND_KEY,
    });
  });
});
