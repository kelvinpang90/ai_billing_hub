/**
 * 审计页的三种状态（spec §132 DoD 第 8 条）、筛选与展开行。
 *
 * 失败那一支必须带上后端的 message 与 request_id；422 时筛选表单还在，用户能改。
 * 查询参数名本身由 `api/adminAudit.test.ts` 管，这里只看页面把什么条件交给了它。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import type { AuditLogEntry } from "../../api/adminAudit";
import type { Page } from "../../api/adminCustomers";
import { ApiError } from "../../api/client";
import { AuditLogPage } from "./AuditLogPage";

const ENTITY_ID = "00000000-0000-0000-0000-000000000000";

const api = vi.hoisted(() => ({
  listAuditLogs: vi.fn(),
}));

vi.mock("../../api/adminAudit", async () => {
  const actual = await vi.importActual<typeof import("../../api/adminAudit")>("../../api/adminAudit");
  return { ...actual, ...api };
});

function entry(overrides: Partial<AuditLogEntry> = {}): AuditLogEntry {
  return {
    created_at: "2026-09-20T08:30:00",
    action: "CUSTOMER_CREATE",
    actor_role: "ADMIN",
    actor_email: "admin@example.com",
    entity_type: "tenant",
    entity_id: ENTITY_ID,
    entity_user_email: null,
    ip_address: "203.0.113.7",
    user_agent: "Mozilla/5.0 (test)",
    reason: null,
    before_state: null,
    after_state: { public_id: ENTITY_ID, company_name: "Acme Sdn Bhd" },
    ...overrides,
  };
}

function page(items: AuditLogEntry[], total: number, number = 1): Page<AuditLogEntry> {
  return { items, page: number, page_size: 20, total };
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <AuditLogPage />
    </QueryClientProvider>,
  );
}

/** 表头之后的数据行。 */
function dataRows(): HTMLElement[] {
  return screen.getAllByRole("row").slice(1);
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("AuditLogPage states", () => {
  it("shows a loading state while the first page is on its way", () => {
    api.listAuditLogs.mockReturnValue(new Promise(() => undefined));

    renderPage();

    expect(screen.getByText("Loading audit records…")).toBeInTheDocument();
  });

  it("shows the backend message and the request id when the list fails", async () => {
    api.listAuditLogs.mockRejectedValue(
      new ApiError("INTERNAL_ERROR", "An unexpected error occurred.", "req-500"),
    );

    renderPage();

    expect(await screen.findByText("The audit log could not be loaded.")).toBeInTheDocument();
    expect(screen.getByText(/An unexpected error occurred\./)).toBeInTheDocument();
    expect(screen.getByText("req-500")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("says there is no permission on a 403 instead of a generic failure", async () => {
    api.listAuditLogs.mockRejectedValue(
      new ApiError("ADMIN_REQUIRED", "Administrator access is required.", "req-403"),
    );

    renderPage();

    expect(
      await screen.findByText("Only administrators can view the audit log."),
    ).toBeInTheDocument();
    expect(screen.queryByText("The audit log could not be loaded.")).not.toBeInTheDocument();
  });

  it("shows the backend message on a 422 and keeps the filters editable", async () => {
    api.listAuditLogs.mockRejectedValue(
      new ApiError("VALIDATION_ERROR", "Invalid request fields: query.entity_id", "req-422"),
    );

    renderPage();

    expect(await screen.findByText("The filters were not accepted.")).toBeInTheDocument();
    expect(screen.getByText(/Invalid request fields: query\.entity_id/)).toBeInTheDocument();
    expect(screen.getByText("req-422")).toBeInTheDocument();
    expect(screen.getByLabelText("Entity ID")).toBeEnabled();
  });

  it("says so when there are no audit records at all", async () => {
    api.listAuditLogs.mockResolvedValue(page([], 0));

    renderPage();

    expect(await screen.findByText("No audit records yet.")).toBeInTheDocument();
  });

  it("says nothing matches when a filter leaves the list empty", async () => {
    const user = userEvent.setup();
    api.listAuditLogs.mockResolvedValueOnce(page([entry()], 1)).mockResolvedValue(page([], 0));

    renderPage();
    await screen.findByText("CUSTOMER_CREATE");
    await user.type(screen.getByLabelText("Entity type"), "project");
    await user.click(screen.getByRole("button", { name: "Apply filters" }));

    expect(await screen.findByText("No audit records match these filters.")).toBeInTheDocument();
  });
});

describe("AuditLogPage table", () => {
  it("lists records in the backend's order with Kuala Lumpur times", async () => {
    api.listAuditLogs.mockResolvedValue(
      page(
        [
          entry({ created_at: "2026-09-20T16:00:00", reason: "Goodwill credit" }),
          entry({
            action: "LOGIN",
            entity_type: "users",
            entity_id: null,
            entity_user_email: "ops@example.com",
            ip_address: null,
          }),
        ],
        2,
      ),
    );

    renderPage();

    await screen.findByText("LOGIN");
    const rows = dataRows();
    expect(rows[0]).toHaveTextContent("CUSTOMER_CREATE");
    // UTC 16:00 在吉隆坡已经是第二天。
    expect(rows[0]).toHaveTextContent("2026-09-21 00:00:00");
    expect(rows[0]).toHaveTextContent("admin@example.com");
    expect(rows[0]).toHaveTextContent("tenant");
    expect(rows[0]).toHaveTextContent(ENTITY_ID);
    expect(rows[0]).toHaveTextContent("203.0.113.7");
    expect(rows[0]).toHaveTextContent("Goodwill credit");
    expect(rows[1]).toHaveTextContent("LOGIN");
    expect(rows[1]).toHaveTextContent("2026-09-20 16:30:00");
    // 以用户为对象：没有 entity_id，显示那个用户的邮箱。
    expect(rows[1]).toHaveTextContent("users");
    expect(rows[1]).toHaveTextContent("ops@example.com");
    expect(api.listAuditLogs).toHaveBeenCalledWith({}, 1, 20, expect.anything());
  });

  it("tells a system action apart from an unknown actor when there is no email", async () => {
    api.listAuditLogs.mockResolvedValue(
      page(
        [
          entry({ action: "TENANT_BILLING_STATUS_CHANGED", actor_role: "SYSTEM", actor_email: null }),
          entry({ action: "LOGIN_FAILED", actor_role: null, actor_email: null, entity_type: null, entity_id: null }),
          // 操作者已经不存在了：角色还在，邮箱查不到。
          entry({ action: "CUSTOMER_UPDATE", actor_role: "ADMIN", actor_email: null }),
        ],
        3,
      ),
    );

    renderPage();

    await screen.findByText("LOGIN_FAILED");
    const rows = dataRows();
    expect(rows[0]).toHaveTextContent("System");
    expect(rows[0]).not.toHaveTextContent("Unknown");
    expect(rows[1]).toHaveTextContent("Unknown");
    expect(rows[1]).not.toHaveTextContent("System");
    expect(rows[2]).toHaveTextContent("Unknown");
  });

  it("shows the before and after state and the user agent in the expanded row", async () => {
    const user = userEvent.setup();
    api.listAuditLogs.mockResolvedValue(
      page(
        [
          entry({
            action: "CUSTOMER_UPDATE",
            before_state: { company_name: "Old Sdn Bhd" },
            after_state: { company_name: "New Sdn Bhd" },
          }),
          entry({ before_state: null }),
        ],
        2,
      ),
    );

    renderPage();
    await screen.findByText("CUSTOMER_UPDATE");
    expect(screen.queryByText(/Old Sdn Bhd/)).not.toBeInTheDocument();

    await user.click(within(dataRows()[0] as HTMLElement).getByRole("button", { name: "Expand row" }));

    expect(await screen.findByText(/"company_name": "Old Sdn Bhd"/)).toBeInTheDocument();
    expect(screen.getByText(/"company_name": "New Sdn Bhd"/)).toBeInTheDocument();
    expect(screen.getByText("Mozilla/5.0 (test)")).toBeInTheDocument();
    expect(screen.getByText("ADMIN")).toBeInTheDocument();
  });

  it("says None for a missing before state", async () => {
    const user = userEvent.setup();
    api.listAuditLogs.mockResolvedValue(page([entry({ before_state: null, user_agent: null })], 1));

    renderPage();
    await screen.findByText("CUSTOMER_CREATE");
    await user.click(screen.getByRole("button", { name: "Expand row" }));

    expect(await screen.findByText(/"company_name": "Acme Sdn Bhd"/)).toBeInTheDocument();
    // user agent 与前状态都没有。
    expect(screen.getAllByText("None")).toHaveLength(2);
  });
});

describe("AuditLogPage filters", () => {
  it("asks the backend for the page the user moves to", async () => {
    const user = userEvent.setup();
    api.listAuditLogs.mockImplementation((_filters: unknown, requested: number) =>
      Promise.resolve(page([entry({ reason: `On page ${String(requested)}` })], 45, requested)),
    );

    renderPage();

    expect(await screen.findByText("45 audit records")).toBeInTheDocument();
    await user.click(screen.getByTitle("2"));

    expect(await screen.findByText("On page 2")).toBeInTheDocument();
    expect(api.listAuditLogs).toHaveBeenLastCalledWith({}, 2, 20, expect.anything());
  });

  it("sends a new request and goes back to page 1 when a filter changes", async () => {
    const user = userEvent.setup();
    api.listAuditLogs.mockImplementation((_filters: unknown, requested: number) =>
      Promise.resolve(page([entry({ reason: `On page ${String(requested)}` })], 45, requested)),
    );

    renderPage();
    await screen.findByText("45 audit records");
    await user.click(screen.getByTitle("2"));
    await screen.findByText("On page 2");

    await user.type(screen.getByLabelText("Actor email"), " ops@example.com ");
    await user.type(screen.getByLabelText("Entity ID"), ENTITY_ID);
    await user.click(screen.getByRole("button", { name: "Apply filters" }));

    await waitFor(() =>
      expect(api.listAuditLogs).toHaveBeenLastCalledWith(
        { entity_id: ENTITY_ID, actor_email: "ops@example.com" },
        1,
        20,
        expect.anything(),
      ),
    );
    expect(await screen.findByText("On page 1")).toBeInTheDocument();
  });

  it("filters by an action picked from the dropdown", async () => {
    const user = userEvent.setup();
    api.listAuditLogs.mockResolvedValue(page([entry()], 1));

    renderPage();
    await screen.findByText("CUSTOMER_CREATE");
    await user.type(screen.getByLabelText("Action"), "LOGIN");
    // LOGIN 与 LOGIN_FAILED 都匹配搜索词；按 title 精确取 LOGIN 那一项。
    fireEvent.click(await screen.findByTitle("LOGIN"));
    await user.click(screen.getByRole("button", { name: "Apply filters" }));

    await waitFor(() =>
      expect(api.listAuditLogs).toHaveBeenLastCalledWith({ action: "LOGIN" }, 1, 20, expect.anything()),
    );
  });

  it("sends the time range as naive UTC, crossing back over midnight", async () => {
    const user = userEvent.setup();
    api.listAuditLogs.mockResolvedValue(page([entry()], 1));

    renderPage();
    await screen.findByText("CUSTOMER_CREATE");
    // 吉隆坡 9 月 21 日 00:00 是 UTC 9 月 20 日 16:00。
    fireEvent.change(screen.getByLabelText("From (Kuala Lumpur time)"), {
      target: { value: "2026-09-21T00:00" },
    });
    fireEvent.change(screen.getByLabelText("Before (Kuala Lumpur time)"), {
      target: { value: "2026-09-22T07:59:59" },
    });
    await user.click(screen.getByRole("button", { name: "Apply filters" }));

    await waitFor(() =>
      expect(api.listAuditLogs).toHaveBeenLastCalledWith(
        { created_from: "2026-09-20T16:00:00", created_to: "2026-09-21T23:59:59" },
        1,
        20,
        expect.anything(),
      ),
    );
  });

  it("refuses a range whose end is not after its start without asking the backend", async () => {
    const user = userEvent.setup();
    api.listAuditLogs.mockResolvedValue(page([entry()], 1));

    renderPage();
    await screen.findByText("CUSTOMER_CREATE");
    fireEvent.change(screen.getByLabelText("From (Kuala Lumpur time)"), {
      target: { value: "2026-09-21T00:00" },
    });
    fireEvent.change(screen.getByLabelText("Before (Kuala Lumpur time)"), {
      target: { value: "2026-09-21T00:00" },
    });
    await user.click(screen.getByRole("button", { name: "Apply filters" }));

    expect(await screen.findByText("The end must be later than the start.")).toBeInTheDocument();
    expect(api.listAuditLogs).toHaveBeenCalledTimes(1);
  });

  it("clears every filter and goes back to page 1", async () => {
    const user = userEvent.setup();
    api.listAuditLogs.mockResolvedValue(page([entry()], 1));

    renderPage();
    await screen.findByText("CUSTOMER_CREATE");
    await user.type(screen.getByLabelText("Entity type"), "tenant");
    await user.click(screen.getByRole("button", { name: "Apply filters" }));
    await waitFor(() =>
      expect(api.listAuditLogs).toHaveBeenLastCalledWith({ entity_type: "tenant" }, 1, 20, expect.anything()),
    );

    await user.click(screen.getByRole("button", { name: "Clear filters" }));

    await waitFor(() => expect(api.listAuditLogs).toHaveBeenLastCalledWith({}, 1, 20, expect.anything()));
    expect(screen.getByLabelText("Entity type")).toHaveValue("");
  });
});
