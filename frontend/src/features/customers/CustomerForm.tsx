/**
 * 客户资料表单（建客户与编辑客户共用）与建客户页。
 *
 * 字段规则照 docs/api.md「建客户」「编辑客户」：
 *   - company_name 必填，去首尾空白后 1–255
 *   - email 必填且合法，≤ 320
 *   - contact_name ≤ 255、phone ≤ 32，可选；空白等于没填
 *
 * 前端校验只是省一次往返，**后端才是准绳**：它回 422 时照实显示它的 message。
 *
 * ⚠️ 长度按**码点**数，不按 `String.length`：后端（Python）数的是码点，而 JS 的
 * `length` 把 emoji 这类字符算成 2，会把后端能收的名字在前端拦下来。
 */

import { useQueryClient } from "@tanstack/react-query";
import { Button, Card, Form, Input, Space, Typography } from "antd";
import { useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate } from "react-router";

import {
  CUSTOMER_LIST_QUERY_KEY,
  createCustomer,
  customerDetailQueryKey,
  type CreateCustomerBody,
  type CustomerPatch,
  type CustomerSummary,
} from "../../api/adminCustomers";
import { ApiError } from "../../api/client";
import { ROUTES, customerDetailPath } from "../../routes/paths";
import { CustomerErrorAlert } from "./CustomerListPage";

/** 与后端 `app/schemas/customers.py` 的上限一致。 */
const COMPANY_NAME_MAX = 255;
const EMAIL_MAX = 320;
const CONTACT_NAME_MAX = 255;
const PHONE_MAX = 32;

/** antd 交回来的表单值。没碰过的字段可能是 `undefined`。 */
export interface CustomerFormValues {
  company_name?: string;
  email?: string;
  contact_name?: string;
  phone?: string;
}

export const EMPTY_CUSTOMER_FORM: CustomerFormValues = {
  company_name: "",
  email: "",
  contact_name: "",
  phone: "",
};

function trimmed(value: string | undefined): string {
  return (value ?? "").trim();
}

/** 可选字段：去首尾空白后是空串就当 `null`，与后端的存法一致。 */
function optional(value: string | undefined): string | null {
  const text = trimmed(value);
  return text === "" ? null : text;
}

function codePoints(value: unknown): number {
  return typeof value === "string" ? [...value.trim()].length : 0;
}

/** 建客户的请求体。可选字段没填就不带，由后端存成 `null`。 */
export function toCreateBody(values: CustomerFormValues): CreateCustomerBody {
  const body: CreateCustomerBody = {
    company_name: trimmed(values.company_name),
    email: trimmed(values.email),
  };
  const contactName = optional(values.contact_name);
  if (contactName !== null) {
    body.contact_name = contactName;
  }
  const phone = optional(values.phone);
  if (phone !== null) {
    body.phone = phone;
  }
  return body;
}

/**
 * 编辑客户的请求体：**只放实际改了的字段**。一个都没改时返回空对象，调用方据此不发请求
 * （后端对 `{}` 回 422）。
 *
 * `contact_name` / `phone` 清空时发 `null`；`company_name` / `email` 从不发 `null`
 * （表单校验保证它们非空）。
 */
export function toUpdatePatch(current: CustomerSummary, values: CustomerFormValues): CustomerPatch {
  const patch: CustomerPatch = {};
  const companyName = trimmed(values.company_name);
  if (companyName !== current.company_name) {
    patch.company_name = companyName;
  }
  const email = trimmed(values.email);
  if (email !== current.email) {
    patch.email = email;
  }
  const contactName = optional(values.contact_name);
  if (contactName !== current.contact_name) {
    patch.contact_name = contactName;
  }
  const phone = optional(values.phone);
  if (phone !== current.phone) {
    patch.phone = phone;
  }
  return patch;
}

export function CustomerForm({
  initialValues,
  busy,
  submitLabel,
  onSubmit,
  onCancel,
}: {
  initialValues: CustomerFormValues;
  busy: boolean;
  submitLabel: string;
  onSubmit: (values: CustomerFormValues) => void;
  onCancel?: () => void;
}) {
  const { t } = useTranslation();

  const atMost = (limit: number, message: string) => ({
    validator: (_rule: unknown, value: unknown) =>
      codePoints(value) > limit ? Promise.reject(new Error(message)) : Promise.resolve(),
  });

  return (
    // ⚠️ `disabled={busy}` 连同提交按钮一起禁用：建客户不幂等，双击就是两个客户。
    <Form<CustomerFormValues>
      layout="vertical"
      initialValues={initialValues}
      onFinish={onSubmit}
      disabled={busy}
    >
      {/* 显式给每条提示：antd 默认的「Please enter ${label}」是英文硬编码，
          不走 i18n（见 LoginPage 里 enrol.codeRequired 的教训）。 */}
      <Form.Item
        name="company_name"
        label={t("customers.field.companyName")}
        rules={[
          { required: true, whitespace: true, message: t("customers.form.companyNameRequired") },
          atMost(COMPANY_NAME_MAX, t("customers.form.companyNameTooLong")),
        ]}
      >
        <Input autoComplete="off" />
      </Form.Item>
      <Form.Item
        name="email"
        label={t("customers.field.email")}
        rules={[
          { required: true, whitespace: true, message: t("customers.form.emailRequired") },
          {
            type: "email",
            transform: (value: unknown) => (typeof value === "string" ? value.trim() : value),
            message: t("customers.form.emailInvalid"),
          },
          atMost(EMAIL_MAX, t("customers.form.emailTooLong")),
        ]}
      >
        {/* 这是客户的联系邮箱，不是登录账号：别让浏览器把管理员自己的邮箱填进来。 */}
        <Input autoComplete="off" inputMode="email" />
      </Form.Item>
      <Form.Item
        name="contact_name"
        label={t("customers.field.contactName")}
        rules={[atMost(CONTACT_NAME_MAX, t("customers.form.contactNameTooLong"))]}
      >
        <Input autoComplete="off" />
      </Form.Item>
      <Form.Item
        name="phone"
        label={t("customers.field.phone")}
        rules={[atMost(PHONE_MAX, t("customers.form.phoneTooLong"))]}
      >
        <Input autoComplete="off" inputMode="tel" />
      </Form.Item>
      <Space>
        <Button type="primary" htmlType="submit" loading={busy} disabled={busy}>
          {submitLabel}
        </Button>
        {onCancel === undefined ? null : (
          <Button onClick={onCancel}>{t("customers.edit.cancel")}</Button>
        )}
      </Space>
    </Form>
  );
}

/**
 * 建客户页。成功后进入该客户的详情。
 *
 * ⚠️ 接口**不幂等**（docs/api.md）：同一请求体提交两次得到两个客户、两个钱包。
 * 提交进行中禁用整张表单；按钮禁用之前那一瞬的第二次提交由 `inFlight` 挡住 ——
 * state 要等下一次渲染才生效，ref 是同步的。
 */
export function CustomerCreatePage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const inFlight = useRef(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);

  const submit = (values: CustomerFormValues): void => {
    if (inFlight.current) {
      return;
    }
    inFlight.current = true;
    setBusy(true);
    setError(null);
    void (async () => {
      try {
        const created = await createCustomer(toCreateBody(values));
        // 建好的详情直接放进缓存，详情页不用再读一次；列表多了一行，作废重读。
        queryClient.setQueryData(customerDetailQueryKey(created.id), created);
        void queryClient.invalidateQueries({ queryKey: CUSTOMER_LIST_QUERY_KEY });
        // ⚠️ 成功之后**不**解除禁用：离开这一页之前，表单不该再能提交一次。
        void navigate(customerDetailPath(created.id));
      } catch (caught) {
        inFlight.current = false;
        setBusy(false);
        if (!(caught instanceof ApiError)) {
          throw caught;
        }
        setError(caught);
      }
    })();
  };

  return (
    <Card
      title={t("customers.create.title")}
      extra={<Link to={ROUTES.customers}>{t("customers.detail.backToList")}</Link>}
      style={{ maxWidth: 640 }}
    >
      <Typography.Paragraph type="secondary">{t("customers.create.intro")}</Typography.Paragraph>
      {error === null ? null : (
        <CustomerErrorAlert error={error} title={t("customers.create.failed")} />
      )}
      <CustomerForm
        initialValues={EMPTY_CUSTOMER_FORM}
        busy={busy}
        submitLabel={t("customers.create.submit")}
        onSubmit={submit}
      />
    </Card>
  );
}
