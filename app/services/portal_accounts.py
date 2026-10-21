"""Customer portal accounts: invite, accept, disable, enable, reset 2FA (design gate #368 v10).

设计 §11 任务 B 的后两部分：查看、开通、重发、接受（AIH-TASK-091），停用、启用、重置 2FA
（AIH-TASK-098）。只有服务函数，没有 HTTP 入口（接口归 AIH-TASK-093 与 094）。

⚠️ **会写库的函数各一个事务**（INV-13）：用户行、令牌、`PORTAL_INVITATION_REQUESTED` outbox
行、2FA 行与审计同一事务提交，任何一步失败整体回滚（设计 §2「事务边界」）。

⚠️ **加锁顺序一律「租户 → 用户 → 令牌」**（设计 §4「串行化」、#369「统一加锁顺序」），与改账户
状态、停用、启用一致，不会成环：

- 开通：锁租户行 → 判定 → 插入用户、令牌、outbox、审计。还没有用户行可锁，同一租户的开通靠
  租户行锁排队；判断「该租户有没有门户账号」用普通读，不用 `FOR UPDATE` —— 对不存在的行加锁读
  在 MySQL 上是间隙锁，两个租户各持一把再各自插入就是死锁。
- 重发：锁租户行 → 锁门户用户行 → 作废旧邀请令牌 → 签发新令牌。
- 接受：不加锁读令牌与用户的 `tenant_id` → 锁租户行 → 锁用户行 → 加锁重读令牌 → 条件
  UPDATE 消费（设计 §4「接受邀请的判定顺序」）。
- 停用、启用、重置 2FA：锁租户行 → 锁门户用户行 → 刷新令牌、重置令牌、2FA 行与恢复码。2FA
  注册服务锁同一用户行（`app/services/two_factor.py`），与停用、重置排队。

⚠️ **唯一约束冲突不解析报错文本**（设计 §4「数据库唯一性」）：捕获 `IntegrityError`、回滚，在
新事务里查是哪一条成立；都不成立就原样抛出（500）。

⚠️ 这里**不写日志**。邮箱是个人数据（REQ-PRIV-001），审计的 `after_state` 只放
`tenant_public_id`、`status`（重发另加 `email_changed`），不放邮箱值；令牌原文只进 outbox
payload（设计 §6），不进审计、异常与返回值。
"""

from __future__ import annotations

import datetime as dt
import json
import secrets
from dataclasses import dataclass
from typing import Final

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.database import session_scope
from app.core.emails import normalise_email
from app.core.errors import AppError
from app.core.passwords import hash_password, validate_password_strength
from app.core.tokens import generate_opaque_token, hash_opaque_token
from app.models.auth import (
    AuditAction,
    DomainOutbox,
    OutboxStatus,
    PasswordResetToken,
    RecoveryCode,
    RefreshToken,
    TokenPurpose,
    TwoFactorSetting,
    User,
    UserRole,
    UserStatus,
)
from app.models.tenancy import AccountStatus, Tenant
from app.repositories import tenancy
from app.services.auth import RequestContext, record_audit, two_factor_confirmed, utc_now
from app.services.customers import CustomerNotFound
from app.services.password_reset import AGGREGATE_USERS
from app.services.project_integration import AccountClosed

# outbox 行的事件类型。⚠️ 这个字符串是投递任务查表选模板的键（渲染归 AIH-TASK-094），
# 改名等于让已经躺在 outbox 里的行认不出自己。
EVENT_PORTAL_INVITATION: Final = "PORTAL_INVITATION_REQUESTED"
# 审计的 entity_type 定义在本模块：审计归类的扫描测试只认本模块的字符串常量。
ENTITY_USERS: Final = "users"

# `INVITED` 账号的不可用密码：随机 32 字节秘密的 Argon2id 哈希，秘密随即丢弃（设计 §6「密钥」）。
_UNUSABLE_SECRET_BYTES: Final = 32
# 重置 2FA 的原因：去首尾空白后 1–255 个字符，与接口层的 `_Reason`、
# `audit_logs.reason` 的列宽一致。
_REASON_MAX_LENGTH: Final = 255


class PortalAccountNotFound(AppError):
    """The customer exists but has no portal account."""

    def __init__(self) -> None:
        super().__init__(
            "This customer has no portal account.",
            code="PORTAL_ACCOUNT_NOT_FOUND",
            http_status=404,
        )


class PortalAccountExists(AppError):
    """The customer already has a portal account (spec §52: one per customer).

    ⚠️ 消息里不带邮箱（设计 §4「幂等与重放」）：前端据此刷新状态。
    """

    def __init__(self) -> None:
        super().__init__(
            "This customer already has a portal account.",
            code="PORTAL_ACCOUNT_EXISTS",
            http_status=409,
        )


class EmailInUse(AppError):
    """Another user already signs in with this address (admin-only, so disclosing it is fine)."""

    def __init__(self) -> None:
        super().__init__(
            "This email address is already in use.",
            code="EMAIL_IN_USE",
            http_status=409,
        )


class PortalAccountNotInvited(AppError):
    """Resend is only possible while the account is `INVITED`."""

    def __init__(self) -> None:
        super().__init__(
            "The portal account is not waiting for an invitation.",
            code="PORTAL_ACCOUNT_NOT_INVITED",
            http_status=409,
        )


class InvalidInvitationToken(AppError):
    """Unknown, used, expired, wrong-purpose token, or the account is not `INVITED` — one code.

    ⚠️ 不细分（设计 §6「限流与枚举」）：分开报会告诉对方「这张令牌存在过」。
    """

    def __init__(self) -> None:
        super().__init__(
            "That invitation link is no longer valid. Please ask for a new one.",
            code="INVALID_INVITATION_TOKEN",
            http_status=400,
        )


class PortalAccountAlreadyDisabled(AppError):
    """Disable on an account that is already `DISABLED`."""

    def __init__(self) -> None:
        super().__init__(
            "The portal account is already disabled.",
            code="PORTAL_ACCOUNT_ALREADY_DISABLED",
            http_status=409,
        )


class PortalAccountNotDisabled(AppError):
    """Enable is only possible while the account is `DISABLED`."""

    def __init__(self) -> None:
        super().__init__(
            "The portal account is not disabled.",
            code="PORTAL_ACCOUNT_NOT_DISABLED",
            http_status=409,
        )


class PortalTwoFactorNotEnabled(AppError):
    """Reset 2FA on an account with neither a confirmed nor a pending enrolment."""

    def __init__(self) -> None:
        super().__init__(
            "Two-factor authentication is not set up for this portal account.",
            code="PORTAL_2FA_NOT_ENABLED",
            http_status=409,
        )


class InvalidResetReason(AppError):
    """The reset reason is empty or longer than 255 characters after trimming.

    ⚠️ 消息里不回显原因：它是管理员写的自由文本。
    """

    def __init__(self) -> None:
        super().__init__(
            f"A reason of 1 to {_REASON_MAX_LENGTH} characters is required.",
            code="VALIDATION_ERROR",
            http_status=422,
        )


@dataclass(frozen=True)
class PortalAccountView:
    """The seven fields of the admin GET (design §2「API」). AIH-TASK-094 only serialises it."""

    email: str
    status: UserStatus
    invited_at: dt.datetime | None
    invitation_expires_at: dt.datetime | None
    activated_at: dt.datetime | None
    last_login_at: dt.datetime | None
    two_factor_enabled: bool


@dataclass(frozen=True)
class InvitationIssued:
    """What create / resend return: the view after the commit and the outbox row just written.

    `outbox_id` 只用来立即触发投递（与忘记密码同一做法）；触发失败也无妨，周期恢复会补上
    （INV-14）。
    """

    account: PortalAccountView
    outbox_id: int


def _moment(now: dt.datetime | None) -> dt.datetime:
    """`now` (or the clock) without microseconds.

    ⚠️ MySQL 的 `DATETIME` 只存到秒（设计 §2「邀请邮件」）：不截的话，信里的到期时刻与库里的
    差几分之一秒。`expires_at` 本身是整秒，所以「`expires_at > now`」截与不截结果相同。
    """
    return (now or utc_now()).replace(microsecond=0)


def _require_tenant(session: Session, customer_id: str, *, for_update: bool) -> Tenant:
    tenant = tenancy.get_tenant_by_public_id(session, customer_id, for_update=for_update)
    if tenant is None:
        raise CustomerNotFound
    return tenant


def _portal_user(session: Session, tenant_id: int, *, for_update: bool) -> User | None:
    statement = select(User).where(User.tenant_id == tenant_id)
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    return session.execute(statement).scalar_one_or_none()


def _email_taken(session: Session, address: str) -> bool:
    return session.execute(select(User.id).where(User.email == address)).first() is not None


def _latest_invitation(session: Session, user_id: int) -> PasswordResetToken | None:
    """The newest `INVITATION` token of this user, by `created_at` then `id` (design §2)."""
    return session.execute(
        select(PasswordResetToken)
        .where(
            PasswordResetToken.user_id == user_id,
            PasswordResetToken.purpose == TokenPurpose.INVITATION,
        )
        .order_by(PasswordResetToken.created_at.desc(), PasswordResetToken.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def _view(session: Session, user: User, *, now: dt.datetime) -> PortalAccountView:
    latest = _latest_invitation(session, user.id)
    live = (
        latest is not None
        and user.status is UserStatus.INVITED
        and latest.used_at is None
        and latest.expires_at > now
    )
    return PortalAccountView(
        email=user.email,
        status=user.status,
        invited_at=latest.created_at if latest is not None else None,
        invitation_expires_at=latest.expires_at if live and latest is not None else None,
        activated_at=user.activated_at,
        last_login_at=user.last_login_at,
        two_factor_enabled=two_factor_confirmed(session, user.id),
    )


def _audit_state(tenant: Tenant, user: User, **extra: object) -> dict[str, object]:
    # ⚠️ 字段白名单（设计 §6）：不放邮箱值。
    return {"tenant_public_id": tenant.public_id, "status": user.status.value, **extra}


def _issue_invitation(
    session: Session, settings: Settings, user: User, *, now: dt.datetime
) -> DomainOutbox:
    """Insert one `INVITATION` token and its outbox row; flush so the outbox id is known.

    ⚠️ payload 的 `expires_at` 与令牌行是同一个值（设计 §2「邀请邮件」）：渲染只用它，不读当前
    配置，outbox 重试与配置改动之后信里的到期时刻仍然与令牌一致。
    """
    raw_token = generate_opaque_token()
    expires_at = now + dt.timedelta(seconds=settings.customer_invitation_ttl_seconds)
    session.add(
        PasswordResetToken(
            user_id=user.id,
            token_hash=hash_opaque_token(raw_token),
            expires_at=expires_at,
            created_at=now,
            purpose=TokenPurpose.INVITATION,
        )
    )
    outbox = DomainOutbox(
        event_type=EVENT_PORTAL_INVITATION,
        aggregate_type=AGGREGATE_USERS,
        aggregate_id=str(user.id),
        # 令牌明文只在这里：库里别处只有哈希，没有它信发不出去。
        payload_json=json.dumps(
            {"to": user.email, "token": raw_token, "expires_at": expires_at.isoformat()},
            sort_keys=True,
        ),
        status=OutboxStatus.PENDING,
        attempt_count=0,
        next_retry_at=now,
        created_at=now,
    )
    session.add(outbox)
    session.flush()
    return outbox


def get_portal_account(
    session_factory: sessionmaker[Session], *, customer_id: str, now: dt.datetime | None = None
) -> PortalAccountView:
    """Read-only. Unknown customer: `CustomerNotFound`; no account: `PortalAccountNotFound`."""
    moment = _moment(now)
    with session_factory() as session:
        tenant = _require_tenant(session, customer_id, for_update=False)
        user = _portal_user(session, tenant.id, for_update=False)
        if user is None:
            raise PortalAccountNotFound
        return _view(session, user, now=moment)


def create_portal_account(
    session_factory: sessionmaker[Session],
    settings: Settings,
    *,
    actor: User,
    customer_id: str,
    email: str,
    context: RequestContext,
    now: dt.datetime | None = None,
) -> InvitationIssued:
    """Create the `INVITED` CUSTOMER user, its invitation and the audit, in one commit.

    判定顺序照设计 §4 的前置条件：客户不存在 404 → 租户 `CLOSED` 409 `ACCOUNT_CLOSED` → 已有门户
    账号 409 `PORTAL_ACCOUNT_EXISTS` → 邮箱被任何用户占用 409 `EMAIL_IN_USE`。
    """
    moment = _moment(now)
    address = normalise_email(email)
    # 在事务之外算：一次 Argon2 约 40ms，不必为它占着租户行锁。
    unusable_hash = hash_password(secrets.token_urlsafe(_UNUSABLE_SECRET_BYTES))
    try:
        with session_scope(session_factory) as session:
            tenant = _require_tenant(session, customer_id, for_update=True)
            if tenant.account_status is AccountStatus.CLOSED:
                raise AccountClosed
            # 普通读即可：同一租户的开通在上面的租户行锁上排队，而 InnoDB 的一致性快照在本事务
            # 第一次普通读时才建立，此时排在前面的开通已经提交、看得见。
            if _portal_user(session, tenant.id, for_update=False) is not None:
                raise PortalAccountExists
            if _email_taken(session, address):
                raise EmailInUse
            user = User(
                email=address,
                password_hash=unusable_hash,
                role=UserRole.CUSTOMER,
                status=UserStatus.INVITED,
                tenant_id=tenant.id,
                failed_login_count=0,
                lockout_level=0,
                password_version=0,
                created_at=moment,
                updated_at=moment,
            )
            session.add(user)
            session.flush()
            outbox = _issue_invitation(session, settings, user, now=moment)
            record_audit(
                session,
                action=AuditAction.PORTAL_ACCOUNT_CREATED,
                context=context,
                now=moment,
                actor=actor,
                entity_type=ENTITY_USERS,
                entity_id=str(user.id),
                after_state=_audit_state(tenant, user),
            )
            session.flush()
            issued = InvitationIssued(
                account=_view(session, user, now=moment), outbox_id=int(outbox.id)
            )
    except IntegrityError:
        # 检查之后、插入之前有别的事务提交了同租户账号或同邮箱用户（设计 §4「数据库唯一性」）。
        _raise_create_conflict(session_factory, customer_id, address)
        raise
    return issued


def _raise_create_conflict(
    session_factory: sessionmaker[Session], customer_id: str, address: str
) -> None:
    """Map a unique-constraint failure by looking, in a fresh transaction. Returns if neither holds.

    ⚠️ 这里刻意不调 `_portal_user` / `_email_taken`：映射必须独立于被它兜底的那两次检查。
    """
    with session_factory() as session:
        tenant = tenancy.get_tenant_by_public_id(session, customer_id)
        if tenant is not None:
            existing = session.execute(select(User.id).where(User.tenant_id == tenant.id)).first()
            if existing is not None:
                raise PortalAccountExists from None
        if session.execute(select(User.id).where(User.email == address)).first() is not None:
            raise EmailInUse from None


def resend_invitation(
    session_factory: sessionmaker[Session],
    settings: Settings,
    *,
    actor: User,
    customer_id: str,
    context: RequestContext,
    email: str | None = None,
    now: dt.datetime | None = None,
) -> InvitationIssued:
    """Void every unused invitation of the account, issue a new one; optionally correct the email.

    判定顺序：客户不存在 404 → 租户 `CLOSED` 409 → 未开通 404 → 不是 `INVITED` 409
    `PORTAL_ACCOUNT_NOT_INVITED` → 改邮箱且被占用 409 `EMAIL_IN_USE`。邮箱比较照设计 §4「重发时的
    邮箱比较」：先按登录同一规则归一，与当前邮箱相同就不算改、不做占用检查。
    """
    moment = _moment(now)
    address = normalise_email(email) if email is not None else None
    try:
        with session_scope(session_factory) as session:
            tenant = _require_tenant(session, customer_id, for_update=True)
            if tenant.account_status is AccountStatus.CLOSED:
                raise AccountClosed
            user = _portal_user(session, tenant.id, for_update=True)
            if user is None:
                raise PortalAccountNotFound
            if user.status is not UserStatus.INVITED:
                raise PortalAccountNotInvited
            email_changed = address is not None and address != user.email
            if email_changed and address is not None:
                if _email_taken(session, address):
                    raise EmailInUse
                user.email = address
                user.updated_at = moment
            # 旧链接立即失效（设计 §4「幂等与重放」）。只作废邀请令牌：`INVITED` 本来就不会有
            # 重置令牌，即使有，也不归这里管。
            session.execute(
                update(PasswordResetToken)
                .where(
                    PasswordResetToken.user_id == user.id,
                    PasswordResetToken.purpose == TokenPurpose.INVITATION,
                    PasswordResetToken.used_at.is_(None),
                )
                .values(used_at=moment)
            )
            session.flush()
            outbox = _issue_invitation(session, settings, user, now=moment)
            record_audit(
                session,
                action=AuditAction.PORTAL_INVITATION_RESENT,
                context=context,
                now=moment,
                actor=actor,
                entity_type=ENTITY_USERS,
                entity_id=str(user.id),
                after_state=_audit_state(tenant, user, email_changed=email_changed),
            )
            session.flush()
            issued = InvitationIssued(
                account=_view(session, user, now=moment), outbox_id=int(outbox.id)
            )
    except IntegrityError:
        # 改邮箱撞上 `users.email` 的唯一约束：同样回滚后在新事务里查（设计 §4「数据库唯一性」）。
        if address is not None:
            with session_factory() as session:
                taken = session.execute(select(User.id).where(User.email == address)).first()
            if taken is not None:
                raise EmailInUse from None
        raise
    return issued


def accept_invitation(
    session_factory: sessionmaker[Session],
    *,
    token: str,
    password: str,
    context: RequestContext,
    now: dt.datetime | None = None,
) -> None:
    """Set the first password and move `INVITED` → `ACTIVE` (design §4「接受邀请的判定顺序」).

    错误码只取决于「链接还能不能用」与「租户状态」，不取决于密码的内容：令牌与账号状态先判
    （400 `INVALID_INVITATION_TOKEN`），再看锁住的租户（409 `ACCOUNT_CLOSED`），最后才是强度
    （400 `WEAK_PASSWORD`，不消费令牌）。不自动登录。
    """
    moment = _moment(now)
    token_hash = hash_opaque_token(token)

    with session_scope(session_factory) as session:
        # 1. 不加锁读：只为拿 `user_id` 与 `tenant_id`，两者创建后都不会变。加锁读会把顺序变成
        #    「先令牌」，与下面的「租户 → 用户 → 令牌」相反。
        row = session.execute(
            select(PasswordResetToken).where(PasswordResetToken.token_hash == token_hash)
        ).scalar_one_or_none()
        if row is None:
            raise InvalidInvitationToken
        token_id = row.id
        tenant_id = session.execute(
            select(User.tenant_id).where(User.id == row.user_id)
        ).scalar_one_or_none()
        if tenant_id is None:
            raise InvalidInvitationToken

        # 2. 租户 → 用户 → 令牌，全部加锁读。⚠️ `populate_existing` 不能少：第 1 步可能已把
        #    同一行放进 identity map，没有它，锁读读到的新值会被丢掉、原样还回旧对象。
        #    锁读总是读最新已提交版本，不受第 1 步建立的快照影响。
        tenant = session.execute(
            select(Tenant)
            .where(Tenant.id == tenant_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        user = session.execute(
            select(User)
            .where(User.id == row.user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        row = session.execute(
            select(PasswordResetToken)
            .where(PasswordResetToken.id == token_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one()
        if tenant is None or user is None:
            raise InvalidInvitationToken

        # 3. 链接还能不能用。⚠️ 只认 `INVITATION` 用途（设计 §2「邀请邮件与重置的隔离」）：
        #    漏掉它，一张重置令牌就能拿来给 `INVITED` 账号设密码。
        if (
            row.purpose is not TokenPurpose.INVITATION
            or row.used_at is not None
            or row.expires_at <= moment
            or user.status is not UserStatus.INVITED
        ):
            raise InvalidInvitationToken

        # 4. 看锁住的租户行，不用普通读：关户先提交时这里一定读得到。
        if tenant.account_status is AccountStatus.CLOSED:
            raise AccountClosed

        # 5. 强度排在消费令牌之前：弱密码不烧掉仍然有效的令牌。
        validate_password_strength(password, email=user.email)

        # 6. 判定来自条件 UPDATE 的受影响行数，不是上面的读。
        consumed = session.execute(
            update(PasswordResetToken)
            .where(
                PasswordResetToken.id == token_id,
                PasswordResetToken.purpose == TokenPurpose.INVITATION,
                PasswordResetToken.used_at.is_(None),
                PasswordResetToken.expires_at > moment,
            )
            .values(used_at=moment)
        )
        if int(consumed.rowcount) != 1:
            raise InvalidInvitationToken

        # 7. 本人设置新密码：版本号与新哈希同一事务递增（#369 §2），写成 SQL 表达式只增不减。
        user.password_hash = hash_password(password)
        user.password_version = User.password_version + 1
        user.status = UserStatus.ACTIVE
        user.activated_at = moment
        user.updated_at = moment
        session.execute(
            update(PasswordResetToken)
            .where(
                PasswordResetToken.user_id == user.id,
                PasswordResetToken.id != token_id,
                PasswordResetToken.purpose == TokenPurpose.INVITATION,
                PasswordResetToken.used_at.is_(None),
            )
            .values(used_at=moment)
        )
        record_audit(
            session,
            action=AuditAction.PORTAL_INVITATION_ACCEPTED,
            context=context,
            now=moment,
            actor=user,
            entity_type=ENTITY_USERS,
            entity_id=str(user.id),
            after_state={"tenant_public_id": tenant.public_id, "status": UserStatus.ACTIVE.value},
        )


def _lock_portal_account(session: Session, customer_id: str) -> tuple[Tenant, User]:
    """Lock the tenant row, then the portal user row (design §2「事务边界」).

    客户不存在 404 `CUSTOMER_NOT_FOUND`，未开通 404 `PORTAL_ACCOUNT_NOT_FOUND`。
    """
    tenant = _require_tenant(session, customer_id, for_update=True)
    user = _portal_user(session, tenant.id, for_update=True)
    if user is None:
        raise PortalAccountNotFound
    return tenant, user


def disable_portal_account(
    session_factory: sessionmaker[Session],
    *,
    actor: User,
    customer_id: str,
    context: RequestContext,
    now: dt.datetime | None = None,
) -> PortalAccountView:
    """`INVITED` / `ACTIVE` → `DISABLED`, and end every way back in (design §4 状态表).

    ⚠️ 租户 `CLOSED` 也允许停用（状态表的前置条件是「—」）。已停用 409
    `PORTAL_ACCOUNT_ALREADY_DISABLED`。
    """
    moment = _moment(now)
    with session_scope(session_factory) as session:
        tenant, user = _lock_portal_account(session, customer_id)
        if user.status is UserStatus.DISABLED:
            raise PortalAccountAlreadyDisabled
        session.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=moment)
        )
        # 两种用途都作废：重新启用后旧邀请链接、旧重置链接都不能再用，需要时管理员重发。
        session.execute(
            update(PasswordResetToken)
            .where(PasswordResetToken.user_id == user.id, PasswordResetToken.used_at.is_(None))
            .values(used_at=moment)
        )
        # ⚠️ 版本号 +1（#369）：停用前签发的访问令牌与 pending 令牌永久失效，重新启用也不会
        # 让它们复活。写成 SQL 表达式只增不减。
        user.password_version = User.password_version + 1
        user.status = UserStatus.DISABLED
        user.updated_at = moment
        record_audit(
            session,
            action=AuditAction.PORTAL_ACCOUNT_DISABLED,
            context=context,
            now=moment,
            actor=actor,
            entity_type=ENTITY_USERS,
            entity_id=str(user.id),
            after_state=_audit_state(tenant, user),
        )
        session.flush()
        view = _view(session, user, now=moment)
    return view


def enable_portal_account(
    session_factory: sessionmaker[Session],
    *,
    actor: User,
    customer_id: str,
    context: RequestContext,
    now: dt.datetime | None = None,
) -> PortalAccountView:
    """`DISABLED` → `INVITED` (never accepted) or `ACTIVE` (design §4 状态表).

    判定顺序：客户不存在 404 → 未开通 404 → 不是 `DISABLED` 409 `PORTAL_ACCOUNT_NOT_DISABLED` →
    租户 `CLOSED` 409 `ACCOUNT_CLOSED`。不自动重发邀请：停用时旧邀请已作废，需要时管理员
    再点重发。
    """
    moment = _moment(now)
    with session_scope(session_factory) as session:
        tenant, user = _lock_portal_account(session, customer_id)
        if user.status is not UserStatus.DISABLED:
            raise PortalAccountNotDisabled
        if tenant.account_status is AccountStatus.CLOSED:
            raise AccountClosed
        user.status = UserStatus.INVITED if user.activated_at is None else UserStatus.ACTIVE
        user.updated_at = moment
        record_audit(
            session,
            action=AuditAction.PORTAL_ACCOUNT_ENABLED,
            context=context,
            now=moment,
            actor=actor,
            entity_type=ENTITY_USERS,
            entity_id=str(user.id),
            after_state=_audit_state(tenant, user),
        )
        session.flush()
        view = _view(session, user, now=moment)
    return view


def _clean_reason(reason: str) -> str:
    """Trim, then require 1–255 characters, as the API layer's `_Reason` does."""
    cleaned = reason.strip()
    if not 1 <= len(cleaned) <= _REASON_MAX_LENGTH:
        raise InvalidResetReason
    return cleaned


def reset_portal_two_factor(
    session_factory: sessionmaker[Session],
    *,
    actor: User,
    customer_id: str,
    reason: str,
    context: RequestContext,
    now: dt.datetime | None = None,
) -> PortalAccountView:
    """Delete the account's 2FA (confirmed or pending) and its recovery codes; end its sessions.

    设计 §2「管理员重置客户 2FA」：管理员已在线下核实身份。原因不合格 422 `VALIDATION_ERROR`，
    在任何读写之前；没有 2FA 409 `PORTAL_2FA_NOT_ENABLED`。账号状态不变，不发通知邮件。
    """
    cleaned = _clean_reason(reason)
    moment = _moment(now)
    with session_scope(session_factory) as session:
        tenant, user = _lock_portal_account(session, customer_id)
        # 锁读：读最新已提交版本。客户的确认排在用户行锁上，先提交的那次确认这里一定看得见。
        enrolled = session.execute(
            select(TwoFactorSetting.user_id)
            .where(TwoFactorSetting.user_id == user.id)
            .with_for_update()
        ).first()
        if enrolled is None:
            raise PortalTwoFactorNotEnabled
        session.execute(delete(RecoveryCode).where(RecoveryCode.user_id == user.id))
        session.execute(delete(TwoFactorSetting).where(TwoFactorSetting.user_id == user.id))
        # ⚠️ 版本号 +1（#369）：吊销该用户全部会话，包括抢先注册验证器的人手里的那一个。
        user.password_version = User.password_version + 1
        user.updated_at = moment
        record_audit(
            session,
            action=AuditAction.PORTAL_2FA_RESET,
            context=context,
            now=moment,
            actor=actor,
            entity_type=ENTITY_USERS,
            entity_id=str(user.id),
            # 设计 §4 状态表这一行：只放 `tenant_public_id`。
            after_state={"tenant_public_id": tenant.public_id},
            reason=cleaned,
        )
        session.flush()
        view = _view(session, user, now=moment)
    return view


__all__ = [
    "ENTITY_USERS",
    "EVENT_PORTAL_INVITATION",
    "EmailInUse",
    "InvalidInvitationToken",
    "InvalidResetReason",
    "InvitationIssued",
    "PortalAccountAlreadyDisabled",
    "PortalAccountExists",
    "PortalAccountNotDisabled",
    "PortalAccountNotFound",
    "PortalAccountNotInvited",
    "PortalAccountView",
    "PortalTwoFactorNotEnabled",
    "accept_invitation",
    "create_portal_account",
    "disable_portal_account",
    "enable_portal_account",
    "get_portal_account",
    "reset_portal_two_factor",
    "resend_invitation",
]
