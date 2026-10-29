/**
 * 门户会话页（AIH-TASK-123）：设计闸门 #460 v2 §7 前端一段里属于会话页的全部场景。
 *
 * 与用量页的测试同一写法：不 mock `api/customerConversations`，而是在 `client.get` 上按路径回应，钉住**页面真正发出去的
 * 地址** —— 默认日期是不是马来西亚时间的本月、项目筛选与「Older」带的参数对不对、有没有带接口不接受的供应商 / 模型 /
 * `conversation_id`、有没有请求任何 `/api/v1/admin/` 路径。点一行之后的去向用一个只显示当前地址的探针路由接住。
 *
 * 「现在」用 `vi.useFakeTimers({ toFake: ["Date"] })` 固定：只假 `Date`，定时器照常走。
 * 每多渲染一次整页，沙箱里并行的别的测试文件就更容易被拖到超时：能在同一次渲染里断言的放在一起。
 *
 * 「Export CSV」（AIH-TASK-124）覆盖设计闸门 #460 v2 §7 前端一段里属于导出按钮的场景：请求地址带已应用的筛选、不带
 * `page_size` 与 `cursor`、未应用的表单值不生效、兜底文件名、区间无效时不可用、加载中不可重复点、三种专门文案与带
 * request_id 的通用失败、换筛选时收起失败、导出不影响列表。导出接口同样在 `client.get` 上按路径回应：成功回一个
 * blob 文件，失败回一个响应体是 blob 的 `AxiosError`（与浏览器里 `responseType: "blob"` 的失败同形）。jsdom 没有
 * 对象 URL，也不会真下载：装上替身、拦下链接的点击，只记录下载的文件名。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AxiosError, AxiosHeaders, type AxiosResponse } from "axios";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import { ApiError, client } from "../../api/client";
import type { CustomerConversation, CustomerConversationPage } from "../../api/customerConversations";
import type { CustomerProjectList } from "../../api/customerUsage";
import { ROUTES } from "../../routes/paths";
import { ConversationsPage } from "./ConversationsPage";

const PROJECTS_URL = "/api/v1/customer/projects";
const CONVERSATIONS_URL = "/api/v1/customer/conversations";
const EXPORT_URL = "/api/v1/customer/conversations/export";

const PROJECT_ID = "00000000-0000-0000-0000-000000000000";
const OTHER_PROJECT_ID = "00000000-0000-0000-0000-000000000009";
const CURSOR = "b3BhcXVlLWN1cnNvcg";

/** 固定的「现在」：UTC 2026-10-25 04:00，马来西亚时间 2026-10-25 12:00。 */
const NOW = new Date("2026-10-25T04:00:00.000Z");
const DEFAULT_QUERY = "from_date=2026-10-01&to_date=2026-10-25&page_size=20";

const NOTE =
  "Only requests that carry a conversation ID are listed here. Requests without one (for example those reported by the AI API on your behalf) are on the Usage page. Totals count only the requests in the chosen date range. Select a conversation to see its requests on the Usage page.";

/** 让请求一直挂着（加载中）。 */
const PENDING = Symbol("pending");

function conversation(overrides: Partial<CustomerConversation> = {}): CustomerConversation {
  return {
    conversation_id: "conv_0001",
    project_id: PROJECT_ID,
    project_name: "Customer service bot",
    first_occurred_at: "2026-10-18T08:30:00.123456",
    last_occurred_at: "2026-10-18T08:41:12.000001",
    request_count: 7,
    input_tokens: 8400,
    output_tokens: 2380,
    charged_amount: "0.28840000",
    ...overrides,
  };
}

function page(items: CustomerConversation[], next: string | null = null): CustomerConversationPage {
  return { items, page_size: 20, next_cursor: next };
}

const PROJECTS: CustomerProjectList = {
  items: [
    { id: PROJECT_ID, name: "Customer service bot" },
    { id: OTHER_PROJECT_ID, name: "Sales assistant" },
  ],
};

function envelope(data: unknown) {
  return { data: { success: true, data, error: null, request_id: "req-1" } };
}

/**
 * 按路径回应 `client.get`：项目接口回 `projects`，会话接口按查询串交给 `conversations`，导出接口回 `exported()` 的
 * 结果（文件响应原样、不包信封；默认当成失败 —— 不点导出按钮的用例里页面不该问它）。别的路径一律当成失败 ——
 * 页面不该问它们，下面也会逐条断言没有问。
 */
function serve(
  conversations: (query: URLSearchParams) => unknown,
  projects: unknown = PROJECTS,
  exported: () => unknown = () => new ApiError("NOT_FOUND", "Unexpected request.", null),
) {
  return vi.spyOn(client, "get").mockImplementation((url: string) => {
    const parsed = new URL(url, "https://portal.example");
    if (parsed.pathname === EXPORT_URL) {
      const file = exported();
      if (file === PENDING) {
        return new Promise(() => undefined);
      }
      return file instanceof Error ? Promise.reject(file) : Promise.resolve(file);
    }
    let reply: unknown;
    if (parsed.pathname === PROJECTS_URL) {
      reply = projects;
    } else if (parsed.pathname === CONVERSATIONS_URL) {
      reply = conversations(parsed.searchParams);
    } else {
      reply = new ApiError("NOT_FOUND", "Unexpected request.", null);
    }
    if (reply === PENDING) {
      return new Promise(() => undefined);
    }
    return reply instanceof Error ? Promise.reject(reply) : Promise.resolve(envelope(reply));
  });
}

/** 发给会话列表接口的全部地址，按发出顺序（不含导出）。 */
function conversationUrls(get: ReturnType<typeof serve>): string[] {
  return get.mock.calls.map(([url]) => url).filter((url) => url.startsWith(`${CONVERSATIONS_URL}?`));
}

/** 发给导出接口的全部地址，按发出顺序。 */
function exportUrls(get: ReturnType<typeof serve>): string[] {
  return get.mock.calls.map(([url]) => url).filter((url) => url.startsWith(EXPORT_URL));
}

/** 用量页的位置上只显示当前地址：点一行之后跳到了哪里。 */
function LocationProbe() {
  const location = useLocation();
  // testid 而不是 aria-label：`no-hardcoded-jsx-text` 把 aria-label 的字面量当成文案。
  return <output data-testid="location">{`${location.pathname}${location.search}`}</output>;
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[ROUTES.portalConversations]}>
        <Routes>
          <Route path={ROUTES.portalConversations} element={<ConversationsPage />} />
          <Route path={ROUTES.portalUsage} element={<LocationProbe />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** 会话表里某一行（`data-row-key` 是「项目 id|会话 id」）各列的文字，按列头取。 */
function cellsOf(container: HTMLElement, key: string): Record<string, string> {
  const table = container.querySelector("table");
  const tr = container.querySelector(`tr[data-row-key="${key}"]`);
  if (table === null || tr === null) {
    throw new Error(`no row ${key}`);
  }
  const heads = Array.from(table.querySelectorAll("thead th"), (th) => th.textContent ?? "");
  const cells = Array.from(tr.querySelectorAll(":scope > td"), (td) => td.textContent ?? "");
  return Object.fromEntries(heads.map((head, index) => [head, cells[index] ?? ""]));
}

/** antd 下拉：按 label 打开，点标题为 option 的那一项。 */
async function choose(label: string, option: string): Promise<void> {
  fireEvent.mouseDown(screen.getByLabelText(label));
  const candidates = await screen.findAllByTitle(option);
  const item = candidates.find((candidate) => candidate.closest(".ant-select-item") !== null) ?? candidates[0];
  if (item === undefined) {
    throw new Error(`no option "${option}"`);
  }
  fireEvent.click(item);
}

function setDate(label: string, value: string): void {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("ConversationsPage default dates", () => {
  // 日期选择的 min / max 与表格上方的说明也在这两次渲染里一并断言。
  it.each<[string, string, string]>([
    // UTC 16:00 之前：马来西亚时间还是 10 月 31 日。
    ["2026-10-31T15:59:59.999Z", "2026-10-01", "2026-10-31"],
    // UTC 16:00 起：马来西亚时间已是 11 月 1 日，本月只有这一天。
    ["2026-10-31T16:00:00.000Z", "2026-11-01", "2026-11-01"],
  ])("at %s asks for %s to %s and explains what the table counts", async (now, from, to) => {
    vi.setSystemTime(new Date(now));
    const get = serve(() => page([conversation()]));

    renderPage();

    await screen.findByText("Customer service bot", { selector: "td" });
    expect(conversationUrls(get)).toEqual([`${CONVERSATIONS_URL}?from_date=${from}&to_date=${to}&page_size=20`]);
    for (const [label, value] of [
      ["From (Malaysia time)", from],
      ["To (Malaysia time)", to],
    ] as const) {
      const input = screen.getByLabelText(label);
      expect(input).toHaveValue(value);
      expect(input).toHaveAttribute("min", "2020-01-01");
      expect(input).toHaveAttribute("max", "2099-12-31");
    }
    expect(screen.getByText(NOTE)).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Conversations" })).toBeInTheDocument();
  });
});

describe("ConversationsPage states", () => {
  it("says it is loading while the first page is on its way", async () => {
    serve(() => PENDING);

    renderPage();

    expect(await screen.findByText("Loading your conversations…")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Older" })).not.toBeInTheDocument();
  });

  it("shows a failure with its request id and a retry button", async () => {
    serve(() => new ApiError("INTERNAL_ERROR", "An unexpected error occurred.", "req-conv-500"));

    renderPage();

    expect(await screen.findByText("Your conversations could not be loaded.")).toBeInTheDocument();
    expect(screen.getByText("req-conv-500")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Older" })).not.toBeInTheDocument();
  });

  it("asks to narrow the search when the query is too broad, not a generic failure", async () => {
    serve(
      () =>
        new ApiError(
          "QUERY_TOO_BROAD",
          "The query took too long. Narrow the date range or change the filters.",
          "req-conv-422",
        ),
    );

    renderPage();

    expect(
      await screen.findByText(
        "This search covers too much usage to finish in time. Narrow the date range or choose a project, then try again.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("req-conv-422")).toBeInTheDocument();
    expect(screen.queryByText("Your conversations could not be loaded.")).not.toBeInTheDocument();
  });

  it("says there are no conversations, without a table or an Older button, and still explains the table", async () => {
    serve(() => page([]));

    renderPage();

    expect(await screen.findByText("No conversations match these filters.")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Older" })).not.toBeInTheDocument();
    expect(screen.getByText(NOTE)).toBeInTheDocument();
  });
});

describe("ConversationsPage rows", () => {
  it("shows the columns of a conversation, times in Malaysia time and amounts to at most 6 places", async () => {
    const amounts: [string, string][] = [
      ["0.28840050", "0.288401"],
      ["0.00000049", "0.00"],
      ["12.50000000", "12.50"],
      ["999999999999.99999950", "1,000,000,000,000.00"],
    ];
    const ids = amounts.map((_, index) => `conv_000${index + 1}`);
    serve(() =>
      page(
        amounts.map(([amount], index) =>
          conversation({
            conversation_id: ids[index] ?? "",
            charged_amount: amount,
            // UTC 16:00 是马来西亚的第二天 00:00。
            last_occurred_at: "2026-10-18T16:00:00",
          }),
        ),
      ),
    );

    const { container } = renderPage();
    await screen.findByText("0.288401");

    const heads = Array.from(container.querySelectorAll("thead th"), (th) => th.textContent);
    expect(heads).toEqual([
      "Conversation ID",
      "Project",
      "First request (MYT)",
      "Last activity (MYT)",
      "Requests",
      "Input tokens",
      "Output tokens",
      "Charged (MYR)",
    ]);
    expect(cellsOf(container, `${PROJECT_ID}|conv_0001`)).toEqual({
      "Conversation ID": "conv_0001",
      Project: "Customer service bot",
      "First request (MYT)": "2026-10-18 16:30:00 MYT",
      "Last activity (MYT)": "2026-10-19 00:00:00 MYT",
      Requests: "7",
      "Input tokens": "8400",
      "Output tokens": "2380",
      "Charged (MYR)": "0.288401",
    });
    amounts.forEach(([, shown], index) => {
      expect(cellsOf(container, `${PROJECT_ID}|${ids[index] ?? ""}`)["Charged (MYR)"]).toBe(shown);
    });
    // 原样的 8 位字符串不出现：显示的是舍入后的值。
    expect(screen.queryByText("0.28840050")).not.toBeInTheDocument();
    expect(container.textContent ?? "").not.toMatch(/cost|margin|markup|tenant|status/i);
  });

  it("goes to the usage page with exactly the conversation, its project and the applied dates", async () => {
    const user = userEvent.setup();
    const get = serve(() =>
      page([
        conversation(),
        // 同一个会话 id 在另一个项目里是另一行。
        conversation({ project_id: OTHER_PROJECT_ID, project_name: "Sales assistant" }),
      ]),
    );
    renderPage();
    await screen.findByText("Customer service bot", { selector: "td" });

    setDate("From (Malaysia time)", "2026-10-02");
    setDate("To (Malaysia time)", "2026-10-20");
    await user.click(screen.getByRole("button", { name: "Apply filters" }));
    await waitFor(() => {
      expect(conversationUrls(get).at(-1)).toBe(`${CONVERSATIONS_URL}?from_date=2026-10-02&to_date=2026-10-20&page_size=20`);
    });
    // 填了但没点「Apply filters」：不算数，跳过去用的是已应用的区间。
    setDate("From (Malaysia time)", "2026-10-05");

    const expected =
      `${ROUTES.portalUsage}?conversation_id=conv_0001&project_id=${OTHER_PROJECT_ID}` +
      "&from_date=2026-10-02&to_date=2026-10-20";
    const links = await screen.findAllByRole("link", { name: "conv_0001" });
    expect(links.map((link) => link.getAttribute("href"))).toEqual([
      `${ROUTES.portalUsage}?conversation_id=conv_0001&project_id=${PROJECT_ID}&from_date=2026-10-02&to_date=2026-10-20`,
      expected,
    ]);

    await user.click(screen.getByText("Sales assistant", { selector: "td" }));

    const location = await screen.findByTestId("location");
    expect(location).toHaveTextContent(expected);
    const target = new URL(location.textContent ?? "", "https://portal.example");
    expect(target.pathname).toBe(ROUTES.portalUsage);
    expect([...target.searchParams.entries()]).toEqual([
      ["conversation_id", "conv_0001"],
      ["project_id", OTHER_PROJECT_ID],
      ["from_date", "2026-10-02"],
      ["to_date", "2026-10-20"],
    ]);
  });
});

describe("ConversationsPage filters and paging", () => {
  it("filters by project, pages older and back to the newest, and only ever asks the two customer endpoints", async () => {
    const user = userEvent.setup();
    const get = serve((query) =>
      query.has("cursor")
        ? page([conversation({ conversation_id: "conv_0002", project_name: "Sales assistant" })])
        : page([conversation()], CURSOR),
    );
    renderPage();
    await screen.findByText("Customer service bot", { selector: "td" });
    expect(conversationUrls(get)).toEqual([`${CONVERSATIONS_URL}?${DEFAULT_QUERY}`]);
    expect(screen.getByRole("button", { name: "Newest" })).toBeDisabled();

    await choose("Project", "Sales assistant");
    await user.click(screen.getByRole("button", { name: "Apply filters" }));
    const filtered = `${CONVERSATIONS_URL}?from_date=2026-10-01&to_date=2026-10-25&project_id=${OTHER_PROJECT_ID}&page_size=20`;
    await waitFor(() => {
      expect(conversationUrls(get).at(-1)).toBe(filtered);
    });
    // 新条件的第一页回来之前按钮是禁用的（表里还是上一份），等它可点。
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Older" })).toBeEnabled();
    });
    await user.click(screen.getByRole("button", { name: "Older" }));

    expect(await screen.findByText("Sales assistant", { selector: "td" })).toBeInTheDocument();
    expect(conversationUrls(get).at(-1)).toBe(`${filtered}&cursor=${CURSOR}`);
    // 最后一页：没有更早的了，按钮不出现。
    await waitFor(() => {
      expect(screen.queryByRole("button", { name: "Older" })).not.toBeInTheDocument();
    });
    expect(screen.getByRole("button", { name: "Newest" })).toBeEnabled();

    await user.click(screen.getByRole("button", { name: "Newest" }));

    expect(await screen.findByText("Customer service bot", { selector: "td" })).toBeInTheDocument();
    await waitFor(() => {
      expect(conversationUrls(get).at(-1)).toBe(filtered);
    });

    // 「Reset」回到默认区间、不按项目筛。
    await user.click(screen.getByRole("button", { name: "Reset" }));
    await waitFor(() => {
      expect(conversationUrls(get).at(-1)).toBe(`${CONVERSATIONS_URL}?${DEFAULT_QUERY}`);
    });

    const urls = get.mock.calls.map(([url]) => url);
    expect(urls).toContain(PROJECTS_URL);
    for (const url of urls) {
      expect(url).not.toContain("/api/v1/admin/");
      expect(url === PROJECTS_URL || url.startsWith(`${CONVERSATIONS_URL}?`)).toBe(true);
      expect(url).not.toMatch(/tenant|customer_id|provider|model|conversation_id|event_id/);
    }
  });

  it("does not send a range longer than 92 days and says why", async () => {
    const user = userEvent.setup();
    const get = serve(() => page([conversation()]));
    renderPage();
    await screen.findByText("Customer service bot", { selector: "td" });

    // 2026-07-01 → 2026-10-01：to − from = 92 天，即 93 个日历日。
    setDate("From (Malaysia time)", "2026-07-01");
    setDate("To (Malaysia time)", "2026-10-01");
    await user.click(screen.getByRole("button", { name: "Apply filters" }));

    expect(await screen.findByText("Choose a date range of 92 days or less.")).toBeInTheDocument();
    expect(conversationUrls(get)).toEqual([`${CONVERSATIONS_URL}?${DEFAULT_QUERY}`]);
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Older" })).not.toBeInTheDocument();

    // 恰好 92 个日历日照常发。
    setDate("From (Malaysia time)", "2026-07-02");
    await user.click(screen.getByRole("button", { name: "Apply filters" }));

    await waitFor(() => {
      expect(conversationUrls(get).at(-1)).toBe(`${CONVERSATIONS_URL}?from_date=2026-07-02&to_date=2026-10-01&page_size=20`);
    });
    expect(screen.queryByText("Choose a date range of 92 days or less.")).not.toBeInTheDocument();
  });
});

describe("ConversationsPage export", () => {
  const EXPORT_NAME = /Export CSV/;
  const FAILED = "Your conversations could not be exported.";
  const originalCreate = Object.getOwnPropertyDescriptor(URL, "createObjectURL");
  const originalRevoke = Object.getOwnPropertyDescriptor(URL, "revokeObjectURL");
  /** 被点击的下载链接的文件名，按点击顺序。 */
  let downloads: string[];

  beforeEach(() => {
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
  });

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

  /** 文件响应；不给文件名时没有 `Content-Disposition`，页面只能用兜底文件名。 */
  function file(name?: string) {
    return {
      data: new Blob(["﻿conversation_id,project_id\r\n"], { type: "text/csv" }),
      status: 200,
      statusText: "",
      headers: name === undefined ? {} : { "content-disposition": `attachment; filename="${name}"` },
    };
  }

  /** 后端的失败：响应体是装着 JSON 信封的 blob。 */
  function failure(status: number, code: string, requestId: string) {
    const body = JSON.stringify({ success: false, data: null, error: { code, message: "Safe message." }, request_id: requestId });
    const response = {
      data: new Blob([body], { type: "application/json" }),
      status,
      statusText: "",
      headers: {},
      config: { headers: new AxiosHeaders() },
    } satisfies AxiosResponse;
    return new AxiosError("Request failed", "ERR_BAD_REQUEST", undefined, undefined, response);
  }

  it("exports with the applied filters, without page_size or cursor, ignores what is still being typed, and leaves the list alone", async () => {
    const user = userEvent.setup();
    const get = serve(
      (query) =>
        query.has("cursor")
          ? page([conversation({ conversation_id: "conv_0002", project_name: "Sales assistant" })])
          : page([conversation()], CURSOR),
      PROJECTS,
      () => file(),
    );
    renderPage();
    await screen.findByText("Customer service bot", { selector: "td" });

    setDate("From (Malaysia time)", "2026-10-02");
    setDate("To (Malaysia time)", "2026-10-20");
    await choose("Project", "Sales assistant");
    await user.click(screen.getByRole("button", { name: "Apply filters" }));
    const filtered = `${CONVERSATIONS_URL}?from_date=2026-10-02&to_date=2026-10-20&project_id=${OTHER_PROJECT_ID}&page_size=20`;
    await waitFor(() => {
      expect(conversationUrls(get).at(-1)).toBe(filtered);
    });
    // 翻到更早的一页：列表带着游标，导出仍然不带。
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Older" })).toBeEnabled();
    });
    await user.click(screen.getByRole("button", { name: "Older" }));
    expect(await screen.findByText("Sales assistant", { selector: "td" })).toBeInTheDocument();
    expect(conversationUrls(get).at(-1)).toBe(`${filtered}&cursor=${CURSOR}`);
    // 填了但没点「Apply filters」：不算数。
    setDate("From (Malaysia time)", "2026-10-05");
    await choose("Project", "Customer service bot");
    const listed = conversationUrls(get).length;

    await user.click(screen.getByRole("button", { name: EXPORT_NAME }));

    // 响应没有 Content-Disposition：兜底文件名用的同样是已应用的区间。
    await waitFor(() => {
      expect(downloads).toEqual(["conversations_2026-10-02_2026-10-20.csv"]);
    });
    expect(exportUrls(get)).toEqual([`${EXPORT_URL}?from_date=2026-10-02&to_date=2026-10-20&project_id=${OTHER_PROJECT_ID}`]);
    const [, config] = get.mock.calls.find(([url]) => url.startsWith(EXPORT_URL)) ?? [];
    expect(config).toMatchObject({ responseType: "blob" });
    for (const url of exportUrls(get)) {
      expect(url).not.toMatch(/page_size|cursor/);
    }
    // 导出不重新请求列表，不清空表格，也不把列表翻回最新一页。
    expect(conversationUrls(get)).toHaveLength(listed);
    expect(screen.getByText("Sales assistant", { selector: "td" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Newest" })).toBeEnabled();
    expect(screen.getByRole("button", { name: EXPORT_NAME })).toBeEnabled();
    for (const [url] of get.mock.calls) {
      expect(url).not.toContain("/api/v1/admin/");
      expect(url).not.toMatch(/tenant|customer_id|provider|model|conversation_id|event_id/);
    }
  });

  it("exports the default range with only the dates when nothing was changed, under the backend's file name", async () => {
    const user = userEvent.setup();
    const get = serve(() => page([conversation()]), PROJECTS, () => file("conversations_2026-10-01_2026-10-25.csv"));
    renderPage();
    await screen.findByText("Customer service bot", { selector: "td" });

    await user.click(screen.getByRole("button", { name: EXPORT_NAME }));

    await waitFor(() => {
      expect(downloads).toEqual(["conversations_2026-10-01_2026-10-25.csv"]);
    });
    expect(exportUrls(get)).toEqual([`${EXPORT_URL}?from_date=2026-10-01&to_date=2026-10-25`]);
  });

  it.each<[string, string, string]>([
    ["reversed", "2026-10-20", "2026-10-10"],
    ["longer than 92 days", "2026-07-01", "2026-10-01"],
    ["incomplete", "", "2026-10-10"],
    // 越界（早于 2020-01-01）不在这里：日期框带 `min`，浏览器（与 jsdom）的表单校验直接拦下提交，根本应用不上；
    // 越界的判定由 UsagePage.test.tsx 的 dateRangeProblem 用例钉住，按钮看的是同一个函数。
  ])("cannot export when the applied range is %s", async (_, from, to) => {
    const user = userEvent.setup();
    const get = serve(() => page([conversation()]), PROJECTS, () => file());
    renderPage();
    await screen.findByText("Customer service bot", { selector: "td" });
    expect(screen.getByRole("button", { name: EXPORT_NAME })).toBeEnabled();

    setDate("From (Malaysia time)", from);
    setDate("To (Malaysia time)", to);
    await user.click(screen.getByRole("button", { name: "Apply filters" }));

    await waitFor(() => {
      expect(screen.getByRole("button", { name: EXPORT_NAME })).toBeDisabled();
    });
    fireEvent.click(screen.getByRole("button", { name: EXPORT_NAME }));
    expect(exportUrls(get)).toEqual([]);
    expect(downloads).toEqual([]);
  });

  it("shows the button loading while exporting, does not export twice, and keeps the list usable", async () => {
    const user = userEvent.setup();
    const get = serve(() => page([conversation()]), PROJECTS, () => PENDING);
    renderPage();
    await screen.findByText("Customer service bot", { selector: "td" });

    await user.click(screen.getByRole("button", { name: EXPORT_NAME }));

    await waitFor(() => {
      expect(screen.getByRole("button", { name: EXPORT_NAME })).toBeDisabled();
    });
    const button = screen.getByRole("button", { name: EXPORT_NAME });
    expect(within(button).getByRole("img", { name: "loading" })).toBeInTheDocument();
    fireEvent.click(button);
    fireEvent.click(button);
    expect(exportUrls(get)).toHaveLength(1);
    expect(downloads).toEqual([]);
    // 列表与筛选照常可用。
    expect(screen.getByText("Customer service bot", { selector: "td" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Apply filters" })).toBeEnabled();
  });

  it.each<[number, string, string]>([
    [
      422,
      "EXPORT_TOO_LARGE",
      "This export has more than 20,000 conversations. Narrow the date range or choose a project, then try again.",
    ],
    [
      422,
      "QUERY_TOO_BROAD",
      "This export covers too much usage to finish in time. Narrow the date range or choose a project, then try again.",
    ],
    [503, "EXPORT_BUSY", "Another export is in progress. Wait a few seconds, then try again."],
  ])("explains a %s %s in its own words", async (status, code, text) => {
    const user = userEvent.setup();
    serve(() => page([conversation()]), PROJECTS, () => failure(status, code, "req-export-specific"));
    renderPage();
    await screen.findByText("Customer service bot", { selector: "td" });

    await user.click(screen.getByRole("button", { name: EXPORT_NAME }));

    expect(await screen.findByText(text)).toBeInTheDocument();
    expect(screen.queryByText(FAILED)).not.toBeInTheDocument();
    expect(screen.queryByText("req-export-specific")).not.toBeInTheDocument();
    // 列表不受影响，按钮可以再点。
    expect(screen.getByText("Customer service bot", { selector: "td" })).toBeInTheDocument();
    expect(screen.queryByText("Your conversations could not be loaded.")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: EXPORT_NAME })).toBeEnabled();
    expect(downloads).toEqual([]);
  });

  it("shows any other failure with its request id, leaves the list alone, and drops it when the filters change", async () => {
    const user = userEvent.setup();
    const get = serve(() => page([conversation()]), PROJECTS, () => failure(500, "INTERNAL_ERROR", "req-export-500"));
    renderPage();
    await screen.findByText("Customer service bot", { selector: "td" });
    const listed = conversationUrls(get).length;

    await user.click(screen.getByRole("button", { name: EXPORT_NAME }));

    expect(await screen.findByText(FAILED)).toBeInTheDocument();
    expect(screen.getByText("req-export-500")).toBeInTheDocument();
    expect(screen.queryByText("Your conversations could not be loaded.")).not.toBeInTheDocument();
    expect(screen.getByText("Customer service bot", { selector: "td" })).toBeInTheDocument();
    expect(conversationUrls(get)).toHaveLength(listed);
    expect(downloads).toEqual([]);

    // 换了条件：上一次的失败说的是旧条件，收起。
    setDate("From (Malaysia time)", "2026-10-02");
    await user.click(screen.getByRole("button", { name: "Apply filters" }));

    await waitFor(() => {
      expect(screen.queryByText(FAILED)).not.toBeInTheDocument();
    });
    expect(screen.queryByText("req-export-500")).not.toBeInTheDocument();
    await waitFor(() => {
      expect(conversationUrls(get).at(-1)).toBe(`${CONVERSATIONS_URL}?from_date=2026-10-02&to_date=2026-10-25&page_size=20`);
    });
  });
});
