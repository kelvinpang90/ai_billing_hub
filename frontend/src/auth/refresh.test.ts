/**
 * Single-flight refresh.
 *
 * ⚠️ **这个文件测的是一条安全后果，不是一条性能指标。**
 *
 * 后端的刷新令牌是一次性的：同一张被提交两次即判为重放，整条会话链被吊销
 * （`app/services/auth.py` 的 `_treat_as_replay`）。所以「并发刷新只发一次」
 * 一旦回归，现象是**用户随机掉登录**——请求越多、网越慢越容易中，而且几乎
 * 不可能从现场复现。它必须由测试钉死，不能靠读代码看出来。
 */

import type { AxiosInstance } from "axios";
import { afterEach, describe, expect, it, vi } from "vitest";

import { refreshAccessToken, resetRefreshState } from "./refresh";
import { getAccessToken, resetTokenStore, setAccessToken } from "./tokenStore";

afterEach(() => {
  resetRefreshState();
  resetTokenStore();
});

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason: unknown) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function envelope(token: string | null) {
  return { data: { success: true, data: { access_token: token }, error: null, request_id: null } };
}

/** 只需要 `post`，其余方法这条路径上用不到。 */
function clientWith(post: unknown): AxiosInstance {
  return { post } as unknown as AxiosInstance;
}

describe("refreshAccessToken", () => {
  it("sends exactly one request no matter how many callers pile up", async () => {
    const gate = deferred<ReturnType<typeof envelope>>();
    const post = vi.fn(() => gate.promise);
    const client = clientWith(post);

    // 三个请求同时 401 —— 这正是真实页面上的样子。
    const all = Promise.all([
      refreshAccessToken(client),
      refreshAccessToken(client),
      refreshAccessToken(client),
    ]);
    gate.resolve(envelope("token-new"));

    expect(await all).toEqual(["token-new", "token-new", "token-new"]);
    expect(post).toHaveBeenCalledTimes(1);
    expect(getAccessToken()).toBe("token-new");
  });

  it("posts to the refresh endpoint with skipAuthRefresh set", async () => {
    const post = vi.fn(() => Promise.resolve(envelope("token-new")));
    await refreshAccessToken(clientWith(post));

    expect(post).toHaveBeenCalledWith("/api/v1/auth/refresh", undefined, {
      skipAuthRefresh: true,
    });
  });

  it("starts a fresh request once the previous one has settled", async () => {
    const post = vi
      .fn()
      .mockResolvedValueOnce(envelope("token-1"))
      .mockResolvedValueOnce(envelope("token-2"));
    const client = clientWith(post);

    expect(await refreshAccessToken(client)).toBe("token-1");
    // ⚠️ 少了 `.finally(() => { inFlight = null })` 这一句，第二次会复用上面
    // 那个**已经完成**的 Promise，永远拿回同一张（很快就过期的）令牌。
    expect(await refreshAccessToken(client)).toBe("token-2");
    expect(post).toHaveBeenCalledTimes(2);
  });

  it("clears the token and reports failure when the refresh is rejected", async () => {
    const post = vi.fn((): Promise<unknown> => Promise.reject(new Error("401")));
    const client = clientWith(post);

    expect(await refreshAccessToken(client)).toBeNull();
    expect(getAccessToken()).toBeNull();

    // 失败也必须解锁：否则一次网络抖动之后，这一整个标签页再也刷不了令牌。
    post.mockImplementation(() => Promise.resolve(envelope("token-late")));
    expect(await refreshAccessToken(client)).toBe("token-late");
  });

  it("does not wipe a session that was signed in while the refresh was in flight", async () => {
    // ⚠️ 页面加载时的静默刷新是异步的，而用户可以在它返回之前就登录成功
    // （密码管理器自动提交 + 慢网络时尤其容易）。那次刷新**迟到地失败**，
    // 若无条件清令牌，就会把刚拿到的会话抹掉 —— 现象是「登录成功后立刻被踢
    // 回登录页」，只在慢网络下偶发。
    const gate = deferred<never>();
    const post = vi.fn(() => gate.promise);
    const pending = refreshAccessToken(clientWith(post));

    // 刷新还在飞的时候，用户走完了登录。
    setAccessToken("token-from-login");
    gate.reject(new Error("401"));

    expect(await pending).toBe("token-from-login");
    expect(getAccessToken()).toBe("token-from-login");
  });

  it("does not replace a newer session with a late successful refresh", async () => {
    // 成功那一支同样不能覆盖：迟到的刷新换回来的是**旧 cookie 那条会话**，
    // 盖上去等于把用户悄悄换回上一个会话。
    const gate = deferred<ReturnType<typeof envelope>>();
    const post = vi.fn(() => gate.promise);
    const pending = refreshAccessToken(clientWith(post));

    setAccessToken("token-from-login");
    gate.resolve(envelope("token-stale"));

    expect(await pending).toBe("token-from-login");
    expect(getAccessToken()).toBe("token-from-login");
  });

  it("treats a successful response without a token as a failed refresh", async () => {
    const post = vi.fn(() =>
      Promise.resolve({
        data: { success: true, data: null, error: null, request_id: null },
      }),
    );

    expect(await refreshAccessToken(clientWith(post))).toBeNull();
    expect(getAccessToken()).toBeNull();
  });
});
