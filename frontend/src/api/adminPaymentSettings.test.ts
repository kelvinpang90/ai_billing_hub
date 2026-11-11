/**
 * 管理端支付设置的两个接口发到哪个地址、用什么方法、带什么请求体。
 *
 * ⚠️ `PUT` 是整体替换且 `extra="forbid"`：请求体多一个或少一个键后端都是 422，所以这里用 toEqual 钉住恰好 7 个键。
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, client } from "./client";
import {
  getPaymentSettings,
  updatePaymentSettings,
  type PaymentSettings,
  type PaymentSettingsUpdate,
} from "./adminPaymentSettings";

const SETTINGS_URL = "/api/v1/admin/payment-settings";

function envelope(data: unknown) {
  return { data: { success: true, data, error: null, request_id: "req-1" } };
}

const SETTINGS: PaymentSettings = {
  min_amount: "10.00",
  max_amount: null,
  recovery_buffer: "10.00",
  preset_amounts: ["50.00", "100.00", "300.00", "500.00", "1000.00"],
  reuse_window_seconds: 60,
  pending_timeout_seconds: 3600,
  version: 1,
  updated_at: "2026-11-08T00:00:00",
  updated_by: null,
};

const UPDATE: PaymentSettingsUpdate = {
  min_amount: "20",
  max_amount: "5000.5",
  recovery_buffer: "1.00",
  preset_amounts: ["50", "100"],
  reuse_window_seconds: 0,
  pending_timeout_seconds: 2400,
  version: 3,
};

afterEach(() => {
  vi.restoreAllMocks();
});

describe("admin payment settings read", () => {
  it("GETs the settings path without a query string and returns the settings", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(SETTINGS));

    const settings = await getPaymentSettings();

    expect(get).toHaveBeenCalledTimes(1);
    expect(get).toHaveBeenCalledWith(SETTINGS_URL, {});
    expect(settings).toEqual(SETTINGS);
  });

  it("turns a failure envelope into an ApiError with the request id", async () => {
    vi.spyOn(client, "get").mockResolvedValue({
      data: {
        success: false,
        data: null,
        error: { code: "PAYMENTS_MISCONFIGURED", message: "Payments are not configured." },
        request_id: "req-503",
      },
    });

    const failure = await getPaymentSettings().catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(ApiError);
    expect(failure).toMatchObject({ code: "PAYMENTS_MISCONFIGURED", requestId: "req-503" });
  });
});

describe("admin payment settings update", () => {
  it("PUTs the six fields and the version to the settings path and returns the new settings", async () => {
    const put = vi.spyOn(client, "put").mockResolvedValue(envelope({ ...SETTINGS, version: 4 }));
    const post = vi.spyOn(client, "post");

    const result = await updatePaymentSettings(UPDATE);

    expect(put).toHaveBeenCalledTimes(1);
    expect(put).toHaveBeenCalledWith(SETTINGS_URL, {
      min_amount: "20",
      max_amount: "5000.5",
      recovery_buffer: "1.00",
      preset_amounts: ["50", "100"],
      reuse_window_seconds: 0,
      pending_timeout_seconds: 2400,
      version: 3,
    });
    expect(post).not.toHaveBeenCalled();
    expect(result).toEqual({ ...SETTINGS, version: 4 });
  });

  it("sends a null maximum as an explicit null", async () => {
    const put = vi.spyOn(client, "put").mockResolvedValue(envelope(SETTINGS));

    await updatePaymentSettings({ ...UPDATE, max_amount: null });

    const body = put.mock.calls[0]?.[1] as Record<string, unknown>;
    expect(Object.hasOwn(body, "max_amount")).toBe(true);
    expect(body.max_amount).toBeNull();
  });

  it("sends only the seven fields, even when the caller passes more", async () => {
    const put = vi.spyOn(client, "put").mockResolvedValue(envelope(SETTINGS));
    const body = { ...UPDATE, updated_at: "2026-11-08T00:00:00", updated_by: null } as PaymentSettingsUpdate;

    await updatePaymentSettings(body);

    // toEqual 不认多出来的键：请求体里有 updated_at 就不相等。
    expect(put.mock.calls[0]?.[1]).toEqual(UPDATE);
  });

  it("turns a version conflict into an ApiError with the request id", async () => {
    vi.spyOn(client, "put").mockResolvedValue({
      data: {
        success: false,
        data: null,
        error: { code: "PAYMENT_SETTINGS_VERSION_CONFLICT", message: "Payment settings were changed." },
        request_id: "req-409",
      },
    });

    const failure = await updatePaymentSettings(UPDATE).catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(ApiError);
    expect(failure).toMatchObject({ code: "PAYMENT_SETTINGS_VERSION_CONFLICT", requestId: "req-409" });
  });
});
