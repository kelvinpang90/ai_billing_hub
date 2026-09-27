/**
 * 客户详情：全部字段与钱包、404 单独成一页、编辑只发改了的字段。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import { ApiError } from "../../api/client";
import { ROUTES } from "../../routes/paths";
import { CustomerDetailPage } from "./CustomerDetailPage";

const api = vi.hoisted(() => ({
  getCustomer: vi.fn(),
  updateCustomer: vi.fn(),
}));

vi.mock("../../api/adminCustomers", async () => {
  const actual = await vi.importActual<typeof import("../../api/adminCustomers")>(
    "../../api/adminCustomers",
  );
  return { ...actual, ...api };
});

// secret-scan 会把随手编的高熵 uuid 当成密钥，测试里一律用全零占位值。
const CUSTOMER_ID = "00000000-0000-4000-8000-000000000000";

const CUSTOMER = {
  id: CUSTOMER_ID,
  company_name: "Acme Sdn Bhd",
  contact_name: "Contact Person",
  email: "ops@example.com",
  phone: "+60 3-0000 0000",
  billing_status: "ACTIVE",
  status_version: 3,
  created_at: "2026-09-20T08:30:00",
  updated_at: "2026-09-21T20:15:00",
  wallet: { currency: "MYR", balance: "1234567.12345678", version: 7 },
};

function renderDetail() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, retryDelay: 0 } },
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

async function openEditor(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: "Edit" }));
  return screen.getByLabelText("Company name");
}

function saveButton() {
  return screen.getByRole("button", { name: /Save changes/ });
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("CustomerDetailPage", () => {
  it("shows every field of the customer and its wallet", async () => {
    api.getCustomer.mockResolvedValue(CUSTOMER);

    renderDetail();

    expect(await screen.findByText(CUSTOMER_ID)).toBeInTheDocument();
    expect(api.getCustomer).toHaveBeenCalledWith(CUSTOMER_ID, expect.anything());
    expect(screen.getAllByText("Acme Sdn Bhd").length).toBeGreaterThan(0);
    expect(screen.getByText("Contact Person")).toBeInTheDocument();
    expect(screen.getByText("ops@example.com")).toBeInTheDocument();
    expect(screen.getByText("+60 3-0000 0000")).toBeInTheDocument();
    expect(screen.getByText("Active")).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.getByText("2026-09-20 16:30:00")).toBeInTheDocument();
    expect(screen.getByText("2026-09-22 04:15:00")).toBeInTheDocument();
    // 钱包：币种、余额（十进制字符串原样展示，不经浮点）、版本。
    expect(screen.getByText("MYR")).toBeInTheDocument();
    expect(screen.getByText("MYR 1,234,567.12345678")).toBeInTheDocument();
    expect(screen.getByText("7")).toBeInTheDocument();
  });

  it("says the customer does not exist on 404 instead of showing a generic error", async () => {
    api.getCustomer.mockRejectedValue(
      new ApiError("CUSTOMER_NOT_FOUND", "Customer not found.", "req-404"),
    );

    renderDetail();

    expect(await screen.findByText("Customer not found")).toBeInTheDocument();
    expect(screen.queryByText("The customer could not be loaded.")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
    // 404 是确定的答案，不重试。
    expect(api.getCustomer).toHaveBeenCalledTimes(1);
  });

  it("sends only the field that changed and refreshes from the response", async () => {
    const user = userEvent.setup();
    api.getCustomer.mockResolvedValue(CUSTOMER);
    api.updateCustomer.mockResolvedValue({
      ...CUSTOMER,
      company_name: "Acme Holdings Sdn Bhd",
      updated_at: "2026-09-22T01:00:00",
    });
    renderDetail();

    const companyName = await openEditor(user);
    await user.clear(companyName);
    await user.type(companyName, "Acme Holdings Sdn Bhd");
    await user.click(saveButton());

    await waitFor(() => {
      expect(api.updateCustomer).toHaveBeenCalledWith(CUSTOMER_ID, {
        company_name: "Acme Holdings Sdn Bhd",
      });
    });
    expect(await screen.findByText("The customer has been updated.")).toBeInTheDocument();
    expect(screen.getAllByText("Acme Holdings Sdn Bhd").length).toBeGreaterThan(0);
    expect(screen.getByText("2026-09-22 09:00:00")).toBeInTheDocument();
    // 用的是 PATCH 的响应，没有再读一次。
    expect(api.getCustomer).toHaveBeenCalledTimes(1);
  });

  it("sends null when the contact name is cleared", async () => {
    const user = userEvent.setup();
    api.getCustomer.mockResolvedValue(CUSTOMER);
    api.updateCustomer.mockResolvedValue({ ...CUSTOMER, contact_name: null });
    renderDetail();

    await openEditor(user);
    await user.clear(screen.getByLabelText("Contact name"));
    await user.click(saveButton());

    await waitFor(() => {
      expect(api.updateCustomer).toHaveBeenCalledWith(CUSTOMER_ID, { contact_name: null });
    });
  });

  it("does not send anything when nothing changed", async () => {
    const user = userEvent.setup();
    api.getCustomer.mockResolvedValue(CUSTOMER);
    renderDetail();

    await openEditor(user);
    await user.click(saveButton());

    expect(await screen.findByText("Nothing was changed, so nothing was saved.")).toBeInTheDocument();
    expect(api.updateCustomer).not.toHaveBeenCalled();
  });

  it("shows the backend message when the edit is rejected", async () => {
    const user = userEvent.setup();
    api.getCustomer.mockResolvedValue(CUSTOMER);
    api.updateCustomer.mockRejectedValue(
      new ApiError("VALIDATION_ERROR", "Invalid request: body.email", "req-422"),
    );
    renderDetail();

    await openEditor(user);
    const email = screen.getByLabelText("Contact email");
    await user.clear(email);
    await user.type(email, "billing@example.com");
    await user.click(saveButton());

    expect(await screen.findByText("The changes could not be saved.")).toBeInTheDocument();
    expect(screen.getByText(/Invalid request: body\.email/)).toBeInTheDocument();
    expect(screen.getByText("req-422")).toBeInTheDocument();
    // 编辑框还在，改过的值没丢。
    expect(screen.getByLabelText("Contact email")).toHaveValue("billing@example.com");
  });
});
