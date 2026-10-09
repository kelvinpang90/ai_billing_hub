/**
 * 供应商页：三种状态与无权限、表格、状态筛选、到计量类型页的链接；建供应商、改名、停用的二次确认、
 * 成功后重读、失败时显示后端码对应的文案（未知码回落到后端 message）与 request_id、确认中防重复提交。
 *
 * 请求路径与字段名由 `api/adminCatalog.test.ts` 管，这里只看页面把什么交给了它。uuid 一律全零。
 * 输入一律 click + paste：逐字 `user.type` 每按一键 antd 表单就重渲染一次，全量并行时会超时。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { configure, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import type { Provider } from "../../api/adminCatalog";
import type { Page } from "../../api/adminCustomers";
import { ApiError } from "../../api/client";
import { ProvidersPage } from "./ProvidersPage";

const ID = "00000000-0000-0000-0000-000000000000";

const api = vi.hoisted(() => ({
  listProviders: vi.fn(),
  createProvider: vi.fn(),
  updateProvider: vi.fn(),
}));

vi.mock("../../api/adminCatalog", async () => {
  const actual = await vi.importActual<typeof import("../../api/adminCatalog")>(
    "../../api/adminCatalog",
  );
  return { ...actual, ...api };
});

function provider(overrides: Partial<Provider> = {}): Provider {
  return {
    id: ID,
    code: "anthropic",
    display_name: "Anthropic",
    status: "ACTIVE",
    created_at: "2026-09-29T08:30:00",
    updated_at: "2026-09-29T08:30:00",
    ...overrides,
  };
}

function page(items: Provider[], total = items.length): Page<Provider> {
  return { items, page: 1, page_size: 20, total };
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <ProvidersPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** 按按钮上的文字找按钮；loading 时按钮名字前面多一个图标名，`getByRole` 的精确名字对不上。 */
function buttonWithText(text: string, container: HTMLElement = document.body): HTMLElement {
  const button = within(container).getByText(text).closest("button");
  if (button === null) {
    throw new Error(`"${text}" is not inside a button`);
  }
  return button;
}

/**
 * 按钮样式的单选项：antd 给里面的 `<input>` 设了 `pointer-events: none`，user-event 不肯点它；
 * 真人点的是外面的 `<label>`，这里也点它。
 */
function radioLabel(name: string): HTMLElement {
  const label = screen.getByRole("radio", { name }).closest("label");
  if (label === null) {
    throw new Error(`radio "${name}" is not inside a label`);
  }
  return label;
}

/** 最上面的那个对话框（确认框叠在表单框上面）。 */
function topDialog(): HTMLElement {
  const dialogs = screen.getAllByRole("dialog");
  const top = dialogs.at(-1);
  if (top === undefined) {
    throw new Error("no dialog");
  }
  return top;
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

type User = ReturnType<typeof userEvent.setup>;

async function fillCreateForm(user: User, code: string, name: string): Promise<void> {
  await user.click(await screen.findByRole("button", { name: "New provider" }));
  await user.click(screen.getByLabelText("Code"));
  await user.paste(code);
  await user.click(screen.getByLabelText("Display name"));
  await user.paste(name);
  await user.click(buttonWithText("Review", topDialog()));
}

beforeEach(() => {
  vi.clearAllMocks();
});

// antd 的表格与对话框在 jsdom 里渲染慢，全量并行时 5 秒不够（同 AdjustmentModal.test.tsx）。
const SLOW = { timeout: 15_000 };
// 同理，findBy / waitFor 默认只等 1 秒，不够 antd 重画；只改本文件（每个测试文件各自隔离）。
configure({ asyncUtilTimeout: 5_000 });

describe("ProvidersPage states", SLOW, () => {
  it("shows a loading state while the first page is on its way", () => {
    api.listProviders.mockReturnValue(new Promise(() => undefined));

    renderPage();

    expect(screen.getByText("Loading providers…")).toBeInTheDocument();
  });

  it("shows the backend message and the request id when the list fails", async () => {
    api.listProviders.mockRejectedValue(
      new ApiError("INTERNAL_ERROR", "An unexpected error occurred.", "req-500"),
    );

    renderPage();

    expect(await screen.findByText("The providers could not be loaded.")).toBeInTheDocument();
    expect(screen.getByText(/An unexpected error occurred\./)).toBeInTheDocument();
    expect(screen.getByText("req-500")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("says there is no permission on a 403 instead of a generic failure", async () => {
    api.listProviders.mockRejectedValue(
      new ApiError("ADMIN_REQUIRED", "Administrator access is required.", "req-403"),
    );

    renderPage();

    expect(
      await screen.findByText("Only administrators can manage the AI catalog."),
    ).toBeInTheDocument();
    expect(screen.queryByText("The providers could not be loaded.")).not.toBeInTheDocument();
    expect(screen.getByText("req-403")).toBeInTheDocument();
  });

  it("says so when there are no providers, and still links to the meter types", async () => {
    api.listProviders.mockResolvedValue(page([]));

    renderPage();

    expect(await screen.findByText("No providers yet.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Meter types" })).toHaveAttribute(
      "href",
      "/catalog/meter-types",
    );
    expect(api.listProviders).toHaveBeenCalledWith({}, 1, 20, expect.anything());
  });
});

describe("ProvidersPage table", SLOW, () => {
  it("shows codes exactly as stored and links each one to its detail page", async () => {
    api.listProviders.mockResolvedValue(
      page([provider(), provider({ code: "open_ai-2", display_name: "OpenAI", status: "RETIRED" })]),
    );

    renderPage();

    const link = await screen.findByRole("link", { name: "anthropic" });
    expect(link).toHaveAttribute("href", `/catalog/providers/${ID}`);
    const rows = screen.getAllByRole("row").slice(1);
    expect(rows[0]).toHaveTextContent("Anthropic");
    expect(rows[0]).toHaveTextContent("Active");
    expect(rows[1]).toHaveTextContent("open_ai-2");
    expect(rows[1]).toHaveTextContent("Retired");
    expect(within(rows[1] as HTMLElement).getByRole("button", { name: "Reactivate" })).toBeInTheDocument();
  });

  it("asks only for retired providers when that filter is chosen, back on page 1", async () => {
    const user = userEvent.setup();
    api.listProviders.mockResolvedValueOnce(page([provider()])).mockResolvedValue(page([]));

    renderPage();
    await screen.findByText("anthropic");
    await user.click(radioLabel("Retired"));

    await waitFor(() =>
      expect(api.listProviders).toHaveBeenLastCalledWith(
        { status: "RETIRED" },
        1,
        20,
        expect.anything(),
      ),
    );
    expect(await screen.findByText("No providers have this status.")).toBeInTheDocument();
  });
});

describe("ProvidersPage create", SLOW, () => {
  it("creates only after the confirmation, with the code untouched, then refreshes", async () => {
    const user = userEvent.setup();
    api.listProviders.mockResolvedValue(page([]));
    api.createProvider.mockResolvedValue(provider());

    renderPage();
    await fillCreateForm(user, "anthropic", "  Anthropic  ");

    // 第一步只是检查：还没发请求。
    expect(await screen.findByText("Create this provider?")).toBeInTheDocument();
    expect(api.createProvider).not.toHaveBeenCalled();

    await user.click(buttonWithText("Confirm and create", topDialog()));

    expect(await screen.findByText("Provider created.")).toBeInTheDocument();
    expect(api.createProvider).toHaveBeenCalledTimes(1);
    expect(api.createProvider).toHaveBeenCalledWith({ code: "anthropic", display_name: "Anthropic" });
    await waitFor(() => expect(api.listProviders).toHaveBeenCalledTimes(2));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("refuses a code the backend would refuse, without changing its case", async () => {
    const user = userEvent.setup();
    api.listProviders.mockResolvedValue(page([]));

    renderPage();
    await fillCreateForm(user, "Anthropic", "Anthropic");

    expect(
      await screen.findByText(
        "Use 1–64 lowercase letters, digits, hyphens or underscores, starting with a letter or digit.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Code")).toHaveValue("Anthropic");
    expect(screen.queryByText("Create this provider?")).not.toBeInTheDocument();
    expect(api.createProvider).not.toHaveBeenCalled();
  });

  it("goes back to the form without sending anything", async () => {
    const user = userEvent.setup();
    api.listProviders.mockResolvedValue(page([]));

    renderPage();
    await fillCreateForm(user, "anthropic", "Anthropic");
    await user.click(buttonWithText("Back to edit", topDialog()));

    await waitFor(() => expect(screen.queryByText("Create this provider?")).not.toBeInTheDocument());
    expect(screen.getByLabelText("Code")).toHaveValue("anthropic");
    expect(api.createProvider).not.toHaveBeenCalled();
  });

  it("shows the text for a known 409 code with the request id", async () => {
    const user = userEvent.setup();
    api.listProviders.mockResolvedValue(page([]));
    api.createProvider.mockRejectedValue(
      new ApiError("AI_PROVIDER_CODE_TAKEN", "The provider code is already in use.", "req-409"),
    );

    renderPage();
    await fillCreateForm(user, "anthropic", "Anthropic");
    await user.click(buttonWithText("Confirm and create", topDialog()));

    expect(await screen.findByText("A provider with this code already exists.")).toBeInTheDocument();
    expect(screen.getByText(/The provider code is already in use\./)).toBeInTheDocument();
    expect(screen.getByText("req-409")).toBeInTheDocument();
    // 失败不刷新列表、不关对话框。
    expect(api.listProviders).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("Provider created.")).not.toBeInTheDocument();
  });

  it("shows the text for a 422 together with the backend's field list", async () => {
    const user = userEvent.setup();
    api.listProviders.mockResolvedValue(page([]));
    api.createProvider.mockRejectedValue(
      new ApiError("VALIDATION_ERROR", "Invalid request fields: body.code", "req-422"),
    );

    renderPage();
    await fillCreateForm(user, "anthropic", "Anthropic");
    await user.click(buttonWithText("Confirm and create", topDialog()));

    expect(await screen.findByText("Some fields were not accepted.")).toBeInTheDocument();
    expect(screen.getByText(/Invalid request fields: body\.code/)).toBeInTheDocument();
    expect(screen.getByText("req-422")).toBeInTheDocument();
  });

  it("falls back to the backend message for a code it does not know", async () => {
    const user = userEvent.setup();
    api.listProviders.mockResolvedValue(page([]));
    api.createProvider.mockRejectedValue(
      new ApiError("SOMETHING_NEW", "A brand new failure.", "req-418"),
    );

    renderPage();
    await fillCreateForm(user, "anthropic", "Anthropic");
    await user.click(buttonWithText("Confirm and create", topDialog()));

    expect(await screen.findByText("The provider was not created.")).toBeInTheDocument();
    expect(screen.getByText(/A brand new failure\./)).toBeInTheDocument();
    expect(screen.getByText("req-418")).toBeInTheDocument();
  });

  it("sends once however often the confirm button is clicked while it is running", async () => {
    const user = userEvent.setup();
    const pending = deferred<Provider>();
    api.listProviders.mockResolvedValue(page([]));
    api.createProvider.mockReturnValue(pending.promise);

    renderPage();
    await fillCreateForm(user, "anthropic", "Anthropic");
    await user.dblClick(buttonWithText("Confirm and create", topDialog()));

    await waitFor(() => expect(buttonWithText("Confirm and create", topDialog())).toBeDisabled());
    expect(buttonWithText("Back to edit", topDialog())).toBeDisabled();
    expect(api.createProvider).toHaveBeenCalledTimes(1);

    pending.resolve(provider());
    expect(await screen.findByText("Provider created.")).toBeInTheDocument();
  });
});

describe("ProvidersPage rename and retire", SLOW, () => {
  it("renames only after the confirmation and sends only the display name", async () => {
    const user = userEvent.setup();
    api.listProviders.mockResolvedValue(page([provider()]));
    api.updateProvider.mockResolvedValue(provider({ display_name: "Anthropic PBC" }));

    renderPage();
    await user.click(await screen.findByRole("button", { name: "Rename" }));
    await user.clear(screen.getByLabelText("Display name"));
    await user.paste(" Anthropic PBC ");
    await user.click(buttonWithText("Review", topDialog()));

    expect(await screen.findByText("Save the new display name?")).toBeInTheDocument();
    expect(api.updateProvider).not.toHaveBeenCalled();
    await user.click(buttonWithText("Confirm and save", topDialog()));

    expect(await screen.findByText("Display name saved.")).toBeInTheDocument();
    expect(api.updateProvider).toHaveBeenCalledWith(ID, { display_name: "Anthropic PBC" });
    await waitFor(() => expect(api.listProviders).toHaveBeenCalledTimes(2));
  });

  it("does not offer to save a name that did not change", async () => {
    const user = userEvent.setup();
    api.listProviders.mockResolvedValue(page([provider()]));

    renderPage();
    await user.click(await screen.findByRole("button", { name: "Rename" }));
    await user.click(buttonWithText("Review", topDialog()));

    expect(await screen.findByText("This is already the display name.")).toBeInTheDocument();
    expect(api.updateProvider).not.toHaveBeenCalled();
  });

  it("retires only after the confirmation, then refreshes", async () => {
    const user = userEvent.setup();
    api.listProviders.mockResolvedValue(page([provider()]));
    api.updateProvider.mockResolvedValue(provider({ status: "RETIRED" }));

    renderPage();
    await user.click(await screen.findByRole("button", { name: "Retire" }));

    expect(await screen.findByText("Retire anthropic?")).toBeInTheDocument();
    expect(api.updateProvider).not.toHaveBeenCalled();
    await user.click(buttonWithText("Confirm and retire", topDialog()));

    expect(await screen.findByText("Retired.")).toBeInTheDocument();
    expect(api.updateProvider).toHaveBeenCalledWith(ID, { status: "RETIRED" });
    await waitFor(() => expect(api.listProviders).toHaveBeenCalledTimes(2));
  });

  it("reactivates a retired provider", async () => {
    const user = userEvent.setup();
    api.listProviders.mockResolvedValue(page([provider({ status: "RETIRED" })]));
    api.updateProvider.mockResolvedValue(provider());

    renderPage();
    await user.click(await screen.findByRole("button", { name: "Reactivate" }));
    await user.click(buttonWithText("Confirm and reactivate", topDialog()));

    expect(await screen.findByText("Reactivated.")).toBeInTheDocument();
    expect(api.updateProvider).toHaveBeenCalledWith(ID, { status: "ACTIVE" });
  });

  it("keeps the confirmation open and shows the error when retiring fails", async () => {
    const user = userEvent.setup();
    api.listProviders.mockResolvedValue(page([provider()]));
    api.updateProvider.mockRejectedValue(
      new ApiError("AI_PROVIDER_NOT_FOUND", "The provider does not exist.", "req-404"),
    );

    renderPage();
    await user.click(await screen.findByRole("button", { name: "Retire" }));
    await user.click(buttonWithText("Confirm and retire", topDialog()));

    expect(await screen.findByText("This provider does not exist.")).toBeInTheDocument();
    expect(screen.getByText("req-404")).toBeInTheDocument();
    expect(screen.getByText("Retire anthropic?")).toBeInTheDocument();
    expect(api.listProviders).toHaveBeenCalledTimes(1);
  });
});
