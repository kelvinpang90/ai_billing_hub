"""Invariants of the Compose stack that must not regress silently.

这些断言的共同点是：**违反它们的改动都能正常启动**，所以只靠「跑一下看看」
发现不了。真正起栈的验证在 T0.6 的任务记录里，这里挡的是以后的回退。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = REPO_ROOT / "docker-compose.yml"
DOCKERFILE = REPO_ROOT / "Dockerfile"
NGINX_CONF = REPO_ROOT / "deploy" / "nginx" / "billing.conf"
NGINX_HEADERS = REPO_ROOT / "deploy" / "nginx" / "billing-proxy-headers.inc"

# ⚠️ 改这个数字，宿主机上主密钥文件的属主必须同步改（ADR-0004）。
APP_UID = "10001"

# spec §99 的服务清单，七个到齐（T0.7 补上 frontend）。
# 写死而不是「至少包含」：多一个服务也要是一次有意识的改动。
EXPECTED_SERVICES = {
    "mysql",
    "redis",
    "api",
    "celery-worker",
    "celery-beat",
    "frontend",
    "nginx",
}


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def instructions(path: Path) -> str:
    """Return the file with comment lines stripped.

    ⚠️ 不剥注释的断言是**假的**：把一行指令注释掉，`"COPY alembic" in text`
    照样为真，测试绿着而镜像里已经没有 alembic 了。变异测试抓到过这个。
    Dockerfile 与 nginx 配置都用 `#` 起注释，所以一个函数够用。
    """
    kept = [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    return "\n".join(kept)


def nginx_location_block(name: str) -> str:
    """Return the body of one `location` block, so assertions can't match the wrong one."""
    text = instructions(NGINX_CONF)
    start = text.index(f"location {name} {{")
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise AssertionError(f"unbalanced braces around `location {name}` in {NGINX_CONF}")


def test_compose_declares_exactly_the_expected_services(compose: dict) -> None:
    assert set(compose["services"]) == EXPECTED_SERVICES


def test_backend_services_run_as_the_documented_non_root_uid(compose: dict) -> None:
    """⚠️ 容器跑 root 时一切照常工作 —— 这正是它危险的地方。

    ADR-0004 的主密钥文件按这个 UID 设属主并给 `0400`；容器一旦改回 root，
    「只有这个进程读得到主密钥」的前提就没了，而且不会有任何报错。
    """
    dockerfile = instructions(DOCKERFILE)
    assert f"ARG APP_UID={APP_UID}" in dockerfile
    assert "USER ${APP_UID}:${APP_GID}" in dockerfile

    # compose 侧不许用 `user:` 把它顶回去。
    for name, service in compose["services"].items():
        assert "user" not in service, f"{name} overrides the image's USER"


def test_the_image_carries_alembic(compose: dict) -> None:
    """⚠️ alembic/ 与 alembic.ini **不在 wheel 里**（wheel 只打 `app*`）。

    漏 COPY 的镜像能正常起、能正常服务，只有在部署时执行迁移那一刻才报
    `No config file 'alembic.ini' found` —— 那时候通常已经在生产上了。
    """
    dockerfile = instructions(DOCKERFILE)
    assert "COPY alembic.ini" in dockerfile
    assert "COPY alembic ./alembic" in dockerfile


def test_migrations_do_not_run_as_a_startup_side_effect(compose: dict) -> None:
    """spec §98 要求迁移有明确顺序与回滚策略，§100 要求 API 可水平扩展。

    把 `alembic upgrade` 塞进启动命令，多实例会同时迁移同一个库。
    """
    assert "alembic upgrade" not in instructions(DOCKERFILE)
    for name, service in compose["services"].items():
        command = " ".join(service.get("command") or [])
        assert "alembic" not in command, f"{name} runs migrations on startup"


def test_api_healthcheck_probes_liveness_not_readiness(compose: dict) -> None:
    """⚠️ 用 /readyz 做容器健康检查，会把一次可恢复的数据库故障放大成 API 的
    滚动重启 —— /readyz 在数据库不可用时返回 503（见 app/api/health.py）。
    """
    test = " ".join(compose["services"]["api"]["healthcheck"]["test"])
    assert "/healthz" in test
    assert "/readyz" not in test


def test_no_password_is_hardcoded_in_the_compose_file(compose: dict) -> None:
    """仓库是公开的：密码只能来自 `.env`，不能有字面量。"""
    for name, service in compose["services"].items():
        for key, value in (service.get("environment") or {}).items():
            if "PASSWORD" in key.upper():
                assert str(value).startswith("${"), f"{name}.{key} is not read from the environment"


def test_only_the_edge_proxy_is_published_to_the_host(compose: dict) -> None:
    """MySQL / Redis / api 都不对宿主机开放，唯一入口是 nginx。"""
    published = {name for name, service in compose["services"].items() if service.get("ports")}
    assert published == {"nginx"}


def test_every_proxied_location_forwards_the_correlation_id() -> None:
    """§94 的关联 ID 要跨进程连起来，转发头少一处，那条链路在日志里就断了。"""
    assert "proxy_set_header X-Request-ID" in instructions(NGINX_HEADERS)

    # 每个 proxy_pass 都必须配一条 include，否则那个 location 少转发头。
    conf = instructions(NGINX_CONF)
    assert conf.count("proxy_pass") == conf.count(
        "include /etc/nginx/conf.d/billing-proxy-headers.inc;"
    )


def test_the_edge_serves_the_frontend_at_the_root() -> None:
    """T0.6 时 `location /` 是一句 503 占位，frontend 落地后必须真的转发过去。

    单独钉这一条，是因为 `EXPECTED_SERVICES` 里加了 frontend **并不能**说明
    边缘已经指向它 —— 服务起着、占位还在，是一个能跑通所有其他用例的状态。
    """
    block = nginx_location_block("/")
    assert "proxy_pass" in block
    assert "return 503" not in block


def test_the_signing_key_is_injected_as_a_file_not_an_environment_variable(compose: dict) -> None:
    """ADR-0004 第 2 节：密钥走文件，不走环境变量。

    ⚠️ 环境变量会进 `/proc/<pid>/environ`、崩溃转储，并被子进程继承。
    这条用例挡的是「图省事把密钥直接写成 env」——那样能跑，而且不会有任何报错。
    """
    backend_env = compose["x-backend"]["environment"]
    assert backend_env["BILLING_JWT_SECRET_FILE"].startswith("/run/secrets/")
    # 任何一个值看起来像密钥本身都不行。
    for key, value in backend_env.items():
        if "JWT" in key.upper() or "SECRET" in key.upper():
            assert str(value).startswith(("/run/secrets/", "${")), f"{key} must not carry a secret"
    assert "billing_jwt_key" in compose["secrets"]


def test_the_auth_endpoints_are_rate_limited_at_the_edge() -> None:
    """spec §53 的 `Login attempt rate limiting`。

    ⚠️ 主控必须在 nginx：登录为了不泄露「邮箱是否存在」，在用户不存在时**也会
    跑一次 Argon2**。没有边缘限流，换着不存在的邮箱发请求就是一条 CPU 放大通道，
    而按账号的锁定永远不会触发。
    """
    conf = instructions(NGINX_CONF)
    assert "limit_req_zone" in conf
    block = nginx_location_block("/api/v1/auth/")
    assert "limit_req zone=" in block


def test_readiness_is_not_exposed_to_the_public_internet() -> None:
    """/readyz 的响应体逐个报出依赖状态，等于公开内部拓扑与故障窗口。"""
    block = nginx_location_block("= /readyz")
    assert "deny all;" in block

    # /healthz 返回的是常量，不需要也不应该被限制 —— 外部探活要用它。
    assert "deny all;" not in nginx_location_block("= /healthz")


def test_nginx_access_log_keeps_query_strings_out() -> None:
    """⚠️ $request 带查询串，查询串里可能有密钥（§94）。应用侧同样只记 path。"""
    conf = instructions(NGINX_CONF)
    log_format = conf[conf.index("log_format billing_json") : conf.index("server_tokens")]
    assert "$uri" in log_format
    assert "$request," not in log_format and '"$request"' not in log_format
