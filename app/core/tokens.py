"""Access tokens (JWT) and opaque refresh tokens (spec §53).

两种令牌**故意用两套机制**：

- 访问令牌是 JWT：校验只要验签，不查库，所以每个请求都便宜。代价是**签发之后
  无法收回**，只能等它过期 —— 所以寿命很短。
- 刷新令牌是不透明随机串，落库：可以立刻吊销，代价是每次刷新一次查库。而刷新
  本来就不频繁。

把这两点反过来做（长寿命 JWT、或每请求查库的访问令牌）都能跑，但一个牺牲吊销
能力、一个牺牲吞吐。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import secrets
import uuid
from pathlib import Path
from typing import Any, Final

import jwt

from app.core.config import Settings
from app.core.errors import AppError

logger = logging.getLogger(__name__)

ALGORITHM: Final = "HS256"

# 令牌用途。⚠️ **必须校验**：2FA 的 pending 令牌如果能当访问令牌用，第二个
# 因子就等于不存在。所以每个令牌都带 typ，验的时候指定期望值。
TOKEN_TYPE_ACCESS: Final = "access"
TOKEN_TYPE_PENDING_2FA: Final = "2fa_pending"

# 刷新令牌的熵。256 位随机串没有字典可猜。
_REFRESH_TOKEN_BYTES = 32


class AuthNotConfigured(AppError):
    """No signing key is configured, so this deployment cannot issue tokens."""

    def __init__(self) -> None:
        super().__init__(
            "Authentication is not configured.",
            code="AUTH_NOT_CONFIGURED",
            http_status=503,
        )


class InvalidToken(AppError):
    """The token is missing, malformed, expired, or of the wrong type."""

    def __init__(self, message: str = "The token is invalid or has expired."):
        super().__init__(message, code="TOKEN_INVALID", http_status=401)


def load_signing_key(settings: Settings) -> str:
    """Read the signing key from the file named by settings.

    ⚠️ 从**文件**读，不从环境变量读（ADR-0004 第 2 节）。
    ⚠️ 没配时抛异常，**不临时生成一个**：临时密钥会让「忘了配」变成静默的，
    而且每次重启都让所有已签发令牌失效 —— 那种故障看起来像「随机掉登录」。
    """
    path = settings.jwt_secret_file.strip()
    if not path:
        raise AuthNotConfigured
    try:
        key = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        # ⚠️ 不把路径写进异常消息：它会进 §107 的错误响应。
        logger.error("Could not read the JWT signing key file", exc_info=True)
        raise AuthNotConfigured from None
    if not key:
        logger.error("The JWT signing key file is empty")
        raise AuthNotConfigured
    return key


def _epoch(moment: dt.datetime) -> int:
    """Seconds since the epoch, treating a naive datetime as UTC.

    ⚠️ **这个函数存在的理由是一个会静默出错的坑。**

    数据库列按 §109 存 naive UTC，所以 `utc_now()` 返回的是 naive 值。而
    `datetime.timestamp()` 对 naive 值的解释是**本机时区**，不是 UTC ——
    在 UTC+8 的机器上，一个 naive UTC 时刻会被算成 8 小时之前的时间戳，
    于是签出的令牌 `exp` 落在过去：**一签出就是过期的**。

    最坏的地方是它在 UTC 的服务器上完全正常。开发机与生产机时区不同时，
    这类缺陷只在其中一边出现，而现象（「用户随机掉登录」）指不回原因。
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    return int(moment.timestamp())


def issue_access_token(
    settings: Settings, *, user_id: int, role: str, session_id: str, now: dt.datetime
) -> str:
    """Sign a short-lived access token."""
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "role": role,
        # sid 把访问令牌绑到一次登录（刷新令牌家族）上，排障时能把两边对起来。
        "sid": session_id,
        "typ": TOKEN_TYPE_ACCESS,
        "iat": _epoch(now),
        "exp": _epoch(now + dt.timedelta(seconds=settings.access_token_ttl_seconds)),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, load_signing_key(settings), algorithm=ALGORITHM)


def issue_pending_2fa_token(
    settings: Settings, *, user_id: int, now: dt.datetime, ttl_seconds: int
) -> str:
    """Sign the short-lived token that carries a half-finished login (T0.8b uses it).

    ⚠️ `ttl_seconds` **没有默认值，这是刻意的**（设计闸门 #37）。两条路径的寿命
    不同：日常登录 120 秒，ADMIN 首次注册 600 秒。留一个默认值，将来第三个签发点
    漏传就会**安静地**拿到其中一个，而症状是「某条路径偶尔提前失效」——最难查的
    那一类。

    同一条道理已经用在 `decode_token(expected_type=...)` 上：那里漏写默认值会让
    pending 令牌被当成访问令牌接受，是一次完整的第二因子绕过。
    """
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "typ": TOKEN_TYPE_PENDING_2FA,
        "iat": _epoch(now),
        "exp": _epoch(now + dt.timedelta(seconds=ttl_seconds)),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, load_signing_key(settings), algorithm=ALGORITHM)


def decode_token(settings: Settings, token: str, *, expected_type: str) -> dict[str, Any]:
    """Verify a token's signature, expiry **and type**.

    ⚠️ `expected_type` 没有默认值是刻意的：调用方必须写出它期望哪一种。
    给个默认值，早晚有人在校验访问令牌的地方漏写，而 2FA 的 pending 令牌
    就会被当成完整的访问令牌接受 —— 那是一次完整的第二因子绕过。
    """
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            load_signing_key(settings),
            algorithms=[ALGORITHM],
            options={"require": ["exp", "iat", "sub", "typ"]},
        )
    except jwt.PyJWTError:
        # 不把底层原因回给调用方：过期、签名错、格式错在攻击者看来应该一样。
        raise InvalidToken from None
    if payload.get("typ") != expected_type:
        raise InvalidToken
    return payload


def generate_refresh_token() -> str:
    """Return a fresh opaque refresh token (the plaintext the client will hold)."""
    return secrets.token_urlsafe(_REFRESH_TOKEN_BYTES)


def hash_refresh_token(token: str) -> str:
    """Return the value stored in the database.

    ⚠️ 用 SHA-256 而**不是** Argon2id。两个理由，缺一不可：

    1. 它是我们自己生成的 256 位随机串，**没有字典可猜**，慢哈希在这里只换来
       每次刷新一次 KDF 开销。密码相反——那是人选的，必须慢。
    2. **查表必须靠等值索引**，而 Argon2 每次带新盐，同一个令牌两次哈希结果
       不同，根本无法索引。
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
