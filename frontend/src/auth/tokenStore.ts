/**
 * 访问令牌只放**内存**。
 *
 * ⚠️ **绝不放 `localStorage` / `sessionStorage` / 非 httpOnly 的 cookie。**
 * 那三个地方 JS 都读得到，于是任何一次 XSS（包括来自某个依赖的）都能把令牌
 * 整个拿走。放内存的代价是刷新页面会丢 —— 但那正是刷新令牌存在的理由：它在
 * httpOnly cookie 里，JS 读不到，页面加载时静默换一张新的访问令牌回来
 * （见 `AuthProvider`）。
 *
 * ⚠️ 用模块级变量而不是 React state，是因为 **axios 的拦截器要同步读它**。
 * 拦截器不在组件树里，拿不到 context；把令牌塞进 state 再想办法传出来，
 * 只会多一条容易读到旧值的路径。
 */

let accessToken: string | null = null;

/** 令牌变化时通知订阅者（React 那边靠它重新渲染）。 */
type Listener = (token: string | null) => void;
const listeners = new Set<Listener>();

export function getAccessToken(): string | null {
  return accessToken;
}

export function setAccessToken(token: string | null): void {
  accessToken = token;
  for (const listener of listeners) {
    listener(token);
  }
}

export function subscribeToAccessToken(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Only for tests. */
export function resetTokenStore(): void {
  accessToken = null;
  listeners.clear();
}
