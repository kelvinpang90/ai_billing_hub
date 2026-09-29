"""`resolve_provider_price` and its serialisation with publishing (design gate #177 v3 §7).

三类用例：

- **取价**（SQLite，冻结时钟）：返回版本与它的分量（`component_code`、`quantity_field`、
  两个金额）；草稿、丢弃的草稿、空区间永不匹配；只看自己的（供应商, 模型）；加锁读的写法；
- **时间稳定性**（SQLite，性质用例）：随机的「发布 / 预约 / 退役 / 撤销预约」序列，每一步之后
  对不晚于此刻的一组固定时刻取价 —— 一旦取到版本 V，之后永远是 V；
- **与发布串行**（真 MySQL，多个连接）：「发布与计费并发」两种顺序、「快照读陷阱」。

MySQL 那部分需要 `BILLING_TEST_DATABASE_URL` 指向一个**可以被清空的**库，没设时 skip ——
**不要把 skipped 读成 passed**，CI 设了它并把 skipped 判成失败。价格一律是明显的虚构值。
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
from decimal import Decimal

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, delete, func, select
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.core.database import create_session_factory
from app.models.ai_catalog import (
    SEED_METER_TYPES,
    AiModel,
    AiModelAlias,
    AiProvider,
    UsageMeterComponent,
    UsageMeterType,
)
from app.models.auth import User, UserRole, UserStatus
from app.models.base import Base
from app.models.provider_prices import PriceVersionStatus, ProviderPriceVersion
from app.repositories import provider_prices as price_repository
from app.repositories.ai_catalog import resolve_model
from app.repositories.provider_prices import ResolvedProviderPrice, resolve_provider_price
from app.schemas.ai_catalog import ModelView
from app.schemas.provider_prices import ComponentInput, PriceVersionView
from app.services import ai_catalog, provider_prices
from app.services.auth import RequestContext, utc_now
from app.services.provider_prices import (
    EffectiveFromConflict,
    EffectiveFromInPast,
    PricePeriodsBroken,
    PriceVersionFinal,
    PriceVersionNotRetirable,
)

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")
TEST_EMAIL_DOMAIN = "@provider-prices-resolve-test.example.com"
CONTEXT = RequestContext(ip_address="203.0.113.9", user_agent="provider-prices-test")

T0 = dt.datetime(2026, 9, 29, 8, 30, 0)
LONG_AGO = dt.datetime(2000, 1, 1)
MICRO = dt.timedelta(microseconds=1)
SECOND = dt.timedelta(seconds=1)
DAY = dt.timedelta(days=1)

# 明显的虚构价格：每 1 000 000 个 token，以及每 1 000 个 embedding token。
FICTIONAL_TOKEN_RATES = {
    "LLM_INPUT_TOKEN": "1.11111111",
    "LLM_OUTPUT_TOKEN": "2.22222222",
    "LLM_CACHE_WRITE_TOKEN": "3.33333333",
    "LLM_CACHE_READ_TOKEN": "4.44444444",
}
FICTIONAL_EMBEDDING_RATE = "5.55555555"


# --- 帮手 -----------------------------------------------------------------------


class Clock:
    def __init__(self, moment: dt.datetime) -> None:
        self.now = moment

    def __call__(self) -> dt.datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += dt.timedelta(**delta)


def component(code: str, rate: str, unit_quantity: str = "1000000") -> ComponentInput:
    return ComponentInput.model_validate(
        {"component_code": code, "unit_quantity": unit_quantity, "rate_amount": rate}
    )


def token_components() -> list[ComponentInput]:
    return [component(code, rate) for code, rate in FICTIONAL_TOKEN_RATES.items()]


def make_admin(factory: sessionmaker[Session]) -> User:
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


def seed_meter_types(factory: sessionmaker[Session]) -> None:
    """What migration 0012 seeds, for the SQLite tables `create_all` builds empty."""
    with factory() as session:
        for seed in SEED_METER_TYPES:
            kind = UsageMeterType(
                public_id=str(uuid.uuid4()),
                code=seed.code,
                display_name=seed.display_name,
                payload_shape=seed.payload_shape,
                unit=seed.unit,
                quantity_kind=seed.quantity_kind,
                created_at=T0,
                updated_at=T0,
            )
            session.add(kind)
            session.flush()
            for part in seed.components:
                session.add(
                    UsageMeterComponent(
                        meter_type_id=kind.id,
                        payload_shape=seed.payload_shape,
                        component_code=part.component_code,
                        quantity_field=part.quantity_field,
                        created_at=T0,
                    )
                )
        session.commit()


class Prices:
    """One admin, one provider and its models, driven through the services."""

    def __init__(
        self,
        factory: sessionmaker[Session],
        clock: Callable[[], dt.datetime],
        *,
        provider_code: str = "anthropic",
        models: tuple[str, ...] = ("model-m",),
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
        self.models: dict[str, ModelView] = {}
        for code in models:
            self.models[code] = ai_catalog.create_model(
                factory,
                actor=self.admin,
                provider_id=self.provider.id,
                code=code,
                display_name=code,
                context=CONTEXT,
                clock=clock,
            )

    def draft(
        self,
        model: str = "model-m",
        components: list[ComponentInput] | None = None,
    ) -> PriceVersionView:
        return provider_prices.create_draft(
            self.factory,
            actor=self.admin,
            provider_id=self.provider.id,
            model_id=self.models[model].id,
            source_currency="USD",
            source_reference="Fictional price sheet",
            components=token_components() if components is None else components,
            context=CONTEXT,
            clock=self.clock,
        )

    def publish(
        self, version_id: str, effective_from: dt.datetime | None = None
    ) -> PriceVersionView:
        return provider_prices.publish(
            self.factory,
            actor=self.admin,
            price_version_id=version_id,
            effective_from=effective_from,
            context=CONTEXT,
            clock=self.clock,
        )

    def publish_new(
        self, effective_from: dt.datetime | None = None, model: str = "model-m"
    ) -> PriceVersionView:
        return self.publish(self.draft(model).id, effective_from)

    def retire(self, version_id: str) -> PriceVersionView:
        return provider_prices.retire(
            self.factory,
            actor=self.admin,
            price_version_id=version_id,
            reason="Fictional retirement",
            context=CONTEXT,
            clock=self.clock,
        )

    def ids(self, model: str = "model-m") -> tuple[int, int]:
        """(provider id, model id), both internal."""
        public_id = self.models[model].id
        statement = select(AiModel.provider_id, AiModel.id).where(AiModel.public_id == public_id)
        with self.factory() as session:
            provider_id, model_id = session.execute(statement).one()
            return int(provider_id), int(model_id)

    def resolve(self, occurred_at: dt.datetime, model: str = "model-m") -> str | None:
        """What one billing run sees: resolve the model, then its price, in one transaction."""
        return billed(self.factory, self.code, model, occurred_at)


def billed(
    factory: sessionmaker[Session], provider_code: str, model_code: str, occurred_at: dt.datetime
) -> str | None:
    """`resolve_model` then `resolve_provider_price` in one transaction, then commit (T-H)."""
    with factory() as session:
        found = _price_of(session, provider_code, model_code, occurred_at)
        session.commit()
        return None if found is None else found.public_id


def _price_of(
    session: Session, provider_code: str, model_code: str, occurred_at: dt.datetime
) -> ResolvedProviderPrice | None:
    model = resolve_model(session, provider_code, model_code, occurred_at)
    assert model is not None
    return resolve_provider_price(session, model.provider_id, model.id, occurred_at)


# --- 夹具 -----------------------------------------------------------------------


@pytest.fixture
def sqlite_factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    seed_meter_types(factory)
    yield factory
    engine.dispose()


@pytest.fixture
def mysql_factory(monkeypatch) -> Iterator[sessionmaker[Session]]:
    if not TEST_DATABASE_URL:
        pytest.skip("BILLING_TEST_DATABASE_URL is not set; the MySQL half needs a real MySQL")
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
    """⚠️ 库是共享的：每个用例前后清场（写法与 test_provider_prices_service.py 相同）。"""
    with engine.begin() as connection:
        connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 0")
        try:
            connection.exec_driver_sql("TRUNCATE TABLE provider_price_components")
            connection.exec_driver_sql("TRUNCATE TABLE provider_price_versions")
        finally:
            connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 1")
    with engine.begin() as connection:
        connection.exec_driver_sql("TRUNCATE TABLE audit_logs")
    with engine.begin() as connection:
        connection.execute(delete(AiModelAlias))
        connection.execute(delete(AiModel))
        connection.execute(delete(AiProvider))
        connection.execute(delete(User).where(User.email.like(f"%{TEST_EMAIL_DOMAIN}")))


# --- 取价 -------------------------------------------------------------------------


def test_the_version_comes_with_its_components(sqlite_factory) -> None:
    """T-G 契约：分量带 `component_code`、`quantity_field` 与两个精确的金额。"""
    prices = Prices(sqlite_factory, Clock(T0))
    embedding = component("EMBEDDING_TOKEN", FICTIONAL_EMBEDDING_RATE, "1000")
    published = prices.publish(prices.draft(components=[*token_components(), embedding]).id)
    provider_id, model_id = prices.ids()

    with sqlite_factory() as session:
        found = resolve_provider_price(session, provider_id, model_id, T0)

    assert found is not None
    assert found.public_id == published.id
    assert (found.provider_id, found.model_id) == (provider_id, model_id)
    assert (found.source_currency, found.effective_from, found.effective_to) == ("USD", None, None)
    million = Decimal("1000000")
    priced = [
        (item.component_code, item.quantity_field, item.unit_quantity, item.rate_amount)
        for item in found.components
    ]
    assert priced == [
        ("EMBEDDING_TOKEN", "quantity", Decimal("1000"), Decimal(FICTIONAL_EMBEDDING_RATE)),
        ("LLM_CACHE_READ_TOKEN", "cache_read_input_tokens", million, Decimal("4.44444444")),
        ("LLM_CACHE_WRITE_TOKEN", "cache_creation_input_tokens", million, Decimal("3.33333333")),
        ("LLM_INPUT_TOKEN", "input_tokens", million, Decimal("1.11111111")),
        ("LLM_OUTPUT_TOKEN", "output_tokens", million, Decimal("2.22222222")),
    ]
    for item in found.components:
        assert isinstance(item.rate_amount, Decimal)
    by_public_id = select(ProviderPriceVersion.id).where(
        ProviderPriceVersion.public_id == published.id
    )
    with sqlite_factory() as session:
        assert found.version_id == session.execute(by_public_id).scalar_one()


def test_drafts_and_discarded_drafts_never_price_anything(sqlite_factory) -> None:
    prices = Prices(sqlite_factory, Clock(T0))
    prices.draft()
    provider_prices.discard_draft(
        sqlite_factory,
        actor=prices.admin,
        price_version_id=prices.draft().id,
        context=CONTEXT,
        clock=prices.clock,
    )

    for moment in (LONG_AGO, T0, T0 + DAY):
        assert prices.resolve(moment) is None


def test_only_the_models_own_versions_are_found(sqlite_factory) -> None:
    """取价只看（供应商, 模型）：别的模型、别的供应商的版本一律取不到。"""
    prices = Prices(sqlite_factory, Clock(T0), models=("model-m", "model-n"))
    others = Prices(sqlite_factory, Clock(T0), provider_code="openai")
    mine = prices.publish_new(model="model-m")
    theirs = others.publish_new()

    assert prices.resolve(T0, "model-m") == mine.id
    assert prices.resolve(T0, "model-n") is None
    assert others.resolve(T0) == theirs.id


def test_resolution_takes_share_locks() -> None:
    """设计 §2：版本与分量都是加锁读（`FOR SHARE`），刷新会话里已有的对象。"""
    shared = price_repository._shared(select(ProviderPriceVersion))
    compiled = str(shared.compile(dialect=mysql.dialect()))
    assert "LOCK IN SHARE MODE" in compiled or "FOR SHARE" in compiled
    assert "with_for_update(read=True)" in inspect.getsource(price_repository._shared)
    # 版本一次、分量一次：两次读都是共享锁读。
    assert inspect.getsource(resolve_provider_price).count("_shared(") == 2
    assert ".with_for_update()" in inspect.getsource(price_repository.lock_provider)
    assert ".with_for_update()" in inspect.getsource(price_repository.period_versions)
    # 发布、退役在锁后的读：`lock=True` 时同样是共享锁读。
    locked = price_repository._locked(select(ProviderPriceVersion), True)
    assert str(locked.compile(dialect=mysql.dialect())) == compiled


# --- 时间稳定性（性质用例，设计 §7） ---------------------------------------------------


def test_a_priced_moment_never_changes_its_version(sqlite_factory) -> None:
    """设计 §7「时间稳定性」性质用例。

    随机的「发布 / 预约 / 退役 / 撤销预约」序列（时钟随机前进，包括不动与不足一秒）；每一步
    之后，对不晚于此刻的一组固定历史时刻逐个取价 —— 计费只处理这样的时刻。一个时刻一旦取到
    某个版本，之后每一步都必须仍取到它（原本无价的时刻可以变成有价：第一个版本）。
    """
    rng = random.Random(20260929)
    clock = Clock(T0 - 3 * SECOND)
    prices = Prices(sqlite_factory, clock)
    provider_id, model_id = prices.ids()
    # 半秒一格：整秒的格子恰好落在边界 `t` 上，半秒的落在区间内。
    probes = [T0 + dt.timedelta(milliseconds=500 * k) for k in range(-6, 400)]
    steps = [0, 1, 250_000, 999_999, 1_000_000, 1_500_000, 7_000_000]
    published: list[str] = []
    seen: dict[dt.datetime, str] = {}
    changes = 0

    for step in range(160):
        clock.advance(microseconds=rng.choice(steps))
        operation = rng.choice(["publish", "publish", "reserve", "retire"])
        try:
            if operation == "publish":
                published.append(prices.publish_new().id)
            elif operation == "reserve":
                ahead = clock.now.replace(microsecond=0) + rng.randint(1, 20) * SECOND
                published.append(prices.publish_new(ahead).id)
            elif published:
                # 最新的那一个：正在生效就是退役，尚未开始就是撤销预约。
                prices.retire(published[-1])
            changes += 1
        except (
            EffectiveFromConflict,
            EffectiveFromInPast,
            # 设计 v3 字面走不通的几格（撤销恢复前一个版本的预约、在预约前或同一秒里不指定时刻
            # 的发布）整体回滚：同样什么都没变（见 docs/TODO.md）。
            PricePeriodsBroken,
            PriceVersionFinal,
            PriceVersionNotRetirable,
        ):
            pass
        with sqlite_factory() as session:
            for probe in probes:
                if probe > clock.now:
                    break
                found = resolve_provider_price(session, provider_id, model_id, probe)
                if probe in seen:
                    assert found is not None, (step, operation, probe)
                    assert found.public_id == seen[probe], (step, operation, probe)
                elif found is not None:
                    seen[probe] = found.public_id

    # 序列里确实发生了足够多的变化，也确实取到了价 —— 否则上面的断言在空转。
    assert changes >= 40
    assert len(seen) >= 40
    assert len(set(seen.values())) >= 5


# --- 与发布串行（真 MySQL） ------------------------------------------------------------


def _pause_inside_the_publish(monkeypatch) -> tuple[threading.Event, threading.Event]:
    """Make the next price write stop just before its commit, holding its locks."""
    written, release = threading.Event(), threading.Event()
    real = provider_prices.record_audit

    def pausing(session: Session, **options: object) -> None:
        real(session, **options)  # type: ignore[arg-type]
        session.flush()
        written.set()
        assert release.wait(timeout=60)

    monkeypatch.setattr(provider_prices, "record_audit", pausing)
    return written, release


def test_billing_waits_for_an_uncommitted_publish(mysql_factory, monkeypatch) -> None:
    """设计 §7「发布与计费并发」①：发布写完未提交时，计费（`resolve_model` +
    `resolve_provider_price`，`occurred_at ≥ t`）阻塞到发布提交，取到新版本。
    """
    prices = Prices(mysql_factory, utc_now)
    old = prices.publish_new()
    draft = prices.draft()
    written, release = _pause_inside_the_publish(monkeypatch)
    # 晚于这次发布可能取到的任何 `t`。
    occurred_at = utc_now() + DAY

    with ThreadPoolExecutor(max_workers=2) as pool:
        publishing = pool.submit(prices.publish, draft.id)
        assert written.wait(timeout=30)
        billing = pool.submit(billed, mysql_factory, prices.code, "model-m", occurred_at)
        with pytest.raises(TimeoutError):
            billing.result(timeout=2)
        release.set()
        new = publishing.result(timeout=30)
        assert billing.result(timeout=30) == new.id

    assert new.id != old.id
    assert new.effective_from is not None
    assert new.effective_from <= occurred_at
    # 提交之后再取同一时刻，与那一次一致。
    assert prices.resolve(occurred_at) == new.id


def test_a_publish_waits_for_an_uncommitted_billing_run(mysql_factory) -> None:
    """设计 §7「发布与计费并发」②：计费事务取价后未提交；发布阻塞到它提交，`t` 晚于该事件，
    提交后再取同一时刻与快照一致。
    """
    prices = Prices(mysql_factory, utc_now)
    old = prices.publish_new()
    draft = prices.draft()
    billing = mysql_factory()
    try:
        occurred_at = utc_now()
        found = _price_of(billing, prices.code, "model-m", occurred_at)
        assert found is not None
        snapshot_id = found.public_id
        with ThreadPoolExecutor(max_workers=1) as pool:
            publishing = pool.submit(prices.publish, draft.id)
            with pytest.raises(TimeoutError):
                publishing.result(timeout=2)
            # 发布只能在这次提交之后拿到锁，它的 `t` 因此晚于此刻。
            before_commit = utc_now()
            billing.commit()
            new = publishing.result(timeout=30)
    finally:
        billing.close()

    t = new.effective_from
    assert snapshot_id == old.id
    assert t is not None
    assert t > occurred_at
    assert t > before_commit
    assert prices.resolve(occurred_at) == snapshot_id
    assert prices.resolve(t) == new.id


def test_pricing_reads_past_the_transaction_snapshot(mysql_factory) -> None:
    """设计 §7「快照读陷阱」：计费事务先做一次普通读（建立快照），另一连接发布并提交，
    之后取价仍得新版本 —— 加锁读读的是最新提交，不是快照。
    """
    prices = Prices(mysql_factory, utc_now)
    prices.publish_new()
    draft = prices.draft()
    occurred_at = utc_now() + DAY
    is_published = ProviderPriceVersion.status == PriceVersionStatus.PUBLISHED
    published = select(func.count()).select_from(ProviderPriceVersion).where(is_published)
    billing = mysql_factory()
    try:
        # 普通读：REPEATABLE READ 在这里建立一致性读快照。
        billing.execute(select(func.count()).select_from(User)).scalar_one()
        assert billing.execute(published).scalar_one() == 1

        new = prices.publish(draft.id)

        # 陷阱确实存在：同一事务里的普通读仍是快照里的一个已发布版本。
        assert billing.execute(published).scalar_one() == 1
        found = _price_of(billing, prices.code, "model-m", occurred_at)
        assert found is not None
        assert found.public_id == new.id
        billing.commit()
    finally:
        billing.close()
