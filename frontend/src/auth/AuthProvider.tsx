/**
 * Who is signed in, as far as the UI is concerned.
 *
 * ⚠️ **这不是访问控制。**spec §51 写死了「Backend MUST independently enforce
 * authorization. Never rely only on frontend menu hiding.」—— 这里做的只是
 * 「该给用户看登录页还是后台」，真正的门在后端。
 */

import { createContext, use, useCallback, useEffect, useState, type ReactNode } from "react";

import { logout as callLogout } from "../api/auth";
import { client } from "../api/client";
import { refreshAccessToken } from "./refresh";
import { getAccessToken, setAccessToken, subscribeToAccessToken } from "./tokenStore";

/**
 * `unknown` 是**必须有**的第三态：页面刚加载时我们还不知道 cookie 里那张刷新
 * 令牌还算不算数。少了它，守卫会在静默刷新回来之前就把人踢到登录页 ——
 * 表现是「每次刷新页面都要重新登录」。
 */
export type AuthStatus = "unknown" | "anonymous" | "authenticated";

interface AuthContextValue {
  status: AuthStatus;
  signedIn: (accessToken: string) => void;
  signOut: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus>("unknown");

  useEffect(() => {
    let cancelled = false;

    // ⚠️ 启动时静默换一张访问令牌。访问令牌只在内存里，刷新页面必然丢；
    // 不做这一步的话，用户每按一次 F5 就得重新登录一次。
    void refreshAccessToken(client).then((token) => {
      if (!cancelled) {
        setStatus(token ? "authenticated" : "anonymous");
      }
    });

    // 令牌可能被拦截器换掉（自动刷新）或清掉（刷新失败），状态要跟着走。
    const unsubscribe = subscribeToAccessToken((token) => {
      if (!cancelled) {
        setStatus(token ? "authenticated" : "anonymous");
      }
    });

    return () => {
      cancelled = true;
      unsubscribe();
    };
  }, []);

  const signedIn = useCallback((accessToken: string) => {
    setAccessToken(accessToken);
  }, []);

  const signOut = useCallback(async () => {
    try {
      // 先告诉后端把整条会话链吊销掉 —— 只清本地令牌的话，那张刷新 cookie
      // 还活着，谁拿到它都能继续用。
      await callLogout();
    } finally {
      setAccessToken(null);
    }
  }, []);

  return <AuthContext value={{ status, signedIn, signOut }}>{children}</AuthContext>;
}

export function useAuth(): AuthContextValue {
  const value = use(AuthContext);
  if (value === null) {
    // 忘了包 Provider 时，状态会永远停在 unknown —— 那是个只表现为「页面一直转」
    // 的故障，很难从现象推回原因。所以这里直接炸。
    throw new Error("useAuth must be used inside <AuthProvider>");
  }
  return value;
}

export { getAccessToken };
