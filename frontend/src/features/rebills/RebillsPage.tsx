/**
 * 管理端 Rebill 列表页（`/rebills`，AIH-TASK-257；设计闸门 #830 v12 §2「管理端页面」）。
 *
 * 语义以 docs/api.md「管理端 Rebill」为准：`GET /api/v1/admin/rebills`，按 Rebill id 倒序（最新确认的在前，顺序由后端决定，
 * 前端不重排），游标分页、不给总数。可按客户筛：文本框填客户的 `public_id`（同用量事件页的 customer_id 筛选），不存在的客户是
 * 空页、不报错。改筛选回到第一页。点一行跳到详情页。
 *
 * 翻页只有上一页 / 下一页：Next 用响应的 `next_cursor`（为 `null` 时禁用），Previous 用页面自己保存的游标栈 —— 栈里是已经翻过
 * 的每一页所用的游标，首页不带 cursor。筛选与游标放在组件状态里，不进地址栏（同用量事件页）。
 *
 * 客户列显示客户的 `public_id`（接口不返回公司名）；Ledger 按账本类型显示 Credit / Debit；Amount 原样显示账本金额字符串，
 * 前端不做任何金额运算。
 *
 * 三种状态都画出来（spec §132 DoD 第 8 条）：加载中、失败（带 request_id）、空列表。403 说「没有权限」。只请求
 * `/api/v1/admin/` 路径。
 *
 * 本文件后半是列表与详情两页共用的部件（账本类型文案、错误提示），照用量事件页的先例在这里导出。
 *
 * ⚠️ 说明不用 antd 的 `Alert`：它带 `role="alert"`，部署后浏览器验收把页面上任何 `role="alert"` 都当成错误提示
 * （scripts/acceptance/admin_customers.mjs 的 open_rebills）。
 */

import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { Alert, Button, Card, Form, Input, Space, Table, Typography, type TableColumnsType } from "antd";
import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router";

import { ADMIN_REQUIRED } from "../../api/adminCustomers";
import {
  REBILL_CREDIT,
  REBILL_DEBIT,
  REBILL_NOT_FOUND,
  REBILL_PAGE_LIMIT,
  listRebills,
  rebillListQueryKey,
  type RebillListParams,
  type RebillSummary,
} from "../../api/adminRebills";
import { ApiError } from "../../api/client";
import { DateTimeText } from "../../components/DateTimeText";
import { RequestReference } from "../../components/RequestReference";
import { rebillDetailPath } from "../../routes/paths";
import { LoadingBlock } from "../catalog/ProvidersPage";

interface FilterValues {
  customer_id?: string | undefined;
}

export function RebillsPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [form] = Form.useForm<FilterValues>();
  const [customerId, setCustomerId] = useState<string | undefined>(undefined);
  // 已经翻过的每一页所用的游标；空栈是首页（不带 cursor）。
  const [cursors, setCursors] = useState<string[]>([]);
  const cursor: string | undefined = cursors[cursors.length - 1];

  const params: RebillListParams = {
    ...(customerId === undefined ? {} : { customer_id: customerId }),
    ...(cursor === undefined ? {} : { cursor }),
    limit: REBILL_PAGE_LIMIT,
  };

  const rebills = useQuery({
    queryKey: rebillListQueryKey(params),
    queryFn: ({ signal }) => listRebills(params, signal),
    // 翻页时先留着上一页，不让整张表闪成骨架屏。
    placeholderData: keepPreviousData,
  });

  const apply = (values: FilterValues): void => {
    const value = (values.customer_id ?? "").trim();
    setCustomerId(value === "" ? undefined : value);
    setCursors([]);
  };

  const clear = (): void => {
    form.resetFields();
    apply({});
  };

  const open = (rebill: RebillSummary): void => {
    void navigate(rebillDetailPath(rebill.id));
  };

  const ledgerLabel = useLedgerLabel();

  const columns: TableColumnsType<RebillSummary> = [
    {
      key: "created_at",
      title: t("rebills.field.createdAt"),
      render: (_: unknown, rebill) => <DateTimeText value={rebill.created_at} />,
    },
    { key: "customer_id", title: t("rebills.field.customer"), dataIndex: "customer_id" },
    { key: "event_count", title: t("rebills.field.events"), render: (_: unknown, rebill) => String(rebill.event_count) },
    { key: "ledger_type", title: t("rebills.field.ledger"), render: (_: unknown, rebill) => ledgerLabel(rebill.ledger_type) },
    { key: "ledger_amount", title: t("rebills.field.amount"), dataIndex: "ledger_amount" },
    { key: "reason", title: t("rebills.field.reason"), dataIndex: "reason" },
    { key: "created_by_email", title: t("rebills.field.by"), dataIndex: "created_by_email" },
  ];

  let result: ReactNode;
  if (rebills.isPending) {
    result = <LoadingBlock text={t("rebills.list.loading")} />;
  } else if (rebills.isError) {
    result = (
      <RebillErrorAlert
        error={rebills.error}
        title={t("rebills.list.loadFailed")}
        onRetry={() => void rebills.refetch()}
      />
    );
  } else {
    const nextCursor = rebills.data.next_cursor;
    // 换页、换条件之后新的一页回来之前，表里留着的是上一份：这时的 next_cursor 不属于当前这一页，翻页按钮先关掉。
    const stale = rebills.isPlaceholderData;
    result = (
      <>
        <Table<RebillSummary>
          rowKey="id"
          columns={columns}
          dataSource={rebills.data.items}
          loading={rebills.isFetching}
          pagination={false}
          scroll={{ x: true }}
          locale={{ emptyText: customerId === undefined ? t("rebills.list.empty") : t("rebills.list.emptyFiltered") }}
          onRow={(rebill) => ({
            onClick: () => open(rebill),
            onKeyDown: (event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                open(rebill);
              }
            },
            tabIndex: 0,
            style: { cursor: "pointer" },
          })}
        />
        <Space style={{ marginTop: 16 }}>
          <Button disabled={stale || cursors.length === 0} onClick={() => setCursors(cursors.slice(0, -1))}>
            {t("rebills.list.previous")}
          </Button>
          <Button
            disabled={stale || nextCursor === null}
            onClick={() => {
              if (nextCursor !== null) {
                setCursors([...cursors, nextCursor]);
              }
            }}
          >
            {t("rebills.list.next")}
          </Button>
        </Space>
      </>
    );
  }

  return (
    <Card title={t("rebills.title")}>
      <Typography.Paragraph type="secondary">{t("rebills.intro")}</Typography.Paragraph>
      <Form<FilterValues>
        form={form}
        name="rebillFilters"
        layout="vertical"
        onFinish={apply}
        style={{ display: "flex", flexWrap: "wrap", columnGap: 16, alignItems: "flex-end" }}
      >
        <Form.Item name="customer_id" label={t("rebills.filter.customerId")}>
          <Input autoComplete="off" spellCheck={false} />
        </Form.Item>
        <Form.Item>
          <Space>
            <Button type="primary" htmlType="submit">
              {t("rebills.filter.apply")}
            </Button>
            <Button onClick={clear}>{t("rebills.filter.clear")}</Button>
          </Space>
        </Form.Item>
      </Form>
      {/* 列表与它的状态单独成一块，用例与验收脚本按 aria-label 找它。 */}
      <div role="region" aria-label={t("rebills.list.title")}>
        {result}
      </div>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// 列表与详情共用
// ---------------------------------------------------------------------------

/** 账本类型的文案：`REBILL_CREDIT` 是 Credit、`REBILL_DEBIT` 是 Debit；认不出的值原样返回。 */
export function useLedgerLabel(): (ledgerType: string) => string {
  const { t } = useTranslation();
  return (ledgerType) => {
    switch (ledgerType) {
      case REBILL_CREDIT:
        return t("rebills.ledger.credit");
      case REBILL_DEBIT:
        return t("rebills.ledger.debit");
      default:
        return ledgerType;
    }
  };
}

/**
 * Rebill 接口出错时的提示。403 `ADMIN_REQUIRED` 单独说「没有权限」（前端不做角色判断），404 `REBILL_NOT_FOUND` 说查不到；
 * 其余用调用方给的标题。下面都带后端 message 与 request_id（`RequestReference`）。
 */
export function RebillErrorAlert({ error, title, onRetry }: { error: Error; title: string; onRetry: () => void }) {
  const { t } = useTranslation();
  const code = error instanceof ApiError ? error.code : null;
  const forbidden = code === ADMIN_REQUIRED;
  let message = title;
  if (forbidden) {
    message = t("rebills.forbidden");
  } else if (code === REBILL_NOT_FOUND) {
    message = t("rebills.detail.notFound");
  }
  return (
    <Alert
      type={forbidden ? "warning" : "error"}
      showIcon
      style={{ marginBottom: 16 }}
      message={message}
      description={<RequestReference error={error} />}
      action={
        // 无权限重试也一样，不给按钮。
        forbidden ? undefined : (
          <Button size="small" onClick={onRetry}>
            {t("rebills.retry")}
          </Button>
        )
      }
    />
  );
}
