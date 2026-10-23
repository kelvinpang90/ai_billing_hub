/**
 * 客户自己开启 2FA（AIH-TASK-097，设计 AIH-TASK-089 §2；契约见 docs/api.md「客户自己开启 2FA」）。
 *
 * 1. 重验密码 → `POST /api/v1/auth/2fa/self/enrol` → 密钥与 `otpauth://` URI
 * 2. 扫码（或手输密钥），输验证器上的六位数 → `/2fa/self/confirm` → 恢复码
 * 3. 恢复码**只展示这一次**：点「已保存」之后从状态里清掉，再也看不到
 *
 * 只在 `/api/v1/me` 的 `two_factor_enabled` 为 `false` 时出现。开启 2FA 在只读状态下仍然允许（docs/api.md
 * 「客户请求的约定」），所以这里不看 `read_only`。
 *
 * ⚠️ 密钥、`otpauth_uri` 与恢复码都是凭据：**只放在这个组件的 state 里**，不进日志、不进 `console`、不进
 * localStorage / sessionStorage / URL / 查询缓存。离开页面组件卸载，它们随之丢弃。
 */

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Form, Input, Space, Typography } from "antd";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { ME_QUERY_KEY, getMe, type Enrolment } from "../../api/auth";
import { ApiError } from "../../api/client";
import {
  ALREADY_ENROLLED,
  INVALID_CREDENTIALS,
  INVALID_TOTP,
  NOT_ENROLLED,
  TOKEN_INVALID,
  confirmSelfEnrolment,
  startSelfEnrolment,
} from "../../api/customer";
import { RequestReference } from "../../components/RequestReference";
import { ErrorReference } from "../auth/ErrorAlert";
import { QrCode } from "../auth/QrCode";

/**
 * `already`：服务端说已经开启过（409），比 `/api/v1/me` 先知道。单独一态，是为了把这句话留在屏幕上 ——
 * 回到 `idle` 的话，`/api/v1/me` 重读回来组件就整个消失了，用户不知道刚才发生了什么。
 */
type Stage = "idle" | "scan" | "codes" | "done" | "already";

export function EnableTwoFactor() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const me = useQuery({
    queryKey: ME_QUERY_KEY,
    queryFn: ({ signal }) => getMe(signal),
    // 与 `RequireRole` 同一个键、同一个设置：换账号登录后拿到的必须是新账号的。
    gcTime: 0,
  });

  const [stage, setStage] = useState<Stage>("idle");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [enrolment, setEnrolment] = useState<Enrolment | null>(null);
  const [recoveryCodes, setRecoveryCodes] = useState<string[]>([]);

  async function guard(work: () => Promise<void>): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      await work();
    } catch (caught) {
      // ⚠️ 只显示固定文案、后端的安全文案与 request_id，**绝不把响应体打进控制台**。
      if (!(caught instanceof ApiError)) {
        throw caught;
      }
      setError(caught);
      if (caught.code === ALREADY_ENROLLED) {
        setEnrolment(null);
        setStage("already");
        void queryClient.invalidateQueries({ queryKey: ME_QUERY_KEY });
      } else if (caught.code === NOT_ENROLLED) {
        // 待确认的注册没了（例如被管理员重置）：手上的密钥已经作废，只能从头来。
        setEnrolment(null);
        setStage("idle");
      }
    } finally {
      setBusy(false);
    }
  }

  // 同步壳 + `void guard(...)`，理由同 `LoginPage`：antd 的 onFinish 要的是 `void`。
  const submitPassword = (values: { password: string }): void => {
    void guard(async () => {
      setEnrolment(await startSelfEnrolment(values.password));
      setStage("scan");
    });
  };

  const submitCode = (values: { code: string }): void => {
    void guard(async () => {
      const codes = await confirmSelfEnrolment(values.code.trim());
      // 密钥用完了：确认之后它不该继续留在内存里。
      setEnrolment(null);
      setRecoveryCodes(codes.recovery_codes);
      setStage("codes");
    });
  };

  /** 抄完恢复码：从 state 里清掉（内存卫生，不只是界面效果），让 `/api/v1/me` 重读成已开启。 */
  const finish = (): void => {
    setRecoveryCodes([]);
    setStage("done");
    void queryClient.invalidateQueries({ queryKey: ME_QUERY_KEY });
  };

  if (me.isPending) {
    return null;
  }
  if (me.isError) {
    return <Alert type="error" showIcon message={t("portal.twoFactor.meFailed")} description={<RequestReference error={me.error} />} />;
  }
  // ⚠️ 只在还没开始时按 `/api/v1/me` 隐藏。流程中途（尤其是恢复码那一屏）`/api/v1/me` 可能被重读成已开启，
  // 那时隐藏就是把唯一一次的恢复码收走。
  if (stage === "idle" && me.data.two_factor_enabled) {
    return null;
  }

  return (
    <Card title={t("portal.twoFactor.title")}>
      {error !== null && stage !== "already" ? <TwoFactorError error={error} /> : null}

      {stage === "idle" ? (
        <Form layout="vertical" onFinish={submitPassword} disabled={busy}>
          <Typography.Paragraph>{t("portal.twoFactor.intro")}</Typography.Paragraph>
          <Form.Item
            name="password"
            label={t("portal.twoFactor.password")}
            rules={[{ required: true, message: t("portal.twoFactor.passwordRequired") }]}
          >
            <Input.Password autoComplete="current-password" />
          </Form.Item>
          <Button type="primary" htmlType="submit" loading={busy}>
            {t("portal.twoFactor.start")}
          </Button>
        </Form>
      ) : null}

      {stage === "scan" && enrolment !== null ? (
        <Form layout="vertical" onFinish={submitCode} disabled={busy}>
          <Typography.Paragraph>{t("portal.twoFactor.scan")}</Typography.Paragraph>
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
            // 显式给提示，理由同 `LoginPage`：antd 的默认提示会拼成「Please enter Enter the code…」。
            rules={[{ required: true, whitespace: true, message: t("enrol.codeRequired") }]}
            style={{ marginTop: 16 }}
          >
            <Input autoComplete="one-time-code" inputMode="numeric" />
          </Form.Item>
          <Button type="primary" htmlType="submit" loading={busy}>
            {t("enrol.confirm")}
          </Button>
        </Form>
      ) : null}

      {stage === "codes" ? (
        <Space direction="vertical" style={{ width: "100%" }}>
          <Alert type="warning" showIcon message={t("recovery.warning")} />
          <Typography.Paragraph>{t("portal.twoFactor.recoveryIntro")}</Typography.Paragraph>
          <Typography.Paragraph copyable={{ text: recoveryCodes.join("\n") }}>
            {recoveryCodes.map((code) => (
              <Typography.Text key={code} code style={{ display: "block" }}>
                {code}
              </Typography.Text>
            ))}
          </Typography.Paragraph>
          <Button type="primary" onClick={finish}>
            {t("recovery.saved")}
          </Button>
        </Space>
      ) : null}

      {stage === "done" ? <Alert type="success" showIcon message={t("portal.twoFactor.done")} /> : null}

      {stage === "already" ? (
        <Alert
          type="info"
          showIcon
          message={t("portal.twoFactor.error.alreadyEnrolled")}
          description={<ErrorReference error={error} />}
        />
      ) : null}
    </Card>
  );
}

/** 密码错、验证码错、会话过期、注册已失效各有一句固定文案；其余显示后端的安全文案。都带 request_id。 */
function TwoFactorError({ error }: { error: ApiError }) {
  const { t } = useTranslation();
  let message: string;
  switch (error.code) {
    case INVALID_CREDENTIALS:
      message = t("portal.twoFactor.error.wrongPassword");
      break;
    case INVALID_TOTP:
      message = t("portal.twoFactor.error.wrongCode");
      break;
    case NOT_ENROLLED:
      message = t("portal.twoFactor.error.notEnrolled");
      break;
    case TOKEN_INVALID:
      message = t("portal.twoFactor.error.sessionExpired");
      break;
    default:
      message = error.message;
  }
  return (
    <Alert
      type="error"
      showIcon
      style={{ marginBottom: 16 }}
      message={message}
      description={<ErrorReference error={error} />}
    />
  );
}
