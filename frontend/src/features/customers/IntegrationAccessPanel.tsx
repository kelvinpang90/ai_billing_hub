/**
 * 一个项目的集成 API 凭据：建、列、轮换、吊销一个版本、吊销整个 key。
 *
 * 语义以 docs/api.md「管理端集成 API 凭据」与 docs/design/AIH-TASK-012-integration-access.md 为准。
 * 挂在 `ProjectsPanel` 每一行的展开区里（AIH-TASK-016 留的挂载点）。
 *
 * 列表分页，顺序由后端决定（先按 `api_key` 的创建先后，再按版本从小到大），前端只把同一页里
 * 相邻的同一个 `api_key` 归成一组，不重排。
 *
 * ⚠️ `secret` 只显示一次 —— 这是本组件最要紧的规则，改之前先读完：
 *
 *   1. `secret` 只来自建凭据与轮换的 **mutation 返回值**，只画在 {@link SecretDialog} 里。
 *   2. **不进查询缓存**：成功后不 `setQueryData`，只让列表查询过期重读（列表响应里本来就没有
 *      `secret`）。mutation 的 `gcTime` 是 0，对话框一关就 `reset()`，mutation 缓存里的那份
 *      随即被丢掉。
 *   3. 不写 localStorage / sessionStorage、不进 console、不进 URL、不进组件之外的任何状态。
 *   4. 对话框关闭就卸载，`secret` 从 DOM 里消失，之后再也看不到。
 *
 * 结果未知（网络错误、超时、5xx）时**不自动重试**：新版本若已入库，`secret` 就拿不回来了，
 * 自动重试只会再多出一个。按 docs/api.md 的恢复方式提示管理员：看列表，再轮换一次或吊销那个版本。
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
  Pagination,
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
  CREDENTIAL_VERSION_CONFLICT,
  createCredential,
  credentialListQueryKey,
  isOutcomeUnknown,
  listCredentials,
  projectCredentialsQueryKey,
  revokeCredential,
  revokeCredentialVersion,
  rotateCredential,
  type Credential,
  type CredentialStatus,
  type IssuedCredential,
} from "../../api/integrationAccess";
import { DateTimeText } from "../../components/DateTimeText";
import { RequestReference } from "../../components/RequestReference";
import { CustomerErrorAlert } from "./CustomerForm";

/** 与后端 `app/schemas/integration_access.py` 的上限一致（去掉首尾空白之后）。 */
const REASON_MAX = 255;

const PAGE_SIZE_OPTIONS = [10, 20, 50, MAX_PAGE_SIZE];

/** 空的时间显示成一道横线，与详情页的空字段一致。 */
const NO_VALUE = "—";

export interface CredentialGroup {
  apiKey: string;
  versions: Credential[];
}

/** 把同一页里相邻的同一个 `api_key` 归成一组。后端已经按 key 再按版本排好，这里不重排。 */
export function groupByApiKey(items: readonly Credential[]): CredentialGroup[] {
  const groups: CredentialGroup[] = [];
  for (const item of items) {
    const last = groups.at(-1);
    if (last !== undefined && last.apiKey === item.api_key) {
      last.versions.push(item);
    } else {
      groups.push({ apiKey: item.api_key, versions: [item] });
    }
  }
  return groups;
}

/** 轮换要带的 `current_key_version`：这个 key 在当前列表里的最大版本。 */
export function newestVersion(group: CredentialGroup): number {
  return Math.max(...group.versions.map((version) => version.key_version));
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

function StatusTag({ status }: { status: CredentialStatus }) {
  const { t } = useTranslation();
  return status === "ACTIVE" ? (
    <Tag color="green">{t("integrations.status.active")}</Tag>
  ) : (
    <Tag>{t("integrations.status.revoked")}</Tag>
  );
}

type RevokeTarget =
  | { kind: "version"; apiKey: string; keyVersion: number }
  | { kind: "key"; apiKey: string };

interface RotateTarget {
  apiKey: string;
  currentKeyVersion: number;
}

export function IntegrationAccessPanel({
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
  const [rotating, setRotating] = useState<RotateTarget | null>(null);
  const [revoking, setRevoking] = useState<RevokeTarget | null>(null);
  const [revoked, setRevoked] = useState<RevokeTarget["kind"] | null>(null);
  // 与建项目同一个理由：`isPending` 要等下一次渲染，双击时第二次点击可能赶在按钮禁用之前。
  // 建凭据**不幂等**，双击就是两个 key。
  const inFlight = useRef(false);

  const credentials = useQuery({
    queryKey: credentialListQueryKey(customerId, projectId, page, pageSize),
    queryFn: ({ signal }) => listCredentials(customerId, projectId, page, pageSize, signal),
    placeholderData: keepPreviousData,
  });

  const refreshList = (): void => {
    void queryClient.invalidateQueries({
      queryKey: projectCredentialsQueryKey(customerId, projectId),
    });
  };

  // 规则 2：`gcTime: 0` —— 对话框关闭、`reset()` 之后，mutation 缓存里的那份 `secret` 立即被丢掉。
  const create = useMutation({
    mutationFn: () => createCredential(customerId, projectId),
    gcTime: 0,
    onSuccess: refreshList,
    onError: (error) => {
      // 结果未知时新 key 可能已经入库：重读列表让管理员看得到它。
      if (isOutcomeUnknown(error)) {
        refreshList();
      }
    },
    onSettled: () => {
      inFlight.current = false;
    },
  });

  const rotate = useMutation({
    mutationFn: (target: RotateTarget) =>
      rotateCredential(customerId, projectId, target.apiKey, target.currentKeyVersion),
    gcTime: 0,
    onSuccess: refreshList,
    onError: (error) => {
      // 409 冲突：别人已经轮换过，列表上的版本号过时了。结果未知：新版本可能已经入库。
      if (
        (error instanceof ApiError && error.code === CREDENTIAL_VERSION_CONFLICT) ||
        isOutcomeUnknown(error)
      ) {
        refreshList();
      }
    },
    onSettled: () => {
      inFlight.current = false;
      setRotating(null);
    },
  });

  /** 开始新的操作之前，把上一次的结果与报错都清掉。 */
  const clearOutcomes = (): void => {
    create.reset();
    rotate.reset();
    setRevoked(null);
  };

  const startCreate = (): void => {
    if (inFlight.current) {
      return;
    }
    clearOutcomes();
    inFlight.current = true;
    create.mutate();
  };

  const startRotate = (group: CredentialGroup): void => {
    clearOutcomes();
    setRotating({ apiKey: group.apiKey, currentKeyVersion: newestVersion(group) });
  };

  const confirmRotate = (): void => {
    if (inFlight.current || rotating === null) {
      return;
    }
    inFlight.current = true;
    rotate.mutate(rotating);
  };

  const startRevoke = (target: RevokeTarget): void => {
    clearOutcomes();
    setRevoking(target);
  };

  const finishRevoke = (kind: RevokeTarget["kind"]): void => {
    setRevoking(null);
    setRevoked(kind);
    refreshList();
  };

  /** 规则 4：关闭即丢弃。两个 mutation 都 reset，`secret` 不留在任何地方。 */
  const discardSecret = (): void => {
    create.reset();
    rotate.reset();
  };

  const goTo = (nextPage: number, nextPageSize: number): void => {
    setPage(nextPageSize === pageSize ? nextPage : 1);
    setPageSize(nextPageSize);
  };

  const issued: IssuedCredential | undefined = create.data ?? rotate.data;
  const busy = create.isPending || rotate.isPending;

  const versionColumns = (apiKey: string): TableColumnsType<Credential> => [
    {
      key: "key_version",
      title: t("integrations.field.keyVersion"),
      dataIndex: "key_version",
    },
    {
      key: "status",
      title: t("integrations.field.status"),
      render: (_: unknown, version) => <StatusTag status={version.status} />,
    },
    {
      key: "verifiable",
      title: t("integrations.field.verifiable"),
      render: (_: unknown, version) =>
        version.verifiable ? t("integrations.verifiable.yes") : t("integrations.verifiable.no"),
    },
    {
      key: "valid_from",
      title: t("integrations.field.validFrom"),
      render: (_: unknown, version) => <DateTimeText value={version.valid_from} />,
    },
    {
      key: "valid_until",
      title: t("integrations.field.validUntil"),
      render: (_: unknown, version) =>
        version.valid_until === null ? (
          t("integrations.noEndDate")
        ) : (
          <DateTimeText value={version.valid_until} />
        ),
    },
    {
      key: "revoked_at",
      title: t("integrations.field.revokedAt"),
      render: (_: unknown, version) =>
        version.revoked_at === null ? NO_VALUE : <DateTimeText value={version.revoked_at} />,
    },
    {
      key: "actions",
      title: t("integrations.field.actions"),
      render: (_: unknown, version) => (
        <Button
          size="small"
          danger
          disabled={version.status === "REVOKED" || busy}
          onClick={() =>
            startRevoke({ kind: "version", apiKey, keyVersion: version.key_version })
          }
        >
          {t("integrations.revoke.version", { version: version.key_version })}
        </Button>
      ),
    },
  ];

  let failure: ReactNode = null;
  const issueError = create.error ?? rotate.error;
  if (issueError !== null) {
    const action = create.error === null ? "rotate" : "create";
    if (isOutcomeUnknown(issueError)) {
      failure = (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          message={t("integrations.unknownTitle")}
          description={
            <>
              <Typography.Paragraph>
                {action === "create"
                  ? t("integrations.create.unknownBody")
                  : t("integrations.rotate.unknownBody")}
              </Typography.Paragraph>
              <RequestReference error={issueError} />
            </>
          }
        />
      );
    } else if (issueError instanceof ApiError && issueError.code === CREDENTIAL_VERSION_CONFLICT) {
      failure = (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          message={t("integrations.rotate.conflictTitle")}
          description={
            <>
              <Typography.Paragraph>{t("integrations.rotate.conflictBody")}</Typography.Paragraph>
              <RequestReference error={issueError} />
            </>
          }
        />
      );
    } else {
      failure = (
        <CustomerErrorAlert
          error={issueError}
          title={
            action === "create" ? t("integrations.create.failed") : t("integrations.rotate.failed")
          }
        />
      );
    }
  }

  const revokedNotice =
    revoked === null ? null : (
      <Alert
        type="success"
        showIcon
        style={{ marginBottom: 16 }}
        message={
          revoked === "version"
            ? t("integrations.revoke.versionDone")
            : t("integrations.revoke.keyDone")
        }
      />
    );

  let body: ReactNode;
  if (credentials.isPending) {
    body = (
      <>
        <Skeleton active title={false} paragraph={{ rows: 2 }} />
        <Typography.Text type="secondary">{t("integrations.loading")}</Typography.Text>
      </>
    );
  } else if (credentials.isError) {
    body = (
      <CustomerErrorAlert
        error={credentials.error}
        title={t("integrations.loadFailed")}
        onRetry={() => void credentials.refetch()}
      />
    );
  } else if (credentials.data.total === 0) {
    body = <Empty description={t("integrations.empty")} />;
  } else {
    const groups = groupByApiKey(credentials.data.items);
    body = (
      <>
        {groups.length === 0 ? (
          <Empty description={t("integrations.emptyPage")} />
        ) : (
          groups.map((group) => (
            <Card
              key={group.apiKey}
              type="inner"
              size="small"
              style={{ marginBottom: 16 }}
              title={
                <Space>
                  <Typography.Text type="secondary">{t("integrations.field.apiKey")}</Typography.Text>
                  <Typography.Text code copyable>
                    {group.apiKey}
                  </Typography.Text>
                </Space>
              }
              extra={
                <Space>
                  <Button size="small" disabled={busy} onClick={() => startRotate(group)}>
                    {t("integrations.rotate.open")}
                  </Button>
                  <Button
                    size="small"
                    danger
                    disabled={busy}
                    onClick={() => startRevoke({ kind: "key", apiKey: group.apiKey })}
                  >
                    {t("integrations.revoke.key")}
                  </Button>
                </Space>
              }
            >
              <Table<Credential>
                size="small"
                rowKey="key_version"
                columns={versionColumns(group.apiKey)}
                dataSource={group.versions}
                pagination={false}
                loading={credentials.isFetching}
              />
            </Card>
          ))
        )}
        <Pagination
          current={page}
          pageSize={pageSize}
          total={credentials.data.total}
          showSizeChanger
          pageSizeOptions={PAGE_SIZE_OPTIONS}
          showTotal={(count) => t("integrations.total", { total: count })}
          onChange={goTo}
        />
      </>
    );
  }

  return (
    <Card
      size="small"
      title={t("integrations.title")}
      extra={
        // ⚠️ 建凭据**不幂等**：提交进行中必须禁用，否则双击就是两个 key。
        <Button onClick={startCreate} loading={create.isPending} disabled={busy}>
          {t("integrations.create.open")}
        </Button>
      }
    >
      {failure}
      {revokedNotice}
      {body}
      {rotating === null ? null : (
        <Modal
          open
          title={t("integrations.rotate.confirmTitle")}
          okText={t("integrations.rotate.confirm")}
          cancelText={t("integrations.cancel")}
          onOk={confirmRotate}
          onCancel={() => {
            if (!rotate.isPending) {
              setRotating(null);
            }
          }}
          okButtonProps={{ loading: rotate.isPending, disabled: rotate.isPending }}
          cancelButtonProps={{ disabled: rotate.isPending }}
          maskClosable={false}
          closable={!rotate.isPending}
          keyboard={!rotate.isPending}
        >
          <Typography.Paragraph>
            <Typography.Text code>{rotating.apiKey}</Typography.Text>
          </Typography.Paragraph>
          <Typography.Paragraph>
            {t("integrations.rotate.confirmBody", { version: rotating.currentKeyVersion })}
          </Typography.Paragraph>
        </Modal>
      )}
      {revoking === null ? null : (
        <RevokeModal
          customerId={customerId}
          projectId={projectId}
          target={revoking}
          onDone={finishRevoke}
          onCancel={() => setRevoking(null)}
        />
      )}
      {issued === undefined ? null : <SecretDialog issued={issued} onClose={discardSecret} />}
    </Card>
  );
}

/**
 * 建凭据与轮换的结果：`secret` 唯一一次出现的地方。
 *
 * 只在有结果时挂载，关闭即卸载。点遮罩、按 Esc 都不关：一不小心关掉就再也看不到了。
 */
function SecretDialog({ issued, onClose }: { issued: IssuedCredential; onClose: () => void }) {
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
      key: "api_key",
      label: t("integrations.field.apiKey"),
      children: <Typography.Text code>{issued.api_key}</Typography.Text>,
    },
    {
      key: "key_version",
      label: t("integrations.field.keyVersion"),
      children: issued.key_version,
    },
    {
      key: "secret",
      label: t("integrations.field.secret"),
      children: (
        <Space direction="vertical">
          <Typography.Text code style={{ wordBreak: "break-all" }}>
            {issued.secret}
          </Typography.Text>
          <Space>
            <Button onClick={copySecret}>{t("integrations.secret.copy")}</Button>
            {copy === "copied" ? (
              <Typography.Text type="success">{t("integrations.secret.copied")}</Typography.Text>
            ) : null}
            {copy === "failed" ? (
              <Typography.Text type="danger">{t("integrations.secret.copyFailed")}</Typography.Text>
            ) : null}
          </Space>
        </Space>
      ),
    },
  ];

  return (
    <Modal
      open
      title={t("integrations.secret.title")}
      width={720}
      footer={[
        <Button key="close" type="primary" onClick={onClose}>
          {t("integrations.secret.close")}
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
        message={t("integrations.secret.warning")}
      />
      <Descriptions bordered size="small" column={1} items={items} />
    </Modal>
  );
}

interface RevokeFormValues {
  reason: string;
}

/**
 * 吊销一个版本或整个 key 的二次确认。只在打开时挂载、关闭即卸载。
 *
 * 吊销是终态，确认框写明；原因必填、去首尾空白后 1–255，只写业务说明。吊销幂等，
 * 失败后原样再点一次是安全的。
 */
function RevokeModal({
  customerId,
  projectId,
  target,
  onDone,
  onCancel,
}: {
  customerId: string;
  projectId: string;
  target: RevokeTarget;
  onDone: (kind: RevokeTarget["kind"]) => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const [form] = Form.useForm<RevokeFormValues>();
  const inFlight = useRef(false);

  const revoke = useMutation({
    // 两个接口的返回值形状不同（一个版本 / 全部版本），面板只靠重读列表，不用返回值。
    mutationFn: async (reason: string): Promise<void> => {
      if (target.kind === "version") {
        await revokeCredentialVersion(
          customerId,
          projectId,
          target.apiKey,
          target.keyVersion,
          reason,
        );
      } else {
        await revokeCredential(customerId, projectId, target.apiKey, reason);
      }
    },
    onSuccess: () => onDone(target.kind),
    onSettled: () => {
      inFlight.current = false;
    },
  });

  const submit = (values: RevokeFormValues): void => {
    if (inFlight.current) {
      return;
    }
    inFlight.current = true;
    revoke.mutate(values.reason.trim());
  };

  const pending = revoke.isPending;
  const close = (): void => {
    if (!pending) {
      onCancel();
    }
  };

  return (
    <Modal
      open
      title={
        target.kind === "version"
          ? t("integrations.revoke.versionTitle", { version: target.keyVersion })
          : t("integrations.revoke.keyTitle")
      }
      footer={[
        <Button key="cancel" onClick={close} disabled={pending}>
          {t("integrations.cancel")}
        </Button>,
        <Button
          key="confirm"
          type="primary"
          danger
          onClick={() => form.submit()}
          loading={pending}
          disabled={pending}
        >
          {t("integrations.revoke.confirm")}
        </Button>,
      ]}
      onCancel={close}
      maskClosable={false}
      closable={!pending}
      keyboard={!pending}
    >
      <Typography.Paragraph>
        <Typography.Text code>{target.apiKey}</Typography.Text>
      </Typography.Paragraph>
      <Alert
        type="warning"
        showIcon
        style={{ marginBottom: 16 }}
        message={
          target.kind === "version"
            ? t("integrations.revoke.versionWarning")
            : t("integrations.revoke.keyWarning")
        }
      />
      <CustomerErrorAlert error={revoke.error} title={t("integrations.revoke.failed")} />
      <Form<RevokeFormValues>
        form={form}
        layout="vertical"
        initialValues={{ reason: "" }}
        onFinish={submit}
        disabled={pending}
      >
        <Form.Item
          name="reason"
          label={t("integrations.revoke.reason")}
          extra={t("integrations.revoke.reasonHint")}
          rules={[
            { required: true, whitespace: true, message: t("integrations.revoke.reasonRequired") },
            maxTrimmed(REASON_MAX, t("integrations.revoke.reasonTooLong")),
          ]}
        >
          <Input.TextArea rows={3} />
        </Form.Item>
      </Form>
    </Modal>
  );
}
