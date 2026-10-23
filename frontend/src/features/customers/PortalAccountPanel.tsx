/**
 * 客户详情页的门户账号面板（AIH-TASK-095）。
 *
 * 语义以 docs/api.md「管理端门户账号」为准（设计闸门 #368 v12 §2）。跃迁规则全在后端，面板不新增规则：
 * 只按当前状态决定给哪些按钮，能不能做由后端的 409 说了算。
 *
 * - 未开通（GET 回 404 `PORTAL_ACCOUNT_NOT_FOUND`，`getPortalAccount` 给 `null`）：开通表单，登录邮箱默认
 *   带出客户的联系邮箱、可改。开通**不幂等**，提交进行中禁用。
 * - 已开通：显示门户账号对象的全部字段；按钮按状态出现 —— `INVITED` 重发邀请与停用，`ACTIVE` 停用，
 *   `DISABLED` 启用；`two_factor_enabled` 为 true 时另有重置 2FA。
 * - 客户已关户（`CLOSED`）：不出开通、重发、启用（后端会回 409 `ACCOUNT_CLOSED`）；停用与重置 2FA 照常。
 * - 重发可改邮箱；停用、启用、重置 2FA 都要确认。重置 2FA 的确认框必须填原因（去首尾空白后 1–255），
 *   并提示先线下核实身份。
 *
 * 成功后让面板的 GET 过期重读：状态以后端为准，不在前端推算。有专门文案的 409 之后同样重读
 * （面板上的状态多半已经过时；`PORTAL_ACCOUNT_EXISTS` 的响应里不带邮箱，只能重读）。
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Form,
  Input,
  Modal,
  Skeleton,
  Space,
  Tag,
  Typography,
  type DescriptionsProps,
  type FormRule,
} from "antd";
import { useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import type { CustomerSummary } from "../../api/adminCustomers";
import { ApiError } from "../../api/client";
import {
  ACCOUNT_CLOSED,
  EMAIL_IN_USE,
  PORTAL_2FA_NOT_ENABLED,
  PORTAL_ACCOUNT_ALREADY_DISABLED,
  PORTAL_ACCOUNT_EXISTS,
  PORTAL_ACCOUNT_NOT_DISABLED,
  PORTAL_ACCOUNT_NOT_INVITED,
  createPortalAccount,
  disablePortalAccount,
  enablePortalAccount,
  getPortalAccount,
  portalAccountQueryKey,
  resendInvitation,
  resetPortalTwoFactor,
  type PortalAccount,
} from "../../api/portalAccounts";
import { DateTimeText } from "../../components/DateTimeText";
import { RequestReference } from "../../components/RequestReference";
import { CustomerErrorAlert } from "./CustomerForm";

/** 与后端的上限一致（去掉首尾空白之后），docs/api.md「重置 2FA」。 */
const REASON_MAX = 255;

/** 空的时间显示成一道横线，与详情页的空字段一致。 */
const NO_VALUE = "—";

/** docs/api.md 为这六个接口列出的 409。 */
const CONFLICT_CODES: ReadonlySet<string> = new Set([
  PORTAL_ACCOUNT_EXISTS,
  EMAIL_IN_USE,
  ACCOUNT_CLOSED,
  PORTAL_ACCOUNT_NOT_INVITED,
  PORTAL_ACCOUNT_ALREADY_DISABLED,
  PORTAL_ACCOUNT_NOT_DISABLED,
  PORTAL_2FA_NOT_ENABLED,
]);

type Action = "resend" | "disable" | "enable" | "reset2fa";
type Done = Action | "created";

function conflictCode(error: unknown): string | null {
  return error instanceof ApiError && CONFLICT_CODES.has(error.code) ? error.code : null;
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

function PortalStatusTag({ status }: { status: string }) {
  const { t } = useTranslation();
  if (status === "INVITED") {
    return <Tag color="gold">{t("portalAccount.status.invited")}</Tag>;
  }
  if (status === "ACTIVE") {
    return <Tag color="green">{t("portalAccount.status.active")}</Tag>;
  }
  if (status === "DISABLED") {
    return <Tag>{t("portalAccount.status.disabled")}</Tag>;
  }
  // 后端以后加了新值：原样显示，不崩。
  return <Tag>{status}</Tag>;
}

function TimeValue({ value }: { value: string | null }) {
  return value === null ? <>{NO_VALUE}</> : <DateTimeText value={value} />;
}

/**
 * 写操作失败时的提示。docs/api.md 列出的各个 409 有专门文案，带 request_id；其余回落到后端 message 与
 * request_id（403 由 `CustomerErrorAlert` 单独说）。
 */
function PortalErrorAlert({ error, title }: { error: Error | null; title: string }) {
  const { t } = useTranslation();
  if (error === null) {
    return null;
  }
  const code = conflictCode(error);
  if (code === null) {
    return <CustomerErrorAlert error={error} title={title} />;
  }
  let message: string;
  switch (code) {
    case PORTAL_ACCOUNT_EXISTS:
      message = t("portalAccount.error.exists");
      break;
    case EMAIL_IN_USE:
      message = t("portalAccount.error.emailInUse");
      break;
    case ACCOUNT_CLOSED:
      message = t("portalAccount.error.accountClosed");
      break;
    case PORTAL_ACCOUNT_NOT_INVITED:
      message = t("portalAccount.error.notInvited");
      break;
    case PORTAL_ACCOUNT_ALREADY_DISABLED:
      message = t("portalAccount.error.alreadyDisabled");
      break;
    case PORTAL_ACCOUNT_NOT_DISABLED:
      message = t("portalAccount.error.notDisabled");
      break;
    default:
      message = t("portalAccount.error.twoFactorNotEnabled");
      break;
  }
  return (
    <Alert
      type="warning"
      showIcon
      style={{ marginBottom: 16 }}
      message={message}
      description={<RequestReference error={error} />}
    />
  );
}

export function PortalAccountPanel({
  customer,
}: {
  customer: Pick<CustomerSummary, "id" | "email" | "account_status">;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const customerId = customer.id;
  const closed = customer.account_status === "CLOSED";
  const [action, setAction] = useState<Action | null>(null);
  const [done, setDone] = useState<Done | null>(null);
  // 开通的报错放在面板上而不是表单里：409 `PORTAL_ACCOUNT_EXISTS` 之后重读，表单随即换成账号详情，
  // 报错要留下来说清楚为什么。
  const [createError, setCreateError] = useState<Error | null>(null);

  const account = useQuery({
    queryKey: portalAccountQueryKey(customerId),
    queryFn: ({ signal }) => getPortalAccount(customerId, signal),
  });

  const refresh = (): void => {
    void queryClient.invalidateQueries({ queryKey: portalAccountQueryKey(customerId) });
  };

  const refreshOnConflict = (error: Error): void => {
    if (conflictCode(error) !== null) {
      refresh();
    }
  };

  const start = (next: Action): void => {
    setDone(null);
    setCreateError(null);
    setAction(next);
  };

  const finish = (finished: Done): void => {
    setAction(null);
    setDone(finished);
    refresh();
  };

  let doneMessage: string | null = null;
  if (done === "created") {
    doneMessage = t("portalAccount.create.done");
  } else if (done === "resend") {
    doneMessage = t("portalAccount.resend.done");
  } else if (done === "disable") {
    doneMessage = t("portalAccount.disable.done");
  } else if (done === "enable") {
    doneMessage = t("portalAccount.enable.done");
  } else if (done === "reset2fa") {
    doneMessage = t("portalAccount.reset2fa.done");
  }

  let body: ReactNode;
  let extra: ReactNode = null;
  if (account.isPending) {
    body = (
      <>
        <Skeleton active title={false} paragraph={{ rows: 2 }} />
        <Typography.Text type="secondary">{t("portalAccount.loading")}</Typography.Text>
      </>
    );
  } else if (account.isError) {
    body = (
      <CustomerErrorAlert
        error={account.error}
        title={t("portalAccount.loadFailed")}
        onRetry={() => void account.refetch()}
      />
    );
  } else if (account.data === null) {
    body = (
      <>
        <Typography.Paragraph>{t("portalAccount.none")}</Typography.Paragraph>
        {closed ? null : (
          <CreateForm
            customerId={customerId}
            defaultEmail={customer.email}
            onStart={() => {
              setDone(null);
              setCreateError(null);
            }}
            onDone={() => finish("created")}
            onFailed={(error) => {
              setCreateError(error);
              refreshOnConflict(error);
            }}
          />
        )}
      </>
    );
  } else {
    const current = account.data;
    body = <AccountDescriptions account={current} />;
    extra = (
      <Space wrap>
        {current.status === "INVITED" && !closed ? (
          <Button onClick={() => start("resend")}>{t("portalAccount.resend.open")}</Button>
        ) : null}
        {current.status === "INVITED" || current.status === "ACTIVE" ? (
          <Button danger onClick={() => start("disable")}>
            {t("portalAccount.disable.open")}
          </Button>
        ) : null}
        {current.status === "DISABLED" && !closed ? (
          <Button onClick={() => start("enable")}>{t("portalAccount.enable.open")}</Button>
        ) : null}
        {current.two_factor_enabled ? (
          <Button danger onClick={() => start("reset2fa")}>
            {t("portalAccount.reset2fa.open")}
          </Button>
        ) : null}
      </Space>
    );
  }

  return (
    // role + aria-label：部署后验收脚本按语义结构找到这个面板（不依赖 antd 的类名）。
    <Card
      role="region"
      aria-label={t("portalAccount.title")}
      title={t("portalAccount.title")}
      extra={extra}
    >
      {closed ? (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message={t("portalAccount.closedNotice")}
        />
      ) : null}
      {doneMessage === null ? null : (
        <Alert type="success" showIcon style={{ marginBottom: 16 }} message={doneMessage} />
      )}
      <PortalErrorAlert error={createError} title={t("portalAccount.create.failed")} />
      {body}
      {/* 只在打开时挂载、关闭即卸载：每次打开都是空表单、没有上一次的报错。 */}
      {action !== null && account.data ? (
        <ActionModal
          customerId={customerId}
          account={account.data}
          action={action}
          onDone={() => finish(action)}
          onFailed={refreshOnConflict}
          onCancel={() => setAction(null)}
        />
      ) : null}
    </Card>
  );
}

function AccountDescriptions({ account }: { account: PortalAccount }) {
  const { t } = useTranslation();
  const items: DescriptionsProps["items"] = [
    { key: "email", label: t("portalAccount.field.email"), children: account.email },
    {
      key: "status",
      label: t("portalAccount.field.status"),
      children: <PortalStatusTag status={account.status} />,
    },
    {
      key: "invited_at",
      label: t("portalAccount.field.invitedAt"),
      children: <TimeValue value={account.invited_at} />,
    },
    {
      key: "invitation_expires_at",
      label: t("portalAccount.field.invitationExpiresAt"),
      children:
        // INVITED 而没有有效期：最近一次邀请已过期或已作废，需要重发。
        account.invitation_expires_at === null && account.status === "INVITED" ? (
          t("portalAccount.invitationNotPending")
        ) : (
          <TimeValue value={account.invitation_expires_at} />
        ),
    },
    {
      key: "activated_at",
      label: t("portalAccount.field.activatedAt"),
      children: <TimeValue value={account.activated_at} />,
    },
    {
      key: "last_login_at",
      label: t("portalAccount.field.lastLoginAt"),
      children: <TimeValue value={account.last_login_at} />,
    },
    {
      key: "two_factor_enabled",
      label: t("portalAccount.field.twoFactor"),
      children: account.two_factor_enabled
        ? t("portalAccount.twoFactor.on")
        : t("portalAccount.twoFactor.off"),
    },
  ];
  return <Descriptions bordered size="small" column={1} items={items} />;
}

interface EmailFormValues {
  email: string;
}

/** 开通表单。⚠️ 开通**不幂等**：提交进行中禁用，双击也只发一次。 */
function CreateForm({
  customerId,
  defaultEmail,
  onStart,
  onDone,
  onFailed,
}: {
  customerId: string;
  defaultEmail: string;
  onStart: () => void;
  onDone: () => void;
  onFailed: (error: Error) => void;
}) {
  const { t } = useTranslation();
  // `isPending` 要等下一次渲染才变 true：双击时第二次点击可能赶在按钮禁用之前。
  const inFlight = useRef(false);

  const create = useMutation({
    mutationFn: (email: string) => createPortalAccount(customerId, email),
    onSuccess: onDone,
    onError: onFailed,
    onSettled: () => {
      inFlight.current = false;
    },
  });

  const submit = (values: EmailFormValues): void => {
    if (inFlight.current) {
      return;
    }
    inFlight.current = true;
    onStart();
    create.mutate(values.email.trim());
  };

  // 报错由面板显示（见 PortalAccountPanel 的 createError）。
  return (
    <>
      <Form<EmailFormValues>
        layout="vertical"
        initialValues={{ email: defaultEmail }}
        onFinish={submit}
        disabled={create.isPending}
        style={{ maxWidth: 480 }}
      >
        <Form.Item
          name="email"
          label={t("portalAccount.field.email")}
          extra={t("portalAccount.create.emailHint")}
          rules={[
            { required: true, whitespace: true, message: t("portalAccount.emailRequired") },
            { type: "email", message: t("portalAccount.emailInvalid") },
          ]}
        >
          <Input autoComplete="off" inputMode="email" />
        </Form.Item>
        <Button type="primary" htmlType="submit" loading={create.isPending}>
          {t("portalAccount.create.submit")}
        </Button>
      </Form>
    </>
  );
}

interface ActionFormValues {
  email?: string;
  reason?: string;
}

/**
 * 重发、停用、启用、重置 2FA 的确认框。只在打开时挂载、关闭即卸载。确认之前不发任何请求；
 * 请求还在路上时不让关（关了就没人看得到结果）。
 */
function ActionModal({
  customerId,
  account,
  action,
  onDone,
  onFailed,
  onCancel,
}: {
  customerId: string;
  account: PortalAccount;
  action: Action;
  onDone: () => void;
  onFailed: (error: Error) => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const [form] = Form.useForm<ActionFormValues>();
  const inFlight = useRef(false);
  const hasForm = action === "resend" || action === "reset2fa";

  const run = useMutation({
    mutationFn: (values: ActionFormValues): Promise<PortalAccount> => {
      if (action === "resend") {
        const email = (values.email ?? "").trim();
        // 归一后与当前相同就是不改：请求体发 `{}`。
        return email.toLowerCase() === account.email
          ? resendInvitation(customerId)
          : resendInvitation(customerId, email);
      }
      if (action === "disable") {
        return disablePortalAccount(customerId);
      }
      if (action === "enable") {
        return enablePortalAccount(customerId);
      }
      return resetPortalTwoFactor(customerId, (values.reason ?? "").trim());
    },
    onSuccess: onDone,
    onError: onFailed,
    onSettled: () => {
      inFlight.current = false;
    },
  });

  const pending = run.isPending;

  const send = (values: ActionFormValues): void => {
    if (inFlight.current) {
      return;
    }
    inFlight.current = true;
    run.mutate(values);
  };

  const close = (): void => {
    if (!pending) {
      onCancel();
    }
  };

  let title: string;
  let confirmLabel: string;
  let failedTitle: string;
  let content: ReactNode;
  if (action === "resend") {
    title = t("portalAccount.resend.open");
    confirmLabel = t("portalAccount.resend.confirm");
    failedTitle = t("portalAccount.resend.failed");
    content = (
      <>
        <Typography.Paragraph>{t("portalAccount.resend.body")}</Typography.Paragraph>
        <Form.Item
          name="email"
          label={t("portalAccount.field.email")}
          extra={t("portalAccount.resend.emailHint")}
          rules={[
            { required: true, whitespace: true, message: t("portalAccount.emailRequired") },
            { type: "email", message: t("portalAccount.emailInvalid") },
          ]}
        >
          <Input autoComplete="off" inputMode="email" />
        </Form.Item>
      </>
    );
  } else if (action === "disable") {
    title = t("portalAccount.disable.open");
    confirmLabel = t("portalAccount.disable.confirm");
    failedTitle = t("portalAccount.disable.failed");
    content = <Alert type="warning" showIcon message={t("portalAccount.disable.body")} />;
  } else if (action === "enable") {
    title = t("portalAccount.enable.open");
    confirmLabel = t("portalAccount.enable.confirm");
    failedTitle = t("portalAccount.enable.failed");
    content = <Typography.Paragraph>{t("portalAccount.enable.body")}</Typography.Paragraph>;
  } else {
    title = t("portalAccount.reset2fa.open");
    confirmLabel = t("portalAccount.reset2fa.confirm");
    failedTitle = t("portalAccount.reset2fa.failed");
    content = (
      <>
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 16 }}
          message={t("portalAccount.reset2fa.verifyFirst")}
          description={t("portalAccount.reset2fa.body")}
        />
        <Form.Item
          name="reason"
          label={t("portalAccount.reset2fa.reason")}
          extra={t("portalAccount.reset2fa.reasonHint")}
          rules={[
            { required: true, whitespace: true, message: t("portalAccount.reset2fa.reasonRequired") },
            maxTrimmed(REASON_MAX, t("portalAccount.reset2fa.reasonTooLong")),
          ]}
        >
          <Input.TextArea rows={3} />
        </Form.Item>
      </>
    );
  }

  return (
    <Modal
      open
      title={title}
      width={560}
      footer={[
        <Button key="cancel" onClick={close} disabled={pending}>
          {t("portalAccount.cancel")}
        </Button>,
        <Button
          key="confirm"
          type="primary"
          danger={action === "disable" || action === "reset2fa"}
          onClick={() => {
            if (hasForm) {
              form.submit();
            } else {
              send({});
            }
          }}
          loading={pending}
          disabled={pending}
        >
          {confirmLabel}
        </Button>,
      ]}
      onCancel={close}
      maskClosable={false}
      closable={!pending}
      keyboard={!pending}
    >
      <Typography.Paragraph strong>{account.email}</Typography.Paragraph>
      <PortalErrorAlert error={run.error} title={failedTitle} />
      {hasForm ? (
        <Form<ActionFormValues>
          form={form}
          layout="vertical"
          initialValues={action === "resend" ? { email: account.email } : { reason: "" }}
          onFinish={send}
          disabled={pending}
        >
          {content}
        </Form>
      ) : (
        content
      )}
    </Modal>
  );
}
