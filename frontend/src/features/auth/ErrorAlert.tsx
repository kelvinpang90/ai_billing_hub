/**
 * 认证相关页面显示后端错误的统一方式。
 *
 * ⚠️ 只显示后端给的安全文案（§107 已保证它不含堆栈与内部细节）与 `request_id`，
 * **绝不把响应体打进控制台**。
 *
 * `request_id` 是用户能报给支持、支持能在日志里搜到的唯一钥匙 —— 少了它，用户
 * 能说的只有「登不进去」。所以它和错误文案是一体的，从 `LoginPage` 里抽出来是
 * 为了让三个认证页面不各写一遍（写三遍的那一遍早晚会漏掉 request_id）。
 */

import { Alert, Typography } from "antd";
import { useTranslation } from "react-i18next";

import { ApiError } from "../../api/client";

export function ErrorAlert({ error }: { error: ApiError | null }) {
  if (error === null) {
    return null;
  }
  return (
    <Alert
      type="error"
      showIcon
      style={{ marginBottom: 16 }}
      message={error.message}
      description={<ErrorReference error={error} />}
    />
  );
}

/** 单独的关联 ID 一行，给不适合套一整个 Alert 的地方（例如 antd 的 `Result`）用。 */
export function ErrorReference({ error }: { error: ApiError | null }) {
  const { t } = useTranslation();
  if (error === null || error.requestId === null) {
    return null;
  }
  return (
    <Typography.Text type="secondary">
      {t("error.referenceLabel")}
      {": "}
      <Typography.Text code copyable>
        {error.requestId}
      </Typography.Text>
    </Typography.Text>
  );
}
