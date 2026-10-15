"""The `status_webhook` alert dimension (design gate #241 v6 §2「健康告警维度」, AIH-TASK-052).

Rows of design §7 covered here:

- **告警范围** — an ENABLED project with an address that failed 6 times, a DISABLED one that failed
  6 times, one without an address: `failing` counts only the first. Then a project without an
  `ACTIVE` secret and one with `SECRET_DECRYPT_FAILED`: the level and summary of each combination
  are exactly those of design §2; a delivery afterwards clears the error and `signing error`
  drops to zero;
- the three outputs **OK / P2 / P1**, the threshold constant 6, the「落后」condition of
  `failing` and `signing error`, and a summary without tenant or project ids.

Delivery rows are written in the state the settlement leaves them in (`attempt_count`,
`last_error_code`, `delivered_version`); the one delivery that clears an error goes through the
real `claim_next` and `settle`. SQLite only: the dimension is a plain read with no locking.
"""

from __future__ import annotations

import datetime as dt
import itertools
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import create_session_factory
from app.models.base import Base
from app.models.integration import ProjectStatusDelivery, ProjectWebhookSecret, WebhookSecretStatus
from app.models.tenancy import IntegrationStatus, Project, Tenant
from app.services import alerts
from app.services.alerts import (
    STATUS_WEBHOOK_FAILING_ATTEMPTS,
    AlertContext,
    AlertLevel,
    AlertResult,
)
from app.services.customers import add_status_delivery
from app.services.status_delivery import (
    ENCRYPTION_NOT_CONFIGURED,
    HTTP_STATUS,
    NO_ACTIVE_SECRET,
    SECRET_DECRYPT_FAILED,
    Attempt,
    claim_next,
    settle,
)

TEST_EMAIL_DOMAIN = "@status-webhook-alerts-test.example.com"
COMPANY = "Status Webhook Alerts Test Sdn Bhd"
HOOK = "https://hooks.example.com/acuven/receive"

NOW = dt.datetime(2026, 10, 12, 9, 0, 0)
# The tenant's current version; a delivery row behind it is「落后」.
VERSION = 5

OK = AlertLevel.OK
P1 = AlertLevel.P1
P2 = AlertLevel.P2
NO_PROBLEM = AlertResult(OK, "no status webhook problem")

_NUMBERS = itertools.count(1)


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


# --- helpers ----------------------------------------------------------------------


def make_project(
    factory: sessionmaker[Session],
    *,
    url: str | None = HOOK,
    integration_status: IntegrationStatus = IntegrationStatus.ENABLED,
    secret: WebhookSecretStatus | None = WebhookSecretStatus.ACTIVE,
) -> tuple[str, str]:
    """A tenant at `VERSION` with one project, its delivery row, and optionally one secret.

    Returns the tenant's and the project's `public_id`.
    """
    number = next(_NUMBERS)
    with factory() as session:
        tenant = Tenant(
            public_id=str(uuid.uuid4()),
            company_name=COMPANY,
            email=f"tenant-{number}{TEST_EMAIL_DOMAIN}",
            status_version=VERSION,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(tenant)
        session.flush()
        project = Project(
            public_id=str(uuid.uuid4()),
            tenant_id=tenant.id,
            name=f"Project {number}",
            integration_status=integration_status,
            status_webhook_url=url,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(project)
        session.flush()
        add_status_delivery(session, project, now=NOW)
        if secret is not None:
            add_secret(session, project, secret)
        session.commit()
        return tenant.public_id, project.public_id


def add_secret(session: Session, project: Project, status: WebhookSecretStatus) -> None:
    """A secret row in `status`. The ciphertext is never opened here: a placeholder."""
    session.add(
        ProjectWebhookSecret(
            project_id=project.id,
            tenant_id=project.tenant_id,
            key_version=1,
            status=status,
            encrypted_secret="not-a-real-ciphertext",
            encryption_key_version=1,
            created_at=NOW,
            activated_at=None if status is WebhookSecretStatus.PENDING else NOW,
            retired_at=NOW if status is WebhookSecretStatus.RETIRED else None,
        )
    )


def failed(
    factory: sessionmaker[Session],
    project_public_id: str,
    attempts: int,
    *,
    error_code: str = HTTP_STATUS,
    delivered_version: int | None = None,
) -> None:
    """The delivery row after `attempts` consecutive failures: what `settle` leaves behind."""
    with factory() as session:
        by_id = select(Project).where(Project.public_id == project_public_id)
        project = session.execute(by_id).scalar_one()
        delivery = session.get(ProjectStatusDelivery, project.id)
        assert delivery is not None
        delivery.attempt_count = attempts
        delivery.last_error_code = error_code
        delivery.last_http_status = 500 if error_code == HTTP_STATUS else None
        delivery.delivered_version = delivered_version
        delivery.last_attempt_at = NOW
        session.commit()


def status_webhook(factory: sessionmaker[Session]) -> AlertResult:
    with factory() as session:
        return alerts.status_webhook(session, AlertContext(now=NOW, currencies=("USD",)))


def deliver_once(factory: sessionmaker[Session]) -> None:
    """Claim the one due row and settle it as a 2xx through the real delivery code."""
    claim = claim_next(factory, lease_seconds=60, clock=lambda: NOW)
    assert claim is not None
    written = settle(
        factory,
        claim,
        Attempt(error_code=None, http_status=200),
        retry_base_seconds=30,
        retry_cap_seconds=3600,
        clock=lambda: NOW,
    )
    assert written


# --- OK -------------------------------------------------------------------------------


def test_status_webhook_is_ok_on_an_empty_database(factory: sessionmaker[Session]) -> None:
    assert status_webhook(factory) == NO_PROBLEM


def test_status_webhook_is_ok_with_a_healthy_pushing_project(
    factory: sessionmaker[Session],
) -> None:
    _tenant, project = make_project(factory)
    # Behind and retrying, but not 6 times yet; the secret is ACTIVE.
    failed(factory, project, STATUS_WEBHOOK_FAILING_ATTEMPTS - 1)

    assert status_webhook(factory) == NO_PROBLEM


# --- 告警范围（设计 §7） -------------------------------------------------------------


def test_status_webhook_counts_only_enabled_projects_with_an_address(
    factory: sessionmaker[Session],
) -> None:
    _tenant, counted = make_project(factory)
    # Neither of these pushes: no secret, failed 6 times, even a signing error — none counts.
    _tenant, disabled = make_project(
        factory, integration_status=IntegrationStatus.DISABLED, secret=None
    )
    _tenant, no_address = make_project(factory, url=None, secret=None)
    failed(factory, counted, 6)
    failed(factory, disabled, 6, error_code=SECRET_DECRYPT_FAILED)
    failed(factory, no_address, 6, error_code=ENCRYPTION_NOT_CONFIGURED)

    assert status_webhook(factory) == AlertResult(
        P2, "failing 1, missing secret 0, signing error 0"
    )

    # A project without an ACTIVE secret: P2, counted from the data.
    make_project(factory, secret=WebhookSecretStatus.RETIRED)
    assert status_webhook(factory) == AlertResult(
        P2, "failing 1, missing secret 1, signing error 0"
    )

    # A signing error makes the line P1; the other two counts stay in the summary.
    _tenant, broken = make_project(factory)
    failed(factory, broken, 1, error_code=SECRET_DECRYPT_FAILED)
    assert status_webhook(factory) == AlertResult(
        P1, "failing 1, missing secret 1, signing error 1"
    )


def test_a_delivery_clears_the_signing_error(factory: sessionmaker[Session]) -> None:
    _tenant, broken = make_project(factory)
    failed(factory, broken, 3, error_code=SECRET_DECRYPT_FAILED)
    assert status_webhook(factory) == AlertResult(
        P1, "failing 0, missing secret 0, signing error 1"
    )

    # A new secret was activated; the next attempt is delivered.
    deliver_once(factory)

    assert status_webhook(factory) == NO_PROBLEM


# --- P2 ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("attempts", "expected"),
    [
        (5, NO_PROBLEM),
        (6, AlertResult(P2, "failing 1, missing secret 0, signing error 0")),
        (10, AlertResult(P2, "failing 1, missing secret 0, signing error 0")),
    ],
)
def test_failing_starts_at_six_consecutive_failures(
    factory: sessionmaker[Session], attempts: int, expected: AlertResult
) -> None:
    assert STATUS_WEBHOOK_FAILING_ATTEMPTS == 6
    _tenant, project = make_project(factory)
    failed(factory, project, attempts, delivered_version=VERSION - 1)

    assert status_webhook(factory) == expected


def test_failing_and_signing_error_count_only_rows_behind(
    factory: sessionmaker[Session],
) -> None:
    _tenant, caught_up = make_project(factory)
    failed(factory, caught_up, 6, error_code=SECRET_DECRYPT_FAILED, delivered_version=VERSION)

    assert status_webhook(factory) == NO_PROBLEM


@pytest.mark.parametrize(
    "secret",
    [
        None,
        WebhookSecretStatus.PENDING,
        WebhookSecretStatus.RETIRED,
    ],
)
def test_missing_secret_is_derived_from_the_data(
    factory: sessionmaker[Session], secret: WebhookSecretStatus | None
) -> None:
    # Already delivered and no error code at all: the count still comes from the secrets.
    make_project(factory, secret=secret)

    assert status_webhook(factory) == AlertResult(
        P2, "failing 0, missing secret 1, signing error 0"
    )


def test_missing_secret_does_not_read_the_error_code(factory: sessionmaker[Session]) -> None:
    # `NO_ACTIVE_SECRET` left over from before a secret was activated is not counted.
    _tenant, project = make_project(factory)
    failed(factory, project, 1, error_code=NO_ACTIVE_SECRET)

    assert status_webhook(factory) == NO_PROBLEM


# --- P1 ---------------------------------------------------------------------------------


@pytest.mark.parametrize("error_code", [SECRET_DECRYPT_FAILED, ENCRYPTION_NOT_CONFIGURED])
def test_a_signing_error_is_p1(factory: sessionmaker[Session], error_code: str) -> None:
    _tenant, project = make_project(factory)
    failed(factory, project, 1, error_code=error_code)

    assert status_webhook(factory) == AlertResult(
        P1, "failing 0, missing secret 0, signing error 1"
    )


def test_the_line_carries_no_tenant_or_project_id(factory: sessionmaker[Session]) -> None:
    tenant, project = make_project(factory, secret=None)
    failed(factory, project, 6, error_code=ENCRYPTION_NOT_CONFIGURED)

    body = alerts.alert_lines(factory, ("USD",), clock=lambda: NOW)

    line = "status_webhook P1 failing 1, missing secret 1, signing error 1\n"
    assert body.splitlines()[-2] == line.rstrip("\n")
    assert body.splitlines()[-1] == (
        "ai_usage_outbox OK dead 0, held 0, review required 0, oldest pending 0 seconds"
    )
    assert tenant not in body
    assert project not in body
