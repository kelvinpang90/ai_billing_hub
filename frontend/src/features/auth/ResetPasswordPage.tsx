/**
 * 「重置密码」—— 用邮件链接里那张令牌换一个新密码（spec §53）。
 *
 * 链接的形状由后端拼（`app/tasks/outbox.py` 的 `_render_password_reset`）：
 * `{BILLING_FRONTEND_BASE_URL}/reset-password?token=...`。
 *
 * ⚠️ **令牌不对时不能把表单继续摆在那里。**那张表单此刻已经**再也不可能提交
 * 成功**，留着只会让用户反复改密码、反复拿到同一句错误 —— 现象指向密码，原因
 * 却在链接。所以这种错误直接换成「链接已失效，去要一张新的」。这是 T0.8c 在
 * pending 令牌上踩过的同一个坑。
 */

import { Button, Card, Form, Input, Result, Typography } from "antd";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useSearchParams } from "react-router";

import { INVALID_RESET_TOKEN, resetPassword } from "../../api/auth";
import { ApiError } from "../../api/client";
import { RESET_TOKEN_PARAM, ROUTES } from "../../routes/paths";
import { ErrorAlert, ErrorReference } from "./ErrorAlert";

type Stage = "form" | "dead-link" | "done";

export function ResetPasswordPage() {
  const { t } = useTranslation();
  const [searchParams] = useSearchParams();
  const token = searchParams.get(RESET_TOKEN_PARAM) ?? "";

  const [stage, setStage] = useState<Stage>("form");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);

  // ⚠️ 「链接里压根没带令牌」是从 URL 推出来的，不进 state。放进 `useState` 的
  // 初始值里的话，它只在首次挂载时算一次 —— 那是个只在 URL 变化时才现形的 bug，
  // 而这里本来就不需要记住任何东西。
  const shown: Stage = token === "" ? "dead-link" : stage;

  const submit = (values: { password: string }): void => {
    setBusy(true);
    setError(null);
    void (async () => {
      try {
        await resetPassword(token, values.password);
        setStage("done");
      } catch (caught) {
        setError(caught instanceof ApiError ? caught : null);
        if (!(caught instanceof ApiError)) {
          throw caught;
        }
        if (caught.code === INVALID_RESET_TOKEN) {
          // 见文件头：这张表单已经不可能成功了，别让用户对着它重试。
          setStage("dead-link");
        }
      } finally {
        setBusy(false);
      }
    })();
  };

  return (
    <div style={{ display: "flex", justifyContent: "center", paddingTop: 64 }}>
      <Card style={{ width: 460 }} title={t("reset.title")}>
        {shown === "dead-link" ? (
          <Result
            status="error"
            title={t("reset.deadLinkTitle")}
            subTitle={
              <>
                {/* 有后端的说法就用后端的 —— 它比我们这边的泛泛而谈更准确。 */}
                {error === null ? t("reset.deadLinkBody") : error.message}
                <div style={{ marginTop: 8 }}>
                  <ErrorReference error={error} />
                </div>
              </>
            }
            extra={
              <Link to={ROUTES.forgotPassword}>
                <Button type="primary">{t("reset.requestNewLink")}</Button>
              </Link>
            }
          />
        ) : null}

        {shown === "form" ? (
          <>
            <ErrorAlert error={error} />
            <Form layout="vertical" onFinish={submit} disabled={busy}>
              {/* ⚠️ 这里**刻意不复述强度规则的具体数字**。规则只有后端
                  `validate_password_strength` 一处说了算；在前端再写一遍最短
                  长度，改了后端就会变成一句**安静地说错**的提示。密码不合格时
                  后端返回的文案本身就是精确的，直接显示那一句。 */}
              <Typography.Paragraph type="secondary">{t("reset.intro")}</Typography.Paragraph>
              <Form.Item
                name="password"
                label={t("reset.newPassword")}
                rules={[{ required: true }]}
              >
                <Input.Password autoComplete="new-password" autoFocus />
              </Form.Item>
              <Form.Item
                name="confirm"
                label={t("reset.confirmPassword")}
                dependencies={["password"]}
                rules={[
                  { required: true },
                  // 纯粹是防打错：两次不一致就别发出去，否则用户会带着一个
                  // 自己不知道的密码离开这一页，下次登录一脸问号。
                  ({ getFieldValue }) => ({
                    validator(_rule, value: string) {
                      if (!value || getFieldValue("password") === value) {
                        return Promise.resolve();
                      }
                      return Promise.reject(new Error(t("reset.mismatch")));
                    },
                  }),
                ]}
              >
                <Input.Password autoComplete="new-password" />
              </Form.Item>
              <Button type="primary" htmlType="submit" loading={busy} block>
                {t("reset.submit")}
              </Button>
            </Form>
          </>
        ) : null}

        {shown === "done" ? (
          <Result
            status="success"
            title={t("reset.doneTitle")}
            // ⚠️ 后端在改密码的同时吊销了该用户**全部**刷新令牌，别的设备上的
            // 会话都会掉线。这一句是为了让那件事不显得像故障。
            subTitle={t("reset.doneBody")}
            extra={
              <Link to={ROUTES.login}>
                <Button type="primary">{t("reset.signIn")}</Button>
              </Link>
            }
          />
        ) : null}
      </Card>
    </div>
  );
}
