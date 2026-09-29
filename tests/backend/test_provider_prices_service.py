"""Provider price versions over time, through the services (design gate #177 v3 §7).

五类用例：

- **区间规则**（SQLite，冻结时钟）：第一个版本对过去生效、截断于 `t`、预约生效、不许回溯、
  撤销预约、退役当前版本、退役历史版本、退役后再发布；复查函数本身、复查失败回滚、「拿到
  供应商行锁之后才取时间」；设计 v3 字面走不通的几格（用例名与文档串标明，见 docs/TODO.md）；
- **发布侧的快照读陷阱**（真 MySQL，两个连接）：等锁期间提交的模型停用、分量删除，发布都看得见；
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
from concurrent.futures import ThreadPoolExecutor

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, delete, func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.core.database import create_session_factory
from app.models.ai_catalog import (
    SEED_METER_TYPES,
    AiModel,
    AiModelAlias,
    AiProvider,
    CatalogStatus,
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
    CatalogItemRetired,
    EffectiveFromConflict,
    EffectiveFromInPast,
    PricePeriodsBroken,
    PriceVersionFinal,
    PriceVersionIncomplete,
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
    # 末尾版本是尚未开始的预约：请求的 F 不晚于它的起点一律冲突（设计表格「F > L 的起点」）。
    for requested in (reserved_at, reserved_at - SECOND, T0 + SECOND):
        with pytest.raises(EffectiveFromConflict):
            prices.publish(later.id, requested)
    assert snapshot(sqlite_factory) == before

    assert prices.publish(later.id, reserved_at + SECOND).effective_from == reserved_at + SECOND
    # 恰好等于 `t` 不算回溯。
    other = Prices(sqlite_factory, clock, provider_code="openai")
    assert other.publish_new(T0 + SECOND).effective_from == T0 + SECOND


def test_withdrawing_a_reservation_that_truncated_a_version_rolls_back(sqlite_factory) -> None:
    """⚠️ 设计 v3 的缺口（未擅改，见 docs/TODO.md）：设计 §7「撤销预约」要求前一个版本恢复为
    未截断，但恢复后它按 `effective_from` 排在被撤销的空区间之前，设计字面的「事务内复查」
    （「前一个 `effective_to` 为空时它必须是最后一个」）不通过 —— 整个撤销回滚（500），
    被撤销的版本、前一个版本与审计都不变。
    """
    clock = Clock(T0)
    prices = Prices(sqlite_factory, clock)
    first = prices.publish_new()
    reserved_at = T0 + DAY
    second = prices.publish_new(reserved_at)
    clock.advance(seconds=10)
    before = snapshot(sqlite_factory)

    with pytest.raises(PricePeriodsBroken):
        prices.retire(second.id)

    assert snapshot(sqlite_factory) == before
    assert prices.version(first.id).effective_to == reserved_at
    assert prices.resolve(reserved_at) == second.id


def test_withdrawing_the_first_reservation_leaves_nothing(sqlite_factory) -> None:
    """撤销一个前面没有版本的预约：空区间，任何时刻都取不到；复查按设计字面通过。"""
    prices = Prices(sqlite_factory, Clock(T0))
    reserved = prices.publish_new(T0 + DAY)

    withdrawn = prices.retire(reserved.id)

    assert (withdrawn.effective_from, withdrawn.effective_to) == (T0 + DAY, T0 + DAY)
    for moment in (LONG_AGO, T0 + DAY, T0 + 2 * DAY):
        assert prices.resolve(moment) is None
    [(before, after, reason)] = audit_states(sqlite_factory, AuditAction.PROVIDER_PRICE_RETIRE)
    assert before == {
        "status": "PUBLISHED",
        "effective_from": (T0 + DAY).isoformat(),
        "effective_to": None,
    }
    assert after == {
        "status": "RETIRED",
        "effective_from": (T0 + DAY).isoformat(),
        "effective_to": (T0 + DAY).isoformat(),
    }
    assert reason == "Fictional retirement"
    # 设计的「末尾版本」是已发布版本中起点最晚的一个，被撤销的预约也算：它的尽头 E 是它的
    # 起点，下一个版本从 max(E, t) 起，不再是「一直以来」。
    assert prices.publish_new().effective_from == T0 + DAY


def test_publishing_without_a_time_before_a_reservation_fails(sqlite_factory) -> None:
    """⚠️ 设计 v3 的缺口（未擅改，见 docs/TODO.md）：末尾版本 L 是尚未开始的预约时，设计表格
    「有 L，请求未给」要求从 `t` 起、把 L 截断于 `t` —— L 的区间会倒置，设计没有给别的结果。
    按 §5「复查不通过」：500，什么都不写。
    """
    prices = Prices(sqlite_factory, Clock(T0))
    prices.publish_new()
    prices.publish_new(T0 + DAY)
    draft = prices.draft()
    before = snapshot(sqlite_factory)

    with pytest.raises(PricePeriodsBroken):
        prices.publish(draft.id)

    assert snapshot(sqlite_factory) == before


def test_a_second_publish_in_the_same_second_fails(sqlite_factory) -> None:
    """⚠️ 同一缺口：同一秒里两次不指定时刻的发布，`t` 相同，设计要求把刚发布的版本截断于它
    自己的起点（空的已发布区间，CHECK 不允许）。500，什么都不写。
    """
    clock = Clock(T0)
    prices = Prices(sqlite_factory, clock)
    prices.publish_new()
    clock.advance(microseconds=100)
    second = prices.publish_new()
    assert second.effective_from == T0 + SECOND
    clock.advance(microseconds=100)
    draft = prices.draft()
    before = snapshot(sqlite_factory)

    with pytest.raises(PricePeriodsBroken):
        prices.publish(draft.id)

    assert snapshot(sqlite_factory) == before


def test_retiring_a_version_that_starts_at_t_leaves_an_empty_period(sqlite_factory) -> None:
    """设计 §2「退役」字面：起点 ≤ `t` 即「已开始生效」，终点写 `t`。起点恰好是 `t`（同一秒里
    发布又退役）时得到空区间 `[t, t)`；前一个版本不恢复（不是「撤销预约」那一行），`t` 起无价。
    """
    clock = Clock(T0)
    prices = Prices(sqlite_factory, clock)
    first = prices.publish_new()
    clock.now = T0 + dt.timedelta(seconds=10, microseconds=300_000)
    second = prices.publish_new()
    t = T0 + 11 * SECOND

    retired = prices.retire(second.id)

    assert (retired.status, retired.effective_from, retired.effective_to) == ("RETIRED", t, t)
    assert prices.version(first.id).effective_to == t
    assert prices.resolve(t - MICRO) == first.id
    assert prices.resolve(t) is None


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
        ([(None, T1, "PUBLISHED"), (T1, T1, "RETIRED")], False),
        ([(None, T0, "PUBLISHED"), (T0, T0, "RETIRED"), (T0, None, "PUBLISHED")], False),
        ([(T1, T1, "RETIRED"), (T1, None, "PUBLISHED")], False),
        ([(T0, None, "PUBLISHED")], False),
        ([(None, None, "PUBLISHED"), (T1, T1, "RETIRED")], True),
        ([(None, T1, "PUBLISHED"), (T0, None, "PUBLISHED")], True),
        ([(None, None, "PUBLISHED"), (T0, None, "PUBLISHED")], True),
        ([(None, T0, "PUBLISHED"), (None, None, "PUBLISHED")], True),
        ([(T0, T2, "PUBLISHED"), (T1, None, "PUBLISHED")], True),
    ],
    ids=[
        "empty",
        "one-open",
        "chained",
        "retired-gap",
        "retired-at-its-start",
        "empty-in-between",
        "after-a-withdrawn-first",
        "first-with-a-start",
        # 设计字面：空区间也参与排序，恢复为未截断的前一个版本排在它之前（设计 v3 的缺口）。
        "restored-before-a-withdrawn",
        "overlap",
        "two-open",
        "two-from-the-beginning",
        "partial-overlap",
    ],
)
def test_verify_periods(rows: list, broken: bool) -> None:
    """设计 §2「事务内复查」逐字的三条断言；区间自身的合法性归数据库的 CHECK。"""
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
    """撤销预约（前面没有版本、设计字面能走通的那一种）：复查不通过时被撤销的一行回滚。"""
    prices = Prices(sqlite_factory, Clock(T0))
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

    `model-m`：一个未截断的已发布版本与一份草稿；`model-n`：只有一个尚未开始的预约（撤销它
    不恢复任何版本 —— 恢复前一个版本的撤销过不了设计 v3 字面的复查，见上面的缺口用例）；
    `model-o`：只有一份草稿（发布它就是该模型的第一个版本）。
    """

    def __init__(self, factory: sessionmaker[Session]) -> None:
        self.clock = Clock(T0)
        self.prices = Prices(factory, self.clock, models=("model-m", "model-n", "model-o"))
        self.current = self.prices.publish_new(model="model-m")
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


# --- 发布侧的快照读陷阱（真 MySQL） -------------------------------------------------------


def test_a_publish_sees_a_model_retired_while_it_waited(mysql_factory) -> None:
    """设计 §7「快照读陷阱」的发布一侧：发布等供应商行锁期间，另一事务停用模型并提交；
    发布拿到锁后读到的是停用后的模型 —— 409 `CATALOG_ITEM_RETIRED`，什么都不写。

    发布若在锁之前做过普通读，REPEATABLE READ 的快照就停在那一刻，锁后的普通读仍是「未停用」，
    会给已停用的模型发布价格（025 契约）。
    """
    prices = Prices(mysql_factory, utc_now)
    draft = prices.draft()
    provider_id, model_id = prices.ids()
    before = snapshot(mysql_factory)
    retiring = mysql_factory()
    try:
        # 025 停用模型的写法：先锁供应商行，再改模型。
        locked = select(AiProvider.id).where(AiProvider.id == provider_id).with_for_update()
        retiring.execute(locked).scalar_one()
        retired = update(AiModel).where(AiModel.id == model_id).values(status=CatalogStatus.RETIRED)
        retiring.execute(retired)
        with ThreadPoolExecutor(max_workers=1) as pool:
            publishing = pool.submit(prices.publish, draft.id)
            with pytest.raises(TimeoutError):
                publishing.result(timeout=2)
            retiring.commit()
            with pytest.raises(CatalogItemRetired):
                publishing.result(timeout=30)
    finally:
        retiring.close()

    assert snapshot(mysql_factory) == before
    assert prices.version(draft.id).status == "DRAFT"


def test_a_publish_checks_the_components_committed_while_it_waited(mysql_factory) -> None:
    """设计 §7「快照读陷阱」的发布一侧：改草稿的事务锁着版本行、删掉一个分量；发布等这把锁，
    它提交后发布读到的是删过的分量 —— 409 `PRICE_VERSION_INCOMPLETE`，版本仍是草稿。

    触发器里的完整性检查是同一事务里的读，挡不住快照读；只能靠服务层锁后的加锁读。
    """
    prices = Prices(mysql_factory, utc_now)
    draft = prices.draft()
    version_id = _internal_version_id(mysql_factory, draft.id)
    cache_read = (
        select(UsageMeterComponent.id)
        .where(UsageMeterComponent.component_code == "LLM_CACHE_READ_TOKEN")
        .scalar_subquery()
    )
    editing = mysql_factory()
    try:
        # 改草稿的写法：先 `FOR UPDATE` 版本行，再替换分量。
        locked = select(ProviderPriceVersion.id).where(ProviderPriceVersion.id == version_id)
        editing.execute(locked.with_for_update()).scalar_one()
        editing.execute(
            delete(ProviderPriceComponent).where(
                ProviderPriceComponent.provider_price_version_id == version_id,
                ProviderPriceComponent.usage_meter_component_id == cache_read,
            )
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            publishing = pool.submit(prices.publish, draft.id)
            with pytest.raises(TimeoutError):
                publishing.result(timeout=2)
            editing.commit()
            with pytest.raises(PriceVersionIncomplete) as raised:
                publishing.result(timeout=30)
    finally:
        editing.close()

    assert raised.value.missing == ("LLM_CACHE_READ_TOKEN",)
    stored = prices.version(draft.id)
    assert stored.status == "DRAFT"
    assert len(stored.components) == 3
    assert audit_states(mysql_factory, AuditAction.PROVIDER_PRICE_PUBLISH) == []


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
    """服务层正常的发布 / 截断 / 退役 / 退役后再发布 / 预约 / 撤销（前面没有版本的）预约，在真
    触发器下都通过。恢复前一个版本的撤销过不了设计 v3 字面的复查，不在这里（见缺口用例）。
    """
    clock = Clock(utc_now())
    prices = Prices(mysql_factory, clock, models=("model-m", "model-n"))
    first = prices.publish_new()
    clock.advance(seconds=3)
    second = prices.publish_new()
    clock.advance(seconds=3)
    prices.retire(second.id)
    clock.advance(seconds=3)
    third = prices.publish_new()
    fourth = prices.publish_new(clock.now.replace(microsecond=0) + DAY)
    lone = prices.publish_new(clock.now.replace(microsecond=0) + DAY, model="model-n")
    clock.advance(seconds=3)
    prices.retire(lone.id)

    for model in ("model-m", "model-n"):
        provider_id, model_id = prices.ids(model)
        with mysql_factory() as session:
            verify_periods(price_repository.period_versions(session, provider_id, model_id))
            session.rollback()
    assert prices.version(first.id).effective_to == prices.version(second.id).effective_from
    assert prices.version(third.id).effective_to == prices.version(fourth.id).effective_from
    assert prices.version(fourth.id).effective_to is None
    withdrawn = prices.version(lone.id)
    assert withdrawn.effective_from == withdrawn.effective_to


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
