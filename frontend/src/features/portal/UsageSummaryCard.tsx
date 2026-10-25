/**
 * 门户首页的本月用量卡（`GET /api/v1/customer/usage-summary`，AIH-TASK-108；设计闸门 #403 v1 §2「金额」「前端」、
 * §8 第 2 步）。语义以 docs/api.md 的 usage-summary 小节为准。
 *
 * - 不带 `month`：后端按请求时刻取马来西亚日历月，卡片标题下的月份就是响应的 `month`（不在前端另算「本月」）。
 * - 计费额（合计与每天）按设计 §2「金额」最多 6 位：`formatAmountRounded`（字符串 `ROUND_HALF_UP`），不转成 number。
 * - 对话数旁注「只计带对话标识的请求」：AI 模块代为上报的调用没有对话标识，不计入（docs/api.md）。
 * - 按日柱状图用纯 CSS 画，不引入图表依赖：柱高只按整数 `request_count` 相对当月最大值的比例；最大值为 0（整月没有
 *   请求）时所有柱高为 0，不做除法。每根柱的提示（`title`）是当天日期、请求数与计费额（6 位）。
 * - 卡片链到用量页，不带参数（用量页默认就是本月）。
 * - 卡自己请求、自己画加载中、失败（带 request_id）与 `QUERY_TOO_BROAD`（专门文案）：哪一种都不影响首页其余部分。
 *
 * ⚠️ 只显示这几个字段：接口本来就不返回成本、毛利或内部字段（INV-7），这里也没有它们的位置。
 */

import { useQuery } from "@tanstack/react-query";
import { Alert, Button, Card, Descriptions, Skeleton, Space, Typography, theme } from "antd";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import { ApiError } from "../../api/client";
import {
  QUERY_TOO_BROAD,
  customerUsageSummaryQueryKey,
  getCustomerUsageSummary,
  type CustomerUsageSummary,
  type CustomerUsageSummaryDay,
} from "../../api/customerUsage";
import { formatAmountRounded } from "../../components/MoneyText";
import { RequestReference } from "../../components/RequestReference";
import { ROUTES } from "../../routes/paths";

/** spec §80「Detailed usage may display up to 6 decimals」；与用量页同一规则。 */
const USAGE_PLACES = 6;

/** 柱状图的高度（像素）。 */
const CHART_HEIGHT = 120;

/**
 * 柱高占图高的百分比：`count` 相对当月最大值 `max` 的比例。`max` 为 0 时一律 0，不做除法。
 * 只用整数请求数，金额不参与。
 */
export function barHeightPercent(count: number, max: number): number {
  if (max <= 0) {
    return 0;
  }
  return (count * 100) / max;
}

function DailyRequestsChart({ days }: { days: CustomerUsageSummaryDay[] }) {
  const { t } = useTranslation();
  const { token } = theme.useToken();
  const max = days.reduce((highest, day) => Math.max(highest, day.request_count), 0);

  return (
    <Space direction="vertical" size="small" style={{ width: "100%" }}>
      <Typography.Text type="secondary">{t("portal.home.usageSummary.chart.caption")}</Typography.Text>
      <div
        role="list"
        aria-label={t("portal.home.usageSummary.chart.caption")}
        style={{ display: "flex", alignItems: "stretch", gap: 2, height: CHART_HEIGHT }}
      >
        {days.map((day) => {
          const tip = t("portal.home.usageSummary.chart.tip", {
            date: day.date,
            requests: day.request_count,
            amount: formatAmountRounded(day.charged_amount, USAGE_PLACES),
          });
          return (
            // 整列都带提示：没有请求的日子柱高为 0，也指得到。
            <div
              key={day.date}
              role="listitem"
              title={tip}
              aria-label={tip}
              style={{ flex: 1, display: "flex", alignItems: "flex-end", background: token.colorFillQuaternary }}
            >
              <div
                data-bar=""
                style={{
                  width: "100%",
                  height: `${barHeightPercent(day.request_count, max)}%`,
                  background: token.colorPrimary,
                }}
              />
            </div>
          );
        })}
      </div>
    </Space>
  );
}

function UsageSummaryBody({ summary }: { summary: CustomerUsageSummary }) {
  const { t } = useTranslation();
  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <Typography.Text strong>{t("portal.home.usageSummary.month", { month: summary.month })}</Typography.Text>
      <Descriptions bordered column={1} size="small">
        <Descriptions.Item label={t("portal.home.usageSummary.field.chargedAmount")}>
          <span style={{ fontVariantNumeric: "tabular-nums" }}>
            {formatAmountRounded(summary.charged_amount, USAGE_PLACES)}
          </span>
        </Descriptions.Item>
        <Descriptions.Item label={t("portal.home.usageSummary.field.requestCount")}>
          <span style={{ fontVariantNumeric: "tabular-nums" }}>{summary.request_count}</span>
        </Descriptions.Item>
        <Descriptions.Item label={t("portal.home.usageSummary.field.conversationCount")}>
          <span style={{ fontVariantNumeric: "tabular-nums" }}>{summary.conversation_count}</span>
        </Descriptions.Item>
      </Descriptions>
      <Typography.Text type="secondary">{t("portal.home.usageSummary.conversationNote")}</Typography.Text>
      <DailyRequestsChart days={summary.days} />
    </Space>
  );
}

export function UsageSummaryCard() {
  const { t } = useTranslation();
  const summary = useQuery({
    queryKey: customerUsageSummaryQueryKey(),
    queryFn: ({ signal }) => getCustomerUsageSummary(null, signal),
    // 理由同余额卡：同一个标签页换账号登录，拿到的必须是新客户的用量。
    gcTime: 0,
  });

  let body: ReactNode;
  if (summary.isPending) {
    body = (
      <Space direction="vertical" aria-busy="true" style={{ width: "100%" }}>
        <Skeleton active title={false} paragraph={{ rows: 3 }} />
        <Typography.Text type="secondary">{t("portal.home.usageSummary.loading")}</Typography.Text>
      </Space>
    );
  } else if (summary.isError) {
    // 整月汇总超过后端的执行预算：不是出错，是这一块暂时算不出；用量页按较短区间可以查。
    const tooBroad = summary.error instanceof ApiError && summary.error.code === QUERY_TOO_BROAD;
    body = (
      <Alert
        type={tooBroad ? "warning" : "error"}
        showIcon
        message={tooBroad ? t("portal.home.usageSummary.queryTooBroad") : t("portal.home.usageSummary.loadFailed")}
        description={<RequestReference error={summary.error} />}
        action={
          <Button size="small" onClick={() => void summary.refetch()}>
            {t("portal.home.retry")}
          </Button>
        }
      />
    );
  } else {
    body = <UsageSummaryBody summary={summary.data} />;
  }

  return (
    // role + aria-label：部署后验收脚本按语义结构找到这张卡（不依赖 antd 的类名）。
    <Card
      role="region"
      aria-label={t("portal.home.usageSummary.title")}
      title={t("portal.home.usageSummary.title")}
      extra={<Link to={ROUTES.portalUsage}>{t("portal.home.usageSummary.viewUsage")}</Link>}
    >
      {body}
    </Card>
  );
}
