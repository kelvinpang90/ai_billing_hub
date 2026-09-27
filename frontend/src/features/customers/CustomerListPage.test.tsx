/**
 * 客户列表的三种状态（spec §132 DoD 第 8 条）与分页（spec §108）。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

// ⚠️ jsdom 没有 ResizeObserver，而 antd 的表格与分页靠它量尺寸。必须在 antd
// 被 import 之前补上，所以放在 `vi.hoisted` 里（它排在所有 import 之前执行）。
vi.hoisted(() => {
  if (!("ResizeObserver" in globalThis)) {
    vi.stubGlobal(
      "ResizeObserver",
      class {
        observe = (): undefined => undefined;
        unobserve = (): undefined => undefined;
        disconnect = (): undefined => undefined;
      },
    );
  }
});

import "../../i18n";
import { ApiError } from "../../api/client";
import { CustomerListPage } from "./CustomerListPage";

const api = vi.hoisted(() => ({
  listCustomers: vi.fn(),
}));

vi.mock("../../api/adminCustomers", async () => {
  const actual = await vi.importActual<typeof import("../../api/adminCustomers")>(
    "../../api/adminCustomers",
  );
  return { ...actual, ...api };
});

// secret-scan 会把随手编的高熵 uuid 当成密钥，测试里一律用全零占位值。
const CUSTOMER_ID = "00000000-0000-4000-8000-000000000000";

function customer(overrides: Record<string, unknown> = {}) {
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
    ...overrides,
  };
}

function page(items: unknown[], total: number, pageNumber = 1, pageSize = 20) {
  return { items, page: pageNumber, page_size: pageSize, total };
}

function renderList(path = "/customers") {
  const queryClient = new QueryClient({
    // retryDelay 0：页面给读请求配了「5xx 重试一次」，用例里不该为此干等一秒。
    defaultOptions: { queries: { retry: false, retryDelay: 0 } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <CustomerListPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("CustomerListPage", () => {
  it("shows that it is loading while the first page is on its way", () => {
    api.listCustomers.mockReturnValue(new Promise(() => undefined));

    renderList();

    expect(screen.getByText("Loading customers…")).toBeInTheDocument();
  });

  it("shows the backend message and the request id when the list fails", async () => {
    api.listCustomers.mockRejectedValue(
      new ApiError("INTERNAL_ERROR", "An unexpected error occurred.", "req-42"),
    );

    renderList();

    expect(await screen.findByText("The customer list could not be loaded.")).toBeInTheDocument();
    expect(screen.getByText(/An unexpected error occurred\./)).toBeInTheDocument();
    // 没有 request_id 的话，管理员能报给支持的只有「打不开」。
    expect(screen.getByText("req-42")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("says so plainly when the caller is not an admin", async () => {
    api.listCustomers.mockRejectedValue(
      new ApiError("ADMIN_REQUIRED", "Administrator access is required.", "req-403"),
    );

    renderList();

    expect(
      await screen.findByText("You do not have permission to manage customers."),
    ).toBeInTheDocument();
    expect(screen.getByText("req-403")).toBeInTheDocument();
    // 再试也还是 403。
    expect(screen.queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
  });

  it("shows an empty state when there are no customers", async () => {
    api.listCustomers.mockResolvedValue(page([], 0));

    renderList();

    expect(await screen.findByText("No customers yet.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "New customer" })).toHaveAttribute(
      "href",
      "/customers/new",
    );
  });

  it("shows each customer with a way into its detail", async () => {
    api.listCustomers.mockResolvedValue(page([customer()], 1));

    renderList();

    const link = await screen.findByRole("link", { name: "Acme Sdn Bhd" });
    expect(link).toHaveAttribute("href", `/customers/${CUSTOMER_ID}`);
    const row = link.closest("tr");
    expect(row).not.toBeNull();
    const cells = within(row as HTMLElement);
    expect(cells.getByText("ops@example.com")).toBeInTheDocument();
    expect(cells.getByText("Suspended")).toBeInTheDocument();
    // 后端的 UTC 08:30 在吉隆坡是 16:30。
    expect(cells.getByText("2026-09-20 16:30:00")).toBeInTheDocument();
  });

  it("asks for the first page of twenty by default", async () => {
    api.listCustomers.mockResolvedValue(page([customer()], 1));

    renderList();

    await screen.findByRole("link", { name: "Acme Sdn Bhd" });
    expect(api.listCustomers).toHaveBeenCalledWith(1, 20, expect.anything());
  });

  it("reads the page and the page size from the address", async () => {
    api.listCustomers.mockResolvedValue(page([customer()], 120, 3, 50));

    renderList("/customers?page=3&page_size=50");

    await screen.findByRole("link", { name: "Acme Sdn Bhd" });
    expect(api.listCustomers).toHaveBeenCalledWith(3, 50, expect.anything());
  });

  it("falls back to the defaults rather than sending an out-of-range page size", async () => {
    api.listCustomers.mockResolvedValue(page([customer()], 1));

    // 后端上限 100，超出是 422。手改坏了的地址不该让整页变成错误页。
    renderList("/customers?page=0&page_size=500");

    await screen.findByRole("link", { name: "Acme Sdn Bhd" });
    expect(api.listCustomers).toHaveBeenCalledWith(1, 20, expect.anything());
  });

  it("fetches the next page when the admin turns the page", async () => {
    const user = userEvent.setup();
    api.listCustomers.mockResolvedValue(page([customer()], 45));

    renderList();

    await screen.findByRole("link", { name: "Acme Sdn Bhd" });
    await user.click(screen.getByTitle("2"));

    await waitFor(() => {
      expect(api.listCustomers).toHaveBeenLastCalledWith(2, 20, expect.anything());
    });
  });
});
