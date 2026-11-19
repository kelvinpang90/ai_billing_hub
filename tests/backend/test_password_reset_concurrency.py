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

import datetime as dt
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, delete, event, func, select, text, update

from alembic import command
from app.core.config import Settings
from app.core.database import create_session_factory
from app.core.errors import AppError
from app.core.passwords import hash_password, verify_password
from app.models.auth import (
    AuditAction,
    AuditLog,
    DomainOutbox,
    PasswordResetToken,
    RefreshToken,
    TokenPurpose,
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
# 场景 ③ 的两个账号（设计闸门 #751 v3 §7）。清场时与 `EMAIL` 一起删。
GAP_EMAILS = ("concurrency-gap-1@example.com", "concurrency-gap-2@example.com")


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
    # 审计表用 TRUNCATE：它是 DDL、不经触发器，AIH-TASK-021 给 audit_logs 加上只追加的
    # 触发器之后 DELETE 会被拒（与 test_wallet_repository.py 清账本同一做法）。
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE TABLE audit_logs"))
    with factory() as session:
        session.execute(delete(DomainOutbox))
        session.execute(delete(PasswordResetToken))
        session.execute(delete(RefreshToken))
        session.execute(delete(User).where(User.email.in_((EMAIL, *GAP_EMAILS))))
        session.commit()
    yield factory
    engine.dispose()


def make_user(session_factory, email: str = EMAIL) -> int:
    now = utc_now()
    with session_factory() as session:
        user = User(
            email=email,
            password_hash=hash_password(PASSWORD),
            role=UserRole.ADMIN,
            status=UserStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
        session.add(user)
        session.commit()
        return int(user.id)


def issue_token(session_factory, settings: Settings, *, now: dt.datetime | None = None) -> str:
    # ⚠️ 断言真的签发了：按账号限频（#751 v3，I4）之后，窗口内的第二次申请返回 `None`，
    # 不断言的话下面会悄悄读回上一张令牌的 outbox 行。
    outbox_id = request_reset(session_factory, settings, email=EMAIL, context=CONTEXT, now=now)
    assert outbox_id is not None, "这次申请被限频了：前一次签发要传早于窗口的 `now`"
    with session_factory() as session:
        row = session.execute(
            select(DomainOutbox).order_by(DomainOutbox.id.desc()).limit(1)
        ).scalar_one()
        return str(json.loads(row.payload_json)["token"])


def before_the_window(settings: Settings) -> dt.datetime:
    """A whole-second moment old enough not to throttle a request made now, yet still live."""
    seconds = settings.password_reset_min_interval_seconds + 60
    return utc_now().replace(microsecond=0) - dt.timedelta(seconds=seconds)


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
    first = issue_token(session_factory, settings, now=before_the_window(settings))
    second = issue_token(session_factory, settings)
    assert first != second
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
    winner = issue_token(session_factory, settings, now=before_the_window(settings))
    loser = issue_token(session_factory, settings)
    assert winner != loser
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


# --- 按账号限频（设计闸门 #751 v3 §4、§7，I4）--------------------------------------
#
# ⚠️ 这四条守的是 `_start_reset` 在 MySQL 上用 READ COMMITTED 这一个决定，SQLite 上一条也测不出：
# ① 删掉设 RC 的那一句（回到 RR）会变红；③ 把 RC 换回 v1 的 `with_for_update()` 锁读会变红；
# ④ 守「RC 只动这一个事务、不泄漏到连接池」。

POLL_INTERVAL_SECONDS = 0.05
WAIT_LIMIT_SECONDS = 10

# 只看本库（xdist 各 worker 的库在同一台 MySQL 上）`users` 表上的锁等待。
# 不用 `information_schema.INNODB_TRX`：那是一份不满 0.1 秒不刷新、也不分库的缓存。
_WAITING_ON_USERS = text(
    "SELECT COUNT(*) FROM performance_schema.data_lock_waits AS w"
    " JOIN performance_schema.data_locks AS l"
    " ON l.ENGINE = w.ENGINE AND l.ENGINE_LOCK_ID = w.REQUESTING_ENGINE_LOCK_ID"
    " WHERE l.OBJECT_SCHEMA = DATABASE() AND l.OBJECT_NAME = 'users'"
)

_LOCKS_ON_RESET_TOKENS = text(
    "SELECT LOCK_TYPE, LOCK_MODE, LOCK_STATUS, LOCK_DATA FROM performance_schema.data_locks"
    " WHERE OBJECT_SCHEMA = DATABASE() AND OBJECT_NAME = 'password_reset_tokens'"
)


def wait_until_someone_waits_on_users(engine) -> bool:
    deadline = time.monotonic() + WAIT_LIMIT_SECONDS
    with engine.connect() as probe:
        while time.monotonic() < deadline:
            if probe.execute(_WAITING_ON_USERS).scalar_one():
                return True
            probe.rollback()
            time.sleep(POLL_INTERVAL_SECONDS)
    return False


def reset_tokens_of(session_factory, user_id: int) -> list[PasswordResetToken]:
    with session_factory() as session:
        return list(
            session.execute(
                select(PasswordResetToken).where(PasswordResetToken.user_id == user_id)
            ).scalars()
        )


def count_rows(session_factory, model) -> int:
    with session_factory() as session:
        return int(session.execute(select(func.count()).select_from(model)).scalar_one())


def reset_requested_audits(session_factory) -> int:
    with session_factory() as session:
        return int(
            session.execute(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == AuditAction.PASSWORD_RESET_REQUESTED)
            ).scalar_one()
        )


@needs_mysql
def test_a_request_queued_on_the_user_lock_sees_the_token_committed_meanwhile(
    session_factory, settings: Settings
) -> None:
    """场景 ①：后到者在用户行锁上排队，拿到锁时先到者已提交 —— 它必须看到那张令牌。

    RR 下后到者的快照建立在它第一次普通读（按邮箱找用户）时，早于先到者提交；拿到锁之后
    的窗口查询读的仍是那份旧快照，看不到令牌，于是再发一封。RC 下那条查询执行时才取读视图。
    """
    user_id = make_user(session_factory)
    engine = session_factory.kw["bind"]
    moment = utc_now().replace(microsecond=0)
    outcome: dict[str, object] = {}

    def run() -> None:
        try:
            outcome["result"] = request_reset(
                session_factory,
                settings,
                email=EMAIL,
                context=CONTEXT,
                now=moment + dt.timedelta(seconds=1),
            )
        except Exception as exc:  # noqa: BLE001 - reported by the assertions below
            outcome["error"] = exc

    worker = threading.Thread(target=run)
    with engine.connect() as holder:
        # 先到者：锁住用户行，插入一张窗口内的令牌，先不提交。
        holder.execute(select(User.id).where(User.id == user_id).with_for_update())
        holder.execute(
            PasswordResetToken.__table__.insert().values(
                user_id=user_id,
                token_hash="0" * 64,
                purpose=TokenPurpose.PASSWORD_RESET.value,
                created_at=moment,
                expires_at=moment + dt.timedelta(seconds=settings.password_reset_ttl_seconds),
            )
        )
        worker.start()
        try:
            queued = wait_until_someone_waits_on_users(engine)
        finally:
            holder.commit()
    worker.join(15)

    assert queued, "后到者没有在 users 行锁上排队，编排没有成立"
    assert not worker.is_alive(), "后到者在先到者提交后仍未返回"
    assert "error" not in outcome, f"后到者出错：{outcome.get('error')!r}"
    assert outcome["result"] is None, "后到者必须被限频：它拿到锁时先到者的令牌已经提交"
    assert [t.token_hash for t in reset_tokens_of(session_factory, user_id)] == ["0" * 64]
    assert count_rows(session_factory, DomainOutbox) == 0
    assert reset_requested_audits(session_factory) == 0


@needs_mysql
def test_two_simultaneous_requests_for_one_account_send_one_email(
    session_factory, settings: Settings
) -> None:
    """场景 ②：同一账号两次并发申请，20 轮，每轮恰好一张令牌、一行 outbox、一条审计。"""
    user_id = make_user(session_factory)
    base = utc_now().replace(microsecond=0)
    # 每轮往后挪过一个窗口，上一轮的令牌不挡下一轮。
    step = dt.timedelta(seconds=settings.password_reset_min_interval_seconds + 1)

    for round_no in range(20):
        moment = base + round_no * step
        start = threading.Barrier(2)

        def attempt(_i: int, moment: dt.datetime = moment, start=start) -> int | None:  # noqa: ANN001
            start.wait(WAIT_LIMIT_SECONDS)
            return request_reset(
                session_factory, settings, email=EMAIL, context=CONTEXT, now=moment
            )

        # 任何一边抛异常都会从 `pool.map` 冒出来让用例失败（「两个线程都正常返回」）。
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, [0, 1]))

        issued = [r for r in results if r is not None]
        assert len(issued) == 1, f"第 {round_no} 轮必须恰好签发一次，实际 {results}"
        in_round = [t for t in reset_tokens_of(session_factory, user_id) if t.created_at == moment]
        assert len(in_round) == 1, f"第 {round_no} 轮必须恰好一张令牌，实际 {len(in_round)}"

    assert len(reset_tokens_of(session_factory, user_id)) == 20
    assert count_rows(session_factory, DomainOutbox) == 20
    assert reset_requested_audits(session_factory) == 20


@needs_mysql
def test_two_accounts_in_the_same_gap_share_no_lock(session_factory, settings: Settings) -> None:
    """场景 ③：两个不同账号的窗口查询落在同一间隙（新索引末尾的 supremum），不能死锁。

    两个线程都做完窗口查询、都还没插入令牌时停在屏障上；此刻 `password_reset_tokens`
    上不能有任何锁。v1 的 `with_for_update()` 锁读在 RR 下会在这里留下 supremum 间隙锁，
    放行后两边的插入意向锁互相挡住，一方以 1213 死锁失败 —— 而只有存在且 ACTIVE 的账号
    才走得到这里，那个 500 就是账号存在的信号。
    """
    first_id = make_user(session_factory, GAP_EMAILS[0])
    second_id = make_user(session_factory, GAP_EMAILS[1])
    assert first_id < second_id
    engine = session_factory.kw["bind"]

    arrived = threading.Barrier(3)
    release = threading.Barrier(3)
    isolation_levels: list[str] = []

    def hold_before_token_insert(conn, _cursor, statement, _params, _context, _many) -> None:  # noqa: ANN001
        if not statement.lstrip().upper().startswith("INSERT INTO PASSWORD_RESET_TOKENS"):
            return
        # 用这个事务自己的连接读隔离级别：证明 RC 落在了真正签发的那个事务上。
        probe = conn.connection.cursor()
        try:
            probe.execute("SELECT @@transaction_isolation")
            isolation_levels.append(str(probe.fetchone()[0]))
        finally:
            probe.close()
        arrived.wait(WAIT_LIMIT_SECONDS)
        release.wait(WAIT_LIMIT_SECONDS)

    outcomes: dict[str, object] = {}

    def run(email: str) -> None:
        try:
            outcomes[email] = request_reset(session_factory, settings, email=email, context=CONTEXT)
        except Exception as exc:  # noqa: BLE001 - reported by the assertions below
            outcomes[email] = exc

    threads = [threading.Thread(target=run, args=(email,)) for email in GAP_EMAILS]
    event.listen(engine, "before_cursor_execute", hold_before_token_insert)
    try:
        for thread in threads:
            thread.start()
        try:
            arrived.wait(WAIT_LIMIT_SECONDS)
            with engine.connect() as probe:
                locks = [tuple(row) for row in probe.execute(_LOCKS_ON_RESET_TOKENS)]
            release.wait(WAIT_LIMIT_SECONDS)
        except BaseException:
            arrived.abort()
            release.abort()
            raise
        finally:
            for thread in threads:
                thread.join(30)
    finally:
        event.remove(engine, "before_cursor_execute", hold_before_token_insert)

    assert locks == [], f"两个线程都停在插入之前时，令牌表上不该有任何锁：{locks}"
    assert isolation_levels == ["READ-COMMITTED", "READ-COMMITTED"]
    assert all(isinstance(outcomes.get(email), int) for email in GAP_EMAILS), (
        f"两个账号都必须正常签发（没有 1213）：{outcomes}"
    )
    assert len(reset_tokens_of(session_factory, first_id)) == 1
    assert len(reset_tokens_of(session_factory, second_id)) == 1
    assert count_rows(session_factory, DomainOutbox) == 2


@needs_mysql
def test_read_committed_does_not_leak_back_into_the_pool(
    session_factory, settings: Settings, monkeypatch
) -> None:
    """场景 ④：连接池只有一条连接，每条路径之后借到的仍是 REPEATABLE READ。"""
    user_id = make_user(session_factory)
    engine = create_engine(TEST_DATABASE_URL, pool_size=1, max_overflow=0)
    factory = create_session_factory(engine)
    interval = dt.timedelta(seconds=settings.password_reset_min_interval_seconds)
    moment = utc_now().replace(microsecond=0)
    seen: list[tuple[str, str, int]] = []

    def check(path: str) -> None:
        with factory() as session:
            level, connection_id = session.execute(
                text("SELECT @@transaction_isolation, CONNECTION_ID()")
            ).one()
        seen.append((path, str(level), int(connection_id)))

    def explode() -> str:
        raise RuntimeError("injected inside _start_reset")

    def ask(email: str = EMAIL, *, at: dt.datetime) -> int | None:
        return request_reset(factory, settings, email=email, context=CONTEXT, now=at)

    try:
        assert ask(at=moment) is not None
        check("issued")
        assert ask(at=moment + dt.timedelta(seconds=1)) is None
        check("throttled")
        assert ask("nobody@example.com", at=moment) is None
        check("unknown")
        with monkeypatch.context() as patch:
            patch.setattr("app.services.password_reset.generate_opaque_token", explode)
            with pytest.raises(RuntimeError, match="injected"):
                ask(at=moment + interval + dt.timedelta(seconds=1))
        check("raised")
        with factory() as session:
            session.execute(
                update(User).where(User.id == user_id).values(status=UserStatus.DISABLED)
            )
            session.commit()
        assert ask(at=moment + 2 * interval + dt.timedelta(seconds=2)) is None
        check("inactive")
    finally:
        engine.dispose()

    paths = ("issued", "throttled", "unknown", "raised", "inactive")
    assert [(path, level) for path, level, _ in seen] == [(p, "REPEATABLE-READ") for p in paths]
    assert len({connection_id for *_, connection_id in seen}) == 1, "必须是同一条物理连接"
