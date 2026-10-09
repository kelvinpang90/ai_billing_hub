/**
 * 管理端 AI 供应商列表（`GET` / `POST` / `PATCH /api/v1/admin/ai-providers`，docs/api.md「AI 目录」）。
 *
 * 顶栏的「AI catalog」进这一页；页顶有到计量类型页的链接。列表分页，按 `code` 升序（由后端决定，
 * 前端不重排），可按状态筛。三种状态都画出来（spec §132 DoD 第 8 条）：加载中、失败（带 request_id）、
 * 空列表。
 *
 * 本文件后半是目录三页共用的部件（错误提示、二次确认、改名、停用 / 重新启用、状态筛选、分页）。
 * 三页都在 AIH-TASK-035 的可改路径里，而公共组件目录不在，所以照 `CustomerForm` 导出
 * `CustomerErrorAlert` 的先例放在这里导出。
 *
 * 写操作一律二次确认：先填表（或点按钮），再在确认框里点确认才发请求。确认进行中禁用按钮，并用一个
 * ref 挡住第二次点击（`isPending` 要等下一次渲染才变 true）。成功后让相关列表过期重读，不在前端拼行。
 *
 * ⚠️ 代码列按原样显示与提交，不去空白、不改大小写（后端按字节比较）。
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
  Radio,
  Skeleton,
  Space,
  Table,
  Tag,
  Typography,
  type DescriptionsProps,
  type FormInstance,
  type FormRule,
  type TableColumnsType,
} from "antd";
import { useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import {
  AI_MODEL_ALIAS_NOT_FOUND,
  AI_MODEL_ALIAS_TAKEN,
  AI_MODEL_CODE_TAKEN,
  AI_MODEL_NOT_FOUND,
  AI_PROVIDER_CODE_TAKEN,
  AI_PROVIDER_NOT_FOUND,
  DISPLAY_NAME_MAX,
  PROVIDER_CODE_PATTERN,
  PROVIDER_LISTS_QUERY_KEY,
  USAGE_METER_COMPONENT_CODE_TAKEN,
  USAGE_METER_TYPE_CODE_TAKEN,
  USAGE_METER_TYPE_NOT_FOUND,
  VALIDATION_ERROR,
  createProvider,
  listProviders,
  providerListQueryKey,
  updateProvider,
  type CatalogEntryPatch,
  type CatalogStatus,
  type CreateProviderBody,
  type Provider,
  type StatusFilter,
} from "../../api/adminCatalog";
import { ADMIN_REQUIRED, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE } from "../../api/adminCustomers";
import { ApiError } from "../../api/client";
import { DateTimeText } from "../../components/DateTimeText";
import { RequestReference } from "../../components/RequestReference";
import { ROUTES, catalogProviderDetailPath } from "../../routes/paths";

export function ProvidersPage() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { status, filter, setStatus, paging } = useStatusFilteredPaging(DEFAULT_PAGE_SIZE);
  const [creating, setCreating] = useState(false);
  const [renaming, setRenaming] = useState<Provider | null>(null);
  const [changingStatus, setChangingStatus] = useState<Provider | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const statusDone = useStatusDoneMessage();

  const providers = useQuery({
    queryKey: providerListQueryKey(filter, paging.page, paging.pageSize),
    queryFn: ({ signal }) => listProviders(filter, paging.page, paging.pageSize, signal),
    // 翻页时先留着上一页，不让整张表闪成骨架屏。
    placeholderData: keepPreviousData,
  });

  const finish = (message: string): void => {
    void queryClient.invalidateQueries({ queryKey: PROVIDER_LISTS_QUERY_KEY });
    setCreating(false);
    setRenaming(null);
    setChangingStatus(null);
    setNotice(message);
  };

  const open = (action: () => void): void => {
    setNotice(null);
    action();
  };

  const columns: TableColumnsType<Provider> = [
    {
      key: "code",
      title: t("catalog.field.code"),
      render: (_: unknown, provider) => (
        <Link to={catalogProviderDetailPath(provider.id)}>
          <Typography.Text code>{provider.code}</Typography.Text>
        </Link>
      ),
    },
    { key: "display_name", title: t("catalog.field.displayName"), dataIndex: "display_name" },
    {
      key: "status",
      title: t("catalog.field.status"),
      render: (_: unknown, provider) => <CatalogStatusTag status={provider.status} />,
    },
    {
      key: "created_at",
      title: t("catalog.field.createdAt"),
      render: (_: unknown, provider) => <DateTimeText value={provider.created_at} />,
    },
    {
      key: "actions",
      title: t("catalog.field.actions"),
      render: (_: unknown, provider) => (
        <EntryActions
          status={provider.status}
          onRename={() => open(() => setRenaming(provider))}
          onChangeStatus={() => open(() => setChangingStatus(provider))}
        />
      ),
    },
  ];

  let body: ReactNode;
  if (providers.isPending) {
    body = <LoadingBlock text={t("catalog.providers.loading")} />;
  } else if (providers.isError) {
    body = (
      <CatalogErrorAlert
        error={providers.error}
        title={t("catalog.providers.loadFailed")}
        onRetry={() => void providers.refetch()}
      />
    );
  } else if (providers.data.total === 0) {
    body = (
      <Empty
        description={
          status === undefined ? t("catalog.providers.empty") : t("catalog.providers.emptyFiltered")
        }
      />
    );
  } else {
    body = (
      <Table<Provider>
        // 供应商 `code` 全局唯一。
        rowKey="code"
        columns={columns}
        dataSource={providers.data.items}
        loading={providers.isFetching}
        // 页码超过末页时后端回空 items、total 照报：说清楚是「这一页没有」。
        locale={{ emptyText: t("catalog.providers.emptyPage") }}
        pagination={{
          current: paging.page,
          pageSize: paging.pageSize,
          total: providers.data.total,
          showSizeChanger: true,
          pageSizeOptions: PAGE_SIZE_OPTIONS,
          showTotal: (count) => t("catalog.providers.total", { total: count }),
          onChange: paging.goTo,
        }}
      />
    );
  }

  return (
    <Card
      title={t("catalog.providers.title")}
      extra={
        <Space>
          <Link to={ROUTES.catalogMeterTypes}>{t("catalog.providers.meterTypesLink")}</Link>
          <Button type="primary" onClick={() => open(() => setCreating(true))}>
            {t("catalog.providers.create")}
          </Button>
        </Space>
      }
    >
      <Typography.Paragraph type="secondary">{t("catalog.providers.intro")}</Typography.Paragraph>
      <NoticeAlert message={notice} />
      <div style={{ marginBottom: 16 }}>
        <StatusFilterControl value={status} onChange={setStatus} />
      </div>
      {body}
      {creating ? (
        <CreateProviderModal
          onDone={() => finish(t("catalog.providers.created"))}
          onCancel={() => setCreating(false)}
        />
      ) : null}
      {renaming === null ? null : (
        <RenameModal
          code={renaming.code}
          displayName={renaming.display_name}
          save={(patch) => updateProvider(renaming.id, patch)}
          onDone={() => finish(t("catalog.rename.done"))}
          onCancel={() => setRenaming(null)}
        />
      )}
      {changingStatus === null ? null : (
        <StatusChangeModal
          code={changingStatus.code}
          status={changingStatus.status}
          save={(patch) => updateProvider(changingStatus.id, patch)}
          onDone={(next) => finish(statusDone(next))}
          onCancel={() => setChangingStatus(null)}
        />
      )}
    </Card>
  );
}

interface ProviderFormValues {
  code: string;
  display_name: string;
}

/** 建供应商：填表 → 确认 → 发请求。重发得 409（代码已存在），不会建出两个。 */
function CreateProviderModal({ onDone, onCancel }: { onDone: () => void; onCancel: () => void }) {
  const { t } = useTranslation();
  const [form] = Form.useForm<ProviderFormValues>();
  const [reviewing, setReviewing] = useState<CreateProviderBody | null>(null);
  const nameRules = useDisplayNameRules();

  const review = (values: ProviderFormValues): void => {
    // 代码原样；显示名去首尾空白（后端也这么存）。
    setReviewing({ code: values.code, display_name: values.display_name.trim() });
  };

  return (
    <>
      <FormStepModal title={t("catalog.providers.create")} form={form} onCancel={onCancel}>
        <Form<ProviderFormValues>
          form={form}
          layout="vertical"
          initialValues={{ code: "", display_name: "" }}
          onFinish={review}
        >
          <Form.Item
            name="code"
            label={t("catalog.field.code")}
            extra={t("catalog.providers.codeHint")}
            rules={codeRules(
              PROVIDER_CODE_PATTERN,
              t("catalog.form.codeRequired"),
              t("catalog.providers.codeInvalid"),
            )}
          >
            <Input autoComplete="off" spellCheck={false} />
          </Form.Item>
          <Form.Item name="display_name" label={t("catalog.field.displayName")} rules={nameRules}>
            <Input autoComplete="off" />
          </Form.Item>
        </Form>
      </FormStepModal>
      {reviewing === null ? null : (
        <ConfirmWriteModal
          title={t("catalog.providers.confirmCreate")}
          confirmLabel={t("catalog.confirmCreate")}
          backLabel={t("catalog.backToEdit")}
          failedTitle={t("catalog.providers.createFailed")}
          run={() => createProvider(reviewing)}
          onDone={onDone}
          onBack={() => setReviewing(null)}
        >
          <SummaryList
            items={[
              { key: "code", label: t("catalog.field.code"), children: <CodeText code={reviewing.code} /> },
              { key: "display_name", label: t("catalog.field.displayName"), children: reviewing.display_name },
            ]}
          />
          <Alert type="warning" showIcon message={t("catalog.codeIsPermanent")} />
        </ConfirmWriteModal>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// 目录三页共用
// ---------------------------------------------------------------------------

export const PAGE_SIZE_OPTIONS = [10, 20, 50, MAX_PAGE_SIZE];

/** 列表页码放在组件状态里；换了每页条数，原来的页码就没有意义了，回到第一页。 */
export function usePaging(initialPageSize: number) {
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(initialPageSize);
  const goTo = (nextPage: number, nextPageSize: number): void => {
    setPage(nextPageSize === pageSize ? nextPage : 1);
    setPageSize(nextPageSize);
  };
  return { page, pageSize, goTo, reset: () => setPage(1) };
}

/** 状态筛选 + 分页。换筛选条件回到第一页。没选状态时 `filter` 是 `{}`，查询串里就没有 `status`。 */
export function useStatusFilteredPaging(initialPageSize: number) {
  const [status, setStatusState] = useState<CatalogStatus | undefined>(undefined);
  const paging = usePaging(initialPageSize);
  const filter: StatusFilter = status === undefined ? {} : { status };
  const setStatus = (next: CatalogStatus | undefined): void => {
    setStatusState(next);
    paging.reset();
  };
  return { status, filter, setStatus, paging };
}

const ALL_STATUSES = "ALL";

/** 全部 / 启用中 / 已停用。 */
export function StatusFilterControl({
  value,
  onChange,
}: {
  value: CatalogStatus | undefined;
  onChange: (next: CatalogStatus | undefined) => void;
}) {
  const { t } = useTranslation();
  return (
    <Space>
      <Typography.Text type="secondary">{t("catalog.filter.status")}</Typography.Text>
      <Radio.Group
        optionType="button"
        value={value ?? ALL_STATUSES}
        onChange={(event) => {
          const raw: unknown = event.target.value;
          onChange(raw === "ACTIVE" || raw === "RETIRED" ? raw : undefined);
        }}
        options={[
          { value: ALL_STATUSES, label: t("catalog.filter.all") },
          { value: "ACTIVE", label: t("catalog.status.active") },
          { value: "RETIRED", label: t("catalog.status.retired") },
        ]}
      />
    </Space>
  );
}

/** 认不出的状态原样显示，不猜。 */
export function CatalogStatusTag({ status }: { status: string }) {
  const { t } = useTranslation();
  if (status === "ACTIVE") {
    return <Tag color="green">{t("catalog.status.active")}</Tag>;
  }
  if (status === "RETIRED") {
    return <Tag>{t("catalog.status.retired")}</Tag>;
  }
  return <Tag>{status}</Tag>;
}

/** 代码一律等宽、原样显示。 */
export function CodeText({ code }: { code: string }) {
  return <Typography.Text code>{code}</Typography.Text>;
}

export function LoadingBlock({ text }: { text: string }) {
  return (
    <>
      <Skeleton active title={false} paragraph={{ rows: 3 }} />
      <Typography.Text type="secondary">{text}</Typography.Text>
    </>
  );
}

export function NoticeAlert({ message }: { message: string | null }) {
  return message === null ? null : (
    <Alert type="success" showIcon style={{ marginBottom: 16 }} message={message} />
  );
}

export function SummaryList({ items }: { items: NonNullable<DescriptionsProps["items"]> }) {
  return (
    <Descriptions bordered size="small" column={1} items={items} style={{ marginBottom: 16 }} />
  );
}

/** 每行的「改名」与「停用 / 重新启用」。 */
export function EntryActions({
  status,
  onRename,
  onChangeStatus,
}: {
  status: CatalogStatus;
  onRename: () => void;
  onChangeStatus: () => void;
}) {
  const { t } = useTranslation();
  return (
    <Space>
      <Button size="small" onClick={onRename}>
        {t("catalog.rename.open")}
      </Button>
      <Button size="small" danger={status === "ACTIVE"} onClick={onChangeStatus}>
        {status === "ACTIVE" ? t("catalog.status.retire") : t("catalog.status.reactivate")}
      </Button>
    </Space>
  );
}

/**
 * 目录接口出错时的提示。
 *
 * 403 `ADMIN_REQUIRED` 单独说「没有权限」—— 前端不做角色判断，是不是管理员只看后端这一句。
 * 本节已知的 409 / 404 / 422 码显示对应的文案；认不出的码用调用方给的标题。两种情况下面都带后端
 * message 与 request_id（`RequestReference`），所以未知码也看得到后端的说法。
 */
export function CatalogErrorAlert({
  error,
  title,
  onRetry,
}: {
  error: Error | null;
  title: string;
  onRetry?: (() => void) | undefined;
}) {
  const { t } = useTranslation();
  if (error === null) {
    return null;
  }
  const code = error instanceof ApiError ? error.code : null;
  let message = title;
  switch (code) {
    case ADMIN_REQUIRED:
      message = t("catalog.forbidden");
      break;
    case USAGE_METER_TYPE_CODE_TAKEN:
      message = t("catalog.error.meterTypeCodeTaken");
      break;
    case USAGE_METER_COMPONENT_CODE_TAKEN:
      message = t("catalog.error.componentCodeTaken");
      break;
    case AI_PROVIDER_CODE_TAKEN:
      message = t("catalog.error.providerCodeTaken");
      break;
    case AI_MODEL_CODE_TAKEN:
      message = t("catalog.error.modelCodeTaken");
      break;
    case AI_MODEL_ALIAS_TAKEN:
      message = t("catalog.error.aliasTaken");
      break;
    case USAGE_METER_TYPE_NOT_FOUND:
      message = t("catalog.error.meterTypeNotFound");
      break;
    case AI_PROVIDER_NOT_FOUND:
      message = t("catalog.error.providerNotFound");
      break;
    case AI_MODEL_NOT_FOUND:
      message = t("catalog.error.modelNotFound");
      break;
    case AI_MODEL_ALIAS_NOT_FOUND:
      message = t("catalog.error.aliasNotFound");
      break;
    case VALIDATION_ERROR:
      message = t("catalog.error.validation");
      break;
    default:
      break;
  }
  return (
    <Alert
      type={code === ADMIN_REQUIRED ? "warning" : "error"}
      showIcon
      style={{ marginBottom: 16 }}
      message={message}
      description={<RequestReference error={error} />}
      action={
        onRetry === undefined ? undefined : (
          <Button size="small" onClick={onRetry}>
            {t("catalog.retry")}
          </Button>
        )
      }
    />
  );
}

/** 按码点数，与 Python 的 `len()` 一致（`.length` 数的是 UTF-16 单元，emoji 算 2）。 */
function trimmedLength(value: unknown): number {
  return typeof value === "string" ? [...value.trim()].length : 0;
}

/** 显示名：去首尾空白后 1–255。 */
export function useDisplayNameRules(): FormRule[] {
  const { t } = useTranslation();
  return [
    { required: true, whitespace: true, message: t("catalog.form.displayNameRequired") },
    {
      validator: (_rule, value: unknown) =>
        trimmedLength(value) > DISPLAY_NAME_MAX
          ? Promise.reject(new Error(t("catalog.form.displayNameTooLong")))
          : Promise.resolve(),
    },
  ];
}

/**
 * 代码：必填，且整串匹配后端的正则（见 api/adminCatalog.ts 的字段规则常量）。不去空白：带空白的
 * 代码后端是 422，这里也就直接说它不合法。
 */
export function codeRules(pattern: RegExp, requiredMessage: string, invalidMessage: string): FormRule[] {
  return [
    { required: true, message: requiredMessage },
    { pattern, message: invalidMessage },
  ];
}

/** 写操作的第一步：填表。底部是「取消」与「检查」；点「检查」走表单校验，通过后由调用方打开确认框。 */
export function FormStepModal<Values>({
  title,
  form,
  onCancel,
  children,
}: {
  title: string;
  form: FormInstance<Values>;
  onCancel: () => void;
  children: ReactNode;
}) {
  const { t } = useTranslation();
  return (
    <Modal
      open
      title={title}
      onCancel={onCancel}
      maskClosable={false}
      footer={[
        <Button key="cancel" onClick={onCancel}>
          {t("catalog.cancel")}
        </Button>,
        <Button key="review" type="primary" onClick={() => form.submit()}>
          {t("catalog.review")}
        </Button>,
      ]}
    >
      {children}
    </Modal>
  );
}

/**
 * 写操作的二次确认：只有点了这里的确认按钮才发请求。
 *
 * 进行中禁用两个按钮、不许关闭，并用 ref 挡住第二次点击。失败时留在确认框里显示错误（可以返回去改）；
 * 成功后交给 `onDone`，由调用方关掉对话框并让列表过期重读。
 */
export function ConfirmWriteModal<Result>({
  title,
  confirmLabel,
  backLabel,
  danger,
  failedTitle,
  run,
  onDone,
  onError,
  onBack,
  children,
}: {
  title: string;
  confirmLabel: string;
  backLabel: string;
  danger?: boolean | undefined;
  failedTitle: string;
  run: () => Promise<Result>;
  onDone: (result: Result) => void;
  onError?: ((error: Error) => void) | undefined;
  onBack: () => void;
  children: ReactNode;
}) {
  const inFlight = useRef(false);
  const write = useMutation({
    mutationFn: run,
    onSuccess: (result) => onDone(result),
    onError: (error) => onError?.(error),
    onSettled: () => {
      inFlight.current = false;
    },
  });

  const confirm = (): void => {
    if (inFlight.current) {
      return;
    }
    inFlight.current = true;
    write.mutate();
  };

  const pending = write.isPending;
  const back = (): void => {
    if (!pending) {
      onBack();
    }
  };

  return (
    <Modal
      open
      title={title}
      onCancel={back}
      maskClosable={false}
      closable={!pending}
      keyboard={!pending}
      footer={[
        <Button key="back" onClick={back} disabled={pending}>
          {backLabel}
        </Button>,
        <Button
          key="confirm"
          type="primary"
          danger={danger === true}
          onClick={confirm}
          loading={pending}
          disabled={pending}
        >
          {confirmLabel}
        </Button>,
      ]}
    >
      {children}
      <div style={{ marginTop: 16 }}>
        <CatalogErrorAlert error={write.error} title={failedTitle} />
      </div>
    </Modal>
  );
}

interface RenameValues {
  display_name: string;
}

/** 改显示名（计量类型、供应商、模型共用）。代码不可改，PATCH 只带 `display_name`。 */
export function RenameModal({
  code,
  displayName,
  save,
  onDone,
  onCancel,
}: {
  code: string;
  displayName: string;
  save: (patch: CatalogEntryPatch) => Promise<unknown>;
  onDone: () => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const [form] = Form.useForm<RenameValues>();
  const [reviewing, setReviewing] = useState<string | null>(null);
  const nameRules = useDisplayNameRules();
  // 没有实际变化时后端 200、什么都不写；那就根本不该让人去确认一次「什么都没改」。
  const unchanged: FormRule = {
    validator: (_rule, value: unknown) =>
      typeof value === "string" && value.trim() === displayName
        ? Promise.reject(new Error(t("catalog.rename.unchanged")))
        : Promise.resolve(),
  };

  return (
    <>
      <FormStepModal title={t("catalog.rename.title", { code })} form={form} onCancel={onCancel}>
        <Form<RenameValues>
          form={form}
          layout="vertical"
          initialValues={{ display_name: displayName }}
          onFinish={(values) => setReviewing(values.display_name.trim())}
        >
          <Form.Item
            name="display_name"
            label={t("catalog.field.displayName")}
            rules={[...nameRules, unchanged]}
          >
            <Input autoComplete="off" />
          </Form.Item>
        </Form>
      </FormStepModal>
      {reviewing === null ? null : (
        <ConfirmWriteModal
          title={t("catalog.rename.confirmTitle")}
          confirmLabel={t("catalog.rename.confirm")}
          backLabel={t("catalog.backToEdit")}
          failedTitle={t("catalog.rename.failed")}
          run={() => save({ display_name: reviewing })}
          onDone={onDone}
          onBack={() => setReviewing(null)}
        >
          <SummaryList
            items={[
              { key: "code", label: t("catalog.field.code"), children: <CodeText code={code} /> },
              { key: "before", label: t("catalog.rename.before"), children: displayName },
              { key: "after", label: t("catalog.rename.after"), children: reviewing },
            ]}
          />
        </ConfirmWriteModal>
      )}
    </>
  );
}

/** 停用或重新启用。`ACTIVE ↔ RETIRED` 双向都允许；停用不影响已上报用量的解析与计费。 */
export function StatusChangeModal({
  code,
  status,
  save,
  onDone,
  onCancel,
}: {
  code: string;
  status: CatalogStatus;
  save: (patch: CatalogEntryPatch) => Promise<unknown>;
  onDone: (next: CatalogStatus) => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const next: CatalogStatus = status === "ACTIVE" ? "RETIRED" : "ACTIVE";
  const retiring = next === "RETIRED";
  return (
    <ConfirmWriteModal
      title={
        retiring
          ? t("catalog.status.retireTitle", { code })
          : t("catalog.status.reactivateTitle", { code })
      }
      confirmLabel={
        retiring ? t("catalog.status.confirmRetire") : t("catalog.status.confirmReactivate")
      }
      backLabel={t("catalog.cancel")}
      danger={retiring}
      failedTitle={t("catalog.status.failed")}
      run={() => save({ status: next })}
      onDone={() => onDone(next)}
      onBack={onCancel}
    >
      <Typography.Paragraph>
        {retiring ? t("catalog.status.retireBody") : t("catalog.status.reactivateBody")}
      </Typography.Paragraph>
    </ConfirmWriteModal>
  );
}

/** 停用 / 重新启用成功后的提示。 */
export function useStatusDoneMessage(): (next: CatalogStatus) => string {
  const { t } = useTranslation();
  return (next) =>
    next === "RETIRED" ? t("catalog.status.retiredDone") : t("catalog.status.reactivatedDone");
}
