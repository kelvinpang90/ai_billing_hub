/**
 * 登录后的落地页。
 *
 * **现在它只有一张平台状态卡片**，因为后端目前只有 `/healthz` 这一个真端点。
 * 它存在的意义不是给客户看，而是证明整条链路真的通：
 * 浏览器 → nginx → api → §107 信封 → TanStack Query → 界面。
 * Phase 1 起这里换成真内容（钱包余额、近期用量），卡片本身会被删掉。
 *
 * 三种状态都必须画出来（spec §132 的 DoD 第 8 条）：加载中、成功、失败。
 * **失败那一支要带上 request_id** —— 那是用户能报给支持、支持能在服务端日志里
 * 搜到的唯一钥匙。
 */

import { useQuery } from "@tanstack/react-query";
import { Alert, Button, Card, Skeleton, Typography } from "antd";
import { useTranslation } from "react-i18next";

import { ApiError } from "../../api/client";
import { HEALTH_QUERY_KEY, fetchHealth } from "../../api/health";

export function DashboardPage() {
  const { t } = useTranslation();
  const health = useQuery({
    queryKey: HEALTH_QUERY_KEY,
    queryFn: ({ signal }) => fetchHealth(signal),
  });

  if (health.isPending) {
    return (
      <Card title={t("dashboard.platformStatus.title")}>
        <Skeleton active title={false} paragraph={{ rows: 1 }} />
        <Typography.Text type="secondary">
          {t("dashboard.platformStatus.checking")}
        </Typography.Text>
      </Card>
    );
  }

  if (health.isError) {
    return (
      <Card title={t("dashboard.platformStatus.title")}>
        <Alert
          type="error"
          showIcon
          message={t("dashboard.platformStatus.unreachable")}
          description={<RequestReference error={health.error} />}
          action={
            <Button size="small" onClick={() => void health.refetch()}>
              {t("dashboard.platformStatus.retry")}
            </Button>
          }
        />
      </Card>
    );
  }

  return (
    <Card title={t("dashboard.platformStatus.title")}>
      <Typography.Text>{t("dashboard.platformStatus.ok")}</Typography.Text>
    </Card>
  );
}

/**
 * 把关联 ID 显示出来。没有它，用户能说的只有「打不开」。
 *
 * ⚠️ 只显示后端给的安全文案与 request_id，**不把响应体打进控制台**（§94）。
 *
 * ⚠️ 参数类型是 `Error` 而不是 `ApiError`，并在运行时收窄：TanStack Query 把
 * `error` 标成 `Error`，而 queryFn 里一个普通的 TypeError 也会走到这里。写成
 * `ApiError` 是在骗类型系统，真出事时读到的是 `undefined`。
 */
function RequestReference({ error }: { error: Error }) {
  const { t } = useTranslation();

  if (!(error instanceof ApiError) || error.requestId === null) {
    return <Typography.Text type="secondary">{error.message}</Typography.Text>;
  }

  return (
    <Typography.Text type="secondary">
      {error.message}
      {" · "}
      {t("error.referenceLabel")}
      {": "}
      <Typography.Text code copyable>
        {error.requestId}
      </Typography.Text>
    </Typography.Text>
  );
}
