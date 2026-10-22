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

import { AxiosError, type AxiosResponse } from "axios";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  INVITATION_ENDPOINT_NOT_FOUND,
  acceptInvitation,
  login,
  requestPasswordReset,
  resetPassword,
} from "./auth";
import { ApiError, client } from "./client";

afterEach(() => {
  vi.restoreAllMocks();
});

/** 让 `client.post` 回一个 §107 的成功信封，不真的发请求。 */
function stubPost() {
  return vi.spyOn(client, "post").mockResolvedValue({
    data: { success: true, data: {}, error: null, request_id: "req-1" },
  });
}

/** 让 `client.post` 像后端对一个没人服务的路径那样回 404（框架异常也走 §107 信封，码是 HTTP_ERROR）。 */
function stubFrameworkNotFound() {
  const response = {
    status: 404,
    statusText: "Not Found",
    headers: {},
    config: {},
    data: {
      success: false,
      data: null,
      error: { code: "HTTP_ERROR", message: "Not Found" },
      request_id: "req-404",
    },
  } as unknown as AxiosResponse;
  return vi
    .spyOn(client, "post")
    .mockRejectedValue(new AxiosError("Not Found", "ERR_BAD_REQUEST", undefined, undefined, response));
}

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

  it("accepts an invitation with the token and a field called password", async () => {
    const post = stubPost();

    await acceptInvitation("invite-token-1", "a much longer passphrase");

    // ⚠️ 这里是 `password`，**不是**重置那边的 `new_password`（设计 AIH-TASK-089 §2）。
    // 两个表单长得一样，照抄 resetPassword 最容易抄错的就是这一个字段名。
    expect(post).toHaveBeenCalledWith("/api/v1/auth/invitation/accept", {
      token: "invite-token-1",
      password: "a much longer passphrase",
    });
  });

  it("reports a missing invitation endpoint under its own code", async () => {
    // 页面先于接口上线（设计 §8）：那段时间里这个端点是框架的 404，信封里的码是笼统的
    // HTTP_ERROR，与 405 分不开，所以按 HTTP 状态认。
    stubFrameworkNotFound();

    const caught = await acceptInvitation("invite-token-1", "a much longer passphrase").catch(
      (error: unknown) => error,
    );

    expect(caught).toBeInstanceOf(ApiError);
    expect((caught as ApiError).code).toBe(INVITATION_ENDPOINT_NOT_FOUND);
    expect((caught as ApiError).requestId).toBe("req-404");
  });

  it("leaves a 404 from the other auth endpoints as the backend reported it", async () => {
    // 回归：404 的改码只给接受邀请，共用的 `post` 不该悄悄改掉别的端点的错误。
    stubFrameworkNotFound();

    const caught = await resetPassword("reset-token-1", "a much longer passphrase").catch(
      (error: unknown) => error,
    );

    expect((caught as ApiError).code).toBe("HTTP_ERROR");
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
