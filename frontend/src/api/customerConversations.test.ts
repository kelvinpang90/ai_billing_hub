/**
 * 会话列表请求发到哪个地址、带什么查询参数（AIH-TASK-123）。
 *
 * 参数名写错时页面照样显示一页（后端忽略未知参数），筛选只是永远不生效。所以这里逐个钉住参数名与顺序、省略的参数
 * 不进查询串、游标原样传回，以及**不带**任何租户或客户标识，也不带接口不接受的供应商、模型、`conversation_id` 筛选。
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { client } from "./client";
import {
  CUSTOMER_CONVERSATIONS_QUERY_KEY,
  customerConversationListQueryKey,
  customerConversationsUrl,
  listCustomerConversations,
} from "./customerConversations";
import { CUSTOMER_USAGE_QUERY_KEY } from "./customerUsage";

const CONVERSATIONS_URL = "/api/v1/customer/conversations";

const PROJECT_ID = "00000000-0000-0000-0000-000000000000";
/** 像后端给的那样：base64url，可能带 `-`、`_`。前端只原样传回。 */
const CURSOR = "MjAyNi0xMC0xOFQwODo0MToxMi4wMDAwMDF8MDAw-_";

const EMPTY_PAGE = { items: [], page_size: 20, next_cursor: null };

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

describe("listCustomerConversations", () => {
  it("reads /api/v1/customer/conversations with only the dates when nothing else is given", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));

    const page = await listCustomerConversations({ fromDate: "2026-10-01", toDate: "2026-10-25" });

    expect(get).toHaveBeenCalledWith(`${CONVERSATIONS_URL}?from_date=2026-10-01&to_date=2026-10-25`, {});
    expect(page).toEqual(EMPTY_PAGE);
  });

  it("sends every parameter under the name the backend reads, in order", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));

    await listCustomerConversations({
      fromDate: "2026-10-01",
      toDate: "2026-10-25",
      projectId: PROJECT_ID,
      pageSize: 20,
      cursor: CURSOR,
    });

    const [url] = get.mock.calls[0] ?? [];
    expect(parse(String(url))).toEqual({
      path: CONVERSATIONS_URL,
      params: [
        ["from_date", "2026-10-01"],
        ["to_date", "2026-10-25"],
        ["project_id", PROJECT_ID],
        ["page_size", "20"],
        ["cursor", CURSOR],
      ],
    });
  });

  it("treats null and empty values like omitted ones", () => {
    expect(
      customerConversationsUrl({ fromDate: "2026-10-01", toDate: "2026-10-25", projectId: null, cursor: "" }),
    ).toBe(`${CONVERSATIONS_URL}?from_date=2026-10-01&to_date=2026-10-25`);
    expect(customerConversationsUrl({ fromDate: "", toDate: "", projectId: "", cursor: null })).toBe(CONVERSATIONS_URL);
  });

  it("passes the cursor back exactly as it was given", () => {
    const url = customerConversationsUrl({ fromDate: "2026-10-01", toDate: "2026-10-25", cursor: CURSOR });

    expect(parse(url).params.at(-1)).toEqual(["cursor", CURSOR]);
  });

  it("never sends a tenant or customer identifier, nor a provider, model or conversation filter", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));

    await listCustomerConversations({
      fromDate: "2026-10-01",
      toDate: "2026-10-25",
      projectId: PROJECT_ID,
      pageSize: 20,
      cursor: CURSOR,
    });

    for (const [url] of get.mock.calls) {
      expect(url).not.toMatch(/tenant|customer_id|provider|model|conversation_id|event_id|\/admin\//);
    }
  });

  it("passes the abort signal through", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));
    const controller = new AbortController();

    await listCustomerConversations({ fromDate: "2026-10-01", toDate: "2026-10-25" }, controller.signal);

    expect(get).toHaveBeenCalledWith(`${CONVERSATIONS_URL}?from_date=2026-10-01&to_date=2026-10-25`, {
      signal: controller.signal,
    });
  });

  it("starts every page key with its own prefix and tells pages apart", () => {
    const prefix = [...CUSTOMER_CONVERSATIONS_QUERY_KEY];
    const first = customerConversationListQueryKey({ fromDate: "2026-10-01", toDate: "2026-10-25", cursor: null });
    const older = customerConversationListQueryKey({ fromDate: "2026-10-01", toDate: "2026-10-25", cursor: CURSOR });
    for (const key of [first, older]) {
      expect(key.slice(0, prefix.length)).toEqual(prefix);
    }
    expect(first).not.toEqual(older);
    expect(prefix).not.toEqual([...CUSTOMER_USAGE_QUERY_KEY]);
  });
});
