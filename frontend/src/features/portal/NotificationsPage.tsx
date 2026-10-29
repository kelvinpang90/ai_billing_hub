/**
 * 客户门户通知页（`/portal/notifications`，AIH-TASK-128；设计闸门 #471 v7 §2「前端」、§8）。
 *
 * 语义以 docs/api.md「客户通知」为准：列表来自 `GET /api/v1/customer/notifications`，单条与全部标已读是
 * `POST …/{notification_id}/read` 与 `POST …/read-all`（写接口，账户只读时 403 `ACCOUNT_READ_ONLY`，有专门文案）。
 *
 * ⚠️ **行序就是接口返回的顺序**（记账顺序倒序），前端不按时间或任何别的键重排：极少数并发调账下，相邻两条的时间
 * 可能不单调（较新的一条时间略早），那正是记账先后，照原样显示。「Load more」把 `next_cursor` 原样作为下一页的
 * `cursor`，新的一页追加在表格末尾；为 `null` 时按钮不出现。
 *
 * 列：时间（马来西亚时间，标 MYT，与用量页同一个 `UsageTime`）、类型说明（三种类型各一条文案，低余额的带阈值；认不出
 * 的类型原样显示）、余额（与钱包页同一个 `formatAmountRounded`，2 位小数）、状态（未读行一个「Mark as read」）。
 * 未读行加粗。卡片右上角一个「Mark all as read」，没有未读时不可用。
 *
 * 标已读成功后**就地更新**：单条用响应里的那一行替换缓存里的同一行、未读数减一；全部已读把已加载的未读行记为已读、
 * 未读数归零。都不重新拉列表。门户导航上的未读数（`PortalLayout`）随后重读一次。
 *
 * 三种状态都画出来：加载中、失败（带 request_id）、空列表。
 *
 * ⚠️ 只请求 `/api/v1/customer/*`：不请求任何 `/api/v1/admin/` 路径，不显示成本、毛利或内部字段（INV-7）。
 */

import { useInfiniteQuery, useMutation, useQueryClient, type InfiniteData } from "@tanstack/react-query";
import { Alert, Button, Card, Empty, Skeleton, Space, Table, Typography, type TableColumnsType } from "antd";
import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { ApiError } from "../../api/client";
import {
  ACCOUNT_READ_ONLY,
  CUSTOMER_NOTIFICATIONS_UNREAD_QUERY_KEY,
  customerNotificationListQueryKey,
  listCustomerNotifications,
  markAllCustomerNotificationsRead,
  markCustomerNotificationRead,
  type CustomerNotification,
  type CustomerNotificationPage,
} from "../../api/customerNotifications";
import { formatAmountRounded } from "../../components/MoneyText";
import { RequestReference } from "../../components/RequestReference";
import { UsageTime } from "./UsageDetail";

/** 每页条数：与接口的默认值相同（docs/api.md：1–100，默认 20）。 */
export const NOTIFICATIONS_PAGE_SIZE = 20;

/** 余额与阈值的显示位数：与钱包页相同（spec §80「MYR normal display → 2 decimals」）。 */
const DISPLAY_PLACES = 2;

const LIST_QUERY_KEY = customerNotificationListQueryKey(NOTIFICATIONS_PAGE_SIZE);

type NotificationPages = InfiniteData<CustomerNotificationPage, string | null>;

/** 类型说明。每个 key 都写成字面量，i18n 的 key 检查才找得到它们；三种之外的类型原样显示。 */
function NotificationText({ row }: { row: CustomerNotification }) {
  const { t } = useTranslation();
  switch (row.notification_type) {
    case "LOW_BALANCE":
      return row.threshold === null
        ? t("portal.notifications.type.lowBalanceWithoutThreshold")
        : t("portal.notifications.type.lowBalance", {
            threshold: formatAmountRounded(row.threshold, DISPLAY_PLACES),
          });
    case "SUSPENDED":
      return t("portal.notifications.type.suspended");
    case "REACTIVATED":
      return t("portal.notifications.type.reactivated");
    default:
      return row.notification_type;
  }
}

/** 标已读失败的说明：账户只读有专门文案，其余是通用文案（request_id 另外显示）。 */
function isReadOnly(error: Error): boolean {
  return error instanceof ApiError && error.code === ACCOUNT_READ_ONLY;
}

/** 把每一页里的行与未读数按 `update` 改掉，页的顺序与行的顺序都不动。 */
function updatePages(
  data: NotificationPages | undefined,
  update: (page: CustomerNotificationPage) => CustomerNotificationPage,
): NotificationPages | undefined {
  return data === undefined ? data : { ...data, pages: data.pages.map(update) };
}

export function NotificationsPage() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  // 「全部已读」成功时已加载的未读行：接口只回条数、不回时刻，这些行的 `read_at` 不编造，只在这里记下它们已读。
  const [readIds, setReadIds] = useState<ReadonlySet<string>>(() => new Set());

  const notifications = useInfiniteQuery({
    queryKey: LIST_QUERY_KEY,
    queryFn: ({ pageParam, signal }) =>
      listCustomerNotifications({ pageSize: NOTIFICATIONS_PAGE_SIZE, cursor: pageParam }, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor,
    // 理由同钱包页：同一个标签页换账号登录，拿到的必须是新客户的通知。
    gcTime: 0,
  });

  const isUnread = (row: CustomerNotification): boolean => row.read_at === null && !readIds.has(row.notification_id);

  const pages = notifications.data?.pages ?? [];
  // 行序就是接口给的顺序：逐页按原样接起来，不排序。
  const rows = pages.flatMap((page) => page.items);
  // 最后读到的那一页最新；标已读时每一页的未读数一起改，所以取哪一页都一致。
  const unreadCount = pages.at(-1)?.unread_count ?? 0;

  const refreshNavCount = (): void => {
    void queryClient.invalidateQueries({ queryKey: CUSTOMER_NOTIFICATIONS_UNREAD_QUERY_KEY });
  };

  const markOne = useMutation({
    mutationFn: (notificationId: string) => markCustomerNotificationRead(notificationId),
    onSuccess: (row) => {
      const cached = queryClient
        .getQueryData<NotificationPages>(LIST_QUERY_KEY)
        ?.pages.flatMap((page) => page.items)
        .find((item) => item.notification_id === row.notification_id);
      const wasUnread = cached !== undefined && isUnread(cached);
      queryClient.setQueryData<NotificationPages>(LIST_QUERY_KEY, (data) =>
        updatePages(data, (page) => ({
          ...page,
          items: page.items.map((item) => (item.notification_id === row.notification_id ? row : item)),
          unread_count: wasUnread ? Math.max(0, page.unread_count - 1) : page.unread_count,
        })),
      );
      refreshNavCount();
    },
  });

  const markAll = useMutation({
    mutationFn: () => markAllCustomerNotificationsRead(),
    onSuccess: () => {
      const loaded = queryClient.getQueryData<NotificationPages>(LIST_QUERY_KEY)?.pages ?? [];
      const marked = loaded.flatMap((page) => page.items).filter((item) => item.read_at === null);
      setReadIds((previous) => new Set([...previous, ...marked.map((item) => item.notification_id)]));
      queryClient.setQueryData<NotificationPages>(LIST_QUERY_KEY, (data) =>
        updatePages(data, (page) => ({ ...page, unread_count: 0 })),
      );
      refreshNavCount();
    },
  });

  const startMarkOne = (notificationId: string): void => {
    markAll.reset();
    markOne.mutate(notificationId);
  };

  const startMarkAll = (): void => {
    // 按钮在进行中与没有未读时已禁用；这里再挡一次，连点也只发一个请求。
    if (markAll.isPending || unreadCount === 0) {
      return;
    }
    markOne.reset();
    markAll.mutate();
  };

  const actionError = markOne.error ?? markAll.error;

  const columns: TableColumnsType<CustomerNotification> = [
    {
      key: "occurred_at",
      title: t("portal.notifications.field.occurredAt"),
      render: (_: unknown, row) => <UsageTime value={row.occurred_at} />,
    },
    {
      key: "notification_type",
      title: t("portal.notifications.field.type"),
      render: (_: unknown, row) => <NotificationText row={row} />,
    },
    {
      key: "balance",
      title: t("portal.notifications.field.balance"),
      align: "right",
      render: (_: unknown, row) => (
        <span style={{ fontVariantNumeric: "tabular-nums" }}>{formatAmountRounded(row.balance, DISPLAY_PLACES)}</span>
      ),
    },
    {
      key: "status",
      title: t("portal.notifications.field.status"),
      render: (_: unknown, row) =>
        isUnread(row) ? (
          <Button
            size="small"
            onClick={() => startMarkOne(row.notification_id)}
            loading={markOne.isPending && markOne.variables === row.notification_id}
            disabled={markAll.isPending || (markOne.isPending && markOne.variables === row.notification_id)}
          >
            {t("portal.notifications.markRead")}
          </Button>
        ) : (
          <Typography.Text type="secondary">{t("portal.notifications.read")}</Typography.Text>
        ),
    },
  ];

  let body: ReactNode;
  if (notifications.isPending) {
    body = (
      <Space direction="vertical" aria-busy="true" style={{ width: "100%" }}>
        <Skeleton active title={false} paragraph={{ rows: 3 }} />
        <Typography.Text type="secondary">{t("portal.notifications.loading")}</Typography.Text>
      </Space>
    );
  } else if (notifications.data === undefined) {
    body = (
      <Alert
        type="error"
        showIcon
        message={t("portal.notifications.loadFailed")}
        description={notifications.error ? <RequestReference error={notifications.error} /> : null}
        action={
          <Button size="small" onClick={() => void notifications.refetch()}>
            {t("portal.notifications.retry")}
          </Button>
        }
      />
    );
  } else if (rows.length === 0) {
    body = <Empty description={t("portal.notifications.empty")} />;
  } else {
    body = (
      <Space direction="vertical" style={{ width: "100%" }}>
        <Table<CustomerNotification>
          rowKey="notification_id"
          columns={columns}
          dataSource={rows}
          pagination={false}
          scroll={{ x: "max-content" }}
          onRow={(row) => (isUnread(row) ? { style: { fontWeight: 600 } } : {})}
        />
        {notifications.isFetchNextPageError && notifications.error ? (
          <Alert
            type="error"
            showIcon
            message={t("portal.notifications.loadMoreFailed")}
            description={<RequestReference error={notifications.error} />}
          />
        ) : null}
        {notifications.hasNextPage ? (
          <Button
            onClick={() => void notifications.fetchNextPage()}
            loading={notifications.isFetchingNextPage}
            disabled={notifications.isFetchingNextPage}
          >
            {t("portal.notifications.loadMore")}
          </Button>
        ) : null}
      </Space>
    );
  }

  return (
    // role + aria-label：部署后验收脚本（check_portal_notifications）按语义结构找到这一块（不依赖 antd 的类名）。
    <Card
      role="region"
      aria-label={t("portal.notifications.title")}
      title={t("portal.notifications.title")}
      extra={
        notifications.data !== undefined ? (
          <Button
            onClick={startMarkAll}
            loading={markAll.isPending}
            disabled={unreadCount === 0 || markAll.isPending}
          >
            {t("portal.notifications.markAllRead")}
          </Button>
        ) : null
      }
    >
      <Typography.Paragraph type="secondary">{t("portal.notifications.note")}</Typography.Paragraph>
      {notifications.data !== undefined ? (
        <Typography.Paragraph>{t("portal.notifications.unreadCount", { unread: unreadCount })}</Typography.Paragraph>
      ) : null}
      {actionError ? (
        <Alert
          type="error"
          showIcon
          closable
          onClose={() => {
            markOne.reset();
            markAll.reset();
          }}
          style={{ marginBottom: 16 }}
          message={isReadOnly(actionError) ? t("portal.notifications.readOnly") : t("portal.notifications.markFailed")}
          description={isReadOnly(actionError) ? null : <RequestReference error={actionError} />}
        />
      ) : null}
      {body}
    </Card>
  );
}
