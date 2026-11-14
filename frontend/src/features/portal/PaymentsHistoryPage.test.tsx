/**
 * 门户支付历史页（AIH-TASK-193，设计闸门 #587 v3 §2「门户页面」、§7「支付历史与付款结果页」）。
 *
 * 与其他门户页的测试同一写法：不 mock `api/customerPayments`，而是在 `client.get` 上按路径回应，钉住**页面真正发出去的
 * 请求** —— 只问 `/api/v1/customer/payments?page=N&page_size=20`，任何时候都不请求 `/api/v1/admin/`。
 *
 * 要钉住的几件：
 * - 列：编号、金额、状态、实际入账额、创建时间与付款时间（+8 显示、列头标 MYT）；七种状态各有文案；空值显示短横。
 * - 分页：页码在查询串 `page` 里；不是正整数时按 1 读并把地址改成 `page=1`；超过最后一页（total > 0）时跳到最后一页；
 *   上一页 / 下一页按 total 判断。
 * - 点一行进付款结果页，路由 state 带上当前页码。
 * - 加载中、失败（带 request_id）、空列表。
 * - 收据列（AIH-TASK-204，设计闸门 #589 v2 §2「门户页面」、§7「门户页面」）：有收据的行是收据号与下载按钮，没有的是短横；
 *   下载走 `/api/v1/customer/receipts/{receipt_id}/download`、不进结果页；下载中只有这一行的按钮不可点；失败在表格上方
 *   显示错误码、信息与 request_id，只留最近一次，下一次点下载或翻页时清除，翻页之后才失败的照样显示。
 *
 * jsdom 没有 `URL.createObjectURL` / `revokeObjectURL`：按属性装上替身、用完还原；`<a>` 的 `click` 拦下来只记录文件名。
 * 收据用例与本文件其他用例同一写法：真时钟，只用 `waitFor` 等一个**正面**的结果（行出现、文件存下、错误出现、按钮恢复）；
 * 点击本身（`fireEvent` 包在同步的 `act` 里）落定的状态（按钮禁用、错误清除）点完立刻同步断言。不在 `act` 里等定时器：
 * 表格在后台还有自己的定时器与更新，异步的 `act` 要等它们全部排空才返回。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import type { Page } from "../../api/adminCustomers";
import { ApiError, client } from "../../api/client";
import type { CustomerPayment } from "../../api/customerPayments";
import { ROUTES } from "../../routes/paths";
import { PaymentsHistoryPage } from "./PaymentsHistoryPage";

const PAYMENTS_URL = "/api/v1/customer/payments";
const RECEIPT_DOWNLOAD = /^\/api\/v1\/customer\/receipts\/([^/]+)\/download$/;
const ID_0 = "00000000-0000-0000-0000-000000000000";
const ID_1 = "00000000-0000-0000-0000-000000000001";
const RECEIPT_0 = "ACV-AIR-2026-000001";
const RECEIPT_1 = "ACV-AIR-2026-000002";

const EMPTY = "—";
const DOWNLOAD_FAILED = "The receipt could not be downloaded.";

/** 让请求一直挂着（加载中）。 */
const PENDING = Symbol("pending");

function payment(overrides: Partial<CustomerPayment> = {}): CustomerPayment {
  return {
    id: ID_0,
    amount: "100.00",
    currency: "MYR",
    status: "PAID",
    credited_amount: "98.50",
    created_at: "2026-11-08T08:00:00",
    paid_at: "2026-11-08T16:30:00",
    expires_at: "2026-11-08T09:00:00",
    receipt_id: null,
    receipt_number: null,
    ...overrides,
  };
}

/** 有收据的支付：收据 id 与支付 id 相同（都是全零占位值），收据号另给。 */
function receipted(id: string, receiptNumber: string): CustomerPayment {
  return payment({ id, receipt_id: id, receipt_number: receiptNumber });
}

function pdf(filename: string) {
  return {
    data: new Blob(["%PDF-1.3"], { type: "application/pdf" }),
    status: 200,
    statusText: "",
    headers: { "content-disposition": `attachment; filename="${filename}"` },
  };
}

interface Deferred {
  promise: Promise<unknown>;
  resolve: (value: unknown) => void;
  reject: (error: Error) => void;
}

function deferred(): Deferred {
  let resolve!: (value: unknown) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<unknown>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function page(items: CustomerPayment[], total: number, pageNumber = 1): Page<CustomerPayment> {
  return { items, page: pageNumber, page_size: 20, total };
}

function envelope(data: unknown) {
  return { data: { success: true, data, error: null, request_id: "req-1" } };
}

/**
 * 按路径回应 `client.get`：列表的每一次请求交给 `handler(页码)`，收据下载交给 `download(收据 id)`（它回的就是 `client.get`
 * 的结果）。别的路径一律当成失败 —— 页面不该问它们，下面也会断言没有问。
 */
function serve(handler: (pageNumber: number) => unknown, download?: (receiptId: string) => Promise<unknown>) {
  return vi.spyOn(client, "get").mockImplementation((url: string) => {
    const parsed = new URL(url, "https://portal.test");
    const receipt = RECEIPT_DOWNLOAD.exec(parsed.pathname);
    if (receipt !== null && download !== undefined && parsed.search === "") {
      return download(receipt[1]!);
    }
    if (parsed.pathname !== PAYMENTS_URL) {
      return Promise.reject(new ApiError("NOT_FOUND", "Unexpected request.", null));
    }
    const value = handler(Number(parsed.searchParams.get("page")));
    if (value === PENDING) {
      return new Promise(() => undefined);
    }
    return value instanceof Error ? Promise.reject(value) : Promise.resolve(envelope(value));
  });
}

/** 页面发出的列表请求的查询串，按顺序。 */
function listQueries(get: ReturnType<typeof serve>): string[] {
  return get.mock.calls
    .map(([url]) => new URL(url, "https://portal.test"))
    .filter((parsed) => parsed.pathname === PAYMENTS_URL)
    .map((parsed) => parsed.search);
}

function noAdminRequests(get: ReturnType<typeof serve>): void {
  expect(get.mock.calls.filter(([url]) => url.includes("/api/v1/admin/"))).toEqual([]);
}

/** 当前地址（路径 + 查询串）与路由 state，放在属性里给用例读。 */
function LocationProbe() {
  const location = useLocation();
  return (
    <output
      data-testid="location"
      data-href={`${location.pathname}${location.search}`}
      data-state={JSON.stringify(location.state ?? null)}
    />
  );
}

function renderPage(entry: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[entry]}>
        <Routes>
          <Route path={ROUTES.portalPayments} element={<PaymentsHistoryPage />} />
          <Route path={ROUTES.portalPaymentResult} element={null} />
        </Routes>
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function currentHref(): string | null {
  return screen.getByTestId("location").getAttribute("data-href");
}

function currentState(): unknown {
  return JSON.parse(screen.getByTestId("location").getAttribute("data-state") ?? "null");
}

function region(): HTMLElement {
  return screen.getByRole("region", { name: "Payment history" });
}

/** 表格里的数据行（不含表头）。 */
function dataRows(): HTMLElement[] {
  return Array.from(region().querySelectorAll<HTMLElement>("tbody tr[data-row-key]"));
}

function cellTexts(row: HTMLElement): string[] {
  return Array.from(row.querySelectorAll("td")).map((cell) => (cell.textContent ?? "").trim());
}

async function rowsShown(count: number): Promise<HTMLElement[]> {
  await waitFor(() => expect(dataRows()).toHaveLength(count));
  return dataRows();
}

function button(name: string): HTMLButtonElement {
  return within(region()).getByRole<HTMLButtonElement>("button", { name });
}

/** 按文字找按钮：antd 页面上 `getByRole("button", { name })` 每次要算可访问名称，单次约 1 秒，反复调用会拖到超时。 */
function downloadButton(row: HTMLElement): HTMLButtonElement {
  const found = within(row).getByText("Download").closest("button");
  expect(found).not.toBeNull();
  return found!;
}

/** 第 index 个数据行的下载按钮：每次重新找行，不拿重绘之前留下的引用。 */
function downloadAt(index: number): HTMLButtonElement {
  return downloadButton(dataRows()[index]!);
}

/** 页面发出的收据下载请求的路径，按顺序。 */
function downloadPaths(get: ReturnType<typeof serve>): string[] {
  return get.mock.calls
    .map(([url]) => new URL(url, "https://portal.test"))
    .filter((parsed) => RECEIPT_DOWNLOAD.test(parsed.pathname))
    .map((parsed) => `${parsed.pathname}${parsed.search}`);
}

const originalCreate = Object.getOwnPropertyDescriptor(URL, "createObjectURL");
const originalRevoke = Object.getOwnPropertyDescriptor(URL, "revokeObjectURL");

function restore(name: "createObjectURL" | "revokeObjectURL", descriptor: PropertyDescriptor | undefined): void {
  if (descriptor === undefined) {
    Reflect.deleteProperty(URL, name);
  } else {
    Object.defineProperty(URL, name, descriptor);
  }
}

/** 存下的文件名（被点击的链接的 download 属性），按顺序。 */
let saved: string[];

beforeEach(() => {
  Object.defineProperty(URL, "createObjectURL", {
    configurable: true,
    writable: true,
    value: vi.fn(() => "blob:https://portal.test/00000000-0000-0000-0000-000000000000"),
  });
  Object.defineProperty(URL, "revokeObjectURL", { configurable: true, writable: true, value: vi.fn() });
  saved = [];
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
    saved.push(this.download);
  });
});

afterEach(() => {
  vi.restoreAllMocks();
  restore("createObjectURL", originalCreate);
  restore("revokeObjectURL", originalRevoke);
});

describe("PaymentsHistoryPage", () => {
  it("shows the columns, money and Kuala Lumpur times, and asks only for the customer payments", async () => {
    const get = serve(() => page([payment()], 1));

    renderPage(`${ROUTES.portalPayments}?page=1`);
    const [row] = await rowsShown(1);

    const heads = Array.from(region().querySelectorAll("thead th")).map((th) => (th.textContent ?? "").trim());
    expect(heads).toEqual([
      "Payment ID",
      "Amount",
      "Status",
      "Amount credited",
      "Created at (MYT)",
      "Paid at (MYT)",
      "Receipt",
    ]);
    // 08:00 UTC → 16:00 MYT；16:30 UTC → 次日 00:30 MYT。
    expect(cellTexts(row!)).toEqual([
      ID_0,
      "MYR 100.00",
      "Credited",
      "MYR 98.50",
      "2026-11-08 16:00:00",
      "2026-11-09 00:30:00",
      EMPTY,
    ]);
    expect(listQueries(get)).toEqual(["?page=1&page_size=20"]);
    noAdminRequests(get);
  });

  it("labels each of the seven customer statuses", async () => {
    const statuses = [
      ["PENDING", "Awaiting payment"],
      ["PAID", "Credited"],
      ["IN_REVIEW", "Under review. If you have questions, please contact support."],
      ["NOT_CREDITED", "Not credited. If you have questions, please contact support."],
      ["FAILED", "Payment failed"],
      ["EXPIRED", "Expired"],
      ["CANCELLED", "Cancelled"],
    ] as const;
    serve(() =>
      page(
        statuses.map(([status], index) => payment({ id: `00000000-0000-0000-0000-00000000000${index}`, status })),
        statuses.length,
      ),
    );

    renderPage(`${ROUTES.portalPayments}?page=1`);
    const rows = await rowsShown(statuses.length);

    expect(rows.map((row) => cellTexts(row)[2])).toEqual(statuses.map(([, label]) => label));
  });

  it("shows a dash when nothing was credited or paid yet", async () => {
    serve(() => page([payment({ status: "PENDING", credited_amount: null, paid_at: null })], 1));

    renderPage(`${ROUTES.portalPayments}?page=1`);
    const [row] = await rowsShown(1);

    const cells = cellTexts(row!);
    expect(cells[3]).toBe(EMPTY);
    expect(cells[5]).toBe(EMPTY);
    expect(cells[4]).toBe("2026-11-08 16:00:00");
  });

  it("reads the page from the query string", async () => {
    const get = serve((pageNumber) => page([payment()], 45, pageNumber));

    renderPage(`${ROUTES.portalPayments}?page=2`);
    await rowsShown(1);

    expect(listQueries(get)).toEqual(["?page=2&page_size=20"]);
    expect(currentHref()).toBe(`${ROUTES.portalPayments}?page=2`);
    expect(screen.getByText("Page 2 of 3 · 45 payments")).toBeInTheDocument();
  });

  it.each(["0", "abc", "-1", "1.5"])("treats page=%s as page 1 and rewrites the address", async (raw) => {
    const get = serve((pageNumber) => page([payment()], 1, pageNumber));

    renderPage(`${ROUTES.portalPayments}?page=${raw}`);
    await rowsShown(1);

    await waitFor(() => expect(currentHref()).toBe(`${ROUTES.portalPayments}?page=1`));
    expect(new Set(listQueries(get))).toEqual(new Set(["?page=1&page_size=20"]));
  });

  it("jumps to the last page when the page is past the end", async () => {
    const get = serve((pageNumber) =>
      pageNumber === 3 ? page([payment()], 45, pageNumber) : page([], 45, pageNumber),
    );

    renderPage(`${ROUTES.portalPayments}?page=9`);

    await waitFor(() => expect(currentHref()).toBe(`${ROUTES.portalPayments}?page=3`));
    await rowsShown(1);
    expect(listQueries(get)).toEqual(["?page=9&page_size=20", "?page=3&page_size=20"]);
    expect(screen.getByText("Page 3 of 3 · 45 payments")).toBeInTheDocument();
  });

  it("pages with Previous and Next by total, keeping the page in the address", async () => {
    const get = serve((pageNumber) =>
      page([payment({ id: pageNumber === 1 ? ID_0 : ID_1 })], 21, pageNumber),
    );

    renderPage(`${ROUTES.portalPayments}?page=1`);
    await rowsShown(1);

    expect(button("Previous")).toBeDisabled();
    expect(button("Next")).toBeEnabled();

    fireEvent.click(button("Next"));
    await waitFor(() => expect(currentHref()).toBe(`${ROUTES.portalPayments}?page=2`));
    await waitFor(() => expect(dataRows()[0]).toHaveAttribute("data-row-key", ID_1));
    // 21 条、每页 20：第 2 页是最后一页。
    expect(button("Next")).toBeDisabled();
    expect(button("Previous")).toBeEnabled();

    fireEvent.click(button("Previous"));
    await waitFor(() => expect(currentHref()).toBe(`${ROUTES.portalPayments}?page=1`));
    await waitFor(() => expect(dataRows()[0]).toHaveAttribute("data-row-key", ID_0));
    expect(listQueries(get)).toEqual(["?page=1&page_size=20", "?page=2&page_size=20", "?page=1&page_size=20"]);
    noAdminRequests(get);
  });

  it("opens the payment result page for a row, carrying the current page in the route state", async () => {
    serve((pageNumber) => page([payment({ id: ID_1 })], 45, pageNumber));

    renderPage(`${ROUTES.portalPayments}?page=2`);
    const [row] = await rowsShown(1);

    fireEvent.click(row!);

    // 结果页的地址本身不带参数，与建单时的 success_url 一致。
    await waitFor(() => expect(currentHref()).toBe(`${ROUTES.portalPayments}/${ID_1}`));
    expect(currentState()).toEqual({ fromPage: 2 });
  });

  it("shows the loading state", async () => {
    serve(() => PENDING);

    renderPage(`${ROUTES.portalPayments}?page=1`);

    expect(await screen.findByText("Loading your payments…")).toBeInTheDocument();
    expect(region().querySelector('[aria-busy="true"]')).not.toBeNull();
  });

  it("shows the failure with its request_id and a retry", async () => {
    let calls = 0;
    serve(() => {
      calls += 1;
      return calls === 1 ? new ApiError("INTERNAL_ERROR", "Something went wrong.", "req-500") : page([payment()], 1);
    });

    renderPage(`${ROUTES.portalPayments}?page=1`);

    expect(await screen.findByText("Your payments could not be loaded.")).toBeInTheDocument();
    expect(screen.getByText("req-500")).toBeInTheDocument();

    fireEvent.click(button("Try again"));
    await rowsShown(1);
    expect(screen.queryByText("Your payments could not be loaded.")).not.toBeInTheDocument();
  });

  it("shows the empty state when there are no payments", async () => {
    serve(() => page([], 0));

    renderPage(`${ROUTES.portalPayments}?page=1`);

    expect(await screen.findByText("You have no payments yet.")).toBeInTheDocument();
    expect(dataRows()).toHaveLength(0);
    expect(currentHref()).toBe(`${ROUTES.portalPayments}?page=1`);
  });
});

describe("PaymentsHistoryPage receipts", () => {
  it("shows the receipt number and a download button for a row with a receipt, and a dash without one", async () => {
    serve(() => page([receipted(ID_0, RECEIPT_0), payment({ id: ID_1, status: "PENDING", paid_at: null })], 2));

    renderPage(`${ROUTES.portalPayments}?page=1`);
    const [withReceipt, withoutReceipt] = await rowsShown(2);

    expect(cellTexts(withReceipt!)[6]).toBe(`${RECEIPT_0}Download`);
    expect(downloadButton(withReceipt!)).toBeEnabled();
    expect(cellTexts(withoutReceipt!)[6]).toBe(EMPTY);
    expect(within(withoutReceipt!).queryByRole("button")).toBeNull();
  });

  it("downloads the receipt by its id without opening the payment result page", async () => {
    const get = serve(
      () => page([receipted(ID_0, RECEIPT_0)], 1),
      () => Promise.resolve(pdf(`${RECEIPT_0}.pdf`)),
    );

    renderPage(`${ROUTES.portalPayments}?page=1`);
    await rowsShown(1);

    fireEvent.click(downloadAt(0));

    await waitFor(() => expect(saved).toEqual([`${RECEIPT_0}.pdf`]));
    await waitFor(() => expect(downloadAt(0)).toBeEnabled());
    expect(downloadPaths(get)).toEqual([`/api/v1/customer/receipts/${ID_0}/download`]);
    expect(currentHref()).toBe(`${ROUTES.portalPayments}?page=1`);
    expect(currentState()).toBeNull();
    expect(screen.queryByText(DOWNLOAD_FAILED)).not.toBeInTheDocument();
    noAdminRequests(get);
  });

  it("does not open the payment result page from the download button's keys either", async () => {
    serve(
      () => page([receipted(ID_0, RECEIPT_0)], 1),
      () => new Promise(() => undefined),
    );

    renderPage(`${ROUTES.portalPayments}?page=1`);
    await rowsShown(1);

    fireEvent.keyDown(downloadAt(0), { key: "Enter" });

    expect(currentHref()).toBe(`${ROUTES.portalPayments}?page=1`);
  });

  it("disables only the downloading row's button while its download is in flight", async () => {
    const first = deferred();
    const get = serve(
      () => page([receipted(ID_0, RECEIPT_0), receipted(ID_1, RECEIPT_1)], 2),
      (receiptId) => (receiptId === ID_0 ? first.promise : Promise.resolve(pdf(`${RECEIPT_1}.pdf`))),
    );

    renderPage(`${ROUTES.portalPayments}?page=1`);
    await rowsShown(2);

    fireEvent.click(downloadAt(0));
    expect(downloadAt(0)).toBeDisabled();
    expect(downloadAt(1)).toBeEnabled();

    // 下载中再点：不发第二个请求，也不进结果页。
    fireEvent.click(downloadAt(0));
    expect(downloadPaths(get)).toHaveLength(1);
    expect(currentHref()).toBe(`${ROUTES.portalPayments}?page=1`);

    // 其他行照常可点。
    fireEvent.click(downloadAt(1));
    await waitFor(() => expect(saved).toEqual([`${RECEIPT_1}.pdf`]));
    await waitFor(() => expect(downloadAt(1)).toBeEnabled());
    expect(downloadAt(0)).toBeDisabled();

    first.resolve(pdf(`${RECEIPT_0}.pdf`));
    await waitFor(() => expect(downloadAt(0)).toBeEnabled());
    expect(saved).toEqual([`${RECEIPT_1}.pdf`, `${RECEIPT_0}.pdf`]);
    expect(downloadPaths(get)).toEqual([
      `/api/v1/customer/receipts/${ID_0}/download`,
      `/api/v1/customer/receipts/${ID_1}/download`,
    ]);
  });

  it("shows a failed download above the table with its code, message and request_id", async () => {
    serve(
      () => page([receipted(ID_0, RECEIPT_0)], 1),
      () => Promise.reject(new ApiError("RECEIPT_NOT_FOUND", "Receipt not found.", "req-404")),
    );

    renderPage(`${ROUTES.portalPayments}?page=1`);
    await rowsShown(1);

    fireEvent.click(downloadAt(0));

    const failed = await waitFor(() => within(region()).getByText(DOWNLOAD_FAILED));
    await waitFor(() => expect(downloadAt(0)).toBeEnabled());
    expect(within(region()).getByText("RECEIPT_NOT_FOUND")).toBeInTheDocument();
    expect(within(region()).getByText(/Receipt not found\./)).toBeInTheDocument();
    expect(within(region()).getByText("req-404")).toBeInTheDocument();
    const table = region().querySelector("table");
    expect(table).not.toBeNull();
    expect(failed.compareDocumentPosition(table!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(saved).toEqual([]);
    expect(currentHref()).toBe(`${ROUTES.portalPayments}?page=1`);
  });

  it("keeps only the latest failure and clears it on the next download click", async () => {
    const first = deferred();
    const second = deferred();
    // 每次下载各回一个：第三次（重点第一行）一直挂着，只看点下去那一刻清掉了错误。
    const responses = [first.promise, second.promise, new Promise(() => undefined)];
    serve(
      () => page([receipted(ID_0, RECEIPT_0), receipted(ID_1, RECEIPT_1)], 2),
      () => responses.shift() ?? Promise.reject(new Error("Unexpected download.")),
    );

    renderPage(`${ROUTES.portalPayments}?page=1`);
    await rowsShown(2);

    fireEvent.click(downloadAt(0));
    fireEvent.click(downloadAt(1));
    first.reject(new ApiError("INTERNAL_ERROR", "Something went wrong.", "req-a"));
    await waitFor(() => expect(within(region()).getByText("req-a")).toBeInTheDocument());

    second.reject(new ApiError("RECEIPT_NOT_FOUND", "Receipt not found.", "req-b"));
    await waitFor(() => expect(within(region()).getByText("req-b")).toBeInTheDocument());
    expect(within(region()).queryByText("req-a")).not.toBeInTheDocument();
    expect(within(region()).getAllByText(DOWNLOAD_FAILED)).toHaveLength(1);

    // 下一次点任一下载：先清掉。
    await waitFor(() => expect(downloadAt(0)).toBeEnabled());
    fireEvent.click(downloadAt(0));
    expect(within(region()).queryByText(DOWNLOAD_FAILED)).not.toBeInTheDocument();
  });

  it("clears the failure when paging, and still shows a download that fails after paging", async () => {
    const slow = deferred();
    serve(
      (pageNumber) =>
        pageNumber === 1
          ? page([receipted(ID_0, RECEIPT_0), receipted(ID_1, RECEIPT_1)], 22, 1)
          : page([payment({ id: "00000000-0000-0000-0000-000000000002" })], 22, 2),
      (receiptId) =>
        receiptId === ID_0
          ? slow.promise
          : Promise.reject(new ApiError("RECEIPT_NOT_FOUND", "Receipt not found.", "req-b")),
    );

    renderPage(`${ROUTES.portalPayments}?page=1`);
    await rowsShown(2);

    fireEvent.click(downloadAt(0));
    fireEvent.click(downloadAt(1));
    await waitFor(() => expect(within(region()).getByText("req-b")).toBeInTheDocument());

    fireEvent.click(button("Next"));
    expect(within(region()).queryByText(DOWNLOAD_FAILED)).not.toBeInTheDocument();
    await waitFor(() => expect(currentHref()).toBe(`${ROUTES.portalPayments}?page=2`));
    await waitFor(() =>
      expect(dataRows()[0]).toHaveAttribute("data-row-key", "00000000-0000-0000-0000-000000000002"),
    );
    expect(within(region()).queryByText(DOWNLOAD_FAILED)).not.toBeInTheDocument();

    slow.reject(new ApiError("INTERNAL_ERROR", "Something went wrong.", "req-a"));
    await waitFor(() => expect(within(region()).getByText("req-a")).toBeInTheDocument());
    expect(within(region()).getByText("INTERNAL_ERROR")).toBeInTheDocument();
    expect(currentHref()).toBe(`${ROUTES.portalPayments}?page=2`);
  });
});
