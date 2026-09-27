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
import { beforeEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import type { CustomerDetail } from "../../api/adminCustomers";
import { ApiError } from "../../api/client";
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
});
