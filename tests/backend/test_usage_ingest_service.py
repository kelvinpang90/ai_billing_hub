"""Usage ingestion below the endpoint (design gate #176 v8 §7, AIH-TASK-029).

Rows of design §7 covered here (the HTTP contract is in test_usage_ingest_api.py):

- **nonce 过期时刻** (unit) — `SET nonce:{api_key}:{Request-Id} 1 NX PXAT` at
  (timestamp + 300) × 1000; a second claim is a replay; a missing Redis raises;
- **解密缓存** (unit) — one decryption per credential row; a new ciphertext (rotation, re-wrap)
  decrypts again; the TTL and the LRU bound; a failed decryption is not cached;
- **规范化与指纹** (unit) — the canonical object, pinned; equivalent spellings agree;
- **事务中途失败** (SQLite and MySQL) — the audit of a conflict raises: the conflict row
  is rolled back with it, and the retry records both;
- **并发重复与并发冲突** (MySQL) — one row; the others are duplicates, or conflicts
  recorded once;
- **冲突表只追加**, **数据库约束**, **必需列非空**, **凭据与归属一致**,
  **形态、单位、数量类型与计量类型一致**, **目录引用列** (MySQL) — direct inserts, refused by
  the database itself.

The MySQL half needs `BILLING_TEST_DATABASE_URL` pointing at a database that may be wiped; it
skips without it — **a skip is not a pass**, CI sets it. Placeholders only: uuids, api keys and
fingerprints are zeros with a counter; usage numbers are fictional; there are no amounts.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import itertools
import os
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from functools import partial

import pytest
import redis
from alembic.config import Config
from sqlalchemy import Engine, create_engine, delete, func, insert, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.core.config import Settings
from app.core.crypto import DecryptionFailed, MasterKeyring, encrypt_secret
from app.core.database import create_session_factory
from app.core.nonce import NonceStore, NonceStoreUnavailable, expires_at_ms, nonce_key
from app.models.ai_catalog import (
    SEED_METER_TYPES,
    PayloadShape,
    QuantityKind,
    UsageMeterType,
)
from app.models.auth import AuditAction, AuditLog
from app.models.base import Base
from app.models.integration import CredentialStatus, IntegrationCredential
from app.models.tenancy import Project, Tenant
from app.models.usage import ConflictMismatch, UsageEvent, UsageEventConflict
from app.services import usage_ingest
from app.services.auth import RequestContext
from app.services.integration_auth import SecretCache, credential_aad
from app.services.usage_ingest import (
    AuthenticatedCredential,
    IdempotencyConflict,
    IngestOutcome,
    ingest_one,
)

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")
TEST_EMAIL_DOMAIN = "@usage-ingest-test.example.com"
CONTEXT = RequestContext(ip_address="203.0.113.9", user_agent="usage-ingest-test")

NOW = dt.datetime(2026, 10, 5, 8, 0, 0)
OCCURRED = "2026-10-05T07:59:30.250000Z"
ZERO_FINGERPRINT = "0" * 64
# A placeholder master key (all zeros) and the keyring around it.
KEYRING = MasterKeyring(keys={1: bytes(32)}, active_version=1)

# MySQL error numbers.
_ER_BAD_NULL = 1048
_ER_DUP_ENTRY = 1062
_ER_NO_REFERENCED_ROW = 1452
_ER_SIGNAL_EXCEPTION = 1644
_ER_CHECK_CONSTRAINT_VIOLATED = 3819

_NUMBERS = itertools.count(1)


def uuid7(number: int) -> str:
    return f"00000000-0000-7000-8000-{number:012d}"


def public_id(number: int) -> str:
    # "0029" marks this task's rows in the shared MySQL database; the rest is zeros.
    return f"00000000-0000-0000-0029-{number:012d}"


def api_key(number: int) -> str:
    return f"ak_{number:032d}"


# --- nonce (unit) ---------------------------------------------------------------------


class RecordingRedis:
    """redis-py's `SET`, recording the exact arguments; NX answers None the second time."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int, bool, int]] = []
        self.keys: set[str] = set()

    def set(self, name: str, value: int, *, nx: bool, pxat: int) -> bool | None:
        self.calls.append((name, value, nx, pxat))
        if nx and name in self.keys:
            return None
        self.keys.add(name)
        return True


class TimingOutRedis:
    def set(self, name: str, value: int, *, nx: bool, pxat: int) -> bool | None:
        raise redis.exceptions.TimeoutError("Timeout reading from 10.201.0.5:6379")


def test_the_nonce_is_set_nx_with_pxat_at_the_timestamp_plus_300_seconds() -> None:
    client = RecordingRedis()
    store = NonceStore(client)
    timestamp = 1_790_000_000

    first = store.claim(api_key(1), "req-1", timestamp)
    replay = store.claim(api_key(1), "req-1", timestamp)

    expected = (f"nonce:{api_key(1)}:req-1", 1, True, 1_790_000_300_000)
    assert (first, replay) == (True, False)
    assert client.calls == [expected, expected]
    assert expires_at_ms(timestamp) == (timestamp + 300) * 1000
    assert nonce_key("ak_a", "b:c") == "nonce:ak_a:b:c"


def test_the_same_request_id_under_another_api_key_is_not_a_replay() -> None:
    store = NonceStore(RecordingRedis())

    assert store.claim(api_key(1), "req-1", 1_790_000_000) is True
    assert store.claim(api_key(2), "req-1", 1_790_000_000) is True


def test_an_unavailable_redis_raises_without_the_key_or_the_host() -> None:
    with pytest.raises(NonceStoreUnavailable) as raised:
        NonceStore(TimingOutRedis()).claim(api_key(1), "req-1", 1_790_000_000)

    message = str(raised.value)
    assert message == "TimeoutError"
    assert api_key(1) not in message


@pytest.mark.parametrize("url", ["", "not a redis url"], ids=["unset", "malformed"])
def test_no_usable_redis_url_means_unavailable(url: str) -> None:
    store = NonceStore.from_settings(Settings(redis_url=url))

    with pytest.raises(NonceStoreUnavailable):
        store.claim(api_key(1), "req-1", 1_790_000_000)


def test_the_redis_client_times_out_after_200_ms() -> None:
    """A pooled client of its own (design §2); `from_url` does not connect yet."""
    store = NonceStore.from_settings(Settings(redis_url="redis://127.0.0.1:6399/0"))

    client = store._client
    assert isinstance(client, redis.Redis)
    options = client.connection_pool.connection_kwargs
    assert options["socket_timeout"] == 0.2
    assert options["socket_connect_timeout"] == 0.2


# --- decryption cache (unit) ----------------------------------------------------------


def credential_row(row_id: int, key: str, plaintext: str, key_version: int = 1):
    """A credential row as the cache sees it (not stored anywhere)."""
    aad = credential_aad(key, key_version)
    encrypted, master_version = encrypt_secret(KEYRING, plaintext, associated_data=aad)
    return IntegrationCredential(
        id=row_id,
        tenant_id=1,
        project_id=1,
        public_api_key=key,
        key_version=key_version,
        encrypted_secret=encrypted,
        encryption_key_version=master_version,
        status=CredentialStatus.ACTIVE,
        valid_from=NOW,
        created_at=NOW,
    )


class CountingKeyring:
    """The keyring loader; it is only called on a cache miss, so it counts decryptions."""

    def __init__(self) -> None:
        self.loads = 0

    def __call__(self) -> MasterKeyring:
        self.loads += 1
        return KEYRING


class Ticks:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_one_credential_row_is_decrypted_once() -> None:
    cache = SecretCache()
    keyring = CountingKeyring()
    row = credential_row(1, api_key(1), "sk_" + "0" * 64)

    secrets = [cache.secret_for(row, keyring) for _ in range(3)]

    assert secrets == ["sk_" + "0" * 64] * 3
    assert keyring.loads == 1


def test_a_new_ciphertext_is_decrypted_again() -> None:
    """Rotation adds a row; re-wrapping changes the ciphertext of the same row: both miss."""
    cache = SecretCache()
    keyring = CountingKeyring()
    version_1 = credential_row(1, api_key(1), "sk_" + "0" * 63 + "1")
    version_2 = credential_row(2, api_key(1), "sk_" + "0" * 63 + "2", key_version=2)
    rewrapped = credential_row(1, api_key(1), "sk_" + "0" * 63 + "1")

    cache.secret_for(version_1, keyring)
    assert cache.secret_for(version_2, keyring) == "sk_" + "0" * 63 + "2"
    assert keyring.loads == 2
    assert cache.secret_for(rewrapped, keyring) == "sk_" + "0" * 63 + "1"
    assert keyring.loads == 3


def test_an_entry_lives_300_seconds() -> None:
    ticks = Ticks()
    cache = SecretCache(clock=ticks)
    keyring = CountingKeyring()
    row = credential_row(1, api_key(1), "sk_" + "0" * 64)

    cache.secret_for(row, keyring)
    ticks.now = 299.0
    cache.secret_for(row, keyring)
    assert keyring.loads == 1

    ticks.now = 300.0
    cache.secret_for(row, keyring)
    assert keyring.loads == 2


def test_the_cache_keeps_the_most_recently_used_entries() -> None:
    cache = SecretCache(max_entries=2)
    keyring = CountingKeyring()
    first, second, third = (credential_row(n, api_key(n), "sk_" + "0" * 64) for n in (1, 2, 3))

    cache.secret_for(first, keyring)
    cache.secret_for(second, keyring)
    cache.secret_for(first, keyring)
    cache.secret_for(third, keyring)
    assert (len(cache), keyring.loads) == (2, 3)

    # `second` was the least recently used: evicted. `first` is still there.
    cache.secret_for(first, keyring)
    assert keyring.loads == 3
    cache.secret_for(second, keyring)
    assert keyring.loads == 4


def test_a_failed_decryption_is_not_cached() -> None:
    cache = SecretCache()
    keyring = CountingKeyring()
    row = credential_row(1, api_key(1), "sk_" + "0" * 64)
    # The AAD binds the ciphertext to its api key: the same ciphertext under another key fails.
    row.public_api_key = api_key(2)

    for _ in range(2):
        with pytest.raises(DecryptionFailed):
            cache.secret_for(row, keyring)

    assert len(cache) == 0
    assert keyring.loads == 2


# --- canonical form and fingerprint (unit) --------------------------------------------


def meter(shape: PayloadShape, unit: str, kind: QuantityKind) -> UsageMeterType:
    return UsageMeterType(payload_shape=shape, unit=unit, quantity_kind=kind)


AUDIO_SECOND = meter(PayloadShape.QUANTITY, "SECOND", QuantityKind.DECIMAL)
LLM_TOKEN = meter(PayloadShape.LLM_TOKEN_FIELDS, "TOKEN", QuantityKind.INTEGER)


def audio_payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "1.0",
        "event_id": uuid7(1),
        "request_id": "call-1",
        "provider": "openai",
        "model": "whisper-fictional",
        "usage_type": "AUDIO_SECOND",
        "quantity": "37.42",
        "unit": "SECOND",
        "occurred_at": OCCURRED,
    }
    payload.update(changes)
    return payload


def fingerprint_of(payload: dict[str, object], meter_type: UsageMeterType) -> str:
    event = usage_ingest._validate(payload)
    return usage_ingest.payload_fingerprint(event, usage_ingest._measures(event, meter_type))


def test_the_fingerprint_is_the_sha256_of_the_canonical_object() -> None:
    """Design §2「规范化与指纹」: sorted keys, no whitespace, UTF-8; quantity to 8 places."""
    template = (
        '{"conversation_id":null,"event_id":"EVENT","model":"whisper-fictional",'
        '"occurred_at":"2026-10-05T07:59:30.250000Z","provider":"openai",'
        '"quantity":"37.42000000","request_id":"call-1","schema_version":"1.0",'
        '"unit":"SECOND","usage_type":"AUDIO_SECOND"}'
    )
    canonical = template.replace("EVENT", uuid7(1))
    expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    assert fingerprint_of(audio_payload(), AUDIO_SECOND) == expected


def test_equivalent_spellings_have_one_fingerprint() -> None:
    plain = fingerprint_of(audio_payload(), AUDIO_SECOND)
    respelled = audio_payload(quantity="37.420", occurred_at="2026-10-05T15:59:30.25+08:00")
    diagnostic = audio_payload(tenant_id=public_id(1), project_id=public_id(2))

    assert fingerprint_of(respelled, AUDIO_SECOND) == plain
    # The diagnostic ids are compared separately and are not part of the fingerprint.
    assert fingerprint_of(diagnostic, AUDIO_SECOND) == plain
    assert fingerprint_of(audio_payload(conversation_id="c-1"), AUDIO_SECOND) != plain
    assert fingerprint_of(audio_payload(quantity="37.42000001"), AUDIO_SECOND) != plain


def test_token_counts_are_in_the_fingerprint_as_integers() -> None:
    tokens = {
        "input_tokens": 3,
        "output_tokens": 2,
        "cache_creation_input_tokens": 1,
        "cache_read_input_tokens": 0,
    }
    payload = audio_payload(usage_type="LLM_TOKEN", **tokens)
    for name in ("quantity", "unit"):
        payload.pop(name)
    event = usage_ingest._validate(payload)
    measures = usage_ingest._measures(event, LLM_TOKEN)

    assert measures.tokens == tokens
    assert (measures.quantity, measures.unit) == (None, "TOKEN")
    other = {**payload, "cache_read_input_tokens": 1}
    assert fingerprint_of(other, LLM_TOKEN) != fingerprint_of(payload, LLM_TOKEN)


# --- SQLite and MySQL factories --------------------------------------------------------


def _sqlite_factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
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
    yield factory
    engine.dispose()


def _mysql_engine(monkeypatch: pytest.MonkeyPatch) -> Engine:
    if not TEST_DATABASE_URL:
        pytest.skip("BILLING_TEST_DATABASE_URL is not set; the MySQL half needs a real MySQL")
    # Tables through alembic: the composite keys, CHECKs and triggers are migration 0017's,
    # the meter types the ones 0012 seeds.
    from app.core.config import get_settings

    monkeypatch.setenv("BILLING_DATABASE_URL", TEST_DATABASE_URL)
    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")
    get_settings.cache_clear()
    return create_engine(TEST_DATABASE_URL, pool_size=10, max_overflow=4)


def _mysql_factory(monkeypatch: pytest.MonkeyPatch) -> Iterator[sessionmaker[Session]]:
    engine = _mysql_engine(monkeypatch)
    clean(engine)
    try:
        yield create_session_factory(engine)
    finally:
        clean(engine)
        engine.dispose()


def clean(engine: Engine) -> None:
    """⚠️ The database is shared: wipe this task's rows before and after every case.

    The conflict table refuses DELETE (0017's trigger), so TRUNCATE (DDL, no triggers) with
    foreign key checks off. The audit table is append-only (0010) and truncated the same way,
    as test_pricing_rules_service.py does.
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
        ours = select(Tenant.id).where(Tenant.email.like(f"%{TEST_EMAIL_DOMAIN}"))
        owned = IntegrationCredential.tenant_id.in_(ours)
        connection.execute(delete(IntegrationCredential).where(owned))
        connection.execute(delete(Project).where(Project.tenant_id.in_(ours)))
        connection.execute(delete(Tenant).where(Tenant.email.like(f"%{TEST_EMAIL_DOMAIN}")))


@pytest.fixture(params=["sqlite", "mysql"])
def factory(request, monkeypatch) -> Iterator[sessionmaker[Session]]:
    if request.param == "mysql":
        yield from _mysql_factory(monkeypatch)
        return
    yield from _sqlite_factory()


@pytest.fixture
def mysql_factory(monkeypatch) -> Iterator[sessionmaker[Session]]:
    yield from _mysql_factory(monkeypatch)


def make_credential(factory: sessionmaker[Session], number: int) -> AuthenticatedCredential:
    """A tenant, its project and version 1 of api key `number`, committed.

    The ciphertext is a placeholder: these cases call `ingest_one` directly, past the signature.
    """
    with factory() as session:
        tenant = Tenant(
            public_id=public_id(number),
            company_name=f"Fictional {number} Sdn Bhd",
            email=f"tenant-{number}{TEST_EMAIL_DOMAIN}",
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


def llm_payload(number: int, **changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "1.0",
        "event_id": uuid7(number),
        "request_id": f"call-{number}",
        "provider": "anthropic",
        "model": "claude-fictional-1",
        "usage_type": "LLM_TOKEN",
        "input_tokens": 1200,
        "output_tokens": 340,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 50,
        "occurred_at": OCCURRED,
    }
    payload.update(changes)
    return payload


def ingest(
    factory: sessionmaker[Session],
    credential: AuthenticatedCredential,
    payload: dict[str, object],
) -> IngestOutcome:
    return ingest_one(factory, credential, payload, context=CONTEXT, now=NOW)


def count(factory: sessionmaker[Session], model: type) -> int:
    with factory() as session:
        return int(session.execute(select(func.count()).select_from(model)).scalar_one())


def conflict_audits(factory: sessionmaker[Session], api_key_value: str) -> list[AuditLog]:
    with factory() as session:
        statement = select(AuditLog).where(
            AuditLog.action == AuditAction.USAGE_EVENT_IDEMPOTENCY_CONFLICT
        )
        rows = session.execute(statement).scalars()
        return [row for row in rows if api_key_value in (row.after_state or "")]


# --- 事务中途失败 (SQLite and MySQL) ----------------------------------------------------


def test_a_failing_conflict_audit_rolls_the_conflict_row_back(factory, monkeypatch) -> None:
    """INV-13: the conflict row and its audit commit together, or not at all."""
    credential = make_credential(factory, 1)
    accepted = ingest(factory, credential, llm_payload(1))
    assert accepted.status == "accepted"

    def refuse(*args: object, **kwargs: object) -> None:
        raise RuntimeError("audit write failed")

    monkeypatch.setattr(usage_ingest, "record_audit", refuse)
    with pytest.raises(RuntimeError):
        ingest(factory, credential, llm_payload(1, output_tokens=341))
    monkeypatch.undo()

    assert count(factory, UsageEventConflict) == 0
    assert conflict_audits(factory, credential.api_key) == []
    assert count(factory, UsageEvent) == 1

    # The client's retry records both (the earlier attempt left nothing to deduplicate against).
    with pytest.raises(IdempotencyConflict):
        ingest(factory, credential, llm_payload(1, output_tokens=341))
    assert count(factory, UsageEventConflict) == 1
    assert len(conflict_audits(factory, credential.api_key)) == 1


# --- 并发 (MySQL) -----------------------------------------------------------------------


def run_together(calls: list[Callable[[], object]]) -> list[str]:
    """Start every call at once; each result is the outcome's status or the exception's name."""
    barrier = threading.Barrier(len(calls))

    def run(call: Callable[[], object]) -> str:
        barrier.wait()
        try:
            outcome = call()
        except Exception as error:
            # The exception's name is the result being compared.
            return type(error).__name__
        assert isinstance(outcome, IngestOutcome)
        return outcome.status

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        return list(pool.map(run, calls))


def test_concurrent_duplicates_store_one_row(mysql_factory) -> None:
    credential = make_credential(mysql_factory, 2)
    payload = llm_payload(2)

    results = run_together([partial(ingest, mysql_factory, credential, payload)] * 8)

    assert sorted(results) == ["accepted"] + ["already_received"] * 7
    assert count(mysql_factory, UsageEvent) == 1
    assert count(mysql_factory, UsageEventConflict) == 0


def test_concurrent_conflicts_are_each_recorded_once(mysql_factory) -> None:
    credential = make_credential(mysql_factory, 3)
    variants = [llm_payload(3, output_tokens=tokens) for tokens in range(6)]
    calls = [partial(ingest, mysql_factory, credential, variant) for variant in variants]

    results = run_together(calls)

    assert sorted(results) == ["IdempotencyConflict"] * 5 + ["accepted"]
    assert count(mysql_factory, UsageEvent) == 1
    with mysql_factory() as session:
        rows = session.execute(select(UsageEventConflict)).scalars().all()
    assert len(rows) == 5
    assert {row.mismatch for row in rows} == {ConflictMismatch.FINGERPRINT}
    assert len({row.payload_fingerprint for row in rows}) == 5
    assert len(conflict_audits(mysql_factory, credential.api_key)) == 5


def test_the_same_conflict_sent_concurrently_is_recorded_once(mysql_factory) -> None:
    """The dedupe unique key decides; the losers write no second audit (design §2)."""
    credential = make_credential(mysql_factory, 4)
    assert ingest(mysql_factory, credential, llm_payload(4)).status == "accepted"
    conflicting = llm_payload(4, input_tokens=1)

    results = run_together([partial(ingest, mysql_factory, credential, conflicting)] * 6)

    assert results == ["IdempotencyConflict"] * 6
    assert count(mysql_factory, UsageEventConflict) == 1
    assert len(conflict_audits(mysql_factory, credential.api_key)) == 1


# --- direct inserts (MySQL) -----------------------------------------------------------


@pytest.fixture
def connection(monkeypatch) -> Iterator[Connection]:
    """One connection in a transaction that is always rolled back: nothing is left behind."""
    engine = _mysql_engine(monkeypatch)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                yield connection
            finally:
                transaction.rollback()
    finally:
        engine.dispose()


def _insert(connection: Connection, table: str, values: dict[str, object]) -> int:
    """Raw SQL: past the model's enum types, straight to what the database refuses."""
    columns = ", ".join(values)
    placeholders = ", ".join(f":{name}" for name in values)
    statement = text(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})")
    return int(connection.execute(statement, values).lastrowid)


def _refused(connection: Connection, statement: Callable[[], object]) -> int:
    """Run in a savepoint; return the MySQL error number it was refused with."""
    with pytest.raises(DBAPIError) as raised:
        with connection.begin_nested():
            statement()
    return int(raised.value.orig.args[0])


class Scope:
    """Two tenants, three projects, a credential on two of them, and the seeded meter types."""

    def __init__(self, connection: Connection) -> None:
        self.connection = connection
        self.tenant_a = self._tenant(51)
        self.tenant_b = self._tenant(52)
        self.project_a1 = self._project(self.tenant_a, 61)
        self.project_a2 = self._project(self.tenant_a, 62)
        self.project_b1 = self._project(self.tenant_b, 63)
        self.credential_a1 = self._credential(self.tenant_a, self.project_a1, 71)
        self.credential_b1 = self._credential(self.tenant_b, self.project_b1, 72)
        rows = connection.execute(select(UsageMeterType.code, UsageMeterType.id)).all()
        self.meter: dict[str, int] = dict(rows)

    def _tenant(self, number: int) -> int:
        values = {
            "public_id": public_id(number),
            "company_name": f"Fictional {number} Sdn Bhd",
            "email": f"tenant-{number}{TEST_EMAIL_DOMAIN}",
            "created_at": NOW,
            "updated_at": NOW,
        }
        return int(self.connection.execute(insert(Tenant).values(**values)).lastrowid)

    def _project(self, tenant_id: int, number: int) -> int:
        values = {
            "public_id": public_id(number),
            "tenant_id": tenant_id,
            "name": "Chatbot",
            "created_at": NOW,
            "updated_at": NOW,
        }
        return int(self.connection.execute(insert(Project).values(**values)).lastrowid)

    def _credential(self, tenant_id: int, project_id: int, number: int) -> int:
        values = {
            "tenant_id": tenant_id,
            "project_id": project_id,
            "public_api_key": api_key(number),
            "key_version": 1,
            "encrypted_secret": "not-a-real-ciphertext",
            "encryption_key_version": 1,
            "status": "ACTIVE",
            "valid_from": NOW,
            "created_at": NOW,
        }
        statement = insert(IntegrationCredential).values(**values)
        return int(self.connection.execute(statement).lastrowid)

    def llm_event(self, **overrides: object) -> dict[str, object]:
        """A valid LLM_TOKEN row on project A1 with A1's credential; `overrides` change it."""
        values: dict[str, object] = {
            "public_id": public_id(1000 + next(_NUMBERS)),
            "event_id": uuid7(1000 + next(_NUMBERS)),
            "schema_version": "1.0",
            "tenant_id": self.tenant_a,
            "project_id": self.project_a1,
            "integration_credential_id": self.credential_a1,
            "request_id": "call-1",
            "conversation_id": None,
            "provider_code_raw": "anthropic",
            "model_code_raw": "claude-fictional-1",
            "provider_id": None,
            "model_id": None,
            "usage_meter_type_id": self.meter["LLM_TOKEN"],
            "payload_shape": "LLM_TOKEN_FIELDS",
            "quantity_kind": "INTEGER",
            "input_tokens": 3,
            "output_tokens": 2,
            "cache_creation_input_tokens": 1,
            "cache_read_input_tokens": 0,
            "quantity": None,
            "unit": "TOKEN",
            "payload_fingerprint": ZERO_FINGERPRINT,
            "status": "RECEIVED",
            "occurred_at": dt.datetime(2026, 10, 5, 7, 59, 30, 250000),
            "received_at": NOW,
            "created_at": NOW,
        }
        values.update(overrides)
        return values

    def quantity_event(
        self,
        code: str = "AUDIO_SECOND",
        **overrides: object,
    ) -> dict[str, object]:
        """A valid QUANTITY row of meter type `code` (unit and kind taken from the seed)."""
        [seed] = [seed for seed in SEED_METER_TYPES if seed.code == code]
        values = self.llm_event(
            usage_meter_type_id=self.meter[code],
            payload_shape="QUANTITY",
            quantity_kind=seed.quantity_kind.value,
            input_tokens=None,
            output_tokens=None,
            cache_creation_input_tokens=None,
            cache_read_input_tokens=None,
            quantity=Decimal("2"),
            unit=seed.unit,
        )
        values.update(overrides)
        return values

    def insert_event(self, values: dict[str, object]) -> int:
        return _insert(self.connection, "usage_events", values)

    def refused(self, values: dict[str, object], table: str = "usage_events") -> int:
        return _refused(self.connection, lambda: _insert(self.connection, table, values))


@pytest.fixture
def scope(connection: Connection) -> Scope:
    return Scope(connection)


def test_valid_rows_of_both_shapes_are_accepted(scope: Scope) -> None:
    """The positive control: the CHECKs and composite keys are not refusing everything."""
    scope.insert_event(scope.llm_event())
    scope.insert_event(scope.quantity_event("AUDIO_SECOND", quantity=Decimal("12.5")))
    scope.insert_event(scope.quantity_event("IMAGE_GENERATION", quantity=Decimal("3")))
    # The other tenant's project with that project's own credential: fine as well.
    other = scope.llm_event(
        tenant_id=scope.tenant_b,
        project_id=scope.project_b1,
        integration_credential_id=scope.credential_b1,
    )
    scope.insert_event(other)


_TOKEN_COLUMNS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)
# Every subset of the four token columns that is neither all of them nor none: the interleaved
# NULLs a chained `=` could not catch (design v2), listed one by one.
_INTERLEAVED = [
    present for size in (1, 2, 3) for present in itertools.combinations(_TOKEN_COLUMNS, size)
]


def _tokens(present: tuple[str, ...]) -> dict[str, object]:
    return {name: (1 if name in present else None) for name in _TOKEN_COLUMNS}


@pytest.mark.parametrize("present", _INTERLEAVED, ids="+".join)
def test_an_llm_row_with_interleaved_token_nulls_is_refused(scope: Scope, present) -> None:
    refused = scope.refused(scope.llm_event(**_tokens(present)))

    assert refused == _ER_CHECK_CONSTRAINT_VIOLATED


@pytest.mark.parametrize("present", [*_INTERLEAVED, _TOKEN_COLUMNS], ids="+".join)
def test_a_quantity_row_with_any_token_is_refused(scope: Scope, present) -> None:
    refused = scope.refused(scope.quantity_event(**_tokens(present)))

    assert refused == _ER_CHECK_CONSTRAINT_VIOLATED


@pytest.mark.parametrize(
    "case",
    [
        "both-groups",
        "neither-group",
        "quantity-without-quantity",
        "negative-quantity",
        "fractional-integer-quantity",
        "unknown-status",
        "model-without-provider",
        *(f"negative-{name}" for name in _TOKEN_COLUMNS),
    ],
)
def test_the_checks_refuse_bad_rows(scope: Scope, case: str) -> None:
    rows = {
        "both-groups": scope.llm_event(quantity=Decimal("1")),
        "neither-group": scope.llm_event(**_tokens(())),
        "quantity-without-quantity": scope.quantity_event(quantity=None),
        "negative-quantity": scope.quantity_event(quantity=Decimal("-1")),
        "fractional-integer-quantity": scope.quantity_event(
            "IMAGE_GENERATION",
            quantity=Decimal("2.5"),
        ),
        "unknown-status": scope.llm_event(status="NOT_A_STATUS"),
        "model-without-provider": scope.llm_event(model_id=1),
        **{f"negative-{name}": scope.llm_event(**{name: -1}) for name in _TOKEN_COLUMNS},
    }

    assert scope.refused(rows[case]) == _ER_CHECK_CONSTRAINT_VIOLATED


# Design §2 lists the nullable columns; every other one is NOT NULL. `id` is not in the list:
# an explicit NULL on an AUTO_INCREMENT key asks MySQL for the next value instead.
_REQUIRED_EVENT_COLUMNS = [
    "public_id",
    "event_id",
    "schema_version",
    "tenant_id",
    "project_id",
    "integration_credential_id",
    "request_id",
    "provider_code_raw",
    "model_code_raw",
    "usage_meter_type_id",
    "payload_shape",
    "quantity_kind",
    "unit",
    "payload_fingerprint",
    "status",
    "occurred_at",
    "received_at",
    "created_at",
]
_REQUIRED_CONFLICT_COLUMNS = [
    "usage_event_id",
    "event_id",
    "integration_credential_id",
    "api_key",
    "payload_fingerprint",
    "mismatch",
    "received_at",
]


@pytest.mark.parametrize("column", _REQUIRED_EVENT_COLUMNS)
def test_every_required_event_column_refuses_null(scope: Scope, column: str) -> None:
    """Design v6: the composite keys and CHECKs cannot be skipped through a NULL."""
    refused = scope.refused(scope.llm_event(**{column: None}))

    assert refused == _ER_BAD_NULL


def _conflict_values(scope: Scope, **overrides: object) -> dict[str, object]:
    event_id = uuid7(2000 + next(_NUMBERS))
    usage_event_id = scope.insert_event(scope.llm_event(event_id=event_id))
    values: dict[str, object] = {
        "usage_event_id": usage_event_id,
        "event_id": event_id,
        "integration_credential_id": scope.credential_b1,
        "api_key": api_key(72),
        "payload_fingerprint": ZERO_FINGERPRINT,
        "mismatch": "OWNERSHIP",
        "received_at": NOW,
    }
    values.update(overrides)
    return values


@pytest.mark.parametrize("column", _REQUIRED_CONFLICT_COLUMNS)
def test_every_conflict_column_refuses_null(scope: Scope, column: str) -> None:
    values = _conflict_values(scope, **{column: None})

    assert scope.refused(values, "usage_event_conflicts") == _ER_BAD_NULL


def test_the_conflict_table_dedupes_and_checks_the_mismatch(scope: Scope) -> None:
    values = _conflict_values(scope)
    _insert(scope.connection, "usage_event_conflicts", values)

    assert scope.refused(values, "usage_event_conflicts") == _ER_DUP_ENTRY
    other = _conflict_values(scope, mismatch="SOMETHING")
    assert scope.refused(other, "usage_event_conflicts") == _ER_CHECK_CONSTRAINT_VIOLATED


def test_the_conflict_table_is_append_only(scope: Scope) -> None:
    conflict_id = _insert(scope.connection, "usage_event_conflicts", _conflict_values(scope))
    update = text("UPDATE usage_event_conflicts SET mismatch = 'BOTH' WHERE id = :id")
    remove = text("DELETE FROM usage_event_conflicts WHERE id = :id")

    for statement in (update, remove):
        run = partial(scope.connection.execute, statement, {"id": conflict_id})
        assert _refused(scope.connection, run) == _ER_SIGNAL_EXCEPTION

    kept = text("SELECT mismatch FROM usage_event_conflicts WHERE id = :id")
    assert scope.connection.execute(kept, {"id": conflict_id}).scalar_one() == "OWNERSHIP"


@pytest.mark.parametrize(
    "case",
    ["other-tenants-project", "same-tenants-other-project", "credential-of-another-tenant"],
)
def test_the_event_owner_is_the_credentials_owner(scope: Scope, case: str) -> None:
    """INV-8 in the database: (credential, project, tenant) is one composite foreign key."""
    owners = {
        # A1's credential on B's project (which exists).
        "other-tenants-project": {"tenant_id": scope.tenant_b, "project_id": scope.project_b1},
        # A1's credential on A2: same tenant, other project.
        "same-tenants-other-project": {"project_id": scope.project_a2},
        # B1's credential on A1.
        "credential-of-another-tenant": {"integration_credential_id": scope.credential_b1},
    }

    refused = scope.refused(scope.llm_event(**owners[case]))

    assert refused == _ER_NO_REFERENCED_ROW


@pytest.mark.parametrize(
    "case",
    ["token-type-quantity-shape", "quantity-type-token-shape", "other-unit", "other-kind"],
)
def test_shape_unit_and_kind_are_the_meter_types(scope: Scope, case: str) -> None:
    rows = {
        # LLM_TOKEN declared as QUANTITY (the field groups fit the declared shape).
        "token-type-quantity-shape": scope.quantity_event(
            usage_meter_type_id=scope.meter["LLM_TOKEN"],
            unit="TOKEN",
            quantity_kind="INTEGER",
        ),
        # AUDIO_SECOND declared as LLM_TOKEN_FIELDS.
        "quantity-type-token-shape": scope.llm_event(
            usage_meter_type_id=scope.meter["AUDIO_SECOND"],
            unit="SECOND",
            quantity_kind="DECIMAL",
        ),
        "other-unit": scope.quantity_event("AUDIO_SECOND", unit="MINUTE"),
        "other-kind": scope.quantity_event("AUDIO_SECOND", quantity_kind="INTEGER"),
    }

    assert scope.refused(rows[case]) == _ER_NO_REFERENCED_ROW


def _provider(connection: Connection, number: int) -> int:
    values = {
        "public_id": public_id(number),
        "code": f"usage-ingest-test-{number}",
        "display_name": "Fictional provider",
        "status": "ACTIVE",
        "created_at": NOW,
        "updated_at": NOW,
    }
    return _insert(connection, "ai_providers", values)


def _model(connection: Connection, provider_id: int, number: int) -> int:
    values = {
        "public_id": public_id(number),
        "provider_id": provider_id,
        "code": f"usage-ingest-test-{number}",
        "display_name": "Fictional model",
        "status": "ACTIVE",
        "created_at": NOW,
        "updated_at": NOW,
    }
    return _insert(connection, "ai_models", values)


def test_the_catalog_reference_columns(scope: Scope) -> None:
    """§79's provider and model: the model belongs to the provider; the provider exists."""
    provider = _provider(scope.connection, 81)
    stranger = _provider(scope.connection, 82)
    model = _model(scope.connection, provider, 83)
    missing_provider = max(provider, stranger) + 1000

    other_providers_model = scope.llm_event(provider_id=stranger, model_id=model)
    assert scope.refused(other_providers_model) == _ER_NO_REFERENCED_ROW
    no_such_provider = scope.llm_event(provider_id=missing_provider)
    assert scope.refused(no_such_provider) == _ER_NO_REFERENCED_ROW
    without_provider = scope.llm_event(model_id=model)
    assert scope.refused(without_provider) == _ER_CHECK_CONSTRAINT_VIOLATED

    # Both empty (as ingestion writes them), the provider alone, both: accepted.
    scope.insert_event(scope.llm_event())
    scope.insert_event(scope.llm_event(provider_id=provider))
    scope.insert_event(scope.llm_event(provider_id=provider, model_id=model))


def test_event_ids_are_unique_byte_for_byte(scope: Scope) -> None:
    event_id = "0" * 25 + "A"
    scope.insert_event(scope.llm_event(event_id=event_id))

    assert scope.refused(scope.llm_event(event_id=event_id)) == _ER_DUP_ENTRY
    # Another spelling is another value (utf8mb4_0900_bin): the endpoint only lets one through.
    scope.insert_event(scope.llm_event(event_id=event_id.lower()))
