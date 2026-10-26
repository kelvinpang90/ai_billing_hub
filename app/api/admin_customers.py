"""Admin customer management endpoints (spec §56, §124; design gate #96 v3 §2).

契约见 [docs/api.md](../../docs/api.md)。

⚠️ **每个处理函数的第一件事是 `require_admin(request)`。**鉴权是显式调用，不是挂在
路由器上的依赖（设计 §9，与 `require_current_user` 的用法一致）。漏调一个就是一个
谁都能调的管理端接口 —— `tests/backend/test_admin_customers_api.py` 从应用的路由表
枚举本前缀下的全部路由，逐个验 401 / 403 且不写库；新增接口不补进那里，测试会红。

⚠️ 请求体与查询参数由 FastAPI 在处理函数**之前**校验，所以没带令牌、参数又不合法的
请求拿到的是 422 而不是 401。422 只列字段名、不回显值（app/core/errors.py）。

这一层不记请求体，也不记 `email` / `contact_name` / `phone`（REQ-PRIV-001），调账的
原因文本同样不记（设计闸门 #111 v1 §6）。

集成 API 凭据的五个接口（AIH-TASK-012，设计闸门 #118 v1 §2）：`secret` 只出现在建凭据与
轮换的 201 响应里；五个响应都带 `Cache-Control: no-store`，中间缓存与浏览器都不留副本。

出站 webhook 签名密钥的四个接口（AIH-TASK-019，设计闸门 #135 v1 §2）同理：`secret` 只出现在
签发的 201 响应里，四个响应都带 `Cache-Control: no-store`。

改账户状态（AIH-TASK-020，设计闸门 #136 v2 §2）：目标只有 `ENABLED` / `DISABLED`，原因只进
审计，这一层不记。

项目集成状态与状态 webhook 地址（AIH-TASK-050，设计闸门 #241 v6 §2「接口」）：规则全在
`app/services/project_integration.py`，这一层只做鉴权、请求体形状与转发。完整地址只出现在
`…/status-webhook` 的 PUT 与 GET 响应里，这一层不记（路径里可能带集成方的令牌）。

钱包流水（AIH-TASK-084，设计闸门 #351 v2 §2「接口」）：只读，一个普通会话两条 SELECT（找客户、
取一页），不提交、不写审计。没有服务层：这一层直接调 repository，与用量事件查询的写法一致。

低余额阈值（AIH-TASK-109，设计闸门 #433 v2 §2「接口一」「接口二」）：GET 只读、不写审计；
PUT 的锁、比较与审计全在 `app/services/customers.py`，这一层只做鉴权、请求体形状与转发。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query, Request, Response, status

from app.api.auth import request_context, require_admin, require_session_factory
from app.core.logging import current_request_id
from app.models.tenancy import AccountStatus, IntegrationStatus
from app.models.wallet import ReferenceType
from app.repositories import wallet as wallet_repository
from app.repositories.tenancy import get_tenant_by_public_id
from app.schemas.customers import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE,
    MAX_PAGE_SIZE,
    ChangeAccountStatusRequest,
    ChangeIntegrationStatusRequest,
    CreateCustomerRequest,
    CreateProjectRequest,
    CustomerDetail,
    CustomerSummary,
    EnableInternalMeteringRequest,
    LowBalanceThresholdView,
    Page,
    ProjectView,
    SetLowBalanceThresholdRequest,
    SetStatusWebhookRequest,
    StatusWebhookSettings,
    UpdateCustomerRequest,
)
from app.schemas.envelope import ApiResponse, success
from app.schemas.integration_access import (
    CreateCredentialRequest,
    CredentialView,
    IssuedCredentialView,
    RevokeCredentialRequest,
    RotateCredentialRequest,
)
from app.schemas.wallet_adjustments import AdjustmentView, CreateAdjustmentRequest
from app.schemas.wallet_transactions import (
    MAX_WALLET_SEQUENCE,
    WalletTransactionPage,
    wallet_transaction_page,
)
from app.schemas.webhook_signing import (
    ActivateWebhookSecretRequest,
    IssuedWebhookSecretView,
    IssueWebhookSecretRequest,
    RetireWebhookSecretRequest,
    WebhookSecretView,
)
from app.services import (
    account_status,
    customers,
    integration_access,
    internal_billing,
    project_integration,
    wallet_adjustments,
    webhook_signing,
)

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

# 超出范围是 422，不静默截断（spec §108）。
PageNumber = Annotated[int, Query(ge=1, le=MAX_PAGE)]
PageSize = Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)]
BeforeSequence = Annotated[int | None, Query(ge=1, le=MAX_WALLET_SEQUENCE)]


@router.post(
    "/customers",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[CustomerDetail],
)
def create_customer(
    request: Request, payload: CreateCustomerRequest
) -> ApiResponse[CustomerDetail]:
    """Create a customer; its MYR wallet and the audit row commit with it."""
    admin = require_admin(request)
    detail = customers.create_customer(
        require_session_factory(request),
        actor=admin,
        company_name=payload.company_name,
        email=payload.email,
        contact_name=payload.contact_name,
        phone=payload.phone,
        context=request_context(request),
    )
    return success(detail, request_id=current_request_id())


@router.get("/customers", response_model=ApiResponse[Page[CustomerSummary]])
def list_customers(
    request: Request, page: PageNumber = 1, page_size: PageSize = DEFAULT_PAGE_SIZE
) -> ApiResponse[Page[CustomerSummary]]:
    """Newest first."""
    require_admin(request)
    listing = customers.list_customers(
        require_session_factory(request), page=page, page_size=page_size
    )
    return success(listing, request_id=current_request_id())


@router.get("/customers/{customer_id}", response_model=ApiResponse[CustomerDetail])
def get_customer(request: Request, customer_id: str) -> ApiResponse[CustomerDetail]:
    """`customer_id` is the customer's `public_id`; anything else is a 404."""
    require_admin(request)
    detail = customers.get_customer(require_session_factory(request), customer_id)
    return success(detail, request_id=current_request_id())


@router.patch("/customers/{customer_id}", response_model=ApiResponse[CustomerDetail])
def update_customer(
    request: Request, customer_id: str, payload: UpdateCustomerRequest
) -> ApiResponse[CustomerDetail]:
    """Partial profile edit; the row and its `CUSTOMER_UPDATE` audit commit together."""
    admin = require_admin(request)
    detail = customers.update_customer(
        require_session_factory(request),
        actor=admin,
        customer_id=customer_id,
        changes=payload.changes(),
        context=request_context(request),
    )
    return success(detail, request_id=current_request_id())


@router.get(
    "/customers/{customer_id}/low-balance-threshold",
    response_model=ApiResponse[LowBalanceThresholdView],
)
def get_low_balance_threshold(
    request: Request, customer_id: str
) -> ApiResponse[LowBalanceThresholdView]:
    """The stored threshold, or `null` when unset. Read-only, no audit."""
    require_admin(request)
    view = customers.get_low_balance_threshold(require_session_factory(request), customer_id)
    return success(view, request_id=current_request_id())


@router.put(
    "/customers/{customer_id}/low-balance-threshold",
    response_model=ApiResponse[LowBalanceThresholdView],
)
def set_low_balance_threshold(
    request: Request, customer_id: str, payload: SetLowBalanceThresholdRequest
) -> ApiResponse[LowBalanceThresholdView]:
    """Set (a string) or clear (`null`) the threshold; the same value writes nothing.

    客户只来自路径（INV-8）。不改余额、不写账本与出站事件、不补发 `tenant.low_balance`。
    """
    admin = require_admin(request)
    view = customers.set_low_balance_threshold(
        require_session_factory(request),
        actor=admin,
        customer_id=customer_id,
        threshold=payload.low_balance_threshold,
        context=request_context(request),
    )
    return success(view, request_id=current_request_id())


@router.post(
    "/customers/{customer_id}/account-status",
    response_model=ApiResponse[CustomerDetail],
)
def change_account_status(
    request: Request, customer_id: str, payload: ChangeAccountStatusRequest
) -> ApiResponse[CustomerDetail]:
    """ENABLED ↔ DISABLED. Already at the target: 200 and nothing is written."""
    admin = require_admin(request)
    detail = account_status.change_account_status(
        require_session_factory(request),
        actor=admin,
        customer_id=customer_id,
        account_status=AccountStatus(payload.account_status),
        reason=payload.reason,
        context=request_context(request),
    )
    return success(detail, request_id=current_request_id())


@router.post(
    "/customers/{customer_id}/internal-metering",
    response_model=ApiResponse[CustomerDetail],
)
def enable_internal_metering(
    request: Request, customer_id: str, payload: EnableInternalMeteringRequest
) -> ApiResponse[CustomerDetail]:
    """One-way internal mode, only before usage or wallet activity; admin and audit required."""
    admin = require_admin(request)
    detail = internal_billing.enable_internal_metering(
        require_session_factory(request),
        actor=admin,
        customer_id=customer_id,
        reason=payload.reason,
        context=request_context(request),
    )
    return success(detail, request_id=current_request_id())


@router.post(
    "/customers/{customer_id}/projects",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[ProjectView],
)
def create_project(
    request: Request, customer_id: str, payload: CreateProjectRequest
) -> ApiResponse[ProjectView]:
    """The project belongs to the customer in the path, never to anything in the body."""
    admin = require_admin(request)
    project = customers.create_project(
        require_session_factory(request),
        actor=admin,
        customer_id=customer_id,
        name=payload.name,
        description=payload.description,
        context=request_context(request),
    )
    return success(project, request_id=current_request_id())


@router.get("/customers/{customer_id}/projects", response_model=ApiResponse[Page[ProjectView]])
def list_projects(
    request: Request,
    customer_id: str,
    page: PageNumber = 1,
    page_size: PageSize = DEFAULT_PAGE_SIZE,
) -> ApiResponse[Page[ProjectView]]:
    """Oldest first, only this customer's projects."""
    require_admin(request)
    listing = customers.list_projects(
        require_session_factory(request),
        customer_id=customer_id,
        page=page,
        page_size=page_size,
    )
    return success(listing, request_id=current_request_id())


@router.post(
    "/customers/{customer_id}/wallet/adjustments",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[AdjustmentView],
)
def post_wallet_adjustment(
    request: Request, response: Response, customer_id: str, payload: CreateAdjustmentRequest
) -> ApiResponse[AdjustmentView]:
    """Post a manual adjustment: 201 the first time, 200 when the key is replayed.

    客户只来自路径，操作者只来自令牌（设计闸门 #111 v1 §2）。
    """
    admin = require_admin(request)
    adjustment = wallet_adjustments.post_adjustment(
        require_session_factory(request),
        actor=admin,
        customer_id=customer_id,
        transaction_type=payload.transaction_type,
        amount=payload.amount,
        reason=payload.reason,
        idempotency_key=payload.idempotency_key,
        context=request_context(request),
    )
    if adjustment.replayed:
        # 重放不入账：200 加 `replayed: true`，调用方看得出这次没有新的财务效果。
        response.status_code = status.HTTP_200_OK
    return success(adjustment, request_id=current_request_id())


@router.get(
    "/customers/{customer_id}/wallet/transactions",
    response_model=ApiResponse[WalletTransactionPage],
)
def list_wallet_transactions(
    request: Request,
    customer_id: str,
    page_size: PageSize = DEFAULT_PAGE_SIZE,
    before_sequence: BeforeSequence = None,
    reference_type: ReferenceType | None = None,
) -> ApiResponse[WalletTransactionPage]:
    """Newest first by `wallet_sequence`; pass `next_before_sequence` back for the next page.

    租户只来自路径（INV-8）；游标是本租户内的序号，拿别的客户的序号来也只会翻到本客户的行。
    """
    require_admin(request)
    with require_session_factory(request)() as session:
        tenant = get_tenant_by_public_id(session, customer_id)
        if tenant is None:
            raise customers.CustomerNotFound
        # 多取一行，只用来判断还有没有下一页（设计 §2）。
        rows = wallet_repository.list_transactions_for_tenant(
            session,
            tenant.id,
            limit=page_size + 1,
            before_sequence=before_sequence,
            reference_type=reference_type,
        )
        page = wallet_transaction_page(rows, page_size=page_size)
    return success(page, request_id=current_request_id())


# --- 集成 API 凭据（AIH-TASK-012） ------------------------------------------------

_CREDENTIALS = "/customers/{customer_id}/projects/{project_id}/credentials"


def _no_store(response: Response) -> None:
    # secret 只显示一次：响应不许被任何一层缓存（设计 §6）。
    response.headers["Cache-Control"] = "no-store"


@router.post(
    _CREDENTIALS,
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[IssuedCredentialView],
)
def create_credential(
    request: Request,
    response: Response,
    customer_id: str,
    project_id: str,
    payload: CreateCredentialRequest,
) -> ApiResponse[IssuedCredentialView]:
    """A new `api_key` at version 1; the `secret` is in this response and never again."""
    admin = require_admin(request)
    issued = integration_access.create_credential(
        require_session_factory(request),
        request.app.state.settings,
        actor=admin,
        customer_id=customer_id,
        project_id=project_id,
        context=request_context(request),
    )
    _no_store(response)
    return success(issued, request_id=current_request_id())


@router.get(_CREDENTIALS, response_model=ApiResponse[Page[CredentialView]])
def list_credentials(
    request: Request,
    response: Response,
    customer_id: str,
    project_id: str,
    page: PageNumber = 1,
    page_size: PageSize = DEFAULT_PAGE_SIZE,
) -> ApiResponse[Page[CredentialView]]:
    """Every version of every key of the project. Never a `secret`, never ciphertext."""
    require_admin(request)
    listing = integration_access.list_credentials(
        require_session_factory(request),
        customer_id=customer_id,
        project_id=project_id,
        page=page,
        page_size=page_size,
    )
    _no_store(response)
    return success(listing, request_id=current_request_id())


@router.post(
    _CREDENTIALS + "/{api_key}/rotate",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[IssuedCredentialView],
)
def rotate_credential(
    request: Request,
    response: Response,
    customer_id: str,
    project_id: str,
    api_key: str,
    payload: RotateCredentialRequest,
) -> ApiResponse[IssuedCredentialView]:
    """Same `api_key`, next version, a new `secret` shown once; older versions overlap."""
    admin = require_admin(request)
    issued = integration_access.rotate_credential(
        require_session_factory(request),
        request.app.state.settings,
        actor=admin,
        customer_id=customer_id,
        project_id=project_id,
        api_key=api_key,
        current_key_version=payload.current_key_version,
        context=request_context(request),
    )
    _no_store(response)
    return success(issued, request_id=current_request_id())


@router.post(
    _CREDENTIALS + "/{api_key}/versions/{key_version}/revoke",
    response_model=ApiResponse[CredentialView],
)
def revoke_credential_version(
    request: Request,
    response: Response,
    customer_id: str,
    project_id: str,
    api_key: str,
    key_version: int,
    payload: RevokeCredentialRequest,
) -> ApiResponse[CredentialView]:
    """Revoke one version at once. Idempotent: already revoked is 200 and writes nothing."""
    admin = require_admin(request)
    revoked = integration_access.revoke_version(
        require_session_factory(request),
        actor=admin,
        customer_id=customer_id,
        project_id=project_id,
        api_key=api_key,
        key_version=key_version,
        reason=payload.reason,
        context=request_context(request),
    )
    _no_store(response)
    return success(revoked, request_id=current_request_id())


@router.post(
    _CREDENTIALS + "/{api_key}/revoke",
    response_model=ApiResponse[list[CredentialView]],
)
def revoke_credential(
    request: Request,
    response: Response,
    customer_id: str,
    project_id: str,
    api_key: str,
    payload: RevokeCredentialRequest,
) -> ApiResponse[list[CredentialView]]:
    """Revoke every version of the key; the response lists all of them, oldest first."""
    admin = require_admin(request)
    revoked = integration_access.revoke_key(
        require_session_factory(request),
        actor=admin,
        customer_id=customer_id,
        project_id=project_id,
        api_key=api_key,
        reason=payload.reason,
        context=request_context(request),
    )
    _no_store(response)
    return success(revoked, request_id=current_request_id())


# --- 出站 webhook 签名密钥（AIH-TASK-019） ---------------------------------------------

_WEBHOOK_SECRETS = "/customers/{customer_id}/projects/{project_id}/webhook-secrets"

# 正整数，INT 列的上界；`0`、负数、非整数都是 422（设计 §2、§7「边界值」）。
WebhookKeyVersion = Annotated[int, Path(ge=1, le=2**31 - 1)]


@router.post(
    _WEBHOOK_SECRETS,
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[IssuedWebhookSecretView],
)
def issue_webhook_secret(
    request: Request,
    response: Response,
    customer_id: str,
    project_id: str,
    payload: IssueWebhookSecretRequest,
) -> ApiResponse[IssuedWebhookSecretView]:
    """A new PENDING version; the `secret` is in this response and never again."""
    admin = require_admin(request)
    issued = webhook_signing.issue_secret(
        require_session_factory(request),
        request.app.state.settings,
        actor=admin,
        customer_id=customer_id,
        project_id=project_id,
        context=request_context(request),
    )
    _no_store(response)
    return success(issued, request_id=current_request_id())


@router.get(_WEBHOOK_SECRETS, response_model=ApiResponse[Page[WebhookSecretView]])
def list_webhook_secrets(
    request: Request,
    response: Response,
    customer_id: str,
    project_id: str,
    page: PageNumber = 1,
    page_size: PageSize = DEFAULT_PAGE_SIZE,
) -> ApiResponse[Page[WebhookSecretView]]:
    """Every version of the project, `key_version` ascending. Never a `secret`."""
    require_admin(request)
    listing = webhook_signing.list_secrets(
        require_session_factory(request),
        customer_id=customer_id,
        project_id=project_id,
        page=page,
        page_size=page_size,
    )
    _no_store(response)
    return success(listing, request_id=current_request_id())


@router.post(
    _WEBHOOK_SECRETS + "/{key_version}/activate",
    response_model=ApiResponse[list[WebhookSecretView]],
)
def activate_webhook_secret(
    request: Request,
    response: Response,
    customer_id: str,
    project_id: str,
    key_version: WebhookKeyVersion,
    payload: ActivateWebhookSecretRequest,
) -> ApiResponse[list[WebhookSecretView]]:
    """PENDING → ACTIVE, the old ACTIVE → RETIRED at once. Already ACTIVE: 200, no write."""
    admin = require_admin(request)
    versions = webhook_signing.activate_secret(
        require_session_factory(request),
        actor=admin,
        customer_id=customer_id,
        project_id=project_id,
        key_version=key_version,
        context=request_context(request),
    )
    _no_store(response)
    return success(versions, request_id=current_request_id())


@router.post(
    _WEBHOOK_SECRETS + "/{key_version}/retire",
    response_model=ApiResponse[WebhookSecretView],
)
def retire_webhook_secret(
    request: Request,
    response: Response,
    customer_id: str,
    project_id: str,
    key_version: WebhookKeyVersion,
    payload: RetireWebhookSecretRequest,
) -> ApiResponse[WebhookSecretView]:
    """PENDING or ACTIVE → RETIRED. Idempotent: already retired is 200 and writes nothing."""
    admin = require_admin(request)
    retired = webhook_signing.retire_secret(
        require_session_factory(request),
        actor=admin,
        customer_id=customer_id,
        project_id=project_id,
        key_version=key_version,
        reason=payload.reason,
        context=request_context(request),
    )
    _no_store(response)
    return success(retired, request_id=current_request_id())


# --- 项目集成状态与状态 webhook 地址（AIH-TASK-050） -------------------------------------

_PROJECT = "/customers/{customer_id}/projects/{project_id}"


def _webhook_settings(view: project_integration.StatusWebhookView) -> StatusWebhookSettings:
    return StatusWebhookSettings(
        status_webhook_url=view.status_webhook_url,
        status_webhook_configured=view.status_webhook_configured,
    )


@router.post(_PROJECT + "/integration-status", response_model=ApiResponse[ProjectView])
def change_integration_status(
    request: Request,
    customer_id: str,
    project_id: str,
    payload: ChangeIntegrationStatusRequest,
) -> ApiResponse[ProjectView]:
    """ENABLED ↔ DISABLED. Already at the target: 200 and nothing is written; CLOSED: 409."""
    admin = require_admin(request)
    project = project_integration.change_integration_status(
        require_session_factory(request),
        actor=admin,
        customer_id=customer_id,
        project_id=project_id,
        integration_status=IntegrationStatus(payload.integration_status),
        reason=payload.reason,
        context=request_context(request),
    )
    return success(project, request_id=current_request_id())


@router.put(_PROJECT + "/status-webhook", response_model=ApiResponse[StatusWebhookSettings])
def set_status_webhook(
    request: Request,
    customer_id: str,
    project_id: str,
    payload: SetStatusWebhookRequest,
) -> ApiResponse[StatusWebhookSettings]:
    """Set (a string) or clear (`null`) the address; the same normalized value writes nothing."""
    admin = require_admin(request)
    view = project_integration.set_status_webhook(
        require_session_factory(request),
        actor=admin,
        customer_id=customer_id,
        project_id=project_id,
        url=payload.url,
        context=request_context(request),
    )
    return success(_webhook_settings(view), request_id=current_request_id())


@router.get(_PROJECT + "/status-webhook", response_model=ApiResponse[StatusWebhookSettings])
def get_status_webhook(
    request: Request, customer_id: str, project_id: str
) -> ApiResponse[StatusWebhookSettings]:
    """The stored address. Read-only, no audit."""
    require_admin(request)
    view = project_integration.get_status_webhook(
        require_session_factory(request), customer_id=customer_id, project_id=project_id
    )
    return success(_webhook_settings(view), request_id=current_request_id())
