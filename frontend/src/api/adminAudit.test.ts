/**
 * 审计查询发到哪个地址、带什么查询参数。
 *
 * ⚠️ 页面用例把整个 `api/adminAudit` 模块 mock 掉了，参数名写错（`pageSize`、
 * `createdFrom`）时它们照样全绿；在浏览器里那是一句 422，或者更糟 —— 未知参数
 * 后端不报错，筛选条件被静默忽略。
 */

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { AxiosError, AxiosHeaders, type AxiosResponse } from "axios";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AUDIT_ACTIONS, listAuditLogs } from "./adminAudit";
import { ApiError, client } from "./client";

const ENTITY_ID = "00000000-0000-0000-0000-000000000000";

const AUTH_MODELS = join(
  dirname(fileURLToPath(import.meta.url)),
  "..",
  "..",
  "..",
  "app",
  "models",
  "auth.py",
);

/** 类体里的 `NAME = "VALUE"` 行，取 VALUE。注释行与文档字符串都不匹配。 */
const ENUM_MEMBER = /^\s+[A-Z][A-Z0-9_]*\s*=\s*"([^"]+)"\s*$/;

/** `AuditAction` 的全部取值，按声明顺序。类体到下一行顶格的非空行为止。 */
function backendAuditActions(): string[] {
  const lines = readFileSync(AUTH_MODELS, "utf8").split(/\r?\n/);
  const start = lines.findIndex((line) => line.startsWith("class AuditAction("));
  if (start === -1) {
    return [];
  }
  const values: string[] = [];
  for (const line of lines.slice(start + 1)) {
    if (line.trim() !== "" && !/^\s/.test(line)) {
      break;
    }
    const value = ENUM_MEMBER.exec(line)?.[1];
    if (value !== undefined) {
      values.push(value);
    }
  }
  return values;
}

function envelope(data: unknown) {
  return { data: { success: true, data, error: null, request_id: "req-1" } };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("admin audit log requests", () => {
  it("sends only page and page_size when no filter is given", async () => {
    const get = vi
      .spyOn(client, "get")
      .mockResolvedValue(envelope({ items: [], page: 1, page_size: 20, total: 0 }));

    const page = await listAuditLogs({}, 1, 20);

    expect(get).toHaveBeenCalledWith("/api/v1/admin/audit-logs?page=1&page_size=20", {});
    expect(page).toEqual({ items: [], page: 1, page_size: 20, total: 0 });
  });

  it("sends every filter under the name the backend reads", async () => {
    const get = vi
      .spyOn(client, "get")
      .mockResolvedValue(envelope({ items: [], page: 2, page_size: 50, total: 0 }));

    await listAuditLogs(
      {
        action: "CUSTOMER_CREATE",
        entity_type: "tenant",
        entity_id: ENTITY_ID,
        actor_email: "admin@example.com",
        created_from: "2026-09-20T16:00:00",
        created_to: "2026-09-21T16:00:00",
      },
      2,
      50,
    );

    expect(get).toHaveBeenCalledWith(
      "/api/v1/admin/audit-logs?page=2&page_size=50" +
        "&action=CUSTOMER_CREATE&entity_type=tenant" +
        `&entity_id=${ENTITY_ID}` +
        "&actor_email=admin%40example.com" +
        "&created_from=2026-09-20T16%3A00%3A00&created_to=2026-09-21T16%3A00%3A00",
      {},
    );
  });

  it("leaves omitted and blank filters out of the query string entirely", async () => {
    const get = vi
      .spyOn(client, "get")
      .mockResolvedValue(envelope({ items: [], page: 1, page_size: 20, total: 0 }));

    await listAuditLogs({ action: "LOGIN", entity_type: "", actor_email: "   " }, 1, 20);

    // `entity_type=` 在后端是「类型等于空串」，不是「不筛」。
    expect(get).toHaveBeenCalledWith("/api/v1/admin/audit-logs?page=1&page_size=20&action=LOGIN", {});
  });

  it("trims the surrounding whitespace of a filter value", async () => {
    const get = vi
      .spyOn(client, "get")
      .mockResolvedValue(envelope({ items: [], page: 1, page_size: 20, total: 0 }));

    await listAuditLogs({ entity_id: `  ${ENTITY_ID} ` }, 1, 20);

    expect(get).toHaveBeenCalledWith(
      `/api/v1/admin/audit-logs?page=1&page_size=20&entity_id=${ENTITY_ID}`,
      {},
    );
  });

  it("turns a 422 envelope into an ApiError carrying the backend message", async () => {
    const response = {
      data: {
        success: false,
        data: null,
        error: { code: "VALIDATION_ERROR", message: "Invalid request fields: query.created_from" },
        request_id: "req-422",
      },
      status: 422,
      statusText: "Unprocessable Entity",
      headers: {},
      config: { headers: new AxiosHeaders() },
    } satisfies AxiosResponse;
    vi.spyOn(client, "get").mockRejectedValue(
      new AxiosError("Request failed", "ERR_BAD_REQUEST", undefined, undefined, response),
    );

    const failure = listAuditLogs({ created_from: "2026-09-20T16:00:00" }, 1, 20);

    await expect(failure).rejects.toBeInstanceOf(ApiError);
    await expect(failure).rejects.toMatchObject({
      code: "VALIDATION_ERROR",
      message: "Invalid request fields: query.created_from",
      requestId: "req-422",
    });
  });
});

describe("audit action options", () => {
  it("finds the AuditAction members at all", () => {
    // ⚠️ 没有这一条，解析一旦失效（找不到类、正则永不匹配）而 AUDIT_ACTIONS 又恰好
    // 被清空，下面的比对会在两张空列表上空转着通过。48 是写这条用例时的枚举个数。
    expect(backendAuditActions().length).toBeGreaterThanOrEqual(48);
  });

  it("lists exactly the backend AuditAction values in declaration order", () => {
    // 前端多一个值，选它就是 422；少一个，下拉里就选不到。
    expect([...AUDIT_ACTIONS]).toEqual(backendAuditActions());
  });
});
