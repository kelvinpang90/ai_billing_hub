/**
 * Sign-in, in the three shapes the backend can return (spec §53, §54).
 *
 * 1. 输邮箱 + 密码
 * 2. 已启用 2FA → 输 6 位验证码（或恢复码）
 * 3. ADMIN 还没启用 2FA → **先扫码注册**，抄下恢复码，再输验证码
 *
 * 第 3 步不是可选项：spec §54 要求 ADMIN 的 2FA 强制启用，所以新管理员第一次
 * 登录必然走到这里。
 */

import { Alert, Button, Card, Form, Input, Space, Typography } from "antd";
import { useCallback, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate } from "react-router";

import {
  STAGE_ENROL_2FA,
  STAGE_TOTP_REQUIRED,
  confirmEnrolment,
  login,
  startEnrolment,
  submitSecondFactor,
  type Enrolment,
} from "../../api/auth";
import { ApiError } from "../../api/client";
import { useAuth } from "../../auth/AuthProvider";
import { ROUTES } from "../../routes/paths";
import { ErrorAlert } from "./ErrorAlert";
import { QrCode } from "./QrCode";

type Stage = "credentials" | "second-factor" | "enrol" | "recovery-codes";

/**
 * 后端 `InvalidToken` 的错误码（`app/core/tokens.py`）。
 *
 * ⚠️ 这里认它，是因为 pending 令牌只活 **120 秒**，而注册那条路要求用户扫码、
 * 抄下 10 个恢复码 —— 本地实测 `/2fa/confirm` 用掉 113 秒，紧贴上限。令牌一
 * 过期，验证码那张表单就**再也不可能提交成功**：用户会对着「验证码无效」反复
 * 重输，而问题根本不在验证码。所以这种错误必须把人退回第一步。
 */
const TOKEN_INVALID = "TOKEN_INVALID";

export function LoginPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { signedIn } = useAuth();

  const [stage, setStage] = useState<Stage>("credentials");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [pendingToken, setPendingToken] = useState<string | null>(null);
  const [enrolment, setEnrolment] = useState<Enrolment | null>(null);
  const [recoveryCodes, setRecoveryCodes] = useState<string[]>([]);
  // 只为了在退回第一步时说清楚「2FA 已经开好了，现在重新登录一次」。
  const [enrolled, setEnrolled] = useState(false);

  /** 回到第一步，并把这一轮的半成品状态全丢掉。 */
  const restart = useCallback(() => {
    setPendingToken(null);
    setEnrolment(null);
    setRecoveryCodes([]);
    setStage("credentials");
  }, []);

  async function guard(work: () => Promise<void>): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      await work();
    } catch (caught) {
      // ⚠️ 只显示后端给的安全文案与 request_id，**绝不把响应体打进控制台**。
      setError(caught instanceof ApiError ? caught : null);
      if (!(caught instanceof ApiError)) {
        throw caught;
      }
      if (caught.code === TOKEN_INVALID) {
        restart();
      }
    } finally {
      setBusy(false);
    }
  }

  // ⚠️ antd 的 onFinish 要的是 `void`。直接把 async 函数递给它，Promise 就没人
  // 接了：里面 throw 出来的是未捕获的 rejection，不是 React 错误边界能看到的东西。
  // 所以每个提交口都是「同步壳 + `void guard(...)`」，异常全在 guard 里收口。
  const submitCredentials = (values: { email: string; password: string }): void => {
    // 重新提交密码 = 上一轮的「注册完成」提示该收起来了。
    setEnrolled(false);
    void guard(async () => {
      const result = await login(values.email, values.password);
      if (result.access_token) {
        signedIn(result.access_token);
        void navigate(ROUTES.dashboard, { replace: true });
        return;
      }
      setPendingToken(result.pending_token);
      if (result.stage === STAGE_ENROL_2FA) {
        // 顺手把密钥取回来，用户才有东西可扫。
        setEnrolment(await startEnrolment(result.pending_token ?? ""));
        setStage("enrol");
      } else if (result.stage === STAGE_TOTP_REQUIRED) {
        setStage("second-factor");
      }
    });
  };

  const submitSecondFactorCode = (values: { code: string }): void => {
    void guard(async () => {
      const result = await submitSecondFactor(pendingToken ?? "", values.code);
      if (result.access_token) {
        signedIn(result.access_token);
        void navigate(ROUTES.dashboard, { replace: true });
      }
    });
  };

  const submitEnrolmentCode = (values: { code: string }): void => {
    void guard(async () => {
      const codes = await confirmEnrolment(pendingToken ?? "", values.code);
      setRecoveryCodes(codes.recovery_codes);
      setStage("recovery-codes");
    });
  };

  /**
   * 抄完恢复码之后，**回第一步重新登录**。
   *
   * ⚠️ 不能直接跳到验证码那一步：确认注册只启用了 2FA、没有发会话，而手上
   * 那张 pending 令牌是登录时签的，只活 120 秒 —— 扫码加抄码之后它几乎必然
   * 已经过期，用户会拿到一句指向验证码的错误，原因却在两步之前。
   *
   * `restart()` 顺手把恢复码从 state 里清掉，这是**内存卫生**而不是界面效果：
   * 换了 stage 之后那一段本来就不渲染了。恢复码是凭据，看完不该继续留着。
   */
  const finishEnrolment = (): void => {
    setEnrolled(true);
    restart();
  };

  return (
    <div style={{ display: "flex", justifyContent: "center", paddingTop: 64 }}>
      <Card style={{ width: 460 }} title={t("login.title")}>
        <ErrorAlert error={error} />

        {stage === "credentials" && enrolled ? (
          <Alert
            type="success"
            showIcon
            style={{ marginBottom: 16 }}
            message={t("enrol.done")}
          />
        ) : null}

        {stage === "credentials" ? (
          <Form layout="vertical" onFinish={submitCredentials} disabled={busy}>
            <Form.Item name="email" label={t("login.email")} rules={[{ required: true }]}>
              <Input autoComplete="username" inputMode="email" autoFocus />
            </Form.Item>
            <Form.Item name="password" label={t("login.password")} rules={[{ required: true }]}>
              <Input.Password autoComplete="current-password" />
            </Form.Item>
            <Button type="primary" htmlType="submit" loading={busy} block>
              {t("login.submit")}
            </Button>
          </Form>
        ) : null}

        {/* ⚠️ 只在密码这一步给入口。走到第二因子那一步的人，问题已经不是密码了，
            这时候再摆一个「忘记密码」只会把人引到一条解决不了他问题的路上。 */}
        {stage === "credentials" ? (
          <Typography.Paragraph style={{ marginTop: 16, marginBottom: 0 }}>
            <Link to={ROUTES.forgotPassword}>{t("login.forgotPassword")}</Link>
          </Typography.Paragraph>
        ) : null}

        {stage === "second-factor" ? (
          <Form layout="vertical" onFinish={submitSecondFactorCode} disabled={busy}>
            <Typography.Paragraph type="secondary">{t("login.totpHint")}</Typography.Paragraph>
            <Form.Item name="code" label={t("login.code")} rules={[{ required: true }]}>
              {/* ⚠️ 同一个框收验证码与恢复码：对用户来说它们是同一件事
                  （「证明你是你」），分两个框只会让人犹豫该填哪个。 */}
              <Input autoComplete="one-time-code" autoFocus />
            </Form.Item>
            <Button type="primary" htmlType="submit" loading={busy} block>
              {t("login.verify")}
            </Button>
          </Form>
        ) : null}

        {stage === "enrol" && enrolment ? (
          <Form layout="vertical" onFinish={submitEnrolmentCode} disabled={busy}>
            <Typography.Paragraph>{t("enrol.intro")}</Typography.Paragraph>
            <QrCode value={enrolment.otpauth_uri} />
            <Typography.Paragraph type="secondary" style={{ marginTop: 12 }}>
              {t("enrol.manualHint")}
            </Typography.Paragraph>
            {/* 扫不了码时手输。⚠️ 这是密钥唯一一次离开服务端。 */}
            <Typography.Text code copyable>
              {enrolment.secret}
            </Typography.Text>
            <Form.Item
              name="code"
              label={t("enrol.code")}
              rules={[{ required: true }]}
              style={{ marginTop: 16 }}
            >
              <Input autoComplete="one-time-code" />
            </Form.Item>
            <Button type="primary" htmlType="submit" loading={busy} block>
              {t("enrol.confirm")}
            </Button>
          </Form>
        ) : null}

        {stage === "recovery-codes" ? (
          <Space direction="vertical" style={{ width: "100%" }}>
            <Alert type="warning" showIcon message={t("recovery.warning")} />
            <Typography.Paragraph>{t("recovery.intro")}</Typography.Paragraph>
            <Typography.Paragraph copyable={{ text: recoveryCodes.join("\n") }}>
              {recoveryCodes.map((code) => (
                <Typography.Text key={code} code style={{ display: "block" }}>
                  {code}
                </Typography.Text>
              ))}
            </Typography.Paragraph>
            <Button type="primary" onClick={finishEnrolment} block>
              {t("recovery.saved")}
            </Button>
          </Space>
        ) : null}
      </Card>
    </div>
  );
}
