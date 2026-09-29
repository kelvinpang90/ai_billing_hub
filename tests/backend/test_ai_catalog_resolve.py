"""Model resolution and alias segments over time (design gate #163 v4 §7, AIH-TASK-025).

四类用例：

- **解析与分段**（SQLite，冻结时钟）：解析顺序、第一次映射对过去生效、改指向不影响过去、
  撤销与重新映射、边界时刻、同一秒内连续改映射、未来时刻的事件、段的复查、加锁顺序，以及
  「解析的时间稳定性」性质用例；
- **事务中途失败**（审计写入、提交抛错）：`factory` 夹具的两个参数，在 SQLite 与真 MySQL 上
  各跑一次；
- **数据库兜底**（真 MySQL）：第二个未截断段、倒置的段、跨供应商的别名、删有引用的模型与
  供应商；
- **锁与并发**（真 MySQL，多个连接）：同时建同名模型、同时映射同一字符串、模型与别名撞名；
  「解析与改映射并发」两种顺序、「快照读陷阱」、「共享锁互不阻塞」。

MySQL 那部分需要 `BILLING_TEST_DATABASE_URL` 指向一个**可以被清空的**库，没设时 skip ——
**不要把 skipped 读成 passed**，CI 设了它并把 skipped 判成失败。
"""

from __future__ import annotations

import datetime as dt
import inspect
import os
import random
import threading
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, delete, func, insert, select
from sqlalchemy.dialects import mysql
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.core.database import create_session_factory
from app.models.ai_catalog import AiModel, AiModelAlias, AiProvider
from app.models.auth import AuditLog, User, UserRole, UserStatus
from app.models.base import Base
from app.repositories import ai_catalog as catalog_repository
from app.repositories.ai_catalog import is_due, resolve_model
from app.schemas.ai_catalog import AliasSegmentView, ModelView
from app.services import ai_catalog
from app.services.ai_catalog import (
    AliasNotFound,
    AliasSegmentsBroken,
    AliasTaken,
    MappedAlias,
    ModelCodeTaken,
    boundary_after,
    verify_segments,
)
from app.services.auth import RequestContext, utc_now

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")
TEST_EMAIL_DOMAIN = "@ai-catalog-test.example.com"
CONTEXT = RequestContext(ip_address="203.0.113.9", user_agent="ai-catalog-test")

T0 = dt.datetime(2026, 9, 29, 8, 30, 0)
LONG_AGO = dt.datetime(2000, 1, 1)
MICRO = dt.timedelta(microseconds=1)
SECOND = dt.timedelta(seconds=1)

# 全零占位值（secret-scan）。
ZERO_ID = "00000000-0000-4000-8000-000000000000"


# --- 帮手 -----------------------------------------------------------------------


class Clock:
    """A frozen clock. The services call it once they hold the provider lock."""

    def __init__(self, moment: dt.datetime) -> None:
        self.now = moment

    def __call__(self) -> dt.datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += dt.timedelta(**delta)


def make_admin(factory: sessionmaker[Session]) -> User:
    """A committed ADMIN, detached with its columns loaded — what `require_admin` returns."""
    now = utc_now()
    with factory() as session:
        user = User(
            email=f"{uuid.uuid4().hex}{TEST_EMAIL_DOMAIN}",
            password_hash="not-a-real-hash",
            role=UserRole.ADMIN,
            status=UserStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
        session.add(user)
        session.commit()
        return user


def resolved(
    factory: sessionmaker[Session], provider: str, string: str, occurred_at: dt.datetime
) -> str | None:
    """Resolve in a transaction of its own and commit — what one billing run looks like."""
    with factory() as session:
        found = resolve_model(session, provider, string, occurred_at)
        code = None if found is None else found.code
        session.commit()
        return code


class Catalog:
    """One admin and one provider, driven through the services."""

    def __init__(
        self,
        factory: sessionmaker[Session],
        clock: Callable[[], dt.datetime],
        provider_code: str = "anthropic",
    ) -> None:
        self.factory = factory
        self.clock = clock
        self.admin = make_admin(factory)
        self.code = provider_code
        self.provider = ai_catalog.create_provider(
            factory,
            actor=self.admin,
            code=provider_code,
            display_name=provider_code,
            context=CONTEXT,
            clock=clock,
        )
        self.models: dict[str, str] = {}

    def model(self, code: str) -> ModelView:
        view = ai_catalog.create_model(
            self.factory,
            actor=self.admin,
            provider_id=self.provider.id,
            code=code,
            display_name=code,
            context=CONTEXT,
            clock=self.clock,
        )
        self.models[code] = view.id
        return view

    def map(self, alias: str, model_code: str) -> MappedAlias:
        return ai_catalog.map_alias(
            self.factory,
            actor=self.admin,
            provider_id=self.provider.id,
            alias=alias,
            model_id=self.models[model_code],
            context=CONTEXT,
            clock=self.clock,
        )

    def current(self, alias: str) -> str:
        """The public id of the untruncated segment of the string; a fake one if none."""
        listing = ai_catalog.list_aliases(
            self.factory,
            provider_id=self.provider.id,
            alias=alias,
            current=True,
            page=1,
            page_size=5,
        )
        return listing.items[0].id if listing.items else ZERO_ID

    def retire(self, alias: str) -> AliasSegmentView:
        return ai_catalog.retire_alias(
            self.factory,
            actor=self.admin,
            provider_id=self.provider.id,
            alias_id=self.current(alias),
            context=CONTEXT,
            clock=self.clock,
        )

    def edit(self, *, model_code: str | None = None, **changes: object) -> None:
        if model_code is None:
            ai_catalog.update_provider(
                self.factory,
                actor=self.admin,
                provider_id=self.provider.id,
                changes=changes,
                context=CONTEXT,
                clock=self.clock,
            )
            return
        ai_catalog.update_model(
            self.factory,
            actor=self.admin,
            provider_id=self.provider.id,
            model_id=self.models[model_code],
            changes=changes,
            context=CONTEXT,
            clock=self.clock,
        )

    def segments(self, alias: str) -> list[AliasSegmentView]:
        listing = ai_catalog.list_aliases(
            self.factory,
            provider_id=self.provider.id,
            alias=alias,
            current=False,
            page=1,
            page_size=100,
        )
        return listing.items

    def resolve(self, string: str, occurred_at: dt.datetime) -> str | None:
        return resolved(self.factory, self.code, string, occurred_at)


def snapshot(factory: sessionmaker[Session]) -> dict[str, object]:
    """Every catalog row a write may touch, as committed, plus the audit count."""
    statements = {
        "providers": select(
            AiProvider.code,
            AiProvider.display_name,
            AiProvider.status,
        ).order_by(AiProvider.id),
        "models": select(
            AiModel.code,
            AiModel.display_name,
            AiModel.status,
        ).order_by(AiModel.id),
        "segments": select(
            AiModelAlias.alias,
            AiModelAlias.model_id,
            AiModelAlias.effective_from,
            AiModelAlias.effective_to,
            AiModelAlias.closed_at,
        ).order_by(AiModelAlias.id),
    }
    stored: dict[str, object] = {}
    with factory() as session:
        for name, statement in statements.items():
            stored[name] = [tuple(row) for row in session.execute(statement)]
        count = select(func.count()).select_from(AuditLog)
        stored["audits"] = session.execute(count).scalar_one()
    return stored


# --- 夹具 -----------------------------------------------------------------------


@pytest.fixture
def sqlite_factory() -> Iterator[sessionmaker[Session]]:
    yield from _sqlite_factory()


@pytest.fixture
def mysql_factory(monkeypatch) -> Iterator[sessionmaker[Session]]:
    yield from _mysql_factory(monkeypatch)


@pytest.fixture(params=["sqlite", "mysql"])
def factory(request, monkeypatch) -> Iterator[sessionmaker[Session]]:
    if request.param == "mysql":
        yield from _mysql_factory(monkeypatch)
        return
    yield from _sqlite_factory()


def _sqlite_factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


def _mysql_factory(monkeypatch) -> Iterator[sessionmaker[Session]]:
    if not TEST_DATABASE_URL:
        pytest.skip("BILLING_TEST_DATABASE_URL is not set; the MySQL half needs a real MySQL")
    # 建表走 alembic：生成列、唯一索引、复合外键与 CHECK 以迁移 0012 建出来的为准。
    from app.core.config import get_settings

    monkeypatch.setenv("BILLING_DATABASE_URL", TEST_DATABASE_URL)
    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")
    get_settings.cache_clear()

    engine = create_engine(TEST_DATABASE_URL, pool_size=6, max_overflow=2)
    _clean(engine)
    try:
        yield create_session_factory(engine)
    finally:
        _clean(engine)
        engine.dispose()


def _clean(engine: Engine) -> None:
    """⚠️ 库是共享的：每个用例前后清场。计量类型的种子不碰。

    目录行没有删除接口（设计 §1）；这里是测试清场，直接删表行。审计表只能 TRUNCATE：0010 的
    触发器拒绝 DELETE，TRUNCATE 是 DDL、不经触发器。
    """
    with engine.begin() as connection:
        connection.exec_driver_sql("TRUNCATE TABLE audit_logs")
    with engine.begin() as connection:
        connection.execute(delete(AiModelAlias))
        connection.execute(delete(AiModel))
        connection.execute(delete(AiProvider))
        connection.execute(delete(User).where(User.email.like(f"%{TEST_EMAIL_DOMAIN}")))


# --- 解析顺序（设计 §7「解析顺序」） -------------------------------------------------


def test_resolution_order_and_exact_matching(sqlite_factory) -> None:
    catalog = Catalog(sqlite_factory, Clock(T0))
    catalog.model("claude-x")
    catalog.model("claude-y")
    catalog.map("claude-x-latest", "claude-y")

    assert catalog.resolve("claude-x", T0) == "claude-x"
    assert catalog.resolve("claude-x-latest", T0) == "claude-y"
    # 未知模型、未知供应商。
    assert catalog.resolve("claude-z", T0) is None
    assert resolved(sqlite_factory, "openai", "claude-x", T0) is None
    # 不做大小写或任何规范化。
    for variant in ("CLAUDE-X", "Claude-X-Latest", "claude-x ", " claude-x", "claude_x"):
        assert catalog.resolve(variant, T0) is None, variant
    assert resolved(sqlite_factory, "Anthropic", "claude-x", T0) is None


def test_retired_providers_and_models_still_resolve(sqlite_factory) -> None:
    """设计 §2「停用的含义」：解析不看状态，在途用量照样算钱。"""
    catalog = Catalog(sqlite_factory, Clock(T0))
    catalog.model("claude-x")
    catalog.model("claude-y")
    catalog.map("claude-x-latest", "claude-y")

    catalog.edit(model_code="claude-x", status="RETIRED")
    catalog.edit(model_code="claude-y", status="RETIRED")
    catalog.edit(status="RETIRED")

    assert catalog.resolve("claude-x", T0) == "claude-x"
    assert catalog.resolve("claude-x-latest", T0) == "claude-y"


def test_a_model_code_wins_over_an_alias_row(sqlite_factory) -> None:
    """解析先看模型代码。服务层不让两者撞名；这里绕过它直接写一段，只为钉住顺序。"""
    catalog = Catalog(sqlite_factory, Clock(T0))
    catalog.model("claude-x")
    catalog.model("claude-y")
    with sqlite_factory() as session:
        provider_id, target = session.execute(
            select(AiModel.provider_id, AiModel.id).where(AiModel.code == "claude-y")
        ).one()
        session.add(
            AiModelAlias(
                public_id=str(uuid.uuid4()),
                provider_id=provider_id,
                model_id=target,
                alias="claude-x",
                created_at=T0,
            )
        )
        session.commit()

    assert catalog.resolve("claude-x", T0) == "claude-x"


# --- 分段语义 -----------------------------------------------------------------------


def test_the_first_mapping_applies_to_the_past(sqlite_factory) -> None:
    """设计 §7「第一次映射对过去生效」：过去一律是「未知」，补上只把未知变成 M。"""
    catalog = Catalog(sqlite_factory, Clock(T0))
    catalog.model("claude-x")
    assert catalog.resolve("claude-x-latest", LONG_AGO) is None

    mapped = catalog.map("claude-x-latest", "claude-x")

    assert (mapped.created, mapped.segment.effective_from, mapped.segment.effective_to) == (
        True,
        None,
        None,
    )
    assert catalog.resolve("claude-x-latest", LONG_AGO) == "claude-x"
    assert catalog.resolve("claude-x-latest", T0) == "claude-x"


@pytest.mark.parametrize("offset", [0, 1, 400_000, 999_999], ids=["whole", "1us", "0.4s", "max"])
def test_remapping_never_changes_the_past(sqlite_factory, offset: int) -> None:
    """设计 §7「改指向不影响过去」「边界时刻」：`t` 是整秒、严格晚于 `now`；
    `t - 1µs` 得旧模型、`t` 得新模型；旧段的指向不变。
    """
    clock = Clock(T0)
    catalog = Catalog(sqlite_factory, clock)
    catalog.model("claude-x")
    catalog.model("claude-y")
    catalog.map("claude-latest", "claude-x")
    clock.now = T0 + dt.timedelta(seconds=10, microseconds=offset)

    mapped = catalog.map("claude-latest", "claude-y")

    t = mapped.segment.effective_from
    assert t == T0 + dt.timedelta(seconds=11)
    assert t > clock.now
    assert catalog.resolve("claude-latest", LONG_AGO) == "claude-x"
    assert catalog.resolve("claude-latest", clock.now) == "claude-x"
    assert catalog.resolve("claude-latest", t - MICRO) == "claude-x"
    assert catalog.resolve("claude-latest", t) == "claude-y"
    old, new = catalog.segments("claude-latest")
    assert (old.model_code, old.effective_from, old.effective_to) == ("claude-x", None, t)
    assert (new.model_code, new.effective_from, new.effective_to) == ("claude-y", t, None)
    assert old.closed_at == T0 + dt.timedelta(seconds=10)


def test_boundary_after_rounds_up_to_the_next_whole_second() -> None:
    assert boundary_after(T0) == T0 + SECOND
    assert boundary_after(T0 + MICRO) == T0 + SECOND
    assert boundary_after(T0 + dt.timedelta(microseconds=999_999)) == T0 + SECOND
    assert boundary_after(T0 + SECOND) == T0 + 2 * SECOND


def test_retire_and_remap_fill_the_gap_end_to_end(sqlite_factory) -> None:
    """设计 §7「撤销与重新映射」：三段首尾相接；撤销期间的时刻在重新映射前是未知、之后得 N。"""
    clock = Clock(T0)
    catalog = Catalog(sqlite_factory, clock)
    for code in ("model-l", "model-m", "model-n"):
        catalog.model(code)
    catalog.map("x", "model-l")
    clock.advance(seconds=5)
    t0 = catalog.map("x", "model-m").segment.effective_from
    clock.advance(seconds=15)
    t1 = catalog.retire("x").effective_to
    assert (t0, t1) == (T0 + 6 * SECOND, T0 + 21 * SECOND)
    gap = t1 + 9 * SECOND

    assert catalog.resolve("x", t0 - MICRO) == "model-l"
    assert catalog.resolve("x", t1 - MICRO) == "model-m"
    assert catalog.resolve("x", t1) is None
    assert catalog.resolve("x", gap) is None

    clock.advance(seconds=40)
    reopened = catalog.map("x", "model-n").segment

    assert reopened.effective_from == t1
    assert catalog.resolve("x", t0 - MICRO) == "model-l"
    assert catalog.resolve("x", t1 - MICRO) == "model-m"
    assert catalog.resolve("x", t1) == "model-n"
    assert catalog.resolve("x", gap) == "model-n"
    assert catalog.resolve("x", clock.now + 100 * SECOND) == "model-n"
    ends = [(s.model_code, s.effective_from, s.effective_to) for s in catalog.segments("x")]
    assert ends == [("model-l", None, t0), ("model-m", t0, t1), ("model-n", t1, None)]


def test_changes_within_one_second_never_make_an_empty_segment(sqlite_factory) -> None:
    """同一秒内连续改映射、再撤销：每次的 `t` 都比当前段的起点晚一个整秒，段不倒置。"""
    clock = Clock(T0 + dt.timedelta(microseconds=500_000))
    catalog = Catalog(sqlite_factory, clock)
    for code in ("model-l", "model-m", "model-n"):
        catalog.model(code)
    catalog.map("x", "model-l")

    first = catalog.map("x", "model-m").segment.effective_from
    second = catalog.map("x", "model-n").segment.effective_from
    ended = catalog.retire("x").effective_to

    assert (first, second, ended) == (T0 + SECOND, T0 + 2 * SECOND, T0 + 3 * SECOND)
    assert all(moment > clock.now for moment in (first, second, ended))
    assert catalog.resolve("x", clock.now) == "model-l"
    assert catalog.resolve("x", first) == "model-m"
    assert catalog.resolve("x", second) == "model-n"
    assert catalog.resolve("x", ended) is None


def test_an_event_later_than_the_locked_now_is_not_due() -> None:
    """设计 §7「未来时刻的事件」：T-H 只处理 `occurred_at ≤` 持锁后当前时间的事件。"""
    assert is_due(T0, T0)
    assert is_due(T0 - MICRO, T0)
    assert is_due(LONG_AGO, T0)
    assert not is_due(T0 + MICRO, T0)
    assert not is_due(T0 + SECOND, T0)


def test_a_resolved_time_never_changes_its_model(sqlite_factory) -> None:
    """设计 §7「解析的时间稳定性」性质用例。

    随机的「建模型 / 映射 / 改指向 / 撤销」序列（时钟随机前进，包括不动与不足一秒）；每一步
    之后，对不晚于此刻的一组固定历史时刻逐个解析 —— T-H 只解析这样的时刻。一个
    `(字符串, 时刻)` 一旦得到某个模型，之后每一步都必须仍得到它。
    """
    rng = random.Random(20260929)
    clock = Clock(T0 - 3 * SECOND)
    catalog = Catalog(sqlite_factory, clock)
    for code in ("m-a", "m-b", "m-c"):
        catalog.model(code)
    strings = ["s-1", "s-2", "s-3"]
    # 半秒一格：整秒的格子恰好落在边界 `t` 上，半秒的落在段内。
    probes = [T0 + dt.timedelta(milliseconds=500 * k) for k in range(-6, 120)]
    steps = [0, 1, 250_000, 999_999, 1_000_000, 1_500_000]
    seen: dict[tuple[str, dt.datetime], str] = {}
    changes = 0

    for step in range(60):
        clock.advance(microseconds=rng.choice(steps))
        operation = rng.choice(["map", "map", "map", "retire", "model"])
        string = rng.choice(strings)
        try:
            if operation == "map":
                changes += catalog.map(string, rng.choice(sorted(catalog.models))).created
            elif operation == "retire":
                catalog.retire(string)
                changes += 1
            else:
                catalog.model(string)
                changes += 1
        except (AliasNotFound, AliasTaken, ModelCodeTaken):
            pass
        with sqlite_factory() as session:
            for probe in probes:
                if probe > clock.now:
                    break
                for candidate in strings:
                    found = resolve_model(session, catalog.code, candidate, probe)
                    code = None if found is None else found.code
                    key = (candidate, probe)
                    if key in seen:
                        assert code == seen[key], (step, operation, string, key)
                    elif code is not None:
                        seen[key] = code

    # 序列里确实发生了足够多的变化，也确实解析出了模型 —— 否则上面的断言在空转。
    assert changes >= 20
    assert len(seen) >= 20


# --- 复查与加锁 ---------------------------------------------------------------------


def _segment(row_id: int, start: dt.datetime | None, end: dt.datetime | None) -> AiModelAlias:
    return AiModelAlias(id=row_id, alias="x", effective_from=start, effective_to=end)


@pytest.mark.parametrize(
    ("rows", "broken"),
    [
        ([], False),
        ([(None, None)], False),
        ([(None, T0), (T0, None)], False),
        ([(None, T0), (T0, T0 + SECOND), (T0 + SECOND, None)], False),
        ([(None, T0)], False),
        ([(T0, None)], True),
        ([(None, T0), (T0 + SECOND, None)], True),
        ([(None, T0 + SECOND), (T0, None)], True),
        ([(None, T0), (None, None)], True),
        ([(None, None), (T0, None)], True),
        ([(None, T0), (T0, T0), (T0, None)], True),
    ],
    ids=[
        "empty",
        "one-open",
        "two-chained",
        "three-chained",
        "one-retired",
        "first-has-a-start",
        "gap",
        "overlap",
        "two-from-the-beginning",
        "open-then-more",
        "empty-segment",
    ],
)
def test_verify_segments(rows: list, broken: bool) -> None:
    segments = [_segment(index + 1, start, end) for index, (start, end) in enumerate(rows)]
    # 顺序打乱也一样：复查自己排序。
    segments.reverse()

    if broken:
        with pytest.raises(AliasSegmentsBroken):
            verify_segments(segments)
    else:
        verify_segments(segments)


def test_a_failed_recheck_rolls_the_map_back(sqlite_factory, monkeypatch) -> None:
    """设计 §2：写别名后同一事务里复查首尾相接，不满足就回滚 —— 旧段也不会被截断。"""
    clock = Clock(T0)
    catalog = Catalog(sqlite_factory, clock)
    catalog.model("model-m")
    catalog.model("model-n")
    catalog.map("x", "model-m")
    before = snapshot(sqlite_factory)
    real = catalog_repository.insert_segment

    def shifted(session: Session, **options: object):
        # 模拟实现错误：新段的起点比截断处晚一秒，两段之间留了空档。
        start = options["effective_from"]
        assert isinstance(start, dt.datetime)
        return real(session, **{**options, "effective_from": start + SECOND})

    monkeypatch.setattr(catalog_repository, "insert_segment", shifted)
    clock.advance(seconds=5)

    with pytest.raises(AliasSegmentsBroken):
        catalog.map("x", "model-n")

    assert snapshot(sqlite_factory) == before


def test_a_failed_recheck_rolls_the_retire_back(sqlite_factory, monkeypatch) -> None:
    catalog = Catalog(sqlite_factory, Clock(T0))
    catalog.model("model-m")
    catalog.map("x", "model-m")
    before = snapshot(sqlite_factory)

    def failing(_rows: object) -> None:
        raise AliasSegmentsBroken("injected")

    monkeypatch.setattr(ai_catalog, "verify_segments", failing)

    with pytest.raises(AliasSegmentsBroken):
        catalog.retire("x")

    assert snapshot(sqlite_factory) == before


def test_alias_and_model_writes_read_the_clock_only_after_the_provider_lock(
    sqlite_factory, monkeypatch
) -> None:
    """设计 §2「时间」：`t` 在拿到供应商行锁之后取。"""
    calls: list[str] = []
    real_lock = catalog_repository.lock_provider

    def lock_spy(session: Session, public_id: str):
        calls.append("lock")
        return real_lock(session, public_id)

    def clock() -> dt.datetime:
        calls.append("clock")
        return T0

    catalog = Catalog(sqlite_factory, clock)
    monkeypatch.setattr(catalog_repository, "lock_provider", lock_spy)
    calls.clear()

    catalog.model("model-m")
    catalog.map("x", "model-m")
    alias_id = catalog.current("x")
    calls.clear()
    ai_catalog.retire_alias(
        sqlite_factory,
        actor=catalog.admin,
        provider_id=catalog.provider.id,
        alias_id=alias_id,
        context=CONTEXT,
        clock=clock,
    )
    assert calls == ["lock", "clock"]

    calls.clear()
    catalog.model("model-n")
    catalog.map("x", "model-n")
    assert calls == ["lock", "clock", "lock", "clock"]


def test_resolution_takes_share_locks_and_writes_take_exclusive_ones() -> None:
    """设计 §2：解析对供应商、模型、别名都用 `FOR SHARE`；写路径锁供应商行 `FOR UPDATE`。"""
    shared = catalog_repository._shared(select(AiProvider))
    compiled = str(shared.compile(dialect=mysql.dialect()))
    assert "LOCK IN SHARE MODE" in compiled or "FOR SHARE" in compiled
    assert "with_for_update(read=True)" in inspect.getsource(catalog_repository._shared)
    # 供应商、按代码的模型、别名段、别名指向的模型：四次读都是共享锁读。
    assert inspect.getsource(resolve_model).count("_shared(") == 4
    assert ".with_for_update()" in inspect.getsource(catalog_repository.lock_provider)


def test_there_is_no_delete_path_and_no_logging() -> None:
    """设计 §1：只停用、不删除；这两层不写日志。"""
    for module in (catalog_repository, ai_catalog):
        source = inspect.getsource(module)
        assert "delete" not in source.lower(), module.__name__
        assert "logging" not in source, module.__name__
        assert "logger" not in source, module.__name__


# --- 事务中途失败：SQLite 与 MySQL 各一次（INV-13） ---------------------------------------


def break_the_audit(monkeypatch) -> None:
    """The audit row fails at flush: `created_at` is NULL (NOT NULL → IntegrityError)."""
    real = ai_catalog.record_audit

    def broken(session: Session, **options: object) -> None:
        real(session, **{**options, "now": None})  # type: ignore[arg-type]

    monkeypatch.setattr(ai_catalog, "record_audit", broken)


def break_the_commit(monkeypatch) -> None:
    def failing_commit(self: Session) -> None:
        raise RuntimeError("injected commit failure")

    monkeypatch.setattr(Session, "commit", failing_commit)


ACTIONS: dict[str, Callable[[Catalog], object]] = {
    "create_provider": lambda catalog: ai_catalog.create_provider(
        catalog.factory,
        actor=catalog.admin,
        code="openai",
        display_name="OpenAI",
        context=CONTEXT,
        clock=catalog.clock,
    ),
    "update_provider": lambda catalog: catalog.edit(display_name="Renamed", status="RETIRED"),
    "create_model": lambda catalog: catalog.model("model-new"),
    "update_model": lambda catalog: catalog.edit(model_code="model-m", status="RETIRED"),
    "first_map": lambda catalog: catalog.map("y", "model-m"),
    "remap": lambda catalog: catalog.map("x", "model-n"),
    "retire": lambda catalog: catalog.retire("x"),
}


@pytest.mark.parametrize("failure", ["audit", "commit"])
@pytest.mark.parametrize("action", sorted(ACTIONS))
def test_a_failed_write_leaves_nothing(factory, monkeypatch, action: str, failure: str) -> None:
    """设计 §7「事务中途失败」：审计写入或提交抛错，目录行与审计全部回滚。"""
    clock = Clock(T0)
    catalog = Catalog(factory, clock)
    catalog.model("model-m")
    catalog.model("model-n")
    catalog.map("x", "model-m")
    clock.advance(seconds=5)
    before = snapshot(factory)
    if failure == "audit":
        break_the_audit(monkeypatch)
    else:
        break_the_commit(monkeypatch)

    with pytest.raises(Exception):  # noqa: B017 - 两种注入各抛各的，这里只关心回滚
        ACTIONS[action](catalog)

    # 下面的核对只读、不提交，所以不必先撤掉提交的替身。
    assert snapshot(factory) == before


# --- 数据库兜底（真 MySQL） -----------------------------------------------------------

# MySQL 的错误号。
_ER_DUP_ENTRY = 1062
_ER_ROW_IS_REFERENCED_2 = 1451
_ER_NO_REFERENCED_ROW_2 = 1452
_ER_CHECK_CONSTRAINT_VIOLATED = 3819


def _ids(factory: sessionmaker[Session], model_code: str) -> tuple[int, int]:
    """(provider id, model id), both internal."""
    with factory() as session:
        statement = select(AiModel.provider_id, AiModel.id).where(AiModel.code == model_code)
        provider_id, model_id = session.execute(statement).one()
        return int(provider_id), int(model_id)


def _refused(factory: sessionmaker[Session], statement) -> int:
    """Run in a transaction that is rolled back; return the MySQL error number."""
    with factory() as session:
        with pytest.raises(DBAPIError) as raised:
            session.execute(statement)
        session.rollback()
    return int(raised.value.orig.args[0])


def _segment_values(provider_id: int, model_id: int, **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "public_id": str(uuid.uuid4()),
        "provider_id": provider_id,
        "model_id": model_id,
        "alias": "x",
        "effective_from": None,
        "effective_to": None,
        "created_at": T0,
    }
    values.update(overrides)
    return values


def test_the_database_allows_one_open_segment_and_no_inverted_one(mysql_factory) -> None:
    """设计 §7「至多一段未截断」「段不能倒置」：绕过服务层直接插，被唯一约束 / CHECK 拒绝。"""
    catalog = Catalog(mysql_factory, utc_now)
    catalog.model("model-m")
    catalog.map("x", "model-m")
    provider_id, model_id = _ids(mysql_factory, "model-m")
    before = snapshot(mysql_factory)

    second_open = _segment_values(provider_id, model_id, effective_from=T0)
    inverted = _segment_values(provider_id, model_id, effective_from=T0 + SECOND, effective_to=T0)
    empty = _segment_values(provider_id, model_id, effective_from=T0, effective_to=T0)

    assert _refused(mysql_factory, insert(AiModelAlias).values(**second_open)) == _ER_DUP_ENTRY
    for bad in (inverted, empty):
        refused = _refused(mysql_factory, insert(AiModelAlias).values(**bad))
        assert refused == _ER_CHECK_CONSTRAINT_VIOLATED
    assert snapshot(mysql_factory) == before

    # 多个已截断的段在 open_slot 上都是 NULL，可以并存（唯一索引允许多个 NULL）。
    with mysql_factory() as session:
        for start in (T0, T0 + SECOND):
            closed = _segment_values(
                provider_id,
                model_id,
                alias="closed",
                effective_from=start,
                effective_to=start + SECOND,
                closed_at=T0,
            )
            session.execute(insert(AiModelAlias).values(**closed))
        session.commit()


def test_the_database_keeps_an_alias_within_its_provider(mysql_factory) -> None:
    """设计 §7「别名跨供应商」：数据库层直接插入指向别家模型的段，被复合外键拒绝。"""
    mine = Catalog(mysql_factory, utc_now, "anthropic")
    theirs = Catalog(mysql_factory, utc_now, "openai")
    mine.model("claude-x")
    theirs.model("gpt-x")
    my_provider, _ = _ids(mysql_factory, "claude-x")
    _, their_model = _ids(mysql_factory, "gpt-x")
    before = snapshot(mysql_factory)

    crossed = _segment_values(my_provider, their_model, alias="gpt-x-latest")

    refused = _refused(mysql_factory, insert(AiModelAlias).values(**crossed))
    assert refused == _ER_NO_REFERENCED_ROW_2
    assert snapshot(mysql_factory) == before


def test_models_and_providers_in_use_cannot_be_deleted(mysql_factory) -> None:
    """设计 §7「不可删除」：外键 RESTRICT（INV-6）。"""
    catalog = Catalog(mysql_factory, utc_now)
    catalog.model("model-m")
    catalog.map("x", "model-m")
    provider_id, model_id = _ids(mysql_factory, "model-m")
    before = snapshot(mysql_factory)

    model_refused = _refused(mysql_factory, delete(AiModel).where(AiModel.id == model_id))
    provider_rows = delete(AiProvider).where(AiProvider.id == provider_id)
    provider_refused = _refused(mysql_factory, provider_rows)

    assert (model_refused, provider_refused) == (_ER_ROW_IS_REFERENCED_2,) * 2
    assert snapshot(mysql_factory) == before


# --- 并发撞车（真 MySQL） -----------------------------------------------------------------


def _race(*attempts: Callable[[], object]) -> list[object]:
    """Run the attempts at once, one thread each; return results or the exceptions raised."""
    start = threading.Barrier(len(attempts))

    def run(attempt: Callable[[], object]) -> object:
        start.wait(timeout=30)
        try:
            return attempt()
        except Exception as error:  # noqa: BLE001 - 调用方逐个判断
            return error

    with ThreadPoolExecutor(max_workers=len(attempts)) as pool:
        return list(pool.map(run, attempts))


def _stored_segments(factory: sessionmaker[Session], alias: str) -> list[AiModelAlias]:
    with factory() as session:
        rows = session.execute(select(AiModelAlias).where(AiModelAlias.alias == alias)).scalars()
        return list(rows)


def test_two_concurrent_creations_make_one_model(mysql_factory) -> None:
    catalog = Catalog(mysql_factory, utc_now)

    results = _race(lambda: catalog.model("model-m"), lambda: catalog.model("model-m"))

    created = [result for result in results if isinstance(result, ModelView)]
    refused = [result for result in results if isinstance(result, ModelCodeTaken)]
    assert (len(created), len(refused)) == (1, 1)
    with mysql_factory() as session:
        count = select(func.count()).select_from(AiModel).where(AiModel.code == "model-m")
        assert session.execute(count).scalar_one() == 1


def test_two_concurrent_maps_of_one_string_chain_up(mysql_factory) -> None:
    """同时把同一字符串映射到不同模型：串行完成，形成首尾相接的两段。"""
    catalog = Catalog(mysql_factory, utc_now)
    catalog.model("model-m")
    catalog.model("model-n")

    results = _race(lambda: catalog.map("x", "model-m"), lambda: catalog.map("x", "model-n"))

    assert all(isinstance(result, MappedAlias) for result in results), results
    rows = _stored_segments(mysql_factory, "x")
    verify_segments(rows)
    assert len(rows) == 2
    assert sum(row.effective_to is None for row in rows) == 1


def test_a_model_and_an_alias_with_one_name_race(mysql_factory) -> None:
    """一个建模型、一个映射同名字符串：恰好一方成功，另一方 409。"""
    catalog = Catalog(mysql_factory, utc_now)
    catalog.model("model-m")

    results = _race(lambda: catalog.model("race"), lambda: catalog.map("race", "model-m"))

    refused = [r for r in results if isinstance(r, ModelCodeTaken | AliasTaken)]
    succeeded = [r for r in results if isinstance(r, ModelView | MappedAlias)]
    assert (len(succeeded), len(refused)) == (1, 1), results
    counted = select(func.count())
    models = counted.select_from(AiModel).where(AiModel.code == "race")
    aliases = counted.select_from(AiModelAlias).where(AiModelAlias.alias == "race")
    with mysql_factory() as session:
        assert session.execute(models).scalar_one() + session.execute(aliases).scalar_one() == 1


# --- 解析与改映射的锁（真 MySQL） -----------------------------------------------------------


def _pause_inside_the_write(monkeypatch) -> tuple[threading.Event, threading.Event]:
    """Make the next catalog write stop just before its commit, holding its locks."""
    written, release = threading.Event(), threading.Event()
    real = ai_catalog.record_audit

    def pausing(session: Session, **options: object) -> None:
        real(session, **options)  # type: ignore[arg-type]
        session.flush()
        written.set()
        assert release.wait(timeout=60)

    monkeypatch.setattr(ai_catalog, "record_audit", pausing)
    return written, release


def test_a_resolution_waits_for_an_uncommitted_remap(mysql_factory, monkeypatch) -> None:
    """设计 §7「解析与改映射并发」①：改映射写完新段、未提交；解析阻塞到它提交，得新模型。"""
    catalog = Catalog(mysql_factory, utc_now)
    catalog.model("model-old")
    catalog.model("model-new")
    catalog.map("x", "model-old")
    written, release = _pause_inside_the_write(monkeypatch)
    # 晚于这次改映射可能取到的任何 `t`。
    occurred_at = utc_now() + dt.timedelta(days=1)

    with ThreadPoolExecutor(max_workers=2) as pool:
        remap = pool.submit(catalog.map, "x", "model-new")
        assert written.wait(timeout=30)
        resolution = pool.submit(resolved, mysql_factory, catalog.code, "x", occurred_at)
        with pytest.raises(TimeoutError):
            resolution.result(timeout=2)
        release.set()
        mapped = remap.result(timeout=30)
        assert resolution.result(timeout=30) == "model-new"

    assert mapped.segment.effective_from is not None
    assert mapped.segment.effective_from <= occurred_at
    # 提交之后再解析同一 (字符串, 时刻)，与那一次一致。
    assert catalog.resolve("x", occurred_at) == "model-new"


def test_a_remap_waits_for_an_uncommitted_resolution(mysql_factory) -> None:
    """设计 §7「解析与改映射并发」②：计费事务解析后未提交；改映射阻塞到它提交，`t` 晚于
    被解析事件的 `occurred_at`，提交后再解析与快照一致。
    """
    catalog = Catalog(mysql_factory, utc_now)
    catalog.model("model-old")
    catalog.model("model-new")
    catalog.map("x", "model-old")
    billing = mysql_factory()
    try:
        occurred_at = utc_now()
        found = resolve_model(billing, catalog.code, "x", occurred_at)
        assert found is not None
        snapshot_code = found.code
        assert is_due(occurred_at, utc_now())
        with ThreadPoolExecutor(max_workers=1) as pool:
            remap = pool.submit(catalog.map, "x", "model-new")
            with pytest.raises(TimeoutError):
                remap.result(timeout=2)
            # 改映射只能在这次提交之后拿到锁，它的 `t` 因此晚于此刻。
            before_commit = utc_now()
            billing.commit()
            mapped = remap.result(timeout=30)
    finally:
        billing.close()

    t = mapped.segment.effective_from
    assert snapshot_code == "model-old"
    assert t is not None
    assert t > occurred_at
    assert t > before_commit
    assert catalog.resolve("x", occurred_at) == snapshot_code
    assert catalog.resolve("x", t) == "model-new"


def test_resolution_reads_past_the_transaction_snapshot(mysql_factory) -> None:
    """设计 §7「快照读陷阱」：计费事务先做一次普通读（建立快照），别的连接改映射并提交，
    之后 `resolve_model` 仍得新段 —— 加锁读读的是最新提交，不是快照。
    """
    catalog = Catalog(mysql_factory, utc_now)
    catalog.model("model-old")
    catalog.model("model-new")
    catalog.map("x", "model-old")
    occurred_at = utc_now() + dt.timedelta(days=1)
    segments = select(func.count()).select_from(AiModelAlias).where(AiModelAlias.alias == "x")
    billing = mysql_factory()
    try:
        # 普通读：REPEATABLE READ 在这里建立一致性读快照。
        billing.execute(select(func.count()).select_from(User)).scalar_one()
        assert billing.execute(segments).scalar_one() == 1

        catalog.map("x", "model-new")

        # 陷阱确实存在：同一事务里的普通读仍是快照里的一段。
        assert billing.execute(segments).scalar_one() == 1
        found = resolve_model(billing, catalog.code, "x", occurred_at)
        assert found is not None
        assert found.code == "model-new"
        billing.commit()
    finally:
        billing.close()


def test_two_resolutions_do_not_block_each_other(mysql_factory) -> None:
    """设计 §7「共享锁互不阻塞」：两个计费事务同时解析同一供应商。"""
    catalog = Catalog(mysql_factory, utc_now)
    catalog.model("model-m")
    catalog.map("x", "model-m")
    first = mysql_factory()
    try:
        found = resolve_model(first, catalog.code, "x", T0)
        assert found is not None
        with ThreadPoolExecutor(max_workers=1) as pool:
            second = pool.submit(resolved, mysql_factory, catalog.code, "x", T0)
            # 第一个事务仍持有共享锁；第二个照样立刻拿到。
            assert second.result(timeout=10) == "model-m"
        first.commit()
    finally:
        first.close()
