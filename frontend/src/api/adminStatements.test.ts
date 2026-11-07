/**
 * 月度对账单的三个接口发到哪个地址、用什么方法、带什么查询参数。
 *
 * ⚠️ 参数名写错（`pageSize`、`customerId`）后端不报错（未知参数忽略），结果是筛选悄悄失效、永远停在第 1 页。
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, client } from "./client";
import { generateStatements, getStatement, listStatements } from "./adminStatements";

const ZERO_UUID = "00000000-0000-0000-0000-000000000000";

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
