/**
 * 金额的唯一展示方式（INV-10）。
 *
 * 后端给的金额是恰好 8 位小数的十进制字符串（docs/api.md「金额」）。这里**只做
 * 字符串处理**：加千分位、去掉小数末尾多余的 0（至少留 2 位）。
 *
 * ⚠️ 不 `parseFloat`、不 `Number()`、不 `Intl.NumberFormat`：三者都先转成双精度
 * 浮点，`"12345678901.12345678"` 这种 19 位有效数字的值会被悄悄改掉末几位，而
 * 界面上看起来一切正常。展示值必须与后端字符串**在数值上完全相等**：不舍入、不截断。
 */

/** 可选负号、整数部分、可选小数部分。别的形状一律不碰。 */
const DECIMAL = /^(-?)(\d+)(?:\.(\d+))?$/;

/**
 * `"1234567.12345678"` → `"1,234,567.12345678"`，`"0.00000000"` → `"0.00"`。
 *
 * 认不出的输入原样返回：宁可显示一串生的字符，也不显示一个猜出来的数。
 */
export function formatDecimal(amount: string): string {
  const match = DECIMAL.exec(amount);
  if (match === null) {
    return amount;
  }
  const [, sign = "", rawInteger = "", rawFraction = ""] = match;

  const integer = rawInteger.replace(/^0+(?=\d)/, "");
  const fraction = rawFraction.replace(/0+$/, "").padEnd(2, "0");
  const grouped = integer.replace(/\B(?=(\d{3})+(?!\d))/g, ",");

  // 「-0.00000000」在数值上就是 0，显示成「-0.00」只会让人以为欠了钱。
  const isZero = /^0*$/.test(integer + fraction);
  return `${isZero ? "" : sign}${grouped}.${fraction}`;
}

/** `formatDecimal` 前面加上币种：`"MYR 1,234,567.12345678"`。 */
export function formatMoney(amount: string, currency: string): string {
  return `${currency} ${formatDecimal(amount)}`;
}

export function MoneyText({ amount, currency }: { amount: string; currency: string }) {
  return (
    <span style={{ fontVariantNumeric: "tabular-nums" }}>{formatMoney(amount, currency)}</span>
  );
}
