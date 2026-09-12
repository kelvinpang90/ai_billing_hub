"""Login, lockout, rotation and replay detection (spec §53, §66).

这些分支的共同点是**失败方式安静**：把重放当成正常刷新、把锁定计数在错误的
时刻清零、把令牌发出去却没写审计 —— 每一种都能正常返回 200。
"""

from __future__ import annotations

import datetime as dt
import os

import pytest
from sqlalchemy import create_engine, select

from app.core.config import Settings
from app.core.database import create_session_factory
from app.core.passwords import hash_password
from app.core.tokens import TOKEN_TYPE_ACCESS, decode_token
from app.models.auth import AuditAction, AuditLog, RefreshToken, User, UserRole, UserStatus
from app.models.base import Base
from app.services.auth import (
    InvalidCredentials,
    RequestContext,
    TokenReused,
    authenticate,
    logout,
    refresh_session,
    utc_now,
)

PASSWORD = "a-perfectly-fine-passphrase"
CONTEXT = RequestContext(ip_address="10.0.0.1", user_agent="pytest")


@pytest.fixture
def settings(tmp_path) -> Settings:
    key = tmp_path / "jwt.key"
    key.write_text("a-test-signing-key-that-is-long-enough", encoding="utf-8")
    return Settings(jwt_secret_file=str(key), login_max_failures=3, login_lockout_seconds=900)


@pytest.fixture
def session_factory():
    """SQLite in-memory 只够跑这一层的逻辑分支。

    ⚠️ **迁移与 DDL 仍然必须对着真 MySQL 验**（见 test_migrations.py）——
    SQLite 的 DDL 与 MySQL 差得远，在这里跑通证明不了生产上跑得通。
    """
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def make_user(session_factory, *, email: str = "admin@example.com", **overrides) -> User:
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


def audit_actions(session_factory) -> list[AuditAction]:
    with session_factory() as session:
        return list(session.execute(select(AuditLog.action).order_by(AuditLog.id)).scalars())


# --- 正常路径 -----------------------------------------------------------------


def test_login_issues_a_session_and_audits_it(session_factory, settings: Settings) -> None:
    make_user(session_factory)
    issued = authenticate(
        session_factory, settings, email="admin@example.com", password=PASSWORD, context=CONTEXT
    )
    payload = decode_token(settings, issued.access_token, expected_type=TOKEN_TYPE_ACCESS)
    assert payload["role"] == "ADMIN"
    assert audit_actions(session_factory) == [AuditAction.LOGIN]


def test_email_matching_is_case_insensitive(session_factory, settings: Settings) -> None:
    """⚠️ `Admin@x.com` 建的账号必须能用 `admin@x.com` 登录。

    不做归一化的话，用户会遇到「密码明明对却登不进去」，而且没有任何线索。
    """
    make_user(session_factory, email="admin@example.com")
    authenticate(
        session_factory, settings, email="ADMIN@Example.COM", password=PASSWORD, context=CONTEXT
    )


# --- 用户枚举 -----------------------------------------------------------------


def test_unknown_email_and_wrong_password_are_indistinguishable(
    session_factory, settings: Settings
) -> None:
    """三种失败共用同一个码与同一句文案（锁定那条见下）。"""
    make_user(session_factory)
    with pytest.raises(InvalidCredentials) as unknown:
        authenticate(
            session_factory,
            settings,
            email="nobody@example.com",
            password=PASSWORD,
            context=CONTEXT,
        )
    with pytest.raises(InvalidCredentials) as wrong:
        authenticate(
            session_factory,
            settings,
            email="admin@example.com",
            password="wrong-one",
            context=CONTEXT,
        )
    assert unknown.value.code == wrong.value.code
    assert unknown.value.message == wrong.value.message


def test_a_failed_login_for_an_unknown_email_is_still_audited(
    session_factory, settings: Settings
) -> None:
    """⚠️ 这一条恰恰最该记：有人在猜不存在的账号，是爆破的早期信号。"""
    with pytest.raises(InvalidCredentials):
        authenticate(
            session_factory,
            settings,
            email="nobody@example.com",
            password=PASSWORD,
            context=CONTEXT,
        )
    assert audit_actions(session_factory) == [AuditAction.LOGIN_FAILED]


# --- 锁定 ---------------------------------------------------------------------


def test_repeated_failures_lock_the_account(session_factory, settings: Settings) -> None:
    make_user(session_factory)
    for _ in range(settings.login_max_failures):
        with pytest.raises(InvalidCredentials):
            authenticate(
                session_factory,
                settings,
                email="admin@example.com",
                password="wrong",
                context=CONTEXT,
            )
    # 锁定后即使密码正确也拒绝。
    with pytest.raises(InvalidCredentials):
        authenticate(
            session_factory, settings, email="admin@example.com", password=PASSWORD, context=CONTEXT
        )


def test_a_locked_account_is_indistinguishable_from_a_wrong_password(
    session_factory, settings: Settings
) -> None:
    """⚠️ 刻意不返回 423 Locked。

    那个状态码等于告诉对方「这个邮箱存在，而且我正在被爆破」。
    """
    make_user(session_factory)
    for _ in range(settings.login_max_failures):
        with pytest.raises(InvalidCredentials):
            authenticate(
                session_factory,
                settings,
                email="admin@example.com",
                password="wrong",
                context=CONTEXT,
            )
    with pytest.raises(InvalidCredentials) as locked:
        authenticate(
            session_factory, settings, email="admin@example.com", password=PASSWORD, context=CONTEXT
        )
    assert locked.value.http_status == 401
    assert "lock" not in locked.value.message.lower()


def test_failures_are_not_cumulative_across_a_success(session_factory, settings: Settings) -> None:
    """⚠️ 「连续失败」才锁定。

    成功时不清零的话，历史失败会永久累积，管理员会在一次次正常登录之间
    突然被锁 —— 而看起来像随机故障。
    """
    make_user(session_factory)
    for _ in range(settings.login_max_failures - 1):
        with pytest.raises(InvalidCredentials):
            authenticate(
                session_factory,
                settings,
                email="admin@example.com",
                password="wrong",
                context=CONTEXT,
            )
    authenticate(
        session_factory, settings, email="admin@example.com", password=PASSWORD, context=CONTEXT
    )
    # 再错一次不应该锁定。
    with pytest.raises(InvalidCredentials):
        authenticate(
            session_factory, settings, email="admin@example.com", password="wrong", context=CONTEXT
        )
    authenticate(
        session_factory, settings, email="admin@example.com", password=PASSWORD, context=CONTEXT
    )


def test_an_expired_lock_resets_the_counter_before_judging(
    session_factory, settings: Settings
) -> None:
    """⚠️ 不清零的话，锁一解开、下一次失败立刻又达阈值，实际锁定时长变成无限。"""
    user = make_user(session_factory)
    with session_factory() as session:
        stored = session.get(User, user.id)
        assert stored is not None
        stored.failed_login_count = settings.login_max_failures
        stored.locked_until = utc_now() - dt.timedelta(seconds=1)
        session.commit()

    # 锁已过期：这一次应该被正常判定（密码对 → 成功）。
    authenticate(
        session_factory, settings, email="admin@example.com", password=PASSWORD, context=CONTEXT
    )
    with session_factory() as session:
        stored = session.get(User, user.id)
        assert stored is not None
        assert stored.failed_login_count == 0
        assert stored.locked_until is None


def test_an_expired_lock_does_not_immediately_relock_on_the_next_failure(
    session_factory, settings: Settings
) -> None:
    """⚠️ 变异测试补的用例。

    上一条断言的是「**成功**登录后计数为 0」—— 而成功路径本来就会清零，所以
    把 `_clear_lock_if_expired` 里的清零删掉，那条用例照样绿。真正要证明的是：
    锁到期之后再失败一次，**不能立刻又达到阈值**，否则实际锁定时长变成无限。
    """
    user = make_user(session_factory)
    with session_factory() as session:
        stored = session.get(User, user.id)
        assert stored is not None
        stored.failed_login_count = settings.login_max_failures
        stored.locked_until = utc_now() - dt.timedelta(seconds=1)
        session.commit()

    # 锁已到期：这一次失败应该从 0 重新数起，所以计数落在 1。
    with pytest.raises(InvalidCredentials):
        authenticate(
            session_factory, settings, email="admin@example.com", password="wrong", context=CONTEXT
        )
    with session_factory() as session:
        stored = session.get(User, user.id)
        assert stored is not None
        assert stored.failed_login_count == 1
        assert stored.locked_until is None, "一次失败不该重新锁定"


def test_a_failed_attempt_still_counts_when_the_lock_had_just_expired(
    session_factory, settings: Settings
) -> None:
    """锁到期的清零与失败计数的递增走**两个不同的事务**，两者都必须落地。

    只落其中一个的话：只落清零 = 计数永远回不去阈值（爆破无限重试）；
    只落递增 = 锁定时长变成无限。
    """
    user = make_user(session_factory)
    with session_factory() as session:
        stored = session.get(User, user.id)
        assert stored is not None
        stored.failed_login_count = 2
        stored.locked_until = utc_now() - dt.timedelta(seconds=1)
        session.commit()

    with pytest.raises(InvalidCredentials):
        authenticate(
            session_factory, settings, email="admin@example.com", password="wrong", context=CONTEXT
        )
    with session_factory() as session:
        stored = session.get(User, user.id)
        assert stored is not None
        # 清零（2 → 0）与递增（0 → 1）都发生了。
        assert stored.failed_login_count == 1


def test_an_unknown_email_still_costs_a_hash_verification(
    session_factory, settings: Settings, monkeypatch
) -> None:
    """⚠️ 变异测试补的用例：原先没有任何断言盯着这条控制。

    用户不存在时如果跳过哈希校验，「存在的慢、不存在的快」本身就是一条
    用户枚举通道 —— 而且它**不会让任何用例变红**。
    """
    calls: list[int] = []
    monkeypatch.setattr("app.services.auth.spend_dummy_verification", lambda: calls.append(1))
    with pytest.raises(InvalidCredentials):
        authenticate(
            session_factory,
            settings,
            email="nobody@example.com",
            password=PASSWORD,
            context=CONTEXT,
        )
    assert calls, "a missing account must cost the same as a wrong password"


@pytest.mark.skipif(
    not os.environ.get("BILLING_TEST_DATABASE_URL"),
    reason="BILLING_TEST_DATABASE_URL is not set; concurrency needs a real database",
)
def test_concurrent_refresh_lets_exactly_one_through(tmp_path) -> None:
    """⚠️ 变异测试补的用例：把条件更新的判定改成恒真，原先没有任何用例变红。

    两个请求同时拿着同一个刷新令牌进来（重试、双标签页、或攻击者与真用户
    同时用）。**判定必须来自 UPDATE 的受影响行数**，先 SELECT 再 UPDATE 的话，
    两边都会读到 `used_at IS NULL`，于是同一个令牌换出两条会话。

    ⚠️ **这条必须跑在真 MySQL 上。**内存 SQLite 要么每连接一个独立的库、要么
    （用 StaticPool）所有会话共用一条连接 —— 后者意味着两个「并发」事务其实是
    同一个事务，根本模拟不出竞态。第一版就是这么写的，结果两边都被判成重放，
    而那个失败与被测代码无关。
    """
    import threading

    key = tmp_path / "jwt.key"
    key.write_text("a-test-signing-key-that-is-long-enough", encoding="utf-8")
    local_settings = Settings(jwt_secret_file=str(key))

    # ⚠️ **用一个独立的库**，不要和迁移用例共用。
    # 第一版共用了：这里的 `drop_all` 把表删掉，而 `alembic_version` 仍停在
    # 最新版，于是 test_migrations 的 downgrade 报 "Unknown table"。
    # 两个用例各自都对，凑在一个库里就互相踩。
    from sqlalchemy.engine import make_url

    base_url = make_url(os.environ["BILLING_TEST_DATABASE_URL"])
    admin_engine = create_engine(base_url.set(database=""), isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.exec_driver_sql("CREATE DATABASE IF NOT EXISTS billing_test_concurrency")
    admin_engine.dispose()

    engine = create_engine(base_url.set(database="billing_test_concurrency"), pool_size=5)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)

    now = utc_now()
    with factory() as session:
        session.add(
            User(
                email="admin@example.com",
                password_hash=hash_password(PASSWORD),
                role=UserRole.ADMIN,
                status=UserStatus.ACTIVE,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()

    issued = authenticate(
        factory, local_settings, email="admin@example.com", password=PASSWORD, context=CONTEXT
    )

    outcomes: list[str] = []
    barrier = threading.Barrier(2)

    def attempt() -> None:
        barrier.wait()
        try:
            refresh_session(
                factory, local_settings, raw_token=issued.refresh_token, context=CONTEXT
            )
            outcomes.append("ok")
        except Exception:  # noqa: BLE001 - 这里只关心成功还是失败
            outcomes.append("refused")

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes.count("ok") == 1, f"exactly one rotation may win, got {outcomes}"

    # ⚠️ 「恰好一个成功」**不够**。实现闸门指出的正是这一点：第一版竞争失败那支
    # 只 rollback 抛错、不吊销家族，于是赢的那一方（可能是攻击者）拿到有效的新
    # 令牌而会话继续可用 —— 而上面那条断言照样绿。
    #
    # 竞争失败就是重放，整个家族必须已被吊销。
    with factory() as session:
        rows = list(session.execute(select(RefreshToken)).scalars())
        assert rows
        assert all(row.revoked_at is not None for row in rows), (
            "losing the race is a replay: the whole family must be revoked"
        )
        actions = list(session.execute(select(AuditLog.action)).scalars())
        assert AuditAction.TOKEN_REUSED in actions

    # 不给后面的迁移用例留下表。
    Base.metadata.drop_all(engine)


def test_a_disabled_account_cannot_log_in(session_factory, settings: Settings) -> None:
    make_user(session_factory, status=UserStatus.DISABLED)
    with pytest.raises(InvalidCredentials):
        authenticate(
            session_factory, settings, email="admin@example.com", password=PASSWORD, context=CONTEXT
        )


# --- 刷新与重放 ---------------------------------------------------------------


def test_refresh_rotates_the_token(session_factory, settings: Settings) -> None:
    make_user(session_factory)
    first = authenticate(
        session_factory, settings, email="admin@example.com", password=PASSWORD, context=CONTEXT
    )
    second = refresh_session(
        session_factory, settings, raw_token=first.refresh_token, context=CONTEXT
    )
    assert second.refresh_token != first.refresh_token


def test_reusing_a_refresh_token_revokes_the_whole_family(
    session_factory, settings: Settings
) -> None:
    """⚠️ 这是令牌被盗的处置。

    攻击者用了偷来的令牌之后，真用户下一次刷新会撞上「已使用」，整条链被吊销、
    双方都被踢出去。代价是真用户要重新登录 —— 远好过两边共用一个会话而谁都
    不知道。
    """
    make_user(session_factory)
    first = authenticate(
        session_factory, settings, email="admin@example.com", password=PASSWORD, context=CONTEXT
    )
    refresh_session(session_factory, settings, raw_token=first.refresh_token, context=CONTEXT)

    with pytest.raises(TokenReused):
        refresh_session(session_factory, settings, raw_token=first.refresh_token, context=CONTEXT)

    with session_factory() as session:
        rows = list(session.execute(select(RefreshToken)).scalars())
        assert rows, "the family should still exist, just revoked"
        assert all(row.revoked_at is not None for row in rows)
    assert AuditAction.TOKEN_REUSED in audit_actions(session_factory)


def test_an_unknown_refresh_token_is_refused(session_factory, settings: Settings) -> None:
    with pytest.raises(TokenReused):
        refresh_session(session_factory, settings, raw_token="never-issued", context=CONTEXT)


def test_an_expired_refresh_token_is_refused(session_factory, settings: Settings) -> None:
    make_user(session_factory)
    issued = authenticate(
        session_factory, settings, email="admin@example.com", password=PASSWORD, context=CONTEXT
    )
    with session_factory() as session:
        row = session.execute(select(RefreshToken)).scalar_one()
        row.expires_at = utc_now() - dt.timedelta(seconds=1)
        session.commit()
    with pytest.raises(TokenReused):
        refresh_session(session_factory, settings, raw_token=issued.refresh_token, context=CONTEXT)


def test_an_idle_refresh_token_is_refused(session_factory, settings: Settings) -> None:
    """闲置上限与绝对上限是两条独立的线；只有绝对上限的话，一个被偷走的令牌
    可以安静地躺 11 个小时再用。"""
    make_user(session_factory)
    issued = authenticate(
        session_factory, settings, email="admin@example.com", password=PASSWORD, context=CONTEXT
    )
    with session_factory() as session:
        row = session.execute(select(RefreshToken)).scalar_one()
        row.issued_at = utc_now() - dt.timedelta(seconds=settings.refresh_token_idle_seconds + 60)
        session.commit()
    with pytest.raises(TokenReused):
        refresh_session(session_factory, settings, raw_token=issued.refresh_token, context=CONTEXT)


def test_logout_revokes_the_family_and_is_idempotent(session_factory, settings: Settings) -> None:
    make_user(session_factory)
    issued = authenticate(
        session_factory, settings, email="admin@example.com", password=PASSWORD, context=CONTEXT
    )
    logout(session_factory, raw_token=issued.refresh_token, context=CONTEXT)
    # 再登出一次仍然不报错 —— 失败的登出没有任何有用语义。
    logout(session_factory, raw_token=issued.refresh_token, context=CONTEXT)
    with pytest.raises(TokenReused):
        refresh_session(session_factory, settings, raw_token=issued.refresh_token, context=CONTEXT)
    assert AuditAction.LOGOUT in audit_actions(session_factory)


def test_refresh_is_refused_once_the_account_is_disabled(
    session_factory, settings: Settings
) -> None:
    """⚠️ 停用账号必须立刻切断续期，否则一次停用要等刷新令牌自然过期才生效。"""
    user = make_user(session_factory)
    issued = authenticate(
        session_factory, settings, email="admin@example.com", password=PASSWORD, context=CONTEXT
    )
    with session_factory() as session:
        stored = session.get(User, user.id)
        assert stored is not None
        stored.status = UserStatus.DISABLED
        session.commit()
    with pytest.raises(TokenReused):
        refresh_session(session_factory, settings, raw_token=issued.refresh_token, context=CONTEXT)


# --- 审计内容 -----------------------------------------------------------------


def test_audit_rows_never_contain_credentials(session_factory, settings: Settings) -> None:
    """⚠️ before/after 是自由 JSON，最容易被人塞进整个请求体。审计表长期保留。"""
    make_user(session_factory)
    issued = authenticate(
        session_factory, settings, email="admin@example.com", password=PASSWORD, context=CONTEXT
    )
    with session_factory() as session:
        blob = " ".join(
            f"{row.before_state} {row.after_state} {row.reason}"
            for row in session.execute(select(AuditLog)).scalars()
        )
    assert PASSWORD not in blob
    assert issued.refresh_token not in blob
    assert issued.access_token not in blob
    assert "$argon2" not in blob
