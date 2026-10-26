/**
 * 客户详情页的低余额阈值面板（AIH-TASK-111）。
 *
 * 语义以 docs/api.md「管理端低余额阈值」为准，显示规则以设计闸门 #433 v2 §2「金额」「前端」为准：
 *
 * - 读 `GET …/low-balance-threshold` 显示当前值：按 2 位显示（`formatAmountRounded`，字符串 `ROUND_HALF_UP`），
 *   `null` 显示「未设置」。
 * - 修改：字符串输入，客户端按后端同一条正则与「大于 0」先校验，不合格不发请求；金额从头到尾是字符串，
 *   不转成 number（INV-10）。最终以后端的 422 为准。
 * - 清除：二次确认后提交 `null`（不提醒）。
 * - 成功后让面板的 GET 过期重读，不用 PUT 的响应拼显示；失败显示后端 message 与 request_id。
 *
 * 面板的查询与详情页互不相干：加载中、加载失败都只画在面板里，详情页其余区块照常显示。
 * 这里没有成本、毛利或任何内部字段，也不请求它们。
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Form,
  Input,
  Modal,
  Skeleton,
  Space,
  Typography,
  type DescriptionsProps,
} from "antd";
import { useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import {
  getLowBalanceThreshold,
  lowBalanceThresholdQueryKey,
  setLowBalanceThreshold,
} from "../../api/adminCustomers";
import { formatAmountRounded } from "../../components/MoneyText";
import { CustomerErrorAlert } from "./CustomerForm";

/** docs/api.md「管理端低余额阈值」的正则：用 `[0-9]` 不用 `\d`，与后端一致；整串匹配，前后空白也不合格。 */
const THRESHOLD_PATTERN = /^[0-9]{1,12}(\.[0-9]{1,2})?$/;
/** 只剩数字与小数点之后，有一位不是 0 就大于 0 —— 不必转成数。 */
const NON_ZERO_DIGIT = /[1-9]/;
/** 设计 §2「金额」：管理端按 2 位显示。 */
const DISPLAY_PLACES = 2;

type Done = "saved" | "cleared";

/** 阈值输入的客户端校验，规则同后端；合格时为 `null`。 */
export function thresholdProblem(value: unknown): "required" | "invalid" | "zero" | null {
  if (typeof value !== "string" || value === "") {
    return "required";
  }
  if (!THRESHOLD_PATTERN.test(value)) {
    return "invalid";
  }
  return NON_ZERO_DIGIT.test(value) ? null : "zero";
}

interface ThresholdFormValues {
  threshold: string;
}

export function LowBalanceThresholdPanel({ customerId }: { customerId: string }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [confirmingClear, setConfirmingClear] = useState(false);
  const [done, setDone] = useState<Done | null>(null);
  // 与建客户同一个理由：`isPending` 要等下一次渲染，双击时第二次提交可能赶在按钮禁用之前。
  const inFlight = useRef(false);

  const threshold = useQuery({
    queryKey: lowBalanceThresholdQueryKey(customerId),
    queryFn: ({ signal }) => getLowBalanceThreshold(customerId, signal),
  });

  const save = useMutation({
    mutationFn: (value: string | null) => setLowBalanceThreshold(customerId, value),
    onSuccess: (_data, value) => {
      setEditing(false);
      setConfirmingClear(false);
      setDone(value === null ? "cleared" : "saved");
      // 以后端为准：重读一次，不用 PUT 的响应。
      void queryClient.invalidateQueries({ queryKey: lowBalanceThresholdQueryKey(customerId) });
    },
    onSettled: () => {
      inFlight.current = false;
    },
  });

  const submit = (value: string | null): void => {
    if (inFlight.current) {
      return;
    }
    inFlight.current = true;
    setDone(null);
    save.mutate(value);
  };

  const startEditing = (): void => {
    save.reset();
    setDone(null);
    setEditing(true);
  };

  const startClear = (): void => {
    save.reset();
    setDone(null);
    setConfirmingClear(true);
  };

  const busy = save.isPending;
  const closeClear = (): void => {
    if (!busy) {
      save.reset();
      setConfirmingClear(false);
    }
  };

  let body: ReactNode;
  let extra: ReactNode = null;
  if (threshold.isPending) {
    body = (
      <>
        <Skeleton active title={false} paragraph={{ rows: 1 }} />
        <Typography.Text type="secondary">{t("lowBalance.loading")}</Typography.Text>
      </>
    );
  } else if (threshold.isError) {
    body = (
      <CustomerErrorAlert
        error={threshold.error}
        title={t("lowBalance.loadFailed")}
        onRetry={() => void threshold.refetch()}
      />
    );
  } else {
    const current = threshold.data.low_balance_threshold;
    const items: DescriptionsProps["items"] = [
      {
        key: "low_balance_threshold",
        label: t("lowBalance.field.threshold"),
        children:
          current === null ? (
            t("lowBalance.notSet")
          ) : (
            <span style={{ fontVariantNumeric: "tabular-nums" }}>
              {formatAmountRounded(current, DISPLAY_PLACES)}
            </span>
          ),
      },
    ];
    body = <Descriptions bordered size="small" column={1} items={items} />;
    extra = editing ? null : (
      <Space>
        <Button disabled={busy} onClick={startEditing}>
          {t("lowBalance.change")}
        </Button>
        <Button danger disabled={busy || current === null} onClick={startClear}>
          {t("lowBalance.clear.open")}
        </Button>
      </Space>
    );
  }

  let doneMessage: string | null = null;
  if (done === "saved") {
    doneMessage = t("lowBalance.saved");
  } else if (done === "cleared") {
    doneMessage = t("lowBalance.cleared");
  }

  return (
    // role + aria-label：部署后验收脚本按语义结构找到这个面板（不依赖 antd 的类名）。
    <Card
      role="region"
      aria-label={t("lowBalance.title")}
      title={t("lowBalance.title")}
      extra={extra}
    >
      <Typography.Paragraph type="secondary">{t("lowBalance.intro")}</Typography.Paragraph>
      {doneMessage === null ? null : (
        <Alert type="success" showIcon style={{ marginBottom: 16 }} message={doneMessage} />
      )}
      {confirmingClear ? null : (
        <CustomerErrorAlert error={save.error} title={t("lowBalance.saveFailed")} />
      )}
      {body}
      {editing ? (
        <Form<ThresholdFormValues>
          layout="vertical"
          initialValues={{ threshold: "" }}
          onFinish={(values) => submit(values.threshold)}
          disabled={busy}
          style={{ marginTop: 16 }}
        >
          <Form.Item
            name="threshold"
            label={t("lowBalance.form.threshold")}
            extra={t("lowBalance.form.hint")}
            rules={[
              {
                // required 只为星号；有 validator 时由它一并判空。
                required: true,
                validator: (_rule, value: unknown) => {
                  const problem = thresholdProblem(value);
                  if (problem === null) {
                    return Promise.resolve();
                  }
                  let message: string;
                  if (problem === "required") {
                    message = t("lowBalance.form.required");
                  } else if (problem === "zero") {
                    message = t("lowBalance.form.zero");
                  } else {
                    message = t("lowBalance.form.invalid");
                  }
                  return Promise.reject(new Error(message));
                },
              },
            ]}
          >
            <Input autoComplete="off" inputMode="decimal" />
          </Form.Item>
          <Space>
            <Button type="primary" htmlType="submit" loading={busy} disabled={busy}>
              {t("lowBalance.form.save")}
            </Button>
            <Button disabled={busy} onClick={() => setEditing(false)}>
              {t("lowBalance.cancel")}
            </Button>
          </Space>
        </Form>
      ) : null}
      {/* 只在打开时挂载、关闭即卸载。 */}
      {confirmingClear ? (
        <Modal
          open
          title={t("lowBalance.clear.confirmTitle")}
          okText={t("lowBalance.clear.confirm")}
          cancelText={t("lowBalance.cancel")}
          onOk={() => submit(null)}
          onCancel={closeClear}
          okButtonProps={{ danger: true, loading: busy, disabled: busy }}
          cancelButtonProps={{ disabled: busy }}
          maskClosable={false}
          closable={!busy}
          keyboard={!busy}
        >
          <CustomerErrorAlert error={save.error} title={t("lowBalance.saveFailed")} />
          <Typography.Paragraph>{t("lowBalance.clear.warning")}</Typography.Paragraph>
        </Modal>
      ) : null}
    </Card>
  );
}
