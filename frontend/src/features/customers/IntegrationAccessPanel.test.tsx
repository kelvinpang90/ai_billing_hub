/**
 * 项目的集成凭据面板：列表与分组、secret 只显示一次、轮换带最大版本与各种失败、吊销的二次确认。
 *
 * ⚠️ 这里最要紧的是 `secret` 去了哪：只能出现在结果对话框里，关掉就从 DOM 消失，查询缓存、
 * mutation 缓存、浏览器存储、控制台、地址栏里都不能有它。这几处漏了都不会报错。
 *
 * 示例值一律是 docs/api.md 的全零占位值（`ak_` 加 32 个 0、`sk_` 加 64 个 0），uuid 也是全零。
 * 输入一律 click + paste：逐字 `user.type` 每按一键 antd 表单就重渲染一次，全量并行时会超时。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import type { Page, Project } from "../../api/adminCustomers";
import { CredentialError, type Credential, type IssuedCredential } from "../../api/integrationAccess";
import { IntegrationAccessPanel, groupByApiKey, newestVersion } from "./IntegrationAccessPanel";

const CUSTOMER_ID = "00000000-0000-0000-0000-000000000000";
const PROJECT_ID = "00000000-0000-0000-0000-000000000000";
const API_KEY = `ak_${"0".repeat(32)}`;
const SECRET = `sk_${"0".repeat(64)}`;
// 第二个 key 只用来测分组：仍是全零，末位换成 1 以示区分。
const OTHER_KEY = `ak_${"0".repeat(31)}1`;

const WARNING =
  "This secret is shown only once. After you close this dialog it cannot be viewed again. Copy it and store it safely now.";
const CONFLICT = "This API key has already been rotated by someone else.";
const UNKNOWN = "We could not confirm whether a new credential was issued.";

const api = vi.hoisted(() => ({
  listCredentials: vi.fn(),
  createCredential: vi.fn(),
  rotateCredential: vi.fn(),
  revokeCredentialVersion: vi.fn(),
  revokeCredential: vi.fn(),
}));

vi.mock("../../api/integrationAccess", async () => {
  const actual = await vi.importActual<typeof import("../../api/integrationAccess")>(
    "../../api/integrationAccess",
  );
  return { ...actual, ...api };
});

const PROJECT: Project = {
  id: PROJECT_ID,
  name: "Chatbot",
  description: null,
  created_at: "2026-09-20T08:31:00",
  updated_at: "2026-09-20T08:31:00",
};

function version(overrides: Partial<Credential> = {}): Credential {
  return {
    api_key: API_KEY,
    key_version: 1,
    status: "ACTIVE",
    valid_from: "2026-09-25T08:30:00",
    valid_until: null,
    last_used_at: null,
    created_at: "2026-09-25T08:30:00",
    revoked_at: null,
    verifiable: true,
    ...overrides,
  };
}

function issued(overrides: Partial<Credential> = {}): IssuedCredential {
  return { ...version(overrides), secret: SECRET };
}

function page(items: Credential[], total = items.length): Page<Credential> {
  return { items, page: 1, page_size: 20, total };
}

/** 三个版本的一个 key：v1、v2 已有截止时间，v3 是最新。 */
function threeVersions(): Credential[] {
  return [
    version({ key_version: 1, valid_until: "2026-10-02T08:30:00" }),
    version({ key_version: 2, valid_until: "2026-10-02T08:30:00" }),
    version({ key_version: 3 }),
  ];
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

function renderPanel() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <IntegrationAccessPanel customerId={CUSTOMER_ID} project={PROJECT} />
    </QueryClientProvider>,
  );
  return { queryClient };
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

/** 查询缓存与 mutation 缓存里所有东西拼成的一个串。 */
function cachedText(queryClient: QueryClient): string {
  const queries = queryClient.getQueryCache().getAll().map((query) => query.state);
  const mutations = queryClient.getMutationCache().getAll().map((mutation) => mutation.state);
  return JSON.stringify({ queries, mutations });
}

function queryCacheText(queryClient: QueryClient): string {
  return JSON.stringify(
    queryClient
      .getQueryCache()
      .getAll()
      .map((query) => query.state),
  );
}

function storedText(): string {
  const values: string[] = [];
  for (const storage of [window.localStorage, window.sessionStorage]) {
    for (let index = 0; index < storage.length; index += 1) {
      const key = storage.key(index);
      if (key !== null) {
        values.push(key, storage.getItem(key) ?? "");
      }
    }
  }
  return values.join("\n");
}

const CONSOLE_METHODS = ["log", "info", "warn", "error", "debug"] as const;

function spyOnConsole() {
  return CONSOLE_METHODS.map((method) => vi.spyOn(console, method));
}

/** 点第一个 key（`API_KEY`）的「Rotate」，再在确认框里确认。 */
async function rotateFirstKey(user: User) {
  const first = screen.getAllByText("Rotate")[0]?.closest("button");
  if (first === null || first === undefined) {
    throw new Error("no Rotate button");
  }
  await user.click(first);
  await user.click(buttonWithText("Rotate", dialog()));
}

beforeEach(() => {
  vi.clearAllMocks();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("IntegrationAccessPanel", { timeout: 15_000 }, () => {
  it("shows a loading state while the list is on its way", () => {
    api.listCredentials.mockReturnValue(new Promise(() => undefined));

    renderPanel();

    expect(screen.getByText("Loading credentials…")).toBeInTheDocument();
  });

  it("shows the backend message and the request id when the list fails", async () => {
    api.listCredentials.mockRejectedValue(
      new CredentialError("PROJECT_NOT_FOUND", "The project does not exist.", "req-404", 404),
    );

    renderPanel();

    expect(await screen.findByText("The credentials could not be loaded.")).toBeInTheDocument();
    expect(screen.getByText(/The project does not exist\./)).toBeInTheDocument();
    expect(screen.getByText("req-404")).toBeInTheDocument();
  });

  it("says so when the project has no credentials, and still offers to create one", async () => {
    api.listCredentials.mockResolvedValue(page([]));

    renderPanel();

    expect(await screen.findByText("This project has no API credentials yet.")).toBeInTheDocument();
    expect(buttonWithText("New API key")).toBeEnabled();
  });

  it("groups the versions by api key and shows every field", async () => {
    api.listCredentials.mockResolvedValue(
      page([
        version({ key_version: 1, valid_until: "2026-10-02T08:30:00" }),
        version({
          key_version: 2,
          status: "REVOKED",
          verifiable: false,
          revoked_at: "2026-09-26T20:00:00",
        }),
        version({ api_key: OTHER_KEY }),
      ]),
    );

    renderPanel();

    expect(await screen.findByText(API_KEY)).toBeInTheDocument();
    expect(screen.getByText(OTHER_KEY)).toBeInTheDocument();
    expect(screen.getByText("3 credential versions")).toBeInTheDocument();
    expect(api.listCredentials).toHaveBeenCalledWith(
      CUSTOMER_ID,
      PROJECT_ID,
      1,
      20,
      expect.anything(),
    );

    const tables = screen.getAllByRole("table");
    expect(tables).toHaveLength(2);
    const [first, second] = tables.map((table) => within(table).getAllByRole("row").slice(1));
    expect(first).toHaveLength(2);
    expect(second).toHaveLength(1);

    // 时间按吉隆坡时间显示；UTC 20:00 已经是第二天。
    expect(first?.[0]).toHaveTextContent("Active");
    expect(first?.[0]).toHaveTextContent("Yes");
    expect(first?.[0]).toHaveTextContent("2026-09-25 16:30:00");
    expect(first?.[0]).toHaveTextContent("2026-10-02 16:30:00");
    expect(first?.[1]).toHaveTextContent("Revoked");
    expect(first?.[1]).toHaveTextContent("No");
    expect(first?.[1]).toHaveTextContent("No end date");
    expect(first?.[1]).toHaveTextContent("2026-09-27 04:00:00");
    expect(second?.[0]).toHaveTextContent("—");

    // 已吊销的版本不能再吊销（终态）。
    expect(buttonWithText("Revoke v2")).toBeDisabled();
    expect(within(tables[0] as HTMLElement).getByText("Revoke v1").closest("button")).toBeEnabled();
  });

  it("shows a new secret only in the result dialog, and forgets it when the dialog closes", async () => {
    const user = userEvent.setup();
    const consoleSpies = spyOnConsole();
    api.listCredentials
      .mockResolvedValueOnce(page([]))
      .mockResolvedValue(page([version()]));
    api.createCredential.mockResolvedValue(issued());
    const { queryClient } = renderPanel();

    await screen.findByText("This project has no API credentials yet.");
    await user.click(buttonWithText("New API key"));

    const shown = await screen.findByText(SECRET);
    expect(within(dialog()).getByText(SECRET)).toBe(shown);
    expect(screen.getAllByText(SECRET)).toHaveLength(1);
    expect(within(dialog()).getByText(WARNING)).toBeInTheDocument();
    expect(buttonWithText("Copy secret", dialog())).toBeInTheDocument();
    expect(api.createCredential).toHaveBeenCalledTimes(1);
    expect(api.createCredential).toHaveBeenCalledWith(CUSTOMER_ID, PROJECT_ID);

    // 列表重读了一次，但读回来的是没有 secret 的列表；查询缓存里从头到尾没有它。
    await waitFor(() => {
      expect(api.listCredentials).toHaveBeenCalledTimes(2);
    });
    expect(queryCacheText(queryClient)).not.toContain(SECRET);

    await user.click(buttonWithText("I have saved the secret, close"));

    await waitFor(() => {
      expect(screen.queryByText(SECRET)).not.toBeInTheDocument();
    });
    expect(screen.queryByText(WARNING)).not.toBeInTheDocument();
    // mutation 的 gcTime 是 0：reset 之后 mutation 缓存里的那份也被丢掉。
    await waitFor(() => {
      expect(cachedText(queryClient)).not.toContain(SECRET);
    });
    expect(storedText()).not.toContain(SECRET);
    expect(window.location.href).not.toContain(SECRET);
    for (const spy of consoleSpies) {
      expect(JSON.stringify(spy.mock.calls)).not.toContain(SECRET);
    }
  });

  it("copies the secret to the clipboard", async () => {
    const user = userEvent.setup();
    api.listCredentials.mockResolvedValue(page([]));
    api.createCredential.mockResolvedValue(issued());
    renderPanel();

    await screen.findByText("This project has no API credentials yet.");
    await user.click(buttonWithText("New API key"));
    await screen.findByText(SECRET);
    await user.click(buttonWithText("Copy secret", dialog()));

    expect(await screen.findByText("Copied.")).toBeInTheDocument();
    await expect(navigator.clipboard.readText()).resolves.toBe(SECRET);
  });

  it("creates only one key on a double click", async () => {
    const user = userEvent.setup();
    const pending = deferred<IssuedCredential>();
    api.listCredentials.mockResolvedValue(page([]));
    api.createCredential.mockReturnValue(pending.promise);
    renderPanel();

    await screen.findByText("This project has no API credentials yet.");
    await user.dblClick(buttonWithText("New API key"));

    await waitFor(() => {
      expect(buttonWithText("New API key")).toBeDisabled();
    });
    pending.resolve(issued());

    expect(await screen.findByText(SECRET)).toBeInTheDocument();
    expect(api.createCredential).toHaveBeenCalledTimes(1);
  });

  it("rotates with the newest version of that key in the list and shows the new secret once", async () => {
    const user = userEvent.setup();
    api.listCredentials.mockResolvedValue(page([...threeVersions(), version({ api_key: OTHER_KEY })]));
    api.rotateCredential.mockResolvedValue(issued({ key_version: 4 }));
    const { queryClient } = renderPanel();

    await screen.findByText(API_KEY);
    await rotateFirstKey(user);

    expect(await screen.findByText(SECRET)).toBeInTheDocument();
    expect(api.rotateCredential).toHaveBeenCalledTimes(1);
    expect(api.rotateCredential).toHaveBeenCalledWith(CUSTOMER_ID, PROJECT_ID, API_KEY, 3);
    expect(queryCacheText(queryClient)).not.toContain(SECRET);

    await user.click(buttonWithText("I have saved the secret, close"));
    await waitFor(() => {
      expect(screen.queryByText(SECRET)).not.toBeInTheDocument();
    });
    await waitFor(() => {
      expect(cachedText(queryClient)).not.toContain(SECRET);
    });
  });

  it("does not rotate when the confirmation is cancelled", async () => {
    const user = userEvent.setup();
    api.listCredentials.mockResolvedValue(page(threeVersions()));
    renderPanel();

    await screen.findByText(API_KEY);
    await user.click(buttonWithText("Rotate"));
    await user.click(buttonWithText("Cancel", dialog()));

    expect(api.rotateCredential).not.toHaveBeenCalled();
  });

  it("says someone else rotated first and refreshes the list after a 409 conflict", async () => {
    const user = userEvent.setup();
    api.listCredentials
      .mockResolvedValueOnce(page(threeVersions()))
      .mockResolvedValue(page([...threeVersions(), version({ key_version: 4 })]));
    api.rotateCredential.mockRejectedValue(
      new CredentialError(
        "CREDENTIAL_VERSION_CONFLICT",
        "The key version is not the newest.",
        "req-409",
        409,
      ),
    );
    renderPanel();

    await screen.findByText(API_KEY);
    await rotateFirstKey(user);

    expect(await screen.findByText(CONFLICT)).toBeInTheDocument();
    expect(screen.getByText("req-409")).toBeInTheDocument();
    await waitFor(() => {
      expect(api.listCredentials).toHaveBeenCalledTimes(2);
    });
    expect(await screen.findByText("Revoke v4")).toBeInTheDocument();
    expect(api.rotateCredential).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["CREDENTIAL_REVOKED", 409, "Every version of this key has been revoked."],
    ["ENCRYPTION_NOT_CONFIGURED", 503, "Encryption is not configured."],
  ])("shows the backend message and request id for %s", async (code, status, message) => {
    const user = userEvent.setup();
    api.listCredentials.mockResolvedValue(page(threeVersions()));
    api.rotateCredential.mockRejectedValue(
      new CredentialError(code, message, `req-${String(status)}`, status),
    );
    renderPanel();

    await screen.findByText(API_KEY);
    await rotateFirstKey(user);

    expect(await screen.findByText("The API key could not be rotated.")).toBeInTheDocument();
    expect(screen.getByText(new RegExp(message.replace(/\./g, "\\.")))).toBeInTheDocument();
    expect(screen.getByText(`req-${String(status)}`)).toBeInTheDocument();
    expect(screen.queryByText(UNKNOWN)).not.toBeInTheDocument();
    expect(screen.queryByText(CONFLICT)).not.toBeInTheDocument();
  });

  it.each([
    ["a network error", new CredentialError("NETWORK_ERROR", "Could not reach the billing platform.", null, null)],
    ["a 500", new CredentialError("INTERNAL_ERROR", "An unexpected error occurred.", "req-500", 500)],
  ])("explains how to recover after %s and does not retry", async (_name, error) => {
    const user = userEvent.setup();
    api.listCredentials.mockResolvedValue(page(threeVersions()));
    api.rotateCredential.mockRejectedValue(error);
    renderPanel();

    await screen.findByText(API_KEY);
    await rotateFirstKey(user);

    expect(await screen.findByText(UNKNOWN)).toBeInTheDocument();
    expect(screen.getByText(/rotate again or revoke that version/)).toBeInTheDocument();
    expect(screen.getByText(new RegExp(error.message.replace(/\./g, "\\.")))).toBeInTheDocument();
    // 结果未知时重读列表，让管理员看得到可能已经入库的新版本；但不自动重发。
    await waitFor(() => {
      expect(api.listCredentials).toHaveBeenCalledTimes(2);
    });
    expect(api.rotateCredential).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(SECRET)).not.toBeInTheDocument();
  });

  it("explains how to recover when creating a key ends with an unknown result", async () => {
    const user = userEvent.setup();
    api.listCredentials.mockResolvedValue(page([]));
    api.createCredential.mockRejectedValue(
      new CredentialError("INTERNAL_ERROR", "An unexpected error occurred.", "req-500", 500),
    );
    renderPanel();

    await screen.findByText("This project has no API credentials yet.");
    await user.click(buttonWithText("New API key"));

    expect(await screen.findByText(UNKNOWN)).toBeInTheDocument();
    expect(screen.getByText(/revoke that key and create another one/)).toBeInTheDocument();
    expect(screen.getByText("req-500")).toBeInTheDocument();
    expect(api.createCredential).toHaveBeenCalledTimes(1);
  });

  it("asks for a reason before revoking a version and says revoking is permanent", async () => {
    const user = userEvent.setup();
    api.listCredentials.mockResolvedValue(page(threeVersions()));
    api.revokeCredentialVersion.mockResolvedValue(version({ key_version: 2, status: "REVOKED" }));
    renderPanel();

    await screen.findByText(API_KEY);
    await user.click(buttonWithText("Revoke v2"));

    expect(within(dialog()).getByText("Revoke version 2?")).toBeInTheDocument();
    expect(within(dialog()).getByText(/Revoking is permanent\./)).toBeInTheDocument();
    expect(within(dialog()).getByText(/Do not include personal data/)).toBeInTheDocument();

    // 没填原因。
    await user.click(buttonWithText("Revoke permanently", dialog()));
    expect(await screen.findByText("Enter the reason.")).toBeInTheDocument();

    // 只有空白。
    await user.click(screen.getByLabelText("Reason"));
    await user.paste("   ");
    await user.click(buttonWithText("Revoke permanently", dialog()));
    expect(await screen.findByText("Enter the reason.")).toBeInTheDocument();

    // 去掉首尾空白后超过 255。
    await user.clear(screen.getByLabelText("Reason"));
    await user.paste(` ${"r".repeat(256)} `);
    await user.click(buttonWithText("Revoke permanently", dialog()));
    expect(
      await screen.findByText("The reason can be at most 255 characters."),
    ).toBeInTheDocument();
    expect(api.revokeCredentialVersion).not.toHaveBeenCalled();

    // 恰好 255，带首尾空白：发出去的是去掉空白之后的。
    await user.clear(screen.getByLabelText("Reason"));
    await user.paste(` ${"r".repeat(255)} `);
    await user.click(buttonWithText("Revoke permanently", dialog()));

    expect(await screen.findByText("Version revoked.")).toBeInTheDocument();
    expect(api.revokeCredentialVersion).toHaveBeenCalledTimes(1);
    expect(api.revokeCredentialVersion).toHaveBeenCalledWith(
      CUSTOMER_ID,
      PROJECT_ID,
      API_KEY,
      2,
      "r".repeat(255),
    );
    await waitFor(() => {
      expect(api.listCredentials).toHaveBeenCalledTimes(2);
    });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("revokes the whole key after a second confirmation with a trimmed reason", async () => {
    const user = userEvent.setup();
    api.listCredentials.mockResolvedValue(page(threeVersions()));
    api.revokeCredential.mockResolvedValue(
      threeVersions().map((each) => ({ ...each, status: "REVOKED" as const })),
    );
    renderPanel();

    await screen.findByText(API_KEY);
    await user.click(buttonWithText("Revoke key"));

    expect(
      within(dialog()).getByText("Revoke every version of this API key?"),
    ).toBeInTheDocument();
    expect(within(dialog()).getByText(/Revoking is permanent\./)).toBeInTheDocument();
    expect(api.revokeCredential).not.toHaveBeenCalled();

    await user.click(screen.getByLabelText("Reason"));
    await user.paste("  Project retired  ");
    await user.click(buttonWithText("Revoke permanently", dialog()));

    expect(await screen.findByText("API key revoked.")).toBeInTheDocument();
    expect(api.revokeCredential).toHaveBeenCalledWith(
      CUSTOMER_ID,
      PROJECT_ID,
      API_KEY,
      "Project retired",
    );
    expect(api.revokeCredentialVersion).not.toHaveBeenCalled();
  });

  it("keeps the revoke dialog open with the backend message when revoking fails", async () => {
    const user = userEvent.setup();
    api.listCredentials.mockResolvedValue(page(threeVersions()));
    api.revokeCredential.mockRejectedValue(
      new CredentialError("CREDENTIAL_NOT_FOUND", "The credential does not exist.", "req-404", 404),
    );
    renderPanel();

    await screen.findByText(API_KEY);
    await user.click(buttonWithText("Revoke key"));
    await user.click(screen.getByLabelText("Reason"));
    await user.paste("Project retired");
    await user.click(buttonWithText("Revoke permanently", dialog()));

    expect(await within(dialog()).findByText(/The credential does not exist\./)).toBeInTheDocument();
    expect(within(dialog()).getByText("req-404")).toBeInTheDocument();
    expect(screen.getByLabelText("Reason")).toHaveValue("Project retired");
  });

  it("does not revoke when the confirmation is cancelled", async () => {
    const user = userEvent.setup();
    api.listCredentials.mockResolvedValue(page(threeVersions()));
    renderPanel();

    await screen.findByText(API_KEY);
    await user.click(buttonWithText("Revoke key"));
    await user.click(buttonWithText("Cancel", dialog()));

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(api.revokeCredential).not.toHaveBeenCalled();
  });
});

describe("groupByApiKey / newestVersion", () => {
  it("keeps the backend's order and groups adjacent versions of the same key", () => {
    const groups = groupByApiKey([
      version({ key_version: 1 }),
      version({ key_version: 2 }),
      version({ api_key: OTHER_KEY, key_version: 1 }),
    ]);

    expect(groups.map((group) => group.apiKey)).toEqual([API_KEY, OTHER_KEY]);
    expect(groups[0]?.versions.map((each) => each.key_version)).toEqual([1, 2]);
  });

  it("takes the largest version, not the last one", () => {
    expect(
      newestVersion({
        apiKey: API_KEY,
        versions: [version({ key_version: 5 }), version({ key_version: 2 })],
      }),
    ).toBe(5);
  });
});
