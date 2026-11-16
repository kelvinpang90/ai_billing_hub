/**
 * 关户（`GET …/closure-check` 与 `POST /api/v1/admin/customers/{customer_id}/close`）。
 *
 * 语义以 docs/api.md「管理端关户」为准，交互以 docs/design/AIH-TASK-209-account-closure.md §2「前端」为准
 * （设计闸门 #707，AIH-TASK-214 实现）。关户把账户从 `DISABLED` 置为终态 `CLOSED`，不能重开；能不能关由
 * 后端说了算，前端只读检查、拦住明显不该发的请求。
 *
 * - 打开即读关户检查，显示余额与四类未结的计数。读的过程中显示加载状态；读失败（4xx、5xx、网络错误）显示
 *   `error.code`（没有时显示 HTTP 状态）与 request_id，给「Refresh」重读；`blockers` 非空时逐条说明原因，
 *   同样给「Refresh」。这几种情况下确认按钮都不可点。
 * - 只有最近一次检查成功且 `blockers` 为空时，确认按钮才由下面两项决定是否可点：
 *   原因去掉首尾空白后 1–255（按码点数，与 `AccountStatusModal` 同一计数法），只写业务说明、不写个人数据；
 *   客户名输入框的值去掉首尾空白后与详情的 `company_name` 完全相同（区分大小写）。客户名只在前端核对。
 * - 404 / 409 / 422 是后端明确拒绝、什么都没写：显示 `error.code`、信息与 request_id，并重读检查。
 * - 5xx 与网络错误：结果不确定，但已关户时后端 200、什么都不写，未关户时重新判定，所以原样重发是安全的。
 *   输入保留且只读，按钮变成「Try again」，发的是同一份请求体。不自动重试（检查也不自动重试）。
 *
 * 成功后关闭弹窗，让这个客户的详情与客户列表过期重读：状态以后端为准，不在前端推算。
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Descriptions,
  Form,
  Input,
  Modal,
  Space,
  Spin,
  Typography,
  type DescriptionsProps,
} from "antd";
import { useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import {
  CloseAccountError,
  CUSTOMER_LISTS_QUERY_KEY,
  closureCheckQueryKey,
  customerDetailQueryKey,
  getClosureCheck,
  postCloseAccount,
  type ClosureCheck,
  type CloseAccountBody,
  type CustomerDetail,
} from "../../api/adminCustomers";
import { ApiError, MALFORMED_RESPONSE } from "../../api/client";
import { MoneyText } from "../../components/MoneyText";
import { RequestReference } from "../../components/RequestReference";
import { CustomerErrorAlert } from "./CustomerForm";

/** 与后端的上限一致（去掉首尾空白之后），docs/api.md「管理端关户」的请求体一节。 */
const REASON_MAX = 255;

/** docs/api.md 为关户接口列出的、后端明确拒绝且什么都没写的状态。 */
const REJECTIONS: ReadonlySet<number> = new Set([404, 409, 422]);

type TFunction = ReturnType<typeof useTranslation>["t"];

type FailureKind = "rejected" | "retryable" | "other";

/**
 * 404 / 409 / 422：明确拒绝。5xx、没有响应（网络错误、超时）、拿不到状态的错误：可以原样重发。
 * 其余（401、403 等）交给通用的错误提示。
 */
function failureKind(error: Error): FailureKind {
  if (!(error instanceof CloseAccountError) || error.status === null) {
    return "retryable";
  }
  if (REJECTIONS.has(error.status)) {
    return "rejected";
  }
  return error.status >= 400 && error.status < 500 ? "other" : "retryable";
}

/** 按码点数，与 Python 的 `len()` 一致（`.length` 数的是 UTF-16 单元，emoji 算 2）。 */
function trimmedLength(value: string): number {
  return [...value.trim()].length;
}

/** 失败时显示的码：后端的 `error.code`；响应里没有（例如代理层的错误页）时显示 HTTP 状态。 */
function failureCode(error: Error, t: TFunction): string | null {
  if (error instanceof CloseAccountError && error.code === MALFORMED_RESPONSE && error.status !== null) {
    return t("customers.closeAccount.httpStatus", { status: error.status });
  }
  return error instanceof ApiError ? error.code : null;
}

/** 每个原因码一句说明。只认 docs/api.md 列出的码；以后多出来的码原样带出，不猜它的意思。 */
function blockerText(code: string, t: TFunction): string {
  switch (code) {
    case "ACCOUNT_STATUS_TRANSITION_INVALID":
      return t("customers.closeAccount.blocker.notDisabled");
    case "ACCOUNT_CLOSURE_BALANCE_NOT_ZERO":
      return t("customers.closeAccount.blocker.balance");
    case "ACCOUNT_CLOSURE_USAGE_PENDING":
      return t("customers.closeAccount.blocker.usage");
    case "ACCOUNT_CLOSURE_AI_USAGE_PENDING":
      return t("customers.closeAccount.blocker.aiUsage");
    case "ACCOUNT_CLOSURE_PAYMENTS_PENDING":
      return t("customers.closeAccount.blocker.payments");
    case "ACCOUNT_CLOSURE_PAYMENT_EVENTS_PENDING":
      return t("customers.closeAccount.blocker.paymentEvents");
    default:
      return t("customers.closeAccount.blocker.other", { code });
  }
}

export function CloseAccountModal({
  customer,
  onClose,
}: {
  customer: Pick<CustomerDetail, "id" | "company_name" | "wallet">;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [reason, setReason] = useState("");
  const [typedName, setTypedName] = useState("");
  const [draft, setDraft] = useState<CloseAccountBody | null>(null);
  // `isPending` 要等下一次渲染才变 true：双击时第二次点击可能赶在按钮禁用之前。
  const inFlight = useRef(false);

  const check = useQuery({
    queryKey: closureCheckQueryKey(customer.id),
    queryFn: ({ signal }) => getClosureCheck(customer.id, signal),
    // 不自动重试；关了弹窗就丢掉，下次打开从头读。
    retry: false,
    gcTime: 0,
    refetchOnWindowFocus: false,
  });

  const closing = useMutation({
    mutationFn: (body: CloseAccountBody) => postCloseAccount(customer.id, body),
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
    onError: (error) => {
      // 后端明确拒绝：此刻的情况与上次检查不同了，重读一次让管理员看到原因。
      if (failureKind(error) === "rejected") {
        void check.refetch();
      }
    },
    onSettled: () => {
      inFlight.current = false;
    },
  });

  const pending = closing.isPending;
  const kind = closing.error === null ? null : failureKind(closing.error);

  const reasonLength = trimmedLength(reason);
  const reasonTooLong = reasonLength > REASON_MAX;
  const checkReady =
    !check.isFetching && check.isSuccess && check.data.blockers.length === 0;
  const canConfirm =
    checkReady &&
    reasonLength >= 1 &&
    !reasonTooLong &&
    typedName.trim() === customer.company_name &&
    !pending;

  const send = (body: CloseAccountBody): void => {
    if (inFlight.current) {
      return;
    }
    inFlight.current = true;
    closing.mutate(body);
  };

  const confirm = (): void => {
    if (!canConfirm) {
      return;
    }
    const body: CloseAccountBody = { reason: reason.trim() };
    closing.reset();
    setDraft(body);
    send(body);
  };

  const retry = (): void => {
    if (draft !== null) {
      send(draft);
    }
  };

  const close = (): void => {
    // 请求还在路上时不让关：关了就没人看得到结果。
    if (pending) {
      return;
    }
    onClose();
  };

  const refresh = (): void => {
    void check.refetch();
  };

  const refreshButton = (
    <Button size="small" onClick={refresh} disabled={check.isFetching}>
      {t("customers.closeAccount.refresh")}
    </Button>
  );

  const retrying = kind === "retryable" && draft !== null;
  const footer: ReactNode[] = [
    <Button key="cancel" onClick={close} disabled={pending}>
      {t("customers.closeAccount.cancel")}
    </Button>,
    retrying ? (
      <Button key="retry" type="primary" danger onClick={retry} loading={pending} disabled={pending}>
        {t("customers.closeAccount.retry")}
      </Button>
    ) : (
      <Button
        key="confirm"
        type="primary"
        danger
        onClick={confirm}
        loading={pending}
        disabled={!canConfirm}
      >
        {t("customers.closeAccount.confirm")}
      </Button>
    ),
  ];

  let checkSection: ReactNode;
  if (check.isFetching) {
    checkSection = (
      <Space style={{ marginBottom: 16 }}>
        <Spin size="small" />
        <Typography.Text type="secondary">{t("customers.closeAccount.check.loading")}</Typography.Text>
      </Space>
    );
  } else if (check.isError) {
    const code = failureCode(check.error, t);
    checkSection = (
      <Alert
        type="error"
        showIcon
        style={{ marginBottom: 16 }}
        message={t("customers.closeAccount.check.loadFailed")}
        description={
          <>
            {code === null ? null : (
              <Typography.Paragraph>{t("customers.closeAccount.errorCode", { code })}</Typography.Paragraph>
            )}
            <RequestReference error={check.error} />
          </>
        }
        action={refreshButton}
      />
    );
  } else if (check.isSuccess) {
    checkSection = (
      <>
        <ClosureCheckDescriptions check={check.data} currency={customer.wallet.currency} />
        {check.data.blockers.length > 0 ? (
          <Alert
            type="error"
            showIcon
            style={{ marginTop: 16, marginBottom: 16 }}
            message={t("customers.closeAccount.blockersTitle")}
            description={
              <ul style={{ margin: 0, paddingInlineStart: 20 }}>
                {check.data.blockers.map((code) => (
                  <li key={code}>
                    {blockerText(code, t)} <Typography.Text code>{code}</Typography.Text>
                  </li>
                ))}
              </ul>
            }
            action={refreshButton}
          />
        ) : (
          <Alert
            type="success"
            showIcon
            style={{ marginTop: 16, marginBottom: 16 }}
            message={t("customers.closeAccount.noBlockers")}
          />
        )}
      </>
    );
  } else {
    checkSection = null;
  }

  let failure: ReactNode = null;
  if (closing.error !== null) {
    const { error } = closing;
    if (kind === "rejected" && error instanceof ApiError) {
      failure = (
        <Alert
          type="error"
          showIcon
          style={{ marginTop: 16 }}
          message={t("customers.closeAccount.rejected")}
          description={
            <>
              <Typography.Paragraph>
                {t("customers.closeAccount.errorCode", { code: error.code })}
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
          message={t("customers.closeAccount.unknownTitle")}
          description={
            <>
              <Typography.Paragraph>{t("customers.closeAccount.unknownBody")}</Typography.Paragraph>
              <RequestReference error={error} />
            </>
          }
        />
      );
    } else {
      failure = (
        <div style={{ marginTop: 16 }}>
          <CustomerErrorAlert error={error} title={t("customers.closeAccount.rejected")} />
        </div>
      );
    }
  }

  // 发送中只读；5xx 与网络错误之后也只读，「Try again」发的就是当时那一份。
  const inputsDisabled = pending || retrying;

  return (
    <Modal
      open
      title={t("customers.closeAccount.title")}
      width={600}
      footer={footer}
      onCancel={close}
      maskClosable={false}
      closable={!pending}
      keyboard={!pending}
    >
      <Typography.Paragraph strong>{customer.company_name}</Typography.Paragraph>
      <Alert
        type="warning"
        showIcon
        style={{ marginBottom: 16 }}
        message={t("customers.closeAccount.effect")}
      />
      {checkSection}
      <Form layout="vertical" disabled={inputsDisabled}>
        <Form.Item
          label={t("customers.closeAccount.field.reason")}
          htmlFor="close-account-reason"
          extra={t("customers.closeAccount.reasonHint")}
          validateStatus={reasonTooLong ? "error" : ""}
          help={reasonTooLong ? t("customers.closeAccount.reasonTooLong") : undefined}
        >
          <Input.TextArea
            id="close-account-reason"
            rows={3}
            value={reason}
            onChange={(event) => setReason(event.target.value)}
          />
        </Form.Item>
        <Form.Item
          label={t("customers.closeAccount.field.companyName")}
          htmlFor="close-account-company-name"
          extra={t("customers.closeAccount.companyNameHint", { company: customer.company_name })}
        >
          <Input
            id="close-account-company-name"
            autoComplete="off"
            value={typedName}
            onChange={(event) => setTypedName(event.target.value)}
          />
        </Form.Item>
      </Form>
      {failure}
    </Modal>
  );
}

function ClosureCheckDescriptions({ check, currency }: { check: ClosureCheck; currency: string }) {
  const { t } = useTranslation();
  const items: DescriptionsProps["items"] = [
    {
      key: "balance",
      label: t("customers.closeAccount.check.balance"),
      children: <MoneyText amount={check.balance} currency={currency} />,
    },
    {
      key: "unresolved_usage_events",
      label: t("customers.closeAccount.check.usageEvents"),
      children: check.unresolved_usage_events,
    },
    {
      key: "unresolved_ai_usage",
      label: t("customers.closeAccount.check.aiUsage"),
      children: check.unresolved_ai_usage,
    },
    {
      key: "unresolved_payments",
      label: t("customers.closeAccount.check.payments"),
      children: check.unresolved_payments,
    },
    {
      key: "unresolved_payment_events",
      label: t("customers.closeAccount.check.paymentEvents"),
      children: check.payment_events_truncated
        ? t("customers.closeAccount.check.paymentEventsTruncated", {
            events: check.unresolved_payment_events,
          })
        : check.unresolved_payment_events,
    },
  ];
  return <Descriptions bordered size="small" column={1} items={items} />;
}
