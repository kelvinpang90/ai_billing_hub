/**
 * 认证请求发到哪个地址、请求体长什么样。
 *
 * ⚠️ 这一层有一个**所有页面用例都盖不到的盲区**：页面的用例把整个 `api/auth`
 * 模块 mock 掉了（它们测的是状态机，不是网络），所以端点路径与字段名写错时
 * 它们照样全绿。`new_password` 打成 `newPassword`、`/password/reset` 打成
 * `/reset-password`，在浏览器里都是一句 422 或 404，而在测试里什么也不会发生。
 *
 * 字段名与后端 Pydantic 模型的一致性另有一条跨端用例
 * （`tests/backend/test_password_reset_link.py`）机械比对；这里钉的是「发出去的
 * 确实是这个形状」。
 */

import { AxiosError, AxiosHeaders, type AxiosAdapter } from "axios";
import { afterEach, describe, expect, it, vi } from "vitest";

import { resetRefreshState } from "../auth/refresh";
import { resetTokenStore, setAccessToken } from "../auth/tokenStore";
import {
  INVALID_INVITATION_TOKEN,
  INVITATION_ENDPOINT_NOT_FOUND,
  acceptInvitation,
  getMe,
  login,
  requestPasswordReset,
  resetPassword,
} from "./auth";
import { client } from "./client";

const realAdapter = client.defaults.adapter;

/** 让 `client.post` 回一个 §107 的成功信封，不真的发请求。 */
function stubPost() {
  return vi.spyOn(client, "post").mockResolvedValue({
    data: { success: true, data: {}, error: null, request_id: "req-1" },
  });
}

afterEach(() => {
  // 下面有的用例让 `client.post` 抛错；不还原的话会漏到后面的用例里。
  vi.restoreAllMocks();
  // `getMe` 的刷新用例换掉了 adapter。axios 默认没设 adapter，所以只能删，不能赋 undefined
  // （`exactOptionalPropertyTypes`，同 interceptors.test.ts）。
  if (realAdapter === undefined) {
    delete client.defaults.adapter;
  } else {
    client.defaults.adapter = realAdapter;
  }
  resetTokenStore();
  resetRefreshState();
});

describe("auth requests", () => {
  it("asks for a reset link at the endpoint the backend serves", async () => {
    const post = stubPost();

    await requestPasswordReset("admin@example.com");

    expect(post).toHaveBeenCalledWith("/api/v1/auth/password/forgot", {
      email: "admin@example.com",
    });
  });

  it("sends the token and the new password under the names the backend reads", async () => {
    const post = stubPost();

    await resetPassword("reset-token-1", "a much longer passphrase");

    // ⚠️ `new_password` 是**蛇形**的：后端的 Pydantic 模型就叫这个。写成驼峰
    // 的话 FastAPI 会以「缺字段」回 422，而这里一条用例都不会红。
    expect(post).toHaveBeenCalledWith("/api/v1/auth/password/reset", {
      token: "reset-token-1",
      new_password: "a much longer passphrase",
    });
  });

  it("accepts an invitation with the token and the password under the names the backend reads", async () => {
    const post = stubPost();

    await acceptInvitation("invitation-token-1", "a much longer passphrase");

    // ⚠️ 设计 AIH-TASK-089 §2 的契约是 `{token, password}` —— 与重置不同，**不是** `new_password`。
    expect(post).toHaveBeenCalledWith("/api/v1/auth/invitation/accept", {
      token: "invitation-token-1",
      password: "a much longer passphrase",
    });
  });

  it("turns a 404 from the not-yet-deployed endpoint into its own code", async () => {
    // 这一页先于接口上线（设计 §8）。框架的 404 信封是通用的 `HTTP_ERROR`，页面认不出来。
    vi.spyOn(client, "post").mockRejectedValue(
      new AxiosError("Not Found", "ERR_BAD_REQUEST", undefined, undefined, {
        status: 404,
        statusText: "Not Found",
        headers: {},
        config: { headers: new AxiosHeaders() },
        data: {
          success: false,
          data: null,
          error: { code: "HTTP_ERROR", message: "Not Found" },
          request_id: "req-404",
        },
      }),
    );

    await expect(acceptInvitation("invitation-token-1", "a much longer passphrase")).rejects.toMatchObject({
      code: INVITATION_ENDPOINT_NOT_FOUND,
      requestId: "req-404",
    });
  });

  it("passes the backend's invitation errors through unchanged", async () => {
    vi.spyOn(client, "post").mockRejectedValue(
      new AxiosError("Bad Request", "ERR_BAD_REQUEST", undefined, undefined, {
        status: 400,
        statusText: "Bad Request",
        headers: {},
        config: { headers: new AxiosHeaders() },
        data: {
          success: false,
          data: null,
          error: { code: INVALID_INVITATION_TOKEN, message: "The invitation is no longer valid." },
          request_id: "req-400",
        },
      }),
    );

    await expect(acceptInvitation("invitation-token-1", "a much longer passphrase")).rejects.toMatchObject({
      code: INVALID_INVITATION_TOKEN,
      requestId: "req-400",
    });
  });

  it("asks who is signed in at /api/v1/me and returns the role", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue({
      data: {
        success: true,
        data: { role: "CUSTOMER", email: "ops@example.com", two_factor_enabled: false },
        error: null,
        request_id: "req-me",
      },
    });

    await expect(getMe()).resolves.toEqual({
      role: "CUSTOMER",
      email: "ops@example.com",
      two_factor_enabled: false,
    });

    // ⚠️ **不在** `/api/v1/auth/` 下（设计 AIH-TASK-089 §2）：那个前缀下的 401 不触发刷新。
    expect(get).toHaveBeenCalledWith("/api/v1/me", {});
  });

  it("refreshes an expired access token and asks /api/v1/me again", async () => {
    // 换 adapter 而不是打桩 `client.get`：要测的正是拦截器对这个路径会不会刷新。
    setAccessToken("token-expired");
    const seen: string[] = [];
    const adapter: AxiosAdapter = (config) => {
      const url = config.url ?? "";
      seen.push(`${url} ${String(config.headers.get("Authorization"))}`);
      const expired = url === "/api/v1/me" && config.headers.get("Authorization") === "Bearer token-expired";
      const data =
        url === "/api/v1/auth/refresh"
          ? { success: true, data: { access_token: "token-new" }, error: null, request_id: "req-r" }
          : {
              success: !expired,
              data: expired ? null : { role: "ADMIN", email: "admin@example.com", two_factor_enabled: true },
              error: expired ? { code: "TOKEN_INVALID", message: "The access token is invalid." } : null,
              request_id: "req-me",
            };
      const response = { data, status: expired ? 401 : 200, statusText: "", headers: {}, config };
      if (expired) {
        return Promise.reject(new AxiosError("Unauthorized", "ERR_BAD_REQUEST", config, undefined, response));
      }
      return Promise.resolve(response);
    };
    client.defaults.adapter = adapter;

    await expect(getMe()).resolves.toMatchObject({ role: "ADMIN" });

    expect(seen).toEqual([
      "/api/v1/me Bearer token-expired",
      "/api/v1/auth/refresh undefined",
      "/api/v1/me Bearer token-new",
    ]);
  });

  it("still sends login the way it always did", async () => {
    // 这条不是新功能的用例，是**回归**：上面两个函数和它共用同一个 `post` 辅助
    // 函数，改那个辅助函数时不该悄悄改掉登录的线上形状。
    const post = stubPost();

    await login("admin@example.com", "correct horse battery");

    expect(post).toHaveBeenCalledWith("/api/v1/auth/login", {
      email: "admin@example.com",
      password: "correct horse battery",
    });
  });
});
