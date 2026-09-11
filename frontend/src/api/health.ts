/**
 * 平台存活探针。
 *
 * 这是目前**唯一**真实存在的后端端点，所以骨架拿它证明整条链路是通的：
 * 浏览器 → nginx → api → §107 信封 → TanStack Query → 界面。
 * 业务端点从 Phase 1 起陆续加进 `src/api/` 的其他文件。
 *
 * ⚠️ 刻意不调 `/readyz`：它逐个报出依赖状态，`deploy/nginx/billing.conf` 只对
 * 私有网段开放（见那里的注释）。浏览器来的请求拿不到它，也不该拿到。
 */

import { apiGet } from "./client";

export interface HealthStatus {
  status: string;
}

export const HEALTH_QUERY_KEY = ["platform", "health"] as const;

export function fetchHealth(signal?: AbortSignal): Promise<HealthStatus> {
  return apiGet<HealthStatus>("/healthz", signal);
}
