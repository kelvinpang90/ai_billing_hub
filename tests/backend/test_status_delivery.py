"""Status webhook delivery: claim, signed send, settle, retry (design gate #241 v6 §2, §4, §7).

覆盖设计 §7 里投递相关的行（告警断言归 AIH-TASK-052）：

- **正常推送**：按 api.md 验签，payload 恰好 11 个字段，`delivered_version` = 新版本；
- **四个递增点**（余额、账户、内部计量、项目状态）各触发一次推送；
- **失败退避**（500、超时、302、200）与**不设上限**（连续失败 10 次）；
- **缺密钥 / 密钥解不开 / 主密钥未配置**：不发 POST、错误码、计数与退避；判定顺序；
  **成功清除错误**；
- **改地址 / 首次送达 / 改地址与在途请求并发 / 停用不推 / 退避期间出现新版本 /
  在途时出现新版本 / 发送期间版本变化 / 租约过期 / dispatch 预算 / 租户越权 / 日志脱敏 /
  不经 Celery 直接调 dispatch**；
- **只在真 MySQL 上**：结算与跃迁并发（两种提交顺序）、领取并发 `SKIP LOCKED`、加锁不成环。

「在途」的场景不开线程：注入的 transport 在「请求发出、还没返回」的那一刻同步执行另一条路径
（跃迁、改地址），领取早已提交，与真实的并发是同一个时序。

MySQL 那部分需要 `BILLING_TEST_DATABASE_URL` 指向一个**可以被清空的**库，没设时 skip ——
**不要把 skipped 读成 passed**，CI 设了它并把 skipped 判成失败。
"""

from __future__ import annotations

import base64
import datetime as dt
import email.message
import http.client
import inspect
import io
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from urllib.parse import urlsplit

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, delete, event, or_, select, text, update
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.core import fx_source
from app.core.config import Settings
from app.core.database import create_session_factory, session_scope
from app.models.auth import AuditLog, DomainOutbox, User, UserRole, UserStatus
from app.models.base import Base
from app.models.integration import ProjectStatusDelivery, ProjectWebhookSecret, WebhookSecretStatus
from app.models.tenancy import (
    AccountStatus,
    BillingStatus,
    EffectiveStatus,
    IntegrationStatus,
    Project,
    ReasonCode,
    Tenant,
    compose_effective_status,
)
from app.models.wallet import ReferenceType, TransactionType, Wallet
from app.repositories import tenancy
from app.repositories.wallet import create_wallet, post_transaction
from app.services import status_delivery, webhook_signing
from app.services.account_status import change_account_status
from app.services.auth import RequestContext
from app.services.customers import add_status_delivery
from app.services.integration_auth import SignatureRejected, verify_signature
from app.services.internal_billing import enable_internal_metering
from app.services.project_integration import change_integration_status, set_status_webhook
from app.services.status_delivery import (
    CONNECTION_ERROR,
    DELIVERED,
    ENCRYPTION_NOT_CONFIGURED,
    HTTP_STATUS,
    NO_ACTIVE_SECRET,
    SECRET_DECRYPT_FAILED,
    SUPERSEDED,
    TIMEOUT,
    Attempt,
    claim_next,
    dispatch,
    process,
    settle,
)
from app.services.wallet_adjustments import post_adjustment
from app.tasks import status_webhook

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")
TEST_EMAIL_DOMAIN = "@status-delivery-test.example.com"
COMPANY = "Status Delivery Test Sdn Bhd"

NOW = dt.datetime(2026, 10, 12, 9, 0, 0)
LATER = NOW + dt.timedelta(minutes=1)
CONTEXT = RequestContext(ip_address="203.0.113.51", user_agent="status-delivery-test")
REASON = "Status delivery test reason"

# 路径里带一段像令牌的串：它不许出现在日志、审计与异常里（设计 §6）。全零占位值。
PATH_TOKEN = "tok_" + "0" * 32
HOOK = f"https://hooks.example.com/acuven/{PATH_TOKEN}?v=1"
OTHER_HOOK = "https://status.example.net/receive"

PAYLOAD_KEYS = {
    "event_id",
    "tenant_id",
    "project_id",
    "account_status",
    "billing_status",
    "billing_mode",
    "project_integration_status",
    "effective_status",
    "reason_code",
    "status_version",
    "effective_at",
}

DELIVERY_COLUMNS = (
    "delivered_version",
    "attempted_version",
    "attempt_count",
    "next_attempt_at",
    "lease_token",
    "lease_expires_at",
    "last_attempt_at",
    "delivered_at",
    "last_http_status",
    "last_error_code",
)


# --- 夹具 -----------------------------------------------------------------------


@pytest.fixture
def master_key_file(tmp_path) -> str:
    lines = [f"{version}:{base64.b64encode(os.urandom(32)).decode()}" for version in (1, 2)]
    path = tmp_path / "master.key"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


@pytest.fixture
def settings(master_key_file) -> Settings:
    return Settings(master_key_file=master_key_file)


@pytest.fixture(params=["sqlite", "mysql"])
def factory(request, monkeypatch) -> Iterator[sessionmaker[Session]]:
    if request.param == "mysql":
        yield from _mysql_factory(monkeypatch)
        return
    yield from _sqlite_factory()


@pytest.fixture
def sqlite_factory() -> Iterator[sessionmaker[Session]]:
    yield from _sqlite_factory()


@pytest.fixture
def mysql_factory(monkeypatch) -> Iterator[sessionmaker[Session]]:
    yield from _mysql_factory(monkeypatch)


def _sqlite_factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


def _mysql_factory(monkeypatch) -> Iterator[sessionmaker[Session]]:
    if not TEST_DATABASE_URL:
        pytest.skip("BILLING_TEST_DATABASE_URL is not set; the MySQL half needs a real MySQL")
    # 建表走 alembic：列、CHECK、复合外键与钱包触发器以迁移建出来的为准。
    from app.core.config import get_settings

    monkeypatch.setenv("BILLING_DATABASE_URL", TEST_DATABASE_URL)
    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")
    get_settings.cache_clear()

    engine = create_engine(TEST_DATABASE_URL, pool_size=10, max_overflow=4)
    _clean(engine)
    try:
        yield create_session_factory(engine)
    finally:
        _clean(engine)
        engine.dispose()


def _clean(engine: Engine) -> None:
    """⚠️ 库是共享的，每个用例前后都清场（同 test_project_integration_service.py）。"""
    ours = select(Tenant.id).where(Tenant.company_name == COMPANY)
    our_tenants = select(Tenant.public_id).where(Tenant.company_name == COMPANY)
    our_projects = select(Project.public_id).where(Project.tenant_id.in_(ours))
    with engine.begin() as connection:
        # 账本拒绝 DELETE，只能 TRUNCATE；0018 起 usage_events 有外键引用账本。
        connection.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        try:
            connection.execute(text("TRUNCATE TABLE wallet_transactions"))
        finally:
            connection.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
        # 审计表同理：0010 的触发器拒绝 DELETE，TRUNCATE 是 DDL、不经触发器。
        connection.execute(text("TRUNCATE TABLE audit_logs"))
    with engine.begin() as connection:
        connection.execute(
            delete(DomainOutbox).where(
                or_(
                    DomainOutbox.aggregate_id.in_(our_tenants),
                    DomainOutbox.aggregate_id.in_(our_projects),
                )
            )
        )
        connection.execute(
            delete(ProjectWebhookSecret).where(ProjectWebhookSecret.tenant_id.in_(ours))
        )
        # 投递行引用项目（RESTRICT），先删它。
        connection.execute(
            delete(ProjectStatusDelivery).where(ProjectStatusDelivery.tenant_id.in_(ours))
        )
        connection.execute(delete(Project).where(Project.tenant_id.in_(ours)))
        connection.execute(delete(Wallet).where(Wallet.tenant_id.in_(ours)))
        connection.execute(delete(Tenant).where(Tenant.company_name == COMPANY))
        connection.execute(delete(User).where(User.email.like(f"%{TEST_EMAIL_DOMAIN}")))


# --- 时钟与 transport ------------------------------------------------------------


class Clock:
    """A settable clock: `dispatch` and the services read the same `now`."""

    def __init__(self, now: dt.datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> dt.datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + dt.timedelta(seconds=seconds)


class FakeResponse:
    """A response with a large body, recording every read (the sender must make none)."""

    def __init__(self, status: int, body: bytes = b"x" * 4096) -> None:
        self.status = status
        self._body = io.BytesIO(body)
        self.reads: list[int] = []
        self.closed = False

    def read(self, amount: int) -> bytes:
        self.reads.append(amount)
        return self._body.read(amount)

    def close(self) -> None:
        self.closed = True


def http_error(request: urllib.request.Request, status: int) -> urllib.error.HTTPError:
    """What urllib raises for a non-2xx answer (and for a 3xx it does not follow)."""
    return urllib.error.HTTPError(
        request.full_url, status, "answer", email.message.Message(), io.BytesIO(b"y" * 4096)
    )


type Answer = int | BaseException


class Receiver:
    """Records every request and answers from a script (status codes or exceptions); 200 after.

    `on_send` runs while the request is "in flight": after it left, before it returns.
    """

    def __init__(
        self,
        *answers: Answer,
        on_send: Callable[[urllib.request.Request], None] | None = None,
    ) -> None:
        self.answers = list(answers)
        self.on_send = on_send
        self.requests: list[urllib.request.Request] = []
        self.timeouts: list[float] = []
        self.responses: list[FakeResponse] = []
        self._lock = threading.Lock()

    def __call__(self, request: urllib.request.Request, timeout: float) -> FakeResponse:
        with self._lock:
            self.requests.append(request)
            self.timeouts.append(timeout)
            answer: Answer = self.answers.pop(0) if self.answers else 200
        if self.on_send is not None:
            self.on_send(request)
        if isinstance(answer, BaseException):
            raise answer
        if 200 <= answer < 300:
            response = FakeResponse(answer)
            self.responses.append(response)
            return response
        raise http_error(request, answer)

    def payloads(self) -> list[dict[str, object]]:
        return [payload_of(request) for request in self.requests]

    def versions(self) -> list[object]:
        return [payload["status_version"] for payload in self.payloads()]


def headers_of(request: urllib.request.Request) -> dict[str, str]:
    return {name.lower(): value for name, value in request.header_items()}


def payload_of(request: urllib.request.Request) -> dict[str, object]:
    assert isinstance(request.data, bytes)
    return json.loads(request.data)


def verify(request: urllib.request.Request, secret: str, sent_at: dt.datetime) -> None:
    """Verify the request exactly as api.md「状态 webhook 签名」tells the integrator to."""
    headers = headers_of(request)
    parts = urlsplit(request.full_url)
    path_and_query = f"{parts.path}?{parts.query}" if parts.query else parts.path
    assert isinstance(request.data, bytes)
    verify_signature(
        secret,
        method=request.get_method(),
        path_and_query=path_and_query,
        timestamp=headers["x-acuven-timestamp"],
        # 出站 webhook 的规范串第四行是 `X-Acuven-Event-ID`。
        request_id=headers["x-acuven-event-id"],
        body=request.data,
        signature=headers["x-acuven-signature"],
        now=sent_at,
    )


# --- 帮手 -----------------------------------------------------------------------


def make_admin(factory) -> User:
    with factory() as session:
        user = User(
            email=f"{uuid.uuid4().hex}{TEST_EMAIL_DOMAIN}",
            password_hash="not-a-real-hash",
            role=UserRole.ADMIN,
            status=UserStatus.ACTIVE,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(user)
        session.commit()
        return user


def make_customer(factory) -> tuple[int, str]:
    """A tenant with an empty wallet: ENABLED, SUSPENDED, PREPAID, version 0."""
    with factory() as session:
        tenant = tenancy.create_tenant(
            session, company_name=COMPANY, email="ops@example.com", now=NOW
        )
        create_wallet(session, tenant_id=tenant.id, now=NOW)
        session.commit()
        return int(tenant.id), tenant.public_id


def make_project(factory, tenant_id: int) -> tuple[int, str]:
    """A project and its one delivery row, as `create_project` makes them."""
    with factory() as session:
        project = tenancy.create_project(session, tenant_id=tenant_id, name="Chatbot", now=NOW)
        add_status_delivery(session, project, now=NOW)
        session.commit()
        return int(project.id), project.public_id


class ProjectHandle:
    """One project of a customer: its address and its signing secret."""

    def __init__(self, scene: Scene, pk: int, public_id: str) -> None:
        self.scene = scene
        self.pk = pk
        self.public_id = public_id
        self.secrets: dict[int, str] = {}

    def set_url(self, url: str | None, *, now: dt.datetime = NOW) -> None:
        set_status_webhook(
            self.scene.factory,
            actor=self.scene.admin,
            customer_id=self.scene.customer_id,
            project_id=self.public_id,
            url=url,
            context=CONTEXT,
            now=now,
        )

    def new_secret(self, *, now: dt.datetime = NOW) -> tuple[int, str]:
        """Issue and activate a new version; the old ACTIVE retires. (key_version, secret)."""
        issued = webhook_signing.issue_secret(
            self.scene.factory,
            self.scene.settings,
            actor=self.scene.admin,
            customer_id=self.scene.customer_id,
            project_id=self.public_id,
            context=CONTEXT,
            now=now,
        )
        webhook_signing.activate_secret(
            self.scene.factory,
            actor=self.scene.admin,
            customer_id=self.scene.customer_id,
            project_id=self.public_id,
            key_version=issued.key_version,
            context=CONTEXT,
            now=now,
        )
        secret = issued.secret.get_secret_value()
        self.secrets[issued.key_version] = secret
        return issued.key_version, secret

    def change(self, target: IntegrationStatus, *, now: dt.datetime = NOW) -> None:
        change_integration_status(
            self.scene.factory,
            actor=self.scene.admin,
            customer_id=self.scene.customer_id,
            project_id=self.public_id,
            integration_status=target,
            reason=REASON,
            context=CONTEXT,
            now=now,
        )

    def delivery(self) -> dict[str, object]:
        with self.scene.factory() as session:
            row = session.get(ProjectStatusDelivery, self.pk)
            assert row is not None
            return {name: getattr(row, name) for name in DELIVERY_COLUMNS}

    def set_delivery(self, **values: object) -> None:
        with self.scene.factory() as session:
            statement = update(ProjectStatusDelivery).where(
                ProjectStatusDelivery.project_id == self.pk
            )
            session.execute(statement.values(**values))
            session.commit()


class Scene:
    """One admin, one customer, and its first project with an address and an ACTIVE secret."""

    def __init__(
        self,
        factory,
        settings: Settings,
        *,
        url: str | None = HOOK,
        secret: bool = True,
        top_up: bool = False,
    ) -> None:
        self.factory = factory
        self.settings = settings
        self.admin = make_admin(factory)
        self.tenant_id, self.customer_id = make_customer(factory)
        if top_up:
            self.top_up()
        self.project = self.add_project(url=url, secret=secret)

    def add_project(self, *, url: str | None = HOOK, secret: bool = True) -> ProjectHandle:
        pk, public_id = make_project(self.factory, self.tenant_id)
        project = ProjectHandle(self, pk, public_id)
        if url is not None:
            project.set_url(url)
        if secret:
            project.new_secret()
        return project

    @property
    def secret(self) -> str:
        return self.project.secrets[max(self.project.secrets)]

    def top_up(self, *, now: dt.datetime = NOW) -> None:
        """0 → 20: SUSPENDED → ACTIVE, one balance-driven transition."""
        post_adjustment(
            self.factory,
            actor=self.admin,
            customer_id=self.customer_id,
            transaction_type=TransactionType.ADJUSTMENT_CREDIT,
            amount=Decimal("20"),
            reason="Status delivery test top-up",
            idempotency_key=str(uuid.uuid4()),
            context=CONTEXT,
            now=now,
        )

    def set_billing_active(self) -> None:
        """Direct db write, no version: a tenant still ACTIVE when the next charge lands.

        ⚠️ 同一租户在 SQLite 上只记一笔账：那里没有推进钱包的触发器，第二笔撞钱包序号。
        """
        with self.factory() as session:
            statement = update(Tenant).where(Tenant.id == self.tenant_id)
            session.execute(statement.values(billing_status=BillingStatus.ACTIVE))
            session.commit()

    def charge(self, amount: str, *, now: dt.datetime = NOW) -> None:
        """An AI usage charge through the ledger (the billing worker's path)."""
        with session_scope(self.factory) as session:
            post_transaction(
                session,
                tenant_id=self.tenant_id,
                transaction_type=TransactionType.AI_USAGE,
                amount=-Decimal(amount),
                reference_type=ReferenceType.USAGE_EVENT,
                reference_id=str(uuid.uuid4()),
                now=now,
            )

    def set_account(self, status: AccountStatus, *, now: dt.datetime = NOW) -> None:
        change_account_status(
            self.factory,
            actor=self.admin,
            customer_id=self.customer_id,
            account_status=status,
            reason=REASON,
            context=CONTEXT,
            now=now,
        )

    def internal(self, *, now: dt.datetime = NOW) -> None:
        enable_internal_metering(
            self.factory,
            actor=self.admin,
            customer_id=self.customer_id,
            reason=REASON,
            context=CONTEXT,
            now=now,
        )

    def tenant(self) -> tuple[object, ...]:
        with self.factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            assert tenant is not None
            return (
                tenant.account_status,
                tenant.billing_status,
                tenant.billing_mode,
                tenant.status_version,
                tenant.status_changed_at,
            )

    def version(self) -> int:
        with self.factory() as session:
            statement = select(Tenant.status_version).where(Tenant.id == self.tenant_id)
            return int(session.execute(statement).scalar_one())

    def dispatch(
        self,
        receiver: Receiver,
        clock: Callable[[], dt.datetime] | None = None,
        *,
        settings: Settings | None = None,
    ) -> status_delivery.DispatchResult:
        return dispatch(
            self.factory,
            settings or self.settings,
            transport=receiver,
            clock=clock or Clock(NOW),
        )

    def claim(self, clock: Callable[[], dt.datetime] | None = None) -> status_delivery.Claim:
        claimed = claim_next(
            self.factory,
            lease_seconds=self.settings.status_webhook_lease_seconds,
            clock=clock or Clock(NOW),
        )
        assert claimed is not None
        return claimed

    def process(
        self,
        claim: status_delivery.Claim,
        receiver: Receiver,
        clock: Callable[[], dt.datetime] | None = None,
    ) -> str:
        return process(
            self.factory, self.settings, claim, transport=receiver, clock=clock or Clock(NOW)
        )


def expected_payload(scene: Scene, version: int, **fields: object) -> dict[str, object]:
    base: dict[str, object] = {
        "event_id": f"sevt_{scene.project.public_id}_{version}",
        "tenant_id": scene.customer_id,
        "project_id": scene.project.public_id,
        "account_status": "ENABLED",
        "billing_status": "SUSPENDED",
        "billing_mode": "PREPAID",
        "project_integration_status": "ENABLED",
        "effective_status": "BLOCK_AI",
        "reason_code": "BALANCE_NOT_POSITIVE",
        "status_version": version,
        "effective_at": NOW.isoformat(),
    }
    return {**base, **fields}


def capture(factory, action) -> list[str]:
    engine = factory.kw["bind"]
    statements: list[str] = []

    def record(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        statements.append(" ".join(statement.split()).upper())

    event.listen(engine, "before_cursor_execute", record)
    try:
        action()
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert statements, "没有抓到任何语句，下面的断言在空列表上空转"
    return statements


# --- 正常推送 -------------------------------------------------------------------


def test_a_balance_suspension_is_pushed_signed_per_api_md(factory, settings) -> None:
    """设计 §7「正常推送」：一次 POST，按 api.md 验签，11 个字段，游标 = 新版本。"""
    scene = Scene(factory, settings)
    scene.set_billing_active()
    scene.charge("20", now=LATER)
    assert scene.version() == 1
    receiver = Receiver()

    result = scene.dispatch(receiver, Clock(LATER))

    assert (result.claimed, result.outcomes) == (1, {DELIVERED: 1})
    [request] = receiver.requests
    assert request.full_url == HOOK
    assert request.get_method() == "POST"
    assert receiver.timeouts == [5]
    payload = payload_of(request)
    assert set(payload) == PAYLOAD_KEYS
    assert payload == expected_payload(
        scene, 1, effective_at=LATER.isoformat(), reason_code="BALANCE_NOT_POSITIVE"
    )
    # 按键排序、无多余空白：签名覆盖的就是这些原始字节。
    assert request.data == json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    headers = headers_of(request)
    assert headers["content-type"] == "application/json"
    assert headers["x-acuven-event-id"] == payload["event_id"]
    assert headers["x-acuven-key-version"] == "1"
    assert headers["x-acuven-timestamp"] == str(int(LATER.replace(tzinfo=dt.UTC).timestamp()))
    verify(request, scene.secret, LATER)
    with pytest.raises(SignatureRejected):
        verify(request, "whs_" + "1" * 64, LATER)
    # 响应体不读，连接关掉。
    [response] = receiver.responses
    assert response.reads == []
    assert response.closed

    assert scene.project.delivery() == {
        "delivered_version": 1,
        "attempted_version": None,
        "attempt_count": 0,
        "next_attempt_at": LATER,
        "lease_token": None,
        "lease_expires_at": None,
        "last_attempt_at": LATER,
        "delivered_at": LATER,
        "last_http_status": 200,
        "last_error_code": None,
    }
    # 已追上：下一轮不再领取。
    again = Receiver()
    assert scene.dispatch(again, Clock(LATER + dt.timedelta(minutes=5))).claimed == 0
    assert again.requests == []


@pytest.mark.parametrize("point", ["balance", "account", "internal", "project"])
def test_each_increment_point_is_pushed(factory, settings, point: str) -> None:
    """设计 §7「四个递增点」：每次版本 +1、`status_changed_at` 更新，`dispatch` 都会推送。"""
    scene = Scene(factory, settings)
    first = Receiver()
    scene.dispatch(first)
    assert first.versions() == [0]
    if point == "project":
        # 直接改库造一个停用的项目（不加版本），再经服务重新启用：这才是一次推送的跃迁。
        with factory() as session:
            statement = update(Project).where(Project.id == scene.project.pk)
            session.execute(statement.values(integration_status=IntegrationStatus.DISABLED))
            session.commit()

    if point == "balance":
        scene.top_up(now=LATER)
        changed = {"billing_status": "ACTIVE", "effective_status": "ALLOW_AI", "reason_code": None}
    elif point == "account":
        scene.set_account(AccountStatus.DISABLED, now=LATER)
        changed = {"account_status": "DISABLED", "reason_code": "ACCOUNT_DISABLED"}
    elif point == "internal":
        scene.internal(now=LATER)
        changed = {
            "billing_mode": "INTERNAL_METERED_ONLY",
            "effective_status": "ALLOW_AI",
            "reason_code": None,
        }
    else:
        scene.project.change(IntegrationStatus.ENABLED, now=LATER)
        changed = {}

    assert scene.tenant()[3:] == (1, LATER)
    receiver = Receiver()
    scene.dispatch(receiver, Clock(LATER))
    [payload] = receiver.payloads()
    assert payload == expected_payload(scene, 1, effective_at=LATER.isoformat(), **changed)
    assert scene.project.delivery()["delivered_version"] == 1


# --- 失败与退避 -----------------------------------------------------------------


def test_failures_back_off_and_success_resets(factory, settings) -> None:
    """设计 §7「失败退避」：500、超时、302、200 → 计数 1、2、3 后清零；302 不跟随。"""
    scene = Scene(factory, settings)
    before = scene.tenant()
    receiver = Receiver(500, TimeoutError(), 302, 200)
    clock = Clock(NOW)
    expected = [
        (1, 30, 500, HTTP_STATUS),
        (2, 60, None, TIMEOUT),
        (3, 120, 302, HTTP_STATUS),
    ]

    for number, (count, wait, status, code) in enumerate(expected, start=1):
        scene.dispatch(receiver, clock)
        assert len(receiver.requests) == number, "302 不跟随：每一轮恰好一次请求"
        row = scene.project.delivery()
        assert (row["attempt_count"], row["last_http_status"], row["last_error_code"]) == (
            count,
            status,
            code,
        )
        assert row["next_attempt_at"] == clock.now + dt.timedelta(seconds=wait)
        assert row["last_attempt_at"] == clock.now
        assert (row["lease_token"], row["lease_expires_at"], row["attempted_version"]) == (
            None,
            None,
            None,
        )
        assert row["delivered_version"] is None
        # 退避期间不领取。
        clock.advance(wait - 1)
        assert scene.dispatch(receiver, clock).claimed == 0
        clock.advance(1)

    scene.dispatch(receiver, clock)

    row = scene.project.delivery()
    assert (row["attempt_count"], row["delivered_version"]) == (0, 0)
    assert (row["last_http_status"], row["last_error_code"]) == (200, None)
    assert receiver.versions() == [0, 0, 0, 0]
    # 每次重发都重新签名，`event_id` 不变。
    stamps = {headers_of(request)["x-acuven-timestamp"] for request in receiver.requests}
    assert len(stamps) == 4
    assert {headers_of(r)["x-acuven-event-id"] for r in receiver.requests} == {
        f"sevt_{scene.project.public_id}_0"
    }
    # INV-1：投递失败不改任何状态。
    assert scene.tenant() == before


def test_retries_have_no_upper_limit(sqlite_factory, settings) -> None:
    """设计 §7「不设上限」：连续失败 10 次仍会被领取，`next_attempt_at` 停在退避上限。"""
    scene = Scene(sqlite_factory, settings)
    receiver = Receiver(*([503] * 11))
    clock = Clock(NOW)
    waits = []

    for _ in range(10):
        assert scene.dispatch(receiver, clock).claimed == 1
        row = scene.project.delivery()
        wait = (row["next_attempt_at"] - clock.now).total_seconds()  # type: ignore[operator]
        waits.append(wait)
        clock.advance(wait)

    assert waits == [30, 60, 120, 240, 480, 960, 1920, 3600, 3600, 3600]
    assert scene.project.delivery()["attempt_count"] == 10
    assert scene.dispatch(receiver, clock).claimed == 1
    assert scene.project.delivery()["attempt_count"] == 11
    assert len(receiver.requests) == 11


@pytest.mark.parametrize(
    ("answer", "status", "code"),
    [
        (204, 204, None),
        (299, 299, None),
        (301, 301, HTTP_STATUS),
        (404, 404, HTTP_STATUS),
        (500, 500, HTTP_STATUS),
        (TimeoutError(), None, TIMEOUT),
        (urllib.error.URLError(TimeoutError()), None, TIMEOUT),
        (urllib.error.URLError(ConnectionRefusedError()), None, CONNECTION_ERROR),
        (ConnectionResetError(), None, CONNECTION_ERROR),
        (http.client.RemoteDisconnected(), None, CONNECTION_ERROR),
    ],
)
def test_only_2xx_is_delivered(answer: Answer, status: int | None, code: str | None) -> None:
    """设计 §2「外部系统与异步边界」：只把 2xx 视为送达，其余一律重试。"""
    receiver = Receiver(answer)
    request = urllib.request.Request(HOOK, data=b"{}", method="POST")

    result = status_delivery.send(request, transport=receiver, timeout_seconds=5)

    assert result == Attempt(error_code=code, http_status=status)
    assert result.delivered is (code is None)


def test_a_non_2xx_response_object_is_a_failure() -> None:
    """transport 直接返回（而不是抛出）非 2xx 时同样是失败，响应体同样不读。"""
    response = FakeResponse(302)
    request = urllib.request.Request(HOOK, data=b"{}", method="POST")

    result = status_delivery.send(request, transport=lambda _r, _t: response, timeout_seconds=5)

    assert result == Attempt(error_code=HTTP_STATUS, http_status=302)
    assert (response.reads, response.closed) == ([], True)


def test_the_real_transport_is_the_no_redirect_opener() -> None:
    """不跟随重定向：默认 transport 就是 BNM 适配器的 opener，3xx 以 HTTPError 浮上来。"""
    assert status_delivery.urllib_transport is fx_source.urllib_transport
    handlers = [type(handler) for handler in fx_source._OPENER.handlers]
    assert fx_source._NoRedirect in handlers
    assert urllib.request.HTTPRedirectHandler not in handlers


# --- 签名密钥 -------------------------------------------------------------------


def test_no_active_secret_sends_nothing_until_one_is_activated(factory, settings) -> None:
    """设计 §7「缺密钥」：不发 POST；`NO_ACTIVE_SECRET`；启用密钥后下一轮送达。"""
    scene = Scene(factory, settings, secret=False)
    receiver = Receiver()

    scene.dispatch(receiver)

    assert receiver.requests == []
    assert scene.project.delivery() == {
        "delivered_version": None,
        "attempted_version": None,
        "attempt_count": 1,
        "next_attempt_at": NOW + dt.timedelta(seconds=30),
        "lease_token": None,
        "lease_expires_at": None,
        "last_attempt_at": NOW,
        "delivered_at": None,
        "last_http_status": None,
        "last_error_code": NO_ACTIVE_SECRET,
    }

    # 启用密钥时重排投递（AIH-TASK-049）：`next_attempt_at = now`，不等退避。
    scene.project.new_secret(now=NOW + dt.timedelta(seconds=5))
    scene.dispatch(receiver, Clock(NOW + dt.timedelta(seconds=5)))

    [request] = receiver.requests
    verify(request, scene.secret, NOW + dt.timedelta(seconds=5))
    row = scene.project.delivery()
    assert (row["delivered_version"], row["attempt_count"], row["last_error_code"]) == (0, 0, None)


def _break_the_ciphertext(scene: Scene, how: str, other: Scene | None = None) -> None:
    with scene.factory() as session:
        row = session.execute(
            select(ProjectWebhookSecret).where(
                ProjectWebhookSecret.project_id == scene.project.pk,
                ProjectWebhookSecret.status == WebhookSecretStatus.ACTIVE,
            )
        ).scalar_one()
        if how == "tampered":
            head, _dot, last = row.encrypted_secret.rpartition(".")
            flipped = ("B" if last[0] == "A" else "A") + last[1:]
            row.encrypted_secret = f"{head}.{flipped}"
        else:
            # AAD 换成别的项目：别的项目的密文拷过来，解不开。
            assert other is not None
            theirs = session.execute(
                select(ProjectWebhookSecret.encrypted_secret).where(
                    ProjectWebhookSecret.project_id == other.project.pk
                )
            ).scalar_one()
            row.encrypted_secret = theirs
        session.commit()


@pytest.mark.parametrize("how", ["tampered", "other project"])
def test_a_secret_that_cannot_be_opened_is_retried_until_a_new_one(
    factory, settings, caplog, how: str
) -> None:
    """设计 §7「密钥解不开」与「成功清除错误」：不发 POST；`SECRET_DECRYPT_FAILED`、计数 +1、
    租约清空；签发并启用新密钥后 `next_attempt_at = now`，下一轮送达，错误码清空。
    """
    caplog.set_level(logging.DEBUG)
    scene = Scene(factory, settings)
    # 另一个项目只提供密文，没有地址：不参与投递。
    other = Scene(factory, settings, url=None)
    _break_the_ciphertext(scene, how, other)
    receiver = Receiver()
    clock = Clock(NOW)

    for count in (1, 2, 3):
        scene.dispatch(receiver, clock)
        row = scene.project.delivery()
        assert (row["attempt_count"], row["last_error_code"]) == (count, SECRET_DECRYPT_FAILED)
        assert row["lease_token"] is None
        assert (row["attempted_version"], row["last_http_status"]) == (None, None)
        clock.now = row["next_attempt_at"]  # type: ignore[assignment]
    assert receiver.requests == []
    # 错误日志只记项目 id 与 key_version（设计 §5）。
    logged = [r for r in caplog.records if getattr(r, "error_code", "") == SECRET_DECRYPT_FAILED]
    assert len(logged) == 3
    assert (logged[0].project_id, logged[0].key_version) == (scene.project.public_id, 1)

    recovered = clock.now - dt.timedelta(seconds=100)
    key_version, secret = scene.project.new_secret(now=recovered)
    assert scene.project.delivery()["next_attempt_at"] == recovered
    scene.dispatch(receiver, Clock(recovered))

    [request] = receiver.requests
    assert headers_of(request)["x-acuven-key-version"] == str(key_version) == "2"
    verify(request, secret, recovered)
    row = scene.project.delivery()
    assert (row["delivered_version"], row["attempt_count"]) == (0, 0)
    assert (row["last_error_code"], row["last_http_status"]) == (None, 200)


def test_an_unconfigured_master_key_sends_nothing(factory, settings) -> None:
    """设计 §7「主密钥未配置」：不发 POST；`ENCRYPTION_NOT_CONFIGURED`，计数与退避同上。"""
    scene = Scene(factory, settings)
    receiver = Receiver()
    broken = Settings(master_key_file="")

    scene.dispatch(receiver, settings=broken)

    assert receiver.requests == []
    row = scene.project.delivery()
    assert (row["attempt_count"], row["last_error_code"]) == (1, ENCRYPTION_NOT_CONFIGURED)
    assert row["next_attempt_at"] == NOW + dt.timedelta(seconds=30)
    assert (row["lease_token"], row["last_http_status"]) == (None, None)

    # 运维补上主密钥：退避到期后自动恢复。
    scene.dispatch(receiver, Clock(NOW + dt.timedelta(seconds=30)))
    [request] = receiver.requests
    verify(request, scene.secret, NOW + dt.timedelta(seconds=30))
    assert scene.project.delivery()["last_error_code"] is None


def test_the_secret_checks_run_in_a_fixed_order(sqlite_factory, settings, monkeypatch) -> None:
    """设计 §4：先查 ACTIVE 行（只读、不解密）→ 再加载主密钥 → 再解密。
    没有 ACTIVE 时根本不加载主密钥：即使主密钥也没配置，错误码仍是 `NO_ACTIVE_SECRET`。
    """
    scene = Scene(sqlite_factory, settings, secret=False)
    loads: list[str] = []
    real = status_delivery.load_keyring

    def spy(given: Settings):
        loads.append(given.master_key_file)
        return real(given)

    monkeypatch.setattr(status_delivery, "load_keyring", spy)

    scene.dispatch(Receiver(), settings=Settings(master_key_file=""))

    assert loads == []
    assert scene.project.delivery()["last_error_code"] == NO_ACTIVE_SECRET


# --- 地址 -----------------------------------------------------------------------


def test_a_new_address_receives_the_current_version(factory, settings) -> None:
    """设计 §7「改 URL」：已送达的版本，改地址后新地址再收到一次。"""
    scene = Scene(factory, settings)
    scene.set_account(AccountStatus.DISABLED)
    first = Receiver()
    scene.dispatch(first)
    assert first.versions() == [1]

    scene.project.set_url(OTHER_HOOK, now=LATER)
    assert scene.project.delivery()["delivered_version"] is None
    receiver = Receiver()
    scene.dispatch(receiver, Clock(LATER))

    [request] = receiver.requests
    assert request.full_url == OTHER_HOOK
    assert payload_of(request)["status_version"] == 1
    verify(request, scene.secret, LATER)
    assert scene.project.delivery()["delivered_version"] == 1


def test_the_first_delivery_sets_the_cursor(factory, settings) -> None:
    """设计 §7「首次送达」：新设的地址（游标为空）→ 送达 v3；下一轮不再领取。"""
    scene = Scene(factory, settings, url=None, secret=False)
    for status in (AccountStatus.DISABLED, AccountStatus.ENABLED, AccountStatus.DISABLED):
        scene.set_account(status)
    scene.project.new_secret()
    receiver = Receiver()
    assert scene.dispatch(receiver).claimed == 0, "没有地址：不领取"

    scene.project.set_url(HOOK)
    assert scene.project.delivery()["delivered_version"] is None
    scene.dispatch(receiver)

    assert receiver.versions() == [3]
    assert scene.project.delivery()["delivered_version"] == 3
    assert scene.dispatch(receiver, Clock(LATER)).claimed == 0
    assert len(receiver.requests) == 1


def test_an_address_change_voids_the_request_in_flight(factory, settings) -> None:
    """设计 §7「改 URL 与在途请求并发」：发往旧地址的请求返回 200 时令牌已被清空，结算不写，
    `delivered_version` 仍为空；之后新地址收到同一版本。
    """
    scene = Scene(factory, settings)
    receiver = Receiver(on_send=lambda _r: scene.project.set_url(OTHER_HOOK))
    claim = scene.claim()

    assert scene.process(claim, receiver) == SUPERSEDED

    [old] = receiver.requests
    assert old.full_url == HOOK
    row = scene.project.delivery()
    assert (row["delivered_version"], row["delivered_at"], row["last_http_status"]) == (
        None,
        None,
        None,
    )
    assert (row["lease_token"], row["next_attempt_at"]) == (None, NOW)

    later = Receiver()
    scene.dispatch(later)
    [new] = later.requests
    assert new.full_url == OTHER_HOOK
    assert payload_of(new)["status_version"] == payload_of(old)["status_version"] == 0
    assert scene.project.delivery()["delivered_version"] == 0


def test_a_cleared_address_is_not_claimed(sqlite_factory, settings) -> None:
    scene = Scene(sqlite_factory, settings)
    scene.project.set_url(None)
    receiver = Receiver()

    assert scene.dispatch(receiver).claimed == 0
    assert receiver.requests == []


# --- 停用、新版本与在途 -----------------------------------------------------------


def test_a_disabled_project_is_not_pushed(factory, settings) -> None:
    """设计 §7「停用不推」：停用后不发 POST，`delivered_version` 不动；有效状态是
    `BLOCK_AI` / `PROJECT_INTEGRATION_DISABLED`；重新启用后收到新版本的 `ALLOW_AI`。
    """
    scene = Scene(factory, settings, top_up=True)
    first = Receiver()
    scene.dispatch(first)
    assert first.versions() == [1]

    scene.project.change(IntegrationStatus.DISABLED, now=LATER)
    receiver = Receiver()
    assert scene.dispatch(receiver, Clock(LATER)).claimed == 0
    assert receiver.requests == []
    assert scene.project.delivery()["delivered_version"] == 1
    # `effective-status` 用的就是这个函数与这几列（app/api/integration_usage.py）。
    with factory() as session:
        tenant = session.get(Tenant, scene.tenant_id)
        project = session.get(Project, scene.project.pk)
        assert tenant is not None and project is not None
        assert tenant.status_version == 2
        effective = compose_effective_status(
            tenant.account_status,
            tenant.billing_status,
            tenant.billing_mode,
            project.integration_status,
        )
    assert effective == (EffectiveStatus.BLOCK_AI, ReasonCode.PROJECT_INTEGRATION_DISABLED)

    enabled_at = LATER + dt.timedelta(minutes=1)
    scene.project.change(IntegrationStatus.ENABLED, now=enabled_at)
    scene.dispatch(receiver, Clock(enabled_at))

    [payload] = receiver.payloads()
    assert payload == expected_payload(
        scene,
        3,
        billing_status="ACTIVE",
        effective_status="ALLOW_AI",
        reason_code=None,
        effective_at=enabled_at.isoformat(),
    )
    assert scene.project.delivery()["delivered_version"] == 3


def test_a_new_version_cuts_the_backoff_short(factory, settings) -> None:
    """设计 §7「退避期间出现新版本」：退避到上限（1 小时）→ 跃迁 → 本轮即发送。"""
    scene = Scene(factory, settings)
    receiver = Receiver(*([500] * 8))
    clock = Clock(NOW)
    for _ in range(8):
        scene.dispatch(receiver, clock)
        clock.now = scene.project.delivery()["next_attempt_at"]  # type: ignore[assignment]
    failed_at = scene.project.delivery()["last_attempt_at"]
    assert clock.now == failed_at + dt.timedelta(hours=1)  # type: ignore[operator]

    moment = failed_at + dt.timedelta(seconds=10)  # type: ignore[operator]
    scene.set_account(AccountStatus.DISABLED, now=moment)
    assert scene.project.delivery()["next_attempt_at"] == moment
    scene.dispatch(receiver, Clock(moment))

    assert receiver.versions()[-1] == 1
    row = scene.project.delivery()
    assert (row["delivered_version"], row["attempt_count"], row["last_error_code"]) == (1, 0, None)


def test_a_new_version_during_a_failing_send_is_retried_at_once(factory, settings) -> None:
    """设计 §7「在途时出现新版本」：领取 v0 → 发送中跃迁到 v1 → v0 返回 500 并结算：
    结算读到 1 > 0，`next_attempt_at = now`；同一次 dispatch 立即再领取并发送 v1。
    """
    scene = Scene(factory, settings)
    bumped: list[int] = []

    def bump_once(_request: urllib.request.Request) -> None:
        if not bumped:
            bumped.append(1)
            scene.set_account(AccountStatus.DISABLED)

    receiver = Receiver(500, on_send=bump_once)
    claim = scene.claim()
    assert scene.process(claim, receiver) == HTTP_STATUS

    row = scene.project.delivery()
    assert (row["attempt_count"], row["last_http_status"], row["next_attempt_at"]) == (1, 500, NOW)
    assert row["delivered_version"] is None

    result = scene.dispatch(receiver)

    assert result.outcomes == {DELIVERED: 1}
    assert receiver.versions() == [0, 1]
    row = scene.project.delivery()
    assert (row["delivered_version"], row["attempt_count"]) == (1, 0)


def test_the_version_sent_is_the_claimed_snapshot(factory, settings) -> None:
    """设计 §7「发送期间版本变化」：领取 v0 → 发送前变成 v1 →
    先发 v0（`delivered_version = 0`），同一次 dispatch 再发 v1，最终 `delivered_version = 1`。
    """
    scene = Scene(factory, settings)
    claim = scene.claim()
    scene.set_account(AccountStatus.DISABLED)
    receiver = Receiver()

    assert scene.process(claim, receiver) == DELIVERED

    [payload] = receiver.payloads()
    assert (payload["status_version"], payload["account_status"]) == (0, "ENABLED")
    row = scene.project.delivery()
    assert (row["delivered_version"], row["next_attempt_at"]) == (0, NOW)

    scene.dispatch(receiver)

    assert receiver.versions() == [0, 1]
    assert receiver.payloads()[1]["account_status"] == "DISABLED"
    assert scene.project.delivery()["delivered_version"] == 1


def test_a_lagging_success_never_moves_the_cursor_back(sqlite_factory, settings) -> None:
    """GREATEST：一次旧版本的 2xx（例如慢请求跨过了租约）不会把更大的游标拉回去。"""
    scene = Scene(sqlite_factory, settings)
    claim = scene.claim()
    scene.project.set_delivery(delivered_version=7)

    assert scene.process(claim, Receiver()) == DELIVERED

    assert scene.project.delivery()["delivered_version"] == 7


# --- 租约与预算 ------------------------------------------------------------------


def test_an_expired_lease_is_claimed_again(factory, settings) -> None:
    """设计 §7「租约过期」：领取后不结算，60 秒内不重复领取；61 秒后重新领取。
    旧令牌的结算不写。
    """
    scene = Scene(factory, settings)
    stale = scene.claim()
    row = scene.project.delivery()
    assert (row["lease_token"], row["attempted_version"]) == (stale.token, 0)
    assert row["lease_expires_at"] == NOW + dt.timedelta(seconds=60)
    receiver = Receiver()

    assert scene.dispatch(receiver, Clock(NOW + dt.timedelta(seconds=59))).claimed == 0
    result = scene.dispatch(receiver, Clock(NOW + dt.timedelta(seconds=61)))

    assert (result.claimed, len(receiver.requests)) == (1, 1)
    delivered = scene.project.delivery()
    assert delivered["delivered_version"] == 0
    written = settle(
        scene.factory,
        stale,
        Attempt(error_code=HTTP_STATUS, http_status=500),
        retry_base_seconds=30,
        retry_cap_seconds=3600,
        clock=Clock(NOW + dt.timedelta(seconds=62)),
    )
    assert written is False
    assert scene.project.delivery() == delivered


def test_dispatch_stops_at_its_budget(sqlite_factory, settings) -> None:
    """设计 §7「dispatch 预算」：30 行到期，每次发送耗时 1 秒（伪时钟）：
    只处理到 12 秒为止；每行的租约从它被领取时算起；没有一行被重复领取。
    """
    scene = Scene(sqlite_factory, settings, url=None, secret=False)
    projects = [scene.project]
    projects += [scene.add_project(url=None, secret=False) for _ in range(29)]
    by_path: dict[str, ProjectHandle] = {}
    for number, project in enumerate(projects):
        project.set_url(f"https://hooks.example.com/p{number}")
        project.new_secret()
        by_path[f"/p{number}"] = project
    elapsed = [0.0]
    leases: list[tuple[dt.datetime, object]] = []

    def clock() -> dt.datetime:
        return NOW + dt.timedelta(seconds=elapsed[0])

    def one_second(request: urllib.request.Request) -> None:
        project = by_path[urlsplit(request.full_url).path]
        leases.append((clock(), project.delivery()["lease_expires_at"]))
        elapsed[0] += 1

    receiver = Receiver(on_send=one_second)

    result = dispatch(
        sqlite_factory, settings, transport=receiver, clock=clock, ticker=lambda: elapsed[0]
    )

    assert result.claimed == 12
    paths = [urlsplit(request.full_url).path for request in receiver.requests]
    assert len(paths) == len(set(paths)) == 12
    for claimed_at, lease_expires_at in leases:
        assert lease_expires_at == claimed_at + dt.timedelta(seconds=60)
    delivered = [p for p in projects if p.delivery()["delivered_version"] == 0]
    assert len(delivered) == 12
    waiting = [p for p in projects if p.delivery()["delivered_version"] is None]
    assert len(waiting) == 18
    assert all(p.delivery()["lease_token"] is None for p in waiting)


# --- 租户越权 -------------------------------------------------------------------


def test_only_the_tenant_that_moved_is_pushed(factory, settings) -> None:
    """设计 §7「租户越权」与 INV-8：只有 A 的地址收到 POST，payload 只含 A 的 id，
    签名用 A 项目的密钥；B 的投递行不变。
    """
    a = Scene(factory, settings)
    b = Scene(factory, settings)
    b.project.set_url(OTHER_HOOK)
    first = Receiver()
    a.dispatch(first)
    assert sorted(r.full_url for r in first.requests) == sorted([HOOK, OTHER_HOOK])
    b_before = b.project.delivery()

    a.set_account(AccountStatus.DISABLED, now=LATER)
    receiver = Receiver()
    a.dispatch(receiver, Clock(LATER))

    [request] = receiver.requests
    assert request.full_url == HOOK
    payload = payload_of(request)
    assert (payload["tenant_id"], payload["project_id"]) == (a.customer_id, a.project.public_id)
    assert b.customer_id not in request.data.decode()  # type: ignore[union-attr]
    assert b.project.public_id not in request.data.decode()  # type: ignore[union-attr]
    verify(request, a.secret, LATER)
    with pytest.raises(SignatureRejected):
        verify(request, b.secret, LATER)
    assert b.project.delivery() == b_before


# --- 日志脱敏 -------------------------------------------------------------------


def test_no_address_signature_or_secret_in_logs(sqlite_factory, settings, caplog) -> None:
    """设计 §7「日志脱敏」：推送失败一次，日志与审计里没有完整 URL、签名、密钥、响应体。"""
    caplog.set_level(logging.DEBUG)
    scene = Scene(sqlite_factory, settings)
    receiver = Receiver(500)

    scene.dispatch(receiver)

    [request] = receiver.requests
    signature = headers_of(request)["x-acuven-signature"]
    [record] = [r for r in caplog.records if getattr(r, "error_code", None) == HTTP_STATUS]
    assert (record.project_id, record.status_version, record.http_status) == (
        scene.project.public_id,
        0,
        500,
    )
    logged = caplog.text + "".join(str(vars(r)) for r in caplog.records)
    for secret_text in (HOOK, PATH_TOKEN, "hooks.example.com", signature, scene.secret, "yyyy"):
        assert secret_text not in logged
    with sqlite_factory() as session:
        audits = list(session.execute(select(AuditLog)).scalars())
    assert audits
    for row in audits:
        for written in (row.before_state or "", row.after_state or "", row.reason or ""):
            assert PATH_TOKEN not in written
            assert scene.secret not in written


def test_a_claim_does_not_show_its_address() -> None:
    claim = status_delivery.Claim(
        project_id=1,
        tenant_id=1,
        project_public_id="p",
        status_version=0,
        attempt_count=0,
        token="t",
        url=HOOK,
        payload={"event_id": "sevt_p_0"},
    )
    assert PATH_TOKEN not in repr(claim)
    assert "hooks.example.com" not in repr(claim)


# --- 不经 Celery ----------------------------------------------------------------


def test_the_task_body_delivers_without_celery(sqlite_factory, settings, monkeypatch) -> None:
    """设计 §7「Redis / Celery 丢失」与 INV-14：待推送的状态只在库里，
    不经 Celery 直接调用任务体（默认 transport）也能补发。
    """
    scene = Scene(sqlite_factory, settings)
    receiver = Receiver()
    monkeypatch.setattr(status_webhook, "_session_factory", lambda: sqlite_factory)
    monkeypatch.setattr(status_webhook, "get_settings", lambda: settings)
    monkeypatch.setattr(status_delivery, "urllib_transport", receiver)

    assert status_webhook.dispatch() == 1

    assert receiver.versions() == [0]
    assert scene.project.delivery()["delivered_version"] == 0
    assert status_webhook.dispatch() == 0


# --- 加锁顺序：两种库都看语句顺序 -------------------------------------------------------


def test_claim_locks_only_the_delivery_row_and_settle_reads_the_tenant_first(
    factory, settings
) -> None:
    """设计 §2「事务边界」：
    领取 `FOR UPDATE OF project_status_deliveries SKIP LOCKED LIMIT 1`；
    结算先 `SELECT … FROM tenants … FOR SHARE`，再带令牌 UPDATE 投递行。
    """
    scene = Scene(factory, settings)
    engine = factory.kw["bind"]
    claims: list[status_delivery.Claim] = []

    claim_sql = capture(factory, lambda: claims.append(scene.claim()))
    settle_sql = capture(
        factory,
        lambda: settle(
            factory,
            claims[0],
            Attempt(error_code=None, http_status=200),
            retry_base_seconds=30,
            retry_cap_seconds=3600,
            clock=Clock(NOW),
        ),
    )

    [select_sql] = [s for s in claim_sql if s.startswith("SELECT")]
    # MySQL 方言编译成 INNER JOIN，SQLite 编译成 JOIN。
    assert "FROM PROJECT_STATUS_DELIVERIES JOIN PROJECTS" in select_sql.replace(
        "INNER JOIN", "JOIN"
    )
    assert "LIMIT" in select_sql
    assert not [s for s in claim_sql if "WALLET" in s]
    tenant = next(i for i, s in enumerate(settle_sql) if s.startswith("SELECT"))
    delivery = next(
        i for i, s in enumerate(settle_sql) if s.startswith("UPDATE PROJECT_STATUS_DELIVERIES")
    )
    assert "FROM TENANTS" in settle_sql[tenant]
    assert tenant < delivery
    assert "PROJECT_STATUS_DELIVERIES.LEASE_TOKEN =" in settle_sql[delivery]
    if engine.dialect.name == "mysql":
        assert select_sql.endswith("FOR UPDATE OF PROJECT_STATUS_DELIVERIES SKIP LOCKED")
        assert settle_sql[tenant].endswith(("FOR SHARE", "LOCK IN SHARE MODE"))


def test_the_service_does_not_import_the_wallet() -> None:
    """INV-4：投递不碰钱包；INV-1：不改租户与项目，只写投递行。"""
    source = inspect.getsource(status_delivery)
    assert "wallet" not in source.lower()
    assert "update(Tenant)" not in source
    assert "update(Project)" not in source


# --- 并发：只在真 MySQL 上 ------------------------------------------------------------


def _lock_and_bump(scene: Scene, session: Session, now: dt.datetime) -> None:
    """Connection A: the account-status path up to (not including) its commit."""
    from app.services import account_status

    tenant = tenancy.get_tenant_by_public_id(session, scene.customer_id, for_update=True)
    assert tenant is not None
    tenant.account_status = AccountStatus.DISABLED

    def make_event(locked: Tenant) -> DomainOutbox:
        payload = {"status_version": locked.status_version}
        return account_status._status_event(locked, payload, now)

    tenancy.bump_status_version(session, tenant, now, make_event=make_event)
    session.flush()


@pytest.mark.parametrize("order", ["bump first", "settle first"])
def test_settle_and_a_transition_serialize_on_the_tenant(mysql_factory, settings, order) -> None:
    """设计 §7「结算与跃迁并发（两种提交顺序）」：

    ① A 执行 `bump_status_version` 到 v1 但不提交；
    B 对 v0 做失败结算，阻塞在 `FOR SHARE`；A 提交后 B 读到 v1，`next_attempt_at = now`。
    ② B 先完成失败结算（退避 30 秒）并提交，A 再跃迁并提交，把退避提前到 now。
    两种顺序结束后都 `next_attempt_at ≤ now`，下一轮即领取并发送 v1；无死锁。
    """
    factory = mysql_factory
    scene = Scene(factory, settings)
    claim = scene.claim()
    failed = Attempt(error_code=HTTP_STATUS, http_status=500)

    def settle_failure() -> bool:
        return settle(
            factory,
            claim,
            failed,
            retry_base_seconds=30,
            retry_cap_seconds=3600,
            clock=Clock(NOW),
        )

    if order == "bump first":
        with factory() as session_a:
            _lock_and_bump(scene, session_a, NOW)
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(settle_failure)
                time.sleep(1.5)
                blocked = not future.done()
                session_a.commit()
                assert future.result(timeout=60) is True
        assert blocked, "结算没有等跃迁提交"
    else:
        assert settle_failure() is True
        assert scene.project.delivery()["next_attempt_at"] == NOW + dt.timedelta(seconds=30)
        with factory() as session_a:
            _lock_and_bump(scene, session_a, NOW)
            session_a.commit()

    row = scene.project.delivery()
    assert row["next_attempt_at"] <= NOW  # type: ignore[operator]
    assert (row["attempt_count"], row["last_error_code"]) == (1, HTTP_STATUS)
    assert scene.version() == 1
    receiver = Receiver()
    scene.dispatch(receiver)
    assert receiver.versions() == [1]
    assert scene.project.delivery()["delivered_version"] == 1


def test_claims_skip_locked_rows(mysql_factory, settings) -> None:
    """设计 §7「加锁不成环」的领取一半：
    投递行被别的事务锁着（例如改地址持锁）时，领取跳过它、不等待；锁放开后照常领取。
    """
    factory = mysql_factory
    scene = Scene(factory, settings)
    with factory() as holder:
        holder.execute(
            select(ProjectStatusDelivery)
            .where(ProjectStatusDelivery.project_id == scene.project.pk)
            .with_for_update()
        ).one()
        started = time.monotonic()
        skipped = claim_next(factory, lease_seconds=60, clock=Clock(NOW))
        elapsed = time.monotonic() - started
        holder.rollback()

    assert skipped is None
    assert elapsed < 2
    assert scene.claim().status_version == 0


def test_two_dispatches_send_each_project_once(mysql_factory, settings) -> None:
    """设计 §7「并发 / 租约」：两个 `dispatch` 同时运行，每个项目只发 1 次。"""
    factory = mysql_factory
    scene = Scene(factory, settings, url=None, secret=False)
    projects = [scene.project] + [scene.add_project(url=None, secret=False) for _ in range(5)]
    for number, project in enumerate(projects):
        project.set_url(f"https://hooks.example.com/p{number}")
        project.new_secret()
    receiver = Receiver(on_send=lambda _r: time.sleep(0.2))
    start = threading.Barrier(2)

    def run() -> status_delivery.DispatchResult:
        start.wait(timeout=30)
        return dispatch(factory, settings, transport=receiver, clock=Clock(NOW))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result(timeout=120) for future in [pool.submit(run) for _ in range(2)]]

    paths = sorted(urlsplit(request.full_url).path for request in receiver.requests)
    assert paths == sorted(f"/p{number}" for number in range(6))
    assert sum(result.claimed for result in results) == 6
    assert all(project.delivery()["delivered_version"] == 0 for project in projects)


def test_dispatch_runs_alongside_transitions_and_address_changes(mysql_factory, settings) -> None:
    """设计 §7「加锁不成环」：`dispatch` 的领取与结算、
    `bump_status_version`（账户启停）、改地址同时反复执行：
    没有死锁、没有锁等待超时；最后一轮之后最新版本送达。
    """
    factory = mysql_factory
    scene = Scene(factory, settings)
    second = scene.add_project()
    rounds = 6
    start = threading.Barrier(3)
    receiver = Receiver(on_send=lambda _r: time.sleep(0.05))
    outcomes: list[str] = []

    def keep_dispatching() -> None:
        start.wait(timeout=30)
        for _ in range(rounds * 2):
            result = dispatch(factory, settings, transport=receiver, clock=Clock(NOW))
            outcomes.extend(result.outcomes)

    def toggle_account() -> None:
        start.wait(timeout=30)
        for number in range(rounds):
            target = AccountStatus.DISABLED if number % 2 == 0 else AccountStatus.ENABLED
            scene.set_account(target)

    def toggle_address() -> None:
        start.wait(timeout=30)
        for number in range(rounds):
            second.set_url(OTHER_HOOK if number % 2 == 0 else HOOK)

    jobs = (keep_dispatching, toggle_account, toggle_address)
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(job) for job in jobs]
        for future in futures:
            # 死锁（1213）或锁等待超时（1205）会在这里抛出。
            future.result(timeout=180)

    # 处理中的意外出错（例如结算撞上死锁）会被 dispatch 记日志后吞掉，这里把它找出来。
    assert set(outcomes) <= {DELIVERED, SUPERSEDED}
    final = Receiver()
    dispatch(factory, settings, transport=final, clock=Clock(NOW))
    assert scene.version() == rounds
    for project in (scene.project, second):
        assert project.delivery()["delivered_version"] == rounds
        assert project.delivery()["lease_token"] is None
