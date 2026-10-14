"""Read-only authorization, status, and model-catalog access for AI requests."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.ai_api.models import AiClientKey, AiClientKeyStatus
from app.models.ai_catalog import AiModel, AiModelAlias, AiProvider, CatalogStatus
from app.models.tenancy import EffectiveStatus, Project, Tenant, compose_effective_status


@dataclass(frozen=True)
class AuthorizedRequest:
    internal_tenant_id: int
    internal_project_id: int
    tenant_public_id: str
    project_public_id: str
    model_code: str


class AuthenticationFailed(Exception):
    pass


class ServiceBlocked(Exception):
    pass


class ModelNotAllowed(Exception):
    pass


def authorize_request(
    factory: sessionmaker[Session], *, client_id: str | None, token: str | None, model: str
) -> AuthorizedRequest:
    """Perform the design-mandated auth -> state -> model sequence in one read transaction."""
    if not client_id or not token:
        raise AuthenticationFailed
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with factory() as session:
        key = session.scalar(
            select(AiClientKey).where(
                AiClientKey.client_id == client_id,
                AiClientKey.status == AiClientKeyStatus.ACTIVE,
            )
        )
        if key is None or not hmac.compare_digest(digest, key.token_sha256):
            raise AuthenticationFailed
        tenant = session.get(Tenant, key.tenant_id)
        project = session.get(Project, key.project_id)
        if tenant is None or project is None or project.tenant_id != tenant.id:
            raise AuthenticationFailed
        effective, _reason = compose_effective_status(
            tenant.account_status,
            tenant.billing_status,
            tenant.billing_mode,
            project.integration_status,
        )
        if effective is EffectiveStatus.BLOCK_AI:
            raise ServiceBlocked
        resolved = _resolve_active_anthropic_model(session, model)
        if resolved is None:
            raise ModelNotAllowed
        return AuthorizedRequest(
            internal_tenant_id=tenant.id,
            internal_project_id=project.id,
            tenant_public_id=tenant.public_id,
            project_public_id=project.public_id,
            model_code=resolved,
        )


def _resolve_active_anthropic_model(session: Session, requested: str) -> str | None:
    direct = session.scalar(
        select(AiModel.code)
        .join(AiProvider, AiProvider.id == AiModel.provider_id)
        .where(
            AiProvider.code == "anthropic",
            AiProvider.status == CatalogStatus.ACTIVE,
            AiModel.code == requested,
            AiModel.status == CatalogStatus.ACTIVE,
        )
    )
    if direct is not None:
        return direct
    now = datetime.now(UTC).replace(tzinfo=None)
    alias = session.scalar(
        select(AiModel.code)
        .join(AiProvider, AiProvider.id == AiModel.provider_id)
        .join(
            AiModelAlias,
            and_(
                AiModelAlias.provider_id == AiProvider.id,
                AiModelAlias.model_id == AiModel.id,
            ),
        )
        .where(
            AiProvider.code == "anthropic",
            AiProvider.status == CatalogStatus.ACTIVE,
            AiModel.status == CatalogStatus.ACTIVE,
            AiModelAlias.alias == requested,
            or_(AiModelAlias.effective_from.is_(None), AiModelAlias.effective_from <= now),
            or_(AiModelAlias.effective_to.is_(None), AiModelAlias.effective_to > now),
        )
    )
    return alias
