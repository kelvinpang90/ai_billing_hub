/**
 * 管理端支付设置页（`/payment-settings`，AIH-TASK-194；设计闸门 #587 v3 §2「管理端页面」）。
 *
 * 语义以 docs/api.md「支付设置（管理端）」为准：读出设置（`GET /api/v1/admin/payment-settings`），编辑后带读到的 `version`
 * 整体替换（`PUT`）。表单字段即接口字段：
 *
 * - 三个金额框（最低额、最高额、缓冲）去掉首尾空白后原样作为字符串发送，格式与范围由后端判（422）；最高额去空白后为空
 *   表示 `null`（不设）。
 * - 两个秒数框去空白后必须匹配 `^[0-9]+$` 才转成整数发送；否则在框下提示、不发请求 —— 空框不当 0。
 * - 推荐金额是一个逗号分隔的输入框：保存前按逗号拆分、去掉每段首尾空白、丢掉空段（`50, 100,,300,` 得到三个）。
 *
 * 显示当前 `version`、最后修改人（种子行为 `null`，显示短横）与时间（按吉隆坡时间，标签标 MYT）。409 时提示设置已被
 * 他人修改并给刷新按钮（重新读，表单换成最新的值）；其他失败（含 422）显示后端的错误码、信息与 request_id。保存成功用
 * 返回值刷新表单。保存中按钮不可再点。只请求 `/api/v1/admin/` 路径。
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Descriptions, Form, Input, Space, Typography } from "antd";
import { useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { ADMIN_REQUIRED } from "../../api/adminCustomers";
import {
  PAYMENT_SETTINGS_QUERY_KEY,
  PAYMENT_SETTINGS_VERSION_CONFLICT,
  getPaymentSettings,
  updatePaymentSettings,
  type PaymentSettings,
  type PaymentSettingsUpdate,
} from "../../api/adminPaymentSettings";
import { ApiError } from "../../api/client";
import { DateTimeText } from "../../components/DateTimeText";
import { RequestReference } from "../../components/RequestReference";
import { CodeText, LoadingBlock, NoticeAlert } from "../catalog/ProvidersPage";

/** 没有值的格子。纯标点，不进翻译文件。 */
const EMPTY = "—";

/** 秒数框去掉首尾空白后的形状：只认 ASCII 数字，至少一位。 */
const SECONDS_PATTERN = /^[0-9]+$/;

/** 推荐金额在输入框里的分隔写法。 */
const PRESET_SEPARATOR = ", ";

/** 表单里各框的原始文字。 */
interface Draft {
  min_amount: string;
  max_amount: string;
  recovery_buffer: string;
  preset_amounts: string;
  reuse_window_seconds: string;
  pending_timeout_seconds: string;
}

function toDraft(settings: PaymentSettings): Draft {
  return {
    min_amount: settings.min_amount,
    max_amount: settings.max_amount ?? "",
    recovery_buffer: settings.recovery_buffer,
    preset_amounts: settings.preset_amounts.join(PRESET_SEPARATOR),
    reuse_window_seconds: String(settings.reuse_window_seconds),
    pending_timeout_seconds: String(settings.pending_timeout_seconds),
  };
}

/** 秒数框的值：去空白后匹配 `^[0-9]+$` 才是整数，否则 `null`（空框也是 `null`，不当 0）。 */
function parseSeconds(text: string): number | null {
  const trimmed = text.trim();
  return SECONDS_PATTERN.test(trimmed) ? Number(trimmed) : null;
}

/** 推荐金额：按逗号拆分、去掉每段首尾空白、丢掉空段。 */
function splitPresets(text: string): string[] {
  return text
    .split(",")
    .map((part) => part.trim())
    .filter((part) => part !== "");
}

/** 把表单换成请求体；秒数框不合格时返回 `null`（不发请求）。 */
function toUpdate(draft: Draft, version: number): PaymentSettingsUpdate | null {
  const reuse = parseSeconds(draft.reuse_window_seconds);
  const timeout = parseSeconds(draft.pending_timeout_seconds);
  if (reuse === null || timeout === null) {
    return null;
  }
  const max = draft.max_amount.trim();
  return {
    min_amount: draft.min_amount.trim(),
    max_amount: max === "" ? null : max,
    recovery_buffer: draft.recovery_buffer.trim(),
    preset_amounts: splitPresets(draft.preset_amounts),
    reuse_window_seconds: reuse,
    pending_timeout_seconds: timeout,
    version,
  };
}

/** 接口出错时的提示：403 说没有权限，其余用调用方给的标题。都带后端的错误码、message 与 request_id。 */
function SettingsErrorAlert({
  error,
  failedText,
  action,
}: {
  error: Error;
  failedText: string;
  action?: ReactNode;
}) {
  const { t } = useTranslation();
  const forbidden = error instanceof ApiError && error.code === ADMIN_REQUIRED;
  return (
    <Alert
      type={forbidden ? "warning" : "error"}
      showIcon
      style={{ marginBottom: 16 }}
      message={forbidden ? t("paymentSettings.forbidden") : failedText}
      description={
        <Space direction="vertical" size={4}>
          {error instanceof ApiError ? <CodeText code={error.code} /> : null}
          <RequestReference error={error} />
        </Space>
      }
      action={forbidden ? undefined : action}
    />
  );
}

export function PaymentSettingsPage() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [notice, setNotice] = useState<string | null>(null);
  // 表单换成新值的次数：保存成功或刷新成功各加一，表单按它重建（正在改的内容与错误提示一并清掉）。不用 dataUpdatedAt：
  // 两次写入落在同一毫秒时它不变，表单就不会换。
  const [generation, setGeneration] = useState(0);

  // ⚠️ 不在窗口重新聚焦时自己重读：每次读到新数据表单都会换成读到的值，正在改的内容会被冲掉。
  const settings = useQuery({
    queryKey: PAYMENT_SETTINGS_QUERY_KEY,
    queryFn: ({ signal }) => getPaymentSettings(signal),
    staleTime: Infinity,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  });

  const refresh = (): void => {
    setNotice(null);
    void settings.refetch().then((result) => {
      if (result.isSuccess) {
        setGeneration((current) => current + 1);
      }
    });
  };

  let body: ReactNode;
  if (settings.isPending) {
    body = <LoadingBlock text={t("paymentSettings.loading")} />;
  } else if (settings.data === undefined) {
    body = (
      <SettingsErrorAlert
        error={settings.error ?? new Error(t("paymentSettings.loadFailed"))}
        failedText={t("paymentSettings.loadFailed")}
        action={
          <Button size="small" onClick={refresh}>
            {t("paymentSettings.retry")}
          </Button>
        }
      />
    );
  } else {
    body = (
      <>
        {settings.isError ? (
          <SettingsErrorAlert error={settings.error} failedText={t("paymentSettings.loadFailed")} />
        ) : null}
        <NoticeAlert message={notice} />
        <SettingsForm
          key={generation}
          settings={settings.data}
          refreshing={settings.isFetching}
          onSaving={() => setNotice(null)}
          onSaved={(saved) => {
            queryClient.setQueryData(PAYMENT_SETTINGS_QUERY_KEY, saved);
            setGeneration((current) => current + 1);
            setNotice(t("paymentSettings.saved"));
          }}
          onRefresh={refresh}
        />
      </>
    );
  }

  return (
    <div role="region" aria-label={t("paymentSettings.title")}>
      <Card title={t("paymentSettings.title")}>
        <Typography.Paragraph type="secondary">{t("paymentSettings.intro")}</Typography.Paragraph>
        {body}
      </Card>
    </div>
  );
}

/**
 * 编辑表单。保存进行中禁用按钮，并用 ref 挡住第二次点击（`isPending` 要等下一次渲染才变 true）。
 */
function SettingsForm({
  settings,
  refreshing,
  onSaving,
  onSaved,
  onRefresh,
}: {
  settings: PaymentSettings;
  refreshing: boolean;
  onSaving: () => void;
  onSaved: (saved: PaymentSettings) => void;
  onRefresh: () => void;
}) {
  const { t } = useTranslation();
  const [draft, setDraft] = useState<Draft>(() => toDraft(settings));
  const [attempted, setAttempted] = useState(false);
  const inFlight = useRef(false);

  const save = useMutation({
    mutationFn: (update: PaymentSettingsUpdate) => updatePaymentSettings(update),
    onSuccess: onSaved,
    onSettled: () => {
      inFlight.current = false;
    },
  });

  const pending = save.isPending;
  const reuseInvalid = attempted && parseSeconds(draft.reuse_window_seconds) === null;
  const timeoutInvalid = attempted && parseSeconds(draft.pending_timeout_seconds) === null;

  const change =
    (field: keyof Draft) =>
    (event: { target: { value: string } }): void => {
      const value = event.target.value;
      setDraft((current) => ({ ...current, [field]: value }));
    };

  const submit = (): void => {
    if (inFlight.current || pending) {
      return;
    }
    setAttempted(true);
    const update = toUpdate(draft, settings.version);
    if (update === null) {
      return;
    }
    inFlight.current = true;
    onSaving();
    save.mutate(update);
  };

  const conflict = save.error instanceof ApiError && save.error.code === PAYMENT_SETTINGS_VERSION_CONFLICT;

  return (
    <>
      <Descriptions
        bordered
        size="small"
        column={1}
        style={{ marginBottom: 16 }}
        items={[
          { key: "version", label: t("paymentSettings.field.version"), children: String(settings.version) },
          { key: "updated_by", label: t("paymentSettings.field.updatedBy"), children: settings.updated_by ?? EMPTY },
          {
            key: "updated_at",
            label: t("paymentSettings.field.updatedAt"),
            children: <DateTimeText value={settings.updated_at} />,
          },
        ]}
      />
      {conflict ? (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          message={t("paymentSettings.conflict")}
          action={
            <Button size="small" onClick={onRefresh} loading={refreshing} disabled={refreshing}>
              {t("paymentSettings.refresh")}
            </Button>
          }
        />
      ) : save.isError ? (
        <SettingsErrorAlert error={save.error} failedText={t("paymentSettings.saveFailed")} />
      ) : null}
      <Form layout="vertical">
        <Form.Item
          label={t("paymentSettings.field.minAmount")}
          htmlFor="payment-settings-min-amount"
          extra={t("paymentSettings.hint.amount")}
        >
          <Input
            id="payment-settings-min-amount"
            value={draft.min_amount}
            disabled={pending}
            onChange={change("min_amount")}
          />
        </Form.Item>
        <Form.Item
          label={t("paymentSettings.field.maxAmount")}
          htmlFor="payment-settings-max-amount"
          extra={t("paymentSettings.hint.maxAmount")}
        >
          <Input
            id="payment-settings-max-amount"
            value={draft.max_amount}
            disabled={pending}
            placeholder={t("paymentSettings.noMaximum")}
            onChange={change("max_amount")}
          />
        </Form.Item>
        <Form.Item
          label={t("paymentSettings.field.recoveryBuffer")}
          htmlFor="payment-settings-recovery-buffer"
          extra={t("paymentSettings.hint.recoveryBuffer")}
        >
          <Input
            id="payment-settings-recovery-buffer"
            value={draft.recovery_buffer}
            disabled={pending}
            onChange={change("recovery_buffer")}
          />
        </Form.Item>
        <Form.Item
          label={t("paymentSettings.field.presetAmounts")}
          htmlFor="payment-settings-preset-amounts"
          extra={t("paymentSettings.hint.presetAmounts")}
        >
          <Input
            id="payment-settings-preset-amounts"
            value={draft.preset_amounts}
            disabled={pending}
            onChange={change("preset_amounts")}
          />
        </Form.Item>
        <Form.Item
          label={t("paymentSettings.field.reuseWindow")}
          htmlFor="payment-settings-reuse-window"
          extra={reuseInvalid ? undefined : t("paymentSettings.hint.reuseWindow")}
          validateStatus={reuseInvalid ? "error" : ""}
          help={reuseInvalid ? t("paymentSettings.secondsInvalid") : undefined}
        >
          <Input
            id="payment-settings-reuse-window"
            value={draft.reuse_window_seconds}
            disabled={pending}
            onChange={change("reuse_window_seconds")}
          />
        </Form.Item>
        <Form.Item
          label={t("paymentSettings.field.pendingTimeout")}
          htmlFor="payment-settings-pending-timeout"
          extra={timeoutInvalid ? undefined : t("paymentSettings.hint.pendingTimeout")}
          validateStatus={timeoutInvalid ? "error" : ""}
          help={timeoutInvalid ? t("paymentSettings.secondsInvalid") : undefined}
        >
          <Input
            id="payment-settings-pending-timeout"
            value={draft.pending_timeout_seconds}
            disabled={pending}
            onChange={change("pending_timeout_seconds")}
          />
        </Form.Item>
        <Button type="primary" onClick={submit} loading={pending} disabled={pending}>
          {t("paymentSettings.save")}
        </Button>
      </Form>
    </>
  );
}
