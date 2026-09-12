"""Forgot password / reset password (spec §53; design gate Issue #32 v5).

这些分支的共同点还是**失败方式安静**：一个会泄漏账号是否存在的 200、一张能用
第二次的令牌、一次改完密码却没吊销旧会话的重置 —— 每一种都「功能正常」。
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from sqlalchemy import create_engine, select

from app.core.config import Settings
from app.core.database import create_session_factory
from app.core.passwords import WeakPassword, hash_password, verify_password
from app.core.tokens import hash_opaque_token
from app.models.auth import (
    AuditAction,
    AuditLog,
    DomainOutbox,
    OutboxStatus,
    PasswordResetToken,
    RefreshToken,
    User,
    UserRole,
    UserStatus,
)
from app.models.base import Base
from app.services.auth import RequestContext, utc_now
from app.services.password_reset import (
    _MIN_DURATION_SECONDS,
    EVENT_PASSWORD_RESET,
    InvalidResetToken,
    request_reset,
    reset_password,
)

PASSWORD = "a-perfectly-fine-passphrase"
NEW_PASSWORD = "another-perfectly-fine-passphrase"
CONTEXT = RequestContext(ip_address="10.0.0.1", user_agent="pytest")
EMAIL = "admin@example.com"


@pytest.fixture
def settings() -> Settings:
    return Settings(password_reset_ttl_seconds=1_800)


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def make_user(session_factory, *, email: str = EMAIL, **overrides) -> User:
    now = utc_now()
    with session_factory() as session:
        user = User(
            email=email,
            password_hash=hash_password(PASSWORD),
            role=UserRole.ADMIN,
            created_at=now,
            updated_at=now,
            **{"status": UserStatus.ACTIVE, **overrides},
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        session.expunge(user)
        return user


def outbox_rows(session_factory) -> list[DomainOutbox]:
    with session_factory() as session:
        rows = list(session.execute(select(DomainOutbox).order_by(DomainOutbox.id)).scalars())
        for row in rows:
            session.expunge(row)
        return rows


def token_rows(session_factory) -> list[PasswordResetToken]:
    with session_factory() as session:
        rows = list(
            session.execute(select(PasswordResetToken).order_by(PasswordResetToken.id)).scalars()
        )
        for row in rows:
            session.expunge(row)
        return rows


def audit_actions(session_factory) -> list[AuditAction]:
    with session_factory() as session:
        return list(session.execute(select(AuditLog.action).order_by(AuditLog.id)).scalars())


def issue_token(session_factory, settings: Settings, *, email: str = EMAIL) -> str:
    """Run the forgot flow and dig the plaintext token back out of the outbox row."""
    request_reset(session_factory, settings, email=email, context=CONTEXT)
    payload = json.loads(outbox_rows(session_factory)[-1].payload_json or "{}")
    return str(payload["token"])


# --- 忘记密码：反用户枚举 -------------------------------------------------------


def test_requesting_a_reset_writes_the_token_outbox_row_and_audit_together(
    session_factory, settings: Settings
) -> None:
    """⚠️ 三件事同事务（Invariant 13 + 14）。

    分开提交的两种坏结果都不会报错：令牌有效但没人知道要发信（用户永远收不到），
    或者信发出去了而令牌不存在（点进去必然失效）。
    """
    user = make_user(session_factory)

    outbox_id = request_reset(session_factory, settings, email=EMAIL, context=CONTEXT)

    tokens = token_rows(session_factory)
    rows = outbox_rows(session_factory)
    assert len(tokens) == 1
    assert tokens[0].user_id == user.id
    assert len(rows) == 1
    assert rows[0].id == outbox_id
    assert rows[0].event_type == EVENT_PASSWORD_RESET
    assert rows[0].status is OutboxStatus.PENDING
    assert audit_actions(session_factory) == [AuditAction.PASSWORD_RESET_REQUESTED]


def test_nothing_survives_a_failure_partway_through(
    session_factory, settings: Settings, monkeypatch
) -> None:
    """⚠️ **变异测试是这条用例的来源**：在写 outbox 行之前插一个 `commit()`，
    正常路径下毫无差别，其余全套用例照样全绿。

    差别只在出错的那一刻显现，而那正是 Invariant 13 要管的：令牌先单独提交的话，
    后面一步失败会留下一张**有效但没人知道要发信**的令牌 —— 用户永远收不到信，
    而库里明明有一条「已申请重置」的记录。
    """
    make_user(session_factory)

    def explode(*_args, **_kwargs):
        raise RuntimeError("the audit write failed")

    monkeypatch.setattr("app.services.password_reset.record_audit", explode)

    with pytest.raises(RuntimeError):
        request_reset(session_factory, settings, email=EMAIL, context=CONTEXT)

    assert token_rows(session_factory) == [], "令牌行必须跟着回滚"
    assert outbox_rows(session_factory) == []


def test_an_unknown_address_looks_exactly_like_a_known_one(
    session_factory, settings: Settings
) -> None:
    """⚠️ 这是本任务最重要的一条。

    这个端点**不需要任何凭据**就能调。只要「存在」与「不存在」在外部可观测的
    行为上有任何一点不同 —— 状态码、文案、异常 —— 它立刻变成一个批量验证邮箱
    是否注册过的工具。
    """
    make_user(session_factory)

    known = request_reset(session_factory, settings, email=EMAIL, context=CONTEXT)
    unknown = request_reset(session_factory, settings, email="nobody@example.com", context=CONTEXT)

    # 调用方拿到的东西里，唯一的差别是那个**不对外暴露**的 outbox id。
    assert known is not None
    assert unknown is None
    # 不存在的账号不留任何痕迹：没有令牌行、没有 outbox 行（不能被当成发信放大器）。
    assert len(token_rows(session_factory)) == 1
    assert len(outbox_rows(session_factory)) == 1


def test_every_path_is_padded_to_the_same_floor(
    session_factory, settings: Settings, monkeypatch
) -> None:
    """⚠️ **这条用例来自整栈实测，不是想出来的**（而且实测抓了两轮）。

    1. 初版在账号不存在时直接 return：响应体一模一样，但「存在」要三次 INSERT +
       commit、「不存在」只有一次 SELECT。实测 11ms vs 4.8ms，**两组完全不重叠**。
    2. 第一次修用「不存在时烧一次 Argon2」对齐 —— Argon2（~40ms）比这条路径的
       真实工作（~11ms）贵得多，于是变成 11ms vs 42ms，**照样 100% 可分辨**。

    现在用固定下限。这里**不测墙钟时间**（那在 CI 上必然是偶发红），测的是
    「补齐这个动作对每条路径都发生了，且补到同一个目标」。
    """
    make_user(session_factory)
    padded: list[float] = []
    monkeypatch.setattr(
        "app.services.password_reset._pad_until", lambda deadline: padded.append(deadline)
    )
    monkeypatch.setattr("app.services.password_reset.time.monotonic", lambda: 1000.0)

    request_reset(session_factory, settings, email=EMAIL, context=CONTEXT)
    request_reset(session_factory, settings, email="nobody@example.com", context=CONTEXT)
    request_reset(session_factory, settings, email="not-an-email", context=CONTEXT)

    assert len(padded) == 3, "每一条路径都要补齐，一条都不能漏"
    assert len(set(padded)) == 1, "而且必须补到同一个目标，否则差异只是换了个来源"


def test_the_delivery_trigger_happens_inside_the_padded_window(
    session_factory, settings: Settings, monkeypatch
) -> None:
    """⚠️ **补齐漏掉任何一段可区分的工作，通道就还在，只是更窄。**

    初版把 celery 触发放在端点层、`request_reset` 返回**之后** —— 那几毫秒落在
    下限外面。实测：补齐之后两条路径仍稳定差约 1ms 且不重叠，来源正是这里。
    """
    make_user(session_factory)
    order: list[str] = []
    monkeypatch.setattr(
        "app.services.password_reset._pad_until", lambda _deadline: order.append("pad")
    )

    request_reset(
        session_factory,
        settings,
        email=EMAIL,
        context=CONTEXT,
        on_queued=lambda _id: order.append("trigger"),
    )

    assert order == ["trigger", "pad"], "触发必须发生在补齐之前，否则它落在窗口外"


def test_the_floor_is_well_above_the_real_work(session_factory, settings: Settings) -> None:
    """⚠️ 下限调小到接近真实工作耗时，这条控制就**悄悄失效**了，不会有任何报错。

    11ms 是实测的真实工作耗时，40ms 是一次 Argon2（另一条认证路径的量级）。
    下限必须在两者之上留余量。
    """
    assert _MIN_DURATION_SECONDS >= 0.1


def test_a_failure_is_padded_too(session_factory, settings: Settings, monkeypatch) -> None:
    """⚠️ 抛异常那条路径同样要补齐 —— 否则「哪种输入会让它出错」又成了一条
    可测量的差异。"""
    make_user(session_factory)
    padded: list[float] = []
    monkeypatch.setattr(
        "app.services.password_reset._pad_until", lambda deadline: padded.append(deadline)
    )

    def explode(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("app.services.password_reset.record_audit", explode)

    with pytest.raises(RuntimeError):
        request_reset(session_factory, settings, email=EMAIL, context=CONTEXT)

    assert len(padded) == 1


def test_a_malformed_address_is_swallowed_too(session_factory, settings: Settings) -> None:
    """⚠️ 连「这不是个邮箱」都不能抛 —— 那同样是一个可区分的响应。"""
    assert request_reset(session_factory, settings, email="not-an-email", context=CONTEXT) is None
    assert outbox_rows(session_factory) == []


def test_a_disabled_account_gets_no_reset_email(session_factory, settings: Settings) -> None:
    """给一个已停用的账号重置密码，等于给它一条重新可用的路。"""
    make_user(session_factory, status=UserStatus.DISABLED)

    assert request_reset(session_factory, settings, email=EMAIL, context=CONTEXT) is None
    assert outbox_rows(session_factory) == []
    assert audit_actions(session_factory) == []


def test_the_outbox_payload_carries_nothing_beyond_what_the_email_needs(
    session_factory, settings: Settings
) -> None:
    """⚠️ 这条 payload 里装着**令牌明文**，而 `domain_outbox` 是长期保留的。

    装进去的每一样东西都得有人真的读 —— 初版还存了 `expires_at`，而渲染邮件时
    根本没用它。没有消费方的字段不该待在一条含敏感材料的记录里。
    """
    make_user(session_factory)
    request_reset(session_factory, settings, email=EMAIL, context=CONTEXT)

    payload = json.loads(outbox_rows(session_factory)[-1].payload_json or "{}")

    assert set(payload) == {"to", "token"}


def test_the_stored_token_is_only_a_hash(session_factory, settings: Settings) -> None:
    """⚠️ 库里不能有任何可以直接拿去重置别人密码的东西。"""
    make_user(session_factory)
    raw = issue_token(session_factory, settings)

    stored = token_rows(session_factory)[0].token_hash

    assert stored != raw
    assert stored == hash_opaque_token(raw)


def test_the_token_expires_at_the_configured_ttl(session_factory) -> None:
    make_user(session_factory)
    settings = Settings(password_reset_ttl_seconds=60)
    moment = utc_now()

    request_reset(session_factory, settings, email=EMAIL, context=CONTEXT, now=moment)

    assert token_rows(session_factory)[0].expires_at == moment + dt.timedelta(seconds=60)


# --- 重置：单用、并发、会话吊销 -------------------------------------------------


def test_resetting_changes_the_password(session_factory, settings: Settings) -> None:
    user = make_user(session_factory)
    raw = issue_token(session_factory, settings)

    reset_password(session_factory, settings, token=raw, new_password=NEW_PASSWORD, context=CONTEXT)

    with session_factory() as session:
        stored = session.get(User, user.id)
        assert stored is not None
        assert verify_password(stored.password_hash, NEW_PASSWORD)
        assert not verify_password(stored.password_hash, PASSWORD)
    assert audit_actions(session_factory)[-1] == AuditAction.PASSWORD_RESET


def test_a_token_cannot_be_used_twice(session_factory, settings: Settings) -> None:
    """⚠️ 单用靠**条件更新的受影响行数**，不靠先读后写。"""
    make_user(session_factory)
    raw = issue_token(session_factory, settings)
    reset_password(session_factory, settings, token=raw, new_password=NEW_PASSWORD, context=CONTEXT)

    with pytest.raises(InvalidResetToken):
        reset_password(
            session_factory,
            settings,
            token=raw,
            new_password="a-third-fine-passphrase",
            context=CONTEXT,
        )


def test_an_expired_token_is_refused(session_factory, settings: Settings) -> None:
    make_user(session_factory)
    raw = issue_token(session_factory, settings)
    later = utc_now() + dt.timedelta(seconds=settings.password_reset_ttl_seconds + 1)

    with pytest.raises(InvalidResetToken):
        reset_password(
            session_factory,
            settings,
            token=raw,
            new_password=NEW_PASSWORD,
            context=CONTEXT,
            now=later,
        )


def test_a_used_token_is_refused_even_when_the_new_password_is_weak(
    session_factory, settings: Settings
) -> None:
    """⚠️ **错误码不能取决于密码的内容。**

    一张已经用过的链接，无论新密码多强都不可能成功 —— 所以它必须回
    `INVALID_RESET_TOKEN`。先校验强度的话会回 `WEAK_PASSWORD`，于是「这条链接
    还能不能用」这个稳定契约变成了「看你填的密码」。
    """
    make_user(session_factory)
    raw = issue_token(session_factory, settings)
    reset_password(session_factory, settings, token=raw, new_password=NEW_PASSWORD, context=CONTEXT)

    with pytest.raises(InvalidResetToken):
        reset_password(session_factory, settings, token=raw, new_password="短", context=CONTEXT)


def test_an_expired_token_is_refused_even_when_the_new_password_is_weak(
    session_factory, settings: Settings
) -> None:
    """过期那条路径同理。"""
    make_user(session_factory)
    raw = issue_token(session_factory, settings)
    later = utc_now() + dt.timedelta(seconds=settings.password_reset_ttl_seconds + 1)

    with pytest.raises(InvalidResetToken):
        reset_password(
            session_factory,
            settings,
            token=raw,
            new_password="短",
            context=CONTEXT,
            now=later,
        )


def test_an_unknown_token_is_refused(session_factory, settings: Settings) -> None:
    make_user(session_factory)

    with pytest.raises(InvalidResetToken):
        reset_password(
            session_factory,
            settings,
            token="not-a-token-we-ever-issued",
            new_password=NEW_PASSWORD,
            context=CONTEXT,
        )


def test_resetting_revokes_every_live_session(session_factory, settings: Settings) -> None:
    """⚠️ 「我怀疑账号被盗所以改了密码」如果对攻击者已有的会话毫无影响，
    用户以为自己解决的问题一个都没解决。

    **跨 family 全吊销**，不是只吊销某一次登录。
    """
    user = make_user(session_factory)
    now = utc_now()
    with session_factory() as session:
        for family in ("family-a", "family-b"):
            session.add(
                RefreshToken(
                    user_id=user.id,
                    family_id=family,
                    token_hash=f"hash-{family}",
                    issued_at=now,
                    expires_at=now + dt.timedelta(hours=12),
                )
            )
        session.commit()
    raw = issue_token(session_factory, settings)

    reset_password(session_factory, settings, token=raw, new_password=NEW_PASSWORD, context=CONTEXT)

    with session_factory() as session:
        live = session.execute(
            select(RefreshToken).where(RefreshToken.revoked_at.is_(None))
        ).scalars()
        assert list(live) == []


def test_resetting_also_invalidates_the_other_outstanding_links(
    session_factory, settings: Settings
) -> None:
    """收件箱里躺着的上一封信，不该在这次重置之后还能再改一次密码。"""
    make_user(session_factory)
    first = issue_token(session_factory, settings)
    second = issue_token(session_factory, settings)

    reset_password(
        session_factory, settings, token=second, new_password=NEW_PASSWORD, context=CONTEXT
    )

    with pytest.raises(InvalidResetToken):
        reset_password(
            session_factory,
            settings,
            token=first,
            new_password="a-third-fine-passphrase",
            context=CONTEXT,
        )


def test_resetting_clears_a_lockout(session_factory, settings: Settings) -> None:
    """⚠️ 旧密码此刻已经作废，继续锁着没有任何安全收益。

    而锁定状态**刻意不对外暴露**（设计闸门 §6），所以不清的后果是「重置成功却
    依然登不进去，且界面上看不出原因」。
    """
    user = make_user(
        session_factory,
        failed_login_count=5,
        locked_until=utc_now() + dt.timedelta(minutes=30),
        lockout_level=2,
    )
    raw = issue_token(session_factory, settings)

    reset_password(session_factory, settings, token=raw, new_password=NEW_PASSWORD, context=CONTEXT)

    with session_factory() as session:
        stored = session.get(User, user.id)
        assert stored is not None
        assert stored.locked_until is None
        assert stored.failed_login_count == 0
        # ⚠️ 档位**不清**：那是历史记录，清了等于每一轮锁定都回到最短时长。
        assert stored.lockout_level == 2


def test_a_weak_new_password_does_not_burn_the_token(session_factory, settings: Settings) -> None:
    """⚠️ 「新密码太弱」不该连令牌一起烧掉 —— 用户什么都没做错，却要重走一遍收信。"""
    make_user(session_factory)
    raw = issue_token(session_factory, settings)

    with pytest.raises(WeakPassword):
        reset_password(session_factory, settings, token=raw, new_password="短", context=CONTEXT)

    # 令牌仍然可用。
    reset_password(session_factory, settings, token=raw, new_password=NEW_PASSWORD, context=CONTEXT)
    assert token_rows(session_factory)[0].used_at is not None


def test_the_new_password_still_cannot_be_the_account_name(
    session_factory, settings: Settings
) -> None:
    """强度规则在 `validate_password_strength` 一处，重置这条路径也走它。"""
    make_user(session_factory)
    raw = issue_token(session_factory, settings)

    with pytest.raises(WeakPassword):
        reset_password(session_factory, settings, token=raw, new_password="admin", context=CONTEXT)


def test_a_reset_for_a_disabled_account_is_refused(session_factory, settings: Settings) -> None:
    """令牌签发之后账号被停用 —— 那张令牌必须跟着失效。"""
    user = make_user(session_factory)
    raw = issue_token(session_factory, settings)
    with session_factory() as session:
        stored = session.get(User, user.id)
        assert stored is not None
        stored.status = UserStatus.DISABLED
        session.commit()

    with pytest.raises(InvalidResetToken):
        reset_password(
            session_factory, settings, token=raw, new_password=NEW_PASSWORD, context=CONTEXT
        )


def test_the_audit_row_never_carries_the_token_or_the_password(
    session_factory, settings: Settings
) -> None:
    """§94 / Invariant 9：审计的自由 JSON 是最容易被塞进敏感材料的地方。"""
    make_user(session_factory)
    raw = issue_token(session_factory, settings)
    reset_password(session_factory, settings, token=raw, new_password=NEW_PASSWORD, context=CONTEXT)

    with session_factory() as session:
        rows = list(session.execute(select(AuditLog)).scalars())
    blob = json.dumps(
        [
            {"before": row.before_state, "after": row.after_state, "reason": row.reason}
            for row in rows
        ]
    )

    assert raw not in blob
    assert NEW_PASSWORD not in blob
    assert PASSWORD not in blob
