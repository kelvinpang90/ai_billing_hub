"""Delivery of persisted AI usage to the local ingestion endpoint."""

from __future__ import annotations

import datetime as dt
import http.client
import json
import logging
import random
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from contextlib import contextmanager
from functools import lru_cache
from time import monotonic
from urllib.parse import urlsplit, urlunsplit

from celery import shared_task
from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session, sessionmaker

from app.ai_api import client_rules
from app.ai_api.database import create_ai_database_engine, create_ai_session_factory
from app.ai_api.outbox import AiUsageOutbox, AiUsageOutboxStatus
from app.core.config import Settings, get_settings
from app.core.crypto import DecryptionFailed, EncryptionNotConfigured, decrypt_secret, load_keyring
from app.models.integration import IntegrationCredential
from app.services.integration_auth import credential_aad, is_verifiable

logger = logging.getLogger(__name__)
_SEND_LIMIT_SECONDS = 10.0
_MAX_RESPONSE_BYTES = 64 * 1024
type Send = Callable[
    [AiUsageOutbox, IntegrationCredential, str, tuple[str, str, int]], tuple[int, bytes]
]


@lru_cache(maxsize=1)
def _session_factory() -> sessionmaker[Session]:
    settings = get_settings()
    return create_ai_session_factory(
        create_ai_database_engine(
            settings.database_url,
            threads=settings.ai_db_threads,
            pool_timeout_seconds=settings.ai_db_pool_timeout_seconds,
            connect_timeout_seconds=settings.ai_db_connect_timeout_seconds,
            rw_timeout_seconds=settings.ai_db_rw_timeout_seconds,
        )
    )


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def _eligible(now: dt.datetime):
    return or_(
        and_(
            AiUsageOutbox.status == AiUsageOutboxStatus.PENDING,
            AiUsageOutbox.next_attempt_at <= now,
        ),
        and_(
            AiUsageOutbox.status == AiUsageOutboxStatus.SENDING,
            AiUsageOutbox.next_attempt_at <= now,
        ),
    )


def claim_one(
    factory: sessionmaker[Session], settings: Settings, now: dt.datetime | None = None
) -> AiUsageOutbox | None:
    """Claim one due row using an affected-row fence, rather than a select lock."""
    moment = now or _now()
    with factory.begin() as session:
        candidate = session.scalar(
            select(AiUsageOutbox.id).where(_eligible(moment)).order_by(AiUsageOutbox.id).limit(1)
        )
        if candidate is None:
            return None
        claim_id = client_rules.new_ulid()
        changed = session.execute(
            update(AiUsageOutbox)
            .where(AiUsageOutbox.id == candidate, _eligible(moment))
            .values(
                status=AiUsageOutboxStatus.SENDING,
                claim_id=claim_id,
                attempts=AiUsageOutbox.attempts + 1,
                first_attempted_at=func.coalesce(AiUsageOutbox.first_attempted_at, moment),
                next_attempt_at=moment + dt.timedelta(seconds=settings.ai_delivery_lease_seconds),
            )
        )
        if changed.rowcount != 1:
            return None
        return session.get(AiUsageOutbox, candidate)


def _credential(
    session: Session, project_id: int, now: dt.datetime
) -> IntegrationCredential | None:
    rows = session.scalars(
        select(IntegrationCredential).where(IntegrationCredential.project_id == project_id)
    )
    usable = [row for row in rows if is_verifiable(row, now)]
    return max(usable, key=lambda row: (row.valid_from, row.id), default=None)


def _retry_delay(attempts: int, random_source=random.uniform) -> float:
    return min(3600, 2 ** min(attempts, 12)) * random_source(0.5, 1.0)


def settle(
    factory: sessionmaker[Session],
    row: AiUsageOutbox,
    *,
    delivered: bool,
    retryable: bool,
    error: str | None,
    now: dt.datetime | None = None,
    random_source=random.uniform,
) -> bool:
    moment = now or _now()
    if delivered:
        values = {
            "status": AiUsageOutboxStatus.DELIVERED,
            "claim_id": None,
            "delivered_at": moment,
            "last_error": None,
        }
    elif retryable:
        values = {
            "status": AiUsageOutboxStatus.PENDING,
            "claim_id": None,
            "next_attempt_at": moment
            + dt.timedelta(seconds=_retry_delay(row.attempts, random_source)),
            "last_error": error,
        }
    else:
        values = {
            "status": AiUsageOutboxStatus.DEAD,
            "claim_id": None,
            "last_error": error,
        }
    with factory.begin() as session:
        result = session.execute(
            update(AiUsageOutbox)
            .where(
                AiUsageOutbox.id == row.id,
                AiUsageOutbox.status == AiUsageOutboxStatus.SENDING,
                AiUsageOutbox.claim_id == row.claim_id,
            )
            .values(**values)
        )
    if not delivered and not retryable and result.rowcount:
        logger.error("AI usage delivery is dead", extra={"event_id": row.event_id, "error": error})
    return result.rowcount == 1


def _resolve(base_url: str) -> tuple[str, str, int] | None:
    parts = urlsplit(base_url)
    host = parts.hostname
    if host is None:
        return None
    port = parts.port or 80
    source = (
        "import socket,sys\n"
        "host,port=sys.argv[1],int(sys.argv[2])\n"
        "print(socket.getaddrinfo(host,port,type=socket.SOCK_STREAM)[0][4][0])\n"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-c", source, host, str(port)],
            capture_output=True,
            check=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    address = result.stdout.strip()
    return (address, host, port) if address else None


class _SocketCloser:
    def __init__(self) -> None:
        # The socket itself, not the connection: on `Connection: close` getresponse() hands the
        # socket to the response and sets `connection.sock` to None before the body is read.
        self.sock: socket.socket | None = None
        self.expired = False

    def close(self) -> None:
        self.expired = True
        sock = self.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class _DirectHandler(urllib.request.HTTPHandler):
    def __init__(self, closer: _SocketCloser) -> None:
        super().__init__()
        self.closer = closer

    def http_open(self, request):
        handler = self

        class Connection(http.client.HTTPConnection):
            def connect(self) -> None:
                super().connect()
                handler.closer.sock = self.sock
                if handler.closer.expired:
                    handler.closer.close()

        return self.do_open(Connection, request)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def urllib_transport(
    request: urllib.request.Request, timeout: float, wall_limit: float = _SEND_LIMIT_SECONDS
):
    """Send with urllib, while a timer tears down a slow response socket."""
    closer = _SocketCloser()
    timer = threading.Timer(wall_limit, closer.close)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _DirectHandler(closer), _NoRedirect()
    )
    timer.start()
    try:
        try:
            response = opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            body = response.read(_MAX_RESPONSE_BYTES + 1)
            if closer.expired:
                raise TimeoutError("delivery wall-clock limit exceeded")
            if len(body) > _MAX_RESPONSE_BYTES:
                raise OSError("response exceeds delivery limit")
            status = response.status if hasattr(response, "status") else response.code
            return status, body
    finally:
        timer.cancel()


def _request(
    row: AiUsageOutbox,
    credential: IntegrationCredential,
    secret: str,
    endpoint: tuple[str, str, int],
) -> tuple[int, bytes]:
    address, host, port = endpoint
    body = row.payload_json.encode("utf-8")
    attempt_id = client_rules.new_ulid()
    timestamp = str(int(dt.datetime.now(dt.UTC).timestamp()))
    headers = client_rules.signed_headers(
        credential.public_api_key,
        credential.key_version,
        secret,
        body,
        timestamp=timestamp,
        attempt_id=attempt_id,
    )
    host_header = host if port == 80 else f"{host}:{port}"
    headers["Host"] = host_header
    direct_host = f"[{address}]" if ":" in address else address
    url = urlunsplit(("http", f"{direct_host}:{port}", client_rules.INGEST_PATH, "", ""))
    return urllib_transport(
        urllib.request.Request(url, body, headers=headers, method="POST"), _SEND_LIMIT_SECONDS
    )


def _outcome(status: int, body: bytes, event_id: str) -> tuple[bool, bool, str | None]:
    try:
        document = json.loads(body)
    except (TypeError, ValueError):
        document = None
    if status in (200, 202):
        return (
            (True, False, None)
            if client_rules.receipt_acknowledges(status, document, event_id)
            else (False, True, "INVALID_BILLING_RECEIPT")
        )
    if not isinstance(document, dict) or not isinstance(document.get("retryable"), bool):
        return (
            (False, True, "BILLING_UNAVAILABLE")
            if status >= 500
            else (False, False, "INVALID_BILLING_RESPONSE")
        )
    code, retryable = client_rules.error_outcome(document)
    return False, retryable, code


@contextmanager
def _delivery_lock(factory: sessionmaker[Session]):
    engine = factory.kw["bind"]
    assert engine is not None
    connection: Connection = engine.connect()
    acquired = False
    try:
        acquired = (
            connection.scalar(text("SELECT GET_LOCK(CONCAT('ai_usage_delivery:', DATABASE()), 0)"))
            == 1
        )
        yield acquired
    finally:
        if acquired:
            connection.execute(
                text("SELECT RELEASE_LOCK(CONCAT('ai_usage_delivery:', DATABASE()))")
            )
        connection.close()


@shared_task(name="ai_api.deliver_due", time_limit=59, soft_time_limit=57)
def deliver_due(
    factory: sessionmaker[Session] | None = None,
    settings: Settings | None = None,
    *,
    random_source=random.uniform,
    send: Send = _request,
) -> int:
    settings = settings or get_settings()
    factory = factory or _session_factory()
    started = monotonic()
    with _delivery_lock(factory) as locked:
        if not locked:
            return 0
        endpoint = _resolve(settings.ai_ingest_base_url)
        if endpoint is None:
            logger.warning("AI usage delivery endpoint could not be resolved")
            return 0
        delivered = 0
        while (
            delivered < settings.ai_delivery_batch
            and monotonic() - started < settings.ai_delivery_run_budget_seconds
        ):
            row = claim_one(factory, settings)
            if row is None:
                break
            with factory() as session:
                credential = _credential(session, row.project_id, _now())
                if credential is None:
                    settle(
                        factory,
                        row,
                        delivered=False,
                        retryable=True,
                        error="SIGNING_CREDENTIAL_MISSING",
                        random_source=random_source,
                    )
                    delivered += 1
                    continue
                try:
                    secret = decrypt_secret(
                        load_keyring(settings),
                        credential.encrypted_secret,
                        associated_data=credential_aad(
                            credential.public_api_key, credential.key_version
                        ),
                    )
                    status, body = send(row, credential, secret, endpoint)
                    accepted, retryable, error = _outcome(status, body, row.event_id)
                except (DecryptionFailed, EncryptionNotConfigured):
                    accepted, retryable, error = False, True, "SIGNING_CREDENTIAL_UNREADABLE"
                except (OSError, urllib.error.URLError, http.client.HTTPException):
                    accepted, retryable, error = False, True, "NETWORK_ERROR"
            settle(
                factory,
                row,
                delivered=accepted,
                retryable=retryable,
                error=error,
                random_source=random_source,
            )
            delivered += 1
        return delivered


__all__ = ["claim_one", "deliver_due", "settle", "urllib_transport"]
