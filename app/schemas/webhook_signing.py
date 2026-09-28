"""Request and response shapes for outbound webhook signing secrets (design gate #135 v1 §2).

⚠️ 请求体一律 `extra="forbid"`：不能指定 `secret`、`key_version`、`status` 或任何 id。
客户与项目只来自路径，操作者只来自令牌（INV-8）。

⚠️ **响应模型是字段白名单，不直接序列化 ORM 对象**：内部 id、`tenant_id`、内部
`project_id`、密文、主密钥版本与两个生成列都不会因为表多了一列而静默出现在响应里。

⚠️ `secret` 只在签发的 201 响应里（§36）。它是 `SecretStr`：`repr` / `str` 只显示掩码，
只有序列化响应时才取出原值（设计 §6）。
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated

from pydantic import BaseModel, ConfigDict, SecretStr, StringConstraints, field_serializer

from app.models.integration import ProjectWebhookSecret, WebhookSecretStatus

# 去掉首尾空白后长度 1–255：写进审计的 `reason` 列（§66）。只写业务说明，不写个人数据。
_Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]


class IssueWebhookSecretRequest(BaseModel):
    """请求体就是 `{}`：secret 与版本号都由服务端生成。"""

    model_config = ConfigDict(extra="forbid")


class ActivateWebhookSecretRequest(BaseModel):
    """请求体就是 `{}`：要启用的版本只来自路径。"""

    model_config = ConfigDict(extra="forbid")


class RetireWebhookSecretRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: _Reason


class WebhookSecretView(BaseModel):
    """一个签名版本。"""

    key_version: int
    status: str
    created_at: dt.datetime
    activated_at: dt.datetime | None
    retired_at: dt.datetime | None


class IssuedWebhookSecretView(WebhookSecretView):
    """签发的响应：版本对象多一个 `secret`，这是它唯一一次离开服务端。"""

    secret: SecretStr

    @field_serializer("secret")
    def _reveal(self, value: SecretStr) -> str:
        # 只在序列化响应时取值；repr / str 仍是掩码。
        return value.get_secret_value()


def _fields(row: ProjectWebhookSecret) -> dict[str, object]:
    return {
        "key_version": row.key_version,
        "status": WebhookSecretStatus(row.status).value,
        "created_at": row.created_at,
        "activated_at": row.activated_at,
        "retired_at": row.retired_at,
    }


def webhook_secret_view(row: ProjectWebhookSecret) -> WebhookSecretView:
    return WebhookSecretView.model_validate(_fields(row))


def issued_webhook_secret_view(
    row: ProjectWebhookSecret, *, secret: str
) -> IssuedWebhookSecretView:
    return IssuedWebhookSecretView.model_validate({**_fields(row), "secret": SecretStr(secret)})
