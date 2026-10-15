"""Read-only access decisions for the AI messages request path."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
from dataclasses import dataclass

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.ai_api.models import AiClientKey, AiClientKeyStatus
from app.models.ai_catalog import AiModel, AiModelAlias, AiProvider, CatalogStatus
from app.models.tenancy import EffectiveStatus, Project, Tenant, compose_effective_status


@dataclass(frozen=True)
class AccessDecision:
    internal_tenant_id: int
    internal_project_id: int
    tenant_id: str
    project_id: str
    model_code: str


class AuthenticationFailed(Exception):
    pass


class ServiceBlocked(Exception):
    pass


class ModelNotAllowed(Exception):
    pass


def _token_from_header(value: str | None) -> str:
    if value is None or not value.startswith("Bearer "):
        raise AuthenticationFailed
    token = value.removeprefix("Bearer ")
    if not token:
        raise AuthenticationFailed
    return token


def resolve_access(
    factory: sessionmaker[Session], *, client_id: str | None, authorization: str | None, model: str
) -> AccessDecision:
    """Authenticate, then inspect status and the Anthropic catalog in one read transaction."""
    token = _token_from_header(authorization)
    if not client_id:
        raise AuthenticationFailed
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    with factory() as session:
        key = session.scalar(
            select(AiClientKey).where(
                AiClientKey.client_id == client_id,
                AiClientKey.status == AiClientKeyStatus.ACTIVE,
            )
        )
        if key is None or not hmac.compare_digest(digest, key.token_sha256):
            raise AuthenticationFailed
        project = session.get(Project, key.project_id)
        tenant = session.get(Tenant, key.tenant_id)
        if project is None or tenant is None or project.tenant_id != tenant.id:
            raise AuthenticationFailed
        effective, _reason = compose_effective_status(
            tenant.account_status,
            tenant.billing_status,
            tenant.billing_mode,
            project.integration_status,
        )
        if effective is EffectiveStatus.BLOCK_AI:
            raise ServiceBlocked

        direct = session.scalar(
            select(AiModel.code)
            .join(AiProvider, AiProvider.id == AiModel.provider_id)
            .where(
                AiProvider.code == "anthropic",
                AiModel.code == model,
                AiModel.status == CatalogStatus.ACTIVE,
            )
        )
        if direct is None:
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
                    AiModelAlias.alias == model,
                    AiModel.status == CatalogStatus.ACTIVE,
                    or_(AiModelAlias.effective_from.is_(None), AiModelAlias.effective_from <= now),
                    or_(AiModelAlias.effective_to.is_(None), AiModelAlias.effective_to > now),
                )
            )
            if alias is None:
                raise ModelNotAllowed
            direct = alias
        return AccessDecision(
            key.tenant_id, key.project_id, tenant.public_id, project.public_id, direct
        )
