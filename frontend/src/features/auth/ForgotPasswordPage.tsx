/**
 * 「忘记密码」——申请一封重置邮件（spec §53）。
 *
 * ⚠️ **这一页最重要的性质是「什么都不说」。**后端对任何输入都返回同一个 200
 * 空响应、还补齐了耗时，为的是让这个不需要凭据的端点不能被当成用户枚举工具。
 * 界面上任何「这个邮箱没注册过」式的贴心提示，都会把后端那一整套努力白费掉 ——
 * 所以提交成功后这里说的是**条件句**：「如果那个地址有账号，信已经在路上」。
 */

import { Button, Card, Form, Input, Result, Typography } from "antd";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import { requestPasswordReset } from "../../api/auth";
import { ApiError } from "../../api/client";
import { ROUTES } from "../../routes/paths";
import { ErrorAlert } from "./ErrorAlert";

export function ForgotPasswordPage() {
  const { t } = useTranslation();

  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [sent, setSent] = useState(false);

  // ⚠️ antd 的 onFinish 要的是 `void`，直接递 async 函数进去的话里面 throw 出来
  // 的是没人接的 rejection（见 LoginPage 同处注释）。所以是「同步壳 + void」。
  const submit = (values: { email: string }): void => {
    setBusy(true);
    setError(null);
    void (async () => {
      try {
        await requestPasswordReset(values.email);
        setSent(true);
      } catch (caught) {
        setError(caught instanceof ApiError ? caught : null);
        if (!(caught instanceof ApiError)) {
          throw caught;
        }
      } finally {
        setBusy(false);
      }
    })();
  };

  return (
    <div style={{ display: "flex", justifyContent: "center", paddingTop: 64 }}>
      <Card style={{ width: 460 }} title={t("forgot.title")}>
        {sent ? (
          <Result
            status="success"
            title={t("forgot.sentTitle")}
            // ⚠️ 条件句，见文件头。这里写成「信已发往 x@y.com」就等于确认了
            // 该账号存在 —— 后端刻意抹平的差异会从界面上漏回来。
            subTitle={t("forgot.sentBody")}
            extra={
              <Link to={ROUTES.login}>
                <Button type="primary">{t("forgot.backToLogin")}</Button>
              </Link>
            }
          />
        ) : (
          <>
            <ErrorAlert error={error} />
            <Form layout="vertical" onFinish={submit} disabled={busy}>
              <Typography.Paragraph type="secondary">{t("forgot.intro")}</Typography.Paragraph>
              <Form.Item name="email" label={t("login.email")} rules={[{ required: true }]}>
                <Input autoComplete="username" inputMode="email" autoFocus />
              </Form.Item>
              <Button type="primary" htmlType="submit" loading={busy} block>
                {t("forgot.submit")}
              </Button>
            </Form>
            <Typography.Paragraph style={{ marginTop: 16, marginBottom: 0 }}>
              <Link to={ROUTES.login}>{t("forgot.backToLogin")}</Link>
            </Typography.Paragraph>
          </>
        )}
      </Card>
    </div>
  );
}
