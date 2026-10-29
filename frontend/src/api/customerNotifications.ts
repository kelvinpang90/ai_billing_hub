/**
 * 客户自己的门户通知（`GET /api/v1/customer/notifications`、`POST …/{notification_id}/read`、`POST …/read-all`）。
 *
 * 语义以 docs/api.md「客户通知」为准（AIH-TASK-127，设计闸门 #471 v7）。列表是读接口；两个标已读接口是**写接口**：
 * 账户 `DISABLED` / `CLOSED` 时 403 `ACCOUNT_READ_ONLY`（{@link ACCOUNT_READ_ONLY}）。
 *
 * ⚠️ **不带任何租户或客户标识**：客户是谁只取自访问令牌。这一层也不接受这类参数。
 *
 * 排序是**记账顺序**倒序，不是时间：相邻两条的 `occurred_at` 可能不单调（较新的一条时间略早）。这一层与页面都不重排。
 *
 * 分页：省略 `cursor` 是最新一页；更早的一页把响应的 `next_cursor` 原样作为 `cursor` 发回去，它为 `null` 时没有更早的
 * 通知。游标只原样传回，前端不解析、不拼装。
 *
 * 两个写接口都**不带请求体**。
 *
 * ⚠️ 金额是**字符串**（INV-10），类型刻意写成 `string`，不在这一层解析。
 */

import { apiGet, client, toApiError, unwrapEnvelope, type ApiEnvelope } from "./client";

export const CUSTOMER_NOTIFICATIONS_URL = "/api/v1/customer/notifications";
export const CUSTOMER_NOTIFICATIONS_READ_ALL_URL = "/api/v1/customer/notifications/read-all";

/** 账户只读时两个标已读接口的错误码（docs/api.md「客户通知」的错误表）。 */
export const ACCOUNT_READ_ONLY = "ACCOUNT_READ_ONLY";

/** 一条通知。恰好这 6 个键：没有内部 id、客户标识、来源事件或任何成本、毛利字段。 */
export interface CustomerNotification {
  /** 小写 uuid；标已读与游标都用它。 */
  notification_id: string;
  /** `LOW_BALANCE` / `SUSPENDED` / `REACTIVATED`；类型刻意写成 `string`，认不出的值由页面原样显示。 */
  notification_type: string;
  /** 事件发生时刻（触发它的那笔流水的记账时间），不带时区的 UTC，精确到秒。 */
  occurred_at: string;
  /** 事件当时、那笔流水之后的余额，恰好 8 位小数的十进制字符串，可以为负。 */
  balance: string;
  /** 只有 `LOW_BALANCE` 有：事件当时的阈值，8 位小数字符串；其他类型为 `null`。 */
  threshold: string | null;
  /** 首次标为已读的时刻（格式同 `occurred_at`）；`null` = 未读。 */
  read_at: string | null;
}

export interface CustomerNotificationPage {
  items: CustomerNotification[];
  /** 还有更早的通知时是一个不透明字符串，否则 `null`。 */
  next_cursor: string | null;
  /** 本客户全部未读通知的条数，不受分页影响。 */
  unread_count: number;
}

export interface CustomerNotificationsReadAll {
  /** 本次标记的条数；没有未读时为 0。 */
  marked: number;
}

/** 查询条件，都可选：不带（`undefined`、`null` 或空串）的就不进查询串。 */
export interface CustomerNotificationQuery {
  pageSize?: number;
  cursor?: string | null;
}

/** 通知相关的 key 都以它开头。 */
export const CUSTOMER_NOTIFICATIONS_QUERY_KEY = ["customer", "notifications"] as const;

/** 门户导航上的未读数（只存一个数字）。 */
export const CUSTOMER_NOTIFICATIONS_UNREAD_QUERY_KEY = [...CUSTOMER_NOTIFICATIONS_QUERY_KEY, "unread"] as const;

/** 通知页的列表（按页追加）。 */
export function customerNotificationListQueryKey(pageSize: number) {
  return [...CUSTOMER_NOTIFICATIONS_QUERY_KEY, "list", pageSize] as const;
}

function present(value: string | null | undefined): value is string {
  return value !== undefined && value !== null && value !== "";
}

/** 列表的地址（含查询串）：按 `page_size`、`cursor` 的顺序，不带的不放。 */
export function customerNotificationsUrl(query: CustomerNotificationQuery = {}): string {
  const params = new URLSearchParams();
  if (query.pageSize !== undefined) {
    params.set("page_size", String(query.pageSize));
  }
  if (present(query.cursor)) {
    params.set("cursor", query.cursor);
  }
  const search = params.toString();
  return search === "" ? CUSTOMER_NOTIFICATIONS_URL : `${CUSTOMER_NOTIFICATIONS_URL}?${search}`;
}

/** 单条标已读的地址。id 只来自列表（小写 uuid），仍按路径段转义。 */
export function customerNotificationReadUrl(notificationId: string): string {
  return `${CUSTOMER_NOTIFICATIONS_URL}/${encodeURIComponent(notificationId)}/read`;
}

/** 一页通知与未读数，记账顺序倒序（顺序由后端决定，前端不重排）。 */
export function listCustomerNotifications(
  query: CustomerNotificationQuery = {},
  signal?: AbortSignal,
): Promise<CustomerNotificationPage> {
  return apiGet<CustomerNotificationPage>(customerNotificationsUrl(query), signal);
}

/** 不带请求体的 POST。 */
async function post<T>(url: string): Promise<T> {
  try {
    const response = await client.post<ApiEnvelope<T>>(url);
    return unwrapEnvelope<T>(response.data);
  } catch (error) {
    throw toApiError(error);
  }
}

/** 把一条标为已读，回这一条（`read_at` 已有值；已读的再标一次保留首次时刻）。 */
export function markCustomerNotificationRead(notificationId: string): Promise<CustomerNotification> {
  return post<CustomerNotification>(customerNotificationReadUrl(notificationId));
}

/** 把此刻全部未读标为已读，回本次标记的条数。 */
export function markAllCustomerNotificationsRead(): Promise<CustomerNotificationsReadAll> {
  return post<CustomerNotificationsReadAll>(CUSTOMER_NOTIFICATIONS_READ_ALL_URL);
}
