"""Provider price versions over time, through the services (design gate #177 v3 §7).

四类用例：

- **区间规则**（SQLite，冻结时钟）：第一个版本对过去生效、截断于 `t`、预约生效、不许回溯、
  撤销预约、退役当前版本、退役历史版本、退役后再发布；复查函数本身、复查失败回滚、「拿到
  供应商行锁之后才取时间」；
- **事务中途失败**（审计写入、提交抛错）：`factory` 夹具的两个参数，在 SQLite 与真 MySQL 上
  各跑一次；
- **触发器**（真 MySQL）：绕过服务直接改已发布的版本与分量、删除、把 `RETIRED` 改回
  `PUBLISHED`；只许截断与退役；
- **完整性、区间不重叠与约束的数据库兜底**（真 MySQL）：直接改状态为已发布、直接插入已发布的
  版本、直接写相交的区间、草稿带区间、非正的单价。

取价本身、性质用例与并发在 test_provider_prices_resolve.py；接口契约在
test_provider_prices_api.py。价格一律是明显的虚构值。

MySQL 那部分需要 `BILLING_TEST_DATABASE_URL` 指向一个**可以被清空的**库，没设时 skip ——
**不要把 skipped 读成 passed**，CI 设了它并把 skipped 判成失败。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import uuid
from collections.abc import Callable, Iterator

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, delete, func, select, text
from sqlalchemy.exc import DBAPIError
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
from app.models.auth import AuditAction, AuditLog, User, UserRole, UserStatus
from app.models.base import Base
from app.models.provider_prices import (
    PriceVersionStatus,
    ProviderPriceComponent,
    ProviderPriceVersion,
)
from app.repositories import provider_prices as price_repository
from app.repositories.provider_prices import resolve_provider_price
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
    verify_periods,
)

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")
TEST_EMAIL_DOMAIN = "@provider-prices-test.example.com"
CONTEXT = RequestContext(ip_address="203.0.113.9", user_agent="provider-prices-test")

T0 = dt.datetime(2026, 9, 29, 8, 30, 0)
LONG_AGO = dt.datetime(2000, 1, 1)
MICRO = dt.timedelta(microseconds=1)
SECOND = dt.timedelta(seconds=1)
DAY = dt.timedelta(days=1)

# 明显的虚构价格（公开仓库，不写任何真实的供应商价格）：每 1 000 000 个 token。
FICTIONAL_TOKEN_RATES = {
    "LLM_INPUT_TOKEN": "1.11111111",
    "LLM_OUTPUT_TOKEN": "2.22222222",
    "LLM_CACHE_WRITE_TOKEN": "3.33333333",
    "LLM_CACHE_READ_TOKEN": "4.44444444",
}

# MySQL 的错误号与触发器的消息。
_ER_DUP_ENTRY = 1062
_ER_SIGNAL_EXCEPTION = 1644
_ER_CHECK_CONSTRAINT_VIOLATED = 3819
_IMMUTABLE = "a published provider price version is immutable"
_RETIRE_ONLY = "a published provider price version can only be retired"
_DISCARDED_FINAL = "a discarded provider price version is final"
_DRAFT_ONLY = "provider price components change only on a draft"
_NEVER_DELETED = "provider price versions are never deleted"
_STARTS_AS_DRAFT = "a provider price version starts as a draft"
_NO_COMPONENTS = "a provider price version needs at least one component"
_MISSING_COMPONENT = "a provider price version misses a meter type component"
_OVERLAP = "provider price periods overlap"


# --- 帮手 -----------------------------------------------------------------------


class Clock:
    """A frozen clock. The services call it once they hold the provider lock."""

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
    """All four LLM_TOKEN components: a complete set."""
    return [component(code, rate) for code, rate in FICTIONAL_TOKEN_RATES.items()]


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

    def edit(
        self,
        version_id: str,
        *,
        source_reference: str | None = None,
        components: list[ComponentInput] | None = None,
    ) -> PriceVersionView:
        return provider_prices.update_draft(
            self.factory,
            actor=self.admin,
            price_version_id=version_id,
            source_currency=None,
            source_reference=source_reference,
            components=components,
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

    def discard(self, version_id: str) -> PriceVersionView:
        return provider_prices.discard_draft(
            self.factory,
            actor=self.admin,
            price_version_id=version_id,
            context=CONTEXT,
            clock=self.clock,
        )

    def version(self, version_id: str) -> PriceVersionView:
        return provider_prices.get_price_version(self.factory, version_id)

    def ids(self, model: str = "model-m") -> tuple[int, int]:
        """(provider id, model id), both internal."""
        return internal_ids(self.factory, self.models[model].id)

    def resolve(self, occurred_at: dt.datetime, model: str = "model-m") -> str | None:
        """The public id of the version in effect, resolved in a transaction of its own."""
        provider_id, model_id = self.ids(model)
        return resolved(self.factory, provider_id, model_id, occurred_at)


def internal_ids(factory: sessionmaker[Session], model_public_id: str) -> tuple[int, int]:
    statement = select(AiModel.provider_id, AiModel.id).where(AiModel.public_id == model_public_id)
    with factory() as session:
        provider_id, model_id = session.execute(statement).one()
        return int(provider_id), int(model_id)


def resolved(
    factory: sessionmaker[Session], provider_id: int, model_id: int, occurred_at: dt.datetime
) -> str | None:
    with factory() as session:
        found = resolve_provider_price(session, provider_id, model_id, occurred_at)
        session.commit()
        return None if found is None else found.public_id


def snapshot(factory: sessionmaker[Session]) -> dict[str, object]:
    """Every price row a write may touch, as committed, plus the audit count."""
    statements = {
        "versions": select(
            ProviderPriceVersion.public_id,
            ProviderPriceVersion.status,
            ProviderPriceVersion.source_currency,
            ProviderPriceVersion.source_reference,
            ProviderPriceVersion.effective_from,
            ProviderPriceVersion.effective_to,
            ProviderPriceVersion.approved_by,
            ProviderPriceVersion.approved_at,
            ProviderPriceVersion.updated_at,
        ).order_by(ProviderPriceVersion.id),
        "components": select(
            ProviderPriceComponent.provider_price_version_id,
            ProviderPriceComponent.usage_meter_component_id,
            ProviderPriceComponent.unit_quantity,
            ProviderPriceComponent.rate_amount,
        ).order_by(ProviderPriceComponent.id),
    }
    stored: dict[str, object] = {}
    with factory() as session:
        for name, statement in statements.items():
            stored[name] = [tuple(row) for row in session.execute(statement)]
        count = select(func.count()).select_from(AuditLog)
        stored["audits"] = session.execute(count).scalar_one()
    return stored


def audit_states(factory: sessionmaker[Session], action: AuditAction) -> list[tuple]:
    """(before, after, reason) of every audit row with the action, oldest first."""
    statement = select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)
    with factory() as session:
        return [
            (
                json.loads(row.before_state) if row.before_state else None,
                json.loads(row.after_state) if row.after_state else None,
                row.reason,
            )
            for row in session.execute(statement).scalars()
        ]


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
    factory = create_session_factory(engine)
    seed_meter_types(factory)
    yield factory
    engine.dispose()


def _mysql_factory(monkeypatch) -> Iterator[sessionmaker[Session]]:
    if not TEST_DATABASE_URL:
        pytest.skip("BILLING_TEST_DATABASE_URL is not set; the MySQL half needs a real MySQL")
    # 建表走 alembic：触发器、生成列、唯一索引、复合外键与 CHECK 以迁移 0013 建出来的为准；
    # 计量类型与分量是迁移 0012 的种子。
    from app.core.config import get_settings

    monkeypatch.setenv("BILLING_DATABASE_URL", TEST_DATABASE_URL)
    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")
    get_settings.cache_clear()

    engine = create_engine(TEST_DATABASE_URL, pool_size=6, max_overflow=2)
    clean(engine)
    try:
        yield create_session_factory(engine)
    finally:
        clean(engine)
        engine.dispose()


def clean(engine: Engine) -> None:
    """⚠️ 库是共享的：每个用例前后清场。计量类型的种子不碰。

    价格版本的触发器拒绝 DELETE，只能 TRUNCATE（DDL，不经触发器）；被外键引用的表要先关掉
    外键检查才能 TRUNCATE。审计表同理（0010 的触发器）。目录行没有删除接口，这里是测试清场。
    """
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


# --- 区间规则（设计 §7） -------------------------------------------------------------


def test_the_first_version_without_a_time_applies_to_all_time(sqlite_factory) -> None:
    """设计 §7「第一个版本」：`effective_from` 为空；很久以前的时刻取到它。"""
    prices = Prices(sqlite_factory, Clock(T0))
    assert prices.resolve(LONG_AGO) is None

    first = prices.publish_new()

    assert (first.status, first.effective_from, first.effective_to) == ("PUBLISHED", None, None)
    assert (first.approved_at, first.approved_by_email) == (T0, prices.admin.email)
    for moment in (LONG_AGO, T0, T0 + DAY):
        assert prices.resolve(moment) == first.id


def test_the_first_version_may_also_start_later(sqlite_factory) -> None:
    prices = Prices(sqlite_factory, Clock(T0))

    first = prices.publish_new(T0 + DAY)

    assert (first.effective_from, first.effective_to) == (T0 + DAY, None)
    assert prices.resolve(T0 + DAY - MICRO) is None
    assert prices.resolve(T0 + DAY) == first.id


@pytest.mark.parametrize("offset", [0, 1, 400_000, 999_999], ids=["whole", "1us", "0.4s", "max"])
def test_publishing_truncates_the_open_version_at_the_next_second(
    sqlite_factory, offset: int
) -> None:
    """`t` 是整秒、严格晚于 `now`；`t - 1µs` 取旧版本、`t` 取新版本；过去不变。"""
    clock = Clock(T0)
    prices = Prices(sqlite_factory, clock)
    first = prices.publish_new()
    clock.now = T0 + dt.timedelta(seconds=10, microseconds=offset)

    second = prices.publish_new()

    t = T0 + 11 * SECOND
    assert second.effective_from == t
    assert prices.version(first.id).effective_to == t
    assert prices.resolve(LONG_AGO) == first.id
    assert prices.resolve(clock.now) == first.id
    assert prices.resolve(t - MICRO) == first.id
    assert prices.resolve(t) == second.id
    [(before, after, _)] = audit_states(sqlite_factory, AuditAction.PROVIDER_PRICE_PUBLISH)[1:]
    assert before == {
        "status": "DRAFT",
        "truncated_version": {"id": first.id, "effective_to": None},
    }
    assert after["effective_from"] == t.isoformat()
    assert after["truncated_version"] == {"id": first.id, "effective_to": t.isoformat()}


def test_a_reservation_takes_over_at_its_time(sqlite_factory) -> None:
    """设计 §7「预约生效」：前一个版本截断于 F；F 之前取旧、之后取新。"""
    prices = Prices(sqlite_factory, Clock(T0))
    first = prices.publish_new()
    reserved_at = T0 + DAY

    second = prices.publish_new(reserved_at)

    assert (second.effective_from, second.effective_to) == (reserved_at, None)
    assert prices.version(first.id).effective_to == reserved_at
    assert prices.resolve(T0) == first.id
    assert prices.resolve(reserved_at - MICRO) == first.id
    assert prices.resolve(reserved_at) == second.id


def test_backdating_is_refused(sqlite_factory) -> None:
    """设计 §7「不许回溯」：F < `t` 是 422；F 不晚于末尾版本的起点是 409。都不写。"""
    clock = Clock(T0 + dt.timedelta(microseconds=500_000))
    prices = Prices(sqlite_factory, clock)
    draft = prices.draft()
    before = snapshot(sqlite_factory)

    for past in (T0, T0 - DAY, LONG_AGO):
        with pytest.raises(EffectiveFromInPast):
            prices.publish(draft.id, past)
    assert snapshot(sqlite_factory) == before

    reserved_at = T0 + DAY
    prices.publish(draft.id, reserved_at)
    later = prices.draft()
    before = snapshot(sqlite_factory)
    # 末尾版本是尚未开始的预约：不晚于它的起点（含不指定时刻、从 `t` 起）一律冲突。
    for requested in (reserved_at, reserved_at - SECOND, T0 + SECOND, None):
        with pytest.raises(EffectiveFromConflict):
            prices.publish(later.id, requested)
    assert snapshot(sqlite_factory) == before

    assert prices.publish(later.id, reserved_at + SECOND).effective_from == reserved_at + SECOND
    # 恰好等于 `t` 不算回溯。
    other = Prices(sqlite_factory, clock, provider_code="openai")
    assert other.publish_new(T0 + SECOND).effective_from == T0 + SECOND


def test_withdrawing_a_reservation_restores_the_previous_version(sqlite_factory) -> None:
    """设计 §7「撤销预约」：空区间；前一个版本恢复为未截断；任何时刻都取不到被撤销的。"""
    clock = Clock(T0)
    prices = Prices(sqlite_factory, clock)
    first = prices.publish_new()
    reserved_at = T0 + DAY
    second = prices.publish_new(reserved_at)
    clock.advance(seconds=10)

    withdrawn = prices.retire(second.id)

    assert withdrawn.status == "RETIRED"
    assert (withdrawn.effective_from, withdrawn.effective_to) == (reserved_at, reserved_at)
    assert prices.version(first.id).effective_to is None
    for moment in (LONG_AGO, T0, reserved_at - MICRO, reserved_at, reserved_at + DAY):
        assert prices.resolve(moment) == first.id
    [(before, after, reason)] = audit_states(sqlite_factory, AuditAction.PROVIDER_PRICE_RETIRE)
    assert before == {
        "status": "PUBLISHED",
        "effective_from": reserved_at.isoformat(),
        "effective_to": None,
        "restored_version": {"id": first.id, "effective_to": reserved_at.isoformat()},
    }
    assert after == {
        "status": "RETIRED",
        "effective_from": reserved_at.isoformat(),
        "effective_to": reserved_at.isoformat(),
        "restored_version": {"id": first.id, "effective_to": None},
    }
    assert reason == "Fictional retirement"
    # 撤销之后照常发布：前一个版本仍是末尾，从 `t` 起被截断。
    clock.advance(seconds=10)
    third = prices.publish_new()
    assert third.effective_from == T0 + 21 * SECOND
    assert prices.version(first.id).effective_to == T0 + 21 * SECOND


def test_withdrawing_the_first_reservation_leaves_nothing(sqlite_factory) -> None:
    prices = Prices(sqlite_factory, Clock(T0))
    reserved = prices.publish_new(T0 + DAY)

    withdrawn = prices.retire(reserved.id)

    assert (withdrawn.effective_from, withdrawn.effective_to) == (T0 + DAY, T0 + DAY)
    for moment in (LONG_AGO, T0 + DAY, T0 + 2 * DAY):
        assert prices.resolve(moment) is None
    # 被撤销的预约不算「已发布过」：下一个版本仍可以是「一直以来」。
    assert prices.publish_new().effective_from is None


def test_retiring_the_current_version_ends_its_price_at_t(sqlite_factory) -> None:
    """设计 §7「退役当前版本」：`t` 起取不到价（None）；`t` 之前照旧。"""
    clock = Clock(T0)
    prices = Prices(sqlite_factory, clock)
    first = prices.publish_new()
    clock.now = T0 + dt.timedelta(seconds=20, microseconds=300_000)

    retired = prices.retire(first.id)

    t = T0 + 21 * SECOND
    assert (retired.status, retired.effective_from, retired.effective_to) == ("RETIRED", None, t)
    assert prices.resolve(LONG_AGO) == first.id
    assert prices.resolve(t - MICRO) == first.id
    assert prices.resolve(t) is None
    assert prices.resolve(t + DAY) is None


def test_retiring_a_superseded_version_is_refused(sqlite_factory) -> None:
    """设计 §7「退役历史版本」：409；草稿不能退役；已退役、已丢弃的是终态。都不写。"""
    clock = Clock(T0)
    prices = Prices(sqlite_factory, clock)
    first = prices.publish_new()
    clock.advance(seconds=5)
    second = prices.publish_new()
    draft = prices.draft()
    discarded = prices.discard(prices.draft().id)
    clock.advance(seconds=5)
    retired = prices.retire(second.id)
    before = snapshot(sqlite_factory)

    for version_id in (first.id, draft.id):
        with pytest.raises(PriceVersionNotRetirable):
            prices.retire(version_id)
    for version_id in (retired.id, discarded.id):
        with pytest.raises(PriceVersionFinal):
            prices.retire(version_id)
        with pytest.raises(PriceVersionFinal):
            prices.publish(version_id)

    assert snapshot(sqlite_factory) == before


def test_publishing_after_a_retirement_leaves_the_gap_unpriced(sqlite_factory) -> None:
    """设计 §7「退役后再发布」：新版本从 max(E, t) 起；空档保持无价，不回填。"""
    clock = Clock(T0)
    prices = Prices(sqlite_factory, clock)
    first = prices.publish_new()
    clock.advance(seconds=10)
    ended = prices.retire(first.id).effective_to
    assert ended == T0 + 11 * SECOND
    clock.advance(seconds=90)

    second = prices.publish_new()

    assert second.effective_from == T0 + 101 * SECOND
    assert prices.version(first.id).effective_to == ended
    assert prices.resolve(ended - MICRO) == first.id
    assert prices.resolve(ended) is None
    assert prices.resolve(T0 + 100 * SECOND) is None
    assert prices.resolve(T0 + 101 * SECOND) == second.id


def test_publishing_in_the_second_of_a_retirement_starts_at_its_end(sqlite_factory) -> None:
    """同一秒内退役又发布：`t` 等于尽头 E，新版本从 E 起、首尾相接。"""
    clock = Clock(T0)
    prices = Prices(sqlite_factory, clock)
    first = prices.publish_new()
    clock.now = T0 + dt.timedelta(seconds=10, microseconds=100)
    ended = prices.retire(first.id).effective_to
    clock.advance(microseconds=500_000)

    second = prices.publish_new()

    assert second.effective_from == ended
    assert prices.resolve(ended - MICRO) == first.id
    assert prices.resolve(ended) == second.id


def test_a_retired_version_still_prices_its_own_period(sqlite_factory) -> None:
    """RETIRED 的版本在它自己的区间里照样取得到（已发生的用量照样算钱）。"""
    clock = Clock(T0)
    prices = Prices(sqlite_factory, clock)
    first = prices.publish_new(T0 + SECOND)
    clock.advance(seconds=30)

    prices.retire(first.id)

    assert prices.resolve(T0 + 10 * SECOND) == first.id


def test_every_model_has_its_own_timeline(sqlite_factory) -> None:
    clock = Clock(T0)
    prices = Prices(sqlite_factory, clock, models=("model-m", "model-n"))
    mine = prices.publish_new(model="model-m")
    clock.advance(seconds=5)

    theirs = prices.publish_new(model="model-n")

    assert theirs.effective_from is None
    assert prices.version(mine.id).effective_to is None
    assert prices.resolve(T0, "model-m") == mine.id
    assert prices.resolve(T0, "model-n") == theirs.id


# --- 复查与加锁 ---------------------------------------------------------------------


def _version(
    row_id: int, start: dt.datetime | None, end: dt.datetime | None, status: str
) -> ProviderPriceVersion:
    return ProviderPriceVersion(
        id=row_id,
        status=PriceVersionStatus(status),
        effective_from=start,
        effective_to=end,
    )


T1 = T0 + SECOND
T2 = T0 + 2 * SECOND


@pytest.mark.parametrize(
    ("rows", "broken"),
    [
        ([], False),
        ([(None, None, "PUBLISHED")], False),
        ([(None, T0, "PUBLISHED"), (T0, None, "PUBLISHED")], False),
        ([(None, T0, "RETIRED"), (T1, None, "PUBLISHED")], False),
        ([(None, None, "PUBLISHED"), (T1, T1, "RETIRED")], False),
        ([(None, T0, "PUBLISHED"), (T0, T0, "RETIRED"), (T0, None, "PUBLISHED")], False),
        ([(T0, None, "PUBLISHED")], False),
        ([(None, T1, "PUBLISHED"), (T0, None, "PUBLISHED")], True),
        ([(None, None, "PUBLISHED"), (T0, None, "PUBLISHED")], True),
        ([(None, T0, "PUBLISHED"), (None, None, "PUBLISHED")], True),
        ([(T0, T0, "PUBLISHED")], True),
        ([(T1, T0, "RETIRED")], True),
        ([(None, None, "RETIRED")], True),
        ([(T0, T2, "PUBLISHED"), (T1, None, "PUBLISHED")], True),
    ],
    ids=[
        "empty",
        "one-open",
        "chained",
        "retired-gap",
        "withdrawn-after-restored",
        "withdrawn-in-between",
        "first-with-a-start",
        "overlap",
        "two-open",
        "two-from-the-beginning",
        "empty-but-published",
        "inverted",
        "retired-without-end",
        "partial-overlap",
    ],
)
def test_verify_periods(rows: list, broken: bool) -> None:
    versions = [_version(index + 1, *row) for index, row in enumerate(rows)]
    # 顺序打乱也一样：复查自己排序。
    versions.reverse()

    if broken:
        with pytest.raises(PricePeriodsBroken):
            verify_periods(versions)
    else:
        verify_periods(versions)


def _always_broken(_rows: object) -> None:
    raise PricePeriodsBroken("injected")


def test_a_failed_recheck_rolls_the_publish_back(sqlite_factory, monkeypatch) -> None:
    """设计 §2「事务内复查」：不满足就回滚 —— 前一个版本也不会被截断。"""
    clock = Clock(T0)
    prices = Prices(sqlite_factory, clock)
    prices.publish_new()
    draft = prices.draft()
    before = snapshot(sqlite_factory)
    monkeypatch.setattr(provider_prices, "verify_periods", _always_broken)
    clock.advance(seconds=5)

    with pytest.raises(PricePeriodsBroken):
        prices.publish(draft.id)

    assert snapshot(sqlite_factory) == before


def test_a_failed_recheck_rolls_the_withdrawal_back(sqlite_factory, monkeypatch) -> None:
    """撤销预约：被撤销的一行与恢复的前一个版本一起回滚。"""
    prices = Prices(sqlite_factory, Clock(T0))
    prices.publish_new()
    reserved = prices.publish_new(T0 + DAY)
    before = snapshot(sqlite_factory)
    monkeypatch.setattr(provider_prices, "verify_periods", _always_broken)

    with pytest.raises(PricePeriodsBroken):
        prices.retire(reserved.id)

    assert snapshot(sqlite_factory) == before


def test_publish_and_retire_read_the_clock_only_after_the_provider_lock(
    sqlite_factory, monkeypatch
) -> None:
    """设计 §2：`t` 在拿到供应商行锁之后取。"""
    calls: list[str] = []
    real_lock = price_repository.lock_provider

    def lock_spy(session: Session, provider_id: int):
        calls.append("lock")
        return real_lock(session, provider_id)

    def clock() -> dt.datetime:
        calls.append("clock")
        return T0

    prices = Prices(sqlite_factory, clock)
    draft = prices.draft()
    monkeypatch.setattr(price_repository, "lock_provider", lock_spy)

    calls.clear()
    published = prices.publish(draft.id)
    assert calls == ["lock", "clock"]

    calls.clear()
    prices.retire(published.id)
    assert calls == ["lock", "clock"]


# --- 事务中途失败：SQLite 与 MySQL 各一次（INV-13） ---------------------------------------


def break_the_audit(monkeypatch) -> None:
    """The audit row fails at flush: `created_at` is NULL (NOT NULL → IntegrityError)."""
    real = provider_prices.record_audit

    def broken(session: Session, **options: object) -> None:
        real(session, **{**options, "now": None})  # type: ignore[arg-type]

    monkeypatch.setattr(provider_prices, "record_audit", broken)


def break_the_commit(monkeypatch) -> None:
    def failing_commit(self: Session) -> None:
        raise RuntimeError("injected commit failure")

    monkeypatch.setattr(Session, "commit", failing_commit)


class Scene:
    """A timeline with every kind of row the writes below act on.

    `model-m`：一个未截断的已发布版本与一份草稿；`model-n`：一个已发布版本与截断它的预约；
    `model-o`：只有一份草稿（发布它就是该模型的第一个版本）。
    """

    def __init__(self, factory: sessionmaker[Session]) -> None:
        self.clock = Clock(T0)
        self.prices = Prices(factory, self.clock, models=("model-m", "model-n", "model-o"))
        self.current = self.prices.publish_new(model="model-m")
        self.prices.publish_new(model="model-n")
        self.reserved = self.prices.publish_new(T0 + DAY, model="model-n")
        self.draft = self.prices.draft("model-m")
        self.lone_draft = self.prices.draft("model-o")
        self.clock.advance(seconds=5)


ACTIONS: dict[str, Callable[[Scene], object]] = {
    "create": lambda scene: scene.prices.draft("model-m"),
    "update": lambda scene: scene.prices.edit(
        scene.draft.id,
        source_reference="Another fictional sheet",
        components=[component("LLM_INPUT_TOKEN", "9.99999999")],
    ),
    "discard": lambda scene: scene.prices.discard(scene.draft.id),
    "publish-truncating": lambda scene: scene.prices.publish(scene.draft.id),
    "publish-first": lambda scene: scene.prices.publish(scene.lone_draft.id),
    "retire-current": lambda scene: scene.prices.retire(scene.current.id),
    "withdraw-reservation": lambda scene: scene.prices.retire(scene.reserved.id),
}


@pytest.mark.parametrize("failure", ["audit", "commit"])
@pytest.mark.parametrize("action", sorted(ACTIONS))
def test_a_failed_write_leaves_nothing(factory, monkeypatch, action: str, failure: str) -> None:
    """设计 §7「事务中途失败」：审计写入或提交抛错，版本、分量、前一个版本与审计全部回滚。

    「发布时截断写入后审计抛错」就是 `publish-truncating` × `audit`：两个版本都回到发布前。
    """
    scene = Scene(factory)
    before = snapshot(factory)
    if failure == "audit":
        break_the_audit(monkeypatch)
    else:
        break_the_commit(monkeypatch)

    with pytest.raises(Exception):  # noqa: B017 - 两种注入各抛各的，这里只关心回滚
        ACTIONS[action](scene)

    # 下面的核对只读、不提交，所以不必先撤掉提交的替身。
    assert snapshot(factory) == before


# --- 触发器（真 MySQL） ---------------------------------------------------------------


def _refused(factory: sessionmaker[Session], statement, **params: object) -> tuple[int, str]:
    """Run in a transaction that is rolled back; return the MySQL error number and message."""
    with factory() as session:
        with pytest.raises(DBAPIError) as raised:
            session.execute(statement, params)
        session.rollback()
    return _error(raised)


def _accepted(factory: sessionmaker[Session], statement, **params: object) -> None:
    """Run and roll back: the database let it through."""
    with factory() as session:
        session.execute(statement, params)
        session.rollback()


def _error(raised: pytest.ExceptionInfo[DBAPIError]) -> tuple[int, str]:
    errno, message = raised.value.orig.args[:2]
    return int(errno), str(message)


def _internal_version_id(factory: sessionmaker[Session], public_id: str) -> int:
    statement = select(ProviderPriceVersion.id).where(ProviderPriceVersion.public_id == public_id)
    with factory() as session:
        return int(session.execute(statement).scalar_one())


def _update_version(assignment: str):
    return text(f"UPDATE provider_price_versions SET {assignment} WHERE id = :id")


_UPDATE_COMPONENTS = text(
    "UPDATE provider_price_components SET rate_amount = 9.99999999"
    " WHERE provider_price_version_id = :id"
)
_DELETE_VERSION = text("DELETE FROM provider_price_versions WHERE id = :id")
_DELETE_COMPONENTS = text(
    "DELETE FROM provider_price_components WHERE provider_price_version_id = :id"
)
_INSERT_COMPONENT = text(
    "INSERT INTO provider_price_components (provider_price_version_id, usage_meter_component_id,"
    " unit_quantity, rate_amount, created_at)"
    " SELECT :id, id, 1, 1, :now FROM usage_meter_components WHERE component_code = :code"
)


@pytest.mark.parametrize(
    "assignment",
    [
        "source_currency = 'EUR'",
        "source_reference = 'Edited'",
        "effective_from = '2026-09-29 08:30:00'",
        "approved_at = '2026-09-29 09:00:00'",
        "created_at = '2026-09-29 09:00:00'",
    ],
)
def test_a_published_version_refuses_edits(mysql_factory, assignment: str) -> None:
    """设计 §7「触发器」：直接 UPDATE 已发布版本的币种、来源、起点等，全部 SIGNAL。"""
    prices = Prices(mysql_factory, utc_now)
    published = prices.publish_new()
    version_id = _internal_version_id(mysql_factory, published.id)
    before = snapshot(mysql_factory)

    refused = _refused(mysql_factory, _update_version(assignment), id=version_id)

    assert refused == (_ER_SIGNAL_EXCEPTION, _IMMUTABLE)
    assert snapshot(mysql_factory) == before


def test_published_components_are_frozen_and_versions_never_deleted(mysql_factory) -> None:
    """分量单价不可改、不可删、不能给已发布版本加分量；任何版本（含草稿）都删不掉。"""
    prices = Prices(mysql_factory, utc_now)
    published = prices.publish_new()
    draft = prices.draft()
    published_id = _internal_version_id(mysql_factory, published.id)
    draft_id = _internal_version_id(mysql_factory, draft.id)
    before = snapshot(mysql_factory)

    frozen = [
        (_UPDATE_COMPONENTS, {"id": published_id}),
        (_DELETE_COMPONENTS, {"id": published_id}),
        (_INSERT_COMPONENT, {"id": published_id, "now": T0, "code": "EMBEDDING_TOKEN"}),
    ]
    for statement, params in frozen:
        assert _refused(mysql_factory, statement, **params) == (_ER_SIGNAL_EXCEPTION, _DRAFT_ONLY)
    for version_id in (published_id, draft_id):
        refused = _refused(mysql_factory, _DELETE_VERSION, id=version_id)
        assert refused == (_ER_SIGNAL_EXCEPTION, _NEVER_DELETED)
    assert snapshot(mysql_factory) == before

    # 草稿的分量照常可改、可删、可加（改草稿是「整体替换」）。
    _accepted(mysql_factory, _UPDATE_COMPONENTS, id=draft_id)
    _accepted(mysql_factory, _DELETE_COMPONENTS, id=draft_id)
    _accepted(mysql_factory, _INSERT_COMPONENT, id=draft_id, now=T0, code="EMBEDDING_TOKEN")


def test_only_truncation_and_retirement_pass_on_a_published_version(mysql_factory) -> None:
    """只有截断与 `PUBLISHED → RETIRED` 通过；改回 `PUBLISHED`、改丢弃的草稿都被拒绝。"""
    clock = Clock(utc_now())
    prices = Prices(mysql_factory, clock)
    published = prices.publish_new()
    discarded = prices.discard(prices.draft().id)
    published_id = _internal_version_id(mysql_factory, published.id)
    discarded_id = _internal_version_id(mysql_factory, discarded.id)
    end = "effective_to = '2999-01-01 00:00:00'"

    _accepted(mysql_factory, _update_version(end), id=published_id)
    _accepted(mysql_factory, _update_version(f"status = 'RETIRED', {end}"), id=published_id)

    clock.advance(seconds=5)
    assert prices.retire(published.id).status == "RETIRED"
    back = _update_version("status = 'PUBLISHED'")
    assert _refused(mysql_factory, back, id=published_id) == (_ER_SIGNAL_EXCEPTION, _RETIRE_ONLY)
    edit = _update_version("source_reference = 'Edited'")
    refused = _refused(mysql_factory, edit, id=discarded_id)
    assert refused == (_ER_SIGNAL_EXCEPTION, _DISCARDED_FINAL)


# --- 完整性、区间与约束的数据库兜底（真 MySQL） ------------------------------------------------


_INSERT_VERSION = text(
    "INSERT INTO provider_price_versions (public_id, provider_id, model_id, source_currency,"
    " source_type, source_reference, status, effective_from, effective_to, created_by,"
    " approved_by, created_at, updated_at, approved_at) VALUES (:public_id, :provider_id,"
    " :model_id, 'USD', 'MANUAL', 'Fictional price sheet', :status, NULL, NULL, :user,"
    " :approver, :now, :now, :approved_at)"
)
_PUBLISH_DIRECTLY = text(
    "UPDATE provider_price_versions SET status = :status, effective_from = :start,"
    " effective_to = :end, approved_by = :user, approved_at = :now WHERE id = :id"
)


def _insert_version(session: Session, prices: Prices, status: str, model: str = "model-m"):
    provider_id, model_id = prices.ids(model)
    approved = status != "DRAFT"
    return session.execute(
        _INSERT_VERSION,
        {
            "public_id": str(uuid.uuid4()),
            "provider_id": provider_id,
            "model_id": model_id,
            "status": status,
            "user": prices.admin.id,
            "approver": prices.admin.id if approved else None,
            "now": T0,
            "approved_at": T0 if approved else None,
        },
    )


def _direct_draft(
    session: Session,
    prices: Prices,
    codes: tuple[str, ...] = ("EMBEDDING_TOKEN",),
    model: str = "model-m",
) -> int:
    """A draft written with plain SQL (the insert trigger lets drafts in), with components."""
    version_id = int(_insert_version(session, prices, "DRAFT", model).lastrowid)
    for code in codes:
        session.execute(_INSERT_COMPONENT, {"id": version_id, "now": T0, "code": code})
    return version_id


def _publish_directly(
    session: Session,
    prices: Prices,
    version_id: int,
    period: tuple[dt.datetime | None, dt.datetime | None],
    status: str = "PUBLISHED",
) -> None:
    start, end = period
    parameters = {
        "status": status,
        "start": start,
        "end": end,
        "user": prices.admin.id,
        "now": T0,
        "id": version_id,
    }
    session.execute(_PUBLISH_DIRECTLY, parameters)


def test_the_database_checks_completeness_on_the_publish_transition(mysql_factory) -> None:
    """设计 §7「完整性由数据库兜底」：零个分量、LLM_TOKEN 缺一个，直接改状态都被拒绝；
    直接插入一条已发布的版本被拒绝；完整的版本直接改状态则通过。
    """
    prices = Prices(mysql_factory, utc_now)
    llm_three = ("LLM_INPUT_TOKEN", "LLM_OUTPUT_TOKEN", "LLM_CACHE_WRITE_TOKEN")
    with mysql_factory() as session:
        empty = _direct_draft(session, prices, ())
        short = _direct_draft(session, prices, llm_three)
        complete = _direct_draft(session, prices, (*llm_three, "LLM_CACHE_READ_TOKEN"))
        for version_id, message in ((empty, _NO_COMPONENTS), (short, _MISSING_COMPONENT)):
            with pytest.raises(DBAPIError) as raised:
                with session.begin_nested():
                    _publish_directly(session, prices, version_id, (None, None))
            assert _error(raised) == (_ER_SIGNAL_EXCEPTION, message)

        with pytest.raises(DBAPIError) as raised:
            with session.begin_nested():
                _insert_version(session, prices, "PUBLISHED")
        assert _error(raised) == (_ER_SIGNAL_EXCEPTION, _STARTS_AS_DRAFT)

        _publish_directly(session, prices, complete, (None, None))
        session.rollback()


@pytest.mark.parametrize(
    ("start", "end", "accepted"),
    [
        (T0 + 5 * SECOND, T0 + 15 * SECOND, False),
        (T0 + 2 * SECOND, T0 + 3 * SECOND, False),
        (T0, T0 + SECOND, False),
        (None, T0 + SECOND, False),
        (T0 - DAY, None, False),
        (T0 + 10 * SECOND, T0 + 20 * SECOND, True),
        (None, T0, True),
        (T0 + 10 * SECOND, None, True),
    ],
    ids=[
        "partial",
        "contained",
        "same-start",
        "from-the-beginning",
        "open-over-it",
        "end-to-end-after",
        "end-to-end-before",
        "open-after",
    ],
)
def test_the_database_refuses_overlapping_periods(
    mysql_factory, start: dt.datetime | None, end: dt.datetime | None, accepted: bool
) -> None:
    """设计 §7「区间不重叠」：绕过服务直接写一个与**已截断**的版本相交的区间，被拒绝；
    首尾相接的通过。
    """
    prices = Prices(mysql_factory, utc_now)
    with mysql_factory() as session:
        existing = _direct_draft(session, prices)
        _publish_directly(session, prices, existing, (T0, T0 + 10 * SECOND))
        candidate = _direct_draft(session, prices)
        if accepted:
            _publish_directly(session, prices, candidate, (start, end))
        else:
            with pytest.raises(DBAPIError) as raised:
                with session.begin_nested():
                    _publish_directly(session, prices, candidate, (start, end))
            assert _error(raised) == (_ER_SIGNAL_EXCEPTION, _OVERLAP)
        session.rollback()


def test_empty_periods_and_other_models_do_not_overlap(mysql_factory) -> None:
    """空区间（撤销的预约）不参与判定；不同模型的区间互不相干；同一（供应商, 模型）的第二个
    未截断版本被拒绝（重叠触发器在唯一索引之前就拦下了，两者任一都算拒绝）。
    """
    prices = Prices(mysql_factory, utc_now, models=("model-m", "model-n"))
    with mysql_factory() as session:
        existing = _direct_draft(session, prices)
        _publish_directly(session, prices, existing, (T0, T0 + 10 * SECOND))
        empty = _direct_draft(session, prices)
        at = T0 + 5 * SECOND
        _publish_directly(session, prices, empty, (at, at), status="RETIRED")
        theirs = _direct_draft(session, prices, model="model-n")
        _publish_directly(session, prices, theirs, (T0, None))

        open_one = _direct_draft(session, prices)
        _publish_directly(session, prices, open_one, (T0 + 10 * SECOND, None))
        second_open = _direct_draft(session, prices)
        with pytest.raises(DBAPIError) as raised:
            with session.begin_nested():
                _publish_directly(session, prices, second_open, (T0 + DAY, None))
        assert _error(raised)[0] in (_ER_SIGNAL_EXCEPTION, _ER_DUP_ENTRY)
        session.rollback()


def test_the_service_paths_pass_the_triggers(mysql_factory) -> None:
    """服务层正常的发布 / 预约 / 撤销预约 / 退役 / 退役后再发布，在真触发器下都通过。"""
    clock = Clock(utc_now())
    prices = Prices(mysql_factory, clock)
    first = prices.publish_new()
    clock.advance(seconds=3)
    second = prices.publish_new()
    third = prices.publish_new(clock.now.replace(microsecond=0) + DAY)
    clock.advance(seconds=3)
    prices.retire(third.id)
    clock.advance(seconds=3)
    prices.retire(second.id)
    clock.advance(seconds=3)
    fourth = prices.publish_new()

    provider_id, model_id = prices.ids()
    with mysql_factory() as session:
        verify_periods(price_repository.period_versions(session, provider_id, model_id))
        session.rollback()
    assert prices.version(first.id).effective_to == prices.version(second.id).effective_from
    assert prices.version(fourth.id).effective_to is None


@pytest.mark.parametrize(
    ("status", "start", "end", "approved"),
    [
        ("DRAFT", T0, None, False),
        ("DRAFT", None, T0, False),
        ("DRAFT", None, None, True),
        ("DISCARDED", None, None, True),
        ("PUBLISHED", None, None, False),
    ],
    ids=[
        "draft-with-start",
        "draft-with-end",
        "draft-with-approver",
        "discarded-with-approver",
        "published-without-approver",
    ],
)
def test_the_checks_tie_the_period_and_the_approver_to_the_status(
    mysql_factory, status: str, start, end, approved: bool
) -> None:
    """设计 §7「数据库约束」：草稿（与丢弃的草稿）不能带区间或发布人；发布过的必须有发布人。"""
    prices = Prices(mysql_factory, utc_now)
    with mysql_factory() as session:
        draft = _direct_draft(session, prices)
        session.commit()
    statement = text(
        "UPDATE provider_price_versions SET status = :status, effective_from = :start,"
        " effective_to = :end, approved_by = :approver, approved_at = :approved_at"
        " WHERE id = :id"
    )
    params = {
        "status": status,
        "start": start,
        "end": end,
        "approver": prices.admin.id if approved else None,
        "approved_at": T0 if approved else None,
        "id": draft,
    }

    errno, _ = _refused(mysql_factory, statement, **params)

    assert errno == _ER_CHECK_CONSTRAINT_VIOLATED


@pytest.mark.parametrize("column", ["unit_quantity", "rate_amount"])
@pytest.mark.parametrize("value", ["0", "-1"])
def test_the_checks_refuse_non_positive_amounts(mysql_factory, column: str, value: str) -> None:
    prices = Prices(mysql_factory, utc_now)
    with mysql_factory() as session:
        draft = _direct_draft(session, prices, ())
        session.commit()
    amounts = {"unit_quantity": "1", "rate_amount": "1", column: value}
    statement = text(
        "INSERT INTO provider_price_components (provider_price_version_id,"
        " usage_meter_component_id, unit_quantity, rate_amount, created_at)"
        " SELECT :id, id, :unit_quantity, :rate_amount, :now"
        " FROM usage_meter_components WHERE component_code = 'EMBEDDING_TOKEN'"
    )

    errno, _ = _refused(mysql_factory, statement, id=draft, now=T0, **amounts)

    assert errno == _ER_CHECK_CONSTRAINT_VIOLATED
