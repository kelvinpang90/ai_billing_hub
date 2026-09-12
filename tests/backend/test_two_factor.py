"""TOTP enrolment, recovery codes, and the four single-use guarantees (spec §54).

设计闸门在这一块挡了两轮，两次都是同一类：**判定来自应用层的先读后写，而不是
条件更新的受影响行数**。所以这里的并发用例不是锦上添花 —— 它们是那两条阻断项
的回归。
"""

from __future__ import annotations

import base64
import datetime as dt
import os
import threading

import pyotp
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.core.database import create_session_factory
from app.core.passwords import hash_password
from app.models.auth import (
    AuditAction,
    AuditLog,
    RecoveryCode,
    TwoFactorSetting,
    User,
    UserRole,
    UserStatus,
)
from app.models.base import Base
from app.services.auth import RequestContext, utc_now
from app.services.two_factor import (
    RECOVERY_CODE_COUNT,
    InvalidTotp,
    TwoFactorAlreadyEnrolled,
    TwoFactorNotEnrolled,
    confirm_enrolment,
    is_enrolled,
    regenerate_recovery_codes,
    start_enrolment,
    verify_second_factor,
)

PASSWORD = "a-perfectly-fine-passphrase"
CONTEXT = RequestContext(ip_address="10.0.0.1", user_agent="pytest")


@pytest.fixture
def settings(tmp_path) -> Settings:
    jwt_key = tmp_path / "jwt.key"
    jwt_key.write_text("a-test-signing-key-that-is-long-enough", encoding="utf-8")
    master = tmp_path / "master.key"
    master.write_text(f"1:{base64.b64encode(os.urandom(32)).decode()}\n", encoding="utf-8")
    return Settings(jwt_secret_file=str(jwt_key), master_key_file=str(master))


@pytest.fixture
def session_factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


@pytest.fixture
def mysql_session_factory():
    """A real database, in its own schema.

    ⚠️ **并发语义没法用内存 SQLite 验。**它要么每个连接一个独立的库，要么
    （用 StaticPool）所有会话共用一条连接 —— 后者意味着两个「并发」事务其实是
    同一个事务，竞态根本不会发生。T0.8a 的刷新令牌并发用例上踩过一次，这里
    第二次遇到同一件事。

    ⚠️ 用**独立的库**，不和迁移用例共用：那边靠 `alembic_version` 记状态，
    这里 `create_all` / `drop_all` 会把它的表删掉（T0.8a 上踩过）。
    """
    url = os.environ.get("BILLING_TEST_DATABASE_URL")
    if not url:
        pytest.skip("BILLING_TEST_DATABASE_URL is not set; concurrency needs a real database")

    from sqlalchemy.engine import make_url

    base_url = make_url(url)
    admin = create_engine(base_url.set(database=""), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.exec_driver_sql("CREATE DATABASE IF NOT EXISTS billing_test_2fa")
    admin.dispose()

    engine = create_engine(base_url.set(database="billing_test_2fa"), pool_size=5)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    try:
        yield create_session_factory(engine)
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture
def mysql_user_id(mysql_session_factory) -> int:
    now = utc_now()
    with mysql_session_factory() as session:
        user = User(
            email="admin@example.com",
            password_hash=hash_password(PASSWORD),
            role=UserRole.ADMIN,
            status=UserStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
        session.add(user)
        session.commit()
        return user.id


@pytest.fixture
def user_id(session_factory) -> int:
    now = utc_now()
    with session_factory() as session:
        user = User(
            email="admin@example.com",
            password_hash=hash_password(PASSWORD),
            role=UserRole.ADMIN,
            status=UserStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
        session.add(user)
        session.commit()
        return user.id


def enrol_and_confirm(session_factory, settings, user_id: int) -> tuple[str, list[str]]:
    enrolment = start_enrolment(session_factory, settings, user_id=user_id)
    codes = confirm_enrolment(
        session_factory,
        settings,
        user_id=user_id,
        code=pyotp.TOTP(enrolment.secret).now(),
        context=CONTEXT,
    )
    return enrolment.secret, codes


# --- 注册流程 -----------------------------------------------------------------


def test_a_pending_enrolment_does_not_count_as_enabled(session_factory, settings, user_id) -> None:
    """⚠️ 「生成了密钥但没扫码确认」算**未启用**。

    算成已启用的话，用户既进不去（没有能用的验证器），也没法重新注册 ——
    直接被锁在门外。
    """
    start_enrolment(session_factory, settings, user_id=user_id)
    with session_factory() as session:
        assert is_enrolled(session, user_id) is False


def test_confirming_enables_it_and_mints_recovery_codes(session_factory, settings, user_id) -> None:
    _, codes = enrol_and_confirm(session_factory, settings, user_id)
    assert len(codes) == RECOVERY_CODE_COUNT
    with session_factory() as session:
        assert is_enrolled(session, user_id) is True
        actions = list(session.execute(select(AuditLog.action)).scalars())
    assert AuditAction.TWO_FACTOR_ENABLED in actions


def test_the_stored_secret_is_encrypted(session_factory, settings, user_id) -> None:
    """⚠️ 库里绝不能有明文 —— 数据库泄露不该等于所有人的第二因子泄露。"""
    enrolment = start_enrolment(session_factory, settings, user_id=user_id)
    with session_factory() as session:
        row = session.get(TwoFactorSetting, user_id)
        assert row is not None
        assert enrolment.secret not in row.encrypted_totp_secret


def test_recovery_codes_are_stored_hashed(session_factory, settings, user_id) -> None:
    _, codes = enrol_and_confirm(session_factory, settings, user_id)
    with session_factory() as session:
        hashes = list(session.execute(select(RecoveryCode.code_hash)).scalars())
    blob = " ".join(hashes)
    for code in codes:
        assert code not in blob
    assert all(h.startswith("$argon2id$") for h in hashes)


def test_a_wrong_code_leaves_the_enrolment_pending(session_factory, settings, user_id) -> None:
    """⚠️ 确认失败**不能**生成恢复码 —— 那会让用户拿到一批他以为可用、
    但 2FA 其实没启用的码。"""
    start_enrolment(session_factory, settings, user_id=user_id)
    with pytest.raises(InvalidTotp):
        confirm_enrolment(
            session_factory, settings, user_id=user_id, code="000000", context=CONTEXT
        )
    with session_factory() as session:
        assert is_enrolled(session, user_id) is False
        assert list(session.execute(select(RecoveryCode.id)).scalars()) == []


def test_enrolling_again_after_confirmation_is_refused(session_factory, settings, user_id) -> None:
    """⚠️ 已确认的注册**不能被覆盖**，也不重发密钥。

    能覆盖的话，任何能调这个端点的人都可以把别人已启用的 2FA 换成自己的。
    """
    enrol_and_confirm(session_factory, settings, user_id)
    with pytest.raises(TwoFactorAlreadyEnrolled):
        start_enrolment(session_factory, settings, user_id=user_id)


def test_an_abandoned_enrolment_can_be_restarted(session_factory, settings, user_id) -> None:
    """没确认过的可以重来 —— 否则扫码扫坏一次就再也注册不了。"""
    first = start_enrolment(session_factory, settings, user_id=user_id)
    second = start_enrolment(session_factory, settings, user_id=user_id)
    assert second.secret != first.secret
    assert second.secret_version > first.secret_version


# --- 校验 ---------------------------------------------------------------------


def test_a_valid_code_is_accepted(session_factory, settings, user_id) -> None:
    secret, _ = enrol_and_confirm(session_factory, settings, user_id)
    with session_factory() as session:
        result = verify_second_factor(
            session, settings, user_id=user_id, code=pyotp.TOTP(secret).now(), now=utc_now()
        )
        session.commit()
    assert result.used_recovery_code is False
    assert result.remaining_recovery_codes == RECOVERY_CODE_COUNT


def test_the_same_code_cannot_be_used_twice(session_factory, settings, user_id) -> None:
    """⚠️ 同一个验证码在它的 30 秒窗口里只能用一次。

    不挡的话，肩窥或一次中间人抓到的码，在窗口内可以被重放。
    """
    secret, _ = enrol_and_confirm(session_factory, settings, user_id)
    code = pyotp.TOTP(secret).now()
    moment = utc_now()
    with session_factory() as session:
        verify_second_factor(session, settings, user_id=user_id, code=code, now=moment)
        session.commit()
    with session_factory() as session, pytest.raises(InvalidTotp):
        verify_second_factor(session, settings, user_id=user_id, code=code, now=moment)


def test_a_recovery_code_works_and_is_single_use(session_factory, settings, user_id) -> None:
    _, codes = enrol_and_confirm(session_factory, settings, user_id)
    with session_factory() as session:
        result = verify_second_factor(
            session, settings, user_id=user_id, code=codes[0], now=utc_now()
        )
        session.commit()
    assert result.used_recovery_code is True
    assert result.remaining_recovery_codes == RECOVERY_CODE_COUNT - 1

    with session_factory() as session, pytest.raises(InvalidTotp):
        verify_second_factor(session, settings, user_id=user_id, code=codes[0], now=utc_now())


def test_regenerating_invalidates_the_old_codes(session_factory, settings, user_id) -> None:
    """⚠️ 不作废旧码的话，重新生成一次就多十个有效码，而用户以为旧的已经失效。"""
    _, old_codes = enrol_and_confirm(session_factory, settings, user_id)
    new_codes = regenerate_recovery_codes(session_factory, user_id=user_id, context=CONTEXT)
    assert set(new_codes).isdisjoint(old_codes)

    with session_factory() as session, pytest.raises(InvalidTotp):
        verify_second_factor(session, settings, user_id=user_id, code=old_codes[0], now=utc_now())

    with session_factory() as session:
        live = list(
            session.execute(
                select(RecoveryCode.id).where(
                    RecoveryCode.used_at.is_(None), RecoveryCode.revoked_at.is_(None)
                )
            ).scalars()
        )
    assert len(live) == RECOVERY_CODE_COUNT, "任何时刻至多十个可用"


def test_verifying_without_an_enrolment_is_refused(session_factory, settings, user_id) -> None:
    with session_factory() as session, pytest.raises(TwoFactorNotEnrolled):
        verify_second_factor(session, settings, user_id=user_id, code="000000", now=utc_now())


def test_a_wrong_totp_and_a_used_recovery_code_look_the_same(
    session_factory, settings, user_id
) -> None:
    """区分「码错了」与「这个恢复码用过了」只对攻击者有价值。"""
    _, codes = enrol_and_confirm(session_factory, settings, user_id)
    with session_factory() as session:
        verify_second_factor(session, settings, user_id=user_id, code=codes[0], now=utc_now())
        session.commit()

    with session_factory() as session:
        with pytest.raises(InvalidTotp) as used:
            verify_second_factor(session, settings, user_id=user_id, code=codes[0], now=utc_now())
        with pytest.raises(InvalidTotp) as wrong:
            verify_second_factor(session, settings, user_id=user_id, code="000000", now=utc_now())
    assert used.value.code == wrong.value.code
    assert used.value.message == wrong.value.message


def test_clock_skew_of_one_step_is_tolerated(session_factory, settings, user_id) -> None:
    """验证器与服务器的时钟总会差一点。差一格（30 秒）要收，再远就不收。"""
    secret, _ = enrol_and_confirm(session_factory, settings, user_id)
    totp = pyotp.TOTP(secret)
    now = utc_now()

    previous = totp.at(now.replace(tzinfo=dt.UTC) - dt.timedelta(seconds=30))
    with session_factory() as session:
        verify_second_factor(session, settings, user_id=user_id, code=previous, now=now)
        session.commit()

    far = totp.at(now.replace(tzinfo=dt.UTC) - dt.timedelta(seconds=300))
    with session_factory() as session, pytest.raises(InvalidTotp):
        verify_second_factor(session, settings, user_id=user_id, code=far, now=now)


# --- 并发：设计闸门两条阻断项的回归 -------------------------------------------


def _run_concurrently(work, count: int = 2) -> list[str]:
    outcomes: list[str] = []
    barrier = threading.Barrier(count)

    def attempt() -> None:
        barrier.wait()
        try:
            work()
            outcomes.append("ok")
        except Exception:  # noqa: BLE001 - 这里只关心成功还是失败
            outcomes.append("refused")

    threads = [threading.Thread(target=attempt) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return outcomes


def test_concurrent_confirm_mints_only_one_batch(
    mysql_session_factory, settings, mysql_user_id
) -> None:
    """⚠️ 设计闸门第三轮的阻断项。

    两个 `confirm` 并发各生成 10 个的话，用户手上会有 **20 个**有效码 ——
    而他只抄了 10 个，另外 10 个躺在库里没人知道。
    """
    enrolment = start_enrolment(mysql_session_factory, settings, user_id=mysql_user_id)
    code = pyotp.TOTP(enrolment.secret).now()

    outcomes = _run_concurrently(
        lambda: confirm_enrolment(
            mysql_session_factory, settings, user_id=mysql_user_id, code=code, context=CONTEXT
        )
    )
    assert outcomes.count("ok") == 1, f"只能确认一次，得到 {outcomes}"

    with mysql_session_factory() as session:
        live = list(
            session.execute(
                select(RecoveryCode.id).where(
                    RecoveryCode.used_at.is_(None), RecoveryCode.revoked_at.is_(None)
                )
            ).scalars()
        )
    assert len(live) == RECOVERY_CODE_COUNT, f"恢复码只能生成一批，得到 {len(live)} 个"


def test_concurrent_use_of_one_totp_code_succeeds_once(
    mysql_session_factory, settings, mysql_user_id
) -> None:
    """⚠️ 设计闸门第一轮的阻断项：同一个验证码并发提交，只能有一方通过。

    先读 `last_used_counter` 再比较的话，两边都会读到旧值、都通过 ——
    一个验证码换出两条会话。
    """
    secret, _ = enrol_and_confirm(mysql_session_factory, settings, mysql_user_id)
    code = pyotp.TOTP(secret).now()
    moment = utc_now()

    def use_it() -> None:
        with mysql_session_factory() as session:
            verify_second_factor(session, settings, user_id=mysql_user_id, code=code, now=moment)
            session.commit()

    outcomes = _run_concurrently(use_it)
    assert outcomes.count("ok") == 1, f"一个 TOTP 码只能用一次，得到 {outcomes}"


def test_a_reenrolment_between_verify_and_confirm_invalidates_the_confirmation(
    session_factory, settings, user_id
) -> None:
    """⚠️ 设计闸门第三轮的另一半：`secret_version` 存在的理由。

    校验之后、置位之前若有人重新 `enrol` 换了密钥，这次确认必须落空 ——
    否则会把一个**从未被验证过的密钥**标成已确认，用户的验证器从此对不上。
    """
    first = start_enrolment(session_factory, settings, user_id=user_id)
    code = pyotp.TOTP(first.secret).now()

    # 模拟交错：确认之前又注册了一次。
    second = start_enrolment(session_factory, settings, user_id=user_id)
    assert second.secret != first.secret

    with pytest.raises(InvalidTotp):
        # 旧密钥算出来的码对新密钥无效 —— 第一道就挡住了。
        confirm_enrolment(session_factory, settings, user_id=user_id, code=code, context=CONTEXT)
    with session_factory() as session:
        assert is_enrolled(session, user_id) is False


def test_concurrent_use_of_one_recovery_code_consumes_it_once(
    mysql_session_factory, settings, mysql_user_id
) -> None:
    """⚠️ 设计闸门第一轮的阻断项之一：恢复码的单用靠条件更新，不靠先读后写。"""
    _, codes = enrol_and_confirm(mysql_session_factory, settings, mysql_user_id)

    def use_it() -> None:
        with mysql_session_factory() as session:
            verify_second_factor(
                session, settings, user_id=mysql_user_id, code=codes[0], now=utc_now()
            )
            session.commit()

    outcomes = _run_concurrently(use_it)
    assert outcomes.count("ok") == 1, f"一个恢复码只能用一次，得到 {outcomes}"

    with mysql_session_factory() as session:
        used = list(
            session.execute(
                select(RecoveryCode.id).where(RecoveryCode.used_at.is_not(None))
            ).scalars()
        )
    assert len(used) == 1
