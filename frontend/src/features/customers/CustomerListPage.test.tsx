/**
 * 客户列表：加载中、失败、空列表三种状态（spec §132 DoD 第 8 条），行内容与分页。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import type { CustomerSummary, Page } from "../../api/adminCustomers";
import { ApiError } from "../../api/client";
import { CustomerListPage } from "./CustomerListPage";

const FIRST_ID = "00000000-0000-4000-8000-000000000000";
const SECOND_ID = "00000000-0000-4000-8000-000000000001";

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
    id: FIRST_ID,
    company_name: "Acme Sdn Bhd",
    contact_name: null,
    email: "ops@example.com",
    phone: null,
    billing_status: "SUSPENDED",
    status_version: 0,
    created_at: "2026-09-20T08:30:00",
    updated_at: "2026-09-20T08:30:00",
    ...overrides,
  };
}

function page(items: CustomerSummary[], total: number, pageNumber = 1): Page<CustomerSummary> {
  return { items, page: pageNumber, page_size: 20, total };
}

function renderList(entry = "/customers") {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, retryDelay: 0 } },
  });
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

  it("shows the backend message and request id when the list cannot be loaded", async () => {
    api.listCustomers.mockRejectedValue(
      new ApiError("INTERNAL_ERROR", "An unexpected error occurred.", "req-500"),
    );
    renderList();

    expect(await screen.findByText("The customers could not be loaded.")).toBeInTheDocument();
    expect(screen.getByText(/An unexpected error occurred\./)).toBeInTheDocument();
    expect(screen.getByText("req-500")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("says the caller lacks permission on a 403", async () => {
    api.listCustomers.mockRejectedValue(
      new ApiError("ADMIN_REQUIRED", "Administrator access is required.", "req-403"),
    );
    renderList();

    expect(
      await screen.findByText("You do not have permission to manage customers."),
    ).toBeInTheDocument();
    expect(screen.getByText("req-403")).toBeInTheDocument();
  });

  it("says so when there are no customers yet", async () => {
    api.listCustomers.mockResolvedValue(page([], 0));
    renderList();

    expect(await screen.findByText("No customers yet.")).toBeInTheDocument();
  });

  it("lists customers in the backend's order with a way into each one", async () => {
    api.listCustomers.mockResolvedValue(
      page(
        [
          summary({ id: SECOND_ID, company_name: "Newer Co", billing_status: "ACTIVE" }),
          summary({ id: FIRST_ID, company_name: "Acme Sdn Bhd", email: "acme@example.com" }),
        ],
        2,
      ),
    );
    renderList();

    const newer = await screen.findByRole("link", { name: "Newer Co" });
    const older = screen.getByRole("link", { name: "Acme Sdn Bhd" });
    expect(newer).toHaveAttribute("href", `/customers/${SECOND_ID}`);
    expect(older).toHaveAttribute("href", `/customers/${FIRST_ID}`);
    // 顺序沿用后端（最新在前），前端不重排。
    expect(newer.compareDocumentPosition(older) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    expect(screen.getByText("acme@example.com")).toBeInTheDocument();
    expect(screen.getByText("Active")).toBeInTheDocument();
    expect(screen.getByText("Suspended")).toBeInTheDocument();
    expect(screen.getAllByText("2026-09-20 16:30:00")).toHaveLength(2);
  });

  it("asks for the first page with the default page size", async () => {
    api.listCustomers.mockResolvedValue(page([summary()], 1));
    renderList();

    await screen.findByRole("link", { name: "Acme Sdn Bhd" });
    expect(api.listCustomers).toHaveBeenCalledWith(1, 20, expect.anything());
  });

  it("asks the backend for the next page when the user pages on", async () => {
    const user = userEvent.setup();
    api.listCustomers.mockImplementation((pageNumber: number) =>
      Promise.resolve(
        page(
          [summary({ company_name: pageNumber === 1 ? "Page One Co" : "Page Two Co" })],
          45,
          pageNumber,
        ),
      ),
    );
    renderList();

    await screen.findByRole("link", { name: "Page One Co" });
    await user.click(screen.getByTitle("2"));

    expect(await screen.findByRole("link", { name: "Page Two Co" })).toBeInTheDocument();
    expect(api.listCustomers).toHaveBeenLastCalledWith(2, 20, expect.anything());
  });

  it("takes the page from the address bar", async () => {
    api.listCustomers.mockResolvedValue(page([summary()], 101, 3));
    renderList("/customers?page=3&page_size=50");

    await waitFor(() => {
      expect(api.listCustomers).toHaveBeenCalledWith(3, 50, expect.anything());
    });
  });

  it("falls back to the defaults when the address bar is out of range", async () => {
    api.listCustomers.mockResolvedValue(page([summary()], 1));
    // 越界的值照发只会换来一个 422；page_size 的上限是 100。
    renderList("/customers?page=abc&page_size=1000");

    await waitFor(() => {
      expect(api.listCustomers).toHaveBeenCalledWith(1, 20, expect.anything());
    });
  });
});
