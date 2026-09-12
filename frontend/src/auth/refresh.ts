/**
 * Single-flight refresh.
 *
 * ⚠️ **这是本模块存在的全部理由，不是性能优化。**
 *
 * 后端的刷新令牌是**一次性**的：用掉就作废，再被提交一次会被判为重放，
 * 于是**整条会话链被吊销**（见 `app/services/auth.py` 的 `_treat_as_replay`）。
 *
 * 所以「每个 401 各自去刷新一次」是一个会咬人的实现：页面上三个请求同时过期，
 * 三个刷新并发发出 —— 第一个换走了令牌，另外两个拿着**已经用过的**那张去刷，
 * 后端判定令牌被盗，把用户踢出去。表现是「用户随机掉登录」，而且越是网络慢、
 * 请求多的页面越容易触发。
 *
 * 这里保证同一时刻只有一次刷新在飞，其余调用方等同一个 Promise。
 */

import type { AxiosInstance } from "axios";

import { getAccessToken, setAccessTokenIfUnchanged, tokenGeneration } from "./tokenStore";

/** 正在飞的那一次。null = 当前没有刷新在进行。 */
let inFlight: Promise<string | null> | null = null;

interface RefreshEnvelope {
  success: boolean;
  data: { access_token?: string | null } | null;
}

/**
 * Exchange the refresh cookie for a new access token.
 *
 * 返回新的访问令牌；刷新失败（cookie 没了 / 过期 / 被吊销）返回 `null`，
 * 由调用方决定怎么办（通常是把用户送回登录页）。
 */
export function refreshAccessToken(client: AxiosInstance): Promise<string | null> {
  inFlight ??= run(client).finally(() => {
    // ⚠️ 必须在 settle 之后清掉，否则下一次过期会复用这个已经完成的 Promise，
    // 永远拿到同一张（已经过期的）令牌。
    inFlight = null;
  });
  return inFlight;
}

async function run(client: AxiosInstance): Promise<string | null> {
  // ⚠️ 出发前记下代号，回来时只在「这期间没人写过令牌」的前提下才写。
  //
  // 页面加载时的静默刷新可能飞很久（超时 15 秒），而用户完全可以在它返回之前
  // 就登录成功。那一次刷新的结果**已经过时**：无论它是失败（会把新会话清掉）
  // 还是成功（会把新会话换成旧 cookie 那条），覆盖都是错的。
  // 现象是「登录成功后立刻被踢回登录页」，只在慢网络下偶发，极难复现。
  const startedAt = tokenGeneration();
  try {
    // ⚠️ `skipAuthRefresh` 有两个作用：请求拦截器据此**不加** Authorization
    // 头（刷新靠 httpOnly cookie 认人），响应拦截器据此**不对它做刷新重试**
    // （刷新自己 401 再刷一次还是 401，只会打转）。
    const response = await client.post<RefreshEnvelope>("/api/v1/auth/refresh", undefined, {
      skipAuthRefresh: true,
    });
    const token = response.data?.data?.access_token ?? null;
    return setAccessTokenIfUnchanged(token, startedAt) ? token : getAccessToken();
  } catch {
    // 刷新失败就是「这条会话结束了」—— 可能过期，也可能因为重放检测被整条吊销。
    // 两种情况下用户都得重新登录，所以这里不区分。
    return setAccessTokenIfUnchanged(null, startedAt) ? null : getAccessToken();
  }
}

/** Only for tests. */
export function resetRefreshState(): void {
  inFlight = null;
}
