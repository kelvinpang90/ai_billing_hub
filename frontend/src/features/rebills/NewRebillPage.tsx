/**
 * 管理端新建 Rebill（`/rebills/new`，AIH-TASK-260；设计闸门 #830 v12 §2「管理端页面」与 §5「提交时或提交后连接中断」）。
 *
 * 语义以 docs/api.md「管理端 Rebill」为准：先 `POST /api/v1/admin/rebills/preview`（只读），再 `POST /api/v1/admin/rebills`
 * （必填原因、带幂等键）。
 *
 * 表单：Customer（客户 `public_id`，同用量事件页的 customer_id 筛选）、Selection 二选一 —— Usage events（多行文本框，每行一个
 * 事件 `public_id`，去空行、去重）或 Date range（From、To 按吉隆坡时间输入，换成带时区的 RFC 3339 发送，`[From, To)` 不含 To
 * 这一刻；Project、Provider、Model 可选）、两个更正版本（`public_id`，都可空；都空时写明「按当前有效版本重算」）。查询参数
 * `customer_id` 与 `usage_event_id`（用量事件详情的「Rebill」按钮）预填 Customer 与 Usage events。
 *
 * 预览与确认：
 *
 * - 客户与选择齐全前 Preview 不可点。预览结果之后，改 Reason 之外的任何字段都作废这份预览（改 Reason 不作废：原因不参与
 *   预览指纹）。预览的 `ledger_type` 为 `null`（没有可入账的差额）时 Confirm 不可点。
 * - **幂等键在第一次点 Confirm 时生成一次**（`crypto.randomUUID()`，小写），连同预览指纹与去首尾空白后的原因定成整个请求体
 *   （`attempt`）。从这一刻起整个表单（含 Reason）只读，直到得到结果。
 * - 结果不明（`ApiError` 的码为 NETWORK_ERROR、MALFORMED_RESPONSE、INTERNAL_ERROR、DATABASE_UNAVAILABLE —— `ApiError` 不带
 *   HTTP 状态，以码判定）：可能已完整提交，也可能已完整回滚。只给 Retry（**原样重发同一请求体**：同一幂等键、同一原因；已提交
 *   时服务端按幂等键返回原结果）与 Start over（放弃：清空预览与幂等键，提示换新键之前必须重新预览 —— 新预览已反映第一次是否
 *   提交）。不允许改字段。
 * - 409 `REBILL_PREVIEW_STALE`：清空预览与幂等键，提示重新预览。其余错误码是服务端明确拒绝、什么都没写：同样清空预览与幂等键、
 *   解锁表单，显示该码的文案。
 * - 201 或 200（`replayed`）：跳到该 Rebill 的详情页。
 *
 * 停复机提示：当前余额 > 0 而预估余额 ≤ 0 时停机警告，当前 ≤ 0 而预估 > 0 时复机提示。比较用 {@link balanceTransition}
 * （字符串转 8 位定点整数，不经浮点）。金额都原样显示接口给的字符串。
 *
 * ⚠️ 说明与提示不用 antd 的 `Alert`，也不用 `Form.Item` 的校验规则（错误列表带 `role="alert"`）：部署后浏览器验收把页面上
 * 任何 `role="alert"` 都当成错误提示（scripts/acceptance/admin_customers.mjs 的 check_new_rebill_form）。只请求
 * `/api/v1/admin/` 路径。
 */

import { useMutation } from "@tanstack/react-query";
import {
  Button,
  Card,
  DatePicker,
  Form,
  Input,
  Radio,
  Space,
  Table,
  Typography,
  type DatePickerProps,
  type DescriptionsProps,
  type TableColumnsType,
} from "antd";
import { useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate, useSearchParams } from "react-router";

import { VALIDATION_ERROR } from "../../api/adminAudit";
import { ADMIN_REQUIRED, CUSTOMER_NOT_FOUND } from "../../api/adminCustomers";
import { AMOUNT_OUT_OF_RANGE } from "../../api/adminPricingPreview";
import { PRICING_RULE_NOT_FOUND } from "../../api/adminPricingRules";
import { PRICE_VERSION_NOT_FOUND } from "../../api/adminProviderPrices";
import {
  REBILL_PREVIEW_STALE,
  confirmRebill,
  previewRebill,
  type RebillConfirmRequest,
  type RebillCriteriaSelection,
  type RebillLine,
  type RebillPreview,
  type RebillRequest,
  type RebillSelection,
} from "../../api/adminRebills";
import { USAGE_EVENT_NOT_FOUND } from "../../api/adminUsageEvents";
import { ApiError, MALFORMED_RESPONSE, NETWORK_ERROR } from "../../api/client";
import { DateTimeText, displayTimeToRfc3339 } from "../../components/DateTimeText";
import { RequestReference } from "../../components/RequestReference";
import {
  REBILL_CUSTOMER_PARAM,
  REBILL_USAGE_EVENT_PARAM,
  ROUTES,
  rebillDetailPath,
  usageEventDetailPath,
} from "../../routes/paths";
import { SummaryList } from "../catalog/ProvidersPage";
import { EMPTY } from "../usage/UsageEventsPage";
import { useLedgerLabel } from "./RebillsPage";

// --- 错误码（docs/api.md「管理端 Rebill」的「错误」） -----------------------------------------

const PROJECT_NOT_FOUND = "PROJECT_NOT_FOUND";
const REBILL_NOT_PREPAID = "REBILL_NOT_PREPAID";
const USAGE_EVENT_NOT_REBILLABLE = "USAGE_EVENT_NOT_REBILLABLE";
const NOTHING_TO_REBILL = "NOTHING_TO_REBILL";
const REBILL_NET_ZERO = "REBILL_NET_ZERO";
const REBILL_CONFLICT = "REBILL_CONFLICT";
const ACCOUNT_CLOSED = "ACCOUNT_CLOSED";
const REBILL_SELECTION_TOO_LARGE = "REBILL_SELECTION_TOO_LARGE";
const REBILL_PRICE_NOT_APPLICABLE = "REBILL_PRICE_NOT_APPLICABLE";
const REBILL_RULE_NOT_APPLICABLE = "REBILL_RULE_NOT_APPLICABLE";

/**
 * 确认的结果不明：连不上 / 超时、响应不是信封、500、503。可能已完整提交，也可能已完整回滚（设计 §5），只能同键重发。
 * `ApiError` 不带 HTTP 状态，所以按码判定。
 */
const UNCERTAIN_CODES: ReadonlySet<string> = new Set([
  NETWORK_ERROR,
  MALFORMED_RESPONSE,
  "INTERNAL_ERROR",
  "DATABASE_UNAVAILABLE",
]);

/** 原因的长度上限（docs/api.md「请求体」：去首尾空白后 1–255 字符）。 */
const REASON_MAX = 255;

const CUSTOMER_FIELD_ID = "new-rebill-customer";
const EVENT_IDS_FIELD_ID = "new-rebill-event-ids";
const FROM_FIELD_ID = "new-rebill-from";
const TO_FIELD_ID = "new-rebill-to";
const PROJECT_FIELD_ID = "new-rebill-project";
const PROVIDER_FIELD_ID = "new-rebill-provider";
const MODEL_FIELD_ID = "new-rebill-model";
const RULE_FIELD_ID = "new-rebill-pricing-rule";
const PRICE_VERSION_FIELD_ID = "new-rebill-price-version";
const REASON_FIELD_ID = "new-rebill-reason";

/** 选择器里显示与输入的格式：吉隆坡墙上时间，精确到秒。 */
const PICKER_FORMAT = "YYYY-MM-DD HH:mm:ss";
/** 交给 `displayTimeToRfc3339` 的墙上时间写法（`<input type="datetime-local">` 的形状）。 */
const WALL_TIME_FORMAT = "YYYY-MM-DDTHH:mm:ss";

/** 单个时刻（不开 `multiple`），去掉 antd 类型里多选时的数组形状。 */
type PickerValue = Exclude<NonNullable<DatePickerProps["value"]>, readonly unknown[]>;

/** 选择器 onChange 给的值收成单个时刻；清空（或意外的多选数组）当作没选。 */
function singlePickerValue(value: DatePickerProps["value"] | null): PickerValue | null {
  if (value === null || value === undefined || Array.isArray(value)) {
    return null;
  }
  return value;
}

// --- 金额比较 ---------------------------------------------------------------------

/** 金额的小数位（docs/api.md：恰好 8 位小数的十进制字符串）。 */
const SCALE = 8;
const UNIT = 100_000_000n;
const DECIMAL = /^(-?)(\d+)(?:\.(\d+))?$/;

/** 十进制字符串 → 8 位定点整数（不经浮点）。形状不对或超过 8 位小数返回 `null`，不猜。 */
export function toFixedPoint(value: string): bigint | null {
  const match = DECIMAL.exec(value.trim());
  if (match === null) {
    return null;
  }
  const [, sign = "", whole = "0", fraction = ""] = match;
  if (fraction.length > SCALE) {
    return null;
  }
  const units = BigInt(whole) * UNIT + BigInt(fraction.padEnd(SCALE, "0"));
  return sign === "-" ? -units : units;
}

/**
 * 确认后计费状态会不会变（设计 §4 跃迁表：余额 > 0 为 ACTIVE、≤ 0 为 SUSPENDED，正好为 0 也算停机）。
 * 当前余额 > 0 而预估 ≤ 0 是 `suspend`，当前 ≤ 0 而预估 > 0 是 `resume`，其余（或认不出的金额）为 `null`。
 */
export function balanceTransition(balance: string, balanceAfter: string): "suspend" | "resume" | null {
  const before = toFixedPoint(balance);
  const after = toFixedPoint(balanceAfter);
  if (before === null || after === null) {
    return null;
  }
  if (before > 0n && after <= 0n) {
    return "suspend";
  }
  if (before <= 0n && after > 0n) {
    return "resume";
  }
  return null;
}

// --- 表单 -----------------------------------------------------------------------

type SelectionMode = "events" | "range";

interface Draft {
  customerId: string;
  mode: SelectionMode;
  /** 多行文本框的原值，每行一个事件 `public_id`。 */
  eventIds: string;
  from: PickerValue | null;
  to: PickerValue | null;
  projectId: string;
  provider: string;
  model: string;
  ruleId: string;
  priceVersionId: string;
  reason: string;
}

/** 查询参数 `customer_id`、`usage_event_id` 预填 Customer 与 Usage events（用量事件详情的「Rebill」按钮）。 */
function initialDraft(params: URLSearchParams): Draft {
  return {
    customerId: params.get(REBILL_CUSTOMER_PARAM) ?? "",
    mode: "events",
    eventIds: params.get(REBILL_USAGE_EVENT_PARAM) ?? "",
    from: null,
    to: null,
    projectId: "",
    provider: "",
    model: "",
    ruleId: "",
    priceVersionId: "",
    reason: "",
  };
}

/** 每行一个 id：去首尾空白、去空行、去重（后端对重复的 id 是 422），顺序照输入。 */
export function parseEventIds(text: string): string[] {
  const ids: string[] = [];
  for (const line of text.split(/\r?\n/)) {
    const id = line.trim();
    if (id !== "" && !ids.includes(id)) {
      ids.push(id);
    }
  }
  return ids;
}

/** 选择器的吉隆坡墙上时间 → 带时区的 RFC 3339（UTC、整秒）：`2026-11-01 08:00:00` → `2026-11-01T00:00:00Z`。 */
export function pickerToRfc3339(value: PickerValue | null): string | null {
  return value === null ? null : displayTimeToRfc3339(value.format(WALL_TIME_FORMAT));
}

function optional(value: string): string | null {
  const trimmed = value.trim();
  return trimmed === "" ? null : trimmed;
}

/** 表单 → 预览请求体。客户或选择不齐全时返回 `null`（Preview 不可点）。 */
function buildRequest(draft: Draft): RebillRequest | null {
  const customerId = draft.customerId.trim();
  if (customerId === "") {
    return null;
  }
  let selection: RebillSelection;
  if (draft.mode === "events") {
    const ids = parseEventIds(draft.eventIds);
    if (ids.length === 0) {
      return null;
    }
    selection = { usage_event_ids: ids };
  } else {
    const from = pickerToRfc3339(draft.from);
    const to = pickerToRfc3339(draft.to);
    if (from === null || to === null) {
      return null;
    }
    const criteria: RebillCriteriaSelection = { occurred_from: from, occurred_to: to };
    const projectId = optional(draft.projectId);
    const provider = optional(draft.provider);
    const model = optional(draft.model);
    if (projectId !== null) {
      criteria.project_id = projectId;
    }
    if (provider !== null) {
      criteria.provider = provider;
    }
    if (model !== null) {
      criteria.model = model;
    }
    selection = criteria;
  }
  return {
    customer_id: customerId,
    selection,
    corrective_pricing_rule_id: optional(draft.ruleId),
    corrective_provider_price_version_id: optional(draft.priceVersionId),
  };
}

// --- 页面 -----------------------------------------------------------------------

interface Failure {
  error: Error;
  /** `uncertain`：确认结果不明，只能 Retry 或 Start over。 */
  stage: "preview" | "confirm" | "uncertain";
}

type Notice = "stale" | "startedOver";

function isUncertain(error: Error): boolean {
  // confirmRebill 抛的都是 ApiError；万一不是，也按结果不明处理：宁可同键重发，不换新键。
  return !(error instanceof ApiError) || UNCERTAIN_CODES.has(error.code);
}

export function NewRebillPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const [draft, setDraft] = useState<Draft>(() => initialDraft(searchParams));
  const [preview, setPreview] = useState<{ body: RebillRequest; result: RebillPreview } | null>(null);
  // 第一次点 Confirm 时定下的整个请求体（含幂等键与原因）。不为 null 时表单只读，Retry 原样重发它。
  const [attempt, setAttempt] = useState<RebillConfirmRequest | null>(null);
  const [failure, setFailure] = useState<Failure | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  // 每次改动（Reason 除外）加一：预览回来时对不上，说明这份结果对应的已经不是表单上的值了。
  const editVersion = useRef(0);
  // `isPending` 要等下一次渲染才变 true，挡不住同一帧里的第二次点击。
  const sending = useRef(false);

  const previewing = useMutation({ mutationFn: (body: RebillRequest) => previewRebill(body) });
  const confirmation = useMutation({ mutationFn: (body: RebillConfirmRequest) => confirmRebill(body) });
  const busy = previewing.isPending || confirmation.isPending;
  const locked = attempt !== null;
  const request = buildRequest(draft);
  const failureTitle = useFailureTitle();

  /** 改 Reason 之外的任何字段：作废当前预览。 */
  const change = (patch: Partial<Omit<Draft, "reason">>): void => {
    if (locked) {
      return;
    }
    editVersion.current += 1;
    setDraft((previous) => ({ ...previous, ...patch }));
    setPreview(null);
    setFailure(null);
    setNotice(null);
  };

  /** 改 Reason：不作废预览（原因不参与预览指纹）。 */
  const changeReason = (reason: string): void => {
    if (!locked) {
      setDraft((previous) => ({ ...previous, reason }));
    }
  };

  const runPreview = (): void => {
    if (request === null || locked) {
      return;
    }
    const version = editVersion.current;
    setPreview(null);
    setFailure(null);
    setNotice(null);
    previewing.mutate(request, {
      onSuccess: (result) => {
        if (version === editVersion.current) {
          setPreview({ body: request, result });
        }
      },
      onError: (error) => {
        if (version === editVersion.current) {
          setFailure({ error, stage: "preview" });
        }
      },
    });
  };

  const send = (body: RebillConfirmRequest): void => {
    if (sending.current) {
      return;
    }
    sending.current = true;
    setFailure(null);
    confirmation.mutate(body, {
      onSuccess: (result) => {
        void navigate(rebillDetailPath(result.id));
      },
      onError: (error) => {
        if (isUncertain(error)) {
          setFailure({ error, stage: "uncertain" });
          return;
        }
        // 服务端明确拒绝、什么都没写：预览与幂等键一起作废，表单解锁。
        setAttempt(null);
        setPreview(null);
        if (error instanceof ApiError && error.code === REBILL_PREVIEW_STALE) {
          setNotice("stale");
        } else {
          setFailure({ error, stage: "confirm" });
        }
      },
      onSettled: () => {
        sending.current = false;
      },
    });
  };

  const reason = draft.reason.trim();
  const canConfirm =
    preview !== null && preview.result.ledger_type !== null && reason !== "" && !locked && !busy;

  const confirm = (): void => {
    if (!canConfirm || preview === null) {
      return;
    }
    const body: RebillConfirmRequest = {
      ...preview.body,
      preview_token: preview.result.preview_token,
      reason,
      idempotency_key: crypto.randomUUID().toLowerCase(),
    };
    setAttempt(body);
    setNotice(null);
    send(body);
  };

  const retry = (): void => {
    if (attempt !== null) {
      send(attempt);
    }
  };

  /** 放弃重试：预览与幂等键都清掉。换新键之前必须重新预览（新预览已反映第一次是否提交）。 */
  const startOver = (): void => {
    if (sending.current) {
      return;
    }
    setAttempt(null);
    setPreview(null);
    setFailure(null);
    setNotice("startedOver");
  };

  // 结果不明：只给 Retry 与 Start over。
  const uncertain = locked && failure !== null && failure.stage === "uncertain" ? failure : null;
  const bothVersionsEmpty = draft.ruleId.trim() === "" && draft.priceVersionId.trim() === "";

  return (
    <Card title={t("rebills.new.title")} extra={<Link to={ROUTES.rebills}>{t("rebills.detail.backToList")}</Link>}>
      <Typography.Paragraph type="secondary">{t("rebills.new.intro")}</Typography.Paragraph>
      <Form layout="vertical" component="div" disabled={locked}>
        <Form.Item
          label={t("rebills.field.customer")}
          htmlFor={CUSTOMER_FIELD_ID}
          extra={t("rebills.new.field.customerHint")}
        >
          <Input
            id={CUSTOMER_FIELD_ID}
            value={draft.customerId}
            autoComplete="off"
            spellCheck={false}
            onChange={(event) => change({ customerId: event.target.value })}
          />
        </Form.Item>
        <Form.Item label={t("rebills.new.field.selection")}>
          <Radio.Group value={draft.mode} onChange={(event) => change({ mode: event.target.value as SelectionMode })}>
            <Radio value="events">{t("rebills.new.selection.events")}</Radio>
            <Radio value="range">{t("rebills.new.selection.range")}</Radio>
          </Radio.Group>
        </Form.Item>
        {draft.mode === "events" ? (
          <Form.Item
            label={t("rebills.new.field.eventIds")}
            htmlFor={EVENT_IDS_FIELD_ID}
            extra={t("rebills.new.field.eventIdsHint")}
          >
            <Input.TextArea
              id={EVENT_IDS_FIELD_ID}
              value={draft.eventIds}
              rows={4}
              autoComplete="off"
              spellCheck={false}
              onChange={(event) => change({ eventIds: event.target.value })}
            />
          </Form.Item>
        ) : (
          <>
            <Form.Item label={t("rebills.new.field.from")} htmlFor={FROM_FIELD_ID}>
              <DatePicker
                id={FROM_FIELD_ID}
                showTime
                format={PICKER_FORMAT}
                value={draft.from}
                onChange={(value) => change({ from: singlePickerValue(value) })}
              />
            </Form.Item>
            <Form.Item label={t("rebills.new.field.to")} htmlFor={TO_FIELD_ID} extra={t("rebills.new.field.toHint")}>
              <DatePicker
                id={TO_FIELD_ID}
                showTime
                format={PICKER_FORMAT}
                value={draft.to}
                onChange={(value) => change({ to: singlePickerValue(value) })}
              />
            </Form.Item>
            <Typography.Paragraph type="secondary">{t("rebills.new.cutoffHint")}</Typography.Paragraph>
            <Form.Item label={t("rebills.new.field.projectId")} htmlFor={PROJECT_FIELD_ID}>
              <Input
                id={PROJECT_FIELD_ID}
                value={draft.projectId}
                autoComplete="off"
                spellCheck={false}
                onChange={(event) => change({ projectId: event.target.value })}
              />
            </Form.Item>
            <Form.Item
              label={t("rebills.new.field.provider")}
              htmlFor={PROVIDER_FIELD_ID}
              extra={t("rebills.new.field.catalogCodeHint")}
            >
              <Input
                id={PROVIDER_FIELD_ID}
                value={draft.provider}
                autoComplete="off"
                spellCheck={false}
                onChange={(event) => change({ provider: event.target.value })}
              />
            </Form.Item>
            <Form.Item
              label={t("rebills.new.field.model")}
              htmlFor={MODEL_FIELD_ID}
              extra={t("rebills.new.field.catalogCodeHint")}
            >
              <Input
                id={MODEL_FIELD_ID}
                value={draft.model}
                autoComplete="off"
                spellCheck={false}
                onChange={(event) => change({ model: event.target.value })}
              />
            </Form.Item>
          </>
        )}
        <Form.Item
          label={t("rebills.detail.pricingRule")}
          htmlFor={RULE_FIELD_ID}
          extra={t("rebills.new.field.pricingRuleHint")}
        >
          <Input
            id={RULE_FIELD_ID}
            value={draft.ruleId}
            autoComplete="off"
            spellCheck={false}
            onChange={(event) => change({ ruleId: event.target.value })}
          />
        </Form.Item>
        <Form.Item
          label={t("rebills.detail.priceVersion")}
          htmlFor={PRICE_VERSION_FIELD_ID}
          extra={t("rebills.new.field.priceVersionHint")}
        >
          <Input
            id={PRICE_VERSION_FIELD_ID}
            value={draft.priceVersionId}
            autoComplete="off"
            spellCheck={false}
            onChange={(event) => change({ priceVersionId: event.target.value })}
          />
        </Form.Item>
        {bothVersionsEmpty ? (
          <Typography.Paragraph type="secondary">{t("rebills.new.currentVersions")}</Typography.Paragraph>
        ) : null}
      </Form>
      <Button
        type="primary"
        onClick={runPreview}
        disabled={request === null || locked || busy}
        loading={previewing.isPending}
      >
        {t("rebills.new.preview")}
      </Button>

      {notice === null ? null : (
        <Typography.Paragraph style={{ marginTop: 16 }}>
          <Typography.Text type="warning">
            {notice === "stale" ? t("rebills.new.stale") : t("rebills.new.startedOver")}
          </Typography.Text>
        </Typography.Paragraph>
      )}
      {failure === null || failure.stage === "uncertain" ? null : (
        <FailureText
          error={failure.error}
          title={failureTitle(failure.error, failure.stage === "preview" ? t("rebills.new.previewFailed") : t("rebills.new.confirmFailed"))}
        />
      )}

      {preview === null ? null : (
        <div role="region" aria-label={t("rebills.new.previewTitle")} style={{ marginTop: 24 }}>
          <Typography.Title level={5}>{t("rebills.new.previewTitle")}</Typography.Title>
          <PreviewResult result={preview.result} />
          <Form layout="vertical" component="div" disabled={locked}>
            <Form.Item label={t("rebills.field.reason")} htmlFor={REASON_FIELD_ID} extra={t("rebills.new.reasonHint")}>
              <Input
                id={REASON_FIELD_ID}
                value={draft.reason}
                maxLength={REASON_MAX}
                autoComplete="off"
                onChange={(event) => changeReason(event.target.value)}
              />
            </Form.Item>
          </Form>
          {uncertain === null ? null : (
            <>
              <Typography.Paragraph>
                <Typography.Text type="warning">{t("rebills.new.uncertain")}</Typography.Text>
              </Typography.Paragraph>
              <FailureText error={uncertain.error} title={t("rebills.new.confirmFailed")} />
            </>
          )}
          <Space>
            <Button type="primary" onClick={confirm} disabled={!canConfirm} loading={confirmation.isPending}>
              {t("rebills.new.confirm")}
            </Button>
            {uncertain !== null ? (
              <>
                <Button onClick={retry} disabled={busy}>
                  {t("rebills.new.retry")}
                </Button>
                <Button onClick={startOver} disabled={busy}>
                  {t("rebills.new.startOver")}
                </Button>
              </>
            ) : null}
          </Space>
        </div>
      )}
    </Card>
  );
}

/** 预览的合计、账本方向与余额变化、停复机提示与明细表。金额原样显示。 */
function PreviewResult({ result }: { result: RebillPreview }) {
  const { t } = useTranslation();
  const ledgerLabel = useLedgerLabel();
  const transition = balanceTransition(result.balance, result.balance_after);

  const summary: NonNullable<DescriptionsProps["items"]> = [
    { key: "events", label: t("rebills.new.summary.events"), children: String(result.event_count) },
    { key: "unchanged", label: t("rebills.new.summary.unchanged"), children: String(result.unchanged_count) },
    { key: "previous_total", label: t("rebills.detail.previousTotal"), children: result.previous_total },
    { key: "corrected_total", label: t("rebills.detail.correctedTotal"), children: result.corrected_total },
    { key: "difference", label: t("rebills.detail.difference"), children: result.difference },
    {
      key: "ledger",
      label: t("rebills.field.ledger"),
      children: result.ledger_type === null ? EMPTY : ledgerLabel(result.ledger_type),
    },
    { key: "ledger_amount", label: t("rebills.detail.ledgerAmount"), children: result.ledger_amount ?? EMPTY },
    { key: "balance", label: t("rebills.new.summary.balance"), children: result.balance },
    { key: "balance_after", label: t("rebills.new.summary.balanceAfter"), children: result.balance_after },
  ];

  const columns: TableColumnsType<RebillLine> = [
    {
      key: "usage_event",
      title: t("rebills.line.usageEvent"),
      render: (_: unknown, line) => <Link to={usageEventDetailPath(line.usage_event_id)}>{line.usage_event_id}</Link>,
    },
    {
      key: "occurred_at",
      title: t("rebills.line.occurred"),
      render: (_: unknown, line) => <DateTimeText value={line.occurred_at} />,
    },
    { key: "previous_charge", title: t("rebills.line.previous"), dataIndex: "previous_charge" },
    { key: "corrected_charge", title: t("rebills.line.corrected"), dataIndex: "corrected_charge" },
    { key: "difference", title: t("rebills.line.difference"), dataIndex: "difference" },
    { key: "original_period", title: t("rebills.line.originalPeriod"), dataIndex: "original_period" },
    {
      key: "statement",
      title: t("rebills.line.statement"),
      render: (_: unknown, line) => line.original_statement_id ?? EMPTY,
    },
  ];

  return (
    <>
      <SummaryList items={summary} />
      <Typography.Paragraph type="secondary">{t("rebills.new.balanceNote")}</Typography.Paragraph>
      {transition === "suspend" ? (
        <Typography.Paragraph>
          <Typography.Text type="danger" strong>
            {t("rebills.new.suspend")}
          </Typography.Text>
        </Typography.Paragraph>
      ) : null}
      {transition === "resume" ? (
        <Typography.Paragraph>
          <Typography.Text type="success" strong>
            {t("rebills.new.resume")}
          </Typography.Text>
        </Typography.Paragraph>
      ) : null}
      {result.ledger_type === null ? (
        <Typography.Paragraph>
          <Typography.Text type="warning">{t("rebills.new.noLedger")}</Typography.Text>
        </Typography.Paragraph>
      ) : null}
      <Table<RebillLine>
        rowKey="usage_event_id"
        columns={columns}
        dataSource={result.lines}
        pagination={false}
        scroll={{ x: true }}
        style={{ marginBottom: 16 }}
      />
    </>
  );
}

/** 按错误码取文案；认不出的码用调用方给的标题（下面照样带后端 message 与 request_id）。 */
function useFailureTitle(): (error: Error, fallback: string) => string {
  const { t } = useTranslation();
  return (error, fallback) => {
    switch (error instanceof ApiError ? error.code : null) {
      case ADMIN_REQUIRED:
        return t("rebills.forbidden");
      case CUSTOMER_NOT_FOUND:
        return t("rebills.new.error.customerNotFound");
      case USAGE_EVENT_NOT_FOUND:
        return t("rebills.new.error.usageEventNotFound");
      case PROJECT_NOT_FOUND:
        return t("rebills.new.error.projectNotFound");
      case PRICING_RULE_NOT_FOUND:
        return t("rebills.new.error.pricingRuleNotFound");
      case PRICE_VERSION_NOT_FOUND:
        return t("rebills.new.error.priceVersionNotFound");
      case REBILL_NOT_PREPAID:
        return t("rebills.new.error.notPrepaid");
      case USAGE_EVENT_NOT_REBILLABLE:
        return t("rebills.new.error.notRebillable");
      case NOTHING_TO_REBILL:
        return t("rebills.new.error.nothingToRebill");
      case REBILL_NET_ZERO:
        return t("rebills.new.error.netZero");
      case REBILL_CONFLICT:
        return t("rebills.new.error.conflict");
      case ACCOUNT_CLOSED:
        return t("rebills.new.error.accountClosed");
      case VALIDATION_ERROR:
        return t("rebills.new.error.validation");
      case REBILL_SELECTION_TOO_LARGE:
        return t("rebills.new.error.tooLarge");
      case REBILL_PRICE_NOT_APPLICABLE:
        return t("rebills.new.error.priceNotApplicable");
      case REBILL_RULE_NOT_APPLICABLE:
        return t("rebills.new.error.ruleNotApplicable");
      case AMOUNT_OUT_OF_RANGE:
        return t("rebills.new.error.amountOutOfRange");
      default:
        return fallback;
    }
  };
}

/**
 * 失败时的一段普通文字：标题、错误码、后端 message（适用性错误里带第一个不适用事件的 `public_id`）与 request_id。
 * 不是 `Alert`，没有 `role="alert"`。
 */
function FailureText({ error, title }: { error: Error; title: string }) {
  const code = error instanceof ApiError ? error.code : null;
  return (
    <Typography.Paragraph style={{ marginTop: 16 }}>
      <Typography.Text type="danger">
        <span>{title}</span>
        {code === null ? null : (
          <>
            {" "}
            <Typography.Text code>{code}</Typography.Text>
          </>
        )}{" "}
        <RequestReference error={error} />
      </Typography.Text>
    </Typography.Paragraph>
  );
}
