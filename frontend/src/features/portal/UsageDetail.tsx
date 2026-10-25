/**
 * 门户用量页一行展开后的请求详情（AIH-TASK-105；设计闸门 #398 v4 §2「前端」、spec §69）。
 *
 * 显示接口 18 个键里对客户有意义的 16 项：event_id、request_id、conversation_id、feature、项目、供应商、模型、
 * 类型、四个 token、quantity 与 unit、计费额、时间。项目与用量事件的 `id` 是内部引用，不显示。接口本来就不返回
 * 成本、毛利与内部状态（INV-7），这里也没有它们的位置。
 *
 * 本文件后半是用量表与详情共用的显示部件：时间（+8 并标 MYT）、计费额（最多 6 位）、类型与单位的文案。
 * - 计费额按设计 §2「金额」：经 `formatAmountRounded` 做 6 位 `ROUND_HALF_UP`，不转成 number、不另写舍入。
 *   详情里不另外显示 8 位原值。
 * - `quantity` 不是金额，按 `DecimalText` 原样显示。
 * - 认不出的 `usage_type` 与 `unit` 原样显示（设计 §5 最后一行）。
 */

import { Descriptions, Typography, type DescriptionsProps } from "antd";
import { useTranslation } from "react-i18next";

import type { CustomerUsageEvent } from "../../api/customerUsage";
import { formatDateTime, parseUtc } from "../../components/DateTimeText";
import { DecimalText } from "../../components/DecimalText";
import { formatAmountRounded } from "../../components/MoneyText";

/** 没有值的格子。纯标点，不进翻译文件。 */
export const EMPTY = "—";

/** spec §80「Detailed usage may display up to 6 decimals」。 */
const USAGE_PLACES = 6;

/** 计费额（MYR，币种写在列头与标签里）：最多 6 位小数。 */
export function ChargedAmount({ value }: { value: string }) {
  return <span style={{ fontVariantNumeric: "tabular-nums" }}>{formatAmountRounded(value, USAGE_PLACES)}</span>;
}

/**
 * 调用发生时刻：接口给的是不带时区的 UTC（保留微秒），显示成马来西亚时间（固定 UTC+8）并标 MYT，到秒。
 * 认不出的形状原样显示、不标 MYT —— 不给一个没换算过的串贴上时区。
 */
export function UsageTime({ value }: { value: string }) {
  const { t } = useTranslation();
  const instant = parseUtc(value);
  if (instant === null) {
    return <>{value}</>;
  }
  return (
    <time dateTime={instant.toISOString()} title={t("time.displayZone")}>
      {t("portal.usage.time", { time: formatDateTime(value) })}
    </time>
  );
}

/** 计量类型的文案。每个 key 都写成字面量，i18n 的 key 检查才找得到它们；九种之外的原样显示。 */
export function UsageTypeText({ type }: { type: string }) {
  const { t } = useTranslation();
  switch (type) {
    case "LLM_TOKEN":
      return t("portal.usage.usageType.llmToken");
    case "EMBEDDING_TOKEN":
      return t("portal.usage.usageType.embeddingToken");
    case "AUDIO_SECOND":
      return t("portal.usage.usageType.audioSecond");
    case "AUDIO_MINUTE":
      return t("portal.usage.usageType.audioMinute");
    case "TTS_CHARACTER":
      return t("portal.usage.usageType.ttsCharacter");
    case "IMAGE_GENERATION":
      return t("portal.usage.usageType.imageGeneration");
    case "OCR_PAGE":
      return t("portal.usage.usageType.ocrPage");
    case "DOCUMENT_PAGE":
      return t("portal.usage.usageType.documentPage");
    case "CUSTOM":
      return t("portal.usage.usageType.custom");
    default:
      return type;
  }
}

/** 计量单位的文案；认不出的原样显示，没有时「—」。 */
export function UnitText({ unit }: { unit: string | null }) {
  const { t } = useTranslation();
  switch (unit) {
    case null:
      return EMPTY;
    case "TOKEN":
      return t("portal.usage.unit.token");
    case "SECOND":
      return t("portal.usage.unit.second");
    case "MINUTE":
      return t("portal.usage.unit.minute");
    case "CHARACTER":
      return t("portal.usage.unit.character");
    case "IMAGE":
      return t("portal.usage.unit.image");
    case "PAGE":
      return t("portal.usage.unit.page");
    case "UNIT":
      return t("portal.usage.unit.unit");
    default:
      return unit;
  }
}

/** token 数是整数（不是金额），原样转成文字；没有时「—」。 */
export function tokenCount(value: number | null): string {
  return value === null ? EMPTY : String(value);
}

/** 集成方给的标识：可复制；没有时「—」。 */
function Identifier({ value }: { value: string | null }) {
  return value === null ? <>{EMPTY}</> : <Typography.Text copyable>{value}</Typography.Text>;
}

export function UsageDetail({ event }: { event: CustomerUsageEvent }) {
  const { t } = useTranslation();

  const items: NonNullable<DescriptionsProps["items"]> = [
    { key: "event_id", label: t("portal.usage.field.eventId"), children: <Identifier value={event.event_id} /> },
    { key: "request_id", label: t("portal.usage.field.requestId"), children: <Identifier value={event.request_id} /> },
    {
      key: "conversation_id",
      label: t("portal.usage.field.conversationId"),
      children: <Identifier value={event.conversation_id} />,
    },
    { key: "feature", label: t("portal.usage.field.feature"), children: event.feature ?? EMPTY },
    { key: "project", label: t("portal.usage.field.project"), children: event.project_name },
    { key: "provider", label: t("portal.usage.field.provider"), children: event.provider },
    { key: "model", label: t("portal.usage.field.model"), children: event.model },
    { key: "usage_type", label: t("portal.usage.field.usageType"), children: <UsageTypeText type={event.usage_type} /> },
    { key: "input_tokens", label: t("portal.usage.field.inputTokens"), children: tokenCount(event.input_tokens) },
    { key: "output_tokens", label: t("portal.usage.field.outputTokens"), children: tokenCount(event.output_tokens) },
    {
      key: "cache_creation_input_tokens",
      label: t("portal.usage.field.cacheWriteTokens"),
      children: tokenCount(event.cache_creation_input_tokens),
    },
    {
      key: "cache_read_input_tokens",
      label: t("portal.usage.field.cacheReadTokens"),
      children: tokenCount(event.cache_read_input_tokens),
    },
    {
      key: "quantity",
      label: t("portal.usage.field.quantity"),
      children: event.quantity === null ? EMPTY : <DecimalText value={event.quantity} />,
    },
    { key: "unit", label: t("portal.usage.field.unit"), children: <UnitText unit={event.unit} /> },
    {
      key: "charged_amount",
      label: t("portal.usage.field.chargedAmount"),
      children: <ChargedAmount value={event.charged_amount} />,
    },
    { key: "occurred_at", label: t("portal.usage.field.occurredAt"), children: <UsageTime value={event.occurred_at} /> },
  ];

  return (
    // role + aria-label：测试与读屏按语义结构找到这一块（不依赖 antd 的类名）。
    <section role="region" aria-label={t("portal.usage.detail.title")}>
      <Descriptions bordered size="small" column={{ xs: 1, sm: 1, md: 2 }} items={items} />
    </section>
  );
}
