/**
 * 时刻的唯一展示方式：后端的 UTC 换成 Asia/Kuala_Lumpur（docs/api.md「时间」，spec §109）。
 *
 * ⚠️ 后端给的是**不带时区**的 ISO 8601，例如 `"2026-09-20T08:30:00"`。直接交给
 * `new Date(...)` 的话，浏览器会把它当成**本地时间**解析 —— 在吉隆坡的机器上恰好
 * 少算 8 小时，而在 UTC 的 CI 上又恰好是对的，测试发现不了。所以这里自己按 UTC
 * 拆字段，再用 `Intl` 换时区，不引入日期库。
 */

export const DISPLAY_TIME_ZONE = "Asia/Kuala_Lumpur";

/** 不带时区的 UTC，精确到秒；容忍小数秒（展示时丢掉）。 */
const NAIVE_UTC = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?$/;

const FORMAT = new Intl.DateTimeFormat("en-GB", {
  timeZone: DISPLAY_TIME_ZONE,
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  // ⚠️ 显式 h23：只写 `hour12: false` 时，有的引擎把午夜写成 "24:00:00"。
  hourCycle: "h23",
});

/**
 * `"2026-09-20T08:30:00"`（UTC）→ `"2026-09-20 16:30:00"`（吉隆坡）。
 *
 * 不是契约里的形状时返回 `null`。
 */
export function formatUtcDateTime(value: string): string | null {
  const match = NAIVE_UTC.exec(value);
  if (match === null) {
    return null;
  }
  // 正则匹配上了，六组就一定都在；默认值只是给 `noUncheckedIndexedAccess` 看的。
  const [, year = "", month = "", day = "", hour = "", minute = "", second = ""] = match;
  const instant = Date.UTC(
    Number(year),
    Number(month) - 1,
    Number(day),
    Number(hour),
    Number(minute),
    Number(second),
  );
  // `Date.UTC` 会把 2 月 30 日静默滚成 3 月 2 日。回读一遍，对不上就是坏值。
  if (
    Number.isNaN(instant) ||
    !new Date(instant).toISOString().startsWith(`${year}-${month}-${day}T${hour}:${minute}:${second}`)
  ) {
    return null;
  }

  const parts = FORMAT.formatToParts(instant);
  const part = (type: Intl.DateTimeFormatPartTypes): string =>
    parts.find((candidate) => candidate.type === type)?.value ?? "";
  return `${part("year")}-${part("month")}-${part("day")} ${part("hour")}:${part("minute")}:${part("second")}`;
}

export function DateTimeText({ value }: { value: string }) {
  const shown = formatUtcDateTime(value);
  if (shown === null) {
    // 不合契约的值原样显示，不猜时区。
    return <span>{value}</span>;
  }
  return <time dateTime={`${value}Z`}>{shown}</time>;
}
