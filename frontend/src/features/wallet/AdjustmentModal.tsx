/**
 * 手工调账表单（`POST /api/v1/admin/customers/{customer_id}/wallet/adjustments`）。
 *
 * 语义以 docs/api.md「管理端手工调账」与 docs/design/AIH-TASK-010-admin-wallet-adjustment.md
 * 为准。三步：填表 → 确认（显示客户、类型、带符号的金额与原因）→ 发请求。确认之前不发任何请求。
 *
 * 管理员输入**不带符号**的金额，符号由类型决定（贷方为正、借方为负），拼成请求里的 `amount`
 * 字符串。全程字符串校验与拼接，不 `parseFloat`、不 `Number()`、不舍入（INV-10）。
 *
 * ⚠️ 幂等键的生命周期 —— 这是本组件最要紧的规则，改之前先读完：
 *
 *   1. **每次打开生成一个。**键在组件挂载时由 `crypto.randomUUID()` 生成（小写 uuid），整个
 *      组件存活期间不变。调用方只在打开时挂载、关闭时卸载本组件，所以「关闭后再打开」才是
 *      新键 —— 不要把本组件改成常驻挂载、靠 `open` 切换，那样键会跨两次打开复用。
 *   2. **同一次打开里的重试带同一个键。**若第一次其实已经记上，重试走重放（200、
 *      `replayed: true`），不会记第二笔。
 *   3. **结果未知就锁死。**一旦发出过请求而结果未知（网络错误、超时、5xx，见
 *      `isOutcomeUnknown`），类型、金额与原因锁定为只读，只剩「用同一个键重试」与关闭：
 *      - 不许改了金额再用同一个键发：若第一次已记上，后端回 409 `ADJUSTMENT_CONFLICT`；
 *      - 不许换新键重发：若第一次已记上，这就是第二笔，重复调账。
 *      锁一直保持到关闭为止（中途哪怕某次重试得到明确的 4xx 也不解锁）。要记一笔不同的，
 *      关掉表单、先看余额，再打开 —— 那时才是新键。
 *   4. 后端**明确拒绝**（404 / 409 / 422 等，什么都没写）时键从没用过：422、404 之后可以回去
 *      改了再发，仍用这个键。409 说明这个键已经记过别的东西，只能关闭。
 *
 * 成功（含重放）后让客户详情与客户列表过期重读：余额与计费状态以后端为准，不在前端推算。
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Descriptions,
  Form,
  Input,
  Modal,
  Radio,
  Space,
  Typography,
  type DescriptionsProps,
  type FormRule,
} from "antd";
import { useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import {
  CUSTOMER_LISTS_QUERY_KEY,
  customerDetailQueryKey,
  type CustomerDetail,
} from "../../api/adminCustomers";
import { ApiError } from "../../api/client";
import {
  ADJUSTMENT_CONFLICT,
  ADJUSTMENT_TYPES,
  amountProblem,
  isOutcomeUnknown,
  postAdjustment,
  signedAmount,
  type Adjustment,
  type AdjustmentBody,
  type AdjustmentType,
  type AmountProblem,
} from "../../api/walletAdjustments";
import { MoneyText } from "../../components/MoneyText";
import { RequestReference } from "../../components/RequestReference";
import { CustomerErrorAlert } from "../customers/CustomerForm";
import { BillingStatusTag } from "../customers/CustomerListPage";

/** 与后端 `app/schemas/wallet_adjustments.py` 的上限一致（去掉首尾空白之后）。 */
const REASON_MAX = 255;

interface AdjustmentFormValues {
  transaction_type?: AdjustmentType;
  amount: string;
  reason: string;
}

/** 确认之后要发的内容。键不在这里：它属于这一次打开，不属于某一份草稿。 */
type Draft = Omit<AdjustmentBody, "idempotency_key">;

/** 按码点数，与 Python 的 `len()` 一致（`.length` 数的是 UTF-16 单元，emoji 算 2）。 */
function trimmedLength(value: unknown): number {
  return typeof value === "string" ? [...value.trim()].length : 0;
}

function maxTrimmed(limit: number, message: string): FormRule {
  return {
    validator: (_rule, value: unknown) =>
      trimmedLength(value) > limit ? Promise.reject(new Error(message)) : Promise.resolve(),
  };
}

function amountRule(messages: Record<AmountProblem, string>): FormRule {
  return {
    validator: (_rule, value: unknown) => {
      const problem = amountProblem(value);
      return problem === null ? Promise.resolve() : Promise.reject(new Error(messages[problem]));
    },
  };
}

/** 类型的显示名。每个 key 都写成字面量，i18n 的 key 检查才找得到它们。 */
export function AdjustmentTypeLabel({ type }: { type: AdjustmentType }) {
  const { t } = useTranslation();
  switch (type) {
    case "ADJUSTMENT_CREDIT":
      return t("wallet.adjustment.type.adjustmentCredit");
    case "BONUS":
      return t("wallet.adjustment.type.bonus");
    case "ADJUSTMENT_DEBIT":
      return t("wallet.adjustment.type.adjustmentDebit");
    case "REFUND_ADJUSTMENT":
      return t("wallet.adjustment.type.refundAdjustment");
  }
}

export function AdjustmentModal({
  customer,
  onClose,
}: {
  customer: CustomerDetail;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [form] = Form.useForm<AdjustmentFormValues>();

  // 规则 1：挂载时生成一次，之后不变（见文件头注释）。`randomUUID` 本来就是小写，
  // 再 `toLowerCase()` 一次是因为后端只收小写，这一点不该依赖实现细节。
  const [idempotencyKey] = useState(() => crypto.randomUUID().toLowerCase());
  const [step, setStep] = useState<"edit" | "review">("edit");
  const [draft, setDraft] = useState<Draft | null>(null);
  // 规则 3：发出过请求而结果未知。只会从 false 变成 true，直到关闭。
  const [locked, setLocked] = useState(false);
  // 规则 4：409，这个键已经记过别的东西。
  const [conflict, setConflict] = useState(false);
  // 与建客户同一个理由：`isPending` 要等下一次渲染，双击时第二次点击可能赶在按钮禁用之前。
  const inFlight = useRef(false);

  const post = useMutation({
    mutationFn: (body: AdjustmentBody) => postAdjustment(customer.id, body),
    onSuccess: () => {
      // `exact`：只重读详情本身，不连带它下面的项目列表。
      void queryClient.invalidateQueries({
        queryKey: customerDetailQueryKey(customer.id),
        exact: true,
      });
      // 计费状态也显示在客户列表里。
      void queryClient.invalidateQueries({ queryKey: CUSTOMER_LISTS_QUERY_KEY });
    },
    onError: (error) => {
      if (isOutcomeUnknown(error)) {
        setLocked(true);
      } else if (error instanceof ApiError && error.code === ADJUSTMENT_CONFLICT) {
        setConflict(true);
      }
    },
    onSettled: () => {
      inFlight.current = false;
    },
  });

  const pending = post.isPending;
  const posted: Adjustment | undefined = post.data;
  const currency = customer.wallet.currency;

  const review = (values: AdjustmentFormValues): void => {
    const type = values.transaction_type;
    // 锁住之后表单已禁用，这里再挡一次：草稿一旦发过而结果未知，就不许再换。
    if (type === undefined || locked || conflict) {
      return;
    }
    post.reset();
    setDraft({
      transaction_type: type,
      amount: signedAmount(type, values.amount),
      reason: values.reason.trim(),
    });
    setStep("review");
  };

  const backToEdit = (): void => {
    if (locked || conflict || pending) {
      return;
    }
    post.reset();
    setStep("edit");
  };

  /** 首次提交与「用同一个键重试」走同一条路：同一份草稿、同一个键（规则 2）。 */
  const send = (): void => {
    if (inFlight.current || draft === null) {
      return;
    }
    inFlight.current = true;
    post.mutate({ ...draft, idempotency_key: idempotencyKey });
  };

  const close = (): void => {
    // 请求还在路上时不让关：关了就没人看得到结果，管理员只能去猜。
    if (pending) {
      return;
    }
    onClose();
  };

  let footer: ReactNode[];
  if (posted !== undefined) {
    footer = [
      <Button key="done" type="primary" onClick={close}>
        {t("wallet.adjustment.done")}
      </Button>,
    ];
  } else if (conflict) {
    footer = [
      <Button key="close" type="primary" onClick={close}>
        {t("wallet.adjustment.close")}
      </Button>,
    ];
  } else if (locked) {
    footer = [
      <Button key="close" onClick={close} disabled={pending}>
        {t("wallet.adjustment.close")}
      </Button>,
      <Button key="retry" type="primary" onClick={send} loading={pending} disabled={pending}>
        {t("wallet.adjustment.retry")}
      </Button>,
    ];
  } else if (step === "review") {
    footer = [
      <Button key="back" onClick={backToEdit} disabled={pending}>
        {t("wallet.adjustment.back")}
      </Button>,
      <Button key="confirm" type="primary" onClick={send} loading={pending} disabled={pending}>
        {t("wallet.adjustment.confirm")}
      </Button>,
    ];
  } else {
    footer = [
      <Button key="cancel" onClick={close}>
        {t("wallet.adjustment.cancel")}
      </Button>,
      <Button key="review" type="primary" onClick={() => form.submit()}>
        {t("wallet.adjustment.review")}
      </Button>,
    ];
  }

  let failure: ReactNode = null;
  if (locked) {
    failure = (
      <Alert
        type="warning"
        showIcon
        style={{ marginTop: 16 }}
        message={t("wallet.adjustment.unknownTitle")}
        description={
          <>
            <Typography.Paragraph>{t("wallet.adjustment.unknownBody")}</Typography.Paragraph>
            {post.error === null ? null : <RequestReference error={post.error} />}
          </>
        }
      />
    );
  } else if (conflict && post.error !== null) {
    failure = (
      <Alert
        type="error"
        showIcon
        style={{ marginTop: 16 }}
        message={t("wallet.adjustment.conflictTitle")}
        description={
          <>
            <Typography.Paragraph>{t("wallet.adjustment.conflictBody")}</Typography.Paragraph>
            <RequestReference error={post.error} />
          </>
        }
      />
    );
  } else if (post.error !== null) {
    failure = (
      <div style={{ marginTop: 16 }}>
        <CustomerErrorAlert error={post.error} title={t("wallet.adjustment.failed")} />
      </div>
    );
  }

  return (
    <Modal
      open
      title={t("wallet.adjustment.title")}
      width={640}
      footer={footer}
      onCancel={close}
      // 点到遮罩就关掉的话，结果未知时「用同一个键重试」这条路就这么丢了。
      maskClosable={false}
      closable={!pending}
      keyboard={!pending}
    >
      {posted === undefined ? (
        <>
          <Form<AdjustmentFormValues>
            form={form}
            layout="vertical"
            initialValues={{ amount: "", reason: "" }}
            onFinish={review}
            // 规则 3：确认中、发送中、结果未知、冲突时，类型、金额与原因都是只读的。
            disabled={step !== "edit" || locked || conflict || pending}
          >
            <Form.Item
              name="transaction_type"
              label={t("wallet.adjustment.field.type")}
              rules={[{ required: true, message: t("wallet.adjustment.typeRequired") }]}
            >
              <Radio.Group>
                <Space direction="vertical">
                  {ADJUSTMENT_TYPES.map((type) => (
                    <Radio key={type} value={type}>
                      <AdjustmentTypeLabel type={type} />
                    </Radio>
                  ))}
                </Space>
              </Radio.Group>
            </Form.Item>
            <Form.Item
              name="amount"
              label={t("wallet.adjustment.field.amount")}
              extra={t("wallet.adjustment.amountHint")}
              required
              rules={[
                amountRule({
                  required: t("wallet.adjustment.amountRequired"),
                  format: t("wallet.adjustment.amountInvalid"),
                  zero: t("wallet.adjustment.amountZero"),
                }),
              ]}
            >
              <Input inputMode="decimal" autoComplete="off" prefix={currency} />
            </Form.Item>
            <Form.Item
              name="reason"
              label={t("wallet.adjustment.field.reason")}
              extra={t("wallet.adjustment.reasonHint")}
              rules={[
                { required: true, whitespace: true, message: t("wallet.adjustment.reasonRequired") },
                maxTrimmed(REASON_MAX, t("wallet.adjustment.reasonTooLong")),
              ]}
            >
              <Input.TextArea rows={3} />
            </Form.Item>
          </Form>
          {step === "review" && draft !== null ? (
            <DraftSummary customer={customer} draft={draft} />
          ) : null}
          {failure}
        </>
      ) : (
        <AdjustmentResult adjustment={posted} currency={currency} />
      )}
    </Modal>
  );
}

/** 确认步骤：发出去的就是这里显示的这些，金额带着符号。 */
function DraftSummary({ customer, draft }: { customer: CustomerDetail; draft: Draft }) {
  const { t } = useTranslation();
  const items: DescriptionsProps["items"] = [
    {
      key: "customer",
      label: t("wallet.adjustment.field.customer"),
      children: (
        <Space direction="vertical" size={0}>
          <Typography.Text strong>{customer.company_name}</Typography.Text>
          <Typography.Text code>{customer.id}</Typography.Text>
        </Space>
      ),
    },
    {
      key: "transaction_type",
      label: t("wallet.adjustment.field.type"),
      children: <AdjustmentTypeLabel type={draft.transaction_type} />,
    },
    {
      key: "amount",
      label: t("wallet.adjustment.field.amount"),
      children: <MoneyText amount={draft.amount} currency={customer.wallet.currency} />,
    },
    {
      key: "reason",
      label: t("wallet.adjustment.field.reason"),
      children: <Typography.Text style={{ whiteSpace: "pre-wrap" }}>{draft.reason}</Typography.Text>,
    },
  ];
  return (
    <Descriptions
      bordered
      size="small"
      column={1}
      title={t("wallet.adjustment.confirmTitle")}
      items={items}
    />
  );
}

/** 201 与 200（重放）都走这里；重放要明确说「没有重复入账」。 */
function AdjustmentResult({ adjustment, currency }: { adjustment: Adjustment; currency: string }) {
  const { t } = useTranslation();
  const items: DescriptionsProps["items"] = [
    {
      key: "amount",
      label: t("wallet.adjustment.field.amount"),
      children: <MoneyText amount={adjustment.amount} currency={currency} />,
    },
    {
      key: "balance_after",
      label: t("wallet.adjustment.field.balanceAfter"),
      children: <MoneyText amount={adjustment.balance_after} currency={currency} />,
    },
    {
      key: "billing_status",
      label: t("customers.field.billingStatus"),
      children: <BillingStatusTag status={adjustment.billing_status} />,
    },
  ];
  return (
    <>
      <Alert
        type={adjustment.replayed ? "info" : "success"}
        showIcon
        style={{ marginBottom: 16 }}
        message={
          adjustment.replayed ? t("wallet.adjustment.replayed") : t("wallet.adjustment.posted")
        }
        description={adjustment.replayed ? t("wallet.adjustment.replayedNote") : undefined}
      />
      <Descriptions bordered size="small" column={1} items={items} />
    </>
  );
}
