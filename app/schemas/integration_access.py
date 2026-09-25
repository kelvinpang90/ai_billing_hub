"""Request and response shapes for integration API credentials (design gate #118 v1 §2).

⚠️ 请求体一律 `extra="forbid"`：不能指定 `api_key`、`secret`、`key_version`、`tenant_id`、
`project_id`、`valid_until` 或任何 id。客户与项目只来自路径，操作者只来自令牌（INV-8）。

⚠️ **响应模型是字段白名单，不直接序列化 ORM 对象**：内部自增 id、`tenant_id`、
`project_id`、密文 `encrypted_secret`、主密钥版本 `encryption_key_version` 都不会因为
表多了一列而静默出现在响应里。

⚠️ `secret` 只在建凭据与轮换的响应里（§36「Never expose secret again after initial
creation」）。它是 `SecretStr`：`repr` / `str` 只显示掩码，只有序列化响应时才取出原值 ——
所以它不会顺着异常信息或调试输出进日志（设计 §6）。
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    field_serializer,
)

from app.models.integration import CredentialStatus, IntegrationCredential

# 去掉首尾空白后长度 1–255：写进审计的 `reason` 列（§66）。只写业务说明，不写个人数据。
_Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
# 严格整数：`"1"`、`1.0`、`true` 都是 422。上界是 INT 列的上界。
_KeyVersion = Annotated[int, Field(strict=True, ge=1, le=2**31 - 1)]


class CreateCredentialRequest(BaseModel):
    """请求体就是 `{}`：`api_key`、`secret`、版本号都由服务端生成。"""

    model_config = ConfigDict(extra="forbid")


class RotateCredentialRequest(BaseModel):
    """`current_key_version` 是调用方看到的最新版本号；与锁内读到的不同就 409。"""

    model_config = ConfigDict(extra="forbid")

    current_key_version: _KeyVersion


class RevokeCredentialRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: _Reason


class CredentialView(BaseModel):
    """一个凭据版本。`verifiable` 按响应那一刻算，存储里没有「已过期」。"""

    api_key: str
    key_version: int
    status: str
    valid_from: dt.datetime
    valid_until: dt.datetime | None
    last_used_at: dt.datetime | None
    created_at: dt.datetime
    revoked_at: dt.datetime | None
    verifiable: bool


class IssuedCredentialView(CredentialView):
    """建凭据与轮换的响应：凭据版本多一个 `secret`，这是它唯一一次离开服务端。"""

    secret: SecretStr

    @field_serializer("secret")
    def _reveal(self, value: SecretStr) -> str:
        # 只在序列化响应时取值；repr / str 仍是掩码。
        return value.get_secret_value()


def _fields(row: IntegrationCredential, *, verifiable: bool) -> dict[str, object]:
    return {
        "api_key": row.public_api_key,
        "key_version": row.key_version,
        "status": CredentialStatus(row.status).value,
        "valid_from": row.valid_from,
        "valid_until": row.valid_until,
        "last_used_at": row.last_used_at,
        "created_at": row.created_at,
        "revoked_at": row.revoked_at,
        "verifiable": verifiable,
    }


def credential_view(row: IntegrationCredential, *, verifiable: bool) -> CredentialView:
    return CredentialView.model_validate(_fields(row, verifiable=verifiable))


def issued_credential_view(
    row: IntegrationCredential, *, secret: str, verifiable: bool
) -> IssuedCredentialView:
    return IssuedCredentialView.model_validate(
        {**_fields(row, verifiable=verifiable), "secret": SecretStr(secret)}
    )
