"""Invariants of the Compose stack that must not regress silently.

这些断言的共同点是：**违反它们的改动都能正常启动**，所以只靠「跑一下看看」
发现不了。真正起栈的验证在 T0.6 的任务记录里，这里挡的是以后的回退。
"""

from __future__ import annotations

import re
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
    # 带前缀，见 docker-compose.yml 里 billing_nginx 那段注释。
    "billing_nginx",
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
    assert published == {"billing_nginx"}


def test_the_edge_config_is_mounted_as_a_directory_not_as_files(compose: dict) -> None:
    """⚠️ 单文件 bind mount 绑的是 inode：`git checkout` 用新文件替换旧文件之后，容器里
    看到的永远是旧的 —— reload 读到的也是旧内容。

    T0.9 在生产上踩过（安全响应头部署后外网没有，冒烟拦下回滚）。Linux 上用
    docker-in-docker 复现：挂文件的容器替换后仍读到 `old`，挂目录的读到 `new`。
    ⚠️ Windows 的 Docker Desktop 复现不出来，所以只有这条用例能在本地挡住回退。
    """
    volumes = compose["services"]["billing_nginx"]["volumes"]
    assert volumes == ["./deploy/nginx:/etc/nginx/conf.d:ro"]
    # 挂的是整个目录，其中每个 *.conf 都会被 nginx 加载：只许有 billing.conf 一个。
    confs = sorted(path.name for path in NGINX_CONF.parent.glob("*.conf"))
    assert confs == ["billing.conf"]


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


def test_the_credential_rotation_overlap_reaches_the_backend(compose: dict) -> None:
    """AIH-TASK-012 设计 §2「配置」要求重叠期可配置；compose 不读 `.env`，不转发就改不动。

    这条来自 AIH-TASK-013 的 Worker run：它要在运维手册里写「怎么改」，发现生产上
    改 `.env` 根本进不了容器。默认值必须与 `Settings` 一致，否则不设时两边各说各话。
    """
    default = Settings.model_fields["credential_rotation_overlap_seconds"].default
    env = compose["x-backend"]["environment"]

    assert str(env.get("BILLING_CREDENTIAL_ROTATION_OVERLAP_SECONDS")) == (
        f"${{BILLING_CREDENTIAL_ROTATION_OVERLAP_SECONDS:-{default}}}"
    )


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


def test_mysql_lets_the_app_account_create_triggers(compose: dict) -> None:
    """钱包账本的不可变与一一对应靠触发器（设计闸门 #88），迁移由应用账号执行。

    binlog 开着而这个开关关着时，建触发器报 ERROR 1419，迁移在生产上失败 ——
    而且 CI 用 root 连库，**CI 看不出来**。所以把开关钉在 compose 里，并由这条测试守住。
    """
    command = [str(part) for part in compose["services"]["mysql"]["command"]]
    assert "--log-bin-trust-function-creators=ON" in command


def test_ci_mysql_matches_production_for_triggers() -> None:
    """CI 的 MySQL service 也必须打开同一个开关，而且要在跑后端测试之前。

    service 容器传不进 mysqld 参数，所以 CI 用一个步骤设全局变量。少了它，迁移 0006
    的预检（开关关着就拒绝建表）会把 CI 上所有迁移用例拦下（Claude Code 审查 #91 发现）。
    """
    ci = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
    steps = yaml.safe_load(ci.read_text(encoding="utf-8"))["jobs"]["backend"]["steps"]
    names = [step.get("name", "") for step in steps]
    runs = [str(step.get("run", "")) for step in steps]
    wanted = "SET GLOBAL log_bin_trust_function_creators = ON"
    setter = next(i for i, run in enumerate(runs) if wanted in run)
    assert setter < names.index("Backend tests")


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


def test_the_api_gets_at_least_one_cpu(compose: dict) -> None:
    """⚠️ 2026-09-17 生产 VPS 实测：api 配额 0.5 核时，一次登录（Argon2）要 ~600 ms。

    Argon2 默认 parallelism=4，四个线程同时烧 CPU，配额越小节流越狠；
    调到 1.0 核之后降到 279 ms。
    那个 0.5 是按升配前的 1 核机器定的，升配后没人调 —— 这条守卫就是防它再悄悄退回去。
    见 docs/perf-baseline.md §9。
    """
    cpus = str(compose["services"]["api"]["cpus"])
    # 文件里是 `${BILLING_BACKEND_CPUS:-1.0}` 这种占位符，钉的是**默认值**
    default = re.fullmatch(r"\$\{BILLING_BACKEND_CPUS:-([0-9.]+)\}", cpus)
    assert default is not None, f"unexpected cpus value: {cpus}"
    assert float(default.group(1)) >= 1.0, f"api cpus default={default.group(1)}"


def test_beat_does_not_inherit_the_api_memory_limit(compose: dict) -> None:
    """beat 只把任务名丢进队列，不执行任务，给它和 api 一样的额度是浪费。

    ⚠️ 它从 `x-backend` 锚点继承 `mem_limit`，所以**不显式覆盖就会静默拿到 384m**。
    在一台只有 1.8 GB 可用的机器上，浪费的那部分是别人要用的。
    """
    beat = compose["services"]["celery-beat"]["mem_limit"]
    api = compose["services"]["api"]["mem_limit"]
    assert beat != api


def test_every_service_has_a_healthcheck(compose: dict) -> None:
    """七个服务都要有存活探针。

    ⚠️ 两个 celery 服务在 T0.9 之前**一个探针都没有**，而 beat 崩掉是本平台最
    安静的故障：API 正常、/readyz 正常、日志无错，只有周期任务不再发生 ——
    而「没发生」是没有信号的。
    """
    missing = [
        name for name, service in compose["services"].items() if "healthcheck" not in service
    ]
    assert missing == []


def test_the_beat_probe_does_not_go_through_the_broker(compose: dict) -> None:
    """⚠️ beat 的判据必须是它**自己**的调度状态，不能是 `celery inspect ping`。

    `inspect ping` 问的是 worker，够不着 beat；而且它走 broker 往返，Redis 一挂
    就会把 beat 也判成红的 —— 那会让「broker 挂了」和「beat 挂了」两件事混在
    同一个信号里，而它们的处置完全不同。
    """
    probe = " ".join(
        str(part) for part in compose["services"]["celery-beat"]["healthcheck"]["test"]
    )
    assert "beat-schedule" in probe
    assert "inspect" not in probe


def test_the_beat_probe_tolerates_a_cold_start(compose: dict) -> None:
    """起步宽限必须大于一个调度周期。

    ⚠️ beat 刚起来时还没派发过任何任务，文件时间戳停在启动时刻。没有这段宽限，
    它会在第一个周期内就被判不健康 —— 而那是**假警报**，最伤告警的可信度。
    """
    beat = compose["services"]["celery-beat"]["healthcheck"]
    assert beat["start_period"] == "90s"


def test_the_worker_probe_names_itself(compose: dict) -> None:
    """⚠️ 指名问自己，不要广播。

    广播式的 `inspect ping` 只要**有人**回应就算通过。将来跑多个 worker 时，
    一个死掉的 worker 会被它的同伴掩护，而探针一路绿着。
    """
    probe = " ".join(
        str(part) for part in compose["services"]["celery-worker"]["healthcheck"]["test"]
    )
    assert "-d" in probe
    # ⚠️ 两个 `$` 是必需的：单个会被 compose **在宿主机上**插值成空串。
    assert "$$HOSTNAME" in probe


# --- T0.9：接入 VPS 共享的 proxy_net -----------------------------------------

PROD_OVERRIDE = REPO_ROOT / "docker-compose.prod.yml"


@pytest.fixture(scope="module")
def prod_override() -> dict:
    return yaml.safe_load(PROD_OVERRIDE.read_text(encoding="utf-8"))


def test_the_published_port_binds_to_loopback_by_default(compose: dict) -> None:
    """⚠️ **Docker 发布的端口会绕过 UFW。**

    `8080:80` 这种写法即使 ufw 拒绝了 8080 也照样能从公网访问 —— 上线后任何人都能
    用 `http://<VPS>:8080` **明文**直连登录接口，完全绕开 infra_nginx 的 HTTPS。
    所以默认必须只绑 127.0.0.1。
    """
    ports = compose["services"]["billing_nginx"]["ports"]
    assert ports == ["${BILLING_HTTP_BIND:-127.0.0.1}:${BILLING_HTTP_PORT:-8080}:80"]


def test_only_the_edge_joins_the_shared_proxy_network(prod_override: dict) -> None:
    """只有边缘 nginx 挂 `proxy_net`，别的一个都不挂。

    ⚠️ `proxy_net` 上还挂着另外八个项目。api / mysql / redis 一旦挂上去，任何一个
    项目的容器被攻破都能直接够到我们的数据库与未经限流的 api。
    """
    joined = {
        name
        for name, service in prod_override["services"].items()
        if "proxy_net" in (service.get("networks") or [])
    }
    assert joined == {"billing_nginx"}
    assert prod_override["networks"]["proxy_net"]["external"] is True


def test_the_edge_keeps_its_default_network_in_production(prod_override: dict) -> None:
    """⚠️ 一个服务一旦写了 `networks:`，compose 就不再自动挂默认网络。

    只写 `proxy_net` 的话，nginx 够不着只在 default 上的 `api:8000` 与 `frontend:80`
    —— 部署成功、健康检查也过（它只问 nginx 自己），但每个请求都是 502。
    """
    assert "default" in prod_override["services"]["billing_nginx"]["networks"]


def test_the_edge_service_name_cannot_collide_on_the_shared_network(compose: dict) -> None:
    """⚠️ compose 把**服务名**注册成它所挂每个网络上的 DNS 别名。

    `vps_infra` 自己的服务就叫 `nginx`；我们也叫 `nginx` 的话，`proxy_net` 上同一个
    名字会解析到两个容器。`erp_os` 的 compose 里记着同一个坑。
    """
    assert "nginx" not in compose["services"]
    assert "billing_nginx" in compose["services"]


def test_production_backends_send_secure_cookies(compose: dict, prod_override: dict) -> None:
    """⚠️ 基础文件为了本地 http 能登录，把 cookie 的 Secure 关掉、环境标成 local。

    生产覆盖漏掉的话，会话 cookie 不带 Secure：用户只要访问一次 `http://`，浏览器就先把
    会话令牌明文发出去，才被 301 到 https。T0.9 首次部署前整理上线手册时发现的。

    后端服务从基础文件里**推出来**，不手写清单 —— 以后加一个后端服务、忘了在生产
    覆盖里加，这条会红。本地容器实测过：三个服务里 `get_settings()` 都是 production / True。
    """
    backends = {
        name
        for name, service in compose["services"].items()
        if "BILLING_SESSION_COOKIE_SECURE" in (service.get("environment") or {})
    }
    assert backends == {"api", "celery-worker", "celery-beat"}
    for name in backends:
        environment = prod_override["services"][name]["environment"]
        assert environment["BILLING_SESSION_COOKIE_SECURE"] == "true", name
        assert environment["BILLING_ENVIRONMENT"] == "production", name


SECURITY_HEADERS = (
    'add_header X-Frame-Options "DENY" always;',
    "add_header Content-Security-Policy \"frame-ancestors 'none'\" always;",
    'add_header X-Content-Type-Options "nosniff" always;',
    'add_header Referrer-Policy "strict-origin-when-cross-origin" always;',
    'add_header Strict-Transport-Security "max-age=31536000" always;',
)


def test_the_edge_sends_security_headers() -> None:
    """T0.9 首次部署后外网实测：页面与 API 响应上一个安全响应头都没有。

    缺 X-Frame-Options / frame-ancestors = 登录页可以被嵌进别人的 iframe 做点击劫持；
    缺 HSTS = 用户手敲 http:// 的那一次仍是明文。`always` 让错误响应也带上。
    """
    config = instructions(NGINX_CONF)
    for header in SECURITY_HEADERS:
        assert header in config, header


def test_no_location_silently_drops_the_security_headers() -> None:
    """⚠️ nginx 的继承规则：location 里只要写了一条 add_header，就**完全不再继承**
    server 层的所有 add_header。

    有人为了给某个路径加个 Cache-Control，会静默丢掉全部安全头 —— 而页面照常工作，
    没有任何报错。所以 add_header 只允许出现在第一个 location 之前；被 include 进每个
    location 的代理头文件里也不许有。
    """
    config = instructions(NGINX_CONF)
    first_location = config.index("location ")
    add_headers = [i for i in range(len(config)) if config.startswith("add_header", i)]
    assert add_headers, "no add_header at all"
    assert all(i < first_location for i in add_headers)
    assert "add_header" not in instructions(NGINX_HEADERS)


# --- 可信代理的范围（T0.9 2026-09-16 按实际拓扑收窄）-------------------------
#
# ⚠️ 这三处必须一起看：`set_real_ip_from`（谁能伪造 X-Forwarded-For）、
# `/readyz` 的 allow 名单、应用侧的 `BILLING_TRUSTED_PROXIES`。放宽任何一处，
# **栈照样起得来、请求照样通** —— 坏掉的是「按来源限流」和「审计里的 IP」，
# 而那两样只有在被人利用之后才看得出来。

STACK_SUBNET = "10.201.0.0/24"
PROXY_NET = "172.19.0.0/16"
# 收窄之前填的就是这三段；任何一段回来都意味着「这台机器上任何容器都可信」。
WIDE_RANGES = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")


def test_the_stack_subnet_is_pinned() -> None:
    """⚠️ 不钉死的话 docker 每次随手分一个 172.x —— 「可信代理是谁」就成了

    每台机器、每次重建都不一样的东西，只能拿三段 RFC1918 兜着。
    """
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    subnets = [entry["subnet"] for entry in compose["networks"]["default"]["ipam"]["config"]]
    # ⚠️ 字面量，**不给环境变量旋钮**：nginx 的 conf 读不到环境变量，`/readyz` 的
    # allow 名单只能抄一份。只对一半生效的旋钮比不给更糟（Codex 审查 PR #68）。
    assert subnets == [STACK_SUBNET]
    # ⚠️ 必须落在 docker 默认分配池（172.17–172.31）之外，否则会和同机其它项目抢。
    assert STACK_SUBNET.startswith("10.")


def test_the_app_only_trusts_its_own_stack_network() -> None:
    """⚠️ api 的直连对端只可能是本栈的 nginx。

    填成三段 RFC1918 等于「这台 VPS 上任何一个容器都能伪造 X-Forwarded-For」——
    而这台机器上还跑着另外八个项目。
    """
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    trusted = compose["services"]["api"]["environment"]["BILLING_TRUSTED_PROXIES"]
    assert trusted == STACK_SUBNET
    for wide in WIDE_RANGES:
        assert wide not in trusted


def test_nginx_only_trusts_the_proxy_network() -> None:
    """⚠️ 实测拓扑：infra_nginx 与本平台 nginx 在同一张 `proxy_net` 上直连容器。

    可信的只有那一张网 —— 多信一段，按来源限流就能被绕过、审计 IP 就能被伪造。
    """
    conf = NGINX_CONF.read_text(encoding="utf-8")
    directives = [
        line.strip() for line in conf.splitlines() if line.strip().startswith("set_real_ip_from")
    ]
    assert directives == [f"set_real_ip_from {PROXY_NET};"]
    # ⚠️ `real_ip_recursive` 保持默认 off：off 时取 X-Forwarded-For 的**最后一个**
    # 地址，那是上游代理亲自追加、客户端伪造不了的那个。
    assert "real_ip_recursive on" not in conf


def test_readyz_is_not_open_to_every_private_address() -> None:
    """⚠️ `/readyz` 的响应体逐个报出依赖状态，等于把内部拓扑与故障窗口告诉任何人。"""
    conf = NGINX_CONF.read_text(encoding="utf-8")
    block = conf[conf.index("location = /readyz") : conf.index("deny all;")]
    allowed = [line.strip() for line in block.splitlines() if line.strip().startswith("allow")]
    assert allowed == ["allow 127.0.0.0/8;", f"allow {STACK_SUBNET};", f"allow {PROXY_NET};"]
    assert "deny all;" in conf


def test_the_stack_subnet_is_the_same_string_everywhere() -> None:
    """⚠️ nginx 的 conf 读不到环境变量，所以本栈网段在那边是**抄**过去的。

    两处对不上时不会报错，只会让 `/readyz` 把宿主机自己的巡检也拒掉 ——
    而那表现成「巡检报 readyz 不可用」，看着像应用出了问题。
    """
    compose = COMPOSE.read_text(encoding="utf-8")
    conf = NGINX_CONF.read_text(encoding="utf-8")
    assert f"subnet: {STACK_SUBNET}" in compose
    # ⚠️ 任何形式的变量写法都不许回来：它只能改到 compose 那一半。
    assert "BILLING_STACK_SUBNET" not in compose
    assert f"allow {STACK_SUBNET};" in conf
