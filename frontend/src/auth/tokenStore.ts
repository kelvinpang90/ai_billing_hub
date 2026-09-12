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

/**
 * 每次写入都 +1。
 *
 * ⚠️ 存在的理由是一个具体的竞态：页面加载时的静默刷新是**异步**的，而用户可以
 * 在它返回之前就把密码输完、登录成功（密码管理器自动提交 + 慢网络时尤其容易）。
 * 那次刷新迟到地失败，若无条件 `setAccessToken(null)`，就会把刚拿到的会话抹掉 ——
 * 现象是「登录成功后立刻被踢回登录页」，而且只在慢网络下偶发。
 *
 * 代号让「我出发之后有没有别人写过」成为一个可判断的事实，而不是靠比较令牌值
 * 去猜（那分不清「没人写过」和「别人写了同一个值」）。
 */
let generation = 0;

/** 令牌变化时通知订阅者（React 那边靠它重新渲染）。 */
type Listener = (token: string | null) => void;
const listeners = new Set<Listener>();

export function getAccessToken(): string | null {
  return accessToken;
}

/** 当前代号。配合 `setAccessTokenIfUnchanged` 使用。 */
export function tokenGeneration(): number {
  return generation;
}

export function setAccessToken(token: string | null): void {
  generation += 1;
  accessToken = token;
  for (const listener of listeners) {
    listener(token);
  }
}

/**
 * 只有在 `since` 之后没有别人写过时才写入。
 *
 * 返回是否真的写了 —— 没写说明调用方手上的结果已经过期，该丢掉。
 */
export function setAccessTokenIfUnchanged(token: string | null, since: number): boolean {
  if (generation !== since) {
    return false;
  }
  setAccessToken(token);
  return true;
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
  generation = 0;
  listeners.clear();
}
