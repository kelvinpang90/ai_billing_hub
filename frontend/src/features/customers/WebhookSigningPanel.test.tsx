/**
 * 项目的出站 webhook 签名密钥面板：三种状态、secret 只显示一次、签发 / 启用 / 退役的二次确认、
 * 成功后重读、三个错误码的文案、结果未知时的恢复提示、退役原因的校验。
 *
 * ⚠️ 这里最要紧的是 `secret` 去了哪：只能出现在结果对话框里，关掉就从 DOM 消失，查询缓存、
 * mutation 缓存、浏览器存储、控制台、地址栏里都不能有它。这几处漏了都不会报错。
 *
 * 示例值一律是 docs/api.md 的全零占位值（`whs_` 加 64 个 0），uuid 也是全零。
 * 输入一律 click + paste：逐字 `user.type` 每按一键 antd 表单就重渲染一次，全量并行时会超时。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import type { Page, Project } from "../../api/adminCustomers";
import {
  WebhookSigningError,
  type IssuedWebhookSecret,
  type WebhookSecret,
} from "../../api/webhookSigning";
import { WebhookSigningPanel } from "./WebhookSigningPanel";

const CUSTOMER_ID = "00000000-0000-0000-0000-000000000000";
const PROJECT_ID = "00000000-0000-0000-0000-000000000000";
const SECRET = `whs_${"0".repeat(64)}`;

const WARNING =
  "This secret is shown only once. After you close this dialog it cannot be viewed again. Copy it and store it safely now.";
const EMPTY = "This project has no webhook signing secrets yet.";
const PENDING_EXISTS =
  "This project already has a pending version. Activate or retire it before issuing another one.";
const NOT_PENDING = "This version has been retired and can no longer be activated.";
const ENCRYPTION =
  "Encryption is not configured on the server, so no signing secret can be issued.";
const UNKNOWN = "We could not confirm whether a new signing secret was issued.";
const INTEGRATION_READY =
  "Activate only after the integration has been configured to accept both the old and the new version. Otherwise its webhooks fail signature verification.";
const NO_SIGNING_SECRET =
  "This is the active version. After retiring it the project has no signing secret, and webhooks wait until a new version is activated.";

const api = vi.hoisted(() => ({
  listWebhookSecrets: vi.fn(),
  issueWebhookSecret: vi.fn(),
  activateWebhookSecret: vi.fn(),
  retireWebhookSecret: vi.fn(),
}));

vi.mock("../../api/webhookSigning", async () => {
  const actual = await vi.importActual<typeof import("../../api/webhookSigning")>(
    "../../api/webhookSigning",
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

function version(overrides: Partial<WebhookSecret> = {}): WebhookSecret {
  return {
    key_version: 1,
    status: "PENDING",
    created_at: "2026-09-28T08:30:00",
    activated_at: null,
    retired_at: null,
    ...overrides,
  };
}

function issued(overrides: Partial<WebhookSecret> = {}): IssuedWebhookSecret {
  return { ...version(overrides), secret: SECRET };
}

function page(items: WebhookSecret[], total = items.length): Page<WebhookSecret> {
  return { items, page: 1, page_size: 20, total };
}

/** v1 已退役、v2 正在签名、v3 待启用：三种状态各一个。 */
function threeVersions(): WebhookSecret[] {
  return [
    version({
      key_version: 1,
      status: "RETIRED",
      activated_at: "2026-09-28T09:00:00",
      retired_at: "2026-09-29T09:00:00",
    }),
    version({
      key_version: 2,
      status: "ACTIVE",
      created_at: "2026-09-29T08:30:00",
      activated_at: "2026-09-29T09:00:00",
    }),
    version({ key_version: 3, created_at: "2026-09-30T20:00:00" }),
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
      <WebhookSigningPanel customerId={CUSTOMER_ID} project={PROJECT} />
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

/** 点「Issue new secret」，再在确认框里确认。 */
async function issueSecret(user: User) {
  await user.click(buttonWithText("Issue new secret"));
  await user.click(buttonWithText("Issue secret", dialog()));
}

/** 点某个版本的「Activate」，再在确认框里确认。 */
async function activateVersion(user: User, keyVersion: number) {
  await user.click(buttonWithText(`Activate v${String(keyVersion)}`));
  await user.click(buttonWithText("Activate", dialog()));
}

function escaped(text: string): RegExp {
  return new RegExp(text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
}

beforeEach(() => {
  vi.clearAllMocks();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("WebhookSigningPanel", { timeout: 20_000 }, () => {
  it("shows a loading state while the list is on its way", () => {
    api.listWebhookSecrets.mockReturnValue(new Promise(() => undefined));

    renderPanel();

    expect(screen.getByText("Loading webhook signing secrets…")).toBeInTheDocument();
  });

  it("shows the backend message and the request id when the list fails", async () => {
    api.listWebhookSecrets.mockRejectedValue(
      new WebhookSigningError("PROJECT_NOT_FOUND", "The project does not exist.", "req-404", 404),
    );

    renderPanel();

    expect(
      await screen.findByText("The webhook signing secrets could not be loaded."),
    ).toBeInTheDocument();
    expect(screen.getByText(/The project does not exist\./)).toBeInTheDocument();
    expect(screen.getByText("req-404")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("says so when the project has no signing secrets, and still offers to issue one", async () => {
    api.listWebhookSecrets.mockResolvedValue(page([]));

    renderPanel();

    expect(await screen.findByText(EMPTY)).toBeInTheDocument();
    expect(buttonWithText("Issue new secret")).toBeEnabled();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("lists the versions in the backend's order with every field", async () => {
    // 故意不按版本排：前端不重排，照后端给的顺序画。
    const [retired, active, pending] = threeVersions() as [WebhookSecret, WebhookSecret, WebhookSecret];
    api.listWebhookSecrets.mockResolvedValue(page([pending, retired, active]));

    renderPanel();

    expect(await screen.findByText("3 signing versions")).toBeInTheDocument();
    expect(api.listWebhookSecrets).toHaveBeenCalledWith(
      CUSTOMER_ID,
      PROJECT_ID,
      1,
      20,
      expect.anything(),
    );
    const region = screen.getByRole("region", { name: "Webhook signing secrets" });
    const rows = within(region).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(3);

    // 时间按吉隆坡时间显示；UTC 20:00 已经是第二天。没发生的时间是一道横线。
    expect(rows[0]).toHaveTextContent(/^3Pending/);
    expect(rows[0]).toHaveTextContent("2026-10-01 04:00:00");
    expect(rows[0]).toHaveTextContent("——");
    expect(rows[1]).toHaveTextContent(/^1Retired/);
    expect(rows[1]).toHaveTextContent("2026-09-28 16:30:00");
    expect(rows[1]).toHaveTextContent("2026-09-28 17:00:00");
    expect(rows[1]).toHaveTextContent("2026-09-29 17:00:00");
    expect(rows[2]).toHaveTextContent(/^2Active/);
    expect(rows[2]).toHaveTextContent("2026-09-29 17:00:00");
    expect(rows[2]).toHaveTextContent("—");

    // 只有 PENDING 能启用；RETIRED 是终态，不能再退役。
    expect(buttonWithText("Activate v3")).toBeEnabled();
    expect(buttonWithText("Activate v2")).toBeDisabled();
    expect(buttonWithText("Activate v1")).toBeDisabled();
    expect(buttonWithText("Retire v3")).toBeEnabled();
    expect(buttonWithText("Retire v2")).toBeEnabled();
    expect(buttonWithText("Retire v1")).toBeDisabled();
  });

  it("shows a new secret only in the result dialog, and forgets it when the dialog closes", async () => {
    const user = userEvent.setup();
    const consoleSpies = spyOnConsole();
    api.listWebhookSecrets.mockResolvedValueOnce(page([])).mockResolvedValue(page([version()]));
    api.issueWebhookSecret.mockResolvedValue(issued());
    const { queryClient } = renderPanel();

    await screen.findByText(EMPTY);
    await user.click(buttonWithText("Issue new secret"));
    // 先确认：没确认之前什么都没发。
    expect(within(dialog()).getByText("Issue a new webhook signing secret?")).toBeInTheDocument();
    expect(api.issueWebhookSecret).not.toHaveBeenCalled();
    await user.click(buttonWithText("Issue secret", dialog()));

    const shown = await screen.findByText(SECRET);
    expect(within(dialog()).getByText(SECRET)).toBe(shown);
    expect(screen.getAllByText(SECRET)).toHaveLength(1);
    expect(within(dialog()).getByText(WARNING)).toBeInTheDocument();
    expect(buttonWithText("Copy secret", dialog())).toBeInTheDocument();
    expect(api.issueWebhookSecret).toHaveBeenCalledTimes(1);
    expect(api.issueWebhookSecret).toHaveBeenCalledWith(CUSTOMER_ID, PROJECT_ID);

    // 列表重读了一次，但读回来的是没有 secret 的列表；查询缓存里从头到尾没有它。
    await waitFor(() => {
      expect(api.listWebhookSecrets).toHaveBeenCalledTimes(2);
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
    api.listWebhookSecrets.mockResolvedValue(page([]));
    api.issueWebhookSecret.mockResolvedValue(issued());
    renderPanel();

    await screen.findByText(EMPTY);
    await issueSecret(user);
    await screen.findByText(SECRET);
    await user.click(buttonWithText("Copy secret", dialog()));

    expect(await screen.findByText("Copied.")).toBeInTheDocument();
    await expect(navigator.clipboard.readText()).resolves.toBe(SECRET);
  });

  it("does not issue when the confirmation is cancelled", async () => {
    const user = userEvent.setup();
    api.listWebhookSecrets.mockResolvedValue(page([]));
    renderPanel();

    await screen.findByText(EMPTY);
    await user.click(buttonWithText("Issue new secret"));
    await user.click(buttonWithText("Cancel", dialog()));

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(api.issueWebhookSecret).not.toHaveBeenCalled();
  });

  it("disables the buttons while issuing and issues only once on a double click", async () => {
    const user = userEvent.setup();
    const pending = deferred<IssuedWebhookSecret>();
    api.listWebhookSecrets.mockResolvedValue(page([]));
    api.issueWebhookSecret.mockReturnValue(pending.promise);
    renderPanel();

    await screen.findByText(EMPTY);
    await user.click(buttonWithText("Issue new secret"));
    await user.dblClick(buttonWithText("Issue secret", dialog()));

    await waitFor(() => {
      expect(buttonWithText("Issue secret", dialog())).toBeDisabled();
    });
    expect(buttonWithText("Cancel", dialog())).toBeDisabled();
    expect(buttonWithText("Issue new secret")).toBeDisabled();
    pending.resolve(issued());

    expect(await screen.findByText(SECRET)).toBeInTheDocument();
    expect(api.issueWebhookSecret).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["WEBHOOK_SECRET_PENDING_EXISTS", 409, PENDING_EXISTS],
    ["ENCRYPTION_NOT_CONFIGURED", 503, ENCRYPTION],
  ])("explains %s with the request id and refreshes the list", async (code, status, text) => {
    const user = userEvent.setup();
    api.listWebhookSecrets.mockResolvedValue(page([version()]));
    api.issueWebhookSecret.mockRejectedValue(
      new WebhookSigningError(code, "Backend says no.", `req-${String(status)}`, status),
    );
    renderPanel();

    await screen.findByText("1 signing versions");
    await issueSecret(user);

    expect(await screen.findByText(text)).toBeInTheDocument();
    expect(screen.getByText(`req-${String(status)}`)).toBeInTheDocument();
    await waitFor(() => {
      expect(api.listWebhookSecrets).toHaveBeenCalledTimes(2);
    });
    expect(api.issueWebhookSecret).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(UNKNOWN)).not.toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("falls back to the backend message for a code without its own text", async () => {
    const user = userEvent.setup();
    api.listWebhookSecrets.mockResolvedValue(page([]));
    api.issueWebhookSecret.mockRejectedValue(
      new WebhookSigningError("PROJECT_NOT_FOUND", "The project does not exist.", "req-404", 404),
    );
    renderPanel();

    await screen.findByText(EMPTY);
    await issueSecret(user);

    expect(await screen.findByText("The signing secret could not be issued.")).toBeInTheDocument();
    expect(screen.getByText(/The project does not exist\./)).toBeInTheDocument();
    expect(screen.getByText("req-404")).toBeInTheDocument();
    expect(screen.queryByText(UNKNOWN)).not.toBeInTheDocument();
  });

  it.each([
    [
      "a network error",
      new WebhookSigningError("NETWORK_ERROR", "Could not reach the billing platform.", null, null),
    ],
    [
      "a 500",
      new WebhookSigningError("INTERNAL_ERROR", "An unexpected error occurred.", "req-500", 500),
    ],
  ])("explains how to recover after %s and does not retry", async (_name, error) => {
    const user = userEvent.setup();
    api.listWebhookSecrets.mockResolvedValue(page([]));
    api.issueWebhookSecret.mockRejectedValue(error);
    renderPanel();

    await screen.findByText(EMPTY);
    await issueSecret(user);

    expect(await screen.findByText(UNKNOWN)).toBeInTheDocument();
    expect(
      screen.getByText(/Retire that pending version and issue another one\./),
    ).toBeInTheDocument();
    expect(screen.getByText(escaped(error.message))).toBeInTheDocument();
    // 结果未知时重读列表，让管理员看得到可能已经入库的 PENDING；但不自动重发。
    await waitFor(() => {
      expect(api.listWebhookSecrets).toHaveBeenCalledTimes(2);
    });
    expect(api.issueWebhookSecret).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(SECRET)).not.toBeInTheDocument();
  });

  it("activates after a confirmation that says the active version is retired at once", async () => {
    const user = userEvent.setup();
    const [retired, active, pending] = threeVersions() as [WebhookSecret, WebhookSecret, WebhookSecret];
    api.listWebhookSecrets
      .mockResolvedValueOnce(page([retired, active, pending]))
      .mockResolvedValue(
        page([
          retired,
          { ...active, status: "RETIRED", retired_at: "2026-10-01T09:00:00" },
          { ...pending, status: "ACTIVE", activated_at: "2026-10-01T09:00:00" },
        ]),
      );
    api.activateWebhookSecret.mockResolvedValue([]);
    renderPanel();

    await screen.findByText("3 signing versions");
    await user.click(buttonWithText("Activate v3"));

    expect(within(dialog()).getByText("Activate version 3?")).toBeInTheDocument();
    expect(
      within(dialog()).getByText(
        "Version 2 is active now. It is retired at the same moment, and the platform signs with version 3 from then on.",
      ),
    ).toBeInTheDocument();
    expect(within(dialog()).getByText(INTEGRATION_READY)).toBeInTheDocument();
    expect(api.activateWebhookSecret).not.toHaveBeenCalled();

    await user.click(buttonWithText("Activate", dialog()));

    expect(await screen.findByText("Version activated.")).toBeInTheDocument();
    expect(api.activateWebhookSecret).toHaveBeenCalledTimes(1);
    expect(api.activateWebhookSecret).toHaveBeenCalledWith(CUSTOMER_ID, PROJECT_ID, 3);
    // 列表是重新读回来的，不是前端拼的。
    await waitFor(() => {
      expect(api.listWebhookSecrets).toHaveBeenCalledTimes(2);
    });
    await waitFor(() => {
      expect(buttonWithText("Activate v3")).toBeDisabled();
    });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("still warns about the active version when it is not on this page", async () => {
    const user = userEvent.setup();
    api.listWebhookSecrets.mockResolvedValue(page([version({ key_version: 4 })], 25));
    renderPanel();

    await screen.findByText("25 signing versions");
    await user.click(buttonWithText("Activate v4"));

    expect(
      within(dialog()).getByText(
        "The version that is active now, if any, is retired at the same moment, and the platform signs with this version from then on.",
      ),
    ).toBeInTheDocument();
    expect(within(dialog()).getByText(INTEGRATION_READY)).toBeInTheDocument();
  });

  it("does not activate when the confirmation is cancelled", async () => {
    const user = userEvent.setup();
    api.listWebhookSecrets.mockResolvedValue(page(threeVersions()));
    renderPanel();

    await screen.findByText("3 signing versions");
    await user.click(buttonWithText("Activate v3"));
    await user.click(buttonWithText("Cancel", dialog()));

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(api.activateWebhookSecret).not.toHaveBeenCalled();
  });

  it("explains WEBHOOK_SECRET_NOT_PENDING with the request id and refreshes the list", async () => {
    const user = userEvent.setup();
    api.listWebhookSecrets
      .mockResolvedValueOnce(page(threeVersions()))
      .mockResolvedValue(
        page(threeVersions().map((each) => ({ ...each, status: "RETIRED" as const }))),
      );
    api.activateWebhookSecret.mockRejectedValue(
      new WebhookSigningError(
        "WEBHOOK_SECRET_NOT_PENDING",
        "The version is not pending.",
        "req-409",
        409,
      ),
    );
    renderPanel();

    await screen.findByText("3 signing versions");
    await activateVersion(user, 3);

    expect(await screen.findByText(NOT_PENDING)).toBeInTheDocument();
    expect(screen.getByText("req-409")).toBeInTheDocument();
    await waitFor(() => {
      expect(api.listWebhookSecrets).toHaveBeenCalledTimes(2);
    });
    await waitFor(() => {
      expect(buttonWithText("Activate v3")).toBeDisabled();
    });
    expect(api.activateWebhookSecret).toHaveBeenCalledTimes(1);
  });

  it("asks for a reason before retiring and checks it the way the backend does", async () => {
    const user = userEvent.setup();
    api.listWebhookSecrets.mockResolvedValue(page(threeVersions()));
    api.retireWebhookSecret.mockResolvedValue(
      version({ key_version: 3, status: "RETIRED", retired_at: "2026-10-01T09:00:00" }),
    );
    renderPanel();

    await screen.findByText("3 signing versions");
    await user.click(buttonWithText("Retire v3"));

    expect(within(dialog()).getByText("Retire version 3?")).toBeInTheDocument();
    expect(within(dialog()).getByText(/Retiring is permanent\./)).toBeInTheDocument();
    expect(within(dialog()).getByText(/Do not include personal data/)).toBeInTheDocument();
    // 退役的是 PENDING：项目照样有签名密钥，不说「没有签名密钥」。
    expect(within(dialog()).queryByText(NO_SIGNING_SECRET)).not.toBeInTheDocument();

    // 没填原因。
    await user.click(buttonWithText("Retire permanently", dialog()));
    expect(await screen.findByText("Enter the reason.")).toBeInTheDocument();

    // 只有空白。
    await user.click(screen.getByLabelText("Reason"));
    await user.paste("   ");
    await user.click(buttonWithText("Retire permanently", dialog()));
    expect(await screen.findByText("Enter the reason.")).toBeInTheDocument();

    // 去掉首尾空白后超过 255。
    await user.clear(screen.getByLabelText("Reason"));
    await user.paste(` ${"r".repeat(256)} `);
    await user.click(buttonWithText("Retire permanently", dialog()));
    expect(
      await screen.findByText("The reason can be at most 255 characters."),
    ).toBeInTheDocument();
    expect(api.retireWebhookSecret).not.toHaveBeenCalled();

    // 恰好 255，带首尾空白：发出去的是去掉空白之后的。
    await user.clear(screen.getByLabelText("Reason"));
    await user.paste(` ${"r".repeat(255)} `);
    await user.click(buttonWithText("Retire permanently", dialog()));

    expect(await screen.findByText("Version retired.")).toBeInTheDocument();
    expect(api.retireWebhookSecret).toHaveBeenCalledTimes(1);
    expect(api.retireWebhookSecret).toHaveBeenCalledWith(
      CUSTOMER_ID,
      PROJECT_ID,
      3,
      "r".repeat(255),
    );
    await waitFor(() => {
      expect(api.listWebhookSecrets).toHaveBeenCalledTimes(2);
    });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("says the project is left without a signing secret when retiring the active version", async () => {
    const user = userEvent.setup();
    api.listWebhookSecrets.mockResolvedValue(page(threeVersions()));
    api.retireWebhookSecret.mockResolvedValue(
      version({ key_version: 2, status: "RETIRED", retired_at: "2026-10-01T09:00:00" }),
    );
    renderPanel();

    await screen.findByText("3 signing versions");
    await user.click(buttonWithText("Retire v2"));

    expect(within(dialog()).getByText(NO_SIGNING_SECRET)).toBeInTheDocument();
    expect(api.retireWebhookSecret).not.toHaveBeenCalled();

    await user.click(screen.getByLabelText("Reason"));
    await user.paste("  Leaked in a support ticket  ");
    await user.click(buttonWithText("Retire permanently", dialog()));

    expect(await screen.findByText("Version retired.")).toBeInTheDocument();
    expect(api.retireWebhookSecret).toHaveBeenCalledWith(
      CUSTOMER_ID,
      PROJECT_ID,
      2,
      "Leaked in a support ticket",
    );
    await waitFor(() => {
      expect(api.listWebhookSecrets).toHaveBeenCalledTimes(2);
    });
  });

  it("keeps the retire dialog open with the backend message when retiring fails", async () => {
    const user = userEvent.setup();
    api.listWebhookSecrets.mockResolvedValue(page(threeVersions()));
    api.retireWebhookSecret.mockRejectedValue(
      new WebhookSigningError(
        "WEBHOOK_SECRET_NOT_FOUND",
        "The signing version does not exist.",
        "req-404",
        404,
      ),
    );
    renderPanel();

    await screen.findByText("3 signing versions");
    await user.click(buttonWithText("Retire v3"));
    await user.click(screen.getByLabelText("Reason"));
    await user.paste("Not needed");
    await user.click(buttonWithText("Retire permanently", dialog()));

    expect(
      await within(dialog()).findByText(/The signing version does not exist\./),
    ).toBeInTheDocument();
    expect(within(dialog()).getByText("req-404")).toBeInTheDocument();
    expect(screen.getByLabelText("Reason")).toHaveValue("Not needed");
  });

  it("does not retire when the confirmation is cancelled", async () => {
    const user = userEvent.setup();
    api.listWebhookSecrets.mockResolvedValue(page(threeVersions()));
    renderPanel();

    await screen.findByText("3 signing versions");
    await user.click(buttonWithText("Retire v2"));
    await user.click(buttonWithText("Cancel", dialog()));

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(api.retireWebhookSecret).not.toHaveBeenCalled();
  });
});
