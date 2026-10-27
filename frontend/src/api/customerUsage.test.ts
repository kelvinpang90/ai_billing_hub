/**
 * 客户项目与用量两个请求发到哪个地址、带什么查询参数。
 *
 * 用量页的用例按路径回应 `client.get`，但参数名写错时页面照样显示一页（后端忽略未知参数）：`conversation_id`
 * 打成 `conversationId` 在浏览器里不报错，只是筛选永远不生效。所以这里逐个钉住参数名、省略的参数不进查询串、
 * 游标原样传回，以及两个请求都**不带**任何租户或客户标识。用量导出（AIH-TASK-117）同样钉住路径、参数名与省略规则；
 * 月度汇总导出（AIH-TASK-120）钉住路径、查询串只有 `month` 与兜底文件名。
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { client } from "./client";
import {
  CUSTOMER_USAGE_QUERY_KEY,
  CUSTOMER_USAGE_SUMMARY_QUERY_KEY,
  customerUsageExportFilename,
  customerUsageExportUrl,
  customerUsageListQueryKey,
  customerUsageSummaryExportFilename,
  customerUsageSummaryExportUrl,
  customerUsageSummaryQueryKey,
  exportCustomerUsageEvents,
  exportCustomerUsageSummary,
  getCustomerUsageSummary,
  listCustomerProjects,
  listCustomerUsageEvents,
} from "./customerUsage";

const PROJECTS_URL = "/api/v1/customer/projects";
const USAGE_URL = "/api/v1/customer/usage-events";
const EXPORT_URL = "/api/v1/customer/usage-events/export";
const SUMMARY_URL = "/api/v1/customer/usage-summary";
const SUMMARY_EXPORT_URL = "/api/v1/customer/usage-summary/export";

const SUMMARY = {
  month: "2026-10",
  charged_amount: "0.41200000",
  request_count: 10,
  conversation_count: 2,
  days: [{ date: "2026-10-01", request_count: 10, charged_amount: "0.41200000" }],
};

const PROJECT_ID = "00000000-0000-0000-0000-000000000000";
const EVENT_ID = "00000000-0000-0000-0000-000000000000";
/** 像后端给的那样：base64url，可能带 `-`、`_` 与填充的 `=`。前端只原样传回。 */
const CURSOR = "MjAyNi0xMC0xOFQwODozMDowMC4xMjM0NTZ8MDAw-_==";

const PROJECTS = { items: [{ id: PROJECT_ID, name: "Customer service bot" }] };
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

describe("listCustomerProjects", () => {
  it("reads /api/v1/customer/projects without any query string", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(PROJECTS));

    await expect(listCustomerProjects()).resolves.toEqual(PROJECTS);

    expect(get).toHaveBeenCalledWith(PROJECTS_URL, {});
  });

  it("passes the abort signal through", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(PROJECTS));
    const controller = new AbortController();

    await listCustomerProjects(controller.signal);

    expect(get).toHaveBeenCalledWith(PROJECTS_URL, { signal: controller.signal });
  });
});

describe("listCustomerUsageEvents", () => {
  it("leaves every omitted parameter out of the query string", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));

    const page = await listCustomerUsageEvents();

    expect(get).toHaveBeenCalledWith(USAGE_URL, {});
    expect(page).toEqual(EMPTY_PAGE);
  });

  it("treats null and empty values like omitted ones", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));

    await listCustomerUsageEvents({
      fromDate: "2026-10-01",
      toDate: "2026-10-25",
      projectId: null,
      provider: "",
      model: null,
      conversationId: "",
      eventId: null,
      cursor: null,
    });

    expect(get).toHaveBeenCalledWith(`${USAGE_URL}?from_date=2026-10-01&to_date=2026-10-25`, {});
  });

  it("sends every filter under the name the backend reads", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));

    await listCustomerUsageEvents({
      fromDate: "2026-10-01",
      toDate: "2026-10-25",
      projectId: PROJECT_ID,
      provider: "anthropic",
      model: "claude-sonnet-5-5",
      conversationId: "conv_0001",
      eventId: EVENT_ID,
      pageSize: 20,
      cursor: CURSOR,
    });

    const [url] = get.mock.calls[0] ?? [];
    expect(parse(String(url))).toEqual({
      path: USAGE_URL,
      params: [
        ["from_date", "2026-10-01"],
        ["to_date", "2026-10-25"],
        ["project_id", PROJECT_ID],
        ["provider", "anthropic"],
        ["model", "claude-sonnet-5-5"],
        ["conversation_id", "conv_0001"],
        ["event_id", EVENT_ID],
        ["page_size", "20"],
        ["cursor", CURSOR],
      ],
    });
  });

  it("sends only event_id when that is all it is given", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));

    await listCustomerUsageEvents({ eventId: EVENT_ID });

    expect(get).toHaveBeenCalledWith(`${USAGE_URL}?event_id=${EVENT_ID}`, {});
  });

  it("passes the cursor back exactly as it was given", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));

    await listCustomerUsageEvents({ cursor: CURSOR });

    const [url] = get.mock.calls[0] ?? [];
    expect(parse(String(url)).params).toEqual([["cursor", CURSOR]]);
  });

  it("never sends a tenant or customer identifier", async () => {
    const get = vi.spyOn(client, "get").mockImplementation((url: string) =>
      Promise.resolve(envelope(url.startsWith(PROJECTS_URL) ? PROJECTS : EMPTY_PAGE)),
    );

    await listCustomerUsageEvents({
      fromDate: "2026-10-01",
      toDate: "2026-10-25",
      projectId: PROJECT_ID,
      pageSize: 20,
      cursor: CURSOR,
    });
    await listCustomerProjects();

    for (const [url] of get.mock.calls) {
      expect(url).not.toMatch(/tenant|customer_id|\/admin\//);
    }
  });

  it("passes the abort signal through", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));
    const controller = new AbortController();

    await listCustomerUsageEvents({}, controller.signal);

    expect(get).toHaveBeenCalledWith(USAGE_URL, { signal: controller.signal });
  });

  it("starts every page key with the same prefix and tells pages apart", () => {
    const prefix = [...CUSTOMER_USAGE_QUERY_KEY];
    const first = customerUsageListQueryKey({ fromDate: "2026-10-01", toDate: "2026-10-25", cursor: null });
    const older = customerUsageListQueryKey({ fromDate: "2026-10-01", toDate: "2026-10-25", cursor: CURSOR });
    for (const key of [first, older]) {
      expect(key.slice(0, prefix.length)).toEqual(prefix);
    }
    expect(first).not.toEqual(older);
  });
});

describe("exportCustomerUsageEvents", () => {
  const originalCreate = Object.getOwnPropertyDescriptor(URL, "createObjectURL");
  const originalRevoke = Object.getOwnPropertyDescriptor(URL, "revokeObjectURL");

  /** jsdom 没有对象 URL，也不会真的下载：装上替身、拦下链接的点击，只看请求发到了哪里。 */
  function stubDownload(): void {
    Object.defineProperty(URL, "createObjectURL", {
      configurable: true,
      writable: true,
      value: () => "blob:https://portal.example/00000000-0000-0000-0000-000000000000",
    });
    Object.defineProperty(URL, "revokeObjectURL", { configurable: true, writable: true, value: () => undefined });
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
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

  function file() {
    return { data: new Blob(["﻿id\r\n"], { type: "text/csv" }), status: 200, statusText: "", headers: {} };
  }

  it("downloads from /api/v1/customer/usage-events/export as a blob with only the dates", async () => {
    stubDownload();
    const get = vi.spyOn(client, "get").mockResolvedValue(file());

    await exportCustomerUsageEvents({ fromDate: "2026-10-01", toDate: "2026-10-25" });

    expect(get).toHaveBeenCalledTimes(1);
    const [url, config] = get.mock.calls[0] ?? [];
    expect(url).toBe(`${EXPORT_URL}?from_date=2026-10-01&to_date=2026-10-25`);
    expect(config).toMatchObject({ responseType: "blob" });
  });

  it("sends every filter under the name the backend reads", () => {
    const url = customerUsageExportUrl({
      fromDate: "2026-10-01",
      toDate: "2026-10-25",
      projectId: PROJECT_ID,
      provider: "anthropic",
      model: "claude-sonnet-5-5",
      conversationId: "conv_0001",
      eventId: EVENT_ID,
    });

    expect(parse(url)).toEqual({
      path: EXPORT_URL,
      params: [
        ["from_date", "2026-10-01"],
        ["to_date", "2026-10-25"],
        ["project_id", PROJECT_ID],
        ["provider", "anthropic"],
        ["model", "claude-sonnet-5-5"],
        ["conversation_id", "conv_0001"],
        ["event_id", EVENT_ID],
      ],
    });
  });

  it("treats null and empty values like omitted ones", () => {
    const url = customerUsageExportUrl({
      fromDate: "2026-10-01",
      toDate: "2026-10-25",
      projectId: null,
      provider: "",
      model: null,
      conversationId: "",
      eventId: null,
    });

    expect(url).toBe(`${EXPORT_URL}?from_date=2026-10-01&to_date=2026-10-25`);
  });

  it("has no page_size or cursor, and never a tenant or customer identifier", () => {
    const url = customerUsageExportUrl({ fromDate: "2026-10-01", toDate: "2026-10-25", projectId: PROJECT_ID });

    expect(url).not.toMatch(/page_size|cursor|tenant|customer_id|\/admin\//);
  });

  it("falls back to the backend's file name rule", () => {
    expect(customerUsageExportFilename({ fromDate: "2026-10-01", toDate: "2026-10-31" })).toBe(
      "usage-events_2026-10-01_2026-10-31.csv",
    );
  });
});

describe("exportCustomerUsageSummary", () => {
  const originalCreate = Object.getOwnPropertyDescriptor(URL, "createObjectURL");
  const originalRevoke = Object.getOwnPropertyDescriptor(URL, "revokeObjectURL");
  /** 被点击的下载链接的文件名，按点击顺序。 */
  let downloads: string[];

  /** jsdom 没有对象 URL，也不会真的下载：装上替身、拦下链接的点击，只记录文件名。 */
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

  it("downloads from /api/v1/customer/usage-summary/export as a blob with only the month", async () => {
    stubDownload();
    // 响应头里没有文件名：用兜底的那一个。
    const get = vi
      .spyOn(client, "get")
      .mockResolvedValue({ data: new Blob(["﻿date_myt\r\n"], { type: "text/csv" }), status: 200, statusText: "", headers: {} });

    await exportCustomerUsageSummary("2026-10");

    expect(get).toHaveBeenCalledTimes(1);
    const [url, config] = get.mock.calls[0] ?? [];
    expect(url).toBe(`${SUMMARY_EXPORT_URL}?month=2026-10`);
    expect(config).toMatchObject({ responseType: "blob" });
    expect(downloads).toEqual(["usage-summary_2026-10.csv"]);
  });

  it("sends the month under the name the backend reads, and nothing else", () => {
    expect(parse(customerUsageSummaryExportUrl("2026-09"))).toEqual({
      path: SUMMARY_EXPORT_URL,
      params: [["month", "2026-09"]],
    });
  });

  it("never sends a filter, a tenant or a customer identifier", () => {
    expect(customerUsageSummaryExportUrl("2026-10")).not.toMatch(
      /from_date|to_date|project_id|provider|model|conversation_id|event_id|page_size|cursor|tenant|customer_id|\/admin\//,
    );
  });

  it("falls back to the backend's file name rule", () => {
    expect(customerUsageSummaryExportFilename("2026-10")).toBe("usage-summary_2026-10.csv");
    expect(customerUsageSummaryExportFilename("2020-01")).toBe("usage-summary_2020-01.csv");
  });
});

describe("getCustomerUsageSummary", () => {
  it("leaves the month out of the query string when it is not given", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(SUMMARY));

    await expect(getCustomerUsageSummary()).resolves.toEqual(SUMMARY);

    expect(get).toHaveBeenCalledWith(SUMMARY_URL, {});
  });

  it("treats null and an empty month like an omitted one", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(SUMMARY));

    await getCustomerUsageSummary(null);
    await getCustomerUsageSummary("");

    expect(get.mock.calls).toEqual([
      [SUMMARY_URL, {}],
      [SUMMARY_URL, {}],
    ]);
  });

  it("sends the month under the name the backend reads, and nothing else", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(SUMMARY));

    await getCustomerUsageSummary("2026-10");

    const [url] = get.mock.calls[0] ?? [];
    expect(parse(String(url))).toEqual({ path: SUMMARY_URL, params: [["month", "2026-10"]] });
    expect(get).toHaveBeenCalledWith(`${SUMMARY_URL}?month=2026-10`, {});
  });

  it("never sends a tenant or customer identifier", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(SUMMARY));

    await getCustomerUsageSummary();
    await getCustomerUsageSummary("2026-10");

    for (const [url] of get.mock.calls) {
      expect(url).not.toMatch(/tenant|customer_id|\/admin\//);
    }
  });

  it("passes the abort signal through", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(SUMMARY));
    const controller = new AbortController();

    await getCustomerUsageSummary(null, controller.signal);

    expect(get).toHaveBeenCalledWith(SUMMARY_URL, { signal: controller.signal });
  });

  it("keys each month apart, under its own prefix", () => {
    const prefix = [...CUSTOMER_USAGE_SUMMARY_QUERY_KEY];
    const current = customerUsageSummaryQueryKey();
    const october = customerUsageSummaryQueryKey("2026-10");
    for (const key of [current, october]) {
      expect(key.slice(0, prefix.length)).toEqual(prefix);
    }
    expect(current).not.toEqual(october);
    expect(prefix).not.toEqual([...CUSTOMER_USAGE_QUERY_KEY]);
  });
});
