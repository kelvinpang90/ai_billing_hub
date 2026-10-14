"""Administrative AI client-key endpoints (design gate #281 v23 §2)."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from app.ai_api import admin as ai_admin
from app.api.auth import request_context, require_admin, require_session_factory
from app.core.logging import current_request_id
from app.schemas.ai_access import AiClientKeyView, IssuedAiClientKeyView
from app.schemas.envelope import ApiResponse, success

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

_AI_ACCESS = "/customers/{customer_id}/projects/{project_id}/ai-access"


def _view(key: ai_admin.AiClientKeyView) -> AiClientKeyView:
    return AiClientKeyView(
        client_id=key.client_id,
        status=key.status,
        created_at=key.created_at,
        revoked_at=key.revoked_at,
    )


@router.get(_AI_ACCESS, response_model=ApiResponse[list[AiClientKeyView]])
def list_ai_client_keys(
    request: Request,
    customer_id: str,
    project_id: str,
) -> ApiResponse[list[AiClientKeyView]]:
    """Every key of the project, newest first; tokens are never returned."""
    require_admin(request)
    keys = ai_admin.list_client_keys(
        require_session_factory(request), customer_id=customer_id, project_id=project_id
    )
    return success([_view(key) for key in keys], request_id=current_request_id())


@router.post(
    _AI_ACCESS + "/client-keys",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[IssuedAiClientKeyView],
)
def issue_ai_client_key(
    request: Request,
    response: Response,
    customer_id: str,
    project_id: str,
) -> ApiResponse[IssuedAiClientKeyView]:
    """Issue a token once; callers must persist it before the response is discarded."""
    actor = require_admin(request)
    issued = ai_admin.issue_client_key(
        require_session_factory(request),
        actor=actor,
        customer_id=customer_id,
        project_id=project_id,
        context=request_context(request),
    )
    response.headers["Cache-Control"] = "no-store"
    return success(
        IssuedAiClientKeyView(
            client_id=issued.client_id,
            token=issued.token,
            status=issued.status,
            created_at=issued.created_at,
        ),
        request_id=current_request_id(),
    )


@router.post(
    _AI_ACCESS + "/client-keys/{client_id}/revoke",
    response_model=ApiResponse[AiClientKeyView],
)
def revoke_ai_client_key(
    request: Request,
    customer_id: str,
    project_id: str,
    client_id: str,
) -> ApiResponse[AiClientKeyView]:
    """Revoke one active client key; a revoked key is a terminal state."""
    actor = require_admin(request)
    revoked = ai_admin.revoke_client_key(
        require_session_factory(request),
        actor=actor,
        customer_id=customer_id,
        project_id=project_id,
        client_id=client_id,
        context=request_context(request),
    )
    return success(_view(revoked), request_id=current_request_id())
