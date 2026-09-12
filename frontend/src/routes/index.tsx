/**
 * 路由表。
 *
 * 目前只有落地页、认证相关的三页与兜底页。**不预建 spec §101 列的那九个
 * feature 目录** —— 空目录会让人以为「那块已经开工了」，和 runbook 不预留空
 * 标题是同一条道理。每个 feature 在它自己那个 Phase 落地时再建。
 */

import { Route, Routes } from "react-router";

import { ForgotPasswordPage } from "../features/auth/ForgotPasswordPage";
import { LoginPage } from "../features/auth/LoginPage";
import { ResetPasswordPage } from "../features/auth/ResetPasswordPage";
import { DashboardPage } from "../features/dashboard/DashboardPage";
import { AppLayout } from "../layouts/AppLayout";
import { NotFoundPage } from "./NotFoundPage";
import { RequireAuth } from "./RequireAuth";
import { ROUTES } from "./paths";

export function AppRoutes() {
  return (
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
  );
}
