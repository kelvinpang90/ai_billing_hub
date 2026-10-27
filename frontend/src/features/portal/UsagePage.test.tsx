/**
 * 门户用量页（AIH-TASK-105）：设计闸门 #398 v4 §7 前端一行的全部场景，外加三种状态与超过 92 天不发请求。
 *
 * 这里不 mock `api/customerUsage`，而是在 `client.get` 上按路径回应：要钉住的正是**页面真正发出去的地址**——
 * 默认日期是不是马来西亚时间的本月、筛选与「Older」带的参数对不对、带 `event_id` 时是不是只按它查、有没有请求任何
 * `/api/v1/admin/` 路径。写错不会报错，页面照样显示一页，只是哪一页不对、或者问了不该问的接口。
 *
 * 「现在」用 `vi.useFakeTimers({ toFake: ["Date"] })` 固定：只假 `Date`，定时器照常走，界面交互与请求不受影响。
 *
 * 「Export CSV」（AIH-TASK-117）同样在 `client.get` 上按路径回应：成功回一个 blob 文件，失败回一个响应体是 blob 的
 * `AxiosError`（与浏览器里 `responseType: "blob"` 的失败同形）。jsdom 没有对象 URL，也不会真下载：装上替身、拦下
 * 链接的点击，只记录下载的文件名。
 *
 * 月度汇总导出区（AIH-TASK-120）覆盖设计闸门 #457 v1 §7 前端一段：默认月份与上下限、无效月份不可用、请求地址恰好只带
 * `month`、兜底文件名、加载状态、`QUERY_TOO_BROAD` 与通用失败（request_id），以及失败不影响用量列表与「Export CSV」。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AxiosError, AxiosHeaders, type AxiosResponse } from "axios";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import { ApiError, client } from "../../api/client";
import type { CustomerProjectList, CustomerUsageEvent, CustomerUsagePage } from "../../api/customerUsage";
import { ROUTES } from "../../routes/paths";
import {
  UsagePage,
  currentMalaysiaMonth,
  dateRangeProblem,
  defaultDateRange,
  exportableMonth,
  type DateRangeProblem,
} from "./UsagePage";

const PROJECTS_URL = "/api/v1/customer/projects";
const USAGE_URL = "/api/v1/customer/usage-events";
const EXPORT_URL = "/api/v1/customer/usage-events/export";
const SUMMARY_EXPORT_URL = "/api/v1/customer/usage-summary/export";

const PROJECT_ID = "00000000-0000-0000-0000-000000000000";
const OTHER_PROJECT_ID = "00000000-0000-0000-0000-000000000009";
const EVENT_ID = "00000000-0000-0000-0000-000000000000";
const CURSOR = "b3BhcXVlLWN1cnNvcg";

/** 固定的「现在」：UTC 2026-10-25 04:00，马来西亚时间 2026-10-25 12:00。 */
const NOW = new Date("2026-10-25T04:00:00.000Z");
const DEFAULT_QUERY = "from_date=2026-10-01&to_date=2026-10-25&page_size=20";

/** 让请求一直挂着（加载中）。 */
const PENDING = Symbol("pending");

function event(overrides: Partial<CustomerUsageEvent> = {}): CustomerUsageEvent {
  return {
    id: "00000000-0000-0000-0000-000000000001",
    event_id: EVENT_ID,
    request_id: "req_0001",
    conversation_id: "conv_0001",
    feature: "crm.contact_summary",
    project_id: PROJECT_ID,
    project_name: "Customer service bot",
    provider: "anthropic",
    model: "claude-sonnet-5-5",
    usage_type: "LLM_TOKEN",
    input_tokens: 1200,
    output_tokens: 340,
    cache_creation_input_tokens: 0,
    cache_read_input_tokens: 15,
    quantity: null,
    unit: "TOKEN",
    charged_amount: "0.04123450",
    occurred_at: "2026-10-18T08:30:00.123456",
    ...overrides,
  };
}

function page(items: CustomerUsageEvent[], next: string | null = null): CustomerUsagePage {
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
 * 按路径回应 `client.get`：项目接口回 `projects`，用量接口按查询串交给 `usage`，导出接口回 `exported()` 的结果，
 * 月度汇总导出接口回 `summaryExported()` 的结果（文件响应原样、不包信封；默认当成失败 —— 不点导出按钮的用例里页面
 * 不该问它们）。别的路径一律当成失败 —— 页面不该问它们，下面也会逐条断言没有问。
 */
function serve(
  usage: (query: URLSearchParams) => unknown,
  projects: unknown = PROJECTS,
  exported: () => unknown = () => new ApiError("NOT_FOUND", "Unexpected request.", null),
  summaryExported: () => unknown = () => new ApiError("NOT_FOUND", "Unexpected request.", null),
) {
  return vi.spyOn(client, "get").mockImplementation((url: string) => {
    const parsed = new URL(url, "https://portal.example");
    if (parsed.pathname === EXPORT_URL || parsed.pathname === SUMMARY_EXPORT_URL) {
      const file = parsed.pathname === EXPORT_URL ? exported() : summaryExported();
      if (file === PENDING) {
        return new Promise(() => undefined);
      }
      return file instanceof Error ? Promise.reject(file) : Promise.resolve(file);
    }
    let reply: unknown;
    if (parsed.pathname === PROJECTS_URL) {
      reply = projects;
    } else if (parsed.pathname === USAGE_URL) {
      reply = usage(parsed.searchParams);
    } else {
      reply = new ApiError("NOT_FOUND", "Unexpected request.", null);
    }
    if (reply === PENDING) {
      return new Promise(() => undefined);
    }
    return reply instanceof Error ? Promise.reject(reply) : Promise.resolve(envelope(reply));
  });
}

/** 发给用量列表接口的全部地址，按发出顺序（不含导出）。 */
function usageUrls(get: ReturnType<typeof serve>): string[] {
  return get.mock.calls.map(([url]) => url).filter((url) => url.startsWith(`${USAGE_URL}?`));
}

/** 发给导出接口的全部地址，按发出顺序。 */
function exportUrls(get: ReturnType<typeof serve>): string[] {
  return get.mock.calls.map(([url]) => url).filter((url) => url.startsWith(EXPORT_URL));
}

/** 发给月度汇总导出接口的全部地址，按发出顺序。 */
function summaryExportUrls(get: ReturnType<typeof serve>): string[] {
  return get.mock.calls.map(([url]) => url).filter((url) => url.startsWith(SUMMARY_EXPORT_URL));
}

function renderPage(entry: string = ROUTES.portalUsage) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[entry]}>
        <UsagePage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** 用量表里某一行（`data-row-key` 是行的 id）各列的文字，按列头取（第一格是展开按钮，没有列头）。 */
function cellsOf(container: HTMLElement, id: string): Record<string, string> {
  const table = container.querySelector("table");
  const tr = container.querySelector(`tr[data-row-key="${id}"]`);
  if (table === null || tr === null) {
    throw new Error(`no row ${id}`);
  }
  const heads = Array.from(table.querySelectorAll("thead th"), (th) => th.textContent ?? "");
  const cells = Array.from(tr.querySelectorAll(":scope > td"), (td) => td.textContent ?? "");
  return Object.fromEntries(heads.map((head, index) => [head, cells[index] ?? ""]));
}

/** 详情（带边框的 Descriptions）：label 在 <th>，值在紧跟的 <td>。 */
function detailValue(region: HTMLElement, label: string): string | null {
  const th = Array.from(region.querySelectorAll("th")).find((cell) => cell.textContent === label);
  return th?.nextElementSibling?.textContent ?? null;
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

describe("UsagePage default dates", () => {
  it.each<[string, string, string]>([
    // UTC 16:00 之前：马来西亚时间还是 10 月 31 日。
    ["2026-10-31T15:59:59.999Z", "2026-10-01", "2026-10-31"],
    // UTC 16:00 起：马来西亚时间已是 11 月 1 日，本月只有这一天。
    ["2026-10-31T16:00:00.000Z", "2026-11-01", "2026-11-01"],
  ])("at %s asks for %s to %s", async (now, from, to) => {
    vi.setSystemTime(new Date(now));
    const get = serve(() => page([event()]));

    renderPage();

    await screen.findByText("Customer service bot", { selector: "td" });
    expect(usageUrls(get)).toEqual([`${USAGE_URL}?from_date=${from}&to_date=${to}&page_size=20`]);
    expect(screen.getByLabelText("From (Malaysia time)")).toHaveValue(from);
    expect(screen.getByLabelText("To (Malaysia time)")).toHaveValue(to);
  });

  it.each<[string, string, string]>([
    ["2026-10-31T15:59:59.999Z", "2026-10-01", "2026-10-31"],
    ["2026-10-31T16:00:00.000Z", "2026-11-01", "2026-11-01"],
    ["2026-12-31T16:00:00.000Z", "2027-01-01", "2027-01-01"],
    ["2026-10-24T16:00:00.000Z", "2026-10-01", "2026-10-25"],
  ])("computes the Malaysia-time month to date at %s", (now, fromDate, toDate) => {
    expect(defaultDateRange(new Date(now))).toEqual({ fromDate, toDate });
  });

  it("limits the date pickers to 2020-01-01 through 2099-12-31", async () => {
    serve(() => page([]));

    renderPage();

    await screen.findByText("No billed usage matches these filters.");
    for (const label of ["From (Malaysia time)", "To (Malaysia time)"]) {
      expect(screen.getByLabelText(label)).toHaveAttribute("min", "2020-01-01");
      expect(screen.getByLabelText(label)).toHaveAttribute("max", "2099-12-31");
    }
  });
});

describe("UsagePage states", () => {
  it("says it is loading while the first page is on its way", async () => {
    serve(() => PENDING);

    renderPage();

    expect(await screen.findByText("Loading your usage…")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Older" })).not.toBeInTheDocument();
  });

  it("shows a failure with its request id and a retry button", async () => {
    serve(() => new ApiError("INTERNAL_ERROR", "An unexpected error occurred.", "req-usage-500"));

    renderPage();

    expect(await screen.findByText("Your usage could not be loaded.")).toBeInTheDocument();
    expect(screen.getByText("req-usage-500")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Older" })).not.toBeInTheDocument();
  });

  it("asks to narrow the search when the query is too broad, not a generic failure", async () => {
    serve(
      () =>
        new ApiError(
          "QUERY_TOO_BROAD",
          "The query took too long. Narrow the date range or change the filters.",
          "req-usage-422",
        ),
    );

    renderPage();

    expect(
      await screen.findByText(
        "This search covers too much usage to finish in time. Narrow the date range or change the filters, then try again.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("req-usage-422")).toBeInTheDocument();
    expect(screen.queryByText("Your usage could not be loaded.")).not.toBeInTheDocument();
  });

  it("says there is no usage, without a table or an Older button", async () => {
    serve(() => page([]));

    renderPage();

    expect(await screen.findByText("No billed usage matches these filters.")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Older" })).not.toBeInTheDocument();
  });

  it("shows the projects of the customer in the project filter", async () => {
    serve(() => page([]));

    renderPage();
    await screen.findByText("No billed usage matches these filters.");

    fireEvent.mouseDown(screen.getByLabelText("Project"));
    expect((await screen.findAllByTitle("Customer service bot")).length).toBeGreaterThan(0);
    expect(screen.getAllByTitle("Sales assistant").length).toBeGreaterThan(0);
  });
});

describe("UsagePage rows", () => {
  it("shows the columns a customer needs and no internal one", async () => {
    serve(() => page([event()]));

    const { container } = renderPage();
    await screen.findByText("Customer service bot", { selector: "td" });

    const heads = Array.from(container.querySelectorAll("thead th"), (th) => th.textContent);
    expect(heads).toEqual(["", "Time (MYT)", "Project", "Provider / model", "Usage type", "Input / output tokens", "Charged (MYR)"]);
    expect(container.textContent ?? "").not.toMatch(/cost|margin|markup|tenant|status/i);
  });

  it("shows the time in Malaysia time (UTC+8), marked MYT", async () => {
    serve(() =>
      page([
        event({ id: "00000000-0000-0000-0000-000000000001", occurred_at: "2026-10-18T08:30:00.123456" }),
        // UTC 16:00 是马来西亚的第二天 00:00。
        event({ id: "00000000-0000-0000-0000-000000000002", occurred_at: "2026-10-18T16:00:00" }),
      ]),
    );

    const { container } = renderPage();
    await screen.findByText("2026-10-18 16:30:00 MYT");

    expect(cellsOf(container, "00000000-0000-0000-0000-000000000001")["Time (MYT)"]).toBe("2026-10-18 16:30:00 MYT");
    expect(cellsOf(container, "00000000-0000-0000-0000-000000000002")["Time (MYT)"]).toBe("2026-10-19 00:00:00 MYT");
  });

  it("shows the charged amount rounded half up to at most 6 places", async () => {
    const amounts: [string, string][] = [
      ["0.04123450", "0.041235"],
      ["0.00000049", "0.00"],
      ["12.50000000", "12.50"],
      ["0.00000050", "0.000001"],
      ["0.99999950", "1.00"],
      ["999999999999.99999950", "1,000,000,000,000.00"],
      ["0.04120000", "0.0412"],
    ];
    const ids = amounts.map((_, index) => `00000000-0000-0000-0000-${String(index + 1).padStart(12, "0")}`);
    serve(() =>
      page(amounts.map(([amount], index) => event({ id: ids[index] ?? "", charged_amount: amount }))),
    );

    const { container } = renderPage();
    await screen.findByText("0.041235");

    amounts.forEach(([, shown], index) => {
      expect(cellsOf(container, ids[index] ?? "")["Charged (MYR)"]).toBe(shown);
    });
    // 原样的 8 位字符串不出现：显示的是舍入后的值。
    expect(screen.queryByText("0.04123450")).not.toBeInTheDocument();
  });

  it("labels the known usage types and shows an unknown one as it is", async () => {
    serve(() =>
      page([
        event({ id: "00000000-0000-0000-0000-000000000001" }),
        event({
          id: "00000000-0000-0000-0000-000000000002",
          usage_type: "VIDEO_SECOND",
          input_tokens: null,
          output_tokens: null,
          cache_creation_input_tokens: null,
          cache_read_input_tokens: null,
          quantity: "12.50000000",
          unit: "FRAME",
        }),
      ]),
    );

    const { container } = renderPage();
    await screen.findByText("VIDEO_SECOND");

    const tokens = cellsOf(container, "00000000-0000-0000-0000-000000000001");
    expect(tokens["Usage type"]).toBe("LLM tokens");
    expect(tokens["Input / output tokens"]).toBe("1200 / 340");
    expect(tokens["Provider / model"]).toBe("anthropic / claude-sonnet-5-5");

    const unknown = cellsOf(container, "00000000-0000-0000-0000-000000000002");
    expect(unknown["Usage type"]).toBe("VIDEO_SECOND");
    expect(unknown["Input / output tokens"]).toBe("—");
  });

  it("expands a row into the request details when it is clicked", async () => {
    const user = userEvent.setup();
    serve(() =>
      page([
        event({ id: "00000000-0000-0000-0000-000000000001" }),
        event({
          id: "00000000-0000-0000-0000-000000000002",
          request_id: "req_0002",
          project_name: "Sales assistant",
          conversation_id: null,
          feature: null,
          usage_type: "VIDEO_SECOND",
          input_tokens: null,
          output_tokens: null,
          cache_creation_input_tokens: null,
          cache_read_input_tokens: null,
          quantity: "1000.50000000",
          unit: "FRAME",
          charged_amount: "0.00000050",
          occurred_at: "2026-10-18T16:00:00",
        }),
      ]),
    );

    renderPage();
    await screen.findByText("Customer service bot", { selector: "td" });
    expect(screen.queryByRole("region", { name: "Request details" })).not.toBeInTheDocument();

    await user.click(screen.getByText("Customer service bot", { selector: "td" }));

    const detail = await screen.findByRole("region", { name: "Request details" });
    const expected: [string, string][] = [
      ["Event ID", EVENT_ID],
      ["Request ID", "req_0001"],
      ["Conversation ID", "conv_0001"],
      ["Feature", "crm.contact_summary"],
      ["Project", "Customer service bot"],
      ["Provider", "anthropic"],
      ["Model", "claude-sonnet-5-5"],
      ["Usage type", "LLM tokens"],
      ["Input tokens", "1200"],
      ["Output tokens", "340"],
      ["Cache write tokens", "0"],
      ["Cache read tokens", "15"],
      ["Quantity", "—"],
      ["Unit", "Tokens"],
      ["Charged (MYR)", "0.041235"],
      ["Time (MYT)", "2026-10-18 16:30:00 MYT"],
    ];
    for (const [label, value] of expected) {
      expect(detailValue(detail, label)).toBe(value);
    }
    expect(detail.textContent ?? "").not.toMatch(/cost|margin|markup|tenant|status/i);

    await user.click(screen.getByText("Sales assistant", { selector: "td" }));

    const details = await screen.findAllByRole("region", { name: "Request details" });
    const second = details.find((region) => detailValue(region, "Request ID") === "req_0002");
    if (second === undefined) {
      throw new Error("the second row did not expand");
    }
    expect(detailValue(second, "Conversation ID")).toBe("—");
    expect(detailValue(second, "Feature")).toBe("—");
    expect(detailValue(second, "Usage type")).toBe("VIDEO_SECOND");
    expect(detailValue(second, "Input tokens")).toBe("—");
    expect(detailValue(second, "Quantity")).toBe("1,000.5");
    // 认不出的单位原样显示。
    expect(detailValue(second, "Unit")).toBe("FRAME");
    expect(detailValue(second, "Charged (MYR)")).toBe("0.000001");
    expect(detailValue(second, "Time (MYT)")).toBe("2026-10-19 00:00:00 MYT");
  });
});

describe("UsagePage filters and paging", () => {
  it("sends the chosen filters, leaves the empty ones out, and goes back to the defaults on Reset", async () => {
    const user = userEvent.setup();
    const get = serve(() => page([event()]));
    renderPage();
    await screen.findByText("Customer service bot", { selector: "td" });

    setDate("From (Malaysia time)", "2026-10-02");
    setDate("To (Malaysia time)", "2026-10-20");
    await choose("Project", "Sales assistant");
    await user.type(screen.getByLabelText("Provider"), " anthropic ");
    await user.type(screen.getByLabelText("Model"), "claude-sonnet-5-5");
    await user.type(screen.getByLabelText("Conversation ID"), "conv_0001");
    await user.click(screen.getByRole("button", { name: "Apply filters" }));

    await waitFor(() => {
      expect(usageUrls(get).at(-1)).toBe(
        `${USAGE_URL}?from_date=2026-10-02&to_date=2026-10-20&project_id=${OTHER_PROJECT_ID}` +
          "&provider=anthropic&model=claude-sonnet-5-5&conversation_id=conv_0001&page_size=20",
      );
    });

    await user.click(screen.getByRole("button", { name: "Reset" }));

    await waitFor(() => {
      expect(usageUrls(get).at(-1)).toBe(`${USAGE_URL}?${DEFAULT_QUERY}`);
    });
    expect(screen.getByLabelText("Provider")).toHaveValue("");
    expect(screen.getByLabelText("From (Malaysia time)")).toHaveValue("2026-10-01");
  });

  it("pages older with next_cursor, has no Older on the last page, and goes back to the newest page", async () => {
    const user = userEvent.setup();
    const get = serve((query) =>
      query.has("cursor")
        ? page([event({ id: "00000000-0000-0000-0000-000000000002", project_name: "Sales assistant" })])
        : page([event()], CURSOR),
    );
    renderPage();

    await screen.findByText("Customer service bot", { selector: "td" });
    expect(screen.getByRole("button", { name: "Newest" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Older" }));

    expect(await screen.findByText("Sales assistant", { selector: "td" })).toBeInTheDocument();
    expect(usageUrls(get).at(-1)).toBe(`${USAGE_URL}?${DEFAULT_QUERY}&cursor=${CURSOR}`);
    // 最后一页：没有更早的了，按钮不出现。
    await waitFor(() => {
      expect(screen.queryByRole("button", { name: "Older" })).not.toBeInTheDocument();
    });
    expect(screen.getByRole("button", { name: "Newest" })).toBeEnabled();

    await user.click(screen.getByRole("button", { name: "Newest" }));

    expect(await screen.findByText("Customer service bot", { selector: "td" })).toBeInTheDocument();
    await waitFor(() => {
      expect(usageUrls(get).at(-1)).toBe(`${USAGE_URL}?${DEFAULT_QUERY}`);
    });
    expect(screen.getByRole("button", { name: "Newest" })).toBeDisabled();
    expect(await screen.findByRole("button", { name: "Older" })).toBeEnabled();
  });

  it("keeps the filters when paging older and drops the cursor when the filters change", async () => {
    const user = userEvent.setup();
    const get = serve((query) => (query.has("cursor") ? page([]) : page([event()], CURSOR)));
    renderPage();
    await screen.findByText("Customer service bot", { selector: "td" });

    await user.type(screen.getByLabelText("Provider"), "anthropic");
    await user.click(screen.getByRole("button", { name: "Apply filters" }));
    await waitFor(() => {
      expect(usageUrls(get).at(-1)).toBe(`${USAGE_URL}?from_date=2026-10-01&to_date=2026-10-25&provider=anthropic&page_size=20`);
    });
    // 新条件的第一页回来之前按钮是禁用的（表里还是上一份），等它可点。
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Older" })).toBeEnabled();
    });
    await user.click(screen.getByRole("button", { name: "Older" }));

    expect(await screen.findByText("There is no older usage.")).toBeInTheDocument();
    expect(usageUrls(get).at(-1)).toBe(
      `${USAGE_URL}?from_date=2026-10-01&to_date=2026-10-25&provider=anthropic&page_size=20&cursor=${CURSOR}`,
    );

    await user.clear(screen.getByLabelText("Provider"));
    await user.type(screen.getByLabelText("Provider"), "openai");
    await user.click(screen.getByRole("button", { name: "Apply filters" }));

    await waitFor(() => {
      expect(usageUrls(get).at(-1)).toBe(`${USAGE_URL}?from_date=2026-10-01&to_date=2026-10-25&provider=openai&page_size=20`);
    });
  });

  it("does not send a range longer than 92 days and says why", async () => {
    const user = userEvent.setup();
    const get = serve(() => page([event()]));
    renderPage();
    await screen.findByText("Customer service bot", { selector: "td" });

    // 2026-07-01 → 2026-10-01：to − from = 92 天，即 93 个日历日。
    setDate("From (Malaysia time)", "2026-07-01");
    setDate("To (Malaysia time)", "2026-10-01");
    await user.click(screen.getByRole("button", { name: "Apply filters" }));

    expect(await screen.findByText("Choose a date range of 92 days or less.")).toBeInTheDocument();
    expect(usageUrls(get)).toEqual([`${USAGE_URL}?${DEFAULT_QUERY}`]);
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Older" })).not.toBeInTheDocument();

    // 恰好 92 个日历日照常发。
    setDate("From (Malaysia time)", "2026-07-02");
    await user.click(screen.getByRole("button", { name: "Apply filters" }));

    await waitFor(() => {
      expect(usageUrls(get).at(-1)).toBe(`${USAGE_URL}?from_date=2026-07-02&to_date=2026-10-01&page_size=20`);
    });
    expect(screen.queryByText("Choose a date range of 92 days or less.")).not.toBeInTheDocument();
  });

  it("does not send a reversed range and says why", async () => {
    const user = userEvent.setup();
    const get = serve(() => page([event()]));
    renderPage();
    await screen.findByText("Customer service bot", { selector: "td" });

    setDate("From (Malaysia time)", "2026-10-20");
    setDate("To (Malaysia time)", "2026-10-10");
    await user.click(screen.getByRole("button", { name: "Apply filters" }));

    expect(await screen.findByText("The end date cannot be before the start date.")).toBeInTheDocument();
    expect(usageUrls(get)).toEqual([`${USAGE_URL}?${DEFAULT_QUERY}`]);
  });

  it.each<[string, string, DateRangeProblem | null]>([
    ["", "2026-10-25", "missing"],
    ["2019-12-31", "2020-01-31", "outOfRange"],
    ["2099-12-01", "2100-01-01", "outOfRange"],
    ["2026-02-30", "2026-03-01", "outOfRange"],
    ["2026-10-20", "2026-10-10", "reversed"],
    ["2026-07-01", "2026-10-01", "tooLong"],
    ["2026-07-02", "2026-10-01", null],
    ["2020-01-01", "2020-01-01", null],
    ["2099-12-31", "2099-12-31", null],
  ])("judges the range %s to %s as %s", (from, to, problem) => {
    expect(dateRangeProblem(from, to)).toBe(problem);
  });
});

describe("UsagePage event_id", () => {
  it("asks only by event_id, ignores the other filters, and says so", async () => {
    const get = serve(() => page([event()]));

    renderPage(`${ROUTES.portalUsage}?event_id=${EVENT_ID}`);

    await screen.findByText("Customer service bot", { selector: "td" });
    expect(usageUrls(get)).toEqual([`${USAGE_URL}?event_id=${EVENT_ID}&page_size=20`]);
    expect(screen.getByText("Showing a single usage event")).toBeInTheDocument();
    expect(screen.getByText(EVENT_ID, { selector: "code" })).toBeInTheDocument();
    for (const label of ["From (Malaysia time)", "To (Malaysia time)", "Provider", "Model", "Conversation ID"]) {
      expect(screen.getByLabelText(label)).toBeDisabled();
    }
    expect(screen.getByRole("button", { name: "Apply filters" })).toBeDisabled();
  });

  it("says so when no billed usage has that event id", async () => {
    serve(() => page([]));

    renderPage(`${ROUTES.portalUsage}?event_id=${EVENT_ID}`);

    expect(await screen.findByText("No billed usage has this event ID.")).toBeInTheDocument();
  });

  it("goes back to the date filters when the event id is cleared", async () => {
    const user = userEvent.setup();
    const get = serve(() => page([event()]));
    renderPage(`${ROUTES.portalUsage}?event_id=${EVENT_ID}`);
    await screen.findByText("Customer service bot", { selector: "td" });

    await user.click(screen.getByRole("button", { name: "Show all usage" }));

    await waitFor(() => {
      expect(usageUrls(get).at(-1)).toBe(`${USAGE_URL}?${DEFAULT_QUERY}`);
    });
    expect(screen.queryByText("Showing a single usage event")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Provider")).toBeEnabled();
  });
});

describe("UsagePage requests", () => {
  it("never asks an admin endpoint, only the two customer usage endpoints", async () => {
    const user = userEvent.setup();
    const get = serve((query) => (query.has("cursor") ? page([]) : page([event()], CURSOR)));
    renderPage();

    await screen.findByText("Customer service bot", { selector: "td" });
    await user.click(screen.getByText("Customer service bot", { selector: "td" }));
    await screen.findByRole("region", { name: "Request details" });
    await user.click(screen.getByRole("button", { name: "Older" }));
    await screen.findByText("There is no older usage.");

    const urls = get.mock.calls.map(([url]) => url);
    expect(urls).toContain(PROJECTS_URL);
    for (const url of urls) {
      expect(url).not.toContain("/api/v1/admin/");
      expect(url === PROJECTS_URL || url.startsWith(`${USAGE_URL}?`)).toBe(true);
      expect(url).not.toMatch(/tenant|customer_id/);
    }
  });

  it("still shows usage when the project list cannot be loaded", async () => {
    serve(() => page([event()]), new ApiError("INTERNAL_ERROR", "An unexpected error occurred.", "req-projects-500"));

    renderPage();

    expect(await screen.findByText("Customer service bot", { selector: "td" })).toBeInTheDocument();
    expect(screen.getByText("Projects could not be loaded")).toBeInTheDocument();
  });

  it("shows the region a screen reader and the acceptance script look for", async () => {
    serve(() => page([event()]));

    renderPage();

    const region = await screen.findByRole("region", { name: "Usage" });
    expect(await within(region).findByText("Customer service bot", { selector: "td" })).toBeInTheDocument();
  });
});

describe("UsagePage export", () => {
  const EXPORT_NAME = /Export CSV/;
  const FILE_NAME = "usage-events_2026-10-01_2026-10-25.csv";
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

  function file() {
    return {
      data: new Blob(["﻿id,event_id\r\n"], { type: "text/csv" }),
      status: 200,
      statusText: "",
      headers: { "content-disposition": `attachment; filename="${FILE_NAME}"` },
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

  it("exports with the applied filters, not the ones still being typed, and leaves the list alone", async () => {
    const user = userEvent.setup();
    const get = serve(() => page([event()]), PROJECTS, file);
    renderPage();
    await screen.findByText("Customer service bot", { selector: "td" });

    await choose("Project", "Sales assistant");
    await user.type(screen.getByLabelText("Provider"), " anthropic ");
    await user.click(screen.getByRole("button", { name: "Apply filters" }));
    await waitFor(() => {
      expect(usageUrls(get).at(-1)).toBe(
        `${USAGE_URL}?from_date=2026-10-01&to_date=2026-10-25&project_id=${OTHER_PROJECT_ID}&provider=anthropic&page_size=20`,
      );
    });
    // 填了但没点「Apply filters」：不算数。
    await user.type(screen.getByLabelText("Model"), "claude-sonnet-5-5");
    const listed = usageUrls(get).length;

    await user.click(screen.getByRole("button", { name: EXPORT_NAME }));

    await waitFor(() => {
      expect(downloads).toEqual([FILE_NAME]);
    });
    expect(exportUrls(get)).toEqual([
      `${EXPORT_URL}?from_date=2026-10-01&to_date=2026-10-25&project_id=${OTHER_PROJECT_ID}&provider=anthropic`,
    ]);
    const [, config] = get.mock.calls.find(([url]) => url.startsWith(EXPORT_URL)) ?? [];
    expect(config).toMatchObject({ responseType: "blob" });
    // 导出不重新请求列表，也不清空表格。
    expect(usageUrls(get)).toHaveLength(listed);
    expect(screen.getByText("Customer service bot", { selector: "td" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: EXPORT_NAME })).toBeEnabled();
    for (const url of exportUrls(get)) {
      expect(url).not.toMatch(/page_size|cursor/);
    }
    for (const [url] of get.mock.calls) {
      expect(url).not.toContain("/api/v1/admin/");
      expect(url).not.toMatch(/tenant|customer_id/);
    }
  });

  it("exports the default range when nothing was changed", async () => {
    const user = userEvent.setup();
    const get = serve(() => page([event()]), PROJECTS, file);
    renderPage();
    await screen.findByText("Customer service bot", { selector: "td" });

    await user.click(screen.getByRole("button", { name: EXPORT_NAME }));

    await waitFor(() => {
      expect(downloads).toEqual([FILE_NAME]);
    });
    expect(exportUrls(get)).toEqual([`${EXPORT_URL}?from_date=2026-10-01&to_date=2026-10-25`]);
  });

  it.each<[string, string, string]>([
    ["reversed", "2026-10-20", "2026-10-10"],
    ["longer than 92 days", "2026-07-01", "2026-10-01"],
    ["incomplete", "", "2026-10-10"],
  ])("cannot export when the applied range is %s", async (_, from, to) => {
    const user = userEvent.setup();
    const get = serve(() => page([event()]), PROJECTS, file);
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
  });

  it("cannot export while showing a single event, which has no date range", async () => {
    const get = serve(() => page([event()]), PROJECTS, file);

    renderPage(`${ROUTES.portalUsage}?event_id=${EVENT_ID}`);

    await screen.findByText("Customer service bot", { selector: "td" });
    expect(screen.getByRole("button", { name: EXPORT_NAME })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: EXPORT_NAME }));
    expect(exportUrls(get)).toEqual([]);
  });

  it("shows the button loading while exporting and does not export twice", async () => {
    const user = userEvent.setup();
    const get = serve(() => page([event()]), PROJECTS, () => PENDING);
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
    // 列表照常可用。
    expect(screen.getByRole("button", { name: "Apply filters" })).toBeEnabled();
  });

  it.each<[number, string, string]>([
    [422, "EXPORT_TOO_LARGE", "This export has more than 20,000 rows. Narrow the date range or add filters, then try again."],
    [
      422,
      "QUERY_TOO_BROAD",
      "This export covers too much usage to finish in time. Narrow the date range or change the filters, then try again.",
    ],
    [503, "EXPORT_BUSY", "Another export is in progress. Wait a few seconds, then try again."],
  ])("explains a %s %s in its own words", async (status, code, text) => {
    const user = userEvent.setup();
    serve(() => page([event()]), PROJECTS, () => failure(status, code, "req-export-specific"));
    renderPage();
    await screen.findByText("Customer service bot", { selector: "td" });

    await user.click(screen.getByRole("button", { name: EXPORT_NAME }));

    expect(await screen.findByText(text)).toBeInTheDocument();
    expect(screen.queryByText("Your usage could not be exported.")).not.toBeInTheDocument();
    expect(screen.queryByText("req-export-specific")).not.toBeInTheDocument();
    // 列表不受影响，按钮可以再点。
    expect(screen.getByText("Customer service bot", { selector: "td" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: EXPORT_NAME })).toBeEnabled();
    expect(downloads).toEqual([]);
  });

  it("shows any other failure with its request id", async () => {
    const user = userEvent.setup();
    serve(() => page([event()]), PROJECTS, () => failure(500, "INTERNAL_ERROR", "req-export-500"));
    renderPage();
    await screen.findByText("Customer service bot", { selector: "td" });

    await user.click(screen.getByRole("button", { name: EXPORT_NAME }));

    expect(await screen.findByText("Your usage could not be exported.")).toBeInTheDocument();
    expect(screen.getByText("req-export-500")).toBeInTheDocument();
    expect(screen.queryByText("Your usage could not be loaded.")).not.toBeInTheDocument();
    expect(downloads).toEqual([]);
  });

  describe("monthly summary", () => {
    // 导出中按钮里多一个名为「loading」的图标，可访问名称随之变化：按正则找（同 EXPORT_NAME）。
    const MONTHLY_NAME = /Export monthly summary/;
    const MONTH_LABEL = "Month (Malaysia time)";
    const INVALID_MONTH = "Choose a month from 2020-01 to 2026-10, written as YYYY-MM.";

    /** 月度汇总文件；不带 `Content-Disposition` 时页面要用兜底文件名。 */
    function summaryFile(filename: string | null = null) {
      return {
        data: new Blob(["\ufeffdate_myt,request_count,charged_amount,conversation_count\r\n"], { type: "text/csv" }),
        status: 200,
        statusText: "",
        headers: filename === null ? {} : { "content-disposition": `attachment; filename="${filename}"` },
      };
    }

    function setMonth(value: string): void {
      fireEvent.change(screen.getByLabelText(MONTH_LABEL), { target: { value } });
    }

    it.each<[string, string]>([
      // UTC 16:00 之前：马来西亚时间还是 10 月 31 日。
      ["2026-10-31T15:59:59.000Z", "2026-10"],
      // UTC 16:00 起：马来西亚时间已是 11 月 1 日。
      ["2026-10-31T16:00:00.000Z", "2026-11"],
    ])("at %s defaults to and is capped at %s", async (now, month) => {
      vi.setSystemTime(new Date(now));
      serve(() => page([event()]));

      renderPage();

      const region = await screen.findByRole("region", { name: "Monthly summary" });
      const input = within(region).getByLabelText(MONTH_LABEL);
      expect(input).toHaveAttribute("type", "month");
      expect(input).toHaveValue(month);
      expect(input).toHaveAttribute("min", "2020-01");
      expect(input).toHaveAttribute("max", month);
      expect(within(region).getByRole("button", { name: MONTHLY_NAME })).toBeEnabled();
      expect(currentMalaysiaMonth(new Date(now))).toBe(month);
    });

    it.each<[string, string]>([
      ["empty", ""],
      // jsdom 与浏览器一样把不合规的值清成空串；不支持 type="month" 的浏览器里它会原样留在文本框里，由 exportableMonth 挡住。
      ["not YYYY-MM", "2026-13"],
      ["before 2020-01", "2019-12"],
      ["after the current month", "2026-11"],
    ])("cannot export when the month is %s", async (_, value) => {
      const user = userEvent.setup();
      const get = serve(() => page([event()]), PROJECTS, file, summaryFile);
      renderPage();
      await screen.findByText("Customer service bot", { selector: "td" });

      setMonth(value);

      await waitFor(() => {
        expect(screen.getByRole("button", { name: MONTHLY_NAME })).toBeDisabled();
      });
      expect(screen.getByText(INVALID_MONTH)).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: MONTHLY_NAME }));
      expect(summaryExportUrls(get)).toEqual([]);
      // 请求级导出不受影响。
      expect(screen.getByRole("button", { name: EXPORT_NAME })).toBeEnabled();
      await user.click(screen.getByRole("button", { name: EXPORT_NAME }));
      await waitFor(() => {
        expect(downloads).toEqual([FILE_NAME]);
      });
      expect(summaryExportUrls(get)).toEqual([]);
    });

    it.each<[string, boolean]>([
      ["2026-10", true],
      ["2020-01", true],
      ["2026-09", true],
      ["", false],
      ["2026-1", false],
      ["2026-13", false],
      ["2026-00", false],
      ["26-10", false],
      ["2026/10", false],
      ["2026-10-01", false],
      ["2019-12", false],
      ["2026-11", false],
    ])("judges %s as exportable: %s", (month, exportable) => {
      expect(exportableMonth(month, "2026-10")).toBe(exportable);
    });

    it("asks for exactly the chosen month, ignores the usage filters, and leaves the list alone", async () => {
      const user = userEvent.setup();
      const get = serve(() => page([event()]), PROJECTS, file, () => summaryFile());
      renderPage();
      await screen.findByText("Customer service bot", { selector: "td" });

      // 用量页的筛选已应用：月度汇总不该带上它们。
      await choose("Project", "Sales assistant");
      await user.type(screen.getByLabelText("Provider"), "anthropic");
      await user.click(screen.getByRole("button", { name: "Apply filters" }));
      await waitFor(() => {
        expect(usageUrls(get).at(-1)).toBe(
          `${USAGE_URL}?from_date=2026-10-01&to_date=2026-10-25&project_id=${OTHER_PROJECT_ID}&provider=anthropic&page_size=20`,
        );
      });
      const listed = usageUrls(get).length;
      setMonth("2026-09");

      await user.click(screen.getByRole("button", { name: MONTHLY_NAME }));

      await waitFor(() => {
        expect(downloads).toEqual(["usage-summary_2026-09.csv"]);
      });
      expect(summaryExportUrls(get)).toEqual([`${SUMMARY_EXPORT_URL}?month=2026-09`]);
      const [, config] = get.mock.calls.find(([url]) => url.startsWith(SUMMARY_EXPORT_URL)) ?? [];
      expect(config).toMatchObject({ responseType: "blob" });
      expect(exportUrls(get)).toEqual([]);
      expect(usageUrls(get)).toHaveLength(listed);
      expect(screen.getByText("Customer service bot", { selector: "td" })).toBeInTheDocument();
      expect(screen.getByRole("button", { name: MONTHLY_NAME })).toBeEnabled();
      for (const [url] of get.mock.calls) {
        expect(url).not.toContain("/api/v1/admin/");
        expect(url).not.toMatch(/tenant|customer_id/);
      }
    });

    it("exports the current month by default, under the file name the backend gives", async () => {
      const user = userEvent.setup();
      const get = serve(() => page([event()]), PROJECTS, file, () => summaryFile("usage-summary_2026-10.csv"));
      renderPage();
      await screen.findByText("Customer service bot", { selector: "td" });

      await user.click(screen.getByRole("button", { name: MONTHLY_NAME }));

      await waitFor(() => {
        expect(downloads).toEqual(["usage-summary_2026-10.csv"]);
      });
      expect(summaryExportUrls(get)).toEqual([`${SUMMARY_EXPORT_URL}?month=2026-10`]);
    });

    it("can export while showing a single event, since the usage filters do not apply", async () => {
      const user = userEvent.setup();
      const get = serve(() => page([event()]), PROJECTS, file, () => summaryFile());
      renderPage(`${ROUTES.portalUsage}?event_id=${EVENT_ID}`);
      await screen.findByText("Customer service bot", { selector: "td" });
      expect(screen.getByRole("button", { name: EXPORT_NAME })).toBeDisabled();

      await user.click(screen.getByRole("button", { name: MONTHLY_NAME }));

      await waitFor(() => {
        expect(downloads).toEqual(["usage-summary_2026-10.csv"]);
      });
      expect(summaryExportUrls(get)).toEqual([`${SUMMARY_EXPORT_URL}?month=2026-10`]);
    });

    it("shows the button loading while exporting and does not export twice", async () => {
      const user = userEvent.setup();
      const get = serve(() => page([event()]), PROJECTS, file, () => PENDING);
      renderPage();
      await screen.findByText("Customer service bot", { selector: "td" });

      await user.click(screen.getByRole("button", { name: MONTHLY_NAME }));

      await waitFor(() => {
        expect(screen.getByRole("button", { name: MONTHLY_NAME })).toBeDisabled();
      });
      const button = screen.getByRole("button", { name: MONTHLY_NAME });
      expect(within(button).getByRole("img", { name: "loading" })).toBeInTheDocument();
      fireEvent.click(button);
      fireEvent.click(button);
      expect(summaryExportUrls(get)).toHaveLength(1);
      expect(downloads).toEqual([]);
      // 请求级导出与列表各管各的。
      expect(screen.getByRole("button", { name: EXPORT_NAME })).toBeEnabled();
      expect(screen.getByRole("button", { name: "Apply filters" })).toBeEnabled();
    });

    it("explains QUERY_TOO_BROAD in its own words, without a request id", async () => {
      const user = userEvent.setup();
      serve(() => page([event()]), PROJECTS, file, () => failure(422, "QUERY_TOO_BROAD", "req-summary-422"));
      renderPage();
      await screen.findByText("Customer service bot", { selector: "td" });

      await user.click(screen.getByRole("button", { name: MONTHLY_NAME }));

      expect(
        await screen.findByText(
          "This month's summary cannot be calculated right now. Try again later, or export the request details above for a shorter date range.",
        ),
      ).toBeInTheDocument();
      expect(screen.queryByText("The monthly summary could not be exported.")).not.toBeInTheDocument();
      expect(screen.queryByText("req-summary-422")).not.toBeInTheDocument();
      // 请求级导出的专门文案不该出现。
      expect(
        screen.queryByText(
          "This export covers too much usage to finish in time. Narrow the date range or change the filters, then try again.",
        ),
      ).not.toBeInTheDocument();
      expect(screen.getByRole("button", { name: MONTHLY_NAME })).toBeEnabled();
      expect(downloads).toEqual([]);
    });

    it("shows any other failure with its request id, and the list and Export CSV keep working", async () => {
      const user = userEvent.setup();
      const get = serve(() => page([event()]), PROJECTS, file, () => failure(500, "INTERNAL_ERROR", "req-summary-500"));
      renderPage();
      await screen.findByText("Customer service bot", { selector: "td" });

      await user.click(screen.getByRole("button", { name: MONTHLY_NAME }));

      expect(await screen.findByText("The monthly summary could not be exported.")).toBeInTheDocument();
      expect(screen.getByText("req-summary-500")).toBeInTheDocument();
      expect(screen.queryByText("Your usage could not be exported.")).not.toBeInTheDocument();
      expect(screen.queryByText("Your usage could not be loaded.")).not.toBeInTheDocument();
      expect(screen.getByText("Customer service bot", { selector: "td" })).toBeInTheDocument();
      expect(downloads).toEqual([]);

      await user.click(screen.getByRole("button", { name: EXPORT_NAME }));

      await waitFor(() => {
        expect(downloads).toEqual([FILE_NAME]);
      });
      expect(exportUrls(get)).toEqual([`${EXPORT_URL}?from_date=2026-10-01&to_date=2026-10-25`]);
      // 月度汇总的失败只在它自己那一块，换月份时收起。
      expect(screen.getByText("The monthly summary could not be exported.")).toBeInTheDocument();
      setMonth("2026-09");
      await waitFor(() => {
        expect(screen.queryByText("The monthly summary could not be exported.")).not.toBeInTheDocument();
      });
    });
  });
});
