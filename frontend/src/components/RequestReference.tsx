import { Typography } from "antd";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api/client";

/**
 * 把关联 ID 显示出来。没有它，用户能说的只有「打不开」。
 *
 * ⚠️ 只显示后端给的安全文案与 request_id，**不把响应体打进控制台**（§94）。
 *
 * ⚠️ 参数类型是 `Error` 而不是 `ApiError`，并在运行时收窄：TanStack Query 把
 * `error` 标成 `Error`，而 queryFn 里一个普通的 TypeError 也会走到这里。写成
 * `ApiError` 是在骗类型系统，真出事时读到的是 `undefined`。
 */
export function RequestReference({ error }: { error: Error }) {
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
