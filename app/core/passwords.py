"""Password hashing and strength rules (spec §53).

**这里是密码规则的唯一实现。**注册、bootstrap CLI、重置密码三条路径都调它 ——
三处各写一遍，早晚有一处比另外两处宽，而宽的那一处不会报错。
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from app.core.errors import AppError

# ⚠️ 长度上限不是摆设：Argon2 的开销随输入长度走，不设上限等于给一条
# 「发一个 10 MB 的密码」的拒绝服务通道。
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 128

# ⚠️ 刻意**不做组成规则**（不强制大小写 / 数字 / 符号）。NIST SP 800-63B 的
# 结论是组成规则把人推向 `Password1!` 这种可预测模式，实际强度反而下降。
# 真正有效的是长度下限 + 拒绝已知弱口令。

_hasher = PasswordHasher()

# 最小可用的弱口令表。⚠️ 这**不是**完整方案：真正的常见口令表有上万条，
# 归 T0.9 随部署一起带（放进镜像或挂成文件）。现在这一份只保证「最蠢的那些
# 进不来」，并把校验点固定下来，将来换数据源不用改调用方。
_COMMON_PASSWORDS = frozenset(
    {
        "password",
        "password1",
        "password123",
        "passw0rd",
        "123456789012",
        "1234567890123",
        "qwertyuiop12",
        "administrator",
        "letmein12345",
        "welcome12345",
        "acuvenbilling",
        "billingadmin",
        "changeme1234",
    }
)


class WeakPassword(AppError):
    """The supplied password does not meet spec §53's strength requirements."""

    def __init__(self, message: str):
        super().__init__(message, code="WEAK_PASSWORD", http_status=400)


def validate_password_strength(password: str, *, email: str | None = None) -> None:
    """Raise :class:`WeakPassword` when the password is not acceptable.

    `email` 传进来是为了挡「密码就是邮箱本地部分」这种——它在任何弱口令表里
    都查不到，但被猜中的代价和弱口令一样。
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPassword(f"Password must be at least {MIN_PASSWORD_LENGTH} characters long.")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise WeakPassword(f"Password must be at most {MAX_PASSWORD_LENGTH} characters long.")
    if password.lower() in _COMMON_PASSWORDS:
        raise WeakPassword("Password is too common.")
    if email:
        local_part = email.split("@", 1)[0].strip().lower()
        if local_part and password.lower() == local_part:
            raise WeakPassword("Password must not match the account name.")


def hash_password(password: str) -> str:
    """Return the full Argon2id encoded hash.

    编码串里**带着参数**（`$argon2id$v=19$m=...,t=...,p=...$salt$hash`），所以
    以后调高开销参数时，老哈希仍然能验 —— 靠 `needs_rehash()` 在用户下次登录
    成功时就地升级，不需要强制所有人改密码。
    """
    return _hasher.hash(password)


def verify_password(encoded_hash: str, password: str) -> bool:
    """Return True when the password matches. Never raises for a wrong password."""
    try:
        _hasher.verify(encoded_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False
    return True


def needs_rehash(encoded_hash: str) -> bool:
    """True when the stored hash used weaker parameters than the current policy."""
    try:
        return _hasher.check_needs_rehash(encoded_hash)
    except InvalidHashError:
        return True


# ⚠️ 用户不存在时也要跑一次真实的哈希校验，否则「存在的邮箱慢、不存在的快」
# 本身就是一个用户枚举通道。这个常量是拿一个固定口令算出来的哈希占位。
#
# ⚠️ 它同时是一条**资源放大通道**：任意不存在的邮箱都能稳定消耗一次 Argon2。
# 所以按来源的限流（app/core/ratelimit.py + nginx limit_req）不是可选项，
# 是这条控制的配套。设计闸门 Issue #32 第四轮就是在这里被挡下的。
_DUMMY_HASH = _hasher.hash("dummy-password-for-constant-time-login")


def spend_dummy_verification() -> None:
    """Burn one hash verification so a missing account costs the same as a wrong password."""
    verify_password(_DUMMY_HASH, "not-the-dummy-password")
