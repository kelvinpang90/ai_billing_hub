"""AST enforcement for the AI module boundary (design gate #281 v23 §2)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
AI_API = ROOT / "app" / "ai_api"

REQUEST_FILES = {
    "routes.py",
    "access.py",
    "provider.py",
    "messages.py",
    "outbox.py",
    "database.py",
    "held.py",
    "models.py",
    "__init__.py",
}
DELIVERY_FILES = {"delivery.py", "client_rules.py"}
ADMIN_FILES = {"admin.py"}
OPS_FILES = {"ops.py"}

REQUEST_ALLOWLIST = {
    "app.ai_api",
    "app.core.config",
    "app.core.logging",
    "app.core.errors",
    "app.models.base",
    "app.models.tenancy",
    "app.models.ai_catalog",
}
DELIVERY_ALLOWLIST = REQUEST_ALLOWLIST | {
    "app.models.integration",
    "app.services.integration_auth",
    "app.core.crypto",
}
ADMIN_ALLOWLIST = REQUEST_ALLOWLIST | {
    "app.models.auth",
    "app.services.auth",
}
OPS_ALLOWLIST = ADMIN_ALLOWLIST | DELIVERY_ALLOWLIST
REVERSE_EXCEPTIONS = {
    Path("app/main.py"),
    Path("app/api/admin_ai_access.py"),
    Path("app/api/health.py"),
    Path("app/services/alerts.py"),
    Path("app/cli.py"),
    Path("alembic/env.py"),
    Path("scripts/packaging_smoke.py"),
}


def _module_exists(module: str) -> bool:
    path = ROOT.joinpath(*module.split("."))
    return path.with_suffix(".py").is_file() or (path / "__init__.py").is_file()


def _app_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names if alias.name.startswith("app."))
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module != "app" and not node.module.startswith("app."):
                continue
            for alias in node.names:
                candidate = f"{node.module}.{alias.name}"
                modules.add(candidate if _module_exists(candidate) else node.module)
    return modules


def _permitted(module: str, allowlist: set[str]) -> bool:
    return module == "app.ai_api" or module.startswith("app.ai_api.") or module in allowlist


@pytest.mark.parametrize(
    ("filenames", "allowlist"),
    [
        (REQUEST_FILES, REQUEST_ALLOWLIST),
        (DELIVERY_FILES, DELIVERY_ALLOWLIST),
        (ADMIN_FILES, ADMIN_ALLOWLIST),
        (OPS_FILES, OPS_ALLOWLIST),
    ],
)
def test_ai_api_files_import_only_their_group_allowlist(
    filenames: set[str], allowlist: set[str]
) -> None:
    for filename in filenames:
        path = AI_API / filename
        if not path.exists():
            continue
        forbidden = sorted(
            module for module in _app_imports(path) if not _permitted(module, allowlist)
        )
        assert not forbidden, f"{path.relative_to(ROOT)} imports forbidden modules: {forbidden}"


def test_ai_api_contains_only_files_assigned_to_a_boundary_group() -> None:
    grouped = REQUEST_FILES | DELIVERY_FILES | ADMIN_FILES | OPS_FILES
    actual = {path.name for path in AI_API.glob("*.py")}
    assert actual <= grouped, f"ungrouped app/ai_api files: {sorted(actual - grouped)}"


def test_only_listed_existing_modules_import_ai_api() -> None:
    scan_roots = (ROOT / "app", ROOT / "alembic", ROOT / "scripts")
    for scan_root in scan_roots:
        for path in scan_root.rglob("*.py"):
            if path.is_relative_to(AI_API):
                continue
            imported_ai_api = any(
                module == "app.ai_api" or module.startswith("app.ai_api.")
                for module in _app_imports(path)
            )
            if imported_ai_api:
                assert path.relative_to(ROOT) in REVERSE_EXCEPTIONS
