/**
 * 管理端价格版本详情（`GET` / `PATCH` / `publish` / `retire` / `discard`，docs/api.md「管理端供应商价格」）。
 *
 * 显示区间、建草稿人与发布人、来源与全部分量；按状态给出能做的写操作：
 *
 * - 草稿：编辑（只发改了的字段；分量整体替换）、发布（现在或预约）、丢弃
 * - 已发布且未被后继截断：退役（已开始的从此刻起无价；尚未开始的预约被撤销）
 * - 已退役 / 已丢弃：只看
 *
 * 每个写操作都二次确认；成功后让详情与全部列表过期重读。
 *
 * ⚠️ 单价与数量按十进制字符串显示（DecimalText），不经 number。
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
  Result,
  Space,
  Table,
  Typography,
  type DescriptionsProps,
  type TableColumnsType,
} from "antd";
import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link, useParams } from "react-router";

import type { MeterType } from "../../api/adminCatalog";
import {
  CURRENCY_PATTERN,
  PRICE_VERSION_NOT_FOUND,
  PROVIDER_PRICE_LISTS_QUERY_KEY,
  discardProviderPrice,
  getProviderPrice,
  providerPriceDetailQueryKey,
  publishProviderPrice,
  retireProviderPrice,
  updateProviderPrice,
  type ComponentRateBody,
  type PriceComponent,
  type PricePatch,
  type PriceVersion,
} from "../../api/adminProviderPrices";
import { ApiError } from "../../api/client";
import { DateTimeText } from "../../components/DateTimeText";
import { DecimalText, normalizeDecimal } from "../../components/DecimalText";
import { ROUTES } from "../../routes/paths";
import { CodeText, LoadingBlock, NoticeAlert, SummaryList } from "../catalog/ProvidersPage";
import {
  ComponentRatesInput,
  rowsFromVersion,
  toComponentBodies,
  useAllMeterTypes,
  useComponentRatesRules,
  type RateRow,
} from "./ComponentRatesInput";
import {
  ComponentBodiesTable,
  CostNotice,
  FormModal,
  PricingErrorAlert,
  PublishModal,
  RangeEnd,
  RangeStart,
  RetireModal,
  VersionStatusTag,
  WriteConfirmModal,
  isRetirable,
  useTextRules,
} from "./ProviderPricesPage";

/** 没有值的格子。纯标点，不进翻译文件。 */
const EMPTY = "—";

type Action = "edit" | "publish" | "retire" | "discard";

export function ProviderPriceDetailPage() {
  const { t } = useTranslation();
  const { priceVersionId = "" } = useParams();
  const queryClient = useQueryClient();
  const [action, setAction] = useState<Action | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const version = useQuery({
    queryKey: providerPriceDetailQueryKey(priceVersionId),
    queryFn: ({ signal }) => getProviderPrice(priceVersionId, signal),
  });

  const backLink = <Link to={ROUTES.providerPrices}>{t("pricing.detail.backToList")}</Link>;

  if (version.isPending) {
    return (
      <Card>
        <LoadingBlock text={t("pricing.detail.loading")} />
      </Card>
    );
  }

  if (version.isError) {
    const { error } = version;
    // 404 是一个明确的答案（没有这个版本），不是「出错了、请重试」。
    if (error instanceof ApiError && error.code === PRICE_VERSION_NOT_FOUND) {
      return (
        <Result
          status="404"
          title={t("pricing.detail.notFound")}
          subTitle={t("pricing.detail.notFoundBody")}
          extra={backLink}
        />
      );
    }
    return (
      <Card>
        <PricingErrorAlert
          error={error}
          title={t("pricing.detail.loadFailed")}
          onRetry={() => void version.refetch()}
        />
        {backLink}
      </Card>
    );
  }

  const current = version.data;
  const subject = <VersionSubject version={current} />;

  const finish = (message: string): void => {
    void queryClient.invalidateQueries({ queryKey: providerPriceDetailQueryKey(current.id) });
    void queryClient.invalidateQueries({ queryKey: PROVIDER_PRICE_LISTS_QUERY_KEY });
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
          {t("pricing.retire.open")}
        </Button>
      ) : null}
    </Space>
  );

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      {backLink}
      <NoticeAlert message={notice} />
      <Card title={subject} extra={actions}>
        <CostNotice />
        <VersionDescriptions version={current} />
      </Card>
      <Card title={t("pricing.field.components")}>
        <ComponentsTable version={current} />
      </Card>

      {action === "edit" ? (
        <EditDraftModal
          version={current}
          onDone={() => finish(t("pricing.edit.done"))}
          onCancel={() => setAction(null)}
        />
      ) : null}
      {action === "publish" ? (
        <PublishModal
          subject={subject}
          run={(effectiveFrom) => publishProviderPrice(current.id, effectiveFrom)}
          onDone={() => finish(t("pricing.publish.done"))}
          onCancel={() => setAction(null)}
        />
      ) : null}
      {action === "retire" ? (
        <RetireModal
          subject={subject}
          body={t("pricing.retire.priceBody")}
          run={(reason) => retireProviderPrice(current.id, reason)}
          onDone={() => finish(t("pricing.retire.done"))}
          onCancel={() => setAction(null)}
        />
      ) : null}
      {action === "discard" ? (
        <WriteConfirmModal
          title={t("pricing.discard.title")}
          confirmLabel={t("pricing.discard.confirm")}
          backLabel={t("pricing.cancel")}
          danger
          failedTitle={t("pricing.discard.failed")}
          run={() => discardProviderPrice(current.id)}
          onDone={() => finish(t("pricing.discard.done"))}
          onBack={() => setAction(null)}
        >
          <Typography.Paragraph>{subject}</Typography.Paragraph>
          <Typography.Paragraph type="secondary">{t("pricing.discard.body")}</Typography.Paragraph>
        </WriteConfirmModal>
      ) : null}
    </Space>
  );
}

/** 「供应商 / 模型」，代码原样。 */
function VersionSubject({ version }: { version: PriceVersion }) {
  return (
    <span>
      <CodeText code={version.provider_code} /> / <CodeText code={version.model_code} />
    </span>
  );
}

function VersionDescriptions({ version }: { version: PriceVersion }) {
  const { t } = useTranslation();
  const items: DescriptionsProps["items"] = [
    { key: "provider", label: t("pricing.field.provider"), children: <CodeText code={version.provider_code} /> },
    { key: "model", label: t("pricing.field.model"), children: <CodeText code={version.model_code} /> },
    { key: "status", label: t("pricing.field.status"), children: <VersionStatusTag status={version.status} /> },
    { key: "effective_from", label: t("pricing.field.effectiveFrom"), children: <RangeStart version={version} /> },
    { key: "effective_to", label: t("pricing.field.effectiveTo"), children: <RangeEnd version={version} /> },
    { key: "currency", label: t("pricing.field.currency"), children: version.source_currency },
    { key: "source_type", label: t("pricing.field.sourceType"), children: version.source_type },
    { key: "source_reference", label: t("pricing.field.sourceReference"), children: version.source_reference },
    { key: "created_by", label: t("pricing.field.createdBy"), children: version.created_by_email ?? EMPTY },
    {
      key: "created_at",
      label: t("pricing.field.createdAt"),
      children: <DateTimeText value={version.created_at} />,
    },
    { key: "approved_by", label: t("pricing.field.approvedBy"), children: version.approved_by_email ?? EMPTY },
    {
      key: "approved_at",
      label: t("pricing.field.approvedAt"),
      children: version.approved_at === null ? EMPTY : <DateTimeText value={version.approved_at} />,
    },
    {
      key: "updated_at",
      label: t("pricing.field.updatedAt"),
      children: <DateTimeText value={version.updated_at} />,
    },
  ];
  return <Descriptions bordered size="small" column={1} items={items} />;
}

function ComponentsTable({ version }: { version: PriceVersion }) {
  const { t } = useTranslation();
  if (version.components.length === 0) {
    return <Empty description={t("pricing.detail.noComponents")} />;
  }
  const columns: TableColumnsType<PriceComponent> = [
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
      title: t("pricing.components.rateAmount"),
      render: (_: unknown, component) => (
        <Space size={4}>
          <Typography.Text type="secondary">{version.source_currency}</Typography.Text>
          <DecimalText value={component.rate_amount} />
        </Space>
      ),
    },
    {
      key: "metadata",
      title: t("pricing.components.metadata"),
      render: (_: unknown, component) =>
        component.metadata === null ? EMPTY : <CodeText code={JSON.stringify(component.metadata)} />,
    },
  ];
  return (
    <Table<PriceComponent>
      size="small"
      rowKey="component_code"
      columns={columns}
      dataSource={version.components}
      pagination={false}
    />
  );
}

// ---------------------------------------------------------------------------
// 编辑草稿
// ---------------------------------------------------------------------------

interface EditValues {
  source_currency: string;
  source_reference: string;
  components: RateRow[];
}

/** 一个分量的比较键：两个数按数值比（`1000000` 与 `1000000.00000000` 相同），不经 number。 */
function componentKey(component: ComponentRateBody | PriceComponent): string {
  const quantity = normalizeDecimal(component.unit_quantity) ?? component.unit_quantity;
  const rate = normalizeDecimal(component.rate_amount) ?? component.rate_amount;
  return JSON.stringify([component.component_code, quantity, rate, component.metadata ?? null]);
}

function sameComponents(next: readonly ComponentRateBody[], current: readonly PriceComponent[]): boolean {
  const a = next.map(componentKey).sort();
  const b = current.map(componentKey).sort();
  return a.length === b.length && a.every((key, index) => key === b[index]);
}

/**
 * 表单值 → PATCH 请求体：只带改了的字段（没改的不出现在请求体里）。什么都没改时是 `{}`，调用方
 * 不该发 —— 后端对 `{}` 回 422。
 */
export function draftPatch(version: PriceVersion, values: EditValues): PricePatch {
  const patch: PricePatch = {};
  if (values.source_currency !== version.source_currency) {
    patch.source_currency = values.source_currency;
  }
  const reference = values.source_reference.trim();
  if (reference !== version.source_reference) {
    patch.source_reference = reference;
  }
  const components = toComponentBodies(values.components);
  if (!sameComponents(components, version.components)) {
    patch.components = components;
  }
  return patch;
}

/** 编辑草稿要先拿到目录里的计量类型：旧草稿的分量按它成组，缺的分量列出来等人补。 */
function EditDraftModal({
  version,
  onDone,
  onCancel,
}: {
  version: PriceVersion;
  onDone: () => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const meterTypes = useAllMeterTypes();
  if (meterTypes.data === undefined) {
    return (
      <Modal open title={t("pricing.edit.title")} footer={null} onCancel={onCancel}>
        {meterTypes.isError ? (
          <PricingErrorAlert
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
    <EditDraftForm version={version} meterTypes={meterTypes.data} onDone={onDone} onCancel={onCancel} />
  );
}

function EditDraftForm({
  version,
  meterTypes,
  onDone,
  onCancel,
}: {
  version: PriceVersion;
  meterTypes: MeterType[];
  onDone: () => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const [form] = Form.useForm<EditValues>();
  const [reviewing, setReviewing] = useState<PricePatch | null>(null);
  const [unchanged, setUnchanged] = useState(false);
  const componentRules = useComponentRatesRules(meterTypes);
  const referenceRules = useTextRules(t("pricing.form.referenceRequired"), t("pricing.form.referenceTooLong"));

  const review = (values: EditValues): void => {
    const patch = draftPatch(version, values);
    // 没有实际变化时后端 200、什么都不写；那就根本不该让人去确认一次「什么都没改」。
    if (Object.keys(patch).length === 0) {
      setUnchanged(true);
      return;
    }
    setUnchanged(false);
    setReviewing(patch);
  };

  let changes: ReactNode = null;
  if (reviewing !== null) {
    const items: NonNullable<DescriptionsProps["items"]> = [];
    if (reviewing.source_currency !== undefined) {
      items.push({ key: "currency", label: t("pricing.field.currency"), children: reviewing.source_currency });
    }
    if (reviewing.source_reference !== undefined) {
      items.push({
        key: "reference",
        label: t("pricing.field.sourceReference"),
        children: reviewing.source_reference,
      });
    }
    changes = (
      <>
        {items.length === 0 ? null : <SummaryList items={items} />}
        {reviewing.components === undefined ? null : (
          <>
            <Typography.Paragraph strong>{t("pricing.edit.newComponents")}</Typography.Paragraph>
            <ComponentBodiesTable components={reviewing.components} />
          </>
        )}
      </>
    );
  }

  return (
    <>
      <FormModal title={t("pricing.edit.title")} form={form} width={760} onCancel={onCancel}>
        <Typography.Paragraph>
          <VersionSubject version={version} />
        </Typography.Paragraph>
        <Form<EditValues>
          name="price-edit"
          form={form}
          layout="vertical"
          initialValues={{
            source_currency: version.source_currency,
            source_reference: version.source_reference,
            components: rowsFromVersion(version.components, meterTypes),
          }}
          onValuesChange={() => setUnchanged(false)}
          onFinish={review}
        >
          <Form.Item
            name="source_currency"
            label={t("pricing.field.currency")}
            extra={t("pricing.form.currencyHint")}
            rules={[
              { required: true, message: t("pricing.form.currencyInvalid") },
              { pattern: CURRENCY_PATTERN, message: t("pricing.form.currencyInvalid") },
            ]}
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
            <ComponentRatesInput meterTypes={meterTypes} />
          </Form.Item>
        </Form>
        {unchanged ? <Typography.Text type="danger">{t("pricing.edit.unchanged")}</Typography.Text> : null}
      </FormModal>
      {reviewing === null ? null : (
        <WriteConfirmModal
          title={t("pricing.edit.confirmTitle")}
          confirmLabel={t("pricing.edit.confirm")}
          backLabel={t("pricing.backToEdit")}
          failedTitle={t("pricing.edit.failed")}
          width={760}
          run={() => updateProviderPrice(version.id, reviewing)}
          onDone={onDone}
          onBack={() => setReviewing(null)}
        >
          {changes}
        </WriteConfirmModal>
      )}
    </>
  );
}
