/**
 * 一个项目的出站 webhook 签名密钥：签发、列、启用一个版本、退役一个版本。
 *
 * 语义以 docs/api.md「管理端出站 webhook 签名密钥」与 docs/design/AIH-TASK-019-webhook-signing.md 为准。
 * 挂在 `ProjectsPanel` 每一行的展开区里，在 `IntegrationAccessPanel` 下面。
 *
 * 列表分页，顺序由后端决定（`key_version` 从小到大），前端不重排。每个项目至多一个 `ACTIVE`、
 * 至多一个 `PENDING`；启用一个 `PENDING` 时原来的 `ACTIVE` 在同一事务里退役（原子替换）。
 *
 * ⚠️ `secret` 只显示一次 —— 规则与 `IntegrationAccessPanel` 相同，改之前先读完：
 *
 *   1. `secret` 只来自签发的 **mutation 返回值**，只画在 {@link SecretDialog} 里。
 *   2. **不进查询缓存**：成功后不 `setQueryData`，只让列表查询过期重读（列表响应里本来就没有
 *      `secret`）。mutation 的 `gcTime` 是 0，对话框一关就 `reset()`，mutation 缓存里的那份
 *      随即被丢掉。
 *   3. 不写 localStorage / sessionStorage、不进 console、不进 URL、不进组件之外的任何状态。
 *   4. 对话框关闭就卸载，`secret` 从 DOM 里消失，之后再也看不到。
 *
 * 签发结果未知（网络错误、超时、5xx）时**不自动重试**：新版本若已入库就是一个拿不到 `secret` 的
 * `PENDING`，按 docs/api.md 的恢复方式提示管理员：看列表，退役那个 `PENDING` 再签发。
 */

import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Empty,
  Form,
  Input,
  Modal,
  Skeleton,
  Space,
  Table,
  Tag,
  Typography,
  type DescriptionsProps,
  type FormRule,
  type TableColumnsType,
} from "antd";
import { useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, type Project } from "../../api/adminCustomers";
import { ApiError } from "../../api/client";
import {
  ENCRYPTION_NOT_CONFIGURED,
  WEBHOOK_SECRET_NOT_PENDING,
  WEBHOOK_SECRET_PENDING_EXISTS,
  activateWebhookSecret,
  isOutcomeUnknown,
  issueWebhookSecret,
  listWebhookSecrets,
  projectWebhookSecretsQueryKey,
  retireWebhookSecret,
  webhookSecretListQueryKey,
  type IssuedWebhookSecret,
  type WebhookSecret,
  type WebhookSecretStatus,
} from "../../api/webhookSigning";
import { DateTimeText } from "../../components/DateTimeText";
import { RequestReference } from "../../components/RequestReference";
import { CustomerErrorAlert } from "./CustomerForm";

/** 与后端 `app/schemas/webhook_signing.py` 的上限一致（去掉首尾空白之后）。 */
const REASON_MAX = 255;

const PAGE_SIZE_OPTIONS = [10, 20, 50, MAX_PAGE_SIZE];

/** 空的时间显示成一道横线，与详情页的空字段一致。 */
const NO_VALUE = "—";

/** 有专门文案的错误码：显示文案与 request_id，并重读列表（列表上的状态多半已经过时）。 */
const KNOWN_CODES: ReadonlySet<string> = new Set([
  WEBHOOK_SECRET_PENDING_EXISTS,
  WEBHOOK_SECRET_NOT_PENDING,
  ENCRYPTION_NOT_CONFIGURED,
]);

function knownCode(error: unknown): string | null {
  return error instanceof ApiError && KNOWN_CODES.has(error.code) ? error.code : null;
}

/** 失败之后要不要重读列表：有专门文案的码，或者不知道写没写上。 */
function shouldRefresh(error: unknown): boolean {
  return knownCode(error) !== null || isOutcomeUnknown(error);
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

function StatusTag({ status }: { status: WebhookSecretStatus }) {
  const { t } = useTranslation();
  if (status === "ACTIVE") {
    return <Tag color="green">{t("webhookSigning.status.active")}</Tag>;
  }
  if (status === "PENDING") {
    return <Tag color="gold">{t("webhookSigning.status.pending")}</Tag>;
  }
  return <Tag>{t("webhookSigning.status.retired")}</Tag>;
}

function TimeCell({ value }: { value: string | null }) {
  return value === null ? <>{NO_VALUE}</> : <DateTimeText value={value} />;
}

/**
 * 写操作失败时的提示。三个有专门文案的码显示文案、request_id 与「列表已刷新」；
 * 其余回落到后端 message 与 request_id（403 由 `CustomerErrorAlert` 单独说）。
 */
function WebhookErrorAlert({ error, title }: { error: Error | null; title: string }) {
  const { t } = useTranslation();
  const code = knownCode(error);
  if (error === null) {
    return null;
  }
  if (code === null) {
    return <CustomerErrorAlert error={error} title={title} />;
  }
  let message: string;
  if (code === WEBHOOK_SECRET_PENDING_EXISTS) {
    message = t("webhookSigning.error.pendingExists");
  } else if (code === WEBHOOK_SECRET_NOT_PENDING) {
    message = t("webhookSigning.error.notPending");
  } else {
    message = t("webhookSigning.error.encryptionNotConfigured");
  }
  return (
    <Alert
      type="warning"
      showIcon
      style={{ marginBottom: 16 }}
      message={message}
      description={
        <>
          <Typography.Paragraph>{t("webhookSigning.error.listRefreshed")}</Typography.Paragraph>
          <RequestReference error={error} />
        </>
      }
    />
  );
}

interface ActivateTarget {
  keyVersion: number;
  /** 当前这一页上的 `ACTIVE` 版本；不在这一页上时是 `null`，确认框改用不点名的说法。 */
  activeVersion: number | null;
}

export function WebhookSigningPanel({
  customerId,
  project,
}: {
  customerId: string;
  project: Project;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const projectId = project.id;
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);
  const [confirmingIssue, setConfirmingIssue] = useState(false);
  const [activating, setActivating] = useState<ActivateTarget | null>(null);
  const [retiring, setRetiring] = useState<WebhookSecret | null>(null);
  const [done, setDone] = useState<"activated" | "retired" | null>(null);
  // 与建项目同一个理由：`isPending` 要等下一次渲染，双击时第二次点击可能赶在按钮禁用之前。
  // 签发**不幂等**。
  const inFlight = useRef(false);

  const secrets = useQuery({
    queryKey: webhookSecretListQueryKey(customerId, projectId, page, pageSize),
    queryFn: ({ signal }) => listWebhookSecrets(customerId, projectId, page, pageSize, signal),
    placeholderData: keepPreviousData,
  });

  const refreshList = (): void => {
    void queryClient.invalidateQueries({
      queryKey: projectWebhookSecretsQueryKey(customerId, projectId),
    });
  };

  // 规则 2：`gcTime: 0` —— 对话框关闭、`reset()` 之后，mutation 缓存里的那份 `secret` 立即被丢掉。
  const issue = useMutation({
    mutationFn: () => issueWebhookSecret(customerId, projectId),
    gcTime: 0,
    onSuccess: refreshList,
    onError: (error) => {
      // 结果未知时新的 PENDING 可能已经入库：重读列表让管理员看得到它。
      if (shouldRefresh(error)) {
        refreshList();
      }
    },
    onSettled: () => {
      inFlight.current = false;
      setConfirmingIssue(false);
    },
  });

  const activate = useMutation({
    mutationFn: (keyVersion: number) => activateWebhookSecret(customerId, projectId, keyVersion),
    onSuccess: () => {
      setDone("activated");
      refreshList();
    },
    onError: (error) => {
      if (shouldRefresh(error)) {
        refreshList();
      }
    },
    onSettled: () => {
      inFlight.current = false;
      setActivating(null);
    },
  });

  /** 开始新的操作之前，把上一次的结果与报错都清掉。 */
  const clearOutcomes = (): void => {
    issue.reset();
    activate.reset();
    setDone(null);
  };

  const startIssue = (): void => {
    clearOutcomes();
    setConfirmingIssue(true);
  };

  const confirmIssue = (): void => {
    if (inFlight.current) {
      return;
    }
    inFlight.current = true;
    issue.mutate();
  };

  const startActivate = (version: WebhookSecret): void => {
    clearOutcomes();
    const active = secrets.data?.items.find((item) => item.status === "ACTIVE");
    setActivating({
      keyVersion: version.key_version,
      activeVersion: active === undefined ? null : active.key_version,
    });
  };

  const confirmActivate = (): void => {
    if (inFlight.current || activating === null) {
      return;
    }
    inFlight.current = true;
    activate.mutate(activating.keyVersion);
  };

  const startRetire = (version: WebhookSecret): void => {
    clearOutcomes();
    setRetiring(version);
  };

  const finishRetire = (): void => {
    setRetiring(null);
    setDone("retired");
    refreshList();
  };

  /** 规则 4：关闭即丢弃。mutation reset，`secret` 不留在任何地方。 */
  const discardSecret = (): void => {
    issue.reset();
  };

  const goTo = (nextPage: number, nextPageSize: number): void => {
    setPage(nextPageSize === pageSize ? nextPage : 1);
    setPageSize(nextPageSize);
  };

  const issued: IssuedWebhookSecret | undefined = issue.data;
  const busy = issue.isPending || activate.isPending;

  const columns: TableColumnsType<WebhookSecret> = [
    {
      key: "key_version",
      title: t("webhookSigning.field.keyVersion"),
      dataIndex: "key_version",
    },
    {
      key: "status",
      title: t("webhookSigning.field.status"),
      render: (_: unknown, version) => <StatusTag status={version.status} />,
    },
    {
      key: "created_at",
      title: t("webhookSigning.field.createdAt"),
      render: (_: unknown, version) => <DateTimeText value={version.created_at} />,
    },
    {
      key: "activated_at",
      title: t("webhookSigning.field.activatedAt"),
      render: (_: unknown, version) => <TimeCell value={version.activated_at} />,
    },
    {
      key: "retired_at",
      title: t("webhookSigning.field.retiredAt"),
      render: (_: unknown, version) => <TimeCell value={version.retired_at} />,
    },
    {
      key: "actions",
      title: t("webhookSigning.field.actions"),
      render: (_: unknown, version) => (
        <Space>
          <Button
            size="small"
            disabled={version.status !== "PENDING" || busy}
            onClick={() => startActivate(version)}
          >
            {t("webhookSigning.activate.open", { version: version.key_version })}
          </Button>
          <Button
            size="small"
            danger
            disabled={version.status === "RETIRED" || busy}
            onClick={() => startRetire(version)}
          >
            {t("webhookSigning.retire.open", { version: version.key_version })}
          </Button>
        </Space>
      ),
    },
  ];

  let failure: ReactNode = null;
  if (issue.error !== null) {
    if (knownCode(issue.error) === null && isOutcomeUnknown(issue.error)) {
      failure = (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          message={t("webhookSigning.issue.unknownTitle")}
          description={
            <>
              <Typography.Paragraph>{t("webhookSigning.issue.unknownBody")}</Typography.Paragraph>
              <RequestReference error={issue.error} />
            </>
          }
        />
      );
    } else {
      failure = <WebhookErrorAlert error={issue.error} title={t("webhookSigning.issue.failed")} />;
    }
  } else if (activate.error !== null) {
    failure = (
      <WebhookErrorAlert error={activate.error} title={t("webhookSigning.activate.failed")} />
    );
  }

  const doneNotice =
    done === null ? null : (
      <Alert
        type="success"
        showIcon
        style={{ marginBottom: 16 }}
        message={
          done === "activated"
            ? t("webhookSigning.activate.done")
            : t("webhookSigning.retire.done")
        }
      />
    );

  let body: ReactNode;
  if (secrets.isPending) {
    body = (
      <>
        <Skeleton active title={false} paragraph={{ rows: 2 }} />
        <Typography.Text type="secondary">{t("webhookSigning.loading")}</Typography.Text>
      </>
    );
  } else if (secrets.isError) {
    body = (
      <CustomerErrorAlert
        error={secrets.error}
        title={t("webhookSigning.loadFailed")}
        onRetry={() => void secrets.refetch()}
      />
    );
  } else if (secrets.data.total === 0) {
    body = <Empty description={t("webhookSigning.empty")} />;
  } else {
    body = (
      <Table<WebhookSecret>
        size="small"
        rowKey="key_version"
        columns={columns}
        dataSource={secrets.data.items}
        loading={secrets.isFetching}
        // 页码超过末页时后端回空 items、total 照报：说清楚是「这一页没有」。
        locale={{ emptyText: t("webhookSigning.emptyPage") }}
        pagination={{
          current: page,
          pageSize,
          total: secrets.data.total,
          showSizeChanger: true,
          pageSizeOptions: PAGE_SIZE_OPTIONS,
          showTotal: (count) => t("webhookSigning.total", { total: count }),
          onChange: goTo,
        }}
      />
    );
  }

  return (
    // role + aria-label：部署后验收脚本按语义结构找到这个面板（不依赖 antd 的类名）。
    <Card
      size="small"
      role="region"
      aria-label={t("webhookSigning.title")}
      title={t("webhookSigning.title")}
      extra={
        // ⚠️ 签发**不幂等**：提交进行中必须禁用。
        <Button onClick={startIssue} loading={issue.isPending} disabled={busy}>
          {t("webhookSigning.issue.open")}
        </Button>
      }
    >
      {failure}
      {doneNotice}
      {body}
      {confirmingIssue ? (
        <Modal
          open
          title={t("webhookSigning.issue.confirmTitle")}
          okText={t("webhookSigning.issue.confirm")}
          cancelText={t("webhookSigning.cancel")}
          onOk={confirmIssue}
          onCancel={() => {
            if (!issue.isPending) {
              setConfirmingIssue(false);
            }
          }}
          okButtonProps={{ loading: issue.isPending, disabled: issue.isPending }}
          cancelButtonProps={{ disabled: issue.isPending }}
          maskClosable={false}
          closable={!issue.isPending}
          keyboard={!issue.isPending}
        >
          <Typography.Paragraph>{t("webhookSigning.issue.confirmBody")}</Typography.Paragraph>
        </Modal>
      ) : null}
      {activating === null ? null : (
        <Modal
          open
          title={t("webhookSigning.activate.confirmTitle", { version: activating.keyVersion })}
          okText={t("webhookSigning.activate.confirm")}
          cancelText={t("webhookSigning.cancel")}
          onOk={confirmActivate}
          onCancel={() => {
            if (!activate.isPending) {
              setActivating(null);
            }
          }}
          okButtonProps={{ loading: activate.isPending, disabled: activate.isPending }}
          cancelButtonProps={{ disabled: activate.isPending }}
          maskClosable={false}
          closable={!activate.isPending}
          keyboard={!activate.isPending}
        >
          <Typography.Paragraph>
            {activating.activeVersion === null
              ? t("webhookSigning.activate.retiresAnyActive")
              : t("webhookSigning.activate.retiresActive", {
                  active: activating.activeVersion,
                  version: activating.keyVersion,
                })}
          </Typography.Paragraph>
          <Alert
            type="warning"
            showIcon
            message={t("webhookSigning.activate.integrationReady")}
          />
        </Modal>
      )}
      {retiring === null ? null : (
        <RetireModal
          customerId={customerId}
          projectId={projectId}
          target={retiring}
          onDone={finishRetire}
          onFailed={(error) => {
            if (shouldRefresh(error)) {
              refreshList();
            }
          }}
          onCancel={() => setRetiring(null)}
        />
      )}
      {issued === undefined ? null : <SecretDialog issued={issued} onClose={discardSecret} />}
    </Card>
  );
}

/**
 * 签发的结果：`secret` 唯一一次出现的地方。
 *
 * 只在有结果时挂载，关闭即卸载。点遮罩、按 Esc 都不关：一不小心关掉就再也看不到了。
 */
function SecretDialog({ issued, onClose }: { issued: IssuedWebhookSecret; onClose: () => void }) {
  const { t } = useTranslation();
  const [copy, setCopy] = useState<"idle" | "copied" | "failed">("idle");

  const copySecret = (): void => {
    // `navigator.clipboard` 在非安全上下文里不存在：放进 then 里，缺了也只是走失败分支。
    void Promise.resolve()
      .then(() => navigator.clipboard.writeText(issued.secret))
      .then(
        () => setCopy("copied"),
        () => setCopy("failed"),
      );
  };

  const items: DescriptionsProps["items"] = [
    {
      key: "key_version",
      label: t("webhookSigning.field.keyVersion"),
      children: issued.key_version,
    },
    {
      key: "status",
      label: t("webhookSigning.field.status"),
      children: <StatusTag status={issued.status} />,
    },
    {
      key: "secret",
      label: t("webhookSigning.field.secret"),
      children: (
        <Space direction="vertical">
          <Typography.Text code style={{ wordBreak: "break-all" }}>
            {issued.secret}
          </Typography.Text>
          <Space>
            <Button onClick={copySecret}>{t("webhookSigning.secret.copy")}</Button>
            {copy === "copied" ? (
              <Typography.Text type="success">{t("webhookSigning.secret.copied")}</Typography.Text>
            ) : null}
            {copy === "failed" ? (
              <Typography.Text type="danger">
                {t("webhookSigning.secret.copyFailed")}
              </Typography.Text>
            ) : null}
          </Space>
        </Space>
      ),
    },
  ];

  return (
    <Modal
      open
      title={t("webhookSigning.secret.title")}
      width={720}
      footer={[
        <Button key="close" type="primary" onClick={onClose}>
          {t("webhookSigning.secret.close")}
        </Button>,
      ]}
      onCancel={onClose}
      maskClosable={false}
      closable={false}
      keyboard={false}
    >
      <Alert
        type="warning"
        showIcon
        style={{ marginBottom: 16 }}
        message={t("webhookSigning.secret.warning")}
      />
      <Descriptions bordered size="small" column={1} items={items} />
      <Typography.Paragraph type="secondary" style={{ marginTop: 16, marginBottom: 0 }}>
        {t("webhookSigning.secret.nextStep")}
      </Typography.Paragraph>
    </Modal>
  );
}

interface RetireFormValues {
  reason: string;
}

/**
 * 退役一个版本的二次确认。只在打开时挂载、关闭即卸载。
 *
 * 退役是终态，确认框写明；退役 `ACTIVE` 时另外写明项目从此没有签名密钥。原因必填、
 * 去首尾空白后 1–255，只写业务说明。退役幂等，失败后原样再点一次是安全的。
 */
function RetireModal({
  customerId,
  projectId,
  target,
  onDone,
  onFailed,
  onCancel,
}: {
  customerId: string;
  projectId: string;
  target: WebhookSecret;
  onDone: () => void;
  onFailed: (error: Error) => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const [form] = Form.useForm<RetireFormValues>();
  const inFlight = useRef(false);

  const retire = useMutation({
    // 面板只靠重读列表，不用返回值。
    mutationFn: async (reason: string): Promise<void> => {
      await retireWebhookSecret(customerId, projectId, target.key_version, reason);
    },
    onSuccess: onDone,
    onError: onFailed,
    onSettled: () => {
      inFlight.current = false;
    },
  });

  const submit = (values: RetireFormValues): void => {
    if (inFlight.current) {
      return;
    }
    inFlight.current = true;
    retire.mutate(values.reason.trim());
  };

  const pending = retire.isPending;
  const close = (): void => {
    if (!pending) {
      onCancel();
    }
  };

  return (
    <Modal
      open
      title={t("webhookSigning.retire.confirmTitle", { version: target.key_version })}
      footer={[
        <Button key="cancel" onClick={close} disabled={pending}>
          {t("webhookSigning.cancel")}
        </Button>,
        <Button
          key="confirm"
          type="primary"
          danger
          onClick={() => form.submit()}
          loading={pending}
          disabled={pending}
        >
          {t("webhookSigning.retire.confirm")}
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
        message={t("webhookSigning.retire.warning")}
      />
      {target.status === "ACTIVE" ? (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 16 }}
          message={t("webhookSigning.retire.activeWarning")}
        />
      ) : null}
      <WebhookErrorAlert error={retire.error} title={t("webhookSigning.retire.failed")} />
      <Form<RetireFormValues>
        form={form}
        layout="vertical"
        initialValues={{ reason: "" }}
        onFinish={submit}
        disabled={pending}
      >
        <Form.Item
          name="reason"
          label={t("webhookSigning.retire.reason")}
          extra={t("webhookSigning.retire.reasonHint")}
          rules={[
            {
              required: true,
              whitespace: true,
              message: t("webhookSigning.retire.reasonRequired"),
            },
            maxTrimmed(REASON_MAX, t("webhookSigning.retire.reasonTooLong")),
          ]}
        >
          <Input.TextArea rows={3} />
        </Form.Item>
      </Form>
    </Modal>
  );
}
