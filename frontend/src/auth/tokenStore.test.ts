/**
 * 访问令牌只在内存里。
 *
 * 这里最值钱的一条是「令牌没有落到任何持久化存储」——它是 XSS 之后能不能把
 * 会话整个拿走的分界线，而它的失败方式是**完全静默**的：谁要是"顺手"加一行
 * `localStorage.setItem`，功能照常，测试照绿。
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import {
  getAccessToken,
  resetTokenStore,
  setAccessToken,
  subscribeToAccessToken,
} from "./tokenStore";

afterEach(() => {
  resetTokenStore();
  localStorage.clear();
  sessionStorage.clear();
});

describe("tokenStore", () => {
  it("starts empty and round-trips the token", () => {
    expect(getAccessToken()).toBeNull();
    setAccessToken("token-1");
    expect(getAccessToken()).toBe("token-1");
    setAccessToken(null);
    expect(getAccessToken()).toBeNull();
  });

  it("never writes the token to a store that JavaScript can read back", () => {
    setAccessToken("token-secret");

    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
    // cookie 里也不许有 —— 能被 document.cookie 读到的 cookie 不是 httpOnly 的。
    expect(document.cookie).not.toContain("token-secret");
  });

  it("notifies subscribers on every change, including clearing", () => {
    const seen: (string | null)[] = [];
    subscribeToAccessToken((token) => seen.push(token));

    setAccessToken("token-1");
    setAccessToken("token-2");
    setAccessToken(null);

    expect(seen).toEqual(["token-1", "token-2", null]);
  });

  it("stops notifying after unsubscribe", () => {
    const listener = vi.fn();
    const unsubscribe = subscribeToAccessToken(listener);

    setAccessToken("token-1");
    unsubscribe();
    setAccessToken("token-2");

    expect(listener).toHaveBeenCalledTimes(1);
    expect(listener).toHaveBeenCalledWith("token-1");
    // 取消订阅后仍然改得动令牌本身，只是没人被通知。
    expect(getAccessToken()).toBe("token-2");
  });
});
