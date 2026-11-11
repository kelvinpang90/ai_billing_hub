/**
 * 客户门户充值页（`/portal/topup`，AIH-TASK-192；设计闸门 #587 v3 §2「门户页面」）。
 *
 * 语义以 docs/api.md「支付（客户）」与「客户建单」为准。进页面读一次 `GET /api/v1/customer/topup-options`：
 * - `available` 为假：只按 `unavailable_reason` 显示对应的说明（五种各有文案），不显示表单。
 * - `available` 为真：推荐金额是一排按钮，低于 `effective_minimum` 或高于 `max_amount` 的不可点；**点了只把金额填进金额
 *   输入框**，不提交。自定义金额就在同一个输入框里：去掉首尾空白后按 {@link AMOUNT_PATTERN} 即时提示，下限是
 *   `effective_minimum`（不是 `min_amount`），上限是 `max_amount`（为空时不设，正则已限住 999999.99）。有
 *   `recovery_minimum` 时在表单上方说明至少充值 `effective_minimum` 才能恢复服务。
 * - 「Go to payment」才发 `POST /api/v1/customer/topups`（请求体只有去空白后的 `amount` 字符串）；金额不合规、请求中、
 *   成功之后都不可点。成功后 `window.location.assign(redirect_url)`：只跳后端返回的地址，不拼接、不读查询串里的地址
 *   （防开放重定向，设计 §6「跳转」）。失败显示错误码、信息与 request_id；`TOPUP_BELOW_RECOVERY_MINIMUM` 时重新读一次
 *   充值选项，页面上的金额随之刷新。
 *
 * 金额全部来自接口（不写死任何金额），用 `MoneyText` 显示、币种取响应的 `currency`；比较只做字符串运算，不经浮点（INV-10）。
 *
 * ⚠️ 只请求 `/api/v1/customer/*`：不请求任何 `/api/v1/admin/` 路径。
 */

import { useMutation, useQuery } from "@tanstack/react-query";
import { Alert, Button, Card, Form, Input, Skeleton, Space, Typography } from "antd";
import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { ApiError } from "../../api/client";
import {
  CUSTOMER_TOPUP_OPTIONS_QUERY_KEY,
  TOPUP_BELOW_RECOVERY_MINIMUM,
  createTopup,
  getTopupOptions,
  type TopupOptions,
} from "../../api/customerPayments";
import { MoneyText } from "../../components/MoneyText";
import { RequestReference } from "../../components/RequestReference";

/** 建单金额的格式（docs/api.md「客户建单」）：1–6 位整数、可带 1–2 位小数。 */
export const AMOUNT_PATTERN = /^[0-9]{1,6}(\.[0-9]{1,2})?$/;

/**
 * 跳去付款页。包一层只为测试能替换它：jsdom 的 `window.location.assign` 不可改写、也不真的跳转。
 * 地址原样交给 `window.location.assign`，不拼接、不改写。
 */
export const topupRedirect = {
  assign(url: string): void {
    window.location.assign(url);
  },
};

const DECIMAL = /^([0-9]+)(?:\.([0-9]+))?$/;

/**
 * 比较两个非负十进制字符串：a < b 回负数、相等回 0、a > b 回正数；任一个不是十进制字符串时回 null。
 * 只做字符串运算（去掉整数部分的前导 0、小数部分补齐到同样长度后逐字比较），不经浮点。
 */
export function compareAmounts(a: string, b: string): number | null {
  const left = DECIMAL.exec(a);
  const right = DECIMAL.exec(b);
  if (left === null || right === null) {
    return null;
  }
  const leftInteger = (left[1] ?? "").replace(/^0+/, "");
  const rightInteger = (right[1] ?? "").replace(/^0+/, "");
  if (leftInteger.length !== rightInteger.length) {
    return leftInteger.length - rightInteger.length;
  }
  if (leftInteger !== rightInteger) {
    return leftInteger < rightInteger ? -1 : 1;
  }
  const width = Math.max((left[2] ?? "").length, (right[2] ?? "").length);
  const leftFraction = (left[2] ?? "").padEnd(width, "0");
  const rightFraction = (right[2] ?? "").padEnd(width, "0");
  if (leftFraction === rightFraction) {
    return 0;
  }
  return leftFraction < rightFraction ? -1 : 1;
}

/** 金额在 [effective_minimum, max_amount] 之内（`max_amount` 为空时不设上限）。比较不了时当作不在。 */
function withinLimits(amount: string, options: TopupOptions): boolean {
  const lower = compareAmounts(amount, options.effective_minimum);
  if (lower === null || lower < 0) {
    return false;
  }
  if (options.max_amount === null) {
    return true;
  }
  const upper = compareAmounts(amount, options.max_amount);
  return upper !== null && upper <= 0;
}

type AmountProblem = "empty" | "format" | "belowMinimum" | "aboveMaximum";

/** 去空白后的金额哪里不合规；合规时回 null。 */
function amountProblem(amount: string, options: TopupOptions): AmountProblem | null {
  if (amount === "") {
    return "empty";
  }
  if (!AMOUNT_PATTERN.test(amount)) {
    return "format";
  }
  const lower = compareAmounts(amount, options.effective_minimum);
  if (lower === null || lower < 0) {
    return "belowMinimum";
  }
  if (options.max_amount !== null) {
    const upper = compareAmounts(amount, options.max_amount);
    if (upper === null || upper > 0) {
      return "aboveMaximum";
    }
  }
  return null;
}

function UnavailableText({ reason }: { reason: string | null }) {
  const { t } = useTranslation();
  switch (reason) {
    case "PAYMENTS_DISABLED":
      return t("portal.topup.unavailable.paymentsDisabled");
    case "ACCOUNT_READ_ONLY":
      return t("portal.topup.unavailable.accountReadOnly");
    case "TOPUPS_NOT_OPEN":
      return t("portal.topup.unavailable.topupsNotOpen");
    case "TOPUP_NOT_ALLOWED":
      return t("portal.topup.unavailable.topupNotAllowed");
    case "RECOVERY_EXCEEDS_MAXIMUM":
      return t("portal.topup.unavailable.recoveryExceedsMaximum");
    default:
      return t("portal.topup.unavailable.unknown");
  }
}

function AmountHint({ problem, options }: { problem: AmountProblem | null; options: TopupOptions }) {
  const { t } = useTranslation();
  switch (problem) {
    case "format":
      return t("portal.topup.amountHint.format");
    case "belowMinimum":
      return (
        <>
          {t("portal.topup.amountHint.belowMinimum")}{" "}
          <MoneyText amount={options.effective_minimum} currency={options.currency} />
        </>
      );
    case "aboveMaximum":
      return options.max_amount === null ? null : (
        <>
          {t("portal.topup.amountHint.aboveMaximum")}{" "}
          <MoneyText amount={options.max_amount} currency={options.currency} />
        </>
      );
    default:
      return null;
  }
}

function SubmitFailure({ error }: { error: Error }) {
  const { t } = useTranslation();
  return (
    <Space direction="vertical" size={4}>
      {error instanceof ApiError ? (
        <Typography.Text type="secondary">
          {t("portal.topup.errorCode")}
          {": "}
          <Typography.Text code>{error.code}</Typography.Text>
        </Typography.Text>
      ) : null}
      <RequestReference error={error} />
    </Space>
  );
}

function TopupForm({ options, onBelowRecoveryMinimum }: { options: TopupOptions; onBelowRecoveryMinimum: () => void }) {
  const { t } = useTranslation();
  const [input, setInput] = useState("");

  const amount = input.trim();
  const problem = amountProblem(amount, options);

  const submitting = useMutation({
    mutationFn: (value: string) => createTopup({ amount: value }),
    onSuccess: (created) => {
      // 只跳后端返回的地址，原样：不拼接、不读查询串里的地址。
      topupRedirect.assign(created.redirect_url);
    },
    onError: (error) => {
      if (error instanceof ApiError && error.code === TOPUP_BELOW_RECOVERY_MINIMUM) {
        onBelowRecoveryMinimum();
      }
    },
  });

  // 成功之后页面正在跳走：按钮保持不可点，免得再建一笔。
  const locked = submitting.isPending || submitting.isSuccess;

  const changeAmount = (value: string): void => {
    setInput(value);
    // 上一次失败说的是旧的金额，改金额时收起。只收失败：请求中 reset 会让按钮提前恢复可点。
    if (submitting.isError) {
      submitting.reset();
    }
  };

  const submit = (): void => {
    // 按钮在不合规与请求中已禁用；这里再挡一次，连点也只发一个请求。
    if (problem !== null || locked) {
      return;
    }
    submitting.mutate(amount);
  };

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      {options.recovery_minimum !== null ? (
        <Alert
          type="warning"
          showIcon
          message={t("portal.topup.recovery.title")}
          description={
            <>
              {t("portal.topup.recovery.body")}{" "}
              <MoneyText amount={options.effective_minimum} currency={options.currency} />
            </>
          }
        />
      ) : null}
      <Typography.Paragraph style={{ marginBottom: 0 }}>
        {t("portal.topup.minimum")}
        {": "}
        <MoneyText amount={options.effective_minimum} currency={options.currency} />
        {options.max_amount !== null ? (
          <>
            {" · "}
            {t("portal.topup.maximum")}
            {": "}
            <MoneyText amount={options.max_amount} currency={options.currency} />
          </>
        ) : null}
      </Typography.Paragraph>
      {/* role + aria-label：测试与部署后验收脚本按语义结构数推荐金额按钮。 */}
      <div role="group" aria-label={t("portal.topup.presets")}>
        <Space wrap>
          {options.preset_amounts.map((preset) => (
            <Button
              key={preset}
              htmlType="button"
              disabled={!withinLimits(preset, options) || locked}
              onClick={() => changeAmount(preset)}
            >
              <MoneyText amount={preset} currency={options.currency} />
            </Button>
          ))}
        </Space>
      </div>
      <Form layout="vertical" onFinish={submit}>
        <Form.Item
          label={t("portal.topup.amount", { currency: options.currency })}
          htmlFor="portal-topup-amount"
          validateStatus={problem !== null && problem !== "empty" ? "error" : ""}
          help={<AmountHint problem={problem} options={options} />}
        >
          <Input
            id="portal-topup-amount"
            inputMode="decimal"
            autoComplete="off"
            value={input}
            disabled={locked}
            onChange={(event) => changeAmount(event.target.value)}
          />
        </Form.Item>
        <Form.Item>
          <Button type="primary" htmlType="submit" loading={submitting.isPending} disabled={problem !== null || locked}>
            {t("portal.topup.submit")}
          </Button>
        </Form.Item>
      </Form>
      {submitting.error ? (
        <Alert
          type="error"
          showIcon
          message={t("portal.topup.submitFailed")}
          description={<SubmitFailure error={submitting.error} />}
        />
      ) : null}
    </Space>
  );
}

export function TopupPage() {
  const { t } = useTranslation();

  const options = useQuery({
    queryKey: CUSTOMER_TOPUP_OPTIONS_QUERY_KEY,
    queryFn: ({ signal }) => getTopupOptions(signal),
    // 同一个标签页换账号登录，拿到的必须是新客户的选项。
    gcTime: 0,
  });

  let body: ReactNode;
  if (options.isPending) {
    body = (
      <Space direction="vertical" aria-busy="true" style={{ width: "100%" }}>
        <Skeleton active title={false} paragraph={{ rows: 3 }} />
        <Typography.Text type="secondary">{t("portal.topup.loading")}</Typography.Text>
      </Space>
    );
  } else if (options.isError) {
    body = (
      <Alert
        type="error"
        showIcon
        message={t("portal.topup.loadFailed")}
        description={<RequestReference error={options.error} />}
        action={
          <Button size="small" onClick={() => void options.refetch()}>
            {t("portal.topup.retry")}
          </Button>
        }
      />
    );
  } else if (!options.data.available) {
    body = (
      <Alert
        type="info"
        showIcon
        message={t("portal.topup.unavailable.title")}
        description={<UnavailableText reason={options.data.unavailable_reason} />}
      />
    );
  } else {
    body = <TopupForm options={options.data} onBelowRecoveryMinimum={() => void options.refetch()} />;
  }

  return (
    // role + aria-label：部署后验收脚本按语义结构找到这一块（不依赖 antd 的类名）。
    <Card role="region" aria-label={t("portal.topup.title")} title={t("portal.topup.title")}>
      {body}
    </Card>
  );
}
