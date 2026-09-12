"""Invariants of the Compose stack that must not regress silently.

这些断言的共同点是：**违反它们的改动都能正常启动**，所以只靠「跑一下看看」
发现不了。真正起栈的验证在 T0.6 的任务记录里，这里挡的是以后的回退。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.core.config import Settings

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
    """仓库是公开的：密码只能来自 `.env`，不能有字面量。

    ⚠️ `*_FILE` 结尾的那些**是路径，不是密码**（`BILLING_SMTP_PASSWORD_FILE` 名字里
    也有 PASSWORD）。它们不归这一条管，但**没有被放过** —— 上面
    `test_every_credential_file_setting_points_at_a_mounted_secret` 要求它们必须指向
    真的挂了的 `/run/secrets/` 条目，所以往那种键里塞一个字面量密码同样会红。
    """
    for name, service in compose["services"].items():
        for key, value in (service.get("environment") or {}).items():
            if key.upper().endswith("_FILE"):
                continue
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


def test_every_credential_file_setting_is_wired_into_compose(compose: dict) -> None:
    """⚠️ **这条用例来自 PR #39 的一个阻断项。**

    T0.8d 给 `Settings` 加了 `smtp_password_file`、在 `.env.example` 里写明「密码走
    文件注入」，**却没在 compose 里挂那个 secret、也没设对应的环境变量**。后果：
    用常规「用户名 + 密码」认证的 SMTP 在 compose 部署下拿到的是**空密码**，
    `login()` 必然失败，outbox 一路重试到死信 —— 而用户那边的现象只是「没收到信」。

    ⚠️ **方向很要紧。**先写的那版是「遍历 compose 里的 `*_FILE` 键，检查它们指向
    真的挂了的 secret」—— 而那个缺陷恰恰是**键根本不存在**，于是用例在空集合上
    通过了。变异测试当场把它抓出来：把那一行删掉（= 被指出的原状），用例照样全绿。

    所以要从 **`Settings` 那一侧**问：每个 `*_file` 字段，compose 配了没有。
    以后再加第四把密钥，忘了配 compose 就会在这里红。
    """
    backend = compose["x-backend"]
    env = backend["environment"]
    mounted = set(backend["secrets"])

    file_fields = [name for name in Settings.model_fields if name.endswith("_file")]
    assert file_fields, "Settings 一个 *_file 字段都没有，这条用例在空集合上空转"

    for field in file_fields:
        key = f"BILLING_{field.upper()}"
        assert key in env, f"Settings.{field} 走文件注入，但 compose 没给它配 {key}"
        value = str(env[key])
        assert value.startswith("/run/secrets/"), f"{key} 应当指向挂载进来的 secret"
        name = value.removeprefix("/run/secrets/")
        assert name in compose["secrets"], f"{key} 指向 {name}，但 secrets: 里没有它"
        assert name in mounted, f"{name} 定义了却没挂给后端，容器里那个路径不存在"


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


# --- T0.9：资源限额与边缘代理 ------------------------------------------------


def test_every_service_has_a_memory_limit(compose: dict) -> None:
    """七个服务都要有内存上限（ADR-0002 的收口条件）。

    ⚠️ 这不是洁癖。生产 VPS 只有 3.6 GB 且已经在用 swap，上面还跑着另外七个项目
    （见 docs/deployment.md §3.1）。少一个限额，那个容器就能把 MySQL 挤出内存 ——
    而表现出来的是「数据库莫名其妙重启」，一条完全指不回原因的现象。

    ⚠️ 三个后端服务的限额来自 `x-backend` 锚点，所以删掉锚点里那一行会**一次
    干掉三个**，而这条用例会红。
    """
    missing = [name for name, service in compose["services"].items() if "mem_limit" not in service]
    assert missing == []


def test_redis_caps_its_own_memory_too(compose: dict) -> None:
    """⚠️ 只设容器 mem_limit 不够，Redis 自己也要知道天花板。

    少了 `maxmemory`，Redis 会一直收数据直到被 OOM kill —— 进程整个消失。
    设了它才会在自己那一侧按策略淘汰。
    """
    command = " ".join(str(part) for part in compose["services"]["redis"]["command"])
    assert "--maxmemory" in command
    assert "--maxmemory-policy" in command


def test_mysql_pins_its_buffer_pool(compose: dict) -> None:
    """buffer pool 必须显式钉住，否则换台大内存机器它会自己长大然后撞限额。

    撞限额的现象是「数据库随机重启」，同样指不回原因。
    """
    command = " ".join(str(part) for part in compose["services"]["mysql"]["command"])
    assert "--innodb-buffer-pool-size" in command


def test_the_edge_resolves_the_real_client_address() -> None:
    """⚠️ 生产上本平台的 nginx 接在 infra_nginx 后面（T0.9 决策 ①A）。

    没有这一段的话三处一起坏，而且**全是静默的**：按来源限流退化成全局限流、
    `/readyz` 的网段限制形同虚设、审计里的 ip_address 全是同一个值。
    """
    config = instructions(NGINX_CONF)
    assert "real_ip_header X-Forwarded-For;" in config
    assert "set_real_ip_from" in config


def test_the_edge_does_not_trust_a_recursive_forwarded_chain() -> None:
    """⚠️ 反直觉的一条：`real_ip_recursive` 必须保持 off（即不出现）。

    off 时 nginx 取 X-Forwarded-For 的**最后一个**地址 —— 那是上游代理亲自追加的、
    客户端伪造不了的那个。开了 recursive 反而要依赖「可信名单写得够准」才安全，
    而名单一宽，客户端塞进去的伪造地址就可能被当成真的。
    """
    config = instructions(NGINX_CONF)
    assert "real_ip_recursive" not in config


def test_the_edge_fails_fast_when_the_backend_is_down() -> None:
    """⚠️ `proxy_connect_timeout` 的默认值是 60 秒。

    T0.7 实测：api 停掉时边缘要 3.96 秒才回 502。不收紧的话，一次后端停机在用户
    侧表现成「点了没反应」而不是「报错」，而且每个挂着的请求都占着 nginx 一条连接。
    """
    config = instructions(NGINX_CONF)
    assert "proxy_connect_timeout" in config
    assert "resolver_timeout" in config


def test_celery_pins_its_concurrency(compose: dict) -> None:
    """⚠️ celery 的默认并发 = 宿主机 CPU 核数，而每个 prefork 子进程都会把整个
    应用 import 一遍。配合 `mem_limit`，内存占用就跟着**跑在哪台机器**变。

    这条是被真实缺陷逼出来的：本地（20 核）第一次加上限额起栈，worker 起了 20
    个子进程、崩溃重启 4 次，而 `docker inspect` 的 OOMKilled 还是 false ——
    被杀的是子进程，主进程自己退了 0。一条完全指不回原因的现象。
    """
    command = " ".join(str(part) for part in compose["services"]["celery-worker"]["command"])
    assert "--concurrency" in command


def test_beat_does_not_inherit_the_api_memory_limit(compose: dict) -> None:
    """beat 只把任务名丢进队列，不执行任务，给它和 api 一样的额度是浪费。

    ⚠️ 它从 `x-backend` 锚点继承 `mem_limit`，所以**不显式覆盖就会静默拿到 384m**。
    在一台只有 1.8 GB 可用的机器上，浪费的那部分是别人要用的。
    """
    beat = compose["services"]["celery-beat"]["mem_limit"]
    api = compose["services"]["api"]["mem_limit"]
    assert beat != api
