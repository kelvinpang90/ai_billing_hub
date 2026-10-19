/**
 * 启用 / 停用客户账户（`POST /api/v1/admin/customers/{customer_id}/account-status`）。
 *
 * 语义以 docs/api.md「管理端账户状态」为准（设计闸门 #136，AIH-TASK-020 实现）。跃迁规则全在后端，
 * 前端不新增规则：详情页只按当前状态决定给哪个按钮（{@link accountStatusTarget}），能不能跃迁由
 * 后端的 409 / 422 说了算。
 *
 * - 原因必填：去掉首尾空白后 1–255（按码点数，与后端一致），只写业务说明，不写个人数据 ——
 *   原因进审计、永久保留。
 * - 停用要两步：填原因 → 确认（写明停用后这个客户的 AI 服务请求被拒，`ACCOUNT_DISABLED`）→ 发请求。
 *   确认之前不发任何请求。启用只有一步。
 * - 404 / 409 / 422 是后端明确拒绝、什么都没写：显示 `error.code` 与 request_id。
 * - 5xx 与网络错误：结果不确定，但目标等于当前状态时后端什么都不写（语义一节），所以原样重发是
 *   安全的。表单内容保留，按钮变成「再试一次」，发的是同一份请求体。不自动重试。
 *
 * 成功后关闭弹窗，让这个客户的详情与客户列表过期重读：状态以后端为准，不在前端推算。
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Form, Input, Modal, Typography, type FormRule } from "antd";
import { useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import {
  AccountStatusError,
  CUSTOMER_LISTS_QUERY_KEY,
  customerDetailQueryKey,
  postAccountStatus,
  type AccountStatusBody,
  type AccountStatusTarget,
  type CustomerSummary,
} from "../../api/adminCustomers";
import { ApiError } from "../../api/client";
import { RequestReference } from "../../components/RequestReference";
import { CustomerErrorAlert } from "./CustomerForm";

/** 与后端的上限一致（去掉首尾空白之后），docs/api.md「管理端账户状态」的请求体一节。 */
const REASON_MAX = 255;

/** docs/api.md 为这个接口列出的、后端明确拒绝且什么都没写的状态。 */
const REJECTIONS: ReadonlySet<number> = new Set([404, 409, 422]);

interface ReasonFormValues {
  reason: string;
}

/**
 * 详情页给哪个按钮：`ENABLED` 给停用，`DISABLED` 给启用。`PENDING_ACTIVATION`、`CLOSED`、
 * 认不出的值与缺字段都不给 —— 不是前端判跃迁，只是不在这些状态上提供入口。
 */
export function accountStatusTarget(status: string | undefined): AccountStatusTarget | null {
  if (status === "ENABLED") {
    return "DISABLED";
  }
  if (status === "DISABLED") {
    return "ENABLED";
  }
  return null;
}

type FailureKind = "rejected" | "retryable" | "other";

/**
 * 404 / 409 / 422：明确拒绝。5xx、没有响应（网络错误、超时）、拿不到状态的错误：可以原样重发。
 * 其余（401、403 等）交给通用的错误提示。
 */
function failureKind(error: Error): FailureKind {
  if (!(error instanceof AccountStatusError) || error.status === null) {
    return "retryable";
  }
  if (REJECTIONS.has(error.status)) {
    return "rejected";
  }
  return error.status >= 400 && error.status < 500 ? "other" : "retryable";
}

/** 按码点数，与 Python 的 `len()` 一致（`.length` 数的是 UTF-16 单元，emoji 算 2）。 */
function trimmedLength(value: unknown): number {
  return typeof value === "string" ? [...value.trim()].length : 0;
}

function maxTrimmed(limit: number, message: string): FormRule {
  return {
    validator: (_rule, value: unknown) =>
      trimmedLength(value) > limit ? Promise.reject(new Error(message)) : Promise.resolve(),
  };
}

export function AccountStatusModal({
  customer,
  target,
  onClose,
}: {
  customer: Pick<CustomerSummary, "id" | "company_name">;
  target: AccountStatusTarget;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [form] = Form.useForm<ReasonFormValues>();
  const [step, setStep] = useState<"edit" | "confirm">("edit");
  const [draft, setDraft] = useState<AccountStatusBody | null>(null);
  // `isPending` 要等下一次渲染才变 true：双击时第二次点击可能赶在按钮禁用之前。
  const inFlight = useRef(false);
  const disabling = target === "DISABLED";

  const change = useMutation({
    mutationFn: (body: AccountStatusBody) => postAccountStatus(customer.id, body),
    onSuccess: () => {
      // `exact`：只重读详情本身，不连带它下面的项目列表。
      void queryClient.invalidateQueries({
        queryKey: customerDetailQueryKey(customer.id),
        exact: true,
      });
      // 账户状态也显示在客户列表里。
      void queryClient.invalidateQueries({ queryKey: CUSTOMER_LISTS_QUERY_KEY });
      onClose();
    },
    onSettled: () => {
      inFlight.current = false;
    },
  });

  const pending = change.isPending;
  const kind = change.error === null ? null : failureKind(change.error);

  const send = (body: AccountStatusBody): void => {
    if (inFlight.current) {
      return;
    }
    inFlight.current = true;
    change.mutate(body);
  };

  const submitReason = (values: ReasonFormValues): void => {
    if (pending) {
      return;
    }
    const body: AccountStatusBody = { account_status: target, reason: values.reason.trim() };
    change.reset();
    setDraft(body);
    if (disabling) {
      // 停用先确认，确认之前不发请求。
      setStep("confirm");
      return;
    }
    send(body);
  };

  const backToEdit = (): void => {
    if (pending) {
      return;
    }
    change.reset();
    setStep("edit");
  };

  const close = (): void => {
    // 请求还在路上时不让关：关了就没人看得到结果。
    if (pending) {
      return;
    }
    onClose();
  };

  const retry = (): void => {
    if (draft !== null) {
      send(draft);
    }
  };

  let footer: ReactNode[];
  if (kind === "retryable" && draft !== null) {
    footer = [
      <Button key="cancel" onClick={close} disabled={pending}>
        {t("customers.accountStatus.cancel")}
      </Button>,
      <Button key="retry" type="primary" onClick={retry} loading={pending} disabled={pending}>
        {t("customers.accountStatus.retry")}
      </Button>,
    ];
  } else if (step === "confirm") {
    footer = [
      <Button key="back" onClick={backToEdit} disabled={pending}>
        {t("customers.accountStatus.back")}
      </Button>,
      <Button
        key="confirm"
        type="primary"
        danger
        onClick={() => {
          if (draft !== null) {
            send(draft);
          }
        }}
        loading={pending}
        disabled={pending}
      >
        {t("customers.accountStatus.confirmDisable")}
      </Button>,
    ];
  } else {
    footer = [
      <Button key="cancel" onClick={close} disabled={pending}>
        {t("customers.accountStatus.cancel")}
      </Button>,
      <Button
        key="submit"
        type="primary"
        onClick={() => form.submit()}
        loading={pending}
        disabled={pending}
      >
        {disabling ? t("customers.accountStatus.continue") : t("customers.accountStatus.confirmEnable")}
      </Button>,
    ];
  }

  let failure: ReactNode = null;
  if (change.error !== null) {
    const { error } = change;
    if (kind === "rejected" && error instanceof ApiError) {
      failure = (
        <Alert
          type="error"
          showIcon
          style={{ marginTop: 16 }}
          message={t("customers.accountStatus.rejected")}
          description={
            <>
              <Typography.Paragraph>
                {t("customers.accountStatus.errorCode", { code: error.code })}
              </Typography.Paragraph>
              <RequestReference error={error} />
            </>
          }
        />
      );
    } else if (kind === "retryable") {
      failure = (
        <Alert
          type="warning"
          showIcon
          style={{ marginTop: 16 }}
          message={t("customers.accountStatus.unknownTitle")}
          description={
            <>
              <Typography.Paragraph>{t("customers.accountStatus.unknownBody")}</Typography.Paragraph>
              <RequestReference error={error} />
            </>
          }
        />
      );
    } else {
      failure = (
        <div style={{ marginTop: 16 }}>
          <CustomerErrorAlert error={error} title={t("customers.accountStatus.rejected")} />
        </div>
      );
    }
  }

  return (
    <Modal
      open
      title={disabling ? t("customers.accountStatus.disable") : t("customers.accountStatus.enable")}
      width={560}
      footer={footer}
      onCancel={close}
      maskClosable={false}
      closable={!pending}
      keyboard={!pending}
    >
      <Typography.Paragraph strong>{customer.company_name}</Typography.Paragraph>
      <Alert
        type={disabling ? "warning" : "info"}
        showIcon
        style={{ marginBottom: 16 }}
        message={
          disabling ? t("customers.accountStatus.disableEffect") : t("customers.accountStatus.enableEffect")
        }
      />
      <Form<ReasonFormValues>
        form={form}
        layout="vertical"
        initialValues={{ reason: "" }}
        onFinish={submitReason}
        // 确认中、发送中只读；5xx 与网络错误之后也只读，「再试一次」发的就是框里这一份。
        disabled={step !== "edit" || pending || kind === "retryable"}
      >
        <Form.Item
          name="reason"
          label={t("customers.accountStatus.field.reason")}
          extra={t("customers.accountStatus.reasonHint")}
          rules={[
            { required: true, whitespace: true, message: t("customers.accountStatus.reasonRequired") },
            maxTrimmed(REASON_MAX, t("customers.accountStatus.reasonTooLong")),
          ]}
        >
          <Input.TextArea rows={3} />
        </Form.Item>
      </Form>
      {step === "confirm" ? (
        <Alert
          type="error"
          showIcon
          message={t("customers.accountStatus.confirmTitle", { company: customer.company_name })}
          description={t("customers.accountStatus.confirmBody")}
        />
      ) : null}
      {failure}
    </Modal>
  );
}
