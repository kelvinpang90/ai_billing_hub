/**
 * AI 目录请求发到哪个地址、带什么查询参数与请求体。
 *
 * ⚠️ 页面用例把整个 `api/adminCatalog` 模块的请求函数 mock 掉了（它们测的是界面），所以路径与
 * 字段名写错时它们照样全绿 —— 与 `adminCustomers.test.ts` 记的是同一个盲区。
 *
 * uuid 一律是全零占位值。
 */

import { AxiosError, AxiosHeaders, type AxiosResponse } from "axios";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  MODEL_CODE_PATTERN,
  PROVIDER_CODE_PATTERN,
  METER_CODE_PATTERN,
  UNIT_PATTERN,
  createMeterType,
  createModel,
  createProvider,
  getMeterType,
  getModel,
  getProvider,
  listMeterTypes,
  listModelAliases,
  listModels,
  listProviders,
  mapModelAlias,
  retireModelAlias,
  updateMeterType,
  updateModel,
  updateProvider,
} from "./adminCatalog";
import { ApiError, client } from "./client";

const ID = "00000000-0000-0000-0000-000000000000";
const PROVIDERS = "/api/v1/admin/ai-providers";
const METER_TYPES = "/api/v1/admin/usage-meter-types";

function envelope(data: unknown) {
  return { data: { success: true, data, error: null, request_id: "req-1" } };
}

const EMPTY_PAGE = { items: [], page: 1, page_size: 20, total: 0 };

function rejection(status: number, code: string, message: string, requestId: string) {
  const response = {
    data: { success: false, data: null, error: { code, message }, request_id: requestId },
    status,
    statusText: "",
    headers: {},
    config: { headers: new AxiosHeaders() },
  } satisfies AxiosResponse;
  return new AxiosError("Request failed", "ERR_BAD_REQUEST", undefined, undefined, response);
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("meter type requests", () => {
  it("lists with page and page_size only when no status is chosen", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));

    const page = await listMeterTypes({}, 2, 100);

    expect(get).toHaveBeenCalledWith(`${METER_TYPES}?page=2&page_size=100`, {});
    expect(page).toEqual(EMPTY_PAGE);
  });

  it("adds status to the query when one is chosen", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));

    await listMeterTypes({ status: "RETIRED" }, 1, 20);

    expect(get).toHaveBeenCalledWith(`${METER_TYPES}?page=1&page_size=20&status=RETIRED`, {});
  });

  it("creates a QUANTITY type with exactly the five fields, codes untouched", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope({ id: ID }));

    await createMeterType({
      code: "VIDEO_SECOND",
      display_name: "Video seconds",
      unit: "SECOND",
      quantity_kind: "DECIMAL",
      component_code: "VIDEO_SECOND",
    });

    // 没有 payload_shape、quantity_field：带了是 422。
    expect(post).toHaveBeenCalledWith(METER_TYPES, {
      code: "VIDEO_SECOND",
      display_name: "Video seconds",
      unit: "SECOND",
      quantity_kind: "DECIMAL",
      component_code: "VIDEO_SECOND",
    });
  });

  it("reads one type and patches it at its own path", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope({ id: ID }));
    const patch = vi.spyOn(client, "patch").mockResolvedValue(envelope({ id: ID }));

    await getMeterType(ID);
    await updateMeterType(ID, { status: "RETIRED" });

    expect(get).toHaveBeenCalledWith(`${METER_TYPES}/${ID}`, {});
    expect(patch).toHaveBeenCalledWith(`${METER_TYPES}/${ID}`, { status: "RETIRED" });
  });
});

describe("provider requests", () => {
  it("lists providers, with the status filter only when given", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));

    await listProviders({}, 1, 20);
    await listProviders({ status: "ACTIVE" }, 3, 50);

    expect(get).toHaveBeenNthCalledWith(1, `${PROVIDERS}?page=1&page_size=20`, {});
    expect(get).toHaveBeenNthCalledWith(2, `${PROVIDERS}?page=3&page_size=50&status=ACTIVE`, {});
  });

  it("creates a provider with the body as given", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope({ id: ID }));

    await createProvider({ code: "anthropic", display_name: "Anthropic" });

    expect(post).toHaveBeenCalledWith(PROVIDERS, { code: "anthropic", display_name: "Anthropic" });
  });

  it("reads and renames a provider by its public id", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope({ id: ID }));
    const patch = vi.spyOn(client, "patch").mockResolvedValue(envelope({ id: ID }));

    await getProvider(ID);
    await updateProvider(ID, { display_name: "Anthropic PBC" });

    expect(get).toHaveBeenCalledWith(`${PROVIDERS}/${ID}`, {});
    expect(patch).toHaveBeenCalledWith(`${PROVIDERS}/${ID}`, { display_name: "Anthropic PBC" });
  });

  it("escapes an id from the address bar instead of letting it change the path", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));

    await getProvider("a/../b");
    await listModels("a/../b", {}, 1, 20);

    expect(get).toHaveBeenNthCalledWith(1, `${PROVIDERS}/a%2F..%2Fb`, {});
    expect(get).toHaveBeenNthCalledWith(2, `${PROVIDERS}/a%2F..%2Fb/models?page=1&page_size=20`, {});
  });
});

describe("model requests", () => {
  it("lists one provider's models, with the status filter only when given", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));

    await listModels(ID, {}, 1, 20);
    await listModels(ID, { status: "RETIRED" }, 2, 20);

    expect(get).toHaveBeenNthCalledWith(1, `${PROVIDERS}/${ID}/models?page=1&page_size=20`, {});
    expect(get).toHaveBeenNthCalledWith(
      2,
      `${PROVIDERS}/${ID}/models?page=2&page_size=20&status=RETIRED`,
      {},
    );
  });

  it("creates a model under the provider in the path, code exactly as typed", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope({ id: ID }));

    await createModel(ID, { code: "GPT-4o", display_name: "GPT-4o" });

    // 归属只来自路径：请求体里没有 provider_id。大小写原样。
    expect(post).toHaveBeenCalledWith(`${PROVIDERS}/${ID}/models`, {
      code: "GPT-4o",
      display_name: "GPT-4o",
    });
  });

  it("reads and retires a model at its own path", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope({ id: ID, aliases: [] }));
    const patch = vi.spyOn(client, "patch").mockResolvedValue(envelope({ id: ID }));

    await getModel(ID, ID);
    await updateModel(ID, ID, { status: "RETIRED" });

    expect(get).toHaveBeenCalledWith(`${PROVIDERS}/${ID}/models/${ID}`, {});
    expect(patch).toHaveBeenCalledWith(`${PROVIDERS}/${ID}/models/${ID}`, { status: "RETIRED" });
  });
});

describe("alias requests", () => {
  it("lists every segment with only page and page_size by default", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));

    await listModelAliases(ID, {}, 1, 100);
    // `current: false` 与不带一样，不发；空的 alias 也不发。
    await listModelAliases(ID, { alias: "", current: false }, 1, 100);

    expect(get).toHaveBeenNthCalledWith(1, `${PROVIDERS}/${ID}/model-aliases?page=1&page_size=100`, {});
    expect(get).toHaveBeenNthCalledWith(2, `${PROVIDERS}/${ID}/model-aliases?page=1&page_size=100`, {});
  });

  it("adds alias and current=true when they are given", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope(EMPTY_PAGE));

    await listModelAliases(ID, { alias: "models/Gemini-X", current: true }, 1, 20);

    expect(get).toHaveBeenCalledWith(
      `${PROVIDERS}/${ID}/model-aliases?page=1&page_size=20&alias=models%2FGemini-X&current=true`,
      {},
    );
  });

  it("maps an alias with the alias and the model id, alias untouched", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope({ id: ID }));

    await mapModelAlias(ID, { alias: "claude-sonnet-4-5-20250929", model_id: ID });

    expect(post).toHaveBeenCalledWith(`${PROVIDERS}/${ID}/model-aliases`, {
      alias: "claude-sonnet-4-5-20250929",
      model_id: ID,
    });
  });

  it("retires a segment with an empty object as the body", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope({ id: ID }));

    await retireModelAlias(ID, ID);

    expect(post).toHaveBeenCalledWith(`${PROVIDERS}/${ID}/model-aliases/${ID}/retire`, {});
  });

  it("turns a 409 into an ApiError carrying the code and the request id", async () => {
    vi.spyOn(client, "post").mockRejectedValue(
      rejection(409, "AI_MODEL_ALIAS_TAKEN", "The alias is a model code.", "req-409"),
    );

    const failure = mapModelAlias(ID, { alias: "claude-x", model_id: ID });

    await expect(failure).rejects.toBeInstanceOf(ApiError);
    await expect(failure).rejects.toMatchObject({
      code: "AI_MODEL_ALIAS_TAKEN",
      message: "The alias is a model code.",
      requestId: "req-409",
    });
  });
});

describe("field rules", () => {
  it("matches the backend patterns, case and all", () => {
    expect(METER_CODE_PATTERN.test("VIDEO_SECOND")).toBe(true);
    expect(METER_CODE_PATTERN.test("video_second")).toBe(false);
    expect(METER_CODE_PATTERN.test("V")).toBe(false);
    expect(UNIT_PATTERN.test("S")).toBe(true);
    expect(UNIT_PATTERN.test("SECOND ")).toBe(false);
    expect(PROVIDER_CODE_PATTERN.test("anthropic")).toBe(true);
    expect(PROVIDER_CODE_PATTERN.test("Anthropic")).toBe(false);
    expect(PROVIDER_CODE_PATTERN.test("a".repeat(65))).toBe(false);
    expect(MODEL_CODE_PATTERN.test("claude-sonnet-4-5-20250929")).toBe(true);
    expect(MODEL_CODE_PATTERN.test("models/gemini-x")).toBe(true);
    expect(MODEL_CODE_PATTERN.test("GPT-4o")).toBe(true);
    expect(MODEL_CODE_PATTERN.test("gpt 4o")).toBe(false);
    expect(MODEL_CODE_PATTERN.test(`a${"b".repeat(127)}`)).toBe(true);
    expect(MODEL_CODE_PATTERN.test(`a${"b".repeat(128)}`)).toBe(false);
  });
});
