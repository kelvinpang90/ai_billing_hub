/**
 * 客户接口发到哪个地址、带什么查询参数与请求体。
 *
 * ⚠️ 与 `auth.test.ts` 同一个盲区：页面用例把整个 `api/adminCustomers` 模块 mock
 * 掉了，路径与字段名写错时它们照样全绿。这里钉的是「发出去的确实是这个形状」。
 */

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
  it("lists customers with page and page_size in the query string", async () => {
    const page = { items: [], page: 2, page_size: 20, total: 0 };
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(page));

    await expect(listCustomers(2, 20)).resolves.toEqual(page);

    expect(get).toHaveBeenCalledWith("/api/v1/admin/customers?page=2&page_size=20", {});
  });

  it("reads one customer by its id", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope({ id: CUSTOMER_ID }));

    await getCustomer(CUSTOMER_ID);

    expect(get).toHaveBeenCalledWith(`/api/v1/admin/customers/${CUSTOMER_ID}`, {});
  });

  it("escapes whatever came from the address bar before putting it in the path", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope({}));

    await getCustomer("../audit?x=1");

    expect(get).toHaveBeenCalledWith("/api/v1/admin/customers/..%2Faudit%3Fx%3D1", {});
  });

  it("creates a customer with the snake_case body the backend reads", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope({ id: CUSTOMER_ID }));

    await createCustomer({
      company_name: "Acme Sdn Bhd",
      email: "ops@example.com",
      contact_name: "Contact Person",
    });

    expect(post).toHaveBeenCalledWith("/api/v1/admin/customers", {
      company_name: "Acme Sdn Bhd",
      email: "ops@example.com",
      contact_name: "Contact Person",
    });
  });

  it("patches only the fields it was given, nulls included", async () => {
    const patch = vi.spyOn(client, "patch").mockResolvedValue(envelope({ id: CUSTOMER_ID }));

    await updateCustomer(CUSTOMER_ID, { phone: null });

    expect(patch).toHaveBeenCalledWith(`/api/v1/admin/customers/${CUSTOMER_ID}`, {
      phone: null,
    });
  });

  it("turns a failed envelope into an ApiError carrying the code and request id", async () => {
    vi.spyOn(client, "post").mockResolvedValue({
      data: {
        success: false,
        data: null,
        error: { code: "VALIDATION_ERROR", message: "body.email" },
        request_id: "req-9",
      },
    });

    const failure = createCustomer({ company_name: "Acme", email: "nope" });

    await expect(failure).rejects.toBeInstanceOf(ApiError);
    await expect(failure).rejects.toMatchObject({
      code: "VALIDATION_ERROR",
      message: "body.email",
      requestId: "req-9",
    });
  });

  it("turns a transport failure into an ApiError too", async () => {
    vi.spyOn(client, "patch").mockRejectedValue(new Error("socket hang up"));

    await expect(updateCustomer(CUSTOMER_ID, { email: "a@example.com" })).rejects.toBeInstanceOf(
      ApiError,
    );
  });
});
