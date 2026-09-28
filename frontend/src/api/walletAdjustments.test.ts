/**
 * 调账请求发到哪、带什么，金额的字符串校验与加符号，以及「结果未知」的判定。
 *
 * ⚠️ 表单用例把 `postAdjustment` mock 掉了，路径与字段名写错时它们照样全绿；
 * 这里补上。`isOutcomeUnknown` 判错一次就是一笔重复调账，所以每种状态都列出来。
 */

import { AxiosError, AxiosHeaders, type AxiosResponse } from "axios";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, client } from "./client";
import {
  AdjustmentError,
  amountProblem,
  isOutcomeUnknown,
  postAdjustment,
  signedAmount,
  type AdjustmentBody,
} from "./walletAdjustments";

const CUSTOMER_ID = "00000000-0000-4000-8000-000000000000";
const KEY = "00000000-0000-4000-8000-000000000000";

const BODY: AdjustmentBody = {
  transaction_type: "ADJUSTMENT_DEBIT",
  amount: "-20.5",
  reason: "Service credit reversal",
  idempotency_key: KEY,
};

function envelope(data: unknown, status = 201) {
  return { data: { success: true, data, error: null, request_id: "req-1" }, status };
}

function failure(status: number | null, code = "SOME_ERROR", message = "It failed.") {
  if (status === null) {
    return new AxiosError("timeout of 15000ms exceeded", "ECONNABORTED");
  }
  const response = {
    data: { success: false, data: null, error: { code, message }, request_id: `req-${status}` },
    status,
    statusText: "",
    headers: {},
    config: { headers: new AxiosHeaders() },
  } satisfies AxiosResponse;
  return new AxiosError("Request failed", "ERR_BAD_RESPONSE", undefined, undefined, response);
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("postAdjustment", () => {
  it("posts the body as given, amount still a string, to the customer's wallet", async () => {
    const post = vi
      .spyOn(client, "post")
      .mockResolvedValue(envelope({ id: "row", amount: "-20.50000000", replayed: false }));

    const adjustment = await postAdjustment(CUSTOMER_ID, BODY);

    expect(post).toHaveBeenCalledWith(
      `/api/v1/admin/customers/${CUSTOMER_ID}/wallet/adjustments`,
      BODY,
    );
    const sent = post.mock.calls[0]?.[1] as AdjustmentBody;
    expect(typeof sent.amount).toBe("string");
    expect(adjustment).toEqual({ id: "row", amount: "-20.50000000", replayed: false });
  });

  it("escapes the customer id instead of letting it change the path", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope({}));

    await postAdjustment("a/../b", BODY);

    expect(post).toHaveBeenCalledWith(
      "/api/v1/admin/customers/a%2F..%2Fb/wallet/adjustments",
      BODY,
    );
  });

  it("turns a 409 into an ApiError that carries the backend message, request id and status", async () => {
    vi.spyOn(client, "post").mockRejectedValue(
      failure(409, "ADJUSTMENT_CONFLICT", "The idempotency key was already used."),
    );

    const attempt = postAdjustment(CUSTOMER_ID, BODY);

    await expect(attempt).rejects.toBeInstanceOf(ApiError);
    await expect(attempt).rejects.toMatchObject({
      code: "ADJUSTMENT_CONFLICT",
      message: "The idempotency key was already used.",
      requestId: "req-409",
      status: 409,
    });
  });

  it("reports no status when there was no response at all", async () => {
    vi.spyOn(client, "post").mockRejectedValue(failure(null));

    await expect(postAdjustment(CUSTOMER_ID, BODY)).rejects.toMatchObject({
      code: "NETWORK_ERROR",
      status: null,
    });
  });
});

describe("isOutcomeUnknown", () => {
  it.each([400, 401, 403, 404, 409, 422])("is known after a %i: nothing was written", (status) => {
    expect(isOutcomeUnknown(new AdjustmentError("X", "x", null, status))).toBe(false);
  });

  it.each([500, 502, 503, 504, 408, 429, 201])("is unknown after a %i", (status) => {
    expect(isOutcomeUnknown(new AdjustmentError("X", "x", null, status))).toBe(true);
  });

  it("is unknown after a network error or timeout", () => {
    expect(isOutcomeUnknown(new AdjustmentError("NETWORK_ERROR", "x", null, null))).toBe(true);
  });

  it("is unknown for an error that carries no status", () => {
    expect(isOutcomeUnknown(new ApiError("CUSTOMER_NOT_FOUND", "x", null))).toBe(true);
    expect(isOutcomeUnknown(new TypeError("boom"))).toBe(true);
  });

  it("is unknown when a 5xx comes back through postAdjustment", async () => {
    vi.spyOn(client, "post").mockRejectedValue(failure(500, "INTERNAL_ERROR"));

    const error: unknown = await postAdjustment(CUSTOMER_ID, BODY).catch((caught: unknown) => caught);

    expect(isOutcomeUnknown(error)).toBe(true);
  });
});

describe("amountProblem", () => {
  it.each(["1", "20", "0.00000001", "123456789012", "123456789012.12345678", " 5.5 "])(
    "accepts %j",
    (value) => {
      expect(amountProblem(value)).toBeNull();
    },
  );

  it.each(["", "   ", undefined])("asks for an amount when it is %j", (value) => {
    expect(amountProblem(value)).toBe("required");
  });

  it.each(["0", "0.0", "0.00000000", "000"])("refuses zero written as %j", (value) => {
    expect(amountProblem(value)).toBe("zero");
  });

  it.each([
    "1234567890123",
    "1.123456789",
    "1e2",
    "1E2",
    "+1",
    "-1",
    "1.",
    ".5",
    "1,000",
    "１",
    "Infinity",
    "NaN",
    "0x10",
  ])("refuses %j", (value) => {
    expect(amountProblem(value)).toBe("format");
  });
});

describe("signedAmount", () => {
  it("keeps credit types positive and the digits untouched", () => {
    expect(signedAmount("ADJUSTMENT_CREDIT", "20.10000001")).toBe("20.10000001");
    expect(signedAmount("BONUS", "0.1")).toBe("0.1");
  });

  it("puts a minus in front for debit types", () => {
    expect(signedAmount("ADJUSTMENT_DEBIT", "20.10000001")).toBe("-20.10000001");
    expect(signedAmount("REFUND_ADJUSTMENT", "123456789012.12345678")).toBe(
      "-123456789012.12345678",
    );
  });

  it("drops surrounding whitespace before adding the sign", () => {
    expect(signedAmount("ADJUSTMENT_DEBIT", " 5 ")).toBe("-5");
  });
});
