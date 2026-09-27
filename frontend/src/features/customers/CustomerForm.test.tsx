/**
 * 建客户：前端校验、提交中禁用（接口不幂等）、成功跳转、422 显示后端的话。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import type { CustomerDetail } from "../../api/adminCustomers";
import { ApiError } from "../../api/client";
import { CustomerCreatePage, customerPatch, toCreateBody } from "./CustomerForm";

const CUSTOMER_ID = "00000000-0000-4000-8000-000000000000";

const navigate = vi.fn();

vi.mock("react-router", async () => {
  const actual = await vi.importActual<typeof import("react-router")>("react-router");
  return { ...actual, useNavigate: () => navigate };
});

const api = vi.hoisted(() => ({
  createCustomer: vi.fn(),
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
    contact_name: null,
    email: "ops@example.com",
    phone: null,
    billing_status: "SUSPENDED",
    status_version: 0,
    created_at: "2026-09-20T08:30:00",
    updated_at: "2026-09-20T08:30:00",
    wallet: { currency: "MYR", balance: "0.00000000", version: 0 },
    ...overrides,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

function renderCreate() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: 0 } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <CustomerCreatePage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function fillRequired(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText("Company name"), "  Acme Sdn Bhd  ");
  await user.type(screen.getByLabelText("Email"), "ops@example.com");
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("CustomerCreatePage", () => {
  it("asks for the required fields before sending anything", async () => {
    const user = userEvent.setup();
    renderCreate();

    await user.click(screen.getByRole("button", { name: "Create customer" }));

    expect(await screen.findByText("Enter the company name.")).toBeInTheDocument();
    expect(screen.getByText("Enter an email address.")).toBeInTheDocument();
    expect(api.createCustomer).not.toHaveBeenCalled();
  });

  it("applies the documented field rules", async () => {
    const user = userEvent.setup();
    renderCreate();

    // 只有空白的公司名去掉首尾空白后是空串，后端会 422。
    await user.type(screen.getByLabelText("Company name"), "   ");
    await user.type(screen.getByLabelText("Email"), "not-an-email");
    await user.type(screen.getByLabelText("Phone"), "1".repeat(33));
    await user.click(screen.getByRole("button", { name: "Create customer" }));

    expect(await screen.findByText("Enter the company name.")).toBeInTheDocument();
    expect(screen.getByText("Enter a valid email address.")).toBeInTheDocument();
    expect(screen.getByText("The phone number can be at most 32 characters.")).toBeInTheDocument();
    expect(api.createCustomer).not.toHaveBeenCalled();
  });

  it("counts length in characters the way the backend does", async () => {
    const user = userEvent.setup();
    api.createCustomer.mockResolvedValue(customer());
    renderCreate();

    // 32 个 emoji 在 JS 里 length 是 64，后端（Python）数的是 32 —— 合法。
    await fillRequired(user);
    await user.click(screen.getByLabelText("Phone"));
    await user.paste("😀".repeat(32));
    await user.click(screen.getByRole("button", { name: "Create customer" }));

    await waitFor(() => {
      expect(api.createCustomer).toHaveBeenCalledTimes(1);
    });
  });

  it("sends trimmed values, leaves out empty optional fields and opens the new customer", async () => {
    const user = userEvent.setup();
    api.createCustomer.mockResolvedValue(customer());
    renderCreate();

    await fillRequired(user);
    await user.type(screen.getByLabelText("Contact name"), "   ");
    await user.click(screen.getByRole("button", { name: "Create customer" }));

    await waitFor(() => {
      expect(navigate).toHaveBeenCalledWith(`/customers/${CUSTOMER_ID}`, { replace: true });
    });
    expect(api.createCustomer).toHaveBeenCalledWith({
      company_name: "Acme Sdn Bhd",
      email: "ops@example.com",
    });
  });

  it("disables the submit button while the request is in flight and sends it only once", async () => {
    // antd 的 loading 按钮带 `pointer-events: none`；这里要的正是「点了也没用」。
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    const gate = deferred<CustomerDetail>();
    api.createCustomer.mockReturnValue(gate.promise);
    renderCreate();

    await fillRequired(user);
    const submit = screen.getByRole("button", { name: "Create customer" });
    // ⚠️ 双击：两次点击都可能在按钮变灰之前通过 antd 的异步校验。接口不幂等，
    // 漏一次就是两个客户、两个钱包。
    await user.dblClick(submit);

    await waitFor(() => {
      expect(submit).toBeDisabled();
    });
    await user.click(submit);
    expect(api.createCustomer).toHaveBeenCalledTimes(1);

    gate.resolve(customer());
    await waitFor(() => {
      expect(navigate).toHaveBeenCalledWith(`/customers/${CUSTOMER_ID}`, { replace: true });
    });
    expect(api.createCustomer).toHaveBeenCalledTimes(1);
  });

  it("shows the backend message and request id on a 422 and lets the user try again", async () => {
    const user = userEvent.setup();
    api.createCustomer.mockRejectedValue(
      new ApiError("VALIDATION_ERROR", "Invalid request: body.email", "req-422"),
    );
    renderCreate();

    await fillRequired(user);
    await user.click(screen.getByRole("button", { name: "Create customer" }));

    expect(await screen.findByText(/Invalid request: body\.email/)).toBeInTheDocument();
    expect(screen.getByText("req-422")).toBeInTheDocument();
    expect(navigate).not.toHaveBeenCalled();
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Create customer" })).toBeEnabled();
    });
  });

  it("says plainly when the caller is not an admin", async () => {
    const user = userEvent.setup();
    api.createCustomer.mockRejectedValue(
      new ApiError("ADMIN_REQUIRED", "Administrator access is required.", "req-403"),
    );
    renderCreate();

    await fillRequired(user);
    await user.click(screen.getByRole("button", { name: "Create customer" }));

    expect(
      await screen.findByText("You do not have permission to manage customers."),
    ).toBeInTheDocument();
    expect(screen.getByText("req-403")).toBeInTheDocument();
  });
});

describe("request bodies", () => {
  const values = {
    company_name: "Acme Sdn Bhd",
    email: "ops@example.com",
    contact_name: "",
    phone: "",
  };

  it("keeps optional fields that were filled in", () => {
    expect(toCreateBody({ ...values, contact_name: " Jo ", phone: "+60 3" })).toEqual({
      company_name: "Acme Sdn Bhd",
      email: "ops@example.com",
      contact_name: "Jo",
      phone: "+60 3",
    });
  });

  it("builds an empty patch when nothing changed, whitespace included", () => {
    const original = customer({ contact_name: "Jo", phone: null });
    const edited = { ...values, company_name: " Acme Sdn Bhd ", contact_name: "Jo" };
    expect(customerPatch(original, edited)).toEqual({});
  });

  it("clears optional fields with null", () => {
    const original = customer({ contact_name: "Jo", phone: "+60 3" });
    expect(customerPatch(original, values)).toEqual({ contact_name: null, phone: null });
  });
});
