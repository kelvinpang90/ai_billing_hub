"""Admin audit log query end to end on SQLite (spec §89; AIH-TASK-022).

测接口契约：筛选（单独与组合）、分页与 `total`、按 id 倒序、字段白名单、内部 id 换成邮箱、
前后状态解析成对象、各种 422、鉴权，以及查看本身不写库。最后一条扫描 `app/` 下所有写审计
的地方，保证每一种 `entity_type` 都归了类。

审计行直接插库：这样时间、操作者与实体都由用例决定。只有「操作者为空」那条走真实的登录
失败路径（`authenticate` 遇到不存在的邮箱）。

uuid 一律用全零占位值（secret-scan）。
"""

from __future__ import annotations

import ast
import datetime as dt
import json
import pathlib
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.core.database import create_session_factory
from app.core.tokens import issue_access_token
from app.main import create_app
from app.models.auth import AuditAction, AuditLog, User, UserRole, UserStatus
from app.models.base import Base
from app.services.audit_query import PUBLIC_ENTITY_TYPES, USER_ENTITY_TYPES
from app.services.auth import InvalidCredentials, RequestContext, authenticate, utc_now

AUDIT_LOGS = "/api/v1/admin/audit-logs"

# 响应字段白名单。断言「恰好等于」：多一个键（审计行 id、actor_user_id）就红。
ITEM_FIELDS = {
    "created_at",
    "action",
    "actor_role",
    "actor_email",
    "entity_type",
    "entity_id",
    "entity_user_email",
    "ip_address",
    "user_agent",
    "reason",
    "before_state",
    "after_state",
}
PAGE_FIELDS = {"items", "page", "page_size", "total"}
ENVELOPE_FIELDS = {"success", "data", "error", "request_id"}

ADMIN_EMAIL = "admin-a@example.com"
OTHER_EMAIL = "admin-b@example.com"
# 全零占位值：两个不同的对外 id。
TENANT_ID = "00000000-0000-0000-0000-000000000000"
PROJECT_ID = "00000000-0000-4000-8000-000000000000"
# 库里不存在的内部用户 id。
MISSING_USER_ID = 999_999

DAY = dt.datetime(2026, 9, 20)


def at(hour: int, minute: int = 0, second: int = 0) -> dt.datetime:
    return DAY.replace(hour=hour, minute=minute, second=second)


def iso(hour: int, minute: int = 0, second: int = 0) -> str:
    return at(hour, minute, second).isoformat()


# --- 夹具与帮手 -----------------------------------------------------------------


@pytest.fixture
def app(tmp_path):
    key = tmp_path / "jwt.key"
    key.write_text("a-test-signing-key-that-is-long-enough", encoding="utf-8")
    application = create_app(Settings(jwt_secret_file=str(key), database_url=""))
    # StaticPool：TestClient 在线程池里跑同步端点，内存库必须所有连接共用一个。
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    application.state.engine = engine
    application.state.session_factory = create_session_factory(engine)
    yield application
    engine.dispose()


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


def make_user(
    application: FastAPI, email: str | None = None, *, role: UserRole = UserRole.ADMIN
) -> int:
    now = utc_now()
    with application.state.session_factory() as session:
        user = User(
            email=email or f"{uuid.uuid4().hex}@example.com",
            password_hash="not-a-real-hash",
            role=role,
            status=UserStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
        session.add(user)
        session.commit()
        return int(user.id)


def token_for(application: FastAPI, user_id: int, role: str = "ADMIN") -> dict[str, str]:
    token = issue_access_token(
        application.state.settings,
        user_id=user_id,
        role=role,
        session_id=uuid.uuid4().hex,
        now=utc_now(),
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin_id(app) -> int:
    return make_user(app, ADMIN_EMAIL)


@pytest.fixture
def admin(app, admin_id) -> dict[str, str]:
    return token_for(app, admin_id)


def add_audit(
    application: FastAPI,
    *,
    action: AuditAction,
    created_at: dt.datetime,
    actor_id: int | None = None,
    actor_role: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    before_state: str | None = None,
    after_state: str | None = None,
    reason: str | None = None,
) -> None:
    with application.state.session_factory() as session:
        session.add(
            AuditLog(
                actor_user_id=actor_id,
                actor_role=actor_role,
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                before_state=before_state,
                after_state=after_state,
                ip_address="203.0.113.7",
                user_agent="admin-console-test",
                reason=reason,
                created_at=created_at,
            )
        )
        session.commit()


def audit_count(application: FastAPI) -> int:
    with application.state.session_factory() as session:
        return session.execute(select(func.count()).select_from(AuditLog)).scalar_one()


def listing(client: TestClient, headers: dict[str, str], **params: object) -> dict:
    response = client.get(AUDIT_LOGS, params=params, headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == ENVELOPE_FIELDS
    assert body["success"] is True
    page = body["data"]
    assert set(page) == PAGE_FIELDS
    assert all(set(item) == ITEM_FIELDS for item in page["items"])
    return page


def reasons(page: dict) -> list[str]:
    return [item["reason"] for item in page["items"]]


def error_code(response) -> str:
    body = response.json()
    assert set(body) == ENVELOPE_FIELDS
    assert body["success"] is False
    assert body["data"] is None
    return body["error"]["code"]


# --- 筛选 -----------------------------------------------------------------------


@pytest.fixture
def other_id(app) -> int:
    return make_user(app, OTHER_EMAIL)


@pytest.fixture
def seeded(app, admin_id, other_id) -> None:
    """Five rows, `reason` is the label; inserted r1 … r5, so newest first is r5 … r1."""
    add_audit(
        app,
        action=AuditAction.CUSTOMER_CREATE,
        created_at=at(8, 0),
        actor_id=admin_id,
        actor_role="ADMIN",
        entity_type="tenant",
        entity_id=TENANT_ID,
        reason="r1",
    )
    add_audit(
        app,
        action=AuditAction.PROJECT_CREATE,
        created_at=at(8, 10),
        actor_id=admin_id,
        actor_role="ADMIN",
        entity_type="project",
        entity_id=PROJECT_ID,
        reason="r2",
    )
    add_audit(
        app,
        action=AuditAction.CUSTOMER_UPDATE,
        created_at=at(8, 20),
        actor_id=other_id,
        actor_role="ADMIN",
        entity_type="tenant",
        entity_id=TENANT_ID,
        reason="r3",
    )
    add_audit(
        app,
        action=AuditAction.LOGIN,
        created_at=at(8, 30),
        actor_id=other_id,
        actor_role="ADMIN",
        entity_type="users",
        entity_id=str(other_id),
        reason="r4",
    )
    add_audit(app, action=AuditAction.LOGIN_FAILED, created_at=at(8, 40), reason="r5")


ALL = ["r5", "r4", "r3", "r2", "r1"]


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({}, ALL),
        # 单独的每一个条件。
        ({"action": "CUSTOMER_CREATE"}, ["r1"]),
        ({"action": "LOGIN_FAILED"}, ["r5"]),
        ({"entity_type": "tenant"}, ["r3", "r1"]),
        ({"entity_type": "Tenant"}, []),
        ({"entity_id": TENANT_ID}, ["r3", "r1"]),
        ({"entity_id": PROJECT_ID}, ["r2"]),
        ({"actor_email": ADMIN_EMAIL}, ["r2", "r1"]),
        # 去首尾空白、转小写后精确匹配。
        ({"actor_email": "  Admin-B@Example.COM  "}, ["r4", "r3"]),
        ({"actor_email": "admin-b@example"}, []),
        ({"actor_email": "nobody@example.com"}, []),
        # 含 created_from、不含 created_to。
        ({"created_from": iso(8, 10)}, ["r5", "r4", "r3", "r2"]),
        ({"created_to": iso(8, 10)}, ["r1"]),
        ({"created_from": iso(8, 10), "created_to": iso(8, 30)}, ["r3", "r2"]),
        ({"created_from": iso(8, 40), "created_to": iso(8, 40, 1)}, ["r5"]),
        ({"created_from": iso(9, 0)}, []),
        # 组合。
        ({"entity_type": "tenant", "entity_id": TENANT_ID}, ["r3", "r1"]),
        ({"entity_type": "project", "entity_id": TENANT_ID}, []),
        ({"action": "CUSTOMER_UPDATE", "actor_email": OTHER_EMAIL}, ["r3"]),
        ({"action": "CUSTOMER_UPDATE", "actor_email": ADMIN_EMAIL}, []),
        ({"entity_type": "tenant", "created_from": iso(8, 10)}, ["r3"]),
        ({"action": "LOGIN", "entity_type": "users"}, ["r4"]),
        (
            {
                "action": "LOGIN",
                "actor_email": OTHER_EMAIL,
                "entity_type": "users",
                "created_from": iso(8, 30),
                "created_to": iso(8, 40),
            },
            ["r4"],
        ),
        # 未知的查询参数不报错，也不影响结果。
        ({"unknown": "ignored"}, ALL),
    ],
)
def test_filters_alone_and_combined(client, admin, seeded, params: dict, expected: list) -> None:
    page = listing(client, admin, **params)

    assert reasons(page) == expected
    assert page["total"] == len(expected)


def test_entity_id_never_matches_a_user_id(client, admin, seeded, other_id) -> None:
    """用户类型的 entity_id 是内部用户 id：不指定类型时按它筛也命中不了（不能拿来探测）。"""
    page = listing(client, admin, entity_id=str(other_id))

    assert (page["items"], page["total"]) == ([], 0)


# --- 排序与分页 -----------------------------------------------------------------


def test_newest_first_is_by_row_id_not_by_created_at(client, app, admin) -> None:
    """登录路径与业务路径的时间取整不一致：后写的那一行 created_at 可能更早。"""
    add_audit(app, action=AuditAction.LOGIN, created_at=at(9, 0, 1), reason="written first")
    add_audit(
        app, action=AuditAction.CUSTOMER_CREATE, created_at=at(9, 0, 0), reason="written second"
    )

    page = listing(client, admin)

    assert reasons(page) == ["written second", "written first"]


def test_pages_and_total(client, app, admin) -> None:
    for n in range(25):
        add_audit(app, action=AuditAction.LOGIN, created_at=at(10, n), reason=f"n{n}")
    newest_first = [f"n{n}" for n in reversed(range(25))]

    pages = [listing(client, admin, page=number, page_size=10) for number in (1, 2, 3, 4)]

    assert [reasons(page) for page in pages] == [
        newest_first[:10],
        newest_first[10:20],
        newest_first[20:],
        [],
    ]
    assert [page["total"] for page in pages] == [25] * 4
    shape = [(page["page"], page["page_size"]) for page in pages]
    assert shape == [(1, 10), (2, 10), (3, 10), (4, 10)]
    # 缺省是第 1 页、每页 20 条。
    default = listing(client, admin)
    assert (default["page"], default["page_size"], default["total"]) == (1, 20, 25)
    assert reasons(default) == newest_first[:20]


@pytest.mark.parametrize(
    "params",
    [
        {"page_size": 0},
        {"page_size": 101},
        {"page": 0},
        {"page": 10001},
        {"page": "abc"},
    ],
    ids=["size-0", "size-101", "page-0", "page-10001", "page-abc"],
)
def test_paging_bounds(client, admin, params: dict) -> None:
    """超出范围是 422，不静默截断（spec §108）。"""
    response = client.get(AUDIT_LOGS, params=params, headers=admin)

    assert (response.status_code, error_code(response)) == (422, "VALIDATION_ERROR")


# --- 字段与内部 id 的替换 -------------------------------------------------------


def test_an_item_carries_exactly_the_documented_fields(client, app, admin, admin_id) -> None:
    add_audit(
        app,
        action=AuditAction.CUSTOMER_CREATE,
        created_at=at(8, 30),
        actor_id=admin_id,
        actor_role="ADMIN",
        entity_type="tenant",
        entity_id=TENANT_ID,
        after_state=json.dumps({"public_id": TENANT_ID, "company_name": "Acme Sdn Bhd"}),
    )

    [item] = listing(client, admin)["items"]

    assert item == {
        "created_at": "2026-09-20T08:30:00",
        "action": "CUSTOMER_CREATE",
        "actor_role": "ADMIN",
        "actor_email": ADMIN_EMAIL,
        "entity_type": "tenant",
        "entity_id": TENANT_ID,
        "entity_user_email": None,
        "ip_address": "203.0.113.7",
        "user_agent": "admin-console-test",
        "reason": None,
        "before_state": None,
        "after_state": {"public_id": TENANT_ID, "company_name": "Acme Sdn Bhd"},
    }


def test_user_entities_are_shown_by_email_not_by_internal_id(client, app, admin) -> None:
    target = make_user(app, "target@example.com")
    for entity_type in ("users", "two_factor_settings", "recovery_codes"):
        add_audit(
            app,
            action=AuditAction.LOGIN,
            created_at=at(8, 0),
            entity_type=entity_type,
            entity_id=str(target),
            reason=entity_type,
        )
    # 用户已不存在：两个都是 null，内部 id 照样不出现。
    add_audit(
        app,
        action=AuditAction.LOGIN,
        created_at=at(8, 0),
        entity_type="users",
        entity_id=str(MISSING_USER_ID),
        reason="missing user",
    )
    # 对外 id 的类型：原样返回，没有 entity_user_email。
    add_audit(
        app,
        action=AuditAction.PROJECT_CREATE,
        created_at=at(8, 0),
        entity_type="project",
        entity_id=PROJECT_ID,
        reason="project",
    )

    page = listing(client, admin)

    shown = {
        item["reason"]: (item["entity_type"], item["entity_id"], item["entity_user_email"])
        for item in page["items"]
    }
    assert shown == {
        "users": ("users", None, "target@example.com"),
        "two_factor_settings": ("two_factor_settings", None, "target@example.com"),
        "recovery_codes": ("recovery_codes", None, "target@example.com"),
        "missing user": ("users", None, None),
        "project": ("project", PROJECT_ID, None),
    }


def test_filtering_a_user_entity_type_still_hides_the_id(client, app, admin) -> None:
    target = make_user(app, "target@example.com")
    add_audit(
        app,
        action=AuditAction.TWO_FACTOR_ENABLED,
        created_at=at(8, 0),
        entity_type="two_factor_settings",
        entity_id=str(target),
    )

    [item] = listing(client, admin, entity_type="two_factor_settings")["items"]

    assert (item["entity_id"], item["entity_user_email"]) == (None, "target@example.com")


def test_an_unknown_email_login_failure_has_no_actor(client, app, admin) -> None:
    """真实的写入路径：不存在的邮箱登录失败，审计没有操作者。"""
    with pytest.raises(InvalidCredentials):
        authenticate(
            app.state.session_factory,
            app.state.settings,
            email="nobody@example.com",
            password="not-the-password",
            context=RequestContext(ip_address="203.0.113.7", user_agent="probe"),
        )

    [item] = listing(client, admin)["items"]

    assert (item["action"], item["reason"]) == ("LOGIN_FAILED", "UNKNOWN_EMAIL")
    assert (item["actor_email"], item["actor_role"]) == (None, None)
    assert (item["entity_type"], item["entity_id"], item["entity_user_email"]) == (None,) * 3
    # 登录路径的时间不截秒；响应里一律精确到秒。
    assert len(item["created_at"]) == len("2026-09-20T08:30:00")


def test_an_actor_that_no_longer_exists_has_no_email(client, app, admin) -> None:
    add_audit(
        app,
        action=AuditAction.CUSTOMER_CREATE,
        created_at=at(8, 0),
        actor_id=MISSING_USER_ID,
        actor_role="ADMIN",
    )

    [item] = listing(client, admin)["items"]

    assert (item["actor_email"], item["actor_role"]) == (None, "ADMIN")


def test_states_are_json_objects(client, app, admin) -> None:
    add_audit(
        app,
        action=AuditAction.WALLET_ADJUSTMENT_POSTED,
        created_at=at(8, 0),
        before_state=json.dumps({"balance": "-5.00000000"}),
        after_state=json.dumps({"balance": "15.00000000", "changed_fields": ["a", "b"]}),
        reason="both",
    )
    add_audit(app, action=AuditAction.LOGIN, created_at=at(8, 1), reason="neither")
    add_audit(
        app,
        action=AuditAction.LOGIN,
        created_at=at(8, 2),
        before_state="",
        after_state="",
        reason="empty",
    )

    states = {
        item["reason"]: (item["before_state"], item["after_state"])
        for item in listing(client, admin)["items"]
    }

    assert states == {
        "both": (
            {"balance": "-5.00000000"},
            {"balance": "15.00000000", "changed_fields": ["a", "b"]},
        ),
        "neither": (None, None),
        "empty": (None, None),
    }


# --- 校验 -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "params",
    [
        {"action": "NOT_AN_ACTION"},
        {"action": "login"},
        {"created_from": "2026-09-20"},
        {"created_from": "2026-09-20T08:30:00Z"},
        {"created_from": "2026-09-20T08:30:00+08:00"},
        {"created_from": "2026-09-20T08:30:00.500"},
        {"created_from": "2026-09-20 08:30:00"},
        {"created_from": "2026-9-20T08:30:00"},
        {"created_from": "2026-02-30T00:00:00"},
        {"created_from": ""},
        {"created_to": "tomorrow"},
        {"created_from": "2026-09-20T08:30:00", "created_to": "2026-09-20T08:30:00"},
        {"created_from": "2026-09-20T08:30:01", "created_to": "2026-09-20T08:30:00"},
        {"entity_type": "users", "entity_id": "1"},
        {"entity_type": "two_factor_settings", "entity_id": "1"},
        {"entity_type": "recovery_codes", "entity_id": "1"},
    ],
    ids=[
        "action-unknown",
        "action-lowercase",
        "time-date-only",
        "time-zulu",
        "time-offset",
        "time-fraction",
        "time-space",
        "time-unpadded",
        "time-not-a-date",
        "time-empty",
        "time-word",
        "range-equal",
        "range-reversed",
        "users-with-entity-id",
        "two-factor-with-entity-id",
        "recovery-codes-with-entity-id",
    ],
)
def test_invalid_queries_are_422(client, app, admin, seeded, params: dict) -> None:
    before = audit_count(app)

    response = client.get(AUDIT_LOGS, params=params, headers=admin)

    assert (response.status_code, error_code(response)) == (422, "VALIDATION_ERROR")
    # 只列字段名，不回显值。
    message = response.json()["error"]["message"]
    for value in params.values():
        if value:
            assert value not in message
    assert audit_count(app) == before


def test_time_and_combination_checks_come_after_authentication(client) -> None:
    """时间与组合由服务层判：没带令牌时先得到 401（分页与 action 仍先于鉴权）。"""
    response = client.get(AUDIT_LOGS, params={"created_from": "not-a-time"})

    assert (response.status_code, error_code(response)) == (401, "TOKEN_INVALID")


# --- 鉴权与只读 -----------------------------------------------------------------


def test_anonymous_is_401(client, app, seeded) -> None:
    response = client.get(AUDIT_LOGS)

    assert (response.status_code, error_code(response)) == (401, "TOKEN_INVALID")


def test_a_customer_is_403(client, app, seeded) -> None:
    customer = make_user(app, role=UserRole.CUSTOMER)

    response = client.get(AUDIT_LOGS, headers=token_for(app, customer, "CUSTOMER"))

    assert (response.status_code, error_code(response)) == (403, "ADMIN_REQUIRED")


def test_viewing_the_audit_log_writes_nothing(client, app, admin, seeded) -> None:
    before = audit_count(app)

    listing(client, admin)
    listing(client, admin, action="LOGIN", created_from=iso(8, 0))

    assert audit_count(app) == before


# --- entity_type 的归类 ---------------------------------------------------------

APP_DIR = pathlib.Path(__file__).resolve().parents[2] / "app"
# 写审计的两个入口：`record_audit(...)` 与直接构造 `AuditLog(...)`（账本的审计）。
AUDIT_WRITERS = {"record_audit", "AuditLog"}


def _callee(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _module_strings(tree: ast.Module) -> dict[str, str]:
    """Module-level `NAME = "literal"` / `NAME: Final = "literal"`."""
    found: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            for target in targets:
                if isinstance(target, ast.Name):
                    found[target.id] = value.value
    return found


class _EntityTypes(ast.NodeVisitor):
    """Every `entity_type=` passed to an audit writer, with the enclosing function's params."""

    def __init__(self) -> None:
        self.params: list[set[str]] = [set()]
        self.found: list[tuple[ast.expr, set[str], int]] = []

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        arguments = node.args
        names = {a.arg for a in [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]}
        self.params.append(names)
        self.generic_visit(node)
        self.params.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node: ast.Call) -> None:
        if _callee(node) in AUDIT_WRITERS:
            for keyword in node.keywords:
                if keyword.arg == "entity_type":
                    self.found.append((keyword.value, self.params[-1], node.lineno))
        self.generic_visit(node)


def written_entity_types() -> dict[str, list[str]]:
    """entity_type literal → where it is written. Fails on anything it cannot resolve."""
    written: dict[str, list[str]] = {}
    unresolved: list[str] = []
    for path in sorted(APP_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        constants = _module_strings(tree)
        visitor = _EntityTypes()
        visitor.visit(tree)
        where = path.relative_to(APP_DIR.parent).as_posix()
        for value, params, line in visitor.found:
            if isinstance(value, ast.Constant) and value.value is None:
                continue
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                literal = value.value
            elif isinstance(value, ast.Name) and value.id in constants:
                literal = constants[value.id]
            elif isinstance(value, ast.Name) and value.id in params:
                # 透传（`record_audit` 把参数交给 `AuditLog`）：字面量在它的调用方那里。
                continue
            else:
                unresolved.append(f"{where}:{line}")
                continue
            written.setdefault(literal, []).append(f"{where}:{line}")
    assert unresolved == [], (
        "写审计时的 entity_type 不是字面量、也不是本模块的字符串常量，扫描认不出来："
        + ", ".join(unresolved)
    )
    return written


def test_every_written_entity_type_is_classified() -> None:
    """新增一种 entity_type 而没有在 app/services/audit_query.py 里归类，这里就红。

    归错了的后果：内部用户 id 出现在响应里，或对外 id 被错误地藏掉。
    """
    written = written_entity_types()
    # 扫描确实找到了东西 —— 否则下面的断言在空集合上空转。
    assert {"users", "tenant", "wallet_transaction"} <= set(written)

    unclassified = {
        entity_type: places
        for entity_type, places in written.items()
        if entity_type not in USER_ENTITY_TYPES | PUBLIC_ENTITY_TYPES
    }

    assert unclassified == {}
    assert not USER_ENTITY_TYPES & PUBLIC_ENTITY_TYPES
