"""Application settings, read from environment variables.

所有配置只有这一个入口。仓库是公开的，默认值里**不许**出现任何真实主机名、
凭据或密钥 —— 需要密钥的配置项在用到它的那个任务里加，并走 Docker secrets
文件注入（ADR-0004），不走环境变量。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "production"]


class Settings(BaseSettings):
    """Environment-driven settings. See .env.example for the local template."""

    model_config = SettingsConfigDict(
        env_prefix="BILLING_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Environment = "local"
    debug: bool = False
    log_level: str = "INFO"

    # 空串 = 未配置。默认值里**不许**出现任何真实主机名或凭据（仓库是公开的），
    # 所以这里不能给一个「看起来能用」的默认连接串。未配置时 `/readyz` 会明确
    # 报 DATABASE_NOT_CONFIGURED，而不是拿着假地址去连然后超时。
    database_url: str = ""

    # 同样是空串 = 未配置。⚠️ Redis **不是**就绪阻断项：spec §74.6 规定
    # 「Redis/Celery 只承载投递触发，数据库 Outbox 才是可恢复的事实来源」，
    # REQ-AVAIL-001 又要求 Redis 不可用不得成为终端 AI 请求路径上的同步依赖。
    # 因为 Redis 挂了就把 API 摘出轮转，恰好制造出 Invariant 1 要防的那种中断。
    redis_url: str = ""

    # --- 认证（T0.8，设计闸门 Issue #32 v5） ---------------------------------

    # 访问令牌签名密钥所在的**文件路径**，不是密钥本身。
    # ⚠️ 密钥绝不走环境变量（ADR-0004 第 2 节）：环境变量会进 /proc/<pid>/environ、
    # 崩溃转储，并被子进程继承。生产上由 Docker secret 挂成文件。
    # 空串 = 未配置：应用照常启动、`/healthz` 照常应答，但认证端点明确报
    # AUTH_NOT_CONFIGURED —— 与数据库未配置时同一种处置。
    # **刻意没有「没配就临时生成一个」的兜底**：那会让配置缺失变成静默的，
    # 而且每次重启都让所有令牌失效。
    jwt_secret_file: str = ""

    # 信封加密的主密钥（KEK）所在的**文件路径**，同样不走环境变量
    # （ADR-0004 第 2 节）。文件里一行一把 `版本:base64`，版本号最大的
    # 那把用于加密 —— 多把并存是为了轮换时老数据仍能解开。
    # 空串 = 未配置：应用照常启动，但 2FA 相关端点明确报
    # ENCRYPTION_NOT_CONFIGURED，**不会临时造一把密钥**。
    master_key_file: str = ""

    # 访问令牌短寿命是刻意的：吊销作用在刷新令牌上，访问令牌靠过期自然失效。
    # 这意味着「吊销后最多还有这么久旧令牌可用」，是明确接受的取舍。
    access_token_ttl_seconds: int = 600
    # 刷新令牌的绝对寿命与闲置上限。计费后台不需要长会话。
    refresh_token_ttl_seconds: int = 43_200
    refresh_token_idle_seconds: int = 1_800

    # 两步登录中间那张 pending 令牌的寿命。**两条路径刻意不同**（设计闸门 #37）：
    #
    # - 日常登录（已启用 2FA）：掏出手机输 6 位数，120 秒绰绰有余
    # - ADMIN 首次登录（还要当场注册 2FA）：扫码 + 抄下 10 个恢复码 + 输验证码。
    #   T0.8c 整栈实测，脚本化操作、恢复码还是复制而非手抄，`/2fa/confirm` 就用掉
    #   了 113 秒 —— 紧贴 120 秒上限。真人必然超时，而超时后拿到的错误是
    #   「令牌无效」，此刻他正盯着验证码输入框：**现象指向验证码，原因在两步之前**。
    #
    # ⚠️ 只放宽注册那一段。日常登录那条一起延长纯属无谓放宽（没有任何收益，
    # 却把「密码已验、第二因子未验」的窗口整体拉长 5 倍）。
    #
    # ⚠️ `gt=0` 不是装饰：设成 0 或负数时令牌**签发即过期**，ADMIN 永远走不完
    # spec §54 强制的 2FA 注册，**全部新管理员被锁在门外**，而症状是「密码明明
    # 对却一直说令牌无效」。这里让进程直接起不来 —— 与 compose 里密码留空直接
    # 报错停住、签名密钥缺失时不临时生成，是同一种 fail-closed。
    pending_token_ttl_seconds: int = Field(default=120, gt=0)
    enrolment_pending_token_ttl_seconds: int = Field(default=600, gt=0)

    # 账号锁定：连续失败次数与锁定时长。⚠️ 这防的是**针对某个账号**的猜测；
    # 针对来源的高频请求由 auth_rate_limit_* 挡，两者防的不是一回事。
    login_max_failures: int = 5
    login_lockout_seconds: int = 900

    # 进程内限流兜底（每来源每分钟）。主控在 nginx 的 limit_req；这一层是为了
    # 「不经 nginx 直接跑 uvicorn」时 Argon2 的调用次数仍有上界。
    auth_rate_limit_per_minute: int = 10
    auth_rate_limit_burst: int = 20

    # ⚠️ 默认 True（安全优先）。本地跑 http 时 Secure cookie 不会被发送，
    # 所以本地 .env 要显式设成 false —— 让不安全成为一次**有意识的**选择。
    session_cookie_secure: bool = True

    # 可信反向代理的 CIDR 清单（逗号分隔）。**只有直连对端落在这里时，才采信
    # `X-Forwarded-For`** —— 那个头是客户端可以随便写的。
    #
    # ⚠️ 不配的后果很具体：经 nginx 时每个请求的直连对端都是 nginx，于是
    # 按来源限流变成**全局**限流（任何人发到第 21 个认证请求，所有人都拿 429），
    # 审计里的 ip_address 也全是 nginx 的地址。见 app/core/clientip.py。
    #
    # 默认空 = 不信任任何转发头。直接跑 uvicorn 时这正好是对的；
    # compose 里已按容器网段配好。
    trusted_proxies: str = ""

    # --- 密码重置（T0.8d，同一个设计闸门 Issue #32 v5） ---------------------

    # 重置令牌的寿命。30 分钟是闸门定的：足够去邮箱把信翻出来，又不至于让一封
    # 躺在收件箱里的旧信长期是一把可用的钥匙。
    password_reset_ttl_seconds: int = Field(default=1_800, gt=0)

    # 重置链接的基址，例如 `https://billing.example.com`。**空 = 未配置**，
    # 此时信里拼不出可点的链接，投递任务会判为失败并重试（配上就自动补投）。
    #
    # ⚠️ 为什么不从请求的 Host 头推：那个头是客户端可以随便写的，而这里拼出来的
    # 是一封**发给用户、带着一把钥匙**的链接。攻击者只要在申请重置时改一个 Host，
    # 就能让受害者收到一封指向自己服务器的信 —— 受害者点进去，令牌就到了对方手上。
    # 这类漏洞有个名字叫 host header poisoning，密码重置是它最经典的落点。
    frontend_base_url: str = ""

    # --- 出站邮件（ADR-0009；仍是 T0.8d） -----------------------------------

    # **空 host = 未配置**，是一个合法状态（ADR-0009）：开发与 CI 不需要真实
    # 邮箱凭据就能跑通全流程。未配置时发送函数返回 False 并记一条 warning，
    # **不抛异常**（Invariant 1 的同一条道理：邮件服务器的事不该把业务打成 500）。
    smtp_host: str = ""
    smtp_port: int = Field(default=587, gt=0, le=65_535)
    smtp_username: str = ""
    # ⚠️ 密码所在的**文件路径**，不是密码本身 —— 与 JWT 签名密钥、加密主密钥
    # 同一条规矩（ADR-0004 第 2 节）：环境变量会进 /proc/<pid>/environ、崩溃转储，
    # 并被子进程继承。生产上由 Docker secret 挂成文件。
    smtp_password_file: str = ""
    # 发件人地址与认证用户名**分开配**：用 relay + API key 认证时两者不是一回事
    # （ADR-0009 借 rs-roof-pms 的那几条之一）。空则回退到 smtp_username。
    smtp_from: str = ""
    # ⚠️ 不能不设。SMTP 连接卡住时默认会一直等下去，worker 的那个进程就永久占着 ——
    # 积压的信一封也发不出去，而现象只是「队列不动了」。
    smtp_timeout_seconds: int = Field(default=10, gt=0)

    # --- Outbox 投递（spec §74.6、§25、§98.1） -------------------------------

    # 退避基数：第 n 次重试等 base * 2^(n-1) 秒（60s → 2m → 4m … ）。
    outbox_retry_base_seconds: int = Field(default=60, gt=0)
    # 用尽后这一行进 `FAILED` 死信，不再被恢复任务捡起。默认 8 次 ≈ 覆盖约 4 小时
    # 的持续故障。⚠️ **没有上限的重试会把一封永远发不出去的信（比如收件地址根本
    # 不存在）变成一个永久转的循环**，而且它会一直排在别的信前面。
    outbox_max_attempts: int = Field(default=8, gt=0)
    # 恢复任务一次最多领多少行。防止一次积压把 worker 淹掉。
    outbox_recovery_batch: int = Field(default=100, gt=0)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached accessor so settings are parsed once per process."""
    return Settings()
