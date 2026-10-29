/**
 * 门户通知页与导航上的未读数（AIH-TASK-128，设计闸门 #471 v7 §2「前端」）。
 *
 * 与其他门户页的测试同一写法：不 mock `api/customerNotifications`，而是在 `client.get` / `client.post` 上按路径回应，
 * 钉住**页面真正发出去的地址**。页面在真的 `PortalLayout` 里渲染：导航进入门户时发的是不带查询串的
 * `GET /api/v1/customer/notifications`，通知页自己发的带 `page_size=20`，两者靠查询串分开。
 *
 * 要钉住的几件：
 * - **行序就是接口给的顺序**：相邻两条时间不单调（较新的一条时间更早）时，仍按接口顺序排，不按时间重排。
 * - 标已读成功后**就地更新**：不再请求通知页的列表；导航上的未读数重读一次。
 * - 「Load more」把 `next_cursor` 原样作为 `cursor`，新的一页追加在末尾。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import type { Me } from "../../api/auth";
import { ApiError, client } from "../../api/client";
import type { CustomerNotification, CustomerNotificationPage } from "../../api/customerNotifications";
import { AuthProvider } from "../../auth/AuthProvider";
import { resetTokenStore } from "../../auth/tokenStore";
import { PortalLayout } from "../../layouts/PortalLayout";
import { ROUTES } from "../../routes/paths";
import { NotificationsPage } from "./NotificationsPage";

const refresh = vi.hoisted(() => ({ refreshAccessToken: vi.fn() }));
const authApi = vi.hoisted(() => ({ getMe: vi.fn() }));

vi.mock("../../auth/refresh", () => refresh);
vi.mock("../../api/auth", async () => {
  const actual = await vi.importActual<typeof import("../../api/auth")>("../../api/auth");
  return { ...actual, ...authApi };
});

const NOTIFICATIONS_URL = "/api/v1/customer/notifications";
const PAGE_URL = `${NOTIFICATIONS_URL}?page_size=20`;

const ID_1 = "00000000-0000-0000-0000-000000000001";
const ID_2 = "00000000-0000-0000-0000-000000000002";
const ID_3 = "00000000-0000-0000-0000-000000000003";
const ID_4 = "00000000-0000-0000-0000-000000000004";

/** 让请求一直挂着（加载中）。 */
const PENDING = Symbol("pending");

function me(): Me {
  return { role: "CUSTOMER", email: "portal@example.com", two_factor_enabled: true };
}

function notification(overrides: Partial<CustomerNotification> = {}): CustomerNotification {
  return {
    notification_id: ID_1,
    notification_type: "SUSPENDED",
    occurred_at: "2026-10-27T05:12:09",
    balance: "0.00000000",
    threshold: null,
    read_at: null,
    ...overrides,
  };
}

function page(
  items: CustomerNotification[],
  unreadCount: number,
  next: string | null = null,
): CustomerNotificationPage {
  return { items, next_cursor: next, unread_count: unreadCount };
}

function envelope(data: unknown) {
  return { data: { success: true, data, error: null, request_id: "req-1" } };
}

function reply(value: unknown) {
  if (value === PENDING) {
    return new Promise(() => undefined);
  }
  return value instanceof Error ? Promise.reject(value) : Promise.resolve(envelope(value));
}

/**
 * 按地址回应 `client.get`：不带查询串的通知列表是导航读未读数（`nav()`，每次调用重算，可以随调用次数变），带查询串的
 * 是通知页的列表（`list(query)`）。别的路径一律当成失败 —— 这两处都不该问它们，下面也会断言没有问。
 */
function serve(list: (query: URLSearchParams) => unknown, nav: () => unknown = () => page([], 0)) {
  return vi.spyOn(client, "get").mockImplementation((url: string) => {
    const parsed = new URL(url, "https://portal.example");
    if (parsed.pathname !== NOTIFICATIONS_URL) {
      return reply(new ApiError("NOT_FOUND", "Unexpected request.", null));
    }
    return reply(parsed.search === "" ? nav() : list(parsed.searchParams));
  });
}

/** 按地址回应 `client.post`：`replies` 里没有的地址当成失败。 */
function servePost(replies: Record<string, () => unknown>) {
  return vi.spyOn(client, "post").mockImplementation((url: string) => {
    const make = replies[url];
    return reply(make === undefined ? new ApiError("NOT_FOUND", "Unexpected request.", null) : make());
  });
}

/** 通知页自己发的列表请求（带查询串），按发出顺序。 */
function listUrls(get: ReturnType<typeof serve>): string[] {
  return get.mock.calls.map(([url]) => url).filter((url) => url.startsWith(`${NOTIFICATIONS_URL}?`));
}

/** 导航读未读数的请求（不带查询串）的次数。 */
function navReads(get: ReturnType<typeof serve>): number {
  return get.mock.calls.filter(([url]) => url === NOTIFICATIONS_URL).length;
}

function renderPortal() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[ROUTES.portalNotifications]}>
        <AuthProvider>
          <Routes>
            <Route element={<PortalLayout />}>
              <Route path={ROUTES.portalNotifications} element={<NotificationsPage />} />
            </Route>
          </Routes>
        </AuthProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function region(): HTMLElement {
  return screen.getByRole("region", { name: "Notifications" });
}

/** 表格里带 data-row-key 的行，按显示顺序。 */
function rowKeys(container: HTMLElement): string[] {
  return Array.from(container.querySelectorAll("tr[data-row-key]"), (tr) => tr.getAttribute("data-row-key") ?? "");
}

function rowOf(container: HTMLElement, key: string): HTMLElement {
  const tr = container.querySelector<HTMLElement>(`tr[data-row-key="${key}"]`);
  if (tr === null) {
    throw new Error(`no row ${key}`);
  }
  return tr;
}

/** 某一行各列的文字，按列头取。 */
function cellsOf(container: HTMLElement, key: string): Record<string, string> {
  const table = container.querySelector("table");
  if (table === null) {
    throw new Error("no table");
  }
  const heads = Array.from(table.querySelectorAll("thead th"), (th) => th.textContent ?? "");
  const cells = Array.from(rowOf(container, key).querySelectorAll(":scope > td"), (td) => td.textContent ?? "");
  return Object.fromEntries(heads.map((head, index) => [head, cells[index] ?? ""]));
}

/**
 * 等表格画出数据行。这里用 DOM 查询而不是 role 查询：antd 注入的样式让 jsdom 里每次 role 查询都要算很多
 * getComputedStyle，一条用例里查多了会超时。
 */
async function rowsShown(): Promise<void> {
  await waitFor(() => expect(document.querySelector("tr[data-row-key]")).not.toBeNull());
}

/** `scope` 里文字恰好是 `name` 的按钮（理由同 {@link rowsShown}，不走 role 查询）。 */
function buttonsNamed(scope: ParentNode, name: string): HTMLButtonElement[] {
  return Array.from(scope.querySelectorAll("button")).filter((button) => button.textContent?.trim() === name);
}

/** 页面上文字为 `name` 的第一个按钮；没有就抛错。 */
function buttonNamed(name: string): HTMLButtonElement {
  const [button] = buttonsNamed(document, name);
  if (button === undefined) {
    throw new Error(`no ${name} button`);
  }
  return button;
}

/** 卡片右上角的「Mark all as read」。 */
function markAllButton(): HTMLButtonElement {
  return buttonNamed("Mark all as read");
}

/** 某一行自己的「Mark as read」。 */
function markReadButton(container: HTMLElement, key: string): HTMLButtonElement {
  const [button] = buttonsNamed(rowOf(container, key), "Mark as read");
  if (button === undefined) {
    throw new Error(`no Mark as read in row ${key}`);
  }
  return button;
}

function isBold(tr: HTMLElement): boolean {
  return tr.style.fontWeight === "600";
}

/** 导航上的未读数（链接旁边，aria-label 为「Unread notifications」）；不显示时为 null。 */
function navCount(): string | null {
  return screen.queryByLabelText("Unread notifications")?.textContent ?? null;
}

beforeEach(() => {
  vi.clearAllMocks();
  resetTokenStore();
  refresh.refreshAccessToken.mockResolvedValue("access-token-1");
  authApi.getMe.mockResolvedValue(me());
});

afterEach(() => {
  vi.restoreAllMocks();
  resetTokenStore();
});

describe("NotificationsPage rows", () => {
  it("keeps the order the API returned, even when a newer row has an earlier time", async () => {
    // 记账顺序倒序：复机（较新）排第一，但它的时间比后面的停机早两秒 —— 按时间排会把它挪到第二。
    serve(() =>
      page(
        [
          notification({ notification_id: ID_1, notification_type: "REACTIVATED", occurred_at: "2026-10-27T10:00:03" }),
          notification({ notification_id: ID_2, notification_type: "SUSPENDED", occurred_at: "2026-10-27T10:00:05" }),
          notification({ notification_id: ID_3, notification_type: "LOW_BALANCE", occurred_at: "2026-10-27T09:00:00" }),
          notification({ notification_id: ID_4, notification_type: "SUSPENDED", occurred_at: "2026-10-28T00:00:00" }),
        ],
        4,
      ),
    );

    const { container } = renderPortal();
    await rowsShown();

    expect(rowKeys(container)).toEqual([ID_1, ID_2, ID_3, ID_4]);
    expect(cellsOf(container, ID_1)["Time (MYT)"]).toBe("2026-10-27 18:00:03 MYT");
    expect(cellsOf(container, ID_2)["Time (MYT)"]).toBe("2026-10-27 18:00:05 MYT");
  });

  it("describes each of the three types, with the threshold on a low balance, and shows time and balance", async () => {
    serve(() =>
      page(
        [
          notification({
            notification_id: ID_1,
            notification_type: "LOW_BALANCE",
            occurred_at: "2026-10-27T16:30:00",
            balance: "1234.50500000",
            threshold: "50.00000000",
          }),
          notification({ notification_id: ID_2, notification_type: "SUSPENDED", balance: "-1.50000000" }),
          notification({ notification_id: ID_3, notification_type: "REACTIVATED", balance: "5.00000000" }),
          notification({ notification_id: ID_4, notification_type: "SOMETHING_NEW", balance: "0.00000000" }),
        ],
        4,
      ),
    );

    const { container } = renderPortal();
    await rowsShown();

    expect(cellsOf(container, ID_1)).toMatchObject({
      // UTC 16:30 是马来西亚时间次日 00:30。
      "Time (MYT)": "2026-10-28 00:30:00 MYT",
      Notification: "Your balance fell to or below your low balance alert of MYR 50.00.",
      "Balance (MYR)": "1,234.51",
    });
    expect(cellsOf(container, ID_2)).toMatchObject({
      Notification: "Your balance reached zero or below, so AI services were paused.",
      "Balance (MYR)": "-1.50",
    });
    expect(cellsOf(container, ID_3)).toMatchObject({
      Notification: "Your balance is above zero again, so AI services were resumed.",
      "Balance (MYR)": "5.00",
    });
    // 认不出的类型原样显示，不猜成某一种。
    expect(cellsOf(container, ID_4).Notification).toBe("SOMETHING_NEW");
  });

  it("shows unread rows in bold with their own Mark as read, and read rows without one", async () => {
    serve(() =>
      page(
        [
          notification({ notification_id: ID_1, read_at: null }),
          notification({ notification_id: ID_2, read_at: "2026-10-27T06:00:00" }),
        ],
        1,
      ),
    );

    const { container } = renderPortal();
    await rowsShown();

    expect(isBold(rowOf(container, ID_1))).toBe(true);
    expect(isBold(rowOf(container, ID_2))).toBe(false);
    expect(buttonsNamed(rowOf(container, ID_1), "Mark as read")).toHaveLength(1);
    expect(rowOf(container, ID_2).querySelector("button")).toBeNull();
    expect(within(rowOf(container, ID_2)).getByText("Read")).toBeInTheDocument();
    expect(buttonsNamed(container, "Mark as read")).toHaveLength(1);
    expect(screen.getByText("Unread: 1")).toBeInTheDocument();
    expect(markAllButton()).toBeEnabled();
  });

  it("asks for the first page with page_size only, and no admin endpoint or customer identifier", async () => {
    const get = serve(() => page([notification()], 1));

    renderPortal();
    await rowsShown();

    expect(listUrls(get)).toEqual([PAGE_URL]);
    for (const [url] of get.mock.calls) {
      expect(url).not.toContain("/api/v1/admin/");
      expect(url).not.toMatch(/tenant|customer_id/);
    }
  });
});

describe("NotificationsPage marking as read", () => {
  it("marks one row in place and refreshes the navigation count, without reading the list again", async () => {
    let unread = 2;
    const get = serve(
      () =>
        page(
          [notification({ notification_id: ID_1 }), notification({ notification_id: ID_2, notification_type: "LOW_BALANCE", threshold: "50.00000000" })],
          2,
        ),
      () => page([], unread),
    );
    const post = servePost({
      [`${NOTIFICATIONS_URL}/${ID_1}/read`]: () => {
        unread = 1;
        return notification({ notification_id: ID_1, read_at: "2026-10-27T06:00:00" });
      },
    });

    const { container } = renderPortal();
    await rowsShown();
    await waitFor(() => expect(navCount()).toBe("2"));
    const navBefore = navReads(get);

    fireEvent.click(markReadButton(container, ID_1));

    await waitFor(() => expect(isBold(rowOf(container, ID_1))).toBe(false));
    expect(rowOf(container, ID_1).querySelector("button")).toBeNull();
    expect(within(rowOf(container, ID_1)).getByText("Read")).toBeInTheDocument();
    // 另一行不受影响。
    expect(isBold(rowOf(container, ID_2))).toBe(true);
    expect(screen.getByText("Unread: 1")).toBeInTheDocument();
    expect(rowKeys(container)).toEqual([ID_1, ID_2]);
    // 不带请求体的 POST，恰好一次；列表没有再读。
    expect(post.mock.calls).toEqual([[`${NOTIFICATIONS_URL}/${ID_1}/read`]]);
    expect(listUrls(get)).toEqual([PAGE_URL]);
    // 导航重读未读数。
    await waitFor(() => expect(navCount()).toBe("1"));
    expect(navReads(get)).toBe(navBefore + 1);
  });

  it("marks every loaded row in place with Mark all as read, then turns the button off", async () => {
    let unread = 2;
    const get = serve(
      () =>
        page(
          [
            notification({ notification_id: ID_1 }),
            notification({ notification_id: ID_2, read_at: "2026-10-26T00:00:00" }),
            notification({ notification_id: ID_3 }),
          ],
          2,
        ),
      () => page([], unread),
    );
    const post = servePost({
      [`${NOTIFICATIONS_URL}/read-all`]: () => {
        unread = 0;
        return { marked: 2 };
      },
    });

    const { container } = renderPortal();
    await rowsShown();
    await waitFor(() => expect(navCount()).toBe("2"));

    fireEvent.click(markAllButton());

    await waitFor(() => expect(screen.getByText("Unread: 0")).toBeInTheDocument());
    for (const id of [ID_1, ID_2, ID_3]) {
      expect(isBold(rowOf(container, id))).toBe(false);
      expect(rowOf(container, id).querySelector("button")).toBeNull();
    }
    expect(markAllButton()).toBeDisabled();
    expect(post.mock.calls).toEqual([[`${NOTIFICATIONS_URL}/read-all`]]);
    expect(listUrls(get)).toEqual([PAGE_URL]);
    // 导航重读后是 0：不显示数字。
    await waitFor(() => expect(navCount()).toBeNull());
  });

  it("does not offer Mark all as read when nothing is unread", async () => {
    const post = servePost({});
    serve(() => page([notification({ read_at: "2026-10-27T06:00:00" })], 0));

    renderPortal();
    await rowsShown();

    const button = markAllButton();
    expect(button).toBeDisabled();
    fireEvent.click(button);
    expect(post).not.toHaveBeenCalled();
    expect(screen.getByText("Unread: 0")).toBeInTheDocument();
  });

  it("explains a read-only account and leaves the row unread", async () => {
    serve(() => page([notification({ notification_id: ID_1 })], 1));
    servePost({
      [`${NOTIFICATIONS_URL}/${ID_1}/read`]: () =>
        new ApiError("ACCOUNT_READ_ONLY", "The account is read-only.", "req-read-only-403"),
      [`${NOTIFICATIONS_URL}/read-all`]: () =>
        new ApiError("ACCOUNT_READ_ONLY", "The account is read-only.", "req-read-only-403"),
    });

    const { container } = renderPortal();
    await rowsShown();

    fireEvent.click(markReadButton(container, ID_1));
    expect(
      await screen.findByText("Your account is read-only, so notifications cannot be marked as read."),
    ).toBeInTheDocument();
    expect(screen.queryByText("The notification could not be marked as read.")).not.toBeInTheDocument();
    expect(isBold(rowOf(container, ID_1))).toBe(true);
    expect(screen.getByText("Unread: 1")).toBeInTheDocument();

    fireEvent.click(markAllButton());
    expect(
      await screen.findByText("Your account is read-only, so notifications cannot be marked as read."),
    ).toBeInTheDocument();
    expect(isBold(rowOf(container, ID_1))).toBe(true);
  });

  it("shows any other failure with its request id", async () => {
    serve(() => page([notification({ notification_id: ID_1 })], 1));
    servePost({
      [`${NOTIFICATIONS_URL}/${ID_1}/read`]: () =>
        new ApiError("INTERNAL_ERROR", "An unexpected error occurred.", "req-mark-500"),
    });

    const { container } = renderPortal();
    await rowsShown();

    fireEvent.click(markReadButton(container, ID_1));

    expect(await screen.findByText("The notification could not be marked as read.")).toBeInTheDocument();
    expect(screen.getByText("req-mark-500")).toBeInTheDocument();
    expect(isBold(rowOf(container, ID_1))).toBe(true);
  });
});

describe("NotificationsPage load more", () => {
  it("passes next_cursor back as the cursor and appends the next page at the end", async () => {
    const get = serve((query) =>
      query.get("cursor") === ID_2
        ? page([notification({ notification_id: ID_3 }), notification({ notification_id: ID_4 })], 4)
        : page([notification({ notification_id: ID_1 }), notification({ notification_id: ID_2 })], 4, ID_2),
    );

    const { container } = renderPortal();
    await rowsShown();
    expect(rowKeys(container)).toEqual([ID_1, ID_2]);

    fireEvent.click(buttonNamed("Load more"));

    await waitFor(() => expect(rowKeys(container)).toEqual([ID_1, ID_2, ID_3, ID_4]));
    expect(listUrls(get)).toEqual([PAGE_URL, `${PAGE_URL}&cursor=${ID_2}`]);
    // 最后一页的 next_cursor 是 null：按钮不再出现。
    expect(buttonsNamed(document, "Load more")).toHaveLength(0);
  });

  it("marks a row from a later page in place too", async () => {
    serve((query) =>
      query.get("cursor") === ID_1
        ? page([notification({ notification_id: ID_2 })], 2)
        : page([notification({ notification_id: ID_1 })], 2, ID_1),
    );
    servePost({
      [`${NOTIFICATIONS_URL}/${ID_2}/read`]: () => notification({ notification_id: ID_2, read_at: "2026-10-27T06:00:00" }),
    });

    const { container } = renderPortal();
    await rowsShown();
    fireEvent.click(buttonNamed("Load more"));
    await waitFor(() => expect(rowKeys(container)).toEqual([ID_1, ID_2]));

    fireEvent.click(markReadButton(container, ID_2));

    await waitFor(() => expect(isBold(rowOf(container, ID_2))).toBe(false));
    expect(isBold(rowOf(container, ID_1))).toBe(true);
    expect(screen.getByText("Unread: 1")).toBeInTheDocument();
    expect(rowKeys(container)).toEqual([ID_1, ID_2]);
  });

  it("has no Load more on the last page", async () => {
    serve(() => page([notification()], 1));

    renderPortal();
    await rowsShown();

    expect(buttonsNamed(document, "Load more")).toHaveLength(0);
  });
});

describe("NotificationsPage states", () => {
  it("says it is loading", async () => {
    serve(() => PENDING);

    renderPortal();

    expect(await screen.findByText("Loading your notifications…")).toBeInTheDocument();
    expect(within(region()).queryByRole("table")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Mark all as read" })).not.toBeInTheDocument();
  });

  it("shows a failure with its request id and a way to try again", async () => {
    serve(() => new ApiError("INTERNAL_ERROR", "An unexpected error occurred.", "req-notifications-500"));

    renderPortal();

    expect(await screen.findByText("Your notifications could not be loaded.")).toBeInTheDocument();
    expect(screen.getByText("req-notifications-500")).toBeInTheDocument();
    expect(within(region()).getByRole("button", { name: "Try again" })).toBeInTheDocument();
    expect(within(region()).queryByRole("table")).not.toBeInTheDocument();
  });

  it("says there are no notifications yet, without a table", async () => {
    serve(() => page([], 0));

    renderPortal();

    expect(await screen.findByText("You have no notifications yet.")).toBeInTheDocument();
    expect(within(region()).queryByRole("table")).not.toBeInTheDocument();
    expect(markAllButton()).toBeDisabled();
  });

  it("shows no internal field", async () => {
    serve(() => page([notification({ notification_type: "LOW_BALANCE", threshold: "50.00000000" })], 1));

    const { container } = renderPortal();
    await rowsShown();

    expect(container.textContent ?? "").not.toMatch(/cost|margin|tenant|customer id|customer_id|outbox|sequence/i);
    // notification_id 只当行的 key，不显示出来。
    expect(container.textContent ?? "").not.toContain(ID_1);
  });
});

describe("Portal navigation unread count", () => {
  it("links to the notifications page after the other items and shows no number at 0", async () => {
    serve(() => page([], 0));

    renderPortal();
    await screen.findByText("You have no notifications yet.");

    const links = within(screen.getByRole("navigation")).getAllByRole("link");
    expect(links.map((link) => link.textContent)).toEqual(["Home", "Wallet", "Usage", "Conversations", "Notifications"]);
    expect(links.at(-1)).toHaveAttribute("href", ROUTES.portalNotifications);
    expect(navCount()).toBeNull();
  });

  it("shows the unread count next to the link, outside the link text", async () => {
    const get = serve(() => page([], 0), () => page([], 3));

    renderPortal();

    await waitFor(() => expect(navCount()).toBe("3"));
    expect(screen.getByRole("link", { name: "Notifications" })).toHaveTextContent(/^Notifications$/);
    // 进入门户时读一次，不带查询串。
    expect(navReads(get)).toBe(1);
  });

  it("shows no number when the count cannot be read, and the navigation still works", async () => {
    serve(
      () => page([], 0),
      () => new ApiError("INTERNAL_ERROR", "An unexpected error occurred.", "req-nav-500"),
    );

    renderPortal();
    await screen.findByText("You have no notifications yet.");

    expect(navCount()).toBeNull();
    expect(screen.getByRole("link", { name: "Notifications" })).toHaveAttribute("href", ROUTES.portalNotifications);
    // 导航读失败不进页面：页面上没有它的 request_id。
    expect(screen.queryByText("req-nav-500")).not.toBeInTheDocument();
  });
});
