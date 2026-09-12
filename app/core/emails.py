"""Email address normalisation — one implementation, used everywhere.

⚠️ 存在的理由是一个具体的坑：登录端点用 `EmailStr` 校验，而 bootstrap CLI
如果不校验，就能建出一个**登录端点拒绝的账号** —— 账号存在、密码正确、却永远
登不进去，而且没有任何报错指向真正的原因。

大小写同理：`Admin@x.com` 建的账号，用 `admin@x.com` 登录必须能进。所以入库前
统一小写，查询也用小写。
"""

from __future__ import annotations

from pydantic import TypeAdapter, ValidationError
from pydantic.networks import EmailStr

from app.core.errors import AppError

_adapter = TypeAdapter(EmailStr)


class InvalidEmail(AppError):
    def __init__(self) -> None:
        super().__init__(
            "That is not a valid email address.", code="INVALID_EMAIL", http_status=400
        )


def normalise_email(raw: str) -> str:
    """Validate and lower-case, or raise :class:`InvalidEmail`."""
    candidate = raw.strip()
    try:
        _adapter.validate_python(candidate)
    except ValidationError:
        raise InvalidEmail from None
    return candidate.lower()
