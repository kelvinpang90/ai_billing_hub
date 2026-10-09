/**
 * 管理端供应商价格列表（`GET` / `POST /api/v1/admin/provider-prices`，docs/api.md「管理端供应商价格」）。
 *
 * 顶栏的「Provider prices」进这一页。列表分页，按供应商、模型、生效起点排序（由后端决定，前端不重排），
 * 可按供应商、模型、状态筛。三种状态都画出来（spec §132 DoD 第 8 条）：加载中、失败（带 request_id）、
 * 空列表。页面写明「成本价，原币种，客户不可见」。
 *
 * 这一页只建草稿；编辑、发布、退役、丢弃都在详情页（{@link ./ProviderPriceDetailPage}）。
 *
 * 本文件后半是价格与汇率两组页面共用的部件（错误提示、二次确认、发布、退役、状态标签、区间显示）。
 * 公共组件目录只开放了 DateTimeText / DecimalText 两个文件，所以照目录页 `ProvidersPage` 的先例放在
 * 这里导出。
 *
 * 写操作一律二次确认：先填表（或点按钮），再在确认框里点确认才发请求。确认进行中禁用按钮，并用一个
 * ref 挡住第二次点击。成功后让相关列表过期重读，不在前端拼行。
 *
 * ⚠️ 提示一律不用 antd 的 `Alert` 画「说明」：它带 `role="alert"`，部署后浏览器验收把页面上任何
 * `role="alert"` 都当成错误提示（scripts/acceptance/admin_customers.mjs）。说明用普通段落。
 */

import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Card,
  Empty,
  Form,
  Input,
  Modal,
  Radio,
  Select,
  Space,
  Table,
  Tag,
  Typography,
  type FormInstance,
  type FormRule,
  type TableColumnsType,
} from "antd";
import { useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import {
  AI_MODEL_NOT_FOUND,
  AI_PROVIDER_NOT_FOUND,
  PROVIDER_LISTS_QUERY_KEY,
  VALIDATION_ERROR,
  allModelsQueryKey,
  listAllModels,
  listProviders,
  type Model,
  type Provider,
} from "../../api/adminCatalog";
import { ADMIN_REQUIRED, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE } from "../../api/adminCustomers";
import {
  FX_RATE_FINAL,
  FX_RATE_NOT_DRAFT,
  FX_RATE_NOT_EDITABLE,
  FX_RATE_NOT_FOUND,
  FX_RATE_NOT_RETIRABLE,
} from "../../api/adminFxRates";
import {
  CATALOG_ITEM_RETIRED,
  CURRENCY_PATTERN,
  EFFECTIVE_FROM_CONFLICT,
  EFFECTIVE_FROM_IN_PAST,
  PRICE_VERSION_FINAL,
  PRICE_VERSION_INCOMPLETE,
  PRICE_VERSION_NOT_DRAFT,
  PRICE_VERSION_NOT_FOUND,
  PRICE_VERSION_NOT_RETIRABLE,
  PRICE_VERSION_STATUSES,
  PROVIDER_PRICE_LISTS_QUERY_KEY,
  TEXT_MAX,
  USAGE_METER_COMPONENT_NOT_FOUND,
  createProviderPrice,
  listProviderPrices,
  providerPriceListQueryKey,
  type ComponentRateBody,
  type CreatePriceBody,
  type PriceFilter,
  type PriceVersion,
  type PriceVersionStatus,
} from "../../api/adminProviderPrices";
import { ApiError } from "../../api/client";
import { DateTimeText, displayTimeToRfc3339, parseUtc } from "../../components/DateTimeText";
import { DecimalText } from "../../components/DecimalText";
import { RequestReference } from "../../components/RequestReference";
import { providerPriceDetailPath } from "../../routes/paths";
import {
  CodeText,
  LoadingBlock,
  NoticeAlert,
  PAGE_SIZE_OPTIONS,
  SummaryList,
  usePaging,
} from "../catalog/ProvidersPage";
import {
  ComponentRatesInput,
  toComponentBodies,
  useAllMeterTypes,
  useComponentRatesRules,
  type RateRow,
} from "./ComponentRatesInput";

/** 筛选里的「全部状态」。不进查询串。 */
const ALL = "ALL";

export function ProviderPricesPage() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const paging = usePaging(DEFAULT_PAGE_SIZE);
  const [filterForm] = Form.useForm<FilterValues>();
  const [filter, setFilter] = useState<PriceFilter>({});
  const [creating, setCreating] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const providers = useAllProviders();
  const filterProviderId = filter.provider_id;
  const models = useQuery({
    queryKey: allModelsQueryKey(filterProviderId ?? ""),
    queryFn: ({ signal }) => listAllModels(filterProviderId ?? "", signal),
    enabled: filterProviderId !== undefined,
  });

  const prices = useQuery({
    queryKey: providerPriceListQueryKey(filter, paging.page, paging.pageSize),
    queryFn: ({ signal }) => listProviderPrices(filter, paging.page, paging.pageSize, signal),
    // 翻页时先留着上一页，不让整张表闪成骨架屏。
    placeholderData: keepPreviousData,
  });

  const changeFilter = (changed: Partial<FilterValues>, all: FilterValues): void => {
    const values = { ...all };
    // 换了供应商，原来选的模型就不属于它了。
    if ("provider_id" in changed) {
      values.model_id = undefined;
      filterForm.setFieldValue("model_id", undefined);
    }
    setFilter(toPriceFilter(values));
    paging.reset();
  };

  const finish = (message: string): void => {
    void queryClient.invalidateQueries({ queryKey: PROVIDER_PRICE_LISTS_QUERY_KEY });
    setCreating(false);
    setNotice(message);
  };

  const columns: TableColumnsType<PriceVersion> = [
    {
      key: "provider",
      title: t("pricing.field.provider"),
      render: (_: unknown, version) => <CodeText code={version.provider_code} />,
    },
    {
      key: "model",
      title: t("pricing.field.model"),
      render: (_: unknown, version) => (
        <Link to={providerPriceDetailPath(version.id)}>
          <Typography.Text code>{version.model_code}</Typography.Text>
        </Link>
      ),
    },
    { key: "currency", title: t("pricing.field.currency"), dataIndex: "source_currency" },
    {
      key: "status",
      title: t("pricing.field.status"),
      render: (_: unknown, version) => <VersionStatusTag status={version.status} />,
    },
    {
      key: "effective_from",
      title: t("pricing.field.effectiveFrom"),
      render: (_: unknown, version) => <RangeStart version={version} />,
    },
    {
      key: "effective_to",
      title: t("pricing.field.effectiveTo"),
      render: (_: unknown, version) => <RangeEnd version={version} />,
    },
    {
      key: "components",
      title: t("pricing.field.components"),
      render: (_: unknown, version) => (
        <Typography.Text type="secondary">
          {version.components.map((component) => component.component_code).join(", ")}
        </Typography.Text>
      ),
    },
  ];

  const filtered =
    filter.provider_id !== undefined || filter.model_id !== undefined || filter.status !== undefined;

  let body: ReactNode;
  if (prices.isPending) {
    body = <LoadingBlock text={t("pricing.prices.loading")} />;
  } else if (prices.isError) {
    body = (
      <PricingErrorAlert
        error={prices.error}
        title={t("pricing.prices.loadFailed")}
        onRetry={() => void prices.refetch()}
      />
    );
  } else if (prices.data.total === 0) {
    body = (
      <Empty
        description={filtered ? t("pricing.prices.emptyFiltered") : t("pricing.prices.empty")}
      />
    );
  } else {
    body = (
      <Table<PriceVersion>
        rowKey="id"
        columns={columns}
        dataSource={prices.data.items}
        loading={prices.isFetching}
        // 页码超过末页时后端回空 items、total 照报：说清楚是「这一页没有」。
        locale={{ emptyText: t("pricing.prices.emptyPage") }}
        pagination={{
          current: paging.page,
          pageSize: paging.pageSize,
          total: prices.data.total,
          showSizeChanger: true,
          pageSizeOptions: PAGE_SIZE_OPTIONS,
          showTotal: (count) => t("pricing.prices.total", { total: count }),
          onChange: paging.goTo,
        }}
      />
    );
  }

  return (
    <Card
      title={t("pricing.prices.title")}
      extra={
        <Button
          type="primary"
          onClick={() => {
            setNotice(null);
            setCreating(true);
          }}
        >
          {t("pricing.prices.create")}
        </Button>
      }
    >
      <CostNotice />
      <NoticeAlert message={notice} />
      <PricingErrorAlert
        error={providers.error ?? models.error}
        title={t("pricing.form.catalogFailed")}
        onRetry={() => {
          void providers.refetch();
          if (filterProviderId !== undefined) {
            void models.refetch();
          }
        }}
      />
      <Form<FilterValues>
        // 每个表单都有名字：字段的 id 是「表单名_字段名」，筛选与建草稿的同名字段不会撞上同一个 id。
        name="price-filter"
        form={filterForm}
        layout="inline"
        style={{ marginBottom: 16, rowGap: 8 }}
        initialValues={{ provider_id: undefined, model_id: undefined, status: ALL }}
        onValuesChange={changeFilter}
      >
        <Form.Item name="provider_id" label={t("pricing.field.provider")}>
          <Select
            allowClear
            showSearch
            optionFilterProp="label"
            style={{ minWidth: 200 }}
            placeholder={t("pricing.filter.anyProvider")}
            loading={providers.isPending}
            options={(providers.data ?? []).map((provider) => ({ value: provider.id, label: provider.code }))}
          />
        </Form.Item>
        <Form.Item name="model_id" label={t("pricing.field.model")}>
          <Select
            allowClear
            showSearch
            optionFilterProp="label"
            style={{ minWidth: 200 }}
            placeholder={t("pricing.filter.anyModel")}
            disabled={filterProviderId === undefined}
            loading={models.isFetching}
            options={(models.data ?? []).map((model) => ({ value: model.id, label: model.code }))}
          />
        </Form.Item>
        <Form.Item name="status" label={t("pricing.field.status")}>
          <Radio.Group
            optionType="button"
            options={[
              { value: ALL, label: t("pricing.filter.all") },
              { value: "DRAFT", label: t("pricing.status.draft") },
              { value: "PUBLISHED", label: t("pricing.status.published") },
              { value: "RETIRED", label: t("pricing.status.retired") },
              { value: "DISCARDED", label: t("pricing.status.discarded") },
            ]}
          />
        </Form.Item>
      </Form>
      {body}
      {creating ? (
        <CreateDraftModal
          onDone={() => finish(t("pricing.create.done"))}
          onCancel={() => setCreating(false)}
        />
      ) : null}
    </Card>
  );
}

interface FilterValues {
  provider_id: string | undefined;
  model_id: string | undefined;
  status: string;
}

function isPriceStatus(value: string): value is PriceVersionStatus {
  return (PRICE_VERSION_STATUSES as readonly string[]).includes(value);
}

/** 表单值 → 查询条件。没选的不带（查询串里也就没有它）。 */
function toPriceFilter(values: FilterValues): PriceFilter {
  return {
    ...(values.provider_id === undefined ? {} : { provider_id: values.provider_id }),
    ...(values.provider_id === undefined || values.model_id === undefined ? {} : { model_id: values.model_id }),
    ...(isPriceStatus(values.status) ? { status: values.status } : {}),
  };
}

/** 「成本价，原币种，客户不可见」。 */
export function CostNotice() {
  const { t } = useTranslation();
  return (
    <Typography.Paragraph>
      <Tag color="orange">{t("pricing.prices.costTag")}</Tag>
      <Typography.Text type="secondary">{t("pricing.prices.costNote")}</Typography.Text>
    </Typography.Paragraph>
  );
}

/** 全部供应商（含已停用的），逐页取完，`code` 升序。挂在供应商列表的前缀下，目录页改了它跟着过期。 */
export async function listAllProviders(signal?: AbortSignal): Promise<Provider[]> {
  const providers: Provider[] = [];
  for (let page = 1; ; page += 1) {
    const result = await listProviders({}, page, MAX_PAGE_SIZE, signal);
    providers.push(...result.items);
    if (result.items.length === 0 || providers.length >= result.total) {
      return providers;
    }
  }
}

function useAllProviders() {
  return useQuery({
    queryKey: [...PROVIDER_LISTS_QUERY_KEY, "all"],
    queryFn: ({ signal }) => listAllProviders(signal),
  });
}

// ---------------------------------------------------------------------------
// 建草稿
// ---------------------------------------------------------------------------

interface DraftValues {
  provider_id: string | undefined;
  model_id: string | undefined;
  source_currency: string;
  source_reference: string;
  components: RateRow[];
}

interface DraftReview {
  body: CreatePriceBody;
  provider: Provider;
  model: Model;
}

/**
 * 建草稿：选供应商与模型（只列启用中的：停用的建草稿是 409）、原币种、来源说明，分量按计量类型成组。
 * 填表 → 确认 → 发请求。重发会建出两个草稿，所以确认中防双击。
 */
function CreateDraftModal({ onDone, onCancel }: { onDone: () => void; onCancel: () => void }) {
  const { t } = useTranslation();
  const [form] = Form.useForm<DraftValues>();
  const [reviewing, setReviewing] = useState<DraftReview | null>(null);
  const providerId = Form.useWatch("provider_id", form);
  const providers = useAllProviders();
  const models = useQuery({
    queryKey: allModelsQueryKey(providerId ?? ""),
    queryFn: ({ signal }) => listAllModels(providerId ?? "", signal),
    enabled: providerId !== undefined,
  });
  const meterTypes = useAllMeterTypes();
  const meterTypeList = meterTypes.data ?? [];
  const componentRules = useComponentRatesRules(meterTypeList);
  const currencyRules = useCurrencyRules();
  const referenceRules = useTextRules(t("pricing.form.referenceRequired"), t("pricing.form.referenceTooLong"));

  const review = (values: DraftValues): void => {
    const provider = providers.data?.find((candidate) => candidate.id === values.provider_id);
    const model = models.data?.find((candidate) => candidate.id === values.model_id);
    if (provider === undefined || model === undefined) {
      return;
    }
    setReviewing({
      provider,
      model,
      body: {
        provider_id: provider.id,
        model_id: model.id,
        source_currency: values.source_currency,
        source_reference: values.source_reference.trim(),
        components: toComponentBodies(values.components),
      },
    });
  };

  return (
    <>
      <FormModal title={t("pricing.create.title")} form={form} width={760} onCancel={onCancel}>
        <CostNotice />
        <PricingErrorAlert
          error={providers.error ?? models.error ?? meterTypes.error}
          title={t("pricing.form.catalogFailed")}
        />
        <Form<DraftValues>
          name="price-draft"
          form={form}
          layout="vertical"
          initialValues={{
            provider_id: undefined,
            model_id: undefined,
            source_currency: "USD",
            source_reference: "",
            components: [],
          }}
          onValuesChange={(changed: Partial<DraftValues>) => {
            if ("provider_id" in changed) {
              form.setFieldValue("model_id", undefined);
            }
          }}
          onFinish={review}
        >
          <Form.Item
            name="provider_id"
            label={t("pricing.field.provider")}
            rules={[{ required: true, message: t("pricing.form.providerRequired") }]}
          >
            <Select
              showSearch
              optionFilterProp="label"
              loading={providers.isPending}
              options={(providers.data ?? [])
                .filter((provider) => provider.status === "ACTIVE")
                .map((provider) => ({ value: provider.id, label: provider.code }))}
            />
          </Form.Item>
          <Form.Item
            name="model_id"
            label={t("pricing.field.model")}
            rules={[{ required: true, message: t("pricing.form.modelRequired") }]}
          >
            <Select
              showSearch
              optionFilterProp="label"
              disabled={providerId === undefined}
              loading={models.isFetching}
              notFoundContent={t("pricing.form.noModels")}
              options={(models.data ?? [])
                .filter((model) => model.status === "ACTIVE")
                .map((model) => ({ value: model.id, label: model.code }))}
            />
          </Form.Item>
          <Form.Item
            name="source_currency"
            label={t("pricing.field.currency")}
            extra={t("pricing.form.currencyHint")}
            rules={currencyRules}
          >
            <Input autoComplete="off" spellCheck={false} maxLength={3} />
          </Form.Item>
          <Form.Item
            name="source_reference"
            label={t("pricing.field.sourceReference")}
            extra={t("pricing.form.referenceHint")}
            rules={referenceRules}
          >
            <Input autoComplete="off" />
          </Form.Item>
          <Form.Item name="components" label={t("pricing.field.components")} rules={componentRules}>
            {meterTypes.isPending ? (
              <LoadingBlock text={t("pricing.components.loading")} />
            ) : (
              <ComponentRatesInput meterTypes={meterTypeList} />
            )}
          </Form.Item>
        </Form>
      </FormModal>
      {reviewing === null ? null : (
        <WriteConfirmModal
          title={t("pricing.create.confirmTitle")}
          confirmLabel={t("pricing.create.confirm")}
          backLabel={t("pricing.backToEdit")}
          failedTitle={t("pricing.create.failed")}
          width={760}
          run={() => createProviderPrice(reviewing.body)}
          onDone={onDone}
          onBack={() => setReviewing(null)}
        >
          <SummaryList
            items={[
              { key: "provider", label: t("pricing.field.provider"), children: <CodeText code={reviewing.provider.code} /> },
              { key: "model", label: t("pricing.field.model"), children: <CodeText code={reviewing.model.code} /> },
              { key: "currency", label: t("pricing.field.currency"), children: reviewing.body.source_currency },
              { key: "reference", label: t("pricing.field.sourceReference"), children: reviewing.body.source_reference },
            ]}
          />
          <ComponentBodiesTable components={reviewing.body.components} />
          <Typography.Paragraph type="secondary" style={{ marginTop: 16 }}>
            {t("pricing.create.draftNote")}
          </Typography.Paragraph>
        </WriteConfirmModal>
      )}
    </>
  );
}

/** 确认框里列出要提交的分量：两个数按十进制字符串显示，不经 number。 */
export function ComponentBodiesTable({ components }: { components: readonly ComponentRateBody[] }) {
  const { t } = useTranslation();
  const columns: TableColumnsType<ComponentRateBody> = [
    {
      key: "component_code",
      title: t("pricing.components.componentCode"),
      render: (_: unknown, component) => <CodeText code={component.component_code} />,
    },
    {
      key: "unit_quantity",
      title: t("pricing.components.unitQuantity"),
      render: (_: unknown, component) => <DecimalText value={component.unit_quantity} />,
    },
    {
      key: "rate_amount",
      title: t("pricing.components.rateAmount"),
      render: (_: unknown, component) => <DecimalText value={component.rate_amount} />,
    },
  ];
  return (
    <Table<ComponentRateBody>
      size="small"
      rowKey="component_code"
      columns={columns}
      dataSource={[...components]}
      pagination={false}
    />
  );
}

// ---------------------------------------------------------------------------
// 价格与汇率共用
// ---------------------------------------------------------------------------

/**
 * 价格与汇率接口出错时的提示。
 *
 * 403 `ADMIN_REQUIRED` 单独说「没有权限」—— 前端不做角色判断，是不是管理员只看后端这一句。
 * 两节已知的 404 / 409 / 422 码显示对应的文案；认不出的码用调用方给的标题。两种情况下面都带后端
 * message 与 request_id（`RequestReference`），所以未知码也看得到后端的说法（例如发布不完整时缺哪些分量）。
 */
export function PricingErrorAlert({
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
      message = t("pricing.forbidden");
      break;
    case PRICE_VERSION_NOT_FOUND:
      message = t("pricing.error.priceNotFound");
      break;
    case AI_PROVIDER_NOT_FOUND:
      message = t("pricing.error.providerNotFound");
      break;
    case AI_MODEL_NOT_FOUND:
      message = t("pricing.error.modelNotFound");
      break;
    case USAGE_METER_COMPONENT_NOT_FOUND:
      message = t("pricing.error.componentNotFound");
      break;
    case PRICE_VERSION_NOT_DRAFT:
      message = t("pricing.error.priceNotDraft");
      break;
    case PRICE_VERSION_INCOMPLETE:
      message = t("pricing.error.priceIncomplete");
      break;
    case CATALOG_ITEM_RETIRED:
      message = t("pricing.error.catalogRetired");
      break;
    case PRICE_VERSION_NOT_RETIRABLE:
      message = t("pricing.error.priceNotRetirable");
      break;
    case PRICE_VERSION_FINAL:
    case FX_RATE_FINAL:
      message = t("pricing.error.final");
      break;
    case EFFECTIVE_FROM_CONFLICT:
      message = t("pricing.error.effectiveFromConflict");
      break;
    case EFFECTIVE_FROM_IN_PAST:
      message = t("pricing.error.effectiveFromInPast");
      break;
    case FX_RATE_NOT_FOUND:
      message = t("pricing.error.fxNotFound");
      break;
    case FX_RATE_NOT_DRAFT:
      message = t("pricing.error.fxNotDraft");
      break;
    case FX_RATE_NOT_EDITABLE:
      message = t("pricing.error.fxNotEditable");
      break;
    case FX_RATE_NOT_RETIRABLE:
      message = t("pricing.error.fxNotRetirable");
      break;
    case VALIDATION_ERROR:
      message = t("pricing.error.validation");
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
            {t("pricing.retry")}
          </Button>
        )
      }
    />
  );
}

/** 状态标签。认不出的状态原样显示，不猜。 */
export function VersionStatusTag({ status }: { status: string }) {
  const { t } = useTranslation();
  switch (status) {
    case "DRAFT":
      return <Tag color="blue">{t("pricing.status.draft")}</Tag>;
    case "PUBLISHED":
      return <Tag color="green">{t("pricing.status.published")}</Tag>;
    case "RETIRED":
      return <Tag>{t("pricing.status.retired")}</Tag>;
    case "DISCARDED":
      return <Tag>{t("pricing.status.discarded")}</Tag>;
    default:
      return <Tag>{status}</Tag>;
  }
}

/** 有区间的版本（价格版本与汇率版本都是这个形状）。 */
interface Ranged {
  status: string;
  effective_from: string | null;
  effective_to: string | null;
}

/** 发布过的状态：只有它们的 `null` 端点有含义（「一直以来」/「仍生效」）。 */
function wasPublished(version: Ranged): boolean {
  return version.status === "PUBLISHED" || version.status === "RETIRED";
}

/** 区间起点。已发布的 `null` =「一直以来」；草稿与丢弃的没有起点。 */
export function RangeStart({ version }: { version: Ranged }) {
  const { t } = useTranslation();
  if (version.effective_from !== null) {
    return <DateTimeText value={version.effective_from} />;
  }
  return <>{wasPublished(version) ? t("pricing.range.sinceStart") : t("pricing.range.notPublished")}</>;
}

/** 区间终点。已发布的 `null` =「仍生效」；起点与终点相等是空区间（撤销的预约），永不生效。 */
export function RangeEnd({ version }: { version: Ranged }) {
  const { t } = useTranslation();
  if (version.effective_to !== null) {
    return (
      <Space size={4} wrap>
        <DateTimeText value={version.effective_to} />
        {version.effective_from === version.effective_to ? (
          <Tag>{t("pricing.range.neverInEffect")}</Tag>
        ) : null}
      </Space>
    );
  }
  return <>{wasPublished(version) ? t("pricing.range.noEnd") : t("pricing.range.notPublished")}</>;
}

/** 已发布、未截断：可以退役（已开始的截断于此刻，尚未开始的预约被撤销）。被后继截断的历史版本不行。 */
export function isRetirable(version: Ranged): boolean {
  return version.status === "PUBLISHED" && version.effective_to === null;
}

/** 按码点数，与 Python 的 `len()` 一致（`.length` 数的是 UTF-16 单元，emoji 算 2）。 */
function trimmedLength(value: unknown): number {
  return typeof value === "string" ? [...value.trim()].length : 0;
}

/** 来源说明与退役原因：去首尾空白后 1–255。 */
export function useTextRules(requiredMessage: string, tooLongMessage: string): FormRule[] {
  return [
    { required: true, whitespace: true, message: requiredMessage },
    {
      validator: (_rule, value: unknown) =>
        trimmedLength(value) > TEXT_MAX ? Promise.reject(new Error(tooLongMessage)) : Promise.resolve(),
    },
  ];
}

/** 原币种：ISO 4217 大写三字母，不替用户改大小写（`usd` 后端是 422，这里也就直接说它不对）。 */
function useCurrencyRules(): FormRule[] {
  const { t } = useTranslation();
  return [
    { required: true, message: t("pricing.form.currencyInvalid") },
    { pattern: CURRENCY_PATTERN, message: t("pricing.form.currencyInvalid") },
  ];
}

/** 写操作的第一步：填表。底部是「取消」与「检查」；点「检查」走表单校验，通过后由调用方打开确认框。 */
export function FormModal<Values>({
  title,
  form,
  width,
  onCancel,
  children,
}: {
  title: string;
  form: FormInstance<Values>;
  width?: number | undefined;
  onCancel: () => void;
  children: ReactNode;
}) {
  const { t } = useTranslation();
  return (
    <Modal
      open
      title={title}
      width={width ?? 520}
      onCancel={onCancel}
      maskClosable={false}
      footer={[
        <Button key="cancel" onClick={onCancel}>
          {t("pricing.cancel")}
        </Button>,
        <Button key="review" type="primary" onClick={() => form.submit()}>
          {t("pricing.review")}
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
 * 进行中禁用两个按钮、不许关闭，并用 ref 挡住第二次点击（`isPending` 要等下一次渲染才变 true）。
 * 失败时留在确认框里显示错误（可以返回去改）；成功后交给 `onDone`，由调用方关掉对话框并让列表过期重读。
 */
export function WriteConfirmModal<Result>({
  title,
  confirmLabel,
  backLabel,
  danger,
  failedTitle,
  width,
  run,
  onDone,
  onBack,
  children,
}: {
  title: string;
  confirmLabel: string;
  backLabel: string;
  danger?: boolean | undefined;
  failedTitle: string;
  width?: number | undefined;
  run: () => Promise<Result>;
  onDone: (result: Result) => void;
  onBack: () => void;
  children: ReactNode;
}) {
  const inFlight = useRef(false);
  const write = useMutation({
    mutationFn: run,
    onSuccess: (result) => onDone(result),
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
      width={width ?? 520}
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
        <PricingErrorAlert error={write.error} title={failedTitle} />
      </div>
    </Modal>
  );
}

interface PublishValues {
  when: "now" | "scheduled";
  at: string;
}

/**
 * 发布：现在，或预约一个时刻。预约时刻按吉隆坡时间输入，提交前换成带时区的 RFC 3339、整秒
 * （`displayTimeToRfc3339`）；「现在」时请求体是 `{}`。填表 → 确认 → 发请求。
 *
 * 价格与汇率共用：`run` 收到 `undefined` 表示不指定时刻。
 */
export function PublishModal({
  subject,
  run,
  onDone,
  onCancel,
}: {
  subject: ReactNode;
  run: (effectiveFrom: string | undefined) => Promise<unknown>;
  onDone: () => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const [form] = Form.useForm<PublishValues>();
  const when = Form.useWatch("when", form);
  // `null`：还在填表；`{ effectiveFrom: undefined }`：现在发布。
  const [reviewing, setReviewing] = useState<{ effectiveFrom: string | undefined } | null>(null);

  const atRule: FormRule = {
    validator: (_rule, value: unknown) =>
      typeof value === "string" && displayTimeToRfc3339(value.trim()) !== null
        ? Promise.resolve()
        : Promise.reject(new Error(t("pricing.publish.atInvalid"))),
  };

  const review = (values: PublishValues): void => {
    if (values.when === "now") {
      setReviewing({ effectiveFrom: undefined });
      return;
    }
    const effectiveFrom = displayTimeToRfc3339(values.at.trim());
    if (effectiveFrom !== null) {
      setReviewing({ effectiveFrom });
    }
  };

  const scheduled = reviewing?.effectiveFrom;

  return (
    <>
      <FormModal title={t("pricing.publish.title")} form={form} onCancel={onCancel}>
        <Typography.Paragraph>{subject}</Typography.Paragraph>
        <Form<PublishValues>
          name="publish"
          form={form}
          layout="vertical"
          initialValues={{ when: "now", at: "" }}
          onFinish={review}
        >
          <Form.Item name="when" label={t("pricing.publish.when")}>
            <Radio.Group
              options={[
                { value: "now", label: t("pricing.publish.now") },
                { value: "scheduled", label: t("pricing.publish.scheduled") },
              ]}
            />
          </Form.Item>
          {when === "scheduled" ? (
            <Form.Item name="at" label={t("pricing.publish.at")} rules={[atRule]}>
              <Input type="datetime-local" step={1} />
            </Form.Item>
          ) : null}
          <Typography.Paragraph type="secondary">
            {when === "scheduled" ? t("pricing.publish.scheduledBody") : t("pricing.publish.nowBody")}
          </Typography.Paragraph>
        </Form>
      </FormModal>
      {reviewing === null ? null : (
        <WriteConfirmModal
          title={t("pricing.publish.confirmTitle")}
          confirmLabel={t("pricing.publish.confirm")}
          backLabel={t("pricing.backToEdit")}
          failedTitle={t("pricing.publish.failed")}
          run={() => run(reviewing.effectiveFrom)}
          onDone={onDone}
          onBack={() => setReviewing(null)}
        >
          <Typography.Paragraph>{subject}</Typography.Paragraph>
          <SummaryList
            items={[
              {
                key: "when",
                label: t("pricing.publish.when"),
                children:
                  scheduled === undefined ? (
                    t("pricing.publish.now")
                  ) : (
                    <ScheduledTime value={scheduled} />
                  ),
              },
            ]}
          />
          <Typography.Paragraph type="secondary">
            {scheduled === undefined ? t("pricing.publish.nowBody") : t("pricing.publish.scheduledBody")}
          </Typography.Paragraph>
        </WriteConfirmModal>
      )}
    </>
  );
}

/** 预约时刻：吉隆坡时间给人看，下面一行是原样提交的 RFC 3339 串。 */
function ScheduledTime({ value }: { value: string }) {
  const { t } = useTranslation();
  // `…Z` 去掉 `Z` 就是后端那种不带时区的 UTC 写法，交给 DateTimeText 换成吉隆坡时间。
  const naive = value.endsWith("Z") ? value.slice(0, -1) : value;
  return (
    <Space direction="vertical" size={0}>
      {parseUtc(naive) === null ? value : <DateTimeText value={naive} />}
      <Typography.Text type="secondary">
        {t("pricing.publish.sentAs")} <Typography.Text code>{value}</Typography.Text>
      </Typography.Text>
    </Space>
  );
}

interface ReasonValues {
  reason: string;
}

/**
 * 退役：填原因（记在审计上）→ 确认 → 发请求。`body` 说清楚退役对计费意味着什么（价格与汇率不同）。
 */
export function RetireModal({
  subject,
  body,
  run,
  onDone,
  onCancel,
}: {
  subject: ReactNode;
  body: string;
  run: (reason: string) => Promise<unknown>;
  onDone: () => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const [form] = Form.useForm<ReasonValues>();
  const [reviewing, setReviewing] = useState<string | null>(null);
  const reasonRules = useTextRules(t("pricing.retire.reasonRequired"), t("pricing.retire.reasonTooLong"));

  return (
    <>
      <FormModal title={t("pricing.retire.title")} form={form} onCancel={onCancel}>
        <Typography.Paragraph>{subject}</Typography.Paragraph>
        <Typography.Paragraph type="secondary">{body}</Typography.Paragraph>
        <Form<ReasonValues>
          name="retire"
          form={form}
          layout="vertical"
          initialValues={{ reason: "" }}
          onFinish={(values) => setReviewing(values.reason.trim())}
        >
          <Form.Item
            name="reason"
            label={t("pricing.retire.reason")}
            extra={t("pricing.retire.reasonHint")}
            rules={reasonRules}
          >
            <Input.TextArea rows={3} />
          </Form.Item>
        </Form>
      </FormModal>
      {reviewing === null ? null : (
        <WriteConfirmModal
          title={t("pricing.retire.confirmTitle")}
          confirmLabel={t("pricing.retire.confirm")}
          backLabel={t("pricing.backToEdit")}
          danger
          failedTitle={t("pricing.retire.failed")}
          run={() => run(reviewing)}
          onDone={onDone}
          onBack={() => setReviewing(null)}
        >
          <Typography.Paragraph>{subject}</Typography.Paragraph>
          <SummaryList items={[{ key: "reason", label: t("pricing.retire.reason"), children: reviewing }]} />
          <Typography.Paragraph type="secondary">{body}</Typography.Paragraph>
        </WriteConfirmModal>
      )}
    </>
  );
}
