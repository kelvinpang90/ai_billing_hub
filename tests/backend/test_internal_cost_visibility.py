"""INV-7 guard: customers never see internal cost, margin or gateway fee fields (AIH-TASK-222).

设计闸门 #709 v2 §3 INV-7、§6「客户侧禁止的字段」、§7「INV-7 守卫」。三项检查：

- `create_app(...)` 的 OpenAPI 里，每个不以 `/api/v1/admin/` 开头的路径的全部响应（`$ref` 递归展开
  组件）的属性名与禁用名单无交集。支付开关打开：客户建单路由只在开关打开时注册。只走 OpenAPI，
  不读 `app.routes`（子路由延迟挂载）；
- 客户侧与 AI 模块不导入毛利分析的仓储、schema 与接口模块（`ast` 扫描 import）；
- 客户的四个导出列常量不含禁用名。

Rebill（AIH-TASK-256，设计闸门 #830 v12 §3 INV-7、§6）：四个新路由与改动后的用量事件详情在管理端，
明细的 `corrected_estimated_provider_cost_myr` 只出现在它们的响应里、不出现在任何门户路由；
客户侧不导入 Rebill 的模块；经接口确认的一次 Rebill，账本行 metadata 只有 Rebill id 与事件数
（这一条连库，SQLite 与真 MySQL 各一遍；MySQL 那一半在 `BILLING_TEST_DATABASE_URL` 未设置时
skip —— 不要把 skipped 读成 passed，CI 设了它）。

另有两条自检：把禁用名注入假 schema / 假模块时，同一个检查函数报出它（防止守卫空转）。
"""

from __future__ import annotations

import ast
import datetime as dt
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import pytest
from rebill_world import A1, R_LOW, A, api_factory, build_world, rebill_api
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.main import create_app
from app.models.wallet import ReferenceType, WalletTransaction
from app.repositories.wallet import FORBIDDEN_METADATA_KEYS
from app.schemas.customer_conversations import CONVERSATION_EXPORT_COLUMNS
from app.schemas.customer_usage import USAGE_EXPORT_COLUMNS, USAGE_SUMMARY_EXPORT_COLUMNS
from app.schemas.customer_wallet import WALLET_EXPORT_COLUMNS

FORBIDDEN: Final = frozenset(
    {
        "estimated_provider_cost_myr",
        "provider_source_cost",
        "fx_rate_applied",
        "reference_customer_price",
        "gross_margin",
        "gross_margin_rate",
        "gross_profit",
        "gross_profit_rate",
        "gateway_fee",
        "gateway_fee_unknown_count",
        "cost_basis",
        "markup",
        # 供应商成本对账（AIH-TASK-245，设计闸门 #755 v1 §6；`fx_rate_applied` 已在上面）。
        "reconciled_cost_myr",
        "source_total",
        "source_variance",
        "source_variance_rate",
        "myr_variance",
        "myr_variance_rate",
        "estimated_source_cost",
        "estimated_cost_myr",
        # 毛利分析的对账后成本（AIH-TASK-248，设计闸门 #756 v2 §6「客户侧禁止的字段」）。
        "covered_estimated_cost_myr",
        "reconciled_provider_cost_myr",
        "provider_cost_myr",
        "provider_cost_basis",
        "gross_margin_on_provider_cost",
        "gross_margin_on_provider_cost_rate",
        "gross_profit_on_provider_cost",
        "gross_profit_on_provider_cost_rate",
        "reconciliation_coverage",
        "unallocated_reconciled_months",
        "unallocated_reconciled_cost_myr",
        # Rebill 明细按更正价格版本算出的估算成本（AIH-TASK-256，设计闸门 #830 v12 §3 INV-7、§6）。
        "corrected_estimated_provider_cost_myr",
    }
)
ADMIN_PREFIX: Final = "/api/v1/admin/"
# Rebill 的四个管理端路由与带更正历史的用量事件详情（AIH-TASK-256）。
REBILL_ROUTES: Final = (
    ("post", "/api/v1/admin/rebills/preview"),
    ("post", "/api/v1/admin/rebills"),
    ("get", "/api/v1/admin/rebills"),
    ("get", "/api/v1/admin/rebills/{rebill_id}"),
)
USAGE_EVENT_DETAIL: Final = "/api/v1/admin/usage-events/{usage_event_id}"
# Rebill 账本行 metadata 只许有这两个键（设计 §2「服务」第 5 步）。
REBILL_METADATA_KEYS: Final = frozenset({"rebill", "events"})
TOPUP_PATH: Final = "/api/v1/customer/topups"
ANALYTICS_MODULES: Final = (
    "app.repositories.usage_analytics",
    "app.schemas.usage_analytics",
    "app.api.admin_usage_analytics",
    # 供应商成本对账（AIH-TASK-245，设计闸门 #755 v1 §6）。
    "app.services.provider_cost_reconciliation",
    "app.repositories.provider_cost_reconciliations",
    "app.schemas.provider_cost_reconciliations",
    "app.api.admin_provider_cost_reconciliations",
    # Rebill（AIH-TASK-256，设计闸门 #830 v12 §6：客户门户没有任何 Rebill 接口）。
    "app.services.usage_rebill",
    "app.services.rebill_pricing",
    "app.repositories.usage_rebills",
    "app.schemas.usage_rebills",
    "app.api.admin_rebills",
)
ROOT: Final = Path(__file__).resolve().parents[2]
CUSTOMER_MODULES: Final = (
    "app/api/customer.py",
    "app/api/customer_payments.py",
    "app/api/me.py",
    "app/api/integration_usage.py",
    "app/api/payments.py",
)
HTTP_METHODS: Final = frozenset(
    {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
)


# --- 检查函数 -----------------------------------------------------------------------------


def _property_names(node: Any, components: dict[str, Any], seen: set[str]) -> Iterator[str]:
    """Every property name under `node`, following `#/components/schemas/…` references once."""
    if isinstance(node, list):
        for value in node:
            yield from _property_names(value, components, seen)
        return
    if not isinstance(node, dict):
        return
    reference = node.get("$ref")
    if isinstance(reference, str):
        name = reference.rsplit("/", 1)[-1]
        if name not in seen:
            seen.add(name)
            yield from _property_names(components[name], components, seen)
    properties = node.get("properties")
    if isinstance(properties, dict):
        yield from properties
    for value in node.values():
        yield from _property_names(value, components, seen)


def non_admin_response_names(openapi: dict[str, Any]) -> dict[str, set[str]]:
    """Property names of all responses, per non-admin path."""
    components = openapi.get("components", {}).get("schemas", {})
    found: dict[str, set[str]] = {}
    for path, item in openapi["paths"].items():
        if path.startswith(ADMIN_PREFIX):
            continue
        names: set[str] = set()
        for method, operation in item.items():
            if method in HTTP_METHODS:
                names.update(_property_names(operation.get("responses", {}), components, set()))
        found[path] = names
    return found


def forbidden_in_responses(openapi: dict[str, Any]) -> dict[str, set[str]]:
    """Non-admin paths whose responses carry a forbidden name, with those names."""
    return {
        path: names & FORBIDDEN
        for path, names in non_admin_response_names(openapi).items()
        if names & FORBIDDEN
    }


def imported_modules(source: str, module: str) -> set[str]:
    """Every module `source` (the text of `module`) imports, `from a import b` also as `a.b`."""
    package = module.rsplit(".", 1)[0]
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parent = package.split(".")
                parent = parent[: len(parent) - (node.level - 1)]
                base = ".".join(part for part in (*parent, base) if part)
            found.add(base)
            found.update(f"{base}.{alias.name}" for alias in node.names)
    return found


def analytics_imports(source: str, module: str) -> set[str]:
    """The margin analytics modules among what `module` imports."""
    return {
        name
        for name in imported_modules(source, module)
        if any(name == banned or name.startswith(f"{banned}.") for banned in ANALYTICS_MODULES)
    }


def customer_side_modules() -> list[Path]:
    ai_modules = sorted((ROOT / "app" / "ai_api").glob("*.py"))
    assert ai_modules, "app/ai_api has no modules: the scan would be empty"
    return [ROOT / relative for relative in CUSTOMER_MODULES] + ai_modules


def module_name(path: Path) -> str:
    return ".".join(path.relative_to(ROOT).with_suffix("").parts)


# --- 守卫 -------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def openapi() -> dict[str, Any]:
    # 支付开关打开：客户建单路由只在开关打开时注册，关着就扫不到它的响应。
    application = create_app(Settings(database_url="", jwt_secret_file="", payments_enabled=True))
    return application.openapi()


def test_customer_side_responses_carry_no_internal_cost_field(openapi) -> None:
    scanned = non_admin_response_names(openapi)
    for path in (TOPUP_PATH, "/api/v1/me"):
        assert path in scanned, path
    assert any(path.startswith("/api/v1/customer/") for path in scanned)
    # 扫描确实读到了属性（没有空转）。
    assert scanned[TOPUP_PATH]
    assert forbidden_in_responses(openapi) == {}


def response_names(openapi: dict[str, Any], method: str, path: str) -> set[str]:
    """Property names of one operation's responses (`$ref` followed)."""
    components = openapi.get("components", {}).get("schemas", {})
    operation = openapi["paths"][path][method]
    return set(_property_names(operation.get("responses", {}), components, set()))


def test_rebill_cost_fields_are_on_the_admin_routes_only(openapi) -> None:
    """AIH-TASK-256：明细的估算成本只在四个管理端 Rebill 路由里；门户路由一个都没有。"""
    # 列表项没有明细；预览、确认与详情都带明细。
    with_lines = {route for route in REBILL_ROUTES if route != ("get", "/api/v1/admin/rebills")}
    for method, path in REBILL_ROUTES:
        assert path.startswith(ADMIN_PREFIX)
        names = response_names(openapi, method, path)
        # 扫描确实读到了属性（没有空转）。
        assert names, path
        if (method, path) in with_lines:
            assert "corrected_estimated_provider_cost_myr" in names, (method, path)
    # 改动后的用量事件详情：更正历史与当前有效计费在管理端，不在任何门户路由里。
    detail = response_names(openapi, "get", USAGE_EVENT_DETAIL)
    assert {"effective_charge", "rebills", "estimated_provider_cost_myr"} <= detail
    for path, names in non_admin_response_names(openapi).items():
        assert "rebill" not in path, path
        assert "corrected_estimated_provider_cost_myr" not in names, path
    assert forbidden_in_responses(openapi) == {}


@pytest.fixture(params=["sqlite", "mysql"])
def rebill_api_db(request, monkeypatch) -> Iterator[sessionmaker[Session]]:
    yield from api_factory(request.param, monkeypatch)


def test_a_rebill_ledger_row_carries_no_cost_in_its_metadata(tmp_path, rebill_api_db) -> None:
    """设计 §3 INV-7：账本 metadata 只有 Rebill id 与事件数；门户只看得到账本行。"""
    world = build_world(rebill_api_db)
    api = rebill_api(tmp_path, world)
    world.add_event(project=A1, occurred_at=dt.datetime(2026, 11, 10))
    body = {
        "customer_id": world.customers[A].public_id,
        "selection": {
            "occurred_from": "2026-11-10T00:00:00Z",
            "occurred_to": "2026-11-20T00:00:00Z",
        },
        "corrective_pricing_rule_id": world.public_rule(R_LOW),
    }
    preview = api.client.post("/api/v1/admin/rebills/preview", json=body, headers=api.admin)
    assert preview.status_code == 200, preview.text
    # 管理端明细确实带估算成本：下面的断言不是因为字段不存在才成立。
    assert "corrected_estimated_provider_cost_myr" in preview.json()["data"]["lines"][0]
    confirmed = api.client.post(
        "/api/v1/admin/rebills",
        json={
            **body,
            "preview_token": preview.json()["data"]["preview_token"],
            "reason": "Fictional price correction",
            "idempotency_key": "00000000-0000-4000-8000-000000000001",
        },
        headers=api.admin,
    )
    assert confirmed.status_code == 201, confirmed.text

    with world.factory() as session:
        [row] = session.execute(
            select(WalletTransaction).where(
                WalletTransaction.reference_type == ReferenceType.REBILL
            )
        ).scalars()
    metadata = row.metadata_json
    assert isinstance(metadata, dict)
    assert set(metadata) == REBILL_METADATA_KEYS
    assert set(metadata) & (FORBIDDEN | FORBIDDEN_METADATA_KEYS) == set()
    assert not any("cost" in key or "margin" in key for key in metadata)
    assert metadata == {"rebill": confirmed.json()["data"]["id"], "events": 1}


def test_customer_side_modules_do_not_import_margin_analytics() -> None:
    offending = {}
    for path in customer_side_modules():
        found = analytics_imports(path.read_text(encoding="utf-8"), module_name(path))
        if found:
            offending[str(path.relative_to(ROOT))] = found
    assert offending == {}


@pytest.mark.parametrize(
    ("name", "columns"),
    [
        ("WALLET_EXPORT_COLUMNS", WALLET_EXPORT_COLUMNS),
        ("USAGE_EXPORT_COLUMNS", USAGE_EXPORT_COLUMNS),
        ("USAGE_SUMMARY_EXPORT_COLUMNS", USAGE_SUMMARY_EXPORT_COLUMNS),
        ("CONVERSATION_EXPORT_COLUMNS", CONVERSATION_EXPORT_COLUMNS),
    ],
)
def test_customer_export_columns_carry_no_internal_cost_field(name: str, columns) -> None:
    assert columns, name
    assert set(columns) & FORBIDDEN == set(), name


# --- 自检：守卫不空转 ----------------------------------------------------------------------


def test_the_response_check_reports_a_forbidden_name_injected_into_a_fake_schema() -> None:
    def response(reference: str) -> dict[str, Any]:
        return {
            "responses": {"200": {"content": {"application/json": {"schema": {"$ref": reference}}}}}
        }

    fake = {
        "paths": {
            "/api/v1/customer/fake": {"get": response("#/components/schemas/Outer")},
            # 管理端路径不在检查范围内。
            "/api/v1/admin/fake": {"get": response("#/components/schemas/Admin")},
        },
        "components": {
            "schemas": {
                "Outer": {
                    "properties": {
                        "items": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/Inner"},
                        }
                    }
                },
                "Inner": {
                    "properties": {
                        "amount": {"type": "string"},
                        "gateway_fee": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    }
                },
                "Admin": {"properties": {"gross_profit": {"type": "string"}}},
            }
        },
    }
    assert forbidden_in_responses(fake) == {"/api/v1/customer/fake": {"gateway_fee"}}


@pytest.mark.parametrize(
    "name",
    [
        "covered_estimated_cost_myr",
        "reconciled_provider_cost_myr",
        "provider_cost_myr",
        "provider_cost_basis",
        "gross_margin_on_provider_cost",
        "gross_margin_on_provider_cost_rate",
        "gross_profit_on_provider_cost",
        "gross_profit_on_provider_cost_rate",
        "reconciliation_coverage",
        "unallocated_reconciled_months",
        "unallocated_reconciled_cost_myr",
    ],
)
def test_the_response_check_reports_each_provider_cost_name(name: str) -> None:
    """AIH-TASK-248（设计闸门 #756 v2 §7「INV-7 守卫」）。

    把每个新键名注入假 schema，守卫报出它。
    """
    fake = {
        "paths": {
            "/api/v1/customer/fake": {
                "get": {
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Totals"}
                                }
                            }
                        }
                    }
                }
            }
        },
        "components": {
            "schemas": {
                "Totals": {"properties": {"amount": {"type": "string"}, name: {"type": "string"}}}
            }
        },
    }
    assert forbidden_in_responses(fake) == {"/api/v1/customer/fake": {name}}


@pytest.mark.parametrize(
    "source",
    [
        "from app.repositories import usage_analytics\n",
        "from app.schemas.usage_analytics import UsageAnalyticsView\n",
        "import app.api.admin_usage_analytics\n",
        "from ..repositories.usage_analytics import paid_payment_totals\n",
        "from app.services import provider_cost_reconciliation\n",
        "from app.schemas.provider_cost_reconciliations import ReconciliationView\n",
    ],
)
def test_the_import_check_reports_a_margin_analytics_import(source: str) -> None:
    assert analytics_imports(source, "app.api.customer")
