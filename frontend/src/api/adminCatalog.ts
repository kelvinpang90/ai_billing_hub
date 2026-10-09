/**
 * 管理端 AI 目录（契约见 docs/api.md「AI 目录：计量类型、供应商、模型与别名」，设计闸门 #163 v4，
 * 后端为 AIH-TASK-025）。
 *
 * 收录那一节的全部 15 个接口。每个都要 ADMIN，角色以后端数据库为准 —— 前端不做任何角色判断，
 * 403 原样交给页面显示。路径里的 id 一律是 `public_id`。
 *
 * ⚠️ 代码（计量类型 / 分量 / 单位 / 供应商 / 模型 / 别名）**按原样**收发：不去空白、不改大小写。
 * 后端按字节比较，`GPT-4o` 与 `gpt-4o` 是两个代码；这一层若替用户 `toLowerCase()`，存进去的就不是
 * 用户以为的那个字符串，上报的用量也就解析不到它。
 *
 * 目录里没有金额、单价或数量字段；将来出现时照 MoneyText 的做法当字符串处理，不在这一层解析。
 */

import { MAX_PAGE_SIZE, type Page } from "./adminCustomers";
import { apiGet, client, toApiError, unwrapEnvelope, type ApiEnvelope } from "./client";

const METER_TYPES_URL = "/api/v1/admin/usage-meter-types";
const PROVIDERS_URL = "/api/v1/admin/ai-providers";

// --- 错误码（docs/api.md 本节「本节专有的错误码」） ---------------------------------

export const USAGE_METER_TYPE_NOT_FOUND = "USAGE_METER_TYPE_NOT_FOUND";
export const AI_PROVIDER_NOT_FOUND = "AI_PROVIDER_NOT_FOUND";
export const AI_MODEL_NOT_FOUND = "AI_MODEL_NOT_FOUND";
export const AI_MODEL_ALIAS_NOT_FOUND = "AI_MODEL_ALIAS_NOT_FOUND";
export const USAGE_METER_TYPE_CODE_TAKEN = "USAGE_METER_TYPE_CODE_TAKEN";
export const USAGE_METER_COMPONENT_CODE_TAKEN = "USAGE_METER_COMPONENT_CODE_TAKEN";
export const AI_PROVIDER_CODE_TAKEN = "AI_PROVIDER_CODE_TAKEN";
export const AI_MODEL_CODE_TAKEN = "AI_MODEL_CODE_TAKEN";
export const AI_MODEL_ALIAS_TAKEN = "AI_MODEL_ALIAS_TAKEN";
/** 422：格式、多余字段、查询参数取值不对。 */
export const VALIDATION_ERROR = "VALIDATION_ERROR";

// --- 字段规则 -------------------------------------------------------------------
//
// 与后端 `app/schemas/ai_catalog.py` 是同一组正则，出处是设计文件
// docs/design/AIH-TASK-025-ai-catalog.md §2「接口」下的「字段规则」表（docs/api.md 本节「字段规则」同表）。
// 前端校验只是提前告诉用户；后端的 422 才是准绳。

/** 计量类型 `code` 与 `component_code`。 */
export const METER_CODE_PATTERN = /^[A-Z][A-Z0-9_]{1,31}$/;
/** 计量类型的 `unit`。 */
export const UNIT_PATTERN = /^[A-Z][A-Z0-9_]{0,15}$/;
/** 供应商 `code`。 */
export const PROVIDER_CODE_PATTERN = /^[a-z0-9][a-z0-9_-]{0,63}$/;
/** 模型 `code` 与别名。不含空白。 */
export const MODEL_CODE_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}$/;
/** `display_name` 去首尾空白后的最大长度（按码点数，与 Python 的 `len()` 一致）。 */
export const DISPLAY_NAME_MAX = 255;

export const CATALOG_STATUSES = ["ACTIVE", "RETIRED"] as const;
export type CatalogStatus = (typeof CATALOG_STATUSES)[number];

export const QUANTITY_KINDS = ["INTEGER", "DECIMAL"] as const;
export type QuantityKind = (typeof QUANTITY_KINDS)[number];

/** `QUANTITY` 是管理员新建的类型唯一可能的形态；`LLM_TOKEN_FIELDS` 只有种子 `LLM_TOKEN`。 */
export type PayloadShape = "LLM_TOKEN_FIELDS" | "QUANTITY";

// --- 对象 -----------------------------------------------------------------------
// 时间都是不带时区的 UTC，精确到秒（docs/api.md「时间」）。

export interface MeterComponent {
  component_code: string;
  /** 这个分量从上报事件的哪个字段取数量。 */
  quantity_field: string;
  created_at: string;
}

export interface MeterType {
  id: string;
  code: string;
  display_name: string;
  payload_shape: PayloadShape;
  unit: string;
  quantity_kind: QuantityKind;
  status: CatalogStatus;
  /** `component_code` 升序（由后端决定，前端不重排）。 */
  components: MeterComponent[];
  created_at: string;
  updated_at: string;
}

export interface Provider {
  id: string;
  code: string;
  display_name: string;
  status: CatalogStatus;
  created_at: string;
  updated_at: string;
}

export interface Model {
  id: string;
  /** 供应商的 `public_id`。 */
  provider_id: string;
  provider_code: string;
  code: string;
  display_name: string;
  status: CatalogStatus;
  created_at: string;
  updated_at: string;
}

/** 一段 = 「某个字符串在 `[effective_from, effective_to)` 内指向某个模型」。 */
export interface AliasSegment {
  id: string;
  alias: string;
  model_id: string;
  model_code: string;
  /** `null` = 「一直以来」；只有一个字符串的第一段是 `null`。 */
  effective_from: string | null;
  /** `null` = 仍未截断（当前段）。 */
  effective_to: string | null;
  created_at: string;
  closed_at: string | null;
}

/** 模型详情多一项：当前（未截断的段）指向它的别名段，`alias` 升序。 */
export interface ModelDetail extends Model {
  aliases: AliasSegment[];
}

// --- 请求体 ---------------------------------------------------------------------
// 所有请求体都拒绝多余字段（422），这里的类型也不给别的字段留位置。

/** 新建的类型固定为 `QUANTITY`：请求体不带 `payload_shape` / `quantity_field`（带了是 422）。 */
export interface CreateMeterTypeBody {
  code: string;
  display_name: string;
  unit: string;
  quantity_kind: QuantityKind;
  component_code: string;
}

export interface CreateProviderBody {
  code: string;
  display_name: string;
}

/** 供应商只来自路径（带 `provider_id` 是 422）。 */
export interface CreateModelBody {
  code: string;
  display_name: string;
}

/** 计量类型、供应商、模型的 PATCH 都只收这两项，至少一个，都不许是 `null`。 */
export interface CatalogEntryPatch {
  display_name?: string;
  status?: CatalogStatus;
}

export interface MapAliasBody {
  alias: string;
  /** **同一供应商下**模型的 `public_id`。 */
  model_id: string;
}

// --- 查询条件 -------------------------------------------------------------------

/** 计量类型、供应商、模型列表的筛选。不带 `status` 时列出全部。 */
export interface StatusFilter {
  status?: CatalogStatus;
}

export interface AliasFilter {
  /** 精确匹配，不做大小写折叠，最长 128。 */
  alias?: string;
  /** `true`：只列未截断的段。 */
  current?: boolean;
}

// --- 查询键 ---------------------------------------------------------------------

export const CATALOG_QUERY_KEY = ["admin", "catalog"] as const;
/** 计量类型的所有列表页：新建、改名、停用之后让它们整体过期。 */
export const METER_TYPE_LISTS_QUERY_KEY = [...CATALOG_QUERY_KEY, "meter-types", "list"] as const;
/** 供应商的所有列表页。 */
export const PROVIDER_LISTS_QUERY_KEY = [...CATALOG_QUERY_KEY, "providers", "list"] as const;

export function meterTypeListQueryKey(filter: StatusFilter, page: number, pageSize: number) {
  return [...METER_TYPE_LISTS_QUERY_KEY, filter, page, pageSize] as const;
}

export function providerListQueryKey(filter: StatusFilter, page: number, pageSize: number) {
  return [...PROVIDER_LISTS_QUERY_KEY, filter, page, pageSize] as const;
}

export function providerDetailQueryKey(providerId: string) {
  return [...CATALOG_QUERY_KEY, "providers", "detail", providerId] as const;
}

/** 一个供应商的所有模型列表页的前缀。 */
export function providerModelsQueryKey(providerId: string) {
  return [...providerDetailQueryKey(providerId), "models"] as const;
}

export function modelListQueryKey(
  providerId: string,
  filter: StatusFilter,
  page: number,
  pageSize: number,
) {
  return [...providerModelsQueryKey(providerId), filter, page, pageSize] as const;
}

/** 映射别名下拉用的全部模型；挂在同一前缀下，模型增改后随列表一起失效。 */
export function allModelsQueryKey(providerId: string) {
  return [...providerModelsQueryKey(providerId), "all"] as const;
}

/** 一个供应商的所有别名段列表页的前缀。 */
export function providerAliasesQueryKey(providerId: string) {
  return [...providerDetailQueryKey(providerId), "aliases"] as const;
}

export function aliasListQueryKey(
  providerId: string,
  filter: AliasFilter,
  page: number,
  pageSize: number,
) {
  return [...providerAliasesQueryKey(providerId), filter, page, pageSize] as const;
}

// --- 地址 -----------------------------------------------------------------------

// id 来自地址栏，不能信它只含 uuid 的字符：不转义的话 `a/b` 会变成另一条路径。
function meterTypeUrl(meterTypeId: string): string {
  return `${METER_TYPES_URL}/${encodeURIComponent(meterTypeId)}`;
}

function providerUrl(providerId: string): string {
  return `${PROVIDERS_URL}/${encodeURIComponent(providerId)}`;
}

function modelsUrl(providerId: string): string {
  return `${providerUrl(providerId)}/models`;
}

function modelUrl(providerId: string, modelId: string): string {
  return `${modelsUrl(providerId)}/${encodeURIComponent(modelId)}`;
}

function aliasesUrl(providerId: string): string {
  return `${providerUrl(providerId)}/model-aliases`;
}

/**
 * 分页参数加上筛选条件。⚠️ 没给的条件**不出现在查询串里**：后端对 `status=` 这种空值回 422，
 * 对 `alias=` 是按空串精确匹配，结果是一张莫名其妙的空列表。
 */
function listQuery(page: number, pageSize: number, extra: Record<string, string | undefined>): string {
  const query = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
  for (const [name, value] of Object.entries(extra)) {
    if (value !== undefined && value !== "") {
      query.set(name, value);
    }
  }
  return query.toString();
}

async function send<T>(method: "post" | "patch", url: string, body: unknown): Promise<T> {
  try {
    const response =
      method === "post"
        ? await client.post<ApiEnvelope<T>>(url, body)
        : await client.patch<ApiEnvelope<T>>(url, body);
    return unwrapEnvelope<T>(response.data);
  } catch (error) {
    throw toApiError(error);
  }
}

// --- 计量类型 -------------------------------------------------------------------

/** 计量类型分页，`code` 升序，每项带分量（顺序由后端决定，前端不重排）。 */
export function listMeterTypes(
  filter: StatusFilter,
  page: number,
  pageSize: number,
  signal?: AbortSignal,
): Promise<Page<MeterType>> {
  const query = listQuery(page, pageSize, { status: filter.status });
  return apiGet<Page<MeterType>>(`${METER_TYPES_URL}?${query}`, signal);
}

/** 新建 `QUANTITY` 类型，同一事务建出它唯一的分量。重发得 409，不会建出两个。 */
export function createMeterType(body: CreateMeterTypeBody): Promise<MeterType> {
  return send<MeterType>("post", METER_TYPES_URL, body);
}

export function getMeterType(meterTypeId: string, signal?: AbortSignal): Promise<MeterType> {
  return apiGet<MeterType>(meterTypeUrl(meterTypeId), signal);
}

export function updateMeterType(meterTypeId: string, patch: CatalogEntryPatch): Promise<MeterType> {
  return send<MeterType>("patch", meterTypeUrl(meterTypeId), patch);
}

// --- 供应商 ---------------------------------------------------------------------

export function listProviders(
  filter: StatusFilter,
  page: number,
  pageSize: number,
  signal?: AbortSignal,
): Promise<Page<Provider>> {
  const query = listQuery(page, pageSize, { status: filter.status });
  return apiGet<Page<Provider>>(`${PROVIDERS_URL}?${query}`, signal);
}

export function createProvider(body: CreateProviderBody): Promise<Provider> {
  return send<Provider>("post", PROVIDERS_URL, body);
}

export function getProvider(providerId: string, signal?: AbortSignal): Promise<Provider> {
  return apiGet<Provider>(providerUrl(providerId), signal);
}

export function updateProvider(providerId: string, patch: CatalogEntryPatch): Promise<Provider> {
  return send<Provider>("patch", providerUrl(providerId), patch);
}

// --- 模型 -----------------------------------------------------------------------

/** 这个供应商的模型分页，`code` 升序。 */
export function listModels(
  providerId: string,
  filter: StatusFilter,
  page: number,
  pageSize: number,
  signal?: AbortSignal,
): Promise<Page<Model>> {
  const query = listQuery(page, pageSize, { status: filter.status });
  return apiGet<Page<Model>>(`${modelsUrl(providerId)}?${query}`, signal);
}

/**
 * 这个供应商的**全部**模型（含已停用的），按最大页长逐页取完再拼起来，`code` 升序。给映射别名的
 * 下拉用：只取一页的话，第 101 个及之后的模型就选不到。取到空页或凑够 `total` 即停。
 */
export async function listAllModels(providerId: string, signal?: AbortSignal): Promise<Model[]> {
  const models: Model[] = [];
  for (let page = 1; ; page += 1) {
    const result = await listModels(providerId, {}, page, MAX_PAGE_SIZE, signal);
    models.push(...result.items);
    if (result.items.length === 0 || models.length >= result.total) {
      return models;
    }
  }
}

export function createModel(providerId: string, body: CreateModelBody): Promise<Model> {
  return send<Model>("post", modelsUrl(providerId), body);
}

export function getModel(
  providerId: string,
  modelId: string,
  signal?: AbortSignal,
): Promise<ModelDetail> {
  return apiGet<ModelDetail>(modelUrl(providerId, modelId), signal);
}

export function updateModel(
  providerId: string,
  modelId: string,
  patch: CatalogEntryPatch,
): Promise<Model> {
  return send<Model>("patch", modelUrl(providerId, modelId), patch);
}

// --- 别名段 ---------------------------------------------------------------------

/** 全部历史，按 `alias`、`effective_from`（`null` 在前）排序（由后端决定，前端不重排）。 */
export function listModelAliases(
  providerId: string,
  filter: AliasFilter,
  page: number,
  pageSize: number,
  signal?: AbortSignal,
): Promise<Page<AliasSegment>> {
  const query = listQuery(page, pageSize, {
    alias: filter.alias,
    // 只有 `true` 有意义；`false` 与不带一样，所以不发。
    current: filter.current === true ? "true" : undefined,
  });
  return apiGet<Page<AliasSegment>>(`${aliasesUrl(providerId)}?${query}`, signal);
}

/**
 * 映射：返回当前未截断的那一段（写了新段是 201，已经指向这个模型是 200、什么都不写）。
 * 只影响边界时刻 `t` 及以后发生的用量；字符串从没映射过时第一段对过去全部生效。
 */
export function mapModelAlias(providerId: string, body: MapAliasBody): Promise<AliasSegment> {
  return send<AliasSegment>("post", aliasesUrl(providerId), body);
}

/** 撤销：截断当前段，返回被截断的那一段。请求体是 `{}`。 */
export function retireModelAlias(providerId: string, aliasId: string): Promise<AliasSegment> {
  return send<AliasSegment>(
    "post",
    `${aliasesUrl(providerId)}/${encodeURIComponent(aliasId)}/retire`,
    {},
  );
}
