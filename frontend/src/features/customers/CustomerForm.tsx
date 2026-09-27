/**
 * 客户表单（建客户与编辑共用），建客户页，以及客户这一块几个页面共用的小件。
 *
 * 字段规则照 docs/api.md「建客户」「编辑客户」写：公司名去首尾空白后 1–255；邮箱
 * 必填、合法、不超过 320；联系人不超过 255、电话不超过 32，可空。前端校验只是为了
 * 少打一个来回，**后端说了算**：它回 422 时照原样显示后端的 message。
 *
 * ⚠️ 长度按 **Unicode 码点**数，不按 JS 的 `length`（UTF-16 码元）。后端是 Python，
 * `len()` 数的是码点；按码元数的话，一个带 emoji 的合法公司名会在前端被拦下。
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Form, Input, Space, Tag, Typography } from "antd";
import { useRef, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate } from "react-router";

import {
  ADMIN_REQUIRED,
  CUSTOMER_LIST_QUERY_KEY,
  CUSTOMER_NOT_FOUND,
  createCustomer,
  customerQueryKey,
  type CreateCustomerBody,
  type CustomerSummary,
  type UpdateCustomerPatch,
} from "../../api/adminCustomers";
import { ApiError } from "../../api/client";
import { RequestReference } from "../../components/RequestReference";
import { ROUTES, customerDetailPath } from "../../routes/paths";

const COMPANY_NAME_MAX = 255;
const EMAIL_MAX = 320;
const CONTACT_NAME_MAX = 255;
const PHONE_MAX = 32;

/** 只挡明显打错的；「合法邮箱」的最终判定在后端（`EmailStr`）。 */
const EMAIL_SHAPE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

/** 表单里的原始值：antd 的输入框给的都是字符串，空着就是 `""`。 */
export interface CustomerFormValues {
  company_name: string;
  email: string;
  contact_name: string;
  phone: string;
}

/** 去掉首尾空白之后、准备发出去的值。可选字段空着就是 `null`（与后端的存法一致）。 */
interface NormalisedCustomer {
  company_name: string;
  email: string;
  contact_name: string | null;
  phone: string | null;
}

function codePoints(value: string): number {
  return [...value].length;
}

function normalise(values: Partial<CustomerFormValues>): NormalisedCustomer {
  const optional = (value: string | undefined): string | null => value?.trim() || null;
  return {
    company_name: (values.company_name ?? "").trim(),
    email: (values.email ?? "").trim(),
    contact_name: optional(values.contact_name),
    phone: optional(values.phone),
  };
}

/** 建客户的请求体。空着的可选字段**不带**，让后端按默认值（`null`）处理。 */
export function toCreateBody(values: CustomerFormValues): CreateCustomerBody {
  const next = normalise(values);
  const body: CreateCustomerBody = { company_name: next.company_name, email: next.email };
  if (next.contact_name !== null) {
    body.contact_name = next.contact_name;
  }
  if (next.phone !== null) {
    body.phone = next.phone;
  }
  return body;
}

/**
 * 编辑时的 PATCH 请求体：**只带实际改了的字段**。
 *
 * 联系人、电话被清空时发 `null`（后端把它当清空）。返回空对象表示什么都没改 ——
 * 调用方据此不发请求：后端对 `{}` 回 422。
 */
export function customerPatch(
  original: CustomerSummary,
  values: CustomerFormValues,
): UpdateCustomerPatch {
  const next = normalise(values);
  const patch: UpdateCustomerPatch = {};
  if (next.company_name !== original.company_name) {
    patch.company_name = next.company_name;
  }
  if (next.email !== original.email) {
    patch.email = next.email;
  }
  if (next.contact_name !== original.contact_name) {
    patch.contact_name = next.contact_name;
  }
  if (next.phone !== original.phone) {
    patch.phone = next.phone;
  }
  return patch;
}

/** 把一个现有客户摊成表单初值。 */
export function formValuesOf(customer: CustomerSummary): CustomerFormValues {
  return {
    company_name: customer.company_name,
    email: customer.email,
    contact_name: customer.contact_name ?? "",
    phone: customer.phone ?? "",
  };
}

const EMPTY_FORM: CustomerFormValues = { company_name: "", email: "", contact_name: "", phone: "" };

function verdict(problem: string | null): Promise<void> {
  return problem === null ? Promise.resolve() : Promise.reject(new Error(problem));
}

interface CustomerFormProps {
  initialValues: CustomerFormValues;
  submitLabel: string;
  busy: boolean;
  onSubmit: (values: CustomerFormValues) => void;
  onCancel: () => void;
}

export function CustomerForm({
  initialValues,
  submitLabel,
  busy,
  onSubmit,
  onCancel,
}: CustomerFormProps) {
  const { t } = useTranslation();

  return (
    <Form<CustomerFormValues>
      layout="vertical"
      initialValues={initialValues}
      onFinish={onSubmit}
      disabled={busy}
    >
      <Form.Item
        name="company_name"
        label={t("customers.field.companyName")}
        required
        rules={[
          {
            validator: (_rule, value: string | undefined) => {
              const name = (value ?? "").trim();
              if (name === "") {
                return verdict(t("customers.validation.companyNameRequired"));
              }
              return verdict(
                codePoints(name) > COMPANY_NAME_MAX
                  ? t("customers.validation.companyNameTooLong")
                  : null,
              );
            },
          },
        ]}
      >
        <Input autoComplete="organization" />
      </Form.Item>
      <Form.Item
        name="email"
        label={t("customers.field.email")}
        required
        rules={[
          {
            validator: (_rule, value: string | undefined) => {
              const email = (value ?? "").trim();
              if (email === "") {
                return verdict(t("customers.validation.emailRequired"));
              }
              if (codePoints(email) > EMAIL_MAX) {
                return verdict(t("customers.validation.emailTooLong"));
              }
              return verdict(
                EMAIL_SHAPE.test(email) ? null : t("customers.validation.emailInvalid"),
              );
            },
          },
        ]}
      >
        <Input autoComplete="off" inputMode="email" />
      </Form.Item>
      <Form.Item
        name="contact_name"
        label={t("customers.field.contactName")}
        rules={[
          {
            validator: (_rule, value: string | undefined) =>
              verdict(
                codePoints((value ?? "").trim()) > CONTACT_NAME_MAX
                  ? t("customers.validation.contactNameTooLong")
                  : null,
              ),
          },
        ]}
      >
        <Input autoComplete="off" />
      </Form.Item>
      <Form.Item
        name="phone"
        label={t("customers.field.phone")}
        rules={[
          {
            validator: (_rule, value: string | undefined) =>
              verdict(
                codePoints((value ?? "").trim()) > PHONE_MAX
                  ? t("customers.validation.phoneTooLong")
                  : null,
              ),
          },
        ]}
      >
        <Input autoComplete="off" inputMode="tel" />
      </Form.Item>
      <Space>
        {/* ⚠️ `disabled` 与 `loading` 都要：提交进行中这个按钮必须真的点不动。 */}
        <Button type="primary" htmlType="submit" loading={busy} disabled={busy}>
          {submitLabel}
        </Button>
        <Button onClick={onCancel} disabled={busy}>
          {t("customers.form.cancel")}
        </Button>
      </Space>
    </Form>
  );
}

/**
 * 建客户页。
 *
 * ⚠️ 这个接口**不幂等**：同一请求体提交两次就是两个客户、两个钱包。按钮在请求
 * 进行中禁用，但那还不够 —— antd 的 `onFinish` 在**异步校验之后**才调，双击的
 * 两次点击可能都在按钮变灰之前通过校验。所以另有一个同步的 ref 挡第二次。
 * 成功后 `replace` 跳到详情：按「后退」不该回到一张填好了、一点就再建一个的表单。
 */
export function CustomerCreatePage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const inFlight = useRef(false);
  const create = useMutation({ mutationFn: createCustomer });

  const submit = (values: CustomerFormValues): void => {
    if (inFlight.current) {
      return;
    }
    inFlight.current = true;
    create.mutate(toCreateBody(values), {
      onSuccess: (customer) => {
        queryClient.setQueryData(customerQueryKey(customer.id), customer);
        void queryClient.invalidateQueries({ queryKey: CUSTOMER_LIST_QUERY_KEY });
        void navigate(customerDetailPath(customer.id), { replace: true });
      },
      onSettled: () => {
        inFlight.current = false;
      },
    });
  };

  return (
    <Card
      title={t("customers.create.title")}
      extra={<Link to={ROUTES.customers}>{t("customers.detail.backToList")}</Link>}
      style={{ maxWidth: 640 }}
    >
      <CustomerErrorAlert error={create.error} title={t("customers.create.failed")} />
      <Typography.Paragraph type="secondary">{t("customers.create.hint")}</Typography.Paragraph>
      <CustomerForm
        initialValues={EMPTY_FORM}
        submitLabel={t("customers.create.submit")}
        busy={create.isPending}
        onSubmit={submit}
        onCancel={() => void navigate(ROUTES.customers)}
      />
    </Card>
  );
}

/**
 * 客户页面上显示后端错误的统一方式：后端的 message 与 request_id（`RequestReference`）。
 *
 * 403 `ADMIN_REQUIRED` 单独说成「没有权限」。⚠️ 前端**不做**角色判断（spec §51，
 * 见 AppLayout 的注释）：是不是 ADMIN 只有后端知道，这里只是把它的回答说清楚。
 */
export function CustomerErrorAlert({
  error,
  title,
  action,
}: {
  error: Error | null;
  title: string;
  action?: ReactNode;
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
      action={action}
    />
  );
}

/** 计费状态。认不出的值原样显示，不猜。 */
export function BillingStatusTag({ status }: { status: string }) {
  const { t } = useTranslation();
  if (status === "ACTIVE") {
    return <Tag color="green">{t("customers.billingStatus.active")}</Tag>;
  }
  if (status === "SUSPENDED") {
    return <Tag color="red">{t("customers.billingStatus.suspended")}</Tag>;
  }
  return <Tag>{status}</Tag>;
}

/**
 * 读客户的查询重试策略：后端已经给了明确答复的错误**不重试**。
 *
 * 404、403、422、401 再问一次也还是同一个答案，重试只会让用户多等一轮才看到
 * 「客户不存在」。其余（5xx、网络）按全局默认重试一次。
 */
const DEFINITE_ANSWERS = new Set([
  CUSTOMER_NOT_FOUND,
  ADMIN_REQUIRED,
  "VALIDATION_ERROR",
  "TOKEN_INVALID",
]);

export function retryUnlessDefinite(failureCount: number, error: Error): boolean {
  if (error instanceof ApiError && DEFINITE_ANSWERS.has(error.code)) {
    return false;
  }
  return failureCount < 1;
}
