/**
 * 管理端汇率（docs/api.md「管理端汇率」）：版本列表、手工草稿、发布 / 退役 / 丢弃，与 BNM 拉取记录。
 *
 * 顶栏的「Exchange rates」进这一页。页面写明「中间价、吉隆坡中午场、从发布时刻起生效」。两张表各自分页，
 * 三种状态都画出来（spec §132 DoD 第 8 条）：加载中、失败（带 request_id）、空列表。
 *
 * 每个版本按状态给出能做的写操作：
 *
 * - 手工草稿：编辑、发布、丢弃
 * - BNM 草稿：发布、丢弃（**没有编辑**：后端 409 `FX_RATE_NOT_EDITABLE`，要改就丢弃后手工录入）
 * - 已发布且未被截断：退役（已开始的从此刻起该币种取不到汇率；尚未开始的预约被撤销）
 *
 * 写操作一律二次确认，部件与价格页共用（{@link ../pricing/ProviderPricesPage}）。
 *
 * ⚠️ `rate` 按十进制字符串显示与提交（DecimalText），最多 10 位小数，不经 number。
 */

import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Button,
  Card,
  Empty,
  Form,
  Input,
  Space,
  Table,
  Tag,
  Typography,
  type DescriptionsProps,
  type FormRule,
  type TableColumnsType,
} from "antd";
import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { DEFAULT_PAGE_SIZE } from "../../api/adminCustomers";
import {
  FETCH_ATTEMPT_LISTS_QUERY_KEY,
  FX_RATE_LISTS_QUERY_KEY,
  FX_RATE_PATTERN,
  QUOTE_CURRENCY,
  createFxRate,
  discardFxRate,
  fetchAttemptListQueryKey,
  fxRateListQueryKey,
  listFxFetchAttempts,
  listFxRates,
  publishFxRate,
  retireFxRate,
  updateFxRate,
  type CreateFxRateBody,
  type FetchAttempt,
  type FxRate,
  type FxRatePatch,
} from "../../api/adminFxRates";
import { CURRENCY_PATTERN } from "../../api/adminProviderPrices";
import {
  DateTimeText,
  displayTimeToRfc3339,
  displayTimeToUtc,
  utcToDisplayInput,
} from "../../components/DateTimeText";
import { DecimalText, isZeroDecimal, normalizeDecimal } from "../../components/DecimalText";
import { CodeText, LoadingBlock, NoticeAlert, PAGE_SIZE_OPTIONS, SummaryList, usePaging } from "../catalog/ProvidersPage";
import {
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
} from "../pricing/ProviderPricesPage";

/** 没有值的格子。纯标点，不进翻译文件。 */
const EMPTY = "—";

type Action = "edit" | "publish" | "retire" | "discard";

export function FxRatesPage() {
  const [notice, setNotice] = useState<string | null>(null);

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <NoticeAlert message={notice} />
      <RatesPanel onNotice={setNotice} />
      <FetchAttemptsPanel />
    </Space>
  );
}

function RatesPanel({ onNotice }: { onNotice: (message: string | null) => void }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const paging = usePaging(DEFAULT_PAGE_SIZE);
  const [creating, setCreating] = useState(false);
  const [acting, setActing] = useState<{ action: Action; rate: FxRate } | null>(null);

  const rates = useQuery({
    queryKey: fxRateListQueryKey({}, paging.page, paging.pageSize),
    queryFn: ({ signal }) => listFxRates({}, paging.page, paging.pageSize, signal),
    placeholderData: keepPreviousData,
  });

  const finish = (message: string): void => {
    void queryClient.invalidateQueries({ queryKey: FX_RATE_LISTS_QUERY_KEY });
    // 拉取记录里 `NEW_DRAFT` 那一行指向的草稿可能刚被丢弃或发布；记录本身不变，但一起重读不费事。
    void queryClient.invalidateQueries({ queryKey: FETCH_ATTEMPT_LISTS_QUERY_KEY });
    setCreating(false);
    setActing(null);
    onNotice(message);
  };

  const open = (action: Action, rate: FxRate): void => {
    onNotice(null);
    setActing({ action, rate });
  };

  const columns: TableColumnsType<FxRate> = [
    {
      key: "currency",
      title: t("fx.field.currency"),
      render: (_: unknown, rate) => <CodeText code={rate.base_currency} />,
    },
    {
      key: "rate",
      title: t("fx.field.rate"),
      render: (_: unknown, rate) => <DecimalText value={rate.rate} />,
    },
    {
      key: "source",
      title: t("fx.field.source"),
      render: (_: unknown, rate) => <SourceTag source={rate.source} />,
    },
    {
      key: "quote_date",
      title: t("fx.field.quoteDate"),
      render: (_: unknown, rate) => rate.source_quote_date ?? EMPTY,
    },
    {
      key: "observed_at",
      title: t("fx.field.observedAt"),
      render: (_: unknown, rate) => <DateTimeText value={rate.observed_at} />,
    },
    {
      key: "status",
      title: t("pricing.field.status"),
      render: (_: unknown, rate) => <VersionStatusTag status={rate.status} />,
    },
    {
      key: "effective_from",
      title: t("pricing.field.effectiveFrom"),
      render: (_: unknown, rate) => <RangeStart version={rate} />,
    },
    {
      key: "effective_to",
      title: t("pricing.field.effectiveTo"),
      render: (_: unknown, rate) => <RangeEnd version={rate} />,
    },
    {
      key: "approved_by",
      title: t("pricing.field.approvedBy"),
      render: (_: unknown, rate) => rate.approved_by_email ?? EMPTY,
    },
    {
      key: "actions",
      title: t("pricing.field.actions"),
      render: (_: unknown, rate) => <RateActions rate={rate} onOpen={(action) => open(action, rate)} />,
    },
  ];

  let body: ReactNode;
  if (rates.isPending) {
    body = <LoadingBlock text={t("fx.loading")} />;
  } else if (rates.isError) {
    body = (
      <PricingErrorAlert
        error={rates.error}
        title={t("fx.loadFailed")}
        onRetry={() => void rates.refetch()}
      />
    );
  } else if (rates.data.total === 0) {
    body = <Empty description={t("fx.empty")} />;
  } else {
    body = (
      <Table<FxRate>
        rowKey="id"
        columns={columns}
        dataSource={rates.data.items}
        loading={rates.isFetching}
        scroll={{ x: true }}
        locale={{ emptyText: t("fx.emptyPage") }}
        pagination={{
          current: paging.page,
          pageSize: paging.pageSize,
          total: rates.data.total,
          showSizeChanger: true,
          pageSizeOptions: PAGE_SIZE_OPTIONS,
          showTotal: (count) => t("fx.total", { total: count }),
          onChange: paging.goTo,
        }}
      />
    );
  }

  const target = acting?.rate;
  const subject = target === undefined ? null : <RateSubject rate={target} />;

  return (
    <Card
      title={t("fx.title")}
      extra={
        <Button
          type="primary"
          onClick={() => {
            onNotice(null);
            setCreating(true);
          }}
        >
          {t("fx.create")}
        </Button>
      }
    >
      <Typography.Paragraph>
        <Tag color="blue">{t("fx.noteTag")}</Tag>
        <Typography.Text type="secondary">{t("fx.note")}</Typography.Text>
      </Typography.Paragraph>
      <Typography.Paragraph type="secondary">{t("fx.rateMeaning")}</Typography.Paragraph>
      {body}
      {creating ? (
        <RateFormModal onDone={() => finish(t("pricing.create.done"))} onCancel={() => setCreating(false)} />
      ) : null}
      {acting?.action === "edit" ? (
        <RateFormModal
          rate={acting.rate}
          onDone={() => finish(t("pricing.edit.done"))}
          onCancel={() => setActing(null)}
        />
      ) : null}
      {acting?.action === "publish" ? (
        <PublishModal
          subject={subject}
          run={(effectiveFrom) => publishFxRate(acting.rate.id, effectiveFrom)}
          onDone={() => finish(t("pricing.publish.done"))}
          onCancel={() => setActing(null)}
        />
      ) : null}
      {acting?.action === "retire" ? (
        <RetireModal
          subject={subject}
          body={t("fx.retireBody")}
          run={(reason) => retireFxRate(acting.rate.id, reason)}
          onDone={() => finish(t("pricing.retire.done"))}
          onCancel={() => setActing(null)}
        />
      ) : null}
      {acting?.action === "discard" ? (
        <WriteConfirmModal
          title={t("pricing.discard.title")}
          confirmLabel={t("pricing.discard.confirm")}
          backLabel={t("pricing.cancel")}
          danger
          failedTitle={t("pricing.discard.failed")}
          run={() => discardFxRate(acting.rate.id)}
          onDone={() => finish(t("pricing.discard.done"))}
          onBack={() => setActing(null)}
        >
          <Typography.Paragraph>{subject}</Typography.Paragraph>
          <Typography.Paragraph type="secondary">{t("pricing.discard.body")}</Typography.Paragraph>
        </WriteConfirmModal>
      ) : null}
    </Card>
  );
}

/** 每行能做的写操作。BNM 草稿**没有**编辑按钮（出处不同：要改就丢弃后手工录入）。 */
function RateActions({ rate, onOpen }: { rate: FxRate; onOpen: (action: Action) => void }) {
  const { t } = useTranslation();
  const isDraft = rate.status === "DRAFT";
  return (
    <Space wrap size={4}>
      {isDraft && rate.source === "MANUAL" ? (
        <Button size="small" onClick={() => onOpen("edit")}>
          {t("pricing.edit.open")}
        </Button>
      ) : null}
      {isDraft ? (
        <Button size="small" type="primary" onClick={() => onOpen("publish")}>
          {t("pricing.publish.open")}
        </Button>
      ) : null}
      {isDraft ? (
        <Button size="small" danger onClick={() => onOpen("discard")}>
          {t("pricing.discard.open")}
        </Button>
      ) : null}
      {isRetirable(rate) ? (
        <Button size="small" danger onClick={() => onOpen("retire")}>
          {t("pricing.retire.open")}
        </Button>
      ) : null}
    </Space>
  );
}

/** 对话框里说的是哪一个版本：「USD → MYR 4.083」。 */
function RateSubject({ rate }: { rate: FxRate }) {
  return (
    <Space size={4}>
      <CodeText code={rate.base_currency} />
      <span>→</span>
      <CodeText code={rate.quote_currency} />
      <DecimalText value={rate.rate} />
      <SourceTag source={rate.source} />
    </Space>
  );
}

function SourceTag({ source }: { source: string }) {
  const { t } = useTranslation();
  if (source === "BNM") {
    return <Tag color="geekblue">{t("fx.source.bnm")}</Tag>;
  }
  if (source === "MANUAL") {
    return <Tag>{t("fx.source.manual")}</Tag>;
  }
  return <Tag>{source}</Tag>;
}

// ---------------------------------------------------------------------------
// 手工草稿：新建与编辑
// ---------------------------------------------------------------------------

interface RateValues {
  base_currency: string;
  rate: string;
  observed_at: string;
  source_reference: string;
}

/** 确认框里等着发的东西：新建的请求体，或编辑的 PATCH（只含改了的字段）。 */
type RateReview =
  | { kind: "create"; body: CreateFxRateBody }
  | { kind: "edit"; id: string; patch: FxRatePatch };

/**
 * 表单值 → PATCH 请求体：只带改了的字段。`rate` 按数值比（`2.5` 与 `2.5000000000` 相同），
 * 观测时刻按换算后的 UTC 比；都不经 number。什么都没改时是 `{}`，调用方不该发。
 */
export function ratePatch(rate: FxRate, values: RateValues): FxRatePatch {
  const patch: FxRatePatch = {};
  if (normalizeDecimal(values.rate) !== normalizeDecimal(rate.rate)) {
    patch.rate = values.rate;
  }
  const observed = values.observed_at.trim();
  if (displayTimeToUtc(observed) !== rate.observed_at) {
    const rfc3339 = displayTimeToRfc3339(observed);
    if (rfc3339 !== null) {
      patch.observed_at = rfc3339;
    }
  }
  const reference = values.source_reference.trim();
  if (reference !== rate.source_reference) {
    patch.source_reference = reference;
  }
  return patch;
}

/**
 * 新建（`rate` 不给）或编辑一个手工草稿。观测时刻按吉隆坡时间输入，提交前换成带时区的 RFC 3339、
 * 整秒。币种建后不可改，编辑时不出现在表单里。填表 → 确认 → 发请求。
 */
function RateFormModal({
  rate,
  onDone,
  onCancel,
}: {
  rate?: FxRate | undefined;
  onDone: () => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const [form] = Form.useForm<RateValues>();
  const [reviewing, setReviewing] = useState<RateReview | null>(null);
  const [unchanged, setUnchanged] = useState(false);
  const referenceRules = useTextRules(t("pricing.form.referenceRequired"), t("pricing.form.referenceTooLong"));
  const editing = rate !== undefined;

  const currencyRules: FormRule[] = [
    { required: true, message: t("fx.form.currencyInvalid") },
    { pattern: CURRENCY_PATTERN, message: t("fx.form.currencyInvalid") },
    {
      validator: (_rule, value: unknown) =>
        value === QUOTE_CURRENCY ? Promise.reject(new Error(t("fx.form.currencyInvalid"))) : Promise.resolve(),
    },
  ];
  const rateRules: FormRule[] = [
    { required: true, message: t("fx.form.rateRequired") },
    {
      validator: (_rule, value: unknown) =>
        typeof value !== "string" || value === "" || (FX_RATE_PATTERN.test(value) && !isZeroDecimal(value))
          ? Promise.resolve()
          : Promise.reject(new Error(t("fx.form.rateInvalid"))),
    },
  ];
  const observedRule: FormRule = {
    validator: (_rule, value: unknown) =>
      typeof value === "string" && displayTimeToRfc3339(value.trim()) !== null
        ? Promise.resolve()
        : Promise.reject(new Error(t("pricing.publish.atInvalid"))),
  };

  const review = (values: RateValues): void => {
    if (rate !== undefined) {
      const patch = ratePatch(rate, values);
      // 没有实际变化时后端 200、什么都不写；不让人去确认一次「什么都没改」。
      if (Object.keys(patch).length === 0) {
        setUnchanged(true);
        return;
      }
      setReviewing({ kind: "edit", id: rate.id, patch });
      return;
    }
    const observedAt = displayTimeToRfc3339(values.observed_at.trim());
    if (observedAt === null) {
      return;
    }
    setReviewing({
      kind: "create",
      body: {
        // 币种与汇率原样：不改大小写、不经 number。
        base_currency: values.base_currency,
        rate: values.rate,
        observed_at: observedAt,
        source_reference: values.source_reference.trim(),
      },
    });
  };

  const fields: FxRatePatch & { base_currency?: string } =
    reviewing === null ? {} : reviewing.kind === "create" ? reviewing.body : reviewing.patch;
  const summary: NonNullable<DescriptionsProps["items"]> = [];
  if (fields.base_currency !== undefined) {
    summary.push({ key: "currency", label: t("fx.field.currency"), children: <CodeText code={fields.base_currency} /> });
  }
  if (fields.rate !== undefined) {
    summary.push({ key: "rate", label: t("fx.field.rate"), children: <DecimalText value={fields.rate} /> });
  }
  if (fields.observed_at !== undefined) {
    summary.push({
      key: "observed_at",
      label: t("fx.field.observedAt"),
      children: <ObservedAt value={fields.observed_at} />,
    });
  }
  if (fields.source_reference !== undefined) {
    summary.push({
      key: "reference",
      label: t("pricing.field.sourceReference"),
      children: fields.source_reference,
    });
  }

  return (
    <>
      <FormModal title={editing ? t("pricing.edit.title") : t("fx.create")} form={form} onCancel={onCancel}>
        {rate === undefined ? null : (
          <Typography.Paragraph>
            <RateSubject rate={rate} />
          </Typography.Paragraph>
        )}
        <Form<RateValues>
          name="fx-rate"
          form={form}
          layout="vertical"
          initialValues={{
            base_currency: rate?.base_currency ?? "",
            rate: rate?.rate ?? "",
            observed_at: rate === undefined ? "" : utcToDisplayInput(rate.observed_at),
            source_reference: rate?.source_reference ?? "",
          }}
          onValuesChange={() => setUnchanged(false)}
          onFinish={review}
        >
          {editing ? null : (
            <Form.Item
              name="base_currency"
              label={t("fx.field.currency")}
              extra={t("fx.form.currencyHint")}
              rules={currencyRules}
            >
              <Input autoComplete="off" spellCheck={false} maxLength={3} />
            </Form.Item>
          )}
          <Form.Item
            name="rate"
            label={t("fx.field.rate")}
            extra={t("fx.form.rateHint")}
            rules={rateRules}
          >
            <Input autoComplete="off" inputMode="decimal" />
          </Form.Item>
          <Form.Item name="observed_at" label={t("fx.form.observedAt")} rules={[observedRule]}>
            <Input type="datetime-local" step={1} />
          </Form.Item>
          <Form.Item
            name="source_reference"
            label={t("pricing.field.sourceReference")}
            extra={t("fx.form.referenceHint")}
            rules={referenceRules}
          >
            <Input autoComplete="off" />
          </Form.Item>
        </Form>
        {unchanged ? <Typography.Text type="danger">{t("pricing.edit.unchanged")}</Typography.Text> : null}
      </FormModal>
      {reviewing === null ? null : (
        <WriteConfirmModal
          title={editing ? t("pricing.edit.confirmTitle") : t("fx.confirmCreate")}
          confirmLabel={editing ? t("pricing.edit.confirm") : t("pricing.create.confirm")}
          backLabel={t("pricing.backToEdit")}
          failedTitle={editing ? t("pricing.edit.failed") : t("pricing.create.failed")}
          run={() =>
            reviewing.kind === "create"
              ? createFxRate(reviewing.body)
              : updateFxRate(reviewing.id, reviewing.patch)
          }
          onDone={onDone}
          onBack={() => setReviewing(null)}
        >
          <SummaryList items={summary} />
          <Typography.Paragraph type="secondary">{t("pricing.create.draftNote")}</Typography.Paragraph>
        </WriteConfirmModal>
      )}
    </>
  );
}

/** 观测时刻：吉隆坡时间给人看。值是带 `Z` 的 RFC 3339，去掉 `Z` 就是后端的不带时区 UTC 写法。 */
function ObservedAt({ value }: { value: string }) {
  return <DateTimeText value={value.endsWith("Z") ? value.slice(0, -1) : value} />;
}

// ---------------------------------------------------------------------------
// BNM 拉取记录
// ---------------------------------------------------------------------------

function FetchAttemptsPanel() {
  const { t } = useTranslation();
  const paging = usePaging(DEFAULT_PAGE_SIZE);

  const attempts = useQuery({
    queryKey: fetchAttemptListQueryKey({}, paging.page, paging.pageSize),
    queryFn: ({ signal }) => listFxFetchAttempts({}, paging.page, paging.pageSize, signal),
    placeholderData: keepPreviousData,
  });

  const columns: TableColumnsType<FetchAttempt> = [
    {
      key: "attempted_at",
      title: t("fx.attempts.field.attemptedAt"),
      render: (_: unknown, attempt) => <DateTimeText value={attempt.attempted_at} />,
    },
    {
      key: "currency",
      title: t("fx.field.currency"),
      render: (_: unknown, attempt) => <CodeText code={attempt.base_currency} />,
    },
    { key: "requested_date", title: t("fx.attempts.field.requestedDate"), dataIndex: "requested_date" },
    {
      key: "outcome",
      title: t("fx.attempts.field.outcome"),
      render: (_: unknown, attempt) => <OutcomeTag outcome={attempt.outcome} />,
    },
    {
      key: "quote_date",
      title: t("fx.field.quoteDate"),
      render: (_: unknown, attempt) => attempt.quote_date ?? EMPTY,
    },
    {
      key: "error_code",
      title: t("fx.attempts.field.errorCode"),
      render: (_: unknown, attempt) =>
        attempt.error_code === null ? EMPTY : <CodeText code={attempt.error_code} />,
    },
  ];

  let body: ReactNode;
  if (attempts.isPending) {
    body = <LoadingBlock text={t("fx.attempts.loading")} />;
  } else if (attempts.isError) {
    body = (
      <PricingErrorAlert
        error={attempts.error}
        title={t("fx.attempts.loadFailed")}
        onRetry={() => void attempts.refetch()}
      />
    );
  } else if (attempts.data.total === 0) {
    body = <Empty description={t("fx.attempts.empty")} />;
  } else {
    body = (
      <Table<FetchAttempt>
        // 拉取记录没有 id（只读、不可单条引用）；同一页里按位置区分即可。
        rowKey={(attempt) => `${attempt.attempted_at}:${attempt.base_currency}:${attempt.requested_date}:${attempt.outcome}`}
        columns={columns}
        dataSource={attempts.data.items}
        loading={attempts.isFetching}
        locale={{ emptyText: t("fx.attempts.emptyPage") }}
        pagination={{
          current: paging.page,
          pageSize: paging.pageSize,
          total: attempts.data.total,
          showSizeChanger: true,
          pageSizeOptions: PAGE_SIZE_OPTIONS,
          showTotal: (count) => t("fx.attempts.total", { total: count }),
          onChange: paging.goTo,
        }}
      />
    );
  }

  return (
    <Card title={t("fx.attempts.title")}>
      <Typography.Paragraph type="secondary">{t("fx.attempts.note")}</Typography.Paragraph>
      {body}
    </Card>
  );
}

function OutcomeTag({ outcome }: { outcome: string }) {
  const { t } = useTranslation();
  switch (outcome) {
    case "NEW_DRAFT":
      return <Tag color="green">{t("fx.outcome.newDraft")}</Tag>;
    case "NO_NEW_QUOTE":
      return <Tag>{t("fx.outcome.noNewQuote")}</Tag>;
    case "NO_QUOTE_FOR_DATE":
      return <Tag>{t("fx.outcome.noQuoteForDate")}</Tag>;
    case "FAILED":
      return <Tag color="red">{t("fx.outcome.failed")}</Tag>;
    default:
      return <Tag>{outcome}</Tag>;
  }
}
