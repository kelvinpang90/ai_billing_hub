/**
 * 会话列表请求发到哪个地址、带什么查询参数（AIH-TASK-123）。
 *
 * 参数名写错时页面照样显示一页（后端忽略未知参数），筛选只是永远不生效。所以这里逐个钉住参数名与顺序、省略的参数
 * 不进查询串、游标原样传回，以及**不带**任何租户或客户标识，也不带接口不接受的供应商、模型、`conversation_id` 筛选。
 *
 * 会话摘要导出（AIH-TASK-124）同样钉住路径、查询串只有 `from_date`、`to_date` 与可选的 `project_id`（没有 `page_size`
 * 与 `cursor`，也没有租户或客户标识）与兜底文件名。
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { client } from "./client";
import {
  CUSTOMER_CONVERSATIONS_QUERY_KEY,
  customerConversationListQueryKey,
  customerConversationsExportFilename,
  customerConversationsExportUrl,
  customerConversationsUrl,
  exportCustomerConversations,
  listCustomerConversations,
} from "./customerConversations";
import { CUSTOMER_USAGE_QUERY_KEY } from "./customerUsage";

const CONVERSATIONS_URL = "/api/v1/customer/conversations";
const EXPORT_URL = "/api/v1/customer/conversations/export";

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

describe("exportCustomerConversations", () => {
  const originalCreate = Object.getOwnPropertyDescriptor(URL, "createObjectURL");
  const originalRevoke = Object.getOwnPropertyDescriptor(URL, "revokeObjectURL");
  /** 被点击的下载链接的文件名，按点击顺序。 */
  let downloads: string[];

  /** jsdom 没有对象 URL，也不会真的下载：装上替身、拦下链接的点击，只记下文件名。 */
  function stubDownload(): void {
    downloads = [];
    Object.defineProperty(URL, "createObjectURL", {
      configurable: true,
      writable: true,
      value: () => "blob:https://portal.example/00000000-0000-0000-0000-000000000000",
    });
    Object.defineProperty(URL, "revokeObjectURL", { configurable: true, writable: true, value: () => undefined });
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      downloads.push(this.download);
    });
  }

  afterEach(() => {
    for (const [name, descriptor] of [
      ["createObjectURL", originalCreate],
      ["revokeObjectURL", originalRevoke],
    ] as const) {
      if (descriptor === undefined) {
        Reflect.deleteProperty(URL, name);
      } else {
        Object.defineProperty(URL, name, descriptor);
      }
    }
  });

  /** 没有 `Content-Disposition` 的文件响应：文件名只能用兜底。 */
  function file() {
    return { data: new Blob(["﻿conversation_id\r\n"], { type: "text/csv" }), status: 200, statusText: "", headers: {} };
  }

  it("downloads from /api/v1/customer/conversations/export as a blob with only the dates, under the fallback name", async () => {
    stubDownload();
    const get = vi.spyOn(client, "get").mockResolvedValue(file());

    await exportCustomerConversations({ fromDate: "2026-10-01", toDate: "2026-10-25" });

    expect(get).toHaveBeenCalledTimes(1);
    const [url, config] = get.mock.calls[0] ?? [];
    expect(url).toBe(`${EXPORT_URL}?from_date=2026-10-01&to_date=2026-10-25`);
    expect(config).toMatchObject({ responseType: "blob" });
    expect(downloads).toEqual(["conversations_2026-10-01_2026-10-25.csv"]);
  });

  it("sends the project under the name the backend reads, after the dates", () => {
    const url = customerConversationsExportUrl({ fromDate: "2026-10-01", toDate: "2026-10-25", projectId: PROJECT_ID });

    expect(parse(url)).toEqual({
      path: EXPORT_URL,
      params: [
        ["from_date", "2026-10-01"],
        ["to_date", "2026-10-25"],
        ["project_id", PROJECT_ID],
      ],
    });
  });

  it("treats null and empty projects like omitted ones", () => {
    for (const projectId of [null, ""]) {
      expect(customerConversationsExportUrl({ fromDate: "2026-10-01", toDate: "2026-10-25", projectId })).toBe(
        `${EXPORT_URL}?from_date=2026-10-01&to_date=2026-10-25`,
      );
    }
    expect(customerConversationsExportUrl({ fromDate: "2026-10-01", toDate: "2026-10-25" })).toBe(
      `${EXPORT_URL}?from_date=2026-10-01&to_date=2026-10-25`,
    );
  });

  it("has no page_size or cursor, and never a tenant or customer identifier", () => {
    // 页面把带分页的列表查询原样传进来，多出来的字段也不进查询串。
    const listQuery = {
      fromDate: "2026-10-01",
      toDate: "2026-10-25",
      projectId: PROJECT_ID,
      pageSize: 20,
      cursor: CURSOR,
    };
    const url = customerConversationsExportUrl(listQuery);

    expect(parse(url).params.map(([name]) => name)).toEqual(["from_date", "to_date", "project_id"]);
    expect(url).not.toMatch(/page_size|cursor|tenant|customer_id|provider|model|conversation_id|event_id|\/admin\//);
  });

  it("names the fallback file after the dates, like the backend", () => {
    expect(customerConversationsExportFilename({ fromDate: "2026-10-01", toDate: "2026-10-31" })).toBe(
      "conversations_2026-10-01_2026-10-31.csv",
    );
    expect(
      customerConversationsExportFilename({ fromDate: "2026-09-01", toDate: "2026-09-30", projectId: PROJECT_ID }),
    ).toBe("conversations_2026-09-01_2026-09-30.csv");
  });

  it("passes the abort signal through", async () => {
    stubDownload();
    const get = vi.spyOn(client, "get").mockResolvedValue(file());
    const controller = new AbortController();

    await exportCustomerConversations({ fromDate: "2026-10-01", toDate: "2026-10-25" }, controller.signal);

    const [, config] = get.mock.calls[0] ?? [];
    expect(config).toMatchObject({ responseType: "blob", signal: controller.signal });
  });
});
