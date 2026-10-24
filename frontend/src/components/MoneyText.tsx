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

const ROUNDED_PLACES = [2, 3, 4, 5, 6, 7, 8];
const DIGITS = "0123456789";

/** 十进制数字串加一：`"0999"` → `"1000"`，`"999"` → `"1000"`。 */
function incrementDigits(digits: string): string {
  const carryFrom = digits.search(/9*$/);
  const tail = "0".repeat(digits.length - carryFrom);
  if (carryFrom === 0) {
    return `1${tail}`;
  }
  const head = digits.slice(0, carryFrom - 1);
  const bumped = DIGITS.charAt(DIGITS.indexOf(digits.charAt(carryFrom - 1)) + 1);
  return `${head}${bumped}${tail}`;
}

/**
 * 客户门户的显示金额（docs/design/AIH-TASK-099-customer-wallet.md §2「金额（前端显示）」）：
 * 对字符串做 `ROUND_HALF_UP` 到 `places` 位，再按 `formatAmount` 的规则展示。
 * `"0.995"` 按 2 位 → `"1.00"`，`"-0.00000049"` 按 6 位 → `"0.00"`。
 *
 * 只改显示，不改持久值；`places` 只接受 2 到 8 的整数，其余抛 `RangeError`。
 * 不是十进制字符串的输入原样返回，与 `formatAmount` 相同。
 */
export function formatAmountRounded(amount: string, places: number): string {
  if (!ROUNDED_PLACES.includes(places)) {
    throw new RangeError(`places must be an integer from 2 to 8, got ${places}`);
  }
  const match = DECIMAL.exec(amount);
  if (match === null) {
    return amount;
  }
  const [, sign = "", integer = "", fraction = ""] = match;

  const padded = fraction.padEnd(places + 1, "0");
  const kept = `${integer}${padded.slice(0, places)}`;
  // 只看紧跟着的那一位：HALF_UP 下它 ≥ 5 就进位，更后面的位不影响结果。
  const rounded = padded.charAt(places) >= "5" ? incrementDigits(kept) : kept;
  const split = rounded.length - places;

  return formatAmount(`${sign}${rounded.slice(0, split)}.${rounded.slice(split)}`);
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
