/**
 * 月度对账单的三个接口发到哪个地址、用什么方法、带什么查询参数。
 *
 * ⚠️ 参数名写错（`pageSize`、`customerId`）后端不报错（未知参数忽略），结果是筛选悄悄失效、永远停在第 1 页。
 *
 * PDF 下载（AIH-TASK-219）：GET `/api/v1/admin/statements/{statement_id}/download`，`statement_id` 原样放进路径、没有
 * 查询串；经 `downloadFile` 取 blob，文件名先认 `Content-Disposition`，取不到时用 `<statement_number>.pdf`。jsdom 没有
 * `URL.createObjectURL` / `revokeObjectURL`，按属性装上替身、用完还原；`<a>` 的 `click` 拦下来只记录文件名。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, client } from "./client";
import { downloadAdminStatement, generateStatements, getStatement, listStatements } from "./adminStatements";
import { DOWNLOAD_TIMEOUT_MS } from "./download";

const ZERO_UUID = "00000000-0000-0000-0000-000000000000";
const STATEMENT_NUMBER = "ST-202612-0A1B2C3D";

function envelope(data: unknown) {
  return { data: { success: true, data, error: null, request_id: "req-1" } };
}

const EMPTY_PAGE = { items: [], page: 1, page_size: 20, total: 0 };

afterEach(() => {
  vi.restoreAllMocks();
});

describe("admin statement list", () => {
  it("lists statements with only page and page_size when there is no filter", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope({ ...EMPTY_PAGE, page: 2, page_size: 50 }));

    const page = await listStatements({}, 2, 50);

    expect(get).toHaveBeenCalledWith("/api/v1/admin/statements?page=2&page_size=50", {});
    expect(page).toEqual({ items: [], page: 2, page_size: 50, total: 0 });
  });

  it("adds customer_id when a customer is given", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));

    await listStatements({ customer_id: ZERO_UUID }, 1, 20);

    expect(get).toHaveBeenCalledWith(`/api/v1/admin/statements?page=1&page_size=20&customer_id=${ZERO_UUID}`, {});
  });

  it("adds period when a period is given", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));

    await listStatements({ period: "2026-12" }, 1, 20);

    expect(get).toHaveBeenCalledWith("/api/v1/admin/statements?page=1&page_size=20&period=2026-12", {});
  });

  it("adds both filters together", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));

    await listStatements({ customer_id: ZERO_UUID, period: "2027-01" }, 3, 20);

    expect(get).toHaveBeenCalledWith(
      `/api/v1/admin/statements?page=3&page_size=20&customer_id=${ZERO_UUID}&period=2027-01`,
      {},
    );
  });
});

describe("admin statement detail", () => {
  it("asks for the statement under its id", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope({ statement_id: ZERO_UUID }));

    await getStatement(ZERO_UUID);

    expect(get).toHaveBeenCalledWith(`/api/v1/admin/statements/${ZERO_UUID}`, {});
  });

  it("puts the statement id into the path exactly as given, without checking its shape", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope({}));

    await getStatement("Not-A-Statement");

    // 形状由后端判（404 STATEMENT_NOT_FOUND），这一层不拦、不改大小写。
    expect(get).toHaveBeenCalledWith("/api/v1/admin/statements/Not-A-Statement", {});
  });

  it("turns a failure envelope into an ApiError with the request id", async () => {
    vi.spyOn(client, "get").mockResolvedValue({
      data: {
        success: false,
        data: null,
        error: { code: "STATEMENT_NOT_FOUND", message: "Statement not found." },
        request_id: "req-404",
      },
    });

    const failure = await getStatement(ZERO_UUID).catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(ApiError);
    expect(failure).toMatchObject({ code: "STATEMENT_NOT_FOUND", requestId: "req-404" });
  });
});

describe("admin statement download", () => {
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
      value: vi.fn(() => `blob:https://admin.example/${ZERO_UUID}`),
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

  it("GETs the statement's admin download path as a blob, with no query string", async () => {
    const get = vi
      .spyOn(client, "get")
      .mockResolvedValue(pdfResponse({ "content-disposition": `attachment; filename="${STATEMENT_NUMBER}.pdf"` }));

    await downloadAdminStatement(ZERO_UUID, STATEMENT_NUMBER);

    expect(get.mock.calls).toEqual([
      [`/api/v1/admin/statements/${ZERO_UUID}/download`, { responseType: "blob", timeout: DOWNLOAD_TIMEOUT_MS }],
    ]);
    expect(saved).toEqual([`${STATEMENT_NUMBER}.pdf`]);
  });

  it("passes the abort signal through", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(pdfResponse({}));
    const controller = new AbortController();

    await downloadAdminStatement(ZERO_UUID, STATEMENT_NUMBER, controller.signal);

    expect(get.mock.calls).toEqual([
      [
        `/api/v1/admin/statements/${ZERO_UUID}/download`,
        { responseType: "blob", timeout: DOWNLOAD_TIMEOUT_MS, signal: controller.signal },
      ],
    ]);
  });

  it("takes the file name from Content-Disposition when there is one", async () => {
    vi.spyOn(client, "get").mockResolvedValue(pdfResponse({ "content-disposition": 'attachment; filename="other.pdf"' }));

    await downloadAdminStatement(ZERO_UUID, STATEMENT_NUMBER);

    expect(saved).toEqual(["other.pdf"]);
  });

  it("falls back to <statement_number>.pdf without a usable file name", async () => {
    vi.spyOn(client, "get").mockResolvedValue(pdfResponse({}));

    await downloadAdminStatement(ZERO_UUID, STATEMENT_NUMBER);

    expect(saved).toEqual([`${STATEMENT_NUMBER}.pdf`]);
  });

  it("puts the statement id into the path exactly as given", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(pdfResponse({}));

    await downloadAdminStatement("Not-A-Statement", STATEMENT_NUMBER);

    // 形状由后端判（404 STATEMENT_NOT_FOUND），这一层不拦、不改大小写。
    expect(get.mock.calls[0]?.[0]).toBe("/api/v1/admin/statements/Not-A-Statement/download");
  });

  it("throws the backend's error code and request_id", async () => {
    vi.spyOn(client, "get").mockRejectedValue(new ApiError("STATEMENT_NOT_FOUND", "Statement not found.", "req-404"));

    await expect(downloadAdminStatement(ZERO_UUID, STATEMENT_NUMBER)).rejects.toMatchObject({
      code: "STATEMENT_NOT_FOUND",
      requestId: "req-404",
    });
    expect(saved).toEqual([]);
  });
});

describe("admin statement generation", () => {
  it("POSTs to the customer's generate path without a body and returns the new statements", async () => {
    const generated = [{ statement_id: ZERO_UUID, period: "2026-12" }];
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope(generated));
    const get = vi.spyOn(client, "get");

    const result = await generateStatements(ZERO_UUID);

    // 只有地址这一个参数：没有请求体。
    expect(post).toHaveBeenCalledWith(`/api/v1/admin/customers/${ZERO_UUID}/statements/generate`);
    expect(post.mock.calls[0]).toHaveLength(1);
    expect(get).not.toHaveBeenCalled();
    expect(result).toEqual(generated);
  });

  it("puts the customer id into the path exactly as given", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope([]));

    await generateStatements("Not-A-Customer");

    expect(post).toHaveBeenCalledWith("/api/v1/admin/customers/Not-A-Customer/statements/generate");
  });

  it("turns a failure into an ApiError with the request id", async () => {
    vi.spyOn(client, "post").mockResolvedValue({
      data: {
        success: false,
        data: null,
        error: { code: "INTERNAL_ERROR", message: "An unexpected error occurred." },
        request_id: "req-500",
      },
    });

    const failure = await generateStatements(ZERO_UUID).catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(ApiError);
    expect(failure).toMatchObject({ code: "INTERNAL_ERROR", requestId: "req-500" });
  });
});
