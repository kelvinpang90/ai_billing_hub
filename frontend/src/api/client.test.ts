/**
 * §107 信封的解包与错误映射。
 *
 * 这一层值得单独测，是因为**它的失败方式是安静的**：把 `success: false` 的响应
 * 当成成功，界面拿到的是 `null`，渲染出来是一片空白而不是一条错误。
 */

import { AxiosError, AxiosHeaders } from "axios";
import { describe, expect, it } from "vitest";

import { MALFORMED_RESPONSE, NETWORK_ERROR, ApiError, toApiError, unwrapEnvelope } from "./client";

function axiosErrorWith(body: unknown): AxiosError {
  const error = new AxiosError("Request failed");
  error.response = {
    data: body,
    status: 503,
    statusText: "Service Unavailable",
    headers: new AxiosHeaders(),
    config: { headers: new AxiosHeaders() },
  };
  return error;
}

describe("unwrapEnvelope", () => {
  it("returns the payload of a successful envelope", () => {
    expect(
      unwrapEnvelope<{ status: string }>({
        success: true,
        data: { status: "ok" },
        error: null,
        request_id: "abc",
      }),
    ).toEqual({ status: "ok" });
  });

  it("throws with the backend code and request id when success is false", () => {
    try {
      unwrapEnvelope({
        success: false,
        data: null,
        error: { code: "DATABASE_NOT_CONFIGURED", message: "Not configured." },
        request_id: "req-1",
      });
      expect.unreachable("a failed envelope must throw");
    } catch (error) {
      expect(error).toBeInstanceOf(ApiError);
      expect((error as ApiError).code).toBe("DATABASE_NOT_CONFIGURED");
      expect((error as ApiError).requestId).toBe("req-1");
    }
  });

  it("treats an unrecognised shape as an error rather than empty data", () => {
    // 代理层出问题时回的是一段 HTML。把它当成 `undefined` 会让界面显示空白
    // 而不报错 —— 那是最难排查的一种故障。
    expect(() => unwrapEnvelope("<html>502 Bad Gateway</html>")).toThrow(ApiError);
    expect(() => unwrapEnvelope(null)).toThrow(ApiError);
    try {
      unwrapEnvelope({ status: "ok" });
      expect.unreachable("a non-envelope body must throw");
    } catch (error) {
      expect((error as ApiError).code).toBe(MALFORMED_RESPONSE);
    }
  });
});

describe("toApiError", () => {
  it("prefers the backend envelope carried by a failed response", () => {
    const error = toApiError(
      axiosErrorWith({
        success: false,
        data: null,
        error: { code: "DATABASE_UNAVAILABLE", message: "Unavailable." },
        request_id: "req-2",
      }),
    );
    expect(error.code).toBe("DATABASE_UNAVAILABLE");
    expect(error.requestId).toBe("req-2");
  });

  it("falls back to a network error when there is no response at all", () => {
    expect(toApiError(new AxiosError("connect ECONNREFUSED")).code).toBe(NETWORK_ERROR);
    expect(toApiError(new Error("boom")).code).toBe(NETWORK_ERROR);
  });

  it("never leaks a non-envelope error body to the caller as data", () => {
    const error = toApiError(axiosErrorWith("<html>504</html>"));
    expect(error).toBeInstanceOf(ApiError);
    expect(error.code).toBe(MALFORMED_RESPONSE);
  });
});
