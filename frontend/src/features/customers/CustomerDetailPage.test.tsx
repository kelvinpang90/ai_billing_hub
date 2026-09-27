/**
 * 客户详情：全部字段与钱包、404 与 403 的说法、编辑只发改动的字段。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import type { CustomerDetail } from "../../api/adminCustomers";
import { ApiError } from "../../api/client";
import { ROUTES } from "../../routes/paths";
import { CustomerDetailPage } from "./CustomerDetailPage";

const CUSTOMER_ID = "00000000-0000-4000-8000-000000000000";

const api = vi.hoisted(() => ({
  getCustomer: vi.fn(),
  updateCustomer: vi.fn(),
}));

vi.mock("../../api/adminCustomers", async () => {
  const actual =
    await vi.importActual<typeof import("../../api/adminCustomers")>("../../api/adminCustomers");
  return { ...actual, ...api };
});

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
    updated_at: "2026-09-21T17:05:09",
    wallet: { currency: "MYR", balance: "1234567.12345678", version: 7 },
    ...overrides,
  };
}

function renderDetail() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, retryDelay: 0 }, mutations: { retry: 0 } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[`/customers/${CUSTOMER_ID}`]}>
        <Routes>
          <Route path={ROUTES.customerDetail} element={<CustomerDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("CustomerDetailPage", () => {
  it("shows every field of the customer and its wallet", async () => {
    api.getCustomer.mockResolvedValue(customer());
    renderDetail();

    expect(await screen.findByText(CUSTOMER_ID)).toBeInTheDocument();
    expect(api.getCustomer).toHaveBeenCalledWith(CUSTOMER_ID, expect.anything());
    expect(screen.getAllByText("Acme Sdn Bhd").length).toBeGreaterThan(0);
    expect(screen.getByText("Contact Person")).toBeInTheDocument();
    expect(screen.getByText("ops@example.com")).toBeInTheDocument();
    expect(screen.getByText("+60 3-0000 0000")).toBeInTheDocument();
    expect(screen.getByText("Active")).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
    // 后端给的是 UTC，页面上是吉隆坡时间（+8）。
    expect(screen.getByText("2026-09-20 16:30:00")).toBeInTheDocument();
    expect(screen.getByText("2026-09-22 01:05:09")).toBeInTheDocument();

    expect(screen.getByText("MYR")).toBeInTheDocument();
    expect(screen.getByText("MYR 1,234,567.12345678")).toBeInTheDocument();
    expect(screen.getByText("7")).toBeInTheDocument();
  });

  it("says when optional fields are empty", async () => {
    api.getCustomer.mockResolvedValue(customer({ contact_name: null, phone: null }));
    renderDetail();

    expect(await screen.findAllByText("Not set")).toHaveLength(2);
  });

  it("says the customer does not exist on a 404, not a generic failure", async () => {
    api.getCustomer.mockRejectedValue(
      new ApiError("CUSTOMER_NOT_FOUND", "Customer not found.", "req-404"),
    );
    renderDetail();

    expect(await screen.findByText("Customer not found")).toBeInTheDocument();
    expect(screen.queryByText("The customer could not be loaded.")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
    // 明确的答复不重试。
    expect(api.getCustomer).toHaveBeenCalledTimes(1);
  });

  it("says the caller lacks permission on a 403", async () => {
    api.getCustomer.mockRejectedValue(
      new ApiError("ADMIN_REQUIRED", "Administrator access is required.", "req-403"),
    );
    renderDetail();

    expect(
      await screen.findByText("You do not have permission to manage customers."),
    ).toBeInTheDocument();
    expect(screen.getByText("req-403")).toBeInTheDocument();
  });

  it("shows the backend message and request id for any other failure", async () => {
    api.getCustomer.mockRejectedValue(
      new ApiError("INTERNAL_ERROR", "An unexpected error occurred.", "req-500"),
    );
    renderDetail();

    expect(await screen.findByText("The customer could not be loaded.")).toBeInTheDocument();
    expect(screen.getByText(/An unexpected error occurred\./)).toBeInTheDocument();
    expect(screen.getByText("req-500")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("patches only the fields that changed and sends null for a cleared phone", async () => {
    const user = userEvent.setup();
    api.getCustomer.mockResolvedValue(customer());
    api.updateCustomer.mockResolvedValue(customer({ company_name: "Acme Holdings", phone: null }));
    renderDetail();

    await user.click(await screen.findByRole("button", { name: "Edit" }));
    const companyName = screen.getByLabelText("Company name");
    await user.clear(companyName);
    await user.type(companyName, "Acme Holdings");
    await user.clear(screen.getByLabelText("Phone"));
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => {
      expect(api.updateCustomer).toHaveBeenCalledTimes(1);
    });
    // 没动过的 email 与联系人不在请求体里。
    expect(api.updateCustomer).toHaveBeenCalledWith(CUSTOMER_ID, {
      company_name: "Acme Holdings",
      phone: null,
    });
  });

  it("refreshes the page from the PATCH response", async () => {
    const user = userEvent.setup();
    api.getCustomer.mockResolvedValue(customer());
    api.updateCustomer.mockResolvedValue(
      customer({ company_name: "Acme Holdings", updated_at: "2026-09-22T00:00:00" }),
    );
    renderDetail();

    await user.click(await screen.findByRole("button", { name: "Edit" }));
    const companyName = screen.getByLabelText("Company name");
    await user.clear(companyName);
    await user.type(companyName, "Acme Holdings");
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(await screen.findByText("2026-09-22 08:00:00")).toBeInTheDocument();
    expect(screen.getAllByText("Acme Holdings").length).toBeGreaterThan(0);
    expect(screen.queryByText("Acme Sdn Bhd")).not.toBeInTheDocument();
    // 用的是响应本身，没有再读一次。
    expect(api.getCustomer).toHaveBeenCalledTimes(1);
  });

  it("does not send a request when nothing changed", async () => {
    const user = userEvent.setup();
    api.getCustomer.mockResolvedValue(customer());
    renderDetail();

    await user.click(await screen.findByRole("button", { name: "Edit" }));
    // 改了又改回去也算没改：比的是值，不是「碰没碰过」。只差首尾空白的情况由
    // CustomerForm.test.tsx 里 `customerPatch` 的用例覆盖。
    const phone = screen.getByLabelText("Phone");
    await user.clear(phone);
    await user.type(phone, "+60 3-0000 0000");
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(
      await screen.findByText("Nothing has changed, so nothing was saved."),
    ).toBeInTheDocument();
    expect(api.updateCustomer).not.toHaveBeenCalled();
  });

  it("shows the backend message when the edit is rejected", async () => {
    const user = userEvent.setup();
    api.getCustomer.mockResolvedValue(customer());
    api.updateCustomer.mockRejectedValue(
      new ApiError("VALIDATION_ERROR", "Invalid request: body.email", "req-422"),
    );
    renderDetail();

    await user.click(await screen.findByRole("button", { name: "Edit" }));
    const email = screen.getByLabelText("Email");
    await user.clear(email);
    await user.type(email, "billing@example.com");
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(await screen.findByText("The changes could not be saved.")).toBeInTheDocument();
    expect(screen.getByText(/Invalid request: body\.email/)).toBeInTheDocument();
    expect(screen.getByText("req-422")).toBeInTheDocument();
    expect(api.updateCustomer).toHaveBeenCalledWith(CUSTOMER_ID, {
      email: "billing@example.com",
    });
  });
});
