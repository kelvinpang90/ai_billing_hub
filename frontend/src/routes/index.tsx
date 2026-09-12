/**
 * 路由表。
 *
 * 目前只有落地页、认证相关的三页与兜底页。**不预建 spec §101 列的那九个
 * feature 目录** —— 空目录会让人以为「那块已经开工了」，和 runbook 不预留空
 * 标题是同一条道理。每个 feature 在它自己那个 Phase 落地时再建。
 *
 * ⚠️ 页面组件走 `lazy()` 动态引入（T0.10）。实测数字记在
 * [perf-baseline.md](../../../docs/perf-baseline.md)。
 */

import { Spin } from "antd";
import { Suspense, lazy } from "react";
import { Route, Routes } from "react-router";

import { AppLayout } from "../layouts/AppLayout";
import { RequireAuth } from "./RequireAuth";
import { RouteErrorBoundary } from "./RouteErrorBoundary";
import { ROUTES } from "./paths";

// ⚠️ 这几个模块都是**具名导出**，`lazy()` 要的是 `{ default }`，所以每条都要
// 转一次。忘了转的报错是「Element type is invalid」—— 一句指不回文件的话。
const LoginPage = lazy(() =>
  import("../features/auth/LoginPage").then((m) => ({ default: m.LoginPage })),
);
const ForgotPasswordPage = lazy(() =>
  import("../features/auth/ForgotPasswordPage").then((m) => ({ default: m.ForgotPasswordPage })),
);
const ResetPasswordPage = lazy(() =>
  import("../features/auth/ResetPasswordPage").then((m) => ({ default: m.ResetPasswordPage })),
);
const DashboardPage = lazy(() =>
  import("../features/dashboard/DashboardPage").then((m) => ({ default: m.DashboardPage })),
);
const NotFoundPage = lazy(() =>
  import("./NotFoundPage").then((m) => ({ default: m.NotFoundPage })),
);

/** 页面分片还在下载时显示的东西。与 `RequireAuth` 的等待态刻意长得一样。 */
function RouteFallback() {
  return (
    <div style={{ display: "flex", justifyContent: "center", paddingTop: 96 }}>
      <Spin size="large" />
    </div>
  );
}

export function AppRoutes() {
  return (
    // ⚠️ 错误边界在 `Suspense` **外面**：它要接住的正是「分片加载失败」，而那
    // 表现为 `lazy()` 的 Promise 被拒 —— Suspense 自己不处理拒绝，只处理挂起。
    // 少了它，一次发版就能让所有开着旧标签页的用户看到一片空白（见该文件注释）。
    <RouteErrorBoundary>
      {/* ⚠️ `Suspense` 放在 `Routes` **外面**。放进每个 element 里的话，切换路由时
          旧页面会先被卸载、再等新分片 —— 中间那一下是整页空白，不是一个转圈。 */}
      <Suspense fallback={<RouteFallback />}>
        <Routes>
          {/* 登录页在守卫**外面** —— 放进去就成了「要先登录才能登录」。
              忘记 / 重置密码同理，而且更硬：走到这两页的人按定义就是进不去的那些人。
              ⚠️ 它们必须在守卫外面**显式登记**。少了这两条，重置邮件里的链接会落到
              守卫里的 `*` 兜底上，被当成未登录直接重定向去登录页 —— 用户点开链接
              看到的是一张登录表单，而他来这儿正是因为登不进去。 */}
          <Route path={ROUTES.login} element={<LoginPage />} />
          <Route path={ROUTES.forgotPassword} element={<ForgotPasswordPage />} />
          <Route path={ROUTES.resetPassword} element={<ResetPasswordPage />} />
          <Route element={<RequireAuth />}>
            <Route element={<AppLayout />}>
              <Route path={ROUTES.dashboard} element={<DashboardPage />} />
              <Route path="*" element={<NotFoundPage />} />
            </Route>
          </Route>
        </Routes>
      </Suspense>
    </RouteErrorBoundary>
  );
}
