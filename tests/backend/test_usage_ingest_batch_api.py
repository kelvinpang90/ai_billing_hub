"""`POST /api/v1/integration/usage-events/batch` (design gate #180 v3 §7, AIH-TASK-030).

Rows of design §7 covered here, through the HTTP endpoint with real signatures on SQLite:

- **正常** — three events: 200, `accepted = 3`, three `RECEIVED` rows;
- **与单条一致** — one list of inputs (valid, duplicate, conflict, every kind of validation
  error, unknown type, scope mismatch) through the single endpoint and through the batch
  endpoint, each on a clean database: the same per-item results and the same rows written;
- **部分成功**, **批内重复**, **中途数据库故障** (also: every item failing; an unexpected 500);
- **条数上限** — 100 / 101 / 0 / 11 with the limit set to 10; the top-level structure;
- **请求体上限** — 1 MiB + 1 byte at the application; one element over 16 KiB;
- **元素不是对象 / 缺 `event_id`**, **认证与重放**, **重发整批**, **同一 `now`**;
- **并发批** (MySQL) — two or more batches sharing an `event_id`: one row, the others
  `already_received`. Needs `BILLING_TEST_DATABASE_URL` pointing at a database that may be wiped;
  it skips without it — **a skip is not a pass**, CI sets it.

The two rows not here: **两层上限一致** is in test_compose.py; **经过 nginx 的 413** is measured
after deployment and written into the task record.

Placeholders only (public repository): uuids, api keys and secrets are zeros with a counter;
usage numbers are fictional; there are no prices, rates or amounts here at all.
"""

from __future__ import annotations

import base64
import datetime as dt
import itertools
import json
import logging
import os
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, delete, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from alembic import command
from app.api import integration_usage
from app.core.config import Settings
from app.core.crypto import encrypt_secret, load_keyring
from app.core.database import create_session_factory
from app.core.nonce import NonceStore
from app.main import create_app
from app.models.ai_catalog import SEED_METER_TYPES, UsageMeterType
from app.models.auth import AuditAction, AuditLog
from app.models.base import Base
from app.models.integration import CredentialStatus, IntegrationCredential
from app.models.tenancy import Project, Tenant
from app.models.usage import ConflictMismatch, UsageEvent, UsageEventConflict, UsageEventStatus
from app.repositories import usage_events
from app.services import usage_ingest
from app.services.auth import RequestContext
from app.services.integration_auth import canonical_request, credential_aad, sign
from app.services.usage_ingest import AuthenticatedCredential, BatchOutcome, ingest_batch

BATCH_PATH = "/api/v1/integration/usage-events/batch"
SINGLE_PATH = "/api/v1/integration/usage-events"
ENVELOPE_FIELDS = {"success", "data", "error", "request_id"}
ERROR_FIELDS = ENVELOPE_FIELDS | {"retryable"}
DATA_FIELDS = {"accepted", "duplicates", "rejected", "results"}
ITEM_FIELDS = {"index", "event_id", "status", "processing_status", "error_code", "retryable"}

NOW = dt.datetime(2026, 10, 5, 8, 0, 0)
SECOND = dt.timedelta(seconds=1)
OCCURRED = "2026-10-05T07:59:30.250000Z"
MIB = 1024 * 1024
KIB = 1024

_REQUEST_NUMBERS = itertools.count(1)


def uuid7(number: int) -> str:
    """A UUIDv7 in its canonical spelling, zeros apart from the version, variant and counter."""
    return f"00000000-0000-7000-8000-{number:012d}"


def public_id(number: int) -> str:
    return f"00000000-0000-0000-0000-{number:012d}"


def api_key(number: int) -> str:
    return f"ak_{number:032d}"


def secret(number: int) -> str:
    return f"sk_{number:064d}"


def epoch(moment: dt.datetime) -> int:
    return int(moment.replace(tzinfo=dt.UTC).timestamp())


# --- fixtures -------------------------------------------------------------------------


class Clock:
    """The endpoint's `utc_now`, frozen; counts how often the endpoint asks."""

    def __init__(self, moment: dt.datetime) -> None:
        self.now = moment
        self.calls = 0

    def __call__(self) -> dt.datetime:
        self.calls += 1
        return self.now


class FakeRedis:
    """redis-py's `SET … NX PXAT`, in memory."""

    def __init__(self) -> None:
        self.keys: dict[str, int] = {}

    def set(self, name: str, value: int, *, nx: bool, pxat: int) -> bool | None:
        if nx and name in self.keys:
            return None
        self.keys[name] = pxat
        return True


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    frozen = Clock(NOW)
    monkeypatch.setattr(integration_usage, "utc_now", frozen)
    return frozen


@pytest.fixture
def build_app(tmp_path: Path, clock: Clock) -> Iterator[Callable[[], FastAPI]]:
    """Each call: a new application on a new, empty in-memory database (seeded meter types)."""
    engines: list[Engine] = []

    def build() -> FastAPI:
        master = tmp_path / f"master-{len(engines)}.key"
        master.write_text(f"1:{base64.b64encode(bytes(32)).decode()}\n", encoding="utf-8")
        settings = Settings(database_url="", master_key_file=str(master), redis_url="")
        application = create_app(settings)
        # StaticPool: the endpoint runs in a thread pool; the in-memory database is one connection.
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        engines.append(engine)
        Base.metadata.create_all(engine)
        application.state.engine = engine
        application.state.session_factory = create_session_factory(engine)
        application.state.nonce_store = NonceStore(FakeRedis())
        seed_meter_types(application.state.session_factory)
        return application

    yield build
    for engine in engines:
        engine.dispose()


@pytest.fixture
def app(build_app: Callable[[], FastAPI]) -> FastAPI:
    """Built in the setup phase: `create_app` reinstalls the root log handlers."""
    return build_app()


@pytest.fixture
def factory(app: FastAPI) -> sessionmaker[Session]:
    return app.state.session_factory


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


@dataclass(frozen=True)
class Credential:
    """One signing version as the integrated backend holds it, plus its owner's public ids."""

    api_key: str
    key_version: int
    secret: str
    tenant_public_id: str
    project_public_id: str


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


def make_credential(app: FastAPI, number: int) -> Credential:
    """A new tenant and project, and version 1 of api key `number`, encrypted like the admin."""
    factory = app.state.session_factory
    keyring = load_keyring(app.state.settings)
    aad = credential_aad(api_key(number), 1)
    encrypted, master_version = encrypt_secret(keyring, secret(number), associated_data=aad)
    with factory() as session:
        tenant = Tenant(
            public_id=public_id(number),
            company_name=f"Fictional {number} Sdn Bhd",
            email=f"ops-{number}@usage-ingest-batch-test.example.com",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(tenant)
        session.flush()
        project = Project(
            public_id=public_id(number + 100),
            tenant_id=tenant.id,
            name="Chatbot",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(project)
        session.flush()
        session.add(
            IntegrationCredential(
                tenant_id=tenant.id,
                project_id=project.id,
                public_api_key=api_key(number),
                key_version=1,
                encrypted_secret=encrypted,
                encryption_key_version=master_version,
                status=CredentialStatus.ACTIVE,
                valid_from=NOW - dt.timedelta(days=1),
                created_at=NOW - dt.timedelta(days=1),
            )
        )
        session.commit()
    return Credential(
        api_key=api_key(number),
        key_version=1,
        secret=secret(number),
        tenant_public_id=public_id(number),
        project_public_id=public_id(number + 100),
    )


@pytest.fixture
def credential(app: FastAPI) -> Credential:
    return make_credential(app, 1)


# --- requests -------------------------------------------------------------------------


def llm_event(number: int = 1, **changes: object) -> dict[str, object]:
    event: dict[str, object] = {
        "schema_version": "1.0",
        "event_id": uuid7(number),
        "request_id": f"call-{number}",
        "conversation_id": "conv-1",
        "provider": "anthropic",
        "model": "claude-fictional-1",
        "usage_type": "LLM_TOKEN",
        "input_tokens": 1200,
        "output_tokens": 340,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 50,
        "occurred_at": OCCURRED,
    }
    event.update(changes)
    return event


def quantity_event(number: int, **changes: object) -> dict[str, object]:
    event: dict[str, object] = {
        "schema_version": "1.0",
        "event_id": uuid7(number),
        "request_id": f"call-{number}",
        "provider": "openai",
        "model": "whisper-fictional",
        "usage_type": "AUDIO_SECOND",
        "quantity": "12.5",
        "unit": "SECOND",
        "occurred_at": OCCURRED,
    }
    event.update(changes)
    return event


def image_event(number: int, **changes: object) -> dict[str, object]:
    """IMAGE_GENERATION: a QUANTITY type whose quantity is an integer."""
    event = quantity_event(number, usage_type="IMAGE_GENERATION", quantity="2", unit="IMAGE")
    event.update(changes)
    return event


def signed_headers(
    credential: Credential,
    body: bytes,
    *,
    path: str = BATCH_PATH,
    timestamp: int | None = None,
    request_id: str | None = None,
    signing_secret: str | None = None,
) -> dict[str, str]:
    request_id = request_id or f"try-{next(_REQUEST_NUMBERS)}"
    timestamp = epoch(NOW) if timestamp is None else timestamp
    canonical = canonical_request("POST", path, str(timestamp), request_id, body)
    return {
        "X-Acuven-Api-Key": credential.api_key,
        "X-Acuven-Key-Version": str(credential.key_version),
        "X-Acuven-Timestamp": str(timestamp),
        "X-Acuven-Request-Id": request_id,
        "X-Acuven-Signature": sign(signing_secret or credential.secret, canonical),
        "Content-Type": "application/json",
    }


def encode(value: object) -> bytes:
    return json.dumps(value).encode("utf-8")


def post_batch(client: TestClient, credential: Credential, events: object, **signing: Any):
    """Sign and send `{"events": events}` (or raw bytes as they are): a new attempt."""
    body = events if isinstance(events, bytes) else encode({"events": events})
    headers = signed_headers(credential, body, **signing)
    return client.post(BATCH_PATH, content=body, headers=headers)


def post_single(client: TestClient, credential: Credential, event: object):
    body = encode(event)
    headers = signed_headers(credential, body, path=SINGLE_PATH)
    return client.post(SINGLE_PATH, content=body, headers=headers)


def batch_data(response) -> dict[str, Any]:
    """The 200's `data`, with the shape of design §2「响应」checked on the way."""
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == ENVELOPE_FIELDS
    assert body["success"] is True
    assert body["error"] is None
    data = body["data"]
    assert set(data) == DATA_FIELDS
    results = data["results"]
    assert [item["index"] for item in results] == list(range(len(results)))
    for item in results:
        assert set(item) == ITEM_FIELDS
    assert data["accepted"] + data["duplicates"] + data["rejected"] == len(results)
    return data


def outcomes(data: dict[str, Any]) -> list[tuple[str, str | None, bool]]:
    return [(item["status"], item["error_code"], item["retryable"]) for item in data["results"]]


ACCEPTED = ("accepted", None, False)
RECEIVED_AGAIN = ("already_received", None, False)


def assert_error(response, status: int, code: str, retryable: bool) -> None:
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == ERROR_FIELDS
    assert body["success"] is False
    assert body["data"] is None
    assert body["error"]["code"] == code
    assert body["retryable"] is retryable


def events(factory: sessionmaker[Session]) -> list[UsageEvent]:
    with factory() as session:
        return list(session.execute(select(UsageEvent).order_by(UsageEvent.id)).scalars())


def conflicts(factory: sessionmaker[Session]) -> list[UsageEventConflict]:
    with factory() as session:
        statement = select(UsageEventConflict).order_by(UsageEventConflict.id)
        return list(session.execute(statement).scalars())


def audits(factory: sessionmaker[Session], action: AuditAction | None = None) -> list[AuditLog]:
    with factory() as session:
        statement = select(AuditLog).order_by(AuditLog.id)
        if action is not None:
            statement = statement.where(AuditLog.action == action)
        return list(session.execute(statement).scalars())


def last_used(factory: sessionmaker[Session]) -> dt.datetime | None:
    with factory() as session:
        return session.execute(select(IntegrationCredential.last_used_at)).scalar_one()


# --- 正常 ------------------------------------------------------------------------------


def test_three_valid_events_are_accepted(client, factory, credential) -> None:
    batch = [llm_event(1), quantity_event(2), llm_event(3)]

    response = post_batch(client, credential, batch)

    data = batch_data(response)
    assert (data["accepted"], data["duplicates"], data["rejected"]) == (3, 0, 0)
    assert data["results"] == [
        {
            "index": index,
            "event_id": uuid7(number),
            "status": "accepted",
            "processing_status": "RECEIVED",
            "error_code": None,
            "retryable": False,
        }
        for index, number in enumerate((1, 2, 3))
    ]
    # A success carries no top-level `retryable` (the single endpoint's rule).
    assert "retryable" not in response.json()
    rows = events(factory)
    assert [row.event_id for row in rows] == [uuid7(1), uuid7(2), uuid7(3)]
    assert {row.status for row in rows} == {UsageEventStatus.RECEIVED}


# --- 与单条一致 ------------------------------------------------------------------------

# Every input goes through both endpoints in this order; the order matters for the duplicate
# and the conflict (they follow the first event). (id, input)
SAME_AS_SINGLE: list[tuple[str, object]] = [
    ("valid", llm_event(1)),
    ("duplicate", llm_event(1)),
    ("conflict", llm_event(1, output_tokens=341)),
    ("valid-quantity", quantity_event(2)),
    ("equivalent-spelling", quantity_event(2, quantity="12.50")),
    ("event-id-not-uuid7", llm_event(3, event_id="00000000-0000-4000-8000-000000000003")),
    ("event-id-missing", {k: v for k, v in llm_event(4).items() if k != "event_id"}),
    ("unknown-field", llm_event(5, note="fictional")),
    ("prompt", llm_event(6, prompt="fictional prompt text")),
    ("customer-cost", llm_event(7, customer_cost="0.00000001")),
    ("tokens-string", llm_event(8, output_tokens="5")),
    ("tokens-missing", {k: v for k, v in llm_event(9).items() if k != "input_tokens"}),
    ("llm-with-quantity", llm_event(10, quantity="1")),
    ("quantity-number", quantity_event(11, quantity=12.5)),
    ("unit-other", quantity_event(12, unit="MINUTE")),
    ("integer-kind-fraction", image_event(13, quantity="3.0")),
    ("no-time-zone", llm_event(14, occurred_at="2026-10-05T07:00:00")),
    ("in-the-future", llm_event(15, occurred_at="2026-10-05T08:05:01Z")),
    ("schema-version", llm_event(16, schema_version="2.0")),
    ("unknown-usage-type", quantity_event(17, usage_type="TRANSLATED_LINE", unit="LINE")),
    ("scope-mismatch", llm_event(18, tenant_id=public_id(77))),
    ("scope-matches", llm_event(19, tenant_id=public_id(1), project_id=public_id(101))),
    ("not-an-object", 7),
    ("empty-object", {}),
    ("string", "text"),
    ("null", None),
    ("too-large", llm_event(20, note="x" * (16 * KIB))),
    ("valid-last", llm_event(21)),
]


def _rows_without_ids(rows: list[Any]) -> list[dict[str, object]]:
    """Every column but the surrogate keys and the generated public ids."""
    return [
        {
            column.key: getattr(row, column.key)
            for column in type(row).__table__.columns
            if column.key not in {"id", "public_id", "entity_id"}
        }
        for row in rows
    ]


def _single_result(response) -> tuple[str, str | None, str | None, bool]:
    body = response.json()
    if response.status_code in {200, 202}:
        return (body["data"]["status"], body["data"]["processing_status"], None, False)
    return ("rejected", None, body["error"]["code"], body["retryable"])


def test_every_item_is_what_the_single_endpoint_answers(build_app) -> None:
    """Design §1 完成标准 1 / §7: status, `error_code`, `retryable` and the rows written."""
    single_app, batch_app = build_app(), build_app()
    single_credential = make_credential(single_app, 1)
    batch_credential = make_credential(batch_app, 1)
    inputs = [value for _, value in SAME_AS_SINGLE]
    single_client = TestClient(single_app, raise_server_exceptions=False)
    batch_client = TestClient(batch_app, raise_server_exceptions=False)

    single = [
        _single_result(post_single(single_client, single_credential, value)) for value in inputs
    ]
    data = batch_data(post_batch(batch_client, batch_credential, inputs))

    batched = [
        (item["status"], item["processing_status"], item["error_code"], item["retryable"])
        for item in data["results"]
    ]

    for (name, _), alone, in_batch in zip(SAME_AS_SINGLE, single, batched, strict=True):
        assert in_batch == alone, name
    # The list really exercises every path (a typo above must not turn it into 28 acceptances).
    codes = {code for _, _, code, _ in single}
    assert codes == {
        None,
        "IDEMPOTENCY_CONFLICT",
        "VALIDATION_ERROR",
        "OCCURRED_AT_IN_FUTURE",
        "UNSUPPORTED_SCHEMA_VERSION",
        "UNKNOWN_USAGE_TYPE",
        "CREDENTIAL_SCOPE_MISMATCH",
        "PAYLOAD_TOO_LARGE",
    }
    assert {status for status, _, _, _ in single} >= {"accepted", "already_received", "rejected"}

    single_factory = single_app.state.session_factory
    batch_factory = batch_app.state.session_factory
    for table in (events, conflicts, audits):
        assert _rows_without_ids(table(batch_factory)) == _rows_without_ids(table(single_factory))
    assert [row.event_id for row in events(batch_factory)] == [uuid7(n) for n in (1, 2, 19, 21)]
    assert len(conflicts(batch_factory)) == 1
    assert len(audits(batch_factory)) == 2


# --- 部分成功与批内重复 ----------------------------------------------------------------


def test_valid_conflicting_and_invalid_events_in_one_batch(client, factory, credential) -> None:
    """INV-1: one bad element never rejects the batch; the good ones are written."""
    assert batch_data(post_batch(client, credential, [llm_event(1)]))["accepted"] == 1

    batch = [
        llm_event(2),
        llm_event(1, output_tokens=341),
        llm_event(3, provider="Not-Lower-Case"),
        llm_event(4),
    ]
    data = batch_data(post_batch(client, credential, batch))

    assert (data["accepted"], data["duplicates"], data["rejected"]) == (2, 0, 2)
    assert outcomes(data) == [
        ACCEPTED,
        ("rejected", "IDEMPOTENCY_CONFLICT", False),
        ("rejected", "VALIDATION_ERROR", False),
        ACCEPTED,
    ]
    assert [item["event_id"] for item in data["results"]] == [uuid7(n) for n in (2, 1, 3, 4)]
    assert [item["processing_status"] for item in data["results"]] == [
        "RECEIVED",
        None,
        None,
        "RECEIVED",
    ]
    assert [row.event_id for row in events(factory)] == [uuid7(1), uuid7(2), uuid7(4)]
    [conflict] = conflicts(factory)
    assert conflict.mismatch is ConflictMismatch.FINGERPRINT


def test_the_same_event_twice_in_a_batch_is_a_duplicate(client, factory, credential) -> None:
    """Not deduplicated beforehand (design §2): the second goes the single endpoint's way."""
    data = batch_data(post_batch(client, credential, [llm_event(1), llm_event(1)]))

    assert outcomes(data) == [ACCEPTED, RECEIVED_AGAIN]
    assert (data["accepted"], data["duplicates"], data["rejected"]) == (1, 1, 0)
    assert data["results"][1]["processing_status"] == "RECEIVED"
    assert len(events(factory)) == 1
    assert conflicts(factory) == []


def test_a_changed_event_twice_in_a_batch_is_a_conflict(client, factory, credential) -> None:
    batch = [llm_event(1), llm_event(1, cache_read_input_tokens=51)]

    data = batch_data(post_batch(client, credential, batch))

    assert outcomes(data) == [ACCEPTED, ("rejected", "IDEMPOTENCY_CONFLICT", False)]
    [row] = events(factory)
    assert row.cache_read_input_tokens == 50
    [conflict] = conflicts(factory)
    assert conflict.event_id == uuid7(1)
    assert len(audits(factory, AuditAction.USAGE_EVENT_IDEMPOTENCY_CONFLICT)) == 1


def test_a_conflict_reveals_nothing_of_the_original(app, client, factory, credential) -> None:
    """Design §6: the item carries only the requester's own event_id and the code."""
    assert batch_data(post_batch(client, credential, [llm_event(1)]))["accepted"] == 1
    stranger = make_credential(app, 2)

    response = post_batch(client, stranger, [llm_event(1)])

    data = batch_data(response)
    assert outcomes(data) == [("rejected", "IDEMPOTENCY_CONFLICT", False)]
    [original] = events(factory)
    for owners_detail in (original.public_id, credential.tenant_public_id, credential.api_key):
        assert owners_detail not in response.text


# --- 数据库故障 ------------------------------------------------------------------------


def _outage_on(calls_that_fail: Callable[[int], bool], monkeypatch) -> list[str]:
    """`find_meter_type` raises a connection-level error on the chosen calls (1-based)."""
    original = usage_events.find_meter_type
    calls: list[str] = []

    def flaky(session: Session, code: str):
        calls.append(code)
        if calls_that_fail(len(calls)):
            raise OperationalError("SELECT", {}, Exception("server has gone away"))
        return original(session, code)

    monkeypatch.setattr(usage_events, "find_meter_type", flaky)
    return calls


def test_a_database_error_on_the_second_item_spares_the_others(
    client,
    factory,
    credential,
    monkeypatch,
) -> None:
    """Design §2: the first stays committed, the third is still tried (no giving up early)."""
    calls = _outage_on(lambda number: number == 2, monkeypatch)

    data = batch_data(post_batch(client, credential, [llm_event(1), llm_event(2), llm_event(3)]))

    assert outcomes(data) == [ACCEPTED, ("rejected", "SERVICE_UNAVAILABLE", True), ACCEPTED]
    assert data["results"][1]["event_id"] == uuid7(2)
    assert len(calls) == 3
    assert [row.event_id for row in events(factory)] == [uuid7(1), uuid7(3)]


def test_a_database_down_for_every_item_is_still_a_200(client, factory, credential, monkeypatch):
    calls = _outage_on(lambda number: True, monkeypatch)

    data = batch_data(post_batch(client, credential, [llm_event(1), llm_event(2)]))

    assert outcomes(data) == [("rejected", "SERVICE_UNAVAILABLE", True)] * 2
    assert (data["accepted"], data["duplicates"], data["rejected"]) == (0, 0, 2)
    assert len(calls) == 2
    assert events(factory) == []


def test_an_unexpected_error_is_500_and_keeps_what_was_committed(
    client,
    factory,
    credential,
    monkeypatch,
) -> None:
    """Design §5: outside the error table → 500, retryable; resending the batch is safe."""
    original = usage_ingest.ingest_one
    calls: list[object] = []

    def explode_on_the_second(*args: Any, **kwargs: Any):
        calls.append(args)
        if len(calls) == 2:
            raise RuntimeError("unexpected")
        return original(*args, **kwargs)

    monkeypatch.setattr(usage_ingest, "ingest_one", explode_on_the_second)
    response = post_batch(client, credential, [llm_event(1), llm_event(2), llm_event(3)])
    # Not `undo()`: that would also unfreeze the endpoint's clock.
    monkeypatch.setattr(usage_ingest, "ingest_one", original)

    assert_error(response, 500, "INTERNAL_ERROR", True)
    assert "unexpected" not in response.text
    assert [row.event_id for row in events(factory)] == [uuid7(1)]

    resent = batch_data(post_batch(client, credential, [llm_event(1), llm_event(2), llm_event(3)]))
    assert outcomes(resent) == [RECEIVED_AGAIN, ACCEPTED, ACCEPTED]


# --- 条数上限与顶层结构 ----------------------------------------------------------------


def test_one_hundred_events_are_accepted(client, factory, credential) -> None:
    batch = [llm_event(number) for number in range(1, 101)]

    data = batch_data(post_batch(client, credential, batch))

    assert data["accepted"] == 100
    assert len(events(factory)) == 100


def test_one_hundred_and_one_events_are_refused_whole(client, factory, credential) -> None:
    batch = [llm_event(number) for number in range(1, 102)]

    response = post_batch(client, credential, batch)

    assert_error(response, 422, "BATCH_TOO_LARGE", False)
    assert events(factory) == []


def test_the_limit_follows_the_setting(app, client, factory, credential) -> None:
    app.state.settings = app.state.settings.model_copy(update={"ingest_batch_max": 10})

    eleven = post_batch(client, credential, [llm_event(number) for number in range(1, 12)])
    ten = post_batch(client, credential, [llm_event(number) for number in range(1, 11)])

    assert_error(eleven, 422, "BATCH_TOO_LARGE", False)
    assert batch_data(ten)["accepted"] == 10
    assert len(events(factory)) == 10


@pytest.mark.parametrize(
    "body",
    [
        encode({"events": []}),
        encode({}),
        encode({"events": {"0": llm_event(1)}}),
        encode({"events": [llm_event(1)], "note": "fictional"}),
        encode([llm_event(1)]),
        encode(llm_event(1)),
        b"not json",
        b'{"events": [{"input_tokens": NaN}]}',
        b"\xff\xfe",
    ],
    ids=[
        "empty",
        "no-events",
        "events-not-an-array",
        "extra-top-level-field",
        "bare-array",
        "a-single-event",
        "not-json",
        "nan",
        "not-utf8",
    ],
)
def test_a_malformed_top_level_is_422_and_writes_nothing(
    client,
    factory,
    credential,
    body: bytes,
) -> None:
    response = post_batch(client, credential, body)

    assert_error(response, 422, "VALIDATION_ERROR", False)
    assert events(factory) == []


# --- 请求体上限 ------------------------------------------------------------------------


def test_one_byte_over_1_mib_is_413_before_anything_else(client, factory, credential) -> None:
    """Step ①: same envelope as nginx's (`PAYLOAD_TOO_LARGE`, not retryable), from the app."""
    compact = encode({"events": [llm_event(1)]})
    body = compact + b" " * (MIB + 1 - len(compact))
    assert len(body) == MIB + 1

    response = post_batch(client, credential, body)

    assert_error(response, 413, "PAYLOAD_TOO_LARGE", False)
    assert response.json()["error"]["message"] == "request body too large"
    # Rejected by the application: unlike nginx's 413 it carries a request id.
    assert response.json()["request_id"] is not None
    assert events(factory) == []


def test_exactly_1_mib_is_read(client, credential) -> None:
    compact = encode({"events": [llm_event(1)]})
    body = compact + b" " * (MIB - len(compact))
    assert len(body) == MIB

    assert batch_data(post_batch(client, credential, body))["accepted"] == 1


def test_the_body_limit_follows_the_setting(app, client, credential) -> None:
    app.state.settings = app.state.settings.model_copy(update={"ingest_batch_max_bytes": 64 * KIB})
    compact = encode({"events": [llm_event(1)]})

    response = post_batch(client, credential, compact + b" " * (64 * KIB + 1 - len(compact)))

    assert_error(response, 413, "PAYLOAD_TOO_LARGE", False)


def test_an_element_over_16_kib_is_rejected_alone(client, factory, credential) -> None:
    """Measured re-serialised, before the element's own checks (the single endpoint's ①)."""
    batch = [llm_event(1), llm_event(2, note="x" * (16 * KIB)), llm_event(3)]

    data = batch_data(post_batch(client, credential, batch))

    assert outcomes(data) == [ACCEPTED, ("rejected", "PAYLOAD_TOO_LARGE", False), ACCEPTED]
    assert data["results"][1]["event_id"] == uuid7(2)
    assert [row.event_id for row in events(factory)] == [uuid7(1), uuid7(3)]


def test_whitespace_inside_an_element_does_not_count(client, credential) -> None:
    """The element's length is its compact re-serialisation, not the bytes it was sent as."""
    element = json.dumps(llm_event(1), separators=(",", ":"))
    padded = "{" + " " * (17 * KIB) + element[1:]
    body = f'{{"events":[{padded}]}}'.encode()

    assert batch_data(post_batch(client, credential, body))["accepted"] == 1


# --- 元素不是对象 / 缺 event_id --------------------------------------------------------


def test_elements_without_an_event_id_are_answered_by_index(client, factory, credential) -> None:
    batch: list[object] = [
        7,
        {},
        "text",
        None,
        [llm_event(1)],
        {"event_id": 5},
        llm_event(2, event_id="1"),
        llm_event(3),
    ]

    data = batch_data(post_batch(client, credential, batch))

    validation = ("rejected", "VALIDATION_ERROR", False)
    assert outcomes(data) == [validation] * 7 + [ACCEPTED]
    assert [item["event_id"] for item in data["results"]] == [None] * 6 + ["1", uuid7(3)]
    assert all(item["processing_status"] is None for item in data["results"][:7])
    assert [row.event_id for row in events(factory)] == [uuid7(3)]


# --- 认证、重放与重发整批 --------------------------------------------------------------


def test_a_wrong_signature_is_401_and_writes_nothing(client, factory, credential) -> None:
    response = post_batch(client, credential, [llm_event(1)], signing_secret=secret(99))

    assert_error(response, 401, "INTEGRATION_AUTH_FAILED", False)
    assert events(factory) == []
    assert last_used(factory) is None


def test_a_signature_for_the_single_endpoint_is_401_here(client, factory, credential) -> None:
    """The signature covers the path: a body signed for `/usage-events` is not a batch's."""
    body = encode({"events": [llm_event(1)]})
    headers = signed_headers(credential, body, path=SINGLE_PATH)

    response = client.post(BATCH_PATH, content=body, headers=headers)

    assert_error(response, 401, "INTEGRATION_AUTH_FAILED", False)
    assert events(factory) == []


def test_a_replayed_batch_is_401_and_writes_nothing_twice(client, factory, credential) -> None:
    """One `X-Acuven-Request-Id` for the whole batch: the same request again is a replay."""
    body = encode({"events": [llm_event(1), llm_event(2)]})
    headers = signed_headers(credential, body, request_id="batch-replay-1")

    first = client.post(BATCH_PATH, content=body, headers=headers)
    second = client.post(BATCH_PATH, content=body, headers=headers)

    assert batch_data(first)["accepted"] == 2
    assert_error(second, 401, "INTEGRATION_AUTH_FAILED", False)
    assert len(events(factory)) == 2


def test_resending_a_batch_with_a_new_request_id_is_safe(client, factory, credential) -> None:
    batch = [llm_event(1), quantity_event(2), llm_event(3)]
    assert batch_data(post_batch(client, credential, batch))["accepted"] == 3

    data = batch_data(post_batch(client, credential, batch))

    assert outcomes(data) == [RECEIVED_AGAIN] * 3
    assert (data["accepted"], data["duplicates"], data["rejected"]) == (0, 3, 0)
    assert len(events(factory)) == 3
    assert conflicts(factory) == []


def test_last_used_at_is_written_once_for_a_processed_batch(client, factory, credential) -> None:
    """Even when every item is rejected: the batch itself authenticated and was answered."""
    data = batch_data(post_batch(client, credential, [7, {}]))
    assert data["rejected"] == 2

    assert last_used(factory) == NOW


# --- 同一 now --------------------------------------------------------------------------


def test_the_whole_batch_shares_one_now(client, factory, credential, clock, monkeypatch) -> None:
    """Design §2「时间语义」: taken once by the request layer and handed to every item."""
    original = usage_ingest.ingest_one
    seen: list[dt.datetime] = []

    def spy(*args: Any, now: dt.datetime, **kwargs: Any):
        seen.append(now)
        return original(*args, now=now, **kwargs)

    monkeypatch.setattr(usage_ingest, "ingest_one", spy)
    clock.now = NOW + dt.timedelta(microseconds=750_000)
    at_the_bound = "2026-10-05T08:05:00.750000Z"
    past_the_bound = "2026-10-05T08:05:00.750001Z"
    batch = [
        llm_event(1, occurred_at=at_the_bound),
        llm_event(2, occurred_at=past_the_bound),
        llm_event(3, occurred_at=at_the_bound),
        llm_event(4, occurred_at=past_the_bound),
        llm_event(5),
    ]

    data = batch_data(post_batch(client, credential, batch))

    future = ("rejected", "OCCURRED_AT_IN_FUTURE", False)
    assert outcomes(data) == [ACCEPTED, future, ACCEPTED, future, ACCEPTED]
    assert clock.calls == 1
    assert seen == [clock.now] * 5
    assert {row.received_at for row in events(factory)} == {NOW}


# --- 日志 ------------------------------------------------------------------------------


def test_the_log_has_the_counts_and_never_the_body(
    client,
    credential,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Design §6: the batch body is not logged; only the number of events and the summary."""
    batch = [llm_event(1), llm_event(1), llm_event(2, provider="Not-Lower-Case")]

    with caplog.at_level(logging.INFO, logger="app.services.usage_ingest"):
        batch_data(post_batch(client, credential, batch))

    [summary] = [r for r in caplog.records if r.getMessage() == "Usage event batch processed"]
    counts = (summary.events, summary.accepted, summary.duplicates, summary.rejected)
    assert counts == (3, 1, 1, 1)
    for part_of_the_body in ("call-1", "claude-fictional-1", "Not-Lower-Case"):
        assert part_of_the_body not in caplog.text
    assert credential.secret not in caplog.text


# --- 并发批 (MySQL) --------------------------------------------------------------------

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")
MYSQL_EMAIL_DOMAIN = "@usage-ingest-batch-test.example.com"
CONTEXT = RequestContext(ip_address="203.0.113.9", user_agent="usage-ingest-batch-test")


def mysql_public_id(number: int) -> str:
    # "0030" marks this task's rows in the shared MySQL database; the rest is zeros.
    return f"00000000-0000-0000-0030-{number:012d}"


def _clean(engine: Engine) -> None:
    """⚠️ The database is shared: wipe the usage tables and this file's tenants, before and after.

    The conflict and audit tables refuse DELETE (triggers), so TRUNCATE with foreign key checks
    off — as test_usage_ingest_service.py does.
    """
    with engine.begin() as connection:
        connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 0")
        try:
            connection.exec_driver_sql("TRUNCATE TABLE usage_event_conflicts")
            connection.exec_driver_sql("TRUNCATE TABLE usage_events")
        finally:
            connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 1")
    with engine.begin() as connection:
        connection.exec_driver_sql("TRUNCATE TABLE audit_logs")
    with engine.begin() as connection:
        ours = select(Tenant.id).where(Tenant.email.like(f"%{MYSQL_EMAIL_DOMAIN}"))
        connection.execute(
            delete(IntegrationCredential).where(IntegrationCredential.tenant_id.in_(ours))
        )
        connection.execute(delete(Project).where(Project.tenant_id.in_(ours)))
        connection.execute(delete(Tenant).where(Tenant.email.like(f"%{MYSQL_EMAIL_DOMAIN}")))


@pytest.fixture
def mysql_factory(monkeypatch: pytest.MonkeyPatch) -> Iterator[sessionmaker[Session]]:
    if not TEST_DATABASE_URL:
        pytest.skip("BILLING_TEST_DATABASE_URL is not set; concurrent batches need MySQL")
    from app.core.config import get_settings

    # Tables through alembic: the unique key on event_id is migration 0017's.
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


def make_mysql_credential(factory: sessionmaker[Session], number: int) -> AuthenticatedCredential:
    """A tenant, its project and version 1 of api key `number`, committed.

    The ciphertext is a placeholder: the batches are run past the request layer.
    """
    with factory() as session:
        tenant = Tenant(
            public_id=mysql_public_id(number),
            company_name=f"Fictional {number} Sdn Bhd",
            email=f"tenant-{number}{MYSQL_EMAIL_DOMAIN}",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(tenant)
        session.flush()
        project = Project(
            public_id=mysql_public_id(number + 100),
            tenant_id=tenant.id,
            name="Chatbot",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(project)
        session.flush()
        row = IntegrationCredential(
            tenant_id=tenant.id,
            project_id=project.id,
            public_api_key=api_key(number),
            key_version=1,
            encrypted_secret="not-a-real-ciphertext",
            encryption_key_version=1,
            status=CredentialStatus.ACTIVE,
            valid_from=NOW,
            created_at=NOW,
        )
        session.add(row)
        session.commit()
        return AuthenticatedCredential(
            id=row.id,
            tenant_id=tenant.id,
            project_id=project.id,
            api_key=api_key(number),
            key_version=1,
        )


def test_concurrent_batches_sharing_an_event_store_it_once(mysql_factory) -> None:
    """Design §4: the unique key decides; every other batch gets `already_received` for it."""
    credential = make_mysql_credential(mysql_factory, 300)
    shared = llm_event(300)
    batches = [[shared, llm_event(301 + number)] for number in range(4)]
    barrier = threading.Barrier(len(batches))

    def run(batch: list[object]) -> BatchOutcome:
        barrier.wait()
        return ingest_batch(mysql_factory, credential, batch, context=CONTEXT, now=NOW)

    with ThreadPoolExecutor(max_workers=len(batches)) as pool:
        results = list(pool.map(run, batches))

    shared_statuses = sorted(outcome.items[0].status for outcome in results)
    assert shared_statuses == ["accepted"] + ["already_received"] * 3
    assert [outcome.items[1].status for outcome in results] == ["accepted"] * 4
    with mysql_factory() as session:
        statement = select(func.count()).select_from(UsageEvent)
        shared_rows = statement.where(UsageEvent.event_id == uuid7(300))
        assert session.execute(shared_rows).scalar_one() == 1
        assert session.execute(statement).scalar_one() == 5
        conflict_count = select(func.count()).select_from(UsageEventConflict)
        assert session.execute(conflict_count).scalar_one() == 0
