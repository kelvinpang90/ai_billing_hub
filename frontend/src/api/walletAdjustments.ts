/**
 * 管理端手工调账（契约见 docs/api.md「管理端手工调账」，设计闸门 #111，
 * 全文 docs/design/AIH-TASK-010-admin-wallet-adjustment.md）。
 *
 * 只有一个接口：`POST /api/v1/admin/customers/{customer_id}/wallet/adjustments`。
 * 首次 201、同一个幂等键重放 200（`replayed: true`），两者响应形状相同。
 *
 * ⚠️ 金额是**字符串**（INV-10），请求与响应都是。这里的校验与加符号只做字符串运算，
 * 不 `parseFloat`、不 `Number()`、不舍入：后端对超过 8 位小数的金额回 422 而不是舍入，
 * 前端在这里舍入一次，记进账本的就不是管理员敲的那个数了。
 */

import { AxiosError } from "axios";

import { ApiError, client, toApiError, unwrapEnvelope, type ApiEnvelope } from "./client";

/** 这个幂等键已经记过，但客户、类型或金额不同。什么都没写。 */
export const ADJUSTMENT_CONFLICT = "ADJUSTMENT_CONFLICT";
/** 这一笔之后余额超出 `DECIMAL(20,8)`。什么都没写。 */
export const BALANCE_OUT_OF_RANGE = "BALANCE_OUT_OF_RANGE";

/** 调账只收这四种；`TOPUP`、`AI_USAGE`、`SYSTEM_CORRECTION` 等后端一律 422。 */
export const ADJUSTMENT_TYPES = [
  "ADJUSTMENT_CREDIT",
  "BONUS",
  "ADJUSTMENT_DEBIT",
  "REFUND_ADJUSTMENT",
] as const;

export type AdjustmentType = (typeof ADJUSTMENT_TYPES)[number];

/** 贷方（金额为正）。其余两种是借方（金额为负）。 */
const CREDIT_TYPES: ReadonlySet<AdjustmentType> = new Set(["ADJUSTMENT_CREDIT", "BONUS"]);

export function isCreditType(type: AdjustmentType): boolean {
  return CREDIT_TYPES.has(type);
}

export interface AdjustmentBody {
  transaction_type: AdjustmentType;
  /** 带符号的十进制字符串，就是记进账本的那个数。贷方为正、借方为负。 */
  amount: string;
  /** 去掉首尾空白后 1–255。只写业务说明：它进账本与审计，永久保留。 */
  reason: string;
  /** 小写 uuid。每次打开调账表单生成一个，重试带同一个。 */
  idempotency_key: string;
}

/** 调账对象。`id` 到 `created_at` 取自账本行，重放时与第一次逐字相同。 */
export interface Adjustment {
  id: string;
  customer_id: string;
  transaction_type: string;
  /** 以下三个都是恰好 8 位小数的字符串。 */
  amount: string;
  balance_before: string;
  balance_after: string;
  wallet_sequence: number;
  reason: string;
  idempotency_key: string;
  created_at: string;
  /** `true`：这次是重放，没有新的入账，返回的是第一次那一行。 */
  replayed: boolean;
  /** **提交时**客户的计费状态与版本。 */
  billing_status: string;
  status_version: number;
}

// ⚠️ 用 [0-9] 而不是 \d，与后端 `app/schemas/wallet_adjustments.py` 一致：\d 在别的
// 引擎里还认全角数字。这里是管理员输入的**不带符号**的金额，符号由类型决定。
const UNSIGNED_AMOUNT = /^[0-9]{1,12}(\.[0-9]{1,8})?$/;
const ZERO = /^0+(\.0+)?$/;

export type AmountProblem = "required" | "format" | "zero";

/**
 * 管理员输入的金额有什么问题；没问题返回 `null`。
 *
 * 规则照 docs/api.md：1–12 位整数、可选 1–8 位小数、不能为 0。符号、`+`、指数、
 * 千分位、全角数字都算格式不对。首尾空白先去掉（粘贴时常带上）。
 */
export function amountProblem(value: unknown): AmountProblem | null {
  const text = typeof value === "string" ? value.trim() : "";
  if (text === "") {
    return "required";
  }
  if (!UNSIGNED_AMOUNT.test(text)) {
    return "format";
  }
  if (ZERO.test(text)) {
    return "zero";
  }
  return null;
}

/**
 * 不带符号的金额 → 请求里带符号的 `amount`：贷方原样、借方前面加 `-`。
 *
 * 调用方先用 {@link amountProblem} 确认过它合法。这里只拼字符串，数字一位不动。
 */
export function signedAmount(type: AdjustmentType, unsigned: string): string {
  const text = unsigned.trim();
  return isCreditType(type) ? text : `-${text}`;
}

/**
 * 调账的错误：一个带 HTTP 状态的 {@link ApiError}。没有响应（连不上、超时）时状态是 `null`。
 *
 * 状态要留着，因为调用方必须分清「后端明确拒绝了」与「不知道记没记上」——
 * 见 {@link isOutcomeUnknown}。
 */
export class AdjustmentError extends ApiError {
  constructor(
    code: string,
    message: string,
    requestId: string | null,
    readonly status: number | null,
  ) {
    super(code, message, requestId);
    this.name = "AdjustmentError";
  }
}

/**
 * 后端**明确拒绝**、什么都没写的状态：docs/api.md 为这个接口列出的 4xx。
 * 这几种之外一律当成「结果未知」。
 */
const DEFINITE_REJECTIONS: ReadonlySet<number> = new Set([400, 401, 403, 404, 409, 422]);

/**
 * 这次失败之后，钱到底记没记上**不知道**吗？
 *
 * 网络错误、超时（没有响应）、5xx、代理回的非信封页面、以及上表之外的任何状态都算
 * 不知道：请求可能已经在服务端提交，只是响应没回来。这时只能带**同一个**幂等键重发 ——
 * 若其实已经记上，重发走重放（200、`replayed: true`），不会记第二笔。
 *
 * ⚠️ 宁可错判成「不知道」：错判的代价是管理员多点一次重试；反过来的代价是重复调账。
 * 所以不是 {@link AdjustmentError}、拿不到状态的错误也算不知道。
 */
export function isOutcomeUnknown(error: unknown): boolean {
  return !(
    error instanceof AdjustmentError &&
    error.status !== null &&
    DEFINITE_REJECTIONS.has(error.status)
  );
}

function adjustmentsUrl(customerId: string): string {
  // 与 adminCustomers 同一个理由：id 来自地址栏，不转义的话 `a/b` 会变成另一条路径。
  return `/api/v1/admin/customers/${encodeURIComponent(customerId)}/wallet/adjustments`;
}

/**
 * 记一笔调账。201 与 200（重放）都返回调账对象，看 `replayed` 区分。
 *
 * 失败一律抛 {@link AdjustmentError}（它是 `ApiError`）。
 *
 * ⚠️ 这里**不自动重试**：要不要重试、带哪个键，是表单的事（见 `AdjustmentModal`）。
 */
export async function postAdjustment(
  customerId: string,
  body: AdjustmentBody,
): Promise<Adjustment> {
  let status: number | null = null;
  try {
    const response = await client.post<ApiEnvelope<Adjustment>>(adjustmentsUrl(customerId), body);
    status = response.status;
    return unwrapEnvelope<Adjustment>(response.data);
  } catch (error) {
    if (error instanceof AxiosError && error.response !== undefined) {
      status = error.response.status;
    }
    const apiError = toApiError(error);
    throw new AdjustmentError(apiError.code, apiError.message, apiError.requestId, status);
  }
}
