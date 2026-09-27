/**
 * 建客户：校验、提交中禁用、成功跳转；以及编辑用的 PATCH 怎么算出来。
 *
 * ⚠️ 建客户**不幂等**（docs/api.md）：双击就是两个客户、两个钱包。所以「提交进行中
 * 不能再提交」单独测，而且用双击测 —— 只断言按钮变灰的话，抓不到两次 onFinish
 * 都赶在按钮变灰之前的那种竞态。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import type { CustomerDetail } from "../../api/adminCustomers";
import { ApiError } from "../../api/client";
import { CustomerCreatePage, toCreateBody, toPatch } from "./CustomerForm";

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
    contact_name: "Contact Person",
    email: "ops@example.com",
    phone: "+60 3-0000 0000",
    billing_status: "SUSPENDED",
    status_version: 0,
    created_at: "2026-09-20T08:30:00",
    updated_at: "2026-09-20T08:30:00",
    wallet: { currency: "MYR", balance: "0.00000000", version: 0 },
    ...overrides,
  };
}

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

function renderCreatePage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <CustomerCreatePage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function submitButton() {
  // 提交中按钮里多一个 loading 图标（aria-label="loading"），名字不再是精确的这一句。
  return screen.getByRole("button", { name: /Create customer/ });
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("CustomerCreatePage", () => {
  it("does not submit without a company name and a valid email", async () => {
    const user = userEvent.setup();
    renderCreatePage();

    await user.click(submitButton());

    expect(await screen.findByText("Enter the company name.")).toBeInTheDocument();
    expect(screen.getByText("Enter an email address.")).toBeInTheDocument();
    expect(api.createCustomer).not.toHaveBeenCalled();
  });

  it("treats a company name of only spaces as missing", async () => {
    const user = userEvent.setup();
    renderCreatePage();

    await user.type(screen.getByLabelText("Company name"), "   ");
    await user.type(screen.getByLabelText("Email"), "not-an-email");
    await user.click(submitButton());

    expect(await screen.findByText("Enter the company name.")).toBeInTheDocument();
    expect(screen.getByText("Enter a valid email address.")).toBeInTheDocument();
    expect(api.createCustomer).not.toHaveBeenCalled();
  });

  it("rejects values longer than the backend accepts", async () => {
    const user = userEvent.setup();
    renderCreatePage();

    await user.type(screen.getByLabelText("Company name"), "Acme");
    await user.type(screen.getByLabelText("Email"), "ops@example.com");
    await user.type(screen.getByLabelText("Phone"), "0".repeat(33));
    await user.click(submitButton());

    expect(
      await screen.findByText("The phone number can be at most 32 characters."),
    ).toBeInTheDocument();
    expect(api.createCustomer).not.toHaveBeenCalled();
  });

  it("sends trimmed values, leaves empty optional fields out, and opens the new customer", async () => {
    const user = userEvent.setup();
    api.createCustomer.mockResolvedValue(customer({ contact_name: null }));
    renderCreatePage();

    await user.type(screen.getByLabelText("Company name"), "  Acme Sdn Bhd  ");
    await user.type(screen.getByLabelText("Email"), "ops@example.com");
    await user.type(screen.getByLabelText("Phone"), "+60 3-0000 0000");
    await user.click(submitButton());

    await waitFor(() => {
      expect(navigate).toHaveBeenCalledWith(`/customers/${CUSTOMER_ID}`);
    });
    expect(api.createCustomer).toHaveBeenCalledWith({
      company_name: "Acme Sdn Bhd",
      email: "ops@example.com",
      phone: "+60 3-0000 0000",
    });
  });

  it("disables the submit button while the request is in flight and sends it once", async () => {
    const user = userEvent.setup();
    const pending = deferred<CustomerDetail>();
    api.createCustomer.mockReturnValue(pending.promise);
    renderCreatePage();

    await user.type(screen.getByLabelText("Company name"), "Acme Sdn Bhd");
    await user.type(screen.getByLabelText("Email"), "ops@example.com");
    await user.dblClick(submitButton());

    await waitFor(() => {
      expect(submitButton()).toBeDisabled();
    });
    expect(api.createCustomer).toHaveBeenCalledTimes(1);
    expect(navigate).not.toHaveBeenCalled();

    pending.resolve(customer());

    await waitFor(() => {
      expect(navigate).toHaveBeenCalledWith(`/customers/${CUSTOMER_ID}`);
    });
    expect(api.createCustomer).toHaveBeenCalledTimes(1);
  });

  it("shows the backend message and request id when the backend rejects the body", async () => {
    const user = userEvent.setup();
    api.createCustomer.mockRejectedValue(
      new ApiError("VALIDATION_ERROR", "Invalid field: body.email", "req-422"),
    );
    renderCreatePage();

    await user.type(screen.getByLabelText("Company name"), "Acme Sdn Bhd");
    await user.type(screen.getByLabelText("Email"), "ops@example.com");
    await user.click(submitButton());

    expect(await screen.findByText(/Invalid field: body\.email/)).toBeInTheDocument();
    expect(screen.getByText("req-422")).toBeInTheDocument();
    expect(screen.getByText("The customer could not be saved.")).toBeInTheDocument();
    expect(navigate).not.toHaveBeenCalled();
    // 失败之后可以改了再交。
    expect(submitButton()).toBeEnabled();
  });

  it("says plainly that only administrators may do this on a 403", async () => {
    const user = userEvent.setup();
    api.createCustomer.mockRejectedValue(
      new ApiError("ADMIN_REQUIRED", "Administrator access is required.", "req-403"),
    );
    renderCreatePage();

    await user.type(screen.getByLabelText("Company name"), "Acme Sdn Bhd");
    await user.type(screen.getByLabelText("Email"), "ops@example.com");
    await user.click(submitButton());

    expect(
      await screen.findByText("Only administrators can manage customers."),
    ).toBeInTheDocument();
    expect(screen.getByText("req-403")).toBeInTheDocument();
  });
});

describe("toCreateBody", () => {
  it("keeps optional fields that have content", () => {
    expect(
      toCreateBody({
        company_name: "Acme",
        email: " ops@example.com ",
        contact_name: " Contact Person ",
        phone: "",
      }),
    ).toEqual({ company_name: "Acme", email: "ops@example.com", contact_name: "Contact Person" });
  });
});

describe("toPatch", () => {
  const original = customer();
  const unchanged = {
    company_name: "Acme Sdn Bhd",
    email: "ops@example.com",
    contact_name: "Contact Person",
    phone: "+60 3-0000 0000",
  };

  it("is empty when nothing changed, surrounding spaces included", () => {
    expect(toPatch(original, unchanged)).toEqual({});
    expect(toPatch(original, { ...unchanged, company_name: " Acme Sdn Bhd " })).toEqual({});
  });

  it("carries only the fields that changed", () => {
    expect(toPatch(original, { ...unchanged, email: "billing@example.com" })).toEqual({
      email: "billing@example.com",
    });
  });

  it("clears contact name and phone with null", () => {
    expect(toPatch(original, { ...unchanged, contact_name: "", phone: "   " })).toEqual({
      contact_name: null,
      phone: null,
    });
  });

  it("does not send null for a field that was already empty", () => {
    const withoutPhone = customer({ phone: null });
    expect(toPatch(withoutPhone, { ...unchanged, phone: "" })).toEqual({});
  });
});
