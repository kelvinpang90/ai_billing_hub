/**
 * 管理端供应商详情：供应商本身、它的模型、它的别名段（docs/api.md「AI 目录」）。
 *
 * 别名段按字符串分组显示：同一个字符串的各段首尾相接，当前（未截断的）那一段高亮。列表是全部历史，
 * 后端按 `alias`、`effective_from`（`null` 在前）排好，这里只把同一页里相邻的同一字符串归成一组，
 * 不重排。每页默认 100 段；一个字符串的段跨了页时会在两页各成一组，按后端顺序读即可。
 *
 * 映射与撤销都只截断当前段、从「现在」起开新段：**改映射只影响此后发生的用量**，已经解析到某个
 * 模型的用量永远不变。字符串从没映射过时，第一段对过去全部生效（过去它一律是「未知」、没扣过钱）。
 * 页面上写明这一条。
 *
 * ⚠️ 代码列（模型、别名）按原样显示与提交，不去空白、不改大小写。
 */

import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Empty,
  Form,
  Input,
  Pagination,
  Result,
  Select,
  Space,
  Table,
  Tag,
  type DescriptionsProps,
  type TableColumnsType,
} from "antd";
import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link, useParams } from "react-router";

import {
  AI_MODEL_ALIAS_NOT_FOUND,
  AI_PROVIDER_NOT_FOUND,
  MODEL_CODE_PATTERN,
  PROVIDER_LISTS_QUERY_KEY,
  aliasListQueryKey,
  createModel,
  getProvider,
  listModelAliases,
  listModels,
  mapModelAlias,
  modelListQueryKey,
  providerAliasesQueryKey,
  providerDetailQueryKey,
  providerModelsQueryKey,
  retireModelAlias,
  updateModel,
  updateProvider,
  type AliasSegment,
  type CreateModelBody,
  type MapAliasBody,
  type Model,
  type Provider,
} from "../../api/adminCatalog";
import { DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE } from "../../api/adminCustomers";
import { ApiError } from "../../api/client";
import { DateTimeText } from "../../components/DateTimeText";
import { ROUTES } from "../../routes/paths";
import {
  CatalogErrorAlert,
  CatalogStatusTag,
  CodeText,
  ConfirmWriteModal,
  EntryActions,
  FormStepModal,
  LoadingBlock,
  NoticeAlert,
  PAGE_SIZE_OPTIONS,
  RenameModal,
  StatusChangeModal,
  StatusFilterControl,
  SummaryList,
  codeRules,
  useDisplayNameRules,
  usePaging,
  useStatusDoneMessage,
  useStatusFilteredPaging,
} from "./ProvidersPage";

/** 当前段的底色（antd 默认主题的 blue-1）。另有「Current」标签，不只靠颜色。 */
const CURRENT_SEGMENT_BACKGROUND = "#e6f4ff";

export function ProviderDetailPage() {
  const { t } = useTranslation();
  const { providerId = "" } = useParams();
  const queryClient = useQueryClient();
  const [renaming, setRenaming] = useState(false);
  const [changingStatus, setChangingStatus] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const statusDone = useStatusDoneMessage();

  const provider = useQuery({
    queryKey: providerDetailQueryKey(providerId),
    queryFn: ({ signal }) => getProvider(providerId, signal),
  });

  const backLink = <Link to={ROUTES.catalogProviders}>{t("catalog.detail.backToList")}</Link>;

  if (provider.isPending) {
    return (
      <Card>
        <LoadingBlock text={t("catalog.detail.loading")} />
      </Card>
    );
  }

  if (provider.isError) {
    const { error } = provider;
    // 404 是一个明确的答案（没有这个供应商），不是「出错了、请重试」。
    if (error instanceof ApiError && error.code === AI_PROVIDER_NOT_FOUND) {
      return (
        <Result
          status="404"
          title={t("catalog.detail.notFound")}
          subTitle={t("catalog.detail.notFoundBody")}
          extra={backLink}
        />
      );
    }
    return (
      <Card>
        <CatalogErrorAlert
          error={error}
          title={t("catalog.detail.loadFailed")}
          onRetry={() => void provider.refetch()}
        />
        {backLink}
      </Card>
    );
  }

  const current = provider.data;

  const finish = (message: string): void => {
    // 只让供应商本身过期（`exact`）：模型与别名在它的键下面，不必跟着重读。
    void queryClient.invalidateQueries({
      queryKey: providerDetailQueryKey(providerId),
      exact: true,
    });
    void queryClient.invalidateQueries({ queryKey: PROVIDER_LISTS_QUERY_KEY });
    setRenaming(false);
    setChangingStatus(false);
    setNotice(message);
  };

  const open = (action: () => void): void => {
    setNotice(null);
    action();
  };

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      {backLink}
      <NoticeAlert message={notice} />
      <Card
        title={current.display_name}
        extra={
          <EntryActions
            status={current.status}
            onRename={() => open(() => setRenaming(true))}
            onChangeStatus={() => open(() => setChangingStatus(true))}
          />
        }
      >
        <ProviderDescriptions provider={current} />
      </Card>

      {/* key：换了供应商，页码、筛选与表单都从头来。 */}
      <ModelsPanel key={`models-${current.id}`} providerId={current.id} />
      <AliasesPanel key={`aliases-${current.id}`} providerId={current.id} />

      {renaming ? (
        <RenameModal
          code={current.code}
          displayName={current.display_name}
          save={(patch) => updateProvider(current.id, patch)}
          onDone={() => finish(t("catalog.rename.done"))}
          onCancel={() => setRenaming(false)}
        />
      ) : null}
      {changingStatus ? (
        <StatusChangeModal
          code={current.code}
          status={current.status}
          save={(patch) => updateProvider(current.id, patch)}
          onDone={(next) => finish(statusDone(next))}
          onCancel={() => setChangingStatus(false)}
        />
      ) : null}
    </Space>
  );
}

function ProviderDescriptions({ provider }: { provider: Provider }) {
  const { t } = useTranslation();
  const items: DescriptionsProps["items"] = [
    { key: "code", label: t("catalog.field.code"), children: <CodeText code={provider.code} /> },
    {
      key: "status",
      label: t("catalog.field.status"),
      children: <CatalogStatusTag status={provider.status} />,
    },
    {
      key: "created_at",
      label: t("catalog.field.createdAt"),
      children: <DateTimeText value={provider.created_at} />,
    },
    {
      key: "updated_at",
      label: t("catalog.field.updatedAt"),
      children: <DateTimeText value={provider.updated_at} />,
    },
  ];
  return <Descriptions bordered size="small" column={1} items={items} />;
}

// ---------------------------------------------------------------------------
// 模型
// ---------------------------------------------------------------------------

function ModelsPanel({ providerId }: { providerId: string }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { status, filter, setStatus, paging } = useStatusFilteredPaging(DEFAULT_PAGE_SIZE);
  const [creating, setCreating] = useState(false);
  const [renaming, setRenaming] = useState<Model | null>(null);
  const [changingStatus, setChangingStatus] = useState<Model | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const statusDone = useStatusDoneMessage();

  const models = useQuery({
    queryKey: modelListQueryKey(providerId, filter, paging.page, paging.pageSize),
    queryFn: ({ signal }) => listModels(providerId, filter, paging.page, paging.pageSize, signal),
    placeholderData: keepPreviousData,
  });

  const finish = (message: string): void => {
    // 映射表单里的模型下拉也在这个前缀下面，一起重读。
    void queryClient.invalidateQueries({ queryKey: providerModelsQueryKey(providerId) });
    setCreating(false);
    setRenaming(null);
    setChangingStatus(null);
    setNotice(message);
  };

  const open = (action: () => void): void => {
    setNotice(null);
    action();
  };

  const columns: TableColumnsType<Model> = [
    {
      key: "code",
      title: t("catalog.field.code"),
      render: (_: unknown, model) => <CodeText code={model.code} />,
    },
    { key: "display_name", title: t("catalog.field.displayName"), dataIndex: "display_name" },
    {
      key: "status",
      title: t("catalog.field.status"),
      render: (_: unknown, model) => <CatalogStatusTag status={model.status} />,
    },
    {
      key: "created_at",
      title: t("catalog.field.createdAt"),
      render: (_: unknown, model) => <DateTimeText value={model.created_at} />,
    },
    {
      key: "actions",
      title: t("catalog.field.actions"),
      render: (_: unknown, model) => (
        <EntryActions
          status={model.status}
          onRename={() => open(() => setRenaming(model))}
          onChangeStatus={() => open(() => setChangingStatus(model))}
        />
      ),
    },
  ];

  let body: ReactNode;
  if (models.isPending) {
    body = <LoadingBlock text={t("catalog.models.loading")} />;
  } else if (models.isError) {
    body = (
      <CatalogErrorAlert
        error={models.error}
        title={t("catalog.models.loadFailed")}
        onRetry={() => void models.refetch()}
      />
    );
  } else if (models.data.total === 0) {
    body = (
      <Empty
        description={
          status === undefined ? t("catalog.models.empty") : t("catalog.models.emptyFiltered")
        }
      />
    );
  } else {
    body = (
      <Table<Model>
        // 同一供应商下模型 `code` 唯一。
        rowKey="code"
        columns={columns}
        dataSource={models.data.items}
        loading={models.isFetching}
        locale={{ emptyText: t("catalog.models.emptyPage") }}
        pagination={{
          current: paging.page,
          pageSize: paging.pageSize,
          total: models.data.total,
          showSizeChanger: true,
          pageSizeOptions: PAGE_SIZE_OPTIONS,
          showTotal: (count) => t("catalog.models.total", { total: count }),
          onChange: paging.goTo,
        }}
      />
    );
  }

  return (
    <Card
      title={t("catalog.models.title")}
      extra={
        <Button onClick={() => open(() => setCreating(true))}>{t("catalog.models.create")}</Button>
      }
    >
      <NoticeAlert message={notice} />
      <div style={{ marginBottom: 16 }}>
        <StatusFilterControl value={status} onChange={setStatus} />
      </div>
      {body}
      {creating ? (
        <CreateModelModal
          providerId={providerId}
          onDone={() => finish(t("catalog.models.created"))}
          onCancel={() => setCreating(false)}
        />
      ) : null}
      {renaming === null ? null : (
        <RenameModal
          code={renaming.code}
          displayName={renaming.display_name}
          save={(patch) => updateModel(providerId, renaming.id, patch)}
          onDone={() => finish(t("catalog.rename.done"))}
          onCancel={() => setRenaming(null)}
        />
      )}
      {changingStatus === null ? null : (
        <StatusChangeModal
          code={changingStatus.code}
          status={changingStatus.status}
          save={(patch) => updateModel(providerId, changingStatus.id, patch)}
          onDone={(next) => finish(statusDone(next))}
          onCancel={() => setChangingStatus(null)}
        />
      )}
    </Card>
  );
}

interface ModelFormValues {
  code: string;
  display_name: string;
}

function CreateModelModal({
  providerId,
  onDone,
  onCancel,
}: {
  providerId: string;
  onDone: () => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const [form] = Form.useForm<ModelFormValues>();
  const [reviewing, setReviewing] = useState<CreateModelBody | null>(null);
  const nameRules = useDisplayNameRules();

  return (
    <>
      <FormStepModal title={t("catalog.models.create")} form={form} onCancel={onCancel}>
        <Form<ModelFormValues>
          form={form}
          layout="vertical"
          initialValues={{ code: "", display_name: "" }}
          // 代码原样（`GPT-4o` 与 `gpt-4o` 是两个模型）；显示名去首尾空白。
          onFinish={(values) =>
            setReviewing({ code: values.code, display_name: values.display_name.trim() })
          }
        >
          <Form.Item
            name="code"
            label={t("catalog.field.code")}
            extra={t("catalog.models.codeHint")}
            rules={codeRules(
              MODEL_CODE_PATTERN,
              t("catalog.form.codeRequired"),
              t("catalog.models.codeInvalid"),
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
          title={t("catalog.models.confirmCreate")}
          confirmLabel={t("catalog.confirmCreate")}
          backLabel={t("catalog.backToEdit")}
          failedTitle={t("catalog.models.createFailed")}
          run={() => createModel(providerId, reviewing)}
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
// 别名段
// ---------------------------------------------------------------------------

export interface AliasGroup {
  alias: string;
  segments: AliasSegment[];
}

/** 把同一页里相邻的同一个字符串归成一组。后端已经按字符串、再按起点排好，这里不重排。 */
export function groupByAlias(items: readonly AliasSegment[]): AliasGroup[] {
  const groups: AliasGroup[] = [];
  for (const item of items) {
    const last = groups.at(-1);
    if (last !== undefined && last.alias === item.alias) {
      last.segments.push(item);
    } else {
      groups.push({ alias: item.alias, segments: [item] });
    }
  }
  return groups;
}

/** 这个字符串当前（未截断）的那一段；都已截断（撤销过）时没有。 */
function currentSegment(group: AliasGroup): AliasSegment | undefined {
  return group.segments.find((segment) => segment.effective_to === null);
}

function AliasesPanel({ providerId }: { providerId: string }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const paging = usePaging(MAX_PAGE_SIZE);
  // `null`：没在映射；`""`：新映射；其余：给这个字符串改映射。
  const [mapping, setMapping] = useState<string | null>(null);
  const [retiring, setRetiring] = useState<AliasSegment | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const aliases = useQuery({
    queryKey: aliasListQueryKey(providerId, {}, paging.page, paging.pageSize),
    queryFn: ({ signal }) => listModelAliases(providerId, {}, paging.page, paging.pageSize, signal),
    placeholderData: keepPreviousData,
  });

  const refreshList = (): void => {
    void queryClient.invalidateQueries({ queryKey: providerAliasesQueryKey(providerId) });
  };

  const finish = (message: string): void => {
    refreshList();
    setMapping(null);
    setRetiring(null);
    setNotice(message);
  };

  const open = (action: () => void): void => {
    setNotice(null);
    action();
  };

  const segmentColumns: TableColumnsType<AliasSegment> = [
    {
      key: "effective_from",
      title: t("catalog.aliases.field.from"),
      render: (_: unknown, segment) =>
        segment.effective_from === null ? (
          t("catalog.aliases.sinceStart")
        ) : (
          <DateTimeText value={segment.effective_from} />
        ),
    },
    {
      key: "effective_to",
      title: t("catalog.aliases.field.until"),
      render: (_: unknown, segment) =>
        segment.effective_to === null ? (
          t("catalog.aliases.noEnd")
        ) : (
          <DateTimeText value={segment.effective_to} />
        ),
    },
    {
      key: "model_code",
      title: t("catalog.aliases.field.model"),
      render: (_: unknown, segment) => <CodeText code={segment.model_code} />,
    },
    {
      key: "state",
      title: t("catalog.aliases.field.state"),
      render: (_: unknown, segment) =>
        segment.effective_to === null ? (
          <Tag color="blue">{t("catalog.aliases.current")}</Tag>
        ) : (
          <Tag>{t("catalog.aliases.ended")}</Tag>
        ),
    },
  ];

  let body: ReactNode;
  if (aliases.isPending) {
    body = <LoadingBlock text={t("catalog.aliases.loading")} />;
  } else if (aliases.isError) {
    body = (
      <CatalogErrorAlert
        error={aliases.error}
        title={t("catalog.aliases.loadFailed")}
        onRetry={() => void aliases.refetch()}
      />
    );
  } else if (aliases.data.total === 0) {
    body = <Empty description={t("catalog.aliases.empty")} />;
  } else {
    const groups = groupByAlias(aliases.data.items);
    body = (
      <>
        {groups.length === 0 ? (
          <Empty description={t("catalog.aliases.emptyPage")} />
        ) : (
          groups.map((group, index) => {
            const live = currentSegment(group);
            return (
              <Card
                // 同一个字符串的段跨页时，下一页的那一组与这一组同名：键里带上位置。
                key={`${String(index)}:${group.alias}`}
                type="inner"
                size="small"
                style={{ marginBottom: 16 }}
                title={<CodeText code={group.alias} />}
                extra={
                  <Space>
                    <Button size="small" onClick={() => open(() => setMapping(group.alias))}>
                      {t("catalog.aliases.remap")}
                    </Button>
                    {live === undefined ? null : (
                      <Button size="small" danger onClick={() => open(() => setRetiring(live))}>
                        {t("catalog.aliases.retire")}
                      </Button>
                    )}
                  </Space>
                }
              >
                <Table<AliasSegment>
                  size="small"
                  // 同一个字符串的各段首尾相接、不是空区间，起点互不相同；只有第一段是 `null`。
                  rowKey={(segment) => segment.effective_from ?? ""}
                  columns={segmentColumns}
                  dataSource={group.segments}
                  pagination={false}
                  loading={aliases.isFetching}
                  onRow={(segment) =>
                    segment.effective_to === null
                      ? { style: { background: CURRENT_SEGMENT_BACKGROUND } }
                      : {}
                  }
                />
              </Card>
            );
          })
        )}
        <Pagination
          current={paging.page}
          pageSize={paging.pageSize}
          total={aliases.data.total}
          showSizeChanger
          pageSizeOptions={PAGE_SIZE_OPTIONS}
          showTotal={(count) => t("catalog.aliases.total", { total: count })}
          onChange={paging.goTo}
        />
      </>
    );
  }

  return (
    <Card
      title={t("catalog.aliases.title")}
      extra={<Button onClick={() => open(() => setMapping(""))}>{t("catalog.aliases.map")}</Button>}
    >
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message={t("catalog.aliases.futureOnly")}
        description={t("catalog.aliases.explanation")}
      />
      <NoticeAlert message={notice} />
      {body}
      {mapping === null ? null : (
        <MapAliasModal
          providerId={providerId}
          alias={mapping}
          onDone={() => finish(t("catalog.aliases.mapped"))}
          onCancel={() => setMapping(null)}
        />
      )}
      {retiring === null ? null : (
        <ConfirmWriteModal
          title={t("catalog.aliases.retireTitle", { alias: retiring.alias })}
          confirmLabel={t("catalog.aliases.confirmRetire")}
          backLabel={t("catalog.cancel")}
          danger
          failedTitle={t("catalog.aliases.retireFailed")}
          run={() => retireModelAlias(providerId, retiring.id)}
          onDone={() => finish(t("catalog.aliases.retired"))}
          onError={(error) => {
            // 不是当前段了（别人刚改过映射或撤销过）：重读，让管理员看到现状。
            if (error instanceof ApiError && error.code === AI_MODEL_ALIAS_NOT_FOUND) {
              refreshList();
            }
          }}
          onBack={() => setRetiring(null)}
        >
          <SummaryList
            items={[
              { key: "alias", label: t("catalog.aliases.field.alias"), children: <CodeText code={retiring.alias} /> },
              { key: "model", label: t("catalog.aliases.field.model"), children: <CodeText code={retiring.model_code} /> },
            ]}
          />
          <Alert type="warning" showIcon message={t("catalog.aliases.retireBody")} />
        </ConfirmWriteModal>
      )}
    </Card>
  );
}

interface MapFormValues {
  alias: string;
  model_id: string | undefined;
}

/**
 * 映射一个字符串到本供应商的某个模型。`alias` 非空时是给已有的字符串改映射，字符串不可改。
 *
 * 模型下拉列出本供应商的前 100 个模型（含已停用的：停用不影响解析，后端也不拦）。超过 100 个时
 * 下拉里写明只列了前 100 个。
 */
function MapAliasModal({
  providerId,
  alias,
  onDone,
  onCancel,
}: {
  providerId: string;
  alias: string;
  onDone: () => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const [form] = Form.useForm<MapFormValues>();
  const [reviewing, setReviewing] = useState<(MapAliasBody & { model_code: string }) | null>(null);

  const models = useQuery({
    queryKey: modelListQueryKey(providerId, {}, 1, MAX_PAGE_SIZE),
    queryFn: ({ signal }) => listModels(providerId, {}, 1, MAX_PAGE_SIZE, signal),
  });

  const options = (models.data?.items ?? []).map((model) => ({
    value: model.id,
    label: model.code,
  }));
  const truncated = models.data !== undefined && models.data.total > models.data.items.length;

  const review = (values: MapFormValues): void => {
    const chosen = models.data?.items.find((model) => model.id === values.model_id);
    if (chosen === undefined) {
      return;
    }
    // 别名原样，不去空白、不改大小写。
    setReviewing({ alias: values.alias, model_id: chosen.id, model_code: chosen.code });
  };

  return (
    <>
      <FormStepModal
        title={alias === "" ? t("catalog.aliases.map") : t("catalog.aliases.remapTitle", { alias })}
        form={form}
        onCancel={onCancel}
      >
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message={t("catalog.aliases.futureOnly")}
        />
        <CatalogErrorAlert
          error={models.error}
          title={t("catalog.models.loadFailed")}
          onRetry={() => void models.refetch()}
        />
        <Form<MapFormValues>
          form={form}
          layout="vertical"
          initialValues={{ alias, model_id: undefined }}
          onFinish={review}
        >
          <Form.Item
            name="alias"
            label={t("catalog.aliases.field.alias")}
            extra={t("catalog.aliases.aliasHint")}
            rules={codeRules(
              MODEL_CODE_PATTERN,
              t("catalog.form.codeRequired"),
              t("catalog.models.codeInvalid"),
            )}
          >
            <Input autoComplete="off" spellCheck={false} disabled={alias !== ""} />
          </Form.Item>
          <Form.Item
            name="model_id"
            label={t("catalog.aliases.field.model")}
            extra={truncated ? t("catalog.aliases.modelsTruncated") : undefined}
            rules={[{ required: true, message: t("catalog.aliases.modelRequired") }]}
          >
            <Select
              showSearch
              optionFilterProp="label"
              loading={models.isPending}
              options={options}
              notFoundContent={t("catalog.aliases.noModels")}
            />
          </Form.Item>
        </Form>
      </FormStepModal>
      {reviewing === null ? null : (
        <ConfirmWriteModal
          title={t("catalog.aliases.confirmMap")}
          confirmLabel={t("catalog.aliases.confirmMapButton")}
          backLabel={t("catalog.backToEdit")}
          failedTitle={t("catalog.aliases.mapFailed")}
          run={() => mapModelAlias(providerId, { alias: reviewing.alias, model_id: reviewing.model_id })}
          onDone={onDone}
          onBack={() => setReviewing(null)}
        >
          <SummaryList
            items={[
              { key: "alias", label: t("catalog.aliases.field.alias"), children: <CodeText code={reviewing.alias} /> },
              { key: "model", label: t("catalog.aliases.field.model"), children: <CodeText code={reviewing.model_code} /> },
            ]}
          />
          <Alert type="warning" showIcon message={t("catalog.aliases.explanation")} />
        </ConfirmWriteModal>
      )}
    </>
  );
}
