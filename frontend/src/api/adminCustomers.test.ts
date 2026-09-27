/**
 * 客户管理请求发到哪个地址、带什么查询参数与请求体。
 *
 * ⚠️ 页面用例把整个 `api/adminCustomers` 模块 mock 掉了（它们测的是界面），所以
 * 路径与字段名写错时它们照样全绿 —— 与 `auth.test.ts` 记的是同一个盲区。
 * `page_size` 打成 `pageSize`、PATCH 打成 PUT，在浏览器里是一句 422 或 405。
 */

import { AxiosError, AxiosHeaders, type AxiosResponse } from "axios";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  createCustomer,
  getCustomer,
  listCustomers,
  updateCustomer,
} from "./adminCustomers";
import { ApiError, client } from "./client";

const CUSTOMER_ID = "00000000-0000-4000-8000-000000000000";

function envelope(data: unknown) {
  return { data: { success: true, data, error: null, request_id: "req-1" } };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("admin customer requests", () => {
  it("lists customers with the page and page_size the backend reads", async () => {
    const get = vi
      .spyOn(client, "get")
      .mockResolvedValue(envelope({ items: [], page: 2, page_size: 50, total: 0 }));

    const page = await listCustomers(2, 50);

    expect(get).toHaveBeenCalledWith("/api/v1/admin/customers?page=2&page_size=50", {});
    expect(page).toEqual({ items: [], page: 2, page_size: 50, total: 0 });
  });

  it("reads one customer by its public id", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope({ id: CUSTOMER_ID }));

    await getCustomer(CUSTOMER_ID);

    expect(get).toHaveBeenCalledWith(`/api/v1/admin/customers/${CUSTOMER_ID}`, {});
  });

  it("escapes an id from the address bar instead of letting it change the path", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope({}));

    await getCustomer("a/../b");

    expect(get).toHaveBeenCalledWith("/api/v1/admin/customers/a%2F..%2Fb", {});
  });

  it("creates a customer with the body as given", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope({ id: CUSTOMER_ID }));

    await createCustomer({ company_name: "Acme Sdn Bhd", email: "ops@example.com", phone: "+60 3" });

    expect(post).toHaveBeenCalledWith("/api/v1/admin/customers", {
      company_name: "Acme Sdn Bhd",
      email: "ops@example.com",
      phone: "+60 3",
    });
  });

  it("edits with PATCH and sends exactly the patch, nulls included", async () => {
    const patch = vi.spyOn(client, "patch").mockResolvedValue(envelope({ id: CUSTOMER_ID }));

    await updateCustomer(CUSTOMER_ID, { company_name: "Renamed", phone: null });

    // ⚠️ `phone: null` 是「清空」，必须原样发出去；被当成 undefined 丢掉的话，
    // 后端收到的是「没改 phone」，界面却说保存成功了。
    expect(patch).toHaveBeenCalledWith(`/api/v1/admin/customers/${CUSTOMER_ID}`, {
      company_name: "Renamed",
      phone: null,
    });
  });

  it("turns a 422 envelope into an ApiError carrying the backend message", async () => {
    const response = {
      data: {
        success: false,
        data: null,
        error: { code: "VALIDATION_ERROR", message: "Invalid field: body.email" },
        request_id: "req-422",
      },
      status: 422,
      statusText: "Unprocessable Entity",
      headers: {},
      config: { headers: new AxiosHeaders() },
    } satisfies AxiosResponse;
    vi.spyOn(client, "post").mockRejectedValue(
      new AxiosError("Request failed", "ERR_BAD_REQUEST", undefined, undefined, response),
    );

    const failure = createCustomer({ company_name: "Acme", email: "not-an-email" });

    await expect(failure).rejects.toBeInstanceOf(ApiError);
    await expect(failure).rejects.toMatchObject({
      code: "VALIDATION_ERROR",
      message: "Invalid field: body.email",
      requestId: "req-422",
    });
  });
});
