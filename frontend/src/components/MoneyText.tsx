/**
 * 金额的唯一展示方式：币种 + 千分位，小数只去掉末尾多余的 0、至少留 2 位。
 *
 * ⚠️ INV-10：金额从头到尾是**十进制字符串**，这里只做字符串变换 —— 不
 * `parseFloat`、不 `Number()`、不 `Intl.NumberFormat`（它也要先变成浮点数）。
 * `"1234567.12345678"` 转成浮点数再格式化，末几位就已经不是后端给的那个数了。
 *
 * 不舍入、不截断：展示值与后端字符串在数值上完全相等，只是写法不同。
 */

/** 可选负号、整数部分、可选小数部分。后端给的是恰好 8 位小数，这里不依赖位数。 */
const DECIMAL = /^(-?)(\d+)(?:\.(\d+))?$/;

/** 至少保留的小数位数。 */
const MIN_FRACTION_DIGITS = 2;

/**
 * `"1234567.12345678"` → `"1,234,567.12345678"`，`"0.00000000"` → `"0.00"`。
 *
 * 不是十进制字符串时返回 `null`，由调用方原样显示 —— 猜一个数出来比显示原文更糟。
 */
export function formatMoney(amount: string): string | null {
  const match = DECIMAL.exec(amount);
  if (match === null) {
    return null;
  }
  const [, sign = "", rawInteger = "0", rawFraction = ""] = match;

  const integer = rawInteger.replace(/^0+(?=\d)/, "");
  const grouped = integer.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const fraction = rawFraction.replace(/0+$/, "").padEnd(MIN_FRACTION_DIGITS, "0");

  // "-0.00000000" 在数值上就是 0，不显示成「负零」。
  const isZero = /^0+$/.test(rawInteger) && /^0*$/.test(rawFraction);
  return `${isZero ? "" : sign}${grouped}.${fraction}`;
}

export function MoneyText({ amount, currency }: { amount: string; currency: string }) {
  const shown = formatMoney(amount) ?? amount;
  return <span style={{ fontVariantNumeric: "tabular-nums" }}>{`${currency} ${shown}`}</span>;
}
