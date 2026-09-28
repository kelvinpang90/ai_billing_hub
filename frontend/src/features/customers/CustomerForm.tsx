/**
 * 客户资料表单：建客户与编辑客户共用（docs/api.md「管理端客户管理」）。
 *
 * 前端校验只是**提前告诉用户**，规则照抄 docs/api.md；后端的 422 才是准绳，
 * 它的 message 原样显示。长度按「去掉首尾空白之后的字符数」算，与后端一致。
 *
 * ⚠️ 表单里没有、也不许加 `billing_status`、余额、`account_status`、低余额阈值：
 * 后端把请求体里的这些字段一律判 422，计费状态只随余额变化。
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Form, Input, Space, Typography, type FormRule } from "antd";
import { useRef } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router";

import {
  ADMIN_REQUIRED,
  CUSTOMER_LISTS_QUERY_KEY,
  createCustomer,
  customerDetailQueryKey,
  type CreateCustomerBody,
  type CustomerPatch,
  type CustomerSummary,
} from "../../api/adminCustomers";
import { ApiError } from "../../api/client";
import { RequestReference } from "../../components/RequestReference";
import { ROUTES, customerDetailPath } from "../../routes/paths";

/** 与后端 `app/schemas/customers.py` 的上限一致。 */
const NAME_MAX = 255;
const EMAIL_MAX = 320;
const PHONE_MAX = 32;

export interface CustomerFormValues {
  company_name: string;
  email: string;
  contact_name: string;
  phone: string;
}

export const EMPTY_CUSTOMER_FORM: CustomerFormValues = {
  company_name: "",
  email: "",
  contact_name: "",
  phone: "",
};

/** 编辑时的初值。`null` 显示成空框；提交时空框再变回 `null`。 */
export function formValuesFrom(customer: CustomerSummary): CustomerFormValues {
  return {
    company_name: customer.company_name,
    email: customer.email,
    contact_name: customer.contact_name ?? "",
    phone: customer.phone ?? "",
  };
}

/** 可选字段：去掉首尾空白，剩空串就是「没有」—— 与后端存 `null` 的规则一致。 */
function optional(value: string | undefined): string | null {
  const trimmed = (value ?? "").trim();
  return trimmed === "" ? null : trimmed;
}

/** 建客户的请求体。空的可选字段不带。 */
export function toCreateBody(values: CustomerFormValues): CreateCustomerBody {
  const body: CreateCustomerBody = {
    company_name: values.company_name.trim(),
    email: values.email.trim(),
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
 * 编辑客户的 PATCH：**只带实际改了的字段**。
 *
 * 比较的是规范化之后的值（去首尾空白、空串当 `null`），所以只在末尾多敲了个空格
 * 不算改动。清空联系人 / 电话发 `null`。返回空对象表示没有改动 —— 调用方不该发请求
 * （后端对 `{}` 回 422）。
 */
export function toPatch(original: CustomerSummary, values: CustomerFormValues): CustomerPatch {
  const patch: CustomerPatch = {};
  const companyName = values.company_name.trim();
  if (companyName !== original.company_name) {
    patch.company_name = companyName;
  }
  const email = values.email.trim();
  if (email !== original.email) {
    patch.email = email;
  }
  const contactName = optional(values.contact_name);
  if (contactName !== original.contact_name) {
    patch.contact_name = contactName;
  }
  const phone = optional(values.phone);
  if (phone !== original.phone) {
    patch.phone = phone;
  }
  return patch;
}

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

/**
 * 客户接口出错时的提示。403 单独说「没有权限」—— 前端不做角色判断，
 * 是不是管理员只看后端这一句。其余一律显示后端 message 与 request_id。
 */
export function CustomerErrorAlert({
  error,
  title,
  onRetry,
}: {
  error: Error | null;
  title: string;
  onRetry?: (() => void) | undefined;
}) {
  const { t } = useTranslation();
  if (error === null) {
    return null;
  }
  const forbidden = error instanceof ApiError && error.code === ADMIN_REQUIRED;
  return (
    <Alert
      type={forbidden ? "warning" : "error"}
      showIcon
      style={{ marginBottom: 16 }}
      message={forbidden ? t("customers.forbidden") : title}
      description={<RequestReference error={error} />}
      action={
        onRetry === undefined ? undefined : (
          <Button size="small" onClick={onRetry}>
            {t("customers.retry")}
          </Button>
        )
      }
    />
  );
}

export function CustomerForm({
  initialValues,
  submitLabel,
  submitting,
  error,
  onSubmit,
  onCancel,
}: {
  initialValues: CustomerFormValues;
  submitLabel: string;
  submitting: boolean;
  error: Error | null;
  onSubmit: (values: CustomerFormValues) => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();

  return (
    <Form<CustomerFormValues>
      layout="vertical"
      initialValues={initialValues}
      onFinish={onSubmit}
      disabled={submitting}
    >
      <CustomerErrorAlert error={error} title={t("customers.form.saveFailed")} />
      <Form.Item
        name="company_name"
        label={t("customers.field.companyName")}
        rules={[
          { required: true, whitespace: true, message: t("customers.form.companyNameRequired") },
          maxTrimmed(NAME_MAX, t("customers.form.companyNameTooLong")),
        ]}
      >
        <Input autoComplete="organization" />
      </Form.Item>
      <Form.Item
        name="email"
        label={t("customers.field.email")}
        rules={[
          { required: true, whitespace: true, message: t("customers.form.emailRequired") },
          { type: "email", message: t("customers.form.emailInvalid") },
          maxTrimmed(EMAIL_MAX, t("customers.form.emailTooLong")),
        ]}
      >
        <Input autoComplete="email" inputMode="email" />
      </Form.Item>
      <Form.Item
        name="contact_name"
        label={t("customers.field.contactName")}
        rules={[maxTrimmed(NAME_MAX, t("customers.form.contactNameTooLong"))]}
      >
        <Input autoComplete="name" />
      </Form.Item>
      <Form.Item
        name="phone"
        label={t("customers.field.phone")}
        rules={[maxTrimmed(PHONE_MAX, t("customers.form.phoneTooLong"))]}
      >
        <Input autoComplete="tel" inputMode="tel" />
      </Form.Item>
      <Space>
        {/* ⚠️ 建客户**不幂等**：提交进行中必须禁用，否则双击就是两个客户。
            `disabled` 显式写上，不只靠 `loading` 的视觉效果。 */}
        <Button type="primary" htmlType="submit" loading={submitting} disabled={submitting}>
          {submitLabel}
        </Button>
        <Button onClick={onCancel}>{t("customers.form.cancel")}</Button>
      </Space>
    </Form>
  );
}

/** 建客户页。成功后直接进入这个客户的详情。 */
export function CustomerCreatePage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  // ⚠️ `isPending` 要等下一次渲染才变 true，而 antd 的校验是异步的：双击时两次
  // onFinish 可能都赶在按钮禁用之前。这个 ref 在同一个事件循环里就挡住第二次。
  const inFlight = useRef(false);

  const create = useMutation({
    mutationFn: (body: CreateCustomerBody) => createCustomer(body),
    onSuccess: (customer) => {
      // 详情页直接用这份响应，不必再读一次。
      queryClient.setQueryData(customerDetailQueryKey(customer.id), customer);
      void queryClient.invalidateQueries({ queryKey: CUSTOMER_LISTS_QUERY_KEY });
      void navigate(customerDetailPath(customer.id));
    },
    onSettled: () => {
      inFlight.current = false;
    },
  });

  const submit = (values: CustomerFormValues): void => {
    if (inFlight.current) {
      return;
    }
    inFlight.current = true;
    create.mutate(toCreateBody(values));
  };

  return (
    <Card title={t("customers.create.title")} style={{ maxWidth: 640 }}>
      <Typography.Paragraph type="secondary">{t("customers.create.intro")}</Typography.Paragraph>
      <CustomerForm
        initialValues={EMPTY_CUSTOMER_FORM}
        submitLabel={t("customers.create.submit")}
        submitting={create.isPending}
        error={create.error}
        onSubmit={submit}
        onCancel={() => void navigate(ROUTES.customers)}
      />
    </Card>
  );
}
