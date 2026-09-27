/**
 * 客户管理请求发到哪个地址、带什么查询参数与请求体。
 *
 * 与 `auth.test.ts` 同一个理由：页面用例把整个 `api/adminCustomers` mock 掉了，
 * `page_size` 写成 `pageSize`、PATCH 写成 PUT，在页面用例里照样全绿，在浏览器里
 * 却是一句 422 或 405。
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import {
  CUSTOMER_NOT_FOUND,
  createCustomer,
  getCustomer,
  listCustomers,
  shouldRetryCustomerQuery,
  updateCustomer,
} from "./adminCustomers";
import { ApiError, client } from "./client";

// secret-scan 会把随手编的高熵 uuid 当成密钥，测试里一律用全零占位值。
const CUSTOMER_ID = "00000000-0000-4000-8000-000000000000";

const DETAIL = {
  id: CUSTOMER_ID,
  company_name: "Acme Sdn Bhd",
  contact_name: null,
  email: "ops@example.com",
  phone: null,
  billing_status: "SUSPENDED",
  status_version: 0,
  created_at: "2026-09-20T08:30:00",
  updated_at: "2026-09-20T08:30:00",
  wallet: { currency: "MYR", balance: "0.00000000", version: 0 },
};

function ok(data: unknown) {
  return { data: { success: true, data, error: null, request_id: "req-1" } };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("admin customer requests", () => {
  it("lists a page with the query parameter names the backend reads", async () => {
    const get = vi
      .spyOn(client, "get")
      .mockResolvedValue(ok({ items: [], page: 2, page_size: 50, total: 0 }));

    const page = await listCustomers(2, 50);

    // ⚠️ `page_size` 是蛇形的。写成 `pageSize` 后端不认，按默认 20 条回，不报错。
    expect(get).toHaveBeenCalledWith("/api/v1/admin/customers?page=2&page_size=50", {});
    expect(page.total).toBe(0);
  });

  it("reads one customer by its public id and keeps the balance a string", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(ok(DETAIL));

    const customer = await getCustomer(CUSTOMER_ID);

    expect(get).toHaveBeenCalledWith(`/api/v1/admin/customers/${CUSTOMER_ID}`, {});
    // INV-10：金额原样是字符串，没有被任何一层转成数字。
    expect(customer.wallet.balance).toBe("0.00000000");
  });

  it("creates a customer with exactly the body it was given", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(ok(DETAIL));

    await createCustomer({ company_name: "Acme Sdn Bhd", email: "ops@example.com" });

    expect(post).toHaveBeenCalledWith("/api/v1/admin/customers", {
      company_name: "Acme Sdn Bhd",
      email: "ops@example.com",
    });
  });

  it("edits with PATCH and sends only the patch", async () => {
    const patch = vi.spyOn(client, "patch").mockResolvedValue(ok(DETAIL));

    await updateCustomer(CUSTOMER_ID, { contact_name: null });

    expect(patch).toHaveBeenCalledWith(`/api/v1/admin/customers/${CUSTOMER_ID}`, {
      contact_name: null,
    });
  });

  it("turns a failure envelope into an ApiError with the backend code and request id", async () => {
    vi.spyOn(client, "get").mockResolvedValue({
      data: {
        success: false,
        data: null,
        error: { code: CUSTOMER_NOT_FOUND, message: "Customer not found." },
        request_id: "req-404",
      },
    });

    const failure = await getCustomer(CUSTOMER_ID).catch((caught: unknown) => caught);

    expect(failure).toBeInstanceOf(ApiError);
    expect(failure).toMatchObject({ code: CUSTOMER_NOT_FOUND, requestId: "req-404" });
  });

  it("does not retry answers that will not change", () => {
    expect(shouldRetryCustomerQuery(0, new ApiError(CUSTOMER_NOT_FOUND, "x", null))).toBe(false);
    expect(shouldRetryCustomerQuery(0, new ApiError("ADMIN_REQUIRED", "x", null))).toBe(false);
    expect(shouldRetryCustomerQuery(0, new ApiError("INTERNAL_ERROR", "x", null))).toBe(true);
    expect(shouldRetryCustomerQuery(1, new ApiError("INTERNAL_ERROR", "x", null))).toBe(false);
  });
});
