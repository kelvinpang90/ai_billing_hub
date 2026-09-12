"""重置邮件里的链接，必须落在前端真的登记了的那条路由上（spec §53）。

⚠️ 这条链接的两半分别住在两个技术栈里：路径由后端 `_render_password_reset` 拼，
路由由前端 `routes/paths.ts` 登记。**两边各自的测试都证明不了它们对得上** ——
后端测「拼出来的字符串长这样」，前端测「这条路由渲染了这个页面」，两边同时全绿
而链接是死的，完全可能。

这种缺陷的现场是：信正常发出、用户正常收到、点开是一个 404（或者更糟，被守卫
当成未登录重定向到登录页 —— 而他来这儿正是因为登不进去）。没有任何一处会报错。

所以这里做的是唯一能真正证明的事：**拿后端真的拼出来的链接，去和前端真的登记
的那条路由比对。**不是比对两段源码的字面量，是比对两边的产物。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from app.core.config import Settings
from app.main import create_app
from app.schemas.auth import ForgotPasswordRequest, ResetPasswordRequest
from app.tasks.outbox import _render_password_reset

FRONTEND_SRC = Path(__file__).resolve().parents[2] / "frontend" / "src"
PATHS_TS = FRONTEND_SRC / "routes" / "paths.ts"
AUTH_TS = FRONTEND_SRC / "api" / "auth.ts"

BASE_URL = "https://billing.example.com"
TOKEN = "a-token-with-url-unsafe-bytes+/="


def _frontend_literal(name: str, pattern: str) -> str:
    """从 `paths.ts` 里取一个字符串字面量。

    ⚠️ 取不到就**失败**，不是跳过。取不到只有两种可能：前端把它改名了，或者
    整个删了 —— 两种都正是这条用例要抓的事。静默跳过的话，守卫会在最需要它的
    那一刻正好不生效。
    """
    source = PATHS_TS.read_text(encoding="utf-8")
    match = re.search(pattern, source)
    assert match is not None, f"{name} is no longer declared in {PATHS_TS.name}"
    return match.group(1)


def frontend_reset_path() -> str:
    return _frontend_literal("ROUTES.resetPassword", r'resetPassword:\s*"([^"]+)"')


def frontend_token_param() -> str:
    return _frontend_literal("RESET_TOKEN_PARAM", r'RESET_TOKEN_PARAM\s*=\s*"([^"]+)"')


def rendered_link() -> str:
    """后端真的会发出去的那封信里，那条链接。"""
    email = _render_password_reset(
        Settings(frontend_base_url=BASE_URL),
        {"to": "admin@example.com", "token": TOKEN},
    )
    assert email is not None
    match = re.search(r"(https://\S+)", email.body)
    assert match is not None, "the reset email no longer contains a link"
    return match.group(1)


def test_the_email_link_lands_on_a_route_the_frontend_registers() -> None:
    assert urlparse(rendered_link()).path == frontend_reset_path()


def test_the_email_link_carries_the_token_in_the_parameter_the_page_reads() -> None:
    query = parse_qs(urlparse(rendered_link()).query)
    # ⚠️ 比的是**解出来的值**，不是链接字面量：后端对令牌做了 percent-encoding，
    # 前端 `useSearchParams` 解码后拿到的必须还是原来那一张令牌。中间任何一处
    # 编码错误，用户拿到的都是「链接无效」，而令牌本身好好的。
    assert query.get(frontend_token_param()) == [TOKEN]


def test_the_frontend_registers_that_route_in_its_route_table() -> None:
    """路径对得上还不够 —— 它还得真的被挂进路由表。

    ⚠️ `paths.ts` 里加一条常量是免费的，忘了在 `routes/index.tsx` 里用它一样
    免费。少了这一条，上面两条用例会在一个**没人挂载的路径**上愉快地通过。
    """
    routes = (PATHS_TS.parent / "index.tsx").read_text(encoding="utf-8")
    assert "ROUTES.resetPassword" in routes
    assert "ROUTES.forgotPassword" in routes


def test_the_reset_page_is_outside_the_signed_in_guard() -> None:
    """这两页必须在 `RequireAuth` **外面**。

    ⚠️ 放进去的后果不是 404 而是更难认的那种：守卫把未登录的访客重定向到登录页，
    于是用户点开重置链接看到的是一张登录表单 —— 而他来这儿正是因为登不进去。
    """
    routes = (PATHS_TS.parent / "index.tsx").read_text(encoding="utf-8")
    guard = routes.index("<RequireAuth />")
    for name in ("ROUTES.login", "ROUTES.forgotPassword", "ROUTES.resetPassword"):
        assert routes.index(name) < guard, f"{name} must be registered before <RequireAuth />"


def _frontend_post(function_name: str) -> tuple[str, set[str]]:
    """从 `api/auth.ts` 里某个导出函数中，取出它 `post()` 的地址与请求体字段名。

    ⚠️ 这一对是**前端所有页面用例都盖不到的**：页面用例把整个 `api/auth` 模块
    mock 掉了（它们测状态机，不测网络），所以路径写错、字段名写成驼峰，前端
    一条用例都不会红 —— 到浏览器里才表现成 404 或 422。

    ⚠️ 定位靠的是**函数名**而不是 URL 片段。按 URL 找的话，路径一改就变成
    「找不到这个调用」，报出来的原因和真正的毛病（发去了一个没人服务的地址）
    对不上，排查要多绕一圈。
    """
    source = AUTH_TS.read_text(encoding="utf-8")
    match = re.search(
        rf"export function {re.escape(function_name)}\b[^{{]*\{{\s*"
        r'return post<[^>]*>\(\s*"([^"]+)",\s*\{([^}]*)\}',
        source,
    )
    assert match is not None, f"{function_name}() no longer posts a body in {AUTH_TS.name}"
    # `{ email }` 与 `{ token, new_password: newPassword }` 两种写法都要认。
    body = {part.split(":")[0].strip() for part in match.group(2).split(",") if part.strip()}
    return match.group(1), body


def test_the_frontend_posts_to_routes_the_backend_actually_serves() -> None:
    # ⚠️ 走 OpenAPI 而不是 `app.routes`：子路由是延迟挂载的，`app.routes` 里
    # 只看得到几个 `_IncludedRouter` 壳子，拿它比对会**永远比不上**。
    served = set(create_app().openapi()["paths"])
    for function_name in ("requestPasswordReset", "resetPassword"):
        url, _ = _frontend_post(function_name)
        assert url in served, f"{function_name}() posts to {url}, which no route serves"


def test_the_frontend_sends_the_field_names_the_backend_declares() -> None:
    """请求体字段名必须和 Pydantic 模型逐字相同。

    ⚠️ `new_password` 打成 `newPassword` 的后果是 FastAPI 以「缺字段」回 422 ——
    界面上表现成一句莫名其妙的错误，而前后端两边的测试全绿。这里比的是**模型
    自己声明的名字**，不是我抄在用例里的一份副本：后端改名时这条会跟着动。
    """
    _, forgot_body = _frontend_post("requestPasswordReset")
    assert forgot_body == set(ForgotPasswordRequest.model_fields)

    _, reset_body = _frontend_post("resetPassword")
    assert reset_body == set(ResetPasswordRequest.model_fields)


def test_the_pages_ask_for_every_translation_key_they_use() -> None:
    """新页面用到的每个 i18n key 都要在 `en.json` 里。

    前端自己有一条更严格的同类用例（`i18n/keys.test.ts`），这里只钉住重置这条
    路上的几个关键文案 —— 缺 key 时 i18next 不报错，只把 key 原样渲染给用户。
    """
    locales = PATHS_TS.parents[1] / "i18n" / "locales" / "en.json"
    english = json.loads(locales.read_text(encoding="utf-8"))
    for key in ("forgot.title", "forgot.sentBody", "reset.title", "reset.deadLinkTitle"):
        assert key in english
