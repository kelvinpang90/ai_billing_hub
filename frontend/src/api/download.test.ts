/**
 * 带令牌的文件下载（AIH-TASK-117）：成功时对象 URL 的创建与释放、文件名的两种来源；失败时 blob 里的 JSON 信封
 * 解析出错误码与 request_id，坏 JSON 为 `MALFORMED_RESPONSE`。
 *
 * jsdom 没有 `URL.createObjectURL` / `revokeObjectURL`，这里按属性装上替身、用完还原；`<a>` 的 `click` 也拦下来
 * 只记录（jsdom 会把点击当成导航并报「not implemented」）。
 *
 * uuid 一律是全零占位值。
 */

import { AxiosError, AxiosHeaders, type AxiosResponse } from "axios";
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from "vitest";

import { ApiError, MALFORMED_RESPONSE, NETWORK_ERROR, client } from "./client";
import {
  DOWNLOAD_TIMEOUT_MS,
  EXPORT_BUSY,
  EXPORT_TOO_LARGE,
  REVOKE_DELAY_MS,
  downloadFile,
  filenameFromContentDisposition,
} from "./download";

const URL_PATH = "/api/v1/customer/usage-events/export?from_date=2026-10-01&to_date=2026-10-25";
const FALLBACK = "usage-events_2026-10-01_2026-10-25.csv";
const OBJECT_URL = "blob:https://portal.example/00000000-0000-0000-0000-000000000000";
const REQUEST_ID = "00000000-0000-0000-0000-000000000000";

const CSV = "﻿id,event_id\r\n";

const originalCreate = Object.getOwnPropertyDescriptor(URL, "createObjectURL");
const originalRevoke = Object.getOwnPropertyDescriptor(URL, "revokeObjectURL");

function restore(name: "createObjectURL" | "revokeObjectURL", descriptor: PropertyDescriptor | undefined): void {
  if (descriptor === undefined) {
    Reflect.deleteProperty(URL, name);
  } else {
    Object.defineProperty(URL, name, descriptor);
  }
}

let create: Mock<(blob: Blob) => string>;
let revoke: Mock<(url: string) => void>;
/** 被点击的链接：点击那一刻的 href、download，以及它当时在不在文档里。 */
let clicks: { href: string; download: string; attached: boolean }[];

beforeEach(() => {
  create = vi.fn<(blob: Blob) => string>(() => OBJECT_URL);
  revoke = vi.fn<(url: string) => void>();
  Object.defineProperty(URL, "createObjectURL", { configurable: true, writable: true, value: create });
  Object.defineProperty(URL, "revokeObjectURL", { configurable: true, writable: true, value: revoke });
  clicks = [];
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
    clicks.push({ href: this.href, download: this.download, attached: this.isConnected });
  });
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  restore("createObjectURL", originalCreate);
  restore("revokeObjectURL", originalRevoke);
});

function fileResponse(headers: Record<string, string>) {
  return { data: new Blob([CSV], { type: "text/csv" }), status: 200, statusText: "", headers };
}

function blobRejection(status: number, body: string) {
  const response = {
    data: new Blob([body], { type: "application/json" }),
    status,
    statusText: "",
    headers: {},
    config: { headers: new AxiosHeaders() },
  } satisfies AxiosResponse;
  return new AxiosError("Request failed", "ERR_BAD_REQUEST", undefined, undefined, response);
}

function failureBody(code: string, message: string, requestId: string | null): string {
  return JSON.stringify({ success: false, data: null, error: { code, message }, request_id: requestId });
}

describe("downloadFile success", () => {
  it("asks for a blob through the shared client with a longer timeout", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(fileResponse({}));

    await downloadFile(URL_PATH, FALLBACK);

    expect(get).toHaveBeenCalledWith(URL_PATH, { responseType: "blob", timeout: DOWNLOAD_TIMEOUT_MS });
  });

  it("passes the abort signal through", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(fileResponse({}));
    const controller = new AbortController();

    await downloadFile(URL_PATH, FALLBACK, controller.signal);

    expect(get).toHaveBeenCalledWith(URL_PATH, {
      responseType: "blob",
      timeout: DOWNLOAD_TIMEOUT_MS,
      signal: controller.signal,
    });
  });

  it("saves the blob through an object URL and releases the URL afterwards", async () => {
    vi.useFakeTimers();
    const response = fileResponse({
      "content-disposition": 'attachment; filename="usage-events_2026-10-01_2026-10-31.csv"',
    });
    vi.spyOn(client, "get").mockResolvedValue(response);

    await downloadFile(URL_PATH, FALLBACK);

    expect(create).toHaveBeenCalledTimes(1);
    expect(create).toHaveBeenCalledWith(response.data);
    expect(clicks).toEqual([{ href: OBJECT_URL, download: "usage-events_2026-10-01_2026-10-31.csv", attached: true }]);
    // 链接点完就从文档里拿掉。
    expect(document.querySelector(`a[href="${OBJECT_URL}"]`)).toBeNull();
    // 点击之后才释放，而且要等一会儿。
    expect(revoke).not.toHaveBeenCalled();

    vi.advanceTimersByTime(REVOKE_DELAY_MS);

    expect(revoke).toHaveBeenCalledTimes(1);
    expect(revoke).toHaveBeenCalledWith(OBJECT_URL);
  });

  it("takes the file name from Content-Disposition", async () => {
    vi.spyOn(client, "get").mockResolvedValue(
      fileResponse({ "content-disposition": 'attachment; filename="usage-events_2026-09-01_2026-09-30.csv"' }),
    );

    await downloadFile(URL_PATH, FALLBACK);

    expect(clicks.map(({ download }) => download)).toEqual(["usage-events_2026-09-01_2026-09-30.csv"]);
  });

  it("falls back to the caller's file name when there is no Content-Disposition", async () => {
    vi.spyOn(client, "get").mockResolvedValue(fileResponse({}));

    await downloadFile(URL_PATH, FALLBACK);

    expect(clicks.map(({ download }) => download)).toEqual([FALLBACK]);
  });

  it("falls back to the caller's file name when Content-Disposition has none", async () => {
    vi.spyOn(client, "get").mockResolvedValue(fileResponse({ "content-disposition": "attachment" }));

    await downloadFile(URL_PATH, FALLBACK);

    expect(clicks.map(({ download }) => download)).toEqual([FALLBACK]);
  });

  it.each<[unknown, string | null]>([
    ['attachment; filename="usage-events_2026-10-01_2026-10-31.csv"', "usage-events_2026-10-01_2026-10-31.csv"],
    ["attachment; filename=usage-events_2026-10-01_2026-10-31.csv", "usage-events_2026-10-01_2026-10-31.csv"],
    ["attachment; filename*=UTF-8''usage%20events.csv", "usage events.csv"],
    ["attachment; filename=\"plain.csv\"; filename*=UTF-8''extended.csv", "extended.csv"],
    ['attachment; filename="../../etc/usage.csv"', "usage.csv"],
    ['attachment; filename=""', null],
    ["attachment", null],
    [undefined, null],
  ])("reads %s as %s", (header, name) => {
    expect(filenameFromContentDisposition(header)).toBe(name);
  });
});

describe("downloadFile failure", () => {
  it.each<[number, string]>([
    [422, EXPORT_TOO_LARGE],
    [422, "QUERY_TOO_BROAD"],
    [503, EXPORT_BUSY],
    [500, "INTERNAL_ERROR"],
  ])("reads a %s %s envelope out of the blob", async (status, code) => {
    vi.spyOn(client, "get").mockRejectedValue(blobRejection(status, failureBody(code, "Safe message.", REQUEST_ID)));

    const failure = downloadFile(URL_PATH, FALLBACK);

    await expect(failure).rejects.toBeInstanceOf(ApiError);
    await expect(failure).rejects.toMatchObject({ code, message: "Safe message.", requestId: REQUEST_ID });
    expect(create).not.toHaveBeenCalled();
    expect(clicks).toEqual([]);
  });

  it("reports a body that is not JSON as MALFORMED_RESPONSE", async () => {
    vi.spyOn(client, "get").mockRejectedValue(blobRejection(502, "<html>Bad gateway</html>"));

    const failure = downloadFile(URL_PATH, FALLBACK);

    await expect(failure).rejects.toBeInstanceOf(ApiError);
    await expect(failure).rejects.toMatchObject({ code: MALFORMED_RESPONSE, requestId: null });
    expect(create).not.toHaveBeenCalled();
  });

  it("reports JSON that is not an envelope as MALFORMED_RESPONSE", async () => {
    vi.spyOn(client, "get").mockRejectedValue(blobRejection(500, JSON.stringify({ detail: "nope" })));

    await expect(downloadFile(URL_PATH, FALLBACK)).rejects.toMatchObject({ code: MALFORMED_RESPONSE, requestId: null });
  });

  it("reports a request that never got a response as NETWORK_ERROR", async () => {
    vi.spyOn(client, "get").mockRejectedValue(new AxiosError("Network Error", "ERR_NETWORK"));

    await expect(downloadFile(URL_PATH, FALLBACK)).rejects.toMatchObject({ code: NETWORK_ERROR, requestId: null });
    expect(create).not.toHaveBeenCalled();
  });

  it("logs nothing", async () => {
    const spies = (["log", "info", "warn", "error", "debug"] as const).map((method) =>
      vi.spyOn(console, method).mockImplementation(() => undefined),
    );
    vi.spyOn(client, "get").mockRejectedValue(blobRejection(500, failureBody("INTERNAL_ERROR", "Safe message.", REQUEST_ID)));

    await expect(downloadFile(URL_PATH, FALLBACK)).rejects.toBeInstanceOf(ApiError);

    for (const spy of spies) {
      expect(spy).not.toHaveBeenCalled();
    }
  });
});
