"""TOTP enrolment and verification (spec §54; design gate Issue #32 v5).

**贯穿本模块的一条规则**，和 `app/services/auth.py` 是同一条：每一次「只能发生
一次」的状态置位，判定都来自一条带完整前置条件的 `UPDATE` 的受影响行数。
先 `SELECT` 判断再 `UPDATE` 执行 —— 中间那一瞬就是 TOCTOU 窗口。

设计闸门上这条规则被漏用了三次（TOTP 计数器、恢复码、以及状态置位本身），
所以这里把受它管辖的位置**列在一处**：

| 只能发生一次的事 | 条件 |
| --- | --- |
| 一个 TOTP 码只能用一次 | `last_used_counter IS NULL OR last_used_counter < :counter` |
| 一个恢复码只能用一次 | `used_at IS NULL AND revoked_at IS NULL` |
| 一份注册只能确认一次 | `confirmed_at IS NULL AND secret_version = :version` |
| 已确认的注册不能被覆盖 | `confirmed_at IS NULL` |
"""

from __future__ import annotations

import datetime as dt
import logging
import secrets
from dataclasses import dataclass

import pyotp
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.crypto import decrypt_secret, encrypt_secret, load_keyring
from app.core.errors import AppError
from app.core.passwords import hash_password, verify_password
from app.models.auth import AuditAction, RecoveryCode, TwoFactorSetting, User
from app.services.auth import RequestContext, record_audit, utc_now

logger = logging.getLogger(__name__)

ISSUER = "Acuven Billing"
RECOVERY_CODE_COUNT = 10
# 恢复码要人抄下来，所以用无歧义的字母表：去掉 0/O、1/I/l。
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_CODE_GROUPS = 3
_CODE_GROUP_LENGTH = 4
# 剩下这么多或更少时，在响应里带一个提醒。
LOW_RECOVERY_CODE_WARNING = 2
# TOTP 允许前后各一格（30 秒）的时钟偏差。再宽就等于把验证码的有效期拉长。
TOTP_VALID_WINDOW = 1


class TwoFactorAlreadyEnrolled(AppError):
    def __init__(self) -> None:
        super().__init__(
            "Two-factor authentication is already enabled for this account.",
            code="ALREADY_ENROLLED",
            http_status=409,
        )


class TwoFactorNotEnrolled(AppError):
    def __init__(self) -> None:
        super().__init__(
            "Two-factor authentication has not been set up.",
            code="NOT_ENROLLED",
            http_status=409,
        )


class InvalidTotp(AppError):
    """⚠️ 错误的验证码与用过的恢复码共用同一个码 —— 区分它们只对攻击者有价值。"""

    def __init__(self) -> None:
        super().__init__(
            "That verification code is not valid.",
            code="INVALID_TOTP",
            http_status=401,
        )


@dataclass(frozen=True)
class Enrolment:
    secret: str
    otpauth_uri: str
    secret_version: int


def generate_recovery_code() -> str:
    groups = (
        "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_GROUP_LENGTH))
        for _ in range(_CODE_GROUPS)
    )
    return "-".join(groups)


def is_enrolled(session: Session, user_id: int) -> bool:
    """⚠️ `PENDING`（`confirmed_at IS NULL`）一律算**未启用**。

    算成已启用的话，「生成了密钥但没扫码确认」会把用户锁在门外 —— 他既进不去，
    也没法重新注册。
    """
    row = session.get(TwoFactorSetting, user_id)
    return row is not None and row.confirmed_at is not None


def start_enrolment(
    session_factory: sessionmaker[Session], settings: Settings, *, user_id: int
) -> Enrolment:
    """Generate a fresh TOTP secret and store it as `PENDING`.

    ⚠️ 返回的明文密钥（与内含它的 `otpauth://` URI）是**它唯一一次离开服务端**。
    之后库里只有密文，服务端也不再回显。
    """
    keyring = load_keyring(settings)
    secret = pyotp.random_base32()
    encrypted, key_version = encrypt_secret(keyring, secret)
    moment = utc_now()

    with session_factory() as session:
        user = session.get(User, user_id)
        if user is None:
            raise TwoFactorNotEnrolled

        existing = session.get(TwoFactorSetting, user_id)
        if existing is None:
            row = TwoFactorSetting(
                user_id=user_id,
                encrypted_totp_secret=encrypted,
                key_version=key_version,
                secret_version=1,
                created_at=moment,
                updated_at=moment,
            )
            session.add(row)
            session.commit()
            version = 1
        else:
            # ⚠️ 条件更新：`confirmed_at IS NULL`。已确认的注册**不能被覆盖** ——
            # 否则任何能调这个端点的人都可以把别人已启用的 2FA 降级回 PENDING。
            result = session.execute(
                update(TwoFactorSetting)
                .where(
                    TwoFactorSetting.user_id == user_id,
                    TwoFactorSetting.confirmed_at.is_(None),
                )
                .values(
                    encrypted_totp_secret=encrypted,
                    key_version=key_version,
                    secret_version=TwoFactorSetting.secret_version + 1,
                    # 换了密钥，旧密钥的重放计数器没有意义了。
                    last_used_counter=None,
                    updated_at=moment,
                )
            )
            if result.rowcount != 1:
                session.rollback()
                raise TwoFactorAlreadyEnrolled
            session.commit()
            version = existing.secret_version + 1

    return Enrolment(
        secret=secret,
        otpauth_uri=pyotp.TOTP(secret).provisioning_uri(name=f"user-{user_id}", issuer_name=ISSUER),
        secret_version=version,
    )


def confirm_enrolment(
    session_factory: sessionmaker[Session],
    settings: Settings,
    *,
    user_id: int,
    code: str,
    context: RequestContext,
) -> list[str]:
    """Verify the first code, mark the enrolment confirmed, and mint recovery codes.

    ⚠️ **确认与生成恢复码必须在同一个事务里，且只发生在条件更新命中的那一支。**
    并发两个 `confirm` 各生成 10 个的话，用户手上会有 20 个有效码 —— 而他只
    抄了 10 个，另外 10 个躺在库里没人知道。
    """
    keyring = load_keyring(settings)
    moment = utc_now()

    with session_factory() as session:
        row = session.get(TwoFactorSetting, user_id)
        if row is None:
            raise TwoFactorNotEnrolled
        if row.confirmed_at is not None:
            raise TwoFactorAlreadyEnrolled

        observed_version = row.secret_version
        secret = decrypt_secret(keyring, row.encrypted_totp_secret)
        if not pyotp.TOTP(secret).verify(code.strip(), valid_window=TOTP_VALID_WINDOW):
            raise InvalidTotp

        # ⚠️ 绑定 `secret_version`：校验之后、置位之前若有人重新 `enrol` 换了
        # 密钥，这次确认必须落空 —— 否则会把一个**从未被验证过的密钥**标成
        # 已确认，用户手上的验证器从此对不上。
        result = session.execute(
            update(TwoFactorSetting)
            .where(
                TwoFactorSetting.user_id == user_id,
                TwoFactorSetting.confirmed_at.is_(None),
                TwoFactorSetting.secret_version == observed_version,
            )
            .values(confirmed_at=moment, updated_at=moment)
        )
        if result.rowcount != 1:
            session.rollback()
            raise TwoFactorAlreadyEnrolled

        codes = _replace_recovery_codes(session, user_id=user_id, now=moment)
        record_audit(
            session,
            action=AuditAction.TWO_FACTOR_ENABLED,
            context=context,
            now=moment,
            actor=session.get(User, user_id),
            entity_type="two_factor_settings",
            entity_id=str(user_id),
        )
        session.commit()
        return codes


def regenerate_recovery_codes(
    session_factory: sessionmaker[Session],
    *,
    user_id: int,
    context: RequestContext,
) -> list[str]:
    """Revoke every live code and issue a fresh batch."""
    moment = utc_now()
    with session_factory() as session:
        if not is_enrolled(session, user_id):
            raise TwoFactorNotEnrolled
        codes = _replace_recovery_codes(session, user_id=user_id, now=moment)
        record_audit(
            session,
            action=AuditAction.RECOVERY_CODES_REGENERATED,
            context=context,
            now=moment,
            actor=session.get(User, user_id),
            entity_type="recovery_codes",
            entity_id=str(user_id),
        )
        session.commit()
        return codes


def _replace_recovery_codes(session: Session, *, user_id: int, now: dt.datetime) -> list[str]:
    """Revoke the live codes and insert a new batch. Caller commits.

    ⚠️ 先作废再插入，保证**任何时刻至多 10 个可用** —— 不作废的话，重新生成
    一次就多十个有效码，而用户以为旧的已经失效了。
    """
    session.execute(
        update(RecoveryCode)
        .where(
            RecoveryCode.user_id == user_id,
            RecoveryCode.used_at.is_(None),
            RecoveryCode.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    codes = [generate_recovery_code() for _ in range(RECOVERY_CODE_COUNT)]
    for code in codes:
        session.add(
            RecoveryCode(
                user_id=user_id,
                # Argon2id：恢复码等价于第二因子本身。
                code_hash=hash_password(code),
                created_at=now,
            )
        )
    return codes


@dataclass(frozen=True)
class SecondFactorResult:
    used_recovery_code: bool
    remaining_recovery_codes: int


def verify_second_factor(
    session: Session,
    settings: Settings,
    *,
    user_id: int,
    code: str,
    now: dt.datetime,
) -> SecondFactorResult:
    """Accept a TOTP code **or** a recovery code. Caller owns the transaction.

    ⚠️ 调用方持有事务，是因为「第二因子通过」必须和「发令牌 + 写审计 + 清零
    失败计数」原子提交（Invariant 13）。
    """
    row = session.get(TwoFactorSetting, user_id)
    if row is None or row.confirmed_at is None:
        raise TwoFactorNotEnrolled

    candidate = code.strip()
    if _consume_totp(session, settings, row=row, code=candidate, now=now):
        return SecondFactorResult(
            used_recovery_code=False,
            remaining_recovery_codes=_count_live_codes(session, user_id),
        )
    if _consume_recovery_code(session, user_id=user_id, code=candidate, now=now):
        logger.warning(
            # 用恢复码登录意味着验证器丢了 —— 值得被看见，但不是错误。
            "Signed in with a recovery code",
            extra={"component": "auth", "user_id": user_id},
        )
        return SecondFactorResult(
            used_recovery_code=True,
            remaining_recovery_codes=_count_live_codes(session, user_id),
        )
    raise InvalidTotp


def _consume_totp(
    session: Session, settings: Settings, *, row: TwoFactorSetting, code: str, now: dt.datetime
) -> bool:
    secret = decrypt_secret(load_keyring(settings), row.encrypted_totp_secret)
    totp = pyotp.TOTP(secret)

    counter = _matching_counter(totp, code, now)
    if counter is None:
        return False

    # ⚠️ 判定来自受影响行数。先读 `last_used_counter` 再比较的话，同一个码并发
    # 提交两次会**都通过**，于是一个验证码换出两条会话。
    result = session.execute(
        update(TwoFactorSetting)
        .where(
            TwoFactorSetting.user_id == row.user_id,
            (TwoFactorSetting.last_used_counter.is_(None))
            | (TwoFactorSetting.last_used_counter < counter),
        )
        .values(last_used_counter=counter, updated_at=now)
    )
    return result.rowcount == 1


def _matching_counter(totp: pyotp.TOTP, code: str, now: dt.datetime) -> int | None:
    """Which time step this code belongs to, or None if it matches no accepted step.

    ⚠️ **必须记「实际匹配到的那一格」，不能记「当前这一格」。**

    我们容忍前后各一格的时钟偏差。记当前格的话会留下一个重放窗口：用户提交了
    `n+1` 格的码 → 通过，计数器记成 `n`；30 秒后 `n+1` 变成当前格，**同一个码
    再提交一次，`n < n+1` 条件成立，于是又通过一次**。一个验证码能用两次。

    这个洞在「同一格内重放」的用例里看不见 —— 那条用例用的是当前格的码，
    记当前格恰好是对的。实现闸门第一轮就是在这里被挡下的。
    """
    current = int(now.replace(tzinfo=dt.UTC).timestamp()) // totp.interval
    for offset in range(-TOTP_VALID_WINDOW, TOTP_VALID_WINDOW + 1):
        candidate = current + offset
        at = dt.datetime.fromtimestamp(candidate * totp.interval, tz=dt.UTC)
        # valid_window=0：只比这一格，不再展开，否则又分不清是哪一格匹配的。
        if totp.verify(code, for_time=at, valid_window=0):
            return candidate
    return None


def _consume_recovery_code(session: Session, *, user_id: int, code: str, now: dt.datetime) -> bool:
    """⚠️ 哈希带盐，**没法等值索引** —— 只能取出该用户还活着的码逐个 verify。

    每人至多 10 个，开销可以忽略。命中之后再做条件更新，判定仍来自受影响行数。
    """
    live = session.execute(
        select(RecoveryCode).where(
            RecoveryCode.user_id == user_id,
            RecoveryCode.used_at.is_(None),
            RecoveryCode.revoked_at.is_(None),
        )
    ).scalars()
    for candidate in live:
        if not verify_password(candidate.code_hash, code):
            continue
        result = session.execute(
            update(RecoveryCode)
            .where(
                RecoveryCode.id == candidate.id,
                RecoveryCode.used_at.is_(None),
                RecoveryCode.revoked_at.is_(None),
            )
            .values(used_at=now)
        )
        return result.rowcount == 1
    return False


def _count_live_codes(session: Session, user_id: int) -> int:
    return len(
        session.execute(
            select(RecoveryCode.id).where(
                RecoveryCode.user_id == user_id,
                RecoveryCode.used_at.is_(None),
                RecoveryCode.revoked_at.is_(None),
            )
        )
        .scalars()
        .all()
    )
