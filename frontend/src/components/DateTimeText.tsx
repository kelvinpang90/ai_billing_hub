/**
 * 时间的唯一展示方式。
 *
 * 后端的 `created_at` / `updated_at` 是**不带时区的 UTC**，例如 `"2026-09-20T08:30:00"`
 * （docs/api.md「时间」、spec §109）。浏览器的 `Date` 会把不带时区的日期时间串当成
 * **本地时间**解析 —— 在吉隆坡的电脑上看不出错，换一台机器就差 8 小时。所以这里先
 * 显式补上 `Z`，再用 `Intl` 换算成 Asia/Kuala_Lumpur。不引入日期库。
 *
 * 反方向（用户按吉隆坡时间输入 → 发给后端的 UTC）也放在这里：`displayTimeToUtc`
 * （AIH-TASK-023 审计页的时间段筛选）。两个方向用同一个时区来源。
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

/** 这个时刻在吉隆坡的墙上时间，逐段取出（不依赖某个 locale 的分隔符写法，不同 ICU 版本不一样）。 */
function displayParts(instant: Date): (type: Intl.DateTimeFormatPartTypes) => string {
  const parts = FORMAT.formatToParts(instant);
  return (type) => parts.find((candidate) => candidate.type === type)?.value ?? "";
}

/** `"2026-09-20T08:30:00"` → `"2026-09-20 16:30:00"`（吉隆坡时间）。认不出的原样返回。 */
export function formatDateTime(value: string): string {
  const instant = parseUtc(value);
  if (instant === null) {
    return value;
  }
  const part = displayParts(instant);
  return `${part("year")}-${part("month")}-${part("day")} ${part("hour")}:${part("minute")}:${part("second")}`;
}

/**
 * 吉隆坡的墙上时间，即 `<input type="datetime-local">` 的值。秒可以省略。
 *
 * ⚠️ 秒不为零时，规范化后的值可能带毫秒（jsdom 一律写成 `07:59:59.000`，浏览器在
 * step 小于 1 时也会）。毫秒认下来再舍掉：后端只收到秒。
 */
const WALL_TIME = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2})(?:\.\d{1,3})?)?$/;

/** 某个整秒时刻上，吉隆坡比 UTC 快多少毫秒。 */
function displayOffsetMs(instantMs: number): number {
  const part = displayParts(new Date(instantMs));
  const wall = Date.UTC(
    Number(part("year")),
    Number(part("month")) - 1,
    Number(part("day")),
    Number(part("hour")),
    Number(part("minute")),
    Number(part("second")),
  );
  return wall - instantMs;
}

/**
 * {@link formatDateTime} 的反方向：吉隆坡时间 → 后端查询参数要的不带时区 UTC。
 *
 * `"2026-09-21T00:00"` → `"2026-09-20T16:00:00"`（跨回前一天）。输出恰好是后端收的
 * 那一种写法（`YYYY-MM-DDTHH:MM:SS`，docs/api.md「管理端审计日志」）。形状不对、或者
 * 日历上不存在（`2026-02-30`、`24:00`）返回 `null` —— 不让 `Date.UTC` 把它悄悄滚到
 * 下个月再发出去。
 *
 * 偏移量用 `Intl` 现算，不写死 +8：与 {@link formatDateTime} 同一个时区来源，两边不会
 * 各说各话。吉隆坡没有夏令时，算一次就准；多算一次是为了不依赖这个事实。
 */
export function displayTimeToUtc(value: string): string | null {
  const match = WALL_TIME.exec(value);
  if (match === null) {
    return null;
  }
  const [year, month, day, hour, minute, second] = match.slice(1).map((group) => Number(group ?? "0"));
  if (
    year === undefined ||
    month === undefined ||
    day === undefined ||
    hour === undefined ||
    minute === undefined ||
    second === undefined ||
    hour > 23 ||
    minute > 59 ||
    second > 59
  ) {
    return null;
  }
  const wall = Date.UTC(year, month - 1, day, hour, minute, second);
  const check = new Date(wall);
  if (
    check.getUTCFullYear() !== year ||
    check.getUTCMonth() !== month - 1 ||
    check.getUTCDate() !== day
  ) {
    return null;
  }
  const guess = wall - displayOffsetMs(wall);
  const instant = wall - displayOffsetMs(guess);
  return new Date(instant).toISOString().slice(0, 19);
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
