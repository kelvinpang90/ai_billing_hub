/**
 * 与计费后端通话的唯一入口。
 *
 * spec §107 规定所有响应都是同一个信封 `{success, data, error, request_id}`，
 * 成功与失败共用一种形状。**解信封的逻辑只能存在这一处** —— 每个 feature 各写
 * 一遍的话，早晚有一处忘了看 `success`，把 `data: null` 当成正常值渲染出去。
 */

import axios, { AxiosError, type AxiosInstance } from "axios";

import { refreshAccessToken } from "../auth/refresh";
import { getAccessToken } from "../auth/tokenStore";

/**
 * 拦截器挂在请求配置上的标记。
 *
 * ⚠️ `skipAuthRefresh` 给刷新请求自己用；`authRetried` 保证一个请求至多被
 * 重试一次 —— 少了它，一个始终 401 的端点会把刷新与重试打成死循环。
 */
declare module "axios" {
  interface AxiosRequestConfig {
    skipAuthRefresh?: boolean;
    authRetried?: boolean;
  }
}

/** 与后端 `app/core/middleware.py` 的 `REQUEST_ID_HEADER` 必须一致。 */
export const REQUEST_ID_HEADER = "X-Request-ID";

/** spec §107 的响应信封。 */
export interface ApiEnvelope<T> {
  success: boolean;
  data: T | null;
  error: { code: string; message: string } | null;
  request_id: string | null;
}

/**
 * 后端返回的错误，或者「根本没通上」。
 *
 * `requestId` 是用户能报给支持、支持能在日志里搜到的**唯一**钥匙 —— 界面上
 * 展示错误时必须把它带上（§94 的关联 ID 链条到这里才算闭合）。
 */
export class ApiError extends Error {
  constructor(
    readonly code: string,
    message: string,
    readonly requestId: string | null,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** 连不上、超时、被取消 —— 这些没有信封可解。 */
export const NETWORK_ERROR = "NETWORK_ERROR";
/** 通上了，但回来的不是 §107 的形状。 */
export const MALFORMED_RESPONSE = "MALFORMED_RESPONSE";

function isEnvelope(body: unknown): body is ApiEnvelope<unknown> {
  return (
    typeof body === "object" &&
    body !== null &&
    // `in` 之后 TS 已经把 body 收窄成带 success 的对象，不需要再断言一次。
    "success" in body &&
    typeof body.success === "boolean"
  );
}

/**
 * 把信封拆成调用方要的值，失败一律抛 {@link ApiError}。
 *
 * ⚠️ 不认识的形状**当成错误**，不当成空数据。后端在代理层出问题时可能回一段
 * HTML 错误页；把它静默当成 `undefined` 会让界面显示一片空白而不报错。
 */
export function unwrapEnvelope<T>(body: unknown): T {
  if (!isEnvelope(body)) {
    throw new ApiError(MALFORMED_RESPONSE, "The server returned an unexpected response.", null);
  }
  if (!body.success) {
    throw new ApiError(
      body.error?.code ?? MALFORMED_RESPONSE,
      body.error?.message ?? "The request failed.",
      body.request_id,
    );
  }
  return body.data as T;
}

/** 把 axios 抛出来的东西统一成 {@link ApiError}。 */
export function toApiError(error: unknown): ApiError {
  if (error instanceof ApiError) {
    return error;
  }
  if (error instanceof AxiosError) {
    // 有响应体就优先用后端自己的说法：它已经是 §107 的安全文案（不含堆栈）。
    if (error.response !== undefined) {
      try {
        return unwrapEnvelope(error.response.data);
      } catch (unwrapped) {
        if (unwrapped instanceof ApiError) {
          return unwrapped;
        }
      }
    }
    return new ApiError(NETWORK_ERROR, "Could not reach the billing platform.", null);
  }
  return new ApiError(NETWORK_ERROR, "Could not reach the billing platform.", null);
}

/**
 * ⚠️ `baseURL` 默认是 `/`，**不是** `/api`。
 *
 * nginx 转发时刻意不改写路径（见 `deploy/nginx/billing.conf`），所以浏览器请求的
 * uri、nginx 日志里的 uri、应用日志里的 path **是同一个字符串**。在这里偷偷加一个
 * `/api` 前缀，三处就对不上了，排障时要在脑子里做一次映射。
 */
export const client: AxiosInstance = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL ?? "/",
  timeout: 15_000,
});

client.interceptors.request.use((config) => {
  // 由浏览器端生成关联 ID，这样用户截图里的 id 就能直接在服务端日志里搜到。
  // 没带的话 nginx 会生成一个，但那个 id 前端自己不知道。
  // 格式必须落在后端 `_SAFE_REQUEST_ID` 的字符集内，否则会被丢弃后重新生成。
  config.headers.set(REQUEST_ID_HEADER, crypto.randomUUID());

  // 访问令牌只在内存里（见 auth/tokenStore.ts）。这里每次现取，不缓存 ——
  // 刷新之后拿到的必须是新的那张。
  //
  // ⚠️ 刷新请求**不带**这个头：它靠 httpOnly cookie 认人，而此刻手上那张
  // 访问令牌已经过期了，带上只会让服务端日志更难读。
  const token = getAccessToken();
  if (token && !config.skipAuthRefresh) {
    config.headers.set("Authorization", `Bearer ${token}`);
  }
  return config;
});

client.interceptors.response.use(
  (response) => response,
  async (error: unknown) => {
    if (!(error instanceof AxiosError) || error.response?.status !== 401) {
      throw error;
    }
    const config = error.config;
    // ⚠️ 三种情况都**不能**重试，否则会打转：
    //   1. 刷新请求自己 401 —— 再刷一次还是 401
    //   2. 已经重试过一次 —— 说明新令牌也不被接受，问题不在过期
    //   3. 认证端点自己 401（密码错、验证码错）—— 那是业务结果，不是令牌过期
    if (!config || config.skipAuthRefresh || config.authRetried) {
      throw error;
    }
    if (config.url?.startsWith("/api/v1/auth/")) {
      throw error;
    }

    // 静态 import 不会成环：`refresh.ts` 把 client 当参数收，自己不 import 它。
    const token = await refreshAccessToken(client);
    if (!token) {
      throw error;
    }

    config.authRetried = true;
    config.headers.set("Authorization", `Bearer ${token}`);
    return client.request(config);
  },
);

/**
 * 发一个 GET 并拆信封。
 *
 * ⚠️ 这里**不打日志**。响应体里可能有客户数据，将来还可能经过带 AI 内容的
 * 路径（§94 禁止 AI 内容进计费日志，浏览器控制台同理）。
 */
export async function apiGet<T>(url: string, signal?: AbortSignal): Promise<T> {
  try {
    // `exactOptionalPropertyTypes` 下不能把 `signal: undefined` 传进去 ——
    // 「没有这个属性」和「属性是 undefined」是两回事。
    const response = await client.get<ApiEnvelope<T>>(url, signal === undefined ? {} : { signal });
    return unwrapEnvelope<T>(response.data);
  } catch (error) {
    throw toApiError(error);
  }
}
