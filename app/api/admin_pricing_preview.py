"""Admin pricing preview: one read-only calculation (design gate #179 v1 §2「试算接口」, spec §59).

契约见 [docs/api.md](../../docs/api.md)「管理端试算预览」。计算口径见
[docs/pricing-engine.md](../../docs/pricing-engine.md)「计算」。

⚠️ **处理函数的第一条语句是 `require_admin(request)`**，与 app/api/admin_customers.py
同一条：鉴权是显式调用，不挂在路由器上。`tests/backend/test_admin_customers_api.py`
从应用的路由表枚举 `/api/v1/admin` 下的全部路由逐个验 401 / 403 且不写库；
`tests/backend/test_pricing_preview_api.py` 用 AST 钉住「第一条语句」。

⚠️ 响应里有供应商成本、汇率与倍数：只在管理端（INV-7 / REQ-PRIV-002），没有任何
客户或集成侧的试算。

⚠️ **只读**：不写库、不写审计（它不改变任何东西）。与计费同一套解析与计算代码，
只差 `locking`。错误状态（模型未知、缺价格 / 汇率 / 规则）也是 200 并给出已解析到的
部分；只有请求本身不合法才 4xx。这一层不写日志。
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.api.auth import require_admin, require_session_factory
from app.core.logging import current_request_id
from app.schemas.envelope import ApiResponse, success
from app.schemas.pricing_preview import PricingPreviewRequest, PricingPreviewView
from app.services import pricing_engine

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


@router.post("/pricing-preview", response_model=ApiResponse[PricingPreviewView])
def preview_pricing(
    request: Request, payload: PricingPreviewRequest
) -> ApiResponse[PricingPreviewView]:
    """§59 preview calculation at an explicit `occurred_at`: every component and version."""
    require_admin(request)
    preview = pricing_engine.preview_charge(require_session_factory(request), payload)
    return success(preview, request_id=current_request_id())
