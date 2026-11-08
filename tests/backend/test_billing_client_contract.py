"""Billing Client ↔ server contract (design gate #571 v24 §7, the four rows of AIH-TASK-171).

客户端 `acuven_billing_client` 与服务端同时导入，服务端走 FastAPI `TestClient`，客户端通过
`ServerTransport`（把客户端的 `Transport` 转发到 `TestClient`）打到真实的接口：

- **签名过服务端**：客户端签的请求打到单条摄取接口，服务端的 `integration_auth` 验签通过，202；
- **载荷过服务端**：LLM 与 QUANTITY 两类事件经客户端写入、`deliver_once` 送到批量接口，
  回执 `accepted`、本地 `DELIVERED`；第二个客户端（另一个 outbox 文件）以相同的 `event_id`、
  `occurred_at` 与字段重记并 `deliver_once`，回执 `already_*`，第二个文件里也是 `DELIVERED`，
  服务端只有一份；
- **有效状态**：客户端对账打到服务端，应用成功，字段集合与服务端应答一致；
- **webhook**：服务端 `status_delivery` 用 `webhook_signing` 的密钥构造出站请求（注入的
  transport 只截下请求，不发网络），交给客户端验签通过并应用。

建租户、项目、凭据、webhook 密钥与计量类型的写法照 test_usage_ingest_api.py、
test_usage_ingest_batch_api.py 与 test_status_delivery.py。服务端库是内存 SQLite，不需要
`BILLING_TEST_DATABASE_URL`，SQLite 与 MySQL 两个分片里都照常运行、不 skip；这里不断言 SQL
文本（CLAUDE.md「写后端测试的约定」）。

Placeholders only (public repository): uuids, api keys and secrets are zeros with a counter;
usage numbers are fictional; there are no prices, rates or amounts here at all.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import time
import urllib.request
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from acuven_billing_client import (
    BillingClient,
    BillingClientConfig,
    DeliveryCounts,
    Response,
    Store,
    build_llm_event,
    new_ulid,
    request_headers,
)
from acuven_billing_client.delivery import BATCH_PATH, SINGLE_PATH
from acuven_billing_client.status import EFFECTIVE_STATUS_PATH
from acuven_billing_client.transport import MAX_RESPONSE_BYTES
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import integration_usage
from app.core.config import Settings
from app.core.crypto import encrypt_secret, load_keyring
from app.core.database import create_session_factory
from app.core.nonce import NonceStore
from app.main import create_app
from app.models.ai_catalog import SEED_METER_TYPES, UsageMeterType
from app.models.auth import User, UserRole, UserStatus
from app.models.base import Base
from app.models.integration import CredentialStatus, IntegrationCredential
from app.models.usage import UsageEvent, UsageEventStatus
from app.repositories import tenancy
from app.repositories.wallet import create_wallet
from app.services import status_delivery, webhook_signing
from app.services.auth import RequestContext
from app.services.customers import add_status_delivery
from app.services.integration_auth import credential_aad, verify_signature
from app.services.project_integration import set_status_webhook

# TestClient 的缺省地址；不是真实主机。
BASE_URL = "http://testserver"
ENVELOPE_FIELDS = {"success", "data", "error", "request_id"}

NOW = dt.datetime(2026, 10, 20, 8, 0, 0)
# 客户端的时刻都是带时区的；服务端的 `utc_now` 是不带时区的 UTC。
OCCURRED = NOW.replace(tzinfo=dt.UTC) - dt.timedelta(seconds=30)
CONTEXT = RequestContext(ip_address="203.0.113.71", user_agent="billing-client-contract-test")
COMPANY = "Billing Client Contract Test Sdn Bhd"
TEST_EMAIL_DOMAIN = "@billing-client-contract-test.example.com"

API_KEY = "ak_" + "0" * 31 + "1"
API_SECRET = "sk_" + "0" * 63 + "1"
# 路径里带一段像令牌的串、再带一个查询串：规范串第二行要含查询串。全零占位值。
HOOK = "https://hooks.example.com/acuven/tok_" + "0" * 32 + "?v=1"


def uuid7(number: int) -> str:
    """A UUIDv7 in its canonical spelling, zeros apart from the version, variant and counter."""
    return f"00000000-0000-7000-8000-{number:012d}"


def public_id(number: int) -> str:
    return f"00000000-0000-0000-0000-{number:012d}"


def epoch(moment: dt.datetime) -> int:
    return int(moment.replace(tzinfo=dt.UTC).timestamp())


def client_clock() -> float:
    """The client's clock: the same second as the server's frozen `utc_now`."""
    return float(epoch(NOW))


# --- 适配器 ---------------------------------------------------------------------------


@dataclass
class Exchange:
    method: str
    path: str
    status: int
    body: bytes

    def json(self) -> dict[str, Any]:
        return json.loads(self.body)


class ServerTransport:
    """The client's `Transport`, forwarded to the server's `TestClient`; keeps every exchange."""

    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.exchanges: list[Exchange] = []

    def send(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes, timeout: float
    ) -> Response:
        parts = urlsplit(url)
        assert f"{parts.scheme}://{parts.netloc}" == BASE_URL
        target = f"{parts.path}?{parts.query}" if parts.query else parts.path
        answer = self.client.request(method, target, content=body or None, headers=dict(headers))
        self.exchanges.append(Exchange(method, parts.path, answer.status_code, answer.content))
        return Response(status=answer.status_code, body=answer.content[:MAX_RESPONSE_BYTES])

    def to(self, path: str) -> list[Exchange]:
        return [exchange for exchange in self.exchanges if exchange.path == path]


# --- 服务端 ---------------------------------------------------------------------------


class FakeRedis:
    """redis-py's `SET … NX PXAT`, in memory."""

    def __init__(self) -> None:
        self.keys: dict[str, int] = {}

    def set(self, name: str, value: int, *, nx: bool, pxat: int) -> bool | None:
        if nx and name in self.keys:
            return None
        self.keys[name] = pxat
        return True


class CapturedResponse:
    """A 2xx answer to the outbound webhook; `status_delivery` reads no byte of it."""

    status = 200

    def close(self) -> None:
        pass


@dataclass
class World:
    """One server, one tenant × project with a credential, and the clients made against it."""

    app: FastAPI
    transport: ServerTransport
    tmp_path: Path
    tenant_pk: int
    project_pk: int
    tenant_public_id: str
    project_public_id: str
    clients: list[BillingClient] = field(default_factory=list)

    @property
    def factory(self) -> sessionmaker[Session]:
        return self.app.state.session_factory

    def config(self, name: str, **changes: Any) -> BillingClientConfig:
        values: dict[str, Any] = {
            "base_url": BASE_URL,
            "api_key": API_KEY,
            "key_version": 1,
            "secret": API_SECRET,
            "tenant_id": self.tenant_public_id,
            "project_id": self.project_public_id,
            "outbox_path": str(self.tmp_path / name),
        }
        values.update(changes)
        return BillingClientConfig(**values)

    def client(self, name: str, **changes: Any) -> BillingClient:
        billing = BillingClient(
            self.config(name, **changes), transport=self.transport, clock=client_clock
        )
        self.clients.append(billing)
        assert billing.store_available
        return billing

    def server_events(self) -> list[UsageEvent]:
        with self.factory() as session:
            return list(session.execute(select(UsageEvent).order_by(UsageEvent.id)).scalars())


def seed_meter_types(factory: sessionmaker[Session]) -> None:
    """What migration 0012 seeds, for the SQLite tables `create_all` builds empty."""
    with factory() as session:
        for number, seed in enumerate(SEED_METER_TYPES, start=900):
            session.add(
                UsageMeterType(
                    public_id=public_id(number),
                    code=seed.code,
                    display_name=seed.display_name,
                    payload_shape=seed.payload_shape,
                    unit=seed.unit,
                    quantity_kind=seed.quantity_kind,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        session.commit()


def make_tenant_and_project(factory: sessionmaker[Session]) -> tuple[int, str, int, str]:
    """A tenant with an empty wallet, and its project with its status delivery row."""
    with factory() as session:
        tenant = tenancy.create_tenant(
            session, company_name=COMPANY, email=f"ops{TEST_EMAIL_DOMAIN}", now=NOW
        )
        create_wallet(session, tenant_id=tenant.id, now=NOW)
        project = tenancy.create_project(session, tenant_id=tenant.id, name="Chatbot", now=NOW)
        add_status_delivery(session, project, now=NOW)
        session.commit()
        return int(tenant.id), tenant.public_id, int(project.id), project.public_id


def add_credential(app: FastAPI, *, tenant_pk: int, project_pk: int) -> None:
    """Version 1 of the api key, encrypted with the row's AAD like the admin service does."""
    keyring = load_keyring(app.state.settings)
    aad = credential_aad(API_KEY, 1)
    encrypted, master_version = encrypt_secret(keyring, API_SECRET, associated_data=aad)
    with app.state.session_factory() as session:
        session.add(
            IntegrationCredential(
                tenant_id=tenant_pk,
                project_id=project_pk,
                public_api_key=API_KEY,
                key_version=1,
                encrypted_secret=encrypted,
                encryption_key_version=master_version,
                status=CredentialStatus.ACTIVE,
                valid_from=NOW - dt.timedelta(days=1),
                created_at=NOW - dt.timedelta(days=1),
            )
        )
        session.commit()


def make_admin(factory: sessionmaker[Session]) -> User:
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


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    """Built in the setup phase: `create_app` reinstalls the root log handlers."""
    master = tmp_path / "master.key"
    master.write_text(f"1:{base64.b64encode(bytes(32)).decode()}\n", encoding="utf-8")
    settings = Settings(database_url="", master_key_file=str(master), redis_url="")
    application = create_app(settings)
    # StaticPool: the endpoint runs in a thread pool; the in-memory database is one connection.
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    application.state.engine = engine
    application.state.session_factory = create_session_factory(engine)
    application.state.nonce_store = NonceStore(FakeRedis())
    monkeypatch.setattr(integration_usage, "utc_now", lambda: NOW)
    factory = application.state.session_factory
    seed_meter_types(factory)
    tenant_pk, tenant_public_id, project_pk, project_public_id = make_tenant_and_project(factory)
    add_credential(application, tenant_pk=tenant_pk, project_pk=project_pk)
    built = World(
        app=application,
        transport=ServerTransport(TestClient(application, raise_server_exceptions=False)),
        tmp_path=tmp_path,
        tenant_pk=tenant_pk,
        project_pk=project_pk,
        tenant_public_id=tenant_public_id,
        project_public_id=project_public_id,
    )
    try:
        yield built
    finally:
        for billing in built.clients:
            billing.close()
        engine.dispose()


def outbox_counts(path: Path) -> DeliveryCounts:
    """The outbox file's counts, read the way the command line does (no tables created)."""
    with Store.open_existing(path, clock=time.time) as store:
        return store.delivery_counts()


def record_both(
    billing: BillingClient, *, llm_id: str | None, quantity_id: str | None
) -> tuple[str, str]:
    """One LLM and one QUANTITY event with fixed fields and `occurred_at`. (llm id, quantity id)."""
    llm = billing.record_llm_usage(
        provider="anthropic",
        model="claude-fictional-1",
        usage_type="LLM_TOKEN",
        input_tokens=1200,
        output_tokens=340,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=50,
        request_id="call-1",
        occurred_at=OCCURRED,
        conversation_id="conv-1",
        feature="chat.reply",
        event_id=llm_id,
    )
    quantity = billing.record_quantity_usage(
        provider="openai",
        model="whisper-fictional",
        usage_type="AUDIO_SECOND",
        quantity=Decimal("12.500"),
        unit="SECOND",
        request_id="call-2",
        occurred_at=OCCURRED,
        event_id=quantity_id,
    )
    return llm, quantity


def batch_statuses(exchange: Exchange) -> list[tuple[str, str]]:
    assert exchange.status == 200, exchange.body
    body = exchange.json()
    assert set(body) == ENVELOPE_FIELDS
    assert body["success"] is True
    return [(item["event_id"], item["status"]) for item in body["data"]["results"]]


# --- 契约：签名过服务端 ---------------------------------------------------------------


def test_a_request_signed_by_the_client_passes_the_server_check(world: World) -> None:
    config = world.config("signing.db")
    event_id, payload = build_llm_event(
        provider="anthropic",
        model="claude-fictional-1",
        usage_type="LLM_TOKEN",
        input_tokens=1200,
        output_tokens=340,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=50,
        request_id="call-1",
        now=NOW.replace(tzinfo=dt.UTC),
        new_id=new_ulid,
        occurred_at=OCCURRED,
    )
    headers = request_headers(
        config, "POST", SINGLE_PATH, payload, timestamp=epoch(NOW), request_id=new_ulid()
    )

    # 服务端的验签函数独立核一遍：不通过会抛 SignatureRejected。
    verify_signature(
        API_SECRET,
        method="POST",
        path_and_query=SINGLE_PATH,
        timestamp=headers["X-Acuven-Timestamp"],
        request_id=headers["X-Acuven-Request-Id"],
        body=payload,
        signature=headers["X-Acuven-Signature"],
        now=NOW,
    )
    response = world.transport.send("POST", BASE_URL + SINGLE_PATH, headers, payload, 10)

    assert response.status == 202, response.body
    body = json.loads(response.body)
    assert set(body) == ENVELOPE_FIELDS
    assert body["success"] is True
    assert body["data"]["event_id"] == event_id
    assert body["data"]["status"] == "accepted"
    [row] = world.server_events()
    assert (row.event_id, row.status) == (event_id, UsageEventStatus.RECEIVED)
    # 归属由签名凭据决定（载荷不带租户与项目）。
    assert (row.tenant_id, row.project_id) == (world.tenant_pk, world.project_pk)


# --- 契约：载荷过服务端 ---------------------------------------------------------------


def test_both_event_kinds_are_delivered_and_a_second_outbox_gets_already(world: World) -> None:
    first = world.client("first.db")
    llm_id, quantity_id = record_both(first, llm_id=None, quantity_id=None)

    summary = first.deliver_once()

    assert (summary.claimed, summary.delivered, summary.retried, summary.dead) == (2, 2, 0, 0)
    [exchange] = world.transport.to(BATCH_PATH)
    # 客户端时钟冻结，两条事件 created_at 相同，批内先后由 event_id 的随机部分决定，所以不比顺序。
    assert sorted(batch_statuses(exchange)) == sorted(
        [(llm_id, "accepted"), (quantity_id, "accepted")]
    )
    first.close()
    counts = outbox_counts(world.tmp_path / "first.db")
    assert (counts.delivered, counts.pending, counts.sending, counts.dead) == (2, 0, 0, 0)
    rows = world.server_events()
    assert {row.event_id for row in rows} == {llm_id, quantity_id}
    assert {row.status for row in rows} == {UsageEventStatus.RECEIVED}
    assert {(row.tenant_id, row.project_id) for row in rows} == {
        (world.tenant_pk, world.project_pk)
    }

    # 另一个 outbox 文件、同样的 event_id、occurred_at 与字段：服务端按 event_id 幂等。
    second = world.client("second.db")
    assert record_both(second, llm_id=llm_id, quantity_id=quantity_id) == (llm_id, quantity_id)

    again = second.deliver_once()

    assert (again.claimed, again.delivered, again.retried, again.dead) == (2, 2, 0, 0)
    [_, repeat] = world.transport.to(BATCH_PATH)
    statuses = batch_statuses(repeat)
    assert sorted(event_id for event_id, _ in statuses) == sorted((llm_id, quantity_id))
    assert all(status.startswith("already_") for _, status in statuses), statuses
    second.close()
    counts = outbox_counts(world.tmp_path / "second.db")
    assert (counts.delivered, counts.pending, counts.sending, counts.dead) == (2, 0, 0, 0)
    # 服务端仍只有一份。
    assert sorted(row.event_id for row in world.server_events()) == sorted((llm_id, quantity_id))


# --- 契约：有效状态 -------------------------------------------------------------------


def test_reconcile_against_the_server_applies_its_status(world: World) -> None:
    billing = world.client("status.db")

    assert billing.reconcile_status_once() is True

    [exchange] = world.transport.to(EFFECTIVE_STATUS_PATH)
    assert (exchange.method, exchange.status) == ("GET", 200)
    served = exchange.json()["data"]
    applied = billing.current_status()
    assert applied is not None
    assert applied.pop("applied_at") == client_clock()
    # 字段集合与值都与服务端应答一致。
    assert applied == served
    # 空钱包的新租户：SUSPENDED → BLOCK_AI，版本 0。
    assert (served["effective_status"], served["status_version"]) == ("BLOCK_AI", 0)
    assert billing.should_block_ai() is True
    assert billing.health()["last_status_reconcile_ok_at"] == client_clock()


# --- 契约：webhook --------------------------------------------------------------------


def test_a_status_webhook_built_by_the_server_verifies_and_applies(world: World) -> None:
    factory = world.factory
    settings = world.app.state.settings
    admin = make_admin(factory)
    set_status_webhook(
        factory,
        actor=admin,
        customer_id=world.tenant_public_id,
        project_id=world.project_public_id,
        url=HOOK,
        context=CONTEXT,
        now=NOW,
    )
    issued = webhook_signing.issue_secret(
        factory,
        settings,
        actor=admin,
        customer_id=world.tenant_public_id,
        project_id=world.project_public_id,
        context=CONTEXT,
        now=NOW,
    )
    webhook_signing.activate_secret(
        factory,
        actor=admin,
        customer_id=world.tenant_public_id,
        project_id=world.project_public_id,
        key_version=issued.key_version,
        context=CONTEXT,
        now=NOW,
    )
    captured: list[urllib.request.Request] = []

    def capture(request: urllib.request.Request, timeout: float) -> CapturedResponse:
        # 不发网络：只截下服务端构造好的出站请求。
        captured.append(request)
        return CapturedResponse()

    result = status_delivery.dispatch(factory, settings, transport=capture, clock=lambda: NOW)

    assert result.claimed == 1
    [request] = captured
    assert isinstance(request.data, bytes)
    payload = json.loads(request.data)
    billing = world.client(
        "webhook.db", webhook_secrets={issued.key_version: issued.secret.get_secret_value()}
    )
    parts = urlsplit(request.full_url)

    # 照 README 的 FastAPI 路由：raw_path 与 query_string 是字节串，请求体是原始字节。
    applied = billing.handle_status_webhook(
        method=request.get_method(),
        raw_path=parts.path.encode("ascii"),
        query_string=parts.query.encode("ascii"),
        headers=request.header_items(),
        body=request.data,
    )

    assert applied is True
    status = billing.current_status()
    assert status is not None
    assert {name: status[name] for name in payload if name != "event_id"} == {
        name: value for name, value in payload.items() if name != "event_id"
    }
    assert billing.should_block_ai() is (payload["effective_status"] == "BLOCK_AI")
    # webhook 不冒充对账成功。
    assert billing.health()["last_status_reconcile_ok_at"] is None
    # 同一条再推一次：同版本不改，但不是错误。
    again = billing.handle_status_webhook(
        method=request.get_method(),
        raw_path=parts.path.encode("ascii"),
        query_string=parts.query.encode("ascii"),
        headers=request.header_items(),
        body=request.data,
    )
    assert again is False
