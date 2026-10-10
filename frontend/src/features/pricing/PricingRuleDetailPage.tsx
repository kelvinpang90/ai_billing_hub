/**
 * 管理端定价规则详情（`GET` / `PATCH` / `publish` / `retire` / `discard`，docs/api.md「管理端定价规则」）。
 *
 * 显示范围、策略、区间、建草稿人与发布人，以及倍数或全部分量（MYR 含税）；按状态给出能做的写操作：
 *
 * - 草稿：编辑（策略、倍数、分量；只发改了的字段，分量整体替换；范围不可改）、发布（现在或预约）、丢弃
 * - 已发布且未被后继截断：停用（已开始的从此刻起不再命中、下落到更低一级；尚未开始的预约被撤销）
 * - 已停用 / 已丢弃：只看
 *
 * 每个写操作都二次确认；成功后让详情与全部列表过期重读。
 *
 * ⚠️ 倍数、单价与数量按十进制字符串显示（DecimalText），比较「改没改」用 normalizeDecimal，不经 number。
 */

import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Button,
  Card,
  Descriptions,
  Empty,
  Form,
  Input,
  Modal,
  Radio,
  Result,
  Space,
  Table,
  Typography,
  type DescriptionsProps,
  type FormRule,
  type TableColumnsType,
} from "antd";
import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link, useParams } from "react-router";

import type { MeterType } from "../../api/adminCatalog";
import type { CustomerSummary } from "../../api/adminCustomers";
import {
  PRICING_RULE_LISTS_QUERY_KEY,
  PRICING_RULE_NOT_FOUND,
  discardPricingRule,
  getPricingRule,
  isPricingStrategy,
  pricingRuleDetailQueryKey,
  publishPricingRule,
  retirePricingRule,
  updatePricingRule,
  type PricingRule,
  type PricingRuleComponent,
  type PricingRulePatch,
  type PricingStrategy,
  type RuleComponentBody,
} from "../../api/adminPricingRules";
import { ApiError } from "../../api/client";
import { DateTimeText, displayTimeToRfc3339, parseUtc } from "../../components/DateTimeText";
import { DecimalText, normalizeDecimal } from "../../components/DecimalText";
import { ROUTES, customerDetailPath } from "../../routes/paths";
import { CodeText, LoadingBlock, NoticeAlert, SummaryList } from "../catalog/ProvidersPage";
import {
  ComponentRatesInput,
  rowsFromVersion,
  useAllMeterTypes,
  useComponentRatesRules,
  type RateRow,
} from "./ComponentRatesInput";
import { FormModal, RangeEnd, RangeStart, isRetirable, useTextRules } from "./ProviderPricesPage";
import {
  CustomerName,
  EMPTY,
  PricingRuleErrorAlert,
  RuleComponentBodiesTable,
  RuleConfirmModal,
  RuleStatusTag,
  ScopeText,
  StrategyRadio,
  StrategyText,
  TaxInclusiveNotice,
  toRuleComponentBodies,
  useAllCustomers,
  useMultiplierRules,
} from "./PricingRulesPage";

type Action = "edit" | "publish" | "retire" | "discard";

export function PricingRuleDetailPage() {
  const { t } = useTranslation();
  const { ruleId = "" } = useParams();
  const queryClient = useQueryClient();
  const [action, setAction] = useState<Action | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const customers = useAllCustomers();

  const rule = useQuery({
    queryKey: pricingRuleDetailQueryKey(ruleId),
    queryFn: ({ signal }) => getPricingRule(ruleId, signal),
  });

  const backLink = <Link to={ROUTES.pricingRules}>{t("pricing.rules.detail.backToList")}</Link>;

  if (rule.isPending) {
    return (
      <Card>
        <LoadingBlock text={t("pricing.rules.detail.loading")} />
      </Card>
    );
  }

  if (rule.isError) {
    const { error } = rule;
    // 404 是一个明确的答案（没有这条规则），不是「出错了、请重试」。
    if (error instanceof ApiError && error.code === PRICING_RULE_NOT_FOUND) {
      return (
        <Result
          status="404"
          title={t("pricing.rules.detail.notFound")}
          subTitle={t("pricing.rules.detail.notFoundBody")}
          extra={backLink}
        />
      );
    }
    return (
      <Card>
        <PricingRuleErrorAlert
          error={error}
          title={t("pricing.rules.detail.loadFailed")}
          onRetry={() => void rule.refetch()}
        />
        {backLink}
      </Card>
    );
  }

  const current = rule.data;
  const subject = <RuleSubject rule={current} customers={customers.data} />;

  const finish = (message: string): void => {
    void queryClient.invalidateQueries({ queryKey: pricingRuleDetailQueryKey(current.id) });
    void queryClient.invalidateQueries({ queryKey: PRICING_RULE_LISTS_QUERY_KEY });
    setAction(null);
    setNotice(message);
  };

  const open = (next: Action): void => {
    setNotice(null);
    setAction(next);
  };

  const isDraft = current.status === "DRAFT";
  const actions = (
    <Space wrap>
      {isDraft ? <Button onClick={() => open("edit")}>{t("pricing.edit.open")}</Button> : null}
      {isDraft ? (
        <Button type="primary" onClick={() => open("publish")}>
          {t("pricing.publish.open")}
        </Button>
      ) : null}
      {isDraft ? (
        <Button danger onClick={() => open("discard")}>
          {t("pricing.discard.open")}
        </Button>
      ) : null}
      {isRetirable(current) ? (
        <Button danger onClick={() => open("retire")}>
          {t("pricing.rules.retire.open")}
        </Button>
      ) : null}
    </Space>
  );

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      {backLink}
      <NoticeAlert message={notice} />
      <Card title={subject} extra={actions}>
        <TaxInclusiveNotice />
        <RuleDescriptions rule={current} customers={customers.data} />
      </Card>
      {current.strategy === "MARKUP" ? null : (
        <Card title={t("pricing.field.components")}>
          <RuleComponentsTable components={current.components} />
        </Card>
      )}

      {action === "edit" ? (
        <EditRuleModal
          rule={current}
          subject={subject}
          onDone={() => finish(t("pricing.edit.done"))}
          onCancel={() => setAction(null)}
        />
      ) : null}
      {action === "publish" ? (
        <RulePublishModal
          subject={subject}
          run={(effectiveFrom) => publishPricingRule(current.id, effectiveFrom)}
          onDone={() => finish(t("pricing.publish.done"))}
          onCancel={() => setAction(null)}
        />
      ) : null}
      {action === "retire" ? (
        <RuleRetireModal
          subject={subject}
          run={(reason) => retirePricingRule(current.id, reason)}
          onDone={() => finish(t("pricing.rules.retire.done"))}
          onCancel={() => setAction(null)}
        />
      ) : null}
      {action === "discard" ? (
        <RuleConfirmModal
          title={t("pricing.discard.title")}
          confirmLabel={t("pricing.discard.confirm")}
          backLabel={t("pricing.cancel")}
          danger
          failedTitle={t("pricing.discard.failed")}
          run={() => discardPricingRule(current.id)}
          onDone={() => finish(t("pricing.discard.done"))}
          onBack={() => setAction(null)}
        >
          <Typography.Paragraph>{subject}</Typography.Paragraph>
          <Typography.Paragraph type="secondary">{t("pricing.discard.body")}</Typography.Paragraph>
        </RuleConfirmModal>
      ) : null}
    </Space>
  );
}

/** 「范围 · 客户 · 供应商 · 模型」，只列这一级有的；代码原样。 */
function RuleSubject({
  rule,
  customers,
}: {
  rule: PricingRule;
  customers: readonly CustomerSummary[] | undefined;
}) {
  return (
    <span>
      <ScopeText scope={rule.priority_scope} />
      {rule.customer_id === null ? null : (
        <>
          {" · "}
          <CustomerName customerId={rule.customer_id} customers={customers} />
        </>
      )}
      {rule.provider_code === null ? null : (
        <>
          {" · "}
          <CodeText code={rule.provider_code} />
        </>
      )}
      {rule.model_code === null ? null : (
        <>
          {" / "}
          <CodeText code={rule.model_code} />
        </>
      )}
    </span>
  );
}

function RuleDescriptions({
  rule,
  customers,
}: {
  rule: PricingRule;
  customers: readonly CustomerSummary[] | undefined;
}) {
  const { t } = useTranslation();
  const items: DescriptionsProps["items"] = [
    { key: "scope", label: t("pricing.rules.field.scope"), children: <ScopeText scope={rule.priority_scope} /> },
    {
      key: "customer",
      label: t("pricing.rules.field.customer"),
      children:
        rule.customer_id === null ? (
          EMPTY
        ) : (
          <Link to={customerDetailPath(rule.customer_id)}>
            <CustomerName customerId={rule.customer_id} customers={customers} />
          </Link>
        ),
    },
    {
      key: "provider",
      label: t("pricing.field.provider"),
      children: rule.provider_code === null ? EMPTY : <CodeText code={rule.provider_code} />,
    },
    {
      key: "model",
      label: t("pricing.field.model"),
      children: rule.model_code === null ? EMPTY : <CodeText code={rule.model_code} />,
    },
    { key: "strategy", label: t("pricing.rules.field.strategy"), children: <StrategyText strategy={rule.strategy} /> },
    ...(rule.markup_multiplier === null
      ? []
      : [
          {
            key: "multiplier",
            label: t("pricing.rules.field.multiplier"),
            children: (
              <Space direction="vertical" size={0}>
                <span>
                  {"× "}
                  <DecimalText value={rule.markup_multiplier} />
                </span>
                <Typography.Text type="secondary">{t("pricing.rules.form.multiplierHint")}</Typography.Text>
              </Space>
            ),
          },
        ]),
    { key: "status", label: t("pricing.field.status"), children: <RuleStatusTag status={rule.status} /> },
    { key: "effective_from", label: t("pricing.field.effectiveFrom"), children: <RangeStart version={rule} /> },
    { key: "effective_to", label: t("pricing.field.effectiveTo"), children: <RangeEnd version={rule} /> },
    { key: "created_by", label: t("pricing.field.createdBy"), children: rule.created_by_email ?? EMPTY },
    { key: "created_at", label: t("pricing.field.createdAt"), children: <DateTimeText value={rule.created_at} /> },
    { key: "approved_by", label: t("pricing.field.approvedBy"), children: rule.approved_by_email ?? EMPTY },
    {
      key: "approved_at",
      label: t("pricing.field.approvedAt"),
      children: rule.approved_at === null ? EMPTY : <DateTimeText value={rule.approved_at} />,
    },
    { key: "updated_at", label: t("pricing.field.updatedAt"), children: <DateTimeText value={rule.updated_at} /> },
  ];
  return <Descriptions bordered size="small" column={1} items={items} />;
}

function RuleComponentsTable({ components }: { components: readonly PricingRuleComponent[] }) {
  const { t } = useTranslation();
  if (components.length === 0) {
    return <Empty description={t("pricing.rules.detail.noComponents")} />;
  }
  const columns: TableColumnsType<PricingRuleComponent> = [
    {
      key: "component_code",
      title: t("pricing.components.componentCode"),
      render: (_: unknown, component) => <CodeText code={component.component_code} />,
    },
    {
      key: "meter_type",
      title: t("pricing.components.meterType"),
      render: (_: unknown, component) => <CodeText code={component.meter_type_code} />,
    },
    {
      key: "unit_quantity",
      title: t("pricing.components.unitQuantity"),
      render: (_: unknown, component) => (
        <Space size={4}>
          <DecimalText value={component.unit_quantity} />
          <Typography.Text type="secondary">{component.unit}</Typography.Text>
        </Space>
      ),
    },
    {
      key: "rate_amount",
      title: t("pricing.rules.field.rateAmount"),
      render: (_: unknown, component) => (
        <Space size={4}>
          <Typography.Text type="secondary">{component.currency}</Typography.Text>
          <DecimalText value={component.rate_amount} />
        </Space>
      ),
    },
  ];
  return (
    <Table<PricingRuleComponent>
      size="small"
      rowKey="component_code"
      columns={columns}
      dataSource={[...components]}
      pagination={false}
    />
  );
}

// ---------------------------------------------------------------------------
// 编辑草稿
// ---------------------------------------------------------------------------

interface EditValues {
  strategy: PricingStrategy;
  markup_multiplier: string;
  components: RateRow[];
}

/** 一个分量的比较键：两个数按数值比（`1.5` 与 `1.50000000` 相同），不经 number。 */
function componentKey(component: RuleComponentBody | PricingRuleComponent): string {
  const quantity = normalizeDecimal(component.unit_quantity) ?? component.unit_quantity;
  const rate = normalizeDecimal(component.rate_amount) ?? component.rate_amount;
  return JSON.stringify([component.component_code, quantity, rate]);
}

function sameComponents(next: readonly RuleComponentBody[], current: readonly PricingRuleComponent[]): boolean {
  const a = next.map(componentKey).sort();
  const b = current.map(componentKey).sort();
  return a.length === b.length && a.every((key, index) => key === b[index]);
}

function sameDecimal(a: string, b: string | null): boolean {
  return b !== null && (normalizeDecimal(a) ?? a) === (normalizeDecimal(b) ?? b);
}

/**
 * 表单值 → PATCH 请求体：只带改了的字段（没改的不出现在请求体里）。换了策略时同时带上新策略自己的那个字段
 * （改成 MARKUP 必须给倍数；改成 FIXED_RATE 时分量取请求里的）。什么都没改时是 `{}`，调用方不该发 ——
 * 后端对 `{}` 回 422。
 */
export function rulePatch(rule: PricingRule, values: EditValues): PricingRulePatch {
  const patch: PricingRulePatch = {};
  const strategyChanged = values.strategy !== rule.strategy;
  if (strategyChanged) {
    patch.strategy = values.strategy;
  }
  if (values.strategy === "MARKUP") {
    const multiplier = values.markup_multiplier.trim();
    if (strategyChanged || !sameDecimal(multiplier, rule.markup_multiplier)) {
      patch.markup_multiplier = multiplier;
    }
  } else {
    const components = toRuleComponentBodies(values.components);
    if (strategyChanged || !sameComponents(components, rule.components)) {
      patch.components = components;
    }
  }
  return patch;
}

/** 编辑草稿要先拿到目录里的计量类型：FIXED_RATE 的分量按它成组，缺的分量列出来等人补。 */
function EditRuleModal({
  rule,
  subject,
  onDone,
  onCancel,
}: {
  rule: PricingRule;
  subject: ReactNode;
  onDone: () => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const meterTypes = useAllMeterTypes();
  if (meterTypes.data === undefined) {
    return (
      <Modal open title={t("pricing.rules.edit.title")} footer={null} onCancel={onCancel}>
        {meterTypes.isError ? (
          <PricingRuleErrorAlert
            error={meterTypes.error}
            title={t("pricing.components.loadFailed")}
            onRetry={() => void meterTypes.refetch()}
          />
        ) : (
          <LoadingBlock text={t("pricing.components.loading")} />
        )}
      </Modal>
    );
  }
  return (
    <EditRuleForm
      rule={rule}
      subject={subject}
      meterTypes={meterTypes.data}
      onDone={onDone}
      onCancel={onCancel}
    />
  );
}

function EditRuleForm({
  rule,
  subject,
  meterTypes,
  onDone,
  onCancel,
}: {
  rule: PricingRule;
  subject: ReactNode;
  meterTypes: MeterType[];
  onDone: () => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const [form] = Form.useForm<EditValues>();
  const [reviewing, setReviewing] = useState<PricingRulePatch | null>(null);
  const [unchanged, setUnchanged] = useState(false);
  const strategy = Form.useWatch("strategy", form);
  const componentRules = useComponentRatesRules(meterTypes);
  const multiplierRules = useMultiplierRules();

  const review = (values: EditValues): void => {
    const patch = rulePatch(rule, values);
    // 没有实际变化时后端 200、什么都不写；那就根本不该让人去确认一次「什么都没改」。
    if (Object.keys(patch).length === 0) {
      setUnchanged(true);
      return;
    }
    setUnchanged(false);
    setReviewing(patch);
  };

  // 规则的分量没有备注；借用价格的成组函数时补上空备注。
  const initialRows = rowsFromVersion(
    rule.components.map((component) => ({ ...component, metadata: null })),
    meterTypes,
  );

  return (
    <>
      <FormModal title={t("pricing.rules.edit.title")} form={form} width={760} onCancel={onCancel}>
        <Typography.Paragraph>{subject}</Typography.Paragraph>
        <TaxInclusiveNotice />
        <Form<EditValues>
          name="rule-edit"
          form={form}
          layout="vertical"
          initialValues={{
            strategy: isPricingStrategy(rule.strategy) ? rule.strategy : "MARKUP",
            markup_multiplier: rule.markup_multiplier ?? "",
            components: initialRows,
          }}
          onValuesChange={() => setUnchanged(false)}
          onFinish={review}
        >
          <Form.Item name="strategy" label={t("pricing.rules.field.strategy")}>
            <StrategyRadio />
          </Form.Item>
          {strategy === "FIXED_RATE" ? (
            <Form.Item name="components" label={t("pricing.field.components")} rules={componentRules}>
              <ComponentRatesInput meterTypes={meterTypes} rateTitle={t("pricing.rules.field.rateAmount")} />
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
        {unchanged ? <Typography.Text type="danger">{t("pricing.edit.unchanged")}</Typography.Text> : null}
      </FormModal>
      {reviewing === null ? null : (
        <RuleConfirmModal
          title={t("pricing.edit.confirmTitle")}
          confirmLabel={t("pricing.edit.confirm")}
          backLabel={t("pricing.backToEdit")}
          failedTitle={t("pricing.edit.failed")}
          width={760}
          run={() => updatePricingRule(rule.id, reviewing)}
          onDone={onDone}
          onBack={() => setReviewing(null)}
        >
          <Typography.Paragraph>{subject}</Typography.Paragraph>
          <PatchSummary patch={reviewing} />
        </RuleConfirmModal>
      )}
    </>
  );
}

function PatchSummary({ patch }: { patch: PricingRulePatch }) {
  const { t } = useTranslation();
  const items: NonNullable<DescriptionsProps["items"]> = [];
  if (patch.strategy !== undefined) {
    items.push({
      key: "strategy",
      label: t("pricing.rules.field.strategy"),
      children: <StrategyText strategy={patch.strategy} />,
    });
  }
  if (patch.markup_multiplier !== undefined) {
    items.push({
      key: "multiplier",
      label: t("pricing.rules.field.multiplier"),
      children: <DecimalText value={patch.markup_multiplier} />,
    });
  }
  return (
    <>
      {items.length === 0 ? null : <SummaryList items={items} />}
      {patch.components === undefined ? null : (
        <>
          <Typography.Paragraph strong>{t("pricing.edit.newComponents")}</Typography.Paragraph>
          <RuleComponentBodiesTable components={patch.components} />
        </>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// 发布与停用
// ---------------------------------------------------------------------------

interface PublishValues {
  when: "now" | "scheduled";
  at: string;
}

/**
 * 发布：现在，或预约一个时刻。预约时刻按吉隆坡时间输入，提交前换成带时区的 RFC 3339、整秒
 * （`displayTimeToRfc3339`）；「现在」时请求体是 `{}`。填表 → 确认 → 发请求。`run` 收到 `undefined`
 * 表示不指定时刻。
 */
function RulePublishModal({
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
          name="rule-publish"
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
            {when === "scheduled" ? t("pricing.rules.publish.scheduledBody") : t("pricing.rules.publish.nowBody")}
          </Typography.Paragraph>
        </Form>
      </FormModal>
      {reviewing === null ? null : (
        <RuleConfirmModal
          title={t("pricing.rules.publish.confirmTitle")}
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
                children: scheduled === undefined ? t("pricing.publish.now") : <ScheduledTime value={scheduled} />,
              },
            ]}
          />
          <Typography.Paragraph type="secondary">
            {scheduled === undefined ? t("pricing.rules.publish.nowBody") : t("pricing.rules.publish.scheduledBody")}
          </Typography.Paragraph>
        </RuleConfirmModal>
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

/** 停用：填原因（记在审计上）→ 确认 → 发请求。 */
function RuleRetireModal({
  subject,
  run,
  onDone,
  onCancel,
}: {
  subject: ReactNode;
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
      <FormModal title={t("pricing.rules.retire.title")} form={form} onCancel={onCancel}>
        <Typography.Paragraph>{subject}</Typography.Paragraph>
        <Typography.Paragraph type="secondary">{t("pricing.rules.retire.body")}</Typography.Paragraph>
        <Form<ReasonValues>
          name="rule-retire"
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
        <RuleConfirmModal
          title={t("pricing.rules.retire.confirmTitle")}
          confirmLabel={t("pricing.rules.retire.confirm")}
          backLabel={t("pricing.backToEdit")}
          danger
          failedTitle={t("pricing.rules.retire.failed")}
          run={() => run(reviewing)}
          onDone={onDone}
          onBack={() => setReviewing(null)}
        >
          <Typography.Paragraph>{subject}</Typography.Paragraph>
          <SummaryList items={[{ key: "reason", label: t("pricing.retire.reason"), children: reviewing }]} />
          <Typography.Paragraph type="secondary">{t("pricing.rules.retire.body")}</Typography.Paragraph>
        </RuleConfirmModal>
      )}
    </>
  );
}
