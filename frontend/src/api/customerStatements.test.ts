/**
 * 客户对账单两个接口发到哪个地址、带什么查询参数（AIH-TASK-162）。
 *
 * 钉住：列表是页码分页（`page` 在前、`page_size` 在后，与 `listCredentials` 同一写法），详情把 `statement_id` 原样放进
 * 路径、没有查询串，两个接口都**不带**任何租户或客户标识、不碰 `/api/v1/admin/`。
 *
 * PDF 下载（AIH-TASK-218）：GET `/api/v1/customer/statements/{statement_id}/download`，`statement_id` 原样放进路径、没有
 * 查询串；经 `downloadFile` 取 blob，文件名先认 `Content-Disposition`，取不到时用 `<statement_number>.pdf`。jsdom 没有
 * `URL.createObjectURL` / `revokeObjectURL`，按属性装上替身、用完还原；`<a>` 的 `click` 拦下来只记录文件名。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, client } from "./client";
import {
  CUSTOMER_STATEMENTS_QUERY_KEY,
  customerStatementDetailQueryKey,
  customerStatementListQueryKey,
  downloadCustomerStatement,
  getCustomerStatement,
  listCustomerStatements,
} from "./customerStatements";
import { DOWNLOAD_TIMEOUT_MS } from "./download";

const STATEMENTS_URL = "/api/v1/customer/statements";
const STATEMENT_ID = "00000000-0000-0000-0000-000000000000";
const STATEMENT_NUMBER = "ST-202612-0A1B2C3D";

const EMPTY_PAGE = { items: [], page: 1, page_size: 12, total: 0 };

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

describe("listCustomerStatements", () => {
  it("sends page and page_size, in that order, to /api/v1/customer/statements", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));

    const page = await listCustomerStatements(3, 12);

    expect(get).toHaveBeenCalledTimes(1);
    const [url] = get.mock.calls[0] ?? [];
    expect(String(url)).toBe(`${STATEMENTS_URL}?page=3&page_size=12`);
    expect(parse(String(url))).toEqual({
      path: STATEMENTS_URL,
      params: [
        ["page", "3"],
        ["page_size", "12"],
      ],
    });
    expect(page).toEqual(EMPTY_PAGE);
  });

  it("passes the abort signal through", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));
    const controller = new AbortController();

    await listCustomerStatements(1, 12, controller.signal);

    expect(get).toHaveBeenCalledWith(`${STATEMENTS_URL}?page=1&page_size=12`, { signal: controller.signal });
  });
});

describe("getCustomerStatement", () => {
  it("puts the statement_id into the path as given, with no query string", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope({ statement_id: STATEMENT_ID }));

    await getCustomerStatement(STATEMENT_ID);

    expect(get.mock.calls[0]).toEqual([`${STATEMENTS_URL}/${STATEMENT_ID}`, {}]);
  });

  it("does not reshape the id: the backend's 404 decides what is not a statement", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope({}));

    await getCustomerStatement("00000000-0000-0000-0000-00000000000A");

    expect(get.mock.calls[0]?.[0]).toBe(`${STATEMENTS_URL}/00000000-0000-0000-0000-00000000000A`);
  });

  it("throws the backend's error code and request_id", async () => {
    vi.spyOn(client, "get").mockResolvedValue({
      data: {
        success: false,
        data: null,
        error: { code: "STATEMENT_NOT_FOUND", message: "Statement not found." },
        request_id: "req-404",
      },
    });

    await expect(getCustomerStatement(STATEMENT_ID)).rejects.toMatchObject({
      code: "STATEMENT_NOT_FOUND",
      requestId: "req-404",
    });
  });
});

describe("downloadCustomerStatement", () => {
  const originalCreate = Object.getOwnPropertyDescriptor(URL, "createObjectURL");
  const originalRevoke = Object.getOwnPropertyDescriptor(URL, "revokeObjectURL");
  /** 被点击的链接的 download 属性（即存成的文件名）。 */
  let saved: string[];

  function restore(name: "createObjectURL" | "revokeObjectURL", descriptor: PropertyDescriptor | undefined): void {
    if (descriptor === undefined) {
      Reflect.deleteProperty(URL, name);
    } else {
      Object.defineProperty(URL, name, descriptor);
    }
  }

  function pdfResponse(headers: Record<string, string>) {
    return { data: new Blob(["%PDF-1.3"], { type: "application/pdf" }), status: 200, statusText: "", headers };
  }

  beforeEach(() => {
    Object.defineProperty(URL, "createObjectURL", {
      configurable: true,
      writable: true,
      value: vi.fn(() => "blob:https://portal.example/00000000-0000-0000-0000-000000000000"),
    });
    Object.defineProperty(URL, "revokeObjectURL", { configurable: true, writable: true, value: vi.fn() });
    saved = [];
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      saved.push(this.download);
    });
  });

  afterEach(() => {
    restore("createObjectURL", originalCreate);
    restore("revokeObjectURL", originalRevoke);
  });

  it("GETs the statement's download path as a blob, with no query string", async () => {
    const get = vi
      .spyOn(client, "get")
      .mockResolvedValue(pdfResponse({ "content-disposition": `attachment; filename="${STATEMENT_NUMBER}.pdf"` }));

    await downloadCustomerStatement(STATEMENT_ID, STATEMENT_NUMBER);

    expect(get.mock.calls).toEqual([
      [`${STATEMENTS_URL}/${STATEMENT_ID}/download`, { responseType: "blob", timeout: DOWNLOAD_TIMEOUT_MS }],
    ]);
    expect(saved).toEqual([`${STATEMENT_NUMBER}.pdf`]);
  });

  it("passes the abort signal through", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(pdfResponse({}));
    const controller = new AbortController();

    await downloadCustomerStatement(STATEMENT_ID, STATEMENT_NUMBER, controller.signal);

    expect(get.mock.calls).toEqual([
      [
        `${STATEMENTS_URL}/${STATEMENT_ID}/download`,
        { responseType: "blob", timeout: DOWNLOAD_TIMEOUT_MS, signal: controller.signal },
      ],
    ]);
  });

  it("takes the file name from Content-Disposition when there is one", async () => {
    vi.spyOn(client, "get").mockResolvedValue(pdfResponse({ "content-disposition": 'attachment; filename="other.pdf"' }));

    await downloadCustomerStatement(STATEMENT_ID, STATEMENT_NUMBER);

    expect(saved).toEqual(["other.pdf"]);
  });

  it("falls back to <statement_number>.pdf without a usable file name", async () => {
    vi.spyOn(client, "get").mockResolvedValue(pdfResponse({}));

    await downloadCustomerStatement(STATEMENT_ID, STATEMENT_NUMBER);

    expect(saved).toEqual([`${STATEMENT_NUMBER}.pdf`]);
  });

  it("does not reshape the id: the backend's 404 decides what is not a statement", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(pdfResponse({}));

    await downloadCustomerStatement("00000000-0000-0000-0000-00000000000A", STATEMENT_NUMBER);

    expect(get.mock.calls[0]?.[0]).toBe(`${STATEMENTS_URL}/00000000-0000-0000-0000-00000000000A/download`);
  });

  it("throws the backend's error code and request_id", async () => {
    vi.spyOn(client, "get").mockRejectedValue(new ApiError("STATEMENT_NOT_FOUND", "Statement not found.", "req-404"));

    await expect(downloadCustomerStatement(STATEMENT_ID, STATEMENT_NUMBER)).rejects.toMatchObject({
      code: "STATEMENT_NOT_FOUND",
      requestId: "req-404",
    });
    expect(saved).toEqual([]);
  });
});

describe("customer statements requests", () => {
  it("never send a tenant or customer identifier and never touch admin paths", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));

    await listCustomerStatements(1, 12);
    await getCustomerStatement(STATEMENT_ID);

    expect(get).toHaveBeenCalledTimes(2);
    for (const [url] of get.mock.calls) {
      expect(url).not.toMatch(/tenant|customer_id|\/admin\//);
    }
  });

  it("keeps the list pages and the details under the customer statements prefix", () => {
    const prefix = [...CUSTOMER_STATEMENTS_QUERY_KEY];
    const list = [...customerStatementListQueryKey(1, 12)];
    const detail = [...customerStatementDetailQueryKey(STATEMENT_ID)];
    for (const key of [list, detail]) {
      expect(key.slice(0, prefix.length)).toEqual(prefix);
    }
    expect(list).not.toEqual([...customerStatementListQueryKey(2, 12)]);
    expect(prefix).not.toEqual(["admin", "statements"]);
  });
});
