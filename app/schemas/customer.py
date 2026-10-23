"""Response shapes for the customer portal endpoints (design gate #368 v12 §2, §6).

⚠️ **字段白名单**，与 app/schemas/customers.py 同一条：不直接序列化 ORM 对象。`/customer/me`
恰好四个键 —— 没有金额、成本、毛利、内部 id、`billing_mode`（INV-7、INV-8）。以后给
`tenants` 加列，不会因此**静默**出现在客户能看到的响应里。
"""

from __future__ import annotations

from pydantic import BaseModel

from app.models.tenancy import AccountStatus


class CustomerMe(BaseModel):
    """`GET /api/v1/customer/me`。`customer_id` 是租户的 `public_id`。"""

    customer_id: str
    company_name: str
    account_status: AccountStatus
    # `DISABLED` / `CLOSED` 时为 true：客户仍能登录、能读，写接口一律 403 `ACCOUNT_READ_ONLY`。
    read_only: bool
