/**
 * Render an `otpauth://` URI as a scannable QR code.
 *
 * ⚠️ **本地渲染，绝不调用任何在线二维码服务。**那个 URI 的查询串里就是 TOTP
 * 密钥 —— 把它发给第三方等于把第二因子交出去。`qrcode` 这个依赖存在的全部
 * 理由就是这个。
 *
 * ⚠️ 生成出来的 data URI 同样含密钥（编码过），所以它不进日志、不进
 * `console`，也不放进任何会被上报的地方。
 */

import { Skeleton, Typography } from "antd";
import QRCode from "qrcode";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

export function QrCode({ value }: { value: string }) {
  const { t } = useTranslation();
  const [dataUri, setDataUri] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    QRCode.toDataURL(value, { errorCorrectionLevel: "M", margin: 1, width: 200 })
      .then((uri) => {
        if (!cancelled) {
          setDataUri(uri);
        }
      })
      .catch(() => {
        // 渲染不出来不是致命的 —— 下面还有手输密钥那条路。但**必须让用户知道**，
        // 否则他会对着一块空白等下去。
        if (!cancelled) {
          setFailed(true);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [value]);

  if (failed) {
    return <Typography.Text type="warning">{t("enrol.qrFailed")}</Typography.Text>;
  }
  if (dataUri === null) {
    return <Skeleton.Node active style={{ width: 200, height: 200 }} />;
  }
  // alt 不放密钥内容 —— 读屏软件会把它念出来。
  return <img src={dataUri} alt={t("enrol.qrAlt")} width={200} height={200} />;
}
