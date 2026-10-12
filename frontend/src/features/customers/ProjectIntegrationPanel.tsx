/**
 * 一个项目的集成状态（`ENABLED` / `DISABLED`）与状态 webhook 地址：显示、启停、设置与清除地址。
 *
 * 语义以 docs/api.md「管理端项目集成状态与状态 webhook 地址」与 docs/design/PHASE-3-status-sync.md 为准。
 * 挂在 `ProjectsPanel` 每一行的展开区里，与集成凭据、签名密钥两个面板并列，各管各的查询与状态。
 *
 * - 集成状态与「是否已配置地址」来自项目对象（项目列表的那一行）；完整地址只有 `…/status-webhook`
 *   的 GET 才给，面板挂载（即这一行展开）时才去读。
 * - 停用与启用都要二次确认并填原因（去首尾空白后 1–255，只进审计）。停用的项目**不推送**状态 webhook，
 *   连使它停用的那一次也不推，集成方经周期对账得知 —— 确认框写明。
 * - 地址前端只提示 `https://` 前缀，规范化与校验以后端为准：422 显示在输入框下面。清除也要二次确认。
 * - 三个写接口都幂等（目标与当前相同时 200、不写）。成功后只让项目列表与地址查询过期重读，不在前端
 *   拼状态；凭据与签名密钥的查询不受影响。
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
  Space,
  Tag,
  Typography,
  type DescriptionsProps,
} from "antd";
import { useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import type { IntegrationStatus, Project } from "../../api/adminCustomers";
import { ApiError } from "../../api/client";
import {
  ACCOUNT_CLOSED,
  VALIDATION_ERROR,
  getStatusWebhook,
  isProjectListQueryKey,
  setIntegrationStatus,
  setStatusWebhook,
  statusWebhookQueryKey,
} from "../../api/projectIntegration";
import { RequestReference } from "../../components/RequestReference";
import { CustomerErrorAlert } from "./CustomerForm";

/** 与后端请求模型的上限一致（去掉首尾空白之后）。 */
const REASON_MAX = 255;

/** 读不到地址时显示一道横线，与详情页的空字段一致。 */
const NO_VALUE = "—";

/** 只是提示：后端的规范化不分大小写地认 scheme，最终以它的 422 为准。 */
const HTTPS_PREFIX = /^https:\/\//i;

type Done = "disabled" | "enabled" | "urlSet" | "urlCleared";

function hasCode(error: unknown, code: string): boolean {
  return error instanceof ApiError && error.code === code;
}

/** 按码点数，与 Python 的 `len()` 一致（`.length` 数的是 UTF-16 单元，emoji 算 2）。 */
function trimmedLength(value: unknown): number {
  return typeof value === "string" ? [...value.trim()].length : 0;
}

/** 原因的校验，与后端一致：去首尾空白后 1–255 个码点。合格时为 `null`。 */
export function reasonProblem(value: unknown): "required" | "tooLong" | null {
  const length = trimmedLength(value);
  if (length === 0) {
    return "required";
  }
  return length > REASON_MAX ? "tooLong" : null;
}

function StatusTag({ status }: { status: IntegrationStatus | undefined }) {
  const { t } = useTranslation();
  if (status === "ENABLED") {
    return <Tag color="green">{t("projectIntegration.status.enabled")}</Tag>;
  }
  if (status === "DISABLED") {
    return <Tag color="red">{t("projectIntegration.status.disabled")}</Tag>;
  }
  return <>{t("projectIntegration.unknown")}</>;
}

function ConfiguredText({ configured }: { configured: boolean | undefined }) {
  const { t } = useTranslation();
  if (configured === undefined) {
    return <>{t("projectIntegration.unknown")}</>;
  }
  return (
    <>
      {configured
        ? t("projectIntegration.webhook.configured")
        : t("projectIntegration.webhook.notConfigured")}
    </>
  );
}

/**
 * 写操作失败时的提示。`ACCOUNT_CLOSED` 显示专门的文案与 request_id；其余回落到后端 message 与
 * request_id（403 由 `CustomerErrorAlert` 单独说）。
 */
function IntegrationErrorAlert({
  error,
  title,
  closedMessage,
}: {
  error: Error | null;
  title: string;
  closedMessage: string;
}) {
  if (error === null) {
    return null;
  }
  if (!hasCode(error, ACCOUNT_CLOSED)) {
    return <CustomerErrorAlert error={error} title={title} />;
  }
  return (
    <Alert
      type="warning"
      showIcon
      style={{ marginBottom: 16 }}
      message={closedMessage}
      description={<RequestReference error={error} />}
    />
  );
}

interface UrlFormValues {
  url: string;
}

export function ProjectIntegrationPanel({
  customerId,
  project,
}: {
  customerId: string;
  project: Project;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const projectId = project.id;
  const status = project.integration_status;
  const [changing, setChanging] = useState<IntegrationStatus | null>(null);
  const [confirmingClear, setConfirmingClear] = useState(false);
  const [done, setDone] = useState<Done | null>(null);
  const [form] = Form.useForm<UrlFormValues>();
  // 与建项目同一个理由：`isPending` 要等下一次渲染，双击时第二次提交可能赶在按钮禁用之前。
  const inFlight = useRef(false);

  const webhook = useQuery({
    queryKey: statusWebhookQueryKey(customerId, projectId),
    queryFn: ({ signal }) => getStatusWebhook(customerId, projectId, signal),
  });

  /** 只让这个客户的项目列表页与这个项目的地址过期；凭据、签名密钥不动。 */
  const refresh = (): void => {
    void queryClient.invalidateQueries({
      predicate: (query) => isProjectListQueryKey(customerId, query.queryKey),
    });
    void queryClient.invalidateQueries({
      queryKey: statusWebhookQueryKey(customerId, projectId),
      exact: true,
    });
  };

  const save = useMutation({
    mutationFn: (url: string) => setStatusWebhook(customerId, projectId, url),
    onSuccess: () => {
      form.resetFields();
      setDone("urlSet");
      refresh();
    },
    onSettled: () => {
      inFlight.current = false;
    },
  });

  const clearOutcomes = (): void => {
    save.reset();
    setDone(null);
  };

  const submitUrl = (values: UrlFormValues): void => {
    if (inFlight.current) {
      return;
    }
    inFlight.current = true;
    setDone(null);
    // 原样发出：规范化（大小写、末尾的点、IDNA）只在后端做，前端不改写地址。
    save.mutate(values.url);
  };

  const startChange = (target: IntegrationStatus): void => {
    clearOutcomes();
    setChanging(target);
  };

  const startClear = (): void => {
    clearOutcomes();
    setConfirmingClear(true);
  };

  const busy = save.isPending;
  const urlRejected = hasCode(save.error, VALIDATION_ERROR) ? save.error : null;
  const configured = webhook.data?.status_webhook_configured ?? project.status_webhook_configured;

  let address: ReactNode;
  if (webhook.isPending) {
    address = (
      <Typography.Text type="secondary">{t("projectIntegration.webhook.loading")}</Typography.Text>
    );
  } else if (webhook.isError) {
    // 失败的提示（带 request_id）画在上面。
    address = NO_VALUE;
  } else if (webhook.data.status_webhook_url === null) {
    address = t("projectIntegration.webhook.notSet");
  } else {
    address = (
      <Typography.Text code copyable style={{ wordBreak: "break-all" }}>
        {webhook.data.status_webhook_url}
      </Typography.Text>
    );
  }

  const items: DescriptionsProps["items"] = [
    {
      key: "integration_status",
      label: t("projectIntegration.field.status"),
      children: <StatusTag status={status} />,
    },
    {
      key: "status_webhook_configured",
      label: t("projectIntegration.field.webhookConfigured"),
      children: <ConfiguredText configured={project.status_webhook_configured} />,
    },
    {
      key: "status_webhook_url",
      label: t("projectIntegration.field.webhookUrl"),
      children: address,
    },
  ];

  // 状态未知时不给启停按钮：不知道当前是什么，就说不清这一下是停用还是启用。
  let toggle: ReactNode = null;
  if (status === "ENABLED") {
    toggle = (
      <Button danger disabled={busy} onClick={() => startChange("DISABLED")}>
        {t("projectIntegration.disable.open")}
      </Button>
    );
  } else if (status === "DISABLED") {
    toggle = (
      <Button disabled={busy} onClick={() => startChange("ENABLED")}>
        {t("projectIntegration.enable.open")}
      </Button>
    );
  }

  let doneMessage: string | null = null;
  if (done === "disabled") {
    doneMessage = t("projectIntegration.disable.done");
  } else if (done === "enabled") {
    doneMessage = t("projectIntegration.enable.done");
  } else if (done === "urlSet") {
    doneMessage = t("projectIntegration.webhook.saved");
  } else if (done === "urlCleared") {
    doneMessage = t("projectIntegration.webhook.cleared");
  }

  return (
    // role + aria-label：部署后验收脚本按语义结构找到这个面板（不依赖 antd 的类名）。
    <Card
      size="small"
      role="region"
      aria-label={t("projectIntegration.title")}
      title={t("projectIntegration.title")}
      extra={toggle}
    >
      {doneMessage === null ? null : (
        <Alert type="success" showIcon style={{ marginBottom: 16 }} message={doneMessage} />
      )}
      <CustomerErrorAlert
        error={webhook.error}
        title={t("projectIntegration.webhook.loadFailed")}
        onRetry={() => void webhook.refetch()}
      />
      <Descriptions bordered size="small" column={1} items={items} style={{ marginBottom: 16 }} />
      {urlRejected === null ? (
        <IntegrationErrorAlert
          error={save.error}
          title={t("projectIntegration.webhook.saveFailed")}
          closedMessage={t("projectIntegration.error.closedWebhook")}
        />
      ) : null}
      <Form<UrlFormValues>
        form={form}
        layout="vertical"
        initialValues={{ url: "" }}
        onFinish={submitUrl}
        onValuesChange={() => {
          // 改了地址，上一次的拒绝就不再说的是这个值。
          if (save.error !== null) {
            save.reset();
          }
        }}
        disabled={busy}
      >
        <Form.Item
          name="url"
          label={t("projectIntegration.webhook.newUrl")}
          extra={t("projectIntegration.webhook.urlHint")}
          rules={[
            {
              required: true,
              whitespace: true,
              message: t("projectIntegration.webhook.urlRequired"),
            },
            {
              // 只提示、不拦：最终以后端的 422 为准。
              warningOnly: true,
              validator: (_rule, value: unknown) =>
                typeof value === "string" && value.trim() !== "" && !HTTPS_PREFIX.test(value)
                  ? Promise.reject(new Error(t("projectIntegration.webhook.httpsHint")))
                  : Promise.resolve(),
            },
          ]}
          {...(urlRejected === null
            ? {}
            : { validateStatus: "error" as const, help: <RequestReference error={urlRejected} /> })}
        >
          <Input autoComplete="off" />
        </Form.Item>
        <Space>
          <Button type="primary" htmlType="submit" loading={busy} disabled={busy}>
            {t("projectIntegration.webhook.save")}
          </Button>
          <Button danger disabled={busy || configured !== true} onClick={startClear}>
            {t("projectIntegration.webhook.clear.open")}
          </Button>
        </Space>
      </Form>
      {changing === null ? null : (
        <StatusModal
          customerId={customerId}
          projectId={projectId}
          target={changing}
          onDone={() => {
            setChanging(null);
            setDone(changing === "DISABLED" ? "disabled" : "enabled");
            refresh();
          }}
          onCancel={() => setChanging(null)}
        />
      )}
      {confirmingClear ? (
        <ClearModal
          customerId={customerId}
          projectId={projectId}
          onDone={() => {
            setConfirmingClear(false);
            setDone("urlCleared");
            refresh();
          }}
          onCancel={() => setConfirmingClear(false)}
        />
      ) : null}
    </Card>
  );
}

interface ReasonFormValues {
  reason: string;
}

/**
 * 停用或启用的二次确认。只在打开时挂载、关闭即卸载。原因必填、去首尾空白后 1–255，只写业务说明。
 * 失败时对话框留着、原因保留；接口幂等，原样再点一次是安全的。
 */
function StatusModal({
  customerId,
  projectId,
  target,
  onDone,
  onCancel,
}: {
  customerId: string;
  projectId: string;
  target: IntegrationStatus;
  onDone: () => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const [form] = Form.useForm<ReasonFormValues>();
  const inFlight = useRef(false);
  const disabling = target === "DISABLED";

  const change = useMutation({
    // 面板只靠重读项目列表，不用返回值。
    mutationFn: async (reason: string): Promise<void> => {
      await setIntegrationStatus(customerId, projectId, target, reason);
    },
    onSuccess: onDone,
    onSettled: () => {
      inFlight.current = false;
    },
  });

  const submit = (values: ReasonFormValues): void => {
    if (inFlight.current) {
      return;
    }
    inFlight.current = true;
    change.mutate(values.reason.trim());
  };

  const pending = change.isPending;
  const close = (): void => {
    if (!pending) {
      onCancel();
    }
  };

  return (
    <Modal
      open
      title={
        disabling
          ? t("projectIntegration.disable.confirmTitle")
          : t("projectIntegration.enable.confirmTitle")
      }
      footer={[
        <Button key="cancel" onClick={close} disabled={pending}>
          {t("projectIntegration.cancel")}
        </Button>,
        <Button
          key="confirm"
          type="primary"
          danger={disabling}
          onClick={() => form.submit()}
          loading={pending}
          disabled={pending}
        >
          {disabling ? t("projectIntegration.disable.confirm") : t("projectIntegration.enable.confirm")}
        </Button>,
      ]}
      onCancel={close}
      maskClosable={false}
      closable={!pending}
      keyboard={!pending}
    >
      <Alert
        type="warning"
        showIcon
        style={{ marginBottom: 16 }}
        message={
          disabling ? t("projectIntegration.disable.warning") : t("projectIntegration.enable.warning")
        }
      />
      <IntegrationErrorAlert
        error={change.error}
        title={t("projectIntegration.statusFailed")}
        closedMessage={t("projectIntegration.error.closedStatus")}
      />
      <Form<ReasonFormValues>
        form={form}
        layout="vertical"
        initialValues={{ reason: "" }}
        onFinish={submit}
        disabled={pending}
      >
        <Form.Item
          name="reason"
          label={t("projectIntegration.reason")}
          extra={t("projectIntegration.reasonHint")}
          rules={[
            {
              // required 只为星号；有 validator 时由它一并判空。
              required: true,
              validator: (_rule, value: unknown) => {
                const problem = reasonProblem(value);
                if (problem === null) {
                  return Promise.resolve();
                }
                return Promise.reject(
                  new Error(
                    problem === "required"
                      ? t("projectIntegration.reasonRequired")
                      : t("projectIntegration.reasonTooLong"),
                  ),
                );
              },
            },
          ]}
        >
          <Input.TextArea rows={3} />
        </Form.Item>
      </Form>
    </Modal>
  );
}

/** 清除地址的二次确认。只在打开时挂载、关闭即卸载。清除不受关户限制；幂等。 */
function ClearModal({
  customerId,
  projectId,
  onDone,
  onCancel,
}: {
  customerId: string;
  projectId: string;
  onDone: () => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const inFlight = useRef(false);

  const clear = useMutation({
    mutationFn: async (): Promise<void> => {
      await setStatusWebhook(customerId, projectId, null);
    },
    onSuccess: onDone,
    onSettled: () => {
      inFlight.current = false;
    },
  });

  const confirm = (): void => {
    if (inFlight.current) {
      return;
    }
    inFlight.current = true;
    clear.mutate();
  };

  const pending = clear.isPending;
  const close = (): void => {
    if (!pending) {
      onCancel();
    }
  };

  return (
    <Modal
      open
      title={t("projectIntegration.webhook.clear.confirmTitle")}
      okText={t("projectIntegration.webhook.clear.confirm")}
      cancelText={t("projectIntegration.cancel")}
      onOk={confirm}
      onCancel={close}
      okButtonProps={{ danger: true, loading: pending, disabled: pending }}
      cancelButtonProps={{ disabled: pending }}
      maskClosable={false}
      closable={!pending}
      keyboard={!pending}
    >
      <IntegrationErrorAlert
        error={clear.error}
        title={t("projectIntegration.webhook.clear.failed")}
        closedMessage={t("projectIntegration.error.closedWebhook")}
      />
      <Typography.Paragraph>{t("projectIntegration.webhook.clear.warning")}</Typography.Paragraph>
    </Modal>
  );
}
