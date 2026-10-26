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
  AccountStatusError,
  createCustomer,
  createProject,
  getCustomer,
  getLowBalanceThreshold,
  listCustomers,
  listProjects,
  postAccountStatus,
  setLowBalanceThreshold,
  updateCustomer,
} from "./adminCustomers";
import { ApiError, client } from "./client";

const CUSTOMER_ID = "00000000-0000-4000-8000-000000000000";
/** 低余额阈值用例的示例客户 id：全零占位。 */
const ZERO_CUSTOMER_ID = "00000000-0000-0000-0000-000000000000";

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

  it("lists one customer's projects with the page and page_size the backend reads", async () => {
    const get = vi
      .spyOn(client, "get")
      .mockResolvedValue(envelope({ items: [], page: 3, page_size: 10, total: 0 }));

    const page = await listProjects(CUSTOMER_ID, 3, 10);

    expect(get).toHaveBeenCalledWith(
      `/api/v1/admin/customers/${CUSTOMER_ID}/projects?page=3&page_size=10`,
      {},
    );
    expect(page).toEqual({ items: [], page: 3, page_size: 10, total: 0 });
  });

  it("escapes the customer id in the project paths too", async () => {
    const get = vi
      .spyOn(client, "get")
      .mockResolvedValue(envelope({ items: [], page: 1, page_size: 20, total: 0 }));
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope({ id: CUSTOMER_ID }));

    await listProjects("a/../b", 1, 20);
    await createProject("a/../b", { name: "Chatbot" });

    expect(get).toHaveBeenCalledWith(
      "/api/v1/admin/customers/a%2F..%2Fb/projects?page=1&page_size=20",
      {},
    );
    expect(post).toHaveBeenCalledWith("/api/v1/admin/customers/a%2F..%2Fb/projects", {
      name: "Chatbot",
    });
  });

  it("creates a project under the customer in the path with the body as given", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope({ id: CUSTOMER_ID }));

    await createProject(CUSTOMER_ID, { name: "Chatbot", description: "Support bot" });

    // 归属只来自路径：请求体里没有 customer_id。
    expect(post).toHaveBeenCalledWith(`/api/v1/admin/customers/${CUSTOMER_ID}/projects`, {
      name: "Chatbot",
      description: "Support bot",
    });
  });

  it("turns a 404 on project creation into an ApiError with CUSTOMER_NOT_FOUND", async () => {
    const response = {
      data: {
        success: false,
        data: null,
        error: { code: "CUSTOMER_NOT_FOUND", message: "The customer does not exist." },
        request_id: "req-404",
      },
      status: 404,
      statusText: "Not Found",
      headers: {},
      config: { headers: new AxiosHeaders() },
    } satisfies AxiosResponse;
    vi.spyOn(client, "post").mockRejectedValue(
      new AxiosError("Request failed", "ERR_BAD_REQUEST", undefined, undefined, response),
    );

    const failure = createProject(CUSTOMER_ID, { name: "Chatbot" });

    await expect(failure).rejects.toBeInstanceOf(ApiError);
    await expect(failure).rejects.toMatchObject({
      code: "CUSTOMER_NOT_FOUND",
      message: "The customer does not exist.",
      requestId: "req-404",
    });
  });

  it("posts the account status change to the customer's account-status path with exactly the body", async () => {
    const post = vi
      .spyOn(client, "post")
      .mockResolvedValue({ ...envelope({ id: CUSTOMER_ID }), status: 200 });

    await postAccountStatus(CUSTOMER_ID, { account_status: "DISABLED", reason: "Contract paused" });

    // 请求体只有这两个字段：客户只来自路径，没有 status_version 之类。
    expect(post).toHaveBeenCalledTimes(1);
    expect(post).toHaveBeenCalledWith(`/api/v1/admin/customers/${CUSTOMER_ID}/account-status`, {
      account_status: "DISABLED",
      reason: "Contract paused",
    });
  });

  it("escapes the customer id in the account-status path too", async () => {
    const post = vi
      .spyOn(client, "post")
      .mockResolvedValue({ ...envelope({ id: CUSTOMER_ID }), status: 200 });

    await postAccountStatus("a/../b", { account_status: "ENABLED", reason: "Paid up" });

    expect(post).toHaveBeenCalledWith("/api/v1/admin/customers/a%2F..%2Fb/account-status", {
      account_status: "ENABLED",
      reason: "Paid up",
    });
  });

  it("keeps the HTTP status of a rejected account status change and does not retry", async () => {
    const response = {
      data: {
        success: false,
        data: null,
        error: { code: "ACCOUNT_STATUS_TRANSITION_INVALID", message: "The account is closed." },
        request_id: "req-409",
      },
      status: 409,
      statusText: "Conflict",
      headers: {},
      config: { headers: new AxiosHeaders() },
    } satisfies AxiosResponse;
    const post = vi
      .spyOn(client, "post")
      .mockRejectedValue(
        new AxiosError("Request failed", "ERR_BAD_REQUEST", undefined, undefined, response),
      );

    const failure = postAccountStatus(CUSTOMER_ID, { account_status: "ENABLED", reason: "Paid up" });

    await expect(failure).rejects.toBeInstanceOf(AccountStatusError);
    await expect(failure).rejects.toMatchObject({
      code: "ACCOUNT_STATUS_TRANSITION_INVALID",
      requestId: "req-409",
      status: 409,
    });
    expect(post).toHaveBeenCalledTimes(1);
  });

  it("reports a network failure of the account status change with no HTTP status", async () => {
    const post = vi
      .spyOn(client, "post")
      .mockRejectedValue(new AxiosError("Network Error", "ERR_NETWORK"));

    const failure = postAccountStatus(CUSTOMER_ID, { account_status: "DISABLED", reason: "Paused" });

    await expect(failure).rejects.toMatchObject({ code: "NETWORK_ERROR", status: null });
    expect(post).toHaveBeenCalledTimes(1);
  });

  it("reads the low balance threshold with GET on the customer's low-balance-threshold path", async () => {
    const get = vi
      .spyOn(client, "get")
      .mockResolvedValue(envelope({ low_balance_threshold: "50.00000000" }));

    const threshold = await getLowBalanceThreshold(ZERO_CUSTOMER_ID);

    expect(get).toHaveBeenCalledTimes(1);
    expect(get).toHaveBeenCalledWith(
      `/api/v1/admin/customers/${ZERO_CUSTOMER_ID}/low-balance-threshold`,
      {},
    );
    // 原样交回字符串，不解析。
    expect(threshold).toEqual({ low_balance_threshold: "50.00000000" });
  });

  it("sets the low balance threshold with PUT and a body of exactly that one key", async () => {
    const put = vi
      .spyOn(client, "put")
      .mockResolvedValue(envelope({ low_balance_threshold: "50.00000000" }));

    await setLowBalanceThreshold(ZERO_CUSTOMER_ID, "50.00");

    expect(put).toHaveBeenCalledTimes(1);
    expect(put).toHaveBeenCalledWith(
      `/api/v1/admin/customers/${ZERO_CUSTOMER_ID}/low-balance-threshold`,
      { low_balance_threshold: "50.00" },
    );
    // 恰好一个键，值是字符串（不是 number）：后端对 JSON 数字与多余字段都回 422。
    const body = put.mock.calls[0]?.[1] as Record<string, unknown>;
    expect(Object.keys(body)).toEqual(["low_balance_threshold"]);
    expect(typeof body.low_balance_threshold).toBe("string");
  });

  it("clears the low balance threshold by sending null explicitly", async () => {
    const put = vi.spyOn(client, "put").mockResolvedValue(envelope({ low_balance_threshold: null }));

    await setLowBalanceThreshold(ZERO_CUSTOMER_ID, null);

    // ⚠️ `null` 是「清除」，必须原样发出去；键缺失后端回 422。
    const body = put.mock.calls[0]?.[1] as Record<string, unknown>;
    expect(Object.keys(body)).toEqual(["low_balance_threshold"]);
    expect(body.low_balance_threshold).toBeNull();
    expect(put).toHaveBeenCalledWith(
      `/api/v1/admin/customers/${ZERO_CUSTOMER_ID}/low-balance-threshold`,
      { low_balance_threshold: null },
    );
  });

  it("escapes the customer id in the low-balance-threshold path too", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope({ low_balance_threshold: null }));
    const put = vi.spyOn(client, "put").mockResolvedValue(envelope({ low_balance_threshold: null }));

    await getLowBalanceThreshold("a/../b");
    await setLowBalanceThreshold("a/../b", null);

    expect(get).toHaveBeenCalledWith("/api/v1/admin/customers/a%2F..%2Fb/low-balance-threshold", {});
    expect(put).toHaveBeenCalledWith("/api/v1/admin/customers/a%2F..%2Fb/low-balance-threshold", {
      low_balance_threshold: null,
    });
  });

  it("turns a rejected threshold into an ApiError carrying the request id", async () => {
    const response = {
      data: {
        success: false,
        data: null,
        error: { code: "VALIDATION_ERROR", message: "Invalid field: body.low_balance_threshold" },
        request_id: "req-422",
      },
      status: 422,
      statusText: "Unprocessable Entity",
      headers: {},
      config: { headers: new AxiosHeaders() },
    } satisfies AxiosResponse;
    vi.spyOn(client, "put").mockRejectedValue(
      new AxiosError("Request failed", "ERR_BAD_REQUEST", undefined, undefined, response),
    );

    const failure = setLowBalanceThreshold(ZERO_CUSTOMER_ID, "50.00");

    await expect(failure).rejects.toBeInstanceOf(ApiError);
    await expect(failure).rejects.toMatchObject({ code: "VALIDATION_ERROR", requestId: "req-422" });
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
