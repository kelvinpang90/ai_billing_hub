/**
 * 「接受邀请」—— 客户凭邀请邮件里那张令牌给门户账号设第一个密码（设计 AIH-TASK-089 §2、§8，任务 C）。
 *
 * 链接的形状由后端拼（设计 §2，AIH-TASK-094 落地）：`{frontend_base_url}/accept-invitation?token=...`。
 *
 * ⚠️ 这一页**先于**接受邀请的接口上线（设计 §8）。那段时间里接口回 404，页面对任何令牌都显示
 * 「链接无效或已过期」—— 那时也还没有任何人能发出邀请，所以不影响任何人。
 *
 * ⚠️ 令牌不对、租户已关户时**不把表单继续摆在那里**：那张表单已经不可能提交成功，留着只会让人
 * 反复改密码（与重置密码页同一条理由，见 `ResetPasswordPage`）。只有密码强度不合格可以留在表单上重试
 * —— 后端把强度校验排在消费令牌之前（设计 §4 判定顺序第 5 步），弱密码不会烧掉令牌。
 *
 * 令牌只在内存里经过：不写日志、不进 localStorage / sessionStorage。
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

/** 成功提示停留的秒数。比 antd 默认的 3 秒长：人刚被换了一页，要来得及读完。 */
const DONE_MESSAGE_SECONDS = 8;

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
        void message.success(t("acceptInvitation.done"), DONE_MESSAGE_SECONDS);
        // `replace`：浏览器「后退」不该退回一张令牌已经用掉的表单。
        void navigate(ROUTES.login, { replace: true });
      } catch (caught) {
        setError(caught instanceof ApiError ? caught : null);
        if (!(caught instanceof ApiError)) {
          throw caught;
        }
        if (caught.code === INVALID_INVITATION_TOKEN || caught.code === INVITATION_ENDPOINT_NOT_FOUND) {
          // 404 = 接口还没上线（设计 §8），对用户来说就是「这个链接用不了」。
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
                {t("acceptInvitation.deadLinkBody")}
                {/* 提交确认丢失后重试也会落到这里（设计 §5）：那时密码其实已经设好了。 */}
                <div style={{ marginTop: 8 }}>{t("acceptInvitation.alreadySet")}</div>
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
                // 规则的具体数字只有后端一处说了算，它的文案本身就是精确的，原样显示。
                description={
                  <>
                    <div>{error.message}</div>
                    <ErrorReference error={error} />
                  </>
                }
              />
            ) : (
              <ErrorAlert error={error} />
            )}
            <Form layout="vertical" onFinish={submit} disabled={busy}>
              {/* ⚠️ 与重置密码页一样，**不复述强度规则的具体数字**（见那边的注释）。 */}
              <Typography.Paragraph type="secondary">{t("acceptInvitation.intro")}</Typography.Paragraph>
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
                  // 两次不一致就别发出去：客户会带着一个自己不知道的密码离开这一页。
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
