/**
 * 钱包核对的两个接口发到哪个地址、带什么查询参数。
 *
 * ⚠️ 参数名写错（`pageSize`）后端不报错（未知参数忽略），结果是永远停在第 1 页、每页 20 条。
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { listReconciliationFindings, listReconciliationRuns } from "./adminWalletReconciliation";
import { ApiError, client } from "./client";

const RUN_ID = "00000000-0000-0000-0000-000000000000";

function envelope(data: unknown) {
  return { data: { success: true, data, error: null, request_id: "req-1" } };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("admin wallet reconciliation requests", () => {
  it("lists runs with page and page_size", async () => {
    const get = vi
      .spyOn(client, "get")
      .mockResolvedValue(envelope({ items: [], page: 2, page_size: 50, total: 0 }));

    const page = await listReconciliationRuns(2, 50);

    expect(get).toHaveBeenCalledWith("/api/v1/admin/wallet-reconciliation/runs?page=2&page_size=50", {});
    expect(page).toEqual({ items: [], page: 2, page_size: 50, total: 0 });
  });

  it("lists the findings of a run under that run's id, with page and page_size", async () => {
    const get = vi
      .spyOn(client, "get")
      .mockResolvedValue(envelope({ items: [], page: 1, page_size: 20, total: 0 }));

    await listReconciliationFindings(RUN_ID, 1, 20);

    expect(get).toHaveBeenCalledWith(
      `/api/v1/admin/wallet-reconciliation/runs/${RUN_ID}/findings?page=1&page_size=20`,
      {},
    );
  });

  it("puts the run id into the path exactly as given, without checking its shape", async () => {
    const get = vi
      .spyOn(client, "get")
      .mockResolvedValue(envelope({ items: [], page: 3, page_size: 10, total: 0 }));

    await listReconciliationFindings("Not-A-Run", 3, 10);

    // 形状由后端判（404 RECONCILIATION_RUN_NOT_FOUND），这一层不拦、不改大小写。
    expect(get).toHaveBeenCalledWith(
      "/api/v1/admin/wallet-reconciliation/runs/Not-A-Run/findings?page=3&page_size=10",
      {},
    );
  });

  it("turns a failure envelope into an ApiError with the request id", async () => {
    vi.spyOn(client, "get").mockResolvedValue({
      data: {
        success: false,
        data: null,
        error: { code: "RECONCILIATION_RUN_NOT_FOUND", message: "Reconciliation run not found." },
        request_id: "req-404",
      },
    });

    const failure = await listReconciliationFindings(RUN_ID, 1, 20).catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(ApiError);
    expect(failure).toMatchObject({ code: "RECONCILIATION_RUN_NOT_FOUND", requestId: "req-404" });
  });
});
