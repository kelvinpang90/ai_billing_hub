/**
 * 时间的唯一展示方式。
 *
 * 后端的 `created_at` / `updated_at` 是**不带时区的 UTC**，例如 `"2026-09-20T08:30:00"`
 * （docs/api.md「时间」、spec §109）。浏览器的 `Date` 会把不带时区的日期时间串当成
 * **本地时间**解析 —— 在吉隆坡的电脑上看不出错，换一台机器就差 8 小时。所以这里先
 * 显式补上 `Z`，再用 `Intl` 换算成 Asia/Kuala_Lumpur。不引入日期库。
 */

import { useTranslation } from "react-i18next";

export const DISPLAY_TIME_ZONE = "Asia/Kuala_Lumpur";

/** 不带时区的 ISO 8601 日期时间，秒可以带小数。 */
const NAIVE_ISO = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?$/;

const FORMAT = new Intl.DateTimeFormat("en-US", {
  timeZone: DISPLAY_TIME_ZONE,
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  // ⚠️ 不写的话，部分 ICU 版本把午夜显示成「24:00:00」。
  hourCycle: "h23",
});

/** 把后端的不带时区 UTC 串解析成时刻；不认识的形状返回 `null`，不猜。 */
export function parseUtc(value: string): Date | null {
  if (!NAIVE_ISO.test(value)) {
    return null;
  }
  const instant = new Date(`${value}Z`);
  return Number.isNaN(instant.getTime()) ? null : instant;
}

/** `"2026-09-20T08:30:00"` → `"2026-09-20 16:30:00"`（吉隆坡时间）。认不出的原样返回。 */
export function formatDateTime(value: string): string {
  const instant = parseUtc(value);
  if (instant === null) {
    return value;
  }
  // 逐段取出再拼，不依赖某个 locale 的分隔符写法（不同 ICU 版本不一样）。
  const parts = FORMAT.formatToParts(instant);
  const part = (type: Intl.DateTimeFormatPartTypes): string =>
    parts.find((candidate) => candidate.type === type)?.value ?? "";
  return `${part("year")}-${part("month")}-${part("day")} ${part("hour")}:${part("minute")}:${part("second")}`;
}

export function DateTimeText({ value }: { value: string }) {
  const { t } = useTranslation();
  const instant = parseUtc(value);
  return (
    <time dateTime={instant?.toISOString() ?? value} title={t("time.displayZone")}>
      {formatDateTime(value)}
    </time>
  );
}
