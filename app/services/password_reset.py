"""Forgot password / reset password (spec §53; design gate Issue #32 v5).

`app/services/auth.py` 顶部那条规则在这里同样成立，而且更要紧：**每一次「只能
发生一次」的状态置位，判定都来自一条带完整前置条件的 `UPDATE` 的受影响行数。**
密码重置令牌是单用的 —— 先 SELECT 判断再 UPDATE 的写法，中间那一瞬就是 TOCTOU
窗口，同一张令牌会被两个并发请求各用一次。

**两个端点的非对称是刻意的**：

- `/password/forgot` **永远返回同一个 200**，无论邮箱是否存在、账号是否停用。
  差异化的响应就是一个用户枚举接口 —— 而这个接口不需要任何凭据就能调。
- `/password/reset` 可以明确报「令牌无效或已过期」：调用方手上已经有一张令牌，
  告诉他这张不能用不泄漏任何他还不知道的事。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
from collections.abc import Callable
from typing import Final

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.emails import InvalidEmail, normalise_email
from app.core.errors import AppError
from app.core.passwords import hash_password, validate_password_strength
from app.core.tokens import generate_opaque_token, hash_opaque_token
from app.models.auth import (
    AuditAction,
    DomainOutbox,
    OutboxStatus,
    PasswordResetToken,
    RefreshToken,
    User,
    UserStatus,
)
from app.services.auth import RequestContext, record_audit, utc_now

logger = logging.getLogger(__name__)

# outbox 行的事件类型。⚠️ 这个字符串是**投递任务查表选模板的键**，改它等于让
# 所有已经躺在 outbox 里、还没投出去的行认不出自己是什么 —— 那些行会直接进死信。
EVENT_PASSWORD_RESET: Final = "PASSWORD_RESET_REQUESTED"
AGGREGATE_USERS: Final = "users"

# 「忘记密码」这个端点的固定耗时下限。⚠️ 它是一条**安全控制**，不是性能旋钮：
# 没有它，「这个邮箱存不存在」可以直接从响应耗时读出来（实测两条路径完全不
# 重叠，见 `request_reset` 的说明）。
#
# 取值的依据是实测：这条路径的真实工作约 11ms，一次 Argon2 约 40ms（另一条
# 认证路径的量级）。120ms 在两者之上留了足够余量，又不至于让线程占用太久。
# ⚠️ 调小到接近真实工作耗时，这条控制就悄悄失效了 —— 而且不会有任何报错。
_MIN_DURATION_SECONDS: Final = 0.12


class InvalidResetToken(AppError):
    """The reset token is unknown, already used, or expired — deliberately one code.

    ⚠️ 三种情况共用一个码：分开报会告诉对方「这张令牌存在过」，而令牌是可以
    被暴力枚举的对象。（真实用户看到的处置都一样：重新申请一次。）
    """

    code = "INVALID_RESET_TOKEN"
    http_status = 400

    def __init__(self) -> None:
        super().__init__("That reset link is no longer valid. Please request a new one.")


def request_reset(
    session_factory: sessionmaker[Session],
    settings: Settings,
    *,
    email: str,
    context: RequestContext,
    now: dt.datetime | None = None,
    on_queued: Callable[[int], None] | None = None,
) -> int | None:
    """Start a reset if that account exists. Returns the outbox row id, if any.

    ⚠️ **返回值与 `on_queued` 只有一个用途**：`delay()` 一下投递任务，省掉等周期
    恢复的那最多一分钟。**它们绝不允许影响响应** —— 拿它去改状态码、改文案、
    改耗时，这个端点立刻变回一个用户枚举工具。触发失败也必须当没事发生：
    行已经在库里，周期恢复会补上（Invariant 14）。

    ⚠️ **耗时也必须相当**（设计闸门 #32 §6）。这一条整栈实测踩了两次：

    1. 初版在账号不存在时直接 return：「存在」要三次 INSERT + commit、「不存在」
       只有一次 SELECT。实测 **11ms vs 4.8ms，两组完全不重叠** —— 响应体一致
       救不了它，那是一个可用的枚举通道，限流只降低速率、不消除通道。
    2. 第一次修用了登录路径那招「不存在时烧一次 Argon2」。**方向反了过来**：
       Argon2 约 40ms，比这条路径的真实工作（数据库写入约 11ms）贵得多，
       于是变成 known 11ms / unknown 42ms，照样 100% 可分辨。
       **拿一个比真实工作贵得多的操作去「对齐」，只是把差值翻到另一边。**

    所以这里用**固定的耗时下限**：两条路径都补到同一个值。代价是每个请求占住
    一个线程池线程 `_MIN_DURATION_SECONDS` 那么久 —— 刻意不用 Argon2 来填，
    那会把代价变成 CPU（而 CPU 是我们的，不是攻击者的）。按来源限流是这条控制
    的配套，不是可选项。

    ⚠️ 下限只在真实工作**快于**它时起作用。数据库慢到超过下限时通道会重新出现，
    但那时两条路径都慢，差值相对总耗时被压小。这是固定下限方案的固有局限。
    """
    deadline = time.monotonic() + _MIN_DURATION_SECONDS
    try:
        outbox_id = _start_reset(session_factory, settings, email=email, context=context, now=now)
        if outbox_id is not None and on_queued is not None:
            # ⚠️ **触发必须发生在补齐窗口之内。**放到这个函数之外去调，它那几
            # 毫秒就落在下限外面，于是「存在」又比「不存在」慢一点点 —— 实测
            # 就是这样：补齐之后两条路径仍稳定差约 1ms 且不重叠，来源正是这里。
            # 补齐只要漏掉任何一段可区分的工作，通道就还在，只是更窄。
            on_queued(outbox_id)
        return outbox_id
    finally:
        # ⚠️ `finally`：抛异常的那条路径同样要补齐，否则「哪种输入会让它出错」
        # 又成了一条可测量的差异。
        _pad_until(deadline)


def _pad_until(deadline: float) -> None:
    """Sleep out whatever is left of the floor.

    ⚠️ 用 `sleep` 而不是「再算一次 Argon2」来填：后者把代价变成 CPU，而 CPU 是
    我们的、不是攻击者的。sleep 只占一个线程池线程（FastAPI 把同步端点丢进线程池，
    不会阻塞事件循环）。
    """
    remaining = deadline - time.monotonic()
    if remaining > 0:
        time.sleep(remaining)


def _start_reset(
    session_factory: sessionmaker[Session],
    settings: Settings,
    *,
    email: str,
    context: RequestContext,
    now: dt.datetime | None,
) -> int | None:
    """The actual work. 耗时由调用方补齐，这里不必为对齐做任何无用功。"""
    moment = now or utc_now()
    try:
        address = normalise_email(email)
    except InvalidEmail:
        # ⚠️ 连「这不是个邮箱」都不能说：那同样是一个可区分的响应。
        # 端点层的 `EmailStr` 已经挡过一道，这里是兜底。
        logger.info("Password reset requested for an unparseable address")
        return None

    with session_factory() as session:
        user = session.execute(select(User).where(User.email == address)).scalar_one_or_none()
        if user is None or user.status is not UserStatus.ACTIVE:
            # 停用的账号也不发信：给一个已被停用的账号重置密码，等于给它一条
            # 重新可用的路。⚠️ 但对外的响应仍然一模一样。
            #
            logger.info(
                "Password reset requested for an unknown or inactive account",
                extra={"account_exists": user is not None},
            )
            return None

        raw_token = generate_opaque_token()
        expires_at = moment + dt.timedelta(seconds=settings.password_reset_ttl_seconds)

        # ⚠️ 三件事**同一个事务**（Invariant 13 + 14）：令牌行、outbox 行、审计行。
        # 分开提交会出现「令牌有效但没人知道要发信」或者反过来 —— 前者表现为
        # 用户永远收不到信，后者表现为收到一封链接必然失效的信。
        session.add(
            PasswordResetToken(
                user_id=user.id,
                token_hash=hash_opaque_token(raw_token),
                expires_at=expires_at,
                created_at=moment,
            )
        )
        outbox = DomainOutbox(
            event_type=EVENT_PASSWORD_RESET,
            aggregate_type=AGGREGATE_USERS,
            aggregate_id=str(user.id),
            # ⚠️ payload 里**只放发这封信必需的东西**：收件地址 + 令牌明文。
            # 令牌明文必须在这里 —— 库里别处只有哈希，没有它信就发不出去。
            # 投递成功的那一刻这一列会被置空（见 app/tasks/outbox.py）。
            #
            # ⚠️ 初版还存了 `expires_at`，而渲染邮件时**根本没用它** —— 一个没有
            # 消费方的字段，却躺在这条含敏感材料的 payload 里。这张表是长期保留的，
            # 装进去的每一样东西都要有人真的读。要在信里写明过期时刻是另一件事，
            # 到时候连同模板层一起做（见 `docs/TODO.md` 的 Phase 6 通知模板）。
            payload_json=json.dumps({"to": user.email, "token": raw_token}, sort_keys=True),
            status=OutboxStatus.PENDING,
            attempt_count=0,
            # 立刻可投。
            next_retry_at=moment,
            created_at=moment,
        )
        session.add(outbox)
        record_audit(
            session,
            action=AuditAction.PASSWORD_RESET_REQUESTED,
            context=context,
            now=moment,
            actor=user,
            entity_type="users",
            entity_id=str(user.id),
        )
        # flush 是为了在提交前拿到自增 id；提交之后对象会 expire，再读一次 id
        # 就是又一次查询。
        session.flush()
        outbox_id = int(outbox.id)
        session.commit()
        return outbox_id


def reset_password(
    session_factory: sessionmaker[Session],
    settings: Settings,
    *,
    token: str,
    new_password: str,
    context: RequestContext,
    now: dt.datetime | None = None,
) -> None:
    """Consume the token and set a new password. Raises on anything else.

    ⚠️ **2FA 没有被绕过。**这里换的只是第一个因子；ADMIN 下次登录仍然要过
    spec §54 强制的第二步。拿到邮箱访问权不等于拿到账号。
    """
    del settings  # 寿命在签发时就钉死在行上，这里只看 expires_at。
    moment = now or utc_now()
    token_hash = hash_opaque_token(token)

    with session_factory() as session:
        # ⚠️ 第一次读**刻意不加锁**：它只用来拿 `user_id`，而 `user_id` 是不变的。
        # 加锁读会把加锁顺序变成「先令牌、后用户」，与下面那两条 UPDATE 的顺序
        # 相反 —— 那正是死锁的配方（见下一段）。
        row = session.execute(
            select(PasswordResetToken).where(PasswordResetToken.token_hash == token_hash)
        ).scalar_one_or_none()
        if row is None:
            raise InvalidResetToken
        token_id = row.id

        # ⚠️ **用户级串行化。**没有这一把锁，同一个用户的两条有效链接同时提交时：
        #   A 消费 token1（持 token1 的行锁）→ 去作废 token2
        #   B 消费 token2（持 token2 的行锁）→ 去作废 token1
        # 两边交叉等待，**MySQL 死锁**，其中一条回滚成 500 —— 而正确的结果应该是
        # 一条成功、另一条拿 `INVALID_RESET_TOKEN`。
        #
        # 锁在 `users` 行上，所有该用户的重置因此排成一队；**加锁顺序统一成
        # 「先用户、后令牌」**，不再有环。同一条道理将来也用在钱包上（spec §81）。
        #
        # ⚠️ SQLite 不支持 `FOR UPDATE`，SQLAlchemy 在那个方言下**静默忽略**它 ——
        # 也就是说单元测试**验证不了这一条**，它只能对着真 MySQL 验
        # （见 tests/backend/test_password_reset_concurrency.py）。
        user = session.execute(
            select(User).where(User.id == row.user_id).with_for_update()
        ).scalar_one_or_none()
        if user is None or user.status is not UserStatus.ACTIVE:
            raise InvalidResetToken

        # ⚠️ 拿到用户锁之后**重新读一次令牌**，而且是加锁读。
        # REPEATABLE READ 下，上面那次普通 SELECT 拿到的是事务开始时的快照 ——
        # 排在队里等锁的那个请求，读到的 `used_at` 可能还是 `NULL`。加锁读总是读
        # 最新已提交版本，这才是「它现在还能不能用」的答案。
        #
        # ⚠️ **`populate_existing=True` 不能少。**没有它，这次查询确实发到了数据库，
        # 但 SQLAlchemy 看见 identity map 里已经有同一行的对象，就**原样把旧对象还
        # 给你**，新读到的值被丢掉 —— 于是「重读」这一步看起来做了、实际什么也没
        # 更新。这是实测发现的：并发用例里落败的那条拿到了 `WEAK_PASSWORD`。
        row = session.execute(
            select(PasswordResetToken)
            .where(PasswordResetToken.id == token_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one()

        # ⚠️ **令牌是否还有效，必须在强度校验之前判定。**
        # 反过来的话，一张已经用过或过期的链接配上一个弱密码，拿到的是
        # `WEAK_PASSWORD` —— 而那张链接无论密码多强都不可能成功。错误码于是取决于
        # 密码的内容，「这条链接还能不能用」这个稳定契约就没了。
        if row.used_at is not None or row.expires_at <= moment:
            raise InvalidResetToken

        # 强度校验排在**消费令牌**之前：这样「新密码太弱」不会连一张**仍然有效**的
        # 令牌一起烧掉，用户不必重走一遍收信流程 —— 他什么都没做错。
        validate_password_strength(new_password, email=user.email)

        consumed = session.execute(
            update(PasswordResetToken)
            .where(
                PasswordResetToken.id == token_id,
                PasswordResetToken.used_at.is_(None),
                PasswordResetToken.expires_at > moment,
            )
            .values(used_at=moment)
        )
        if int(consumed.rowcount) != 1:
            # 上面已经在持锁状态下判过一次，正常走不到这里。留着是因为**判定必须
            # 来自条件更新的受影响行数** —— 它是唯一不依赖「读与写之间没人插队」
            # 这个假设的判据（本文件开头那条规则）。
            raise InvalidResetToken

        user.password_hash = hash_password(new_password)
        user.updated_at = moment

        # ⚠️ **锁定一并解除。**旧密码此刻已经作废，继续锁着没有任何安全收益，
        # 只剩下「重置成功却依然登不进去，且界面上看不出原因」这一个后果
        # （锁定状态刻意不对外暴露，见设计闸门 §6）。
        # `lockout_level` 不清 —— 那是历史档位，见 app/services/auth.py。
        user.failed_login_count = 0
        user.locked_until = None

        # ⚠️ **吊销这个用户的全部会话**，不只是某一个 family。
        # 「我怀疑账号被盗所以改了密码」这个动作，如果对攻击者已经拿到的会话
        # 毫无影响，那用户以为自己解决的问题其实一个都没解决。
        session.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=moment)
        )

        # 同一个用户其余还没用的重置令牌一并作废：一次重置只该消耗掉「重置」
        # 这件事本身，收件箱里躺着的上一封信不该还能再改一次密码。
        session.execute(
            update(PasswordResetToken)
            .where(
                PasswordResetToken.user_id == user.id,
                PasswordResetToken.id != token_id,
                PasswordResetToken.used_at.is_(None),
            )
            .values(used_at=moment)
        )

        record_audit(
            session,
            action=AuditAction.PASSWORD_RESET,
            context=context,
            now=moment,
            actor=user,
            entity_type="users",
            entity_id=str(user.id),
        )
        session.commit()


__all__ = [
    "AGGREGATE_USERS",
    "EVENT_PASSWORD_RESET",
    "InvalidResetToken",
    "request_reset",
    "reset_password",
]
