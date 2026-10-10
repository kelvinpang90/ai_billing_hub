/**
 * 管理端试算预览（`POST /api/v1/admin/pricing-preview`，docs/api.md「管理端试算预览」）。
 *
 * 顶栏的「Pricing preview」进这一页。填客户、供应商、模型（代码或别名）、计量类型、用量与 `occurred_at`，
 * 得到这一刻会怎么计价：状态与细分码、命中的模型（代码或别名）、价格版本、汇率版本、规则、每个分量的未舍入值、
 * 三个存储值与「含税」。**错误状态（模型未知、缺价格 / 汇率 / 规则）照样画出已解析到的部分**。页面写明
 * 「估算成本，客户不可见」。
 *
 * 表单按计量类型的上报形态切换数量字段：`LLM_TOKEN_FIELDS` 是四个 token，`QUANTITY` 是 quantity（单位取自
 * 计量类型，只显示、不让改：不等于类型单位就是 422）。`occurred_at` 按吉隆坡时间输入，换成带时区的 RFC 3339。
 *
 * 试算是只读的（不写库、不写审计），所以不走二次确认；提交中禁用按钮防重复提交。
 *
 * ⚠️ 金额、未舍入值、汇率、倍数与数量一律按十进制字符串显示（DecimalText），token 个数也不转成 number
 * （见 api/adminPricingPreview.ts）。
 *
 * ⚠️ 说明一律不用 antd 的 `Alert`（`role="alert"` 会被部署后浏览器验收当成错误提示），用普通段落。
 */

import { useMutation } from "@tanstack/react-query";
import {
  Button,
  Card,
  Descriptions,
  Empty,
  Form,
  Input,
  Select,
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
import { Link } from "react-router";

import { MODEL_CODE_PATTERN, type MeterType } from "../../api/adminCatalog";
import {
  INTEGER_QUANTITY_PATTERN,
  QUANTITY_PATTERN,
  canonicalTokenCount,
  previewPricing,
  type PreviewComponent,
  type PreviewPriceVersion,
  type PricingPreview,
  type PricingPreviewRequest,
} from "../../api/adminPricingPreview";
import { RULE_CURRENCY } from "../../api/adminPricingRules";
import { DateTimeText, displayTimeToRfc3339, utcToDisplayInput } from "../../components/DateTimeText";
import { DecimalText } from "../../components/DecimalText";
import { pricingRuleDetailPath, providerPriceDetailPath } from "../../routes/paths";
import { CodeText } from "../catalog/ProvidersPage";
import { useAllMeterTypes } from "./ComponentRatesInput";
import {
  EMPTY,
  PricingRuleErrorAlert,
  ScopeText,
  StrategyText,
  customerOptions,
  useAllCustomers,
  useAllProviders,
} from "./PricingRulesPage";

const TOKEN_SHAPE = "LLM_TOKEN_FIELDS";

interface PreviewValues {
  customer_id: string | undefined;
  provider: string | undefined;
  model: string;
  usage_type: string | undefined;
  input_tokens: string;
  output_tokens: string;
  cache_creation_input_tokens: string;
  cache_read_input_tokens: string;
  quantity: string;
  occurred_at: string;
}

/** 现在的吉隆坡墙上时间（`datetime-local` 的值），给 `occurred_at` 预填。 */
function nowForInput(): string {
  return utcToDisplayInput(new Date().toISOString().slice(0, 19));
}

/**
 * 表单值 → 请求。用量字段只带这个计量类型的形态要的那一组（另一组带了是 422）；`unit` 取计量类型自己的。
 * `occurred_at` 换不成 RFC 3339 时返回 `null`（表单校验已挡住）。
 */
export function toPreviewRequest(values: PreviewValues, meterType: MeterType): PricingPreviewRequest | null {
  const occurredAt = displayTimeToRfc3339(values.occurred_at.trim());
  if (occurredAt === null || values.customer_id === undefined || values.provider === undefined) {
    return null;
  }
  const base = {
    customer_id: values.customer_id,
    provider: values.provider,
    model: values.model.trim(),
    usage_type: meterType.code,
    occurred_at: occurredAt,
  };
  if (meterType.payload_shape === TOKEN_SHAPE) {
    return {
      ...base,
      input_tokens: values.input_tokens.trim(),
      output_tokens: values.output_tokens.trim(),
      cache_creation_input_tokens: values.cache_creation_input_tokens.trim(),
      cache_read_input_tokens: values.cache_read_input_tokens.trim(),
    };
  }
  return { ...base, quantity: values.quantity.trim(), unit: meterType.unit };
}

export function PricingPreviewPage() {
  const { t } = useTranslation();
  const [form] = Form.useForm<PreviewValues>();
  const [initialTime] = useState(nowForInput);
  const inFlight = useRef(false);
  const usageType = Form.useWatch("usage_type", form);
  const customers = useAllCustomers();
  const providers = useAllProviders();
  const meterTypes = useAllMeterTypes();
  const meterType = meterTypes.data?.find((candidate) => candidate.code === usageType);

  const preview = useMutation({
    mutationFn: previewPricing,
    onSettled: () => {
      inFlight.current = false;
    },
  });

  const submit = (values: PreviewValues): void => {
    const chosen = meterTypes.data?.find((candidate) => candidate.code === values.usage_type);
    const request = chosen === undefined ? null : toPreviewRequest(values, chosen);
    if (request === null || inFlight.current) {
      return;
    }
    inFlight.current = true;
    preview.mutate(request);
  };

  const tokenRules = useTokenRules();
  const quantityRules = useQuantityRules(meterType);
  const occurredAtRule: FormRule = {
    validator: (_rule, value: unknown) =>
      typeof value === "string" && displayTimeToRfc3339(value.trim()) !== null
        ? Promise.resolve()
        : Promise.reject(new Error(t("pricing.publish.atInvalid"))),
  };

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <Card title={t("pricing.preview.title")}>
        <EstimateNotice />
        <Typography.Paragraph type="secondary">{t("pricing.preview.intro")}</Typography.Paragraph>
        <PricingRuleErrorAlert
          error={customers.error ?? providers.error ?? meterTypes.error}
          title={t("pricing.rules.form.lookupFailed")}
          onRetry={() => {
            void customers.refetch();
            void providers.refetch();
            void meterTypes.refetch();
          }}
        />
        <Form<PreviewValues>
          name="preview"
          form={form}
          layout="vertical"
          style={{ maxWidth: 640 }}
          initialValues={{
            customer_id: undefined,
            provider: undefined,
            model: "",
            usage_type: undefined,
            input_tokens: "0",
            output_tokens: "0",
            cache_creation_input_tokens: "0",
            cache_read_input_tokens: "0",
            quantity: "",
            occurred_at: initialTime,
          }}
          // 改了任何输入，上一次的结果就不再对应表单里的东西：收起来，免得被当成这一次的。
          onValuesChange={() => preview.reset()}
          onFinish={submit}
        >
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
          <Form.Item
            name="provider"
            label={t("pricing.field.provider")}
            rules={[{ required: true, message: t("pricing.form.providerRequired") }]}
          >
            <Select
              showSearch
              optionFilterProp="label"
              loading={providers.isPending}
              options={(providers.data ?? []).map((provider) => ({ value: provider.code, label: provider.code }))}
            />
          </Form.Item>
          <Form.Item
            name="model"
            label={t("pricing.field.model")}
            extra={t("pricing.preview.form.modelHint")}
            rules={[
              { required: true, message: t("pricing.form.modelRequired") },
              { pattern: MODEL_CODE_PATTERN, message: t("catalog.models.codeInvalid") },
            ]}
          >
            <Input autoComplete="off" spellCheck={false} />
          </Form.Item>
          <Form.Item
            name="usage_type"
            label={t("pricing.preview.field.usageType")}
            rules={[{ required: true, message: t("pricing.preview.form.usageTypeRequired") }]}
          >
            <Select
              showSearch
              optionFilterProp="label"
              loading={meterTypes.isPending}
              options={(meterTypes.data ?? []).map((candidate) => ({ value: candidate.code, label: candidate.code }))}
            />
          </Form.Item>
          {meterType?.payload_shape === TOKEN_SHAPE ? (
            <>
              <Form.Item name="input_tokens" label={t("pricing.preview.field.inputTokens")} rules={tokenRules}>
                <Input inputMode="numeric" autoComplete="off" />
              </Form.Item>
              <Form.Item name="output_tokens" label={t("pricing.preview.field.outputTokens")} rules={tokenRules}>
                <Input inputMode="numeric" autoComplete="off" />
              </Form.Item>
              <Form.Item
                name="cache_creation_input_tokens"
                label={t("pricing.preview.field.cacheCreationTokens")}
                rules={tokenRules}
              >
                <Input inputMode="numeric" autoComplete="off" />
              </Form.Item>
              <Form.Item
                name="cache_read_input_tokens"
                label={t("pricing.preview.field.cacheReadTokens")}
                rules={tokenRules}
              >
                <Input inputMode="numeric" autoComplete="off" />
              </Form.Item>
            </>
          ) : null}
          {meterType !== undefined && meterType.payload_shape !== TOKEN_SHAPE ? (
            <>
              <Form.Item
                name="quantity"
                label={t("pricing.preview.field.quantity")}
                extra={
                  meterType.quantity_kind === "INTEGER"
                    ? t("pricing.preview.form.integerQuantityHint")
                    : t("pricing.preview.form.decimalQuantityHint")
                }
                rules={quantityRules}
              >
                <Input inputMode="decimal" autoComplete="off" suffix={meterType.unit} />
              </Form.Item>
              <Form.Item
                label={t("pricing.preview.field.unit")}
                htmlFor="preview_unit"
                extra={t("pricing.preview.form.unitHint")}
              >
                <Input id="preview_unit" value={meterType.unit} disabled />
              </Form.Item>
            </>
          ) : null}
          <Form.Item
            name="occurred_at"
            label={t("pricing.preview.field.occurredAt")}
            extra={t("pricing.preview.form.occurredAtHint")}
            rules={[occurredAtRule]}
          >
            <Input type="datetime-local" step={1} />
          </Form.Item>
          <Button type="primary" htmlType="submit" loading={preview.isPending} disabled={preview.isPending}>
            {t("pricing.preview.submit")}
          </Button>
        </Form>
        <div style={{ marginTop: 16 }}>
          <PricingRuleErrorAlert error={preview.error} title={t("pricing.preview.failed")} />
        </div>
      </Card>
      {preview.data === undefined ? null : <PreviewResult result={preview.data} />}
    </Space>
  );
}

/** 「估算成本，客户不可见」。 */
function EstimateNotice() {
  const { t } = useTranslation();
  return (
    <Typography.Paragraph>
      <Tag color="orange">{t("pricing.preview.estimateTag")}</Tag>
      <Typography.Text type="secondary">{t("pricing.preview.estimateNote")}</Typography.Text>
    </Typography.Paragraph>
  );
}

/** token 个数：0–10¹² 的整数（后端是 JSON 整数）。只做字符串检查。 */
function useTokenRules(): FormRule[] {
  const { t } = useTranslation();
  return [
    {
      validator: (_rule, value: unknown) =>
        typeof value === "string" && canonicalTokenCount(value.trim()) !== null
          ? Promise.resolve()
          : Promise.reject(new Error(t("pricing.preview.form.tokensInvalid"))),
    },
  ];
}

/** quantity：整数类型不许有小数部分（`"3.0"` 也拒绝），小数类型最多 8 位小数。 */
function useQuantityRules(meterType: MeterType | undefined): FormRule[] {
  const { t } = useTranslation();
  const integer = meterType?.quantity_kind === "INTEGER";
  return [
    {
      validator: (_rule, value: unknown) => {
        const text = typeof value === "string" ? value.trim() : "";
        const valid = integer ? INTEGER_QUANTITY_PATTERN.test(text) : QUANTITY_PATTERN.test(text);
        if (valid) {
          return Promise.resolve();
        }
        return Promise.reject(
          new Error(integer ? t("pricing.preview.form.integerQuantityHint") : t("pricing.preview.form.decimalQuantityHint")),
        );
      },
    },
  ];
}

// ---------------------------------------------------------------------------
// 结果
// ---------------------------------------------------------------------------

/** 试算状态的标签。认不出的原样显示。 */
function PreviewStatusTag({ status }: { status: string }) {
  const { t } = useTranslation();
  switch (status) {
    case "PRICED":
      return <Tag color="green">{t("pricing.preview.status.priced")}</Tag>;
    case "MODEL_UNKNOWN":
      return <Tag color="orange">{t("pricing.preview.status.modelUnknown")}</Tag>;
    case "PRICING_ERROR":
      return <Tag color="red">{t("pricing.preview.status.pricingError")}</Tag>;
    case "FX_RATE_ERROR":
      return <Tag color="red">{t("pricing.preview.status.fxRateError")}</Tag>;
    default:
      return <Tag>{status}</Tag>;
  }
}

/** 状态与细分码的说明。`PRICED` 没有说明；认不出的细分码只显示代码本身。 */
function OutcomeExplanation({ status, errorCode }: { status: string; errorCode: string | null }) {
  const { t } = useTranslation();
  if (status === "MODEL_UNKNOWN") {
    return <>{t("pricing.preview.explain.modelUnknown")}</>;
  }
  if (status === "FX_RATE_ERROR") {
    return <>{t("pricing.preview.explain.fxRateError")}</>;
  }
  switch (errorCode) {
    case "NO_PROVIDER_PRICE":
      return <>{t("pricing.preview.explain.noProviderPrice")}</>;
    case "MISSING_PROVIDER_COMPONENT":
      return <>{t("pricing.preview.explain.missingProviderComponent")}</>;
    case "NO_PRICING_RULE":
      return <>{t("pricing.preview.explain.noPricingRule")}</>;
    case "MISSING_RULE_COMPONENT":
      return <>{t("pricing.preview.explain.missingRuleComponent")}</>;
    default:
      return null;
  }
}

/** 没解析到的部分。 */
function NotResolved() {
  const { t } = useTranslation();
  return <Typography.Text type="secondary">{t("pricing.preview.notResolved")}</Typography.Text>;
}

/** 价格版本的区间：`null` 端是「一直以来」/「仍生效」。 */
function PriceRange({ version }: { version: PreviewPriceVersion }) {
  const { t } = useTranslation();
  return (
    <Space size={4} wrap>
      {version.effective_from === null ? (
        t("pricing.range.sinceStart")
      ) : (
        <DateTimeText value={version.effective_from} />
      )}
      {" – "}
      {version.effective_to === null ? t("pricing.range.noEnd") : <DateTimeText value={version.effective_to} />}
    </Space>
  );
}

/** 一个可能为 `null` 的十进制字符串（带币种前缀）。 */
function AmountText({ value, currency }: { value: string | null; currency: string }) {
  if (value === null) {
    return <>{EMPTY}</>;
  }
  return (
    <Space size={4}>
      {currency === "" ? null : <Typography.Text type="secondary">{currency}</Typography.Text>}
      <DecimalText value={value} />
    </Space>
  );
}

export function PreviewResult({ result }: { result: PricingPreview }) {
  const { t } = useTranslation();
  const sourceCurrency = result.provider_price_version?.source_currency ?? "";

  // 原币是 MYR 时不查汇率（`null` 是「不需要」）；别的币种 `null` 才是「没解析到」。
  let fxRate: ReactNode;
  if (result.fx_rate_version !== null) {
    fxRate = (
      <Space direction="vertical" size={0}>
        <Space size={4}>
          <DecimalText value={result.fx_rate_version.rate} />
          <Typography.Text type="secondary">{t("pricing.preview.fxRateUnit", { currency: sourceCurrency })}</Typography.Text>
        </Space>
        <Typography.Text type="secondary">
          {t("pricing.preview.observedAt")} <DateTimeText value={result.fx_rate_version.observed_at} />
        </Typography.Text>
        <CodeText code={result.fx_rate_version.id} />
      </Space>
    );
  } else if (sourceCurrency === RULE_CURRENCY) {
    fxRate = <Typography.Text type="secondary">{t("pricing.preview.fxNotNeeded")}</Typography.Text>;
  } else {
    fxRate = <NotResolved />;
  }

  const resolved: DescriptionsProps["items"] = [
    {
      key: "status",
      label: t("pricing.field.status"),
      children: (
        <Space direction="vertical" size={4}>
          <Space size={4} wrap>
            <PreviewStatusTag status={result.status} />
            {result.error_code === null ? null : <CodeText code={result.error_code} />}
          </Space>
          <Typography.Text type="secondary">
            <OutcomeExplanation status={result.status} errorCode={result.error_code} />
          </Typography.Text>
        </Space>
      ),
    },
    {
      key: "model",
      label: t("pricing.preview.field.matchedModel"),
      children:
        result.model === null ? (
          <NotResolved />
        ) : (
          <Space size={4} wrap>
            <CodeText code={result.model.provider} />
            {" / "}
            <CodeText code={result.model.model} />
            <MatchedVia via={result.model.matched_via} />
          </Space>
        ),
    },
    {
      key: "price_version",
      label: t("pricing.preview.field.priceVersion"),
      children:
        result.provider_price_version === null ? (
          <NotResolved />
        ) : (
          <Space direction="vertical" size={0}>
            <Link to={providerPriceDetailPath(result.provider_price_version.id)}>
              <CodeText code={result.provider_price_version.id} />
            </Link>
            <Space size={4} wrap>
              <Typography.Text type="secondary">{result.provider_price_version.source_currency}</Typography.Text>
              <PriceRange version={result.provider_price_version} />
            </Space>
          </Space>
        ),
    },
    { key: "fx_rate", label: t("pricing.preview.field.fxRateVersion"), children: fxRate },
    {
      key: "rule",
      label: t("pricing.preview.field.rule"),
      children:
        result.pricing_rule === null ? (
          <NotResolved />
        ) : (
          <Space direction="vertical" size={0}>
            <Link to={pricingRuleDetailPath(result.pricing_rule.id)}>
              <ScopeText scope={result.pricing_rule.priority_scope} />
            </Link>
            <Space size={4} wrap>
              <StrategyText strategy={result.pricing_rule.strategy} />
              {result.pricing_rule.markup_multiplier === null ? null : (
                <span>
                  {"× "}
                  <DecimalText value={result.pricing_rule.markup_multiplier} />
                </span>
              )}
            </Space>
          </Space>
        ),
    },
  ];

  const componentColumns: TableColumnsType<PreviewComponent> = [
    {
      key: "component_code",
      title: t("pricing.components.componentCode"),
      render: (_: unknown, component) => <CodeText code={component.component_code} />,
    },
    {
      key: "quantity",
      title: t("pricing.preview.field.quantity"),
      render: (_: unknown, component) => <DecimalText value={component.quantity} />,
    },
    {
      key: "provider_cost",
      title: t("pricing.preview.field.providerCostUnrounded"),
      render: (_: unknown, component) => (
        <AmountText value={component.provider_cost_unrounded} currency={sourceCurrency} />
      ),
    },
    {
      key: "customer_price",
      title: t("pricing.preview.field.customerPriceUnrounded"),
      render: (_: unknown, component) => (
        <AmountText value={component.customer_price_unrounded} currency={RULE_CURRENCY} />
      ),
    },
  ];

  const amounts: AmountRow[] = [
    {
      key: "provider_source_cost",
      label: t("pricing.preview.amount.providerSourceCost"),
      currency: sourceCurrency,
      unrounded: result.provider_source_cost_unrounded,
      stored: result.provider_source_cost,
      taxInclusive: false,
    },
    {
      key: "estimated_provider_cost_myr",
      label: t("pricing.preview.amount.estimatedProviderCostMyr"),
      currency: RULE_CURRENCY,
      unrounded: result.estimated_provider_cost_myr_unrounded,
      stored: result.estimated_provider_cost_myr,
      taxInclusive: false,
    },
    {
      key: "billable_cost",
      label: t("pricing.preview.amount.billableCost"),
      currency: RULE_CURRENCY,
      unrounded: result.billable_cost_unrounded,
      stored: result.billable_cost,
      taxInclusive: result.tax_inclusive,
    },
  ];
  const amountColumns: TableColumnsType<AmountRow> = [
    {
      key: "label",
      title: t("pricing.preview.amount.name"),
      render: (_: unknown, row) => (
        <Space size={4} wrap>
          {row.label}
          {row.taxInclusive ? <Tag color="orange">{t("pricing.rules.taxTag")}</Tag> : null}
        </Space>
      ),
    },
    {
      key: "unrounded",
      title: t("pricing.preview.amount.unrounded"),
      render: (_: unknown, row) => <AmountText value={row.unrounded} currency={row.currency} />,
    },
    {
      key: "stored",
      title: t("pricing.preview.amount.stored"),
      render: (_: unknown, row) => <AmountText value={row.stored} currency={row.currency} />,
    },
  ];

  return (
    <Card title={t("pricing.preview.result.title")}>
      <EstimateNotice />
      <Descriptions bordered size="small" column={1} items={resolved} />
      <Typography.Title level={5} style={{ marginTop: 16 }}>
        {t("pricing.field.components")}
      </Typography.Title>
      {result.components.length === 0 ? (
        <Empty description={t("pricing.preview.result.noComponents")} />
      ) : (
        <Table<PreviewComponent>
          size="small"
          rowKey="component_code"
          columns={componentColumns}
          dataSource={result.components}
          pagination={false}
        />
      )}
      <Typography.Title level={5} style={{ marginTop: 16 }}>
        {t("pricing.preview.result.amounts")}
      </Typography.Title>
      <Typography.Paragraph type="secondary">{t("pricing.preview.result.amountsNote")}</Typography.Paragraph>
      <Table<AmountRow> size="small" rowKey="key" columns={amountColumns} dataSource={amounts} pagination={false} />
    </Card>
  );
}

interface AmountRow {
  key: string;
  label: string;
  currency: string;
  unrounded: string | null;
  stored: string | null;
  taxInclusive: boolean;
}

/** 命中模型的方式：按模型代码，或按别名。 */
function MatchedVia({ via }: { via: string }) {
  const { t } = useTranslation();
  if (via === "code") {
    return <Tag>{t("pricing.preview.matchedVia.code")}</Tag>;
  }
  if (via === "alias") {
    return <Tag>{t("pricing.preview.matchedVia.alias")}</Tag>;
  }
  return <Tag>{via}</Tag>;
}
