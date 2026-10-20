"""Login, refresh and logout (spec §53, §66; design gate Issue #32 v5).

**贯穿本模块的一条规则**：每一次「只能发生一次」的状态置位，判定都来自一条带
完整前置条件的 `UPDATE` 的受影响行数。任何「先 SELECT 判断、再 UPDATE 执行」
都不合规 —— 中间那一瞬就是 TOCTOU 窗口。副作用（发令牌、改密码）只允许发生在
受影响行数为 1 的那一支里，且与该 UPDATE 同事务。

设计闸门上这条规则被反复漏用了三次（TOTP、恢复码、2FA 状态置位），所以这里把它
写在最前面，而不是散在各个函数的注释里。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any, NoReturn

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.errors import AppError
from app.core.passwords import spend_dummy_verification, verify_password
from app.core.tokens import (
    CLAIM_PASSWORD_VERSION,
    TOKEN_TYPE_ACCESS,
    TOKEN_TYPE_PENDING_2FA,
    decode_token,
    generate_refresh_token,
    hash_refresh_token,
    issue_access_token,
    issue_pending_2fa_token,
)
from app.models.auth import AuditAction, AuditLog, RefreshToken, User, UserRole, UserStatus

logger = logging.getLogger(__name__)

# 递增锁定的倍数（设计 v5：15 → 30 → 60 分钟，封顶 60）。
# ⚠️ 写成相对倍数而不是写死秒数，是为了 `login_lockout_seconds` 仍然是唯一的
# 基准旋钮 —— 两处各配一份，早晚有一处被改而另一处没有。
_LOCKOUT_LADDER = (1, 2, 4)


def _lockout_seconds(settings: Settings, level: int) -> int:
    """How long this lockout lasts, given how many times the account has been locked."""
    index = min(max(level, 1), len(_LOCKOUT_LADDER)) - 1
    return settings.login_lockout_seconds * _LOCKOUT_LADDER[index]


REFRESH_COOKIE_NAME = "billing_refresh"
# cookie 只发给刷新端点：别的端点根本收不到它，XSS 之外的误用面小一圈。
REFRESH_COOKIE_PATH = "/api/v1/auth"


class InvalidCredentials(AppError):
    """Wrong email, wrong password, or a locked account — deliberately indistinguishable.

    ⚠️ 三种情况共用同一个码和同一句文案。分开报会把「这个邮箱存在」
    （以及「它正在被爆破」）免费告诉对方。
    """

    def __init__(self) -> None:
        super().__init__(
            "Invalid email or password.",
            code="INVALID_CREDENTIALS",
            http_status=401,
        )


class TokenReused(AppError):
    """A refresh token was presented twice — treated as theft, not as a retry."""

    def __init__(self) -> None:
        super().__init__(
            "The session has been terminated. Sign in again.",
            code="TOKEN_REUSED",
            http_status=401,
        )


@dataclass(frozen=True)
class IssuedSession:
    access_token: str
    refresh_token: str
    refresh_expires_at: dt.datetime


# 登录第一步的两种「还没完」结果。
STAGE_TOTP_REQUIRED = "TOTP_REQUIRED"
STAGE_ENROL_2FA = "ENROL_2FA"


@dataclass(frozen=True)
class LoginOutcome:
    """What the first step of login produced.

    要么已经拿到会话（客户侧未启用 2FA），要么停在第二因子那一步 —— 后者手上
    只有一张 `pending_token`，**它不是访问令牌**，调不动任何业务端点。
    """

    issued: IssuedSession | None = None
    stage: str | None = None
    pending_token: str | None = None


@dataclass(frozen=True)
class RequestContext:
    """Who is calling, for the audit trail (spec §66)."""

    ip_address: str | None
    user_agent: str | None


def utc_now() -> dt.datetime:
    """Naive UTC, matching how the columns are stored (spec §109)."""
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def token_password_version(payload: dict[str, Any]) -> int:
    """The `password_version` a decoded JWT was issued under (设计闸门 #369 v6 §2).

    ⚠️ 没有 `pwv` 的令牌按 0 处理：部署前签发的令牌在该用户第一次改密码之前
    照常可用（§2「兼容」）。每个使用点都经这里取值，缺省规则只写一份。
    """
    return int(payload.get(CLAIM_PASSWORD_VERSION, 0))


def record_audit(
    session: Session,
    *,
    action: AuditAction,
    context: RequestContext,
    now: dt.datetime,
    actor: User | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    before_state: dict[str, object] | None = None,
    after_state: dict[str, object] | None = None,
    reason: str | None = None,
) -> None:
    """Append one audit row. **Never updates.**

    ⚠️ `before_state` / `after_state` 只接受调用方显式挑好的字段。
    **绝不要把整个 ORM 对象或请求体 dump 进来** —— `password_hash`、令牌、
    将来的 TOTP 密钥都会顺着进去，而审计表是长期保留的。
    """
    session.add(
        AuditLog(
            actor_user_id=actor.id if actor else None,
            actor_role=actor.role.value if actor else None,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            before_state=json.dumps(before_state, sort_keys=True) if before_state else None,
            after_state=json.dumps(after_state, sort_keys=True) if after_state else None,
            ip_address=context.ip_address,
            # 截到列宽：User-Agent 是客户端可控的，不截会让一次插入直接报错。
            user_agent=context.user_agent[:512] if context.user_agent else None,
            reason=reason,
            created_at=now,
        )
    )


def _register_failure(
    session_factory: sessionmaker[Session],
    settings: Settings,
    *,
    user_id: int,
    context: RequestContext,
    now: dt.datetime,
    reason: str,
) -> None:
    """Increment the lockout counter in its **own** transaction.

    ⚠️ 单独提交是刻意的。和请求的其余部分共用一个会被回滚的事务时，一次数据库
    错误就把暴力破解的计数清零了 —— 攻击者只要想办法让后续步骤失败就行。
    """
    with session_factory() as session:
        session.execute(
            update(User)
            .where(User.id == user_id)
            .values(failed_login_count=User.failed_login_count + 1)
        )
        locked = session.execute(select(User).where(User.id == user_id)).scalar_one_or_none()
        if locked is not None and locked.failed_login_count >= settings.login_max_failures:
            # ⚠️ **递增**，不是每次都锁同样的时长。固定时长的话，攻击者每过一个
            # 锁定周期就白拿一轮 5 次猜测，长期看根本挡不住。档位存在
            # `lockout_level` 上，它不随锁到期而清零。
            locked.lockout_level = min(locked.lockout_level + 1, len(_LOCKOUT_LADDER))
            seconds = _lockout_seconds(settings, locked.lockout_level)
            locked.locked_until = now + dt.timedelta(seconds=seconds)
            locked.failed_login_count = 0
            logger.warning(
                "Account locked after repeated failures",
                extra={
                    "user_id": user_id,
                    "lockout_level": locked.lockout_level,
                    "lockout_seconds": seconds,
                },
            )
        record_audit(
            session,
            action=AuditAction.LOGIN_FAILED,
            context=context,
            now=now,
            actor=locked,
            reason=reason,
        )
        session.commit()


def two_factor_confirmed(session: Session, user_id: int) -> bool:
    """True when this account has a **confirmed** TOTP enrolment.

    ⚠️ 在函数里 import 是因为 `app.services.two_factor` 反过来要用本模块的
    `record_audit` / `RequestContext` —— 顶层互相 import 会转不动。
    这是两个模块之间唯一的一处环。
    """
    from app.services.two_factor import is_enrolled

    return is_enrolled(session, user_id)


def _clear_lock_if_expired(session: Session, user: User, now: dt.datetime) -> None:
    """A lock that has run out resets the counter **before** this attempt is judged.

    ⚠️ 不清的话，锁一解开、下一次失败立刻又达阈值，实际锁定时长变成无限。

    ⚠️ **但 `lockout_level` 绝不在这里清零。**清了的话每一轮锁定都是同样的时长，
    攻击者每过一个锁定周期就白拿一轮猜测，递增策略等于没有。它只在**完整认证
    成功**时清零 —— 那才是「这个账号确实是本人在用」的证据。
    """
    if user.locked_until is not None and user.locked_until <= now:
        user.locked_until = None
        user.failed_login_count = 0


def _issue_session(
    session: Session,
    settings: Settings,
    *,
    user: User,
    context: RequestContext,
    now: dt.datetime,
    password_version: int,
    family_id: str | None = None,
) -> IssuedSession:
    """Create one refresh-token row and a matching access token, and audit it.

    ⚠️ 令牌行与审计行**同一个事务**（Invariant 13）。分开提交会出现「发了令牌
    但没有登录记录」或反过来，两种都让审计不可信。

    ⚠️ `password_version` 由调用方传入，**不在这里重新读用户**（设计闸门 #369
    v6 §2「令牌盘点」）：重新读到的可能是改密码之后的新值，会给旧密码换来的
    会话盖上新版本号。它来自验证密码时读到的那一行、被消费的 pending 令牌，
    或被轮换的上一行刷新令牌。
    """
    family = family_id or uuid.uuid4().hex
    raw = generate_refresh_token()
    expires_at = now + dt.timedelta(seconds=settings.refresh_token_ttl_seconds)
    session.add(
        RefreshToken(
            user_id=user.id,
            family_id=family,
            token_hash=hash_refresh_token(raw),
            issued_at=now,
            expires_at=expires_at,
            password_version=password_version,
        )
    )
    access = issue_access_token(
        settings,
        user_id=user.id,
        role=user.role.value,
        session_id=family,
        now=now,
        password_version=password_version,
    )
    return IssuedSession(access_token=access, refresh_token=raw, refresh_expires_at=expires_at)


def authenticate(
    session_factory: sessionmaker[Session],
    settings: Settings,
    *,
    email: str,
    password: str,
    context: RequestContext,
    now: dt.datetime | None = None,
) -> LoginOutcome:
    """First step of login: verify the email/password pair.

    ⚠️ **密码正确不是「认证成功」。**启用了 2FA（或身为必须启用的 ADMIN）时，
    这一步只发一张 `pending_token`，失败计数**不清零** —— 清了的话，知道密码
    但不知道验证码的人可以无限次猜 TOTP。
    """
    moment = now or utc_now()

    with session_factory() as session:
        # 小写后再查：`Admin@x.com` 建的账号必须能用 `admin@x.com` 登录。
        # 这里**不校验格式**——登录时因为「邮箱格式不对」报错等于多一条枚举信号，
        # 而且格式在建账号那一步已经把住了。
        user = session.execute(
            select(User).where(User.email == email.strip().lower())
        ).scalar_one_or_none()

        if user is None:
            # ⚠️ 用户不存在也要花掉一次哈希校验，否则「存在的慢、不存在的快」
            # 本身就是用户枚举通道。配套的资源保护在 ratelimit.py。
            spend_dummy_verification()
            record_audit(
                session,
                action=AuditAction.LOGIN_FAILED,
                context=context,
                now=moment,
                reason="UNKNOWN_EMAIL",
            )
            session.commit()
            raise InvalidCredentials

        _clear_lock_if_expired(session, user, moment)
        locked = user.locked_until is not None and user.locked_until > moment

        if locked or user.status is UserStatus.DISABLED:
            # 仍然跑一次哈希校验：锁定与未锁定的耗时差别同样能被测出来。
            spend_dummy_verification()
            record_audit(
                session,
                action=AuditAction.LOGIN_FAILED,
                context=context,
                now=moment,
                actor=user,
                reason="LOCKED" if locked else "DISABLED",
            )
            session.commit()
            raise InvalidCredentials

        if not verify_password(user.password_hash, password):
            session.commit()  # 先落下解锁后的清零，失败计数由独立事务负责
            _register_failure(
                session_factory,
                settings,
                user_id=user.id,
                context=context,
                now=moment,
                reason="BAD_PASSWORD",
            )
            raise InvalidCredentials

        # ⚠️ 版本号取自 `verify_password` 用的这一行，**现在就记下来**
        # （设计闸门 #369 v6 §2「签发点」）。下面两条 pending 分支先 commit 再签发，
        # commit 会让 ORM 对象过期，之后再读 `user.password_version` 是重新查库 ——
        # 读到的可能是改密码之后的新值，旧密码换来的令牌就盖上了新版本号。
        password_version = user.password_version

        # ⚠️ **密码正确不等于认证成功。**这里**绝不清零失败计数** —— 清了的话，
        # 知道密码但不知道验证码的人可以无限次猜 TOTP，每猜一次都顺手把计数
        # 清掉。清零的唯一触发点是 `_complete_login()`：令牌真的发出去那一刻。
        if two_factor_confirmed(session, user.id):
            session.commit()
            return LoginOutcome(
                stage=STAGE_TOTP_REQUIRED,
                pending_token=issue_pending_2fa_token(
                    settings,
                    user_id=user.id,
                    now=moment,
                    # 已经有验证器了，只需输 6 位数。
                    ttl_seconds=settings.pending_token_ttl_seconds,
                    password_version=password_version,
                ),
            )

        if user.role is UserRole.ADMIN:
            # spec §54：ADMIN 的 2FA 是**强制**的。没注册过就先去注册，
            # 不发访问令牌 —— 否则「强制」只是一句话。
            session.commit()
            return LoginOutcome(
                stage=STAGE_ENROL_2FA,
                pending_token=issue_pending_2fa_token(
                    settings,
                    user_id=user.id,
                    now=moment,
                    # ⚠️ 这条路径要扫码 + 抄下 10 个恢复码，寿命刻意更长
                    # （设计闸门 #37；120 秒实测不够，见 config.py 的说明）。
                    ttl_seconds=settings.enrolment_pending_token_ttl_seconds,
                    password_version=password_version,
                ),
            )

        # CUSTOMER 且未启用 2FA：spec §54 说客户侧「supported」，是否强制由配置
        # 决定（Phase 4 的事）。现在直接发令牌。
        issued = _complete_login(
            session,
            settings,
            user=user,
            context=context,
            now=moment,
            password_version=password_version,
        )
        session.commit()
        return LoginOutcome(issued=issued)


def _complete_login(
    session: Session,
    settings: Settings,
    *,
    user: User,
    context: RequestContext,
    now: dt.datetime,
    password_version: int,
) -> IssuedSession:
    """The one place a session is actually handed out. Caller commits.

    ⚠️ **清零失败计数、锁定与递增档位只发生在这里。**「完整认证成功」的定义是
    令牌真的发出去了，不是「某一个因子过了」—— 把清零放在密码那一步，等于给
    知道密码的人一个无限次猜验证码的通道。

    ⚠️ `lockout_level` 同样只在这里清零。它是递增锁定的锚点：成功登录才是
    「确实是本人」的证据，锁到期不是。
    """
    user.failed_login_count = 0
    user.locked_until = None
    user.lockout_level = 0
    user.last_login_at = now
    user.updated_at = now
    issued = _issue_session(
        session,
        settings,
        user=user,
        context=context,
        now=now,
        password_version=password_version,
    )
    record_audit(
        session,
        action=AuditAction.LOGIN,
        context=context,
        now=now,
        actor=user,
        entity_type="users",
        entity_id=str(user.id),
    )
    return issued


def complete_second_factor(
    session_factory: sessionmaker[Session],
    settings: Settings,
    *,
    pending_token: str,
    code: str,
    context: RequestContext,
    now: dt.datetime | None = None,
) -> tuple[IssuedSession, int]:
    """Second step of login: verify TOTP (or a recovery code) and hand out the session.

    返回 `(会话, 剩余可用恢复码数)`。
    """
    moment = now or utc_now()
    # ⚠️ `expected_type` 必须是 pending —— 拿访问令牌来走这一步就等于跳过第一步。
    payload = decode_token(settings, pending_token, expected_type=TOKEN_TYPE_PENDING_2FA)
    user_id = int(payload["sub"])
    password_version = token_password_version(payload)

    with session_factory() as session:
        # ⚠️ **事务开头就锁用户行**（设计闸门 #369 v6 §2「使用点」）：这一步会消耗
        # 一次性认证材料（TOTP 计数器、恢复码），锁一直持有到签发后提交。
        # `reset_password` 锁的是同一行，两者因此排队 —— 改密码先提交，这里读到
        # 新版本号而拒绝；这里先提交，登录成立，随后改密码让它失效。
        # 加锁顺序是「用户 → 2FA 设置 / 恢复码 → 插入刷新令牌」（§2「统一加锁顺序」）。
        user = session.execute(
            select(User).where(User.id == user_id).with_for_update()
        ).scalar_one_or_none()
        if user is None or user.status is UserStatus.DISABLED:
            raise InvalidCredentials
        if user.password_version != password_version:
            # 与「用户不存在或停用」同一个对外结果。回滚、**什么都不提交**：
            # 不验证第二因子、不发会话，也不落下面那步清过期锁。
            session.rollback()
            raise InvalidCredentials
        _clear_lock_if_expired(session, user, moment)
        if user.locked_until is not None and user.locked_until > moment:
            session.commit()
            raise InvalidCredentials

        from app.services.two_factor import InvalidTotp, verify_second_factor

        try:
            result = verify_second_factor(session, settings, user_id=user_id, code=code, now=moment)
        except InvalidTotp:
            # 先落下解锁后的清零。⚠️ 这次提交同时释放用户行锁，**必须排在**
            # `_register_failure` 之前：那个独立事务要写同一行，持锁时调它会
            # 等待本事务自己的锁（§2「锁与失败计数」）。
            session.commit()
            # ⚠️ TOTP 失败与密码失败**共用同一个计数器**。分开计数只会多一个
            # 能被分别耗尽的额度 —— 知道密码的人照样有无限次机会猜验证码。
            _register_failure(
                session_factory,
                settings,
                user_id=user_id,
                context=context,
                now=moment,
                reason="BAD_SECOND_FACTOR",
            )
            raise

        # 版本号用 pending 令牌上的那个：上面已在锁内核对它等于当前值。
        issued = _complete_login(
            session,
            settings,
            user=user,
            context=context,
            now=moment,
            password_version=password_version,
        )
        session.commit()
        return issued, result.remaining_recovery_codes


def refresh_session(
    session_factory: sessionmaker[Session],
    settings: Settings,
    *,
    raw_token: str,
    context: RequestContext,
    now: dt.datetime | None = None,
) -> IssuedSession:
    """Rotate a refresh token, detecting reuse.

    重放检测的价值在于：攻击者偷到刷新令牌后一旦使用，真正的用户下次刷新就会
    撞上「已使用」，**整条链被吊销**，双方都被踢出去。代价是真用户要重新登录，
    但那远好过两边共用一个会话而谁都不知道。

    ⚠️ 加锁顺序是「用户 → 刷新令牌」，与 `reset_password` 相同（设计闸门 #369 v6
    §2「统一加锁顺序」）。原先先条件更新旧行、插入新行时才因外键检查碰到用户行，
    与重置正好相反，两者并发会死锁。
    """
    moment = now or utc_now()
    token_hash = hash_refresh_token(raw_token)

    with session_factory() as session:
        # 第一次读**不加锁**：只用来拿 `user_id`（不变），加锁读会把顺序变成
        # 「先令牌、后用户」。
        row = session.execute(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        ).scalar_one_or_none()
        if row is None:
            raise TokenReused

        user = session.execute(
            select(User).where(User.id == row.user_id).with_for_update()
        ).scalar_one_or_none()

        # 入口检查：按不加锁读到的行判定。
        _refuse_if_unusable(
            session, settings, row=row, context=context, now=moment, replay_reason="REPLAY_DETECTED"
        )

        if user is None or user.status is UserStatus.DISABLED:
            session.rollback()
            raise TokenReused

        # ⚠️ 判定来自受影响行数，不是上面那次 SELECT。两个请求同时拿着同一个
        # 令牌进来时，恰好一个能把 used_at 从 NULL 改掉。
        marked = session.execute(
            update(RefreshToken)
            .where(
                RefreshToken.id == row.id,
                RefreshToken.used_at.is_(None),
                RefreshToken.revoked_at.is_(None),
            )
            .values(used_at=moment)
        )
        if marked.rowcount != 1:
            # 条件更新落空：加锁重读这一行，与入口检查**同一张判别表**。
            # ⚠️ `populate_existing` 不能少，否则 SQLAlchemy 把 identity map 里的
            # 旧对象原样还回来，重读等于没做（password_reset.py 实测踩过）。
            row = session.execute(
                select(RefreshToken)
                .where(RefreshToken.id == row.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).scalar_one()
            _refuse_if_unusable(
                session, settings, row=row, context=context, now=moment, replay_reason="RACE_LOST"
            )
            # ⚠️ 重读之后仍是「都为空、未过期」：理论上不会出现（条件更新失败却
            # 读到可用的行）。宁可多踢一次也不放行，按重放处理。
            #
            # 第一版竞争失败这一支只 rollback 后抛错、不吊销家族（实现闸门判为
            # 阻断项）：攻击者与真用户并发提交同一个令牌，赢的一方拿到有效的新
            # 令牌而家族没被吊销，重放检测等于没生效。
            _treat_as_replay(session, row=row, context=context, now=moment, reason="RACE_LOST")

        # 版本号在锁住用户行之后比较；不等按「已被吊销」处理：那条链已被改密码
        # 作废，不是重放（§2「刷新令牌不可用时的判别」）。回滚连同上面的标记已用。
        if row.password_version != user.password_version:
            session.rollback()
            raise TokenReused

        # 新行沿用旧行的版本号（使用点刚核对过它等于当前值），不重新读用户。
        issued = _issue_session(
            session,
            settings,
            user=user,
            context=context,
            now=moment,
            password_version=row.password_version,
            family_id=row.family_id,
        )
        session.commit()
        return issued


def _refuse_if_unusable(
    session: Session,
    settings: Settings,
    *,
    row: RefreshToken,
    context: RequestContext,
    now: dt.datetime,
    replay_reason: str,
) -> None:
    """Refuse a refresh token that cannot be rotated; return if it still can.

    入口检查与条件更新失败**共用这一张表**（设计闸门 #369 v6 §2「刷新令牌
    不可用时的判别」）：

    | 行的状态 | 判定 | 副作用 |
    | --- | --- | --- |
    | `used_at` 非空 | 重放或并发抢占 | `_treat_as_replay` |
    | `used_at` 为空、`revoked_at` 非空 | 已被吊销（改密码、登出、整链吊销） | 回滚，无 |
    | 都为空、已过绝对或闲置期限 | 过期 | 回滚，无 |
    | 都为空、未过期 | 可用 | 返回 |

    ⚠️ 「已吊销、从未用过」**不是**重放。原先它也进 `_treat_as_replay`，于是每次
    正常改密码之后另一台设备的下一次刷新、以及登出之后的刷新，都会触发盗用告警。
    对外结果一样是 `TOKEN_REUSED`，只是不写审计、不记 warning、不再吊销整条链。
    """
    if row.used_at is not None:
        _treat_as_replay(session, row=row, context=context, now=now, reason=replay_reason)
    if row.revoked_at is not None:
        session.rollback()
        raise TokenReused
    idle_deadline = row.issued_at + dt.timedelta(seconds=settings.refresh_token_idle_seconds)
    if row.expires_at <= now or idle_deadline <= now:
        session.rollback()
        raise TokenReused


def _treat_as_replay(
    session: Session,
    *,
    row: RefreshToken,
    context: RequestContext,
    now: dt.datetime,
    reason: str,
) -> NoReturn:
    """Revoke the whole family, audit, alert, and refuse.

    ⚠️ **只此一处。**重放有两条发现途径（进来就看到 `used_at` 非空、以及条件
    更新竞争失败），两条的处置必须完全一样。分成两段写过一次，结果其中一段漏了
    吊销家族 —— 而漏掉的那段恰好是攻击者与真用户同时提交时走的那条。
    """
    _revoke_family(session, user_id=row.user_id, family_id=row.family_id, now=now)
    record_audit(
        session,
        action=AuditAction.TOKEN_REUSED,
        context=context,
        now=now,
        actor=session.get(User, row.user_id),
        entity_type="refresh_tokens",
        entity_id=row.family_id,
        reason=reason,
    )
    session.commit()
    # ⚠️ 稳定的告警契约：这是令牌被盗的信号，T0.9 要挂告警。
    logger.warning(
        "Refresh token reuse detected",
        extra={
            "component": "auth",
            "family_id": row.family_id,
            "user_id": row.user_id,
            "reason": reason,
        },
    )
    raise TokenReused


def _revoke_family(session: Session, *, user_id: int, family_id: str, now: dt.datetime) -> int:
    """Revoke every row in one login's token family. Returns how many were still live."""
    result = session.execute(
        update(RefreshToken)
        .where(
            RefreshToken.user_id == user_id,
            RefreshToken.family_id == family_id,
            RefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    return int(result.rowcount)


def logout(
    session_factory: sessionmaker[Session],
    *,
    raw_token: str,
    context: RequestContext,
    now: dt.datetime | None = None,
) -> None:
    """Revoke the whole family behind this refresh token.

    ⚠️ 幂等：令牌不存在或已吊销都照样返回成功。登出失败没有任何有用的语义 ——
    用户能做的只有再点一次，而那一次同样会失败。
    """
    moment = now or utc_now()
    with session_factory() as session:
        row = session.execute(
            select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(raw_token))
        ).scalar_one_or_none()
        if row is None:
            return
        _revoke_family(session, user_id=row.user_id, family_id=row.family_id, now=moment)
        record_audit(
            session,
            action=AuditAction.LOGOUT,
            context=context,
            now=moment,
            actor=session.get(User, row.user_id),
            entity_type="refresh_tokens",
            entity_id=row.family_id,
        )
        session.commit()


__all__ = [
    "REFRESH_COOKIE_NAME",
    "REFRESH_COOKIE_PATH",
    "STAGE_ENROL_2FA",
    "STAGE_TOTP_REQUIRED",
    "TOKEN_TYPE_ACCESS",
    "InvalidCredentials",
    "IssuedSession",
    "LoginOutcome",
    "RequestContext",
    "TokenReused",
    "authenticate",
    "complete_second_factor",
    "logout",
    "record_audit",
    "refresh_session",
    "token_password_version",
    "two_factor_confirmed",
    "utc_now",
]
