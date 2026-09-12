/**
 * 拦截器：带令牌、401 之后刷新一次再重试。
 *
 * 这里不打桩 axios，而是换掉 `client` 的 adapter —— 拦截器、`skipAuthRefresh`
 * 标记、重试出去的那个 config 全都是真的。打桩 `client.post` 的话，被测的正好
 * 是这一整条链里唯一不会出错的那一环。
 */

import { AxiosError, type AxiosAdapter, type AxiosRequestConfig, type AxiosResponse } from "axios";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { resetRefreshState } from "../auth/refresh";
import { getAccessToken, resetTokenStore, setAccessToken } from "../auth/tokenStore";
import { REQUEST_ID_HEADER, client } from "./client";

const realAdapter = client.defaults.adapter;

interface Recorded {
  url: string;
  authorization: string | undefined;
  requestId: string | undefined;
}

/** 每个被 adapter 看到的请求，按顺序记下来。 */
let seen: Recorded[] = [];

function respond(config: AxiosRequestConfig, status: number, body: unknown): AxiosResponse {
  return {
    data: body,
    status,
    statusText: String(status),
    headers: {},
    config: config as AxiosResponse["config"],
  };
}

function ok(data: unknown) {
  return { success: true, data, error: null, request_id: "req-1" };
}

/**
 * 装一个按 URL 作答的 adapter。
 *
 * `routes` 的值是一个函数，因为好几条用例要「第一次 401、第二次 200」。
 */
function install(routes: Record<string, (n: number) => { status: number; body: unknown }>): void {
  const counts = new Map<string, number>();
  const adapter: AxiosAdapter = (config) => {
    const url = config.url ?? "";
    const headers = config.headers;
    seen.push({
      url,
      authorization: headers.get("Authorization") as string | undefined,
      requestId: headers.get(REQUEST_ID_HEADER) as string | undefined,
    });

    const route = routes[url];
    if (!route) {
      throw new Error(`test adapter has no route for ${url}`);
    }
    const n = (counts.get(url) ?? 0) + 1;
    counts.set(url, n);
    const { status, body } = route(n);
    const response = respond(config, status, body);
    if (status >= 400) {
      // axios 在 adapter 之外用 validateStatus 判定成败，所以这里要自己抛。
      const error = Object.assign(new Error(`Request failed with status code ${status}`), {
        isAxiosError: true,
        config,
        response,
        toJSON: () => ({}),
      });
      // ⚠️ `instanceof AxiosError` 是响应拦截器的第一道判断。伪造的错误不挂上
      // 真原型的话，整条 401 逻辑根本不会被执行，而测试会"通过"得毫无意义。
      Object.setPrototypeOf(error, AxiosError.prototype);
      return Promise.reject(error);
    }
    return Promise.resolve(response);
  };
  client.defaults.adapter = adapter;
}

beforeEach(() => {
  seen = [];
  resetTokenStore();
  resetRefreshState();
});

afterEach(() => {
  // `exactOptionalPropertyTypes` 下「没有这个属性」和「属性是 undefined」是
  // 两回事，而 axios 默认就没设过 adapter —— 所以只能删，不能赋 undefined。
  if (realAdapter === undefined) {
    delete client.defaults.adapter;
  } else {
    client.defaults.adapter = realAdapter;
  }
});

describe("request interceptor", () => {
  it("attaches the access token and a correlation id", async () => {
    setAccessToken("token-1");
    install({ "/api/v1/wallets": () => ({ status: 200, body: ok({ items: [] }) }) });

    await client.get("/api/v1/wallets");

    expect(seen[0]?.authorization).toBe("Bearer token-1");
    // §94 的关联 ID 链条：浏览器发的这个 id 要能在服务端日志里搜到。
    expect(seen[0]?.requestId).toMatch(/^[0-9a-f-]{36}$/);
  });

  it("sends no Authorization header when nobody is signed in", async () => {
    install({ "/api/v1/wallets": () => ({ status: 200, body: ok(null) }) });

    await client.get("/api/v1/wallets");

    expect(seen[0]?.authorization).toBeUndefined();
  });

  it("sends no Authorization header on the refresh request itself", async () => {
    setAccessToken("token-expired");
    install({ "/api/v1/auth/refresh": () => ({ status: 200, body: ok(null) }) });

    await client.post("/api/v1/auth/refresh", undefined, { skipAuthRefresh: true });

    // 刷新靠 httpOnly cookie 认人；手上那张访问令牌此刻已经过期，带上只是噪音。
    expect(seen[0]?.authorization).toBeUndefined();
  });
});

describe("response interceptor", () => {
  it("refreshes once on 401 and replays the request with the new token", async () => {
    setAccessToken("token-old");
    install({
      "/api/v1/wallets": (n) =>
        n === 1
          ? { status: 401, body: ok(null) }
          : { status: 200, body: ok({ balance: "1.00000000" }) },
      "/api/v1/auth/refresh": () => ({
        status: 200,
        body: ok({ access_token: "token-new" }),
      }),
    });

    const response = await client.get("/api/v1/wallets");

    expect(response.status).toBe(200);
    expect(seen.map((r) => r.url)).toEqual([
      "/api/v1/wallets",
      "/api/v1/auth/refresh",
      "/api/v1/wallets",
    ]);
    // 重试用的必须是**新**令牌。拿旧的重试会再吃一个 401，而那一次因为
    // `authRetried` 已经置位，会直接把 401 抛给界面 —— 表现是「偶尔要重登」。
    expect(seen[2]?.authorization).toBe("Bearer token-new");
  });

  it("gives up after one retry instead of looping", async () => {
    setAccessToken("token-old");
    install({
      // 端点**始终** 401：问题不在令牌过期，再刷多少次都一样。
      "/api/v1/wallets": () => ({ status: 401, body: ok(null) }),
      "/api/v1/auth/refresh": () => ({
        status: 200,
        body: ok({ access_token: "token-new" }),
      }),
    });

    await expect(client.get("/api/v1/wallets")).rejects.toThrow();

    expect(seen.filter((r) => r.url === "/api/v1/auth/refresh")).toHaveLength(1);
    expect(seen.filter((r) => r.url === "/api/v1/wallets")).toHaveLength(2);
  });

  it("does not refresh when an auth endpoint answers 401", async () => {
    install({
      "/api/v1/auth/login": () => ({
        status: 401,
        body: {
          success: false,
          data: null,
          error: { code: "INVALID_CREDENTIALS", message: "Invalid email or password." },
          request_id: "req-1",
        },
      }),
    });

    await expect(client.post("/api/v1/auth/login", { email: "a@b.c", password: "x" })).rejects.toThrow();

    // 密码错是**业务结果**，不是令牌过期。在这里刷新，等于每输错一次密码就
    // 白发一个刷新请求，还会把还活着的会话搅进来。
    expect(seen.map((r) => r.url)).toEqual(["/api/v1/auth/login"]);
  });

  it("propagates the original 401 when the refresh fails", async () => {
    setAccessToken("token-old");
    install({
      "/api/v1/wallets": () => ({ status: 401, body: ok(null) }),
      "/api/v1/auth/refresh": () => ({ status: 401, body: ok(null) }),
    });

    await expect(client.get("/api/v1/wallets")).rejects.toThrow();

    expect(seen.map((r) => r.url)).toEqual(["/api/v1/wallets", "/api/v1/auth/refresh"]);
    // 刷新失败 = 这条会话结束了。令牌被清掉，AuthProvider 的订阅会把界面
    // 切回未登录。
    expect(getAccessToken()).toBeNull();
  });

  it("refreshes only once when several requests expire together", async () => {
    setAccessToken("token-old");
    install({
      "/api/v1/wallets": (n) => (n <= 3 ? { status: 401, body: ok(null) } : { status: 200, body: ok(null) }),
      "/api/v1/auth/refresh": () => ({
        status: 200,
        body: ok({ access_token: "token-new" }),
      }),
    });

    // ⚠️ 真实页面就是这样：三个并发请求同时过期。刷新令牌是一次性的，
    // 三次并发刷新会让后端判定重放并吊销整条会话链。
    await Promise.all([
      client.get("/api/v1/wallets"),
      client.get("/api/v1/wallets"),
      client.get("/api/v1/wallets"),
    ]);

    expect(seen.filter((r) => r.url === "/api/v1/auth/refresh")).toHaveLength(1);
  });
});
