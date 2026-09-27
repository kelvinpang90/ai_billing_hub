/**
 * 时间的唯一展示方式。
 *
 * 后端的 `created_at` / `updated_at` 是**不带时区的 UTC**（docs/api.md「时间」，
 * spec §109），例如 `"2026-09-20T08:30:00"`。展示时换成 Asia/Kuala_Lumpur。
 *
 * ⚠️ 不能直接 `new Date(value)`：ECMAScript 把不带时区的「日期 + 时间」按**浏览器
 * 本地时区**解析。在吉隆坡的浏览器里，UTC 08:30 会被当成本地 08:30，整整错 8 小时，
 * 而在 UTC 时区的 CI 上测试照样全绿。所以这里先拆字段、按 UTC 组装，再用 `Intl`
 * 换到目标时区 —— 不引入日期库。
 */

export const DISPLAY_TIME_ZONE = "Asia/Kuala_Lumpur";

/** 不带时区的 ISO 8601，秒后面允许有小数。带 `Z` 或偏移的不认，免得换算两次。 */
const NAIVE_UTC = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?$/;

const FORMATTER = new Intl.DateTimeFormat("en-GB", {
  timeZone: DISPLAY_TIME_ZONE,
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hourCycle: "h23",
});

/**
 * `"2026-09-20T08:30:00"` → `"2026-09-20 16:30:00"`（吉隆坡时间）。
 *
 * 认不出的输入原样返回：宁可显示后端的原串，也不显示一个换算错的时刻。
 */
export function formatDateTime(value: string): string {
  const match = NAIVE_UTC.exec(value);
  if (match === null) {
    return value;
  }
  const field = (index: number): number => Number.parseInt(match[index] ?? "", 10);
  const instant = new Date(
    Date.UTC(field(1), field(2) - 1, field(3), field(4), field(5), field(6)),
  );
  if (Number.isNaN(instant.getTime())) {
    return value;
  }

  // 按部件取值再自己拼：`format()` 的整串格式随 locale 数据变化，部件不会。
  const parts = FORMATTER.formatToParts(instant);
  const part = (type: Intl.DateTimeFormatPartTypes): string =>
    parts.find((candidate) => candidate.type === type)?.value ?? "";
  return (
    `${part("year")}-${part("month")}-${part("day")} ` +
    `${part("hour")}:${part("minute")}:${part("second")}`
  );
}

export function DateTimeText({ value }: { value: string }) {
  return <time dateTime={`${value}Z`}>{formatDateTime(value)}</time>;
}
