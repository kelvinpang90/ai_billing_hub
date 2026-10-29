/**
 * 通知三个接口发到哪个地址、用什么方法、带什么查询参数（AIH-TASK-128）。
 *
 * 参数名写错时页面照样显示一页（后端忽略未知参数），翻页只是永远停在第一页。所以这里钉住参数名与顺序、省略的参数
 * 不进查询串、游标原样传回、两个写接口是不带请求体的 POST，以及**不带**任何租户或客户标识。
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { client } from "./client";
import {
  CUSTOMER_NOTIFICATIONS_QUERY_KEY,
  CUSTOMER_NOTIFICATIONS_UNREAD_QUERY_KEY,
  customerNotificationListQueryKey,
  customerNotificationsUrl,
  listCustomerNotifications,
  markAllCustomerNotificationsRead,
  markCustomerNotificationRead,
} from "./customerNotifications";
import { CUSTOMER_CONVERSATIONS_QUERY_KEY } from "./customerConversations";

const NOTIFICATIONS_URL = "/api/v1/customer/notifications";
const NOTIFICATION_ID = "00000000-0000-0000-0000-000000000000";
/** 后端给的是上一页最后一条的 id；前端只原样传回，不解析。 */
const CURSOR = "00000000-0000-0000-0000-000000000000";

const EMPTY_PAGE = { items: [], next_cursor: null, unread_count: 0 };

const ROW = {
  notification_id: NOTIFICATION_ID,
  notification_type: "SUSPENDED",
  occurred_at: "2026-10-27T05:12:09",
  balance: "0.00000000",
  threshold: null,
  read_at: "2026-10-27T06:00:00",
};

function envelope(data: unknown) {
  return { data: { success: true, data, error: null, request_id: "req-1" } };
}

/** 被调用的地址拆成路径与查询参数（按出现顺序）。 */
function parse(url: string): { path: string; params: [string, string][] } {
  const parsed = new URL(url, "https://portal.example");
  return { path: parsed.pathname, params: [...parsed.searchParams.entries()] };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("listCustomerNotifications", () => {
  it("reads /api/v1/customer/notifications without a query string when nothing is given", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));

    const page = await listCustomerNotifications();

    expect(get).toHaveBeenCalledWith(NOTIFICATIONS_URL, {});
    expect(page).toEqual(EMPTY_PAGE);
  });

  it("sends page_size and cursor under the names the backend reads, in order", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));

    await listCustomerNotifications({ pageSize: 20, cursor: CURSOR });

    const [url] = get.mock.calls[0] ?? [];
    expect(parse(String(url))).toEqual({
      path: NOTIFICATIONS_URL,
      params: [
        ["page_size", "20"],
        ["cursor", CURSOR],
      ],
    });
  });

  it("leaves out omitted, null and empty values", () => {
    expect(customerNotificationsUrl({})).toBe(NOTIFICATIONS_URL);
    expect(customerNotificationsUrl({ cursor: null })).toBe(NOTIFICATIONS_URL);
    expect(customerNotificationsUrl({ cursor: "" })).toBe(NOTIFICATIONS_URL);
    expect(customerNotificationsUrl({ pageSize: 20 })).toBe(`${NOTIFICATIONS_URL}?page_size=20`);
    expect(customerNotificationsUrl({ cursor: CURSOR })).toBe(`${NOTIFICATIONS_URL}?cursor=${CURSOR}`);
  });

  it("passes the cursor back exactly as it was given", () => {
    const opaque = "not-a-uuid_-~";

    expect(parse(customerNotificationsUrl({ cursor: opaque })).params).toEqual([["cursor", opaque]]);
  });

  it("never sends a tenant or customer identifier", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));

    await listCustomerNotifications({ pageSize: 20, cursor: CURSOR });
    await listCustomerNotifications();

    for (const [url] of get.mock.calls) {
      expect(url).not.toMatch(/tenant|customer_id|\/admin\//);
    }
  });

  it("passes the abort signal through", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));
    const controller = new AbortController();

    await listCustomerNotifications({}, controller.signal);

    expect(get).toHaveBeenCalledWith(NOTIFICATIONS_URL, { signal: controller.signal });
  });

  it("keeps the unread count and the list under their own keys, both under the notifications prefix", () => {
    const prefix = [...CUSTOMER_NOTIFICATIONS_QUERY_KEY];
    const list = customerNotificationListQueryKey(20);
    const unread = [...CUSTOMER_NOTIFICATIONS_UNREAD_QUERY_KEY];
    for (const key of [list, unread]) {
      expect(key.slice(0, prefix.length)).toEqual(prefix);
    }
    expect([...list]).not.toEqual(unread);
    expect(prefix).not.toEqual([...CUSTOMER_CONVERSATIONS_QUERY_KEY]);
  });
});

describe("markCustomerNotificationRead", () => {
  it("posts to …/{notification_id}/read without a request body", async () => {
    const get = vi.spyOn(client, "get");
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope(ROW));

    const row = await markCustomerNotificationRead(NOTIFICATION_ID);

    expect(post).toHaveBeenCalledTimes(1);
    expect(post.mock.calls[0]).toEqual([`${NOTIFICATIONS_URL}/${NOTIFICATION_ID}/read`]);
    expect(get).not.toHaveBeenCalled();
    expect(row).toEqual(ROW);
  });

  it("has no query string and no tenant or customer identifier", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope(ROW));

    await markCustomerNotificationRead(NOTIFICATION_ID);

    const [url] = post.mock.calls[0] ?? [];
    expect(String(url)).not.toContain("?");
    expect(url).not.toMatch(/tenant|customer_id|\/admin\//);
  });

  it("throws the backend's error code", async () => {
    vi.spyOn(client, "post").mockResolvedValue({
      data: {
        success: false,
        data: null,
        error: { code: "ACCOUNT_READ_ONLY", message: "The account is read-only." },
        request_id: "req-403",
      },
    });

    await expect(markCustomerNotificationRead(NOTIFICATION_ID)).rejects.toMatchObject({
      code: "ACCOUNT_READ_ONLY",
      requestId: "req-403",
    });
  });
});

describe("markAllCustomerNotificationsRead", () => {
  it("posts to …/read-all without a request body", async () => {
    const get = vi.spyOn(client, "get");
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope({ marked: 3 }));

    await expect(markAllCustomerNotificationsRead()).resolves.toEqual({ marked: 3 });

    expect(post).toHaveBeenCalledTimes(1);
    expect(post.mock.calls[0]).toEqual([`${NOTIFICATIONS_URL}/read-all`]);
    expect(get).not.toHaveBeenCalled();
  });
});
