"""Two reset links submitted at the same time (design gate Issue #32 §4).

⚠️ **这个文件只能对着真 MySQL 跑，而且理由不是「更真实一点」——是 SQLite 在这里
结构性地测不出东西：**

- `SELECT ... FOR UPDATE` 在 SQLite 方言下被 SQLAlchemy **静默忽略**，
  所以「用户级串行化」那把锁在内存库上根本不存在
- 行锁与死锁检测也不存在

也就是说，把 `with_for_update()` 整个删掉，`test_password_reset.py` 里那 24 条
**照样全绿**。这个文件就是为了让那种删除红起来。

需要 `BILLING_TEST_DATABASE_URL`，CI 的 `backend` job 已经设了，所以 CI 里不跳过。
本地没设时会 skip —— **不要把 skipped 读成 passed**。
"""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, delete, select

from alembic import command
from app.core.config import Settings
from app.core.database import create_session_factory
from app.core.errors import AppError
from app.core.passwords import hash_password, verify_password
from app.models.auth import (
    AuditLog,
    DomainOutbox,
    PasswordResetToken,
    RefreshToken,
    User,
    UserRole,
    UserStatus,
)
from app.services.auth import RequestContext, utc_now
from app.services.password_reset import InvalidResetToken, request_reset, reset_password

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")

needs_mysql = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="BILLING_TEST_DATABASE_URL is not set; row locking needs a real MySQL",
)

PASSWORD = "a-perfectly-fine-passphrase"
CONTEXT = RequestContext(ip_address="10.0.0.1", user_agent="pytest")
EMAIL = "concurrency@example.com"


@pytest.fixture
def settings() -> Settings:
    return Settings(password_reset_ttl_seconds=1_800)


@pytest.fixture
def session_factory(monkeypatch):
    # ⚠️ **建表必须走 alembic，不能用 `Base.metadata.create_all`。**
    # `create_all` 建出来的表不在 `alembic_version` 的记账里，于是
    # `test_migrations.py` 的 `downgrade("base")` 清不掉它们，紧接着的
    # `upgrade("head")` 撞 `(1050) Table already exists` —— **三条迁移用例连带红**，
    # 而且只在共享同一个库的 CI 上红。实测踩过。
    from app.core.config import get_settings

    monkeypatch.setenv("BILLING_DATABASE_URL", TEST_DATABASE_URL)
    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")
    get_settings.cache_clear()

    engine = create_engine(TEST_DATABASE_URL, pool_size=8, max_overflow=8)
    factory = create_session_factory(engine)
    # ⚠️ 每个用例自己清场：这张库是共享的，上一个用例留下的行会让断言读到别人的数据。
    with factory() as session:
        session.execute(delete(DomainOutbox))
        session.execute(delete(PasswordResetToken))
        session.execute(delete(RefreshToken))
        session.execute(delete(AuditLog))
        session.execute(delete(User).where(User.email == EMAIL))
        session.commit()
    yield factory
    engine.dispose()


def make_user(session_factory) -> int:
    now = utc_now()
    with session_factory() as session:
        user = User(
            email=EMAIL,
            password_hash=hash_password(PASSWORD),
            role=UserRole.ADMIN,
            status=UserStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
        session.add(user)
        session.commit()
        return int(user.id)


def issue_token(session_factory, settings: Settings) -> str:
    request_reset(session_factory, settings, email=EMAIL, context=CONTEXT)
    with session_factory() as session:
        row = session.execute(
            select(DomainOutbox).order_by(DomainOutbox.id.desc()).limit(1)
        ).scalar_one()
        return str(json.loads(row.payload_json)["token"])


@needs_mysql
def test_two_live_links_submitted_together_settle_cleanly(
    session_factory, settings: Settings
) -> None:
    """⚠️ **这条用例来自 PR #39 第二轮的一个阻断项。**

    同一个用户的两条有效链接同时提交时，原来的写法是：

        A 消费 token1（持 token1 的行锁）→ 去作废 token2
        B 消费 token2（持 token2 的行锁）→ 去作废 token1

    两边交叉等待，**MySQL 死锁**，其中一条回滚成 500 —— 而正确的结果是一条成功、
    另一条拿 `INVALID_RESET_TOKEN`。

    修法是先在 `users` 行上取锁，把加锁顺序统一成「先用户、后令牌」。
    """
    user_id = make_user(session_factory)
    first = issue_token(session_factory, settings)
    second = issue_token(session_factory, settings)
    passwords = {first: "first-brand-new-passphrase", second: "second-brand-new-passphrase"}

    def attempt(token: str):
        try:
            reset_password(
                session_factory,
                settings,
                token=token,
                new_password=passwords[token],
                context=CONTEXT,
            )
        except AppError as exc:
            return exc
        return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, [first, second]))

    # ⚠️ **一条成功、一条 InvalidResetToken。**任何别的组合都是缺陷：
    # 两条都成功 = 单用失效；出现别的异常 = 死锁漏成了 500。
    succeeded = [o for o in outcomes if o is None]
    refused = [o for o in outcomes if isinstance(o, InvalidResetToken)]
    others = [o for o in outcomes if o is not None and not isinstance(o, InvalidResetToken)]

    assert others == [], f"并发下不该出现别的错误（死锁会以 500 的形式冒出来）：{others}"
    assert len(succeeded) == 1, f"必须恰好一条成功，实际 {len(succeeded)}"
    assert len(refused) == 1, f"另一条必须是 INVALID_RESET_TOKEN，实际 {refused}"

    with session_factory() as session:
        still_usable = session.execute(
            select(PasswordResetToken).where(
                PasswordResetToken.user_id == user_id,
                PasswordResetToken.used_at.is_(None),
            )
        ).scalars()
        assert list(still_usable) == [], "两张令牌都该结掉了"

        user = session.get(User, user_id)
        assert user is not None
        # 密码必定是两个候选之一，而且**只有一个**生效。
        matched = [p for p in passwords.values() if verify_password(user.password_hash, p)]
        assert len(matched) == 1, f"最终密码必须恰好匹配一个候选，实际 {len(matched)}"


@needs_mysql
def test_the_loser_reports_an_invalid_token_not_a_weak_password(
    session_factory, settings: Settings, monkeypatch
) -> None:
    """⚠️ **这条钉的是「拿到用户锁之后要重读令牌」那一步。**

    它是阻断项①（错误码不能取决于密码内容）在**并发**下的样子，而上面那条用例
    看不见它 —— 两条都用强密码时，重不重读结果都一样。变异测试是这么发现的：
    把重读删掉，其余用例全绿。

    编排是确定的，不靠抢跑：

        A 进到强度校验就停住（持有 users 锁）
        B 这时开始：先无锁读到 token2 的快照（`used_at` 还是 NULL），然后卡在 users 锁上
        放行 A → A 消费 token1、顺带作废 token2、提交、释放锁
        B 拿到锁 —— **此刻它手上那份快照已经过期了**

    有重读：B 读到最新的 `used_at`，回 `INVALID_RESET_TOKEN` ✅
    没重读：B 拿旧快照当真，往下走到强度校验，回 `WEAK_PASSWORD` ❌
            —— 而 token2 此刻无论密码多强都不可能成功。
    """
    import threading

    from app.core import passwords as passwords_module

    make_user(session_factory)
    winner = issue_token(session_factory, settings)
    loser = issue_token(session_factory, settings)
    strong = "the-winning-brand-new-passphrase"

    real_validate = passwords_module.validate_password_strength
    a_is_holding_the_lock = threading.Event()
    let_a_finish = threading.Event()

    def staged_validate(password: str, *, email: str | None = None) -> None:
        if password == strong:
            a_is_holding_the_lock.set()
            # 卡在这里 —— 此时 A 已经持有 users 行锁。
            assert let_a_finish.wait(10), "编排超时"
        real_validate(password, email=email)

    monkeypatch.setattr("app.services.password_reset.validate_password_strength", staged_validate)

    outcomes: dict[str, object] = {}

    def run(name: str, token: str, password: str) -> None:
        try:
            reset_password(
                session_factory, settings, token=token, new_password=password, context=CONTEXT
            )
            outcomes[name] = None
        except AppError as exc:
            outcomes[name] = exc

    a = threading.Thread(target=run, args=("a", winner, strong))
    a.start()
    assert a_is_holding_the_lock.wait(10), "A 没能走到强度校验"

    # ⚠️ B 现在开始：它会先读到 token2 的**旧快照**，再卡在 users 锁上。
    b = threading.Thread(target=run, args=("b", loser, "短"))
    b.start()
    threading.Event().wait(0.5)  # 让 B 确实走到「等锁」那一步

    let_a_finish.set()
    a.join(15)
    b.join(15)

    assert outcomes["a"] is None, f"A 应当成功，实际 {outcomes['a']}"
    assert isinstance(outcomes["b"], InvalidResetToken), (
        f"B 手上那张令牌已经被 A 作废了，必须回 INVALID_RESET_TOKEN，实际 {outcomes['b']!r}"
    )


@needs_mysql
def test_the_same_link_submitted_twice_at_once_only_lands_once(
    session_factory, settings: Settings
) -> None:
    """同一张令牌被并发提交两次 —— 单用靠的是条件更新的受影响行数，不是先读后写。"""
    make_user(session_factory)
    token = issue_token(session_factory, settings)

    def attempt(_i: int):
        try:
            reset_password(
                session_factory,
                settings,
                token=token,
                new_password="a-brand-new-fine-passphrase",
                context=CONTEXT,
            )
        except AppError as exc:
            return exc
        return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, [0, 1]))

    assert len([o for o in outcomes if o is None]) == 1
    assert len([o for o in outcomes if isinstance(o, InvalidResetToken)]) == 1
