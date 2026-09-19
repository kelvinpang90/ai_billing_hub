"""Request and response shapes for admin customer management (design gate #96 v3 §2).

⚠️ **响应模型是字段白名单，不直接序列化 ORM 对象**（与 app/schemas/auth.py 同一条）。
内部自增 `id`、`low_balance_threshold`，以及以后给 `tenants` / `projects` 加的任何列，
都不会因为模型多了一列而**静默**出现在响应里（INV-7、INV-8）。对外的 `id` 就是
`public_id`。

⚠️ 余额是字符串，不是数字（INV-10）：先 quantize 到 8 位再 `format(value, "f")`。
刚建好的钱包在内存里是 `Decimal(0)`，不先 quantize 会输出 `"0"`，与之后从库里读回的
`"0.00000000"` 不一致。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    EmailStr,
    StringConstraints,
    field_validator,
)

from app.models.base import quantize_money
from app.models.tenancy import Project, Tenant
from app.models.wallet import Wallet

# 分页边界（spec §108；设计 §2）。超出范围是 422，不静默截断。`page` 的上限防止
# OFFSET 溢出变成 500。
MAX_PAGE = 10_000
MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20

# 与 `tenants.email` 的列宽一致。
_EMAIL_LENGTH = 320


def _blank_to_none(value: object) -> object:
    """可空文本：去掉首尾空白，剩下空串就存成 NULL。"""
    if isinstance(value, str):
        return value.strip() or None
    return value


# 必填名称：去掉首尾空白后长度 1–255。
_Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
_ContactName = Annotated[
    Annotated[str, StringConstraints(max_length=255)] | None, BeforeValidator(_blank_to_none)
]
_Phone = Annotated[
    Annotated[str, StringConstraints(max_length=32)] | None, BeforeValidator(_blank_to_none)
]
_Description = Annotated[
    Annotated[str, StringConstraints(max_length=1000)] | None, BeforeValidator(_blank_to_none)
]


class CreateCustomerRequest(BaseModel):
    """⚠️ 多余字段一律拒绝：请求体不能指定 `public_id`、`billing_status`、余额或任何 id。"""

    model_config = ConfigDict(extra="forbid")

    company_name: _Name
    email: EmailStr
    contact_name: _ContactName = None
    phone: _Phone = None

    @field_validator("email", mode="before")
    @classmethod
    def _email_fits_the_column(cls, value: object) -> object:
        if isinstance(value, str) and len(value) > _EMAIL_LENGTH:
            # 422 只列字段名，不回显这个值。
            raise ValueError("email is too long")
        return value


class CreateProjectRequest(BaseModel):
    """⚠️ 项目归属只来自路径里的客户，请求体里的 `tenant_id` 之类一律 422（INV-8）。"""

    model_config = ConfigDict(extra="forbid")

    name: _Name
    description: _Description = None


class WalletView(BaseModel):
    currency: str
    # 恰好 8 位小数的定点字符串，永远不是浮点数。
    balance: str
    version: int


class CustomerSummary(BaseModel):
    """列表项。详情另附 `wallet`。"""

    id: str
    company_name: str
    contact_name: str | None
    email: str
    phone: str | None
    billing_status: str
    status_version: int
    created_at: dt.datetime
    updated_at: dt.datetime


class CustomerDetail(CustomerSummary):
    wallet: WalletView


class ProjectView(BaseModel):
    id: str
    name: str
    description: str | None
    created_at: dt.datetime
    updated_at: dt.datetime


class Page[T](BaseModel):
    """spec §108 的传统分页：小数据集，`total` 一条 COUNT 即可。"""

    items: list[T]
    page: int
    page_size: int
    total: int


def money_text(value: Decimal) -> str:
    """`Decimal` → 恰好 8 位小数的字符串，如 `"0.00000000"`。"""
    return format(quantize_money(value), "f")


def _customer_fields(tenant: Tenant) -> dict[str, Any]:
    return {
        "id": tenant.public_id,
        "company_name": tenant.company_name,
        "contact_name": tenant.contact_name,
        "email": tenant.email,
        "phone": tenant.phone,
        "billing_status": tenant.billing_status.value,
        "status_version": tenant.status_version,
        "created_at": tenant.created_at,
        "updated_at": tenant.updated_at,
    }


def customer_summary(tenant: Tenant) -> CustomerSummary:
    return CustomerSummary(**_customer_fields(tenant))


def customer_detail(tenant: Tenant, wallet: Wallet) -> CustomerDetail:
    return CustomerDetail(
        **_customer_fields(tenant),
        wallet=WalletView(
            currency=wallet.currency,
            balance=money_text(wallet.balance),
            version=wallet.version,
        ),
    )


def project_view(project: Project) -> ProjectView:
    return ProjectView(
        id=project.public_id,
        name=project.name,
        description=project.description,
        created_at=project.created_at,
        updated_at=project.updated_at,
    )
