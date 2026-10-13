"""Administrative AI client-key rules (design gate #281 v23 §2).

This module deliberately contains service functions only.  The HTTP routes are
introduced separately, so the secret token has no request-path or logging
surface here.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
from dataclasses import dataclass
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.ai_api.models import AiClientKey, AiClientKeyStatus
from app.core.errors import AppError
from app.models.auth import AuditAction, User
from app.models.tenancy import Project, Tenant
from app.services.auth import RequestContext, record_audit, utc_now

ENTITY_AI_CLIENT_KEY: Final = "ai_client_key"
CLIENT_ID_PREFIX: Final = "aic_"
TOKEN_PREFIX: Final = "ait_"
MAX_ACTIVE_KEYS: Final = 2


class CustomerNotFound(AppError):
    """The customer path component does not identify a customer."""

    def __init__(self) -> None:
        super().__init__("Customer not found.", code="CUSTOMER_NOT_FOUND", http_status=404)


class ProjectNotFound(AppError):
    """The project is absent or is not owned by the customer."""

    def __init__(self) -> None:
        super().__init__("Project not found.", code="PROJECT_NOT_FOUND", http_status=404)


class AiClientKeyNotFound(AppError):
    def __init__(self) -> None:
        super().__init__(
            "AI client key not found.", code="AI_CLIENT_KEY_NOT_FOUND", http_status=404
        )


class AiClientKeyLimit(AppError):
    def __init__(self) -> None:
        super().__init__(
            "The project already has two active AI client keys.",
            code="AI_CLIENT_KEY_LIMIT",
            http_status=409,
        )


class AiClientKeyRevoked(AppError):
    def __init__(self) -> None:
        super().__init__(
            "The AI client key is revoked.", code="AI_CLIENT_KEY_REVOKED", http_status=409
        )


@dataclass(frozen=True)
class AiClientKeyView:
    client_id: str
    status: str
    created_at: dt.datetime
    revoked_at: dt.datetime | None


@dataclass(frozen=True)
class IssuedAiClientKeyView:
    client_id: str
    token: str
    status: str
    created_at: dt.datetime


def _now() -> dt.datetime:
    return utc_now().replace(microsecond=0)


def _view(row: AiClientKey) -> AiClientKeyView:
    return AiClientKeyView(
        client_id=row.client_id,
        status=row.status.value,
        created_at=row.created_at,
        revoked_at=row.revoked_at,
    )


def _resolve_project(session: Session, customer_id: str, project_id: str) -> tuple[Tenant, Project]:
    tenant = session.execute(
        select(Tenant).where(Tenant.public_id == customer_id)
    ).scalar_one_or_none()
    if tenant is None:
        raise CustomerNotFound
    project = session.execute(
        select(Project).where(
            Project.tenant_id == tenant.id,
            Project.public_id == project_id,
        )
    ).scalar_one_or_none()
    if project is None:
        raise ProjectNotFound
    return tenant, project


def _locked_project(session: Session, customer_id: str, project_id: str) -> tuple[Tenant, Project]:
    tenant, project = _resolve_project(session, customer_id, project_id)
    locked = session.execute(
        select(Project).where(Project.id == project.id).with_for_update()
    ).scalar_one()
    return tenant, locked


def list_client_keys(
    session_factory: sessionmaker[Session], *, customer_id: str, project_id: str
) -> list[AiClientKeyView]:
    """Return every key, including revoked rows, newest first."""
    with session_factory.begin() as session:
        _tenant, project = _resolve_project(session, customer_id, project_id)
        rows = session.execute(
            select(AiClientKey)
            .where(AiClientKey.project_id == project.id)
            .order_by(AiClientKey.created_at.desc(), AiClientKey.id.desc())
        ).scalars()
        return [_view(row) for row in rows]


def issue_client_key(
    session_factory: sessionmaker[Session],
    *,
    actor: User,
    customer_id: str,
    project_id: str,
    context: RequestContext,
    now: dt.datetime | None = None,
) -> IssuedAiClientKeyView:
    """Issue one key while holding the project lock that protects the two-key limit."""
    moment = now or _now()
    with session_factory.begin() as session:
        tenant, project = _locked_project(session, customer_id, project_id)
        active = session.execute(
            select(func.count())
            .select_from(AiClientKey)
            .where(
                AiClientKey.project_id == project.id,
                AiClientKey.status == AiClientKeyStatus.ACTIVE,
            )
        ).scalar_one()
        if active >= MAX_ACTIVE_KEYS:
            raise AiClientKeyLimit

        client_id = CLIENT_ID_PREFIX + secrets.token_hex(16)
        token = TOKEN_PREFIX + secrets.token_hex(32)
        row = AiClientKey(
            tenant_id=tenant.id,
            project_id=project.id,
            client_id=client_id,
            token_sha256=hashlib.sha256(token.encode()).hexdigest(),
            status=AiClientKeyStatus.ACTIVE,
            created_at=moment,
            revoked_at=None,
        )
        session.add(row)
        record_audit(
            session,
            action=AuditAction.AI_CLIENT_KEY_CREATE,
            context=context,
            now=moment,
            actor=actor,
            entity_type=ENTITY_AI_CLIENT_KEY,
            entity_id=client_id,
            after_state={
                "client_id": client_id,
                "status": AiClientKeyStatus.ACTIVE.value,
            },
        )
        return IssuedAiClientKeyView(
            client_id=client_id,
            token=token,
            status=AiClientKeyStatus.ACTIVE.value,
            created_at=moment,
        )


def revoke_client_key(
    session_factory: sessionmaker[Session],
    *,
    actor: User,
    customer_id: str,
    project_id: str,
    client_id: str,
    context: RequestContext,
    now: dt.datetime | None = None,
) -> AiClientKeyView:
    """Revoke one active key and append its audit event in the same transaction."""
    moment = now or _now()
    with session_factory.begin() as session:
        _tenant, project = _resolve_project(session, customer_id, project_id)
        row = session.execute(
            select(AiClientKey)
            .where(AiClientKey.project_id == project.id, AiClientKey.client_id == client_id)
            .with_for_update()
        ).scalar_one_or_none()
        if row is None:
            raise AiClientKeyNotFound
        if row.status is AiClientKeyStatus.REVOKED:
            raise AiClientKeyRevoked

        before = {"client_id": row.client_id, "status": row.status.value}
        row.status = AiClientKeyStatus.REVOKED
        row.revoked_at = moment
        record_audit(
            session,
            action=AuditAction.AI_CLIENT_KEY_REVOKE,
            context=context,
            now=moment,
            actor=actor,
            entity_type=ENTITY_AI_CLIENT_KEY,
            entity_id=row.client_id,
            before_state=before,
            after_state={
                "client_id": row.client_id,
                "status": AiClientKeyStatus.REVOKED.value,
            },
        )
        return _view(row)
