"""Customer portal endpoints (design gate #368 v12 §2「客户端」, §6).

契约见 [docs/api.md](../../docs/api.md)「客户门户认证」。

⚠️ **每个处理函数的第一条语句是 `require_customer(request)`**（写接口用
`require_customer_writable(request)`）。与管理端的 `require_admin` 一样是显式调用，不挂路由器
依赖；漏调由 `tests/backend/test_customer_api.py` 的路由守卫用例兜底。

⚠️ **租户只取自 `CustomerPrincipal.tenant_id`**（INV-8、spec §90）：这里的路径、查询串、请求体都
没有租户参数，带了也不读。

⚠️ 以后的客户写接口用 `require_customer_writable`，并且**必须**在自己的写事务里、写入之前调用
`app.services.account_status.lock_tenant_for_customer_write`：依赖只是提前拒绝
（设计 §2「依赖」）。
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.api.auth import require_customer, require_session_factory
from app.core.logging import current_request_id
from app.models.tenancy import Tenant
from app.schemas.customer import CustomerMe
from app.schemas.envelope import ApiResponse, success
from app.services.account_status import READ_ONLY_STATUSES
from app.services.customers import CustomerNotFound

router = APIRouter(prefix="/api/v1/customer", tags=["customer"])


@router.get("/me", response_model=ApiResponse[CustomerMe])
def customer_me(request: Request) -> ApiResponse[CustomerMe]:
    """The caller's own customer: `{customer_id, company_name, account_status, read_only}`.

    四种账户状态都能读：只读限制在写接口那一层（AIH-TASK-020 的契约）。
    """
    principal = require_customer(request)
    with require_session_factory(request)() as session:
        tenant = session.get(Tenant, principal.tenant_id)
        if tenant is None:
            # 外键保证不会发生。
            raise CustomerNotFound
        view = CustomerMe(
            customer_id=tenant.public_id,
            company_name=tenant.company_name,
            account_status=tenant.account_status,
            # 与 `require_customer_writable` 同一个集合：两处对「只读」的判定不会对不上。
            read_only=tenant.account_status in READ_ONLY_STATUSES,
        )
    return success(view, request_id=current_request_id())
