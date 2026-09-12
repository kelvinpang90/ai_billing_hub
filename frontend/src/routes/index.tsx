/**
 * 路由表。
 *
 * 目前只有落地页与兜底页。**不预建 spec §101 列的那九个 feature 目录** ——
 * 空目录会让人以为「那块已经开工了」，和 runbook 不预留空标题是同一条道理。
 * 每个 feature 在它自己那个 Phase 落地时再建。
 */

import { Route, Routes } from "react-router";

import { LoginPage } from "../features/auth/LoginPage";
import { DashboardPage } from "../features/dashboard/DashboardPage";
import { AppLayout } from "../layouts/AppLayout";
import { NotFoundPage } from "./NotFoundPage";
import { RequireAuth } from "./RequireAuth";
import { ROUTES } from "./paths";

export function AppRoutes() {
  return (
    <Routes>
      {/* 登录页在守卫**外面** —— 放进去就成了「要先登录才能登录」。 */}
      <Route path={ROUTES.login} element={<LoginPage />} />
      <Route element={<RequireAuth />}>
        <Route element={<AppLayout />}>
          <Route path={ROUTES.dashboard} element={<DashboardPage />} />
          <Route path="*" element={<NotFoundPage />} />
        </Route>
      </Route>
    </Routes>
  );
}
