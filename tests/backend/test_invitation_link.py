"""邀请邮件里的链接，必须落在前端登记了、匿名可达的那条路由上。

设计闸门 #368 v12 §2、§7「链接守卫」「邀请链接匿名可达」、§8。

与 test_password_reset_link.py 同一种守卫：拿**后端真的渲染出的链接**，
去和**前端真的登记的路由**比对，不是比对两段源码里的字面量。
那个文件名含 password，Worker 不能改，所以另起这个文件；
也不从它导入，两份守卫互不依赖。

邀请链接比重置链接更硬：点它的客户账号还是 `INVITED`，**根本没有可用的密码**。
路由一旦落进 `RequireAuth`，客户会被送去登录页 ——
一条没有任何报错的死链（设计 §8「部分部署时的行为」）。
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from app.core.config import Settings
from app.main import create_app
from app.tasks.outbox import _render_portal_invitation

FRONTEND_SRC = Path(__file__).resolve().parents[2] / "frontend" / "src"
PATHS_TS = FRONTEND_SRC / "routes" / "paths.ts"
ROUTES_TSX = FRONTEND_SRC / "routes" / "index.tsx"
AUTH_TS = FRONTEND_SRC / "api" / "auth.ts"

BASE_URL = "https://billing.example.com"
TOKEN = "a-token-with-url-unsafe-bytes+/="
EXPIRES_AT = "2026-10-24T08:00:00"

ACCEPT_ROUTE = "path={ROUTES.acceptInvitation}"


def _frontend_literal(name: str, pattern: str) -> str:
    """从 `paths.ts` 里取一个字符串字面量。

    取不到就**失败**，不是跳过：前端改名或删除，正是这条用例要抓的事。
    """
    source = PATHS_TS.read_text(encoding="utf-8")
    match = re.search(pattern, source)
    assert match is not None, f"{name} is no longer declared in {PATHS_TS.name}"
    return match.group(1)


def frontend_accept_path() -> str:
    return _frontend_literal("ROUTES.acceptInvitation", r'acceptInvitation:\s*"([^"]+)"')


def frontend_token_param() -> str:
    return _frontend_literal(
        "ACCEPT_INVITATION_TOKEN_PARAM", r'ACCEPT_INVITATION_TOKEN_PARAM\s*=\s*"([^"]+)"'
    )


def rendered_link() -> str:
    """后端真的会发出去的那封邀请信里，那条链接。"""
    email = _render_portal_invitation(
        Settings(frontend_base_url=BASE_URL),
        {"to": "portal@example.com", "token": TOKEN, "expires_at": EXPIRES_AT},
    )
    assert email is not None
    match = re.search(r"(https://\S+)", email.body)
    assert match is not None, "the invitation email no longer contains a link"
    return match.group(1)


def _without_comments(source: str) -> str:
    """去掉 `/* … */` 与 `// …` 注释：注释里提到路由名或 `<Route` 不该影响判定。"""
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", "", source)


def _opening_tag_end(source: str, start: int) -> int:
    """`<Route` 起始处到它的开标签结束的 `>`（花括号之外的那一个）的下标。"""
    depth = 0
    for index in range(start + 1, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        elif char == ">" and depth == 0:
            return index
    raise AssertionError("an unterminated <Route> tag in index.tsx")


def guarded_spans(source: str) -> list[tuple[int, int]]:
    """每个开标签里含 `RequireAuth` 的 `<Route>…</Route>` 元素覆盖的下标区间。

    ⚠️ 按标签配对算嵌套，不按「出现在 `<RequireAuth />` 之后」判断：
    挪到守卫后面、但在守卫元素之外的路由同样匿名可达，挪进守卫里面的才是死链。
    """
    spans: list[tuple[int, int]] = []
    stack: list[tuple[int, bool]] = []
    for match in re.finditer(r"<Route\b|</Route>", source):
        if match.group(0) == "</Route>":
            assert stack, "an unmatched </Route> in index.tsx"
            start, guarded = stack.pop()
            if guarded:
                spans.append((start, match.end()))
            continue
        end = _opening_tag_end(source, match.start())
        if source[end - 1] == "/":
            continue  # 自闭合：没有子路由
        stack.append((match.start(), "RequireAuth" in source[match.start() : end]))
    assert not stack, "an unclosed <Route> in index.tsx"
    return spans


# --- 链接对得上前端登记的路由 -----------------------------------------------------


def test_the_invitation_link_lands_on_a_route_the_frontend_registers() -> None:
    assert urlparse(rendered_link()).path == frontend_accept_path()


def test_the_invitation_link_carries_the_token_in_the_parameter_the_page_reads() -> None:
    # 比的是**解出来的值**：后端对令牌做了 percent-encoding，
    # 前端解码后拿到的必须还是原来那张。
    query = parse_qs(urlparse(rendered_link()).query)
    assert query.get(frontend_token_param()) == [TOKEN]


def test_the_invitation_link_starts_at_the_configured_front_end() -> None:
    assert rendered_link().startswith(f"{BASE_URL}/")


# --- 路由挂进了路由表，且在 RequireAuth 之外 ----------------------------------------


def test_the_frontend_mounts_the_accept_route_once() -> None:
    """`paths.ts` 里加一条常量是免费的，忘了挂进路由表一样免费。"""
    source = _without_comments(ROUTES_TSX.read_text(encoding="utf-8"))
    assert source.count(ACCEPT_ROUTE) == 1


def test_the_accept_route_is_outside_every_require_auth_element() -> None:
    """路由不在任何 `RequireAuth` 元素之内，自己的 `element` 也不包一层 `RequireAuth`。

    把路由挪进 `<Route element={<RequireAuth />}>` 的变异会让这条变红。
    """
    source = _without_comments(ROUTES_TSX.read_text(encoding="utf-8"))
    spans = guarded_spans(source)
    # 一个守卫都没找到时，下面的断言是在空列表上空转。
    assert spans, "no <Route> element with RequireAuth was found in index.tsx"

    position = source.index(ACCEPT_ROUTE)
    start = source.rindex("<Route", 0, position)
    own_tag = source[start : _opening_tag_end(source, start) + 1]
    assert "RequireAuth" not in own_tag
    for begin, end in spans:
        assert not begin <= position < end, "ROUTES.acceptInvitation is inside <RequireAuth />"


def test_the_guard_check_catches_a_route_moved_inside_require_auth() -> None:
    """守卫自己的变异用例：把接受页挪进 `RequireAuth` 之后，上面的判定必须认出来。"""
    source = _without_comments(ROUTES_TSX.read_text(encoding="utf-8"))
    route = re.search(r"<Route path=\{ROUTES\.acceptInvitation\}[^\n]*\n", source)
    assert route is not None
    guard = re.search(r"<Route element=\{<RequireAuth />\}>\n", source)
    assert guard is not None
    moved = source.replace(route.group(0), "")
    moved = moved.replace(guard.group(0), guard.group(0) + route.group(0))

    position = moved.index(ACCEPT_ROUTE)
    assert any(begin <= position < end for begin, end in guarded_spans(moved))


# --- 接受页提交的地址后端真的在服务 ---------------------------------------------------


def test_the_accept_page_posts_to_a_route_the_backend_serves() -> None:
    # 走 OpenAPI 而不是 `app.routes`：子路由是延迟挂载的（见 test_password_reset_link.py）。
    served = set(create_app(Settings(database_url="", jwt_secret_file="")).openapi()["paths"])
    source = AUTH_TS.read_text(encoding="utf-8")
    match = re.search(
        r'export async function acceptInvitation\b[\s\S]*?\.post<[^(]*>\(\s*"([^"]+)"', source
    )
    assert match is not None, f"acceptInvitation() no longer posts in {AUTH_TS.name}"
    assert match.group(1) in served
