/**
 * 客户列表的三种状态（spec §132 DoD 第 8 条）与分页（spec §108）。
 *
 * 失败那一支必须带上后端的 message 与 request_id —— 那是用户能报给支持、支持能在
 * 服务端日志里搜到的唯一钥匙。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import type { CustomerSummary, Page } from "../../api/adminCustomers";
import { ApiError } from "../../api/client";
import { CustomerListPage } from "./CustomerListPage";

const CUSTOMER_ID = "00000000-0000-4000-8000-000000000000";

const api = vi.hoisted(() => ({
  listCustomers: vi.fn(),
}));

vi.mock("../../api/adminCustomers", async () => {
  const actual =
    await vi.importActual<typeof import("../../api/adminCustomers")>("../../api/adminCustomers");
  return { ...actual, ...api };
});

function summary(overrides: Partial<CustomerSummary> = {}): CustomerSummary {
  return {
    id: CUSTOMER_ID,
    company_name: "Acme Sdn Bhd",
    contact_name: null,
    email: "ops@example.com",
    phone: null,
    billing_status: "SUSPENDED",
    billing_mode: "PREPAID",
    ai_service_enabled: false,
    status_version: 0,
    created_at: "2026-09-20T08:30:00",
    updated_at: "2026-09-20T08:30:00",
    ...overrides,
  };
}

function page(items: CustomerSummary[], total: number, number = 1): Page<CustomerSummary> {
  return { items, page: number, page_size: 20, total };
}

function renderList(entry = "/customers") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[entry]}>
        <CustomerListPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("CustomerListPage", () => {
  it("shows a loading state while the first page is on its way", () => {
    api.listCustomers.mockReturnValue(new Promise(() => undefined));

    renderList();

    expect(screen.getByText("Loading customers…")).toBeInTheDocument();
  });

  it("shows the backend message and the request id when the list fails", async () => {
    api.listCustomers.mockRejectedValue(
      new ApiError("INTERNAL_ERROR", "An unexpected error occurred.", "req-500"),
    );

    renderList();

    expect(await screen.findByText("The customer list could not be loaded.")).toBeInTheDocument();
    expect(screen.getByText(/An unexpected error occurred\./)).toBeInTheDocument();
    expect(screen.getByText("req-500")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("says there is no permission on a 403 instead of a generic failure", async () => {
    api.listCustomers.mockRejectedValue(
      new ApiError("ADMIN_REQUIRED", "Administrator access is required.", "req-403"),
    );

    renderList();

    expect(
      await screen.findByText("Only administrators can manage customers."),
    ).toBeInTheDocument();
    expect(screen.queryByText("The customer list could not be loaded.")).not.toBeInTheDocument();
  });

  it("says so when there are no customers at all", async () => {
    api.listCustomers.mockResolvedValue(page([], 0));

    renderList();

    expect(await screen.findByText("No customers yet.")).toBeInTheDocument();
    // 空列表时仍然要能建第一个客户。
    expect(screen.getByRole("link", { name: "New customer" })).toHaveAttribute(
      "href",
      "/customers/new",
    );
  });

  it("lists customers in the backend's order with a link into each one", async () => {
    api.listCustomers.mockResolvedValue(
      page(
        [
          summary({ company_name: "Newest Sdn Bhd", billing_status: "ACTIVE" }),
          summary({
            id: "00000000-0000-4000-8000-000000000001",
            company_name: "Older Sdn Bhd",
            email: "older@example.com",
            created_at: "2026-09-19T20:00:00",
          }),
        ],
        2,
      ),
    );

    renderList();

    const newest = await screen.findByRole("link", { name: "Newest Sdn Bhd" });
    expect(newest).toHaveAttribute("href", `/customers/${CUSTOMER_ID}`);
    const rows = screen.getAllByRole("row").slice(1);
    expect(rows[0]).toHaveTextContent("Newest Sdn Bhd");
    expect(rows[0]).toHaveTextContent("Active");
    expect(rows[1]).toHaveTextContent("Older Sdn Bhd");
    expect(rows[1]).toHaveTextContent("older@example.com");
    expect(rows[1]).toHaveTextContent("Suspended");
    // 创建时间按吉隆坡时间显示：UTC 20:00 已经是第二天。
    expect(rows[1]).toHaveTextContent("2026-09-20 04:00:00");
    expect(api.listCustomers).toHaveBeenCalledWith(1, 20, expect.anything());
  });

  it("asks the backend for the page the user moves to", async () => {
    const user = userEvent.setup();
    api.listCustomers.mockImplementation((requested: number) =>
      Promise.resolve(
        page([summary({ company_name: `Customer on page ${String(requested)}` })], 45, requested),
      ),
    );

    renderList();

    expect(await screen.findByText("45 customers")).toBeInTheDocument();
    await user.click(screen.getByTitle("2"));

    expect(await screen.findByText("Customer on page 2")).toBeInTheDocument();
    expect(api.listCustomers).toHaveBeenLastCalledWith(2, 20, expect.anything());
  });

  it("starts on the page named in the address bar", async () => {
    api.listCustomers.mockResolvedValue(page([summary()], 45, 3));

    renderList("/customers?page=3&page_size=10");

    await screen.findByText("Acme Sdn Bhd");
    expect(api.listCustomers).toHaveBeenCalledWith(3, 10, expect.anything());
  });

  it("falls back to the defaults when the address bar asks for more than the backend allows", async () => {
    api.listCustomers.mockResolvedValue(page([summary()], 1));

    renderList("/customers?page=0&page_size=500");

    await screen.findByText("Acme Sdn Bhd");
    expect(api.listCustomers).toHaveBeenCalledWith(1, 20, expect.anything());
  });

  it("explains an empty page past the end instead of claiming there are no customers", async () => {
    api.listCustomers.mockResolvedValue(page([], 45, 9));

    renderList("/customers?page=9");

    expect(await screen.findByText("There are no customers on this page.")).toBeInTheDocument();
    expect(screen.queryByText("No customers yet.")).not.toBeInTheDocument();
  });
});
