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
    """刷新令牌**不在这里** —— 它走 httpOnly cookie，JS 读不到。"""

    access_token: str
    token_type: str = "Bearer"
    expires_in: int
