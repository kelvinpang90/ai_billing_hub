/**
 * 「接受邀请」—— 客户用邀请邮件里那张令牌给门户账号设密码（设计 AIH-TASK-089 §2、§8，AIH-TASK-092）。
 *
 * 链接的形状由后端拼：`{frontend_base_url}/accept-invitation?token=...`。这一页在 `RequireAuth`
 * **外面**（见 `routes/index.tsx`）：点链接的客户账号还是 `INVITED`，没有可用的密码，送去登录页就是死链。
 *
 * ⚠️ 这一页**先于**接受邀请接口上线（设计 §8）。那段窗口里提交只会得到 404，按「链接无效」处理 ——
 * 此时还没有任何邀请发出去过，不影响任何人。
 *
 * ⚠️ 与重置密码页同一条规矩：令牌不对（或账户已关）时**把表单收走**。那张表单已经再也不可能提交成功，
 * 留着只会让客户反复改密码、反复拿到同一句错误。只有密码强度不合格才留在表单上。
 *
 * ⚠️ 令牌只从 URL 读、只进请求体：不打日志，不写 localStorage / sessionStorage。
 */

import { Alert, App, Button, Card, Form, Input, Result, Typography } from "antd";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate, useSearchParams } from "react-router";

import {
  ACCOUNT_CLOSED,
  INVALID_INVITATION_TOKEN,
  INVITATION_ENDPOINT_NOT_FOUND,
  WEAK_PASSWORD,
  acceptInvitation,
} from "../../api/auth";
import { ApiError } from "../../api/client";
import { ACCEPT_INVITATION_TOKEN_PARAM, ROUTES } from "../../routes/paths";
import { ErrorAlert, ErrorReference } from "./ErrorAlert";

type Stage = "form" | "dead-link" | "closed";

/** 成功提示停留的秒数：它出现在登录页上，要留够读完的时间。 */
const DONE_NOTICE_SECONDS = 8;

export function AcceptInvitationPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { message } = App.useApp();
  const [searchParams] = useSearchParams();
  const token = searchParams.get(ACCEPT_INVITATION_TOKEN_PARAM) ?? "";

  const [stage, setStage] = useState<Stage>("form");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);

  // 「链接里压根没带令牌」从 URL 推出来，不进 state（理由同 ResetPasswordPage）。
  const shown: Stage = token === "" ? "dead-link" : stage;

  const submit = (values: { password: string }): void => {
    setBusy(true);
    setError(null);
    void (async () => {
      try {
        await acceptInvitation(token, values.password);
        void message.success(t("acceptInvitation.doneNotice"), DONE_NOTICE_SECONDS);
        void navigate(ROUTES.login, { replace: true });
      } catch (caught) {
        setError(caught instanceof ApiError ? caught : null);
        if (!(caught instanceof ApiError)) {
          throw caught;
        }
        if (caught.code === INVALID_INVITATION_TOKEN || caught.code === INVITATION_ENDPOINT_NOT_FOUND) {
          // 见文件头：这张表单已经不可能成功了，别让客户对着它重试。
          setStage("dead-link");
        } else if (caught.code === ACCOUNT_CLOSED) {
          setStage("closed");
        }
      } finally {
        setBusy(false);
      }
    })();
  };

  return (
    <div style={{ display: "flex", justifyContent: "center", paddingTop: 64 }}>
      <Card style={{ width: 460 }} title={t("acceptInvitation.title")}>
        {shown === "dead-link" ? (
          <Result
            status="error"
            title={t("acceptInvitation.deadLinkTitle")}
            subTitle={
              <>
                {/* ⚠️ 这里**不用**后端的说法：接口未上线时它是一句「Not Found」。提交确认丢失后的重试
                    也落在这里（设计 §6），所以文案里提示「已经设过密码就直接登录」。 */}
                {t("acceptInvitation.deadLinkBody")}
                <div style={{ marginTop: 8 }}>
                  <ErrorReference error={error} />
                </div>
              </>
            }
            extra={
              <Link to={ROUTES.login}>
                <Button type="primary">{t("acceptInvitation.signIn")}</Button>
              </Link>
            }
          />
        ) : null}

        {shown === "closed" ? (
          <Result
            status="warning"
            title={t("acceptInvitation.closedTitle")}
            subTitle={
              <>
                {t("acceptInvitation.closedBody")}
                <div style={{ marginTop: 8 }}>
                  <ErrorReference error={error} />
                </div>
              </>
            }
          />
        ) : null}

        {shown === "form" ? (
          <>
            {error !== null && error.code === WEAK_PASSWORD ? (
              <Alert
                type="error"
                showIcon
                style={{ marginBottom: 16 }}
                message={t("acceptInvitation.weakPassword")}
                // 具体规则只有后端说了算，它回的那一句是精确的，原样附上。
                description={
                  <>
                    {error.message}
                    <div>
                      <ErrorReference error={error} />
                    </div>
                  </>
                }
              />
            ) : (
              <ErrorAlert error={error} />
            )}
            <Form layout="vertical" onFinish={submit} disabled={busy}>
              {/* ⚠️ 与重置密码页一样**不复述强度规则的具体数字**：规则只有后端
                  `validate_password_strength` 一处说了算。 */}
              <Typography.Paragraph type="secondary">
                {t("acceptInvitation.intro")}
              </Typography.Paragraph>
              <Form.Item
                name="password"
                label={t("acceptInvitation.password")}
                rules={[{ required: true }]}
              >
                <Input.Password autoComplete="new-password" autoFocus />
              </Form.Item>
              <Form.Item
                name="confirm"
                label={t("acceptInvitation.confirmPassword")}
                dependencies={["password"]}
                rules={[
                  { required: true },
                  // 防打错：两次不一致就别发出去，否则客户带着一个自己不知道的密码离开。
                  ({ getFieldValue }) => ({
                    validator(_rule, value: string) {
                      if (!value || getFieldValue("password") === value) {
                        return Promise.resolve();
                      }
                      return Promise.reject(new Error(t("acceptInvitation.mismatch")));
                    },
                  }),
                ]}
              >
                <Input.Password autoComplete="new-password" />
              </Form.Item>
              <Button type="primary" htmlType="submit" loading={busy} block>
                {t("acceptInvitation.submit")}
              </Button>
            </Form>
          </>
        ) : null}
      </Card>
    </div>
  );
}
