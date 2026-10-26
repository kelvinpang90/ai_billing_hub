/**
 * 带令牌下载一个文件（AIH-TASK-117；设计闸门 #435 v10 §2「前端」）。
 *
 * 语义以 docs/api.md「客户导出」为准：成功是一个附件（`Content-Disposition: attachment; filename="…"`），失败**不是**
 * 文件，仍是 JSON 信封（`{success:false, error, request_id}`），HTTP 状态码照常。
 *
 * ⚠️ 下载要带访问令牌，所以不能用普通链接：经现有 `client` 取（拦截器负责令牌与 401 后的刷新重试），
 * `responseType: "blob"`。这样失败的响应体也是 blob，要先读成文本再按信封解析 —— 不能交给 `toApiError`，它看到的
 * 是一个 Blob，会当成「不认识的形状」。
 *
 * ⚠️ 这里**不打日志**：文件里是客户的计费明细，错误信封里也可能带客户数据（理由同 `apiGet`）。
 */

import { AxiosError } from "axios";

import { ApiError, MALFORMED_RESPONSE, client, toApiError, unwrapEnvelope } from "./client";

/** 超过行数上限（20 000 行）：请客户缩短日期区间或加筛选条件（docs/api.md「客户导出」）。 */
export const EXPORT_TOO_LARGE = "EXPORT_TOO_LARGE";

/** 同一时刻已有一个导出在生成或下载：几秒后重试（docs/api.md「客户导出」）。 */
export const EXPORT_BUSY = "EXPORT_BUSY";

/**
 * 下载的超时。`client` 默认 15 秒是给 JSON 接口的；导出文件可达几 MB，后端要等整份文件拼好才开始发送，
 * 慢网络下 15 秒不够。
 */
export const DOWNLOAD_TIMEOUT_MS = 120_000;

/**
 * 触发下载之后多久释放对象 URL。点击只是让浏览器开始下载，同步释放在部分浏览器里会让下载拿不到数据；
 * 等一会儿再放，文件已经交给浏览器的下载管理器。
 */
export const REVOKE_DELAY_MS = 1_000;

/**
 * 从 `Content-Disposition` 取文件名：先认 RFC 5987 的 `filename*=UTF-8''…`，再认 `filename="…"` 或不带引号的
 * `filename=…`。取不到、或取到的是空串时 `null`。只取最后一段（去掉路径分隔符之前的部分），不让响应头决定目录。
 */
export function filenameFromContentDisposition(header: unknown): string | null {
  if (typeof header !== "string") {
    return null;
  }
  let name: string | null = null;
  const extended = /filename\*\s*=\s*([^;]+)/i.exec(header);
  if (extended?.[1] !== undefined) {
    const value = extended[1].trim().replace(/^"(.*)"$/, "$1");
    const quote = value.indexOf("''");
    if (quote >= 0) {
      try {
        name = decodeURIComponent(value.slice(quote + 2));
      } catch {
        name = null;
      }
    }
  }
  if (name === null) {
    const plain = /filename\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^;\s]+))/i.exec(header);
    name = plain?.[1]?.replace(/\\(.)/g, "$1") ?? plain?.[2] ?? null;
  }
  const base = name?.split(/[/\\]/).pop()?.trim() ?? "";
  return base === "" ? null : base;
}

/** 把失败响应里的 blob 读成文本、按信封解析；不是信封（坏 JSON、HTML 错误页）时 `MALFORMED_RESPONSE`。 */
async function errorFromBlob(blob: Blob): Promise<ApiError> {
  let body: unknown;
  try {
    body = JSON.parse(await blob.text());
  } catch {
    return new ApiError(MALFORMED_RESPONSE, "The server returned an unexpected response.", null);
  }
  try {
    unwrapEnvelope(body);
  } catch (error) {
    if (error instanceof ApiError) {
      return error;
    }
  }
  // 失败的状态码配一个 `success: true` 的信封：同样是认不出的形状。
  return new ApiError(MALFORMED_RESPONSE, "The server returned an unexpected response.", null);
}

/** 失败统一成与 `toApiError` 同形的 {@link ApiError}；响应体是 blob 时先按信封解析。 */
async function downloadError(error: unknown): Promise<ApiError> {
  if (error instanceof AxiosError && error.response !== undefined && error.response.data instanceof Blob) {
    return errorFromBlob(error.response.data);
  }
  return toApiError(error);
}

/** 用对象 URL 让浏览器把 blob 存成 `filename`，之后释放这个 URL。 */
function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.style.display = "none";
  document.body.appendChild(link);
  try {
    link.click();
  } finally {
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), REVOKE_DELAY_MS);
  }
}

/**
 * GET `url` 并把响应存成文件。文件名取自 `Content-Disposition`，取不到时用 `fallbackFilename`。
 * 失败抛 {@link ApiError}（错误码与 `request_id` 来自后端的信封；连不上时 `NETWORK_ERROR`）。
 */
export async function downloadFile(url: string, fallbackFilename: string, signal?: AbortSignal): Promise<void> {
  let response;
  try {
    response = await client.get<Blob>(url, {
      responseType: "blob",
      timeout: DOWNLOAD_TIMEOUT_MS,
      ...(signal === undefined ? {} : { signal }),
    });
  } catch (error) {
    throw await downloadError(error);
  }
  if (!(response.data instanceof Blob)) {
    throw new ApiError(MALFORMED_RESPONSE, "The server returned an unexpected response.", null);
  }
  const headers = response.headers as Record<string, unknown>;
  saveBlob(response.data, filenameFromContentDisposition(headers["content-disposition"]) ?? fallbackFilename);
}
