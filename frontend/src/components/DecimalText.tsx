/**
 * 不带币种的十进制数的唯一展示方式：单价、汇率、倍数与数量（AIH-TASK-036）。
 *
 * 与 {@link ./MoneyText} 同一条规矩（INV-10）：后端给的是十进制**字符串**，这里**只做字符串
 * 操作** —— 去掉多余的前导 0 与末尾 0、整数部分加千分位。不舍入、不截断，所以显示出来的值与
 * 后端字符串在数值上完全相等。与金额不同，小数部分不补到 2 位：`"1000000.00000000"` 个单位就是
 * `1,000,000`，汇率 `"4.083"` 就是 `4.083`。
 *
 * ⚠️ 不许 `parseFloat` / `Number()` / `Intl.NumberFormat`：汇率有 10 位小数、单价有 8 位，
 * 一经浮点数，`"0.1000000001"` 这类值就已经不是后端给的那个数了。
 */

const DECIMAL = /^(-?)(\d+)(?:\.(\d+))?$/;

/**
 * 十进制字符串的规范写法：`"007.50"` → `"7.5"`，`"1000000.00000000"` → `"1000000"`，
 * `"-0.000"` → `"0"`。不是十进制字符串时返回 `null`。
 *
 * 两个字符串数值相等，当且仅当它们的规范写法相同 —— 比较「改没改」就靠它，不经过 number。
 */
export function normalizeDecimal(value: string): string | null {
  const match = DECIMAL.exec(value);
  if (match === null) {
    return null;
  }
  const [, sign = "", integer = "", fraction = ""] = match;
  const whole = integer.replace(/^0+(?=\d)/, "");
  const decimals = fraction.replace(/0+$/, "");
  const isZero = whole === "0" && decimals === "";
  return `${isZero ? "" : sign}${whole}${decimals === "" ? "" : `.${decimals}`}`;
}

/** 数值上是不是 0（`"0"`、`"0.00000000"`、`"000.0"`）。不是十进制字符串时为 `false`。 */
export function isZeroDecimal(value: string): boolean {
  return normalizeDecimal(value) === "0";
}

/**
 * `"1000000.00000000"` → `"1,000,000"`，`"1234.5678901234"` → `"1,234.5678901234"`。
 *
 * 不是十进制字符串的输入**原样返回**：宁可让人看到一个奇怪的串，也不猜它是什么数。
 */
export function formatDecimal(value: string): string {
  const normalized = normalizeDecimal(value);
  if (normalized === null) {
    return value;
  }
  const [whole = "", decimals] = normalized.split(".");
  const sign = whole.startsWith("-") ? "-" : "";
  const digits = sign === "" ? whole : whole.slice(1);
  const grouped = digits.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return decimals === undefined ? `${sign}${grouped}` : `${sign}${grouped}.${decimals}`;
}

export function DecimalText({ value }: { value: string }) {
  return <span style={{ fontVariantNumeric: "tabular-nums" }}>{formatDecimal(value)}</span>;
}
