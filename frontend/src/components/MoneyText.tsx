/**
 * 金额的唯一展示方式（INV-10）。
 *
 * 后端给的金额是恰好 8 位小数的十进制字符串（docs/api.md「金额」）。这里**只做字符串
 * 操作**：整数部分加千分位，小数部分去掉末尾多余的 0、至少留 2 位。不舍入、不截断，
 * 所以显示出来的值与后端字符串在数值上完全相等。
 *
 * ⚠️ 不许 `parseFloat` / `Number()` / `Intl.NumberFormat`：前两个把 `"0.10000001"`
 * 这类值变成二进制浮点的近似值；`Intl.NumberFormat` 收的是 number（或按实现不同的
 * 字符串精度），而且默认只保留 3 位小数 —— 那是**舍入**，账上的钱会凭空少一截。
 */

const DECIMAL = /^(-?)(\d+)(?:\.(\d+))?$/;
const MIN_FRACTION_DIGITS = 2;

/**
 * `"1234567.12345678"` → `"1,234,567.12345678"`，`"0.00000000"` → `"0.00"`。
 *
 * 不是十进制字符串的输入**原样返回**：宁可让人看到一个奇怪的串，也不猜它是什么数。
 */
export function formatAmount(amount: string): string {
  const match = DECIMAL.exec(amount);
  if (match === null) {
    return amount;
  }
  const [, sign = "", integer = "", fraction = ""] = match;

  const grouped = integer.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const trimmed = fraction.replace(/0+$/, "").padEnd(MIN_FRACTION_DIGITS, "0");
  // 「-0.00」与「0.00」数值相等，但前者让人以为欠了钱。
  const isZero = /^0*$/.test(integer) && /^0*$/.test(fraction);

  return `${isZero ? "" : sign}${grouped}.${trimmed}`;
}

/** 币种在前：`MYR 1,234,567.12345678`。 */
export function formatMoney(amount: string, currency: string): string {
  return `${currency} ${formatAmount(amount)}`;
}

export function MoneyText({ amount, currency }: { amount: string; currency: string }) {
  return (
    <span style={{ fontVariantNumeric: "tabular-nums" }}>{formatMoney(amount, currency)}</span>
  );
}
