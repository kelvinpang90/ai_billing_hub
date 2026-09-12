"""Phase 0 的容量基线测量（spec §119）。

spec §119 给的那组 V1 目标写明是「**to be validated and adjusted through a
documented Phase 0 capacity baseline**」—— 这个脚本就是产出那份基线的工具，
结果写进 `docs/perf-baseline.md`。

⚠️ **它量的不是 §119 里那几条目标本身。**那几条说的是用量事件与钱包变更，
两者在 Phase 0 都还不存在。凭空造一个假的用量事件去量，得到的是一个**看起来
像基线的数字**，而它与将来真实的那条路径没有关系 —— 比没有基线更糟。

所以这里只量**现在真实存在、且将来仍在关键路径上**的东西：

  S1  Argon2id 的哈希 / 校验开销 —— 登录的 CPU 上限，也是**内存**上限
  S2  一次密码重置申请的写入路径 —— Phase 0 里最接近 §119「durable acceptance」
      写入形状的东西（三张表 + 一次提交），并发扫描用来定连接池大小
  S3  outbox 的领取 / 结算吞吐 —— §119「Backlog recovery >= 5x peak」那条将来
      就落在这套机制上
  S4  经 nginx 的请求地板 —— 客户端观测到的最低延迟

⚠️ **认证端点刻意不做 HTTP 压测。**它们带按来源限流（默认 10/min、突发 20），
打到第 21 个就是 429 —— 压出来的是限流器的形状，不是服务的容量。而**为压测
加一个「关掉限流」的开关是不行的**：那种开关一旦存在，早晚有人在生产上打开
（与 ADR-0009 拒绝「允许明文 SMTP」开关同一个理由）。所以 Argon2 在进程内量，
而限流器本身作为一条容量事实记进文档。

用法（`scripts/` 不在镜像里，要挂进去）：

    docker compose run --rm \\
      -v "$PWD/scripts:/srv/billing/scripts:ro" \\
      api python scripts/perf_baseline.py

⚠️ **它会往库里写垃圾行**，所以拒绝在 `BILLING_ENVIRONMENT=production` 下运行。
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import get_settings
from app.core.database import create_database_engine, create_session_factory
from app.core.passwords import hash_password, verify_password
from app.models.auth import (
    DomainOutbox,
    OutboxStatus,
    PasswordResetToken,
    User,
    UserRole,
    UserStatus,
)
from app.services.auth import RequestContext, utc_now
from app.services.password_reset import _start_reset, request_reset
from app.tasks import outbox as outbox_task

# 这个账号只在基线测量里用，跑完删掉。
#
# ⚠️ 域名用 `example.com`（RFC 2606 留给文档用），**不是 `.invalid`**。
# `.invalid` 也是保留 TLD，看起来更贴切，但 `normalise_email` 会拒绝它 ——
# 而 `_start_reset` 拒绝之后**立刻返回、根本不碰数据库**。第一版就是这么写的，
# 结果整组 S2 报出 0.02ms 的漂亮数字，测的却是一次提前返回。
# 下面 `_assert_writes_rows` 那道守卫就是为这件事加的。
#
# ⚠️ **每次跑都换一个地址**（复审第一轮的阻断项）。第一版是固定地址 + 「有就
# 复用」，而清理阶段是**无条件删除** —— 库里若已经有同名账号，这个脚本会连它的
# 审计记录（§66）一起删掉。改成每轮唯一之后，我们只可能删掉自己刚建的那一个。
BASELINE_EMAIL_TEMPLATE = "perf-baseline+{run}@example.com"
BASELINE_PASSWORD = "a throwaway baseline passphrase"

CONTEXT = RequestContext(ip_address="127.0.0.1", user_agent="perf-baseline")


# --------------------------------------------------------------------------
# 统计
# --------------------------------------------------------------------------


@dataclass
class Timings:
    """一组耗时样本，以及从中读出来的东西。

    ⚠️ **p99 需要的样本量比 p95 大一个量级。**样本不够时报出来的 p99 就是
    「最慢的那一次」，它随便一次 GC 或一次页错误就会跳一倍 —— 看着像测量结果，
    其实是噪声。所以下面每个场景都显式记了 `samples`，文档里也照抄。
    """

    label: str
    seconds: list[float]
    concurrency: int = 1
    notes: str = ""
    # 整批的墙钟耗时。⚠️ 吞吐必须由它算，**不能拿 `concurrency / p50` 倒推** ——
    # 尾部一长，那个倒推值会比真实吞吐高出好几倍，而它看起来一样可信。
    wall_seconds: float = 0.0

    @property
    def samples(self) -> int:
        return len(self.seconds)

    def percentile(self, fraction: float) -> float:
        ordered = sorted(self.seconds)
        # 最近秩法。`ceil(n*p)` 的下标，夹在两端之内。
        index = min(len(ordered) - 1, max(0, int(-(-len(ordered) * fraction // 1)) - 1))
        return ordered[index]

    def as_row(self) -> dict[str, object]:
        return {
            "scenario": self.label,
            "concurrency": self.concurrency,
            "samples": self.samples,
            "p50_ms": round(self.percentile(0.50) * 1000, 2),
            "p95_ms": round(self.percentile(0.95) * 1000, 2),
            "p99_ms": round(self.percentile(0.99) * 1000, 2),
            "max_ms": round(max(self.seconds) * 1000, 2),
            "per_second": round(self.samples / self.wall_seconds, 1) if self.wall_seconds else None,
            "notes": self.notes,
        }


def measure(
    label: str,
    work: Callable[[int], None],
    *,
    samples: int,
    warmup: int,
    concurrency: int = 1,
    notes: str = "",
) -> Timings:
    """跑 `work`，量每一次的耗时。

    ⚠️ **热身不是礼貌，是必需的。**第一次哈希要加载 76 KB 的弱口令表，第一次
    查询要建连接、第一次走 SQLAlchemy 的某条路径要编译 SQL —— 把这些算进样本，
    p99 量到的是「冷启动」，而那件事一个进程只发生一次。
    """
    for i in range(warmup):
        work(-1 - i)

    seconds: list[float] = []
    batch_started = time.perf_counter()
    if concurrency == 1:
        for i in range(samples):
            started = time.perf_counter()
            work(i)
            seconds.append(time.perf_counter() - started)
        return Timings(label, seconds, 1, notes, time.perf_counter() - batch_started)

    def timed(i: int) -> float:
        started = time.perf_counter()
        work(i)
        return time.perf_counter() - started

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        seconds = list(pool.map(timed, range(samples)))
    return Timings(label, seconds, concurrency, notes, time.perf_counter() - batch_started)


# --------------------------------------------------------------------------
# 场景
# --------------------------------------------------------------------------


@dataclass
class Baseline:
    environment: dict[str, object] = field(default_factory=dict)
    rows: list[dict[str, object]] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)

    def add(self, timings: Timings) -> Timings:
        row = timings.as_row()
        self.rows.append(row)
        print(
            f"  {row['scenario']:<46} c={row['concurrency']:<3} n={row['samples']:<5} "
            f"p50={row['p50_ms']:>9} p95={row['p95_ms']:>9} p99={row['p99_ms']:>9} ms "
            f"| {row['per_second']:>8}/s"
        )
        return timings


def describe_environment(session_factory) -> dict[str, object]:
    """⚠️ spec §119 明确要求报出硬件与数据集规模，不是可选项。

    一份没写清楚「在什么机器上、多少数据、跑多久」的基线，读的人无法判断它
    能不能外推到生产 —— 那样的数字只会被当成结论引用，然后错得很有信心。
    """
    with session_factory() as session:
        mysql_version = session.execute(text("SELECT VERSION()")).scalar_one()
        users = session.execute(text("SELECT COUNT(*) FROM users")).scalar_one()
        outbox = session.execute(text("SELECT COUNT(*) FROM domain_outbox")).scalar_one()
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "mysql": mysql_version,
        "dataset_users": users,
        "dataset_outbox_rows": outbox,
    }


def scenario_argon2(baseline: Baseline, samples: int) -> None:
    """S1 —— 登录的 CPU 与内存上限。

    ⚠️ 这一项**只量、不调**。Argon2 的参数是认证与会话的安全参数（设计闸门
    #32 的范畴），要改必须重新过闸门 —— 在一个性能任务里顺手调低它，是把一次
    安全决策伪装成性能优化。
    """
    from argon2 import PasswordHasher

    params = PasswordHasher()
    baseline.findings.append(
        f"Argon2id 参数：time_cost={params.time_cost}、"
        f"memory_cost={params.memory_cost} KiB（{params.memory_cost / 1024:.0f} MiB）、"
        f"parallelism={params.parallelism}（argon2-cffi 默认值，未调整）"
    )

    encoded = hash_password(BASELINE_PASSWORD)
    baseline.add(
        measure(
            "S1a Argon2id hash（设置密码）",
            lambda _: hash_password(BASELINE_PASSWORD),
            samples=samples,
            warmup=5,
            notes="登录不走这条；注册 / 重置走",
        )
    )
    baseline.add(
        measure(
            "S1b Argon2id verify（每次登录）",
            lambda _: verify_password(encoded, BASELINE_PASSWORD),
            samples=samples,
            warmup=5,
            notes="登录的 CPU 下限，密码对错都要付",
        )
    )


def _row_count(session_factory, table: str) -> int:
    """⚠️ 表名只允许来自本文件里的字面量 —— 它是直接拼进 SQL 的。"""
    with session_factory() as session:
        return int(session.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one())


def _assert_writes_rows(session_factory, settings, email: str) -> None:
    """跑 S2 之前先确认它**真的写了行**。

    ⚠️ 这道守卫不是防御性编程，是**被真实缺陷逼出来的**。`_start_reset` 在
    「邮箱解析不了」「账号不存在」「账号已停用」三种情况下都会**立刻返回 None、
    一行不写** —— 而那条路径快到 0.02ms，于是脚本会报出一个漂亮得离谱、却什么
    也没测的 p50。一个测量了空气的自信数字，比没有基线更糟：它会被人引用。

    所以这里先跑一次、数一下行数，没写就直接失败。
    """
    with session_factory() as session:
        before = session.execute(text("SELECT COUNT(*) FROM password_reset_tokens")).scalar_one()
    outbox_id = _start_reset(session_factory, settings, email=email, context=CONTEXT, now=None)
    with session_factory() as session:
        after = session.execute(text("SELECT COUNT(*) FROM password_reset_tokens")).scalar_one()
    if outbox_id is None or after <= before:
        raise RuntimeError(
            "S2 measured nothing: _start_reset returned without writing a row. "
            f"Check that {email} exists, is ACTIVE, and parses as an address."
        )


def scenario_reset_write(
    baseline: Baseline, session_factory, settings, email: str, concurrencies
) -> None:
    """S2 —— 一次密码重置申请的写入路径（三张表 + 一次提交）。

    Phase 0 里最接近 §119「durable acceptance」写入形状的东西。并发扫描的用途
    是找连接池的拐点：池默认 5 + 溢出 10 = 15，超过之后请求要排队等连接。
    """

    def start_reset(_: int) -> None:
        _start_reset(session_factory, settings, email=email, context=CONTEXT, now=None)

    _assert_writes_rows(session_factory, settings, email)

    for concurrency in concurrencies:
        # ⚠️ 行数要在**每一轮开跑前**数。整个脚本跑下来这张表会涨到上千行，
        # 拿开头那一次的计数去描述后面每一轮，等于报了一个错的数据集规模 ——
        # 而 spec §119 要求报它，正是为了让读的人能判断数字能不能外推。
        rows = _row_count(session_factory, "password_reset_tokens")
        baseline.add(
            measure(
                "S2 密码重置写入路径（token+outbox+audit，一次提交）",
                start_reset,
                samples=max(60, concurrency * 20),
                warmup=5,
                concurrency=concurrency,
                notes=f"不含反枚举的 120ms 地板；开跑时表内 {rows} 行",
            )
        )

    # 对照：客户端真正经历的那一个（含地板）。
    def full_request(_: int) -> None:
        request_reset(session_factory, settings, email=email, context=CONTEXT)

    baseline.add(
        measure(
            "S2b 同上，含反枚举的固定耗时地板",
            full_request,
            samples=40,
            warmup=2,
            notes="地板 120ms；这是客户端观测值",
        )
    )


def scenario_outbox_drain(
    baseline: Baseline, session_factory, settings, email: str, rows: int
) -> None:
    """S3 —— outbox 的领取 / 结算吞吐。

    §119 的「Backlog recovery throughput >= 5x peak」将来就落在这套机制上，所以
    它现在能跑多快是一条**会被将来引用**的基线。

    ⚠️ 传输层换成一个什么都不做的假实现：量的是**我们这一侧**（领取的条件更新、
    结算、提交），不是某个 SMTP 服务器有多快。
    """

    class NullTransport:
        def send(self, _message) -> bool:
            return True

    # ⚠️ 三样都要换，换漏一样这个场景就在**别的库**上跑。
    #
    # `deliver()` 不用调用方给的 session_factory：它调 `outbox_task._session_factory()`，
    # 那个函数带 `lru_cache`、自己按全局 `get_settings()` 建引擎 —— 也就是**栈自己
    # 那个库**。只换 `build_transport` 的话，S3 往测量库写行、`deliver()` 却去栈的
    # 库里读，两边根本不是一张表。
    #
    # 这不是推演出来的：隔离改完第一次跑就报 `Table 'billing.domain_outbox'
    # doesn't exist` —— 反过来说明**在隔离之前，投递一直打在栈的库上**。
    # `tests/backend/test_outbox.py` 的 `wired` 夹具早就这么做了，我漏看了。
    original_transport = outbox_task.build_transport
    original_factory = outbox_task._session_factory
    original_settings = outbox_task.get_settings
    outbox_task.build_transport = lambda _settings: NullTransport()  # type: ignore[assignment]
    outbox_task._session_factory = lambda: session_factory  # type: ignore[assignment]
    outbox_task.get_settings = lambda: settings  # type: ignore[assignment]
    try:
        with session_factory() as session:
            user_id = session.execute(select(User.id).where(User.email == email)).scalar_one()
            for _ in range(rows):
                session.add(
                    DomainOutbox(
                        event_type="PASSWORD_RESET_REQUESTED",
                        aggregate_type="users",
                        aggregate_id=str(user_id),
                        payload_json=json.dumps({"to": email, "token": "x" * 43}),
                        status=OutboxStatus.PENDING,
                        attempt_count=0,
                        next_retry_at=utc_now(),
                        created_at=utc_now(),
                    )
                )
            session.commit()
            ids = list(
                session.execute(
                    select(DomainOutbox.id).where(DomainOutbox.status == OutboxStatus.PENDING)
                ).scalars()
            )

        pending = iter(ids)
        table_rows = _row_count(session_factory, "domain_outbox")
        baseline.add(
            measure(
                "S3 outbox 一行的领取 + 投递 + 结算",
                lambda _: outbox_task.deliver(next(pending)),
                samples=min(rows - 5, 200),
                warmup=5,
                notes=f"传输层是空实现；开跑时表内 {table_rows} 行",
            )
        )
    finally:
        outbox_task.build_transport = original_transport  # type: ignore[assignment]
        outbox_task._session_factory = original_factory  # type: ignore[assignment]
        outbox_task.get_settings = original_settings  # type: ignore[assignment]


def scenario_http_floor(baseline: Baseline, base_url: str, concurrencies) -> None:
    """S4 —— 经 nginx 的请求地板。

    `/healthz` 不碰数据库、不碰 Redis，所以量到的是**路径本身**的开销：
    nginx 转发 + ASGI + 中间件（request id、日志、错误处理）。任何业务端点都
    不可能比它快，它是读其它数字时的零点。
    """

    def get_healthz(_: int) -> None:
        with urllib.request.urlopen(f"{base_url}/healthz", timeout=10) as response:
            response.read()

    try:
        get_healthz(0)
    except (urllib.error.URLError, OSError) as error:
        baseline.findings.append(f"⚠️ S4 跳过：{base_url} 连不上（{error}）")
        return

    for concurrency in concurrencies:
        baseline.add(
            measure(
                "S4 GET /healthz（经 nginx，不碰任何依赖）",
                get_healthz,
                samples=max(200, concurrency * 50),
                warmup=20,
                concurrency=concurrency,
                notes="路径开销的零点",
            )
        )


# --------------------------------------------------------------------------
# 夹具与清理
# --------------------------------------------------------------------------


def create_baseline_user(session_factory, email: str) -> None:
    """建这一轮专用的账号。

    ⚠️ **地址已经存在就直接失败，绝不复用**（复审第一轮的阻断项）。下面的
    `cleanup` 会把这个账号连同它的审计记录一起删掉 —— 复用一个别人的账号，
    等于顺手销毁了它的审计轨迹（§66）。地址每轮唯一，撞上只可能是真撞了。
    """
    with session_factory() as session:
        existing = session.execute(select(User.id).where(User.email == email)).scalar_one_or_none()
        if existing is not None:
            raise RuntimeError(
                f"{email} already exists; refusing to reuse an account this script will delete"
            )
        session.add(
            User(
                email=email,
                password_hash=hash_password(BASELINE_PASSWORD),
                role=UserRole.ADMIN,
                status=UserStatus.ACTIVE,
                created_at=utc_now(),
                updated_at=utc_now(),
            )
        )
        session.commit()


def cleanup(session_factory, email: str) -> None:
    """把这次测量写进去的东西删干净。

    ⚠️ 顺序是「先子后父」：`password_reset_tokens` 有指向 `users` 的外键。
    反过来删会在外键上报错，而那时候前面的删除已经提交了一半。

    ⚠️ 删的范围只到**这一轮自己建的那个账号**。地址每轮唯一（见
    `create_baseline_user`），所以 `aggregate_id` / `actor_user_id` 命中的行
    必然是这一轮写的。
    """
    with session_factory() as session:
        user_id = session.execute(select(User.id).where(User.email == email)).scalar_one_or_none()
        if user_id is not None:
            session.execute(delete(PasswordResetToken).where(PasswordResetToken.user_id == user_id))
            session.execute(delete(DomainOutbox).where(DomainOutbox.aggregate_id == str(user_id)))
            session.execute(
                text("DELETE FROM audit_logs WHERE actor_user_id = :uid"), {"uid": user_id}
            )
            session.execute(delete(User).where(User.id == user_id))
        session.commit()


def _measurement_settings(raw_url: str, settings) -> object:
    """把测量用的库换成调用方显式指定的那一个。

    ⚠️ **不允许就是栈自己那个库**（复审第一轮的阻断项）。S2 会写出上千行状态为
    `PENDING`、payload 里带着**真实重置令牌**的 outbox 行；而 compose 里的
    celery-beat 每 60 秒跑一次恢复扫描，worker 会在**它自己的进程**里投递它们 ——
    脚本里那个 `NullTransport` 只换掉了脚本自己进程的传输层，对 worker 毫无作用。

    本地栈刻意不配 SMTP，所以这件事在本地看不见；**配了 SMTP 的环境会真的把上千
    封带令牌的信发出去**。这正是「本地没报错」最危险的那一类：缺陷被环境的另一个
    缺省值挡住了。

    根治的办法不是在脚本里再加一层开关，而是**让测量根本不和 worker 共用一个库**。
    """
    if not raw_url:
        raise SystemExit(
            "--database-url is required. It must NOT be the stack's own database: "
            "the celery worker attached to that one will deliver the outbox rows this "
            "script writes. See docs/perf-baseline.md section 6 for the throwaway schema."
        )
    if settings.database_url and _same_target(raw_url, settings.database_url):
        raise SystemExit(
            "refusing to measure against the stack's own database "
            "(BILLING_DATABASE_URL): its celery worker would deliver the reset tokens "
            "this script writes. Use a separate schema; see docs/perf-baseline.md."
        )
    return settings.model_copy(update={"database_url": raw_url})


def _same_target(left: str, right: str) -> bool:
    """两个连接串是不是指向**同一个 schema**。

    ⚠️ **比的是归一化之后的目标，不是原始字符串**（复审第二轮的阻断项）。
    第一版直接比字符串，而 `...@mysql:3306/billing` 与
    `...@mysql:3306/billing?charset=utf8mb4` 是同一个 schema 的两种写法 ——
    去掉一个查询参数就能绕过整道防线，而后果是往运行中的库里写真实重置令牌。

    只看 host / port / database：用户名、密码与查询参数都不改变「写到哪张表」。

    ⚠️ **它挡不住同一台机器的不同写法**（`mysql` 与 `127.0.0.1`）。那一层由
    `_assert_distinct_server` 在真的连上之后用服务端自己的身份兜底 —— URL 比对
    只能做到「形状不同但目标相同」这一层，再往上必须问服务器。
    """
    a, b = make_url(left), make_url(right)
    return (
        (a.host or "").lower() == (b.host or "").lower()
        and (a.port or 3306) == (b.port or 3306)
        and a.database == b.database
    )


def _identity(engine) -> tuple[str, str] | None:
    """问服务器：你是谁、我现在在哪个库里。连不上就返回 None。"""
    try:
        with engine.connect() as connection:
            row = connection.execute(text("SELECT @@server_uuid, DATABASE()")).one()
    except SQLAlchemyError:
        return None
    return (str(row[0]), str(row[1]))


def _assert_distinct_server(measurement_engine, settings) -> None:
    """连上之后再确认一次：测量库和栈的库**不是同一个 schema**。

    ⚠️ 这道和 `_same_target` 不是重复，它们挡的是不同的东西：前者比 URL 的形状，
    只能发现「写法不同、目标相同」；这一道问的是**服务器自己**（`@@server_uuid`
    加上当前 database 名），所以连 `mysql` 与 `127.0.0.1` 这种同机不同写法也挡得住。

    URL 比对永远追不上所有等价写法 —— 复审第二轮指出的 `?charset=utf8mb4` 只是
    其中一种。能给出确定答案的只有服务器本身。

    ⚠️ 栈的库连不上时**放行**：那种情况下本来也没有 worker 在跑，而让一个连不上
    的依赖去阻断测量，只会让人把这道检查整个绕过去。
    """
    if not settings.database_url:
        return
    stack_engine = create_engine(settings.database_url, pool_pre_ping=True)
    try:
        stack = _identity(stack_engine)
    finally:
        stack_engine.dispose()
    if stack is None:
        return
    if _identity(measurement_engine) == stack:
        raise SystemExit(
            "refusing to measure against the stack's own database: the server reports "
            f"the same instance and schema ({stack[1]}). Its celery worker would deliver "
            "the reset tokens this script writes. See docs/perf-baseline.md."
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python scripts/perf_baseline.py")
    parser.add_argument("--samples", type=int, default=200, help="S1 的样本数")
    parser.add_argument("--outbox-rows", type=int, default=300)
    parser.add_argument("--base-url", default="http://nginx")
    parser.add_argument("--json-out", default=None, help="把结果另存成 JSON")
    parser.add_argument(
        "--database-url",
        default="",
        help="测量专用的库。⚠️ 必填，且不能是栈自己那个（见 _measurement_settings）",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    if settings.environment == "production":
        # ⚠️ 这个脚本往库里写垃圾行。生产上跑它不是「慢一点」，是脏数据。
        print("refusing to run against BILLING_ENVIRONMENT=production", file=sys.stderr)
        return 2

    settings = _measurement_settings(args.database_url, settings)
    engine = create_database_engine(settings)
    _assert_distinct_server(engine, get_settings())
    session_factory = create_session_factory(engine)

    baseline = Baseline()
    baseline.environment = describe_environment(session_factory)
    baseline.environment["pool_size"] = engine.pool.size()
    baseline.environment["pool_max_overflow"] = engine.pool._max_overflow
    print(json.dumps(baseline.environment, indent=2, ensure_ascii=False))
    print()

    email = BASELINE_EMAIL_TEMPLATE.format(run=uuid.uuid4().hex[:12])
    baseline.environment["baseline_account"] = email
    create_baseline_user(session_factory, email)
    try:
        scenario_argon2(baseline, args.samples)
        scenario_reset_write(baseline, session_factory, settings, email, (1, 2, 4, 8, 16, 32))
        scenario_outbox_drain(baseline, session_factory, settings, email, args.outbox_rows)
        scenario_http_floor(baseline, args.base_url.rstrip("/"), (1, 8, 32))
    finally:
        cleanup(session_factory, email)

    baseline.environment["dataset_after_run"] = {
        "users": _row_count(session_factory, "users"),
        "password_reset_tokens": _row_count(session_factory, "password_reset_tokens"),
        "domain_outbox": _row_count(session_factory, "domain_outbox"),
        "audit_logs": _row_count(session_factory, "audit_logs"),
    }
    print()
    print(json.dumps({"dataset_after_run": baseline.environment["dataset_after_run"]}, indent=2))

    print()
    for finding in baseline.findings:
        print(f"  * {finding}")

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "environment": baseline.environment,
                    "rows": baseline.rows,
                    "findings": baseline.findings,
                },
                handle,
                indent=2,
                ensure_ascii=False,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
