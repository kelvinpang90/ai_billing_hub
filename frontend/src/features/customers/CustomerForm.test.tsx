/**
 * 建客户：字段校验、提交中禁用（接口不幂等）、成功后进入详情、422 显示后端 message。
 * 以及编辑时「只发改了的字段」那个纯函数。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import { ApiError } from "../../api/client";
import { CustomerCreatePage, toCreateBody, toUpdatePatch } from "./CustomerForm";

const navigate = vi.fn();

vi.mock("react-router", async () => {
  const actual = await vi.importActual<typeof import("react-router")>("react-router");
  return { ...actual, useNavigate: () => navigate };
});

const api = vi.hoisted(() => ({
  createCustomer: vi.fn(),
}));

vi.mock("../../api/adminCustomers", async () => {
  const actual = await vi.importActual<typeof import("../../api/adminCustomers")>(
    "../../api/adminCustomers",
  );
  return { ...actual, ...api };
});

// secret-scan 会把随手编的高熵 uuid 当成密钥，测试里一律用全零占位值。
const CUSTOMER_ID = "00000000-0000-4000-8000-000000000000";

const CREATED = {
  id: CUSTOMER_ID,
  company_name: "Acme Sdn Bhd",
  contact_name: null,
  email: "ops@example.com",
  phone: null,
  billing_status: "SUSPENDED" as const,
  status_version: 0,
  created_at: "2026-09-20T08:30:00",
  updated_at: "2026-09-20T08:30:00",
  wallet: { currency: "MYR", balance: "0.00000000", version: 0 },
};

function renderCreate() {
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
  // antd 在 loading 时往按钮里塞一个带 aria-label 的图标，名字会多出前缀。
  return screen.getByRole("button", { name: /Create customer/ });
}

async function fillRequired(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText("Company name"), "Acme Sdn Bhd");
  await user.type(screen.getByLabelText("Contact email"), "ops@example.com");
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("CustomerCreatePage", () => {
  it("requires a company name and an email before sending anything", async () => {
    const user = userEvent.setup();
    renderCreate();

    await user.click(submitButton());

    expect(await screen.findByText("Enter the company name.")).toBeInTheDocument();
    expect(screen.getByText("Enter the contact email.")).toBeInTheDocument();
    // antd 默认的「Please enter …」是不走 i18n 的英文。
    expect(screen.queryByText(/Please enter/)).not.toBeInTheDocument();
    expect(api.createCustomer).not.toHaveBeenCalled();
  });

  it("treats a company name of only spaces as missing", async () => {
    const user = userEvent.setup();
    renderCreate();

    await user.type(screen.getByLabelText("Company name"), "   ");
    await user.type(screen.getByLabelText("Contact email"), "ops@example.com");
    await user.click(submitButton());

    expect(await screen.findByText("Enter the company name.")).toBeInTheDocument();
    expect(api.createCustomer).not.toHaveBeenCalled();
  });

  it("rejects an invalid email and over-long fields", async () => {
    const user = userEvent.setup();
    renderCreate();

    await user.click(screen.getByLabelText("Company name"));
    await user.paste("a".repeat(256));
    await user.type(screen.getByLabelText("Contact email"), "not-an-email");
    await user.click(screen.getByLabelText("Contact name"));
    await user.paste("b".repeat(256));
    await user.click(screen.getByLabelText("Phone"));
    await user.paste("1".repeat(33));
    await user.click(submitButton());

    expect(
      await screen.findByText("The company name can be at most 255 characters."),
    ).toBeInTheDocument();
    expect(screen.getByText("Enter a valid email address.")).toBeInTheDocument();
    expect(screen.getByText("The contact name can be at most 255 characters.")).toBeInTheDocument();
    expect(screen.getByText("The phone number can be at most 32 characters.")).toBeInTheDocument();
    expect(api.createCustomer).not.toHaveBeenCalled();
  });

  it("accepts a company name of exactly 255 characters", async () => {
    const user = userEvent.setup();
    api.createCustomer.mockResolvedValue(CREATED);
    renderCreate();

    await user.click(screen.getByLabelText("Company name"));
    await user.paste("a".repeat(255));
    await user.type(screen.getByLabelText("Contact email"), "ops@example.com");
    await user.click(submitButton());

    await waitFor(() => {
      expect(api.createCustomer).toHaveBeenCalledTimes(1);
    });
  });

  it("sends the trimmed fields and opens the new customer", async () => {
    const user = userEvent.setup();
    api.createCustomer.mockResolvedValue(CREATED);
    renderCreate();

    await user.type(screen.getByLabelText("Company name"), "  Acme Sdn Bhd  ");
    await user.type(screen.getByLabelText("Contact email"), "ops@example.com");
    await user.type(screen.getByLabelText("Contact name"), "Contact Person");
    await user.click(submitButton());

    await waitFor(() => {
      expect(navigate).toHaveBeenCalledWith(`/customers/${CUSTOMER_ID}`);
    });
    // 没填的电话不带：后端把缺省存成 null。
    expect(api.createCustomer).toHaveBeenCalledWith({
      company_name: "Acme Sdn Bhd",
      email: "ops@example.com",
      contact_name: "Contact Person",
    });
  });

  it("disables the submit button while the request is in flight", async () => {
    const user = userEvent.setup();
    let finish: (value: typeof CREATED) => void = () => undefined;
    api.createCustomer.mockReturnValue(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    renderCreate();

    await fillRequired(user);
    await user.click(submitButton());

    // ⚠️ 接口不幂等：双击就是两个客户、两个钱包。
    await waitFor(() => {
      expect(submitButton()).toBeDisabled();
    });
    await user.click(submitButton());
    expect(api.createCustomer).toHaveBeenCalledTimes(1);

    finish(CREATED);
    await waitFor(() => {
      expect(navigate).toHaveBeenCalledWith(`/customers/${CUSTOMER_ID}`);
    });
  });

  it("shows the backend message on 422 and lets the admin try again", async () => {
    const user = userEvent.setup();
    api.createCustomer.mockRejectedValue(
      new ApiError("VALIDATION_ERROR", "Invalid request: body.email", "req-422"),
    );
    renderCreate();

    await fillRequired(user);
    await user.click(submitButton());

    expect(await screen.findByText("The customer could not be created.")).toBeInTheDocument();
    expect(screen.getByText(/Invalid request: body\.email/)).toBeInTheDocument();
    expect(screen.getByText("req-422")).toBeInTheDocument();
    expect(submitButton()).toBeEnabled();
    expect(navigate).not.toHaveBeenCalled();
  });
});

describe("toCreateBody", () => {
  it("leaves out optional fields that are blank", () => {
    expect(
      toCreateBody({ company_name: " Acme ", email: " ops@example.com ", contact_name: "  ", phone: "" }),
    ).toEqual({ company_name: "Acme", email: "ops@example.com" });
  });
});

describe("toUpdatePatch", () => {
  const current = {
    ...CREATED,
    contact_name: "Contact Person",
    phone: "+60 3-0000 0000",
  };
  const unchanged = {
    company_name: "Acme Sdn Bhd",
    email: "ops@example.com",
    contact_name: "Contact Person",
    phone: "+60 3-0000 0000",
  };

  it("is empty when nothing changed", () => {
    expect(toUpdatePatch(current, unchanged)).toEqual({});
  });

  it("ignores whitespace the backend would strip anyway", () => {
    expect(toUpdatePatch(current, { ...unchanged, company_name: " Acme Sdn Bhd " })).toEqual({});
  });

  it("carries only the fields that changed", () => {
    expect(toUpdatePatch(current, { ...unchanged, email: "billing@example.com" })).toEqual({
      email: "billing@example.com",
    });
  });

  it("sends null when an optional field is cleared", () => {
    expect(toUpdatePatch(current, { ...unchanged, contact_name: "", phone: "  " })).toEqual({
      contact_name: null,
      phone: null,
    });
  });
});
