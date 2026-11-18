"""INV-7 guard: customers never see internal cost, margin or gateway fee fields (AIH-TASK-222).

设计闸门 #709 v2 §3 INV-7、§6「客户侧禁止的字段」、§7「INV-7 守卫」。三项检查：

- `create_app(...)` 的 OpenAPI 里，每个不以 `/api/v1/admin/` 开头的路径的全部响应（`$ref` 递归展开
  组件）的属性名与禁用名单无交集。支付开关打开：客户建单路由只在开关打开时注册。只走 OpenAPI，
  不读 `app.routes`（子路由延迟挂载）；
- 客户侧与 AI 模块不导入毛利分析的仓储、schema 与接口模块（`ast` 扫描 import）；
- 客户的四个导出列常量不含禁用名。

另有两条自检：把禁用名注入假 schema / 假模块时，同一个检查函数报出它（防止守卫空转）。
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import pytest

from app.core.config import Settings
from app.main import create_app
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
    }
)
ADMIN_PREFIX: Final = "/api/v1/admin/"
TOPUP_PATH: Final = "/api/v1/customer/topups"
ANALYTICS_MODULES: Final = (
    "app.repositories.usage_analytics",
    "app.schemas.usage_analytics",
    "app.api.admin_usage_analytics",
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
    "source",
    [
        "from app.repositories import usage_analytics\n",
        "from app.schemas.usage_analytics import UsageAnalyticsView\n",
        "import app.api.admin_usage_analytics\n",
        "from ..repositories.usage_analytics import paid_payment_totals\n",
    ],
)
def test_the_import_check_reports_a_margin_analytics_import(source: str) -> None:
    assert analytics_imports(source, "app.api.customer")
