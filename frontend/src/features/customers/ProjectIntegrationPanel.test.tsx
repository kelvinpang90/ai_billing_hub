/**
 * 项目的集成状态与状态 webhook 地址面板：显示、地址的加载中与失败、启停的二次确认与原因校验、
 * 地址的设置与清除、成功后只让项目列表与地址查询过期、`ACCOUNT_CLOSED` 与未知错误码的显示。
 *
 * 地址示例一律用保留的虚构域名（`example.invalid`），uuid 是全零。
 * 输入一律 click + paste：逐字 `user.type` 每按一键 antd 表单就重渲染一次，全量并行时会超时。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import { projectListQueryKey, type Page, type Project } from "../../api/adminCustomers";
import { ApiError } from "../../api/client";
import { credentialListQueryKey } from "../../api/integrationAccess";
import { ProjectIntegrationError, type StatusWebhook } from "../../api/projectIntegration";
import { webhookSecretListQueryKey } from "../../api/webhookSigning";
import { ProjectIntegrationPanel, reasonProblem } from "./ProjectIntegrationPanel";

// antd 的 TextArea 不管有没有 autoSize 都会挂 ResizeObserver，jsdom 里没有：补一个空实现。
class NoopResizeObserver {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}
if (typeof globalThis.ResizeObserver === "undefined") {
  globalThis.ResizeObserver = NoopResizeObserver;
}

const CUSTOMER_ID = "00000000-0000-0000-0000-000000000000";
const PROJECT_ID = "00000000-0000-0000-0000-000000000000";
const ADDRESS = "https://hooks.example.invalid/acuven/receive";

const LOADING = "Loading the status webhook address…";
const LOAD_FAILED = "The status webhook address could not be loaded.";
const CLOSED_STATUS =
  "This customer's account is closed, so the integration status of its projects cannot be changed. Nothing was changed.";
const CLOSED_WEBHOOK =
  "This customer's account is closed, so a status webhook address cannot be set. Clearing the address is still allowed. Nothing was changed.";
const HTTPS_HINT = "The address must start with https://.";

const api = vi.hoisted(() => ({
  getStatusWebhook: vi.fn(),
  setIntegrationStatus: vi.fn(),
  setStatusWebhook: vi.fn(),
}));

vi.mock("../../api/projectIntegration", async () => {
  const actual = await vi.importActual<typeof import("../../api/projectIntegration")>(
    "../../api/projectIntegration",
  );
  return { ...actual, ...api };
});

function project(overrides: Partial<Project> = {}): Project {
  return {
    id: PROJECT_ID,
    name: "Chatbot",
    description: null,
    integration_status: "ENABLED",
    status_webhook_configured: false,
    created_at: "2026-09-20T08:31:00",
    updated_at: "2026-09-20T08:31:00",
    ...overrides,
  };
}

function address(url: string | null): StatusWebhook {
  return { status_webhook_url: url, status_webhook_configured: url !== null };
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

const PROJECT_LIST_KEY = projectListQueryKey(CUSTOMER_ID, 1, 20);
const CREDENTIALS_KEY = credentialListQueryKey(CUSTOMER_ID, PROJECT_ID, 1, 20);
const SECRETS_KEY = webhookSecretListQueryKey(CUSTOMER_ID, PROJECT_ID, 1, 20);

/**
 * 预先放进项目列表、凭据、签名密钥三份缓存：写操作之后看哪些被判过期。
 * 它们都没有观察者，过期之后不会自己重读，`isInvalidated` 一直留着。
 */
function renderPanel(subject: Project = project()) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const list: Page<Project> = { items: [subject], page: 1, page_size: 20, total: 1 };
  queryClient.setQueryData(PROJECT_LIST_KEY, list);
  queryClient.setQueryData(CREDENTIALS_KEY, { items: [], page: 1, page_size: 20, total: 0 });
  queryClient.setQueryData(SECRETS_KEY, { items: [], page: 1, page_size: 20, total: 0 });
  render(
    <QueryClientProvider client={queryClient}>
      <ProjectIntegrationPanel customerId={CUSTOMER_ID} project={subject} />
    </QueryClientProvider>,
  );
  return { queryClient };
}

function invalidated(queryClient: QueryClient, key: readonly unknown[]): boolean {
  return queryClient.getQueryState(key)?.isInvalidated ?? false;
}

/** 只让项目列表与地址过期：列表被判过期、地址重读了一次，凭据与签名密钥不动。 */
async function expectOnlyListAndAddressRefreshed(queryClient: QueryClient): Promise<void> {
  await waitFor(() => {
    expect(api.getStatusWebhook).toHaveBeenCalledTimes(2);
  });
  expect(invalidated(queryClient, PROJECT_LIST_KEY)).toBe(true);
  expect(invalidated(queryClient, CREDENTIALS_KEY)).toBe(false);
  expect(invalidated(queryClient, SECRETS_KEY)).toBe(false);
}

type User = ReturnType<typeof userEvent.setup>;

/** 按按钮上的文字找按钮；loading 时按钮名字前面多一个图标名，`getByRole` 的精确名字对不上。 */
function buttonWithText(text: string, container: HTMLElement = document.body): HTMLElement {
  const button = within(container).getByText(text).closest("button");
  if (button === null) {
    throw new Error(`"${text}" is not inside a button`);
  }
  return button;
}

function dialog(): HTMLElement {
  return screen.getByRole("dialog");
}

/** 带边框的 Descriptions：label 在 <th>，值在紧跟的 <td>。 */
function valueOf(label: string): string {
  const th = screen.getByText(label).closest("th");
  return th?.nextElementSibling?.textContent ?? "";
}

async function fillReason(user: User, reason: string): Promise<void> {
  await user.clear(screen.getByLabelText("Reason"));
  await user.click(screen.getByLabelText("Reason"));
  await user.paste(reason);
}

async function fillAddress(user: User, value: string): Promise<void> {
  await user.clear(screen.getByLabelText("New status webhook address"));
  await user.click(screen.getByLabelText("New status webhook address"));
  await user.paste(value);
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("ProjectIntegrationPanel: what it shows", () => {
  it("shows the project's status and whether an address is set while the address is loading", () => {
    api.getStatusWebhook.mockReturnValue(new Promise(() => undefined));

    renderPanel();

    expect(screen.getByRole("region", { name: "Integration status and status webhook" })).toBeInTheDocument();
    expect(valueOf("Integration status")).toBe("Enabled");
    expect(valueOf("Status webhook")).toBe("Not configured");
    expect(screen.getByText(LOADING)).toBeInTheDocument();
    expect(api.getStatusWebhook).toHaveBeenCalledWith(CUSTOMER_ID, PROJECT_ID, expect.anything());
  });

  it("shows the backend message and the request id when the address cannot be loaded", async () => {
    const user = userEvent.setup();
    api.getStatusWebhook
      .mockRejectedValueOnce(new ApiError("INTERNAL_ERROR", "An unexpected error occurred.", "req-500"))
      .mockResolvedValue(address(null));

    renderPanel();

    expect(await screen.findByText(LOAD_FAILED)).toBeInTheDocument();
    expect(screen.getByText(/An unexpected error occurred\./)).toBeInTheDocument();
    expect(screen.getByText("req-500")).toBeInTheDocument();
    expect(screen.queryByText(LOADING)).not.toBeInTheDocument();
    // 状态来自项目对象，读不到地址也照样显示。
    expect(valueOf("Integration status")).toBe("Enabled");

    await user.click(screen.getByRole("button", { name: "Try again" }));

    expect(await screen.findByText("Not set")).toBeInTheDocument();
    expect(screen.queryByText(LOAD_FAILED)).not.toBeInTheDocument();
  });

  it("says the caller is not an administrator on a 403", async () => {
    api.getStatusWebhook.mockRejectedValue(
      new ApiError("ADMIN_REQUIRED", "Administrator access is required.", "req-403"),
    );

    renderPanel();

    expect(await screen.findByText("Only administrators can manage customers.")).toBeInTheDocument();
    expect(screen.getByText("req-403")).toBeInTheDocument();
  });

  it("shows the current address once it is read", async () => {
    api.getStatusWebhook.mockResolvedValue(address(ADDRESS));

    renderPanel(project({ integration_status: "DISABLED", status_webhook_configured: true }));

    expect(await screen.findByText(ADDRESS)).toBeInTheDocument();
    expect(valueOf("Integration status")).toBe("Disabled");
    expect(valueOf("Status webhook")).toBe("Configured");
    expect(buttonWithText("Enable integration")).toBeEnabled();
    expect(screen.queryByText("Disable integration")).not.toBeInTheDocument();
  });

  it("says when no address is set and does not offer to clear it", async () => {
    api.getStatusWebhook.mockResolvedValue(address(null));

    renderPanel();

    expect(await screen.findByText("Not set")).toBeInTheDocument();
    expect(buttonWithText("Clear address")).toBeDisabled();
  });

  it("shows an unknown status, and no enable or disable button, when the project has none", async () => {
    api.getStatusWebhook.mockResolvedValue(address(null));
    const subject: Project = {
      id: PROJECT_ID,
      name: "Chatbot",
      description: null,
      created_at: "2026-09-20T08:31:00",
      updated_at: "2026-09-20T08:31:00",
    };

    renderPanel(subject);

    await screen.findByText("Not set");
    expect(valueOf("Integration status")).toBe("Unknown");
    expect(valueOf("Status webhook")).toBe("Unknown");
    expect(screen.queryByText("Disable integration")).not.toBeInTheDocument();
    expect(screen.queryByText("Enable integration")).not.toBeInTheDocument();
  });
});

describe("reasonProblem", () => {
  it("checks the reason the way the backend does, after trimming", () => {
    expect(reasonProblem(undefined)).toBe("required");
    expect(reasonProblem("")).toBe("required");
    expect(reasonProblem("   ")).toBe("required");
    expect(reasonProblem(" r ")).toBeNull();
    expect(reasonProblem(` ${"r".repeat(255)} `)).toBeNull();
    expect(reasonProblem(` ${"r".repeat(256)} `)).toBe("tooLong");
    // 按码点数：255 个 emoji 是 510 个 UTF-16 单元，后端照样收。
    expect(reasonProblem("😀".repeat(255))).toBeNull();
    expect(reasonProblem("😀".repeat(256))).toBe("tooLong");
  });
});

describe("ProjectIntegrationPanel: enabling and disabling", () => {
  it("asks for a reason before disabling, checks it the way the backend does, and refreshes", async () => {
    const user = userEvent.setup();
    api.getStatusWebhook.mockResolvedValue(address(null));
    api.setIntegrationStatus.mockResolvedValue(project({ integration_status: "DISABLED" }));
    const { queryClient } = renderPanel();

    await screen.findByText("Not set");
    await user.click(buttonWithText("Disable integration"));

    expect(within(dialog()).getByText("Disable the integration for this project?")).toBeInTheDocument();
    // 停用后不再推送、集成方经对账得知，确认框写明。
    expect(within(dialog()).getByText(/no longer pushes status webhooks/)).toBeInTheDocument();
    expect(within(dialog()).getByText(/periodic reconciliation/)).toBeInTheDocument();
    expect(within(dialog()).getByText(/Do not include personal data/)).toBeInTheDocument();

    // 没填原因（其余边界由 reasonProblem 的用例覆盖，表单用的就是它）。
    await user.click(buttonWithText("Disable", dialog()));
    expect(await screen.findByText("Enter the reason.")).toBeInTheDocument();
    expect(api.setIntegrationStatus).not.toHaveBeenCalled();

    // 恰好 255，带首尾空白：发出去的是去掉空白之后的。
    await fillReason(user, ` ${"r".repeat(255)} `);
    await user.click(buttonWithText("Disable", dialog()));

    expect(await screen.findByText("Integration disabled.")).toBeInTheDocument();
    expect(api.setIntegrationStatus).toHaveBeenCalledTimes(1);
    expect(api.setIntegrationStatus).toHaveBeenCalledWith(
      CUSTOMER_ID,
      PROJECT_ID,
      "DISABLED",
      "r".repeat(255),
    );
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    await expectOnlyListAndAddressRefreshed(queryClient);
  });

  it("enables a disabled project after confirmation with a trimmed reason", async () => {
    const user = userEvent.setup();
    api.getStatusWebhook.mockResolvedValue(address(null));
    api.setIntegrationStatus.mockResolvedValue(project({ integration_status: "ENABLED" }));
    const { queryClient } = renderPanel(project({ integration_status: "DISABLED" }));

    await screen.findByText("Not set");
    await user.click(buttonWithText("Enable integration"));

    expect(within(dialog()).getByText("Enable the integration for this project?")).toBeInTheDocument();
    await fillReason(user, "  Customer confirmed the integration is fixed  ");
    await user.click(buttonWithText("Enable", dialog()));

    expect(await screen.findByText("Integration enabled.")).toBeInTheDocument();
    expect(api.setIntegrationStatus).toHaveBeenCalledWith(
      CUSTOMER_ID,
      PROJECT_ID,
      "ENABLED",
      "Customer confirmed the integration is fixed",
    );
    await expectOnlyListAndAddressRefreshed(queryClient);
  });

  it("disables the confirm button while the change is in flight and sends it once", async () => {
    const user = userEvent.setup();
    const pending = deferred<Project>();
    api.getStatusWebhook.mockResolvedValue(address(null));
    api.setIntegrationStatus.mockReturnValue(pending.promise);
    renderPanel();

    await screen.findByText("Not set");
    await user.click(buttonWithText("Disable integration"));
    await fillReason(user, "Paused for maintenance");
    await user.dblClick(buttonWithText("Disable", dialog()));

    await waitFor(() => {
      expect(buttonWithText("Disable", dialog())).toBeDisabled();
    });
    expect(buttonWithText("Cancel", dialog())).toBeDisabled();
    expect(api.setIntegrationStatus).toHaveBeenCalledTimes(1);

    pending.resolve(project({ integration_status: "DISABLED" }));

    expect(await screen.findByText("Integration disabled.")).toBeInTheDocument();
    expect(api.setIntegrationStatus).toHaveBeenCalledTimes(1);
  });

  it("keeps the dialog open and explains a closed account with the request id", async () => {
    const user = userEvent.setup();
    api.getStatusWebhook.mockResolvedValue(address(null));
    api.setIntegrationStatus.mockRejectedValue(
      new ProjectIntegrationError("ACCOUNT_CLOSED", "The customer account is closed.", "req-409", 409),
    );
    const { queryClient } = renderPanel();

    await screen.findByText("Not set");
    await user.click(buttonWithText("Disable integration"));
    await fillReason(user, "Paused for maintenance");
    await user.click(buttonWithText("Disable", dialog()));

    expect(await within(dialog()).findByText(CLOSED_STATUS)).toBeInTheDocument();
    expect(within(dialog()).getByText("req-409")).toBeInTheDocument();
    expect(screen.getByLabelText("Reason")).toHaveValue("Paused for maintenance");
    expect(screen.queryByText("Integration disabled.")).not.toBeInTheDocument();
    expect(invalidated(queryClient, PROJECT_LIST_KEY)).toBe(false);
  });

  it("falls back to the backend message for an error code it has no text for", async () => {
    const user = userEvent.setup();
    api.getStatusWebhook.mockResolvedValue(address(null));
    api.setIntegrationStatus.mockRejectedValue(
      new ProjectIntegrationError("PROJECT_NOT_FOUND", "The project does not exist.", "req-404", 404),
    );
    renderPanel();

    await screen.findByText("Not set");
    await user.click(buttonWithText("Disable integration"));
    await fillReason(user, "Paused for maintenance");
    await user.click(buttonWithText("Disable", dialog()));

    expect(
      await within(dialog()).findByText("The integration status was not changed."),
    ).toBeInTheDocument();
    expect(within(dialog()).getByText(/The project does not exist\./)).toBeInTheDocument();
    expect(within(dialog()).getByText("req-404")).toBeInTheDocument();
  });

  it("does not change anything when the confirmation is cancelled", async () => {
    const user = userEvent.setup();
    api.getStatusWebhook.mockResolvedValue(address(null));
    renderPanel();

    await screen.findByText("Not set");
    await user.click(buttonWithText("Disable integration"));
    await user.click(buttonWithText("Cancel", dialog()));

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(api.setIntegrationStatus).not.toHaveBeenCalled();
  });
});

describe("ProjectIntegrationPanel: the status webhook address", () => {
  it("does not save an empty address", async () => {
    const user = userEvent.setup();
    api.getStatusWebhook.mockResolvedValue(address(null));
    renderPanel();

    await screen.findByText("Not set");
    await user.click(buttonWithText("Save address"));

    expect(await screen.findByText("Enter the address.")).toBeInTheDocument();
    expect(api.setStatusWebhook).not.toHaveBeenCalled();
  });

  it("saves the address exactly as typed and refreshes only the list and the address", async () => {
    const user = userEvent.setup();
    api.getStatusWebhook.mockResolvedValueOnce(address(null)).mockResolvedValue(address(ADDRESS));
    api.setStatusWebhook.mockResolvedValue(address(ADDRESS));
    const { queryClient } = renderPanel();

    await screen.findByText("Not set");
    await fillAddress(user, ADDRESS);
    expect(screen.queryByText(HTTPS_HINT)).not.toBeInTheDocument();
    await user.click(buttonWithText("Save address"));

    expect(await screen.findByText("Status webhook address saved.")).toBeInTheDocument();
    expect(api.setStatusWebhook).toHaveBeenCalledTimes(1);
    expect(api.setStatusWebhook).toHaveBeenCalledWith(CUSTOMER_ID, PROJECT_ID, ADDRESS);
    await expectOnlyListAndAddressRefreshed(queryClient);
    // 新地址是从后端读回来的。
    expect(await screen.findByText(ADDRESS)).toBeInTheDocument();
    expect(screen.getByLabelText("New status webhook address")).toHaveValue("");
  });

  it("only hints at https and leaves the verdict to the backend's field error", async () => {
    const user = userEvent.setup();
    api.getStatusWebhook.mockResolvedValue(address(null));
    api.setStatusWebhook.mockRejectedValue(
      new ProjectIntegrationError(
        "VALIDATION_ERROR",
        "The status webhook address is not valid.",
        "req-422",
        422,
      ),
    );
    renderPanel();

    await screen.findByText("Not set");
    await fillAddress(user, "http://hooks.example.invalid/acuven/receive");

    expect(await screen.findByText(HTTPS_HINT)).toBeInTheDocument();
    // 提示不拦提交：发出去，由后端判。
    await user.click(buttonWithText("Save address"));

    expect(await screen.findByText(/The status webhook address is not valid\./)).toBeInTheDocument();
    expect(screen.getByText("req-422")).toBeInTheDocument();
    expect(api.setStatusWebhook).toHaveBeenCalledWith(
      CUSTOMER_ID,
      PROJECT_ID,
      "http://hooks.example.invalid/acuven/receive",
    );
    expect(screen.queryByText("Status webhook address saved.")).not.toBeInTheDocument();
    // 字段错误挂在输入框上，输入框里的值留着。
    expect(screen.getByLabelText("New status webhook address")).toHaveValue(
      "http://hooks.example.invalid/acuven/receive",
    );
  });

  it("explains a closed account with the request id when setting an address", async () => {
    const user = userEvent.setup();
    api.getStatusWebhook.mockResolvedValue(address(null));
    api.setStatusWebhook.mockRejectedValue(
      new ProjectIntegrationError("ACCOUNT_CLOSED", "The customer account is closed.", "req-409", 409),
    );
    renderPanel();

    await screen.findByText("Not set");
    await fillAddress(user, ADDRESS);
    await user.click(buttonWithText("Save address"));

    expect(await screen.findByText(CLOSED_WEBHOOK)).toBeInTheDocument();
    expect(screen.getByText("req-409")).toBeInTheDocument();
  });

  it("falls back to the backend message when saving fails for another reason", async () => {
    const user = userEvent.setup();
    api.getStatusWebhook.mockResolvedValue(address(null));
    api.setStatusWebhook.mockRejectedValue(
      new ProjectIntegrationError("INTERNAL_ERROR", "An unexpected error occurred.", "req-500", 500),
    );
    renderPanel();

    await screen.findByText("Not set");
    await fillAddress(user, ADDRESS);
    await user.click(buttonWithText("Save address"));

    expect(await screen.findByText("The address was not saved.")).toBeInTheDocument();
    expect(screen.getByText(/An unexpected error occurred\./)).toBeInTheDocument();
    expect(screen.getByText("req-500")).toBeInTheDocument();
  });

  it("disables saving while the request is in flight and sends it once", async () => {
    const user = userEvent.setup();
    const pending = deferred<StatusWebhook>();
    api.getStatusWebhook.mockResolvedValue(address(null));
    api.setStatusWebhook.mockReturnValue(pending.promise);
    renderPanel();

    await screen.findByText("Not set");
    await fillAddress(user, ADDRESS);
    await user.dblClick(buttonWithText("Save address"));

    await waitFor(() => {
      expect(buttonWithText("Save address")).toBeDisabled();
    });
    expect(buttonWithText("Disable integration")).toBeDisabled();
    expect(api.setStatusWebhook).toHaveBeenCalledTimes(1);

    pending.resolve(address(ADDRESS));

    expect(await screen.findByText("Status webhook address saved.")).toBeInTheDocument();
    expect(api.setStatusWebhook).toHaveBeenCalledTimes(1);
  });

  it("asks before clearing the address, then clears it with null and refreshes", async () => {
    const user = userEvent.setup();
    api.getStatusWebhook.mockResolvedValueOnce(address(ADDRESS)).mockResolvedValue(address(null));
    api.setStatusWebhook.mockResolvedValue(address(null));
    const { queryClient } = renderPanel(project({ status_webhook_configured: true }));

    await screen.findByText(ADDRESS);
    await user.click(buttonWithText("Clear address"));

    expect(within(dialog()).getByText("Clear the status webhook address?")).toBeInTheDocument();
    expect(within(dialog()).getByText(/stops pushing status webhooks/)).toBeInTheDocument();
    expect(api.setStatusWebhook).not.toHaveBeenCalled();

    await user.click(buttonWithText("Clear address", dialog()));

    expect(await screen.findByText("Status webhook address cleared.")).toBeInTheDocument();
    expect(api.setStatusWebhook).toHaveBeenCalledTimes(1);
    expect(api.setStatusWebhook).toHaveBeenCalledWith(CUSTOMER_ID, PROJECT_ID, null);
    await expectOnlyListAndAddressRefreshed(queryClient);
    expect(await screen.findByText("Not set")).toBeInTheDocument();
  });

  it("keeps the clear dialog open with the backend message when clearing fails", async () => {
    const user = userEvent.setup();
    api.getStatusWebhook.mockResolvedValue(address(ADDRESS));
    api.setStatusWebhook.mockRejectedValue(
      new ProjectIntegrationError("PROJECT_NOT_FOUND", "The project does not exist.", "req-404", 404),
    );
    renderPanel(project({ status_webhook_configured: true }));

    await screen.findByText(ADDRESS);
    await user.click(buttonWithText("Clear address"));
    await user.click(buttonWithText("Clear address", dialog()));

    expect(await within(dialog()).findByText("The address was not cleared.")).toBeInTheDocument();
    expect(within(dialog()).getByText(/The project does not exist\./)).toBeInTheDocument();
    expect(within(dialog()).getByText("req-404")).toBeInTheDocument();
  });

  it("does not clear the address when the confirmation is cancelled", async () => {
    const user = userEvent.setup();
    api.getStatusWebhook.mockResolvedValue(address(ADDRESS));
    renderPanel(project({ status_webhook_configured: true }));

    await screen.findByText(ADDRESS);
    await user.click(buttonWithText("Clear address"));
    await user.click(buttonWithText("Cancel", dialog()));

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(api.setStatusWebhook).not.toHaveBeenCalled();
  });
});
