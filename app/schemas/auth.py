"""Request and response shapes for the auth endpoints.

⚠️ **响应模型是独立定义的，不直接序列化 ORM 对象。**直接序列化会让
`password_hash`、`failed_login_count`、`locked_until` 这些内部字段跟着出去 ——
而且是在某人给模型加了一列之后**静默**发生。
"""

from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field

from app.core.passwords import MAX_PASSWORD_LENGTH


class LoginRequest(BaseModel):
    email: EmailStr
    # ⚠️ 上限在这里也钉一次：让超长输入在进 Argon2 之前就被挡掉。
    # 下限**不在这里查** —— 登录时报「密码太短」等于告诉对方密码的长度下限，
    # 而且真实用户的旧密码可能短于现行策略。强度只在设置密码时查。
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)


class LoginResponse(BaseModel):
    """刷新令牌**不在这里** —— 它走 httpOnly cookie，JS 读不到。

    三种可能的形状，用 `stage` 区分：

    - `stage=None` + `access_token`：已经登进去了
    - `stage="TOTP_REQUIRED"` + `pending_token`：去输验证码
    - `stage="ENROL_2FA"` + `pending_token`：ADMIN 还没注册 2FA，先去注册

    ⚠️ `pending_token` **不是**访问令牌，调不动任何业务端点（令牌里的 `typ`
    不同，校验时会被拒）。
    """

    access_token: str | None = None
    token_type: str | None = None
    expires_in: int | None = None
    stage: str | None = None
    pending_token: str | None = None
    # 剩余可用恢复码。少于阈值时前端该提醒用户重新生成。
    recovery_codes_remaining: int | None = None


class SecondFactorRequest(BaseModel):
    pending_token: str = Field(min_length=1, max_length=4096)
    # TOTP 是 6 位数字，恢复码是 `XXXX-XXXX-XXXX` —— 同一个字段收两种，
    # 因为对用户来说它们是同一件事：「证明你是你」。
    code: str = Field(min_length=1, max_length=64)


class EnrolRequest(BaseModel):
    """注册 2FA 时手上只有 pending_token（ADMIN 首次登录就在这一步）。"""

    pending_token: str = Field(min_length=1, max_length=4096)


class EnrolResponse(BaseModel):
    """⚠️ 这是明文密钥**唯一一次**离开服务端。之后库里只有密文。"""

    secret: str
    otpauth_uri: str


class ConfirmEnrolRequest(EnrolRequest):
    code: str = Field(min_length=1, max_length=64)


class RecoveryCodesResponse(BaseModel):
    """⚠️ 恢复码明文**唯一一次**返回。之后库里只有 Argon2id 哈希。"""

    recovery_codes: list[str]


class ForgotPasswordRequest(BaseModel):
    """⚠️ 响应对任何输入都一模一样 —— 这里收什么不改变外部可观测的结果。"""

    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=1, max_length=512)
    # ⚠️ 上限在这里钉一次，让超长输入在进 Argon2 之前就被挡掉。
    # **下限刻意不在这里查**：强度规则（长度、弱口令表、不得等于账号名）在
    # `validate_password_strength` 一处，分两处写早晚会对不上，而对不上的那一半
    # 会**安静地**放行一个不该放行的密码。
    new_password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)


class RegenerateRecoveryCodesRequest(BaseModel):
    """重新生成恢复码要**重新验证密码**。

    ⚠️ 光有访问令牌不够：一张被偷走的访问令牌就能换出十个新的第二因子，
    那等于把 2FA 绕过了。
    """

    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)
