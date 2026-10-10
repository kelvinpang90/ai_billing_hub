/**
 * 管理端定价规则列表（`GET` / `POST /api/v1/admin/pricing-rules`，docs/api.md「管理端定价规则」）。
 *
 * 顶栏的「Pricing rules」进这一页。列表就是 spec §59 的「价格历史」：按范围（§16 的五级）、生效起点排序（由后端
 * 决定，前端不重排），可按范围、客户、供应商、模型、状态筛。三种状态都画出来（spec §132 DoD 第 8 条）：加载中、
 * 失败（带 request_id）、空列表。**价格一律是 MYR 含税价**（ADR-0008），每个出现价格的地方都写明「含税」。
 *
 * 这一页只建草稿：先选范围（`priority_scope`），再只显示这一级要的客户 / 供应商 / 模型（与后端的范围 / NULL
 * 组合一致，见 `SCOPE_FIELDS`）；MARKUP 只填倍数，FIXED_RATE 按计量类型成组录入分量（ComponentRatesInput）。
 * 编辑、发布、停用、丢弃都在详情页（{@link ./PricingRuleDetailPage}）。
 *
 * 本文件后半是规则详情页与试算页共用的部件（错误提示、二次确认、范围与策略的显示、客户下拉）。价格页的同类部件
 * （ProviderPricesPage 的 `PricingErrorAlert` / `WriteConfirmModal`）只认价格与汇率的错误码，规则的 409 在那里
 * 只会显示成笼统的失败，所以这里另有一份认规则错误码的。
 *
 * 写操作一律二次确认：先填表，再在确认框里点确认才发请求。确认进行中禁用按钮，并用一个 ref 挡住第二次点击。
 * 成功后让相关列表过期重读，不在前端拼行。
 *
 * ⚠️ 提示一律不用 antd 的 `Alert` 画「说明」：它带 `role="alert"`，部署后浏览器验收把页面上任何
 * `role="alert"` 都当成错误提示（scripts/acceptance/admin_customers.mjs）。说明用普通段落。
 *
 * ⚠️ 倍数与单价按十进制字符串显示（DecimalText），校验只用正则，不经 number。
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
  Table,
  Tag,
  Typography,
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
  USAGE_METER_TYPE_NOT_FOUND,
  VALIDATION_ERROR,
  allModelsQueryKey,
  listAllModels,
  type Model,
  type Provider,
} from "../../api/adminCatalog";
import {
  ADMIN_REQUIRED,
  CUSTOMER_LISTS_QUERY_KEY,
  CUSTOMER_NOT_FOUND,
  DEFAULT_PAGE_SIZE,
  MAX_PAGE_SIZE,
  listCustomers,
  type CustomerSummary,
} from "../../api/adminCustomers";
import { AMOUNT_OUT_OF_RANGE } from "../../api/adminPricingPreview";
import {
  PRICING_RULE_FINAL,
  PRICING_RULE_INCOMPLETE,
  PRICING_RULE_LISTS_QUERY_KEY,
  PRICING_RULE_NOT_DRAFT,
  PRICING_RULE_NOT_FOUND,
  PRICING_RULE_NOT_RETIRABLE,
  PRIORITY_SCOPES,
  SCOPE_FIELDS,
  createPricingRule,
  isPricingRuleStatus,
  isPriorityScope,
  listPricingRules,
  pricingRuleListQueryKey,
  type CreatePricingRuleBody,
  type PriorityScope,
  type PricingRule,
  type PricingRuleFilter,
  type PricingStrategy,
  type RuleComponentBody,
} from "../../api/adminPricingRules";
import {
  CATALOG_ITEM_RETIRED,
  EFFECTIVE_FROM_CONFLICT,
  EFFECTIVE_FROM_IN_PAST,
  PRICE_DECIMAL_PATTERN,
  USAGE_METER_COMPONENT_NOT_FOUND,
} from "../../api/adminProviderPrices";
import { ApiError } from "../../api/client";
import { DecimalText, isZeroDecimal } from "../../components/DecimalText";
import { RequestReference } from "../../components/RequestReference";
import { pricingRuleDetailPath } from "../../routes/paths";
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
import { FormModal, RangeEnd, RangeStart, VersionStatusTag, listAllProviders } from "./ProviderPricesPage";

/** 筛选里的「全部状态」。不进查询串。 */
const ALL = "ALL";

/** 没有值的格子。纯标点，不进翻译文件。 */
export const EMPTY = "—";

export function PricingRulesPage() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const paging = usePaging(DEFAULT_PAGE_SIZE);
  const [filterForm] = Form.useForm<FilterValues>();
  const [filter, setFilter] = useState<PricingRuleFilter>({});
  const [creating, setCreating] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const scopeOptions = useScopeOptions();

  const customers = useAllCustomers();
  const providers = useAllProviders();
  const filterProviderId = filter.provider_id;
  const models = useQuery({
    queryKey: allModelsQueryKey(filterProviderId ?? ""),
    queryFn: ({ signal }) => listAllModels(filterProviderId ?? "", signal),
    enabled: filterProviderId !== undefined,
  });

  const rules = useQuery({
    queryKey: pricingRuleListQueryKey(filter, paging.page, paging.pageSize),
    queryFn: ({ signal }) => listPricingRules(filter, paging.page, paging.pageSize, signal),
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
    setFilter(toRuleFilter(values));
    paging.reset();
  };

  const finish = (message: string): void => {
    void queryClient.invalidateQueries({ queryKey: PRICING_RULE_LISTS_QUERY_KEY });
    setCreating(false);
    setNotice(message);
  };

  const columns: TableColumnsType<PricingRule> = [
    {
      key: "scope",
      title: t("pricing.rules.field.scope"),
      render: (_: unknown, rule) => (
        <Link to={pricingRuleDetailPath(rule.id)}>
          <ScopeText scope={rule.priority_scope} />
        </Link>
      ),
    },
    {
      key: "customer",
      title: t("pricing.rules.field.customer"),
      render: (_: unknown, rule) => <CustomerName customerId={rule.customer_id} customers={customers.data} />,
    },
    {
      key: "provider",
      title: t("pricing.field.provider"),
      render: (_: unknown, rule) => (rule.provider_code === null ? EMPTY : <CodeText code={rule.provider_code} />),
    },
    {
      key: "model",
      title: t("pricing.field.model"),
      render: (_: unknown, rule) => (rule.model_code === null ? EMPTY : <CodeText code={rule.model_code} />),
    },
    {
      key: "strategy",
      title: t("pricing.rules.field.strategy"),
      render: (_: unknown, rule) => <StrategyText strategy={rule.strategy} />,
    },
    {
      key: "price",
      title: t("pricing.rules.field.price"),
      render: (_: unknown, rule) => <RulePriceSummary rule={rule} />,
    },
    {
      key: "status",
      title: t("pricing.field.status"),
      render: (_: unknown, rule) => <RuleStatusTag status={rule.status} />,
    },
    {
      key: "effective_from",
      title: t("pricing.field.effectiveFrom"),
      render: (_: unknown, rule) => <RangeStart version={rule} />,
    },
    {
      key: "effective_to",
      title: t("pricing.field.effectiveTo"),
      render: (_: unknown, rule) => <RangeEnd version={rule} />,
    },
  ];

  const filtered = Object.keys(filter).length > 0;

  let body: ReactNode;
  if (rules.isPending) {
    body = <LoadingBlock text={t("pricing.rules.loading")} />;
  } else if (rules.isError) {
    body = (
      <PricingRuleErrorAlert
        error={rules.error}
        title={t("pricing.rules.loadFailed")}
        onRetry={() => void rules.refetch()}
      />
    );
  } else if (rules.data.total === 0) {
    body = <Empty description={filtered ? t("pricing.rules.emptyFiltered") : t("pricing.rules.empty")} />;
  } else {
    body = (
      <Table<PricingRule>
        rowKey="id"
        columns={columns}
        dataSource={rules.data.items}
        loading={rules.isFetching}
        // 页码超过末页时后端回空 items、total 照报：说清楚是「这一页没有」。
        locale={{ emptyText: t("pricing.rules.emptyPage") }}
        pagination={{
          current: paging.page,
          pageSize: paging.pageSize,
          total: rules.data.total,
          showSizeChanger: true,
          pageSizeOptions: PAGE_SIZE_OPTIONS,
          showTotal: (count) => t("pricing.rules.total", { total: count }),
          onChange: paging.goTo,
        }}
      />
    );
  }

  return (
    <Card
      title={t("pricing.rules.title")}
      extra={
        <Button
          type="primary"
          onClick={() => {
            setNotice(null);
            setCreating(true);
          }}
        >
          {t("pricing.rules.create")}
        </Button>
      }
    >
      <TaxInclusiveNotice />
      <Typography.Paragraph type="secondary">{t("pricing.rules.intro")}</Typography.Paragraph>
      <NoticeAlert message={notice} />
      <PricingRuleErrorAlert
        error={customers.error ?? providers.error ?? models.error}
        title={t("pricing.rules.form.lookupFailed")}
        onRetry={() => {
          void customers.refetch();
          void providers.refetch();
          if (filterProviderId !== undefined) {
            void models.refetch();
          }
        }}
      />
      <Form<FilterValues>
        // 每个表单都有名字：字段的 id 是「表单名_字段名」，筛选与建草稿的同名字段不会撞上同一个 id。
        name="rule-filter"
        form={filterForm}
        layout="inline"
        style={{ marginBottom: 16, rowGap: 8 }}
        initialValues={{
          priority_scope: undefined,
          customer_id: undefined,
          provider_id: undefined,
          model_id: undefined,
          status: ALL,
        }}
        onValuesChange={changeFilter}
      >
        <Form.Item name="priority_scope" label={t("pricing.rules.field.scope")}>
          <Select
            allowClear
            style={{ minWidth: 220 }}
            placeholder={t("pricing.rules.filter.anyScope")}
            options={scopeOptions}
          />
        </Form.Item>
        <Form.Item name="customer_id" label={t("pricing.rules.field.customer")}>
          <Select
            allowClear
            showSearch
            optionFilterProp="label"
            style={{ minWidth: 220 }}
            placeholder={t("pricing.rules.filter.anyCustomer")}
            loading={customers.isPending}
            options={customerOptions(customers.data)}
          />
        </Form.Item>
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
              { value: "RETIRED", label: t("pricing.rules.status.disabled") },
              { value: "DISCARDED", label: t("pricing.status.discarded") },
            ]}
          />
        </Form.Item>
      </Form>
      {body}
      {creating ? (
        <CreateRuleModal
          onDone={() => finish(t("pricing.create.done"))}
          onCancel={() => setCreating(false)}
        />
      ) : null}
    </Card>
  );
}

interface FilterValues {
  priority_scope: string | undefined;
  customer_id: string | undefined;
  provider_id: string | undefined;
  model_id: string | undefined;
  status: string;
}

/** 表单值 → 查询条件。没选的不带（查询串里也就没有它）。 */
function toRuleFilter(values: FilterValues): PricingRuleFilter {
  return {
    ...(isPriorityScope(values.priority_scope) ? { priority_scope: values.priority_scope } : {}),
    ...(values.customer_id === undefined ? {} : { customer_id: values.customer_id }),
    ...(values.provider_id === undefined ? {} : { provider_id: values.provider_id }),
    ...(values.provider_id === undefined || values.model_id === undefined ? {} : { model_id: values.model_id }),
    ...(isPricingRuleStatus(values.status) ? { status: values.status } : {}),
  };
}

// ---------------------------------------------------------------------------
// 建草稿
// ---------------------------------------------------------------------------

interface DraftValues {
  priority_scope: PriorityScope | undefined;
  customer_id: string | undefined;
  provider_id: string | undefined;
  model_id: string | undefined;
  strategy: PricingStrategy;
  markup_multiplier: string;
  components: RateRow[];
}

interface DraftReview {
  body: CreatePricingRuleBody;
  customer: CustomerSummary | undefined;
  provider: Provider | undefined;
  model: Model | undefined;
}

/**
 * 建草稿：先选范围，再只显示这一级要的范围字段（供应商与模型只列启用中的：停用的建草稿是 409）；策略决定
 * 填倍数还是分量。填表 → 确认 → 发请求。重发会建出两个草稿，所以确认中防双击。
 */
function CreateRuleModal({ onDone, onCancel }: { onDone: () => void; onCancel: () => void }) {
  const { t } = useTranslation();
  const [form] = Form.useForm<DraftValues>();
  const [reviewing, setReviewing] = useState<DraftReview | null>(null);
  const scope = Form.useWatch("priority_scope", form);
  const providerId = Form.useWatch("provider_id", form);
  const strategy = Form.useWatch("strategy", form);
  const customers = useAllCustomers();
  const providers = useAllProviders();
  const models = useQuery({
    queryKey: allModelsQueryKey(providerId ?? ""),
    queryFn: ({ signal }) => listAllModels(providerId ?? "", signal),
    enabled: providerId !== undefined,
  });
  const meterTypes = useAllMeterTypes();
  const meterTypeList = meterTypes.data ?? [];
  const componentRules = useComponentRatesRules(meterTypeList);
  const multiplierRules = useMultiplierRules();
  const scopeOptions = useScopeOptions();
  const needs = isPriorityScope(scope) ? SCOPE_FIELDS[scope] : null;

  const review = (values: DraftValues): void => {
    if (!isPriorityScope(values.priority_scope)) {
      return;
    }
    const fields = SCOPE_FIELDS[values.priority_scope];
    const customer = fields.customer ? customers.data?.find((candidate) => candidate.id === values.customer_id) : undefined;
    const provider = fields.provider ? providers.data?.find((candidate) => candidate.id === values.provider_id) : undefined;
    const model = fields.model ? models.data?.find((candidate) => candidate.id === values.model_id) : undefined;
    if ((fields.customer && customer === undefined) || (fields.provider && provider === undefined)) {
      return;
    }
    if (fields.model && model === undefined) {
      return;
    }
    // 范围字段只带这一级要的，其余的不出现在请求体里（多给是 422）；策略只带它自己的那个字段。
    const body: CreatePricingRuleBody = {
      priority_scope: values.priority_scope,
      ...(customer === undefined ? {} : { customer_id: customer.id }),
      ...(provider === undefined ? {} : { provider_id: provider.id }),
      ...(model === undefined ? {} : { model_id: model.id }),
      strategy: values.strategy,
      ...(values.strategy === "MARKUP"
        ? { markup_multiplier: values.markup_multiplier.trim() }
        : { components: toRuleComponentBodies(values.components) }),
    };
    setReviewing({ body, customer, provider, model });
  };

  return (
    <>
      <FormModal title={t("pricing.rules.create.title")} form={form} width={760} onCancel={onCancel}>
        <TaxInclusiveNotice />
        <PricingRuleErrorAlert
          error={customers.error ?? providers.error ?? models.error ?? meterTypes.error}
          title={t("pricing.rules.form.lookupFailed")}
        />
        <Form<DraftValues>
          name="rule-draft"
          form={form}
          layout="vertical"
          initialValues={{
            priority_scope: undefined,
            customer_id: undefined,
            provider_id: undefined,
            model_id: undefined,
            strategy: "MARKUP",
            markup_multiplier: "",
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
            name="priority_scope"
            label={t("pricing.rules.field.scope")}
            extra={t("pricing.rules.form.scopeHint")}
            rules={[{ required: true, message: t("pricing.rules.form.scopeRequired") }]}
          >
            <Select options={scopeOptions} />
          </Form.Item>
          {needs?.customer === true ? (
            <Form.Item
              name="customer_id"
              label={t("pricing.rules.field.customer")}
              rules={[{ required: true, message: t("pricing.rules.form.customerRequired") }]}
            >
              <Select
                showSearch
                optionFilterProp="label"
                loading={customers.isPending}
                options={customerOptions(customers.data)}
              />
            </Form.Item>
          ) : null}
          {needs?.provider === true ? (
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
          ) : null}
          {needs?.model === true ? (
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
          ) : null}
          <Form.Item name="strategy" label={t("pricing.rules.field.strategy")}>
            <StrategyRadio />
          </Form.Item>
          {strategy === "FIXED_RATE" ? (
            <Form.Item name="components" label={t("pricing.field.components")} rules={componentRules}>
              {meterTypes.isPending ? (
                <LoadingBlock text={t("pricing.components.loading")} />
              ) : (
                <ComponentRatesInput meterTypes={meterTypeList} rateTitle={t("pricing.rules.field.rateAmount")} />
              )}
            </Form.Item>
          ) : (
            <Form.Item
              name="markup_multiplier"
              label={t("pricing.rules.field.multiplier")}
              extra={t("pricing.rules.form.multiplierHint")}
              rules={multiplierRules}
            >
              <Input inputMode="decimal" autoComplete="off" spellCheck={false} />
            </Form.Item>
          )}
        </Form>
      </FormModal>
      {reviewing === null ? null : (
        <RuleConfirmModal
          title={t("pricing.rules.create.confirmTitle")}
          confirmLabel={t("pricing.create.confirm")}
          backLabel={t("pricing.backToEdit")}
          failedTitle={t("pricing.create.failed")}
          width={760}
          run={() => createPricingRule(reviewing.body)}
          onDone={onDone}
          onBack={() => setReviewing(null)}
        >
          <SummaryList
            items={[
              {
                key: "scope",
                label: t("pricing.rules.field.scope"),
                children: <ScopeText scope={reviewing.body.priority_scope} />,
              },
              ...(reviewing.customer === undefined
                ? []
                : [{ key: "customer", label: t("pricing.rules.field.customer"), children: reviewing.customer.company_name }]),
              ...(reviewing.provider === undefined
                ? []
                : [{ key: "provider", label: t("pricing.field.provider"), children: <CodeText code={reviewing.provider.code} /> }]),
              ...(reviewing.model === undefined
                ? []
                : [{ key: "model", label: t("pricing.field.model"), children: <CodeText code={reviewing.model.code} /> }]),
              {
                key: "strategy",
                label: t("pricing.rules.field.strategy"),
                children: <StrategyText strategy={reviewing.body.strategy} />,
              },
              ...(reviewing.body.markup_multiplier === undefined
                ? []
                : [
                    {
                      key: "multiplier",
                      label: t("pricing.rules.field.multiplier"),
                      children: <DecimalText value={reviewing.body.markup_multiplier} />,
                    },
                  ]),
            ]}
          />
          {reviewing.body.components === undefined ? null : (
            <RuleComponentBodiesTable components={reviewing.body.components} />
          )}
          <Typography.Paragraph type="secondary" style={{ marginTop: 16 }}>
            {t("pricing.create.draftNote")}
          </Typography.Paragraph>
        </RuleConfirmModal>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// 规则详情页与试算页共用
// ---------------------------------------------------------------------------

/** 「含税」：规则的价格一律是 MYR 含税价（ADR-0008）；倍数与单价只在管理端可见。 */
export function TaxInclusiveNotice() {
  const { t } = useTranslation();
  return (
    <Typography.Paragraph>
      <Tag color="orange">{t("pricing.rules.taxTag")}</Tag>
      <Typography.Text type="secondary">{t("pricing.rules.taxNote")}</Typography.Text>
    </Typography.Paragraph>
  );
}

/**
 * 范围的名字（下拉的选项要纯文字：antd 只给字符串标签生成 `title`，搜索也只认字符串）。认不出的原样返回，不猜。
 */
export function useScopeLabel(): (scope: string) => string {
  const { t } = useTranslation();
  return (scope) => {
    switch (scope) {
      case "CUSTOMER_PROVIDER_MODEL":
        return t("pricing.rules.scope.customerProviderModel");
      case "CUSTOMER_PROVIDER":
        return t("pricing.rules.scope.customerProvider");
      case "CUSTOMER":
        return t("pricing.rules.scope.customer");
      case "GLOBAL_PROVIDER_MODEL":
        return t("pricing.rules.scope.globalProviderModel");
      case "GLOBAL":
        return t("pricing.rules.scope.global");
      default:
        return scope;
    }
  };
}

export function ScopeText({ scope }: { scope: string }) {
  const scopeLabel = useScopeLabel();
  return <>{scopeLabel(scope)}</>;
}

/** 五级范围的下拉选项，从高到低。 */
function useScopeOptions() {
  const scopeLabel = useScopeLabel();
  return PRIORITY_SCOPES.map((scope) => ({ value: scope, label: scopeLabel(scope) }));
}

/** 策略的名字。认不出的原样显示。 */
export function StrategyText({ strategy }: { strategy: string }) {
  const { t } = useTranslation();
  switch (strategy) {
    case "MARKUP":
      return <>{t("pricing.rules.strategy.markup")}</>;
    case "FIXED_RATE":
      return <>{t("pricing.rules.strategy.fixedRate")}</>;
    default:
      return <>{strategy}</>;
  }
}

/** 规则的状态标签：与价格版本相同，只是 `RETIRED` 叫「已停用」（spec §59「disable rule」）。 */
export function RuleStatusTag({ status }: { status: string }) {
  const { t } = useTranslation();
  return status === "RETIRED" ? <Tag>{t("pricing.rules.status.disabled")}</Tag> : <VersionStatusTag status={status} />;
}

/** 策略单选：倍数（乘 MYR 估算成本）或固定分量价。 */
export function StrategyRadio({
  value,
  onChange,
}: {
  value?: PricingStrategy;
  onChange?: (next: PricingStrategy) => void;
}) {
  const { t } = useTranslation();
  return (
    <Radio.Group
      value={value}
      onChange={(event) => {
        const raw: unknown = event.target.value;
        if (raw === "MARKUP" || raw === "FIXED_RATE") {
          onChange?.(raw);
        }
      }}
      options={[
        { value: "MARKUP", label: t("pricing.rules.strategy.markup") },
        { value: "FIXED_RATE", label: t("pricing.rules.strategy.fixedRate") },
      ]}
    />
  );
}

/** 列表里的「价格（MYR，含税）」一格：MARKUP 是倍数，FIXED_RATE 是分量代码。 */
function RulePriceSummary({ rule }: { rule: PricingRule }) {
  const { t } = useTranslation();
  if (rule.markup_multiplier !== null) {
    return (
      <span>
        {"× "}
        <DecimalText value={rule.markup_multiplier} />
      </span>
    );
  }
  if (rule.components.length === 0) {
    return <Typography.Text type="secondary">{t("pricing.rules.noComponents")}</Typography.Text>;
  }
  return (
    <Typography.Text type="secondary">
      {rule.components.map((component) => component.component_code).join(", ")}
    </Typography.Text>
  );
}

/** 表单行 → 规则的分量请求体：只有代码与两个数（规则的分量没有备注，带了是 422）。 */
export function toRuleComponentBodies(rows: readonly RateRow[]): RuleComponentBody[] {
  return toComponentBodies(rows).map((component) => ({
    component_code: component.component_code,
    unit_quantity: component.unit_quantity,
    rate_amount: component.rate_amount,
  }));
}

/** 确认框里列出要提交的分量：两个数按十进制字符串显示，单价一列写明「MYR，含税」。 */
export function RuleComponentBodiesTable({ components }: { components: readonly RuleComponentBody[] }) {
  const { t } = useTranslation();
  const columns: TableColumnsType<RuleComponentBody> = [
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
      title: t("pricing.rules.field.rateAmount"),
      render: (_: unknown, component) => <DecimalText value={component.rate_amount} />,
    },
  ];
  return (
    <Table<RuleComponentBody>
      size="small"
      rowKey="component_code"
      columns={columns}
      dataSource={[...components]}
      pagination={false}
    />
  );
}

/** 倍数：正数，整数最多 12 位、小数最多 8 位（与单价同一条正则）；只用正则与字符串判零，不经 number。 */
export function useMultiplierRules(): FormRule[] {
  const { t } = useTranslation();
  return [
    { required: true, whitespace: true, message: t("pricing.rules.form.multiplierRequired") },
    {
      validator: (_rule, value: unknown) => {
        const text = typeof value === "string" ? value.trim() : "";
        if (text === "" || (PRICE_DECIMAL_PATTERN.test(text) && !isZeroDecimal(text))) {
          return Promise.resolve();
        }
        return Promise.reject(new Error(t("pricing.rules.form.multiplierInvalid")));
      },
    },
  ];
}

/** 全部客户，逐页取完（列表最新在前，由后端决定）。挂在客户列表的前缀下，建客户之后跟着过期。 */
export async function listAllCustomers(signal?: AbortSignal): Promise<CustomerSummary[]> {
  const customers: CustomerSummary[] = [];
  for (let page = 1; ; page += 1) {
    const result = await listCustomers(page, MAX_PAGE_SIZE, signal);
    customers.push(...result.items);
    if (result.items.length === 0 || customers.length >= result.total) {
      return customers;
    }
  }
}

export function useAllCustomers() {
  return useQuery({
    queryKey: [...CUSTOMER_LISTS_QUERY_KEY, "all"],
    queryFn: ({ signal }) => listAllCustomers(signal),
  });
}

/** 全部供应商（含已停用的）。与价格页同一个查询键，共用缓存。 */
export function useAllProviders() {
  return useQuery({
    queryKey: [...PROVIDER_LISTS_QUERY_KEY, "all"],
    queryFn: ({ signal }) => listAllProviders(signal),
  });
}

export function customerOptions(customers: readonly CustomerSummary[] | undefined) {
  return (customers ?? []).map((customer) => ({ value: customer.id, label: customer.company_name }));
}

/** 客户的公司名；客户列表里找不到（还在加载、或取不到）时退回到 `customer_id` 本身。 */
export function CustomerName({
  customerId,
  customers,
}: {
  customerId: string | null;
  customers: readonly CustomerSummary[] | undefined;
}) {
  if (customerId === null) {
    return <>{EMPTY}</>;
  }
  const customer = customers?.find((candidate) => candidate.id === customerId);
  return customer === undefined ? <CodeText code={customerId} /> : <>{customer.company_name}</>;
}

/**
 * 定价规则与试算接口出错时的提示。
 *
 * 403 `ADMIN_REQUIRED` 单独说「没有权限」—— 前端不做角色判断，是不是管理员只看后端这一句。两节已知的
 * 404 / 409 / 422 码显示对应的文案；认不出的码用调用方给的标题。两种情况下面都带后端 message 与 request_id
 * （`RequestReference`），所以未知码也看得到后端的说法（例如发布不完整时缺哪些分量）。
 */
export function PricingRuleErrorAlert({
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
      message = t("pricing.rules.forbidden");
      break;
    case PRICING_RULE_NOT_FOUND:
      message = t("pricing.rules.error.notFound");
      break;
    case CUSTOMER_NOT_FOUND:
      message = t("pricing.rules.error.customerNotFound");
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
    case USAGE_METER_TYPE_NOT_FOUND:
      message = t("pricing.rules.error.meterTypeNotFound");
      break;
    case PRICING_RULE_NOT_DRAFT:
      message = t("pricing.rules.error.notDraft");
      break;
    case PRICING_RULE_INCOMPLETE:
      message = t("pricing.rules.error.incomplete");
      break;
    case CATALOG_ITEM_RETIRED:
      message = t("pricing.error.catalogRetired");
      break;
    case PRICING_RULE_NOT_RETIRABLE:
      message = t("pricing.rules.error.notRetirable");
      break;
    case PRICING_RULE_FINAL:
      message = t("pricing.rules.error.final");
      break;
    case EFFECTIVE_FROM_CONFLICT:
      message = t("pricing.rules.error.effectiveFromConflict");
      break;
    case EFFECTIVE_FROM_IN_PAST:
      message = t("pricing.error.effectiveFromInPast");
      break;
    case AMOUNT_OUT_OF_RANGE:
      message = t("pricing.rules.error.amountOutOfRange");
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

/**
 * 写操作的二次确认：只有点了这里的确认按钮才发请求。
 *
 * 进行中禁用两个按钮、不许关闭，并用 ref 挡住第二次点击（`isPending` 要等下一次渲染才变 true）。
 * 失败时留在确认框里显示错误（{@link PricingRuleErrorAlert}，认规则的错误码）；成功后交给 `onDone`，由调用方
 * 关掉对话框并让列表过期重读。
 */
export function RuleConfirmModal<Result>({
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
        <PricingRuleErrorAlert error={write.error} title={failedTitle} />
      </div>
    </Modal>
  );
}
